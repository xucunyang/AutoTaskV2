# 落地实施方案总纲领 第3版（IMPLEMENTATION PLAN V3）

> 延续 V2，为当前唯一执行依据。相对V2的三处修正：①主从结构更正+并发改协程（见§7）；②汇总报告改为链接任务输出报告（见§8）；③ workers按半算力+阈值重调（见§7/§10/§12）。
> 主题：文件/DB为唯一事实源（真相只存磁盘），调度器独立于Agent，主Agent单个编排器只派活、子Agent并发干活，所有状态可恢复、可幂等（重复执行结果不变）、可校验。
> 技术栈：Python + SQLite的WAL模式（预写日志，崩溃不丢的写入方式）+ APScheduler（Python定时任务库）+ AnyIO（异步并发库，跑在asyncio后端上，见§7选型）。

## 1. 目标与非目标

目标：
- Daily任务定时定点执行，错过可补跑，重复触发无副作用。
- 任务列表是持久化FIFO队列（先入先出、落盘的排队结构）：同优先级按入队顺序，Daily低优（priority=10），管道高优（priority=0..4），高优可抢占（插队并中断）低优。
- 主从Agent结构（1个主编排器+多个子执行器，主只调度不干活）：主层单一逻辑实例防脑裂（双主同时写坏数据），并发发生在子Agent层，用协程实现以省时间。
- 轻量网关：本地Ollama（本机小模型服务）/在线API可插拔路由；隐私判断默认关闭只留扩展接口（确认）。
- 全环节计时+汇总报告：汇总报告链接到各任务输出报告（只放链接不搬运正文），哪个失败/耗时/阻塞一跳即达。
- 长任务可中断可恢复；上下文可控（任务卡白名单）；闭环校验；全链路run_id（一次运行全局编号）+事件重放+分级告警。

非目标：分布式多主调度不做；重型网关/计费中台不做（只装电表+保险丝）；DAG（任务依赖关系图）图形UI不做。

## 2. 核心原则

1. 上下文不是状态：先读`PROGRESS.json`+`state.db`恢复。
2. 调度器是确定性程序（到点必触发的死板程序）：cron/APScheduler管时间，管道走队列入口。
3. 主Agent全局唯一逻辑实例（防双主），只拆分/派发/校验/推进；并发的是子Agent协程，不是多个主。
4. 子Agent无状态：看任务卡干活，输出产物+SUBMITTED+handoff（交接摘要），干完销毁。
5. 写原子+幂等+带锁+围栏token fencing_token（防脑裂号码牌）。
6. 主文件是汇总视图，DB行+分片是事实源，事件表是WAL（只追加日志）。
7. 单表队列：只`tasks`为队列；排序`ORDER BY priority ASC, seq ASC`（优先级越小越先，同级先到先得）；`seq`由AUTOINCREMENT（数据库自动发号）分配。
8. 抢占统一出口`RUNNING→PREEMPTED→READY/RETRY`，抢占不加attempts（尝试次数）；可抢占必须幂等。
9. 网关可插拔，隐私默认关；计时阈值进配置。

## 3. 事实源与目录（同V2，加网关/报告配置）

```
state.db(WAL)：plans/tasks/events/metrics/schema_version（版本号表）→ 原子导出state/export/ → events日分区（上海自然日）/logs/PROGRESS.json/checkpoints/artifacts+manifest/inbox投递箱/reports汇总+日报/backup每日拷贝
orchestrator/
  config/schedule.yaml（含workers，见§7）, gateway.yaml（privacy.enabled=false默认）, report.yaml
  templates/daily_plan.yaml, pipeline_task.json, task_card.j2
  schemas/models.py(Pydantic校验库), transitions.py, acceptance_*.yaml
  core/store.py, lease.py, enqueue.py, scheduler.py, orchestrator.py（AnyIO主循环）, executor.py（协程任务）, gateway.py, providers/, routing/, privacy.py（接口保留默认关闭）, timing.py, verifier.py, notifier.py, report.py（含链接）, utils.py
  tests/  scripts/
```

## 4. ID与字段规范（同V2，隐私默认关）

- `plan_id/template_YYYY-MM-DD`；DB存UTC（协调世界时），展示Asia/Shanghai。
- `idempotency_key（幂等键）=plan/task/inputs_hash全hash` UNIQUE；重发返回。
- `run_id=plan_uuid7`透传；`seq` AUTOINCREMENT；`priority 0..10枚举`；`preemptible（可抢）`须`idempotent（幂等）`为真；`preempt_count>3`免死；`last_success_hash`判缓存；`cancel_requested`配事件审计；`privacy/complexity`保留，隐私关闭时全填public/simple不扫描。

## 5. 状态机（同V2）

`PENDING→BLOCKED/READY→RUNNING→SUBMITTED→VERIFYING→DONE/RETRY/FAILED/WAITING_APPROVAL（人工审批，4h升级P0）；RUNNING→PREEMPTED（仅系统）→READY/CANCELLED；终态DONE/DEAD_LETTER/CANCELLED/SKIPPED*`。`DONE`无出边；子Agent仅`RUNNING→SUBMITTED/FAILED`；非法记REJECTED+P1。

## 6. 存储/调度/租约/队列入口（同V2，时间参数见§10）

