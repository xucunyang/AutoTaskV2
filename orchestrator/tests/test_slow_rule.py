"""2.6 慢 rule：整轮验收的墙钟预算。

问题：每条 rule 各有 timeout_s，但**没有整轮上限**。一个任务声明
6条会挂的 python_test 就是 6×timeout 的占用，而 verify worker 被占住，
后面所有任务排队。更糟的是 timeout_s 由**任务自己配**，不封顶就能让
一个任务占住 worker 一小时。

注意 CMD_WHITELIST 只放行 "pytest tests/" / "python tests/" /
"python -m pytest tests/" —— 连 `python -c` 都不行。所以慢rule/快rule
都必须是 tests/ 下的真脚本，且子进程的 cwd 是 ctx["repo"]，
所以脚本必须写在**各自的临时根**里，不能写进真实仓库。
"""
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue                                    # noqa: E402
from core import verifier as vf                             # noqa: E402
from core import verify_orchestrator as vmod                # noqa: E402
from core.store import Store                                # noqa: E402


def _ctx(root: Path) -> dict:
    """在 root/tests/ 下放真脚本，并返回以 root 为 repo 的 ctx。"""
    d = root / "tests"
    d.mkdir(parents=True, exist_ok=True)
    (d / "zz_slow.py").write_text("import time\ntime.sleep(30)\n",
                                 encoding="utf-8")
    (d / "zz_fast.py").write_text("print('ok')\n", encoding="utf-8")
    return {"repo": root}


SLOW = {"type": "python_test", "cmd": "python tests/zz_slow.py",
        "flaky_retry": 0}
FAST = {"type": "python_test", "cmd": "python tests/zz_fast.py",
        "flaky_retry": 0}


def _n(rule, i):
    return {**rule, "rule_id": f"rule_{i}"}


# ---------------------------------------------------------------- 整轮预算

def test_budget_stops_the_round(tmp_path):
    """预算耗尽后不再跑后面的rule（而不是跑完6条×30s）。"""
    rules = [_n(SLOW, i) for i in range(4)]
    t0 = time.monotonic()
    out = vf.run_rules(_ctx(tmp_path), rules, budget_s=2.0)
    dt = time.monotonic() - t0
    assert dt < 120, f"整轮跑了{dt:.0f}s，预算没起作用"
    deferred = [r for r in out if r.get("not_evaluated")]
    assert deferred, "没有rule被标记为未评估"
    assert all("verify_budget_exceeded" in r["detail"] for r in deferred)


def test_budget_exhausted_is_not_a_pass_and_not_a_failure(tmp_path):
    """not_evaluated 既不是失败也不是通过。

    算失败 → 任务进RETRY，下一轮重跑同样的慢rule直到死信；
    算通过 → 进 verified_rules 账本，等于"没验说验过了"。
    两条路都会制造比原问题更难查的故障。
    """
    rules = [_n(SLOW, i) for i in range(3)]
    out = vf.run_rules(_ctx(tmp_path), rules, budget_s=2.0)
    deferred = [r for r in out if r.get("not_evaluated")]
    assert deferred
    for r in deferred:
        assert r["ok"] is False
        assert r["not_evaluated"] is True
        # 不能混入已通过集合
        assert r["rule_id"] not in {x["rule_id"] for x in out
                                    if x["ok"] and not x.get("not_evaluated")}


def test_evaluated_rules_before_budget_are_normal(tmp_path):
    """预算内的rule照常评估。"""
    rules = [_n(FAST, i) for i in range(3)]
    out = vf.run_rules(_ctx(tmp_path), rules, budget_s=60)
    assert all(r["ok"] for r in out), [r["detail"] for r in out]
    assert not any(r.get("not_evaluated") for r in out)


def test_skipped_rules_do_not_consume_budget(tmp_path):
    """已验证过的rule走skip，不该占预算。"""
    rules = [_n(FAST, 0), _n(FAST, 1)]
    out = vf.run_rules(_ctx(tmp_path), rules, skip={"rule_0"}, budget_s=60)
    s0 = [r for r in out if r["rule_id"] == "rule_0"][0]
    assert s0["skipped"] is True
    assert not any(r.get("not_evaluated") for r in out)


# ---------------------------------------------------------------- 超时封顶

