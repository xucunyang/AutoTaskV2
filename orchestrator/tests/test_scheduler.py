import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from core import scheduler
from core.store import Store

SH = timezone(timedelta(hours=8))
TPL = "templates/daily_plan.yaml"


def _seed_config(root: Path) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "schedule.yaml").write_text(
        yaml.safe_dump({
            "jobs": [{"id": "daily_report", "cron": "0 8 * * *",
                      "timezone": "Asia/Shanghai", "template": TPL,
                      "coalesce": True, "max_instances": 1,
                      "misfire_grace_time": 3600, "catchup": True}],
            "lease": {"ttl_s": 120, "heartbeat_s": 30},
            "session_switch_limit": 5,
        }, allow_unicode=True), encoding="utf-8")


def _seed_templates(root: Path) -> None:
    (root / "templates").mkdir(parents=True, exist_ok=True)
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    shutil_src = Path(__file__).resolve().parent.parent
    (root / "templates" / "daily_plan.yaml").write_text(
        (shutil_src / "templates" / "daily_plan.yaml").read_text(encoding="utf-8"),
        encoding="utf-8")
    for n in ("acceptance_t1.yaml", "acceptance_t2.yaml", "acceptance_t3.yaml"):
        (root / "schemas" / n).write_text(
            (shutil_src / "schemas" / n).read_text(encoding="utf-8"), encoding="utf-8")


def _store(root: Path) -> Store:
    _seed_config(root)
    _seed_templates(root)
    return Store(root)


def test_expand_template_substitutes_date_and_rules(tmp_path):
    _store(tmp_path)
    plan = scheduler.expand_template(tmp_path, TPL, "2026-09-29")
    assert plan["plan_id"] == "daily_report_2026-09-29"
    ids = [t["task_id"] for t in plan["tasks"]]
    assert ids == ["t1_collect", "t2_clean", "t3_report"]
    t1 = plan["tasks"][0]
    assert t1["source"] == "daily" and t1["priority"] == 10
    assert t1["idempotency_key"] == "daily_report_2026-09-29:t1_collect"
    assert t1["outputs"] == ["artifacts/2026-09-29/t1_collect.csv"]
    # {date} 在 acceptance path 中也被替换，且每条规则拿到 rule_id
    paths = [r["path"] for r in t1["acceptance"]]
    assert "artifacts/2026-09-29/t1_collect.csv" in paths
    assert all(r["rule_id"] for r in t1["acceptance"])
    # 依赖关系保留
    assert plan["tasks"][1]["depends_on"] == ["t1_collect"]


def test_housing_acceptance_points_at_own_output(tmp_path):
    """t1/t2/t3 共用一份 acceptance，path 必须是**各自**的产物。

    实测 2026-10-03：acceptance_market.yaml 的 path 写死
    artifacts/{date}/report.md，而三条任务的产物是 t1_tier.md /
    t2_xuzhou_nanjing.md / t3_hexagon_cities.md——report.md 是其中一条
    顺手写的，于是谁交了它，另外两条的 file_exists/bytes 也一起"通过"，
    产物存在性互相顶替。{output} 展开后各查各的。
    """
    root = Path(__file__).resolve().parent.parent
    plan = scheduler.expand_template(root, "templates/housing_daily.yaml",
                                     "2026-10-04", job_id="housing_daily")
    assert {t["outputs"][0] for t in plan["tasks"][:3]} == {
        "artifacts/2026-10-04/t1_tier.md",
        "artifacts/2026-10-04/t2_xuzhou_nanjing.md",
        "artifacts/2026-10-04/t3_hexagon_cities.md",
    }
    for t in plan["tasks"]:
        own = t["outputs"][0]
        for r in t["acceptance"]:
            if r.get("type") in ("file_exists", "business_rule"):
                assert r["path"] == own, (t["task_id"], r)
                assert "{output}" not in r["path"] and "{date}" not in r["path"]


def test_ensure_plan_idempotent(tmp_path):
    s = _store(tmp_path)
    assert scheduler.ensure_plan(s, "2026-09-29", TPL) == ("created", 3)
    assert scheduler.ensure_plan(s, "2026-09-29", TPL) == ("skipped", 0)
    assert len(s.list_by_status("PENDING")) == 3
    assert len(s.list_plans()) == 1


def test_two_plans_same_day_tasks_do_not_collide(tmp_path):
    s = _store(tmp_path)
    scheduler.ensure_plan(s, "2026-09-28", TPL)
    scheduler.ensure_plan(s, "2026-09-29", TPL)
    tasks = s.list_by_status("PENDING")
    assert len(tasks) == 6
    assert {t["plan_id"] for t in tasks} == {"daily_report_2026-09-28",
                                             "daily_report_2026-09-29"}
    seqs = [t["seq"] for t in tasks]
    assert seqs == sorted(seqs)  # seq不回退不丢


def test_catchup_backfills_missing_within_grace(tmp_path):
    s = _store(tmp_path)
    today = datetime.now(SH).strftime("%Y-%m-%d")
    res = scheduler.catchup_missing(s, 1, TPL, grace_s=3600)
    actions = {r["date"]: r["action"] for r in res}
    # 当天plan一律补建（当天日报仍要做），不按陈旧任务跳过
    assert actions[today] == "created"
    assert len(s.list_plans()) >= 1


def test_catchup_late_today_is_created_with_warn(tmp_path):
    s = _store(tmp_path)
    today = datetime.now(SH).strftime("%Y-%m-%d")
    res = scheduler.catchup_missing(s, 0, TPL, grace_s=1)
    assert res[0]["date"] == today and res[0]["action"] == "created"
    logs = (tmp_path / "logs" / "structured.jsonl").read_text(encoding="utf-8")
    assert "catchup_late_today" in logs


