# Phase1 存储/调度/租约设计（V7）

状态：已按V7对齐，待用户评审，前置依赖 Phase0契约冻结。
对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`；修订见文末§8。

## 1. 目标

- 单机SQLite做到跨表事务+崩溃可恢复+重复触发幂等+双实例不脑裂。
- 任务列表为持久化FIFO队列：同优先级`seq`先入先出，Daily低优（10），管道高优（0），高优排队等待，当前任务完成后自然衔接执行。
- 调度丢触发可补跑，错过策略明确，手动重跑/试跑有入口；管道输入有独立入口（CLI + inbox目录），不依赖cron。

## 2. 存储设计 `core/store.py`

### 2.1 DDL（`state.db`，`journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000`）

```sql
PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;
CREATE TABLE schema_version(version INT PRIMARY KEY, applied_at TEXT); -- migrate()按user_version递增
CREATE TABLE plans(plan_id TEXT PRIMARY KEY, template TEXT, status TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE tasks(
  plan_id TEXT NOT NULL, task_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','BLOCKED','READY','RUNNING','SUBMITTED','VERIFYING','WAITING_APPROVAL','RETRY','FAILED','DONE','DEAD_LETTER','CANCELLED','SKIPPED','SKIPPED_CACHED')),
  version INT NOT NULL DEFAULT 0, fencing_token INT NOT NULL DEFAULT 0,
  owner TEXT, lease_until TEXT,
  idempotency_key TEXT UNIQUE NOT NULL, inputs_hash TEXT NOT NULL,
  last_success_hash TEXT,
  attempts INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 3,
  session_switch_total INT NOT NULL DEFAULT 0,  -- V7: 让出次数，>K熔断
  priority INT NOT NULL DEFAULT 10 CHECK(priority BETWEEN 0 AND 10),
  source TEXT NOT NULL DEFAULT 'daily' CHECK(source IN ('daily','pipeline')),
  seq INTEGER PRIMARY KEY AUTOINCREMENT, -- 全局FIFO，禁max+1（评审M1）
  cancel_requested INT NOT NULL DEFAULT 0,
  privacy TEXT NOT NULL DEFAULT 'public', complexity TEXT NOT NULL DEFAULT 'simple',
  needs_web INT NOT NULL DEFAULT 0 CHECK(needs_web IN (0,1)),  -- V7
  freshness TEXT NOT NULL DEFAULT 'none' CHECK(freshness IN ('none','recent','strict')),  -- V7
  enqueued_at TEXT NOT NULL,
  shard_json TEXT NOT NULL, updated_at TEXT NOT NULL,  -- shard_json含slice_rationale/key_questions
  verify_progress_json TEXT,  -- V7§3：已通过rule_id账本，VERIFYING只重跑未通过的rule
  UNIQUE(plan_id, task_id));
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  plan_id TEXT NOT NULL, task_id TEXT NOT NULL, from_s TEXT NOT NULL, to_s TEXT NOT NULL,
  run_id TEXT NOT NULL, agent TEXT NOT NULL, payload TEXT);  -- payload含让出reason/checkpoint引用
CREATE TABLE metrics(ts TEXT NOT NULL, plan_id TEXT NOT NULL, task_id TEXT NOT NULL,
  name TEXT NOT NULL, value REAL NOT NULL, run_id TEXT NOT NULL,
  span TEXT, duration_ms REAL);
CREATE INDEX idx_tasks_sched ON tasks(status, priority, seq);
CREATE INDEX idx_tasks_plan ON tasks(plan_id,status);
CREATE INDEX idx_events_plan ON events(plan_id, seq);
```
单表队列（评审B1）：删`queue_items`可变表，`inbox/done|error`即ingress log。`fencing`只在lease相关跃迁（acquire/renew/revoke）+1，普通跃迁只+version（评审Minor2）。

派发排序（FIFO+优先级，唯一查询）：
```sql
SELECT * FROM tasks
 WHERE status='READY'
 ORDER BY priority ASC, seq ASC
 LIMIT :n;
