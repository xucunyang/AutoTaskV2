"""子执行器（Phase2 §5/§6b）：无状态、可断点续跑、可session切换。

执行器只做一件事：把一张任务卡跑到出产物或出结论，然后交回状态机。
它**不重试**（重试是编排层的策略，Phase0权限矩阵明确子Agent无回退权），
它**不判断终态**（状态机在Store层），它**不吞异常**（异常一律映射成FAILED或让出）。

协作式取消：llm_or_tool_execute每步检查cancel_requested，
命中即存checkpoint + 主动让出（RUNNING→READY, reason=cancel_requested），
而不是硬杀——硬杀会丢现场且让下次从零开始。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from core import checkpoint as cp
from core import lease as lease_mod
from core.store import Conflict, IllegalTransition, StaleOwner, Store
from core.task_card import render_task_card
from core.utils import atomic_write_json, jlog, now_utc_iso
from schemas.models import Checkpoint, Handoff

MAX_PROMPT_TOKENS = 8000        # DoD：200MB CSV的prompt仍<8k（preview模式）
WATERMARK = 0.8                 # §6b：上下文水位线
DEFAULT_BUDGET = {"max_steps": 20, "timeout_s": 900}


class SessionYield(Exception):
    """主动让出。reason必须是YIELD_REASONS里的值，Store层还会再校验一次。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}:{detail}")
        self.reason = reason
        self.detail = detail


class SelfTestFail(Exception):
    pass


class LostOwnership(Exception):
    """心跳丢了/租约被抢：执行器必须放弃写回，不能继续跑。"""


def should_yield(usage: dict, final_window: int) -> tuple[bool, str]:
    """§6b水位线判定：prompt_tokens / final_window >= 0.8。
    分母是动态窗口final（不是任务预算），分子是**当前上下文**不是累计消耗。"""
    if not final_window or final_window <= 0:
        return False, ""
    prompt_tokens = int((usage or {}).get("prompt_tokens") or 0)
    if prompt_tokens / final_window >= WATERMARK:
        return True, f"ctx={prompt_tokens}/{final_window}"
    return False, ""


def chat_with_yield_check(provider, prompt: str, budget: dict,
                          final_window: int) -> dict:
    """每次chat返回后检查水位线；命中raise SessionYield(context_full)。"""
    resp = provider.chat(prompt, budget)
    over, detail = should_yield(resp.get("usage", {}), final_window)
    if over:
        raise SessionYield("context_full", detail)
    return resp


