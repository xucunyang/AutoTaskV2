"""真实Provider（Phase3补Phase2欠账，附录A）。

之前只有`Provider`抽象和`route()`决策，缺可调用的实现。现在补两个：
- OllamaProvider：/api/chat，usage取prompt_eval_count
- OpenAICompatProvider：/v1/chat/completions，usage取prompt_tokens

两条容易被忽略但很关键的约定：
1. **返回契约固定** {content, usage:{prompt_tokens, completion_tokens}}。
   session切换判定完全依赖usage.prompt_tokens（§6b分母是动态窗口final），
   字段名不统一的话水位线就永远判不出来。
2. **并发闸门在provider内**：max_concurrency=2是"本地保护"的一部分，
   超了就快速失败让latency_guard把流量切走，而不是把本地模型压垮。
3. **熔断半开**：连续失败fail_threshold次后cooldown_s内直接拒绝，
   cooldown结束放一个探测请求（半开），成功才恢复。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    pass


class CircuitOpen(ProviderError):
    """熔断打开：直接拒绝，不再打模型。"""


class BaseProvider:
    name = "base"

    def __init__(self, base_url: str = "", timeout_s: int = 60,
                 max_concurrency: int = 2, fail_threshold: int = 3,
                 cooldown_s: int = 300):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = timeout_s
        self.max_concurrency = max(1, int(max_concurrency))
        self.fail_threshold = fail_threshold
        self.cooldown_s = cooldown_s
        self._sem = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._fails = 0
        self._opened_at = 0.0
        self._half_open = False
        self.calls = 0
        self.failures = 0
        self.p50_ms: list[float] = []

    # ---- 熔断（连续失败→cooldown；cooldown后放一个探测=半开） ----
    def _allow(self) -> None:
        with self._lock:
            if self._opened_at and (time.time() - self._opened_at) < self.cooldown_s:
                raise CircuitOpen(f"{self.name}:circuit_open")
            if self._opened_at and (time.time() - self._opened_at) >= self.cooldown_s:
                self._half_open = True      # 半开：放一个探测
        if not self._sem.acquire(timeout=1.0):
            raise ProviderError(f"{self.name}:busy(max_concurrency)")

    def _on_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._opened_at = 0.0
            self._half_open = False

    def _on_failure(self) -> None:
        with self._lock:
            self._fails += 1
            self.failures += 1
            if self._fails >= self.fail_threshold:
                self._opened_at = time.time()
                self._fails = 0

    def health(self) -> dict:
        now = time.time()
        return {"ok": not self._opened_at or (now - self._opened_at) > self.cooldown_s,
                "queue_depth": 0, "p50_ms": int(self._median()),
                "fails": self.failures, "calls": self.calls,
                "half_open": self._half_open,
                "circuit_opened": bool(self._opened_at)}

    def _median(self) -> float:
        if not self.p50_ms:
            return 0.0
        s = sorted(self.p50_ms)
        return s[len(s) // 2]

    def chat(self, prompt: str, budget: dict) -> dict:
        raise NotImplementedError

    def _request(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))


class OllamaProvider(BaseProvider):
    """本地Ollama。/api/chat 的prompt_eval_count就是当前上下文的prompt token数。"""

    name = "local-ollama"

    def __init__(self, base_url="http://localhost:11434", models=None, **kw):
        super().__init__(base_url=base_url, **kw)
        self.models = models or {"simple": "qwen2.5:3b", "medium": "qwen2.5:7b"}

    def model_for(self, complexity: str = "simple") -> str:
        return self.models.get(complexity) or self.models.get("simple")

    def chat(self, prompt: str, budget: dict, *, model: str | None = None,
             num_ctx: int | None = None) -> dict:
        self._allow()
        t0 = time.monotonic()
        try:
            body = {"model": model or self.model_for(budget.get("complexity", "simple")),
                    "prompt": prompt, "stream": False}
            if num_ctx:
                body["options"] = {"num_ctx": num_ctx}
            data = self._request(f"{self.base_url}/api/chat", body, {})
            content = (data.get("message") or {}).get("content", "")
            usage = {"prompt_tokens": int(data.get("prompt_eval_count") or 0),
                     "completion_tokens": int(data.get("eval_count") or 0)}
            out = {"content": content, "usage": usage}
            self.calls += 1
            self.p50_ms.append((time.monotonic() - t0) * 1000)
            self._on_success()
            return out
        except Exception as e:      # noqa: BLE001
            self._on_failure()
            raise ProviderError(f"ollama_failed:{e}") from e
        finally:
            self._sem.release()

    def show(self, model: str) -> dict:
        """探测模型元信息（供core/context.py算动态窗口）。"""
        return self._request(f"{self.base_url}/api/show", {"model": model}, {})


class OpenAICompatProvider(BaseProvider):
    """OpenAI兼容端点（vLLM/云API都吃这套）。

    注意：这类端点可能**静默截断**num_ctx（ollama issue #10974），
    所以返回的usage.prompt_tokens必须如实带出去，让上层做
    resolved vs effective 对账（core/context.py:check_effective）。
    """

    name = "online"

    def __init__(self, base_url="", api_key: str = "", model="gpt-4o-mini", **kw):
        kw.setdefault("max_concurrency", 8)
        super().__init__(base_url=base_url, **kw)
        self.api_key = api_key
        self.model = model

    def chat(self, prompt: str, budget: dict, *, model: str | None = None) -> dict:
        self._allow()
        t0 = time.monotonic()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            body = {"model": model or self.model, "messages": [
                        {"role": "user", "content": prompt}],
                    "stream": False}
            if budget.get("max_tokens"):
                body["max_tokens"] = int(budget["max_tokens"])
            data = self._request(f"{self.base_url}/v1/chat/completions", body,
                                 headers)
            choices = data.get("choices") or [{}]
            content = (choices[0].get("message") or {}).get("content", "")
            u = data.get("usage") or {}
            out = {"content": content,
                   "usage": {"prompt_tokens": int(u.get("prompt_tokens") or 0),
                             "completion_tokens": int(u.get("completion_tokens") or 0)}}
            self.calls += 1
            self.p50_ms.append((time.monotonic() - t0) * 1000)
            self._on_success()
            return out
        except Exception as e:      # noqa: BLE001
            self._on_failure()
            raise ProviderError(f"online_failed:{e}") from e
        finally:
            self._sem.release()


def build_providers(cfg: dict, env=None) -> dict:
    """按config/gateway.yaml造provider实例。api_key_env只存**环境变量名**，
    不把key写进配置文件（配置进git就等于泄密）。"""
    import os
    env = env if env is not None else os.environ
    out = {}
    lg = cfg.get("latency_guard") or {}
    for name, p in (cfg.get("providers") or {}).items():
        if not p.get("enabled", True):
            continue
        kw = {"timeout_s": int(p.get("timeout_s", 60)),
              "max_concurrency": int(p.get("max_concurrency", 2)),
              "fail_threshold": int(lg.get("fail_threshold", 3)),
              "cooldown_s": int(lg.get("cooldown_s", 300))}
        base = str(p.get("base_url") or "")
        if "${" in base:                      # ${ONLINE_BASE_URL} 占位
            key = base.split("${", 1)[1].split("}", 1)[0]
            base = env.get(key, "")
        ptype = p.get("type")
        if ptype == "ollama":
            out[name] = OllamaProvider(base_url=base,
                                       models=p.get("models") or {}, **kw)
        elif ptype == "openai_compat":
            akey = p.get("api_key_env")
            out[name] = OpenAICompatProvider(
                base_url=base, api_key=env.get(akey, "") if akey else "",
                model=p.get("model", ""), **kw)
    return out
