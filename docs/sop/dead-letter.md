# 死信处理 SOP（DEAD_LETTER）

任务进死信 = 重试耗尽（`attempts >= max_attempts`）后仍失败，由
`escalate_dead_letters` 自动升级并报 P0。
死信三必填（`reason` / `last_error` / `sop_ref`）里的 `sop_ref` 就指向本文件。
**没有 SOP 链接的死信等于没人知道怎么处理**，所以 `store.transition`
会强制校验这一项。

**下面每条命令都是实测过的**（2026-10-01，见 `tests/test_dead_letter_reopen.py`）。
SOP 里写跑不通的命令比没有 SOP 更糟——照着做失败等于没处理。

## 1. 先看什么

死信任务只读，不做任何写操作：

```powershell
# 该 plan 最近的事件（Store.daily_summary：plan 摘要 + 状态计数 + 最近 N 条 events）
python -X utf8 -c "import sys; sys.path.insert(0,'orchestrator'); \
  from core.store import Store; import json; \
  s = Store('orchestrator'); \
  print(json.dumps(s.daily_summary('daily_report_YYYY-MM-DD'), ensure_ascii=False, indent=1)[:3000])"
```

看 `orchestrator/reports/{plan_id}.summary.md` 的「失败（置顶）」章节，以及
`orchestrator/reports/alerts.jsonl` 里 `kind=dead_letter` 那条
（含 attempts 与 last_error）。**注意文件名是小写 alerts.jsonl。**

## 2. 分诊（按 last_error 归类）

| last_error 特征 | 大概率原因 | 处理 |
|---|---|---|
| `deps_unsatisfied` | 上游没完成 | 先查上游任务是否真 DONE；别直接重跑本任务 |
| `session_switch_fused` | 单任务让出超过熔断上限（默认5次） | 拆分太粗或窗口太小。把任务拆细，或调大对应模型的 context_window |
| `no_artifacts_produced` | 模型没把产物写盘 | 手工补产物后重开（见 §3） |
| `verify_failed` + `hallucination` | claims 无来源 | 补来源后重开；这条同时会发 P0 INCONSISTENT |
| `no_search` / `needs_web_but_no_search` | 没配检索源 | 配 `TAVILY_API_KEY` 或切 `provider: bocha`（见 `config/search.yaml` 注释）后重开 |
| `verify_rule_slow` | 单条验收超 60s | 拆细该 rule，或确认是不是产物过大 |
| `local_window_insufficient` | 本地窗口不够 | 该任务应走在线；检查 `config/gateway.yaml` 里 `complexity_models` 是否把它指到 local |
| `db_locked` / `db_write_failed` | 并发写冲突 | 看 `db_lock_retry` 指标；worker 过多则调小并发（governor 会自动压，但手动 override 可能锁住了档位） |

> 注：`complexity_models` 是复杂度→模型名的显式映射（如 `{simple: local, ...}`），
> 不要去查旧的 `providers.local-ollama.models`——那个结构在模型注册表重构时已改名。

## 3. 修复动作：重开死信

**只有一条**合法的出死信路径：`Store.reopen_dead_letter()`，CLI 入口是

```powershell
python -m core.scheduler --root orchestrator --date=2026-09-29 --rerun=t1_collect --reason="已补产物"
```

它做三件事（缺一不可，见下"为什么"）：
1. 状态 `DEAD_LETTER → RETRY`（此后按正常退避重排）
2. `attempts` 重置为 0
3. 写审计事件（含操作人、理由、重置前的 attempts）并报 P1 `dead_letter_reopened`

**为什么必须重置 attempts**：`escalate_dead_letters` 的判据是
`attempts >= max_attempts`，而 `RETRY→READY` 每次还会 +1。不重置的话
重开等于白开——跑一次、失败、立刻又升级成死信。

**为什么必须带 reason**（`--reason`，缺了会被拒）：
无理由的重开等于"我看着办重试"，而死信是系统**主动放弃**的结果。
随手重开同一批任务只会无限循环，reason 强迫你先说清"这次和上次有何不同"。

**不要做的事**：
- **不要**直接改 `state/state.db` 里的状态字段（版本号会与 `events` 对不上，
  `replay` 一致性校验随即失败，而且 `reopen_dead_letter` 已经给了正规路径，
  没必要走旁门）。
- **不要**把 `max_attempts` 调大来"绕过"死信——那只是把问题往后推，
  而且下一次还是这个 SOP。
- **不要**删 `events/` 里的记录来"清理"历史。

## 4. 验证重开是否生效

```powershell
# 状态应为 RETRY，attempts 应为 0
python -X utf8 -c "import sys; sys.path.insert(0,'orchestrator'); \
  from core.store import Store; \
  print(Store('orchestrator').get_task('daily_report_2026-09-29','t1_collect')['status'])"
```

此后编排器按退避自动重排（`promote_retries`）。若再次失败并回到死信，
说明根因没修——回到 §2 重新分诊，不要连续重开超过 2 次。

## 5. 复盘要求

每个死信在日报里要有一条对应记录。连续 3 天出现同一 `last_error` 的死信，
属于**系统性问题**，应升级为设计变更（改 prompt / 改拆分 / 改验收），
而不是继续人工重试。
