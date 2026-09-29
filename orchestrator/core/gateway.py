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
        if time.time() < self.muted_until:
            if getattr(task, "privacy", "public") != "secret":
                return {"provider": "online", "reason": "local_cooldown"}
            return None
        if st.get("queue_depth", 0) > ctx.get("queue_threshold", 4) or st.get("fails", 0) >= 3:
            return {"provider": "online", "reason": "local_overload"} if task.privacy != "secret" else None
        return None

class CapabilityMatch(RoutePolicy):
    name = "capability_match"
    TIERS = {"simple": 0, "medium": 1, "complex": 2}
    LOCAL_MAX = 1  # 7B -> medium
    def decide(self, task, ctx):
        need = self.TIERS.get(task.complexity, 0)
        if task.privacy == "secret":
            return {"provider": "local-ollama", "reason": "privacy_local_only"}
        if need <= self.LOCAL_MAX and ctx.get("prefer_local", True):
            # V7本地门禁：usable低于min_usable→本地实际不可用，改走在线；
            # 缺省（ctx无local_usable键）保持旧行为，避免调用方未传参时全切在线
            if ctx.get("local_usable") is False:
                return {"provider": "online", "reason": "local_window_insufficient"}
            return {"provider": "local-ollama", "reason": f"capability_{task.complexity}"}
        return {"provider": "online", "reason": f"capability_{task.complexity}_online"}

def route(task, ctx: dict | None = None, policies: list[RoutePolicy] | None = None) -> dict:
    ctx = ctx or {}
    for p in policies or [PrivacyGuard(), LatencyGuard(), CapabilityMatch()]:
        r = p.decide(task, ctx)
        if r:
            return r
    return {"provider": "online", "reason": "default"}


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
