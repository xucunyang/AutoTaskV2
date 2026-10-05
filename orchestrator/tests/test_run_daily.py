"""run_daily 的 plan 归属：--job 决定 plan_id 前缀，两个 job 互不串门。

背景（真实事故，不是假想）：
`run_daily.py` 曾经调 `ensure_plan(store, date, tpl)` 而**不传 job_id**，
于是 `--job housing_daily` 也去建 `daily_report_{date}`——housing 模板塞进
daily 的 plan_id。state.db 里的 `daily_report_2026-10-05`（template=
housing_daily.yaml、4 个任务全 CANCELLED）就是这么来的，而且因为
`ensure_plan` 对"存在且有任务"一律 skip，当天的日报**静默不跑**。
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_daily import main as run_daily_main     # noqa: E402
from core.store import Store                             # noqa: E402

ROOT_TPL = Path(__file__).resolve().parents[1]
DATE = "2026-10-05"


def _root(tmp_path: Path) -> Path:
    """带 config/templates/schemas 的隔离 root（两个 job 都在 schedule 里）。"""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "schedule.yaml").write_text(
        yaml.safe_dump({
            "jobs": [
                {"id": "daily_report", "cron": "0 8 * * *",
                 "timezone": "Asia/Shanghai",
                 "template": "templates/daily_plan.yaml", "catchup": True},
                {"id": "housing_daily", "cron": "0 8 * * *",
                 "timezone": "Asia/Shanghai",
                 "template": "templates/housing_daily.yaml", "catchup": True},
            ],
            "workers": {"reserved_low_slot": 1},
        }, allow_unicode=True), encoding="utf-8")
    for sub in ("templates", "schemas"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        # 全量拷贝而不是列名单：列名单时新增一个文件就会漏拷，
        # 测的就变成"种子没跟上"而不是被测系统。
        for src in sorted((ROOT_TPL / sub).iterdir()):
            if src.is_file():
                (tmp_path / sub / src.name).write_text(
                    src.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _plan_ids(root: Path) -> set[str]:
    return {p["plan_id"] for p in Store(root).list_plans()}


def _task_ids(root: Path, plan_id: str) -> set[str]:
    return {t["task_id"] for t in Store(root).list_plan_tasks(plan_id)}


def test_housing_job_builds_its_own_plan(tmp_path, capsys):
    _root(tmp_path)
    rc = run_daily_main(["--root", str(tmp_path), "--date", DATE,
                         "--job", "housing_daily", "--dry-run"])
    assert rc == 0, capsys.readouterr().out
    out = json.loads(capsys.readouterr().out)
    assert out["plan"] == "created"

    ids = _plan_ids(tmp_path)
    assert "housing_daily_2026-10-05" in ids
    # 核心断言：**没有**把 housing 的活儿挂到 daily 的 plan_id 上
    assert "daily_report_2026-10-05" not in ids
    tasks = _task_ids(tmp_path, "housing_daily_2026-10-05")
    assert "t4_rollup" in tasks and "t5_charts" in tasks


def test_daily_job_builds_its_own_plan(tmp_path, capsys):
    _root(tmp_path)
    rc = run_daily_main(["--root", str(tmp_path), "--date", DATE,
                         "--job", "daily_report", "--dry-run"])
    assert rc == 0, capsys.readouterr().out
    capsys.readouterr()

    ids = _plan_ids(tmp_path)
    assert "daily_report_2026-10-05" in ids
    assert "housing_daily_2026-10-05" not in ids
    tasks = _task_ids(tmp_path, "daily_report_2026-10-05")
    # daily 模板的图表任务；housing 模板的 t4_rollup 不该出现
    assert "t4_charts" in tasks
    assert "t4_rollup" not in tasks


def test_both_jobs_coexist_for_the_same_date(tmp_path, capsys):
    """同一天两个 job 都跑：各建各的 plan，互不 skip。"""
    _root(tmp_path)
    for job in ("daily_report", "housing_daily"):
        rc = run_daily_main(["--root", str(tmp_path), "--date", DATE,
                             "--job", job, "--dry-run"])
        assert rc == 0, capsys.readouterr().out
        capsys.readouterr()

    ids = _plan_ids(tmp_path)
    assert {"daily_report_2026-10-05", "housing_daily_2026-10-05"} <= ids
    assert len(_task_ids(tmp_path, "daily_report_2026-10-05")) >= 4
    assert len(_task_ids(tmp_path, "housing_daily_2026-10-05")) >= 5


def test_second_run_is_idempotent(tmp_path, capsys):
    """重复触发（计划任务重跑/StartWhenAvailable 补跑）不能重复建任务。"""
    _root(tmp_path)
    for _ in range(2):
        rc = run_daily_main(["--root", str(tmp_path), "--date", DATE,
                             "--job", "housing_daily", "--dry-run"])
        assert rc == 0, capsys.readouterr().out
        capsys.readouterr()
    assert len(_task_ids(tmp_path, "housing_daily_2026-10-05")) >= 5


def test_unknown_job_fails_loudly(tmp_path, capsys):
    """拼错 job 必须退出码 3，不能静默拿 jobs[0] 的模板凑合。"""
    _root(tmp_path)
    rc = run_daily_main(["--root", str(tmp_path), "--date", DATE,
                         "--job", "housing_dayly", "--dry-run"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 3
    assert out["error"] == "unknown_job"
    assert out["job"] == "housing_dayly"
    assert set(out["known_jobs"]) == {"daily_report", "housing_daily"}
    assert _plan_ids(tmp_path) == set()
