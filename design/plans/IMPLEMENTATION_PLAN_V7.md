# 落地实施方案总纲领 第7版（IMPLEMENTATION PLAN V7）—— 设计定稿，待评审

> 本版为 V6 的升级定稿，唯一执行依据。继承 V6 全部结论（33/33验证 + 3bug修复 + 简化抢占模型），新增：LLM任务拆分、Web搜索一等能力、Session切换正式化、调研质量验收、飞书预留接口。历史版本（V1~V6）仅作追溯，不再作为执行依据。
> 主题：文件/DB为唯一事实源（真相只存磁盘），调度器独立于Agent，主Agent做拆分+派活、子Agent无状态干活，所有状态可恢复、可幂等（重复执行结果不变）、可校验。
> 业务链路：自然语言 → LLM拆分 → 分级路由 →（按需Web搜索）→ 子Agent执行 → 调研质量验收 → MD报告 →（预留飞书）交付。
> 技术栈：Python + SQLite的WAL模式（预写日志，崩溃不丢的写入方式）+ APScheduler（Python定时任务库）+ AnyIO（异步并发库，跑在asyncio后端上）+ psutil（系统信息库）+ Pydantic（数据校验库）。

## 1. 目标与非目标

目标：
- 笼统问题经LLM拆分成任务卡DAG（拆分层：`planner`），Daily定时调研 + 管道高优（飞书仅预留接口）两种入口。
- 任务列表是持久化FIFO队列（先入先出、落盘的排队结构）：同优先级按入队顺序，Daily低优（priority=10），管道高优（priority=0..4），高优排队等待，当前任务完成后自然衔接执行。
- 主从Agent结构（1主编排器+N子执行器）：主全局唯一防脑裂（双主同时写坏数据），并发在子Agent层用协程（轻量并发，单线程轮流跑任务）实现。
- Session切换：模型上下文使用超模型窗口80%（`context_window`的80%，非任务预算）→ 保存checkpoint → 切新session恢复现场继续执行，防失忆。
- Web搜索一等能力：资讯/政策/消息类任务必须检索最新资料（`needs_web=true`），不得使用模型参数内知识；推理/逻辑类任务不检索。
- 分级路由：本地Ollama（本机小模型服务）/在线API（OpenAI兼容即统一HTTP接口规范）按复杂度（simple/medium/complex）可插拔路由；隐私判断默认关闭只留扩展接口。
- 调研质量验收：来源可追溯（逐条判定来源等级A/B/C，A类超半数）、时效性（recent=2年/strict=6个月）、覆盖度（预定义问题全回答）、无幻觉（结论必须挂来源）。
- 全环节计时+汇总报告：汇总只链接各任务输出报告，失败置顶/耗时Top5/阻塞点一跳即达；报告含任务分解视图（体现拆分质量，可审计）。
- 动态算力：人在约1/8优先响应速度，空闲8~9成，内存同比例，手动可强制满负载。
- 长任务可中断可恢复；上下文可控；闭环校验；全链路run_id（一次运行全局编号）+事件重放+分级告警。

非目标：分布式多主调度；重型网关/计费中台（只装电表+保险丝）；DAG（任务依赖关系图）图形UI；抢占式中断（高优不打断正在执行的任务，排队等待）；飞书入口本期不做（只留`IngressProvider`接口）；拆分结果不做人工确认（报告体现拆分质量供复盘）。

## 2. 核心原则

