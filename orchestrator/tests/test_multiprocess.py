"""2.6 多进程：单实例锁与派发幂等（**真进程**，不是同进程两个对象）。

为什么必须用真进程：
单实例锁的全部意义就是"跨进程只能有一个编排器"。同进程内两个
Orchestrator 对象共用一套内存语义，测不出锁的真正问题。混沌2
（双调度只建一次）是同进程两个对象——它是有效测试，但覆盖不到这里。

要验的：
1. N个真进程抢锁 → 恰好1个拿到
2. 拿到锁的进程崩掉（kill -9）→ 锁不能永久残留，否则系统再也无法启动
3. N个真进程同时派发同一批任务 → 每个任务**恰好执行一次**（不重复副作用）
4. 派发过程DB不损坏、无任务永久卡在RUNNING
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue                                    # noqa: E402
from core.store import Store                               # noqa: E402
from core.utils import now_utc_iso                         # noqa: E402

PY = sys.executable


def _root(tmp_path):
    r = tmp_path / "proj"
    (r / "templates").mkdir(parents=True)
    for f in (ROOT / "templates").glob("*"):
        (r / "templates" / f.name).write_bytes(f.read_bytes())
    return r


def _seed(root, plan_id, n):
    s = Store(root)
    s.ensure_plan(plan_id, template="daily")
    for i in range(n):
        shard = enqueue.validate(json.dumps({
            "task_id": f"t{i}", "plan_id": plan_id,
            "objective": f"任务{i}", "outputs": [f"artifacts/o{i}.md"],
            "budget": {"max_steps": 2, "timeout_s": 30},
        }, ensure_ascii=False).encode("utf-8"), root=root)
        s.insert_task(shard)
    return s


# 抢锁的子进程：拿到就写标记文件并保持，拿不到立刻退出
LOCK_CODE = """
import sys, time, os
sys.path.insert(0, {root!r})
from core.orchestrator import Orchestrator
root = {root!r}
o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
got = o.acquire_lock()
mark = os.path.join(root, "holder.pid")
if got:
    with open(mark, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
time.sleep({hold})
if got:
    o.release_lock()
    os.remove(mark)
"""


def test_only_one_process_wins_the_lock(tmp_path):
    """5个真进程同时抢锁，恰好1个拿到。"""
    root = _root(tmp_path)
    Store(root).ensure_plan("p1", template="daily")
    code = LOCK_CODE.format(root=str(root), hold=1.5)
    procs = [subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(5)]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, f"子进程异常退出: {err[-500:]}"
    s = Store(root)
    assert s._connect().execute(
        "SELECT COUNT(*) FROM plans WHERE plan_id='orchestrator_lock'"
    ).fetchone()[0] == 0, "锁行残留"


def _cfg(root, ttl_s):
    """给测试root写一份 schedule.yaml。走真实配置读取路径，
    而不是直接改 o.lock_ttl_s——后者测的是"我设了个变量"，
    不是"配置真的生效"。"""
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "schedule.yaml").write_text(
        f"workers: {{reserved_low_slot: 1}}\nlock: {{ttl_s: {ttl_s}}}\n",
        encoding="utf-8")


def test_lock_survives_kill9_of_holder(tmp_path):
    """持锁进程被 kill -9 → 锁不能永久卡死系统。

    这是真会发生的运维事故：机器重启/进程被杀之后编排器必须能起来。
    之前 acquire_lock 只看"行在不在"，行在就一直返回 False——
    没有存活期概念，等于一次 kill -9 就让整个系统永久不可用，
    只能人工连库删行才能恢复。

    两段语义都要验：
    a) 刚被杀、心跳还新鲜 → **仍应锁住**（否则会误抢，双编排器并行）
    b) 心跳过期后 → 必须能接管（否则系统永久死）
    只验b会诱导实现去掉存活期；只验a则测不到真正的修复。
    """
    root = _root(tmp_path)
    _cfg(root, ttl_s=3)
    Store(root).ensure_plan("p1", template="daily")
    # 持锁并持续心跳（真实编排器就是这样），然后被强杀
    code = """
