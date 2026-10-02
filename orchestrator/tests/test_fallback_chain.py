"""降级链（FallbackProvider）+ MiniMax 注册表接入。

要锁住的不变量（每条都对应一个具体的坏后果）：
1. minimax 没配 key 时**不注册**，链自动塌缩成原来的 provider
2. 额度/限流才降级；模型名写错、上下文超长这类永久错误**不降级**
3. 降级必须留痕（served_by / fell_from），否则产物是低档模型写的没人知道
4. 最后一跳失败要把错误原样抛上去（走既有 RETRY/死信语义）
5. health 取下一跳，不能让熔断的第一跳把整条链判死
6. 每跳各自记账：不能共用一个并发闸门
7. 本地窗口不足时整条含 local 的链换在线
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from core import gateway
from core.providers import (CircuitOpen, FallbackProvider, ProviderError,
                            build_models, is_limit_error)


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config" / "gateway.yaml").read_text(encoding="utf-8"))


class Task:
    def __init__(self, complexity="simple", privacy="public"):
        self.complexity = complexity
        self.privacy = privacy


def _spec(kind, **kw):
    """造一个可控的假 provider：按脚本抛错或返回。"""
    class _P:
        name = kind

        def __init__(self):
            self.script = kw.pop("script", None)
            self.calls = 0
            self.failures = 0
            self.p50_ms = []
            self.last_permanent = ""
            self._opened = 0.0
            self.seen = []

        def chat(self, prompt, budget, **k):
            self.calls += 1
            self.seen.append(prompt)
            if self.script:
                err = self.script.pop(0) if isinstance(self.script, list) else self.script
                if err:
                    self.failures += 1
                    raise err
            self.p50_ms.append(12.0)
            return {"content": f"{kind}-out", "usage": {"prompt_tokens": 10,
                                                       "completion_tokens": 5}}

        def health(self):
            return {"ok": not self._opened, "circuit_opened": bool(self._opened),
                    "fails": self.failures, "calls": self.calls,
                    "queue_depth": 0, "p50_ms": 12}

    return _P()


LIMIT = ProviderError("http_429:rate limit exceeded", status=429, permanent=False)
QUOTA_403 = ProviderError("http_403:insufficient balance", status=403, permanent=True)
BAD_MODEL = ProviderError("http_400:model not found", status=400, permanent=True)
CTX_OVER = ProviderError("http_400:exceeds available context size", status=400,
                         permanent=False)


# ------------------------------------------------------------------ 限流判据

def test_429_is_limit():
    assert is_limit_error(LIMIT) is True


def test_quota_text_on_403_counts_as_limit():
    """配额型套餐常给 403 + 文本说明，不给 429。"""
    assert is_limit_error(QUOTA_403) is True


def test_model_not_found_is_not_a_limit():
    """模型名写错换 provider 也没用——换过去一样错。"""
    assert is_limit_error(BAD_MODEL) is False


def test_context_overflow_is_not_a_limit():
    """上下文装不下是上下文的事，换模型只会把更大的上下文送出去。"""
    assert is_limit_error(CTX_OVER) is False


def test_circuit_open_counts_as_limit():
    """已熔断 = 现在不可用，换下一跳合理。"""
    assert is_limit_error(CircuitOpen("x:circuit_open")) is True


def test_plain_network_error_is_not_a_limit():
    assert is_limit_error(ProviderError("boom")) is False


# ------------------------------------------------------------------ 降级链行为

def test_primary_used_when_healthy():
    a, b = _spec("minimax"), _spec("flash")
    fb = FallbackProvider([a, b], name="c")
    out = fb.chat("p", {})
    assert out["content"] == "minimax-out"
    assert out["served_by"] == "minimax"
    assert b.calls == 0


def test_falls_back_on_429_and_records_it():
    a, b = _spec("minimax", script=LIMIT), _spec("flash")
    fb = FallbackProvider([a, b], name="c")
    out = fb.chat("p", {})
    assert out["content"] == "flash-out"
    assert out["served_by"] == "flash"
    assert out["fell_from"] == ["minimax"]      # 降级事实必须留痕
    assert fb.attempts[-1]["reason"] == "fallback"


def test_does_not_fall_back_on_bad_model_name():
    """配置错误不降级：换一家也是错的，白白把所有 provider 熔断一遍。"""
    a, b = _spec("minimax", script=BAD_MODEL), _spec("flash")
    fb = FallbackProvider([a, b], name="c")
    with pytest.raises(ProviderError):
        fb.chat("p", {})
    assert b.calls == 0


def test_does_not_fall_back_on_context_overflow():
    a, b = _spec("minimax", script=CTX_OVER), _spec("local")
    fb = FallbackProvider([a, b], name="c")
    with pytest.raises(ProviderError):
        fb.chat("p", {})
    assert b.calls == 0


def test_last_hop_failure_propagates_original_error():
    """最后一跳还不行 → 原样抛出，让上层按既有 RETRY/死信处理。"""
    a, b = _spec("minimax", script=LIMIT), _spec("flash", script=LIMIT)
    fb = FallbackProvider([a, b], name="c")
    with pytest.raises(ProviderError) as ei:
        fb.chat("p", {})
    assert "rate limit" in str(ei.value)
    assert fb.attempts[-1]["reason"] == "exhausted"


def test_recovers_when_first_hop_becomes_healthy():
    """额度恢复后要能自动回到首选，否则永远停在降级档。"""
    a = _spec("minimax", script=[LIMIT])
    b = _spec("flash")
    fb = FallbackProvider([a, b], name="c")
    assert fb.chat("p", {})["served_by"] == "flash"
    a.script = None
    assert fb.chat("p", {})["served_by"] == "minimax"


def test_single_hop_chain_is_valid():
    """塌缩成单跳也要能用（minimax 没配 key 时就是这个形状）。"""
    a = _spec("flash")
    fb = FallbackProvider([a], name="c")
    assert fb.chat("p", {})["served_by"] == "flash"


def test_empty_chain_rejected():
    with pytest.raises(ValueError):
        FallbackProvider([], name="c")


def test_health_reflects_a_healthy_later_hop():
    """第一跳熔断但后面是好的 → 整条链必须报 ok。

    否则 latency_guard 看到 fails 达标就把流量全切走，
    而后面的 provider 明明可用。
    """
    a, b = _spec("minimax"), _spec("flash")
    a._opened = 1.0
    fb = FallbackProvider([a, b], name="c")
    h = fb.health()
    assert h["ok"] is True
    assert h["chain"] == ["minimax", "flash"]


def test_health_false_only_when_every_hop_is_down():
    a, b = _spec("minimax"), _spec("flash")
    a._opened = b._opened = 1.0
    assert FallbackProvider([a, b], name="c").health()["ok"] is False


def test_each_hop_keeps_its_own_circuit_state():
    """各跳熔断必须独立：在外面包一层共用信号量的话，
    local 的 max_concurrency=1 会连带限死在付费通道上。"""
    a, b = _spec("minimax"), _spec("local")
    fb = FallbackProvider([a, b], name="c")
    fb.chat("p", {})
    assert a.failures == 0 and b.failures == 0
    assert a.calls == 1 and b.calls == 0
    # 降级过一次之后，两跳的记账各自独立
    a.script = LIMIT
    fb.chat("p", {})
    assert a.failures == 1
    assert b.calls == 1


def test_chat_forwards_tool_loop_kwargs():
    """tool loop 靠 messages/tools 关键字，链不能吞掉。"""
    got = {}

    class _P:
        name = "minimax"

        def chat(self, prompt, budget, **kw):
            got.update(kw)
            return {"content": "x", "usage": {"prompt_tokens": 1,
                                              "completion_tokens": 1}}

        def health(self):
            return {"ok": True}

    FallbackProvider([_P()], name="c").chat("p", {}, messages=[{"role": "user",
                                                              "content": "m"}],
                                            tools=[{"type": "function"}])
    assert got["messages"] == [{"role": "user", "content": "m"}]
    assert got["tools"] == [{"type": "function"}]


def test_tool_call_encoding_follows_the_serving_hop():
    """回填方言要问实际服务的那一跳。

    链里混 ollama 时 arguments 必须是 dict；一律用 OpenAI 形状（JSON字符串）
    会 400，症状看起来像"模型生成的 JSON 坏了"，查错方向完全跑偏。
    """
    from core.providers import OllamaProvider, OpenAICompatProvider
    calls = [{"id": "c1", "name": "atomic_write",
              "arguments": {"path": "a.md", "content": "x"}}]
    fb = FallbackProvider([_spec("minimax")], name="c")
    # 桩没有 encode 方法 → 退回 OpenAI 形状
    assert isinstance(fb.encode_assistant_tool_calls(calls)[0]["function"]["arguments"],
                      str)
    # ollama 那一跳要 dict
    fb2 = FallbackProvider([OllamaProvider(models={"simple": "m"})], name="c")
    assert isinstance(fb2.encode_assistant_tool_calls(calls)[0]["function"]["arguments"],
                      dict)


def test_missing_attributes_forward_to_primary():
    """context.py 之类会问 provider 要 model 等属性；不能因为包了一层
    就变成 AttributeError（那看起来像 provider 写坏了）。"""
    p = _spec("minimax")
    p.model = "MiniMax-M3"
    fb = FallbackProvider([p], name="c")
    assert fb.model == "MiniMax-M3"
    with pytest.raises(AttributeError):
        fb._definitely_not_here


# ------------------------------------------------------------------ 注册表

def test_minimax_not_registered_without_key():
    """默认配置（enabled: false）下 registry 里不该有 minimax。"""
    m = build_models(CFG, env={"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
                               "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u"})
    assert "minimax" not in m
    assert "flash" in m and "pro" in m and "local" in m


def test_chain_collapses_to_original_when_minimax_absent():
    """核心保证：加了 MiniMax 但没启用时，行为与改动前逐跳一致。"""
    m = build_models(CFG, env={"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
                               "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u"})
    assert m["simple_chain"]._names() == ["local"]
    assert m["medium_chain"]._names() == ["flash"]
    assert m["complex_chain"]._names() == ["pro"]


def test_minimax_leads_all_chains_when_enabled():
    env = {"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
           "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u",
           "MINIMAX_API_KEY": "mk", "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
           "MINIMAX_MODEL": "MiniMax-M3"}
    cfg = yaml.safe_load(yaml.safe_dump(CFG))
    cfg["models"]["minimax"]["enabled"] = True
    m = build_models(cfg, env=env)
    assert m["simple_chain"]._names() == ["minimax", "local"]
    assert m["medium_chain"]._names() == ["minimax", "flash"]
    assert m["complex_chain"]._names() == ["minimax", "pro"]


def test_minimax_uses_configured_model_and_url():
    """按 .env 真实配的值组装：url 不能叠成 /v1/v1，模型名不能是空串。"""
    env = {"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
           "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u",
           "MINIMAX_API_KEY": "mk",
           "MINIMAX_BASE_URL": "https://api.minimax.cn/v1",
           "MINIMAX_MODEL": "MiniMax-M3"}
    cfg = yaml.safe_load(yaml.safe_dump(CFG))
    cfg["models"]["minimax"]["enabled"] = True
    p = build_models(cfg, env=env)["minimax"]
    assert p.model == "MiniMax-M3"       # ${VAR} 真的展开了，没变成空串
    assert p.base_url + p.path == "https://api.minimax.cn/v1/chat/completions"
    assert "/v1/v1" not in p.base_url + p.path
    assert p.api_key == "mk"


def test_chain_collapses_when_minimax_disabled():
    """把 minimax 关掉时链必须塌缩回原逻辑（而不是留下一个空跳）。"""
    cfg = yaml.safe_load(yaml.safe_dump(CFG))
    cfg["models"]["minimax"]["enabled"] = False
    m = build_models(cfg, env={"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
                               "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u",
                               "MINIMAX_API_KEY": "mk"})
    assert m["medium_chain"]._names() == ["flash"]
    assert m["complex_chain"]._names() == ["pro"]
    assert m["simple_chain"]._names() == ["local"]


def test_online_variant_exists_only_when_local_is_a_member():
    """__online 变体只对含本地的链生成，别给纯在线链造一份多余的。"""
    m = build_models(CFG, env={"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
                               "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u"})
    # minimax 关着时 simple 塌缩成 [local]，此时没有"在线"可退，不该造变体
    assert "simple_chain__online" not in m
    cfg = yaml.safe_load(yaml.safe_dump(CFG))
    cfg["models"]["minimax"]["enabled"] = True
    m2 = build_models(cfg, env={"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
                                "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u",
                                "MINIMAX_API_KEY": "mk",
                                "MINIMAX_BASE_URL": "u", "MINIMAX_MODEL": "m"})
    assert "simple_chain__online" in m2
    assert m2["simple_chain__online"]._names() == ["minimax"]
    assert "medium_chain__online" not in m2     # medium 链本来就不含 local


def test_online_provider_without_key_is_not_registered():
    """缺 key 的在线 provider 不能注册：注册了会被路由选中然后 401，
    让熔断/退避/fallback 为一次"其实压根没配"的错误买单。"""
    cfg = {"models": {"pro": {"type": "openai_compat", "api_key_env": "NOPE_KEY",
                              "base_url": "u", "model": "m"}}}
    assert "pro" not in build_models(cfg, env={})


def test_chain_reports_skipped_members():
    """链里点名要用的 provider 没注册 → 必须留痕。"""
    cfg = {"models": {"flash": {"type": "openai_compat", "api_key_env": "K",
                                "base_url": "u", "model": "m"}},
           "chains": {"c": ["minimax", "flash"]}}
    m = build_models(cfg, env={"K": "k"})
    assert m["c"]._names() == ["flash"]


def test_skip_report_does_not_claim_registered_members_are_skipped(capsys):
    """告警文案不能把已注册的成员误报成 skip。

    比对时拿的是注册表 out 的键（字符串），不是 provider 实例——
    拿实例比字符串永远不相等，于是每条链都会多报一堆假 skip，
    真出现"额度耗尽没回落"时这条日志就没法看了。
    """
    cfg = {"models": {"flash": {"type": "openai_compat", "api_key_env": "K",
                                "base_url": "u", "model": "m"},
                      "minimax": {"type": "openai_compat", "api_key_env": "NOPE",
                                  "base_url": "u", "model": "m"}},
           "chains": {"c": ["minimax", "flash"]}}
    build_models(cfg, env={"K": "k"})
    err = capsys.readouterr().err
    assert "skip ['minimax']" in err
    assert "flash" not in err.split("effective=")[0].split("skip")[-1]


# ------------------------------------------------------------------ 路由层

def test_route_returns_chain_for_each_tier():
    ctx = gateway.build_ctx(CFG)
    for c, chain in (("simple", "simple_chain"), ("medium", "medium_chain"),
                     ("complex", "complex_chain")):
        r = gateway.route(Task(c), ctx)
        assert r["model"] == chain
        assert r["chain"]


def test_chain_with_local_moves_online_when_window_insufficient():
    """本地窗口不足时走去掉 local 的变体。

    只判 `explicit == local_model_name` 的话，链出现后这个等式永不成立，
    请求会带着装不下的上下文打给 4B 然后超限失败——链解决的是额度问题，
    不是上下文装不下的问题。
    """
    cfg = {"models": {"local": {"type": "ollama", "models": {"simple": "m"}},
                      "flash": {"type": "openai_compat"}},
           "complexity_models": {"simple": "simple_chain"},
           "chains": {"simple_chain": ["minimax", "local"]}}
    r = gateway.route(Task("simple"), gateway.build_ctx(cfg, local_usable=False))
    assert r["model"] == "simple_chain__online"
    assert r["reason"] == "local_window_insufficient"
    assert "local" not in r["chain"]


def test_chain_without_local_is_unaffected_by_window_check():
    cfg = {"models": {"local": {"type": "ollama", "models": {"simple": "m"}},
                      "flash": {"type": "openai_compat"}},
           "complexity_models": {"simple": "simple_chain"},
           "chains": {"simple_chain": ["minimax", "flash"]}}
    r = gateway.route(Task("simple"), gateway.build_ctx(cfg, local_usable=False))
    assert r["model"] == "simple_chain"


def test_secret_task_never_goes_to_a_chain_with_online_members():
    """涉密任务不能经降级链外发：链头是 minimax 就等于泄密。"""
    cfg = {"models": {"local": {"type": "ollama", "models": {"simple": "m"}},
                      "minimax": {"type": "openai_compat"}},
           "complexity_models": {"simple": "simple_chain"},
           "chains": {"simple_chain": ["minimax", "local"]},
           "privacy_models": {"secret": "local"}}
    r = gateway.route(Task("simple", "secret"), gateway.build_ctx(cfg))
    assert r["model"] == "local"
    assert "local" not in r.get("chain", [])


def test_resolve_chain_returns_fallback_provider():
    m = build_models(CFG, env={"ONLINE_API_KEY": "k", "ONLINE_FLASH_MODEL": "f",
                               "ONLINE_PRO_MODEL": "p", "ONLINE_BASE_URL": "u"})
    ctx = gateway.build_ctx(CFG)
    r = gateway.route(Task("medium"), ctx)
    p = gateway.resolve(r, m)
    assert isinstance(p, FallbackProvider)


# ------------------------------------------------------------------ extra_body

def test_extra_body_is_sent_but_only_whitelisted():
    """厂商特有参数（MiniMax thinking）要能下发，但不能让 yaml
    变成任意 JSON 注入口。"""
    p = build_models({"models": {"m": {
        "type": "openai_compat", "api_key_env": "K", "base_url": "u",
        "model": "mm", "extra_body": {"thinking": {"type": "disabled"},
                                      "evil": "rm -rf /"}}}},
        env={"K": "k"})["m"]
    assert p.extra_body == {"thinking": {"type": "disabled"}}


def test_extra_body_lands_in_request_body():
    """真打一次本地 HTTP server，确认字段真的进了请求体。"""
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen.update(json.loads(self.rfile.read(n) or b"{}"))
            body = json.dumps({"choices": [{"message": {"content": "ok"}}],
                               "usage": {"prompt_tokens": 3,
                                         "completion_tokens": 2}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        p = build_models({"models": {"mm": {
            "type": "openai_compat", "api_key_env": "K", "base_url":
            f"http://127.0.0.1:{srv.server_port}",
            "path": "/v1/chat/completions", "model": "MiniMax-M3",
            "extra_body": {"thinking": {"type": "disabled"}}}}},
            env={"K": "k"})["mm"]
        out = p.chat("hi", {})
        assert out["content"] == "ok"
        assert out["usage"]["prompt_tokens"] == 3
        assert seen["thinking"] == {"type": "disabled"}
        assert seen["model"] == "MiniMax-M3"
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ 端到端降级

def test_real_http_429_triggers_fallback_to_local_model():
    """真 HTTP 429 + 真回落：确认整条路径（错误分类→换跳→留痕）都通。"""
    hits = {"mm": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            hits["mm"] += 1
            body = json.dumps({"base_resp": {"status_code": 1008},
                               "error": {"message": "insufficient balance",
                                         "code": 1008}}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        mm = build_models({"models": {"minimax": {
            "type": "openai_compat", "api_key_env": "K",
            "base_url": f"http://127.0.0.1:{srv.server_port}",
            "path": "/v1/chat/completions", "model": "MiniMax-M3"}}},
            env={"K": "k"})["minimax"]
        local = _spec("local")
        fb = FallbackProvider([mm, local], name="c")
        out = fb.chat("p", {})
        assert out["served_by"] == "local"
        assert hits["mm"] == 1
    finally:
        srv.shutdown()