1. 上下文不是状态：先读`PROGRESS.json`+`state.db`恢复。
2. 调度器是确定性程序（到点必触发的死板程序）：cron/APScheduler管时间，管道走队列入口。
3. 主Agent全局唯一，负责拆分（planner，LLM）+派发/校验/推进（确定性代码）；并发的是子Agent协程。
4. 子Agent无状态：看任务卡干活，输出产物+SUBMITTED+handoff（交接摘要），干完销毁。
5. 写原子+幂等+带锁+围栏token fencing_token（防脑裂号码牌）。
6. 主文件是汇总视图，DB行+分片是事实源，事件表是WAL（只追加日志）。
7. 单表队列：只`tasks`为队列；`ORDER BY priority ASC, seq ASC`；`seq`由AUTOINCREMENT（数据库自动发号）分配，禁`max+1`。
8. 高优排队等待：管道高优（priority 0..4）入队后排在Daily低优（priority 10）前面，但**不打断正在执行的任务**；当前任务完成后，主循环下一轮自然派发队列中优先级最高的READY任务。无PREEMPTED状态、无抢占协作、无preemptible/preempt_count字段。
9. Session切换规则：80%分母=动态窗口`final_window`=`min(架构上限,档位建议,显存可撑,配置覆盖)`-`context_reserved`(8192)（模型窗口总上下文，非任务预算）；判定用`usage.prompt_tokens`（当前上下文，非累计消耗）；触发→`RUNNING→READY`（子Agent主动让出，attempts不变，带checkpoint断言，long_running=false拒让出）；单任务切换>5次熔断改走FAILED（防无限让出烧token）；撞`context_length_exceeded`→强制让出（`reason=context_overflow`）+水位线自适应下调。
10. Web搜索规则：`needs_web`是能力开关非路由维度；资讯/政策/消息必须检索（新鲜度recent=730天/strict=180天）；来源分级=域名先验+LLM逐条判定（只喂url+域名+title+发布者，禁喂正文防注入）；A类来源占比>50%。
11. 网关/搜索插件可插拔，隐私默认关（`privacy.enabled=false`，跳过扫描与脱敏，接口保留）。
12. 计时阈值与算力配额全部进配置，不改代码调参。

## 3. 事实源与目录

```
state.db(WAL)：plans/tasks/events/metrics/schema_version（版本号表）
  → 原子导出state/export/（tmp+fsync刷盘+rename改名）→ events日分区（上海自然日）/logs/PROGRESS.json/checkpoints/artifacts+manifest/inbox投递箱/reports汇总+日报/backup每日拷贝
orchestrator/
  config/schedule.yaml（含workers+governor，见§7）, gateway.yaml（含privacy.enabled=false+context_window）, search.yaml（检索provider）, report.yaml
  templates/daily_plan.yaml, pipeline_task.json, task_card.j2
  schemas/models.py（含Source/Claim/SourceTier）, transitions.py, acceptance_*.yaml, summary.schema.json
  core/store.py, lease.py, enqueue.py, scheduler.py, orchestrator.py, executor.py
  core/planner.py（LLM拆分）, core/governor.py（含override三态+Win/Mac双平台空闲检测）, gateway.py（含CapabilityMatch本地窗口门禁）
  core/context.py（动态窗口计算：探测+分档+显存取min+resolved/effective对账）
  core/providers/{base,ollama,online_stub}.py
  core/routing/{chain,privacy_guard,latency_guard,capability_match}.py
  core/search/{base.py, tavily.py, classify_source.py}（Web搜索插件+来源分级）
  core/ingress/{base.py}（IngressProvider接口，飞书预留）
  core/privacy.py（数字前后瞻正则，见§11修复①）, timing.py, verifier.py, notifier.py, report.py, utils.py
  tests/test_*.py  scripts/smoke_daily.py scripts/chaos_*.py
```

骨架现状：`schemas/transitions.py`（14态+让出边+`context_overflow`第五reason✅）、`schemas/models.py`（`schema_ref`改名+`long_running`/`rule_id`/`Source`/`Claim`✅，Budget已删max_tokens）、`core/{gateway(含本地门禁),privacy,timing,governor(双平台),utils,context(动态窗口)}.py`✅、`config/{gateway(分档+reserved+门禁),schedule,report(tuning段),search}.yaml`（privacy段+workers/governor段已补）、`docs/deploy-prereq.md`✅、`requirements.txt`（pydantic/pyyaml/anyio/psutil/pytest✅）。待编码：`core/search/*`、`core/ingress/*`、`core/planner.py`、`Store.transition`。

