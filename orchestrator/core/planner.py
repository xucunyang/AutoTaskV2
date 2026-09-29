"""拆分层（Phase2 §2b）：笼统问题 → 任务卡DAG。

三条设计要点：
1. **同一问题不重复拆**：plan_id由问题内容的hash决定，重复投递返回同一个plan。
   否则用户重发一次就多一套任务，队列被自己灌爆。
2. **拆分侧自己也会session切换**：一个问题拆10~20张卡，planner同样是长任务，
   超80%就存拆分checkpoint（已拆出前N张+剩余问题）继续拆。
3. **质量可审计**：planner_run_id/model/prompt_version落plan，
   报告的"任务分解视图"靠这三个字段复盘拆分质量，prompt调优才有依据。

LLM可注入（splitter_fn），生产接网关，测试用假实现——拆分层不绑死任何provider。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from core import checkpoint as cp
from core.store import Conflict, IllegalTransition, Store
from core.utils import atomic_write_json, jlog, now_utc_iso
from schemas.models import Checkpoint, TaskShard

PROMPT_VERSION = "planner-v1"


class SplitError(Exception):
    """拆分失败：进DEAD_LETTER + P1告警（§2b）。"""


def question_hash(text: str) -> str:
    """问题内容的全hash。idempotency的来源。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def plan_id_for(text: str) -> str:
    return f"adhoc_{question_hash(text)[:16]}"


def build_prompt(text: str, max_tasks: int = 20) -> str:
    return (
        "把下面这个问题拆成可独立执行的任务卡DAG。只输出JSON，结构：\n"
        '{"plan_title":"","key_questions":[""],"tasks":[{"task_id":"t1_xxx",'
        '"objective":"","depends_on":[],"complexity":"simple|medium|complex",'
        '"needs_web":true,"freshness":"none|recent|strict",'
        '"slice_rationale":"为什么切这一片","outputs":["artifacts/{date}/t1_x.md"]}]}\n'
        f"最多{max_tasks}张卡。needs_web=true必须带freshness(recent/strict)。\n"
        "每个切片必须写slice_rationale——报告的分解视图靠它判断拆分质量。\n"
        f"问题：{text}\n"
    )


def validate_split(raw: dict, *, text: str, date: str,
                   max_tasks: int = 20) -> dict:
    """校验LLM输出。结构不对就抛SplitError，不做"尽力修复"——
    拆错的DAG比不拆更糟（会派发错误任务）。"""
    problems = []
    if not isinstance(raw, dict):
        raise SplitError("split_not_object")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SplitError("split_no_tasks")
    if len(tasks) > max_tasks:
        problems.append(f"too_many_tasks:{len(tasks)}")
    kq = raw.get("key_questions") or []
    if not isinstance(kq, list) or not kq:
        problems.append("no_key_questions")
    seen = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            problems.append(f"task{i}_not_object")
            continue
        tid = str(t.get("task_id", ""))
        if not tid:
            problems.append(f"task{i}_no_id")
        elif tid in seen:
            problems.append(f"duplicate_task_id:{tid}")
        seen.add(tid)
        if not t.get("objective"):
            problems.append(f"{tid}_no_objective")
        if not t.get("slice_rationale"):
            # 缺拆分依据=无法复盘拆分质量，设计要求必填
            problems.append(f"{tid}_no_slice_rationale")
        if t.get("needs_web") and t.get("freshness", "none") == "none":
            problems.append(f"{tid}_needs_web_without_freshness")
        for dep in t.get("depends_on") or []:
            if dep == tid:
                problems.append(f"{tid}_self_dependency")
    if problems:
        raise SplitError(";".join(problems[:8]))
    return {"plan_title": str(raw.get("plan_title") or text[:60]),
            "key_questions": [str(q) for q in kq],
            "tasks": tasks}


