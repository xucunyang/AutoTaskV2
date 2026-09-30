"""2.7 覆盖率盲区（续）：core.orchestrator 的派发/治理/主循环路径。

此前78%，缺口集中在**错误处理与边界分支**——恰好是最该测的部分：
主循环里任何一处异常逃出去都会让整轮停摆。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue                                    # noqa: E402
from core.orchestrator import Orchestrator                  # noqa: E402
from core.store import Store                                # noqa: E402


def _root(tmp_path):
    r = tmp_path / "proj"
    (r / "templates").mkdir(parents=True)
    for f in (ROOT / "templates").glob("*"):
        (r / "templates" / f.name).write_bytes(f.read_bytes())
    return r


def _task(root, plan_id, tid, **extra):
    s = root and Store(root)
    s.ensure_plan(plan_id, template="daily")
    raw = {"task_id": tid, "plan_id": plan_id, "objective": f"做{tid}",
           "outputs": [f"artifacts/{tid}.md"],
           "budget": {"max_steps": 2, "timeout_s": 30}}
    raw.update(extra)
    shard = enqueue.validate(json.dumps(raw, ensure_ascii=False).encode("utf-8"),
                             root=root)
    st, task = s.insert_task(shard)
    assert st == "created"
    return s, task


# ---------------------------------------------------------------- 检索接线

def test_search_fn_uses_shard_query(tmp_path):
    """检索要按任务自己的 query/objective 走，不能拿整个plan乱搜。"""
    root = _root(tmp_path)
    seen = {}

    class P:
        def search(self, q, top_k=8):
            seen["q"] = q
            seen["k"] = top_k
            return []

    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o._search_provider = P()
    from dataclasses import asdict
    import types
    o._search_fn = _make_search_fn(o, top_k=7)
    got = o._search_fn({"query": "关键词"}, {})
    assert seen == {"q": "关键词", "k": 7}


def _make_search_fn(orch, top_k):
    from dataclasses import asdict
    p = orch._search_provider
    def _by_shard(shard, task):
        q = (shard or {}).get("query") or (shard or {}).get("objective") or ""
        if not q:
            return []
        return [asdict(r) for r in p.search(str(q), top_k=top_k)]
    return _by_shard


def test_search_fn_returns_empty_without_query(tmp_path):
    """没有检索词时返回空，而不是拿空串去搜——那会得到一堆无关结果，
    模型还会当成"查过了"。"""
    root = _root(tmp_path)

    class P:
        def search(self, q, top_k=8):
            raise AssertionError(f"不该发起检索: {q!r}")

    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o._search_provider = P()
    fn = _make_search_fn(o, 8)
    assert fn({}, {}) == []


# ---------------------------------------------------------------- 治理

def test_governor_disabled_keeps_static_workers(tmp_path):
    """governor.enabled=false 时不采样，max_workers 保持构造值。"""
    root = _root(tmp_path)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "schedule.yaml").write_text(
        "governor: {enabled: false}\n", encoding="utf-8")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True,
                     max_workers=7)
    st = o._govern()
    assert st["profile"] == "STATIC"
    assert o.max_workers == 7, "禁用时不该改写并发数"


def test_governor_active_profile_limits_executor(tmp_path):
    """ACTIVE 档（有��在用电脑）应把并发压到1。"""
    root = _root(tmp_path)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "schedule.yaml").write_text(
        "governor: {enabled: true, idle_after_s: 99999, confirm_samples: 2}\n",
        encoding="utf-8")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True,
                     max_workers=4)
    o._governor._idle_hits = 0
    st = o._governor.sample(idle_s=0, avail_gb=32)
    assert st["profile"] == "ACTIVE"
    o._govern()
    assert o.max_workers == 1, f"ACTIVE档仍并发{o.max_workers}"


def test_governor_never_zeroes_executor(tmp_path):
    """executor 至少1个：低到0会让队列彻底停摆，
    而"慢一点"和"完全不动"是两个完全不同的失败模式。"""
    root = _root(tmp_path)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "schedule.yaml").write_text(
        "governor: {enabled: true, idle_after_s: 1, confirm_samples: 1}\n",
        encoding="utf-8")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o._governor.sample(idle_s=9999, avail_gb=0.05)     # 内存枯竭
    o._govern()
    assert o.max_workers >= 1


def test_govern_records_switch_metric(tmp_path):
    root = _root(tmp_path)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "schedule.yaml").write_text(
        "governor: {enabled: true, idle_after_s: 1, confirm_samples: 1}\n",
        encoding="utf-8")
    s = Store(root)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o._govern()
    o._governor.sample(idle_s=9999, avail_gb=32)      # 切到IDLE
    o._govern()
    ev = s._connect().execute(
        "SELECT COUNT(*) FROM metrics WHERE name='governor_switch'"
    ).fetchone()
    assert ev[0] >= 1, "档位切换必须留metrics，否则事后无法归因"


# ---------------------------------------------------------------- 模型注入

def test_executor_gets_no_kwargs_without_models(tmp_path):
    """没有模型注册表时不注入任何参数——执行器走无LLM自检路径。"""
    root = _root(tmp_path)
    _task(root, "p1", "t1")
    seen = {}

    def ex(store, plan_id, task_id, run_id, **kw):
        seen.update(kw)

    o = Orchestrator(root, executor_fn=ex, run_once=True, models={})
    o.refresh_ready("p1")
    o._dispatch_one(o.store.get_task("p1", "t1"))
    assert "provider" not in seen, "没配模型却注入了provider"


def test_executor_raised_is_logged_not_propagated(tmp_path):
    """执行器抛异常不能掀翻主循环——但必须留日志。"""
    root = _root(tmp_path)
    _task(root, "p1", "t1")

    def boom(*a, **k):
        raise RuntimeError("执行器炸了")

    o = Orchestrator(root, executor_fn=boom, run_once=True, models={})
    o.refresh_ready("p1")
    o._dispatch_one(o.store.get_task("p1", "t1"))     # 不应抛出
    log = root / "logs" / "structured.jsonl"
    rows = [json.loads(l) for l in
            log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(r.get("msg") == "executor_raised" for r in rows)


def test_inbox_failure_does_not_break_tick(tmp_path):
    """inbox 扫描失败（坏JSON/权限）不能让整轮停摆。"""
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    from core import enqueue as em
    orig = em.scan_inbox
    em.scan_inbox = lambda *a, **k: (_ for _ in ()).throw(OSError("inbox炸了"))
    try:
        st = o.tick()
    finally:
        em.scan_inbox = orig
    assert st is not None, "tick 因inbox失败而中断"


def test_export_failure_does_not_break_tick(tmp_path):
    root = _root(tmp_path)
    _task(root, "p1", "t1")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    orig = o.store.rebuild_export
    o.store.rebuild_export = lambda p: (_ for _ in ()).throw(
        OSError("导出炸了"))
    try:
        o.tick()
    finally:
        o.store.rebuild_export = orig


# ---------------------------------------------------------------- 锁异常

def test_lock_error_is_contained(tmp_path, monkeypatch):
    """取锁时DB异常不能掀翻启动——返回False并记日志。"""
    root = _root(tmp_path)
    Store(root).ensure_plan("p1", template="daily")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.store._write_txn = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db挂了"))
    assert o.acquire_lock() is False
    assert o._lock_held is False


def test_heartbeat_error_drops_ownership(tmp_path):
    """心跳异常时必须认为自己丢了锁并停下，不能装作还持有。"""
    root = _root(tmp_path)
    Store(root).ensure_plan("p1", template="daily")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert o.acquire_lock() is True
    o.store._write_txn = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db挂了"))
    assert o.heartbeat_lock() is False
    assert o._lock_held is False


def test_release_lock_is_safe_when_not_held(tmp_path):
    root = _root(tmp_path)
    Store(root).ensure_plan("p1", template="daily")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.release_lock()                       # 没拿到锁就释放：不应抛


def test_run_forever_returns_when_lock_busy(tmp_path):
    """拿不到锁就安静退出——不能与另一个编排器并行跑。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    o1 = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert o1.acquire_lock() is True
    o2 = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    called = []
    o2.tick = lambda: called.append(1)
    o2.run_forever()                      # 应立即返回
    assert not called, "拿不到锁还在跑主循环"