```
同`priority`按`seq`先入先出；`priority`越小越先。`seq`由`INSERT`时AUTOINCREMENT分配，重启不回退。

### 2.2 写路径（唯一入口 `transition()`）

```python
def transition(plan_id, task_id, to_s, *, agent, run_id,
               expect_version, expect_fencing=None, handoff=None, payload=None):
    with db:  # BEGIN IMMEDIATE
        s,v,f,switch = SELECT status,version,fencing_token,session_switch_total ...
        if to_s not in ALLOWED[s]: raise IllegalTransition + 记REJECTED
        if v != expect_version: raise Conflict("乐观锁冲突")
        if expect_fencing is not None and f != expect_fencing: raise StaleOwner("旧owner")
        if to_s == 'READY' and s == 'RUNNING':  # 让出边（V7）
            assert payload.reason in {'context_full','steps_exhausted','timeout','cancel_requested','context_overflow'}
            assert checkpoint_exists(plan_id, task_id)          # 无checkpoint=丢现场，拒
            if not task.long_running:                           # 短任务拒让出→走失败重试+自动置True自学习
                raise IllegalTransition("short_task_no_yield")
            if switch >= SESSION_SWITCH_LIMIT:                  # 熔断K=5（可配）
                raise IllegalTransition("session_switch熔断")   # 上层改走FAILED
            session_switch_total += 1                           # attempts不变
        UPDATE tasks SET status=to_s, version=v+1, ...  # fencing仅lease相关跃迁(acquire/renew/revoke)+1，普通跃迁不动(评审Minor2)
        INSERT INTO events(...) VALUES (now_utc, from_s=s, to_s, ...)
        INSERT/UPDATE metrics(transition_latency等)
    # 事务外：原子导出 + 追加jsonl（失败可重建，不影响DB）
    export_task_json(plan_id, task_id)  # tmp+fsync+rename
    append events/YYYY-MM-DD.jsonl
