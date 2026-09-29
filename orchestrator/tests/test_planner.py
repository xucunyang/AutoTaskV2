"""Phase2 §2b：planner拆分层——幂等/契约校验/质量审计/拆分侧session恢复。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import planner
from core.planner import SplitError, plan_id_for, plan_question, question_hash
from core.store import Store
from tests.test_orchestrator import _root

GOOD = {
    "plan_title": "新能源政策调研",
    "key_questions": ["2026补贴政策有哪些变化", "行业规模数据"],
    "tasks": [
        {"task_id": "t1_collect", "objective": "检索政策原文",
         "depends_on": [], "complexity": "simple", "needs_web": True,
         "freshness": "strict", "slice_rationale": "先拿一手政策原文",
         "outputs": ["artifacts/{date}/t1.md"]},
        {"task_id": "t2_analyze", "objective": "对比分析",
         "depends_on": ["t1_collect"], "complexity": "medium",
         "needs_web": False, "freshness": "none",
         "slice_rationale": "有原文才能对比，避免凭记忆",
         "outputs": ["artifacts/{date}/t2.md"]},
    ],
}
QUESTION = "帮我调研2026年新能源补贴政策"


def _split_fn(payload=GOOD):
    return lambda prompt: json.dumps(payload, ensure_ascii=False)


# ---------- 幂等（§2b：同一问题不重复拆） ----------

def test_same_question_gives_same_plan_id():
    assert plan_id_for(QUESTION) == plan_id_for("  " + QUESTION + " ")  # 首尾空白归一
    assert plan_id_for(QUESTION) != plan_id_for(QUESTION + "!")


def test_split_is_idempotent(tmp_path):
    s = Store(_root(tmp_path))
    r1 = plan_question(s, QUESTION, splitter_fn=_split_fn(), date="2026-09-29",
                       run_id="pr-1", model="qwen2.5-7b")
    r2 = plan_question(s, QUESTION, splitter_fn=_split_fn(), date="2026-09-29",
                       run_id="pr-2", model="qwen2.5-7b")
    assert r1["created"] is True and r1["tasks"] == 2
    assert r2["created"] is False and r2["tasks"] == 0
    assert r1["plan_id"] == r2["plan_id"]
    # 队列里不能出现两套同样的任务
    assert len(s.list_plan_tasks(r1["plan_id"])) == 2


# ---------- 契约校验 ----------

def test_missing_slice_rationale_is_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["tasks"][0].pop("slice_rationale")
    with pytest.raises(SplitError, match="no_slice_rationale"):
        planner.validate_split(bad, text=QUESTION, date="2026-09-29")


def test_needs_web_without_freshness_is_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["tasks"][0]["freshness"] = "none"
    with pytest.raises(SplitError, match="needs_web_without_freshness"):
        planner.validate_split(bad, text=QUESTION, date="2026-09-29")


def test_duplicate_task_id_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["tasks"][1]["task_id"] = "t1_collect"
    with pytest.raises(SplitError, match="duplicate_task_id"):
        planner.validate_split(bad, text=QUESTION, date="2026-09-29")


def test_self_dependency_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["tasks"][0]["depends_on"] = ["t1_collect"]
    with pytest.raises(SplitError, match="self_dependency"):
        planner.validate_split(bad, text=QUESTION, date="2026-09-29")


def test_no_tasks_rejected():
    with pytest.raises(SplitError, match="split_no_tasks"):
        planner.validate_split({"plan_title": "x", "key_questions": ["q"],
                                "tasks": []}, text=QUESTION, date="2026-09-29")


def test_no_key_questions_rejected():
    bad = {"plan_title": "x", "key_questions": [], "tasks": GOOD["tasks"]}
    with pytest.raises(SplitError, match="no_key_questions"):
        planner.validate_split(bad, text=QUESTION, date="2026-09-29")


def test_too_many_tasks_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["tasks"] = [dict(bad["tasks"][0], task_id=f"t{i}") for i in range(30)]
    bad["tasks"][0]["depends_on"] = []
    with pytest.raises(SplitError, match="too_many_tasks"):
        planner.validate_split(bad, text=QUESTION, date="2026-09-29", max_tasks=20)


def test_unparseable_splitter_output():
    with pytest.raises(SplitError, match="unparseable"):
        planner.split(QUESTION, splitter_fn=lambda p: "我觉得应该先调研再分析")


def test_split_requires_splitter():
    with pytest.raises(SplitError, match="splitter_required"):
        planner.split(QUESTION, splitter_fn=None)


def test_failed_split_alerts_p1(tmp_path):
    s = Store(_root(tmp_path))
    with pytest.raises(SplitError):
        plan_question(s, QUESTION, splitter_fn=lambda p: "垃圾输出",
                      date="2026-09-29")
    alerts = (tmp_path / "reports" / "alerts.jsonl").read_text(encoding="utf-8")
    assert "planner_split_failed" in alerts


# ---------- 落库与质量审计 ----------

def test_persisted_tasks_carry_split_metadata(tmp_path):
    s = Store(_root(tmp_path))
    r = plan_question(s, QUESTION, splitter_fn=_split_fn(), date="2026-09-29",
                      run_id="pr-1", model="qwen2.5-7b")
    tasks = {t["task_id"]: t for t in s.list_plan_tasks(r["plan_id"])}
    t1 = tasks["t1_collect"]
    assert t1["shard"]["slice_rationale"] == "先拿一手政策原文"
    assert t1["shard"]["key_questions"] == GOOD["key_questions"]
    assert t1["shard"]["needs_web"] == 1
    assert t1["shard"]["freshness"] == "strict"
    assert t1["shard"]["source"] == "pipeline"
    assert t1["shard"]["priority"] == 0
    # outputs里的{date}已展开
    assert t1["shard"]["outputs"] == ["artifacts/2026-09-29/t1.md"]
    assert t1["shard"]["acceptance"][0]["path"] == "artifacts/2026-09-29/t1.md"
    # 依赖进了shard，depends_on门禁才有效
    assert tasks["t2_analyze"]["shard"]["depends_on"] == ["t1_collect"]


def test_planner_audit_file_written(tmp_path):
    s = Store(_root(tmp_path))
    r = plan_question(s, QUESTION, splitter_fn=_split_fn(), date="2026-09-29",
                      run_id="pr-9", model="qwen2.5-7b")
    audit = json.loads(
        (tmp_path / "state" / "planner_runs" / f"{r['plan_id']}.json")
        .read_text(encoding="utf-8"))
    assert audit["planner_run_id"] == "pr-9"
    assert audit["planner_model"] == "qwen2.5-7b"
    assert audit["planner_prompt_version"] == planner.PROMPT_VERSION
    assert len(audit["tasks"]) == 2
    assert all(t["slice_rationale"] for t in audit["tasks"])


def test_decomposition_view_for_report(tmp_path):
    """附录C：报告的分解视图要有拆分依据和planner审计字段，
    否则复盘时无法判断是prompt问题还是模型问题。"""
    s = Store(_root(tmp_path))
    r = plan_question(s, QUESTION, splitter_fn=_split_fn(), date="2026-09-29",
                      run_id="pr-3", model="m1")
    view = planner.decomposition_view(s, r["plan_id"])
    assert view["planner_run_id"] == "pr-3"
    assert view["planner_model"] == "m1"
    assert len(view["slices"]) == 2
    assert view["missing_rationale"] == []
    assert view["key_questions"] == GOOD["key_questions"]


def test_decomposition_view_flags_missing_rationale(tmp_path):
    s = Store(_root(tmp_path))
    _, t = s.insert_task({"task_id": "manual", "plan_id": "pm", "status": "PENDING",
                          "idempotency_key": "k", "objective": "手写任务"})
    view = planner.decomposition_view(s, "pm")
    assert view["missing_rationale"] == ["manual"]


# ---------- 拆分侧session恢复 ----------

def test_split_checkpoint_saves_progress(tmp_path):
    root = _root(tmp_path)
    pid = plan_id_for(QUESTION)
    ck = planner.save_split_checkpoint(root, pid, 1, "剩余待拆：行业规模数据那一片")
    got = planner.load_split_checkpoint(root, pid)
    assert got is not None
    # done_steps是序号(int)：已拆出的卡在DB里，不需要在checkpoint里存id
    assert got.done_steps == [0]
    assert got.step == 1
    assert "剩余待拆" in got.cursor
    assert ck.exists()


def test_split_checkpoint_generation_increments(tmp_path):
    from core import checkpoint as cp
    root = _root(tmp_path)
    pid = plan_id_for(QUESTION)
    planner.save_split_checkpoint(root, pid, 1, "rest")
    first = planner.load_split_checkpoint(root, pid)
    nxt = cp.bump_generation(first, f"planner_{pid}", pid)
    cp.save(root, nxt)
    assert planner.load_split_checkpoint(root, pid).generation == 1
    assert planner.load_split_checkpoint(root, pid).done_steps == [0]


# ---------- inbox入口 ----------

def test_read_inbox_planner(tmp_path):
    s = Store(_root(tmp_path))
    d = tmp_path / "inbox" / "planner"
    d.mkdir(parents=True, exist_ok=True)
    (d / "q1.json").write_text(json.dumps({"type": "question",
                                           "text": QUESTION}, ensure_ascii=False),
                               encoding="utf-8")
    (d / "bad.json").write_text(json.dumps({"type": "nope"}), encoding="utf-8")
    (d / "broken.json").write_text("{坏", encoding="utf-8")
    got = planner.read_inbox_planner(s)
    assert [g["text"] for g in got] == [QUESTION]
    assert (d / "done" / "q1.json").exists()
    assert (d / "error" / "bad.json").exists()
    assert (d / "error" / "broken.json").exists()
    assert not list(d.glob("*.json"))


def test_plan_question_ends_up_dispatchable(tmp_path):
    """拆完的DAG要能真的被主循环推进：依赖门禁不能把它全挡住。"""
    from core.orchestrator import Orchestrator
    root = _root(tmp_path)
    s = Store(root)
    r = plan_question(s, QUESTION, splitter_fn=_split_fn(), date="2026-09-29",
                      run_id="pr-1", model="m")
    o = Orchestrator(root, executor_fn=lambda *a, **k: None)
    assert o.validate_dag(r["plan_id"]) == []
    n = o.refresh_ready(r["plan_id"])
    assert n == 1        # 只有t1无依赖能进READY
    assert s.get_task(r["plan_id"], "t2_analyze")["status"] == "PENDING"
