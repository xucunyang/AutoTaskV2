# Phase1 实现记录（2026-09-29）

配套设计：`design/phases/Phase1-storage-scheduler-lease-design.md`
纲领：`design/plans/IMPLEMENTATION_PLAN_V7.md`
状态：**已完成并经 Phase0 补落码后复查**（2026-09-29），143 passed。
> 2026-09-29 复查后本文档已更新：补 §3 的第 15~18 条缺陷、补 §4/§5/§6 的对应变化。
> 复查详情见 `Phase1-storage-scheduler-lease-design.md` §6.2。

本文件记录"实际怎么做的"，设计文件记录"应该怎么做"。两者不一致的地方都写在这里，不藏。

---

## 1. 交付物

| 文件 | 行数 | 职责 |
|---|---|---|
| `orchestrator/core/store.py` | 673 | 存储事实源：DDL、`transition()` 唯一写入口、导出/重放/迁移/备份/完整性 |
| `orchestrator/core/lease.py` | 140 | 租约：acquire / renew / release / recover_expired / request_cancel |
| `orchestrator/core/enqueue.py` | 233 | 入队：CLI + inbox 目录投递，六道硬校验门 |
| `orchestrator/core/scheduler.py` | 250 | 调度：APScheduler cron + Daily 模板展开 + 补跑 + 手动 CLI |
| `orchestrator/tests/test_store.py` | 12 例 | Store 状态机与约束 |
| `orchestrator/tests/test_lease.py` | 9 例 | 租约与 fencing |
| `orchestrator/tests/test_enqueue.py` | 17 例 | 入队硬校验与 inbox 流转 |
| `orchestrator/tests/test_scheduler.py` | 14 例 | 模板展开/补跑/CLI |
| `orchestrator/tests/test_recovery.py` | 5 例 | kill -9 混沌、seq 单调、备份恢复、DB 锁退避 |

改动统计：6 笔提交，`store.py` +510/+128/+8/+64 行演进，其余为新增文件。

### 提交序列（每笔含原因说明）

| commit | 内容 | 为什么单独一笔 |
|---|---|---|
| `27da4f7` | `store.py` + `test_store.py` + `schedule.yaml` 加 `session_switch_limit` | 存储是所有后续模块的地基，必须先独立可测 |
| `909c4ed` | `lease.py` + `Store.time_params/lease_update/list_by_status/request_cancel` | 租约需要 Store 侧开专用写通道，逻辑上耦合但职责不同 |
| `8530ba5` | `enqueue.py` | 唯一外部输入面，安全门禁自成一体便于审计 |
| `a9f0211` | `scheduler.py` | 时间入口独立于队列，便于单独回滚 |
| `d54647e` | `verify_progress_json` + 备份幂等 + `integrity_check` + `test_recovery.py` | 补 V7 漏项 + 混沌演练，性质是"验证"而非"新增能力" |
| `5b25452` | `PROGRESS.json` + 设计 DoD 勾选 + 偏差记录 + 测试污染修复 | 状态同步类改动单独成笔 |

---

## 2. 关键实现决策

### 2.1 `transition()` 是唯一写入口

所有状态变更必须过 `Store.transition()`，裸 `sqlite3` 禁止散落（评审 M6）。`lease.py` / `enqueue.py` / `scheduler.py` 三个文件里 **零裸 SQL**，全部通过 Store 的公开方法。

单次写入的守卫链（任一不过即整体回滚）：

```
BEGIN IMMEDIATE
  → 查 status/version/fencing_token/session_switch_total/attempts/shard_json
  → 边合法性 is_allowed(from, to)
  → 乐观锁 version == expect_version
  → fencing == expect_fencing（给了才比）
  → 让出边四重门（见下）
  → UPDATE（version+1；fencing 仅 lease_op 时 +1）
  → INSERT events
  → INSERT metrics
COMMIT
→ 原子导出 task json + 追加 events jsonl（失败不回滚 DB）
```

