"""gateway路由单测：本地能力以**配置里配了哪些模型**为准，不写死档位表。"""
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gateway import (
    CapabilityMatch,
    build_ctx,
    local_complexities_from,
    route,
)


def _task(complexity="simple", privacy="public"):
    return SimpleNamespace(complexity=complexity, privacy=privacy)


def test_gate_blocks_local_when_window_insufficient():
    p = CapabilityMatch()
    r = p.decide(_task("simple"), {"local_complexities": {"simple"},
                                   "prefer_local": True, "local_usable": False})
    assert r == {"provider": "online", "reason": "local_window_insufficient"}


def test_gate_passes_when_usable():
    p = CapabilityMatch()
    r = p.decide(_task("simple"), {"local_complexities": {"simple"},
                                   "prefer_local": True, "local_usable": True})
    assert r == {"provider": "local-ollama", "reason": "local_has_simple"}


def test_medium_goes_online_when_not_configured_locally():
    """核心改动：本地没配medium → 走在线。
    此前写死LOCAL_MAX=1，medium一律判"本地能做"，拿小模型扛难任务。"""
    p = CapabilityMatch()
    r = p.decide(_task("medium"), {"local_complexities": {"simple"},
                                   "local_usable": True})
    assert r == {"provider": "online", "reason": "no_local_model_for_medium"}


def test_medium_goes_local_when_configured():
    p = CapabilityMatch()
    r = p.decide(_task("medium"), {"local_complexities": {"simple", "medium"},
                                   "local_usable": True})
    assert r["provider"] == "local-ollama"


def test_missing_ctx_is_conservative():
    """调用方没传local_complexities → 保守只认simple，不猜。"""
    p = CapabilityMatch()
    assert p.decide(_task("medium"), {})["provider"] == "online"
    assert p.decide(_task("simple"), {})["provider"] == "local-ollama"


def test_privacy_secret_bypasses_gate():
    # 隐私任务永不上云：即使本地窗口不足也不改走在线（宁可让出重试）
    p = CapabilityMatch()
    r = p.decide(_task("simple", privacy="secret"),
                 {"local_complexities": {"simple"}, "local_usable": False})
    assert r == {"provider": "local-ollama", "reason": "privacy_local_only"}


def test_complex_always_online():
    p = CapabilityMatch()
    r = p.decide(_task("complex"), {"local_complexities": {"simple"},
                                    "local_usable": True})
    assert r["provider"] == "online"


def test_route_chain_uses_gate():
    r = route(_task("simple"), {"local_complexities": {"simple"},
                                "prefer_local": True, "local_usable": False,
                                "local_health": {}, "queue_threshold": 4})
    assert r == {"provider": "online", "reason": "local_window_insufficient"}


# ---------- 配置驱动：local_complexities 从 models 实际配了什么推出来 ----------

def _cfg(models: dict) -> dict:
    return {"providers": {"local-ollama": {"type": "ollama",
                                            "models": models, "enabled": True}}}


def test_local_complexities_from_config():
    assert local_complexities_from(_cfg({"simple": "q4b"})) == {"simple"}
    assert local_complexities_from(
        _cfg({"simple": "q4b", "medium": "q7b"})) == {"simple", "medium"}


def test_local_complexities_empty_when_disabled():
    cfg = _cfg({"simple": "q4b"})
    cfg["providers"]["local-ollama"]["enabled"] = False
    assert local_complexities_from(cfg) == set()


def test_blank_model_value_not_counted():
    """配了key但值为空 = 没配这个能力，不能算本地能做。"""
    assert local_complexities_from(_cfg({"simple": "q4b", "medium": ""})) == {"simple"}


def test_build_ctx_reads_real_gateway_config():
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config" / "gateway.yaml").read_text(encoding="utf-8"))
    ctx = build_ctx(cfg)
    # 仓库当前配置：只配了simple → medium/complex 走在线
    assert ctx["local_complexities"] == {"simple"}
    assert ctx["queue_threshold"] == 4


def test_shipped_config_routes_medium_online():
    """对着仓库真实配置验证：medium 确实走在线（用户明确要求）。"""
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "config" / "gateway.yaml").read_text(encoding="utf-8"))
    ctx = build_ctx(cfg, local_usable=True)
    assert route(_task("simple"), ctx)["provider"] == "local-ollama"
    assert route(_task("medium"), ctx)["provider"] == "online"
    assert route(_task("complex"), ctx)["provider"] == "online"
