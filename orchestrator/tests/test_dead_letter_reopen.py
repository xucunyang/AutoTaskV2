"""死信重开（reopen）：显式、受审计、且真的能恢复。

背景：死信原本**没有任何出边**，于是死信无法恢复——唯一办法是手工改
state.db，而那会让 version 与 events 对不上，replay 一致性校验随即失败
（SOP §5 又明确禁止改库）。而 `scheduler --rerun` 做的正是
DEAD_LETTER→RETRY，所以它**对死信必然失败**——而死信正是它的主题。
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue, lease, scheduler                  # noqa: E402
from core.store import Store, StoreError                    # noqa: E402
from schemas.transitions import ALLOWED, TERMINAL, is_terminal  # noqa: E402

DATE = "2026-09-30"
PLAN = f"daily_report_{DATE}"


def _root(tmp_path):
    r = tmp_path / "proj"
    (r / "templates").mkdir(parents=True)
    for f in (ROOT / "templates").glob("*"):
        (r / "templates" / f.name).write_bytes(f.read_bytes())
    (r / "config").mkdir(exist_ok=True)
    for f in (ROOT / "config").glob("*"):
        (r / "config" / f.name).write_bytes(f.read_bytes())
    return r


def _dead_letter(root, task_id="t1", attempts=3, max_attempts=3):
    """造一个真死信：READY→RUNNING→FAILED→DEAD_LETTER，attempts 拉满。"""
    s = Store(root)
    s.ensure_plan(PLAN, template="daily")
    shard = enqueue.validate(json.dumps({
        "task_id": task_id, "plan_id": PLAN, "objective": "x",
        "outputs": [f"artifacts/{task_id}.md"],
    }, ensure_ascii=False).encode("utf-8"), root=root)
    _st, task = s.insert_task(shard)
    v = lambda: s.get_task(PLAN, task_id)["version"]      # noqa: E731
    f = lambda: s.get_task(PLAN, task_id)["fencing_token"]  # noqa: E731
    s.transition(PLAN, task_id, "READY", agent="o", run_id="r",
                 expect_version=v())
    s.transition(PLAN, task_id, "RUNNING", agent="o", run_id="r",
                 expect_version=v(), lease_op="acquire")
    s.transition(PLAN, task_id, "FAILED", agent="sub", run_id="r",
                 expect_version=v(), expect_fencing=f(),
                 payload={"error": "verify_failed", "reason": "verify_failed"})
    s._write_txn(lambda con: con.execute(
        "UPDATE tasks SET attempts=? WHERE plan_id=? AND task_id=?",
        (attempts, PLAN, task_id)))
    s.transition(PLAN, task_id, "DEAD_LETTER", agent="orchestrator", run_id="r",
                 expect_version=v(), expect_fencing=f(),
                 payload={"reason": "max_attempts_exceeded",
                          "last_error": "verify_failed",
                          "sop_ref": "docs/sop/dead-letter.md"})
    return s


# ---------------------------------------------------------------- 状态机

def test_dead_letter_has_exactly_one_out_edge():
    """死信只能被显式 reopen 带走，不能被任何自动流程顺手推进。"""
    assert ALLOWED["DEAD_LETTER"] == {"RETRY"}


def test_dead_letter_still_counts_as_terminal():
    """TERMINAL 的含义是"系统不会自动再处理"，不是"人也不能碰"。
    放开显式 reopen 不改变这个语义。"""
    assert "DEAD_LETTER" in TERMINAL
    assert is_terminal("DEAD_LETTER") is True


def test_no_automatic_path_reads_dead_letters():
    """没有任何自动流程会扫 DEAD_LETTER——这是 reopen 安全的前提。
    escalate 只看 FAILED/RETRY（两条失败路径，都得能收口到死信）；
    promote_retries 只看 RETRY。"""
    import inspect
    from core import lease as lease_mod
    from core.orchestrator import Orchestrator
    src_escalate = inspect.getsource(lease_mod.escalate_dead_letters)
    # 扫描集写在元组里（"FAILED", "RETRY"），两条失败路径都要收口到死信
    assert '("FAILED", "RETRY")' in src_escalate, \
        "验收失败走RETRY，只扫FAILED会让这类任务无限重试"
    assert 'list_by_status("DEAD_LETTER")' not in src_escalate
    src_promote = inspect.getsource(Orchestrator.promote_retries)
    assert 'list_by_status("RETRY")' in src_promote
    assert 'dead_letter_if_exhausted' in src_promote, \
        "promote_retries 自己要带闸，不能只依赖 housekeeping 的执行顺序"


def _exhausted_retry(root, task_id="t1", attempts=3, max_attempts=3):
    """造一个"验收一直失败"的任务：停在 **RETRY**（不是 FAILED）且 attempts 已满。

    这才是真实分布：验收失败走 VERIFYING→RETRY，压根不经过 FAILED，
    所以原来的 escalate（只扫 FAILED）永远看不见它。
    """
    s = Store(root)
    s.ensure_plan(PLAN, template="daily")
    shard = enqueue.validate(json.dumps({
        "task_id": task_id, "plan_id": PLAN, "objective": "x",
        "outputs": [f"artifacts/{task_id}.md"],
    }, ensure_ascii=False).encode("utf-8"), root=root)
    s.insert_task(shard)
    v = lambda: s.get_task(PLAN, task_id)["version"]      # noqa: E731
    f = lambda: s.get_task(PLAN, task_id)["fencing_token"]  # noqa: E731
    s.transition(PLAN, task_id, "READY", agent="o", run_id="r", expect_version=v())
    s.transition(PLAN, task_id, "RUNNING", agent="o", run_id="r",
                 expect_version=v(), lease_op="acquire")
    s.transition(PLAN, task_id, "SUBMITTED", agent="sub", run_id="r",
                 expect_version=v(), expect_fencing=f(),
                 handoff={"task_id": task_id, "status": "SUBMITTED",
                          "artifacts": [], "self_test": []})
    s.transition(PLAN, task_id, "VERIFYING", agent="orchestrator", run_id="r",
                 expect_version=v())
    s.transition(PLAN, task_id, "RETRY", agent="verifier", run_id="r",
                 expect_version=v(), role="verifier",
                 payload={"reason": "verify_failed",
                          "detail": "rule_2:no_claims_json; rule_3:no_sources_json"})
    s._write_txn(lambda con: con.execute(
        "UPDATE tasks SET attempts=?, max_attempts=? WHERE plan_id=? AND task_id=?",
        (attempts, max_attempts, PLAN, task_id)))
    return s


def test_escalate_picks_up_exhausted_retry_task(tmp_path):
    """验收失败的任务也必须能进死信——否则无限退避重跑且没人会去看它。"""
    r = _root(tmp_path)
    s = _exhausted_retry(r, attempts=3, max_attempts=3)
    assert s.get_task(PLAN, "t1")["status"] == "RETRY"
    out = lease.escalate_dead_letters(s, run_id="r1")
    assert [o["task_id"] for o in out] == ["t1"]
    assert s.get_task(PLAN, "t1")["status"] == "DEAD_LETTER"
    payload = next(json.loads(e["payload"]) for e in s.recent_events(PLAN)
                   if e["to_s"] == "DEAD_LETTER")
    assert payload["reason"] == "max_attempts_exceeded"
    assert payload["sop_ref"] and payload["last_error"]
    # last_error 要能看出是哪条规则挂了，而不是一个词 "verify_failed"
    assert "no_claims_json" in payload["last_error"]
    alerts = (r / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert '"priority": "P0"' in alerts and "dead_letter" in alerts


def test_escalate_leaves_retry_before_max_alone(tmp_path):
    r = _root(tmp_path)
    s = _exhausted_retry(r, attempts=1, max_attempts=3)
    assert lease.escalate_dead_letters(s) == []
    assert s.get_task(PLAN, "t1")["status"] == "RETRY"


def test_promote_retries_gates_exhausted_task(tmp_path):
    """promote_retries 自带闸：不依赖 housekeeping 跑在它前面。
    没有闸时它会把 attempts 已满的任务照常重排（实测跑到 7 次）。"""
    from core.orchestrator import Orchestrator
    r = _root(tmp_path)
    s = _exhausted_retry(r, attempts=3, max_attempts=3)
    o = Orchestrator(r, executor_fn=lambda *a, **k: None, run_once=True)
    assert o.promote_retries() == [], "重排了 attempts 已满的任务"
    assert s.get_task(PLAN, "t1")["status"] == "DEAD_LETTER"


def test_promote_retries_still_promotes_normal_task(tmp_path):
    """闸门不能误伤正常重试（attempts 未满的照常退避后重排）。"""
    from core.orchestrator import Orchestrator
    r = _root(tmp_path)
    s = _exhausted_retry(r, attempts=1, max_attempts=3)
    s._write_txn(lambda con: con.execute(
        "UPDATE tasks SET updated_at=? WHERE plan_id=? AND task_id=?",
        ("2026-01-01T00:00:00+00:00", PLAN, "t1")))
    o = Orchestrator(r, executor_fn=lambda *a, **k: None, run_once=True)
    assert o.promote_retries(), "正常任务没被重排"
    assert s.get_task(PLAN, "t1")["status"] == "READY"


# ------------------------------------------------- FAILED 出口（2026-10-04 加）

def _failed_task(root, task_id="t1", attempts=0, max_attempts=3):
    """造一个停在 **FAILED** 且 attempts 未满的任务（executor 抛异常的常态）。

    实测 2026-10-04：t3_hexagon 想往 /tmp 写临时脚本被 path_escapes_root 拒
    → RUNNING→FAILED a=0，从此没人管——refresh_ready 只捞 PENDING/BLOCKED、
    promote_retries 只捞 RETRY——t4 因依赖不满足一起停摆。
    """
    s = Store(root)
    s.ensure_plan(PLAN, template="daily")
    shard = enqueue.validate(json.dumps({
        "task_id": task_id, "plan_id": PLAN, "objective": "x",
        "outputs": [f"artifacts/{task_id}.md"],
    }, ensure_ascii=False).encode("utf-8"), root=root)
    s.insert_task(shard)
    v = lambda: s.get_task(PLAN, task_id)["version"]      # noqa: E731
    f = lambda: s.get_task(PLAN, task_id)["fencing_token"]  # noqa: E731
    s.transition(PLAN, task_id, "READY", agent="o", run_id="r", expect_version=v())
    s.transition(PLAN, task_id, "RUNNING", agent="o", run_id="r",
                 expect_version=v(), lease_op="acquire")
    s.transition(PLAN, task_id, "FAILED", agent="sub", run_id="r",
                 expect_version=v(), expect_fencing=f(),
                 payload={"error": "path_escapes_root:/tmp/check_t3.py",
                          "reason": "exec_error"})
    s._write_txn(lambda con: con.execute(
        "UPDATE tasks SET attempts=?, max_attempts=? WHERE plan_id=? AND task_id=?",
        (attempts, max_attempts, PLAN, task_id)))
    return s


def test_failed_task_is_promoted_to_retry(tmp_path):
    """FAILED 没有自动出边 = 永久卡死，必须接进 RETRY 的统一通道。

    attempts 只在 RETRY→READY 时 +1，所以 a=0 的 FAILED 连
    attempts>=max_attempts 的死信判据都够不到：既不重试、也进不了死信，
    唯一出路是手工改库（SOP §5 明令禁止，会让 version 与 events 对不上）。
    """
    from core.orchestrator import Orchestrator
    r = _root(tmp_path)
    s = _failed_task(r, attempts=0)
    o = Orchestrator(r, executor_fn=lambda *a, **k: None, run_once=True)
    out = o.promote_failed()
    assert [t["task_id"] for t in out] == ["t1"]
    t = s.get_task(PLAN, "t1")
    assert t["status"] == "RETRY"
    payload = next(json.loads(e["payload"]) for e in s.recent_events(PLAN)
                   if e["to_s"] == "RETRY")
    assert payload["reason"] == "failed_retry"        # store 强制要求 reason
    assert "path_escapes_root" in payload["detail"]   # 要能看见原本错在哪
    assert s.replay(PLAN)["ok"], "走正规事件流，重放必须仍一致"


def test_failed_task_at_max_attempts_left_for_escalate(tmp_path):
    """attempts 已满的不能被 promote_failed 转走——那会绕开死信判据。"""
    from core.orchestrator import Orchestrator
    r = _root(tmp_path)
    s = _failed_task(r, attempts=3, max_attempts=3)
    o = Orchestrator(r, executor_fn=lambda *a, **k: None, run_once=True)
    assert o.promote_failed() == [], "重排了 attempts 已满的任务"
    assert s.get_task(PLAN, "t1")["status"] == "FAILED"
    # 它的出口是 escalate（housekeeping 每轮先跑）
    assert [t["task_id"] for t in lease.escalate_dead_letters(s, run_id="r")] == ["t1"]
    assert s.get_task(PLAN, "t1")["status"] == "DEAD_LETTER"


def test_tick_wires_promote_failed(tmp_path):
    """光有方法不算数，主循环得真的调它（verify 池那次教训还热着）。"""
    from core.orchestrator import Orchestrator
    r = _root(tmp_path)
    s = _failed_task(r, attempts=0)
    o = Orchestrator(r, executor_fn=lambda *a, **k: None, run_once=True)
    stats = o.tick()
    assert stats.get("failed_promoted") == 1, "tick 没调 promote_failed"
    assert s.get_task(PLAN, "t1")["status"] == "RETRY"


# ---------------------------------------------------------------- reopen

def test_reopen_moves_to_retry(tmp_path):
    s = _dead_letter(_root(tmp_path))
    assert s.get_task(PLAN, "t1")["status"] == "DEAD_LETTER"
    got = s.reopen_dead_letter(PLAN, "t1", reason="已补产物")
    assert got["status"] == "RETRY"


def test_reopen_resets_attempts(tmp_path):
    """必须重置 attempts：escalate 的判据是 attempts >= max_attempts，
    而 RETRY→READY 每次还会 +1。不重置的话重开等于白开——
    跑一次、失败、立刻又升级成死信。"""
    s = _dead_letter(_root(tmp_path))
    assert s.get_task(PLAN, "t1")["attempts"] == 3
    got = s.reopen_dead_letter(PLAN, "t1", reason="修好了")
    assert got["attempts"] == 0, f"attempts未重置: {got['attempts']}"


def test_reopened_task_can_run_again(tmp_path):
    """端到端：重开 → 退避重排 → 跑通 → 不再死信。"""
    root = _root(tmp_path)
    s = _dead_letter(root)
    s.reopen_dead_letter(PLAN, "t1", reason="上游已修")
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    s._write_txn(lambda con: con.execute(
        "UPDATE tasks SET updated_at=? WHERE plan_id=? AND task_id='t1'",
        (old, PLAN)))
    from core.orchestrator import Orchestrator
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.promote_retries()
    assert s.get_task(PLAN, "t1")["status"] == "READY", "重开后没能重排"
    # 再跑一次并成功
    v = s.get_task(PLAN, "t1")["version"]
    s.transition(PLAN, "t1", "RUNNING", agent="o", run_id="r2",
                 expect_version=v, lease_op="acquire")
    s.transition(PLAN, "t1", "SUBMITTED", agent="sub", run_id="r2",
                 expect_version=s.get_task(PLAN, "t1")["version"],
                 expect_fencing=s.get_task(PLAN, "t1")["fencing_token"],
                 handoff={"task_id": "t1", "status": "SUBMITTED",
                          "artifacts": [], "self_test": []})
    s.transition(PLAN, "t1", "VERIFYING", agent="orchestrator", run_id="r2",
                 expect_version=s.get_task(PLAN, "t1")["version"])
    s.transition(PLAN, "t1", "DONE", agent="verifier", run_id="r2",
                 expect_version=s.get_task(PLAN, "t1")["version"],
                 expect_fencing=s.get_task(PLAN, "t1")["fencing_token"],
                 payload={"rules": 0, "failed": []})
    assert s.get_task(PLAN, "t1")["status"] == "DONE"
    # 再升级也不会直接死信（attempts已重置）
    lease.escalate_dead_letters(s, run_id="r3")
    assert s.get_task(PLAN, "t1")["status"] == "DONE"


def test_reopen_requires_reason(tmp_path):
    """无理由的重开等于"我看着办重试"，而死信是系统主动放弃的结果。"""
    s = _dead_letter(_root(tmp_path))
    with pytest.raises(StoreError, match="reopen_requires_reason"):
        s.reopen_dead_letter(PLAN, "t1", reason="")


def test_reopen_rejects_non_dead_letter(tmp_path):
    s = _dead_letter(_root(tmp_path))
    s.reopen_dead_letter(PLAN, "t1", reason="x")
    with pytest.raises(StoreError, match="not_dead_letter"):
        s.reopen_dead_letter(PLAN, "t1", reason="再来一次")


def test_reopen_missing_task(tmp_path):
    s = _dead_letter(_root(tmp_path))
    with pytest.raises(StoreError, match="task_not_found"):
        s.reopen_dead_letter(PLAN, "nope", reason="x")


def test_reopen_writes_audit_event(tmp_path):
    """重开必须留事件：否则事后查不到"谁在什么时候重开过"。"""
    s = _dead_letter(_root(tmp_path))
    s.reopen_dead_letter(PLAN, "t1", reason="已补产物", operator="alice")
    row = s._connect().execute(
        "SELECT agent,payload FROM events WHERE plan_id=? AND to_s='RETRY'"
        " ORDER BY rowid DESC LIMIT 1", (PLAN,)).fetchone()
    assert row is not None
    assert row[0] == "alice"
    p = json.loads(row[1])
    assert p["reopened"] is True
    assert p["operator"] == "alice"
    assert p["attempts_reset_from"] == 3
    assert p["reason"] == "已补产物"


def test_reopen_emits_p1_alert(tmp_path):
    """死信是系统主动放弃的结果，人工重开必须可见。

    告警落在 **store.root** 下（tmp 根），不是仓库根——读错地方会
    误以为"没告警"，而实际上只是看错了文件。
    """
    root = _root(tmp_path)
    s = _dead_letter(root)
    s.reopen_dead_letter(PLAN, "t1", reason="x")
    p = root / "reports" / "alerts.jsonl"
    assert p.exists(), f"没有告警文件: {list((root / 'reports').glob('*'))}"
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]
    got = [r for r in rows if r.get("kind") == "dead_letter_reopened"]
    assert got, "重开没有告警——人工推翻系统的放弃决定不该悄无声息"


def test_replay_stays_consistent_after_reopen(tmp_path):
    """reopen 走的是正规事件流，所以 replay 仍应一致。
    这正是不必改库的原因。"""
    s = _dead_letter(_root(tmp_path))
    s.reopen_dead_letter(PLAN, "t1", reason="x")
    rep = s.replay(PLAN)
    assert rep["ok"], f"重开后重放不一致: {rep.get('mismatches')}"


# ---------------------------------------------------------------- CLI

def test_scheduler_rerun_reopens_dead_letter(tmp_path):
    """CLI 那条路必须真的能用——SOP 就是让照着它跑的人用的。"""
    root = _root(tmp_path)
    _dead_letter(root)
    rc = scheduler.main(["--root", str(root), "--date", DATE,
                         "--rerun", "t1", "--reason", "已补产物"])
    assert rc == 0
    s = Store(root)
    t = s.get_task(PLAN, "t1")
    assert t["status"] == "RETRY"
    assert t["attempts"] == 0


def test_scheduler_rerun_still_works_for_failed(tmp_path):
    """FAILED→RETRY 本来就合法，不能被新分支破坏。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan(PLAN, template="daily")
    shard = enqueue.validate(json.dumps({
        "task_id": "t2", "plan_id": PLAN, "objective": "x",
        "outputs": ["artifacts/t2.md"]}, ensure_ascii=False).encode("utf-8"),
        root=root)
    _st, task = s.insert_task(shard)
    s.transition(PLAN, "t2", "READY", agent="o", run_id="r",
                 expect_version=task["version"])
    s.transition(PLAN, "t2", "RUNNING", agent="o", run_id="r",
                 expect_version=s.get_task(PLAN, "t2")["version"],
                 lease_op="acquire")
    s.transition(PLAN, "t2", "FAILED", agent="sub", run_id="r",
                 expect_version=s.get_task(PLAN, "t2")["version"],
                 expect_fencing=s.get_task(PLAN, "t2")["fencing_token"],
                 payload={"error": "x", "reason": "verify_failed"})
    rc = scheduler.main(["--root", str(root), "--date", DATE, "--rerun", "t2"])
    assert rc == 0
    assert s.get_task(PLAN, "t2")["status"] == "RETRY"


def test_scheduler_rerun_unknown_task(tmp_path):
    root = _root(tmp_path)
    _dead_letter(root)
    assert scheduler.main(["--root", str(root), "--date", DATE,
                           "--rerun", "ghost"]) == 2
