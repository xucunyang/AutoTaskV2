"""轻量网关：Provider + RoutePolicy 双插件，配置链编排。

Provider实现在core/providers.py（Phase3补齐）；本文件只管"选谁"。
路由可换不改执行器——执行器永远只调 gateway.chat(task_card)。
"""
from __future__ import annotations


class Provider:
    name: str

    def chat(self, prompt: str, budget: dict) -> dict:
        """返回契约固定：{content, usage:{prompt_tokens, completion_tokens}}。
        session切换判定依赖usage.prompt_tokens，字段名不能各provider自定。"""
        raise NotImplementedError

    def health(self) -> dict:
        return {"ok": True, "queue_depth": 0, "p50_ms": 0}

class RoutePolicy:
    name: str
    def decide(self, task, ctx: dict) -> dict | None:
        raise NotImplementedError

# ---------------------------------------------------------------------------
# 路由结果一律是 {"model": <注册表里的模型名>, "reason": <为什么>}
# 不再返回 provider 名：provider 是"怎么连"（代码里的类），
# model 是"用哪个"（配置里的名字）。用户换模型/加模型时，
# 改配置不该牵动代码里的 provider 命名。
# ---------------------------------------------------------------------------

def _model_for(ctx: dict, complexity: str) -> str:
    """复杂度→模型引用。

    兜底顺序：complexity_models → default_model → "flash"。
    任何一步都不该返回 None：路由结果里出现 model=None 会一路飘到
    resolve() 才报"model_not_found:None"，那错误信息指向配置缺失，
    其实只是兜底链断了，排查会被带偏。
    """
    fallback = ctx.get("default_model") or "flash"
    local_ok = ctx.get("local_complexities")
    if local_ok is not None and complexity in local_ok:
        local_name = ctx.get("local_model_name") or "local"
        if ctx.get("prefer_local", True):
            return local_name
        # 调用方显式要求走在线
        return (ctx.get("complexity_models") or {}).get(complexity, fallback)
    return (ctx.get("complexity_models") or {}).get(complexity, fallback)

class PrivacyGuard(RoutePolicy):
    name = "privacy_guard"
    def decide(self, task, ctx):
        if getattr(task, "privacy", "public") == "secret":
            return {"model": ctx.get("secret_model") or ctx.get("local_model_name")
                    or "local", "reason": "privacy_secret"}
        return None

class LatencyGuard(RoutePolicy):
    name = "latency_guard"
    def __init__(self, cooldown_s=300):
        self.muted_until = 0
    def decide(self, task, ctx):
        import time
        st = ctx.get("local_health", {})
        privacy = getattr(task, "privacy", "public")
        online = ctx.get("online_model_name") or ctx.get("default_model")
        if time.time() < self.muted_until:
            if privacy != "secret":
                return {"model": online, "reason": "local_cooldown"}
            return None
        if st.get("queue_depth", 0) > ctx.get("queue_threshold", 4) or st.get("fails", 0) >= 3:
            return {"model": online, "reason": "local_overload"} if privacy != "secret" else None
        return None

class CapabilityMatch(RoutePolicy):
    """按**本地实际配了哪些模型**决定能不能本地跑，而不是写死档位表。

    之前是 `TIERS={simple:0,medium:1,complex:2}` + `LOCAL_MAX=1` 写死，
    两个后果：
    1. 本地只有4B时，medium 仍被判"本地能做"→ 拿小模型跑难任务，
       质量掉了但没人知道（config/local_tiers 写了却没人读，是死配置）；
    2. OllamaProvider.model_for 对未知复杂度会**静默回落到 simple**，
       等于用4B悄悄跑medium。

    现在以 ctx["local_complexities"] 为准——它来自 gateway.yaml 里
    local 这个 ollama 模型的 models 配了哪些 key。**没配就是本地做不了，
    走配置里映射的在线模型**，不猜、不降级。
    """
    name = "capability_match"

    def decide(self, task, ctx):
        complexity = getattr(task, "complexity", "simple")
        if getattr(task, "privacy", "public") == "secret":
            return {"model": ctx.get("secret_model")
                    or ctx.get("local_model_name") or "local",
                    "reason": "privacy_local_only"}
        local_ok = ctx.get("local_complexities")
        if local_ok is None:          # 调用方没给（旧调用方/测试）→ 保守只认simple
            local_ok = {"simple"}
        if complexity in local_ok and ctx.get("prefer_local", True):
            if ctx.get("local_usable") is False:
                return {"model": ctx.get("online_model_name")
                        or ctx.get("default_model"),
                        "reason": "local_window_insufficient"}
            return {"model": ctx.get("local_model_name") or "local",
                    "reason": f"local_has_{complexity}"}
        return {"model": _model_for(ctx, complexity),
                "reason": f"no_local_model_for_{complexity}"}

