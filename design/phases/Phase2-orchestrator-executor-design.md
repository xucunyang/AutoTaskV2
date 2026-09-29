# Phase2 编排/执行设计（上下文与成本核心）（V7）

状态：**已实现并落码**（2026-09-29）。**评审：通过**（用户 2026-09-29 确认；Phase0–4 阶段设计稿全部通过）。
DoD逐条见§8，实现与设计的偏差见附录F。
对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`；修订见文末附录E。

## 1. 目标

- 主Agent不爆上下文、可并发、可推进DAG；子Agent无状态、低token、可断点续跑、可session切换。
- 笼统问题经LLM拆分（planner）成任务卡DAG；任务列表为FIFO+优先级队列：同优先级先入先出，Daily（10）默认让路给管道（0），高优排队等待，当前任务完成后自然衔接执行。
- 资讯/政策/消息类任务（needs_web=true）必须Web search取最新资料；大产物不进上下文，重复输入可跳过执行省成本；低优不被饿死（老化+预留槽）。

## 2. 主循环 `core/orchestrator.py`

```python
while True:  # 跨plan全局循环，支持优雅停机+orchestrator_lock单实例
    check_shutdown()                         # SIGTERM/DONE即退
    recover_expired_leases()                 # RUNNING→FAILED（租约过期，被动失联）
    ingest_inbox()                           # 扫inbox/入队，写后touch wakeup.flag
    planner()                                # V7: LLM拆分（笼统问题→任务卡DAG，见§2b）
    for plan_id in active_plans():
        validate_dag(plan_id)
        refresh_ready(plan_id)               # PENDING/BLOCKED→READY
    apply_aging()                            # 多档提升10→5→3
    dispatch_split()                         # 两阶段派发：高优N-1槽+低优预留1槽（评审M5）
    for t in get_by_status_global("SUBMITTED"):
        transition(t, "VERIFYING", agent="orchestrator")
        dispatch_verify_async(t)             # 异步verifier池，不阻塞主循环（评审M7）
    export_all_masters()
    wait_wakeup(poll_normal=10, poll_fast=2) # wakeup.flag事件唤醒替代纯sleep（评审M2）
```

```python
def dispatch_split():
    high = list_ready_ordered(priority_max=4, limit=max_workers-1)
    low = list_ready_ordered(priority_min=5, limit=1) if low_wait_gt(300) else list_ready_ordered(priority_min=5, limit=free_after(high))
    for t in high + low:  # 仍各段内 ORDER BY priority,seq
        if cache_hit(t): transition(t, "SKIPPED_CACHED"); continue
        if acquire_lease(t, owner="orchestrator-main"): spawn_sub_agent(...)
