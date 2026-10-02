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
    """真实 config/gateway.yaml 三档都必须路由到已注册的东西，
    防止"配置写了条路但没通"。"""
    ctx = gateway.build_ctx(CFG)
    models = set(CFG["models"]) | set(CFG.get("chains") or {})
    for c in ("simple", "medium", "complex"):
        assert gateway.route(T(c), ctx)["model"] in models
    # 实际生效的那一跳（链头）必须与原逻辑一致：本地 / 在线快 / 在线强。
    # MiniMax 默认关着，所以链头就是原来的 provider。
    assert gateway.route(T("medium"), ctx)["model"] == "medium_chain"
    assert gateway.route(T("complex"), ctx)["model"] == "complex_chain"
    # simple 特别轻本地qwen3.5:4b 实测能稳定跑薄tool loop；
    # medium/complex 仍走在线：4B 的长文分析与多轮推理质量不够。
    assert gateway.route(T("simple"), ctx)["model"] == "simple_chain"


def test_shipped_config_model_entries_are_resolvable():
    """每档路由结果都必须在注册表或降级链里，不能悬空解析。"""
    models = set(CFG.get("models") or {}) | set(CFG.get("chains") or {})
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


def test_configured_mapping_is_authoritative():
    """complexity_models 写了就是它说了算——"配置即路由"。
    之前是"本地能做就本地"，于是 ollama 配了 simple 之后，
    用户在配置里写 simple:flash 也不起作用：配置写了却不管用，
    比没这个配置更糟。"""
    ctx = gateway.build_ctx(CFG)
    r = gateway.route(T("medium"), ctx)
    # 真实配置下 medium 指向降级链。链的**声明顺序**头是 minimax
    # （yaml 层），但 minimax 默认 enabled: false，build_models 不会注册它，
    # 于是实际链塌缩成 [flash]——两层分工不同，这里只断言路由层。
    assert r["model"] == "medium_chain"
    assert r["chain"][0] == "minimax"
    assert r["chain"][-1] == "flash"      # 兜底必须在，且是原来的在线快模型
    assert r["reason"] == "configured_medium"


def test_explicit_mapping_beats_local_capability():
    """本地配了某个复杂度，配置指向别处时，配置优先。"""
    cfg = {"models": {"local": {"type": "ollama",
                                "models": {"simple": "m4b", "medium": "m7b"}},
                      "pro": {"type": "openai_compat"}},
           "complexity_models": {"simple": "local", "medium": "pro",
                                 "complex": "pro"}}
    ctx = gateway.build_ctx(cfg)
    # 本地明明有 medium，但配置说 pro -> pro
    assert gateway.route(T("medium"), ctx)["model"] == "pro"


def test_no_explicit_mapping_falls_back_to_local_capability():
    """没有显式映射时才退回本地能力推断（老调用方兼容）。"""
    cfg = {"models": {"local": {"type": "ollama",
                                "models": {"simple": "m4b", "medium": "m7b"}},
                      "flash": {"type": "openai_compat"}}}
    ctx = gateway.build_ctx(cfg)          # 没配 complexity_models
    assert gateway.route(T("simple"), ctx)["model"] == "local"
    assert gateway.route(T("medium"), ctx)["model"] == "local"
    assert gateway.route(T("complex"), ctx)["model"] == "flash"


def test_local_usable_false_moves_to_online():
    """没有降级链的旧配置：窗口不够时仍退回裸在线 provider。"""
    ctx = gateway.build_ctx(CFG, local_usable=False)
    cfg2 = {"models": CFG["models"],
            "complexity_models": {"simple": "local", "medium": "flash",
                                  "complex": "pro"},
            "privacy_models": CFG.get("privacy_models")}
    ctx2 = gateway.build_ctx(cfg2, local_usable=False)
    r = gateway.route(T("simple"), ctx2)
    assert r["model"] == "minimax"       # 该 cfg 无 chains → 回退到第一个在线
    assert r["reason"] == "local_window_insufficient"
    assert ctx["local_complexities"] == {"simple"}   # 原ctx未被污染


def test_local_window_insufficient_drops_local_from_chain():
    """窗口装不下时走链的 __online 变体：**去掉 local 那一跳**。

    "本地不可用"有两种，不能混为一谈：
    - 过载/冷却 → 整条链照走（minimax 不行还能回落本地，不必付钱）
    - 窗口装不下 → 把 local 摘掉，否则兜底那跳必然超限失败
    """
    ctx = gateway.build_ctx(CFG, local_usable=False)
    r = gateway.route(T("simple"), ctx)
    assert r["model"] == "simple_chain__online"
    assert r["reason"] == "local_window_insufficient"
    assert "local" not in r["chain"]
    assert r["chain"][0] == "minimax"          # 仍按顺序，不是随便挑


def test_local_overload_keeps_the_whole_chain():
    """过载不该摘掉 local：额度耗尽时本地是免费兜底，没必要付钱。"""
    ctx = gateway.build_ctx(CFG, local_health={"queue_depth": 99})
    r = gateway.route(T("simple"), ctx)
    assert r["model"] == "simple_chain"
    assert r["reason"] == "local_overload"
    assert "local" in r["chain"]


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
    # 公开任务走链（链里仍保留 local 作免费兜底），而不是裸的在线 provider——
    # 裸 provider 会绕过降级链，minimax 额度耗尽时就没有下一跳了。
    assert gateway.route(T("simple", "public"), ctx)["model"] == "simple_chain"
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
