# Phase4 验证设计：单元 + 混沌 + 端到端冒烟（V7）

状态：已按V7对齐，待用户评审，前置依赖 Phase3冻结。
对齐基线：`design/plans/IMPLEMENTATION_PLAN_V7.md`；修订见文末§8。

## 1. 目标

- 用最小用例证明生产级三件套：不丢状态、不重副作用、不爆上下文。
- V7新增验证面：session切换与恢复、LLM拆分、Web搜索+来源分级、调研质量验收。

## 2. 单元测试（`pytest`）

- `tests/test_transitions.py`：ALLOWED正例全覆盖 + 非法`READY->DONE/RUNNING->DONE/DONE->*`反例；**V7新增：`RUNNING->READY`让出正例（带reason+checkpoint）、无checkpoint让出拒、熔断超限拒、attempts不变断言**。
- `tests/test_models.py`：非法handoff（超长summary/缺字段）、manifest缺sha被拒；**V7新增：Source缺tier_reason拒、Claim空source_ids拒、needs_web=true时freshness=none拒**。
- `tests/test_store.py`：`transition`乐观锁冲突、fencing旧owner被拒、非法跃迁REJECTED、AUTOINCREMENT seq不重、**让出边熔断计数（session_switch_total递增到K后拒）**。
- `tests/test_lease.py`：acquire/renew/expire/recover全周期、**主动让出与租约过期区分（前者attempts不变后者计）**。
- `tests/test_verifier.py`：路径越界拒绝、白名单外cmd拒绝、flaky重试、business_rule注入拒绝、**source_traceable/freshness/source_quality/coverage四handler正反例**。
- `tests/test_planner.py`（新增）：拆分输出JSON校验、key_questions/slice_rationale齐全、同一问题二次拆分幂等、拆分侧session恢复（超80%→checkpoint→续拆）。
- `tests/test_search.py`（新增）：域名先验（官方域名直接A不调LLM）、平台域名逐条判（蓝V判A案例）、未知域名LLM判、**注入防护（snippet正文不影响分级）**、分级结果落盘复用。
- 新增：`test_queue_order/test_reserved_slot/test_gateway_route/test_privacy_guard/test_timing_spans`（`test_privacy*`在`privacy.enabled=false`下默认skip，接口保留，V7§8）。
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

## 4. 端到端冒烟（`scripts/smoke_daily.py`）

- 用`templates/daily_plan.yaml`跑`t1_collect→t2_clean→t3_report`三任务链：
  `ensure_plan(today) → orchestrator_loop(max_iter=20) → verify_all → report`。
- 断言：三任务`DONE`、产物+manifest存在、`events`可重放、`reports/today.md`生成。
- 二次同输入触发：命中`SKIPPED_CACHED`至少1个。
- 混合冒烟：Daily跑一半时经`enqueue`注入管道高优，验证高优排队等待+当前任务完成后自然衔接+最终双链路DONE；另覆盖网关熔断（kill ollama公开自动切在线）与verify慢rule标验收阻塞。
- **调研冒烟（V7新增）**：笼统问题→planner拆分→needs_web=true任务走四段式（检索→筛选→分析→产出三件套）→4条调研验收全过→report.md含任务分解视图。
- 全程日志含同一`run_id`（跨来源任务run_id独立但queue seq可关联）。

## 5. 性能与成本基线

- 单计划三任务端到端<5min（无LLM stub模式）。
- `state.db` <10MB/千计划，导出JSON可重建时间<5s。
- token基线：任务卡<2k tokens/次，缓存命中省100%执行。
- 来源分级：官方域名0次LLM调用；平台域名每来源1次本地小模型调用。

## 6. DoD（全项目放行标准）

- [ ] 上述单元全绿，覆盖率≥80%。
- [ ] 混沌12项全过（含FIFO/高优排队p95/防饿死/网关熔断/verify慢标阻塞/session切换/让出熔断/幻觉拒收/拆分幂等），证据写入`reports/chaos_*.log`，时间断言用p95/p99非wall-clock硬线。
- [ ] 冒烟连续2次全绿（含调研冒烟），`{plan}.summary.md`含失败置顶+Top5+阻塞+任务分解视图+调研质量章节可审计。
- [ ] `PROGRESS.json`全Phase `done` + evidence可追溯，`design/phases/*.md`评审签字通过。

## 7. V6→V7 变更清单（2026-09-28，本轮，待评审）

- §2：transitions/models/store/lease测试加让出边与熔断用例；新增test_planner.py/test_search.py；覆盖率扩到planner+search。
- §3：混沌8项→12项（加session切换/让出熔断/幻觉拒收/拆分幂等）。
- §4：混合冒烟加调研冒烟（planner→四段式→4验收→任务分解视图）。
- §5：基线加来源分级调用成本。
- §6：DoD混沌8→12项。
- 前轮（V6）保留：隔离库、VACUUM INTO+迁移演练、p95/p99断言、test_privacy默认skip。