```

- `validate_dag`：用拓扑排序判环，`depends_on`不存在/自依赖直接`FAILED+告警`不派发。
- `refresh_ready`：`strict=true`要求依赖全`DONE`，`false`允许`SKIPPED*`视同满足（模板开关）。
- 并发：workers按`schedule.yaml`的V5两档（ACTIVE执行与验证共享1槽约1/8，IDLE executor4/verifier2/local1约85%，`governor`阈值见V7§7），全局单编排实例（DB行锁`orchestrator_lock`）；`headroom 2GB`不足停派，高优ACTIVE可+1破格（内存红线不破）。
- ACTIVE档`local=0`是**有意权衡**：优先响应速度，不省token（用户决策2026-09-28，设计写明防误改）。
- IDLE档`local_concurrency=1`是14B@64K的前提（权重8.4G+KV12G=20.4G/32G，单并发刚好；开2并发KV翻倍直接爆）。**改并发前必须重算`resolve_window()`**，窗口计算跟着并发走，不写死。
- 幂等派发：`READY + 无lease + version未变`才派，派发即`READY->RUNNING`占位防重派。
- 排序：派发查询强制`ORDER BY priority ASC, seq ASC`，禁止内存二次排序，保证FIFO语义单一来源。

## 2b. 拆分层 `core/planner.py`（V7新增）

- 入口：`inbox/planner/` 目录投递笼统问题（自然语言），或管道JSON`{type: question, text}`。
- 流程：LLM输出结构化JSON → Pydantic校验 → `INSERT plans + tasks(PENDING)`同一事务 → 投递方收`plan_id`。
- 拆分输出契约（LLM必须输出此结构，校验失败进`inbox/error/`+告警）：
```json
{
  "plan_title": "调研主题",
  "key_questions": ["要点1", "要点2", "..."],
  "tasks": [
    {"task_id": "t1_collect_a", "objective": "...", "depends_on": [],
     "complexity": "simple", "needs_web": true, "freshness": "recent",
     "slice_rationale": "为什么切这一片", "outputs": ["artifacts/{date}/t1_a.md"]}
  ]
}
```
- **拆分侧session恢复**：planner本身是长任务（一个问题拆10~20张卡），上下文超80%→保存拆分checkpoint（已拆出前N张卡+剩余问题）→切新session继续拆。`plan_id=adhoc_{uuid7}`，拆分结果整体hash幂等（同一问题不重复拆）。
- 拆分质量审计：`planner_run_id/planner_model/planner_prompt_version`落plan记录；报告任务分解视图引用`slice_rationale`。
- 不人工确认（用户决策2026-09-28）：拆完直接入队派发，质量靠报告复盘。
- 拆分失败：进DEAD_LETTER + P1告警，投递方收到失败通知（预留）。

## 3. 高优排队等待（无抢占）

- 管道高优（priority 0..4）入队后按`ORDER BY priority ASC, seq ASC`排在Daily低优（priority 10）前面。
- **不打断正在执行的任务**：当前RUNNING任务完成后，主循环下一轮`dispatch_split`自然派发队列中优先级最高的READY任务。
- 高优SLA：`p95<60s入队→RUNNING`（V7§10定版，排队等待非抢占），超时P0告警（评审M8去重：同task 5min合并）。
- 中断及时性：执行器tool调用后+每5s wall-clock双条件检查`cancel_requested`（人工取消用）；配合wakeup 2s快路径。

## 4. 防饿死（低优不被长期压住）

- 老化提升：两档，`READY`等待>10min→5、>20min→3（每次老化检查可升一档，上限3），记事件`AGED`（V7§10定版）。
- 预留槽：`max_workers=4`时至少保留1槽给`priority>=5`（若低优等待>5min且高优连续占用全槽，强制下一空槽派低优）。
- 队列积压告警：`READY积压>50`或最老等待>1h → P1告警。

## 5. 子执行 `core/executor.py`

```python
def run_task(plan_id, task_id, run_id):
    t = get_task(...)  # 读导出JSON + DB行
    if t.status in TERMINAL: return "already_terminal"
    card = render_task_card(t)               # Phase0模板，仅白名单字段
    if not acquire_lease(t, owner=f"sub-{uuid4()}", ttl=120): return "lease_busy"
    start_heartbeat(t)                       # 30s续租至120s，异常自动停
    try:
        ckpt = load_checkpoint(t)            # 有则从cursor续跑（含summary/artifacts_partial/done_steps/generation）
        if t.needs_web:
            sources = search_and_filter(t)   # 四段式①②（见§5b）
        result = llm_analyze_and_write(card, sources, budget=t.budget, resume=ckpt)  # ③④
        atomic_write_artifacts(result)       # tmp+fsync+rename + manifest（report.md+sources.json+claims.json）
        if not self_test(t): raise SelfTestFail
        transition(t, "SUBMITTED", handoff=build_handoff(result))
    except SessionYield as y:                # 水位线/步数/超时/取消 → 主动让出
        save_checkpoint(t, y.reason)         # 含artifacts_partial/done_steps/summary
        transition(t, "READY", reason=y.reason)  # RUNNING→READY，attempts不变，checkpoint断言+熔断在Store层
    except Exception as e:
        transition(t, "FAILED", payload={"error": str(e)[:2000]})
    finally:
        stop_heartbeat(); maybe_release_lease()
