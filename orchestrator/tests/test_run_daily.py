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
from scripts.run_daily import report_links               # noqa: E402
from scripts.run_daily import exit_code, stuck_on_dead   # noqa: E402
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


def test_run_daily_wires_the_desktop_notification():
    """接线门禁：跑批收尾必须弹框+声音，"起不来"那条也不能漏。

    计划任务跑批时没人看终端——退出码、reports/*.log、结构化事件
    全都等于没发生。这条桌面通知是这套自动化里唯一会主动找人的东西，
    被谁顺手删掉就等于通知功能静默失效（测试还全绿）。
    """
    src = (ROOT_TPL / "scripts" / "run_daily.py").read_text(encoding="utf-8")
    assert "winnotify.notify_summary(" in src, "收尾通知被摘掉了"
    assert "winnotify.notify(" in src, "unknown_job 那条不弹了"
    assert "--no-notify" in src, "没有关闭开关"
    # 通知必须带报告链接：路径印成文字点不动，链接靠 report_links 传进去
    assert "report_links(" in src, "报告链接没接进 run_daily"
    assert "links=report_links(" in src, "links 参数没传给 notify_summary"


def test_report_links_picks_own_existing_reports(tmp_path):
    """链接只挑**本 plan 声明的、已落盘的**正文，最后一条是产物目录。

    `artifacts/{date}/` 是两条链共用的：不按声明挑，housing 的通知就会
    点开 daily 的日报（用户打开的是别人的报告，比没有按钮更误导）。
    """
    date = "2026-10-06"
    d = tmp_path / "artifacts" / date
    d.mkdir(parents=True)
    for name in ("report.md", "charts.md", "t4_investment_watch.md",
                 "other_chain.md"):
        (d / name).write_text("x", encoding="utf-8")
    (d / "summary.json").write_text("{}", encoding="utf-8")
    rows = [
        {"shard": {"outputs": [f"artifacts/{date}/report.md",
                               f"artifacts/{date}/summary.json"]}},
        {"shard": {"outputs": [f"artifacts/{date}/t4_investment_watch.md"]}},
        {"shard": {"outputs": [f"artifacts/{date}/charts.md",
                               f"artifacts/{date}/missing.md"]}},
        # other_chain.md 只在盘上、**没被本 plan 声明** → 绝不能进链接
    ]
    got = report_links(tmp_path, date, rows)
    paths = [p for _lab, p in got]
    assert [lab for lab, _p in got] == ["日报", "投资观察", "产物目录"]
    assert str(d / "report.md") in paths
    assert str(d / "t4_investment_watch.md") in paths
    assert str(d / "missing.md") not in paths, "没落盘的文件不能做成按钮"
    assert str(d / "summary.json") not in paths, "json 不是给人读的正文"
    # other_chain.md 虽然在盘上，但**不是本 plan 声明的** → 不进链接
    assert not any("other_chain" in p for p in paths), "串了别人家的产物"
    assert len(got) <= 3, "toast 按钮最多 3 个"


def test_report_links_without_artifacts_dir(tmp_path):
    """目录都还没有时不能凭空造一个按钮（点了没反应比没按钮更糟）。"""
    assert report_links(tmp_path, "2099-01-01",
                        [{"shard": {"outputs": []}}]) == []
    # 没有任何 md，但目录在 → 只剩目录
    (tmp_path / "artifacts" / "2099-01-01").mkdir(parents=True)
    got = report_links(tmp_path, "2099-01-01", [{"shard": {"outputs": []}}])
    assert [lab for lab, _p in got] == ["产物目录"]


# ---- 卡死提前收工 + 退出码优先级（2026-10-05 finance 首跑实测） ----

def _row(task_id: str, status: str, deps=()) -> dict:
    return {"task_id": task_id, "status": status,
            "shard": {"depends_on": list(deps)}}


def test_exit_code_dead_letter_wins_over_in_flight():
    """死信优先于在途：被死信挡住的任务会一直 PENDING。

    按老顺序（先判 not_terminal）会一律报 1，通知和监控只说"还在跑"，
    真正需要人处理的死信反被遮住。
    """
    assert exit_code(0, 0) == 0
    assert exit_code(2, 0) == 1
    assert exit_code(0, 1) == 2
    assert exit_code(2, 1) == 2, "死信被在途遮住，监控就看不见了"


def test_stuck_when_every_waiting_task_is_blocked_by_dead():
    """全被死信挡住 → 再等也不会有终态，提前收工把机器让给下一个 job。"""
    # 直接依赖：t3 → t1(死)
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t3", "PENDING", ["t1"]),
                          _row("t2", "DONE")]) is True
    # 间接依赖：t3 → t2(PENDING) → t1(死)，照样卡死
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "PENDING", ["t1"]),
                          _row("t3", "PENDING", ["t2"])]) is True
    # 自环/未知依赖不能把判断卡住
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "PENDING", ["t2", "ghost"])]) is False


def test_not_stuck_when_still_have_something_to_wait_for():
    """只在"没死信 / 还有人在动 / 依赖链还通着"时才算没卡死。"""
    # 没死信：正常等待
    assert stuck_on_dead([_row("t1", "PENDING")]) is False
    # 有死信但另一条链还在动 → 它可能做完，也可能新死一个，再等等
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "RUNNING")]) is False
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "VERIFYING")]) is False
    # READY 的依赖是满足的，它随时能被派发 → 不是卡死
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "READY")]) is False
    # 剩下的任务依赖链通着（上游 DONE）→ 不是卡死
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "PENDING", ["t3"]),
                          _row("t3", "DONE")]) is False
    # 全进终态 → 走正常出口，别抢在这报卡死
    assert stuck_on_dead([_row("t1", "DEAD_LETTER"),
                          _row("t2", "DONE")]) is False