- `Store`单写串行+`BEGIN IMMEDIATE（立即加写锁）`重试3次；`fencing`仅lease跃迁+1；`export`出事务；`schema_version+migrate（版本迁移）`+`VACUUM INTO`备份。
- 租约 lease（限时持有权）：`owner+lease_until+fencing`；`acquire`仅READY/过期；心跳30s续120s；过期走`PREEMPTED(reason=lease_expired，attempts+1)`；`request_cancel`置旗，宽限见§10。
- 调度JobStore（持久化仓库）+`coalesce（合并错过）`+单实例+`misfire_grace3600s`；`ensure_plan`幂等；`recover_missed`补N天；CLI含`--dry-run（试跑）/--rerun/--catchup（补跑）`。
- 入口硬限制：<100KB、命名正则、路径锁`artifacts/{date}/`、cmd白名单、单小时100；`business_rule`仅4种AST（抽象语法树）节点。

## 7. 编排/执行并发更正（相对V2的核心修正）

**更正**：V2第3点“主层Agent支持并发”表述错误。正确结构是主从式：主Agent全局唯一（1个逻辑编排器，防双主脑裂），并发发生在子Agent层（多个任务同时跑省时间）。不存在多个主同时调度。

**协程选型评估（asyncio即Python自带异步库 vs AnyIO即兼容层异步库）：**
- asyncio：零依赖，`create_task/gather`够用，但取消（cancel）是粗暴抛异常语义，抢占的`cancel_requested`协作退出、超时分级、结构化任务组（task group，一组任务同生共死）都要手写，易漏。
- AnyIO（asyncio后端）：`TaskGroup/cancel scope（取消域，可限时限范围取消）/move_on_after（超时即过）`原生匹配抢占+超时分级+半开熔断语义；同一套代码以后可切trio后端；代价多一个小依赖。
- 决定：用AnyIO跑在asyncio后端。`orchestrator`主循环与`executor/verifier`全协程化；阻塞调用（sqlite、subprocess pytest、Ollama HTTP）包`to_thread（扔线程池跑阻塞活）`，避免卡住事件循环（单线程轮流跑任务的核心，一卡全卡）。

**半算力workers编程（本机8逻辑/4物理，半算力=4逻辑）：**
```yaml
# config/schedule.yaml
workers:
  auto_half: true            # 首选：os.cpu_count()//2=4为总预算，自动分配
  executor: 2                # 子执行协程2（5min/任务，2并行吞吐翻倍，IO等待型）
  verifier: 1                # 验证协程1（pytest偏CPU，单跑防抖）
  # 本地模型并发1（Ollama 7B吃CPU，单发防拖慢，见网关local_tiers）
  # 合计 2+1+1=4逻辑 = 半算力；另半留给系统/Ollama主进程/突发
```
编程实现：`workers.resolve()`先`total=os.cpu_count()//2`，再按`2:1:1`切分，配置显式值则覆盖；超预算启动即告警。`dispatch_split`按`executor`数取READY，高优占N-1槽、低优保1槽不变。

其余抢占/防饿死同V2：`peek`须可派发高优；`pick_victim`须幂等、排除依赖链、`preempt_count≤3`；保留原seq；p95/p99见§10。

## 8. 汇总报告改链接（相对V2的第二修正）

V2是汇总报告搬运正文，现改为**汇总只放链接**：
```markdown
# 汇总 {plan_id} 总耗时X 成功率Y%
## 失败（n个置顶）
- t2_clean FAILED [看任务输出报告](artifacts/2026-09-28/t2_clean/report.md) last_error… [trace]
## 耗时Top5（点链接跳原文）
- t3_report execute 212s [输出](artifacts/.../report.md) [manifest](artifacts/.../manifest.json)
## 阻塞点/甘特/网关用量（同V2，略）
```
- `report.build(plan_id)`负责：扫DB聚合→写`reports/{plan}.summary.md+json`→校验每个链接文件存在，不存在标`[缺失]`+P1；任务输出报告（各任务自己的`report.md`）由执行器写，汇总不重写。
- 日报18:00合Reports/YYYY-MM-DD.md时同样只链入各plan汇总。

## 9. 校验/观测/计时（同V2）

- 验证器异步协程池（AnyIO TaskGroup，`max=verifier`数）；白名单+`shell=False（不走系统壳防注入）`+flaky重试不计attempts；`final_consistency`逐plan。
- `timing.span()`7处不变；阻塞规则进`report.yaml`；`AlertPolicy（去重策略）`合并；`metrics`留30天；日志脱敏在隐私开启时才生效，关闭时跳过。

## 10. 阈值重调（任务均5min，按确认重定）

| 参数 | V2 | V3 | 理由（5min/任务） |
|---|---|---|---|
| 老化 aging_after | 30min→5 | 10min→5，20min→3 | 30min=6个任务周期太久，低优饿死；10min=2周期提一档，20min再提，封顶3 |
| 预留槽 | 1槽 | 保持1槽 | 确认OK |
| 熔断 cooldown | 5min | 保持5min | =1个任务周期，半开试1单合理 |
| 高优SLA（入队→RUNNING） | p95<30s | p95<60s | 5min任务占满workers时抢占需`2s唤醒+5s步检+checkpoint落盘`，30s太紧；60s配抢占仍及时，总耗时仍≈5min级 |
| cancel宽限 | 30s高优/60s默认 | 保持 | 高优30s≈快路径2s+步检5s+落盘余量 |
| lease_ttl/心跳 | 120s/30s | 保持 | 5min任务需续约2~3次，心跳30s余量足 |

## 11. 验证（同V2，8项混沌+隔离库+备份迁移演练）

- 超时断言按§10新阈值（抢占p95<60s调度，报告30s落盘不变）；`test_privacy`保留但默认skip（开关关），接口不断。

## 12. 待确认（V3 review请回）

1. 主从单主+AnyIO协程子并发+半算力`2/1/1`分配，确认开工？
2. 汇总链入任务输出报告（只链不搬），确认？
3. §10新阈值（老化10/20min、高优SLA 60s、其余保持），确认？