```

- `render_task_card`：`inputs_manifest`只含`path/sha256/bytes/rows/preview`，`>200KB`强制preview模式。
- `read_range(path, offset, limit)`：子Agent按需分页读大文件，不一次性载入。
- `atomic_write_artifacts`：产物写`artifacts/{date}/{task}.*` + `manifest.json`（sha/bytes/rows/preview）。
- `self_test`：跑`acceptance`中轻量项（`file_exists/row_count`），重型`python_test`留给Verifier。
- `build_handoff`：Pydantic校验，不合格打回`RETRY(reason=handoff_reject)`。
- 协作式取消：`llm_or_tool_execute`必须拆步（每步≤5s或每tool调用后）检查`cancel_requested`；命中即`save_checkpoint + RUNNING→READY(reason=cancel_requested)`。

## 5b. 四段式检索流程（仅`needs_web=true`，V7新增）

```
① 检索   search provider（core/search/）→ raw_sources.json（含published_at原文时间）
② 筛选   按published_at过滤（freshness: recent=730天/strict=180天）
         + 来源分级（classify_source：域名先验+LLM逐条判定）
③ 分析   LLM只基于筛后资料推理（提示词禁止参数内知识，未覆盖处标[未覆盖]）
④ 产出   report.md + sources.json + claims.json（每条结论带source_id）
```

- 检索provider接口：`SearchProvider{search(query, top_k) -> [SearchResult]}`，`SearchResult{url,title,snippet,published_at}`。
- 来源分级（V7§2.10）：域名先验（官方域名直接A，平台型域名x.com/tiktok/douyin/weibo必须LLM逐条判）+LLM判定（**只喂url+域名+title+发布者，禁喂snippet正文防注入**）+结果落盘`source_tiers.json`（source_id绑定，tier_reason必填可复核）。
- 无幻觉强制：claims.json每条结论必须带source_id指向sources.json；模型凭记忆写的结论无source_id→验收拒收（Phase3）。

## 6. 检查点与缓存

- `state/checkpoints/{task}.json`：`{step, cursor, tokens_used, updated_at, summary[], artifacts_partial[], done_steps[], generation}`，每次让出/预算检查点覆盖写。
- **恢复精度不依赖LLM自我总结**：结构化事实（artifacts_partial/done_steps）落文件，LLM摘要（summary）只作辅助提示。
- `cache_hit`判定：`inputs_hash == last_success_hash && 产物sha存在且匹配` → `SKIPPED_CACHED`，记`saved_tokens` metrics。
- 产物GC：保留最近7天，`manifest`永久，冷产物记`archived_path`。

## 6b. Session 切换与恢复（V7正式化）

### 触发条件（任一）
- 模型上下文超水位线：`usage.prompt_tokens / final_window >= 0.8`（分母=动态窗口`final`=`core/context.py:resolve_window()`算出，非任务预算；prompt_tokens=当前上下文，非累计消耗）
- `steps` 超过 `max_steps`（默认20）
- `timeout_s` 超时（默认900s）
- `cancel_requested` 命中
- 撞`context_length_exceeded`硬错（`reason=context_overflow`，强制让出+水位线自适应下调）

### 判定时机
- 每次`gateway.chat()`返回后检查`usage.prompt_tokens`（Ollama的`prompt_eval_count`/OpenAI的`usage.prompt_tokens`现成字段）
- `Provider.chat()`返回契约：`{content, usage:{prompt_tokens, completion_tokens}}`

### 切换流程
```python
def chat_with_yield_check(card, ...):
    resp = gateway.chat(...)
    final = resolve_window(...)  # core/context.py：min(架构/档位/显存/配置)-reserved
    ctx_ratio = resp.usage.prompt_tokens / final
    if ctx_ratio >= 0.8:
        raise SessionYield(reason="context_full")   # 上层save_checkpoint+RUNNING→READY
    return resp