def split(text: str, *, splitter_fn=None, max_tasks: int = 20) -> dict:
    """调LLM拆分并校验。splitter_fn(prompt) -> dict|str。"""
    if splitter_fn is None:
        raise SplitError("splitter_required")
    out = splitter_fn(build_prompt(text, max_tasks=max_tasks))
    if isinstance(out, str):
        try:
            s, e = out.find("{"), out.rfind("}")
            out = json.loads(out[s:e + 1]) if s >= 0 and e > s else None
        except ValueError:
            out = None
    if out is None:
        raise SplitError("splitter_output_unparseable")
    return out


def persist(store: Store, text: str, spec: dict, *, date: str,
            run_id: str = "", model: str = "", prompt_version: str = PROMPT_VERSION,
            priority: int = 0, agent: str = "planner") -> dict:
    """单事务写入plan+tasks(PENDING)。幂等：plan已存在直接返回不重拆。"""
    pid = plan_id_for(text)
    audit = json.dumps({"planner_run_id": run_id, "planner_model": model,
                        "planner_prompt_version": prompt_version,
                        "plan_title": spec["plan_title"],
                        "key_questions": spec["key_questions"]},
                       ensure_ascii=False)
    if not store.ensure_plan(pid, template="planner"):
        return {"plan_id": pid, "created": False, "tasks": 0,
                "key_questions": spec["key_questions"],
                "planner_run_id": run_id}
    n = 0
    for t in spec["tasks"]:
        tid = str(t["task_id"])
        shard = {
            "task_id": tid,
            "plan_id": pid,
            "status": "PENDING",
            "idempotency_key": f"{pid}/{tid}/{question_hash(text)[:8]}",
            "inputs_hash": question_hash(text),
            "objective": str(t.get("objective", "")),
            "depends_on": [str(d) for d in t.get("depends_on") or []],
            "outputs": [str(o).replace("{date}", date)
                        for o in t.get("outputs") or []],
            "complexity": t.get("complexity", "simple"),
            "needs_web": bool(t.get("needs_web", False)),
            "freshness": t.get("freshness", "none"),
            "key_questions": spec["key_questions"],
            "slice_rationale": str(t.get("slice_rationale", "")),
            "long_running": bool(t.get("long_running", False)),
            "priority": int(t.get("priority", priority)),
            "source": "pipeline",
        }
        shard.update(_acceptance_of(t, date))
        try:
            TaskShard(**shard)          # 契约层最后一道闸
            store.insert_task(shard)
            n += 1
        except Exception as e:
            jlog(store.root, "WARN", "planner_task_rejected", task_id=tid,
                 error=str(e)[:200])
    store.record_schema_reject(pid, pid, "planner_audit", "plan_created",
                               payload={"tasks": n})
    atomic_write_json(store.root / "state" / "planner_runs" / f"{pid}.json", {
        "plan_id": pid, "planner_run_id": run_id, "planner_model": model,
        "planner_prompt_version": prompt_version,
        "key_questions": spec["key_questions"],
        "plan_title": spec["plan_title"],
        "tasks": [{"task_id": str(t["task_id"]),
                   "slice_rationale": str(t.get("slice_rationale", "")),
                   "depends_on": t.get("depends_on") or []} for t in spec["tasks"]],
        "created_at": now_utc_iso(),
    })
    jlog(store.root, "INFO", "plan_split", plan_id=pid, tasks=n,
         planner_model=model, prompt_version=prompt_version)
    return {"plan_id": pid, "created": True, "tasks": n,
            "key_questions": spec["key_questions"],
            "planner_run_id": run_id, "planner_model": model,
            "planner_prompt_version": prompt_version}


def _acceptance_of(t: dict, date: str) -> dict:
    acc = []
    for o in t.get("outputs") or []:
        acc.append({"type": "file_exists", "path": str(o).replace("{date}", date),
                    "rule_id": f"rule_{len(acc)}"})
    return {"acceptance": acc}