## 4. ID与字段规范

- `plan_id=template_YYYY-MM-DD`（Daily）或`adhoc_{uuid7}`（管道/拆分）；DB存UTC（协调世界时），展示Asia/Shanghai。
- `idempotency_key（幂等键）=plan/task/inputs_hash全hash` UNIQUE，禁截断。
- `run_id=plan_uuid7`透传；`priority 0..10枚举`（0..4管道，5普通，10 Daily）；`last_success_hash`判缓存；`cancel_requested`配`CANCEL_REQUESTED`事件；`privacy/complexity`保留，关闭时填public/simple。
- `TaskShard.task_id`正则`^[a-z0-9_]{1,64}$`；`AcceptanceRule.schema`改名`schema_ref`（与Pydantic父类属性重名告警，见§11修复③）。
- `TaskShard.needs_web: bool`（资讯/政策/消息类=true）；`freshness: none|recent|strict`（recent=730天，strict=180天）；`slice_rationale: str`（拆分依据，报告任务分解视图引用）；`long_running: bool`（planner判定预期耗时+允许手工覆盖，false拒让出，撞线重试自动置True自学习）。
- `AcceptanceRule.rule_id`（VERIFYING局部重入定位键，为空时按rule_index自动编号）。
- `Source{source_id,url,title,snippet,published_at,source_tier(A|B|C),tier_reason}`；`Claim{claim_id,text,source_ids[],verdict}`（结论必须挂来源，无来源=幻觉拒收）。
- `Budget`删`max_tokens`（语义改为provider级`context_window`），保留`max_steps/timeout_s`为任务级执行限制。

## 5. 状态机

`PENDING→BLOCKED/READY→RUNNING→SUBMITTED→VERIFYING→DONE/RETRY/FAILED/WAITING_APPROVAL（4h升级P0）；RUNNING→READY（子Agent主动让出，reason=context_full/steps_exhausted/timeout/cancel_requested/context_overflow，attempts不变，须带checkpoint，long_running=false拒让出）；终态DONE/DEAD_LETTER/CANCELLED/SKIPPED*`。`DONE`无出边；子Agent仅`RUNNING→SUBMITTED/FAILED/READY(让出)`；非法记REJECTED+P1。让出熔断：`session_switch_total>5`→P1告警+下次让出改走FAILED。

## 6. 存储/调度/租约/队列入口

- `Store`单写串行+`BEGIN IMMEDIATE（立即加写锁）`重试3次；`fencing`仅lease跃迁+1；`export`出事务；`schema_version+migrate（版本迁移）`+`VACUUM INTO`备份。
- DDL按Phase1（CHECK约束+`seq AUTOINCREMENT`+索引`idx_tasks_sched`）；`session_switch_total`列进tasks（或metrics聚合，Phase1定）。
- 租约 lease（限时持有权）：`owner+lease_until+fencing`；`acquire`仅READY/过期；心跳30s续120s；过期走`RUNNING→FAILED→RETRY→READY`（attempts+1在RETRY→READY边）；`request_cancel`置旗，宽限高优30s/默认60s，不让出`force_revoke`。
- 调度JobStore（持久化仓库）+`coalesce（合并错过）`+单实例+`misfire_grace3600s`；`ensure_plan`幂等；`recover_missed`；CLI含`--dry-run（试跑）/--rerun/--catchup（补跑）`。
- 入口硬限制：<100KB、命名正则、路径锁`artifacts/{date}/`、cmd白名单前置、单小时100；`business_rule`仅4种AST（抽象语法树）节点。

## 7. 编排/执行/动态算力（AnyIO + V5两档三源 + V7拆分层）

