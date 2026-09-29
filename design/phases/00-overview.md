# 00 总览设计：AutoTask 稳定编排框架（V7）

> 源主题：`task.md` — 文件/DB为唯一事实源，调度器独立于Agent，主编排+无状态子执行，可恢复、可幂等、可校验。
> 技术锁定：Python + SQLite(WAL) + APScheduler，生产级直达（单机可跑，平滑切PG/Redis）。
> 优先级：上下文/成本 + 校验/可观测优先。
> 对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`（V7设计定稿唯一依据）；修订见文末§9。

## 1. 目标与非目标

目标：
- 笼统问题经LLM拆分成任务卡DAG（planner），每天固定任务定时定点执行，错过可补跑，重复触发无副作用。
- 任务列表为持久化FIFO队列：同优先级先入先出，Daily低优先级，管道输入高优先级，高优排队等待，当前任务完成后自然衔接执行。
- Session切换：模型上下文使用超模型窗口80% → 保存checkpoint → 切新session恢复现场，防失忆。
- Web搜索一等能力：资讯/政策/消息类必须检索最新资料（needs_web=true），推理逻辑类不检索。
- 长任务可中断可恢复，Agent失忆/重启/崩溃后从磁盘重建。
- 上下文可控：主Agent不被长任务占满，子Agent只见任务卡。
- 闭环校验：自测→独立验收→终检，无验收不DONE；调研质量验收（来源可追溯A类>50%/时效性recent 730天+strict 180天/覆盖度/无幻觉）。
- 可观测：全链路run_id、事件可重放、告警分级、日报自动（含队列等待指标+任务分解视图体现拆分质量）。

非目标（本期不做）：
- 分布式多机调度（预留fencing/队列接口，单机先行）。
- 重型LLM网关/计费中台不做；做轻量网关：只做本地Ollama/在线API路由+用量记录+熔断（详见Phase2附录A）。
  大白话：不管钱和key池，只给每个任务装电表+保险丝——超预算就断电存档，本地慢就切在线，隐私任务永不上云。
- 通用DAG可视化UI（只出markdown+JSON汇总报告，失败置顶/耗时Top5/阻塞点三件套）。
- 抢占式中断（高优不打断正在执行的任务，排队等待）。
- 飞书入口本期不做（只留`IngressProvider`接口，项目跑通后再扩展）。
- 拆分结果不做人工确认（报告体现拆分质量供复盘）。

## 2. 核心原则（不可违背）

1. 聊天上下文不是状态：每次执行前从`state.db + PROGRESS.json`恢复。
2. 调度器是确定性程序：cron/APScheduler负责时间，Agent只被触发；管道输入走队列入口，不走时间触发。
3. 主Agent只做拆分（planner，LLM）+派发/校验/推进（确定性代码），不执行长任务。
4. 子Agent无状态：输入=任务卡，输出=产物+SUBMITTED+handoff，执行完销毁。
5. 所有写原子+幂等+带锁+带围栏token。
6. 主文件是汇总视图，任务分片+DB行是事实源，事件表是WAL。
7. 队列是唯一派发来源：持久化FIFO，同优先级按`seq`先入先出；优先级越小越优先（0=管道高优，10=Daily低优）；高优排队等待，当前任务完成后自然衔接执行。单表制：只以`tasks`为队列，`inbox`文件即ingress凭据，不另设可变状态队列表（评审B1）。
8. 网关可插拔：Provider（本地Ollama/在线API）与RoutePolicy（隐私守卫/延迟守卫/能力匹配）均为插件，配置链式编排；本地有限并发+熔断，公开任务溢出走在线，隐私永不上云。隐私默认关（`privacy.enabled=false`，跳过扫描与脱敏，接口保留，V7§2.11）。
9. Session切换：80%分母=模型窗口（provider级`context_window`），判定用`usage.prompt_tokens`（当前上下文非累计消耗）；触发→`RUNNING→READY`（子Agent主动让出，attempts不变，带checkpoint断言），单任务切换>5次熔断改走FAILED（V7§2.9）。
10. Web搜索：`needs_web`是能力开关非路由维度；资讯/政策/消息必须检索（freshness：recent=730天/strict=180天）；来源分级=域名先验+LLM逐条判定（只喂url+域名+title+发布者，禁喂正文防注入）；A类来源占比>50%（V7§2.10）。
11. 全环节计时：queue_wait/dispatch/execute/verify/export全埋点，plan终态30s内出`reports/{plan}.summary.md`（失败置顶/耗时Top5/阻塞点/甘特/网关用量/任务分解视图），阈值进`config/report.yaml`。
12. 计时阈值与算力配额全部进配置，不改代码调参（V7§2.12）。

## 3. 事实源分层

```
SQLite state.db (WAL)  ← 唯一写事实源
  ├─ plans / tasks(含priority/source/seqAUTOINCREMENT/last_success_hash/cancel_requested/needs_web/freshness/slice_rationale/session_switch_total) / events / metrics / schema_version
  ├─ 单表队列（评审B1）：只tasks为队列，inbox/done|error文件即ingress凭据，不设queue_items可变表
  └─ 提交后原子导出 ↓
