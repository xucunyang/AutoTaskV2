# Phase3 校验/可观测设计（V7）

状态：已按V7对齐，待用户评审，前置依赖 Phase2执行冻结。
对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`；修订见文末§6。

## 1. 目标

- 无验收不DONE，验收本身防注入、防 flaky 误杀。
- 调研质量可机器验收：来源可追溯（A类>50%）、时效性（recent 730天/strict 180天）、覆盖度、无幻觉（结论必须挂来源）。
- 失败可定位（run_id一串到底），分级告警不骚扰，日报自动可审计。

## 2. 验证器 `core/verifier.py`

### 2.1 入口（异步worker池，不阻塞主循环，评审M7）

```python
def dispatch_verify_async(t): verify_tg.start_soon(verify, t.plan_id, t.task_id, t.run_id)  # AnyIO协程池(max_verify_workers=2)，高优验证优先，不阻塞主循环
def verify(plan_id, task_id, run_id) -> VerifyResult:
    t = get_task(...)  # 必须 VERIFYING，否则拒绝
    with timing.span("verify", plan_id, task_id, run_id):
        results = [dispatch_timed(rule) for rule in t.acceptance]  # per-rule计时，>60s标验收阻塞
    ... # DONE/RETRY/WAITING_APPROVAL同前
```

### 2.2 Rule handlers（全部沙箱化）

- `file_exists`：规范化路径，越界（`..`或非`artifacts/{date}/`）直接FAIL+安全告警。
- `row_count`：流式计数，不全量读。
- `python_test`：白名单 `pytest tests/`, `python tests/` 前缀，`subprocess.run(shell=False, timeout=rule.timeout_s, cwd=repo)`，输出截断5k，`flaky_retry=2`（仅测试层重试，不计任务attempts）。
- `json_schema`：读`AcceptanceRule.schema_ref`指向的`schemas/*.schema.json`做`jsonschema`校验。
- `business_rule`：受限表达式（`rows/null_rate/bytes` + `> < == && ||`），用AST白名单求值（仅`Compare/BoolOp/Name/Constant`四节点，评审M9），禁用`eval`。

### 2.2b 调研质量验收（V7新增，4 handler）

- `source_traceable`：加载`claims.json`与`sources.json`；每条claim的`source_ids`非空且全部存在于sources.json，且URL格式合法（http/https）；缺来源=幻觉，FAIL+`INCONSISTENT`候选。
- `freshness`：遍历`sources.json`的`published_at`，距今≤`freshness_days`（TaskShard.freshness渲染：recent=730/strict=180）；超期来源比例>阈值（默认30%，可配）→FAIL；`published_at`缺失的来源不计入A类统计且标`[无时间]`P2。
- `source_quality`：按`sources.json`的`source_tier`字段统计A类占比（`tier_reason`非空的条目才参与统计，保证分级有据）；A类占比>50%（`source_tier_min_ratio.A=0.5`）否则FAIL。
- `coverage`：逐条检查`TaskShard.key_questions`是否在report.md有对应非空section（按标题或标注匹配）；有未回答问题→FAIL+列出缺口。

### 2.2c VERIFYING局部重入（V7新增，不加状态边）

- 验收规则天然可重入：中断后重入时跳过`verified_rules`（已通过的`rule_id`列表），从断点规则接着跑。
- `rule_id`定位：`AcceptanceRule.rule_id`为空时按`rule_{index}`自动编号（老YAML不break）。
- 进度存`tasks.verify_progress_json`（DB列，走事务安全，不用checkpoint文件）。
- 中断=保存进度+释放租约，状态保持VERIFYING；主循环`requeue_verify()`捞出无lease的VERIFYING任务重派（见Phase2§2）。

- 四件套校验顺序：`json_schema`（格式）→ `source_traceable`（链接有效）→ `freshness`（时效）→ `source_quality`（等级占比）→ `coverage`（完整度）。任一FAIL整体FAIL，不短路后续（全量报告）。`tuning.md`由report.py生成，不参与验收规则，只在终检查存在性。

### 2.3 终检 `final_consistency(plan_id)`

- DAG无环、依赖满足、状态合法、无孤儿、产物存在+sha匹配、终态任务有验收记录。
- needs_web=true的任务必须有sources.json+claims.json+tuning.md（否则INCONSISTENT）。
- 任一项不过，计划不标`DONE`，记`INCONSISTENT` + P0告警。

## 3. 可观测 `core/notifier.py + metrics + reports`

### 3.1 日志与追踪

- `logs/structured.jsonl`：`{ts, level, run_id, plan_id, task_id, msg, elapsed_ms}`，所有transition/lease/verify必记。
- `events`表 + `events/YYYY-MM-DD.jsonl`：状态机审计，可`replay`重建快照。
- `metrics`表：`task_duration_s/retry_total/lease_expired_total/verify_fail_total/tokens_used/cache_hit/queue_wait_s/high_prio_schedule_s/session_switch_total/search_cost/source_tier_a_ratio/freshness_pass_rate/context_window_resolved/context_window_effective/route_decision/context_overflow_fallback/local_unavailable/governor_switch`，导出Prometheus文本 `reports/metrics.prom`供Grafana。
- 队列看板：`READY按priority分组积压数/最老等待/高优p95调度延迟/老化次数/让出次数`，日报独立章节。

### 3.2 告警分级

- P0（电话+群，仅三项，V7§9定版）：`DEAD_LETTER`、`INCONSISTENT`、高优SLA超时（入队p95<60s未RUNNING）。
- P1（群）：单次`FAILED/RETRY`、连续3次`FAILED`、安全越界、租约过期回收、`WAITING_APPROVAL`超时4h、队列积压>50/最老>1h、**session_switch_total>K熔断**、**拆分失败进DEAD_LETTER**。
- P2（日报聚合）：`SKIPPED_CACHED`、缓存命中率、耗时p50、老化统计、**来源无published_at**、**让出原因分布**。
- 实现：`Notifier.send(level, title, body, run_id)`，`print+写logs+写reports/alerts.jsonl`，预留Webhook。`AlertPolicy{dedup_window=5min, max_per_hour}`同task+reason合并，P0仅`DEAD_LETTER/INCONSISTENT/高优SLA超时`（评审M8）。
- `metrics` retention 30天分区，日表滚动；`final_consistency`按plan维度执行（跨plan全局循环下逐plan判定）。

### 3.3 日报 `templates/daily_report.md.j2 + core/report.py`

- 每次plan终态30s内生成`reports/{plan_id}.summary.md + .json`：失败置顶/耗时Top5/阻塞点/甘特/网关用量/**任务分解视图**/**调研质量章节（A类占比/时效性达标率/覆盖度）**（阈值`config/report.yaml: TopN=5`）；每天18:00再合`reports/YYYY-MM-DD.md`。
- 数据来源仅DB聚合，不读聊天记录。时区按上海自然日切分（DB存UTC，展示+8）。
- 任务分解视图：分片主题+依赖+`slice_rationale`+`planner_run_id`（拆分质量可审计，Phase2附录C）。
- 三层报告结构（只链不搬）：任务级`artifacts/{date}/{task}/tuning.md`自包含诊断（每行带说明列）；`{plan}.summary.md`只做索引（一行一任务+链接，不重复贴模型表）；日报一屏总览+算力画像+弱建议调参+质量告警行。