- 主循环（跨plan全局，单实例+优雅停机）：`recover→ingest_inbox→planner（LLM拆分：笼统问题→任务卡DAG，含拆分侧session恢复）→refresh_ready→aging（10min→5，20min→3）→dispatch_split（高优N-1槽+低优保1槽）→verify_async（池2）→export→wait_wakeup（wakeup.flag事件+10s/2s）`。
- 子Agent四段式（仅`needs_web=true`）：`①检索（search provider→raw_sources.json）→②筛选（freshness+来源分级）→③分析（只基于筛后资料，禁止参数内知识）→④产出（report.md+sources.json+claims.json）`；推理逻辑类跳过检索直接分析。
- 阻塞调用包`to_thread`，防卡事件循环（单线程轮流跑任务的核心）。
- 算力（部署目标32G Mac统一内存；切换逻辑V5-1~V5-5在8逻辑/16GB开发机验证✅）：

| 档位 | 判定 | CPU | 内存 | executor | verifier | 本地 |
|---|---|---|---|---|---|---|
| ACTIVE | 有输入/idle<5min/手动active | ~1/8（共享1槽，执行优先） | ≤2GB | 1（共享） | 0（排队） | 0（公开走在线） |
| IDLE | idle≥5min两确认/手动idle | ~85%（7核） | min(12GB，可用-2GB) | 4 | 2 | 1 |

- ACTIVE档`local=0`是**有意权衡**：优先响应速度，不省token（用户决策2026-09-28，设计写明防误改）。
- 手动：`python -m core.governor --mode idle|active|auto`写`state/governor.override`，手动优先于侦测，重启有效，切档记`GOVERNOR_SWITCH{by}`；`headroom（余量）2GB`不足停派；高优ACTIVE可+1破格（内存红线不破）。
- 配置：`governor: {idle_after_s:300, confirm_samples:2, active_cpu:0.125, idle_cpu:0.85, mem_headroom_gb:2, high_burst:1}`。

## 8. 网关/搜索（隐私默认关）与报告（只链不搬）

- 网关插件链`privacy_guard→latency_guard→capability_match→cost_saver`；能力`3B→simple/4B→simple/7B→medium/complex走在线`；验证结论G1-G5✅（secret强制本地、simple本地、complex在线、过载公开切在线且隐私不切）。
- `Provider.chat()`返回契约：`{content, usage:{prompt_tokens, completion_tokens}}`（session切换判定依据）；窗口动态计算`final=min(架构上限,档位建议,显存可撑,配置覆盖)`-`context_reserved`(8192)，`usable<min_usable`(16384)→本地不可用→`CapabilityMatch`门禁改走在线（privacy=secret绕过门禁，永不上云）；`resolved` vs `effective`对账（启动自检+每次切档）防Ollama静默截断num_ctx。
- 搜索插件链（与网关同构可插拔）：`core/search/{base,tavily}.py`（exa/bing预留桩）+`config/search.yaml`（provider/api_key_env/timeout/top_k/日额度）+`classify_source.py`（域名先验+LLM逐条判定，禁喂正文）。
- 隐私关闭时：`enqueue`跳扫描、`route`跳`classify`、日志跳脱敏；接口`classify/redact/PrivacyGuard`保留，`test_privacy`默认skip。正则已修数字前后瞻（中文旁`\b`漏检，见§11修复②）。
- 本地保护：并发按§7（ACTIVE0/IDLE1）、`slo_p50_8s`、队列>4/连败3熔断5min半开、simple20s/medium60s超时切换；高优公开直走在线。
- 汇总`reports/{plan}.summary.md+json`：失败置顶/Top5/阻塞/甘特/网关用量/任务分解视图（分片主题+依赖+slice_rationale，体现拆分质量），只做索引不重复贴模型表；任务级`artifacts/{date}/{task}/tuning.md`自包含诊断（每行带"说明"列）；日报一屏总览+算力画像+弱建议调参，全链任务输出报告，缺失标`[缺失]`+P1；日报18:00合集。计时`timing.span()`7处✅（C1-C2）。

