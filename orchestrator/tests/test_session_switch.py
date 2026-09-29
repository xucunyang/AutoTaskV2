"""Phase2 DoD 硬骨头：Session切换全链路 + kill后续跑 + 200MB preview。

对应设计§6 DoD：
- kill子进程后从checkpoint续跑成功
- Session切换：水位线触发→让出→新session恢复，attempts不变，
  done_steps不重复；无checkpoint让出拒；熔断超限改FAILED
- 200MB CSV任务prompt长度<8k tokens（preview模式验证）
"""
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import checkpoint as cp
from core import executor
from core.executor import SessionYield, chat_with_yield_check, read_range, should_yield
from core.store import IllegalTransition, Store
from tests.test_orchestrator import _add, _root

REPO = Path(__file__).resolve().parents[1]


def _running(store, tid="t1", plan_id="p1", **over):
    t = _add(store, tid, plan_id, **over)
    t = store.transition(plan_id, tid, "READY", agent="o", run_id="r",
                         expect_version=t["version"])
    return store.transition(plan_id, tid, "RUNNING", agent="o", run_id="r",
                            expect_version=t["version"], lease_op="acquire")


def _long_running(store, tid, plan_id="p1"):
    from core.test_util import mark_long_running   # noqa: F401  (见下方定义)


# ---------- §6b 水位线判定 ----------

def test_should_yield_uses_dynamic_window():
    # 窗口final=10000，prompt=8000 → 正好80% → 让出
    assert should_yield({"prompt_tokens": 8000}, 10000)[0] is True
    assert should_yield({"prompt_tokens": 7999}, 10000)[0] is False
    # 同样的prompt，换个大窗口就不该让出——这就是"动态窗口"的意义
    assert should_yield({"prompt_tokens": 8000}, 65536)[0] is False


def test_should_yield_ignores_cumulative_tokens():
    """分子是**当前上下文**不是累计消耗。用累计值判会让第二个session必让出。"""
    over, _ = should_yield({"prompt_tokens": 100, "completion_tokens": 900000},
                           10000)
    assert over is False


def test_should_yield_handles_missing_window():
    assert should_yield({"prompt_tokens": 99999}, 0)[0] is False
    assert should_yield({}, 10000)[0] is False


def test_chat_with_yield_check_raises_context_full():
    class P:
        def chat(self, prompt, budget):
            return {"content": "x", "usage": {"prompt_tokens": 8500,
                                              "completion_tokens": 100}}
    with pytest.raises(SessionYield) as ei:
        chat_with_yield_check(P(), "prompt", {}, 10000)
    assert ei.value.reason == "context_full"


def test_chat_with_yield_check_passes_through():
    class P:
        def chat(self, prompt, budget):
            return {"content": "ok", "usage": {"prompt_tokens": 100}}
    out = chat_with_yield_check(P(), "prompt", {}, 10000)
    assert out["content"] == "ok"


# ---------- DoD：让出→新session恢复，attempts不变 ----------

def _mark_long_running(store, plan_id, task_id):
    import sqlite3
    t = store.get_task(plan_id, task_id)
    shard = dict(t["shard"])
    shard["long_running"] = True
    con = sqlite3.connect(str(store.db_path))
    try:
        con.execute("UPDATE tasks SET shard_json=? WHERE plan_id=? AND task_id=?",
                    (json.dumps(shard, ensure_ascii=False), plan_id, task_id))
        con.commit()
    finally:
        con.close()


def _yield_once(store, plan_id="p1", task_id="t1", reason="context_full"):
    t = store.get_task(plan_id, task_id)
    cp.save(store.root, cp.Checkpoint(task_id=task_id, plan_id=plan_id,
                                      step=2, cursor="row-200",
                                      done_steps=[1, 2]))
    return store.transition(plan_id, task_id, "READY", agent="sub", run_id="r",
                            expect_version=t["version"],
                            expect_fencing=t["fencing_token"],
                            payload={"reason": reason}, role="subagent")