def read_range(path: str | Path, offset: int = 0, limit: int = 200) -> list[str]:
    """子Agent按需分页读大文件，不一次性载入（§5）。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with p.open("r", encoding="utf-8", errors="ignore") as f:
        for i, line in enumerate(f):
            if i < offset:
                continue
            if len(out) >= limit:
                break
            out.append(line.rstrip("\n"))
    return out


def build_card(store: Store, task: dict, run_id: str) -> str:
    """渲染任务卡（Phase0§5白名单）。

    budget必须有兜底：模板里用了budget.max_steps/timeout_s，任务卡没写budget
    时传空dict会让StrictUndefined直接抛错，把执行器在渲染阶段就打崩——
    而"任务没显式写预算"是完全正常的输入，不该是异常路径。
    """
    shard = task.get("shard", {}) or {}
    budget = shard.get("budget") or dict(DEFAULT_BUDGET)
    return render_task_card(
        store.root, task_id=task["task_id"], plan_id=task["plan_id"],
        run_id=run_id, objective=shard.get("objective", ""),
        idempotency_key=task["idempotency_key"],
        outputs=shard.get("outputs") or [],
        acceptance=shard.get("acceptance") or [],
        budget=budget,
        inputs=shard.get("inputs") or [],
        needs_web=bool(shard.get("needs_web")),
        freshness=shard.get("freshness", "none"),
        key_questions=shard.get("key_questions") or [])


def self_test(store: Store, task: dict) -> list[dict]:
    """跑轻量验收项（file_exists/row_count）；重型python_test留给Verifier（§5）。"""
    results = []
    root = Path(store.root)
    for i, rule in enumerate((task.get("shard", {}) or {}).get("acceptance") or []):
        rtype = rule.get("type")
        rid = rule.get("rule_id") or f"rule_{i}"
        if rtype == "file_exists":
            p = root / str(rule.get("path", ""))
            ok = p.exists()
            results.append({"rule_id": rid, "type": rtype, "ok": ok,
                            "detail": str(p) if not ok else ""})
        elif rtype == "row_count":
            p = root / str(rule.get("path", ""))
            need = int(rule.get("min") or 0)
            rows = -1
            if p.exists():
                with p.open("r", encoding="utf-8", errors="ignore") as f:
                    rows = max(0, sum(1 for _ in f) - 1)
            results.append({"rule_id": rid, "type": rtype,
                            "ok": rows >= need,
                            "detail": f"rows={rows} need>={need}"})
        else:
            results.append({"rule_id": rid, "type": rtype, "ok": True,
                            "detail": "deferred_to_verifier"})
    return results


def build_handoff(task: dict, results: list[dict], artifacts: list[str],
                  tokens_used: int = 0, done: bool = True) -> Handoff:
    """构造handoff（Phase0要求Pydantic校验，summary≤2000字）。"""
    failed = [r for r in results if not r["ok"]]
    lines = [f"{r['rule_id']}:{'ok' if r['ok'] else 'FAIL'}"
             f"{(' ' + r['detail']) if r['detail'] else ''}" for r in results]
    return Handoff(done=done and not failed,
                   summary="; ".join(lines)[:2000],
                   artifact_refs=artifacts,
                   tests_passed=not failed,
                   tokens_used=tokens_used)


def run_task(store: Store, plan_id: str, task_id: str, run_id: str,
             owner: str | None = None, provider=None, final_window: int = 0,
             search_fn=None) -> str:
    """执行一个任务。返回结局标签（便于测试与观测）。

    结局：submitted / yielded:{reason} / failed:{err} / lost / skipped_terminal
    """
    owner = owner or f"sub-{uuid.uuid4().hex[:8]}"
    task = store.get_task(plan_id, task_id)
    if task is None:
        return "not_found"
    if task["status"] in ("DONE", "DEAD_LETTER", "CANCELLED", "SKIPPED",
                          "SKIPPED_CACHED"):
        return "skipped_terminal"

    ckpt = cp.load(store.root, task_id)
    resuming = ckpt is not None
    if resuming:
        ckpt = cp.bump_generation(ckpt, task_id, plan_id)   # 第N代session

    hb = lease_mod.Heartbeat(store, plan_id, task_id, owner=owner, run_id=run_id)
    hb.start()
    t0 = time.monotonic()
    try:
        card = build_card(store, task, run_id)
        if provider is None:
            # 没有真实provider时不做LLM调用，只跑自检与结构校验（冒烟/测试路径）
            results = self_test(store, task)
            artifacts = [str(p) for p in
                         (Path(store.root) / o
                          for o in (task["shard"] or {}).get("outputs") or [])
                         if p.exists()]
        else:
            artifacts = _run_with_provider(store, task, card, ckpt, provider,
                                           final_window, run_id, search_fn)
            results = self_test(store, task)

        handoff = build_handoff(task, results, artifacts)
        store.record_metric("execute", (time.monotonic() - t0) * 1000,
                            plan_id=plan_id, task_id=task_id, run_id=run_id)
        if ckpt is not None:
            cp.save(store.root, ckpt)     # 成功也留一份（generation可观测）
        handoff_err = None
        try:
            got = store.transition(plan_id, task_id, "SUBMITTED", agent=owner,
                                   run_id=run_id, expect_version=task["version"],
                                   expect_fencing=_fencing(store, plan_id, task_id),
                                   handoff=handoff.model_dump(mode="json"),
                                   role="subagent")
        except (Conflict, StaleOwner) as e:
            return "lost"
        if handoff_err:
            return "failed:handoff"
        return "submitted"
    except SessionYield as y:
        # 让出：先存checkpoint再让出（顺序反了会被Store的checkpoint断言拒）
        if ckpt is not None:
            ckpt.summary = [f"yield:{y.reason}:{y.detail}"[:500]]
            cp.save(store.root, ckpt)
        else:
            cp.save(store.root, Checkpoint(task_id=task_id, plan_id=plan_id,
                                           summary=[f"yield:{y.reason}"[:500]]))
        try:
            store.transition(plan_id, task_id, "READY", agent=owner,
                             run_id=run_id, expect_version=task["version"],
                             expect_fencing=_fencing(store, plan_id, task_id),
                             payload={"reason": y.reason, "detail": y.detail[:200]},
                             role="subagent")
            return f"yielded:{y.reason}"
        except StaleOwner:
            return "lost"
        except Conflict:
            return "lost"
        except IllegalTransition as e:
            # 让出被Store拒（短任务不让出/熔断超限/无checkpoint）→ 改走FAILED。
            # 这里绝不能让异常逃出去：逃出去任务就永久卡在RUNNING，
            # 只能等租约过期被回收，白等一个TTL。设计§6b也要求"熔断改走FAILED"。
            _fail(store, plan_id, task_id, owner, run_id, task,
                  f"yield_rejected:{e}; original_yield={y.reason}")
            return f"yield_rejected:{y.reason}"
    except LostOwnership as e:
        return "lost"
    except Exception as e:  # noqa: BLE001 — 任何异常都要落到FAILED，不能吞
        _fail(store, plan_id, task_id, owner, run_id, task, str(e))
        jlog(store.root, "ERROR", "task_failed", plan_id=plan_id,
             task_id=task_id, error=str(e)[:300])
        return f"failed:{str(e)[:60]}"
    finally:
        hb.stop()
        if hb.lost:
            jlog(store.root, "WARN", "heartbeat_lost_after_task",
                 plan_id=plan_id, task_id=task_id)


def _fencing(store: Store, plan_id: str, task_id: str) -> int:
    t = store.get_task(plan_id, task_id)
    return t["fencing_token"] if t else 0


def _fail(store: Store, plan_id: str, task_id: str, owner: str, run_id: str,
          task: dict, error: str) -> None:
    """落FAILED。失败也要处理失败：写不进去时只记日志，不二次抛。"""
    try:
        store.transition(plan_id, task_id, "FAILED", agent=owner,
                         run_id=run_id, expect_version=task["version"],
                         expect_fencing=_fencing(store, plan_id, task_id),
                         payload={"error": str(error)[:2000]}, role="subagent")
    except Exception as e:  # noqa: BLE001
        jlog(store.root, "ERROR", "fail_transition_failed", plan_id=plan_id,
             task_id=task_id, error=str(e)[:200])


def _run_with_provider(store: Store, task: dict, card: str, ckpt, provider,
                       final_window: int, run_id: str, search_fn) -> list[str]:
    """真实LLM路径（Phase2）。这里只跑一轮：chat → 落产物 → manifest。"""
    shard = task.get("shard", {}) or {}
    sources = []
    if shard.get("needs_web"):
        if search_fn is None:
            raise SessionYield("context_full", "needs_web_but_no_search")
        sources = search_fn(shard, task)
    payload = card
    if ckpt is not None:
        payload = card + "\n【续跑】" + json.dumps(cp.resume_payload(ckpt),
                                                  ensure_ascii=False)
    resp = chat_with_yield_check(provider, payload, shard.get("budget") or {},
                                 final_window)
    content = (resp or {}).get("content", "")
    written = []
    for out in shard.get("outputs") or []:
        p = Path(store.root) / out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(out)
        manifest = cp.build_manifest(store.root, p)
        atomic_write_json(p.parent / f"{p.stem}.manifest.json", manifest)
    if sources:
        atomic_write_json(Path(store.root) / "artifacts" / f"{task_id}.sources.json",
                          sources)
    return written


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="core.executor")
    ap.add_argument("--root", default=None)
    ap.add_argument("--plan-id", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--run-id", default="manual")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    store = Store(root)
    outcome = run_task(store, args.plan_id, args.task_id, args.run_id)
    print(json.dumps({"task_id": args.task_id, "outcome": outcome},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
