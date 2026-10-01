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
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio

from core import enqueue as enqueue_mod
from core import governor as gov_mod
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
                 poll_fast_s: float = 2, run_once: bool = False,
                 verify_pool=None, models: dict | None = None):
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
        self.verify_pool = verify_pool     # 外部注入的VerifyPool（可为None）
        self._in_flight: set[tuple[str, str]] = set()   # 已丢池、尚未回收的
        cfg = _load_cfg(self.root)
        self.low_slot_reserve = int(cfg.get("workers", {})
                                    .get("reserved_low_slot", RESERVED_LOW_SLOTS))
        # 单实例锁的存活期。心跳过期即允许接管（见 acquire_lock 的说明）。
        # 必须是主循环轮询的数倍，否则一个卡住的 tick 就会让别人以为它死了。
        self.lock_ttl_s = float(cfg.get("lock", {}).get("ttl_s", 90))
        # 动态算力总督：ACTIVE(有人在用,~1/8 CPU) / IDLE(空闲,放开跑)。
        # 之前 governor.py 写好了却**从没被调用**——profile 算了没人消费，
        # 于是 ACTIVE/IDLE 只是配置文件里的两组静态数字。
        gcfg = cfg.get("governor") or {}
        self._governor = gov_mod.Governor(
            idle_after_s=int(gcfg.get("idle_after_s", 300)),
            confirm=int(gcfg.get("confirm_samples", 2)),
            headroom_gb=float(gcfg.get("mem_headroom_gb", 2.0)),
            notifier=self.store) if gcfg.get("enabled", True) else None
        self._gov_profile = None
        # 模型注册表（{名字: provider}）与网关配置。留空则不注入provider，
        # 执行器走无LLM的自检路径——冒烟/测试就是这个模式，不能被
        # "配置读不到key"这种环境问题静默改掉。
        self.models = models if models is not None else _load_models(self.root)
        self.gw_cfg = _load_cfg(self.root, "gateway.yaml")
        self._search_provider = _load_search(self.root)
        # 编排侧检索签名是 search_fn(shard, task)，按任务决定检索式；
        # 工具侧要的是 search_fn(query, max_results)。这里暴露前者，
        # executor内部再包一层给ToolBox（见 executor._search_adapter）。
        p = self._search_provider
        top_k = int(_load_cfg(self.root, "search.yaml").get("top_k", 8))
        self._search_fn = None
        if p is not None:
            def _by_shard(shard, task, _p=p, _k=top_k):
                q = (shard or {}).get("query") or (shard or {}).get("objective") or ""
                if not q:
                    return []
                res = _p.search(str(q), top_k=_k)
                return [asdict(r) for r in res]
            self._search_fn = _by_shard

    # ---------- 单实例锁（§2：全局单编排实例，DB行锁） ----------

    def acquire_lock(self) -> bool:
        """DB级单实例锁。跨进程有效，不依赖文件锁。

        **带存活期**（租约语义）。之前只看"行在不在"，行在就返回 False——
        于是持锁进程被 kill -9 或机器断电之后，那一行永远留在表里，
        整个系统再也无法启动，只能人工连库删行。
        一次意外崩溃导致系统永久不可用，这个代价比"可能误抢锁"高得多：
        误抢的最坏后果是两个编排器短暂并行，而派发本身是幂等的
        （expect_version 挡住重复派发），且下面的 heartbeat_lock 会
        立刻发现行被换主人并退出。

        存活判据用**心跳时间**而不是"进程是否活着"：跨平台一致
        （Windows 上 os.kill(pid,0) 语义不同），且顺带覆盖了
        "进程活着但卡死"的情况。
        """
        def _fn(con):
            now = now_utc_iso()
            cur = con.execute(
                "SELECT template,updated_at FROM plans WHERE plan_id=?",
                ("orchestrator_lock",)).fetchone()
            if cur is None:
                con.execute(
                    "INSERT INTO plans(plan_id,template,status,created_at,updated_at)"
                    " VALUES ('orchestrator_lock',?,'HELD',?,?)",
                    (f"pid={os.getpid()}", now, now))
                return ("acquired", None)
            holder, updated = cur[0], cur[1]
            age = _age_s(updated)
            if age is not None and age < self.lock_ttl_s:
                return ("busy", holder)
            # 心跳过期：接管。留下审计痕迹，否则"上一任去哪了"永远查不到。
            con.execute(
                "UPDATE plans SET template=?,updated_at=?,status='HELD'"
                " WHERE plan_id=?",
                (f"pid={os.getpid()}", now, "orchestrator_lock"))
            return ("taken_over", f"{holder}@{updated}(age={age}s)")

        try:
            verdict, detail = self.store._write_txn(_fn)
        except Exception as e:
            jlog(self.root, "ERROR", "orchestrator_lock_error", error=str(e)[:200])
            self._lock_held = False
            return False
        self._lock_held = verdict in ("acquired", "taken_over")
        if not self._lock_held:
            jlog(self.root, "WARN", "orchestrator_lock_busy", pid=os.getpid(),
                 holder=detail, age_ttl_s=self.lock_ttl_s)
        elif verdict == "taken_over":
            jlog(self.root, "WARN", "orchestrator_lock_taken_over",
                 pid=os.getpid(), previous=detail, ttl_s=self.lock_ttl_s)
            try:
                self.store.alert("P1", "orchestrator_lock_taken_over",
                                 previous=detail[:200], pid=os.getpid())
            except Exception:      # noqa: BLE001
                pass
        return self._lock_held

    def heartbeat_lock(self) -> bool:
        """刷新锁的心跳。返回False表示锁已被别人接管，要收手。

        放在 tick 里而不是独立线程：tick 本身就是心跳（主循环在跑），
        再开一个线程只会多一个"线程活着但循环卡住"的新问题。
        """
        if not self._lock_held:
            return False
        def _fn(con):
            cur = con.execute(
                "SELECT template FROM plans WHERE plan_id=?",
                ("orchestrator_lock",)).fetchone()
            if cur is None or not str(cur[0] or "").endswith(str(os.getpid())):
                return False
            con.execute("UPDATE plans SET updated_at=? WHERE plan_id=?",
                        (now_utc_iso(), "orchestrator_lock"))
            return True
        try:
            self._lock_held = bool(self.store._write_txn(_fn))
        except Exception as e:      # noqa: BLE001
            jlog(self.root, "ERROR", "lock_heartbeat_failed", error=str(e)[:200])
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
                                      expected=True,   # 依赖没满足是常态，不告警
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

    # ---------- 算力档位（动态算力总督） ----------

    def _govern(self) -> dict:
        """每tick采一次算力档位，并把结果写进 max_workers / 保留槽。

        设计§7 的表：ACTIVE=executor1/verifier0/local0（共享1槽，约1/8 CPU），
        IDLE=放开跑。所以 max_workers 不能是构造时的常量——
        否则 governor 存在的意义只是"算一个没人用的数"。
        """
        if self._governor is None:
            return {"profile": "STATIC", "workers": {"executor": self.max_workers},
                    "by": "disabled"}
        st = self._governor.sample()
        w = st["workers"]
        # executor 至少留1个：低到0会让队列彻底停摆，
        # 而"宁可慢一点"和"完全不动"是两个完全不同的失败模式。
        self.max_workers = max(1, int(w.get("executor", 1)))
        self.low_slot_reserve = int(w.get("shared_slot", 0))
        if st.get("switched"):
            jlog(self.root, "INFO", "governor_switch", **{k: v for k, v in st.items()
                 if k in ("profile", "prev", "by", "avail_gb")})
            self.store.record_metric("governor_switch", 1,
                                     span=f"{st['prev']}->{st['profile']}")
        return st

    def dispatch_split(self) -> list[dict]:
        """高优N-1槽 + 低优预留1槽。分段内仍 ORDER BY priority,seq（不内存重排）。

        预留的含义是"低优至少有1个"，不是"高优最多只能拿 N-1 个"。
        之前高优 lane 按 `high_quota = free - reserve` 硬切：
        当 free=1（ACTIVE 档常态）且只有高优任务在等时，
        高优 lane 配额是 0、低优 lane 又要 priority>=5——
        一个 priority=0 的任务两头都够不着，**永远饿死**。
        实测：planner 给管道任务 priority=0，t3_assessment 在 READY 上
        挂了 110 个 tick。
        修法：两 lane 跑完后若还有空槽，把配额饿死的高优任务补上。
        （已派发的变成 RUNNING，不会再被 list_ready_ordered 捡回来，
        所以补发不会重复。）
        """
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
        # 溢出补发：配额饿死的高优任务（free 小到 reserve 吃掉全部时）。
        # reserve 只在"低优真有任务在等且会被挤掉"时有意义；
        # 低优 lane 没东西可派却占着配额，等于白白浪费槽位。
        if len(dispatched) < free:
            for t in self.store.list_ready_ordered(limit=free,
                                                   priority_max=HIGH_PRIORITY_MAX):
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
                             owner=self.owner, **self._executor_kwargs(t))
        except Exception as e:
            jlog(self.root, "ERROR", "executor_raised", plan_id=plan_id,
                 task_id=task_id, error=str(e)[:300])
        return running

    def _executor_kwargs(self, t: dict) -> dict:
        """按任务复杂度选模型，把选中的provider注入执行器。

        之前这里什么都不传，执行器收到 provider=None 直接走无LLM的
        自检路径——于是 build_models/gateway.route 写好了一整套，
        生产路径上从没被调用过。现在接上。

        选不到模型时返回空dict（走自检路径），并记日志说明原因；
        不能静默拿一个错的模型顶上，那会让"复杂任务用了4B"这种问题
        只在事后从产物质量上体现。
        """
        if not self.models:
            return {}
        from core import gateway
        ctx = gateway.build_ctx(self.gw_cfg)
        r = gateway.route(t, ctx)
        provider = gateway.resolve(r, self.models)
        if provider is None:
            jlog(self.root, "WARN", "model_unavailable", task_id=t["task_id"],
                 model=r.get("model"), reason=r.get("reason"),
                 known=sorted(self.models))
            return {}
        jlog(self.root, "INFO", "model_routed", task_id=t["task_id"],
             model=r.get("model"), reason=r.get("reason"),
             complexity=t.get("shard", {}).get("complexity"))
        return {"provider": provider, "search_fn": self._search_fn}

    def promote_retries(self) -> list[dict]:
        """RETRY→READY：退避到点才重排（Phase2§2.1 指数退避 60*2^attempts）。

        没有这一步，验收失败的任务会**永久卡在RETRY**——refresh_ready只管
        PENDING/BLOCKED，没人把RETRY捞回来，整个plan就此停摆。
        """
        out = []
        now = time.time()
        for t in self.store.list_by_status("RETRY"):
            waited = now - _epoch(t["updated_at"])
            delay = lease_mod.backoff_delay_s(t["attempts"])
            if waited < delay:
                continue
            try:
                out.append(self.store.transition(
                    t["plan_id"], t["task_id"], "READY", agent=self.owner,
                    run_id=self._run_id(), expect_version=t["version"],
                    expected=True,      # 退避没到点是常态，不告警
                    payload={"retry_after_s": int(delay),
                              "waited_s": int(waited),
                              "reason": "backoff_elapsed"}))
            except (Conflict, IllegalTransition):
                continue
        return out

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

    def dispatch_verify_async(self) -> list[tuple[str, str]]:
        """把VERIFYING任务丢进异步校验池（评审M7），并捞回上次中断的。

        两条约束：
        - 同一个task不能重复入池，否则两个worker会抢同一个任务双写终态；
        - 只捞**无lease**的VERIFYING（requeue_verify）：有租约说明worker还活着，
          抢过来等于自己和自己打架。
        """
        from core import verify_orchestrator as vmod
        if self.verify_pool is None:
            return []
        pending = vmod.requeue_verify(self.store, run_id=self._run_id())
        out = []
        for t in pending:
            key = (t["plan_id"], t["task_id"])
            if key in self._in_flight:
                continue
            self._in_flight.add(key)
            self.verify_pool._tg.start_soon(self._verify_worker, *key,
                                           self._run_id())
            out.append(key)
        return out

    async def _verify_worker(self, plan_id: str, task_id: str,
                             run_id: str) -> None:
        """校验是阻塞的（跑pytest/数行数），丢线程池执行，
        否则一条慢的python_test会把主循环一起卡住。"""
        from core import verify_orchestrator as vmod
        try:
            await anyio.to_thread.run_sync(
                lambda: vmod.verify(self.store, plan_id, task_id, run_id))
        except Exception as e:      # noqa: BLE001 单个校验炸了不能带崩主循环
            jlog(self.root, "ERROR", "verify_dispatch_failed", plan_id=plan_id,
                 task_id=task_id, error=str(e)[:300])
        finally:
            self._in_flight.discard((plan_id, task_id))

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
                 "promoted": 0, "retried": 0}
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
        stats["retried"] = len(self.promote_retries())
        stats["aged"] = len(self.apply_aging())
        stats["backlog"] = self.backlog_check()
        stats["dispatched"] = len(self.dispatch_split())
        stats["promoted"] = len(self.promote_submitted())
        stats["verify_dispatched"] = len(self.dispatch_verify_async())
        stats["governor"] = self._govern()["profile"]
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
        async def _drive():
            """有池子时在async上下文里跑tick，池子退出时会等所有worker收尾
            （不cancel——中途cancel会让任务永远停在VERIFYING）。"""
            n = 0
            async with self.verify_pool:
                while not self.check_shutdown():
                    if not self.heartbeat_lock():
                        jlog(self.root, "WARN", "lock_lost_stopping",
                             pid=os.getpid())
                        break
                    self.tick()
                    n += 1
                    if self.run_once or (max_ticks is not None and n >= max_ticks):
                        break
                    self.wait_wakeup(self.poll_normal_s)
            return n
        try:
            if self.verify_pool is not None:
                n = anyio.run(_drive)
            else:
                while not self.check_shutdown():
                    if not self.heartbeat_lock():
                        jlog(self.root, "WARN", "lock_lost_stopping",
                             pid=os.getpid())
                        break
                    self.tick()
                    n += 1
                    if self.run_once or (max_ticks is not None and n >= max_ticks):
                        break
                    self.wait_wakeup(self.poll_normal_s)
        finally:
            self.release_lock()
            jlog(self.root, "INFO", "orchestrator_stopped", ticks=n)