def test_yield_does_not_increment_attempts(tmp_path):
    """DoD：让出是session切换不是失败，attempts必须不变。
    变了就会把长任务算成反复失败，最后误入死信。"""
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    t = _running(s)
    _mark_long_running(s, "p1", "t1")
    got = _yield_once(s)
    assert got["status"] == "READY"
    assert got["attempts"] == 0
    assert got["session_switch_total"] == 1


def test_resume_does_not_repeat_done_steps(tmp_path):
    """DoD：done_steps不重复执行——续跑时已完成步骤从清单里排除。"""
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    _running(s)
    _mark_long_running(s, "p1", "t1")
    _yield_once(s)
    ck = cp.load(tmp_path, "t1")
    payload = cp.resume_payload(ck)
    assert payload["done_steps"] == [1, 2]
    assert payload["cursor"] == "row-200"
    # 续跑侧：把已完成步骤与全部步骤求差，得到真正要做的
    all_steps = [1, 2, 3, 4]
    todo = [x for x in all_steps if x not in payload["done_steps"]]
    assert todo == [3, 4]


def test_second_session_increments_generation(tmp_path):
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    _running(s)
    _mark_long_running(s, "p1", "t1")
    _yield_once(s)
    ck = cp.load(tmp_path, "t1")
    nxt = cp.bump_generation(ck, "t1", "p1")
    cp.save(tmp_path, nxt)
    assert cp.load(tmp_path, "t1").generation == 1


def test_yield_without_checkpoint_is_rejected(tmp_path):
    """DoD：无checkpoint让出拒（让出=丢现场）。"""
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    t = _running(s)
    _mark_long_running(s, "p1", "t1")
    with pytest.raises(IllegalTransition, match="yield_without_checkpoint"):
        s.transition("p1", "t1", "READY", agent="sub", run_id="r",
                     expect_version=t["version"],
                     expect_fencing=t["fencing_token"],
                     payload={"reason": "context_full"}, role="subagent")
    assert s.get_task("p1", "t1")["status"] == "RUNNING"


def test_session_fuse_after_limit_goes_to_failed(tmp_path):
    """DoD：熔断超限改FAILED（计attempts走正常重试/死信）。"""
    s = Store(_root(tmp_path), session_switch_limit=2)
    s.ensure_plan("p1")
    _running(s)
    _mark_long_running(s, "p1", "t1")
    for _ in range(2):
        t = s.get_task("p1", "t1")
        if t["status"] == "READY":
            t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                             expect_version=t["version"], lease_op="acquire")
        _yield_once(s)
    t = s.get_task("p1", "t1")
    assert t["session_switch_total"] == 2
    t = s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    with pytest.raises(IllegalTransition, match="session_switch_fused"):
        _yield_once(s)
    # 熔断后改走FAILED
    failed = s.transition("p1", "t1", "FAILED", agent="o", run_id="r",
                          expect_version=s.get_task("p1", "t1")["version"],
                          payload={"error": "session_switch_fused"})
    assert failed["status"] == "FAILED"


def test_executor_yield_outcome_and_checkpoint(tmp_path):
    """执行器侧：SessionYield → 存checkpoint → RUNNING→READY，结局标签可观测。"""
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    t = _running(s)
    _mark_long_running(s, "p1", "t1")

    class P:
        def chat(self, prompt, budget):
            return {"content": "x", "usage": {"prompt_tokens": 99999}}
    # 有provider且窗口很小 → 水位线命中 → SessionYield
    got = executor.run_task(s, "p1", "t1", "run-1", owner="sub-x",
                            provider=P(), final_window=1000)
    assert got == "yielded:context_full"
    assert s.get_task("p1", "t1")["status"] == "READY"
    ck = cp.load(tmp_path, "t1")
    assert ck is not None and ck.task_id == "t1"
    assert any("context_full" in x for x in ck.summary)


def test_executor_yield_rejected_falls_back_to_failed(tmp_path):
    """让出被Store拒（短任务不让出/熔断）→ 改走FAILED，不能让异常逃出去。

    逃出去的后果：任务永久卡在RUNNING，只能等租约过期被回收，
    白等一个TTL，而且没人知道它为什么停在那。
    """
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    t = _running(s)          # 注意：**没有**标long_running → 短任务

    class P:
        def chat(self, prompt, budget):
            return {"content": "x", "usage": {"prompt_tokens": 99999}}
    got = executor.run_task(s, "p1", "t1", "run-1", owner="sub-x",
                            provider=P(), final_window=1000)
    assert got == "yield_rejected:context_full"
    assert s.get_task("p1", "t1")["status"] == "FAILED"
    payloads = [json.loads(e["payload"]) for e in s.recent_events("p1")]
    assert any("yield_rejected" in str(p.get("error", "")) for p in payloads)


