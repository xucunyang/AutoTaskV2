# 落地实施方案总纲领 第2版（IMPLEMENTATION PLAN V2）

> 延续 `IMPLEMENTATION_PLAN.md`，为当前唯一执行依据。V1 + `design/phases/00-Phase4` + `design/phases/REVIEW-architect.md` + 网关/耗时/审计三批增补全部合入本版。
> 主题：文件/DB为唯一事实源（真相只存磁盘，聊天记录不算数），调度器独立于Agent（管时间的程序和干活的模型分离），主Agent只编排（只派活不干活），子Agent无状态执行（每次只看任务卡、干完即焚），所有状态可恢复、可幂等（重复执行结果不变）、可校验（无验收不算完）。
> 技术栈：Python + SQLite的WAL模式（预写日志，一种崩溃不丢的写入方式）+ APScheduler（Python定时任务库），生产级直达单机，预留切PostgreSQL路径。

## 1. 目标与非目标

目标：
- Daily任务定时定点执行，错过可补跑，重复触发无副作用。
- 任务列表是持久化FIFO队列（先入先出、落盘保存的排队结构）：同优先级按入队顺序执行，Daily低优（priority=10），管道输入高优（priority=0..4），高优可抢占（插队并中断）低优。
- 主层Agent支持并发（同时跑多个子任务省时间，默认4 workers线程池+2验证workers）。
- 长任务可中断可恢复（含被抢占），Agent失忆/重启/崩溃后从磁盘重建。
- 上下文可控：主Agent不被长任务占满，子Agent只见任务卡。
- 轻量网关：本地Ollama（本机跑的小模型服务）/在线API（远端大模型接口）可插拔路由，隐私判断默认关闭只留扩展接口。
- 全环节计时+汇总报告：哪个失败、哪个耗时、哪个节点阻塞，一份报告定位。
- 闭环校验+可观测：自测→独立验收→终检；全链路run_id（一次运行的全局编号）+事件重放+分级告警+日报。

非目标：
- 分布式多机调度不做（单编排实例+DB行锁防双主，预留围栏接口）。
- 重型LLM网关/计费中台不做（不管钱和key池）；只做轻路由+用量记录+熔断（失败多了自动断开一段时间）。大白话：只装电表+保险丝。
- 通用DAG可视化UI不做（DAG是有向无环图，即任务依赖关系图，不做图形界面，只出markdown+JSON报告）。

## 2. 核心原则（不可违背）

1. 上下文不是状态：每次先读`PROGRESS.json`+`state.db`恢复，不依赖聊天记忆。
2. 调度器是确定性程序（到点必触发的死板程序）：cron/APScheduler管时间，Agent只被触发；管道走队列入口不走时间。
3. 主Agent只拆分/派发/校验/推进，不执行长任务。
4. 子Agent无状态：输入任务卡，输出产物+SUBMITTED+handoff（交接摘要：干了什么、产物在哪、测过没），干完销毁。
5. 所有写原子（要么全成功要么全回滚）+幂等+带锁+带围栏token fencing_token（单调递增的防脑裂号码牌，旧主人写回即拒）。
6. 主文件是汇总视图，DB行+任务分片是事实源，事件表是WAL（只追加的操作日志，可重放）。
7. 单表队列：只以`tasks`为队列，不设可变状态的第二张队列表；同优先级按`seq`（全局自增序号，AUTOINCREMENT即数据库自动发号）先入先出。
8. 抢占统一出口：`RUNNING→PREEMPTED→READY/RETRY`，抢占不加attempts（尝试次数），过期才加；可抢占必须幂等。
9. 网关可插拔：Provider（模型供应商插件）与RoutePolicy（路由策略插件）配置链编排；隐私开关默认关。
10. 计时先行：7处埋点（见§8），阈值进配置不用改代码。

## 3. 事实源与目录