## 4. DoD

- [ ] 恶意`cmd: rm -rf`被白名单拒绝 + 安全告警。
- [ ] flaky测试重试2次后通过不计任务重试。
- [ ] 全链路`run_id`可从调度grep到日报。
- [ ] `final_consistency` single-fail用例拦截计划DONE。
- [ ] 幻觉拒收：claim无source_id或source_id不存在→FAIL。
- [ ] 时效拒收：source超期比例>30%→FAIL；recent/strict天数分别生效。
- [ ] 等级拒收：A类占比≤50%→FAIL；tier_reason缺失条目不参与统计。
- [ ] 覆盖拒收：key_questions有未回答项→FAIL+缺口列表。
- [ ] 来源分级注入：snippet正文含"A类权威"字样不影响分级结果（分级器不喂正文）。

## 5. V6→V7 变更清单（2026-09-28，本轮，待评审）

- §2.2b（新增）：调研质量验收4 handler（source_traceable/freshness/source_quality/coverage）+四件套校验顺序+不短路原则。
- §2.2c（新增）：VERIFYING局部重入（rule_id定位+verified_rules跳过+requeue_verify，不加状态边）。
- §2.3：终检加needs_web四件套存在性检查。
- §3.1：metrics加session_switch_total/search_cost/source_tier_a_ratio/freshness_pass_rate/**context_window_resolved/effective/route_decision/overflow_fallback/local_unavailable/governor_switch**；看板加让出次数。
- §3.2：P1加熔断告警/拆分失败告警；P2加来源无时间/让出原因分布。
- §3.3：日报加任务分解视图+调研质量章节+三层报告结构（任务tuning/汇总索引/日报总览+质量告警行）。
- §4：DoD加幻觉/时效/等级/覆盖/注入五条。
- 本轮（动态窗口+局部重入+三层报告）：§2.2b三件套→四件套；§2.2c新增局部重入；§2.3终检查四件套；§3.1 metrics加6个上下文/路由埋点；§3.3加三层报告结构。
- 前轮（V6）保留：AnyIO协程池、schema_ref、P0三项、metrics留30天、上海自然日切分。