def route(task, ctx: dict | None = None, policies: list[RoutePolicy] | None = None) -> dict:
    ctx = ctx or {}
    for p in policies or [PrivacyGuard(), LatencyGuard(), CapabilityMatch()]:
        r = p.decide(task, ctx)
        if r:
            return r
    return {"model": ctx.get("default_model") or "flash", "reason": "default"}


def _first_ollama(cfg: dict) -> tuple[str | None, dict]:
    models = cfg.get("models") or {}
    for name, spec in models.items():
        if isinstance(spec, dict) and spec.get("type") == "ollama" \
                and spec.get("enabled", True):
            return name, spec
    return None, {}

def _first_online(cfg: dict) -> tuple[str | None, dict]:
    models = cfg.get("models") or {}
    for name, spec in models.items():
        if isinstance(spec, dict) and spec.get("type") != "ollama" \
                and spec.get("enabled", True):
            return name, spec
    return None, {}

def local_complexities_from(cfg: dict) -> set[str]:
    """从 gateway.yaml 推"本地能做哪些复杂度"。

    唯一依据是 ollama 那条 models 里**实际配了哪些 key**。
    配了 simple 没配 medium → medium 走在线；不去猜模型能不能扛。
    """
    _name, spec = _first_ollama(cfg)
    models = spec.get("models") or {}
    return {str(k) for k in models.keys() if models.get(k)}


def build_ctx(cfg: dict | None = None, **over) -> dict:
    """组装route()要的上下文。配置从gateway.yaml来，不再靠调用方硬编。"""
    ctx: dict = {"local_complexities": {"simple"}}
    if cfg:
        ctx["local_complexities"] = local_complexities_from(cfg)
        local_name, _ = _first_ollama(cfg)
        online_name, _ = _first_online(cfg)
        ctx["local_model_name"] = local_name
        ctx["online_model_name"] = online_name
        ctx["complexity_models"] = dict(cfg.get("complexity_models") or {})
        ctx["default_model"] = cfg.get("default_model") or online_name
        # 隐私强制模型：配置里没写就退回本地（不能外发）
        sec = dict(cfg.get("privacy_models") or {}).get("secret")
        ctx["secret_model"] = sec or local_name
        lg = cfg.get("latency_guard") or {}
        ctx["queue_threshold"] = int(lg.get("queue_threshold", 4))
        ctx["fails"] = int(lg.get("fail_threshold", 3))
    ctx.update(over)
    return ctx


def resolve(route_result: dict, models: dict):
    """路由结果 → 具体provider实例。

    查不到就返回None而不是造一个空provider：静默拿一个不会真调模型的
    假provider，任务会"成功"但产物是空的，比直接报错危险得多。
    """
    return models.get(route_result.get("model", ""))


def chat(route_result: dict, models: dict, prompt: str, budget: dict) -> dict:
    """按路由结果真调一次。契约与Provider.chat一致。"""
    p = resolve(route_result, models)
    if p is None:
        raise KeyError(f"model_not_found:{route_result.get('model')}")
    return p.chat(prompt, budget)
