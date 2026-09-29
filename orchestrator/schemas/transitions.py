"""状态机跃迁表：唯一合法来源，store.py强制校验（V7：简化抢占模型+让出边）。"""
ALLOWED = {
    "PENDING": {"READY", "CANCELLED", "SKIPPED", "BLOCKED"},
    "BLOCKED": {"READY", "CANCELLED", "SKIPPED"},
    "READY": {"RUNNING", "CANCELLED", "SKIPPED", "SKIPPED_CACHED"},
    # RUNNING→READY为让出边（session切换）：子Agent主动让出，attempts不变，须带checkpoint
    "RUNNING": {"SUBMITTED", "FAILED", "CANCELLED", "READY"},
    "SUBMITTED": {"VERIFYING", "CANCELLED"},
    "VERIFYING": {"DONE", "RETRY", "FAILED", "WAITING_APPROVAL"},
    "WAITING_APPROVAL": {"DONE", "RETRY", "CANCELLED"},
    "RETRY": {"READY", "CANCELLED"},
    "FAILED": {"RETRY", "DEAD_LETTER"},
}
TERMINAL = {"DONE", "DEAD_LETTER", "CANCELLED", "SKIPPED", "SKIPPED_CACHED"}
# 权限矩阵：仅orchestrator可写系统跃迁，子Agent仅RUNNING->SUBMITTED/FAILED/READY(让出)
SYSTEM_ONLY = {"VERIFYING"}
ORCH_ONLY_TRANSITIONS = {("SUBMITTED", "VERIFYING")}
# V7让出边：仅持有正确fencing的owner可写，须带reason+checkpoint断言，受session_switch_total熔断
SELF_YIELD_TRANSITIONS = {("RUNNING", "READY")}
YIELD_REASONS = {"context_full", "steps_exhausted", "timeout", "cancel_requested", "context_overflow"}
# context_overflow：撞context_length_exceeded硬错（窗口配错或被静默截断）→ 强制让出 + 水位线自适应下调
SESSION_SWITCH_LIMIT = 5

def is_allowed(fr: str, to: str) -> bool:
    return to in ALLOWED.get(fr, set())

def is_terminal(s: str) -> bool:
    return s in TERMINAL

def is_self_yield(fr: str, to: str) -> bool:
    return (fr, to) in SELF_YIELD_TRANSITIONS
