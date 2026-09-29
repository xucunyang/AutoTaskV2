"""Phase0 §2.2/§2.3 强制规则的落码测试：此前这些规则只存在于文档，
store.transition()并未强制（权限矩阵常量定义了但从未被调用）。

本文件是"文档→代码"一致性的护栏：设计里写死的规则，这里逐条验证真会拦。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import Conflict, IllegalTransition, StaleOwner, Store
from tests.test_store import _ckpt, _mark_long_running, _ready_task, _shard


def _dag(store: Store) -> None:
    """t1 → t2 → t3 三级链，全PENDING。"""
    for tid, deps in (("t1", []), ("t2", ["t1"]), ("t3", ["t2"])):
        store.insert_task(_shard(tid, "p1", depends_on=deps))


def _run(store: Store, tid: str) -> dict:
    t = store.get_task("p1", tid)
    if t["status"] == "PENDING":        # 依赖门禁：只有PENDING才需要先过READY
        t = store.transition("p1", tid, "READY", agent="o", run_id="r",
                             expect_version=t["version"])
    return store.transition("p1", tid, "RUNNING", agent="o", run_id="r",
                            expect_version=t["version"], lease_op="acquire")


def _finish(store: Store, tid: str) -> None:
    t = store.get_task("p1", tid)
    t = store.transition("p1", tid, "SUBMITTED", agent="sub", run_id="r",
                         expect_version=t["version"], role="subagent")
    store.transition("p1", tid, "VERIFYING", agent="o", run_id="r",
                     expect_version=t["version"])
    t = store.get_task("p1", tid)
    store.transition("p1", tid, "DONE", agent="v", run_id="r",
                     expect_version=t["version"], role="verifier")


# ---------- §2.3 depends_on 门禁 ----------

def test_depends_gate_blocks_ready_until_upstream_done(tmp_path):
    s = Store(tmp_path)
    _dag(s)
    t = s.get_task("p1", "t2")
    with pytest.raises(IllegalTransition, match="deps_unsatisfied"):
        s.transition("p1", "t2", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert s.unsatisfied_dependencies("p1", "t2") == [{"task_id": "t1",
                                                       "status": "PENDING"}]
    # 上游DONE后放行
    _run(s, "t1")
    _finish(s, "t1")
    t = s.get_task("p1", "t2")
    t = s.transition("p1", "t2", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert t["status"] == "READY"


def test_depends_gate_transitive_chain(tmp_path):
    s = Store(tmp_path)
    _dag(s)
    _run(s, "t1")
    _finish(s, "t1")
    t = s.get_task("p1", "t2")
    t = s.transition("p1", "t2", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    # t2还没DONE，t3仍被挡
    t3 = s.get_task("p1", "t3")
    with pytest.raises(IllegalTransition, match="deps_unsatisfied"):
        s.transition("p1", "t3", "READY", agent="o", run_id="r",
                     expect_version=t3["version"])


def test_skipped_dependency_satisfies_by_default(tmp_path):
    """默认视SKIPPED/SKIPPED_CACHED为满足：跳过的任务不该永久堵死下游。"""
    s = Store(tmp_path)
    _dag(s)
    t = s.get_task("p1", "t1")
    s.transition("p1", "t1", "SKIPPED", agent="o", run_id="r",
                 expect_version=t["version"])
    assert s.unsatisfied_dependencies("p1", "t2") == []
    t = s.get_task("p1", "t2")
    t = s.transition("p1", "t2", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert t["status"] == "READY"


def test_strict_mode_does_not_accept_skipped(tmp_path):
    s = Store(tmp_path)
    _dag(s)
    t = s.get_task("p1", "t1")
    s.transition("p1", "t1", "SKIPPED", agent="o", run_id="r",
                 expect_version=t["version"])
    t = s.get_task("p1", "t2")
    with pytest.raises(IllegalTransition, match="deps_unsatisfied"):
        s.transition("p1", "t2", "READY", agent="o", run_id="r",
                     expect_version=t["version"],
                     payload={"strict_depends": True})


def test_missing_dependency_is_unsatisfied(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t9", "p1", depends_on=["ghost"]))
    t = s.get_task("p1", "t9")
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t9", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert s.unsatisfied_dependencies("p1", "t9") == [{"task_id": "ghost",
                                                       "status": "MISSING"}]


def test_task_without_depends_always_ready(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("solo", "p1"))
    t = s.get_task("p1", "solo")
    t = s.transition("p1", "solo", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert t["status"] == "READY"


# ---------- §2.2 权限矩阵 ----------

def test_subagent_cannot_write_system_transitions(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = s.get_task("p1", "t1")
    t = s.transition("p1", "t1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    # 子Agent不得自行推进到VERIFYING（系统跃迁）
    t = s.transition("p1", "t1", "SUBMITTED", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")
    # 子Agent不得自行推进到VERIFYING（系统跃迁：SYSTEM_ONLY + ORCH_ONLY双拦）
    with pytest.raises(IllegalTransition, match="system_only|orch_only"):
        s.transition("p1", "t1", "VERIFYING", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")
    assert s.get_task("p1", "t1")["status"] == "SUBMITTED"


def test_subagent_cannot_skip_verification(tmp_path):
    """子Agent不得 READY→DONE 直写（Phase0目标：杜绝直写终态）。"""
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = s.get_task("p1", "t1")
    t = s.transition("p1", "t1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t1", "DONE", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")


def test_verifier_role_scope(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = _run(s, "t1")
    t = s.transition("p1", "t1", "SUBMITTED", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")
    t = s.transition("p1", "t1", "VERIFYING", agent="o", run_id="r",
                     expect_version=t["version"])
    # Verifier能写VERIFYING出边
    t = s.transition("p1", "t1", "DONE", agent="v", run_id="r",
                     expect_version=t["version"], role="verifier")
    assert t["status"] == "DONE"


def test_verifier_cannot_run_task(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = s.get_task("p1", "t1")
    t = s.transition("p1", "t1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    with pytest.raises(IllegalTransition, match="verifier_cannot"):
        s.transition("p1", "t1", "RUNNING", agent="v", run_id="r",
                     expect_version=t["version"], role="verifier")


def test_unknown_role_rejected(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = s.get_task("p1", "t1")
    with pytest.raises(IllegalTransition, match="unknown_role"):
        s.transition("p1", "t1", "READY", agent="x", run_id="r",
                     expect_version=t["version"], role="root")


def test_orchestrator_keeps_full_authority(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = _run(s, "t1")
    t = s.transition("p1", "t1", "SUBMITTED", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "VERIFYING", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "DONE", agent="o", run_id="r",
                     expect_version=t["version"])
    assert t["status"] == "DONE"


# ---------- §2.3 RETRY / DEAD_LETTER 必填 ----------

def test_retry_requires_reason(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = _run(s, "t1")
    t = s.transition("p1", "t1", "FAILED", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")
    with pytest.raises(IllegalTransition, match="retry_requires_reason"):
        s.transition("p1", "t1", "RETRY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert s.get_task("p1", "t1")["status"] == "FAILED"


def test_dead_letter_requires_reason_error_and_sop(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = _run(s, "t1")
    t = s.transition("p1", "t1", "FAILED", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")
    with pytest.raises(IllegalTransition, match="dead_letter_missing"):
        s.transition("p1", "t1", "DEAD_LETTER", agent="o", run_id="r",
                     expect_version=t["version"])
    with pytest.raises(IllegalTransition, match="last_error"):
        s.transition("p1", "t1", "DEAD_LETTER", agent="o", run_id="r",
                     expect_version=t["version"],
                     payload={"reason": "max_attempts"})
    with pytest.raises(IllegalTransition, match="sop_ref"):
        s.transition("p1", "t1", "DEAD_LETTER", agent="o", run_id="r",
                     expect_version=t["version"],
                     payload={"reason": "max_attempts", "last_error": "boom"})
    t = s.transition("p1", "t1", "DEAD_LETTER", agent="o", run_id="r",
                     expect_version=t["version"],
                     payload={"reason": "max_attempts", "last_error": "boom",
                              "sop_ref": "docs/sop/dead-letter.md"})
    assert t["status"] == "DEAD_LETTER"


# ---------- §2.3 CANCELLED 级联 ----------

def test_cancel_cascade_cancels_unstarted_downstream(tmp_path):
    s = Store(tmp_path)
    _dag(s)
    out = s.cancel_cascade("p1", "t1", reason="upstream_gone", run_id="r")
    assert {o["task_id"] for o in out} == {"t1", "t2", "t3"}
    assert all(o["status"] == "CANCELLED" for o in out)
    # 审计留痕
    payloads = [__import__("json").loads(e["payload"]) for e in s.recent_events("p1")]
    assert any(p.get("cascaded_from") for p in payloads)


def test_cancel_cascade_keeps_started_downstream_alone(tmp_path):
    """级联不波及已在跑的下游。

    注：正常路径下依赖门禁让"父未终态而子已开跑"不可能出现
    （这正是P0-1的意义）。可达路径是父被SKIPPED（视同满足）→子开跑。
    此时再对父发取消，终态父不级联，RUNNING子保持不动。
    """
    s = Store(tmp_path)
    s.insert_task(_shard("a", "p1", depends_on=[]))
    s.insert_task(_shard("b", "p1", depends_on=["a"]))
    t = s.get_task("p1", "a")
    s.transition("p1", "a", "SKIPPED", agent="o", run_id="r",
                 expect_version=t["version"])
    _run(s, "b")
    assert s.get_task("p1", "b")["status"] == "RUNNING"
    out = s.cancel_cascade("p1", "a", reason="upstream_gone", run_id="r")
    assert out == []                              # a已终态，不级联
    assert s.get_task("p1", "b")["status"] == "RUNNING"   # 没被动
    assert s.get_task("p1", "b")["cancel_requested"] == 0


def test_cancel_cascade_cancels_target_in_any_state(tmp_path):
    """目标本身按跃迁表取消：RUNNING→CANCELLED 是合法边。"""
    s = Store(tmp_path)
    s.insert_task(_shard("a", "p1", depends_on=[]))
    _run(s, "a")
    out = s.cancel_cascade("p1", "a", reason="operator_stop", run_id="r")
    assert [o["task_id"] for o in out] == ["a"]
    assert s.get_task("p1", "a")["status"] == "CANCELLED"


def test_cancel_cascade_stops_at_branch_not_related(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("a", "p1", depends_on=[]))
    s.insert_task(_shard("b", "p1", depends_on=["a"]))
    s.insert_task(_shard("c", "p1", depends_on=[]))   # 与a无关的分支
    out = s.cancel_cascade("p1", "a", reason="x", run_id="r")
    assert {o["task_id"] for o in out} == {"a", "b"}
    assert s.get_task("p1", "c")["status"] == "PENDING"


def test_cancel_cascade_idempotent(tmp_path):
    s = Store(tmp_path)
    _dag(s)
    s.cancel_cascade("p1", "t1", reason="x", run_id="r")
    assert s.cancel_cascade("p1", "t1", reason="x", run_id="r") == []


# ---------- §3 SCHEMA_REJECT 记录 ----------

def test_record_schema_reject_writes_audit_row(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    s.record_schema_reject("p1", "t1", "dispatch", "validation error: objective")
    evs = s.recent_events("p1")
    import json as _j
    last = _j.loads(evs[0]["payload"])       # recent_events按seq倒序，最新在前
    assert last["event"] == "SCHEMA_REJECT"
    assert last["where"] == "dispatch"
    assert s.get_task("p1", "t1")["status"] == "PENDING"   # 不改状态
    assert s.replay("p1")["ok"]                          # 同状态行replay跳过


def test_alert_writes_alerts_file(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("t1", "p1"))
    t = s.get_task("p1", "t1")
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t1", "DONE", agent="o", run_id="r",
                     expect_version=t["version"])
    alerts = (tmp_path / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert '"priority": "P1"' in alerts
    assert "illegal_transition" in alerts