### 2.2 让出边四重门（V7 §2.9）

`RUNNING → READY` 不是普通跃迁，四条全过才放行：

1. `payload.reason ∈ {context_full, steps_exhausted, timeout, cancel_requested, context_overflow}`
2. checkpoint 文件真实存在（`no checkpoint = 没收敛`）
3. `shard.long_running == true`（短任务让出 = 假拆分）
4. `session_switch_total < 5`（超限熔断，交给调用方改走 FAILED 计重试）

且 **attempts 不变**——让出是正常 session 切换，不是一次失败尝试。这条与 `RETRY → READY` 时 `attempts + 1` 严格区分开：前者是"换上下文继续"，后者是"这次做砸了"。

熔断上限 K=5 从 `config/schedule.yaml` 读，不硬编码。

### 2.3 fencing 语义

- **只有** lease 相关的写（`lease_update`）才 `fencing_token + 1`。
- 普通状态跃迁只 `version + 1`（评审 Minor2 已定版）。
- 原因：若每次跃迁都动 fencing，worker 心跳带的 fencing 会因为编排层的合法写回而失效，脑裂防护反而被自己搞坏。
- 租约过期回收后新 owner 拿到的 fencing 必然更大，旧 owner 的任何写回带旧 fencing → `StaleOwner` 直接丢弃。

### 2.4 租约过期 vs 主动让出

两者都让任务离开 RUNNING，但语义不同，实现上刻意不合并：

| | 触发 | 目标态 | attempts | 含义 |
|---|---|---|---|---|
| 租约过期 | 被动失联（worker 崩了） | FAILED | 后续 RETRY→READY 时 +1 | 真的做砸了 |
| 主动让出 | 主动 `RUNNING→READY` | READY | 不变 | 上下文满了，换个 session 继续 |

把两者混为一谈会导致"长任务因为正常让出被算成反复失败"，最终误入死信。

### 2.5 入队六道硬门（M9）

入队是系统唯一的外部输入面，不设门就是 RCE 与灌爆入口：

| 门 | 拦什么 |
|---|---|
| `<100KB` | 超大 JSON 打爆内存 |
| `task_id` 正则 `^[a-z0-9_]{1,64}$` | 注入非法标识符 |
| 路径必须落在 `artifacts/YYYY-MM-DD/` | `../` 穿越、绝对路径、盘符 |
| `cmd` 禁 shell 元字符 `\|;&\`$><\n\r` | 命令注入 |
| `business_rule.expr` 走 AST 白名单 | `__import__('os').system(...)` 之类 |
| 单实例 100 条/小时 | inbox 被灌爆 |

AST 白名单**刻意不含** `ast.Call` / `ast.Attribute` / `ast.Lambda`：任何调用或属性链都可能是代码执行。最初版本误把 `ast.Attribute` 放进白名单，测试直接抓出来了。

inbox 流转：先 `move` 到 `processing/`（占有，避免并发重复拾取）→ 处理 → `done/` 或 `error/`，**原始文件永不丢**。损坏 JSON 也进 error 而不是抛异常中断整轮扫描。

入队成功 `touch wakeup.flag`（M2），不等下一轮 cron。

### 2.6 调度只做"展开"，不做"派发"

APScheduler 的职责边界被刻意压到最小：**只把 `templates/*.yaml` 展开成 plan + tasks**。

- `ensure_plan` 用 `INSERT OR IGNORE` 幂等，双调度同 `plan_id` 时唯一键保证单实例。
- 队列派发在别处，主循环只认 `status/priority/seq`，天然实现高优插队。
- 时间触发（Daily）与事件触发（管道）完全解耦：管道走 `enqueue`，即时入队。

### 2.7 补跑的"当天必补"例外

设计原文是"超 misfire_grace 记 SKIPPED + 告警"。照字面实现会出现一个很糟的后果：

