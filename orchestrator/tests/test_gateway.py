"""gateway本地门禁单测：usable低于min_usable→改走在线；缺省保持旧行为。"""
import sys
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gateway import CapabilityMatch, route


def _task(complexity="simple", privacy="public"):
    return SimpleNamespace(complexity=complexity, privacy=privacy)


def test_gate_blocks_local_when_window_insufficient():
    p = CapabilityMatch()
    r = p.decide(_task("simple"), {"prefer_local": True, "local_usable": False})
    assert r == {"provider": "online", "reason": "local_window_insufficient"}


def test_gate_passes_when_usable():
    p = CapabilityMatch()
    r = p.decide(_task("simple"), {"prefer_local": True, "local_usable": True})
    assert r == {"provider": "local-ollama", "reason": "capability_simple"}


def test_gate_default_keeps_old_behavior():
    # ctx无local_usable键 → 保持旧行为（不因调用方未传参而全切在线）
    p = CapabilityMatch()
    r = p.decide(_task("medium"), {})
    assert r == {"provider": "local-ollama", "reason": "capability_medium"}


def test_privacy_secret_bypasses_gate():
    # 隐私任务永不上云：即使本地窗口不足也不改走在线（宁可让出重试）
    p = CapabilityMatch()
    r = p.decide(_task("simple", privacy="secret"), {"local_usable": False})
    assert r == {"provider": "local-ollama", "reason": "privacy_local_only"}


def test_complex_always_online():
    p = CapabilityMatch()
    r = p.decide(_task("complex"), {"local_usable": True})
    assert r["provider"] == "online"


def test_route_chain_uses_gate():
    r = route(_task("simple"), {"prefer_local": True, "local_usable": False,
                                "local_health": {}, "queue_threshold": 4})
    assert r == {"provider": "online", "reason": "local_window_insufficient"}
