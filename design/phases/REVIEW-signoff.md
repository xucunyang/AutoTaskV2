# 评审签字记录

## 2026-09-29：Phase0–4 阶段设计稿全部评审通过

用户确认：`design/phases/Phase0~4` 全部通过评审。Phase0–4 代码已实现并落码
（pytest 406 passed，覆盖率 91.0%），DoD 逐条对应见各阶段设计稿。

### 同时确认的实现决策

以下决策在设计稿里偏离/补充了原文，评审时逐条确认"按实现来"：

| # | 决策 | 落点 | 确认 |
|---|---|---|---|
| 1 | 告警级别由政策单一决定，调用方不能自选；P0 只认三种 kind，冲突时降级并留痕 | `core/notifier.py:level_for` | ✅ |
| 2 | 老化告警从 P0 改为 P2（设计 §3.2 原本就把"老化统计"归 P2） | `core/orchestrator.py:apply_aging` | ✅ |
| 3 | RETRY 指数退避 `60*2^attempts` **封顶 1 小时**（设计只写公式没写上限） | `core/lease.py:backoff_delay_s` | ✅ |
| 4 | 产物由模型自己用工具写；执行器只确认落盘 + 补 manifest | `core/executor.py:_run_with_provider` | ✅ |
| 5 | planner 拆分 checkpoint 存**序号(int)**而非 task_id | `core/planner.py:save_split_checkpoint` | ✅ |
| 6 | `master.json` 降级为索引，细节进 `plans/{plan_id}.json` | `core/store.py:rebuild_export` | ✅（详见下） |

### 关于 `master.json` 的结论（评审提问）

**它是 V1/V2 时代 `task.md` 的遗留，V7 纲领已不再提它。**

- 出处：`task.md` §4.1「Master State — 跨天记录计划/DAG/全局状态」，
  出现在 `IMPLEMENTATION_PLAN_V1/V2.md`，**V7 全文无 `master` 字样**。
- 它原本要解决的问题——"跨天还能看到昨天计划的全局状态"——在 V7 里由
  `Store.daily_summary(plan_id)`（plan 摘要 + 状态计数 + 最近 N 条 events）
  和 `reports/{plan_id}.summary.md` 承担，**数据在 DB，不在导出文件里**。
- 我在 Phase1 实现时按 V1 语义写了单文件 `master.json`，Phase1 复查时发现
  多 plan 并存会互相覆盖（`daily` 与 `pipeline` 同时导出，后写的盖掉先写的），
  已改为：细节进 `plans/{plan_id}.json`，`master.json` 只做 plan 清单索引。
- **现状判定：保留但降级**。它现在的作用是"导出目录的入口索引"，
  没有任何代码依赖它做决策（所有读路径都走 DB）。若将来要彻底去掉，
  影响面仅为 `rebuild_export` 的一处写文件 + 一个测试断言。

### 关于本地 14B 的澄清（评审提问）

评审时我提到"14B 在配置里不存在"，表述不清，此处说明：

- `docs/deploy-prereq.md` §4 整节在算 **14B@64K 在 32G Mac 上的内存账**
  （权重 + KV 约 20.4G，单并发），这是**部署可行性论证**。
- `config/gateway.yaml` 的 `providers.local-ollama.models` 只配了
  `simple: qwen3.5:4b`。**没有任何配置指向 14B**，`complex` 恒走在线。
- 两者不矛盾：deploy-prereq 论证的是"若哪天要在本地跑 14B，内存够不够"，
  当时的决策是"IDLE 档 local_concurrency=1，前提是 14B@64K"。
- **当前实际形态：本地只跑 simple(4B)，medium/complex 走在线**
  （`deepseek-v4-flash`）。这与 deploy-prereq 的结论一致（32G 上 7B 很宽裕，
  14B 单并发也可行），只是**尚未把 14B 配进 gateway.yaml**。
- 要启用 14B 本地：只需在 `models` 里加一行 `medium: <14B模型名>`
  （路由逻辑已支持，见 `core/gateway.py:local_complexities_from`），
  并按 deploy-prereq §4 重算窗口。**这不是缺陷，是尚未启用。**

### 关于 planner 续跑精度（评审提问）

用户选择"按任务颗粒度续跑即可，不追求拆分层 id 级精度"——
与当前实现一致，无需改动。若将来要改 id 级，代价是：
Phase0 契约 `Checkpoint.done_steps: List[int]` 要放宽为 `List[int] | List[str]`，
外加 `models.py` 与 `planner.py` 各一处，**改动面很小（约 3 个文件）**，
但会牺牲该字段的类型严格性。维持现状。