> 每天 8:00 跑日报。如果进程在 9:00 才启动，`misfire_grace_time=3600` 已超，
> 当天的日报 plan 就被跳过了——**当天日报整天消失**，而这恰恰是最该补跑的场景。

所以拆成两种情况：

- **历史日期**缺 plan 且超宽限 → `skipped_overdue` + WARN（不补陈旧任务，避免半夜补跑昨天的活）。
- **当天** plan 一律补建；若已超宽限，额外记 `catchup_late_today` 告警。

迟到要建，迟到要喊，但迟到的当天日报不能不建。

---

## 3. 过程中修掉的实际缺陷

这些是"跑起来才发现"的，不是 review 能看出来的：

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 11 个 Store 测试全挂在 `insert_task` | INSERT 占位符 22 个、列 23 个 | 补占位符 |
| 2 | 非法跃迁后查不到 REJECTED 事件 | 审计行写在会回滚的事务里 | 回滚后另开事务补记 `_record_rejected_outside` |
| 3 | `backup()` 同日第二次直接抛错 | `VACUUM INTO` 要求目标不存在 | 已存在则幂等返回 + 记 `backup_skipped_exists` |
| 4 | `backup()` 用 f-string 拼路径 | — | 改参数化 SQL |
| 5 | `business_rule.expr` 放行 `a.b.c > 1` | 白名单误含 `ast.Attribute` | 移出 `ast.Call`/`Attribute`/`Lambda` |
| 6 | 合规路径 `artifacts/2026-09-29/x.csv` 被拒 | 校验依赖 payload 里的 `date` 字段，但契约无此字段 | 改 `ARTIFACT_PATH_RE` 直接匹配日期段 |
| 7 | 补跑把当天日报跳过 | 见 §2.7 | 当天必补 |
| 8 | `--rerun` 静默推非法边 | 直接改状态绕过状态机 | 走 `transition()`，非法时报错退出码 2 |
| 9 | 混沌测试读不到子进程输出 | `readline()` 得空行但看不见原因 | 加 `_wait_line`，失败时把子进程 stderr 带进断言 |
| 10 | 混沌测试的子进程脚本 IndentationError | 模板自带缩进 + body 缩进叠加 | 改为不 dedent 拼接 |
| 11 | 备份拷到新 root 读不到数据 | 拷到了 `state.db`，实际路径是 `state/state.db` | 测试修正 |
| 12 | 每次跑测试都在生产目录建 `state/state.db` | `test_time_params_from_config` 在生产 root 上 `new Store` | 改为只读 tmp_path 下的 config |
| 13 | **仓库自带的 `templates/pipeline_task.json` 根本入不了队** | 模板缺 `status` 与 `idempotency_key`，而 Pydantic 校验发生在入队通道补齐这两个字段**之前** | 补齐顺序反转：`validate()` 先 `setdefault` 再校验；派生规则 `{plan_id}:{task_id}` 与 `scheduler.expand_template` 一致 |
| 14 | 管道任务被标成 `source=daily` | 模板没写 `source`，Pydantic 默认 `daily` | 模板显式声明 `source: pipeline`，并加断言防回退 |

第 13 条是**写完单测、全绿之后，跑真实 CLI 才暴露**的：单测里每个用例都自己构造了完整 payload，把"模板缺字段"这个真实使用路径绕过去了。已补一条回归测试直接用仓库里的模板文件入队。

第 12 条值得单独强调：**测试污染生产数据目录**这类问题不会让测试变红，只会安静地在你机器上留垃圾。已修，并已清理 `orchestrator/state/`。

### 3.1 Phase0 补落码后复查新增的 4 条