```
SQLite state.db(WAL) ← 唯一写源：plans/tasks/events/metrics/schema_version
  └─ 提交后原子导出 ↓（tmp临时文件+fsync刷盘+rename改名，保证半写不可见）
state/export/master.json + tasks/*.json ← Agent只读视图（坏了可重建）
events/YYYY-MM-DD.jsonl ← DB事件按天导出（审计双保险，按上海自然日切分）
logs/structured.jsonl ← 运行日志（非状态）
PROGRESS.json ← 工程进度指针（防忘顶层）
state/checkpoints/{task}.json ← 断点续跑（做到哪的书签）
artifacts/{date}/{task}.* + manifest.json ← 产物+清单（路径/哈希sha256/大小/行数/preview前5行）
inbox/*.json → inbox/done|error ← 管道投递箱（文件即 ingress凭据）
reports/{plan}.summary.md+json + reports/YYYY-MM-DD.md ← 汇总+日报
backup/state-YYYYMMDD.db ← 每日备份（VACUUM INTO拷贝）
```

```
orchestrator/
  config/schedule.yaml, gateway.yaml, report.yaml
  templates/daily_plan.yaml, pipeline_task.json, task_card.j2, daily_report.md.j2
  schemas/models.py(Pydantic数据模型，Python参数校验库), transitions.py, acceptance_*.yaml, summary.schema.json
  core/store.py, lease.py, enqueue.py, scheduler.py, orchestrator.py, executor.py
  core/gateway.py, providers/{base,ollama,online_stub}.py, routing/{chain,privacy_guard,latency_guard,capability_match}.py
  core/privacy.py, timing.py, verifier.py, notifier.py, report.py, utils.py
  tests/test_*.py  scripts/smoke_daily.py scripts/chaos_*.py
```

## 4. ID与术语规范

- `plan_id={template}_{YYYY-MM-DD}`，如`daily_report_2026-09-27`；DB存UTC时间（协调世界时），展示转Asia/Shanghai。
- `idempotency_key（幂等键，去重身份证）={plan}/{task}/{inputs_hash全hash}`，UNIQUE（唯一约束），重发直接返回。
- `run_id={plan}_{uuid7}`全链路透传；跨plan抢占另记`correlation`关联。
- `seq`由AUTOINCREMENT分配，重启不回退；排序唯一查询`ORDER BY priority ASC, seq ASC`（优先级越小越先，同级先到先得）。
- `priority（优先级）0..10枚举`：0..4管道高优，5普通，10 Daily；`source（来源）daily|pipeline`；`preemptible（可被抢否）`要求`idempotent（幂等否）`同时为真。
- `preempt_count（被抢次数）>3`免死一次；`last_success_hash（上次成功输入哈希）`判缓存命中；`cancel_requested（取消请求旗）`配`CANCEL_REQUESTED`事件审计。
- `privacy（隐私级）public|internal|secret` + `complexity（复杂度）simple|medium|complex`为网关输入；隐私默认关闭。
- `inputs_hash=sha256(排序后输入清单)`；命中+产物不变即`SKIPPED_CACHED`跳过执行省token（大模型计费单位）。

## 5. 状态机（ALLOWED白名单，代码唯一来源）

```
PENDING→BLOCKED/READY；BLOCKED→READY（依赖全DONE，strict开关决定SKIPPED*是否视同满足）
READY→RUNNING/SKIPPED/SKIPPED_CACHED/CANCELLED
RUNNING→SUBMITTED/FAILED/CANCELLED/PREEMPTED（抢占， 仅orchestrator系统跃迁）
PREEMPTED→READY/CANCELLED（attempts不变，从checkpoint续跑）
SUBMITTED→VERIFYING（仅系统占位）；VERIFYING→DONE/RETRY/FAILED/WAITING_APPROVAL（人工审批，SLA超时4h升级P0最高警）
RETRY→READY（attempts+1，指数退避60*2^attempts秒）；FAILED→RETRY/DEAD_LETTER（死信队列，超max_attempts=3进人工池）
终态（不可再写）：DONE/DEAD_LETTER/CANCELLED/SKIPPED/SKIPPED_CACHED
```

规则：`DONE`无出边；子Agent仅`RUNNING→SUBMITTED/FAILED`；非法跃迁记REJECTED+P1告警；`CANCELLED`向下级联下游；`RETRY/DEAD_LETTER`必须带reason+last_error。

## 6. 存储/调度/租约/队列入口（Phase1）

