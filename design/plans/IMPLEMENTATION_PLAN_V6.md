# 落地实施方案总纲领 第6版（IMPLEMENTATION PLAN V6）—— 开工终版

> 本版为新会话全力编程的唯一依据。合并 V1→V5 全部结论 + 33/33验证中发现的3个真bug修复 + 4项coding清单，历史版本仅作追溯，不再作为执行依据。
> 主题：文件/DB为唯一事实源（真相只存磁盘），调度器独立于Agent，主Agent单个编排器只派活、子Agent并发干活，所有状态可恢复、可幂等（重复执行结果不变）、可校验。
> 技术栈：Python + SQLite的WAL模式（预写日志，崩溃不丢的写入方式）+ APScheduler（Python定时任务库）+ AnyIO（异步并发库，跑在asyncio后端上）+ psutil（系统信息库）+ Pydantic（数据校验库）。

## 1. 目标与非目标

目标：
- Daily任务定时定点执行，错过可补跑，重复触发无副作用。
- 任务列表是持久化FIFO队列（先入先出、落盘的排队结构）：同优先级按入队顺序，Daily低优（priority=10），管道高优（priority=0..4），高优排队等待，当前任务完成后自然衔接执行。
- 主从Agent结构（1主编排器+N子执行器）：主全局唯一防脑裂（双主同时写坏数据），并发在子Agent层用协程（轻量并发，单线程轮流跑任务）实现。
- 轻量网关：本地Ollama（本机小模型服务）/在线API（OpenAI兼容即统一HTTP接口规范）可插拔路由；隐私判断默认关闭只留扩展接口。
- 全环节计时+汇总报告：汇总只链接各任务输出报告，失败置顶/耗时Top5/阻塞点一跳即达。
- 动态算力：人在约1/8，空闲8~9成，内存同比例，手动可强制满负载。
- 长任务可中断可恢复；上下文可控；闭环校验；全链路run_id（一次运行全局编号）+事件重放+分级告警。

非目标：分布式多主调度；重型网关/计费中台（只装电表+保险丝）；DAG（任务依赖关系图）图形UI；抢占式中断（高优不打断正在执行的任务，排队等待）。

## 2. 核心原则

1. 上下文不是状态：先读`PROGRESS.json`+`state.db`恢复。
2. 调度器是确定性程序（到点必触发的死板程序）：cron/APScheduler管时间，管道走队列入口。
3. 主Agent全局唯一，只拆分/派发/校验/推进；并发的是子Agent协程。
4. 子Agent无状态：看任务卡干活，输出产物+SUBMITTED+handoff（交接摘要），干完销毁。
5. 写原子+幂等+带锁+围栏token fencing_token（防脑裂号码牌）。
6. 主文件是汇总视图，DB行+分片是事实源，事件表是WAL（只追加日志）。
7. 单表队列：只`tasks`为队列；`ORDER BY priority ASC, seq ASC`；`seq`由AUTOINCREMENT（数据库自动发号）分配，禁`max+1`。
8. 高优排队等待：管道高优（priority 0..4）入队后排在Daily低优（priority 10）前面，但**不打断正在执行的任务**；当前任务完成后，主循环下一轮自然派发队列中优先级最高的READY任务。无PREEMPTED状态、无抢占协作、无preemptible/preempt_count字段。
9. 网关可插拔，隐私默认关（`privacy.enabled=false`，跳过扫描与脱敏，接口保留）。
10. 计时阈值与算力配额全部进配置，不改代码调参。

## 3. 事实源与目录

```
state.db(WAL)：plans/tasks/events/metrics/schema_version（版本号表）
  → 原子导出state/export/（tmp+fsync刷盘+rename改名）→ events日分区（上海自然日）/logs/PROGRESS.json/checkpoints/artifacts+manifest/inbox投递箱/reports汇总+日报/backup每日拷贝
orchestrator/
  config/schedule.yaml（含workers+governor，见§7）, gateway.yaml（含privacy.enabled=false）, report.yaml
  templates/daily_plan.yaml, pipeline_task.json, task_card.j2
  schemas/models.py, transitions.py, acceptance_*.yaml, summary.schema.json
  core/store.py, lease.py, enqueue.py, scheduler.py, orchestrator.py, executor.py
  core/governor.py（含override三态）, gateway.py, providers/{base,ollama,online_stub}.py
  core/routing/{chain,privacy_guard,latency_guard,capability_match}.py
  core/privacy.py（数字前后瞻正则，见§11修复①）, timing.py, verifier.py, notifier.py, report.py, utils.py
  tests/test_*.py  scripts/smoke_daily.py scripts/chaos_*.py
```

