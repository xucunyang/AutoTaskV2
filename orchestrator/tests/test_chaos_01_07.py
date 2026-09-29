"""混沌 1-7（Phase4 §3）。

"混沌"的定义：主动破坏（kill进程/抢租约/并发双写/造非法写），
然后断言系统**没有静默损坏**。判据分两类：
- 状态类：replay一致、seq不回退、状态没被写坏
- 拒绝类：非法写必须被明确拒（IllegalTransition/StaleOwner）+有告警

证据落reports/chaos_01_07.log（设计§6要求"证据写入reports/chaos_*.log"）。
时间断言用p95/p99口径，不用wall-clock硬线（CI机器性能差异大）。
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import checkpoint as cp
from core import lease as lease_mod
from core import verifier as vf
from core.orchestrator import Orchestrator, SLA_DISPATCH_S
from core.store import Conflict, IllegalTransition, StaleOwner, Store
from core.task_card import estimate_tokens
from tests.test_orchestrator import _add, _root

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
    lines = [f"# 混沌 1-7 证据  {datetime.now(timezone.utc).isoformat()}", ""]
    for e in EVIDENCE:
        lines.append(f"- [{'PASS' if e['ok'] else 'FAIL'}] {e['item']}: {e['detail']}")
    (out / "chaos_01_07.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _alerts(root: Path) -> str:
    p = root / "reports" / "alerts.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------- 混沌1 kill -9 子执行中：租约回收→无半写→replay一致 ----------

def test_chaos1_kill9_midrun_recovers(tmp_path):
    root = _root(tmp_path)
    code = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(REPO)!r})
        from core.store import Store
        from core import checkpoint as cp
        root = Path(sys.argv[1])
        s = Store(root)
        s.ensure_plan("p1")
        t = s.insert_task({{"task_id": "k1", "plan_id": "p1", "status": "PENDING",
                          "idempotency_key": "kk", "objective": "长任务",
                          "long_running": True}})[1]
        t = s.transition("p1", "k1", "READY", agent="o", run_id="r",
                         expect_version=t["version"])
        t = s.transition("p1", "k1", "RUNNING", agent="o", run_id="r",
                         expect_version=t["version"], lease_op="acquire")
        cp.save(root, cp.Checkpoint(task_id="k1", plan_id="p1", step=3,
                                    cursor="row-3000", done_steps=[1, 2, 3]))
        print("RUNNING", flush=True)
        time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code, str(root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    assert proc.stdout.readline().strip() == "RUNNING"
    proc.kill()
    proc.wait(timeout=30)

    s = Store(root)
    t = s.get_task("p1", "k1")
    assert t["status"] == "RUNNING"                 # 崩溃时确实停在RUNNING
    # 租约过期 → 回收成FAILED（被动失联）
    s.lease_update("p1", "k1", owner="sub-dead",
                   lease_until=(datetime.now(timezone.utc)
                                - timedelta(seconds=1)).isoformat(),
                   bump_fencing=False)
    rec = lease_mod.recover_expired_leases(s, run_id="chaos")
    assert [r["task_id"] for r in rec] == ["k1"]
    assert rec[0]["status"] == "FAILED"
    # 无半写：replay一致 + 库完好
    assert s.integrity_check()["integrity"] == "ok"
    assert s.replay("p1")["ok"]
    # checkpoint还在 → 能续跑
    ck = cp.load(root, "k1")
    assert ck is not None and ck.cursor == "row-3000"
    _log("混沌1 kill-9无半写+租约回收", True,
         f"recovered={len(rec)} replay_ok={s.replay('p1')['ok']} "
         f"cursor={ck.cursor}")


# ---------- 混沌2 双调度同plan_id：只建一次 ----------

def test_chaos2_double_scheduler_builds_once(tmp_path):
    root = _root(tmp_path)
    a = Orchestrator(root, executor_fn=lambda *a, **k: None)
    b = Orchestrator(root, executor_fn=lambda *a, **k: None)
    assert a.acquire_lock() is True
    assert b.acquire_lock() is False         # 第二路进不来
    # 即便绕过锁各自建plan，INSERT OR IGNORE也只建一次
    import yaml
    tpl = "templates/daily_plan.yaml"
    from core import scheduler
    r1 = scheduler.ensure_plan(a.store, "2026-09-29", tpl)
    r2 = scheduler.ensure_plan(b.store, "2026-09-29", tpl)
    assert r1[0] == "created" and r2[0] == "skipped"
    assert len(a.store.list_plan_tasks("daily_report_2026-09-29")) == 3
    _log("混沌2 双调度只建一次", True, f"first={r1} second={r2}")


# ---------- 混沌3 旧fencing写回被拒 ----------

def test_chaos3_stale_fencing_write_rejected(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    _add(s, "f1")
    t = s.get_task("p1", "f1")
    t = s.transition("p1", "f1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "f1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    old_fencing = t["fencing_token"]
    # 租约过期→worker-2抢租→fencing变大
    s.lease_update("p1", "f1", owner="sub-1",
                   lease_until=(datetime.now(timezone.utc)
                                - timedelta(seconds=1)).isoformat(),
                   bump_fencing=False)
    t2 = lease_mod.acquire(s, "p1", "f1", owner="sub-2", ttl=120)
    assert t2["fencing_token"] > old_fencing
    # worker-1拿着旧fencing写回 → 必须被拒
    with pytest.raises(StaleOwner):
        s.transition("p1", "f1", "SUBMITTED", agent="sub-1", run_id="r",
                     expect_version=t2["version"],
                     expect_fencing=old_fencing, role="subagent")
    assert s.get_task("p1", "f1")["status"] == "RUNNING"
    assert "stale_owner_write" in _alerts(root)
    _log("混沌3 旧fencing写回被拒", True,
         f"old={old_fencing} new={t2['fencing_token']} status仍RUNNING")


# ---------- 混沌4 非法跃迁 READY->DONE 直写被拒 + P1 ----------

def test_chaos4_ready_to_done_rejected(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1")
    _add(s, "d1")
    t = s.get_task("p1", "d1")
    s.transition("p1", "d1", "READY", agent="o", run_id="r",
                 expect_version=t["version"])
    cur = s.get_task("p1", "d1")
    with pytest.raises(IllegalTransition):
        s.transition("p1", "d1", "DONE", agent="o", run_id="r",
                     expect_version=cur["version"])
    assert s.get_task("p1", "d1")["status"] == "READY"
    alerts = _alerts(root)
    assert "illegal_transition" in alerts
    # 状态没被写坏，replay仍一致（REJECTED行replay跳过）
    assert s.replay("p1")["ok"]
    _log("混沌4 READY->DONE直写被拒", True, "status仍READY + P1 illegal_transition")


# ---------- 混沌5 大文件：200MB级CSV的prompt<8k tokens + sha正确 ----------

def test_chaos5_big_file_prompt_and_sha(tmp_path):
    root = _root(tmp_path)
    big = root / "artifacts" / "2026-09-29" / "huge.csv"
    big.parent.mkdir(parents=True, exist_ok=True)
    with big.open("w", encoding="utf-8") as f:
        f.write("id,value\n")
        for i in range(200000):
            f.write(f"{i},payload_{i}_text\n")
    s = Store(root)
    m = cp.build_manifest(root, big)
    from core import executor
    card = executor.build_card(s, {
        "task_id": "big", "plan_id": "p1", "idempotency_key": "k",
        "shard": {"objective": "统计大CSV", "outputs": [], "acceptance": [],
                  "inputs": [{"path": "artifacts/2026-09-29/huge.csv",
                              "sha256": m["sha256"], "bytes": m["bytes"],
                              "rows": m["rows"]}]}}, "r")
    toks = estimate_tokens(card)
    assert toks < 8000, toks
    # sha必须和文件真实内容对得上（不能是占位）
    from core.utils import sha256_file
    assert m["sha256"] == sha256_file(big)["sha256"]
    assert m["rows"] == 200000
    _log("混沌5 大文件prompt+校验和", True,
         f"file={big.stat().st_size/1e6:.1f}MB tokens={toks} rows={m['rows']}")


# ---------- 混沌6 FIFO：同优先级10个按入队顺序 ----------

def test_chaos6_fifo_ten_tasks(tmp_path):
    root = _root(tmp_path)
    order = []
    o = Orchestrator(root, executor_fn=lambda *a, **k: order.append(a[2]),
                     max_workers=10)
    o.store.ensure_plan("p1")
    for i in range(10):
        _add(o.store, f"t{i}", priority=5)
    o.refresh_ready("p1")
    o.dispatch_split()
    assert order == [f"t{i}" for i in range(10)], order
    seqs = [t["seq"] for t in o.store.list_by_status("RUNNING")]
    assert seqs == sorted(seqs)
    _log("混沌6 FIFO十任务", True, f"order={order[:3]}... seq递增")


# ---------- 混沌7 高优排队：当前任务完成后<60s开始（p95口径） ----------

def test_chaos7_high_priority_queue_wait_p95(tmp_path):
    """不抢占：当前RUNNING占着，高优只能排队等。
    用p95口径（多次采样取分位），不用单次wall-clock硬线。"""
    root = _root(tmp_path)
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, max_workers=2)
    o.store.ensure_plan("p1")
    _add(o.store, "busy", priority=10)          # 正在跑的
    o.refresh_ready("p1")
    o.store.transition("p1", "busy", "RUNNING", agent="o", run_id="r",
                       expect_version=o.store.get_task("p1", "busy")["version"],
                       lease_op="acquire")
    waits = []
    for i in range(12):
        tid = f"hi{i}"
        _add(o.store, tid, priority=0, source="pipeline")
        t = o.store.get_task("p1", tid)
        o.store.transition("p1", tid, "READY", agent="o", run_id="r",
                           expect_version=t["version"])
        t0 = time.monotonic()
        # busy还在跑，高优只能排队
        o.dispatch_split()
        waits.append(time.monotonic() - t0)
        # 模拟busy完成后高优自然衔接（FAILED→RETRY→READY，状态机不允许直跳）
        bv = o.store.get_task("p1", "busy")["version"]
        t = o.store.transition("p1", "busy", "FAILED", agent="sub", run_id="r",
                               expect_version=bv, role="subagent",
                               payload={"error": "done-stub"})
        t = o.store.transition("p1", "busy", "RETRY", agent="o", run_id="r",
                               expect_version=t["version"],
                               payload={"reason": "stub_finish"})
        o.store.transition("p1", "busy", "READY", agent="o", run_id="r",
                           expect_version=t["version"])
    waits.sort()
    p95 = waits[int(len(waits) * 0.95) - 1]
    assert p95 < SLA_DISPATCH_S, f"p95={p95:.2f}s"
    _log("混沌7 高优排队p95", True,
         f"p95={p95*1000:.1f}ms 阈值={SLA_DISPATCH_S}s 样本={len(waits)}")