## 9. 校验/观测/计时

- 验证器AnyIO协程池；白名单+`shell=False（不走系统壳）`+flaky重试不计attempts；`final_consistency`逐plan；`AlertPolicy（去重策略）`5min合并，P0仅死信/不一致/高优SLA超时；`metrics`留30天。
- 调研质量验收4新规则：`source_traceable`（每条claim的source_id存在于sources.json，URL合法）、`freshness`（published_at距今≤阈值：recent 730天/strict 180天）、`source_quality`（A类来源占比>50%）、`coverage`（预定义key_questions全部有非空回答）。
- metrics加`session_switch_total`（单任务切换次数，熔断依据）、`context_window_resolved/effective`（对账，防静默截断）、`route_decision{provider,reason}`（切在线原因分解）、`context_overflow_fallback`（>0即窗口配错）、`local_unavailable`（门禁拦截）；日报显示"本任务切了几次session"。

## 10. 阈值（定版）

老化10min→5、20min→3；预留1槽；熔断5min；高优SLA p95<60s（排队等待）；cancel宽限30s/60s；lease120s/心跳30s；报告plan终态30s落盘；session切换水位线=动态窗口`final`的80%、让出熔断K=5；`context_reserved`=8192、`min_usable`=16384、`system_reserved_gb`=5；freshness：recent=730天/strict=180天；来源A类占比>50%；调优阈值见`report.yaml`的`tuning`段。

## 11. 验证基线与coding前修复（33/33已过，带入coding）

已修并验证：①隐私正则`\b`→数字前后瞻；②`gateway.yaml`的`${ONLINE_BASE_URL}`加引号（YAML流映射解析失败）；③上述两处复测通过；④简化抢占模型（移除PREEMPTED/preemptible/preempt_count，15态→14态）；⑤Budget语义改为provider级context_window；⑥动态窗口（分档+reserved+门禁+双平台governor+让出前置+兜底reason），pytest 42 passed。
coding清单（剩余）：①`gateway.py`的`chat()`返回契约改usage；②`Store.transition`为第一块硬骨头（含让出边`long_running`前置+熔断计数）；③`core/search/*`+来源分级器；④`core/planner.py`（LLM拆分+`long_running`判定）；⑤`requeue_verify()`+`verified_rules`局部重入。

## 12. 开工顺序（竖切优先，每步有可运行产出，每步更新PROGRESS.json）

1. S0端到端骨架：手工任务卡→enqueue→stub LLM执行→验收→report.md（验证Store/状态机/Verifier三件套）。
2. S1拆分层：`planner.py`（LLM拆分，含拆分侧session恢复）+测试。
3. S2搜索层：`core/search/*`+来源分级器+config/search.yaml。
4. S3路由：complexity+needs_web分级路由+Provider usage返回契约。
5. S4 session切换：RUNNING→READY让出边+熔断+checkpoint扩字段（summary/artifacts_partial/done_steps/generation）。
6. S5 governor调度+老化+ACTIVE档有意权衡注释。
7. S6调研质量验收：4新规则handler+三件套产出契约。
8. S7完备性：混沌14项+smoke_daily（含调研冒烟）+覆盖率≥80%+飞书IngressProvider接口桩+PROGRESS.json全done。

## 13. 新会话首读清单（防失忆，按序读）

`IMPLEMENTATION_PLAN_V7.md`（本文件）→ `PROGRESS.json` → `docs/deploy-prereq.md`（部署前置） → `design/phases/00-overview.md` → `design/phases/Phase0~4-*.md` → `design/phases/REVIEW-architect.md`（历史评审记录）→ `orchestrator/core/{governor,gateway,privacy,context}.py` → `orchestrator/schemas/{transitions,models}.py`。
