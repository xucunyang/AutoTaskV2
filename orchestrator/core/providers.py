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


class OpenAICompatProvider(BaseProvider):
    """OpenAI兼容端点（vLLM/云API都吃这套）。

    注意：这类端点可能**静默截断**num_ctx（ollama issue #10974），
    所以返回的usage.prompt_tokens必须如实带出去，让上层做
    resolved vs effective 对账（core/context.py:check_effective）。
    """

    name = "online"

    def __init__(self, base_url="", api_key: str = "", model="gpt-4o-mini",
                 path: str = "/v1/chat/completions",
                 name: str | None = None, **kw):
        kw.setdefault("max_concurrency", 8)
        super().__init__(base_url=base_url, **kw)
        if name:
            self.name = name            # 熔断/健康信息用注册表里的名字
        self.api_key = api_key
        self.model = model
        # 路径可配：DeepSeek是 /chat/completions，标准OpenAI兼容是 /v1/chat/completions
        self.path = path if path.startswith("/") else f"/{path}"

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
            out[name] = OpenAICompatProvider(
                name=name, base_url=base,
                api_key=env.get(akey, "") if akey else "",
                model=_expand(p.get("model"), env),
                path=_expand(p.get("path", "/v1/chat/completions"), env), **kw)
    return out


def build_models_from_config(root, env=None) -> dict:
    """读 config/gateway.yaml 再造模型。给不想自己load_yaml的调用方用。"""
    import yaml
    cfg_path = Path(root) / "config" / "gateway.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return build_models(cfg, env=env)
