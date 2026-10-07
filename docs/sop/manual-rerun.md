# 手工重跑 SOP（DONE 重开 / `reopen_done`）

任务**验收全绿、但产物的口径或内容其实错了**时用本 SOP。

典型场景：2026-10-06 `finance_daily` 三个任务全部 DONE、退出码 0，
但 `finance_summary.json` 的 `data_period` 把 A 股的"上一交易日"
（2026-09-30）推广到了全市场——而美股 10-05（美东）正常交易，
它的最近交易日根本不是 9-30。**验收规则没覆盖这个维度，所以它全绿通过，
但产物是错的。** 这种"绿但错"只能靠人发现，也只能靠重开修。

`reopen_done` 写的审计事件里 `sop_ref` 默认就指向本文件。

> **与 `dead-letter.md` 的区别**（选错 SOP 会走错路径）：
> - 任务**没通过**验收、卡在 `DEAD_LETTER` → 看 [dead-letter.md](dead-letter.md)
> - 任务**通过了**验收、卡在 `DONE`，但产物要重做 → **本文件**
>
> `CANCELLED` / `SKIPPED` / `SKIPPED_CACHED` **不开放**重开：
> 它们是另一种语义（上游取消 / 上游判定不该跑），
> 混进 rerun 流程会把语义搞乱。

## 1. 为什么需要这条独立路径

`DONE` 和 `DEAD_LETTER` 一样是 **TERMINAL**，没有自动出边。
V7 状态机里 `DONE → RETRY` 虽然在 `schemas/transitions.py` 的 ALLOWED 表中，
但它**只允许走 reopen 路径**，`transition()` 走不通。

2026-10-06 之前 `--rerun` 只认死信，对 DONE 直接报：

```
IllegalTransition: DONE->RETRY
```

于是"想改就只能改库"——而改库会让 `version` 与 `events` 对不上，
`replay` 一致性校验随即失败（SOP 明确禁止改库）。这就是
"改库=破坏完整性" 和 "重做=改状态" 之间的死结。

`ac30820` 加 `Store.reopen_done` 就是为了把这个死结解开：
**留一条带审计、带理由、不破坏一致性的合法出边。**

## 2. 命令

```powershell
cd E:\code\AutoTaskByMuseSpark1.3\orchestrator
python -X utf8 -m core.scheduler --date=2026-10-06 --job=finance_daily --rerun=t1_stocks --reason="美股应取 10-05 美东口径"
```

> ⚠️ **必须在 `orchestrator\` 目录下执行**。
> `--root` 只决定去哪里找 `state/state.db`，**不影响 `python -m` 的模块解析**——
> `core` 包在 `orchestrator/` 下，在仓库根直接跑会报
> `ModuleNotFoundError: No module named 'core'`（2026-10-06 实测）。
> 进目录后 `--root` 可省略，默认就是包的上级。

- `--job`：**非 `daily_report` 的链必须带**。`plan_id` 是
  `{job_id}_{date}`，`--job` 决定 `job_id`；漏了会去 `daily_report_2026-10-06`
  里找，自然 `task_not_found`。
- `--rerun` 必须是**单个** task_id。一次要重开多个就分几条命令跑，
  每条自带 reason，审计里才看得出各自为什么被重开。
- `--reason`：**必填**，空会被拒（见 §3）。

成功返回：

```json
{"task_id":"t1_stocks","from":"DONE","to":"RETRY","attempts":0,"reopened":true}
```

## 3. 它做的四件事（缺一不可）

| # | 动作 | 为什么 |
|---|---|---|
| 1 | `DONE → RETRY` | 之后按正常退避重排（`promote_retries`） |
| 2 | `attempts` 重置为 0 | `dead_letter_if_exhausted` 判据是 `attempts >= max_attempts`，不重置等于重开即死 |
| 3 | 写审计事件 | `reopened_from=DONE` + `reason` + `sop_ref` + `attempts_reset_from` + operator |
| 4 | 报 **P2** `task_done_reopened` | 产物被覆盖 = 人会用到它；但不必半夜炸，P2 走日报不进电话 |

另外记一条 metric `terminal_reopened_done`
（metrics 表没有 `from_status` 列，所以从状态合并进 name；
死信那条叫 `terminal_reopened_dead_letter`，两边 grep 时别混）。

**为什么必须带 `--reason`**：
无理由的重开等于"我看着办重试"。reason 强迫你先说清
"这次和上次有何不同"——这是事后能审计的唯一线索。
空 reason 直接 `StoreError("reopen_requires_reason")`。

**为什么 DONE 重开是 P2 而死信是 P1**：
死信是系统**主动放弃**，没人知道它停了；DONE 是产物已发布、
只是需要刷新，紧急度低一档。但它**不可漏**——被覆盖的产物会被人当权威用，
所以必须留在日报里。

## 4. 完整操作流程

```powershell
# ① 先看清楚要重开什么、当前是什么状态（只读）
python -X utf8 -c "import sys; sys.path.insert(0,'orchestrator'); \
  from core.store import Store; import json; \
  s=Store('orchestrator'); \
  print(json.dumps(s.get_task('finance_daily_2026-10-06','t1_stocks'), ensure_ascii=False, default=str)[:1200])"

