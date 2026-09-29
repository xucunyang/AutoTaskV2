# 死信处理 SOP（DEAD_LETTER）

任务进死信 = 重试耗尽（`attempts >= max_attempts`）后仍失败。
死信三必填（`reason` / `last_error` / `sop_ref`）里的 `sop_ref` 就指向本文件。
**没有 SOP 链接的死信等于没人知道怎么处理**，所以 `store.transition` 会强制校验这一项。

## 1. 先看什么

```powershell
# 该任务最近发生了什么（最近50条事件，倒序）
python -m core.orchestrator --root . --status
```

看 `reports/{plan_id}.summary.md` 的「失败（置顶）」章节，以及
`reports/ALERTS.jsonl` 里 `kind=dead_letter` 那条（含 attempts 与 last_error）。

## 2. 分诊（按 last_error 归类）

| last_error 特征 | 大概率原因 | 处理 |
|---|---|---|
| `deps_unsatisfied` / `yield_requires_fencing` | 上游没完成或有脑裂 | 先查上游任务是否真 DONE；别直接重跑本任务 |
| `session_switch_fused` | 单任务让出超过 K=5 次 | 拆分太粗或窗口太小。把任务拆细，或调大模型 context_window |
| `no_artifacts_produced` | 模型没把产物写盘 | 手工补产物后走 `python -m core.scheduler --date=... --rerun={task_id}` |
| `verify_failed` + `hallucination` | claims 无来源 | 补来源后重跑；这条同时会发 P0 INCONSISTENT |
| `no_search` / `needs_web_but_no_search` | 没配检索源 | 配 `TAVILY_API_KEY`（见 `.env.example`）后重跑 |
| `verify_rule_slow_s` | 单条验收超 60s | 拆细该 rule，或确认是不是产物过大 |
| `local_window_insufficient` | 本地窗口不够 | 该任务应走在线；检查 `local-ollama.models` 是否误配了它 |
| `db_locked` / `db_write_failed` | 并发写冲突 | 看 `db_lock_retry` 指标；worker 过多则调 `max_workers` |

## 3. 修复动作

**A. 手工补产物**（产物还在、只是没走完流程）
```powershell
python -m core.scheduler --date=2026-09-29 --rerun=t1_collect
```
注意：死信是终态，状态机没有"死信→重试"的边。需先把该任务状态改回
`RETRY`（见下），再由编排层按退避重排。

**B. 改配置后整体重跑该 plan**
```powershell
python -m core.scheduler --date=2026-09-29
```

**C. 确认是任务本身设计问题**（反复死信）
不要重跑。回到 `design/plans/IMPLEMENTATION_PLAN_V7.md` 复盘：
是拆分粒度不对，还是验收规则定得不合理。

## 4. 复盘要求

每个死信在日报里要有一条对应记录。连续 3 天出现同一 `last_error` 的死信，
属于**系统性问题**，应升级为设计变更（改 prompt / 改拆分 / 改验收），
而不是继续人工重试。

## 5. 不要做的事

- **不要**直接改 `state/state.db` 里的状态字段绕过状态机（版本号会与
  `events` 对不上，`replay` 一致性校验随即失败）。
- **不要**把 `max_attempts` 调大来"绕过"死信——那只是把问题往后推。
- **不要**删 `events/` 里的记录来"清理"历史。
