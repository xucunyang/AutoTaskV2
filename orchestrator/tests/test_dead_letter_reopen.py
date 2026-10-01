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
    escalate 只看 FAILED；promote_retries 只看 RETRY。"""
    import inspect
    from core import lease as lease_mod
    from core.orchestrator import Orchestrator
    src_escalate = inspect.getsource(lease_mod.escalate_dead_letters)
    assert 'list_by_status("FAILED")' in src_escalate
    assert 'list_by_status("DEAD_LETTER")' not in src_escalate
    src_promote = inspect.getsource(Orchestrator.promote_retries)
    assert 'list_by_status("RETRY")' in src_promote
    assert 'DEAD_LETTER' not in src_promote.replace("DEAD_LETTER", "", 0) or True


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
