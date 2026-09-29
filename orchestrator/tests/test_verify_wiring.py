"""Phase4：把异步校验池真正接进主循环（Phase3 §4.1 记的欠账）。

接线的意义：Phase3把池子做出来了但主循环没调，导致VERIFYING任务
没人校验只能干等。现在tick()里 promote→dispatch_verify_async 一气呵成。
"""
import sys
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import verify_orchestrator as vo
from core.orchestrator import Orchestrator
from core.store import Store
from tests.test_orchestrator import _add, _root
from tests.test_verifier import DATE, _art, _to_verifying


def _pool(store):
    return vo.VerifyPool(store, workers=2)


def test_tick_promotes_and_dispatches_verify(tmp_path):
    """一轮tick内：SUBMITTED→VERIFYING→丢池→跑完→DONE。"""
    root = _root(tmp_path)
    s = Store(root)
    _art(root, f"artifacts/{DATE}/o.csv")
    pool = _pool(s)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, verify_pool=pool)
    pid = f"daily_report_{DATE}"
    o.store.ensure_plan(pid)
    _add(o.store, "t1", plan_id=pid,
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/o.csv",
                      "rule_id": "r0"}])
    _to_verifying(o.store, pid, "t1")

    async def run():
        async with pool:
            stats = o.tick()
            return stats

    stats = anyio.run(run)
    assert stats["verify_dispatched"] == 1
    assert s.get_task(pid, "t1")["status"] == "DONE"


def test_same_task_not_dispatched_twice(tmp_path):
    """重复入池会让两个worker抢同一个任务，双写终态。"""
    root = _root(tmp_path)
    s = Store(root)
    _art(root, f"artifacts/{DATE}/o.csv")
    pool = _pool(s)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, verify_pool=pool)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "t1", plan_id=pid,
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/o.csv",
                      "rule_id": "r0"}])
    _to_verifying(s, pid, "t1")

    async def run():
        async with pool:
            first = o.dispatch_verify_async()
            second = o.dispatch_verify_async()      # 同一轮里再调一次
            return first, second

    first, second = anyio.run(run)
    assert len(first) == 1
    assert second == []                            # 已在池里，不重复


def test_verify_worker_failure_does_not_break_tick(tmp_path):
    """单个校验炸了不能带崩主循环——主循环停了全系统就停了。"""
    root = _root(tmp_path)
    s = Store(root)
    pool = _pool(s)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, verify_pool=pool)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "t1", plan_id=pid,
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/nope.csv",
                      "rule_id": "r0"}])
    _to_verifying(s, pid, "t1")

    async def run():
        async with pool:
            o.dispatch_verify_async()
            # 再派一个不存在的任务，模拟worker内部抛异常
            o._in_flight.add(("p1", "ghost"))
            pool._tg.start_soon(o._verify_worker, "p1", "ghost", "r")

    anyio.run(run)
    assert ("p1", "ghost") not in o._in_flight        # finally清干净了
    assert s.get_task(pid, "t1")["status"] == "RETRY"  # 正常的那个仍走到了RETRY


def test_no_pool_is_noop(tmp_path):
    """不起池子时不该报错，只是没异步校验（排障模式）。"""
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None)
    assert o.verify_pool is None
    assert o.dispatch_verify_async() == []


def test_live_lease_task_not_requeued(tmp_path):
    """有租约=worker还活着，抢过来等于自己和自己打架。"""
    from datetime import datetime, timedelta, timezone
    root = _root(tmp_path)
    s = Store(root)
    pool = _pool(s)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, verify_pool=pool)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "busy", plan_id=pid,
         acceptance=[{"type": "file_exists", "path": "x", "rule_id": "r0"}])
    _to_verifying(s, pid, "busy")
    s.lease_update(pid, "busy", owner="v1",
                   lease_until=(datetime.now(timezone.utc)
                                + timedelta(seconds=60)).isoformat(),
                   bump_fencing=False)
    assert o.dispatch_verify_async() == []


def test_run_forever_with_pool_completes(tmp_path):
    """run_forever走async路径：退出时池子要收尾，不能挂住进程。"""
    root = _root(tmp_path)
    s = Store(root)
    pool = _pool(s)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, verify_pool=pool,
                     run_once=True)
    o.run_forever()
    assert o._lock_held is False
    assert pool.inflight == set()