state/export/master.json + tasks/*.json  ← Agent只读视图（坏了可重建）
events/YYYY-MM-DD.jsonl ← DB events按天导出（审计双保险）
logs/structured.jsonl ← 运行日志（非状态）
PROGRESS.json ← 跨Phase工程进度（防忘顶层指针）
state/checkpoints/{task}.json ← 断点续跑（含summary/artifacts_partial/done_steps/generation）
artifacts/{date}/{task}.* + manifest.json ← 内容寻址产物
artifacts/{date}/{task}/sources.json ← 结构化来源（机器可验）
artifacts/{date}/{task}/claims.json ← 结论-证据映射（无幻觉验收）
```

重启恢复顺序：读PROGRESS.json定位Phase → 连state.db查plan/task/events → 读checkpoint → 重建派发。

## 4. 术语与ID规范

- `plan_id = {template}_{YYYY-MM-DD}`（Daily）或`adhoc_{uuid7}`（管道/拆分），UTC存、展示Asia/Shanghai。
- `idempotency_key = {plan_id}/{task_id}/{inputs_hash全hash}`（禁截断，评审Minor1），UNIQUE，重复触发直接返回。
- `run_id = {plan_id}_{uuid7}`，全链路透传（调度→编排→执行→驗证→告警）。
- `fencing_token`：每任务单调递增，写回必须匹配，防旧owner脑裂；仅lease相关跃迁（acquire/renew/revoke）+1，普通跃迁只+version（评审Minor2）。
- `cancel_requested`配`CANCEL_REQUESTED`事件审计（评审B2）；DB全UTC ISO，cron按Asia/Shanghai解析，日分区/日报按上海自然日切分（评审Minor4）。
- `inputs_hash = sha256(sorted(inputs manifest sha))`，命中+产物不变可SKIPPED_CACHED。
- `source_id`：`{run_id}_{n}`唯一，来源分级结果与source_id绑定落盘可复核。
- `context_window`：provider级模型窗口大小（3B/7B=32768，online=128000），session切换80%判定分母。

## 5. Phase依赖图

```
Phase0 契约 (transitions/models/acceptance) ─┐
Phase1 存储/调度/租约 (store/lease/scheduler) ├→ Phase2 编排/执行 → Phase3 校验/观测 → Phase4 混沌/冒烟
PROGRESS.json + design/phases/*.md 全程伴随                      ↑
```

任何Phase的DoD未满足不得进入下一Phase。

## 6. 全局风险与对策

| 风险 | 对策 | 在哪Phase解决 |
|---|---|---|
| 跨文件半写不一致 | DB事务+导出重建 | P1 |
| 文件锁跨进程失效/脑裂 | DB lease+fencing | P1 |
| 非法状态跃迁 | ALLOWED表代码强制 | P0+P1 |
| 大产物灌爆上下文 | manifest+preview+read_range | P2 |
| 验收命令注入 | 白名单+shell=False+timeout | P3 |
| 调度单点丢触发 | JobStore持久化+catchup | P1 |
| 失败难定位 | run_id+结构化日志+日报 | P3 |
| token烧钱 | 上下文水位线checkpoint+缓存跳过 | P2 |
| 低优饿死 / 高优延迟 | 优先级队列+老化提升+预留1槽给Daily | P1+P2 |
| 告警风暴（管道突发100任务即100条） | AlertPolicy去重5min合并，P0仅死信/不一致/高优SLA超时三项（评审M8） | P3 |
| SQLite单写者打爆（多线程database is locked） | Store单写串行+BEGIN IMMEDIATE重试3次+export移出事务（评审M6） | P1 |
| 管道突发压垮 workers | 最大并发+队列积压告警 | P2 |
| 模型失忆（上下文超80%） | session切换+checkpoint恢复+让出熔断K=5 | P2 |
| 子Agent无限让出烧token | session_switch_total熔断，超限改FAILED | P2 |
| 来源分级注入（内容自证清白） | 分级器禁喂正文，只喂url+域名+title+发布者 | P2 |
| 报告幻觉（无来源结论） | claims.json机器验收，无source_id拒收 | P3 |
| 拆分质量不可审计 | 报告任务分解视图+slice_rationale+planner_run_id | P2 |

## 7. 文档索引

- `design/phases/Phase0-contract-design.md` — 状态机、Pydantic、任务卡契约
- `design/phases/Phase1-storage-scheduler-lease-design.md` — DDL、事务、租约、调度
- `design/phases/Phase2-orchestrator-executor-design.md` — 主循环、planner拆分、子执行、session切换、搜索层
- `design/phases/Phase3-verifier-observability-design.md` — 沙箱验收、调研质量验收、告警、日报
- `design/phases/Phase4-verification-chaos-design.md` — 测试、混沌、冒烟、DoD
- `design/plans/IMPLEMENTATION_PLAN_V7.md` — 执行总入口（唯一依据，历史版本仅追溯）
- `PROGRESS.json` — 进度事实源

## 8. 需求追溯表（7条原始需求 → 设计位置）

| # | 原始需求 | 设计位置 | 状态 |
|---|---|---|---|
| 1 | 定时任务+管道高优（飞书预留） | §1目标；Phase1 §4调度；V7§6入口；飞书=`core/ingress/base.py`接口预留 | 定时✅ 飞书🔲预留 |
| 2 | 上下文占满→切session→恢复现场 | §2原则9；Phase2 §6b Session切换与恢复；Phase0 §2.1让出边 | ✅设计完成 |
| 3 | LLM拆分+分级路由+子Agent无状态 | Phase2 §2主循环planner；Phase2 附录A网关；V7§7拆分层 | ✅设计完成 |
| 4 | 分片执行完需测试检验 | Phase3 §2验证器；§2.2调研质量4新规则 | ✅设计完成 |
| 5 | 模型参数量/地址/key可配置 | config/gateway.yaml（provider级+context_window）；config/search.yaml | ✅设计完成 |
| 6 | 兼顾电脑使用 | Phase2 §2 governor两档；ACTIVE档local=0有意权衡（优先响应速度） | ✅设计完成 |
| 7 | 隐私安全接口可插拔可配置 | §2原则8；Phase2 附录A privacy_guard；V7§8 | ✅设计完成 |

## 9. V7对齐修订记录（2026-09-28，本轮，待评审）

- §1目标：新增LLM拆分、Session切换、Web搜索、调研质量验收、任务分解视图；非目标加飞书预留/拆分不人工确认。
- §2原则：新增9（Session切换规则：80%=模型窗口+让出熔断K=5）、10（Web搜索：needs_web开关+freshness阈值+来源分级注入防护+A类>50%）、12（阈值全进配置）；原9（网关）→8。
- §3：tasks列加`needs_web/freshness/slice_rationale/session_switch_total`；checkpoint加4字段；新增sources.json/claims.json产物。
- §4：`plan_id`加`adhoc_{uuid7}`；新增`source_id/context_window`术语。
- §6：风险表新增5行（模型失忆/无限让出/来源分级注入/报告幻觉/拆分质量）。
- §8：新增需求追溯表（7条原始需求→设计位置→状态）。
- 前轮（V6）保留：简化抢占模型、隐私默认关、schema_ref改名、14态状态机、排队等待模型。
