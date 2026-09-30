"""Phase3 路由测试。

路由结果契约是 {"model": <注册表模型名>, "reason": ...}，
不是 {"provider": ...}——provider是"怎么连"，model是"用哪个"。
用户换模型时改 yaml 的 models/complexity_models，不该动代码里的命名。
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import gateway


class T:
    """最小任务替身。route只读 complexity/privacy 两个属性。"""
    def __init__(self, complexity="simple", privacy="public"):
        self.complexity = complexity
        self.privacy = privacy


CFG = yaml.safe_load((Path(__file__).resolve().parents[1]
                      / "config" / "gateway.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 已入库配置

def test_shipped_config_routes_complexity_to_named_models():
    """对着真实 config/gateway.yaml 验：复杂度→模型名的映射如配置所写。
    这条防止'改了 yaml 但路由没跟上'。"""
    ctx = gateway.build_ctx(CFG)
    assert gateway.route(T("simple"), ctx)["model"] == "local"
    assert gateway.route(T("medium"), ctx)["model"] == "flash"
    assert gateway.route(T("complex"), ctx)["model"] == "pro"


def test_shipped_config_model_entries_are_resolvable():
    """每个被路由到的模型名都必须在注册表里存在（拼错名字要炸出来）。"""
    models = set(CFG.get("models") or {})
    ctx = gateway.build_ctx(CFG)
    for c in ("simple", "medium", "complex"):
        assert gateway.route(T(c), ctx)["model"] in models


def test_shipped_config_complexity_models_cover_all_tiers():
    cm = CFG.get("complexity_models") or {}
    for c in ("simple", "medium", "complex"):
        assert c in cm, f"{c} 没配模型，会落到 default_model"


def test_shipped_config_no_dead_provider_keys():
    """providers 已改名 models；残留旧键说明有人按老结构改配置。"""
    assert "providers" not in CFG
    assert "cost_saver" not in (CFG.get("policies") or []), \
        "cost_saver 没有实现类，写在 policies 里是死配置"


# ---------------------------------------------------------------- 复杂度映射

def test_complexity_mapping_is_config_driven():
    """同一份上下文，换一份 complexity_models 就换分配——改配置即可。"""
    cfg = {"models": {"local": {"type": "ollama",
                                "models": {"simple": "m4b"}},
                      "flash": {"type": "openai_compat"}},
           "complexity_models": {"simple": "local", "medium": "flash",
                                 "complex": "flash"}}
    ctx = gateway.build_ctx(cfg)
    assert gateway.route(T("medium"), ctx)["model"] == "flash"
    cfg["complexity_models"]["complex"] = "local"
    ctx2 = gateway.build_ctx(cfg)
    assert gateway.route(T("complex"), ctx2)["model"] == "local"


def test_no_local_model_for_tier_goes_to_configured_model():
    """本地没配 medium → 不能静默拿 simple 跑，也不能硬编码"online"。"""
    ctx = gateway.build_ctx(CFG)
    r = gateway.route(T("medium"), ctx)
    assert r["model"] == "flash"
    assert r["reason"] == "no_local_model_for_medium"


def test_local_usable_false_moves_to_online():
    ctx = gateway.build_ctx(CFG, local_usable=False)
    r = gateway.route(T("simple"), ctx)
    assert r["model"] == "flash"
    assert r["reason"] == "local_window_insufficient"


def test_caller_without_local_complexities_is_conservative():
    """老调用方没给local_complexities → 只认simple，不猜。"""
    assert gateway.route(T("complex"), {})["model"] == "flash"
    assert gateway.route(T("simple"), {})["model"] == "local"


# ---------------------------------------------------------------- 隐私

def test_secret_always_local():
    """secret 不外发：无论复杂度、无论本地多忙都走本地。"""
    ctx = gateway.build_ctx(CFG)
    r = gateway.route(T("complex", "secret"), ctx)
    assert r["model"] == "local" and r["reason"] == "privacy_secret"


def test_secret_model_is_configurable():
    """privacy_models.secret 存在时按配置走（不配就退回本地）。"""
    cfg = {"models": {"local": {"type": "ollama", "models": {"simple": "m"}},
                      "pro": {"type": "openai_compat"}},
           "complexity_models": {"simple": "local", "medium": "pro",
                                 "complex": "pro"},
           "privacy_models": {"secret": "pro"}}
    r = gateway.route(T("simple", "secret"), gateway.build_ctx(cfg))
    assert r["model"] == "pro"      # 配置说pro就pro（内网自建pro也算"不外发"）


def test_secret_beats_latency_guard():
    """本地过载时公开任务切走，secret 任务不许切走。"""
    ctx = gateway.build_ctx(CFG, local_health={"queue_depth": 99})
    assert gateway.route(T("simple", "public"), ctx)["model"] == "flash"
    assert gateway.route(T("simple", "secret"), ctx)["model"] == "local"


def test_latency_guard_reasons():
    ctx = gateway.build_ctx(CFG, local_health={"queue_depth": 99})
    assert gateway.route(T("simple"), ctx)["reason"] == "local_overload"
    ctx2 = gateway.build_ctx(CFG, local_health={"fails": 5})
    assert gateway.route(T("simple"), ctx2)["reason"] == "local_overload"


# ---------------------------------------------------------------- 上下文/窗口

def test_build_ctx_reads_local_capabilities_from_ollama_entry():
    """local_complexities 来自 ollama 那条的 models key，不是 providers。"""
    cfg = {"models": {"local": {"type": "ollama",
                                "models": {"simple": "m4b", "medium": "m7b"}}}}
    assert gateway.local_complexities_from(cfg) == {"simple", "medium"}


def test_disabled_local_makes_nothing_local():
    cfg = {"models": {"local": {"type": "ollama", "enabled": False,
                                "models": {"simple": "m4b"}}}}
    assert gateway.local_complexities_from(cfg) == set()


def test_online_model_name_is_first_non_ollama():
    cfg = {"models": {"local": {"type": "ollama", "models": {"simple": "m"}},
                      "pro": {"type": "openai_compat"},
                      "flash": {"type": "openai_compat"}}}
    assert gateway.build_ctx(cfg)["online_model_name"] == "pro"


# ---------------------------------------------------------------- 解析

def test_resolve_returns_none_for_unknown_model():
    """查不到就None，不造假provider。"""
    assert gateway.resolve({"model": "nope"}, {"local": object()}) is None
    assert gateway.resolve({"model": "local"}, {"local": 42}) == 42


def test_chat_raises_on_unknown_model():
    with pytest.raises(KeyError, match="model_not_found"):
        gateway.chat({"model": "ghost"}, {}, "hi", {})


def test_chat_uses_routed_model():
    class P:
        def chat(self, prompt, budget):
            return {"content": f"via-{self.__class__.__name__}", "usage": {}}
    got = gateway.chat({"model": "flash"}, {"flash": P()}, "hi", {})
    assert got["content"] == "via-P"


def test_route_result_has_no_legacy_provider_key():
    """别再返回 provider 键：resolve()只看model，返回两套会让调用方挑花眼。"""
    r = gateway.route(T("simple"), gateway.build_ctx(CFG))
    assert "provider" not in r