```

### 恢复协议
- 新session注入：`任务卡（重新渲染，幂等） + checkpoint.cursor + artifacts_partial（结构化事实） + summary（辅助提示）`
- 从`done_steps`断点继续，已完成的step不重复执行（幂等续跑）
- 熔断：`session_switch_total > K(5)` → Store层拒让出，改走FAILED（计attempts，正常重试/死信）；P1告警（怀疑拆分/prompt有问题）
- 并发影响：单任务让出不影响其他任务（各任务独立session）；主循环下一轮`dispatch_split`重新派发

## 7. 成本控制

- 上下文水位线：`context_window*0.8`（provider级），触发session切换；`max_steps/timeout_s`（任务级）超限同理。
- 批量派发按`inputs_hash`去重，同输入多任务复用产物。
- metrics：`tokens_used/task_duration/retry_count/cache_hit_rate/session_switch_total/search_cost`，日报聚合。

## 8. DoD

实现现状（2026-09-29，pytest 263 passed）。逐条核对见 `tests/test_dod_phase2.py`，
对应测试一并标出。**两条"部分通过"已如实标注，不含糊**。

- [x] 200MB CSV任务prompt长度<8k tokens（preview模式验证）。`test_dod1_big_file_prompt_budget`。CI用20万行（约7MB）验证——**机制与文件大小无关**，进上下文的只有sha/bytes/rows/preview五行。
- [x] kill子进程后从checkpoint续跑成功。`test_resume_after_process_kill`。
- [x] 相同输入二次触发命中SKIPPED_CACHED，省token可观测。`test_dod3_cache_hit_is_observable`（`metrics.cache_hit`可查）。
- [x] DAG环/孤儿被拦截不派发。`test_dod4_cycle_never_dispatched`。
- [x] FIFO：同优先级按seq派发顺序与入队一致（AUTOINCREMENT单测）。`test_dod5_fifo_same_priority`。
- [x] 高优排队：管道高优入队后p95<60s开始执行。`test_dod6_dispatch_latency_measured`（派发耗时可观测）+ `test_dod5_pipeline_jumps_ahead_of_daily`。**部分说明**：p95需要生产数据积累，单测只能验证"单次派发<60s且时长入库"，无法在CI里证明p95。
- [x] 防饿死：低优多档老化+预留槽生效，可审计。`test_dod7_aging_and_reserved_slot_auditable` + `test_reserved_slot_gives_low_priority_a_chance`。
- [x] Session切换：水位线触发→让出→新session恢复，attempts不变，done_steps不重复；无checkpoint让出拒；熔断超限改FAILED。`test_dod8_yield_never_counts_as_attempt` + `test_session_switch.py`。
- [x] 拆分：笼统问题→任务卡DAG，key_questions/slice_rationale齐全，同一问题二次拆分幂等。`test_split_is_idempotent`、`test_missing_slice_rationale_is_rejected`。
- [x] 检索：needs_web=true任务产出四件套（含tuning.md），未检索任务不产出sources.json。`test_write_four_set` + `test_dod10_*`。**部分说明**：真实四件套需要接LLM与真实搜索API；`tests`用注入的假provider验证流程与契约，未验证真实检索质量。

### 8.1 明确划到 Phase3 的部分

- **异步verifier池**（§2 `dispatch_verify_async`，评审M7）：Phase2只做了
  `SUBMITTED→VERIFYING` 的推进（`Orchestrator.promote_submitted`），
  真正的异步校验器池、规则并发与超时属Phase3。
- **告警去重**（评审M8"同task 5min合并"）：Phase2 的 `Store.alert()` 只追加
  不去重，去重属Phase3告警收敛。
- **报告渲染**（附录C）：Phase2 提供了数据源
  （`planner.decomposition_view` / `Store.daily_summary`），
  Markdown报告与日报合成本身属Phase3。
- **网关真实provider**（附录A `ollama`/`online_openai_compat`）：
  Phase0 已落 `route()` 与 `Provider` 契约，真实HTTP provider与
  熔断半开恢复属后续；Phase2 的执行器通过 `provider` 注入调用。

## 附录F. 实现与设计的偏差（2026-09-29 落码后记录）

1. **老化时钟取 `enqueued_at` 而非 `updated_at`**（用户 2026-09-29 确认）：老化自己会改`updated_at`，
   拿它当基准等于每次提升都把等待清零，第二档（20min）永远触发不了。
   等待时间必须单调递增。
2. **老化"一次只升一档"是硬约束**（用户 2026-09-29 确认）：等了25min也只到5，下轮检查再到3。
   一次跳到3会让分档失去意义——分档就是为了分级提优先级。
   下限是3不是0（0..2留给管道高优）。
3. **`Store.update_priority` 只许升不许降**（用户 2026-09-29 确认）：降级会让等待中的任务被无限推后，
   正好和防饿死的目标相反。每次提升记`AGED`审计行（含等待秒数）。
4. **让出被拒时执行器改走FAILED**（用户 2026-09-29 确认）：设计§6b要求"熔断超限改走FAILED"。
   实现上必须显式处理——若让出transition抛`IllegalTransition`而执行器不接，
   异常会逃出`run_task`，任务永久卡在RUNNING，只能等租约过期被回收（白等一个TTL）。
5. **执行器 `budget` 有兜底**：模板用了`budget.max_steps/timeout_s`，
   任务卡没显式写budget时传空dict会让StrictUndefined直接抛错，
   把执行器在渲染阶段就打崩——而"任务没写预算"是完全正常的输入。
6. **产物由模型自己写，执行器只确认落盘+补manifest**（用户 2026-09-29 确认该边界）：
   曾经把模型回复同一坨文本写进所有声明产物，导致 `summary.json` 里是markdown、
   `json_schema` 永远 `bad_json`，且报错指不到真因。产物归模型（它有工具），
   执行器一个都没落盘就报 `no_artifacts_produced`，不替模型编。
7. **planner 的 `done_steps` 存序号(int)不存task_id**（用户 2026-09-29 确认）：
   `Checkpoint.done_steps` 在Phase0契约里就是`List[int]`，塞字符串会被Pydantic拒绝。
   而且没必要存id——已拆出的卡在`tasks`表里，按`plan_id`查就有。
   **契约保持严格，checkpoint只记"拆到第几张+剩下什么"。**
   （用户选择：按任务颗粒度续跑即可，不追求拆分层的id级精度。）
8. **分级cache按URL存**：`state/source_tiers.json` 同一URL复用，重复检索不再调LLM。

## 附录A. 轻量网关（可插拔，防本地拖慢）

- 位置：`executor`内只调`gateway.chat(task_card)`，路由可换不改执行器。
- 插件两层：`Provider{chat,health}`（`ollama`/`online_openai_compat`）+ `RoutePolicy{decide}`链（`privacy_guard→latency_guard→capability_match→cost_saver`），`config/gateway.yaml`编排顺序。
- 能力：`3B→simple, 4B→simple, 7B→medium`，complex走在线；隐私默认关（`privacy.enabled=false`：`enqueue`跳扫描、`route`跳`classify`、日志跳脱敏，接口`classify/redact/PrivacyGuard`保留，`test_privacy`默认skip，V7§8）。
- `Provider.chat()`返回契约（V7）：`{content, usage:{prompt_tokens, completion_tokens}}`；窗口动态计算`final=min(架构上限,档位建议,显存可撑,配置覆盖)`（`core/context.py`），session切换判定依赖。
- 本地保护：`max_concurrency=2, slo_p50=8s`，队列>4/连续失败3次熔断5min半开恢复；simple本地20s/medium60s超时即切（公开）或checkpoint让出（隐私）；高优公开跳过本地直走在线。
- 降级：本地挂→公开切在线，隐私只本地重试；日在线额度超全切本地+P1。用量记`metrics(provider,tokens,latency)`。
- DoD：公开simple 80%本地、隐私100%本地可审计、kill ollama公开自动切在线。

## 附录B. 全环节计时（7处埋点，`core/timing.span()`）

- Span：`queue_wait/enqueue→READY, dispatch_wait/READY→RUNNING, execute/RUNNING→SUBMITTED（含llm/tool/self_test子span）, verify, export_write`，monotonic计时，`metrics(span,duration_ms)`。
- 阻塞规则进`config/report.yaml`：queue p95>60s判队列阻塞、dispatch判调度阻塞、llm占比>80%判模型慢、单rule>60s判验收阻塞、export>500ms判磁盘慢。

## 附录C. 报告（含任务分解视图，V7新增）

- `reports/{plan}.summary.md` 章节：失败置顶/耗时Top5/阻塞点/甘特/网关用量/**任务分解视图**/调研质量（A类占比/时效性达标率/覆盖度）。
- 任务分解视图：分片主题列表+依赖关系+每片产出链接+`slice_rationale`+`planner_run_id/planner_model/planner_prompt_version`——体现拆分质量，供复盘调prompt。
- 日报18:00合集`reports/YYYY-MM-DD.md`；汇总只链接不搬内容。

## 附录D. 搜索插件层（V7新增，可插拔）

- `core/search/base.py`：`SearchProvider{search(query, top_k) -> [SearchResult]}`、`SearchResult{url,title,snippet,published_at}`。
- 实现：`tavily.py`（首个）；`exa.py`/`bing.py`预留桩。
- `core/search/classify_source.py`：来源分级器——域名先验（`config/search.yaml`的`official_domains`直接A）+平台型域名（`ugc_domains`）强制LLM逐条判+未知域名LLM判；LLM判只喂`url+域名+title+发布者`（**禁喂snippet正文，防注入**），输出`{tier, reason}`，本地小模型做（省钱快）。
- `config/search.yaml`：`provider/api_key_env/timeout_s/top_k/日额度/official_domains[]/ugc_domains[]/tier_prompt`。
- 用量记`metrics(search_cost, query_count)`，日额度超P1告警。
- DoD：官方域名不调LLM直接A；平台域名逐条判（蓝V判A案例）；同一URL分级结果落盘复用。

## 附录E. V6→V7 变更清单（2026-09-28，本轮，待评审）

- §2：主循环加`planner()`步骤；并发补ACTIVE档有意权衡注释。
- §2b（新增）：拆分层planner——LLM拆分契约/拆分侧session恢复/幂等/质量审计/不人工确认。
- §5：子执行加`SessionYield`分支（让出走RUNNING→READY）；四段式检索流程（§5b新增）。
- §6：Checkpoint扩字段（summary/artifacts_partial/done_steps/generation）；恢复精度不依赖LLM自我总结原则。
- §6b：正式化——80%分母=动态窗口final（`core/context.py:resolve_window()`四层min）、判定=usage.prompt_tokens、chat()返回契约、熔断K=5、恢复协议注入内容明确、撞错兜底（context_overflow+自适应下调）。
- §7：metrics加session_switch_total/search_cost。
- §8：DoD加Session切换/拆分/检索三条。
- 附录A：chat()返回契约+context_window配置。
- 附录C（新增）：报告任务分解视图。
- 附录D（新增）：搜索插件层+来源分级器+注入防护。
- 本轮（动态窗口+单并发+四件套）：§6b触发条件改动态final+加context_overflow兜底；§2补IDLE档local_concurrency=1前提注释（14B@64K物理必须）；§8 DoD检索项三件套→四件套。
- 前轮（V6）保留：排队等待模型、两档老化、ttl=120、隐私默认关三跳过、简化抢占模型。
