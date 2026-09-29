# Phase0 契约设计：状态机 + 数据模型 + 验收DSL（V7）

状态：已按V7对齐，待用户评审（评审通过后才可进入Phase1编码）。
对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`；修订见文末§9。

## 1. 目标

- 所有状态、任务卡、产物、验收都有机器可校验的Schema，非法数据在入口被拒。
- 状态跃迁有白名单，`DONE`终态不可变，杜绝`READY->DONE`直写。
- 无PREEMPTED状态（抢占简化为排队等待，见V7§2原则8）；新增`RUNNING→READY`主动让出边（session切换）。
- 契约覆盖调研场景：来源（Source）、结论-证据（Claim）、时效性（freshness）、拆分依据（slice_rationale）。

## 2. 状态机设计

### 2.1 状态定义

```
PENDING → BLOCKED / READY
BLOCKED → READY (依赖全DONE由系统推进)
READY → RUNNING / SKIPPED / SKIPPED_CACHED / CANCELLED
RUNNING → SUBMITTED / FAILED / CANCELLED / READY（主动让出，session切换）
SUBMITTED → VERIFYING (仅系统)
VERIFYING → DONE / RETRY / FAILED / WAITING_APPROVAL
WAITING_APPROVAL → DONE / RETRY / CANCELLED (SLA 4h超时升级P0)
RETRY → READY (attempts+1，指数退避 60*2^attempts)
FAILED → RETRY / DEAD_LETTER (超max_attempts=3进死信)
终态：DONE / DEAD_LETTER / CANCELLED / SKIPPED / SKIPPED_CACHED
```

> 抢占说明：高优任务（priority 0..4）入队后排在Daily低优（priority 10）前面，但**不打断正在执行的任务**；当前任务完成后，主循环下一轮自然派发队列中优先级最高的READY任务。无PREEMPTED状态、无抢占协作、无preemptible/preempt_count字段（V7§2原则8）。

> 让出说明（`RUNNING→READY`）：子Agent在模型上下文超动态窗口`final`的80%、steps超限、timeout超时、收到`cancel_requested`、或撞`context_length_exceeded`硬错时，保存checkpoint后主动让出。attempts不变（不算失败）；**必须带checkpoint**（无checkpoint让出=丢失现场，非法拒绝）；`reason ∈ {context_full, steps_exhausted, timeout, cancel_requested, context_overflow}`；`long_running=false`拒让出（短任务走失败重试+自动置True自学习）；`session_switch_total`超K（默认5，可配）→P1告警+下次让出改走FAILED（防无限让出烧token）。

### 2.2 跃迁表（代码唯一来源 `schemas/transitions.py::ALLOWED`）

- 校验点：`core/store.transition()`内 `if to not in ALLOWED[from]`抛`IllegalTransition` + 记`events(REJECTED)` + P1告警。
- 子Agent只允许：`RUNNING->SUBMITTED/FAILED/READY(让出)`；Verifier只允许：`VERIFYING->DONE/RETRY/FAILED/WAITING_APPROVAL`；Orchestrator拥有全部系统跃迁。
- 权限矩阵：`SYSTEM_ONLY={VERIFYING}`；`ORCH_ONLY={(SUBMITTED,VERIFYING)}`；`SELF_YIELD={(RUNNING,READY)}`（仅持有正确fencing的owner可写，须带reason+checkpoint断言，受熔断约束）。
- `DONE`无出边，任何写入直接拒绝。

### 2.3 补充规则

- 只有`depends_on`全`DONE`（或`SKIPPED/SKIPPED_CACHED`视同满足，按模板`strict`开关）才可`BLOCKED/PENDING->READY`。
- `CANCELLED`向下级联：取消上游自动取消未启动下游。
- `RETRY`必须带`reason + attempts`，`DEAD_LETTER`必须带`reason + last_error + 人工SOP链接`。
- 让出必须带`reason`+`checkpoint`存在性断言；`session_switch_total>K`熔断。

## 3. Pydantic模型（`schemas/models.py`）

- `Budget{max_steps=20, timeout_s=900}` — 任务级执行限制（步数+时长上限）。**已删`max_tokens`**：上下文上限改为provider级`context_window`（模型窗口），80%水位线判定在`Provider.chat()`返回的`usage.prompt_tokens`上做（见Phase2 §6b）。
- `ArtifactRef{path, sha256, bytes, rows}` — 输入声明。
- `AcceptanceRule{type: file_exists|row_count|python_test|json_schema|business_rule|source_traceable|freshness|source_quality|coverage, path/cmd/min/schema_ref/expr/freshness_days/source_tier_min_ratio/key_questions/rule_id, timeout_s}`（`schema`已改名`schema_ref`，避Pydantic父类属性重名告警，V7§11修复③；`rule_id`为空时verifier按rule_index自动编号）。
- `Handoff{done, summary≤2000字, artifact_refs, tests_passed, tokens_used, open_issues}` — 不合格打回RETRY且不计业务重试外另计`handoff_reject`。
- `TaskShard{task_id, plan_id, status, version, fencing_token, owner, lease_until, idempotency_key, inputs_hash, attempts/max_attempts, objective, inputs/outputs/depends_on, acceptance, handoff, budget, priority, source, seq, last_success_hash, cancel_requested, enqueued_at, privacy, complexity, needs_web, freshness, slice_rationale, key_questions, long_running}`。
  - `priority: int 0..10` 越小越优先，枚举校验，`0..4=管道高优，5=普通，10=Daily低优`。
  - `source: daily|pipeline`；`seq: int` 由DB AUTOINCREMENT分配（禁`max+1`），同优先级按seq先入先出。
  - `needs_web: bool` 能力开关（非路由维度）：资讯/政策/消息类=true（必须Web search取最新，禁用参数内知识），推理/方法步骤/逻辑类=false。
  - `freshness: none|recent|strict`（recent=730天，strict=180天，V7§10定版）；仅needs_web=true时生效。
  - `slice_rationale: str` 拆分依据（planner写，报告任务分解视图引用，判断拆分质量）。
  - `key_questions: List[str]` 预定义关键问题（coverage验收依据）。
  - `long_running: bool` 预期耗时长短（planner判定+允许任务卡手工覆盖）；false拒让出，撞线重试自动置True自学习。
  - `last_success_hash` 缓存命中比对用；`cancel_requested: bool` 协作中断旗标（配`CANCEL_REQUESTED`事件审计，评审B2）。
  - `privacy: public|internal|secret` + `complexity: simple|medium|complex` 网关路由输入。
  - `idempotency_key={plan}/{task}/{inputs_hash全hash}`（禁截断8位，防碰撞，评审Minor1）。
- `Source{source_id, url, title, snippet, published_at, source_tier: A|B|C, tier_reason}` — 结构化来源（sources.json一行一条）。`tier_reason`必填（分级理由落盘可复核）。
- `Claim{claim_id, text, source_ids: List[str], verdict}` — 结论-证据映射（claims.json）。`source_ids`非空且存在于sources.json（无来源=幻觉，验收拒收）。
- `QueueIngress`只追加log：`inbox/done|error`文件即凭据，不设可变状态队列表；`tasks`为唯一队列（评审B1）。
- `ArtifactManifest{path, sha256, bytes, rows, preview[5行×500字]}`。
- `Checkpoint{task_id, plan_id, step, cursor, tokens_used, updated_at, summary: List[str], artifacts_partial: List[ArtifactRef], done_steps: List[int], generation: int}`。
  - `summary`：最近N步摘要（LLM生成，仅作辅助不作准）。
  - `artifacts_partial`：已产出的中间产物路径+sha（结构化事实，恢复精度不依赖LLM自我总结）。
  - `done_steps`：已完成步骤id列表（幂等续跑依据）。
  - `generation`：第几代session（观测用，日报显示"本任务切了几次session"）。

校验策略：启动/派发/写回三处 `model_validate`，失败记`SCHEMA_REJECT`不派发。

## 4. 验收DSL设计

示例 `schemas/acceptance_t2.yaml`：
```yaml
- type: file_exists
  path: artifacts/{date}/t2_clean.csv
