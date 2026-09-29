"""Phase2 欠账三条的测试：心跳线程 / 死信升级 / 指数退避。

这三条在 Phase1 复查时被明确划到Phase2（"没做"而非"漏做"），此处兑现。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import lease
from core.lease import Heartbeat, backoff_delay_s, escalate_dead_letters
from core.store import Store
from tests.test_store import _shard


def _running(store: Store, tid: str = "t1", plan_id: str = "p1") -> dict:
    _, t = store.insert_task(_shard(tid, plan_id))
    t = store.transition(plan_id, tid, "READY", agent="o", run_id="r",
                         expect_version=t["version"])
    return store.transition(plan_id, tid, "RUNNING", agent="o", run_id="r",
                            expect_version=t["version"], lease_op="acquire")


def _wait_until(fn, timeout=6.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


# ---------- §3 心跳线程 ----------

def test_heartbeat_keeps_lease_alive(tmp_path):
    """心跳必须真的续租，否则ttl=120一过就被主循环当失联回收。"""
    s = Store(tmp_path)
    _running(s)
    held = lease.acquire(s, "p1", "t1", owner="sub-1", ttl=90)   # ttl与心跳一致
    hb = Heartbeat(s, "p1", "t1", owner="sub-1", interval_s=0.05, ttl_s=90)
    hb.start()
    try:
        assert _wait_until(lambda: hb.beats >= 2), hb.last_error
    finally:
        hb.stop()
    assert hb.lost is False
    cur = s.get_task("p1", "t1")
    assert cur["lease_until"] > held["lease_until"]   # 租约被推后
    assert cur["fencing_token"] > held["fencing_token"]


def test_heartbeat_stops_when_lease_stolen(tmp_path):
    """脑裂：worker-2抢走租约后，worker-1的心跳必须停并置lost，
    不能继续对一个不属于自己的任务续租。"""
    s = Store(tmp_path)
    _running(s)
    lease.acquire(s, "p1", "t1", owner="sub-1", ttl=120)
    hb = Heartbeat(s, "p1", "t1", owner="sub-1", interval_s=0.05, ttl_s=90)
    hb.start()
    try:
        assert _wait_until(lambda: hb.beats >= 1)
        # 模拟worker-2抢租：把租约置过期后acquire
        from datetime import datetime, timedelta, timezone
        s.lease_update("p1", "t1", owner="sub-1",
                       lease_until=(datetime.now(timezone.utc)
                                    - timedelta(seconds=1)).isoformat(),
                       bump_fencing=False)
        lease.acquire(s, "p1", "t1", owner="sub-2", ttl=120)
        assert _wait_until(lambda: hb.lost), "心跳应置lost并停止"
    finally:
        hb.stop()
    assert hb.beats == 1   # 丢了之后没有再续
    alerts = (tmp_path / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert "heartbeat_lost" in alerts


def test_heartbeat_is_daemon_and_stoppable(tmp_path):
    """线程必须daemon且能join：执行器finally要停它，进程退出不能被挂住。"""
    s = Store(tmp_path)
    _running(s)
    lease.acquire(s, "p1", "t1", owner="sub-1", ttl=120)
    hb = Heartbeat(s, "p1", "t1", owner="sub-1", interval_s=60)
    hb.start()
    assert hb._thread.daemon is True
    hb.stop(timeout=2)
    assert hb._thread is None


def test_heartbeat_context_manager_stops(tmp_path):
    s = Store(tmp_path)
    _running(s)
    lease.acquire(s, "p1", "t1", owner="sub-1", ttl=120)
    with Heartbeat(s, "p1", "t1", owner="sub-1", interval_s=60) as hb:
        assert hb._thread is not None
    assert hb._thread is None


# ---------- §3 死信升级 ----------

def _failed_with_attempts(store: Store, attempts: int, max_attempts: int = 3,
                          tid: str = "t1", plan_id: str = "p1") -> dict:
    """造一个FAILED任务，attempts=指定值（通过RETRY→READY累加）。"""
    t = _running(store, tid, plan_id)
    t = store.transition(plan_id, tid, "FAILED", agent="sub", run_id="r",
                         expect_version=t["version"], role="subagent",
                         payload={"error": "boom-attempt"})
    for _ in range(attempts):
        t = store.transition(plan_id, tid, "RETRY", agent="o", run_id="r",
                             expect_version=t["version"],
                             payload={"reason": "retry_backoff"})
        t = store.transition(plan_id, tid, "READY", agent="o", run_id="r",
                             expect_version=t["version"])
        t = store.transition(plan_id, tid, "RUNNING", agent="o", run_id="r",
                             expect_version=t["version"], lease_op="acquire")
        t = store.transition(plan_id, tid, "FAILED", agent="sub", run_id="r",
                             expect_version=t["version"], role="subagent",
                             payload={"error": "boom-attempt"})
    return t


def test_dead_letter_not_before_max_attempts(tmp_path):
    s = Store(tmp_path)
    _failed_with_attempts(s, attempts=1, max_attempts=3)
    assert escalate_dead_letters(s) == []
    assert s.get_task("p1", "t1")["status"] == "FAILED"


def test_dead_letter_at_max_attempts_with_p0(tmp_path):
    s = Store(tmp_path)
    t = _failed_with_attempts(s, attempts=3, max_attempts=3)
    out = escalate_dead_letters(s, run_id="r")
    assert [o["task_id"] for o in out] == ["t1"]
    got = s.get_task("p1", "t1")
    assert got["status"] == "DEAD_LETTER"
    import json as _j
    payload = None
    for e in s.recent_events("p1"):
        if e["to_s"] == "DEAD_LETTER":
            payload = _j.loads(e["payload"])
    assert payload["reason"] == "max_attempts_exceeded"
    assert payload["last_error"]              # 从历史事件里翻出来的
    assert payload["sop_ref"]                 # 死信必带SOP链接（Phase0§2.3）
    alerts = (tmp_path / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert '"priority": "P0"' in alerts and "dead_letter" in alerts


def test_dead_letter_is_idempotent(tmp_path):
    s = Store(tmp_path)
    _failed_with_attempts(s, attempts=3, max_attempts=3)
    assert len(escalate_dead_letters(s)) == 1
    assert escalate_dead_letters(s) == []      # 已是死信，不再重复告警


def test_dead_letter_respects_conflict(tmp_path, monkeypatch):
    """读到列表之后、真正写之前版本被改（并发）：本轮跳过，不硬盖。
    用打桩把list_by_status换成返回过期版本，真实触发Conflict分支。"""
    s = Store(tmp_path)
    _failed_with_attempts(s, attempts=3, max_attempts=3)
    real = s.list_by_status

    def stale_list(status):
        rows = real(status)
        for r in rows:
            r["version"] = r["version"] - 1      # 假装读到的是旧版本
        return rows

    monkeypatch.setattr(s, "list_by_status", stale_list)
    assert escalate_dead_letters(s) == []      # 版本对不上，跳过下轮
    assert s.get_task("p1", "t1")["status"] == "FAILED"   # 没被误升级


# ---------- §2.1 指数退避 ----------

def test_backoff_doubles():
    assert [backoff_delay_s(n) for n in range(5)] == [60, 120, 240, 480, 960]


def test_backoff_is_capped():
    """不封顶的话attempts=12就是6.8小时，一条重试链能堵死队列。"""
    assert backoff_delay_s(12) == 3600
    assert backoff_delay_s(50) == 3600


def test_backoff_clamps_negative():
    assert backoff_delay_s(-3) == 60
