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
from pathlib import Path


class ProviderError(RuntimeError):
    """provider调用失败。

    带 status / permanent 两个属性给熔断器分类：
    - status    HTTP状态码（网络层错误为None）
    - permanent 是否"重试也没用"。401/403 属于这类：key 错了，
      再打一百次还是错，等阈值攒够纯属浪费。
    """

    def __init__(self, msg: str, status: int | None = None,
                 permanent: bool = False):
        super().__init__(msg)
        self.status = status
        self.permanent = permanent


# 这些状态码重试没有意义：不是配置错就是请求本身不被接受。
# 立刻熔断，让运维先修配置，而不是用无效重试把端点刷一遍。
PERMANENT_STATUS = (401, 403, 404)


class CircuitOpen(ProviderError):
    """熔断打开：直接拒绝，不再打模型。"""


# 配额/限流类错误的识别。命中这些才允许换下一个 provider——
# 判据太宽会把"模型名写错了""上下文超长"这类永久性错误也当成限流，
# 于是一个配置错误被静默降级成"换个模型跑"，跑出来的结果没人知道降过级。
#
# 429 是标准限流。但配额型套餐经常不给 429，直接 400/403 + 文本说明，
# 所以文本也要认。命中即**不重试当前 provider**，交给下一跳。
LIMIT_STATUS = (429,)

_LIMIT_TEXTS = (
    "rate limit", "rate_limit", "ratelimit",
    "too many requests",
    "quota", "insufficient_quota", "insufficient balance",
    "insufficient funds", "exceeded your current quota",
    "package quota", "out of balance", "billing",
    "resource_exhausted", "try again later",
)


def _norm(text) -> str:
    return str(text or "").lower()


def is_limit_error(err: Exception) -> bool:
    """这个错误是不是"额度/限流"导致的（值得换下一个 provider）。

    分三类：
    - 429：标准限流，无条件认
    - 5xx 之外的 400/402/403 且文本提到额度：配额型套餐的常见形态
    - CircuitOpen：熔断已经打开 = 这个 provider 现在不可用，换下一跳同样合理
    """
    if isinstance(err, CircuitOpen):
        return True
    status = getattr(err, "status", None)
    text = _norm(err)
    if status in LIMIT_STATUS:
        return True
    if status in (400, 402, 403, 500, 502, 503, 529):
        return any(t in text for t in _LIMIT_TEXTS)
    return False


def _is_cold_start(data: dict) -> bool:
    """ollama 冷启动特征：done_reason="load" 且没算过 prompt。

    含义是"这次只把模型载入内存就返回了"，没有真正推理。
    把它当正常响应会让调用方拿到空回复。
    """
    d = data or {}
    return (d.get("done_reason") == "load"
            and not d.get("prompt_eval_count"))


