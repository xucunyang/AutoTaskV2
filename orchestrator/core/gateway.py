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

class PrivacyGuard(RoutePolicy):
    name = "privacy_guard"
    def decide(self, task, ctx):
        if getattr(task, "privacy", "public") == "secret":
            return {"provider": "local-ollama", "reason": "privacy_secret"}
        return None

class LatencyGuard(RoutePolicy):
    name = "latency_guard"
    def __init__(self, cooldown_s=300):
        self.muted_until = 0
    def decide(self, task, ctx):
        import time
        st = ctx.get("local_health", {})
        privacy = getattr(task, "privacy", "public")
        if time.time() < self.muted_until:
            if privacy != "secret":
                return {"provider": "online", "reason": "local_cooldown"}
            return None
        if st.get("queue_depth", 0) > ctx.get("queue_threshold", 4) or st.get("fails", 0) >= 3:
            return {"provider": "online", "reason": "local_overload"} if privacy != "secret" else None
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
    local-ollama.models 配了哪些 key。**没配就是本地做不了，走在线**，
    不猜、不降级。
    """
    name = "capability_match"

    def decide(self, task, ctx):
        complexity = getattr(task, "complexity", "simple")
        if getattr(task, "privacy", "public") == "secret":
            return {"provider": "local-ollama", "reason": "privacy_local_only"}
        local_ok = ctx.get("local_complexities")
        if local_ok is None:          # 调用方没给（旧调用方/测试）→ 保守走在线
            local_ok = {"simple"}
        if complexity in local_ok and ctx.get("prefer_local", True):
            if ctx.get("local_usable") is False:
                return {"provider": "online", "reason": "local_window_insufficient"}
            return {"provider": "local-ollama",
                    "reason": f"local_has_{complexity}"}
        return {"provider": "online",
                "reason": f"no_local_model_for_{complexity}"}

def route(task, ctx: dict | None = None, policies: list[RoutePolicy] | None = None) -> dict:
    ctx = ctx or {}
    for p in policies or [PrivacyGuard(), LatencyGuard(), CapabilityMatch()]:
        r = p.decide(task, ctx)
        if r:
            return r
    return {"provider": "online", "reason": "default"}


def local_complexities_from(cfg: dict) -> set[str]:
    """从 gateway.yaml 推"本地能做哪些复杂度"。

    唯一依据是 local-ollama.models 里**实际配了哪些 key**。
    配了 simple 没配 medium → medium 走在线；不去猜模型能不能扛。
    """
    providers = cfg.get("providers") or {}
    local = providers.get("local-ollama") or {}
    if not local.get("enabled", True):
        return set()
    models = local.get("models") or {}
    return {str(k) for k in models.keys() if models.get(k)}


def build_ctx(cfg: dict | None = None, **over) -> dict:
    """组装route()要的上下文。配置从gateway.yaml来，不再靠调用方硬编。"""
    ctx: dict = {"local_complexities": {"simple"}}
    if cfg:
        ctx["local_complexities"] = local_complexities_from(cfg)
        lg = cfg.get("latency_guard") or {}
        ctx["queue_threshold"] = int(lg.get("queue_threshold", 4))
        ctx["fails"] = int(lg.get("fail_threshold", 3))
    ctx.update(over)
    return ctx


def resolve(route_result: dict, providers: dict):
    """路由结果 → 具体provider实例。

    查不到就返回None而不是造一个空provider：静默拿一个不会真调模型的
    假provider，任务会"成功"但产物是空的，比直接报错危险得多。
    """
    return providers.get(route_result.get("provider", ""))


def chat(route_result: dict, providers: dict, prompt: str, budget: dict) -> dict:
    """按路由结果真调一次。契约与Provider.chat一致。"""
    p = resolve(route_result, providers)
    if p is None:
        raise KeyError(f"provider_not_found:{route_result.get('provider')}")
    return p.chat(prompt, budget)
