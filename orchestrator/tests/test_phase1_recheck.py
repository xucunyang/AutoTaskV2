"""Phase1 复查：Phase0补落码后，Phase1的调用点是否还成立。

这批测试是"上游规则收紧后，下游是否被打破"的回归网。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import scheduler
from core.store import IllegalTransition, Store
from tests.test_scheduler import TPL, _store


def test_daily_dag_respects_depends_gate(tmp_path):
    """日报模板是 t1→t2→t3 三级链。Phase0 depends_on门禁落地后，
    展开的任务必须真的按序才能进READY，不能像以前一样无脑全推。"""
    s = _store(tmp_path)
    scheduler.ensure_plan(s, "2026-09-29", TPL)
    pid = "daily_report_2026-09-29"
    # 无依赖的 t1 可以进
    t = s.get_task(pid, "t1_collect")
    t = s.transition(pid, "t1_collect", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert t["status"] == "READY"
    # t2/t3 被门禁挡住
    for tid in ("t2_clean", "t3_report"):
        row = s.get_task(pid, tid)
        with pytest.raises(IllegalTransition, match="deps_unsatisfied"):
            s.transition(pid, tid, "READY", agent="o", run_id="r",
                         expect_version=row["version"])


def test_daily_dag_advances_in_order(tmp_path):
    """整条链走通：t1 DONE → t2 READY → t2 DONE → t3 READY。"""
    s = _store(tmp_path)
    scheduler.ensure_plan(s, "2026-09-29", TPL)
    pid = "daily_report_2026-09-29"

    def advance(tid):
        row = s.get_task(pid, tid)
        if row["status"] == "PENDING":
            row = s.transition(pid, tid, "READY", agent="o", run_id="r",
                               expect_version=row["version"])
        row = s.transition(pid, tid, "RUNNING", agent="o", run_id="r",
                           expect_version=row["version"], lease_op="acquire")
        row = s.transition(pid, tid, "SUBMITTED", agent="sub", run_id="r",
                           expect_version=row["version"], role="subagent")
        row = s.transition(pid, tid, "VERIFYING", agent="o", run_id="r",
                           expect_version=row["version"])
        return s.transition(pid, tid, "DONE", agent="v", run_id="r",
                            expect_version=row["version"], role="verifier")

    for tid in ("t1_collect", "t2_clean", "t3_report"):
        got = advance(tid)
        assert got["status"] == "DONE", tid
    assert s.replay(pid)["ok"]


def test_expand_template_carries_depends_on(tmp_path):
    """模板的depends_on必须真的进shard，否则门禁形同虚设。"""
    _store(tmp_path)
    plan = scheduler.expand_template(tmp_path, TPL, "2026-09-29")
    by_id = {t["task_id"]: t for t in plan["tasks"]}
    assert by_id["t1_collect"]["depends_on"] == []
    assert by_id["t2_clean"]["depends_on"] == ["t1_collect"]
    assert by_id["t3_report"]["depends_on"] == ["t2_clean"]


def test_store_read_dag_blocked_count(tmp_path):
    """主循环需要知道"还有多少任务被依赖挡住"才算得出还剩多少活。"""
    s = _store(tmp_path)
    scheduler.ensure_plan(s, "2026-09-29", TPL)
    pid = "daily_report_2026-09-29"
    blocked = [tid for tid in ("t1_collect", "t2_clean", "t3_report")
               if s.unsatisfied_dependencies(pid, tid)]
    assert blocked == ["t2_clean", "t3_report"]