def _parse_tool_calls(raw) -> list[dict]:
    """统一各家 tool_calls 形状 → [{id, name, arguments(dict)}]。

    arguments 各家给法不一样：OpenAI 给JSON字符串，Ollama给dict，
    有的还把名字塞在 function.name 里。执行器只认统一后的形状，
    否则每接一个provider就要在执行器里加一次分支。
    """
    out: list[dict] = []
    for i, tc in enumerate(raw or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or tc
        name = fn.get("name") or tc.get("name")
        if not name:
            continue
        a = fn.get("arguments")
        if a is None:
            a = tc.get("arguments")
        if isinstance(a, str):
            try:
                a = json.loads(a) if a.strip() else {}
            except ValueError:
                a = {"_raw": a}     # 保留原文，别把解析失败伪装成空参
        if not isinstance(a, dict):
            a = {}
        out.append({"id": str(tc.get("id") or f"call_{i}"),
                    "name": str(name), "arguments": a})
    return out


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
        self.last_permanent = ""     # 最近一次永久性失败的状态码（排查配置用）
        self.calls = 0
        self.failures = 0
        self.p50_ms: list[float] = []

    # ---- 熔断（连续失败→cooldown；cooldown后放**一个**探测=半开） ----
    def _allow(self) -> None:
        with self._lock:
            if self._opened_at:
                age = time.time() - self._opened_at
                if age < self.cooldown_s:
                    raise CircuitOpen(f"{self.name}:circuit_open")
                if self._half_open:
                    # 探测已在飞。cooldown 一过就全放行是标准的惊群——
                    # 熔断的意义就是别在恢复瞬间压垮端点，而文档
                    # §附录A 承诺的就是"放一个探测"，之前代码里没有这个闸门。
                    raise CircuitOpen(f"{self.name}:half_open_probe_inflight")
                self._half_open = True       # 放一个探测
        if not self._sem.acquire(timeout=1.0):
            raise ProviderError(f"{self.name}:busy(max_concurrency)")

    def _on_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._opened_at = 0.0
            self._half_open = False

    def _on_failure(self, err: Exception | None = None) -> None:
        with self._lock:
            self._fails += 1
            self.failures += 1
            # 探测失败 → 立刻重新熔断。原来只是 fails+1，而阈值是3，
            # 要连败三次才重新打开，中间两次等于"完全放开"——
            # 熔断会退化成随机失败，对持续故障的端点毫无保护作用。
            probe_failed = self._half_open
            permanent = bool(getattr(err, "permanent", False))
            if probe_failed or permanent or self._fails >= self.fail_threshold:
                self._opened_at = time.time()
                self._half_open = False
                self._fails = 0
                if permanent:
                    # 配置类错误：让上层一眼看出要改配置而不是等重试
                    self.last_permanent = str(getattr(err, "status", "")) or "?"

    def health(self) -> dict:
        now = time.time()
        return {"ok": not self._opened_at or (now - self._opened_at) > self.cooldown_s,
                "queue_depth": 0, "p50_ms": int(self._median()),
                "fails": self.failures, "calls": self.calls,
                "half_open": self._half_open,
                "circuit_opened": bool(self._opened_at),
                "last_permanent": self.last_permanent}

    def _median(self) -> float:
        if not self.p50_ms:
            return 0.0
        s = sorted(self.p50_ms)
        return s[len(s) // 2]

    def chat(self, prompt: str, budget: dict) -> dict:
        raise NotImplementedError

    def encode_assistant_tool_calls(self, calls: list) -> list[dict]:
        """把统一后的 tool_calls 编成"本家方言"回填进 assistant 消息。

        **不能一律按 OpenAI 形状回填**。OpenAI 兼容端点要
        `arguments` 是JSON字符串；**Ollama 要的是dict**——把字符串喂给它，
        它在解析时直接 400
        （"Value looks like object, but can't find closing '}' symbol"）。
        这个错误在真实跑本地模型时才暴露：第一步不回填所以正常，
        文件也真的写出来了，到第二步才炸，看起来像"模型生成的JSON坏了"，
        实际是我们回填的格式不对。查错方向会完全跑偏。
        """
        return [{"id": c["id"], "type": "function",
                 "function": {"name": c["name"],
                              "arguments": json.dumps(c["arguments"],
                                                      ensure_ascii=False)}}
                for c in calls]

    def _request(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 把响应体带出来。ollama/DeepSeek 的错误码都在body里说明原因
            # （如 "the request exceeds the available context size"），
            # 只报 "HTTP Error 400" 等于把唯一线索扔了。
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:       # noqa: BLE001
                detail = ""
            # 注意不要再拼一次 self.name：调用方还会包一层，
            # 拼两次就成了 local-ollama:failed:local-ollama:http_500
            raise ProviderError(
                f"http_{e.code}:{detail or e.reason}",
                status=e.code, permanent=e.code in PERMANENT_STATUS) from e


class OllamaProvider(BaseProvider):
    """本地Ollama。/api/chat 的prompt_eval_count就是当前上下文的prompt token数。"""

    name = "local-ollama"

    def __init__(self, base_url="http://localhost:11434", models=None,
                 name: str | None = None, think: bool | None = None, **kw):
        super().__init__(base_url=base_url, **kw)
        if name:
            self.name = name            # 熔断/健康信息用注册表里的名字
        # think: 思维模型（qwen3.5等）在思考时token消耗和延迟都是数倍。
        # 实测同一任务卡 think=false 6.6s / think默认 21.4s，正确性相同。
        # None = 不下发该字段，由ollama按模型默认。
        self.think = think
        # 不给默认模型：原来兜底 {"simple":qwen2.5:3b, "medium":qwen2.5:7b"}
        # 会让"配置漏了models"变成静默跑一个谁都没指定的模型。空就是空，
        # 路由层会因此判"本地做不了"并走在线，配置错误立刻可见。
        self.models = dict(models or {})

    def encode_assistant_tool_calls(self, calls: list) -> list[dict]:
        """Ollama 方言：arguments 是 dict，不是JSON字符串。"""
        return [{"id": c["id"], "type": "function",
                 "function": {"name": c["name"],
                              "arguments": dict(c["arguments"])}}
                for c in calls]

    def model_for(self, complexity: str = "simple") -> str:
        """取本地模型。**没有对应档位就报错，不静默降级**。

        原来对未知复杂度回落models["simple"]——等于用4B悄悄跑medium任务，
        质量掉了但日志里看不出来。路由层已按"配了哪些档位"决定走不走本地，
        走到这里却没有对应模型说明配置不一致，必须炸出来而不是猜。
        """
        m = self.models.get(complexity)
        if not m:
            raise ProviderError(
                f"{self.name}:no_local_model_for_{complexity} "
                f"(configured={sorted(self.models)})")
        return m

    def chat(self, prompt: str, budget: dict, *, messages: list | None = None,
             tools: list | None = None, model: str | None = None,
             num_ctx: int | None = None, retries: int = 1) -> dict:
        """Ollama /api/chat。

        多轮用 messages（tool loop 需要），单轮也统一走 messages——
        qwen3.5 这类**思维模型**在 `prompt` 字段下基本不输出正文
        （实测返回 content 空 + 无 tool_calls），用 messages 才有正常响应。

        retries 是给**冷启动**用的：模型不在内存时，ollama 第一次请求
        会把模型载入内存然后直接返回空响应（done_reason="load"，
        连 prompt_eval_count 都没有）。这不是错误，是加载动作。
        不重试的话每次重启ollama后的第一个任务都会拿到空回复，
        然后被误判成"模型什么都没产出"。
        """
        self._allow()
        t0 = time.monotonic()
        try:
            body = {"model": model or self.model_for(budget.get("complexity", "simple")),
                    "prompt": prompt, "stream": False}
            if messages or not prompt:
                body.pop("prompt")
                body["messages"] = messages or [
                    {"role": "user", "content": prompt or ""}]
            if tools:
                body["tools"] = tools
            if self.think is not None:
                body["think"] = bool(self.think)     # 顶字段，不是options里
            if num_ctx:
                body["options"] = {"num_ctx": num_ctx}
            data = None
            for attempt in range(retries + 1):
                data = self._request(f"{self.base_url}/api/chat", body, {})
                if not _is_cold_start(data):
                    break
                if attempt < retries:
                    self.calls += 1     # 这次确实打到了模型，只是载入
                    continue
            msg = data.get("message") or {}
            usage = {"prompt_tokens": int(data.get("prompt_eval_count") or 0),
                     "completion_tokens": int(data.get("eval_count") or 0)}
            out = {"content": msg.get("content", "") or "", "usage": usage,
                   "tool_calls": _parse_tool_calls(msg.get("tool_calls")),
                   "thinking": (msg.get("thinking") or msg.get("reasoning_content")
                                or "") or ""}
            self.calls += 1
            self.p50_ms.append((time.monotonic() - t0) * 1000)
            self._on_success()
            return out
        except Exception as e:      # noqa: BLE001
            # 熔断要按错误类型分类：401/403 是配置错，不能和"服务抖动"
            # 一起攒够阈值——所以把异常原样交给 _on_failure，
            # 并把 status/permanent 透传给上层（否则上层分不清限流和宕机）
            self._on_failure(e)
            if isinstance(e, ProviderError):
                raise ProviderError(f"{self.name}:failed:{e}", status=e.status,
                                    permanent=e.permanent) from e
            raise ProviderError(f"{self.name}:failed:{e}") from e
        finally:
            self._sem.release()

    def show(self, model: str) -> dict:
        """探测模型元信息（供core/context.py算动态窗口）。"""
        return self._request(f"{self.base_url}/api/show", {"model": model}, {})


# 允许通过配置附加到请求体的厂商特有键。闭集，不接受任意键。
_EXTRA_BODY_ALLOWLIST = {"thinking"}


class OpenAICompatProvider(BaseProvider):
    """OpenAI兼容端点（vLLM/云API都吃这套）。

    注意：这类端点可能**静默截断**num_ctx（ollama issue #10974），
    所以返回的usage.prompt_tokens必须如实带出去，让上层做
    resolved vs effective 对账（core/context.py:check_effective）。
    """

    name = "online"

    def __init__(self, base_url="", api_key: str = "", model="gpt-4o-mini",
                 path: str = "/v1/chat/completions",
                 name: str | None = None, extra_body: dict | None = None, **kw):
        kw.setdefault("max_concurrency", 8)
        super().__init__(base_url=base_url, **kw)
        if name:
            self.name = name            # 熔断/健康信息用注册表里的名字
        self.api_key = api_key
        self.model = model
        # 路径可配：DeepSeek是 /chat/completions，标准OpenAI兼容是 /v1/chat/completions
        self.path = path if path.startswith("/") else f"/{path}"
        # 厂商特有参数（如 MiniMax 的 thinking）。**只放行白名单键**：
        # 这里是把配置原样拼进请求体，等于给了 yaml 一个任意 JSON 注入的口子。
        self.extra_body = {k: v for k, v in (extra_body or {}).items()
                           if k in _EXTRA_BODY_ALLOWLIST}

    def chat(self, prompt: str, budget: dict, *, messages: list | None = None,
             tools: list | None = None, model: str | None = None) -> dict:
        """OpenAI 兼容 /chat/completions（DeepSeek、vLLM 都吃这套）。"""
        self._allow()
        t0 = time.monotonic()
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            msgs = list(messages) if messages else [{"role": "user", "content": prompt}]
            body = {"model": model or self.model, "messages": msgs,
                    "stream": False}
            if budget.get("max_tokens"):
                body["max_tokens"] = int(budget["max_tokens"])
            if tools:
                body["tools"] = tools
                body["tool_choice"] = budget.get("tool_choice", "auto")
            body.update(self.extra_body)
            data = self._request(f"{self.base_url}{self.path}", body, headers)
            choices = data.get("choices") or [{}]
            msg = choices[0].get("message") or {}
            u = data.get("usage") or {}
            out = {"content": msg.get("content") or "",
                   "usage": {"prompt_tokens": int(u.get("prompt_tokens") or 0),
                             "completion_tokens": int(u.get("completion_tokens") or 0)},
                   "tool_calls": _parse_tool_calls(msg.get("tool_calls"))}
            self.calls += 1
            self.p50_ms.append((time.monotonic() - t0) * 1000)
            self._on_success()
            return out
        except Exception as e:      # noqa: BLE001
            # 熔断要按错误类型分类：401/403 是配置错，不能和"服务抖动"
            # 一起攒够阈值——所以把异常原样交给 _on_failure，
            # 并把 status/permanent 透传给上层（否则上层分不清限流和宕机）
            self._on_failure(e)
            if isinstance(e, ProviderError):
                raise ProviderError(f"{self.name}:failed:{e}", status=e.status,
                                    permanent=e.permanent) from e
            raise ProviderError(f"{self.name}:failed:{e}") from e
        finally:
            self._sem.release()


class FallbackProvider(BaseProvider):
    """按顺序试多个 provider，前一个额度/限流/熔断就换下一个。

    为什么需要它：MiniMax 这类是**按 token 套餐计费**的通道，额度会
    按小时/按天耗尽。额度耗尽时任务不该跟着一起死——原来的逻辑
    （simple→local / medium→flash / complex→pro）本身是好的，
    只需要在前面多插一个"能用就省钱的"选项。

    三条硬约束：
    1. **只对限流类错误降级**（见 is_limit_error）。模型名写错、上下文
       超长、鉴权失败都不该换 provider——那些换过去也一样错，白白
       把所有 provider 的熔断都打一遍。
    2. **每一跳各自记账**：熔断/并发闸门留在各自的 provider 上。
       在这里包一层共用信号量的话，local 的 max_concurrency=1 会
       连带限死在付费通道上。
    3. **降级必须留痕**：返回结果里带 served_by 和 fell_from，
       否则"这个任务其实是用 4B 跑的"只有查日志才知道，
       而产物质量下降恰恰就是这样发生的。

    attempts 记录每个任务实际试过几跳，供告警与容量判断使用。
    """

    name = "fallback"

    def __init__(self, chain: list, name: str | None = None):
        # 刻意不调 super().__init__：自己的并发闸门会跨 provider 生效，
        # 每跳的闸门已经各自存在了。这里只借它的属性名。
        self.name = name or "fallback"
        self.chain = [p for p in (chain or []) if p is not None]
        if not self.chain:
            raise ValueError("FallbackProvider 需要至少一个 provider")
        self.calls = 0
        self.failures = 0
        self.p50_ms: list[float] = []
        self.last_permanent = ""
        self.attempts: list[dict] = []

    @property
    def primary(self):
        return self.chain[0]

    def _names(self) -> list[str]:
        return [getattr(p, "name", "?") for p in self.chain]

    def chat(self, prompt: str, budget: dict, **kw) -> dict:
        tried: list[str] = []
        last: Exception | None = None
        for i, p in enumerate(self.chain):
            tried.append(getattr(p, "name", "?"))
            try:
                out = p.chat(prompt, budget, **kw)
            except Exception as e:      # noqa: BLE001
                last = e
                is_last = i == len(self.chain) - 1
                if is_last or not is_limit_error(e):
                    # 最后一跳或不可降级的错误：原样抛出，让上层按
                    # 既有语义处理（RETRY/FAILED/死信），不做额外包装。
                    self.attempts.append({"chain": self._names(),
                                          "tried": tried,
                                          "served_by": None,
                                          "reason": "exhausted" if is_last
                                          else "non_limit_error"})
                    raise
                continue
            self.calls += 1
            self.p50_ms.append(getattr(p, "p50_ms", [0.0])[-1]
                               if getattr(p, "p50_ms", None) else 0.0)
            self.attempts.append({"chain": self._names(), "tried": tried,
                                  "served_by": getattr(p, "name", "?"),
                                  "reason": "fallback" if i else "primary"})
            out["served_by"] = getattr(p, "name", "?")
            if i:
                # 记清降级事实：产物是低档模型写的
                out["fell_from"] = tried[:-1]
            return out
        raise ProviderError(f"{self.name}:no_provider_left:{last}")

    def health(self) -> dict:
        """熔断状态取**下一跳**的——它才是现在真正会接流量的那个。

        用第一跳的健康度会有个具体的坏后果：额度耗尽后第一跳持续
        熔断，于是 latency_guard 看到 fails 达标就把流量全切走，
        而此时后面的 provider 明明是好的。
        """
        nxt = next((p for p in self.chain if not p.health().get("circuit_opened")),
                   self.chain[-1])
        h = dict(nxt.health())
        h["ok"] = any(p.health().get("ok") for p in self.chain)
        h["chain"] = self._names()
        h["chain_health"] = {getattr(p, "name", "?"): p.health()
                             for p in self.chain}
        return h

    def show(self, model: str) -> dict:
        """动态窗口探测：谁支持 show() 就用谁。"""
        for p in self.chain:
            fn = getattr(p, "show", None)
            if callable(fn):
                return fn(model)
        raise ProviderError(f"{self.name}:no_show_capability")

    def encode_assistant_tool_calls(self, calls: list) -> list[dict]:
        """按**实际服务的那一跳**的方言回填。

        不能一律用 OpenAI 形状：链里混了 ollama 时 arguments 要 dict，
        给字符串会 400（"can't find closing '}' symbol"），症状看起来
        像"模型生成的 JSON 坏了"，查错方向完全跑偏。
        降级发生时上一跳是谁已经变了，所以要问 provider 本人。
        """
        enc = getattr(self.primary, "encode_assistant_tool_calls", None)
        if callable(enc):
            return enc(calls)
        return [{"id": c["id"], "type": "function",
                 "function": {"name": c["name"],
                              "arguments": json.dumps(c["arguments"],
                                                      ensure_ascii=False)}}
                for c in calls]

    def __getattr__(self, item):
        """把没定义的方法/属性转给当前服务的那一跳。

        context.py 之类的地方会问 provider 要 model / name 之类的东西。
        没有这个转发的话，链能 chat 但探测、报账就都断了——
        而且报错是 AttributeError，看起来像 provider 写坏了。
        只在正常属性查找失败时触发，不影响显式定义的方法。
        """
        if item.startswith("_"):
            raise AttributeError(item)
        primary = self.__dict__.get("chain", [None])[0]
        if primary is None:
            raise AttributeError(item)
        return getattr(primary, item)


def _expand(value, env) -> str:
    """解析 ${VAR} 占位。

    之前只对 base_url 做这件事，model 字段漏了——于是
    `model: "${ONLINE_FLASH_MODEL}"` 会把字面量"${ONLINE_FLASH_MODEL}"
    当模型名发给DeepSeek，表现为模型不存在。所有字符串字段统一走这里。
    """
    s = str(value or "")
    if "${" not in s:
        return s
    key = s.split("${", 1)[1].split("}", 1)[0]
    return str(env.get(key, "") or "")


def build_models(cfg: dict, env=None) -> dict:
    """按 config/gateway.yaml 的 models 注册表造 provider 实例。

    返回 {注册表里的模型名: provider}。路由结果里的 {"model": name}
    就是这个 name——换模型/加模型只改 yaml，不动代码。

    密钥/地址**不写进 yaml**，只写变量名（api_key_env / ${VAR}），
    实际值从 .env 或环境变量取——配置进 git 就等于泄密。
    """
    import os
    from core.utils import load_dotenv
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root)                 # .env 不覆盖已存在的真实环境变量
    env = env if env is not None else os.environ
    out = {}
    lg = cfg.get("latency_guard") or {}
    for name, p in (cfg.get("models") or {}).items():
        if not p.get("enabled", True):
            continue
        kw = {"timeout_s": int(p.get("timeout_s", 60)),
              "max_concurrency": int(p.get("max_concurrency", 2)),
              "fail_threshold": int(lg.get("fail_threshold", 3)),
              "cooldown_s": int(lg.get("cooldown_s", 300))}
        base = _expand(p.get("base_url"), env)
        ptype = p.get("type")
        if ptype == "ollama":
            out[name] = OllamaProvider(
                name=name, base_url=base,
                models={k: _expand(v, env)
                        for k, v in (p.get("models") or {}).items()},
                think=p.get("think"), **kw)
        elif ptype == "openai_compat":
            akey = p.get("api_key_env")
            api_key = env.get(akey, "") if akey else ""
            # 没配 key 的在线 provider 直接不注册。注册一个 key 为空的
            # provider 更糟：它会被路由选中，然后在第一次调用时 401，
            # 熔断、退避、fallback 全部为一次"其实压根没配"的错误买单。
            # 不注册 = 路由看不到它 = 直接走链上下一个，行为正确且安静。
            if akey and not api_key:
                continue
            out[name] = OpenAICompatProvider(
                name=name, base_url=base,
                api_key=api_key,
                model=_expand(p.get("model"), env),
                path=_expand(p.get("path", "/v1/chat/completions"), env),
                extra_body=p.get("extra_body") or None, **kw)

    for cname, chain in (cfg.get("chains") or {}).items():
        """chains: {名字: [优先…, 兜底…]}。按顺序试，限流就换下一跳。"""
        if not chain:
            continue
        picked = [out[n] for n in chain if n in out]
        # 链里点名要用的 provider 没能注册（缺 key / disabled）——必须记下来。
        # 否则"链没生效"只能靠猜：现象是额度用完了却没回落到兜底那一跳，
        # 而配置看上去完全正确。
        # 注意拿 out 的**键**比对，别拿 provider 对象比字符串（那样永远不等，
        # 已注册的成员也会被误报成 skip）。
        skipped = [n for n in chain if n not in out]
        if skipped:
            import sys
            print(f"[gateway] chain {cname}: skip {skipped} "
                  f"(disabled or missing api key); effective={_chain_names(picked)}",
                  file=sys.stderr)
        if not picked:
            continue
        out[cname] = FallbackProvider(picked, name=cname)
    return out


def _chain_names(providers: list) -> list[str]:
    return [getattr(p, "name", "?") for p in providers]


def build_models_from_config(root, env=None) -> dict:
    """读 config/gateway.yaml 再造模型。给不想自己load_yaml的调用方用。"""
    import yaml
    cfg_path = Path(root) / "config" / "gateway.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return build_models(cfg, env=env)
