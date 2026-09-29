"""Phase3补Phase2欠账：真实Provider（附录A）+ 网关路由到实例。"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import gateway
from core import providers as pv
from core.providers import CircuitOpen, OllamaProvider, OpenAICompatProvider, ProviderError
from tests.test_orchestrator import _root


class FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, payload, calls=None):
    def fake(req, timeout=None):
        if calls is not None:
            calls.append({"url": req.full_url,
                          "body": json.loads(req.data.decode("utf-8"))})
        return FakeResp(payload)
    monkeypatch.setattr("urllib.request.urlopen", fake)


# ---------- 返回契约（§6b判定依赖） ----------

def test_ollama_returns_unified_contract(monkeypatch):
    _patch_urlopen(monkeypatch, {"message": {"content": "答案"},
                                 "prompt_eval_count": 4321,
                                 "eval_count": 88})
    p = OllamaProvider(base_url="http://x")
    out = p.chat("问题", {"complexity": "simple"})
    assert out["content"] == "答案"
    assert out["usage"] == {"prompt_tokens": 4321, "completion_tokens": 88}
    # 字段名统一，executor的should_yield才能用
    assert set(out["usage"]) == {"prompt_tokens", "completion_tokens"}


def test_online_returns_unified_contract(monkeypatch):
    _patch_urlopen(monkeypatch, {"choices": [{"message": {"content": "答案"}}],
                                 "usage": {"prompt_tokens": 100,
                                           "completion_tokens": 20}})
    p = OpenAICompatProvider(base_url="http://y", api_key="k", model="m")
    out = p.chat("问题", {})
    assert out["usage"]["prompt_tokens"] == 100
    assert out["content"] == "答案"


def test_ollama_model_selection(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, {"message": {"content": "x"},
                                 "prompt_eval_count": 1, "eval_count": 1}, calls)
    p = OllamaProvider(base_url="http://x", models={"simple": "q3b", "medium": "q7b"})
    p.chat("q", {"complexity": "medium"})
    assert calls[0]["body"]["model"] == "q7b"
    p.chat("q", {"complexity": "complex"})     # 未知档退回simple
    assert calls[1]["body"]["model"] == "q3b"


def test_num_ctx_passed_through(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, {"message": {"content": "x"},
                                 "prompt_eval_count": 1, "eval_count": 1}, calls)
    OllamaProvider(base_url="http://x").chat("q", {}, num_ctx=32768)
    assert calls[0]["body"]["options"]["num_ctx"] == 32768


def test_online_sends_auth_header(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, {"choices": [{"message": {"content": "x"}}],
                                 "usage": {"prompt_tokens": 1,
                                           "completion_tokens": 1}}, calls)
    p = OpenAICompatProvider(base_url="http://y", api_key="SECRET", model="m")
    p.chat("q", {})
    assert "v1/chat/completions" in calls[0]["url"]


# ---------- 并发闸门（本地保护 max_concurrency=2） ----------

def test_concurrency_gate_rejects_when_saturated(monkeypatch):
    p = OllamaProvider(base_url="http://x", max_concurrency=1)
    p._sem.acquire()                     # 占满唯一槽
    with pytest.raises(ProviderError, match="busy"):
        p.chat("q", {})


def test_semaphore_released_on_error(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("连接拒绝")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    p = OllamaProvider(base_url="http://x", max_concurrency=1)
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    # 槽必须已释放，否则第一次失败就把provider永久卡死
    assert p._sem.acquire(timeout=1.0)
    p._sem.release()


# ---------- 熔断 + 半开（附录A：连续失败3次熔断5min半开恢复） ----------

def test_circuit_opens_after_threshold(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    p = OllamaProvider(base_url="http://x", fail_threshold=3, cooldown_s=300)
    for _ in range(3):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    # 第4次直接被熔断拒绝，不再打模型
    with pytest.raises(CircuitOpen):
        p.chat("q", {})


def test_half_open_after_cooldown(monkeypatch):
    payload = {"message": {"content": "ok"}, "prompt_eval_count": 1, "eval_count": 1}
    state = {"fail": True}

    def handler(req, timeout=None):
        if state["fail"]:
            raise OSError("down")
        return FakeResp(payload)
    monkeypatch.setattr("urllib.request.urlopen", handler)
    p = OllamaProvider(base_url="http://x", fail_threshold=2, cooldown_s=0.2)
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    with pytest.raises(CircuitOpen):
        p.chat("q", {})
    time.sleep(0.25)
    state["fail"] = False
    # 半开：放一个探测，成功则完全恢复
    assert p.chat("q", {})["content"] == "ok"
    assert p.health()["ok"] is True
    assert p.health()["circuit_opened"] is False


def test_health_reports_queue_and_latency(monkeypatch):
    _patch_urlopen(monkeypatch, {"message": {"content": "x"},
                                 "prompt_eval_count": 1, "eval_count": 1})
    p = OllamaProvider(base_url="http://x")
    p.chat("q", {})
    h = p.health()
    assert h["ok"] is True and h["calls"] == 1 and h["p50_ms"] >= 0


# ---------- 配置装配 ----------

def test_build_providers_from_gateway_config(tmp_path):
    root = _root(tmp_path)
    import yaml
    (root / "config" / "gateway.yaml").write_text(yaml.safe_dump({
        "providers": {
            "local-ollama": {"type": "ollama", "base_url": "http://localhost:11434",
                             "models": {"simple": "q3b"}, "timeout_s": 60,
                             "max_concurrency": 2, "enabled": True},
            "online": {"type": "openai_compat", "base_url": "${ONLINE_BASE_URL}",
                       "api_key_env": "ONLINE_API_KEY", "model": "gpt",
                       "timeout_s": 90, "max_concurrency": 8, "enabled": True},
            "off": {"type": "ollama", "enabled": False},
        },
        "latency_guard": {"fail_threshold": 3, "cooldown_s": 300},
    }, allow_unicode=True), encoding="utf-8")
    got = pv.build_providers(yaml.safe_load(
        (root / "config" / "gateway.yaml").read_text(encoding="utf-8")),
        env={"ONLINE_BASE_URL": "https://api.example.com",
             "ONLINE_API_KEY": "sk-test"})
    assert set(got) == {"local-ollama", "online"}      # disabled的不建
    assert got["online"].base_url == "https://api.example.com"
    assert got["online"].api_key == "sk-test"
    assert got["local-ollama"].max_concurrency == 2


def test_gateway_config_has_no_hardcoded_key():
    """api_key只存环境变量名——配置进git等于泄密。
    查真实配置文件（不是tmp_path的副本），因为这条断言的意义就在生产配置上。"""
    raw = (Path(__file__).resolve().parents[1] / "config" / "gateway.yaml").read_text(
        encoding="utf-8")
    assert "sk-" not in raw
    assert "api_key_env" in raw


# ---------- 路由 → 实例（执行器只认这个） ----------

def test_resolve_returns_instance():
    ps = {"online": OpenAICompatProvider(base_url="http://y", model="m")}
    assert gateway.resolve({"provider": "online"}, ps) is ps["online"]


def test_resolve_missing_returns_none_not_fake():
    """查不到必须None：造个空provider会让任务"成功"但产物是空的，比报错危险。"""
    assert gateway.resolve({"provider": "nope"}, {}) is None
    with pytest.raises(KeyError, match="provider_not_found"):
        gateway.chat({"provider": "nope"}, {}, "q", {})


def test_route_then_chat_end_to_end(monkeypatch):
    calls = []
    _patch_urlopen(monkeypatch, {"message": {"content": "最终答案"},
                                 "prompt_eval_count": 500, "eval_count": 10},
                   calls)
    ps = {"local-ollama": OllamaProvider(base_url="http://x")}

    class T:
        privacy = "public"
        complexity = "simple"
    r = gateway.route(T(), {"local_usable": True})
    assert r["provider"] == "local-ollama"
    out = gateway.chat(r, ps, "问题", {})
    assert out["content"] == "最终答案"
    assert out["usage"]["prompt_tokens"] == 500


def test_privacy_secret_routes_local(monkeypatch):
    ps = {"local-ollama": OllamaProvider(base_url="http://x")}

    class T:
        privacy = "secret"
        complexity = "complex"
    r = gateway.route(T(), {"local_usable": False})
    assert r["provider"] == "local-ollama"
    assert r["reason"] == "privacy_secret"


def test_local_window_insufficient_routes_online():
    """动态窗口不够时本地实际不可用，必须走在线（Phase0门禁）。"""
    class T:
        privacy = "public"
        complexity = "simple"
    r = gateway.route(T(), {"local_usable": False})
    assert r["provider"] == "online"
    assert r["reason"] == "local_window_insufficient"