# ---------------------------------------------------------------- 退避重排

def _to_retry(s, plan_id, tid, task):
    """把任务送进"等退避"状态。

    路径必须是 RUNNING→FAILED→RETRY：状态机里 RETRY 只允许从
    VERIFYING/WAITING_APPROVAL/FAILED 进，RUNNING 不能直接到 RETRY。
    FAILED 也不是终态——它还能升级为 DEAD_LETTER 或回到 RETRY。
    """
    s.transition(plan_id, tid, "READY", agent="o", run_id="r",
                 expect_version=task["version"])
    s.transition(plan_id, tid, "RUNNING", agent="o", run_id="r",
                 expect_version=s.get_task(plan_id, tid)["version"],
                 lease_op="acquire")
    s.transition(plan_id, tid, "FAILED", agent="sub", run_id="r",
                 expect_version=s.get_task(plan_id, tid)["version"],
                 expect_fencing=s.get_task(plan_id, tid)["fencing_token"],
                 payload={"error": "x"})
    s.transition(plan_id, tid, "RETRY", agent="orchestrator", run_id="r",
                 expect_version=s.get_task(plan_id, tid)["version"],
                 expect_fencing=s.get_task(plan_id, tid)["fencing_token"],
                 payload={"reason": "verify_failed", "detail": "x"})