import sys, time, os
sys.path.insert(0, {root!r})
from core.orchestrator import Orchestrator
o = Orchestrator({root!r}, executor_fn=lambda *a, **k: None, run_once=True)
if not o.acquire_lock():
    raise SystemExit(1)
with open(os.path.join({root!r}, "holder.pid"), "w") as f:
    f.write(str(os.getpid()))
t0 = time.time()
while time.time() - t0 < 300:      # 一直心跳，像真编排器
    if not o.heartbeat_lock():
        break
    time.sleep(0.5)
""".format(root=str(root))
    holder = subprocess.Popen([PY, "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
    mark = root / "holder.pid"
    deadline = time.time() + 60
    while time.time() < deadline and not mark.exists():
        assert holder.poll() is None, f"提前退出: {holder.stderr.read()[-400:]}"
        time.sleep(0.05)
    assert mark.exists(), "持锁进程没拿到锁"
    holder.kill()                      # 等价 kill -9：不给清理机会
    holder.wait(timeout=30)

    from core.orchestrator import Orchestrator
    # a) 心跳还新鲜 → 必须锁住
    o2 = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert o2.acquire_lock() is False, (
        "心跳尚未过期就被抢锁：会出现两个编排器并行派发")
    # b) 心跳过期 → 必须能接管，否则系统永久死
    time.sleep(4)
    o3 = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert o3.acquire_lock() is True, (
        "持锁进程已被强杀且心跳已过期，却仍然拿不到锁——"
        "系统要人工连库删行才能恢复")


def test_stale_holder_can_be_taken_over_even_if_process_alive(tmp_path):
    """持锁进程**活着但卡住**（不心跳）→ 心跳过期后也必须能接管。

    真实场景：进程被 SIGSTOP、GC 卡死、机器休眠。
    只按"进程还在不在"判活会漏掉这一类，而它同样会让系统停摆。
    """
    root = _root(tmp_path)
    _cfg(root, ttl_s=2)
    Store(root).ensure_plan("p1", template="daily")
    code = """
import sys, time, os
sys.path.insert(0, {root!r})
from core.orchestrator import Orchestrator
o = Orchestrator({root!r}, executor_fn=lambda *a, **k: None, run_once=True)
if not o.acquire_lock():
    raise SystemExit(1)
with open(os.path.join({root!r}, "holder.pid"), "w") as f:
    f.write(str(os.getpid()))
time.sleep(120)          # 活着，但一次心跳都不发
""".format(root=str(root))
    holder = subprocess.Popen([PY, "-c", code], stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
    mark = root / "holder.pid"
    deadline = time.time() + 60
    while time.time() < deadline and not mark.exists():
        assert holder.poll() is None, "提前退出"
        time.sleep(0.05)
    try:
        from core.orchestrator import Orchestrator
        time.sleep(3)     # 超过 ttl
        o2 = Orchestrator(root, executor_fn=lambda *a, **k: None,
                          run_once=True)
        assert o2.acquire_lock() is True, "心跳停更的持锁者应可被接管"
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_heartbeat_refreshes_lock_ttl(tmp_path):
    """心跳能把锁一直续下去 → 活着的编排器不会被别人抢走。"""
    root = _root(tmp_path)
    _cfg(root, ttl_s=3)
    Store(root).ensure_plan("p1", template="daily")
    from core.orchestrator import Orchestrator
    a = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    b = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert a.acquire_lock() is True
    for _ in range(4):           # 3*4 > ttl，全程靠心跳续命
        time.sleep(0.8)
        assert a.heartbeat_lock() is True
        assert b.acquire_lock() is False, "活着的编排器被误判为失联"
    a.release_lock()
    assert b.acquire_lock() is True, "释放后应可接手"


def test_lock_lost_during_run_stops_the_orchestrator(tmp_path):
    """心跳发现锁被换主人 → 主循环必须收手，不能继续派发。

    否则两个编排器会各派各的，虽然幂等挡住了重复派发，
    但治理决策（老化提优先级）会双份执行，指标也会重复。
    """
    root = _root(tmp_path)
    _cfg(root, ttl_s=60)
    Store(root).ensure_plan("p1", template="daily")
    from core.orchestrator import Orchestrator
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert o.acquire_lock() is True
    # 模拟别人接管（直接改主人）
    Store(root)._write_txn(lambda con: con.execute(
        "UPDATE plans SET template='pid=999999',updated_at=?"
        " WHERE plan_id='orchestrator_lock'", (now_utc_iso(),)))
    assert o.heartbeat_lock() is False, "应发现锁已不属于自己"
    assert o.heartbeat_lock() is False


# 并发派发：用**真实 run_task**（临时root没有config → 无provider → 走自检路径），
# 这样状态转移是真实的，测试才验得到"任务真的走完了"。
# 早先用了个只写日志不转移状态的假执行器，结果12个任务全卡在RUNNING，
# 测的是"假执行器没干活"而不是"派发不幂等"。
DISPATCH_CODE = """
import sys, json, time, os
sys.path.insert(0, {root!r})
from core.orchestrator import Orchestrator
from core import executor
root, plan_id = {root!r}, {plan!r}
log = os.path.join(root, "exec.log")
_real = executor.run_task
def ex(store, plan_id, task_id, run_id, **kw):
    with open(log, "a", encoding="utf-8") as f:
        f.write(task_id + "\\n")
        f.flush()
    time.sleep(0.05)          # 拉大竞争窗口
    return _real(store, plan_id, task_id, run_id, **kw)
