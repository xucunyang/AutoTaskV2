"""Phase0 DoD: 全跃迁正反例（V7：简化抢占模型+让出边RUNNING->READY）。

依据：V7§5状态机 + Phase0设计§2（让出边：reason四值+checkpoint断言+熔断K=5）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schemas.transitions import (
    ALLOWED,
    TERMINAL,
    SYSTEM_ONLY,
    ORCH_ONLY_TRANSITIONS,
    SELF_YIELD_TRANSITIONS,
    YIELD_REASONS,
    SESSION_SWITCH_LIMIT,
    is_allowed,
    is_self_yield,
    is_terminal,
)


def test_happy_path_chain():
    """主路径：PENDING->READY->RUNNING->SUBMITTED->VERIFYING->DONE"""
    for fr, to in [
        ("PENDING", "READY"),
        ("READY", "RUNNING"),
        ("RUNNING", "SUBMITTED"),
        ("SUBMITTED", "VERIFYING"),
        ("VERIFYING", "DONE"),
    ]:
        assert is_allowed(fr, to), f"{fr}->{to}"


def test_no_preempted_state():
    """简化抢占模型：无PREEMPTED状态，RUNNING不可直达RETRY"""
    assert "PREEMPTED" not in ALLOWED
    assert not is_allowed("RUNNING", "PREEMPTED")
    assert not is_allowed("RUNNING", "RETRY")
    assert not is_allowed("RUNNING", "DONE")


def test_yield_edge_positive():
    """V7让出边正例：RUNNING->READY（session切换），reason四值，attempts不变（Store层断言）"""
    assert is_allowed("RUNNING", "READY")
    assert is_self_yield("RUNNING", "READY")
    assert SELF_YIELD_TRANSITIONS == {("RUNNING", "READY")}
    for reason in ("context_full", "steps_exhausted", "timeout", "cancel_requested"):
        assert reason in YIELD_REASONS
    assert SESSION_SWITCH_LIMIT == 5


def test_yield_edge_constraints():
    """让出约束：非法reason拒、熔断超限拒、无checkpoint拒（Store层落码断言，此处理由集完备性）"""
    assert YIELD_REASONS == {"context_full", "steps_exhausted", "timeout", "cancel_requested"}
    assert "preempted_by" not in YIELD_REASONS  # 抢占reason已随简化抢占模型移除


def test_done_has_no_outgoing():
    """DONE无出边，终态不可变（Phase0§2.2）"""
    assert "DONE" not in ALLOWED
    assert is_terminal("DONE")
    for s in ("DEAD_LETTER", "CANCELLED", "SKIPPED", "SKIPPED_CACHED"):
        assert is_terminal(s)
    for s in ("PENDING", "READY", "RUNNING", "SUBMITTED", "VERIFYING", "RETRY", "FAILED"):
        assert not is_terminal(s)


def test_retry_loop():
    """VERIFYING->RETRY->READY，FAILED->RETRY/DEAD_LETTER"""
    assert is_allowed("VERIFYING", "RETRY")
    assert is_allowed("RETRY", "READY")
    assert is_allowed("FAILED", "RETRY")
    assert is_allowed("FAILED", "DEAD_LETTER")


def test_cancel_paths():
    """取消路径：各非终态均可->CANCELLED"""
    for fr in ("PENDING", "BLOCKED", "READY", "SUBMITTED", "WAITING_APPROVAL", "RETRY"):
        assert is_allowed(fr, "CANCELLED"), f"{fr}->CANCELLED"


def test_subagent_permissions():
    """子Agent权限：RUNNING->SUBMITTED/FAILED/READY(让出)；系统跃迁仅orchestrator"""
    sub_allowed = {("RUNNING", "SUBMITTED"), ("RUNNING", "FAILED"), ("RUNNING", "READY")}
    for fr, to in sub_allowed:
        assert is_allowed(fr, to)
    assert ("SUBMITTED", "VERIFYING") in ORCH_ONLY_TRANSITIONS
    assert "VERIFYING" in SYSTEM_ONLY