def _age(s, plan_id, tid, hours):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    s._write_txn(lambda con: con.execute(
        "UPDATE tasks SET updated_at=? WHERE plan_id=? AND task_id=?",
        (old, plan_id, tid)))


def test_promote_retries_respects_backoff(tmp_path):
    """退避没到点不重排——这是"预期内"，不该吵出告警。"""
    root = _root(tmp_path)
    s, task = _task(root, "p1", "t1")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    _to_retry(s, "p1", "t1", task)
    assert s.get_task("p1", "t1")["status"] == "RETRY"
    out = o.promote_retries()
    assert not [x for x in out if x.get("task_id") == "t1"], \
        "退避没到点却重排了"
    assert s.get_task("p1", "t1")["status"] == "RETRY"


def test_promote_retries_after_backoff(tmp_path):
    root = _root(tmp_path)
    s, task = _task(root, "p1", "t1")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    _to_retry(s, "p1", "t1", task)
    _age(s, "p1", "t1", 2)              # 退避(60s)早过了
    out = o.promote_retries()
    assert [x for x in out if x.get("task_id") == "t1"], "退避已过却没重排"
    assert s.get_task("p1", "t1")["status"] == "READY"


def test_backlog_alert_fires_when_ready_waits_too_long(tmp_path):
    """READY 积压等太久要告警——队列堵住是运维必须知道的事。

    阈值是模块常量（>50个 或 最老等待>1h），不读实例属性。
    这里走"等太久"那条：把 updated_at 推到2小时前，比造51个任务便宜。
    """
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    _task(root, "p1", "t1")
    o.refresh_ready("p1")
    assert s.get_task("p1", "t1")["status"] == "READY"
    _age(s, "p1", "t1", 2)
    out = o.backlog_check()
    assert out["ready"] == 1
    assert out["oldest_wait_s"] > 3600
    p = root / "reports" / "alerts.jsonl"
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if p.exists() else []
    assert any(r.get("kind") == "queue_backlog" for r in rows), "积压未告警"


def test_no_backlog_alert_when_queue_is_short(tmp_path):
    """队列很短就不该告警——否则告警会被日常噪声淹没。"""
    root = _root(tmp_path)
    _task(root, "p1", "t1")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("p1")
    o.backlog_check()
    p = root / "reports" / "alerts.jsonl"
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if p.exists() else []
    assert not any(r.get("kind") == "queue_backlog" for r in rows), \
        "队列只有1个任务却报了积压"


# ---------------------------------------------------------------- 关机

def test_request_shutdown_stops_the_loop(tmp_path):
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=False)
    assert o.check_shutdown() is False
    o.request_shutdown()
    assert o.check_shutdown() is True
    st = o.tick()
    assert st.get("shutdown") is True, "关机后仍跑了一整轮"


def test_wait_wakeup_uses_flag(tmp_path):
    """enqueue 会 touch wakeup.flag 提前唤醒主循环，省掉一个轮询周期。"""
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    (root / "wakeup.flag").write_text("1", encoding="utf-8")
    t0 = time.monotonic()
    assert o.wait_wakeup(5) is True
    assert time.monotonic() - t0 < 2, "有flag却仍睡满了"
    assert not (root / "wakeup.flag").exists(), "flag没被消费掉"


def test_wait_wakeup_times_out(tmp_path):
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert o.wait_wakeup(0.05) is False


import time  # noqa: E402  放在最后供上面两个测试使用
