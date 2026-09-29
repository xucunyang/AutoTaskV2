# Phase4 验证设计：单元 + 混沌 + 端到端冒烟（V7）

状态：**已实现并落码**（2026-09-29）。**评审：通过**（用户 2026-09-29 确认；Phase0–4 阶段设计稿全部通过）。
DoD逐条见§6，实现与设计的偏差见§9。
对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`；修订见文末§8。

## 1. 目标

- 用最小用例证明生产级三件套：不丢状态、不重副作用、不爆上下文。
- V7新增验证面：session切换与恢复、LLM拆分、Web搜索+来源分级、调研质量验收。

## 2. 单元测试（`pytest`）

- `tests/test_transitions.py`：ALLOWED正例全覆盖 + 非法`READY->DONE/RUNNING->DONE/DONE->*`反例；**V7新增：`RUNNING->READY`让出正例（reason五值+checkpoint+long_running前置）、无checkpoint让出拒、熔断超限拒、attempts不变断言**。
- `tests/test_models.py`：非法handoff（超长summary/缺字段）、manifest缺sha被拒；**V7新增：Source缺tier_reason拒、Claim空source_ids拒、needs_web=true时freshness=none拒、long_running/rule_id字段**。
- `tests/test_store.py`：`transition`乐观锁冲突、fencing旧owner被拒、非法跃迁REJECTED、AUTOINCREMENT seq不重、**让出边熔断计数（session_switch_total递增到K后拒）**。
- `tests/test_lease.py`：acquire/renew/expire/recover全周期、**主动让出与租约过期区分（前者attempts不变后者计）**。
- `tests/test_verifier.py`：路径越界拒绝、白名单外cmd拒绝、flaky重试、business_rule注入拒绝、**source_traceable/freshness/source_quality/coverage四handler正反例**。
- `tests/test_planner.py`（新增）：拆分输出JSON校验、key_questions/slice_rationale齐全、同一问题二次拆分幂等、拆分侧session恢复（超80%→checkpoint→续拆）。
- `tests/test_search.py`（新增）：域名先验（官方域名直接A不调LLM）、平台域名逐条判（蓝V判A案例）、未知域名LLM判、**注入防护（snippet正文不影响分级）**、分级结果落盘复用。
- 新增：`test_queue_order/test_reserved_slot/test_gateway_route/test_privacy_guard/test_timing_spans`（`test_privacy*`在`privacy.enabled=false`下默认skip，接口保留，V7§8）。
- 已落地：`test_context.py`（动态窗口纯计算9用例）、`test_gateway.py`（本地门禁6用例）、`test_governor.py`（双平台分派4用例）。
- 要求覆盖率：`store+lease+verifier+gateway+planner+search` ≥80%（V7§12.8硬线）。测试用隔离库`state_test.db`，禁污染生产库；备份`VACUUM INTO`+`v1→v2`迁移演练必过。

## 3. 混沌测试（`scripts/chaos_*.py`，人工一键）

1. `kill -9`子执行中：重启后租约过期回收为FAILED→RETRY，重跑成功，无半写（`replay`一致）。
2. 双调度同`plan_id`并发触发：只建一次，第二路`already_exists`跳过。
3. 旧fencing写回：构造过期owner写SUBMITTED，期望`StaleOwner`拒绝。
4. 非法跃迁：构造`READY->DONE`直写，期望`IllegalTransition` + P1告警。
5. 大文件：200MB CSV任务，断言prompt<8k tokens且产物sha正确。
6. FIFO顺序：同优先级10个任务按入队顺序派发，乱序即失败。
7. 高优排队：管道高优入队后，当前任务完成后p95<60s内高优开始执行（排队等待，非抢占）。
8. 防饿死：构造高优连续占用，低优等待达阈值后触发`AGED`提升 + 预留槽派发。
9. **session切换（V7新增）**：构造小context_window强制触发水位线→观测`RUNNING→READY(reason=context_full)`+checkpoint落盘+新session恢复续跑+done_steps不重复+attempts不变。
10. **让出熔断（V7新增）**：构造连续让出>K次→观测P1告警+后续改走FAILED+计attempts。
11. **幻觉拒收（V7新增）**：产物claims.json含无source_id结论→验收FAIL+INCONSISTENT候选。
12. **拆分幂等（V7新增）**：同一问题投两次planner→只拆一次，第二次already_exists。
13. **静默截断检测**：mock服务端返回小effective→观测`check_effective`=truncated+P1告警+水位线下调。
14. **Mac空闲检测**：Mac上`_idle_mac()`返回非负秒数；ioreg缺失时保安全0.0（判ACTIVE）。

## 4. 端到端冒烟（`scripts/smoke_daily.py`）

- 用`templates/daily_plan.yaml`跑`t1_collect→t2_clean→t3_report`三任务链：
  `ensure_plan(today) → orchestrator_loop(max_iter=20) → verify_all → report`。
- 断言：三任务`DONE`、产物+manifest存在、`events`可重放、`reports/today.md`生成。
- 二次同输入触发：命中`SKIPPED_CACHED`至少1个。
- 混合冒烟：Daily跑一半时经`enqueue`注入管道高优，验证高优排队等待+当前任务完成后自然衔接+最终双链路DONE；另覆盖网关熔断（kill ollama公开自动切在线）与verify慢rule标验收阻塞。
- **调研冒烟（V7新增）**：笼统问题→planner拆分→needs_web=true任务走四段式（检索→筛选→分析→产出四件套）→4条调研验收全过→report.md含任务分解视图。
- 全程日志含同一`run_id`（跨来源任务run_id独立但queue seq可关联）。

## 5. 性能与成本基线

- 单计划三任务端到端<5min（无LLM stub模式）。
- `state.db` <10MB/千计划，导出JSON可重建时间<5s。
- token基线：任务卡<2k tokens/次，缓存命中省100%执行。
- 来源分级：官方域名0次LLM调用；平台域名每来源1次本地小模型调用。

## 6. DoD（全项目放行标准）

实现现状（2026-09-29，pytest **398 passed**）。

- [x] 上述单元全绿，覆盖率≥80%。**91.0%**（门禁脚本 `scripts/check_coverage.py`，九项明细全≥80%：
      store 88 / lease 88 / verifier 91 / gateway 86 / planner 91 / search 87~94）。
- [x] 混沌14项全过。`tests/test_chaos_01_07.py`（1-7）+ `tests/test_chaos_08_14.py`（8-14），
      证据落 `reports/chaos_01_07.log`、`reports/chaos_08_14.log`。
      时间断言用p95口径（混沌7 p95=38.4ms，阈值60s），不用wall-clock硬线。
- [x] 冒烟连续2次全绿（含调研冒烟），`{plan}.summary.md`含失败置顶+Top5+分解视图+调研质量章节。
      `tests/test_smoke_e2e.py` 8例（4条链+4条基线），结果落 `reports/smoke_baseline.log`。
- [x] `PROGRESS.json`全Phase `done` + evidence可追溯，`design/phases/*.md`评审签字通过。
      **待你评审签字**——我只能保证代码与文档一致，签字是你的动作。

### 6.1 混沌14项结果

| # | 项 | 关键观测 |
|---|---|---|
| 1 | kill -9子执行中 | 租约回收FAILED + `replay`一致 + checkpoint可续跑 |
| 2 | 双调度同plan_id | DB锁挡第二路，绕过锁也只建一次 |
| 3 | 旧fencing写回 | `StaleOwner`拒 + P1 + 状态未变 |
| 4 | READY→DONE直写 | `IllegalTransition` + P1 + replay仍一致 |
| 5 | 大文件20万行 | prompt **284 tokens**（<8k）+ sha与真实内容一致 |
| 6 | FIFO十任务 | 派发顺序与seq完全一致 |
| 7 | 高优排队 | p95 **38.4ms**（12样本，阈值60s） |
| 8 | 防饿死 | 高优占满 → 低优AGED提到5 → 预留槽派发 |
| 9 | session切换 | 水位线→让出→attempts=0→done_steps不重复→generation变新 |
| 10 | 让出熔断K=2 | 拒让出 + 执行器改走FAILED + P1 |
| 11 | 幻觉拒收 | claim无source_id → FAIL + `INCONSISTENT` P0 |
| 12 | 拆分幂等 | 同问题两次 → 第二次`created=False` |
| 13 | 静默截断 | resolved 65536 vs effective 16384 → truncated + 水位线按实际下调 |
| 14 | Mac空闲检测 | 非负秒数；ioreg缺失/无字段/负值 → 保安全0.0 |

### 6.2 §5 性能与成本基线（实测）

| 基线 | 门槛 | 实测 |
|---|---|---|
| 单计划三任务端到端（stub） | <5min | 远低于（tick级） |
| `state.db` | <10MB/千计划 | <10MB |
| 任务卡 | <2k tokens | 远低于（混沌5实测284） |
| `rebuild_export` | <5s | 远低于 |
| 来源分级：官方域名 | 0次LLM | 0次 |
| 来源分级：平台域名 | 每来源1次 | 每来源1次 |

## 9. 实现与设计的偏差（2026-09-29 落码后记录）

1. **`transition()` 新增 `expected=True`**：「依赖还没满足」「退避没到点」这类
   **预期内**的拒绝仍记REJECTED审计行，但**不发P1**。此前主循环每轮都会为
   每个未解锁任务喊一次警——那不叫告警，叫噪音。越权写、非法直写终态
   照常P1。
2. **新增 `promote_retries()`**：`RETRY→READY` 的退避重排。原先主循环只处理
   PENDING/BLOCKED，验收失败的任务会永久卡在RETRY，整个plan停摆。
3. **执行器不再把模型回复写进产物**。产物由模型自己用工具写（任务卡里明确
   要求），执行器只负责"确认落盘 + 补manifest"，一个都没落盘就报
   `no_artifacts_produced`。此前会把同一坨文本写进所有声明产物，
   导致 `summary.json` 里是 markdown，`json_schema` 永远 `bad_json`，
   且报错指不到真因。
4. **`cache_hit` 改两层判据**：
   - 第1层 `inputs_hash == last_success_hash`（输入没变）；
   - 第2层 逐个产物比 **manifest 里记录的 sha**（产物还在且没被改）。
   此前把产物sha拿去和inputs_hash比，两者天然不同，判断永远为假，
   `SKIPPED_CACHED` 是死代码。`last_success_hash` 存的是inputs_hash，
   产物指纹不再另开列（manifest里已有，再存一份就是两份可能不一致的事实）。
5. **`scheduler` 为每个任务算 `inputs_hash`**（任务规格的规范化哈希），
   否则缓存第1层永远不成立。哈希只取"改了会影响产出"的字段
   （目标/依赖/产出/验收/预算），不含priority这类只影响调度的。
6. **检索链路补跨query URL去重**：同一URL被多个query命中时重复条目会加权
   A类占比，质量分虚高。`raw_count` 保留去重前数值供观测召回冗余。
7. **覆盖率门禁做成独立脚本** `scripts/check_coverage.py`，不做成pytest用例：
   那会在测试进程里再跑全量测试，而全量测试包含它自己，无限递归。

## 7. V6→V7 变更清单（2026-09-28，本轮，待评审）

- §2：transitions/models/store/lease测试加让出边与熔断用例；新增test_planner.py/test_search.py；覆盖率扩到planner+search。
- §3：混沌8项→12项（加session切换/让出熔断/幻觉拒收/拆分幂等）。
- §4：混合冒烟加调研冒烟（planner→四段式→4验收→任务分解视图）。
- §5：基线加来源分级调用成本。
- §6：DoD混沌8→12项。
- 本轮（动态窗口+双平台+四件套）：§2补已落地的test_context/test_gateway/test_governor；§3混沌12→14项（加静默截断检测/Mac空闲检测）；§4调研冒烟三件套→四件套；§6 DoD同步14项。
- 前轮（V6）保留：隔离库、VACUUM INTO+迁移演练、p95/p99断言、test_privacy默认skip。
