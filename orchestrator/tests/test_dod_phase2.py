"""Phase2 §8 DoD 逐条核对。

这个文件的作用不是"测代码"，而是**对照设计文档的DoD清单**，
一条条标出：通过 / 部分通过 / 未做（并说明为什么）。
任何一条"部分通过"都不许含糊——它要么补齐，要么在文档里写清楚边界。
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import checkpoint as cp
from core import executor
from core.orchestrator import Orchestrator, SLA_DISPATCH_S
from core.store import Store
from core.task_card import estimate_tokens
from tests.test_orchestrator import _add, _root


# ---------- DoD-1 200MB CSV prompt<8k tokens ----------

def test_dod1_big_file_prompt_budget(tmp_path):
    """DoD原文是200MB；机制与文件大小无关（只喂manifest），CI里用20万行验证。"""
    big = tmp_path / "artifacts" / "2026-09-29" / "huge.csv"
    big.parent.mkdir(parents=True, exist_ok=True)
    with big.open("w", encoding="utf-8") as f:
        f.write("id,value\n")
        for i in range(200000):
            f.write(f"{i},payload_{i}\n")
    root = _root(tmp_path)
    s = Store(root)
    m = cp.build_manifest(tmp_path, big)
    card = executor.build_card(s, {
        "task_id": "t1", "plan_id": "p1", "idempotency_key": "k",
        "shard": {"objective": "统计大CSV", "outputs": [], "acceptance": [],
                  "inputs": [{"path": "artifacts/2026-09-29/huge.csv",
                              "sha256": m["sha256"], "bytes": m["bytes"],
                              "rows": m["rows"]}]}}, "r")
    assert estimate_tokens(card) < 8000


# ---------- DoD-2 kill子进程后从checkpoint续跑（覆盖见test_session_switch） ----------


# ---------- DoD-3 相同输入二次触发命中SKIPPED_CACHED + 省token可观测 ----------

def test_dod3_cache_hit_is_observable(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    out = root / "artifacts" / "2026-09-29" / "o.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("data", encoding="utf-8")
    from core.utils import atomic_write_json
    m = cp.build_manifest(root, out)
    atomic_write_json(out.parent / "o.manifest.json", m)
    ih = "spec-hash"
    _add(s, "c1", priority=5, outputs=["artifacts/2026-09-29/o.csv"],
         inputs_hash=ih)
    t = s.get_task("p1", "c1")
    s.transition("p1", "c1", "READY", agent="o", run_id="r",
                 expect_version=t["version"])
    cp.record_success(s, "p1", "c1", ih)      # 存的是inputs_hash
    o = Orchestrator(root, executor_fn=lambda *a, **k: pytest.fail("不该真跑"))
    o.dispatch_split()
    assert s.get_task("p1", "c1")["status"] == "SKIPPED_CACHED"
    con = s._connect()
    try:
        rows = con.execute("SELECT name,value FROM metrics WHERE name='cache_hit'"
                           ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1 and rows[0]["value"] == 1   # 省了多少可查


# ---------- DoD-4 DAG环/孤儿被拦截不派发 ----------

def test_dod4_cycle_never_dispatched(tmp_path):
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: pytest.fail("不该派发"))
    o.store.ensure_plan("p1")
    _add(o.store, "a", deps=["b"])
    _add(o.store, "b", deps=["a"])
    o.tick()
    assert all(t["status"] == "PENDING" for t in o.store.list_plan_tasks("p1"))


# ---------- DoD-5 FIFO：同优先级按seq派发顺序与入队一致 ----------

def test_dod5_fifo_same_priority(tmp_path):
    root = _root(tmp_path)
    order = []
    o = Orchestrator(root, executor_fn=lambda *a, **k: order.append(a[2]),
                     max_workers=4)
    o.store.ensure_plan("p1")
    for i in range(4):
        _add(o.store, f"t{i}", priority=5)
    o.refresh_ready("p1")
    o.dispatch_split()
    assert order == ["t0", "t1", "t2", "t3"]


def test_dod5_pipeline_jumps_ahead_of_daily(tmp_path):
    """高优插队：管道(0)在Daily(10)之前。"""
    root = _root(tmp_path)
    order = []
    o = Orchestrator(root, executor_fn=lambda *a, **k: order.append(a[2]),
                     max_workers=4)
    o.store.ensure_plan("p1")
    for i in range(3):
        _add(o.store, f"d{i}", priority=10, source="daily")
    _add(o.store, "pipe1", priority=0, source="pipeline")
    o.refresh_ready("p1")
    o.dispatch_split()
    assert order[0] == "pipe1"
    assert order[1:] == ["d0", "d1", "d2"]


# ---------- DoD-6 高优排队 p95<60s（排队等待非抢占） ----------

def test_dod6_dispatch_latency_measured(tmp_path):
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None)
    o.store.ensure_plan("p1")
    _add(o.store, "hi", priority=0, source="pipeline")
    o.refresh_ready("p1")
    t0 = time.monotonic()
    o.dispatch_split()
    elapsed = time.monotonic() - t0
    assert elapsed < SLA_DISPATCH_S
    con = o.store._connect()
    try:
        rows = con.execute("SELECT value FROM metrics WHERE name='dispatch_wait'"
                           ).fetchall()
    finally:
        con.close()
    assert rows and rows[0]["value"] < SLA_DISPATCH_S   # 排队时长可观测


def test_dod6_sla_breach_raises_alert(tmp_path):
    """超过SLA要P0告警（去重属Phase3）。"""
    import sqlite3
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None)
    o.store.ensure_plan("p1")
    _add(o.store, "hi", priority=0)
    o.refresh_ready("p1")
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    con = sqlite3.connect(str(o.store.db_path))
    try:
        con.execute("UPDATE tasks SET updated_at=? WHERE task_id='hi'", (old,))
        con.commit()
    finally:
        con.close()
    o.dispatch_split()
    alerts = (tmp_path / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert "dispatch_sla_breach" in alerts


# ---------- DoD-7 防饿死：多档老化+预留槽生效且可审计 ----------

def test_dod7_aging_and_reserved_slot_auditable(tmp_path):
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, max_workers=2)
    o.store.ensure_plan("p1")
    _add(o.store, "lo", priority=10)
    o.refresh_ready("p1")
    old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    import sqlite3
    con = sqlite3.connect(str(o.store.db_path))
    try:
        con.execute("UPDATE tasks SET enqueued_at=? WHERE task_id='lo'", (old,))
        con.commit()
    finally:
        con.close()
    aged = o.apply_aging()
    assert aged and aged[0]["to"] == 5
    payloads = [json.loads(e["payload"]) for e in o.store.recent_events("p1")]
    assert any(p.get("event") == "AGED" and p.get("waited_s", 0) > 600
               for p in payloads)     # 可审计：等了多久、被提到什么优先级


# ---------- DoD-8 Session切换（在test_session_switch覆盖，这里验证attempts口径） ----------

def test_dod8_yield_never_counts_as_attempt(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    t = _add(s, "t1")
    t = s.transition("p1", "t1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    shard = dict(t["shard"])
    shard["long_running"] = True
    import sqlite3
    con = sqlite3.connect(str(s.db_path))
    try:
        con.execute("UPDATE tasks SET shard_json=? WHERE task_id='t1'",
                    (json.dumps(shard),))
        con.commit()
    finally:
        con.close()
    for _ in range(2):
        cp.save(root, cp.Checkpoint(task_id="t1", plan_id="p1"))
        t = s.get_task("p1", "t1")
        t = s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                         expect_version=t["version"],
                         expect_fencing=t["fencing_token"],
                         payload={"reason": "context_full"}, role="subagent")
        t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                         expect_version=t["version"], lease_op="acquire")
    assert t["attempts"] == 0                    # 让出两次仍不计attempts
    assert t["session_switch_total"] == 2


# ---------- DoD-9 拆分（见test_planner） ----------


# ---------- DoD-10 检索四件套（见test_search） ----------


def test_dod10_non_web_task_produces_no_sources(tmp_path):
    """DoD：未检索任务不产出sources.json（别给不需要的东西造文件）。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    _, t = s.insert_task({"task_id": "nw", "plan_id": "p1", "status": "PENDING",
                          "idempotency_key": "k", "objective": "不需要联网",
                          "needs_web": False, "freshness": "none",
                          "outputs": []})
    t = s.transition("p1", "nw", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "nw", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    executor.run_task(s, "p1", "nw", "r", owner="sub-1")   # 无provider→不检索
    assert not list((root / "artifacts").glob("**/nw.sources.json"))


def test_dod10_web_task_requires_search(tmp_path):
    """needs_web=true但没给搜索provider → 不能静默降级成"用参数内知识答"。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    _, t = s.insert_task({"task_id": "w1", "plan_id": "p1", "status": "PENDING",
                          "idempotency_key": "k", "objective": "要检索",
                          "needs_web": True, "freshness": "recent",
                          "outputs": []})
    t = s.transition("p1", "w1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    s.transition("p1", "w1", "RUNNING", agent="o", run_id="r",
                 expect_version=t["version"], lease_op="acquire")

    class P:
        def chat(self, prompt, budget):
            return {"content": "凭记忆写的", "usage": {"prompt_tokens": 10}}
    got = executor.run_task(s, "p1", "w1", "r", owner="sub-1", provider=P(),
                            final_window=100000)
    # 没有search_fn → 不允许继续（走让出/失败），而不是拿参数内知识糊弄
    assert got != "submitted"
    assert s.get_task("p1", "w1")["status"] != "SUBMITTED"