def test_rule_timeout_is_capped(tmp_path):
    """任务的 timeout_s 不能突破上限。"""
    import core.verifier as v
    orig = v.subprocess.run
    seen = {}

    class R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def spy(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        return R()

    v.subprocess.run = spy
    try:
        v.run_rules(_ctx(tmp_path),
                    [{"type": "python_test", "cmd": "python tests/zz_fast.py",
                      "timeout_s": 9999, "flaky_retry": 0}], budget_s=60)
    finally:
        v.subprocess.run = orig
    assert seen.get("timeout") == v.RULE_TIMEOUT_CAP_S, \
        f"timeout未封顶: {seen.get('timeout')}（任务可配任意大）"


def test_no_timeout_in_rule_uses_cap(tmp_path):
    import core.verifier as v
    orig = v.subprocess.run
    seen = {}

    class R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def spy(cmd, **kw):
        seen["timeout"] = kw.get("timeout")
        return R()

    v.subprocess.run = spy
    try:
        v.run_rules(_ctx(tmp_path),
                    [{"type": "python_test", "cmd": "python tests/zz_fast.py",
                      "flaky_retry": 0}], budget_s=60)
    finally:
        v.subprocess.run = orig
    assert seen.get("timeout") == v.RULE_TIMEOUT_CAP_S


# ---------------------------------------------------------------- 端到端

def _submitted(tmp_path, n_rules):
    """按生产布局搭：repo/orchestrator。

    make_ctx 里 `repo = store.root.parent`——生产的编排器根就是
    repo/orchestrator。测试里若把 store 建在 tmp 根上，repo 会指到
    pytest 的临时父目录，子进程就找不到 tests/ 下的脚本（表现为
    rc=2 file not found，而不是"慢"）。
    """
    repo = Path(tmp_path)
    root = repo / "orchestrator"
    (root / "templates").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "templates").glob("*"):
        shutil.copy2(f, root / "templates" / f.name)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "zz_slow.py").write_text(
        "import time\ntime.sleep(20)\n", encoding="utf-8")
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    acc = [{"type": "python_test", "cmd": "python tests/zz_slow.py",
            "flaky_retry": 0} for _ in range(n_rules)]
    shard = enqueue.validate(json.dumps({
        "task_id": "t1", "plan_id": "p1", "objective": "x",
        "outputs": ["artifacts/t1.md"], "acceptance": acc,
        "budget": {"max_steps": 2, "timeout_s": 30},
    }, ensure_ascii=False).encode("utf-8"), root=root)
    _st, task = s.insert_task(shard)
    s.transition("p1", "t1", "READY", agent="o", run_id="r",
                 expect_version=task["version"])
    s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                 expect_version=s.get_task("p1", "t1")["version"],
                 lease_op="acquire")
    s.transition("p1", "t1", "SUBMITTED", agent="sub", run_id="r",
                 expect_version=s.get_task("p1", "t1")["version"],
                 expect_fencing=s.get_task("p1", "t1")["fencing_token"],
                 handoff={"task_id": "t1", "status": "SUBMITTED",
                          "artifacts": [], "self_test": []})
    # 进VERIFYING不传role：与 orchestrator.dispatch_verify_async 一致
    # （role="verifier" 会撞上 system_only:VERIFYING）
    s.transition("p1", "t1", "VERIFYING", agent="orchestrator", run_id="r",
                 expect_version=s.get_task("p1", "t1")["version"])
    return s, root


def test_slow_rules_never_reach_DONE(tmp_path, monkeypatch):
    """慢rule没跑完 → 任务绝不能被标DONE。

    这是"慢rule拖死整轮"最严重的后果：验收没做完，任务却显示成功。
    """
    s, root = _submitted(tmp_path, 5)
    orig = vf.run_rules
    monkeypatch.setattr(vmod.vf, "run_rules",
                        lambda ctx, rules, skip=None, budget_s=None:
                        orig(ctx, rules, skip=skip, budget_s=0.01))
    vmod.verify(s, "p1", "t1", "r")
    cur = s.get_task("p1", "t1")
    assert cur["status"] != "DONE", \
        f"验收被预算打断，任务却到了DONE（status={cur['status']}）"
    assert cur["status"] == "RETRY", f"期望RETRY，实际{cur['status']}"


def test_retry_carries_reason_and_deferred_rules(tmp_path, monkeypatch):
    """走RETRY必须带reason（Phase0§2.3），并记录哪些rule被推迟。"""
    s, root = _submitted(tmp_path, 4)
    orig = vf.run_rules
    monkeypatch.setattr(vmod.vf, "run_rules",
                        lambda ctx, rules, skip=None, budget_s=None:
                        orig(ctx, rules, skip=skip, budget_s=0.01))
    vmod.verify(s, "p1", "t1", "r")
    ev = s._connect().execute(
        "SELECT payload FROM events WHERE plan_id='p1' AND to_s='RETRY'"
        " ORDER BY rowid DESC LIMIT 1").fetchone()
    payload = json.loads(ev[0]) if ev and ev[0] else {}
    assert payload.get("reason"), f"RETRY没带reason: {payload}"
    assert payload.get("deferred_rules"), f"没记录被推迟的rule: {payload}"


def test_deferred_rules_not_recorded_as_verified(tmp_path, monkeypatch):
    """没跑到的rule不能进 verified_rules，否则下一轮会被跳过。

    注意第一条rule是**真跑过并通过**的（它耗时20s，预算在它之后才耗尽），
    所以它理应进账本。要断言的是"被推迟的那几条不在账本里"。
    """
    s, root = _submitted(tmp_path, 4)
    orig = vf.run_rules
    monkeypatch.setattr(vmod.vf, "run_rules",
                        lambda ctx, rules, skip=None, budget_s=None:
                        orig(ctx, rules, skip=skip, budget_s=0.01))
    vmod.verify(s, "p1", "t1", "r")
    prog = s.get_task("p1", "t1")["verify_progress"] or {}
    verified = set(prog.get("verified_rules") or [])
    deferred = {r["rule_id"] for r in (prog.get("last_results") or [])
                if "verify_budget_exceeded" in str(r.get("detail"))}
    assert deferred, f"没有rule被推迟: {prog.get('last_results')}"
    assert not (deferred & verified), \
        f"未评估的rule被记成已验证: {sorted(deferred & verified)}"


def test_budget_exceeded_emits_p2_alert(tmp_path, monkeypatch):
    """预算耗尽要留告警痕迹（P2），否则"任务反复RETRY"看不出原因。"""
    s, root = _submitted(tmp_path, 3)
    orig = vf.run_rules
    monkeypatch.setattr(vmod.vf, "run_rules",
                        lambda ctx, rules, skip=None, budget_s=None:
                        orig(ctx, rules, skip=skip, budget_s=0.01))
    vmod.verify(s, "p1", "t1", "r")
    # 告警落在 store.root 下（= repo/orchestrator），不是 repo 下
    p = root / "reports" / "alerts.jsonl"
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if p.exists() else []
    kinds = {r.get("kind") for r in rows}
    assert "verify_budget_exceeded" in kinds, f"没有预算告警: {kinds}"
