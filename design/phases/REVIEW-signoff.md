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
| 6 | ~~`master.json` 降级为索引~~ → **后改为彻底删除**（见下方"追加决策"§7） | `core/store.py:rebuild_export` | ✅（已被§7取代） |

### 关于 `master.json` 的结论（评审提问）

**它是 V1/V2 时代 `task.md` 的遗留，V7 纲领已不再提它。**

- 出处：`task.md` §4.1「Master State — 跨天记录计划/DAG/全局状态」，
  出现在 `IMPLEMENTATION_PLAN_V1/V2.md`，**V7 全文无 `master` 字样**。
- 它原本要解决的问题——"跨天还能看到昨天计划的全局状态"——在 V7 里由
  `Store.daily_summary(plan_id)`（plan 摘要 + 状态计数 + 最近 N 条 events）
  和 `reports/{plan_id}.summary.md` 承担，**数据在 DB，不在导出文件里**。
- 我在 Phase1 实现时按 V1 语义写了单文件 `master.json`，Phase1 复查时发现
  多 plan 并存会互相覆盖（`daily` 与 `pipeline` 同时导出，后写的盖掉先写的），
  先改为：细节进 `plans/{plan_id}.json`，`master.json` 只做 plan 清单索引。
- **后续用户选择彻底删除**（见下方"追加决策"§7）：作为索引的价值也很弱
  ——索引里的 plan 列表 `Store.list_plans()` 一行就能查，而导出目录
  本身不是查询入口。留着它只是**一致性负债**。

## 2026-09-29 追加决策（Phase2 收尾后）

| # | 决策 | 落点 | 确认 |
|---|---|---|---|
| 7 | **删除 `master.json`**（不是降级为索引，是彻底删） | `core/store.py:rebuild_export` | ✅ |
| 8 | **自己做薄 tool loop**，不拉起 CC/opencode | `core/tools.py` + `core/executor.py:_tool_loop` | ✅ |
| 9 | **模型注册表 + 复杂度映射**，改配置即可切换 | `config/gateway.yaml` + `core/gateway.py` | ✅ |

### 决策 7：为什么 `master.json` 可以彻底删掉

原判断是"保留但降级为索引"，用户选择直接删除，理由充分：

- V1/V2 的 `master.json` 要解决"跨天看全局计划状态"，V7 已由
  `Store.daily_summary(plan_id)` 承担，**数据在 DB，不在导出文件里**。
- 它作为"导出目录入口索引"的价值很弱：索引里的 plan 列表，
  `Store.list_plans()` 一行就能查到，而导出目录本身不是查询入口。
- 留着它的实际成本是**一致性负债**：任何绕过 `rebuild_export` 的
  直接写文件都会让它和 DB 长期不一致，而它没有任何代码依赖。

现在导出结构只有两层：`plans/{plan_id}.json`（plan 级）+ `tasks/{id}.json`（分片级）。

### 决策 8：为什么不用 CC/opencode

对照 Phase2 §5/§6b 的三个设计要求：

| 设计要求 | CC/opencode | 自建薄循环 |
|---|---|---|
| §6b session 让出（`RUNNING→READY` + checkpoint） | ❌ 黑盒进程，只能 kill，状态全丢 | ✅ 在 `chat()` 返回处判水位线 |
| checkpoint 按 `generation` 续跑 | ❌ 无处落地 | ✅ 已有全套机制 |
| usage 采样（`prompt_tokens` 是水位线分子） | ❌ 要从 CLI 日志里刨 | ✅ `chat()` 契约直接给 |
| `run_cmd` 白名单沙箱 | ❌ 工具全交给它，`shell=False` 白名单形同虚设 | ✅ 工具层自己说了算 |

**代价**：工具层要自己维护（4 个工具 + 循环 + 错误处理，约 300 行）。
**收益**：上面四项都还在自己手里，不依赖第三方 agent 的实现细节。

后续如果发现自建工具层不划算，可以把 runner 抽象成接口再换成 CC——
`_tool_loop` 已经是唯一的执行入口，替换成本可控。这是有意留的退路。

### 决策 9：模型注册表的扩展路径

用户提到"以后可能会用 token 套餐"（多个额度/多个 key）。当前结构：

```yaml
models:              # 注册表：加厂商/加套餐 = 加条目
  local:  {type: ollama, models: {simple: qwen3.5:4b}}
  flash:  {type: openai_compat, api_key_env: ONLINE_API_KEY, model: ${ONLINE_FLASH_MODEL}}
  pro:    {type: openai_compat, api_key_env: ONLINE_API_KEY, model: ${ONLINE_PRO_MODEL}}
complexity_models:   # 分配：三行内改完
  {simple: local, medium: flash, complex: pro}
privacy_models:      # 强制不外发
  {secret: local}
```

多个套餐的做法：`api_key_env` 指向不同变量名，`.env` 里各填各的 key，
再在 `complexity_models` 里按额度分配即可。**不需要改任何代码。**

注意 `default_model` 不指 `local`：兜底到 4B 等于静默降级，
宁可让任务失败也不要"复杂任务用了小模型"这种事后才发现的问题。

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