def test_executor_failure_maps_to_failed(tmp_path):
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    _running(s)

    class Boom:
        def chat(self, prompt, budget):
            raise RuntimeError("model exploded")
    got = executor.run_task(s, "p1", "t1", "run-1", owner="sub-x",
                            provider=Boom(), final_window=1000)
    assert got.startswith("failed:")
    assert s.get_task("p1", "t1")["status"] == "FAILED"
    payloads = [json.loads(e["payload"]) for e in s.recent_events("p1")]
    assert any("model exploded" in str(p.get("error", "")) for p in payloads)


def test_executor_skips_terminal(tmp_path):
    s = Store(_root(tmp_path))
    s.ensure_plan("p1")
    _add(s, "done1")
    t = s.get_task("p1", "done1")
    t = s.transition("p1", "done1", "READY", agent="o", run_id="r",
                     expect_version=t["version"])
    t = s.transition("p1", "done1", "RUNNING", agent="o", run_id="r",
                     expect_version=t["version"], lease_op="acquire")
    t = s.transition("p1", "done1", "SUBMITTED", agent="sub", run_id="r",
                     expect_version=t["version"], role="subagent")
    t = s.transition("p1", "done1", "VERIFYING", agent="o", run_id="r",
                     expect_version=t["version"])
    s.transition("p1", "done1", "DONE", agent="v", run_id="r",
                 expect_version=t["version"], role="verifier")
    assert executor.run_task(s, "p1", "done1", "r") == "skipped_terminal"


# ---------- DoD：kill子进程后续跑 ----------

def test_resume_after_process_kill(tmp_path):
    """DoD：kill子进程后从checkpoint续跑成功。

    子进程跑到一半存checkpoint然后被kill；父进程重建Store后能读到
    checkpoint并据此续跑，且done_steps完整。
    """
    code = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(REPO)!r})
        from core.store import Store
        from core import checkpoint as cp
        root = Path(sys.argv[1])
        s = Store(root)
        s.ensure_plan("p1")
        t = s.insert_task({{"task_id": "kt", "plan_id": "p1",
                          "status": "PENDING", "idempotency_key": "k",
                          "objective": "long job",
                          "long_running": True}})[1]
        t = s.transition("p1", "kt", "READY", agent="o", run_id="r",
                         expect_version=t["version"])
        t = s.transition("p1", "kt", "RUNNING", agent="o", run_id="r",
                         expect_version=t["version"], lease_op="acquire")
        cp.save(root, cp.Checkpoint(task_id="kt", plan_id="p1", step=5,
                                    cursor="row-50000", done_steps=[1,2,3,4,5],
                                    artifacts_partial=[]))
        print("CKPT_SAVED", flush=True)
        time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    line = proc.stdout.readline()
    assert line.strip() == "CKPT_SAVED", line
    proc.kill()
    proc.wait(timeout=30)

    # 重启后：读回 checkpoint 并续跑
    s = Store(tmp_path)
    ck = cp.load(tmp_path, "kt")
    assert ck is not None
    assert ck.cursor == "row-50000"
    assert ck.done_steps == [1, 2, 3, 4, 5]
    payload = cp.resume_payload(ck)
    assert payload["done_steps"] == [1, 2, 3, 4, 5]
    # 让出后重派：store层仍能正常走（checkpoint已在）
    t = s.get_task("p1", "kt")
    got = s.transition("p1", "kt", "READY", agent="sub", run_id="r",
                       expect_version=t["version"],
                       expect_fencing=t["fencing_token"],
                       payload={"reason": "context_full"}, role="subagent")
    assert got["status"] == "READY"
    assert s.integrity_check()["integrity"] == "ok"


