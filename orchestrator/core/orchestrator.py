"""主循环编排（Phase2 §2/§4）。

主Agent不碰业务数据，只做三件事：算谁该跑、把谁派出去、把跑完的推进下一态。
所有状态变更都经Store（状态机在Store层强制），本模块不持有状态真相。

防饿死（§4）是本模块最容易做错的地方：高优插队是对的，
但"永远只按priority取"会让Daily（10）被管道（0）压到饿死，
所以老化提升（10→5→3）+ 预留槽（至少1槽给低优）两套机制必须同时在。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import enqueue as enqueue_mod
from core import lease as lease_mod
from core.store import Conflict, IllegalTransition, Store, StaleOwner
from core.utils import jlog, now_utc_iso

SHANGHAI = timezone(timedelta(hours=8))
# §4老化两档：>10min→5，>20min→3，每次检查最多升一档，上限3
AGING_STEPS = ((600, 5), (1200, 3))
AGING_FLOOR = 3              # 0..2留给管道高优，Daily老化不与之争抢
HIGH_PRIORITY_MAX = 4      # 0..4算高优（管道）
RESERVED_LOW_SLOTS = 1
BACKLOG_ALERT = 50        # READY积压>50告警
OLDEST_WAIT_ALERT_S = 3600
SLA_DISPATCH_S = 60       # §3：入队→RUNNING p95<60s


class Orchestrator:
    """单实例编排器。executor_fn由注入（测试用假执行器，生产用executor.run_task）。"""

    def __init__(self, root: str | Path, executor_fn=None, owner: str = "orchestrator-main",
                 max_workers: int = 4, poll_normal_s: float = 10,
                 poll_fast_s: float = 2, run_once: bool = False):
        self.root = Path(root)
        self.store = Store(self.root)
        self.executor_fn = executor_fn
        self.owner = owner
        self.max_workers = max_workers
        self.poll_normal_s = poll_normal_s
        self.poll_fast_s = poll_fast_s
        self.run_once = run_once
        self._shutdown = False
        self._lock_held = False
        cfg = _load_cfg(self.root)
        self.low_slot_reserve = int(cfg.get("workers", {})
                                    .get("reserved_low_slot", RESERVED_LOW_SLOTS))

    # ---------- 单实例锁（§2：全局单编排实例，DB行锁） ----------

    def acquire_lock(self) -> bool:
        """用 plans表占一行做DB级单实例锁。跨进程有效，不依赖文件锁。"""
        def _fn(con):
            cur = con.execute(
                "SELECT template FROM plans WHERE plan_id=?", ("orchestrator_lock",)
            ).fetchone()
            if cur is not None:
                return False
            now = now_utc_iso()
            con.execute(
                "INSERT INTO plans(plan_id,template,status,created_at,updated_at)"
                " VALUES ('orchestrator_lock',?,'HELD',?,?)",
                (f"pid={os.getpid()}", now, now))
            return True

        try:
            self._lock_held = bool(self.store._write_txn(_fn))
        except Exception as e:
            jlog(self.root, "ERROR", "orchestrator_lock_error", error=str(e)[:200])
            self._lock_held = False
        return self._lock_held

    def release_lock(self) -> None:
        if not self._lock_held:
            return
        def _fn(con):
            con.execute("DELETE FROM plans WHERE plan_id=?", ("orchestrator_lock",))
        try:
            self.store._write_txn(_fn)
        except Exception:
            pass
        self._lock_held = False

    def check_shutdown(self) -> bool:
        """SIGTERM/停止文件 → 优雅停机（§2）。"""
        if self._shutdown:
            return True
        stop_file = self.root / "state" / "STOP"
        if stop_file.exists():
            jlog(self.root, "INFO", "shutdown_requested", via=str(stop_file))
            self._shutdown = True
        return self._shutdown

    def request_shutdown(self) -> None:
        self._shutdown = True

    # ---------- DAG 校验（§2：环/孤儿/自依赖 → FAILED+告警，不派发） ----------

    def validate_dag(self, plan_id: str) -> list[dict]:
        bad = []
        tasks = {t["task_id"]: t for t in self.store.list_plan_tasks(plan_id)}
        for t in tasks.values():
            deps = (t["shard"] or {}).get("depends_on") or []
            for d in deps:
                if d == t["task_id"]:
                    bad.append({"task_id": t["task_id"], "problem": "self_dependency"})
                elif d not in tasks:
                    bad.append({"task_id": t["task_id"],
                                "problem": "missing_dependency", "missing": d})
        # 环检测（DFS三色）
        colour: dict[str, int] = {}

        def visit(node: str, stack: list[str]) -> None:
            colour[node] = 1
            for d in (tasks.get(node, {}).get("shard", {}) or {}).get("depends_on") or []:
                if d not in tasks:
                    continue
                if colour.get(d) == 1:
                    bad.append({"task_id": d, "problem": "cycle",
                                "cycle": stack + [node, d]})
                elif colour.get(d, 0) == 0:
                    visit(d, stack + [node])
            colour[node] = 2

        for tid in tasks:
            if colour.get(tid, 0) == 0:
                visit(tid, [])
        if bad:
            for b in bad:
                self.store.alert("P1", "dag_invalid", plan_id=plan_id, **b)
            jlog(self.root, "WARN", "dag_invalid", plan_id=plan_id, count=len(bad))
        return bad

    # ---------- 推进 READY（§2：依赖满足才派发） ----------

    def refresh_ready(self, plan_id: str) -> int:
        n = 0
        for t in self.store.list_by_status("PENDING"):
            if t["plan_id"] != plan_id:
                continue
            try:
                self.store.transition(plan_id, t["task_id"], "READY",
                                      agent=self.owner, run_id=self._run_id(),
                                      expect_version=t["version"],
                                      payload={"queue_wait_ms": _wait_ms(t["enqueued_at"])})
                n += 1
            except (IllegalTransition, Conflict):
                continue      # 依赖未满足或并发改了，下轮再试
        return n

    # ---------- 防饿死（§4） ----------

    def apply_aging(self) -> list[dict]:
        """READY等待越久优先级越高：>10min→5，>20min→3。

        三条容易写错的：
        - **一次只升一档**（设计§4"每次老化检查可升一档"）：等了25min也只到5，
          下轮检查再到3。一次跳到3会让"档位"失去意义（分档就是为了分级提优先级）。
        - **下限是3不是0**：0..2留给管道高优，Daily老化不与管道争抢。
        - **时钟取enqueued_at而不是updated_at**：老化自己会改updated_at，
          拿它当基准等于每次提升都把等待时间清零，第二档(20min)永远触发不了。
          等待时间必须单调递增，只能取入队时刻。
        """
        aged = []
        now = time.time()
        for t in self.store.list_by_status("READY"):
            waited = now - _epoch(t["enqueued_at"])
            target = t["priority"]
            for threshold, p in AGING_STEPS:
                if waited >= threshold and p < t["priority"]:
                    target = p
                    break          # 一次只升一档
            if target >= t["priority"]:
                continue
            try:
                self.store.update_priority(
                    t["plan_id"], t["task_id"], target,
                    expect_version=t["version"], reason="aging",
                    waited_s=int(waited))
            except (Conflict, IllegalTransition):
                continue
            aged.append({"task_id": t["task_id"], "from": t["priority"],
                         "to": target, "waited_s": int(waited)})
            self.store.alert("P0", "task_aged", plan_id=t["plan_id"],
                             task_id=t["task_id"], new_priority=target)
        for a in aged:
            jlog(self.root, "INFO", "task_aged", **a)
        return aged

    # ---------- 派发（§2 dispatch_split，评审M5两阶段） ----------

    def dispatch_split(self) -> list[dict]:
        """高优N-1槽 + 低优预留1槽。分段内仍 ORDER BY priority,seq（不内存重排）。"""
        if self.executor_fn is None:
            return []
        running = len(self.store.list_by_status("RUNNING"))
        free = max(0, self.max_workers - running)
        if free == 0:
            return []
        high_quota = max(0, free - self.low_slot_reserve)
        dispatched = []
        if high_quota:
            for t in self.store.list_ready_ordered(limit=high_quota,
                                                   priority_max=HIGH_PRIORITY_MAX):
                got = self._dispatch_one(t)
                if got:
                    dispatched.append(got)
        # 低优：预留槽必须留出来，否则高优连续占满时Daily饿死（§4）
        for t in self.store.list_ready_ordered(limit=max(free, 1),
                                               priority_min=HIGH_PRIORITY_MAX + 1):
            if len(dispatched) >= free:
                break
            got = self._dispatch_one(t)
            if got:
                dispatched.append(got)
        return dispatched

    def _dispatch_one(self, t: dict) -> dict | None:
        """幂等派发：READY+无lease+version未变才派；派发即占位防重派。"""
        from core import checkpoint as cp
        plan_id, task_id = t["plan_id"], t["task_id"]
        hit, why = cp.cache_hit(self.root, t)
        if hit:
            try:
                got = self.store.transition(plan_id, task_id, "SKIPPED_CACHED",
                                           agent=self.owner, run_id=self._run_id(),
                                           expect_version=t["version"],
                                           payload={"cache": why})
                self.store.record_metric("cache_hit", 1, plan_id=plan_id,
                                         task_id=task_id, run_id=self._run_id())
                jlog(self.root, "INFO", "cache_hit", plan_id=plan_id,
                     task_id=task_id)
                return got
            except (Conflict, IllegalTransition):
                return None
        try:
            running = self.store.transition(
                plan_id, task_id, "RUNNING", agent=self.owner,
                run_id=self._run_id(), expect_version=t["version"],
                lease_op="acquire", payload={"dispatch_wait_ms": _wait_ms(t["updated_at"])})
        except (Conflict, IllegalTransition) as e:
            return None
        self.store.record_metric("dispatch_wait", _wait_ms(t["updated_at"]) / 1000.0,
                                 plan_id=plan_id, task_id=task_id,
                                 run_id=self._run_id())
        wait_s = _wait_ms(t["updated_at"]) / 1000.0
        if wait_s > SLA_DISPATCH_S:
            self.store.alert("P0", "dispatch_sla_breach", plan_id=plan_id,
                             task_id=task_id, wait_s=int(wait_s))
        running = self.store.lease_update(
            plan_id, task_id, owner=self.owner, lease_until=None,
            bump_fencing=False, expect_version=running["version"],
            audit_event={"run_id": self._run_id(), "agent": self.owner,
                         "payload": {"event": "DISPATCHED"}})
        try:
            self.executor_fn(self.store, plan_id, task_id, self._run_id(),
                             owner=self.owner)
        except Exception as e:
            jlog(self.root, "ERROR", "executor_raised", plan_id=plan_id,
                 task_id=task_id, error=str(e)[:300])
        return running

    # ---------- 收尾：SUBMITTED→VERIFYING、租约回收、死信 ----------

    def promote_submitted(self) -> list[dict]:
        """SUBMITTED→VERIFYING是系统跃迁（Phase0权限矩阵：仅orchestrator可写）。"""
        out = []
        for t in self.store.list_by_status("SUBMITTED"):
            try:
                out.append(self.store.transition(
                    t["plan_id"], t["task_id"], "VERIFYING", agent=self.owner,
                    run_id=self._run_id(), expect_version=t["version"]))
            except (Conflict, IllegalTransition):
                continue
        return out

    def housekeeping(self) -> dict:
        """一轮的收尾工作：租约回收→死信升级→取消收尾。"""
        rec = lease_mod.recover_expired_leases(self.store, run_id=self._run_id(),
                                               agent=self.owner)
        dead = lease_mod.escalate_dead_letters(self.store, run_id=self._run_id(),
                                               agent=self.owner)
        return {"recovered": len(rec), "dead_letter": len(dead)}

    def backlog_check(self) -> dict:
        """§4：READY积压>50或最老等待>1h → P1。"""
        ready = self.store.list_by_status("READY")
        if not ready:
            return {"ready": 0}
        oldest = max(_wait_ms(t["updated_at"]) for t in ready) / 1000.0
        out = {"ready": len(ready), "oldest_wait_s": int(oldest)}
        if len(ready) > BACKLOG_ALERT or oldest > OLDEST_WAIT_ALERT_S:
            self.store.alert("P1", "queue_backlog", **out)
        return out

    def active_plans(self) -> list[str]:
        return [p["plan_id"] for p in self.store.list_plans()
                if p["plan_id"] != "orchestrator_lock" and p["status"] != "DONE"]

    # ---------- 唤醒（评审M2：事件唤醒替代纯sleep） ----------

    def wait_wakeup(self, timeout_s: float) -> bool:
        """等wakeup.flag出现或超时。返回是否被唤醒。
        flag由enqueue入队时touch，管道任务不用等满一个轮询周期。"""
        flag = self.root / "wakeup.flag"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if flag.exists():
                try:
                    flag.unlink()
                except OSError:
                    pass
                return True
            if self.check_shutdown():
                return False
            time.sleep(0.1)
        return False

    def _run_id(self) -> str:
        if not hasattr(self, "_rid") or not self._rid:
            self._rid = f"run-{int(time.time())}"
        return self._rid

    # ---------- 一轮 / 主循环 ----------

    def tick(self) -> dict:
        """跑一轮主循环。返回本轮各步计数（可观测、可测试）。"""
        stats = {"inbox": None, "dispatched": 0, "refreshed": 0, "aged": 0,
                 "promoted": 0}
        if self.check_shutdown():
            return {**stats, "shutdown": True}
        try:
            stats["inbox"] = enqueue_mod.scan_inbox(self.store)
        except Exception as e:
            jlog(self.root, "WARN", "ingest_inbox_failed", error=str(e)[:200])
        stats["housekeeping"] = self.housekeeping()
        for plan_id in self.active_plans():
            self.validate_dag(plan_id)
            stats["refreshed"] += self.refresh_ready(plan_id)
        stats["aged"] = len(self.apply_aging())
        stats["backlog"] = self.backlog_check()
        stats["dispatched"] = len(self.dispatch_split())
        stats["promoted"] = len(self.promote_submitted())
        for p in self.active_plans():
            try:
                self.store.rebuild_export(p)
            except Exception as e:
                jlog(self.root, "WARN", "export_failed", plan_id=p,
                     error=str(e)[:200])
        return stats

    def run_forever(self, max_ticks: int | None = None) -> None:
        if not self.acquire_lock():
            jlog(self.root, "WARN", "orchestrator_lock_busy", pid=os.getpid())
            return
        jlog(self.root, "INFO", "orchestrator_started", pid=os.getpid(),
             owner=self.owner)
        n = 0
        try:
            while not self.check_shutdown():
                self.tick()
                n += 1
                if self.run_once or (max_ticks is not None and n >= max_ticks):
                    break
                self.wait_wakeup(self.poll_normal_s)
        finally:
            self.release_lock()
            jlog(self.root, "INFO", "orchestrator_stopped", ticks=n)


def _load_cfg(root: Path) -> dict:
    try:
        import yaml
        return yaml.safe_load((root / "config" / "schedule.yaml").read_text(
            encoding="utf-8")) or {}
    except Exception:
        return {}


def _epoch(ts: str | None) -> float:
    if not ts:
        return time.time()
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return time.time()


def _wait_ms(ts: str | None) -> int:
    return int(max(0.0, (time.time() - _epoch(ts))) * 1000)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="core.orchestrator")
    ap.add_argument("--root", default=None)
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--max-ticks", type=int, default=None)
    ap.add_argument("--no-lock", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    from core import executor
    orch = Orchestrator(root, executor_fn=executor.run_task,
                        run_once=args.once)
    if args.no_lock:
        orch._lock_held = True
    orch.run_forever(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