def _age_s(ts: str | None) -> float | None:
    """ISO时间戳距今多少秒。解析不了返回None（按"很旧"处理，见调用方）。"""
    if not ts:
        return None
    from datetime import datetime as _dt
    try:
        d = _dt.fromisoformat(str(ts))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds()


def _load_cfg(root: Path, name: str = "schedule.yaml") -> dict:
    """读配置。**解析失败要留痕**，不能静默返回 {}。

    运行时容忍坏配置是对的（一个坏文件不该让编排器起不来），但代价是
    配置错误会**完全隐形**：governor 不启用、租约参数全部回落默认值、
    锁存活期回落，而没有任何报错。我本人就踩过——给 schedule.yaml
    加一个键时多缩进两格，整个文件解析失败，而 612 个测试全绿
    （因为几乎没有测试解析**生产的** schedule.yaml）。
    """
    import yaml
    p = Path(root) / "config" / name
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        jlog(root, "ERROR", "config_parse_failed", file=str(p),
             error=str(e)[:300])
        return {}


def _load_search(root: Path):
    """按 config/search.yaml 造检索器，失败返回None。

    返回None是允许的：没有检索能力的任务（needs_web=false）照常跑，
    真需要检索时ToolBox会明确告诉模型"未配置检索"，而不是让它
    凭记忆编事实。
    """
    try:
        from core.search.base import build as build_search
        return build_search(_load_cfg(root, "search.yaml"))
    except Exception as e:
        jlog(root, "WARN", "search_load_failed", error=str(e)[:200])
        return None


def _load_models(root: Path) -> dict:
    """按 config/gateway.yaml 造模型注册表。

    读不到配置或造不出模型时返回空 dict 而不是抛错：编排器还要能在
    没有模型的环境里跑（自检路径、冒烟）。真要跑LLM时由 resolve 阶段
    报"模型不可用"，那时能准确指出是哪个名字找不到。
    """
    try:
        from core.providers import build_models
        return build_models(_load_cfg(root, "gateway.yaml"))
    except Exception as e:
        jlog(root, "WARN", "gateway_load_failed", error=str(e)[:200])
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
    ap.add_argument("--no-verify-pool", action="store_true",
                    help="不起异步校验池（同步跑，用于单测/排障）")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    from core import executor
    from core.verify_orchestrator import VerifyPool
    store = Store(root)
    pool = None if args.no_verify_pool else VerifyPool(store)
    orch = Orchestrator(root, executor_fn=executor.run_task,
                        run_once=args.once, verify_pool=pool)
    if args.no_lock:
        orch._lock_held = True
    orch.run_forever(max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
