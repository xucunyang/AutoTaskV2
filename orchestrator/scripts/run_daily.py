"""每日任务的完整跑批：建计划 → 跑编排器直到无在途任务。

为什么需要这个脚本，而不是直接 `python -m core.orchestrator`：
- `core.scheduler` 只**建**计划（ensure_plan），不执行；
- `core.orchestrator` 常驻循环，前台跑起来就停不下来，不适合放进
  计划任务（Windows 会认为任务永远没结束，最后按 ExecutionTimeLimit
  强杀，任务状态停在半路）。
所以这里把两件事串起来，并且**跑到这一天的任务全部进终态就退出**，
让计划任务能干净地拿到退出码。

退出码（供计划任务与监控判别）：
    0  全部任务进终态且无死信
    1  有任务仍在在途（超时退出，说明一次跑不完）
    2  有死信（DEAD_LETTER），需要人工按 SOP 处理
    3  配置/存储层错误（含 --job 在 schedule.yaml 里查不到）

plan 归属：`--job` 同时决定模板**和** plan_id 前缀（`{job}_{date}`）。
漏传 job_id 会让 housing 的活儿挂到 `daily_report_{date}` 上，且因为
ensure_plan 对"存在且有任务"一律 skip，当天日报**静默不跑**——踩过。

日志：进度行同时落 `reports/run_daily_{job}_{date}.log`（计划任务的
stdout 没人收），结构化事件走 `logs/structured.jsonl`。

防休眠：整个执行期用 power.KeepAwake 挡住系统休眠。定时器负责
"把机器唤醒"，这个负责"唤醒后别再睡回去"——两件事，缺一不可。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import executor, power, scheduler as sched_mod, winnotify   # noqa: E402
from core.orchestrator import Orchestrator                  # noqa: E402
from core.store import Store                                # noqa: E402
from core.utils import jlog                                 # noqa: E402

TERMINAL = ("DONE", "SKIPPED", "SKIPPED_CACHED", "CANCELLED", "DEAD_LETTER")

# 只有这些状态"需要继续等"。READY/VERIFYING 之所以算在途：它们正
# 等下一次派发或验收池回收，等同于"这批还没干完"。
#
# PENDING 单独说明：它在等 depends_on 满足。若只等"没有在途任务"就退出，
# 首期运行时 t4 因为上游还没产出而一直 PENDING，会被误判成"跑完了"——
# 实测就是这样：三个采集任务刚进 READY，脚本就认为无事可做而退出，
# 一个都没派发出去。必须把 PENDING 算进在途。
WAITING = ("PENDING", "BLOCKED", "READY", "RUNNING", "SUBMITTED",
           "VERIFYING", "RETRY", "YIELDING")


def shanghai_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def template_for(root: Path, job_id: str) -> str | None:
    """按 job_id 取模板；schedule.yaml 里查不到时返回 None（调用方必须报错退出）。

    这里曾经无条件回退到 jobs[0] 的模板——于是 `--job housing_dayly` 这种
    打错字会**静默**用 daily 模板建出 `housing_dayly_2026-10-05` 这种 plan，
    计划任务每天照跑、日志照写，没有任何一行说"你传错了"。
    无人值守的自动化里，拼错必须炸，不能猜。
    """
    cfg = sched_mod.load_schedule(root)
    for job in cfg.get("jobs") or []:
        if job.get("id") == job_id:
            return job.get("template", "templates/daily_plan.yaml")
    return None


def known_jobs(root: Path) -> list[str]:
    return [j.get("id") for j in (sched_mod.load_schedule(root).get("jobs") or [])]


# 通知里要给"能一键打开"的产物。按钮顺序 = 人最想先点什么：
# 日报正文 → housing 的投资观察汇总 → 图表。
LINK_PRIORITY = ("report.md", "t4_investment_watch.md", "charts.md")
LINK_LABELS = {
    "report.md": "日报",
    "t4_investment_watch.md": "投资观察",
    "charts.md": "图表",
    "t1_tier.md": "分线跟踪",
    "t2_xuzhou_nanjing.md": "徐州南京",
    "t3_hexagon_cities.md": "六边形城市",
}
BODY_SUFFIXES = (".md", ".html", ".pdf", ".txt")


def report_links(root: Path, date: str, rows: list[dict]) -> list[tuple[str, str]]:
    """跑完通知里可点击的报告链接：(按钮文字, 绝对路径)，最多 3 条。

    用户要的是"通知里带报告链接，方便我打开"——只把路径印成一行文字不够：
    toast 里的文字点不动，得是 activationType=protocol 的按钮。

    三条约束：
    - 只挑**本 plan 声明的**产物。`artifacts/{date}/` 是两条链共用目录，
      拿目录里所有 md 去链接，housing 的通知就会点开 daily 的日报。
    - 只挑已落盘的。声明了却没写出来的文件做成按钮 = 点了没反应，
      比没有按钮更糟。
    - 最后一条永远是"产物目录"：报告不止一份时，人更想自己进去挑。
    """
    found: dict[str, Path] = {}
    for r in rows:
        for o in (r.get("shard") or {}).get("outputs") or []:
            o = str(o).replace("\\", "/")
            if not o.endswith(BODY_SUFFIXES):
                continue
            p = root / o
            if p.is_file():
                found[o] = p

    def _key(o: str) -> tuple[int, str]:
        name = Path(o).name
        try:
            return (LINK_PRIORITY.index(name), o)
        except ValueError:
            return (len(LINK_PRIORITY), o)

    links = [(LINK_LABELS.get(Path(o).name, Path(o).stem), str(found[o]))
             for o in sorted(found, key=_key)][:2]
    out_dir = root / "artifacts" / date
    if out_dir.is_dir():
        links.append(("产物目录", str(out_dir)))
    return links[:3]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_daily")
    ap.add_argument("--date", default=None, help="默认今天（上海时区）")
    ap.add_argument("--template", default=None,
                    help="计划模板；默认按 --job 从 schedule.yaml 取")
    ap.add_argument("--job", default="daily_report",
                    help="schedule.yaml 里的 job id（决定 plan_id 前缀与模板）")
    ap.add_argument("--timeout-min", type=float, default=90.0,
                    help="墙钟上限，超时退出码 1")
    ap.add_argument("--tick-s", type=float, default=10.0, help="tick 间隔")
    ap.add_argument("--verify-workers", type=int, default=2,
                    help="验收 worker 数（读 config workers.verifier_idle）")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只建计划不执行")
    ap.add_argument("--no-notify", action="store_true",
                    help="不弹桌面通知（默认弹框+声音，见 core/winnotify.py）")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else ROOT
    date = args.date or shanghai_today()
    t0 = time.monotonic()

    # 计划任务跑的时候没人盯着终端：Task Scheduler 会把 stdout 丢掉，
    # 于是"今天到底跑没跑、跑到哪一步"只剩结构化事件可查。这里把进度行
    # 同时落一份到 reports/run_daily_{job}_{date}.log，事后直接 tail 就行。
    log_path = root / "reports" / f"run_daily_{args.job}_{date}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("a", encoding="utf-8")

    def _say(msg: str = "") -> None:
        print(msg, flush=True)
        log_f.write(msg + "\n")
        log_f.flush()

    if args.template:
        tpl = args.template
    else:
        tpl = template_for(root, args.job)
        if tpl is None:
            jobs = known_jobs(root)
            _say(json.dumps({"error": "unknown_job", "job": args.job,
                             "known_jobs": jobs,
                             "detail": "schedule.yaml 里没有这个 job_id，"
                                       "拒绝用别的模板凑合"},
                            ensure_ascii=False, indent=2))
            # 起都起不来更要弹：不通知的话，这台机器"每天都在跑"其实是
            # 每天都在静默退出，没人会发现。
            winnotify.notify(
                f"❌ 起不来 {args.job} · {date}",
                "退出码 3：schedule.yaml 里没有这个 job_id\n"
                f"已知 job：{', '.join(str(j) for j in jobs)}",
                fail=True)
            return 3

    store = Store(root)
    # job_id 必须传：它决定 plan_id 前缀（plan_id_for）。漏传会退回默认的
    # "daily_report_{date}"——`--job housing_daily` 于是拿 housing 模板去建
    # daily 的 plan，并且下面 _progress 盯的也是别人家的 plan。
    # 实测产物：state.db 里的 daily_report_2026-10-05（template=housing_daily.yaml，
    # 4 个任务全 CANCELLED），就是因为这条没传 job_id。
    status, n = sched_mod.ensure_plan(store, date, tpl, job_id=args.job)
    _say(json.dumps({"date": date, "job": args.job, "template": tpl,
                     "plan": status, "tasks_created": n},
                    ensure_ascii=False))
    if args.dry_run:
        return 0
    if status == "skipped":
        _say("计划已存在，跳过建计划（任务状态仍会推进）")

    # 必须注入 VerifyPool。Orchestrator.verify_pool 默认是 None，
    # 而 dispatch_verify_async 第一行就是 `if self.verify_pool is None: return []`
    # ——于是验收永远不会被派发，任务全部卡在 VERIFYING。
    # 症状特别有欺骗性：产物文件**已经写出来了**（11~13KB 的报告就在磁盘上），
    # 任务状态却是 VERIFYING，日志里连 verify_result 都没有。
    # 看起来像"验收很慢"，实际是"验收从没开始过"。
    from core.verify_orchestrator import VerifyPool
    pool = VerifyPool(store, workers=int(args.verify_workers))
    orch = Orchestrator(root, executor_fn=executor.run_task, verify_pool=pool)
    deadline = time.monotonic() + args.timeout_min * 60

    # 防休眠包住整个执行期；退出（含超时/异常）自动还原电源策略。
    with power.KeepAwake() as ka:
        if not ka.ok and power.IS_WINDOWS:
            jlog(root, "WARN", "keep_awake_failed",
                 detail=power.status().get("error", "")[:200])
            _say("警告：未能阻止休眠，本次运行可能被系统中断")
        jlog(root, "INFO", "daily_run_started", date=date, template=tpl,
             keep_awake=ka.ok)

        def _progress() -> dict:
            """跑一个 tick 并回报进度。包成函数以便同步/异步两条路共用。"""
            orch.tick()
            # 与 ensure_plan 同一个 plan_id；漏传 job_id 会盯错 plan，
            # 于是"别人的任务跑完了"被报成"今天全部进终态 done=N"。
            plan_id = sched_mod.plan_id_for(date, args.job)
            rows = store.list_plan_tasks(plan_id)
            return {
                "plan_id": plan_id,
                "waiting": [r for r in rows if r["status"] in WAITING],
                "dead": [r for r in rows if r["status"] == "DEAD_LETTER"],
                "done": [r for r in rows if r["status"]
                         in ("DONE", "SKIPPED", "SKIPPED_CACHED")],
            }

        # **必须在 anyio 事件循环里驱动**。verify 池用 start_soon 起 worker，
        # 那是 anyio 的 API：没有运行中的事件循环时，任务被排进队列却永远
        # 不会被执行。症状是产物已落盘、任务卡在 VERIFYING、tick 日志正常，
        # 看起来像"验收慢"，实际是"验收 worker 一次都没启动"。
        # 所以这里不用裸 orch.tick()，而是把 async 循环真正跑起来。
        import anyio

        async def _drive() -> None:
            async with pool:                      # 启动并管理 worker 组
                while True:
                    st_ = _progress()
                    waiting, dead, done = (st_["waiting"], st_["dead"],
                                           st_["done"])
                    if not waiting:
                        _say(f"全部任务进终态：done={len(done)} "
                             f"dead={len(dead)}")
                        return
                    _say(f"[{time.strftime('%H:%M:%S')}] "
                         f"done={len(done)} waiting={len(waiting)} "
                         f"dead={len(dead)} "
                         f"waiting_ids={[r['task_id'] for r in waiting][:5]}")
                    if time.monotonic() > deadline:
                        _say(json.dumps({
                            "error": "timeout", "date": date,
                            "still_running": [{"task_id": r["task_id"],
                                               "status": r["status"]}
                                              for r in waiting][:10],
                            "elapsed_min": round(args.timeout_min, 1)},
                            ensure_ascii=False, indent=2))
                        jlog(root, "WARN", "daily_run_timeout", date=date,
                             waiting=len(waiting),
                             ids=",".join(r["task_id"] for r in waiting[:10]))
                        return
                    await anyio.sleep(args.tick_s)

        timed_out = {"v": False}
        anyio.run(_drive)

    plan_id = sched_mod.plan_id_for(date, args.job)
    rows = store.list_plan_tasks(plan_id)
    summary = {
        "date": date,
        "plan_id": plan_id,
        "tasks": [{"task_id": r["task_id"], "status": r["status"],
                   "complexity": (r.get("shard") or {}).get("complexity")}
                  for r in rows],
        "dead_letter": [r["task_id"] for r in rows
                        if r["status"] == "DEAD_LETTER"],
        "not_terminal": [r["task_id"] for r in rows
                         if r["status"] not in TERMINAL],
    }
    _say(json.dumps(summary, ensure_ascii=False, indent=2))
    jlog(root, "INFO", "daily_run_finished", date=date,
         dead=len(summary["dead_letter"]),
         open=len(summary["not_terminal"]))
    if summary["not_terminal"]:
        rc = 1
    elif summary["dead_letter"]:
        rc = 2
    else:
        rc = 0
    # 弹框 + 声音收尾。计划任务跑批时没人看终端，退出码与日志都等于
    # 没发生；这条通知是这套自动化里唯一会主动找人的东西。
    # links：通知里带上能一键打开的报告按钮（正文里也印一份路径，
    # 气泡回退没有按钮时仍可复制）。见 report_links()。
    winnotify.notify_summary(
        root, args.job, date, rc=rc,
        done=sum(1 for t in summary["tasks"]
                 if t["status"] in ("DONE", "SKIPPED", "SKIPPED_CACHED")),
        dead=len(summary["dead_letter"]),
        not_terminal=[t["task_id"] for t in summary["tasks"]
                      if t["status"] not in TERMINAL],
        elapsed_min=(time.monotonic() - t0) / 60.0,
        enabled=not args.no_notify,
        links=report_links(root, date, rows))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())