- type: row_count
  path: artifacts/{date}/t2_clean.csv
  min: 1
- type: python_test
  cmd: pytest tests/test_clean.py -q
  timeout_s: 120
```

- `{date}`模板变量由调度器渲染，不允许`..`/`/`越界，渲染后必须落在`artifacts/{date}/`下。
- `cmd`仅允许白名单前缀（`pytest tests/`, `python tests/`），`shell=False`，见Phase3沙箱。
- `business_rule`表达式用受限求值器（AST白名单仅`Compare/BoolOp/Name/Constant`四种节点，评审M9），禁用`eval/__import__`。

### 4.1 调研质量验收（V7新增，4规则）

示例 `schemas/acceptance_research.yaml`：
```yaml
- type: source_traceable   # 每条claim的source_id存在于sources.json，URL合法
- type: freshness          # published_at距今≤阈值
  freshness_days: 730      # recent=730 / strict=180，由TaskShard.freshness渲染
- type: source_quality     # A类来源占比
  source_tier_min_ratio: {A: 0.5}   # A类>50%（用户决策2026-09-28）
- type: coverage           # 预定义问题全回答
  key_questions: ["要点1", "要点2"]
```

- 四件套产出契约（needs_web=true必出）：`report.md`（给人看）+`sources.json`（机器可验）+`claims.json`（结论-证据映射）+`tuning.md`（任务级调优诊断，自包含，每行带说明列）。
- 原则：**MD是给人看的，json是给机器验的**——只有MD就只能靠LLM主观判断，失去验收意义。

## 5. 任务卡契约（`templates/task_card.j2`）

只给白名单字段：`task_id/plan_id/run_id/objective/inputs_manifest(路径+sha256+preview)/acceptance/budget/outputs/idempotency_key/checkpoint.cursor/needs_web/freshness/key_questions`。
禁止给：master全文、其他任务分片、全量events、大文件内容。
`needs_web=true`时加注：**仅基于检索资料回答，资料未覆盖的部分标注[未覆盖]，禁止使用模型参数内知识**。

## 6. 目录与文件契约

- `templates/daily_plan.yaml`：DAG模板，含`id/objective/depends_on/outputs/acceptance_ref/max_attempts/priority(默认10)/source(默认daily)/needs_web/freshness/key_questions`。
- `templates/pipeline_task.json`：管道入口模板，含`task_id/objective/inputs/priority(默认0)/max_attempts/needs_web/freshness`，经`enqueue`校验后入队。
- `schemas/summary.schema.json`：产物JSON Schema示例。
- `config/schedule.yaml`：见Phase1。
- `config/search.yaml`：搜索provider配置（V7新增，见Phase2附录D）。
- 报告契约：`reports/{plan}.summary.md`含**任务分解视图**（分片主题+依赖+slice_rationale链接+planner_run_id，体现拆分质量）。

## 7. DoD（完成标准）

- [ ] `pytest tests/test_transitions.py` 全跃迁正反例通过（含`RUNNING->READY`让出正例、无checkpoint让出拒、熔断超限拒，`READY->DONE`/`DONE->*`反例）。
- [ ] `pytest tests/test_queue_order.py` 同优先级FIFO、跨优先级高优先行通过。
- [ ] `pytest tests/test_models.py` 非法handoff/manifest被拒；Source缺tier_reason拒；Claim空source_ids拒。
- [ ] 任务卡渲染后token估算<2k且不含敏感字段（人工抽查3例）。
- [ ] 设计评审通过（本文件签字：通过/打回）。

## 8. V6→V7 变更清单（2026-09-28，本轮，待评审）

- §2.1：状态机加`RUNNING→READY`让出边（reason五值+checkpoint断言+long_running前置+熔断K=5）；14态不变（让出是边不是新状态）。
- §2.2：权限矩阵加`SELF_YIELD={(RUNNING,READY)}`。
- §3：`Budget`删`max_tokens`（语义改为provider级`context_window`）；`TaskShard`加`needs_web/freshness/slice_rationale/key_questions/long_running`；`AcceptanceRule`加`rule_id`；新增`Source/Claim`模型；`Checkpoint`加`summary/artifacts_partial/done_steps/generation`。
- §4：验收DSL加4规则（source_traceable/freshness/source_quality/coverage）+四件套产出契约（含tuning.md）。
- §5：任务卡加`needs_web/freshness/key_questions`+禁参数内知识注。
- §6：加`config/search.yaml`+报告任务分解视图契约。
- 本轮（动态窗口+兜底reason）：§2.1让出reason四值→五值（+context_overflow）+long_running前置；§3补long_running/rule_id字段说明；§4产出三件套→四件套（+tuning.md）。
- 前轮（V6）保留：简化抢占模型、schema_ref改名、AST四节点白名单、14态状态机。
