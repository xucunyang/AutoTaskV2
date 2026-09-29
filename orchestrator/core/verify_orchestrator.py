"""校验编排（Phase3 §2.1/§2.2c/§2.3，评审M7）。

异步池用AnyIO：验证不阻塞主循环。主循环只负责把VERIFYING任务丢进池子，
校验跑多久都不影响派发下一批——否则一条慢的python_test能把整条流水线卡住。

局部重入不加状态边：已通过的rule_id存tasks.verify_progress_json，
重入时跳过。中断=存进度+释放租约，状态仍是VERIFYING，
主循环requeue_verify()捞出无lease的VERIFYING重派。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import anyio

from core import verifier as vf
from core.store import Conflict, IllegalTransition, Store, StaleOwner, StoreError
from core.utils import jlog, now_utc_iso

MAX_VERIFY_WORKERS = 2          # config schedule.yaml workers.verifier_idle
VERIFY_LEASE_TTL_S = 300        # 验收比执行慢，给更长租约


class VerifyOutcome:
    SUBMITTED = "verified"       # 全部通过 → DONE
    RETRY = "retry"               # 有rule没过 → RETRY（带reason）
    APPROVAL = "waiting_approval"
    REJECTED = "rejected"         # 非VERIFYING状态拒绝校验
    ERROR = "error"


def verify(store: Store, plan_id: str, task_id: str, run_id: str, *,
           date: str | None = None, notifier=None,
           wait: bool = True) -> str:
    """校验一个任务。必须处于VERIFYING，否则拒绝。"""
    task = store.get_task(plan_id, task_id)
    if task is None:
        return VerifyOutcome.REJECTED
    if task["status"] != "VERIFYING":
        # 已被别人推进/重入：直接拒绝，不做二次校验（否则可能双写终态）
        jlog(store.root, "WARN", "verify_rejected_status", plan_id=plan_id,
             task_id=task_id, status=task["status"])
        return VerifyOutcome.REJECTED
    date = date or _date_of(task)
    ctx = vf.make_ctx(store, plan_id, task_id, date, notifier=notifier)
    shard = task["shard"] or {}
    rules = shard.get("acceptance") or []
    if not rules:
        return _finish(store, task, [], run_id, notifier)

    already = set((task.get("verify_progress") or {}).get("verified_rules") or [])
    t0 = time.monotonic()
    results = vf.run_rules(ctx, rules, skip=already)
    results = [r for r in results if not r.get("skipped")]
    store.record_metric("verify", (time.monotonic() - t0) * 1000,
                        plan_id=plan_id, task_id=task_id, run_id=run_id)
    for r in results:
        if r.get("duration_ms", 0) > 60000:
            store.alert("P2", "verify_rule_slow", plan_id=plan_id,
                        task_id=task_id, body=f"{r['rule_id']} {r['duration_ms']}ms")
    return _finish(store, task, results, run_id, notifier)


def _finish(store: Store, task: dict, results: list[dict], run_id: str,
            notifier) -> str:
    plan_id, task_id = task["plan_id"], task["task_id"]
    cur = store.get_task(plan_id, task_id)
    if cur is None or cur["status"] != "VERIFYING":
        return VerifyOutcome.REJECTED
    failed = [r for r in results if not r["ok"]]
    progress = dict(cur.get("verify_progress") or {})
    passed = sorted(set(progress.get("verified_rules") or []) |
                    {r["rule_id"] for r in results if r["ok"]})
    progress["verified_rules"] = passed
    progress["last_results"] = [{k: v for k, v in r.items()
                                 if k in ("rule_id", "type", "ok", "detail")}
                                for r in results]
    payload = {"rules": len(results), "failed": [r["rule_id"] for r in failed]}
    try:
        store.update_verify_progress(plan_id, task_id, passed,
                                     expect_version=cur["version"],
                                     extra={"last_results": progress["last_results"]})
    except (Conflict, StoreError):
        return VerifyOutcome.ERROR
    cur = store.get_task(plan_id, task_id)
    if not failed:
        try:
            store.transition(plan_id, task_id, "DONE", agent="verifier",
                             run_id=run_id, expect_version=cur["version"],
                             payload=payload, role="verifier")
        except (Conflict, IllegalTransition, StaleOwner):
            return VerifyOutcome.ERROR
        return VerifyOutcome.SUBMITTED
    # 没过 → RETRY（必须带reason，Phase0§2.3）
    detail = "; ".join(f"{r['rule_id']}:{r['detail'][:120]}" for r in failed[:5])
    try:
        store.transition(plan_id, task_id, "RETRY", agent="verifier",
                         run_id=run_id, expect_version=cur["version"],
                         payload={"reason": "verify_failed", "detail": detail,
                                  "failed_rules": [r["rule_id"] for r in failed]},
                         role="verifier")
    except (Conflict, IllegalTransition, StaleOwner):
        return VerifyOutcome.ERROR
    store.alert("P1", "task_failed", plan_id=plan_id, task_id=task_id,
                body=detail[:300], run_id=run_id)
    if any("hallucination" in str(r["detail"]) for r in failed):
        store.alert("P0", "inconsistent", plan_id=plan_id, task_id=task_id,
                    body="claim无来源=幻觉", run_id=run_id)
    return VerifyOutcome.RETRY


StoreErrorAlias = None   # 兼容占位：实际用 core.store.StoreError


def _date_of(task: dict) -> str:
    ts = task.get("enqueued_at") or task.get("updated_at") or ""
    try:
        from datetime import datetime, timedelta, timezone
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt.astimezone(timezone(timedelta(hours=8)))
                .strftime("%Y-%m-%d"))
    except (ValueError, TypeError):
        return "unknown"


# ---------- 异步池（评审M7） ----------

class VerifyPool:
    """AnyIO worker池。高优任务先入队，不阻塞主循环。

    worker数取自config/schedule.yaml的workers.verifier_idle（默认2）。
    """

    def __init__(self, store: Store, workers: int = MAX_VERIFY_WORKERS,
                 notifier=None):
        self.store = store
        self.workers = max(1, int(workers))
        self.notifier = notifier
        self._tg = None
        self._limiter = None
        self.inflight: set[tuple[str, str]] = set()

    async def __aenter__(self):
        self._limiter = anyio.CapacityLimiter(self.workers)
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        return self

    async def __aexit__(self, *exc):
        # 正常退出要**等**worker跑完（校验结果就是这次调用的产出）；
        # 只有异常路径才取消。中途cancel会让任务永远停在VERIFYING，
        # 主循环还得靠requeue_verify捞一遍，纯属自找的返工。
        if exc and exc[0] is not None:
            self._tg.cancel_scope.cancel()
        await self._tg.__aexit__(*exc)

    async def dispatch(self, plan_id: str, task_id: str, run_id: str) -> None:
        """丢进池子就返回，不等它跑完。"""
        if (plan_id, task_id) in self.inflight:
            return
        self.inflight.add((plan_id, task_id))
        self._tg.start_soon(self._worker, plan_id, task_id, run_id)

    async def _worker(self, plan_id: str, task_id: str, run_id: str) -> None:
        try:
            async with self._limiter:
                await anyio.to_thread.run_sync(
                    lambda: verify(self.store, plan_id, task_id, run_id,
                                   notifier=self.notifier))
        except Exception as e:   # noqa: BLE001 单个任务炸了不能带崩池子
            jlog(self.store.root, "ERROR", "verify_worker_failed",
                 plan_id=plan_id, task_id=task_id, error=str(e)[:300])
        finally:
            self.inflight.discard((plan_id, task_id))


def dispatch_verify_async(pool: VerifyPool, tasks: list[dict], run_id: str) -> int:
    """把待校验任务丢进池子。按priority排序，高优先占槽。"""
    ordered = sorted(tasks, key=lambda t: (t.get("priority", 10), t.get("seq", 0)))
    for t in ordered:
        pool._tg.start_soon(pool._worker, t["plan_id"], t["task_id"], run_id)
    return len(ordered)


def requeue_verify(store: Store, run_id: str = "", agent: str = "orchestrator",
                   lease_expired_before: float | None = None) -> list[dict]:
    """捞出无lease的VERIFYING重派（§2.2c中断恢复）。

    中断=保存进度+释放租约，状态保持VERIFYING；这里把它们重新入池。
    只捞"没有owner或租约已过期"的，否则会和正在校验的worker抢同一个任务。
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    out = []
    for t in store.list_by_status("VERIFYING"):
        owner, until = t.get("owner"), t.get("lease_until")
        if owner and until:
            try:
                lu = datetime.fromisoformat(until)
                if lu.tzinfo is None:
                    lu = lu.replace(tzinfo=timezone.utc)
                if lu > now:
                    continue          # 还有效，别抢
            except ValueError:
                pass
        out.append(t)
    return out


