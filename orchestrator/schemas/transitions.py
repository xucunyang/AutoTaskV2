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
    # DEAD_LETTER→RETRY：**只有显式的运维动作会走这条边**
    # （Store.reopen_dead_letter / `scheduler --rerun`），必须带 reason+sop_ref
    # 且重置 attempts。
    #
    # 为什么不留成"完全无出边"：那样死信就**无法恢复**——只能手工改库，
    # 而改库会让 version 与 events 对不上，replay 一致性校验随即失败
    # （SOP §5 明确禁止）。也就是说"无出边"实际上等于"死信是永久的"，
    # 除非有人去破坏数据完整性。
    #
    # 为什么这不破坏"终态"语义：TERMINAL 的含义是**系统不会自动再处理它**
    # （escalate 只扫 FAILED、promote_retries 只扫 RETRY、
    #  refresh_ready 只处理 PENDING/BLOCKED）——没有任何自动路径离开死信。
    # 离开它的只有一个：人明确说"我修好了，重来"。
    "DEAD_LETTER": {"RETRY"},
}
# TERMINAL = 终态：**没有自动出边**。可以有显式的运维 reopen（见上）。
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

# 权限矩阵（Phase0§2.2，唯一来源，store.transition强制落码）
ROLES = {"orchestrator", "subagent", "verifier"}
# 子Agent只允许：执行产物/失败/主动让出（V7无回退重试权，重试由系统决定）
SUBAGENT_TRANSITIONS = {("RUNNING", "SUBMITTED"), ("RUNNING", "FAILED"),
                        ("RUNNING", "READY")}
# Verifier只允许：VERIFYING的四个出边
VERIFIER_TRANSITIONS = {("VERIFYING", "DONE"), ("VERIFYING", "RETRY"),
                        ("VERIFYING", "FAILED"), ("VERIFYING", "WAITING_APPROVAL")}

def role_allowed(role: str, fr: str, to: str) -> tuple[bool, str]:
    """校验role能否写这条边；返回(是否允许, 拒绝原因)。orchestrator拥有全部系统跃迁。"""
    if role not in ROLES:
        return False, f"unknown_role:{role}"
    if role == "orchestrator":
        return True, ""
    if to in SYSTEM_ONLY:
        return False, f"system_only:{to}"
    if (fr, to) in ORCH_ONLY_TRANSITIONS:
        return False, f"orch_only:{fr}->{to}"
    if role == "subagent":
        if (fr, to) not in SUBAGENT_TRANSITIONS:
            return False, f"subagent_cannot:{fr}->{to}"
    else:  # verifier
        if (fr, to) not in VERIFIER_TRANSITIONS:
            return False, f"verifier_cannot:{fr}->{to}"
    return True, ""