# ② 确认根因不是"验收规则漏了"——如果是，先改 yaml/acceptance 再重开，
#    否则重跑一遍还是同样的错（2026-10-06 就是先改 schema 才重跑的）

# ③ 重开（每条任务一条命令，各自带 reason）
cd E:\code\AutoTaskByMuseSpark1.3\orchestrator
python -X utf8 -m core.scheduler --date=2026-10-06 --job=finance_daily --rerun=t1_stocks --reason="美股改 10-05 数据口径"
python -X utf8 -m core.scheduler --date=2026-10-06 --job=finance_daily --rerun=t3_finance_report --reason="data_period 改 per-market"

# ④ 驱动跑批（重开只是把任务放回 RETRY，还得跑一次才会重新执行）
python -X utf8 -m scripts.run_daily --date=2026-10-06 --job=finance_daily --timeout-min 25

# ⑤ 验证：状态 RETRY、attempts=0
python -X utf8 -c "import sys; sys.path.insert(0,'orchestrator'); \
  from core.store import Store; \
  print(Store('orchestrator').get_task('finance_daily_2026-10-06','t1_stocks')['status'])"
```

**依赖顺序**：下游任务要一起重开。上游产物变了、下游没重开，
下游就是拿旧摘要重算一遍，错的还是错的（2026-10-06 的
`t3_finance_report` 就是因为 `data_period` 是它写的才必须一起重开）。

## 5. 不要做的事

- **不要**直接改 `orchestrator/state/state.db` 的状态字段。
  `version` 会与 `events` 对不上，`replay` 一致性校验失败。
  `reopen_done` 已经是正规路径。
- **不要**手工编辑已 DONE 的产物文件来"修正"。
  产物带 `sha256` manifest，手改会让 `final_consistency` 的
  产物 sha 校验对不上；正确做法是重开让任务自己重算。
- **不要**把 `max_attempts` 调大来绕过。
- **不要**对同一批任务反复重开超过 2 次。连续 2 次重开还是同样的错，
  说明根因在验收规则或模板，不在人——见 §6。

## 6. 复盘要求

重开属于"人工介入"，日报里要有一条对应记录。
若某个 `reason` 在一周内重复出现 ≥3 次，那是**系统性问题**，
应升级为设计变更（改验收规则 / 改模板 objective），
而不是继续人工重跑。

真实案例见 §0：验收全绿却放过了错误的数据口径，
说明 `data_period` 当时根本没被规则覆盖——
后来 `c024fca` 把 `finance_summary.schema.json` 的 `data_period`
从 string 改成 object、并加了三条 `business_rule` 卡死，
才从根上堵住这个洞。**这才是重开之后该做的事。**