def test_catchup_skips_overdue_with_alert(tmp_path):
    s = _store(tmp_path)
    old = (datetime.now(SH) - timedelta(days=5)).strftime("%Y-%m-%d")
    res = scheduler.catchup_missing(s, 5, TPL, grace_s=3600)
    old_row = next(r for r in res if r["date"] == old)
    assert old_row["action"] == "skipped_overdue"
    assert old_row["overdue_s"] > 3600
    assert s.get_task(scheduler.plan_id_for(old), "t1_collect") is None
    logs = (tmp_path / "logs" / "structured.jsonl").read_text(encoding="utf-8")
    assert "catchup_skipped_overdue" in logs


def test_recover_missed_uses_catchup_flag(tmp_path):
    s = _store(tmp_path)
    out = scheduler.recover_missed(s, lookback_days=1)
    assert out
    assert all("plan_id" in r and "action" in r for r in out)


def test_recover_missed_skips_job_without_catchup(tmp_path):
    s = _store(tmp_path)
    cfg = scheduler.load_schedule(tmp_path)
    cfg["jobs"][0]["catchup"] = False
    (tmp_path / "config" / "schedule.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    assert scheduler.recover_missed(s, lookback_days=1) == []


def test_build_scheduler_registers_cron_job(tmp_path):
    _store(tmp_path)
    cfg = scheduler.load_schedule(tmp_path)
    sched = scheduler.build_scheduler(tmp_path, cfg)
    try:
        job = sched.get_job("daily_report")
        assert job is not None
        assert str(job.trigger).startswith("cron[")
        assert job.max_instances == 1
        assert job.misfire_grace_time == 3600
        assert job.coalesce is True
    finally:
        if sched.running:
            sched.shutdown(wait=False)   # 未start时shutdown会抛SchedulerNotRunningError


def test_build_scheduler_logs_timezone_resolution(tmp_path):
    """Phase1§4：内部UTC、cron按job.timezone解析，DST切换以APScheduler为准并记日志。
    记下一次触发时刻，不记偏移量——偏移量每天一样，看不出DST切换发生过。"""
    _store(tmp_path)
    cfg = scheduler.load_schedule(tmp_path)
    sched = scheduler.build_scheduler(tmp_path, cfg)
    try:
        pass
    finally:
        if sched.running:
            sched.shutdown(wait=False)
    logs = (tmp_path / "logs" / "structured.jsonl").read_text(encoding="utf-8")
    assert "job_timezone_resolved" in logs
    assert "Asia/Shanghai" in logs
    assert "cron[" in logs


def test_daily_job_expands_today(tmp_path):
    s = _store(tmp_path)
    scheduler._daily_job(str(tmp_path), "daily_report", "0 8 * * *", TPL)
    today = datetime.now(SH).strftime("%Y-%m-%d")
    assert s.get_task(scheduler.plan_id_for(today), "t1_collect") is not None


def test_cli_dry_run_lists_jobs(tmp_path, capsys):
    _store(tmp_path)
    assert scheduler.main(["--root", str(tmp_path), "--dry-run"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["jobs"][0]["id"] == "daily_report"
    assert out["jobs"][0]["tz"] == "Asia/Shanghai"


def test_cli_date_and_catchup(tmp_path, capsys):
    s = _store(tmp_path)
    assert scheduler.main(["--root", str(tmp_path), "--date", "2026-09-01"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == 3
    assert s.get_task("daily_report_2026-09-01", "t3_report") is not None
    assert scheduler.main(["--root", str(tmp_path), "--catchup"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_cli_rerun_rejects_illegal_edge(tmp_path, capsys):
    s = _store(tmp_path)
    scheduler.ensure_plan(s, "2026-09-01", TPL)
    # PENDING不能直接到RETRY：必须明确报错而不是静默改状态
    rc = scheduler.main(["--root", str(tmp_path), "--date", "2026-09-01",
                         "--rerun", "t1_collect"])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["error"] == "IllegalTransition"
    assert out["from_status"] == "PENDING"


def test_dependent_tasks_declare_inputs():
    """依赖上游却没声明 inputs → 卡片上是"（无输入产物）"，模型只能猜路径。

    实测 2026-10-04：daily_plan.yaml 的 t3_report 只有 depends_on、没有 inputs，
    objective 写的是"基于clean.csv"，而真实产物叫 artifacts/{date}/t2_clean.csv。
    模型全仓库找不到这个文件名，把 {"error": "clean.csv not found..."} 当产物
    交了上去 → json_schema violation → 连挂三轮，日报照样出不来。

    housing_daily.yaml 的 t4_rollup 是正确写法（depends_on + inputs 都写）。
    depends_on 决定**派发顺序**，inputs 决定**卡片里给不给路径**，缺一不可。
    """
    root = Path(__file__).resolve().parent.parent
    for tpl in sorted((root / "templates").glob("*.yaml")):
        data = yaml.safe_load(tpl.read_text(encoding="utf-8")) or {}
        for t in data.get("tasks") or []:
            if t.get("depends_on") and not t.get("inputs"):
                raise AssertionError(
                    f"{tpl.name}: 任务 {t.get('id')} 依赖 {t.get('depends_on')} "
                    f"却没声明 inputs——任务卡会写（无输入产物），模型只能猜路径")


def test_cli_rerun_reports_missing_task(tmp_path, capsys):
    _store(tmp_path)
    assert scheduler.main(["--root", str(tmp_path), "--date", "2026-09-01",
                           "--rerun", "nope"]) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"] == "task_not_found"
