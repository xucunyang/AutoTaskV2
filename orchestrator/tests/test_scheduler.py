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
    # 全量拷贝：模板的 acceptance_ref 指向哪些文件就可能读哪些，
    # 少拷一个（如新增的 acceptance_charts.yaml）→ 展开时 FileNotFoundError，
    # 测出来的不是"模板写错了"而是"种子没跟上"。
    for n in sorted(p.name for p in (shutil_src / "schemas").glob("*.yaml")):
        (root / "schemas" / n).write_text(
            (shutil_src / "schemas" / n).read_text(encoding="utf-8"),
            encoding="utf-8")


def _store(root: Path) -> Store:
    _seed_config(root)
    _seed_templates(root)
    return Store(root)


def test_expand_template_substitutes_date_and_rules(tmp_path):
    _store(tmp_path)
    plan = scheduler.expand_template(tmp_path, TPL, "2026-09-29")
    assert plan["plan_id"] == "daily_report_2026-09-29"
    ids = [t["task_id"] for t in plan["tasks"]]
    # t4_charts 插在 t2 与 t3 之间：t3 要贴图，图必须先有
    assert ids == ["t1_collect", "t2_clean", "t4_charts", "t3_report"]
    t1 = plan["tasks"][0]
    assert t1["source"] == "daily" and t1["priority"] == 10
    assert t1["idempotency_key"] == "daily_report_2026-09-29:t1_collect"
    assert t1["outputs"] == ["artifacts/2026-09-29/t1_collect.csv"]
    # {date} 在 acceptance path 中也被替换，且每条规则拿到 rule_id
    # （.get：source_traceable/freshness/source_quality 没有 path 字段）
    paths = [r.get("path") for r in t1["acceptance"]]
    assert "artifacts/2026-09-29/t1_collect.csv" in paths
    assert all(r["rule_id"] for r in t1["acceptance"])
    # 依赖关系保留
    assert plan["tasks"][1]["depends_on"] == ["t1_collect"]


def test_housing_acceptance_points_at_own_output(tmp_path):
    """三条 housing 链的验收 path 必须落在**该任务自己声明的** outputs 里。

    实测 2026-10-03：acceptance_market.yaml 的 path 写死
    artifacts/{date}/report.md，而三条任务的产物是 t1_tier.md /
    t2_xuzhou_nanjing.md / t3_hexagon_cities.md——report.md 是其中一条
    顺手写的，于是谁交了它，另外两条的 file_exists/bytes 也一起"通过"，
    产物存在性互相顶替。{output} 展开后各查各的。

    2026-10-07 拆链后这条断言的**覆盖面变大了**：t1/t2/t3 分处三条链，
    且 t1_tier / t2_xz_nj 各自有第二个产物（spotlight.json / data.json），
    验收里会引用 outputs[1]。所以判据从"等于 outputs[0]"放宽成
    "属于本任务的 outputs 集合"——放宽的是**集合，不是归属**：
    查别人家的产物照样红（tests/test_acceptance_refs.py 里那条
    test_acceptance_paths_point_at_declared_outputs 从模板侧查同一件事）。
    """
    root = Path(__file__).resolve().parent.parent
    chains = (("templates/housing_daily.yaml", "housing_daily"),
              ("templates/housing_monthly.yaml", "housing_monthly"),
              ("templates/housing_fortnight.yaml", "housing_fortnight"),
              ("templates/housing_rr_fiveday.yaml", "housing_rr_fiveday"))
    seen = 0
    for tpl_rel, job in chains:
        plan = scheduler.expand_template(root, tpl_rel, "2026-10-04", job_id=job)
        assert plan["tasks"], f"{tpl_rel} 展开出 0 个任务"
        for t in plan["tasks"]:
            own = set(t["outputs"])
            assert own, f"{job}::{t['task_id']} 没声明 outputs"
            for r in t["acceptance"]:
                p = r.get("path")
                if not p:
                    continue
                assert p in own, (job, t["task_id"], p, sorted(own))
                assert "{output}" not in p and "{date}" not in p, (job, t["task_id"], p)
            seen += 1
    # 四条链一共 7 个任务，少一个说明有任务没被这段断言覆盖到
    assert seen == 7, f"只覆盖到 {seen} 个任务"


def test_ensure_plan_idempotent(tmp_path):
    s = _store(tmp_path)
    assert scheduler.ensure_plan(s, "2026-09-29", TPL) == ("created", 4)
    assert scheduler.ensure_plan(s, "2026-09-29", TPL) == ("skipped", 0)
    assert len(s.list_by_status("PENDING")) == 4
    assert len(s.list_plans()) == 1


def test_two_plans_same_day_tasks_do_not_collide(tmp_path):
    s = _store(tmp_path)
    scheduler.ensure_plan(s, "2026-09-28", TPL)
    scheduler.ensure_plan(s, "2026-09-29", TPL)
    tasks = s.list_by_status("PENDING")
    # 两天 × 4 个任务（2026-10-04 加了 t4_charts）
    assert len(tasks) == 8
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


def test_catchup_late_today_is_created_with_warn(tmp_path, monkeypatch):
    """今天 08:00 计划 + grace_s=1：跑到 09:00 应触发 catchup_late_today 告警。

    catchup_missing 内部用 `datetime.now(SHANGHAI)` 拿当前时间，overdue=
    (now - 08:00)。**0:00 ~ 17:00 UTC（8:00 ~ 01:00 SH）之间** overdue ≤ 0，
    grace_s=1 时 `overdue > grace_s` 不成立，告警不触发，测试就红。

    把"现在"固定到今天 09:00 SH：overdue = 3600s > 1 → 告警触发。
    """
    from unittest.mock import MagicMock
    s = _store(tmp_path)
    today = datetime.now(SH).strftime("%Y-%m-%d")
    fake_now = datetime.now(SH).replace(hour=9, minute=0, second=0, microsecond=0)
    # core/scheduler.py 里写 `from datetime import datetime`，调用
    # datetime.now(SHANGHAI)。直接替换模块里的 datetime 引用为 Mock 类
    # ——这样 `.now(SHANGHAI)` 调用走 Mock；但 Mock 自身**不能**作为
    # strptime 的第一参数（datetime 还在用 strptime(date_str)）。
    # 简单做法：保留原 datetime，但把它的 .now 静态方法 patch 掉——
    # 不可行（datetime 是 C 类型）。所以改成 module-level replace：
    # 给 core.scheduler 注入一个替代 datetime 类，保留原 datetime 的
    # strptime 行为。
    real_dt = datetime
    fake_dt = MagicMock(wraps=real_dt)
    fake_dt.now.return_value = fake_now
    monkeypatch.setattr("core.scheduler.datetime", fake_dt)
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
    assert json.loads(capsys.readouterr().out)["tasks"] == 4
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