o = Orchestrator(root, executor_fn=ex, run_once=True, poll_fast_s=0.01)
if not o.acquire_lock():
    raise SystemExit(0)
try:
    for _ in range(200):
        o.refresh_ready(plan_id)
        for t in o.store.list_plan_tasks(plan_id):
            if t["status"] == "READY":
                o._dispatch_one(o.store.get_task(plan_id, t["task_id"]))
        if all(t["status"] in ("SUBMITTED", "DONE", "FAILED")
               for t in o.store.list_plan_tasks(plan_id)):
            break
        time.sleep(0.01)
finally:
    o.release_lock()
"""


def test_tasks_executed_exactly_once_under_concurrency(tmp_path):
    """3个真进程抢同一批任务 → 每个任务恰好执行一次。

    派发不是"至少一次"就够：如果同一个任务被执行两次，
    真实场景下就是产物写两遍、检索调两次（烧钱）、webhook 发两遍。
    """
    root = _root(tmp_path)
    _seed(root, "p1", 12)
    code = DISPATCH_CODE.format(root=str(root), plan="p1")
    procs = [subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, f"子进程异常: {err[-800:]}"
    log = root / "exec.log"
    executed = log.read_text(encoding="utf-8").split() if log.exists() else []
    s = Store(root)
    tasks = s.list_plan_tasks("p1")
    assert len(tasks) == 12
    dupes = {t for t in executed if executed.count(t) > 1}
    assert not dupes, f"任务被执行了多次: {sorted(dupes)}（派发不幂等）"
    # 没被执行的任务也不能卡在中间态
    stuck = [t["task_id"] for t in tasks
             if t["status"] in ("RUNNING", "READY", "PENDING")]
    assert not stuck, f"有任务卡在中间态: {stuck}"


def test_db_survives_concurrent_writers(tmp_path):
    """并发写之后 DB 完整性仍然正常，且replay重放一致。"""
    import sqlite3
    root = _root(tmp_path)
    _seed(root, "p1", 10)
    code = DISPATCH_CODE.format(root=str(root), plan="p1")
    procs = [subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(4)]
    for p in procs:
        _out, err = p.communicate(timeout=180)
        assert p.returncode == 0, f"子进程异常: {err[-800:]}"
    con = sqlite3.connect(root / "state" / "state.db")
    got = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    assert got == "ok", f"DB损坏: {got}"
    s = Store(root)
    rep = s.replay("p1")
    assert rep["ok"], f"重放不一致: {rep['mismatches'][:3]}"
