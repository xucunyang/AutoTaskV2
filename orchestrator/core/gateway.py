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

def _field(task, name: str, default=None):
    """取任务字段，**同时支持 dict 和对象**。

    之前三处 policy 都用 getattr(task, "complexity", "simple")。
    对 Pydantic 对象没问题，但编排层传进来的是 store.get_task() 的
    **dict**——getattr(dict, "complexity") 取不到属性，会静默返回默认值
    "simple"。后果是所有任务都被当成 simple 路由到本地小模型，
    包括 medium 和 complex。
    这类"取不到就默认"在路由上是特别危险的默认值：它不报错，
    只会让错模型静默跑完整个任务。真实端到端跑一次就抓到了。
    """
    if isinstance(task, dict):
        v = task.get(name, None)
        return default if v is None else v
    return getattr(task, name, default)


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


def _online_target(ctx: dict, complexity: str, *, reason: str = "local_offline",
                   drop_local: bool = False) -> dict:
    """"这件事现在得走在线" → 路由结果。

    必须回**降级链**，不能回裸的 online_model_name：
    直接指 minimax 等于绕过链，于是 minimax 额度耗尽时没有兜底，
    任务直接进 RETRY/死信——恰恰是引入降级链要避免的情况。

    drop_local=True 用于"本地窗口装不下"：那种情况不是换一家就好，
    而是不能把装不下的请求发给本地那条链成员，所以走去掉 local 的
    变体（build_models 会为含 local 的链预建 __online 变体）。
    """
    chains = ctx.get("chains") or {}
    target = (ctx.get("complexity_models") or {}).get(complexity)
    if target in chains:
        if drop_local:
            onl = f"{target}__online"
            if onl in chains:
                return {"model": onl, "reason": reason,
                        "chain": list(chains[onl])}
        return {"model": target, "reason": reason, "chain": list(chains[target])}
    return {"model": ctx.get("online_model_name") or ctx.get("default_model"),
            "reason": reason}

class PrivacyGuard(RoutePolicy):
    name = "privacy_guard"
    def decide(self, task, ctx):
        if _field(task, "privacy", "public") == "secret":
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
        privacy = _field(task, "privacy", "public")
        complexity = _field(task, "complexity", "simple")
        if time.time() < self.muted_until:
            if privacy != "secret":
                return _online_target(ctx, complexity, reason="local_cooldown")
            return None
        if st.get("queue_depth", 0) > ctx.get("queue_threshold", 4) or st.get("fails", 0) >= 3:
            return _online_target(ctx, complexity, reason="local_overload") \
                if privacy != "secret" else None
        return None

class CapabilityMatch(RoutePolicy):
    """复杂度→模型。**以 config 的 complexity_models 为准**。

    之前这里是"本地能做就本地，本地做不了才查映射"，于是
    complexity_models 只是个兜底：ollama 那条配了 simple，
    simple 就永远走本地，用户在配置里写 simple:flash 也不起作用。
    配置写了却不管用，比没这个配置更糟——改配置的人会以为改好了。

    现在的顺序：
    1. complexity_models 里有这个复杂度 → 就用它（配置即路由）
    2. 没有显式映射 → 才退回"本地能力表"推断（老调用方兼容）
    3. 隐私强制永远压过上面两条（secret 不外发）

    原来的写死档位表（TIERS + LOCAL_MAX）已删除：它让"本地只有4B时
    medium 仍被判本地能做"，拿小模型跑难任务，质量掉了没人知道。
    本地能不能做某档只看 ollama 那条 models 里**实际配了哪些 key**，
    没配就是做不了，不猜不降级。
    """
    name = "capability_match"

    def decide(self, task, ctx):
        complexity = _field(task, "complexity", "simple")
        if _field(task, "privacy", "public") == "secret":
            return {"model": ctx.get("secret_model")
                    or ctx.get("local_model_name") or "local",
                    "reason": "privacy_local_only"}
        online = ctx.get("online_model_name") or ctx.get("default_model")
        explicit = (ctx.get("complexity_models") or {}).get(complexity)
        if explicit:
            chain = (ctx.get("chains") or {}).get(explicit)
            if chain:
                # 链成员里含 local 且本地窗口不够 → 整条链换在线，且**去掉 local**。
                # 原来只判 `explicit == local_model_name`，链出现后这个
                # 等式永远不成立，本地窗口不足就没人拦了：请求会带着
                # 装不下的上下文打给 4B，然后超限失败——降级链解决的是
                # 额度问题，不是上下文装不下的问题。
                local_n = ctx.get("local_model_name")
                if local_n and local_n in chain and ctx.get("local_usable") is False:
                    return _online_target(ctx, complexity,
                                          reason="local_window_insufficient",
                                          drop_local=True)
                return {"model": explicit,
                        "reason": f"configured_{complexity}",
                        "chain": list(chain)}
            if explicit == ctx.get("local_model_name") and ctx.get("local_usable") is False:
                return {"model": online, "reason": "local_window_insufficient"}
            return {"model": explicit,
                    "reason": f"configured_{complexity}"}
        # 没有显式映射：退回本地能力推断（老调用方）
        local_ok = ctx.get("local_complexities")
        if local_ok is None:
            local_ok = {"simple"}
        if complexity in local_ok and ctx.get("prefer_local", True):
            if ctx.get("local_usable") is False:
                return {"model": online, "reason": "local_window_insufficient"}
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


def models_of(cfg: dict) -> dict:
    """配置里的模型注册表（区别于真正 build 出来的 provider 实例）。"""
    return cfg.get("models") or {}


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
        # 降级链原样带进 ctx：路由要判断"这条链里有没有 local"，
        # 以便本地窗口不足时整条链换在线。
        chains = {k: list(v or []) for k, v in (cfg.get("chains") or {}).items()}
        # __online 变体（去掉本地成员后的链）由 build_models 预建，
        # 这里补进 ctx 让路由层能选中。成员名沿用去掉本地后的顺序。
        local_names = {n for n, s in (models_of(cfg)).items()
                       if isinstance(s, dict) and s.get("type") == "ollama"}
        for name, members in list(chains.items()):
            rest = [m for m in members if m not in local_names]
            if rest and len(rest) < len(members):
                chains[f"{name}__online"] = rest
        ctx["chains"] = chains
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
