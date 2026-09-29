"""告警分级与去重（Phase3 §3.2，评审M8）。

设计的核心意图是"**分级告警不骚扰**"，所以下面两条是硬约束，不是建议：

1. **P0 只有三项**：DEAD_LETTER / INCONSISTENT / 高优SLA超时。
   P0是电话+群通知，多一条就稀释真告警的注意力。
   所以级别由 `level_for(kind)` 单一决定，调用方**不能自选级别**——
   否则哪天有人图省事写 alert("P0", whatever) 就破防了。
   请求级别与政策不符时按政策降级，并留痕。

2. **同(task_id, kind) 在去重窗口内合并**，另有每小时上限兜底。
   没有去重的话，一个每30s失败一次的任务一晚上能刷出几千条告警，
   真出事的那条反而被埋掉。

去重状态落state/alerts_state.json（跨进程有效），不是内存字典——
多实例各写各的内存就等于没去重。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.utils import append_jsonl, atomic_write_json, jlog, now_utc_iso

# ---- P0：仅三项（V7§9定版） ----
P0_KINDS = {"dead_letter", "inconsistent", "dispatch_sla_breach"}

# ---- P1：群通知 ----
P1_KINDS = {
    "task_failed", "task_failed_3x", "security_violation", "lease_expired",
    "waiting_approval_timeout", "queue_backlog", "session_switch_fused",
    "planner_split_failed", "final_consistency_failed", "planner_dead_letter",
    "heartbeat_lost", "db_write_failed",
    # 非法跃迁/旧owner写回/坏DAG：说明有东西在试图做不该做的事，或图跑不动了，
    # 都是"需要人看一眼"的级别，不该悄悄沉进日报
    "illegal_transition", "stale_owner_write", "dag_invalid",
}

# ---- P2：只在日报聚合，不即时打扰 ----
P2_KINDS = {
    "task_aged", "skipped_cached", "cache_hit_rate", "slow_task",
    "source_no_published_at", "yield_reason", "schema_reject",
    "db_lock_retry", "governor_switch", "local_unavailable",
    "context_overflow_fallback",
}


def level_for(kind: str) -> str:
    """告警级别的**唯一**决定点。P0严格只认三种kind。"""
    if kind in P0_KINDS:
        return "P0"
    if kind in P1_KINDS:
        return "P1"
    return "P2"          # 未知kind默认最低级：宁可进日报也不要半夜打电话


def is_p0(kind: str) -> bool:
    return kind in P0_KINDS


class AlertPolicy:
    """去重：同(task,kind)窗口内合并 + 每小时上限。"""

    def __init__(self, dedup_window_s: int = 300, max_per_hour: int = 20):
        self.dedup_window_s = dedup_window_s
        self.max_per_hour = max_per_hour

    def key_of(self, kind: str, task_id: str = "-", plan_id: str = "-") -> str:
        return f"{plan_id}/{task_id}/{kind}"

    def check(self, state: dict, key: str, now: float) -> tuple[bool, str]:
        """返回(是否放行, 原因)。"""
        hits = state.get(key, [])
        recent = [t for t in hits if now - t < self.dedup_window_s]
        if recent:
            state[key] = recent
            return False, f"dedup:{len(recent)}in_window"
        hour = [t for t in hits if now - t < 3600]
        if len(hour) >= self.max_per_hour:
            state[key] = hour
            return False, "hourly_cap"
        state[key] = hits + [now]
        return True, "ok"


class Notifier:
    def __init__(self, root: str | Path, policy: AlertPolicy | None = None,
                 printer=print):
        self.root = Path(root)
        self.policy = policy or AlertPolicy()
        self.printer = printer
        self.state_path = self.root / "state" / "alerts_state.json"

    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def save_state(self, state: dict) -> None:
        atomic_write_json(self.state_path, state)

    def send(self, level: str, kind: str, *, task_id: str = "-",
             plan_id: str = "-", body: str = "", run_id: str = "",
             now: float | None = None) -> dict:
        """发一条告警。level是**请求**级别，会被政策纠正。

        返回 {sent, level, requested, kind, reason}，测试与观测都靠它。
        """
        want = str(level).upper()
        actual = level_for(kind)
        state = self.load_state()
        key = self.policy.key_of(kind, task_id, plan_id)
        ts = now if now is not None else time.time()
        allow, reason = self.policy.check(state, key, ts)
        self.save_state(state)
        row = {"ts": now_utc_iso(), "priority": actual, "kind": kind,
               "plan_id": plan_id, "task_id": task_id, "run_id": run_id,
               "body": body[:500]}
        if not allow:
            # 被去重的也要留痕（否则事后查"为什么没告警"没线索）
            append_jsonl(self.root / "reports" / "alerts_suppressed.jsonl",
                         {**row, "suppressed_reason": reason,
                          "requested": want})
            return {"sent": False, "level": actual, "requested": want,
                    "kind": kind, "reason": reason}
        append_jsonl(self.root / "reports" / "alerts.jsonl", row)
        jlog(self.root, "ERROR" if actual in ("P0", "P1") else "WARN",
             f"alert_{actual}", kind=kind, plan_id=plan_id, task_id=task_id,
             run_id=run_id, body=body[:200])
        self.printer(f"[{actual}] {kind} plan={plan_id} task={task_id} "
                     f"{body[:160]}")
        return {"sent": True, "level": actual, "requested": want,
                "kind": kind, "reason": reason,
                "downgraded": want != actual}

    # 便捷入口
    def p0(self, kind: str, **kw) -> dict:
        return self.send("P0", kind, **kw)

    def p1(self, kind: str, **kw) -> dict:
        return self.send("P1", kind, **kw)

    def p2(self, kind: str, **kw) -> dict:
        return self.send("P2", kind, **kw)