# ---------- DoD：200MB CSV 的 prompt < 8k tokens ----------

def test_big_csv_prompt_under_8k_tokens(tmp_path):
    """DoD：200MB CSV任务prompt长度<8k tokens（preview模式验证）。

    关键不是文件多大，而是**只喂manifest不喂全文**——prompt大小与文件大小
    无关。CI里不真造200MB（慢且占盘），用20万行（约7MB）验证机制；
    真到200MB时行为完全一样，因为进上下文的只有sha/bytes/rows/preview五行。
    """
    big = tmp_path / "artifacts" / "2026-09-29" / "huge.csv"
    big.parent.mkdir(parents=True, exist_ok=True)
    with big.open("w", encoding="utf-8") as f:
        f.write("id,value\n")
        for i in range(200000):
            f.write(f"{i},payload_{i}_some_text_here\n")
    assert big.stat().st_size > 5 * 1024 * 1024   # 确认确实是大文件

    from core import checkpoint as _cp
    from core.utils import sha256_file
    manifest = _cp.build_manifest(tmp_path, big)
    root = _root(tmp_path)          # 需要templates/才能渲染任务卡
    s = Store(root)
    from core.task_card import estimate_tokens
    card = executor.build_card(
        s,
        {"task_id": "t1", "plan_id": "p1", "idempotency_key": "k",
         "shard": {"objective": "统计这个大CSV", "outputs": [],
                   "acceptance": [], "budget": {"max_steps": 20, "timeout_s": 900},
                   "inputs": [{"path": "artifacts/2026-09-29/huge.csv",
                               "sha256": manifest["sha256"],
                               "bytes": manifest["bytes"],
                               "rows": manifest["rows"]}]}},
        "run-1")
    from core.task_card import estimate_tokens
    assert estimate_tokens(card) < 8000, estimate_tokens(card)
    # 且确实带上了行列数与哈希（可校验的事实），但没有正文
    assert str(manifest["rows"]) in card or manifest["bytes"] in card
    assert "payload_0_some_text_here" not in card


def test_read_range_pages_large_file(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1000)), encoding="utf-8")
    page = read_range(p, offset=100, limit=5)
    assert page == ["line100", "line101", "line102", "line103", "line104"]
    assert read_range(tmp_path / "nope.txt") == []


def test_self_test_covers_light_rules_only(tmp_path):
    s = Store(_root(tmp_path))
    p = tmp_path / "artifacts" / "2026-09-29" / "o.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("a\n1\n2\n3\n", encoding="utf-8")
    task = {"shard": {"acceptance": [
        {"type": "file_exists", "path": "artifacts/2026-09-29/o.csv",
         "rule_id": "r0"},
        {"type": "row_count", "path": "artifacts/2026-09-29/o.csv",
         "min": 2, "rule_id": "r1"},
        {"type": "python_test", "cmd": "pytest tests/x.py -q", "rule_id": "r2"}]}}
    res = executor.self_test(s, task)
    by = {r["rule_id"]: r for r in res}
    assert by["r0"]["ok"] is True
    assert by["r1"]["ok"] is True
    assert by["r2"]["ok"] is True
    assert by["r2"]["detail"] == "deferred_to_verifier"   # 重型留给Verifier


def test_self_test_reports_failure(tmp_path):
    s = Store(_root(tmp_path))
    task = {"shard": {"acceptance": [
        {"type": "file_exists", "path": "artifacts/2026-09-29/missing.csv",
         "rule_id": "r0"},
        {"type": "row_count", "path": "artifacts/2026-09-29/missing.csv",
         "min": 1, "rule_id": "r1"}]}}
    res = executor.self_test(s, task)
    assert all(r["ok"] is False for r in res)


def test_build_handoff_flags_failure(tmp_path):
    h = executor.build_handoff({}, [{"rule_id": "r0", "type": "file_exists",
                                     "ok": False, "detail": "missing"}], [])
    assert h.done is False and h.tests_passed is False
    h2 = executor.build_handoff({}, [{"rule_id": "r0", "type": "file_exists",
                                      "ok": True, "detail": ""}], ["a.csv"])
    assert h2.done is True and h2.artifact_refs == ["a.csv"]
