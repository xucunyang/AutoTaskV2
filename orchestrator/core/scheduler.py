"""调度：APScheduler BackgroundScheduler + Daily模板展开（Phase1§4）。

时间触发（Daily）与事件触发（管道）严格解耦：
  - 本模块只负责把 templates/*.yaml 展开成 plan+tasks（ensure_plan，幂等）
  - 管道任务走 core.enqueue，即时入队，不等cron
内部UTC存储，cron按job.timezone（Asia/Shanghai）解析。
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from core.store import Store, AlreadyExists, Conflict, IllegalTransition
from core.utils import jlog, now_utc_iso

SHANGHAI = timezone(timedelta(hours=8))


def plan_id_for(date: str) -> str:
    return f"daily_report_{date}"


def load_schedule(root: Path) -> dict:
    return yaml.safe_load((root / "config" / "schedule.yaml").read_text(encoding="utf-8"))


def expand_template(root: Path, template_rel: str, date: str) -> dict:
    """读模板 → 展开{date} → 合并 acceptance_ref → 返回 plan+tasks 结构。"""
    tpl_path = root / template_rel
    tpl = yaml.safe_load(tpl_path.read_text(encoding="utf-8"))
    plan_id = plan_id_for(date)
    tasks = []
    for i, t in enumerate(tpl.get("tasks", []) or []):
        tid = t["id"]
        acc_file = t.get("acceptance_ref")
        acceptance = []
        if acc_file:
            acc_raw = (root / acc_file).read_text(encoding="utf-8")
            rules = yaml.safe_load(acc_raw) or []
            for j, r in enumerate(rules):
                rule = dict(r)
                rule.setdefault("rule_id", f"rule_{j}")
                for k in ("path", "schema_ref"):
                    if isinstance(rule.get(k), str):
                        rule[k] = rule[k].replace("{date}", date)
                acceptance.append(rule)
        shard = {
            "task_id": tid,
            "plan_id": plan_id,
            "status": "PENDING",
            "idempotency_key": f"{plan_id}:{tid}",
            "objective": t.get("objective", ""),
            "inputs": t.get("inputs", []) or [],
            "outputs": [o.replace("{date}", date) for o in t.get("outputs", []) or []],
            "depends_on": t.get("depends_on", []) or [],
            "acceptance": acceptance,
            "priority": int(t.get("priority", 10)),
            "source": "daily",
            "max_attempts": int(t.get("max_attempts", 3)),
            "privacy": t.get("privacy", "public"),
            "complexity": t.get("complexity", "simple"),
            "needs_web": bool(t.get("needs_web", False)),
            "freshness": t.get("freshness", "none"),
            "long_running": bool(t.get("long_running", False)),
        }
        shard["inputs"] = [
            {**a, "path": a["path"].replace("{date}", date)}
            if isinstance(a, dict) and isinstance(a.get("path"), str) else a
            for a in shard["inputs"]
        ]
        tasks.append(shard)
    return {"plan_id": plan_id, "template": template_rel, "date": date,
            "status": "ACTIVE", "created_at": now_utc_iso(),
            "updated_at": now_utc_iso(), "tasks": tasks}


def ensure_plan(store: Store, date: str, template_rel: str) -> tuple[str, int]:
    """幂等展开：plan已存在则skip（防双调度重复展开，§异常矩阵）。
    返回("created"|"skipped", 新建任务数)。"""
    pid = plan_id_for(date)
    created = store.ensure_plan(pid, template=template_rel)
    if not created:
        return "skipped", 0
    plan = expand_template(Path(store.root), template_rel, date)
    n = 0
    for shard in plan["tasks"]:
        try:
            _status, _row = store.insert_task(shard)
            n += 1
        except AlreadyExists:
            continue
    jlog(store.root, "INFO", "plan_expanded", plan_id=pid, tasks=n,
         template=template_rel)
    return "created", n


def catchup_missing(store: Store, lookback_days: int, template_rel: str,
                    grace_s: int) -> list[dict]:
    """补跑：扫过去N天缺plan；历史日期超misfire_grace跳过并告警（不补陈旧任务）。
    当天plan一律补建（当天日报仍要做），只是晚建时记WARN，不按陈旧任务处理。"""
    root = Path(store.root)
    out = []
    now_sh = datetime.now(SHANGHAI)
    today = now_sh.strftime("%Y-%m-%d")
    existing = {p["plan_id"] for p in store.list_plans()}
    for d in range(lookback_days, -1, -1):
        date = (now_sh - timedelta(days=d)).strftime("%Y-%m-%d")
        pid = plan_id_for(date)
        if pid in existing:
            out.append({"date": date, "plan_id": pid, "action": "exists"})
            continue
        planned_at = (datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
                      + timedelta(hours=8))
        overdue = (now_sh - planned_at).total_seconds()
        if date != today and overdue > grace_s:
            jlog(root, "WARN", "catchup_skipped_overdue", plan_id=pid,
                 overdue_s=int(overdue), grace_s=grace_s)
            out.append({"date": date, "plan_id": pid, "action": "skipped_overdue",
                        "overdue_s": int(overdue)})
            continue
        status, n = ensure_plan(store, date, template_rel)
        if date == today and overdue > grace_s:
            jlog(root, "WARN", "catchup_late_today", plan_id=pid,
                 overdue_s=int(overdue), grace_s=grace_s)
        out.append({"date": date, "plan_id": pid, "action": status, "tasks": n,
                    "overdue_s": int(overdue)})
    return out


def build_scheduler(root: Path, cfg: dict) -> BackgroundScheduler:
    jobstore_path = (root / "state" / "scheduler.db").as_posix()
    url = f"sqlite:///{jobstore_path}"
    sched = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=url)},
        timezone=SHANGHAI,
        job_defaults={"coalesce": True, "max_instances": 1,
                      "misfire_grace_time": 3600},
    )
    for job in cfg.get("jobs", []) or []:
        sched.add_job(
            _daily_job,
            trigger="cron",
            args=[str(root), job["id"], job["cron"], job.get("template")],
            id=job["id"],
            timezone=job.get("timezone", "Asia/Shanghai"),
            replace_existing=True,
            coalesce=job.get("coalesce", True),
            max_instances=job.get("max_instances", 1),
            misfire_grace_time=job.get("misfire_grace_time", 3600),
        )
    return sched


def _daily_job(root_str: str, job_id: str, cron: str, template: str | None) -> None:
    root = Path(root_str)
    store = Store(root)
    cfg = load_schedule(root)
    tpl = template or next(
        (j["template"] for j in cfg.get("jobs", []) or [] if j["id"] == job_id),
        "templates/daily_plan.yaml")
    date = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    status, n = ensure_plan(store, date, tpl)
    jlog(root, "INFO", "daily_fired", job_id=job_id, date=date,
         result=status, tasks=n)


def recover_missed(store: Store, lookback_days: int = 3) -> list[dict]:
    """启动补跑：catchup=true的job补齐N天内缺失plan；超宽限记skip+告警。"""
    root = Path(store.root)
    cfg = load_schedule(root)
    out = []
    for job in cfg.get("jobs", []) or []:
        if not job.get("catchup", False):
            continue
        out.extend(catchup_missing(store, lookback_days,
                                   job.get("template", "templates/daily_plan.yaml"),
                                   int(job.get("misfire_grace_time", 3600))))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="core.scheduler")
    ap.add_argument("--date", default=None, help="手动展开指定日期（默认今天）")
    ap.add_argument("--catchup", action="store_true", help="补跑过去N天缺失plan")
    ap.add_argument("--lookback-days", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不启动")
    ap.add_argument("--rerun", default=None, help="重跑指定任务ID（需配合--date）")
    ap.add_argument("--root", default=None, help="orchestrator根目录（默认本包上级）")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    store = Store(root)
    cfg = load_schedule(root)
    tpl = (cfg.get("jobs") or [{}])[0].get("template", "templates/daily_plan.yaml")

    if args.catchup:
        print(json.dumps(recover_missed(store, args.lookback_days),
                         ensure_ascii=False, indent=2))
        return 0
    if args.date:
        status, n = ensure_plan(store, args.date, tpl)
        print(json.dumps({"date": args.date, "result": status, "tasks": n},
                         ensure_ascii=False))
        if args.rerun:
            pid = plan_id_for(args.date)
            t = store.get_task(pid, args.rerun)
            if t is None:
                print(json.dumps({"error": "task_not_found", "task_id": args.rerun}))
                return 2
            try:
                reset = store.transition(pid, args.rerun, "RETRY", agent="cli",
                                         run_id="manual_rerun",
                                         expect_version=t["version"],
                                         payload={"reason": "manual_cli_rerun",
                                                  "manual_rerun": True})
            except (IllegalTransition, Conflict) as e:
                # 只允许状态机允许的边（当前态→RETRY），否则明确报错而非静默改状态
                print(json.dumps({"error": type(e).__name__, "detail": str(e),
                                  "from_status": t["status"]}, ensure_ascii=False))
                return 2
            print(json.dumps({"rerun": reset["task_id"], "status": reset["status"]},
                             ensure_ascii=False))
        return 0
    if args.dry_run:
        print(json.dumps({"jobs": [
            {"id": j["id"], "cron": j["cron"], "tz": j.get("timezone"),
             "template": j.get("template"), "catchup": j.get("catchup", False)}
            for j in cfg.get("jobs", [])]}, ensure_ascii=False, indent=2))
        return 0
    sched = build_scheduler(root, cfg)
    missed = recover_missed(store)
    if missed:
        jlog(root, "INFO", "startup_catchup", result=missed)
    sched.start()
    jlog(root, "INFO", "scheduler_started", jobs=[j["id"] for j in cfg.get("jobs", [])])
    try:
        import time as _t
        while True:
            _t.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown(wait=True)
        jlog(root, "INFO", "scheduler_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
