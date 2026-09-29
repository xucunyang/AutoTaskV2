"""动态上下文窗口：探测+分档+显存三层取min，算出真正可用的窗口。

原因见 docs/deploy-prereq.md：
- Ollama 默认 4096 且架构上限因模型而异（Qwen2.5-7B 是 128K、Qwen3-14B 只有 40K），写死必错；
- OpenAI 兼容端点可能静默截断 num_ctx（ollama issue #10974），必须 resolved/effective 对账；
- KV cache 随层数×头数增长快于参数量，最终 usable 还要过 min_usable 门禁。

本模块只做纯计算（可单测），网络探测由调用方注入 payload。
"""
from __future__ import annotations
import re

DEFAULT_RESERVED = 8192
DEFAULT_MIN_USABLE = 16384


def parse_params_b(parameter_size: str | None) -> float | None:
    """解析 /api/show 的 details.parameter_size，如 "7.6B"→7.6、"3.2B"→3.2。"""
    if not parameter_size:
        return None
    m = re.match(r"\s*([\d.]+)\s*([BMK])", str(parameter_size).upper())
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2)
    return {"B": val, "M": val / 1000.0, "K": val / 1000000.0}[unit]


def profile_suggest(params_b: float | None, profiles: list) -> int | None:
    """按参数量分档取建议值；profiles 按 max_params_b 升序，取首个 params_b<=max 的档。"""
    if params_b is None:
        return None
    for p in sorted(profiles, key=lambda x: x["max_params_b"]):
        if params_b <= p["max_params_b"]:
            return int(p["context_window"])
    return None


def _arch_prefix(model_info: dict, arch: str | None) -> str:
    if arch:
        return arch
    for k in model_info:
        if k.endswith(".block_count"):
            return k[: -len(".block_count")]
    return ""


def kv_bytes_per_token(model_info: dict, arch: str | None = None, dtype_bytes: int = 2) -> int | None:
    """从 model_info 算 KV cache 单 token 字节数：2(K/V)×层数×KV头数×head_dim×dtype。
    键名随架构变（qwen2./llama./gemma2.），按 general.architecture 或 block_count 后缀定位前缀；
    解析不到返回 None（调用方退回归档表，不阻塞启动）。"""
    if not model_info:
        return None
    prefix = _arch_prefix(model_info, arch)
    dot = (prefix + ".") if prefix else ""
    try:
        layers = int(model_info[f"{dot}block_count"])
        kv_heads = int(model_info.get(f"{dot}attention.num_key_value_heads",
                                      model_info.get(f"{dot}attention.head_count_kv", 0)) or 0)
        head_dim = int(model_info.get(f"{dot}attention.key_length",
                                      model_info.get(f"{dot}attention.head_dim", 0)) or 0)
        if layers <= 0 or kv_heads <= 0 or head_dim <= 0:
            return None
        return 2 * layers * kv_heads * head_dim * dtype_bytes
    except (KeyError, TypeError, ValueError):
        return None


def arch_max_tokens(model_info: dict, arch: str | None = None) -> int | None:
    """架构上限：优先 model_info.<prefix>.context_length，没有则 None。"""
    if not model_info:
        return None
    prefix = _arch_prefix(model_info, arch)
    for key in (f"{prefix}.context_length" if prefix else "context_length",
                "general.context_length"):
        if key in model_info:
            try:
                v = int(model_info[key])
                return v if v > 0 else None
            except (TypeError, ValueError):
                continue
    return None


def detect_ollama_profile(show_payload: dict) -> dict:
    """解析 /api/show 返回体 → {params_b, arch, arch_max, kv_bytes_per_token, num_ctx_configured}。
    任一字段解析失败记 None，不抛异常（降级链在 resolve 里处理）。"""
    details = show_payload.get("details", {}) or {}
    model_info = show_payload.get("model_info", {}) or {}
    parameters = show_payload.get("parameters", "") or ""
    arch = model_info.get("general.architecture")
    m = re.search(r"num_ctx\s+(\d+)", str(parameters))
    return {
        "params_b": parse_params_b(details.get("parameter_size")),
        "arch": arch,
        "arch_max": arch_max_tokens(model_info, arch),
        "kv_bytes_per_token": kv_bytes_per_token(model_info, arch),
        "num_ctx_configured": int(m.group(1)) if m else None,
    }


def resolve_window(*, arch_max: int | None, profile_suggest: int | None,
                   mem_cap: int | None, config_override: int | None) -> int | None:
    """四层取min；全空返回 None（调用方走 fallback 降级值）。"""
    cands = [c for c in (arch_max, profile_suggest, mem_cap, config_override)
             if isinstance(c, int) and c > 0]
    return min(cands) if cands else None


def usable_window(final_window: int | None, reserved: int = DEFAULT_RESERVED) -> int | None:
    """减去系统提示+工具+completion 预留；final 为空返回 None。"""
    if not isinstance(final_window, int) or final_window <= 0:
        return None
    return final_window - reserved


def is_local_usable(usable: int | None, min_usable: int = DEFAULT_MIN_USABLE) -> bool:
    """门禁：usable 低于下限 → 本地不可用 → 路由走在线。"""
    return isinstance(usable, int) and usable >= min_usable


def check_effective(resolved: int | None, effective: int | None) -> str:
    """resolved（我们算的） vs effective（服务端实际采纳）对账。
    返回 ok / truncated / unknown，供 metrics + 告警用。"""
    if resolved is None or effective is None:
        return "unknown"
    # 允许 5% 以内的取整/对齐误差
    if effective >= resolved * 0.95:
        return "ok"
    return "truncated"