骨架现状（新会话起点）：`schemas/transitions.py`（无PREEMPTED，简化状态机✅）、`schemas/models.py`（无preemptible/preempt_count，`schema`已改名`schema_ref`✅）、`core/{gateway,privacy,timing,governor,utils}.py`✅、`config/{gateway,schedule,report}.yaml`（gateway引号已修，`privacy`段与`workers/governor`段已补✅）、`requirements.txt`（pydantic/pyyaml/anyio/psutil/pytest✅）。

## 4. ID与字段规范

- `plan_id=template_YYYY-MM-DD`；DB存UTC（协调世界时），展示Asia/Shanghai。
- `idempotency_key（幂等键）=plan/task/inputs_hash全hash` UNIQUE，禁截断。
- `run_id=plan_uuid7`透传；`priority 0..10枚举`（0..4管道，5普通，10 Daily）；`last_success_hash`判缓存；`cancel_requested`配`CANCEL_REQUESTED`事件；`privacy/complexity`保留，关闭时填public/simple。
- `TaskShard.task_id`正则`^[a-z0-9_]{1,64}$`；`AcceptanceRule.schema`改名`schema_ref`（与Pydantic父类属性重名告警，见§11修复③）。

## 5. 状态机

`PENDING→BLOCKED/READY→RUNNING→SUBMITTED→VERIFYING→DONE/RETRY/FAILED/WAITING_APPROVAL（4h升级P0）；终态DONE/DEAD_LETTER/CANCELLED/SKIPPED*`。`DONE`无出边；子Agent仅`RUNNING→SUBMITTED/FAILED`；非法记REJECTED+P1。无PREEMPTED状态（抢占简化为排队等待，见§2原则8）。

## 6. 存储/调度/租约/队列入口

- `Store`单写串行+`BEGIN IMMEDIATE（立即加写锁）`重试3次；`fencing`仅lease跃迁+1；`export`出事务；`schema_version+migrate（版本迁移）`+`VACUUM INTO`备份。
- DDL按Phase1（CHECK约束+`seq AUTOINCREMENT`+索引`idx_tasks_sched`）；验证结论：FIFO排序✅、seq不重✅、UNIQUE✅、旧version拒写✅（D1-D5）。
- 租约 lease（限时持有权）：`owner+lease_until+fencing`；`acquire`仅READY/过期；心跳30s续120s；过期走`RUNNING→FAILED→RETRY→READY`（attempts+1在RETRY→READY边）；`request_cancel`置旗，宽限高优30s/默认60s，不让出`force_revoke`。
- 调度JobStore（持久化仓库）+`coalesce（合并错过）`+单实例+`misfire_grace3600s`；`ensure_plan`幂等；`recover_missed`；CLI含`--dry-run（试跑）/--rerun/--catchup（补跑）`。
- 入口硬限制：<100KB、命名正则、路径锁`artifacts/{date}/`、cmd白名单前置、单小时100；`business_rule`仅4种AST（抽象语法树）节点。

## 7. 编排/执行/动态算力（AnyIO + V5两档三源）

- 主循环（跨plan全局，单实例+优雅停机）：`recover→ingest_inbox→refresh_ready→aging（10min→5，20min→3）→dispatch_split（高优N-1槽+低优保1槽）→verify_async（池2）→export→wait_wakeup（wakeup.flag事件+10s/2s）`。
- 阻塞调用包`to_thread`，防卡事件循环（单线程轮流跑任务的核心）。
- 算力（本机8逻辑/16GB，已验证切换逻辑V5-1~V5-5✅）：