- DDL见设计Phase1（CHECK约束+外键意图+索引`idx_tasks_sched(status,priority,seq)`）；`schema_version`表+`migrate()`按`PRAGMA user_version`递增；备份见§3。
- `Store`类（存储抽象层）：单写串行+`BEGIN IMMEDIATE（SQLite立即加写锁）`重试3次指数退避+读并发；接口`transition/get_task/list_ready_ordered/insert_task/rebuild_export/replay/migrate/backup`，为切PG留抽象，禁裸`sqlite3`散落。
- 写路径：`BEGIN→查status/version/fencing→校验ALLOWED→比对version(乐观锁，即先查后比对版本号防并发覆盖)→比对fencing(防脑裂，即旧主人号码对不上就拒)→UPDATE(version+1，lease相关才fencing+1)+INSERT events/metrics→COMMIT→事务外export+append jsonl`。
- 租约 lease（限时持有权）：`owner+lease_until+fencing`；`acquire`仅READY或过期可拿；心跳30s续120s租；`recover`扫`RUNNING且过期→PREEMPTED(reason=lease_expired，attempts+1)`；`release`须owner+fencing匹配；`request_cancel`置旗+事件，宽限30s高优/60s默认，不让出`force_revoke(fencing+1续跑)`。
- 时间参数集中`schedule.yaml`：`lease_ttl120s/heartbeat30s/cancel_grace30或60s/poll_normal10s/poll_fast2s/aging1800s/preempt_after10s`。
- 调度：APScheduler+SQLAlchemyJobStore（持久化任务仓库，重启不丢）+`coalesce(合并错过)`+`max_instances1`+`misfire_grace3600s(错过宽限)`；`ensure_plan`按`INSERT OR IGNORE`幂等；`recover_missed`补过去N天；CLI支持`--date/--dry-run(试跑)/--rerun/--catchup(补跑)`。
- 队列入口：`enqueue CLI`+`inbox/`5s扫描（合法移processing→入队→done，非法→error+告警）；硬限制文件<100KB、`task_id`正则、路径锁`artifacts/{date}/`、cmd白名单前置、单小时100上限；`business_rule`只允4种AST节点（抽象语法树白名单，对表达式做安全检查）。

## 7. 编排/执行/抢占/防饿死/网关（Phase2，主层并发所在）

主循环（跨plan全局，单实例+优雅停机）：
```
recover_expired→ingest_inbox(写后touch wakeup.flag唤醒)→refresh_ready(全plan，必须在抢占前)→apply_aging(老化提升10→5→3)→maybe_preempt(仅可派发高优)→dispatch_split→verify_async→export→wait_wakeup
```
- 并发模型：主循环1线程调度；`spawn_sub_agent`线程池`max_workers=4`并行执行；`verifier_pool=2`异步验收；`READY→RUNNING`占位防重派；`Store`串行写保证线程安全。加workers改配置一行。
- `dispatch_split`两阶段：高优槽N-1查`priority≤4`，低优预留1槽查`priority≥5`（低优等待>5min才强制留）；段内仍`ORDER BY priority,seq`；`cache_hit`走`SKIPPED_CACHED`。
- 抢占：`peek`须READY且依赖满足（BLOCKED高优只加速其上游，不抢）；`pick_victim`须`preemptible&&idempotent`、排除高优依赖链、`preempt_count≤3`、选priority最大seq最大；保留原seq回队；高优SLA p95（95分位延迟）<30s超时P0；端到端p95<15s/p99<30s；`wakeup.flag`事件唤醒替代纯sleep。
- 防饿死：老化多档+预留槽+`preempt_count` boost；积压>50/最老>1h P1告警。
- 子执行：读卡→终态退出→acquire→心跳→load_checkpoint→`gateway.chat`→步间查cancel（每tool后+5s双条件）→原子写产物+manifest→自测轻量→SUBMITTED+handoff（Pydantic校验）；`read_range`分页读大文件；>200KB强制preview；预算三限超80%存档退出。
- 网关（轻量可插拔）：`Provider{chat,health}`（Ollama本地/在线OpenAI兼容即统一HTTP接口规范）+`RoutePolicy{decide}`链`privacy_guard→latency_guard→capability_match→cost_saver`，`gateway.yaml`编排；能力`3B→simple/4B→simple/7B→medium/complex走在线`；**隐私默认关闭**：`privacy.enabled=false`时跳过扫描与脱敏，只留`classify/redact/PrivacyGuard`接口，后续改true+加回链首即开；本地`max_concurrency2/slo_p50_8s`，队列>4/连败3熔断5min半开恢复，simple20s/medium60s超时切换；高优公开直走在线错峰；用量记metrics。