复查时发现 Phase1 自己也有"设计写了、代码没写"的问题（详见设计文档 §6.2）：

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 14 | 老库拿不到 `verify_progress_json` | **`CREATE TABLE IF NOT EXISTS` 不会给已存在的表补列** —— 上一版给 `tasks` 加的列对已有库根本不生效，启动一路绿灯，直到运行期某条查询才 `no such column` | `ADDED_COLUMNS` 声明 + `ALTER TABLE` 补列，`SCHEMA_VERSION` 升 2，补列动作记审计日志 |
| 15 | 迁移前没有备份 | `backup()` 只能手动调，`migrate()` 直接建表 | `migrate()` 先探测旧 `user_version`，确认是真迁移才先 `VACUUM`；全新库/版本已最新时不备份 |
| 16 | 锁争用只在日志里 | §5 要求"仍失败记 metrics 并告警"，此前只有 `jlog` | 每次重试记 `db_lock_retry`，耗尽记 `db_lock_exhausted` + P1 告警。**全部重试成功也不该在指标上消失**——锁争用是运维要看的信号 |
| 17 | 多 plan 时 `master.json` 互相覆盖 | 单文件全局视图 | 细节进 `plans/{plan_id}.json`，`master.json` 只做索引 |

第 14 条是本轮最值得记住的：**给既有表加字段，光改 DDL 是不够的**，必须同时声明进迁移清单。这类 bug 不会让任何测试变红——因为测试用的都是新建的库。

第 17 条是复查时才发现的：`daily` 与 `pipeline` plan 并存时，后 `rebuild_export` 的会覆盖先写的。设计原文只说"单 plan 卡 + 任务计数"，多 plan 是实现时才暴露的场景。

---

## 4. DoD 对应关系

| 设计 DoD | 对应测试 | 结论 |
|---|---|---|
| kill -9 无半写，`replay` 一致，`seq` 不回退 | `test_kill9_before_commit_leaves_no_half_write`、`test_kill9_after_commit_survives_restart`、`test_seq_never_goes_backwards` | 通过 |
| 双触发同 `idempotency_key` 只执行一次 | `test_insert_idempotent_on_key_conflict`、`test_idempotent_reenqueue_returns_already_exists` | 通过 |
| 租约过期回收 + 旧 owner 写被拒 | `test_recover_expired_running_to_failed`、`test_renew_by_old_owner_is_stale` | 通过 |
| 让出边四场景 + attempts 不变 | `test_yield_ok_with_reason_and_checkpoint`、`test_yield_rejects`、`test_yield_fused_after_limit` | 通过 |
| 错过补跑、超期跳过 + 告警 | `test_catchup_skips_overdue_with_alert`、`test_catchup_late_today_is_created_with_warn` | 通过 |
| FIFO + 高优插队 | `test_priority_override_and_fifo_order` | 通过 |
| `request_cancel` 置旗 → 60s 内让出落 checkpoint | 置旗与审计行已实现；**让出侧属 Phase2 执行器** | 部分（已标注，不算完成） |

### 4.1 上游规则收紧后是否被打破

Phase0 把 `depends_on` 门禁落进 `store` 后，日报的 `t1→t2→t3` 三级链第一次真正被门禁管住。`tests/test_phase1_recheck.py` 验证：新库展开的日报确实只能按序推进（t2/t3 被 `deps_unsatisfied` 挡住），模板的 `depends_on` 真的进了 shard（否则门禁形同虚设）。这是"上游改动打破下游"的那类回归网，以后每轮上游收紧都该补一条。

---

## 5. 与设计的偏差（6 处）

1. `Store` 增开 `list_plans / list_by_status / lease_update / request_cancel / update_verify_progress / integrity_check`，目的是让 lease 与 scheduler 保持零裸 SQL。
2. 时间参数统一从 `Store.time_params` 读 `config/schedule.yaml`；测试传 `tmp_path` 时回退设计默认值，不回退到硬编码常量。
3. `backup()` 当日已存在则幂等返回，不抛错。
4. `enqueue` 路径白名单用 `ARTIFACT_PATH_RE` 正则，不依赖 payload 的 `date` 字段。
5. 速率限制落 `events/enqueue_log.jsonl`（跨进程可见），上限 100 条/小时/实例。
6. `scheduler` CLI 新增 `--root`，用于多实例与测试隔离。

