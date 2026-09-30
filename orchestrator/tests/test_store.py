"""Phase1 DoD: transition乐观锁/fencing/非法跃迁/让出边/幂等/FIFO/replay/backup。

依据：Phase1设计§2 + Phase4§2（store乐观锁+fencing+seq不重+让出熔断）。
隔离库：tmp_path，不碰生产库。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import (
    AlreadyExists,
    Conflict,
    IllegalTransition,
    StaleOwner,
    Store,
)


def _shard(task_id="t1", plan_id="p1", **over):
    d = {
        "task_id": task_id,
        "plan_id": plan_id,
        "status": "PENDING",
        "idempotency_key": f"{plan_id}/{task_id}/hash123",
        "inputs_hash": "hash123",
        "objective": "采集",
        "priority": 10,
        "source": "daily",
    }
    d.update(over)
    return d


def _ready_task(store, task_id="t1", plan_id="p1", **over):
    _, t = store.insert_task(_shard(task_id, plan_id, **over))
    return store.transition(plan_id, task_id, "READY", agent="t",
                            run_id="r", expect_version=t["version"])


def _ckpt(store, task_id="t1"):
    p = store.checkpoint_dir / f"{task_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"step": 1, "cursor": "c1"}), encoding="utf-8")


def test_transition_happy_path_bumps_version(tmp_path):
    s = Store(tmp_path)
    _, t = s.insert_task(_shard())
    assert t["status"] == "PENDING" and t["version"] == 0
    t2 = s.transition("p1", "t1", "READY", agent="o", run_id="r",
                      expect_version=0)
    assert t2["status"] == "READY" and t2["version"] == 1
    assert t2["fencing_token"] == 0  # 普通跃迁不加fencing（评审Minor2）


def test_illegal_transition_records_rejected(tmp_path):
    s = Store(tmp_path)
    t = _ready_task(s)
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t1", "DONE", agent="x", run_id="r",
                     expect_version=t["version"])
    evs = s.recent_events("p1")
    assert any(e["to_s"] == "REJECTED" for e in evs)


def test_conflict_on_version_mismatch(tmp_path):
    s = Store(tmp_path)
    _ready_task(s)
    with pytest.raises(Conflict):
        s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=999)


def test_stale_owner_on_fencing_mismatch(tmp_path):
    s = Store(tmp_path)
    t = _ready_task(s)
    t2 = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                      expect_version=t["version"], lease_op="acquire")
    assert t2["fencing_token"] == 1  # lease操作+1
    with pytest.raises(StaleOwner):
        s.transition("p1", "t1", "SUBMITTED", agent="old", run_id="r",
                     expect_version=t2["version"], expect_fencing=0)


def test_attempts_plus_one_only_on_retry_to_ready(tmp_path):
    s = Store(tmp_path)
    t = _ready_task(s)
    t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "FAILED", agent="s", run_id="r",
                     expect_version=t["version"], role="subagent")
    assert t["attempts"] == 0
    with pytest.raises(IllegalTransition, match="retry_requires_reason"):
        s.transition("p1", "t1", "RETRY", agent="v", run_id="r",
                     expect_version=t["version"])          # Phase0§2.3：RETRY必带reason
    t = s.transition("p1", "t1", "RETRY", agent="v", run_id="r",
                     expect_version=t["version"],
                     payload={"reason": "verify_failed"})
    assert t["attempts"] == 0
    t = s.transition("p1", "t1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    assert t["attempts"] == 1  # +1只在此边


def _to_running(s, tid="t1", pid="p1", **over):
    """跑到RUNNING并**持有租约**（lease_op=acquire使fencing≥1），
    因为Phase0§2.2：让出边必须回带fencing，只有租约持有者能写。"""
    t = _ready_task(s, tid, pid, **over)
    return s.transition(pid, tid, "RUNNING", agent="o", run_id="r",
                        expect_version=t["version"], lease_op="acquire")


def _mark_long_running(s, tid, pid):
    t = s.get_task(pid, tid)
    shard = dict(t["shard"])
    shard["long_running"] = True
    con = s._connect()
    try:
        con.execute("UPDATE tasks SET shard_json=? WHERE plan_id=? AND task_id=?",
                    (json.dumps(shard, ensure_ascii=False), pid, tid))
        con.commit()
    finally:
        con.close()


def test_yield_ok_with_reason_and_checkpoint(tmp_path):
    s = Store(tmp_path)
    t = _to_running(s)
    _mark_long_running(s, "t1", "p1")
    _ckpt(s)
    t2 = s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                      expect_version=t["version"],
                      expect_fencing=t["fencing_token"],
                      payload={"reason": "context_full"}, role="subagent")
    assert t2["status"] == "READY"
    assert t2["attempts"] == 0  # 让出不加attempts
    assert t2["session_switch_total"] == 1


def test_yield_rejects(tmp_path):
    s = Store(tmp_path)
    t = _to_running(s)
    f = t["fencing_token"]
    # 无checkpoint拒
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"], expect_fencing=f,
                     payload={"reason": "context_full"}, role="subagent")
    # 非法reason拒
    _ckpt(s)
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"], expect_fencing=f,
                     payload={"reason": "preempted_by_x"}, role="subagent")
    # 短任务拒（long_running=false）
    with pytest.raises(IllegalTransition):
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"], expect_fencing=f,
                     payload={"reason": "timeout"}, role="subagent")


def test_yield_requires_matching_fencing(tmp_path):
    """Phase0§2.2：让出必须回带fencing，且与当前一致。
    脑裂下旧owner拿着过期fencing让出，会把新owner正在跑的任务打回READY。"""
    s = Store(tmp_path)
    t = _to_running(s)
    _mark_long_running(s, "t1", "p1")
    _ckpt(s)
    with pytest.raises(IllegalTransition, match="yield_requires_fencing"):
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"],
                     payload={"reason": "context_full"}, role="subagent")
    with pytest.raises(StaleOwner):
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"],
                     expect_fencing=t["fencing_token"] - 1,
                     payload={"reason": "context_full"}, role="subagent")


def test_yield_fused_after_limit(tmp_path):
    s = Store(tmp_path, session_switch_limit=1)
    t = _to_running(s)
    _mark_long_running(s, "t1", "p1")
    _ckpt(s)
    f = t["fencing_token"]
    t = s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"], expect_fencing=f,
                     payload={"reason": "context_full"}, role="subagent")
    assert t["session_switch_total"] == 1
    t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    _ckpt(s)
    with pytest.raises(IllegalTransition):  # 超限拒，改走FAILED
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"],
                     expect_fencing=t["fencing_token"],
                     payload={"reason": "context_full"}, role="subagent")


def test_insert_idempotent_on_key_conflict(tmp_path):
    s = Store(tmp_path)
    st, t1 = s.insert_task(_shard())
    assert st == "created"
    st2, t2 = s.insert_task(_shard())
    assert st2 == "already_exists"
    assert t2["seq"] == t1["seq"]  # 未重复入队


def test_fifo_list_ready_ordered(tmp_path):
    s = Store(tmp_path)
    s.insert_task(_shard("low1", priority=10))
    s.insert_task(_shard("high", priority=0))
    s.insert_task(_shard("low2", priority=10))
    for tid in ("low1", "high", "low2"):
        t = s.get_task("p1", tid)
        s.transition("p1", tid, "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    got = [t["task_id"] for t in s.list_ready_ordered(limit=10)]
    assert got == ["high", "low1", "low2"]


def test_verify_progress_tracks_passed_rules(tmp_path):
    """V7§3：VERIFYING局部重入的进度账本——只重跑未通过的rule。"""
    s = Store(tmp_path)
    t = _ready_task(s)
    t = s.transition("p1", "t1", "RUNNING", agent="a", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    t = s.transition("p1", "t1", "SUBMITTED", agent="a", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "VERIFYING", agent="orch", run_id="r",
                     expect_version=t["version"])
    assert t["verify_progress"] == {}
    got = s.update_verify_progress("p1", "t1", ["rule_0", "rule_2", "rule_0"],
                                   expect_version=t["version"])
    assert got["verify_progress"]["verified_rules"] == ["rule_0", "rule_2"]
    assert got["version"] == t["version"] + 1
    assert got["status"] == "VERIFYING"      # 进度写不改状态
    with pytest.raises(Conflict):
        s.update_verify_progress("p1", "t1", ["rule_1"], expect_version=t["version"])


def test_replay_and_rebuild_and_backup(tmp_path):
    s = Store(tmp_path)
    s.ensure_plan("p1", template="daily")
    _, t = s.insert_task(_shard())
    s.transition("p1", "t1", "READY", agent="o", run_id="r",
                 expect_version=t["version"])
    rep = s.replay("p1")
    assert rep["ok"], rep["mismatches"]
    assert s.rebuild_export("p1") == 1
    assert (s.export_dir / "tasks" / "t1.json").exists()
    # 每plan一个文件；不设master.json（V1遗留，V7已不提）
    plan_file = s.export_dir / "plans" / "p1.json"
    assert plan_file.exists()
    import json as _j
    body = _j.loads(plan_file.read_text(encoding="utf-8"))
    assert body["plan_id"] == "p1" and len(body["tasks"]) == 1
    assert not (s.export_dir / "master.json").exists()
    b = s.backup()
    assert b.exists() and b.stat().st_size > 0
    from core.store import SCHEMA_VERSION
    assert s.migrate() == SCHEMA_VERSION  # 幂等
