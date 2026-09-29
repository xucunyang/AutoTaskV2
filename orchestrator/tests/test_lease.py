import json
from datetime import datetime, timedelta, timezone

import pytest

from core import lease
from core.store import Conflict, Store, StaleOwner
from tests.test_store import _shard


def _running(store: Store, tid: str = "t1", plan_id: str = "p1") -> dict:
    _, t = store.insert_task(_shard(tid, plan_id))
    t = store.transition(plan_id, tid, "READY", agent="a", run_id="r",
                         expect_version=t["version"])
    return store.transition(plan_id, tid, "RUNNING", agent="a", run_id="r",
                            expect_version=t["version"], lease_op="acquire")


def test_acquire_sets_owner_and_bumps_fencing(tmp_path):
    s = Store(tmp_path)
    t = _running(s)
    got = lease.acquire(s, "p1", "t1", owner="worker-1", ttl=60, run_id="r")
    assert got["owner"] == "worker-1"
    assert got["fencing_token"] == t["fencing_token"] + 1
    assert got["version"] == t["version"] + 1
    lu = datetime.fromisoformat(got["lease_until"])
    assert lu > datetime.now(timezone.utc)


def test_acquire_rejected_while_lease_valid(tmp_path):
    s = Store(tmp_path)
    _running(s)
    lease.acquire(s, "p1", "t1", owner="worker-1", ttl=120)
    with pytest.raises(Conflict):
        lease.acquire(s, "p1", "t1", owner="worker-2", ttl=120)


def test_renew_by_old_owner_is_stale(tmp_path):
    """核心脑裂防护：worker-1持有租约，worker-2抢走后worker-1心跳必须失败。"""
    s = Store(tmp_path)
    _running(s)
    t1 = lease.acquire(s, "p1", "t1", owner="worker-1", ttl=120)
    # 把worker-1的租约置为过期，使worker-2可以合法抢租
    s.lease_update("p1", "t1", owner="worker-1",
                   lease_until=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                   bump_fencing=False, expect_fencing=t1["fencing_token"])
    t2 = lease.acquire(s, "p1", "t1", owner="worker-2", ttl=120)
    assert t2["fencing_token"] > t1["fencing_token"]
    with pytest.raises(StaleOwner):
        lease.renew(s, "p1", "t1", owner="worker-1")
    got = lease.renew(s, "p1", "t1", owner="worker-2")
    assert got["fencing_token"] == t2["fencing_token"] + 1


def test_release_requires_matching_owner(tmp_path):
    s = Store(tmp_path)
    _running(s)
    t = lease.acquire(s, "p1", "t1", owner="worker-1", ttl=120)
    with pytest.raises(StaleOwner):
        lease.release(s, "p1", "t1", owner="worker-2")
    got = lease.release(s, "p1", "t1", owner="worker-1")
    assert got["owner"] is None and got["lease_until"] is None
    assert got["fencing_token"] == t["fencing_token"]  # release不+1（评审Minor2）
    assert got["version"] == t["version"] + 1


def test_recover_expired_running_to_failed(tmp_path):
    s = Store(tmp_path)
    t = _running(s)
    s.lease_update("p1", "t1", owner="worker-1",
                   lease_until=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                   bump_fencing=False)
    rec = lease.recover_expired_leases(s, run_id="r")
    assert [r["task_id"] for r in rec] == ["t1"]
    assert rec[0]["status"] == "FAILED"
    evs = [e for e in s.recent_events("p1") if e["to_s"] == "FAILED"]
    assert evs and json.loads(evs[0]["payload"])["reason"] == "lease_expired"
    # 回收不碰attempts（attempts只在RETRY→READY时+1）
    assert rec[0]["attempts"] == t["attempts"]


def test_recover_skips_valid_lease(tmp_path):
    s = Store(tmp_path)
    _running(s)
    lease.acquire(s, "p1", "t1", owner="worker-1", ttl=120)
    assert lease.recover_expired_leases(s) == []


def test_request_cancel_sets_flag_and_audit_row(tmp_path):
    s = Store(tmp_path)
    t = _running(s)
    got = lease.request_cancel(s, "p1", "t1", reason="user_stop",
                               expect_version=t["version"])
    assert got["cancel_requested"] == 1
    assert got["shard"]["cancel_requested"] is True
    assert got["status"] == "RUNNING"  # 不直接改状态
    assert got["version"] == t["version"] + 1
    payloads = [json.loads(e["payload"]) for e in s.recent_events("p1")]
    assert any(p.get("event") == "CANCEL_REQUESTED" for p in payloads)
    with pytest.raises(Conflict):
        lease.request_cancel(s, "p1", "t1", reason="again",
                             expect_version=t["version"])


def test_time_params_from_config(tmp_path):
    """测试用tmp_path无config/回退默认值；生产root读schedule.yaml。"""
    s = Store(tmp_path)
    assert s.time_params["lease_ttl_s"] == 120
    assert s.time_params["heartbeat_s"] == 30
    assert s.time_params["session_switch_limit"] == 5
    prod = Store(__file__.rsplit("orchestrator", 1)[0] + "orchestrator")
    assert prod.time_params["lease_ttl_s"] == 120
    assert prod.session_switch_limit == 5