另有 1 处**补漏**：`tasks.verify_progress_json` 在 V7 已批准但 Phase1 设计与初版 DDL 都漏了，本次补上（含 `update_verify_progress()`）。它是 Phase3 "VERIFYING 只重跑未通过 rule" 的持久化落点，缺列会让语义退回全量重验。

## 5.1 明确划到 Phase2 的三条

复查时逐条确认，以下设计条目**本阶段不做**而不是漏做——它们都需要主循环/执行器在场：

| 条目 | 为什么不属于 Phase1 | 现状 |
|---|---|---|
| §3 心跳线程（每 30s 续 120s） | 谁周期调用 `renew()` 是执行器的活 | `lease.renew()` 已实现且有测试，**无调用方** |
| §3 回收后 `FAILED→DEAD_LETTER` 升级 + P0 告警 | 超 `max_attempts` 判定属于编排策略 | 租约过期会正确回收成 FAILED，**停在那里等 Phase2 接手** |
| §2.1 `RETRY→READY` 指数退避 `60*2^attempts` | 退避由主循环轮询节奏实现 | 跃迁本身已实现，退避节奏在 Phase2 |

这三条如果不说清，下一个人会当成 bug 去"修"，或者以为已经实现了。

---

## 6. 已知遗留（不阻塞 Phase1）

| 项 | 影响 | 建议 |
|---|---|---|
| `.gitignore` 未覆盖 `state/ logs/ events/ inbox/ backup/ artifacts/ wakeup.flag` | 跑一次真实调度就会把这些目录带进 `git status` | Phase2 前补一条运行时目录忽略规则（**待你确认后再改**） |
| `opencode_ctx.py` 在仓库根目录 | 是一次性探查脚本，与系统无关 | 建议删除或移出仓库 |
| `master.json` 只记单 plan 的全局视图 | **已修**（复查轮）：多 plan 并存时后写的覆盖先写的 | 细节进 `plans/{plan_id}.json`，master 只做索引 |
| 执行器未实现 | 让出边、checkpoint、租约续租目前只有写入侧，没有自动持有者 | Phase2（已列明边界，见 §5.1） |
| `metrics` 表已建但只有 `transition_latency` | Phase3 观测指标未接 | Phase3（锁争用类指标已补） |

---

## 7. 复现方式

```powershell
cd E:\code\AutoTaskByMuseSpark1.3\orchestrator
python -m pytest tests/ -q          # 99 passed
```

端到端手工验证（会写 `state/ logs/ events/`，属于运行时数据）：

```powershell
python -m core.scheduler --dry-run                    # 列 cron
python -m core.scheduler --date 2026-09-29            # 展开当天日报 3 个任务
python -m core.enqueue --file templates\pipeline_task.json --priority 0
python -m core.enqueue --file templates\pipeline_task.json --priority 0   # 应输出 already_exists
```

实测输出（验证过，队列排序与幂等都符合预期）：

```
{"status": "inserted",     "plan_id": "pipeline", "task_id": "pipe_example", "priority": 0}
{"status": "already_exists","plan_id": "pipeline", "task_id": "pipe_example", "priority": 0}

--- READY order (priority ASC, seq ASC) ---
prio=0   seq=4   pipe_example      ← 管道高优插到 Daily 之前
prio=10  seq=1   t1_collect        ← 同优先级严格按 seq FIFO
prio=10  seq=2   t2_clean
prio=10  seq=3   t3_report
--- integrity --- {'integrity': 'ok', 'tasks': 4, 'events': 8, 'seq_max': 4}
```

注：`--root` 指向的目录必须自带 `config/ templates/ schemas/`，否则 `load_schedule` 找不到配置会直接 `FileNotFoundError`（不是友好报错，Phase2 可考虑补默认值）。