## 8. 校验/观测/计时/报告（Phase3）

- 验证器异步：主循环只占位，`verify_pool`执行；`file_exists`越界拒、`row_count`流式数、`python_test`白名单+`shell=False(不走系统壳防注入)`+timeout+输出截断5k+flaky重试2次不计attempts、`json_schema`校验、`business_rule`受限求值；`final_consistency`逐plan六项全过才DONE。
- 计时`timing.span()`（计时上下文，monotonic单调时钟防回拨）7处：`queue_wait/dispatch_wait/execute(llm/tool/self_test子span)/verify/preempt_overhead/export_write`→`metrics(span,duration_ms)`；阻塞规则进`report.yaml`（queue p95>60s判队列阻塞等）。
- 日志追踪：`structured.jsonl{ts/level/run_id/plan/task/msg}`+`events`+`metrics.prom(Prometheus文本指标，供Grafana看板)`；队列看板`READY分组积压/最老等待/高优p95/抢占/老化`。
- 告警`AlertPolicy（去重策略）{dedup5min/限频}`同task+reason合并；P0仅死信/不一致/高优SLA超时，P1失败重试/租约回收/每次抢占/积压，P2日报聚合。
- 报告：plan终态30s内`reports/{plan}.summary.md+json`（失败置顶/耗时Top5/阻塞点/甘特ASCII/网关用量），18:00合日报；阈值`report.yaml{top_n5等}`。

## 9. 验证（Phase4）

- 单元pytest：transitions正反例（含PREEMPTED）、models非法拒、store乐观锁+fencing+seq不重、lease全周期、verifier越界/白名单/注入拒、新增queue_order/preempt幂等/reserved_slot/gateway/privacy/timing；覆盖`store+lease+verifier+gateway≥80%`；隔离库`state_test.db`禁污生产；备份+`v1→v2`迁移演练。
- 混沌8项：kill-9恢复、双调度幂等、旧fencing拒、非法跃迁拒、大文件preview、FIFO顺序、抢占15s链+attempts不变、防饿死+网关熔断+verify慢标阻塞；时间断言用p95/p99；证据进`reports/chaos_*.log`。
- 冒烟`smoke_daily`：`t1→t2→t3`全DONE+产物manifest+重放+报告；二次命中缓存；混合冒烟Daily半途注管道验抢占续跑；全程同run_id（跨源独立run+seq关联）。
- 基线：三任务<5min（stub无LLM模式）、千计划DB<10MB/重建<5s、任务卡<2k tokens。

## 10. 风险与对策（审计必看）

| 风险 | 对策 |
|---|---|
| 半写不一致 | DB事务+导出重建+replay校验 |
| 脑裂（双主/旧主写回） | 单实例锁+fencing+单写串行 |
| 非法跃迁 | ALLOWED代码强制+REJECTED审计 |
| 大产物爆上下文 | manifest+preview+read_range |
| 验收注入 | 白名单+shell=False+AST白名单 |
| 调度丢触发 | JobStore+catchup+幂等plan |
| 低优饿死/活锁（反复被抢永不完） | 老化多档+预留槽+抢占预算>3免死 |
| 本地拖慢全局 | 并发隔离2+熔断5min+超时分级+高优直走在线 |
| 告警风暴 | 去重合并+P0三项 |
| 隐私误上云 | 打标+扫描双检（默认关，開即用）+日志脱敏 |

## 11. 执行Phase与DoD总门

- P0契约→P1存储调度→P2编排网关→P3校验报告→P4验证；上游DoD不过不进下游；每步更新`PROGRESS.json{status/evidence/updated_at}`。
- 放行：单元全绿+覆盖达标+混沌8过+冒烟2连绿+报告可审计+PROGRESS全done+本纲领评审签字。

## 12. 待你拍板（review请回）

1. `max_workers=4/verifier2/本地并发2`是否按默认开工？
2. 隐私默认关闭只留接口，确认？
3. 老化30min/预留1槽/熔断5min/高优SLA30s阈值确认？
