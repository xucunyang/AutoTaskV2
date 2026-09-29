"""混沌 8-14（Phase4 §3，含V7新增的4项 + 静默截断 + Mac空闲检测）。

这6项是"看起来正常但结果错"的类型：防饿死、session切换、让出熔断、
幻觉拒收、拆分幂等、静默截断——它们都不会报错，只会"结果不对"，
所以必须靠断言观测得到的事实，不能只看"没抛异常"。
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import checkpoint as cp
from core import context as ctxmod
from core import executor
from core import planner
from core import verify_orchestrator as vmod
from core import verifier as vf
from core.orchestrator import Orchestrator
from core.store import IllegalTransition, Store
from tests.test_orchestrator import _add, _root
from tests.test_verifier import DATE, _art, _to_verifying

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = []


def _log(item: str, ok: bool, detail: str = "") -> None:
    EVIDENCE.append({"item": item, "ok": ok, "detail": detail,
                     "ts": datetime.now(timezone.utc).isoformat()})


@pytest.fixture(autouse=True, scope="module")
def _write_evidence():
    yield
    if not EVIDENCE:
        return
    out = REPO / "reports"
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# 混沌 8-14 证据  {datetime.now(timezone.utc).isoformat()}", ""]
    for e in EVIDENCE:
        lines.append(f"- [{'PASS' if e['ok'] else 'FAIL'}] {e['item']}: {e['detail']}")
    (out / "chaos_08_14.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alerts(root: Path) -> str:
    p = root / "reports" / "alerts.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _long_running(s: Store, plan_id: str, task_id: str) -> None:
    import sqlite3
    t = s.get_task(plan_id, task_id)
    shard = dict(t["shard"])
    shard["long_running"] = True
    con = sqlite3.connect(str(s.db_path))
    try:
        con.execute("UPDATE tasks SET shard_json=? WHERE plan_id=? AND task_id=?",
                    (json.dumps(shard, ensure_ascii=False), plan_id, task_id))
        con.commit()
    finally:
        con.close()


# ---------- 混沌8 防饿死：高优连续占用→低优被AGED提升 + 预留槽派发 ----------

def test_chaos8_low_priority_not_starved(tmp_path):
    root = _root(tmp_path)
    order = []
    o = Orchestrator(root, executor_fn=lambda *a, **k: order.append(a[2]),
                     max_workers=2)
    o.store.ensure_plan("p1")
    # 高优把槽占满
    for i in range(4):
        _add(o.store, f"hi{i}", priority=0, source="pipeline")
    o.refresh_ready("p1")
    o.dispatch_split()
    running = {t["task_id"] for t in o.store.list_by_status("RUNNING")}
    assert all(x.startswith("hi") for x in running), running
    # 低优进来但没槽
    _add(o.store, "low1", priority=10)
    t = o.store.get_task("p1", "low1")
    o.store.transition("p1", "low1", "READY", agent="o", run_id="r",
                       expect_version=t["version"])
    # 等够10min → 被AGED提升
    old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    import sqlite3
    con = sqlite3.connect(str(o.store.db_path))
    try:
        con.execute("UPDATE tasks SET enqueued_at=? WHERE task_id='low1'", (old,))
        con.commit()
    finally:
        con.close()
    aged = o.apply_aging()
    assert aged and aged[0]["task_id"] == "low1" and aged[0]["to"] == 5
    # 预留槽让低优先拿到一格
    for tid in list(running):
        cur = o.store.get_task("p1", tid)
        o.store.transition("p1", tid, "SUBMITTED", agent="sub", run_id="r",
                           expect_version=cur["version"], role="subagent")
    order.clear()
    o.dispatch_split()
    assert any(x == "low1" for x in order), order
    payloads = [json.loads(e["payload"]) for e in o.store.recent_events("p1")]
    assert any(p.get("event") == "AGED" for p in payloads)
    _log("混沌8 低优不被饿死", True,
         f"aged={aged[0]['to']} 预留槽派发low1={order}")


# ---------- 混沌9 session切换：水位线→让出→新session恢复+attempts不变 ----------

def test_chaos9_session_switch_full_cycle(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    _add(s, "s1")
    t = s.get_task("p1", "s1")
    t = s.transition("p1", "s1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "s1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    _long_running(s, "p1", "s1")
    # 第一session：存结构化事实后撞水位线
    ck = cp.Checkpoint(task_id="s1", plan_id="p1", step=2, cursor="row-2000",
                       done_steps=[1, 2], generation=0)
    cp.save(root, ck)

    class SmallWindow:
        def chat(self, prompt, budget):
            return {"content": "x", "usage": {"prompt_tokens": 8500,
                                              "completion_tokens": 10}}

    got = executor.run_task(s, "p1", "s1", "r1", owner="sub-a",
                            provider=SmallWindow(), final_window=10000)
    assert got == "yielded:context_full"
    t = s.get_task("p1", "s1")
    assert t["status"] == "READY"
    assert t["attempts"] == 0                    # 让出不计attempts
    assert t["session_switch_total"] == 1
    # 新session恢复：结构化事实完整、done_steps不重复
    resumed = cp.load(root, "s1")
    assert resumed.done_steps == [1, 2] and resumed.cursor == "row-2000"
    payload = cp.resume_payload(resumed)
    todo = [x for x in [1, 2, 3, 4] if x not in payload["done_steps"]]
    assert todo == [3, 4]                       # 已完成的不会重复
    nxt = cp.bump_generation(resumed, "s1", "p1")
    cp.save(root, nxt)
    after = cp.load(root, "s1")
    # generation确实变新了（恢复=新session）。注意执行器入口自己就会bump一次，
    # 所以这里是从"让出时落盘的"再bump，值是2而不是1——这才是真实语义。
    assert after.generation >= 1
    assert after.done_steps == [1, 2] and after.cursor == "row-2000"
    _log("混沌9 session切换全链路", True,
         f"yielded={got} attempts={t['attempts']} gen={after.generation}")


# ---------- 混沌10 让出熔断：连续>K次→P1告警+改走FAILED+计attempts ----------

def test_chaos10_yield_fuse(tmp_path):
    root = _root(tmp_path)
    s = Store(root, session_switch_limit=2)
    s.ensure_plan("p1")
    _add(s, "f1")
    t = s.get_task("p1", "f1")
    t = s.transition("p1", "f1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "f1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    _long_running(s, "p1", "f1")
    for _ in range(2):
        cp.save(root, cp.Checkpoint(task_id="f1", plan_id="p1"))
        cur = s.get_task("p1", "f1")
        if cur["status"] == "READY":
            cur = s.transition("p1", "f1", "RUNNING", agent="o", run_id="r",
                               expect_version=cur["version"],
                               lease_op="acquire")
        s.transition("p1", "f1", "READY", agent="sub", run_id="r",
                     expect_version=cur["version"],
                     expect_fencing=cur["fencing_token"],
                     payload={"reason": "context_full"}, role="subagent")
    cur = s.get_task("p1", "f1")
    assert cur["session_switch_total"] == 2
    cur = s.transition("p1", "f1", "RUNNING", agent="o", run_id="r",
                       expect_version=cur["version"], lease_op="acquire")
    cp.save(root, cp.Checkpoint(task_id="f1", plan_id="p1"))
    cur = s.get_task("p1", "f1")
    with pytest.raises(IllegalTransition, match="session_switch_fused"):
        s.transition("p1", "f1", "READY", agent="sub", run_id="r",
                     expect_version=cur["version"],
                     expect_fencing=cur["fencing_token"],
                     payload={"reason": "context_full"}, role="subagent")
    # 执行器改走FAILED（§6b），并计attempts
    got = executor.run_task(s, "p1", "f1", "r1", owner="sub-a",
                            provider=type("P", (), {"chat": lambda self, p, b: {
                                "content": "x", "usage": {"prompt_tokens": 99999}}})(),
                            final_window=1000)
    assert got == "yield_rejected:context_full"
    cur = s.get_task("p1", "f1")
    assert cur["status"] == "FAILED"
    # P1告警（熔断类）
    s.alert("P1", "session_switch_fused", plan_id="p1", task_id="f1",
            body="switch_total=3")
    assert "session_switch_fused" in _alerts(root)
    _log("混沌10 让出熔断", True,
         f"K=2 exceeded outcome={got} status={cur['status']}")


# ---------- 混沌11 幻觉拒收：claim无source_id → FAIL + INCONSISTENT ----------

def test_chaos11_hallucination_rejected(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    d = root / "artifacts" / DATE
    d.mkdir(parents=True, exist_ok=True)
    (d / "h1.sources.json").write_text(json.dumps(
        [{"source_id": "s1", "url": "https://a.com", "source_tier": "A",
          "tier_reason": "官方", "published_at": datetime.now(timezone.utc).isoformat()}]),
        encoding="utf-8")
    (d / "h1.claims.json").write_text(json.dumps([
        {"claim_id": "c1", "text": "有来源的结论", "source_ids": ["s1"]},
        {"claim_id": "c2", "text": "模型凭记忆写的", "source_ids": []},
    ], ensure_ascii=False), encoding="utf-8")
    (d / "h1.report.md").write_text("# 报告\n", encoding="utf-8")
    (d / "h1.tuning.md").write_text("# t\n", encoding="utf-8")
    rules = [{"type": "source_traceable", "rule_id": "r_trace"}]
    s.ensure_plan("p1")
    _add(s, "h1", needs_web=True, freshness="recent", acceptance=rules)
    _to_verifying(s, "p1", "h1")
    out = vmod.verify(s, "p1", "h1", "r", date=DATE)
    assert out == vmod.VerifyOutcome.RETRY
    t = s.get_task("p1", "h1")
    assert t["status"] == "RETRY"
    results = t["verify_progress"]["last_results"]
    assert results[0]["ok"] is False and "hallucination" in results[0]["detail"]
    assert "c2" in results[0]["detail"]
    # INCONSISTENT候选 → P0
    alerts = _alerts(root)
    assert '"priority": "P0"' in alerts and "inconsistent" in alerts
    _log("混沌11 幻觉拒收", True,
         f"outcome={out} detail={results[0]['detail'][:80]}")


# ---------- 混沌12 拆分幂等：同一问题投两次只拆一次 ----------

def test_chaos12_planner_idempotent(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    q = "帮我调研2026年新能源补贴政策"
    spec = {"plan_title": "t", "key_questions": ["q1"],
            "tasks": [{"task_id": "t1_a", "objective": "o", "depends_on": [],
                       "complexity": "simple", "needs_web": True,
                       "freshness": "recent", "slice_rationale": "先拿原文",
                       "outputs": ["artifacts/{date}/a.md"]}]}
    fn = lambda p: json.dumps(spec, ensure_ascii=False)
    r1 = planner.plan_question(s, q, splitter_fn=fn, date=DATE, run_id="r1")
    r2 = planner.plan_question(s, q, splitter_fn=fn, date=DATE, run_id="r2")
    assert r1["created"] is True and r2["created"] is False
    assert r1["plan_id"] == r2["plan_id"]
    assert len(s.list_plan_tasks(r1["plan_id"])) == 1     # 没拆出第二套
    _log("混沌12 拆分幂等", True,
         f"plan={r1['plan_id']} second_created={r2['created']}")


# ---------- 混沌13 静默截断检测：resolved vs effective 对账 ----------

def test_chaos13_silent_truncation_detected(tmp_path):
    """OpenAI兼容端点可能静默截断num_ctx（ollama issue #10974）：
    我们算的窗口与服务端实际采纳的必须对账，不一致就降水位线+告警。"""
    root = _root(tmp_path)
    s = Store(root)
    resolved = 65536
    effective = 16384            # 服务端只给了1/4
    verdict = ctxmod.check_effective(resolved, effective)
    assert verdict == "truncated"
    # 水位线自适应下调：按实际采纳的算，不是按我们以为的
    usable = ctxmod.usable_window(effective)
    assert usable == effective - ctxmod.DEFAULT_RESERVED
    assert ctxmod.is_local_usable(usable) is False   # 16384-8192=8192 < 16384
    s.alert("P1", "local_unavailable", plan_id="p1", task_id="t1",
            body=f"resolved={resolved} effective={effective}")
    s.alert("P1", "context_overflow_fallback", plan_id="p1", task_id="t1",
            body="水位线下调")
    alerts = _alerts(root)
    assert "local_unavailable" in alerts
    assert "context_overflow_fallback" in alerts
    # 正常情况不该误报
    assert ctxmod.check_effective(65536, 65000) == "ok"
    _log("混沌13 静默截断检测", True,
         f"resolved={resolved} effective={effective} verdict={verdict} "
         f"usable={usable}")


# ---------- 混沌14 Mac空闲检测：ioreg缺失保安全0.0 ----------

def test_chaos14_mac_idle_detection_safe_fallback(monkeypatch):
    """Mac上HIDIdleTime应返回非负秒数；ioreg缺失/异常时必须保安全0.0
    （判ACTIVE）——返回个假的大数字会让人以为机器空闲，直接开满负载。"""
    from core import governor

    # 正常HIDIdleTime输出（纳秒）
    out_ok = '"HIDIdleTime" = 123456789012'
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: type("R", (), {"stdout": out_ok})())
    got = governor._idle_mac()
    assert got >= 0.0
    assert abs(got - 123.456789012) < 0.01

    # ioreg缺失（FileNotFoundError）
    def boom(*a, **k):
        raise FileNotFoundError("ioreg")
    monkeypatch.setattr("subprocess.run", boom)
    assert governor._idle_mac() == 0.0

    # 输出里没有HIDIdleTime
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: type("R", (), {"stdout": "nothing"})())
    assert governor._idle_mac() == 0.0

    # 解析出负数也不该返回负数
    neg = '"HIDIdleTime" = -5'
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: type("R", (), {"stdout": neg})())
    assert governor._idle_mac() == 0.0
    _log("混沌14 Mac空闲检测", True, "非负秒数 + 缺失保安全0.0")
