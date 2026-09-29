"""Phase2 §2/§4：主循环派发、DAG校验、老化防饿死、唤醒。"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.orchestrator import Orchestrator
from core.store import Store

ROOT_TPL = Path(__file__).resolve().parents[1]


def _root(tmp_path: Path) -> Path:
    """带config/templates/schemas的隔离root。"""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "schedule.yaml").write_text(
        yaml.safe_dump({"jobs": [{"id": "daily_report", "cron": "0 8 * * *",
                                  "timezone": "Asia/Shanghai",
                                  "template": "templates/daily_plan.yaml",
                                  "catchup": True}],
                        "workers": {"reserved_low_slot": 1}}, allow_unicode=True),
        encoding="utf-8")
    for sub in ("templates", "schemas"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for f in ("templates/task_card.j2", "templates/daily_plan.yaml",
              "schemas/acceptance_t1.yaml", "schemas/acceptance_t2.yaml",
              "schemas/acceptance_t3.yaml"):
        dst = tmp_path / f
        dst.write_text((ROOT_TPL / f).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _orch(tmp_path, executor_fn=None, **kw):
    _root(tmp_path)
    return Orchestrator(tmp_path, executor_fn=executor_fn or (lambda *a: None), **kw)


def _add(store, tid, plan_id="p1", deps=None, priority=10, outputs=None,
         inputs_hash="h1", source="daily", **over):
    """建任务。**over透传进shard（acceptance/needs_web/freshness等）。"""
    _, t = store.insert_task({
        "task_id": tid, "plan_id": plan_id, "status": "PENDING",
        "idempotency_key": f"{plan_id}/{tid}/x", "inputs_hash": inputs_hash,
        "objective": f"do {tid}", "depends_on": deps or [],
        "priority": priority, "source": source, "outputs": outputs or [],
        **over})
    return t


# ---------- §2 单实例锁 ----------

def test_lock_is_exclusive(tmp_path):
    a = _orch(tmp_path)
    b = Orchestrator(tmp_path, executor_fn=lambda *a: None)
    assert a.acquire_lock() is True
    assert b.acquire_lock() is False        # 第二个编排器进不来
    a.release_lock()
    assert b.acquire_lock() is True


def test_lock_appears_in_active_plans_filter(tmp_path):
    """锁行不能被当成业务plan去validate_dag/rebuild_export。"""
    o = _orch(tmp_path)
    o.acquire_lock()
    o.store.ensure_plan("p1")
    _add(o.store, "t1")
    assert o.active_plans() == ["p1"]
    assert "orchestrator_lock" not in o.active_plans()


# ---------- §2 validate_dag ----------

def test_validate_dag_detects_cycle(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", deps=["c"])
    _add(o.store, "b", deps=["a"])
    _add(o.store, "c", deps=["b"])
    bad = o.validate_dag("p1")
    assert any(b["problem"] == "cycle" for b in bad)
    alerts = (tmp_path / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert "dag_invalid" in alerts


def test_validate_dag_detects_self_and_missing_dep(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", deps=["a"])
    _add(o.store, "b", deps=["ghost"])
    problems = {b["problem"] for b in o.validate_dag("p1")}
    assert "self_dependency" in problems
    assert "missing_dependency" in problems


def test_validate_dag_clean(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a")
    _add(o.store, "b", deps=["a"])
    _add(o.store, "c", deps=["b"])
    assert o.validate_dag("p1") == []


def test_broken_dag_is_not_dispatched(tmp_path):
    """DoD：环/孤儿被拦截不派发。"""
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", deps=["a"])
    o.tick()
    assert o.store.get_task("p1", "a")["status"] == "PENDING"   # 没被推走


# ---------- §2 refresh_ready ----------

def test_refresh_ready_respects_dependencies(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a")
    _add(o.store, "b", deps=["a"])
    n = o.refresh_ready("p1")
    assert n == 1
    assert o.store.get_task("p1", "a")["status"] == "READY"
    assert o.store.get_task("p1", "b")["status"] == "PENDING"


def test_refresh_ready_unblocks_after_upstream_done(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a")
    _add(o.store, "b", deps=["a"])
    o.refresh_ready("p1")
    t = o.store.get_task("p1", "a")
    t = o.store.transition("p1", "a", "RUNNING", agent="o", run_id="r",
                           expect_version=t["version"], lease_op="acquire")
    t = o.store.transition("p1", "a", "SUBMITTED", agent="sub", run_id="r",
                           expect_version=t["version"], role="subagent")
    t = o.store.transition("p1", "a", "VERIFYING", agent="o", run_id="r",
                           expect_version=t["version"])
    o.store.transition("p1", "a", "DONE", agent="v", run_id="r",
                       expect_version=t["version"], role="verifier")
    assert o.refresh_ready("p1") == 1
    assert o.store.get_task("p1", "b")["status"] == "READY"


# ---------- §4 防饿死 ----------

def _age_ready(store, tid, waited_s, priority=10, plan_id="p1"):
    """造一个READY且enqueued_at在waited_s之前的任务。
    老化时钟取enqueued_at（入队时刻），不是updated_at——
    老化自己会改updated_at，拿它当基准会把等待清零导致第二档永不触发。"""
    import sqlite3
    ts = (datetime.now(timezone.utc) - timedelta(seconds=waited_s)).isoformat()
    con = sqlite3.connect(str(store.db_path))
    try:
        con.execute("UPDATE tasks SET enqueued_at=?, priority=? WHERE plan_id=? AND task_id=?",
                    (ts, priority, plan_id, tid))
        con.commit()
    finally:
        con.close()


def test_aging_promotes_10_to_5_after_10min(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", priority=10)
    o.refresh_ready("p1")
    _age_ready(o.store, "a", 700, priority=10)
    aged = o.apply_aging()
    assert aged and aged[0]["to"] == 5
    assert o.store.get_task("p1", "a")["priority"] == 5


def test_aging_raises_only_one_tier_per_check(tmp_path):
    """设计§4"每次老化检查可升一档"：等了25min也只到5，下轮再到3。
    一次跳到3会让分档失去意义——分档就是为了分级提优先级。"""
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", priority=10)
    o.refresh_ready("p1")
    _age_ready(o.store, "a", 1500, priority=10)     # 25min
    assert o.apply_aging()[0]["to"] == 5
    assert o.apply_aging()[0]["to"] == 3             # 第二轮才到3
    assert o.apply_aging() == []                     # 到3封顶，不动


def test_aging_respects_cap_and_never_downgrades(tmp_path):
    """上限3；且只升不降——降级会让等待中的任务被无限推后。"""
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", priority=1)          # 已经很急，别动
    o.refresh_ready("p1")
    _age_ready(o.store, "a", 5000, priority=1)
    assert o.apply_aging() == []
    assert o.store.get_task("p1", "a")["priority"] == 1


def test_aging_records_aged_event(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a", priority=10)
    o.refresh_ready("p1")
    _age_ready(o.store, "a", 700, priority=10)
    o.apply_aging()
    payloads = [json.loads(e["payload"]) for e in o.store.recent_events("p1")]
    assert any(p.get("event") == "AGED" for p in payloads)


def test_update_priority_refuses_downgrade(tmp_path):
    from core.store import IllegalTransition
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    _add(s, "a", priority=5)
    t = s.get_task("p1", "a")
    s.transition("p1", "a", "READY", agent="o", run_id="r",
                 expect_version=t["version"])
    t = s.get_task("p1", "a")
    with pytest.raises(IllegalTransition, match="downgrade"):
        s.update_priority("p1", "a", 9, expect_version=t["version"])


def test_reserved_slot_gives_low_priority_a_chance(tmp_path):
    """高优连续占满时，低优必须还能拿到预留槽（否则Daily饿死）。"""
    calls = []
    o = _orch(tmp_path, executor_fn=lambda *a, **k: calls.append(a[2]),
              max_workers=2)
    o.store.ensure_plan("p1")
    for i in range(4):
        _add(o.store, f"hi{i}", priority=0)
    for i in range(2):
        _add(o.store, f"lo{i}", priority=10)
    o.refresh_ready("p1")
    o.dispatch_split()
    got = {c for c in calls}
    assert any(c.startswith("hi") for c in got)
    assert any(c.startswith("lo") for c in got), "低优没拿到预留槽，会饿死"


# ---------- §2/§6 cache_hit ----------

def test_dispatch_skips_cached_task(tmp_path):
    """DoD：相同输入二次触发命中SKIPPED_CACHED。"""
    seen = []
    o = _orch(tmp_path, executor_fn=lambda *a: seen.append(a[2]))
    o.store.ensure_plan("p1")
    out = tmp_path / "artifacts" / "2026-09-29" / "o.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("data", encoding="utf-8")
    from core.utils import sha256_file
    digest = sha256_file(out)["sha256"]
    _add(o.store, "c1", priority=5, outputs=["artifacts/2026-09-29/o.csv"],
         inputs_hash=digest)
    s = o.store
    from core import checkpoint as cp
    t = s.get_task("p1", "c1")
    s.transition("p1", "c1", "READY", agent="o", run_id="r",
                 expect_version=t["version"])
    cp.record_success(s, "p1", "c1", digest)
    o.dispatch_split()
    assert seen == []                          # 没真跑
    assert s.get_task("p1", "c1")["status"] == "SKIPPED_CACHED"
    metrics = s.daily_summary("p1")
    assert metrics is not None


def test_dispatch_respects_max_workers(tmp_path):
    o = _orch(tmp_path, max_workers=2)
    o.store.ensure_plan("p1")
    for i in range(5):
        _add(o.store, f"t{i}", priority=5)
    o.refresh_ready("p1")
    o.store.ensure_plan("p2")
    dispatched = o.dispatch_split()
    assert len(dispatched) <= 2


# ---------- §2 promote_submitted ----------

def test_promote_submitted_is_orchestrator_only(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a")
    t = o.store.get_task("p1", "a")
    t = o.store.transition("p1", "a", "READY", agent="o", run_id="r",
                           expect_version=t["version"])
    t = o.store.transition("p1", "a", "RUNNING", agent="o", run_id="r",
                           expect_version=t["version"], lease_op="acquire")
    o.store.transition("p1", "a", "SUBMITTED", agent="sub", run_id="r",
                       expect_version=t["version"], role="subagent")
    got = o.promote_submitted()
    assert [g["task_id"] for g in got] == ["a"]
    assert o.store.get_task("p1", "a")["status"] == "VERIFYING"


# ---------- §2 唤醒 ----------

def test_wait_wakeup_returns_true_on_flag(tmp_path):
    o = _orch(tmp_path, poll_normal_s=2)
    (tmp_path / "wakeup.flag").write_text("1", encoding="utf-8")
    assert o.wait_wakeup(1.0) is True
    assert not (tmp_path / "wakeup.flag").exists()   # 消费掉，避免下轮空转


def test_wait_wakeup_times_out(tmp_path):
    o = _orch(tmp_path)
    t0 = time.time()
    assert o.wait_wakeup(0.3) is False
    assert time.time() - t0 < 2.0


def test_shutdown_via_stop_file(tmp_path):
    o = _orch(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "STOP").write_text("1", encoding="utf-8")
    assert o.check_shutdown() is True
    assert o.tick()["shutdown"] is True


def test_tick_runs_end_to_end(tmp_path):
    o = _orch(tmp_path)
    o.store.ensure_plan("p1")
    _add(o.store, "a")
    _add(o.store, "b", deps=["a"])
    stats = o.tick()
    assert stats["refreshed"] == 1
    assert stats["dispatched"] == 1
    assert stats["shutdown"] is None if "shutdown" in stats else True
    assert o.store.replay("p1")["ok"]


def test_run_forever_releases_lock(tmp_path):
    o = _orch(tmp_path, run_once=True)
    o.run_forever()
    assert o._lock_held is False
    b = Orchestrator(tmp_path, executor_fn=lambda *a: None)
    assert b.acquire_lock() is True      # 锁已让出
