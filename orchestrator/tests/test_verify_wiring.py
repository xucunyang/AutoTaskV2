"""Phase4：把异步校验池真正接进主循环（Phase3 §4.1 记的欠账）。

接线的意义：Phase3把池子做出来了但主循环没调，导致VERIFYING任务
没人校验只能干等。现在tick()里 promote→dispatch_verify_async 一气呵成。
"""
import sys
import threading
import time
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


def test_wait_wakeup_async_yields_to_pool_children(tmp_path):
    """异步等待必须真让出控制权，否则任务组里的协程一个都起不来。

    同步 time.sleep 会把 asyncio 循环整段饿死——这类 bug 在单测里最阴：
    测试代码自己在 await，所以照过不误，只有生产主循环（全程不 await）
    才暴露。见 test_drive_loop_verifies_while_still_running。
    """
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None)
    ran = []

    async def run():
        async with anyio.create_task_group() as tg:
            async def child():
                ran.append("child")
            tg.start_soon(child)
            await o.wait_wakeup_async(0.05)
            return list(ran)

    assert anyio.run(run) == ["child"]


def test_drive_loop_verifies_while_still_running(tmp_path):
    """主循环必须在**运行中**把 VERIFYING 校验掉，而不是等退出时补票。

    2026-10-03 生产实测：tick()/wait_wakeup() 全是同步 sleep，事件循环整段
    被饿死，verify worker 只在进程退出、任务组 __aexit__ 收尾时才跑——表现
    是任务永远卡 VERIFYING，metric 一条没记、日志一条没有，且 kill 掉进程
    连收尾机会都没有（=永远卡死）。
    """
    root = _root(tmp_path)
    s = Store(root)
    _art(root, f"artifacts/{DATE}/o.csv")
    pool = _pool(s)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, verify_pool=pool)
    o.poll_normal_s = 0.1
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "t1", plan_id=pid,
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/o.csv",
                      "rule_id": "r0"}])
    _to_verifying(s, pid, "t1")

    marks: dict = {}
    # 每轮 tick 结束时，上一轮丢出去的 worker 是否已经跑完并回收。
    # 旧实现（主循环全程不 await）里 worker 只在退出收尾时才跑，所以
    # 每一轮 tick 看到的 _in_flight 都还挂着——这个判据不依赖毫秒级时序。
    probe: list[bool] = []
    orig_tick = o.tick

    def tick_probe():
        stats = orig_tick()
        probe.append(not o._in_flight)
        return stats

    o.tick = tick_probe

    def drive():
        o.run_forever(max_ticks=3)
        marks["loop_end"] = time.monotonic()

    th = threading.Thread(target=drive, daemon=True)
    th.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and s.get_task(pid, "t1")["status"] != "DONE":
        time.sleep(0.02)
    th.join(timeout=10)

    assert s.get_task(pid, "t1")["status"] == "DONE"
    assert "loop_end" in marks, "主循环没跑完"
    assert len(probe) >= 2, "主循环没跑够轮次"
    assert any(probe), (
        "verify worker 在主循环运行期间一次都没跑起来（_in_flight 每轮都还挂着），"
        "等于校验只在进程退出收尾时补跑——生产上就是任务永远卡 VERIFYING")