def plan_question(store: Store, text: str, *, splitter_fn=None, date: str,
                  run_id: str = "", model: str = "",
                  prompt_version: str = PROMPT_VERSION) -> dict:
    """完整流程：拆 → 校验 → 落库。失败按§2b走DEAD_LETTER+P1。"""
    try:
        spec = validate_split(split(text, splitter_fn=splitter_fn), text=text,
                              date=date)
    except SplitError as e:
        store.alert("P1", "planner_split_failed", error=str(e)[:300])
        raise
    return persist(store, text, spec, date=date, run_id=run_id, model=model,
                   prompt_version=prompt_version)


def load_split_checkpoint(root: str | Path, plan_id: str) -> Checkpoint | None:
    return cp.load(root, f"planner_{plan_id}")


def save_split_checkpoint(root: str | Path, plan_id: str, done_count: int,
                          remaining: str, *, step: int = 0) -> Checkpoint:
    """拆分侧session恢复点：已拆出前N张卡 + 剩余问题原文。

    done_steps存的是**序号(int)**不是task_id字符串——Checkpoint.done_steps在
    Phase0契约里就是List[int]，塞字符串会让Pydantic直接拒绝。
    而且也不需要存id：已拆出的卡已经落进DB了（tasks表），
    按plan_id一查就有，checkpoint只负责记"拆到第几张"和"剩下什么"。
    """
    ck = Checkpoint(task_id=f"planner_{plan_id}", plan_id=plan_id,
                    step=step or done_count,
                    cursor=remaining[:2000],
                    done_steps=list(range(max(0, done_count))))
    return cp.save(root, ck)


def read_inbox_planner(store: Store, inbox: str | Path | None = None) -> list[dict]:
    """扫inbox/planner/下的{type:question,text}，逐个拆（§2b入口）。
    失败的文件移到planner/error/，不丢。"""
    import shutil
    root = Path(store.root)
    d = Path(inbox) if inbox else root / "inbox" / "planner"
    for sub in ("processing", "done", "error"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    out = []
    for src in sorted(d.glob("*.json")):
        proc = d / "processing" / src.name
        try:
            shutil.move(str(src), str(proc))
        except OSError:
            continue
        try:
            payload = json.loads(proc.read_text(encoding="utf-8"))
            if payload.get("type") != "question" or not payload.get("text"):
                raise SplitError("bad_planner_inbox_payload")
            out.append({"file": src.name, "text": payload["text"]})
            shutil.move(str(proc), str(d / "done" / src.name))
        except (ValueError, SplitError) as e:
            shutil.move(str(proc), str(d / "error" / src.name))
            jlog(root, "WARN", "planner_inbox_rejected", file=src.name,
                 error=str(e)[:200])
    return out


def decomposition_view(store: Store, plan_id: str) -> dict:
    """报告的"任务分解视图"数据源（附录C）：分片主题+依赖+拆分依据+planner审计。"""
    audit = {}
    p = store.root / "state" / "planner_runs" / f"{plan_id}.json"
    if p.exists():
        try:
            audit = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            audit = {}
    rows = []
    for t in store.list_plan_tasks(plan_id):
        sh = t["shard"] or {}
        rows.append({"task_id": t["task_id"], "status": t["status"],
                     "objective": sh.get("objective", ""),
                     "depends_on": sh.get("depends_on") or [],
                     "slice_rationale": sh.get("slice_rationale", ""),
                     "outputs": sh.get("outputs") or [],
                     "needs_web": sh.get("needs_web", False)})
    return {"plan_id": plan_id,
            "planner_run_id": audit.get("planner_run_id", ""),
            "planner_model": audit.get("planner_model", ""),
            "planner_prompt_version": audit.get("planner_prompt_version", ""),
            "key_questions": audit.get("key_questions", []),
            "slices": rows,
            "missing_rationale": [r["task_id"] for r in rows
                                  if not r["slice_rationale"]]}