```

- 导出：`state/export/master.json`（计划汇总+任务索引）+ `tasks/{id}.json`（分片只读）。
- 恢复：`rebuild_export(plan_id)`从DB全量重建导出；`replay(plan_id)`从events重放校验快照一致。

### 2.3 读取路径

- Agent只读导出JSON或`get_task()`视图，不直接扫目录。
- 主循环每轮只读：计划摘要+READY/SUBMITTED列表+最近50条events。

## 2.3b Store抽象与时间参数（评审M6/B2收敛）

- `core/store.py::Store`：单写串行（写队列+`BEGIN IMMEDIATE`重试3次指数退避）+读并发；接口`transition/get_task/list_ready_ordered/insert_task/rebuild_export/replay/migrate/backup`，为切PG留抽象层，禁止裸`sqlite3`散落。
- 时间参数集中`config/schedule.yaml`：`lease_ttl=120s, heartbeat=30s, cancel_grace=30s(高优)/60s(默认), poll_normal=10s, poll_fast=2s, aging两档(600s→5, 1200s→3, 上限3, V7§10定版), session_switch_limit=5`。
- 备份/迁移：`ensure_plan`前`VACUUM INTO backup/state-YYYYMMDD.db`；`WAL`坏走`backup+events jsonl重放`；`schema_version`表+migrate演练。

## 2.4 队列入口 `core/enqueue.py + inbox/`

- 管道任务不走cron，走队列入口，二者最终都落`tasks`表统一排序：
  - `CLI: python -m core.enqueue --file pipeline_task.json --priority 0`，校验Pydantic后`INSERT tasks(PENDING)`同一事务（单表制，评审B1），`seq`由AUTOINCREMENT分配，`idempotency_key`冲突则`already_exists`返回。
  - `enqueue`硬校验：文件`<100KB`、`task_id ^[a-z0-9_]{1-64}$`、产物路径必须`artifacts/{date}/`内、`acceptance.cmd`白名单前置、单小时入队上限100（评审M9）、`needs_web=true`时`freshness`必须∈{recent,strict}。入队成功后`touch orchestrator/wakeup.flag`唤醒主循环（评审M2）。
  - `inbox/*.json`目录投递：调度器每5s扫`inbox/`，合法文件原子移入`inbox/processing/`再入队，成功进`inbox/done/`，失败进`inbox/error/`并告警。崩溃重启重扫不丢。
  - 预留HTTP hook（飞书IngressProvider，见V7§12.8，不在本期实现）。
- Daily任务：调度器按模板批量入队，`priority=10, source=daily`，INSERT顺序即模板顺序，`seq` AUTOINCREMENT天然FIFO。
- 幂等：`idempotency_key` UNIQUE，管道重发同一文件不会重复入队。`business_rule`求值只允`Compare/BoolOp/Name/Constant`四种AST节点（评审M9）。

## 3. 租约设计 `core/lease.py`

- 字段：`owner(实例ID) + lease_until(UTC ISO) + fencing_token`。
- `acquire(plan,task,owner,ttl=120)`：仅当`READY`或租约过期才可获取，成功`fencing+1`。
- 心跳：子Agent独立线程`每30s续120s`（`renew`需带回`fencing`比对）。
- 回收：主循环`recover_expired_leases()`扫`RUNNING && lease_until<now` → `FAILED`（attempts+1在后续`RETRY→READY`边），超`max`→`DEAD_LETTER` + P0告警。**与让出区分**：租约过期=被动失联（计attempts），主动让出=正常session切换（不计attempts，见§2.2）。
- 释放：`release()`仅当`owner+fencing`匹配才清`owner`，`SUBMITTED`后由系统接管不再需要owner。
- 脑裂防护：任何写回带`expect_fencing`，过期owner的写直接`StaleOwner`丢弃 + 记事件。
- 取消协作：`request_cancel(plan,task,reason)`置`cancel_requested=1`（需version比对+记事件`CANCEL_REQUESTED`）；执行器心跳/步间检查该旗标，见旗标即保存checkpoint后主动让出（`RUNNING→READY(reason=cancel_requested)`）。宽限60s仍不让出则按失联强制回收（fencing+1）。

## 4. 调度设计 `core/scheduler.py`

- 基于APScheduler `BackgroundScheduler + SQLAlchemyJobStore(sqlite:///scheduler.db)`，`coalesce=True, max_instances=1, misfire_grace_time=3600`。
- `config/schedule.yaml`：`jobs(cron/timezone/template/catchup)` + `workers/governor`（V5两档配额）+ `lease/poll/aging/session_switch_limit`（时间参数收敛，评审B2，不散落魔法数字）。
- 生成：`ensure_plan(date)`: `plan_id=daily_report_{date}`，`INSERT OR IGNORE`，存在直接skip（幂等）。
- 补跑：启动`recover_missed()`查过去N天缺失plan按`catchup=true`补建，超`misfire_grace`记`SKIPPED+告警`。
- 手动：CLI `python -m core.scheduler --date=... --dry-run/--rerun=t2_clean/--catchup`。
- 时区：内部UTC，cron按`Asia/Shanghai`解析，DST切换以APScheduler为准并记日志。

## 4.1 调度与队列的关系

- 时间触发（Daily）与事件触发（管道）解耦：APScheduler只负责Daily模板展开；管道由`enqueue/inbox`即时入队，无需等下一轮cron。
- 主循环每轮先看队列排序结果，不区分来源，只看`priority/seq`，天然实现高优插队。

## 5. 异常矩阵

| 场景 | 行为 |
|---|---|
| 进程kill -9在COMMIT前 | 回滚，下轮重试，无半写 |
| COMMIT后导出前崩溃 | 下轮`rebuild_export`补齐 |
| 双调度同plan_id | `INSERT OR IGNORE` + 唯一键保证单实例 |
| 租约过期仍在跑 | `RUNNING→FAILED`回收（attempts+1在`RETRY→READY`），旧owner写被fencing拒绝，新owner重跑 |
| 子Agent主动让出（session切换） | `RUNNING→READY`，attempts不变，checkpoint断言+熔断K=5 |
| 让出超熔断K次 | 拒让出改走FAILED（计attempts，走正常重试/死信） |
| DB locked | `busy_timeout`重试3次 + 指数退避，仍失败记metrics并告警 |

## 6. DoD

实现现状（2026-09-29，pytest 96 passed）：

- [x] kill -9混沌后重启可恢复，无半写（`replay`一致），队列`seq`不回退不丢。`tests/test_recovery.py`：子进程停在未提交事务被kill→重启查无ghost行且`integrity_check=ok`且`replay.ok`；COMMIT后被kill→行还在且`rebuild_export`可补齐导出。
- [x] 双触发同`idempotency_key`只执行一次（含管道重发）。`test_insert_idempotent_on_key_conflict` + `test_idempotent_reenqueue_returns_already_exists`。
- [x] 租约过期回收+旧owner写被拒单测通过。`test_recover_expired_running_to_failed` / `test_renew_by_old_owner_is_stale`（worker-2抢租后worker-1心跳StaleOwner）。
- [x] 让出边单测：带reason+checkpoint通过；无checkpoint拒；熔断超限拒；attempts不变。`test_yield_ok_with_reason_and_checkpoint` / `test_yield_rejects` / `test_yield_fused_after_limit`。
- [x] 错过1天补跑、超期跳过+告警验证通过。`test_catchup_skips_overdue_with_alert`；当天plan一律补建并记`catchup_late_today`（当天日报不能因启动晚于8点整天消失）。
- [x] FIFO验证：同优先级按入队顺序派发；管道高优插队到Daily之前。`test_priority_override_and_fifo_order`。
- [x] `request_cancel`置旗后执行器60s内让出并落checkpoint。置旗与审计行已实现（`test_request_cancel_sets_flag_and_audit_row`）；让出侧由store的让出边断言兜住，`reason=cancel_requested`在`YIELD_REASONS`内——执行器实现属Phase2。

### 6.1 实现与设计的偏差（已落地并说明）

- `Store`额外提供 `list_plans/list_plan_tasks/list_by_status/lease_update/request_cancel/update_verify_progress/integrity_check/record_metric/daily_summary/cancel_cascade/unsatisfied_dependencies/alert`，让lease与scheduler保持零裸SQL（评审M6）。

## 6.2 Phase0 补落码后的复查（2026-09-29）

Phase0把设计里只写在文档的规则真正落进`store.transition()`之后，回头复查本阶段，发现5条设计条目未落代码，已全部补齐（`22f47c2`）：

| 设计条目 | 补法 | 测试 |
|---|---|---|
| §2.1 DDL新增列对老库无效 | `ADDED_COLUMNS` + `ALTER TABLE`补列，`SCHEMA_VERSION`升2 | `test_migration_adds_missing_column_to_legacy_db` |
| §2.3b 迁移前备份 | `migrate()`探测旧版本，真迁移才先`VACUUM` | `test_real_migration_takes_backup_first` |
| §5 锁失败记metrics+告警 | `record_metric` + `db_lock_retry/db_lock_exhausted` + P1 | `test_db_locked_retries_then_succeeds` |
| §2.2 master单文件覆盖 | `plans/{plan_id}.json` + `master.json`只做索引 | `test_replay_and_rebuild_and_backup` |
| §4 DST切换记日志 | `_log_timezone_resolution`记下一次触发时刻 | `test_build_scheduler_logs_timezone_resolution` |

第一条是本轮最值得记的：`CREATE TABLE IF NOT EXISTS` **不会给已存在的表补列**。上一版给`tasks`加了`verify_progress_json`，对已有库完全不生效——启动一路绿灯，直到运行期某条查询才 `no such column`。凡是给既有表加字段，都必须同时声明进`ADDED_COLUMNS`。

另新增`tests/test_phase1_recheck.py`验证"上游规则收紧后下游是否被打破"：日报`t1→t2→t3`三级链在新的`depends_on`门禁下确实按序推进。

### 6.2.1 明确划到Phase2的条目

§3 的**心跳线程**（子Agent独立线程每30s续120s）与 **FAILED→DEAD_LETTER升级**（超max_attempts进死信+P0告警）本阶段**未实现**：`lease.renew()`与`recover_expired_leases()`已提供且有测试，但谁来周期调用、以及回收后谁做死信升级，需要主循环和执行器都在场，属Phase2编排层。现状是：任务会因租约过期被正确回收成FAILED，但停在那里等Phase2接手升级。

§2.3的"RETRY指数退避 60*2^attempts"同理——退避由主循环的轮询节奏实现，Phase2。

## 7. V6→V7 变更清单（2026-09-28，本轮，待评审）

- §2.1：DDL加`session_switch_total`/`needs_web`/`freshness`列；shard_json承载slice_rationale/key_questions。
- §2.2：`transition()`加让出边校验（reason五值+checkpoint断言+long_running前置+熔断K=5，attempts不变）。
- §2.3b：时间参数加`session_switch_limit=5`。
- §2.4：enqueue加`needs_web=true`时freshness必填校验；飞书改口（预留IngressProvider接口，V7§12.8）。
- §3：租约回收与主动让出明确区分（被动失联计attempts vs 主动切换不计）。
- §5：异常矩阵加让出两行。
- 本轮（动态窗口+兜底reason）：§2.2让出reason四值→五值（+context_overflow）+long_running前置校验（短任务拒让出→失败重试+自学习）。
- 前轮（V6）保留：单表制、AUTOINCREMENT、简化抢占模型、wakeup.flag、两档老化、ttl=120/心跳30s续120s。