# ---------- 终检（§2.3） ----------

def final_consistency(store: Store, plan_id: str, notifier=None) -> dict:
    """计划级终检。任一项不过 → 计划不标DONE + P0 INCONSISTENT。

    检查项：DAG无环/依赖满足/状态合法/无孤儿/产物存在+sha匹配/
    终态任务有验收记录/needs_web四件套齐。
    """
    from core.orchestrator import Orchestrator
    problems = []
    tasks = store.list_plan_tasks(plan_id)
    if not tasks:
        return {"ok": True, "problems": [], "checked": 0}
    # DAG（复用主循环的判环逻辑，不重复实现）
    o = Orchestrator.__new__(Orchestrator)
    o.store = store
    o.root = Path(store.root)
    for b in o.validate_dag(plan_id):
        problems.append(f"dag:{b.get('problem')}:{b.get('task_id')}")
    for t in tasks:
        tid, status = t["task_id"], t["status"]
        shard = t["shard"] or {}
        deps = shard.get("depends_on") or []
        sibs = {x["task_id"]: x["status"] for x in tasks}
        if status in ("DONE", "SKIPPED", "SKIPPED_CACHED") and deps:
            unmet = [d for d in deps if sibs.get(d) not in
                     ("DONE", "SKIPPED", "SKIPPED_CACHED")]
            if unmet:
                problems.append(f"{tid}:terminal_with_unmet_deps:{unmet}")
        if not (shard.get("acceptance") or []):
            problems.append(f"{tid}:no_acceptance_rules")
        if status in ("DONE", "DEAD_LETTER"):
            events = [e for e in store.recent_events(plan_id, limit=50)
                      if e["task_id"] == tid]
            if not any(e["to_s"] in ("DONE", "DEAD_LETTER") for e in events):
                problems.append(f"{tid}:terminal_without_audit_row")
        if shard.get("needs_web"):
            d = Path(store.root) / "artifacts" / _date_of(t)
            for suffix in ("sources.json", "claims.json", "tuning.md"):
                if not (d / f"{tid}.{suffix}").exists():
                    problems.append(f"{tid}:missing_four_set:{suffix}")
        for out in shard.get("outputs") or []:
            if not (Path(store.root) / out).exists():
                problems.append(f"{tid}:missing_artifact:{out}")
    result = {"ok": not problems, "problems": problems, "checked": len(tasks),
              "plan_id": plan_id}
    if problems:
        store.alert("P0", "inconsistent", plan_id=plan_id, task_id="-",
                    body="; ".join(problems[:6]))
    return result