| 档位 | 判定 | CPU | 内存 | executor | verifier | 本地 |
|---|---|---|---|---|---|---|
| ACTIVE | 有输入/idle<5min/手动active | ~1/8（共享1槽，执行优先） | ≤2GB | 1（共享） | 0（排队） | 0（公开走在线） |
| IDLE | idle≥5min两确认/手动idle | ~85%（7核） | min(12GB，可用-2GB) | 4 | 2 | 1 |

- 手动：`python -m core.governor --mode idle|active|auto`写`state/governor.override`，手动优先于侦测，重启有效，切档记`GOVERNOR_SWITCH{by}`；`headroom（余量）2GB`不足停派；高优ACTIVE可+1破格（内存红线不破）。
- 配置：`governor: {idle_after_s:300, confirm_samples:2, active_cpu:0.125, idle_cpu:0.85, mem_headroom_gb:2, high_burst:1}`。

## 8. 网关（隐私默认关）与报告（只链不搬）

- 插件链`privacy_guard→latency_guard→capability_match→cost_saver`；能力`3B→simple/4B→simple/7B→medium/complex走在线`；验证结论G1-G5✅（secret强制本地、simple本地、complex在线、过载公开切在线且隐私不切）。
- 隐私关闭时：`enqueue`跳扫描、`route`跳`classify`、日志跳脱敏；接口`classify/redact/PrivacyGuard`保留，`test_privacy`默认skip。正则已修数字前后瞻（中文旁`\b`漏检，见§11修复②）。
- 本地保护：并发按§7（ACTIVE0/IDLE1）、`slo_p50_8s`、队列>4/连败3熔断5min半开、simple20s/medium60s超时切换；高优公开直走在线。
- 汇总`reports/{plan}.summary.md+json`：失败置顶/Top5/阻塞/甘特/网关用量，全链任务输出报告，缺失标`[缺失]`+P1；日报18:00合集。计时`timing.span()`7处✅（C1-C2）。

## 9. 校验/观测/计时

- 验证器AnyIO协程池；白名单+`shell=False（不走系统壳）`+flaky重试不计attempts；`final_consistency`逐plan；`AlertPolicy（去重策略）`5min合并，P0仅死信/不一致/高优SLA超时；`metrics`留30天。

## 10. 阈值（任务均5min，定版）

老化10min→5、20min→3；预留1槽；熔断5min；高优SLA p95<60s（排队等待，非抢占）；cancel宽限30s/60s；lease120s/心跳30s；报告plan终态30s落盘。

## 11. 验证基线与coding前修复（33/33已过，4项带入coding）

已修并验证：①隐私正则`\b`→数字前后瞻；②`gateway.yaml`的`${ONLINE_BASE_URL}`加引号（YAML流映射解析失败）；③上述两处复测通过。
coding清单：①`gateway.yaml`补`privacy:{enabled:false}`段、`schedule.yaml`补`workers/governor`段（V5值）✅已补；②`AcceptanceRule.schema`→`schema_ref`✅已改；③`Store.transition`为第一块硬骨头。

## 12. 开工顺序（新会话照此执行，每步更新PROGRESS.json）

1. Phase0收尾：清单②③ + `test_transitions/test_models/test_queue_order`。
2. Phase1：`store.py`→`lease.py`→`enqueue.py`→`scheduler.py` + `test_store/test_lease` + 备份迁移演练。
3. Phase2：`orchestrator.py`→`executor.py`+网关providers/routing + `test_gateway/test_reserved_slot`。
4. Phase3：`verifier.py`异步化→`notifier.py`去重→`report.py`链接汇总。
5. Phase4：混沌8项+`smoke_daily`（混合冒烟）+覆盖率≥80% + `PROGRESS.json`全done。

## 13. 新会话首读清单（防失忆，按序读）

`IMPLEMENTATION_PLAN_V6.md`（本文件）→ `PROGRESS.json` → `design/phases/REVIEW-architect.md` → `orchestrator/core/{governor,gateway,privacy}.py` → `orchestrator/schemas/{transitions,models}.py`。
