"""context.py 单测：纯计算，无网络、无Ollama依赖。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.context import (
    arch_max_tokens,
    check_effective,
    detect_ollama_profile,
    is_local_usable,
    kv_bytes_per_token,
    parse_params_b,
    profile_suggest,
    resolve_window,
    usable_window,
)

PROFILES = [
    {"max_params_b": 4, "context_window": 32768},
    {"max_params_b": 7, "context_window": 65536},
    {"max_params_b": 14, "context_window": 65536},
    {"max_params_b": 999, "context_window": 32768},
]

QWEN2_7B_INFO = {
    "general.architecture": "qwen2",
    "qwen2.block_count": 28,
    "qwen2.attention.num_key_value_heads": 4,
    "qwen2.attention.key_length": 128,
    "qwen2.context_length": 131072,
}


def test_parse_params_b():
    assert parse_params_b("7.6B") == 7.6
    assert parse_params_b("3.2B") == 3.2
    assert parse_params_b("1.5B") == 1.5
    assert parse_params_b(None) is None
    assert parse_params_b("unknown") is None


def test_profile_suggest():
    assert profile_suggest(3.2, PROFILES) == 32768
    assert profile_suggest(7.6, PROFILES) == 65536
    assert profile_suggest(14.8, PROFILES) == 32768  # >14 落XL档
    assert profile_suggest(None, PROFILES) is None


def test_kv_bytes_per_token_qwen2_7b():
    # 2×28层×4头×128×2字节 = 57344
    assert kv_bytes_per_token(QWEN2_7B_INFO) == 57344
    assert kv_bytes_per_token({}) is None
    assert kv_bytes_per_token({"qwen2.block_count": 28}) is None  # 缺头数/维度不硬猜


def test_arch_max_tokens():
    assert arch_max_tokens(QWEN2_7B_INFO) == 131072
    assert arch_max_tokens({}) is None


def test_detect_ollama_profile():
    p = detect_ollama_profile({
        "details": {"parameter_size": "7.6B"},
        "model_info": QWEN2_7B_INFO,
        "parameters": "num_ctx 32768",
    })
    assert p["params_b"] == 7.6
    assert p["arch_max"] == 131072
    assert p["kv_bytes_per_token"] == 57344
    assert p["num_ctx_configured"] == 32768


def test_detect_ollama_profile_missing_fields_no_raise():
    p = detect_ollama_profile({})
    assert p == {"params_b": None, "arch": None, "arch_max": None,
                 "kv_bytes_per_token": None, "num_ctx_configured": None}


def test_resolve_window_min():
    # Qwen3-14B：档位建议64K但架构上限40K → 架构纠正档位
    assert resolve_window(arch_max=40960, profile_suggest=65536,
                          mem_cap=96000, config_override=None) == 40960
    assert resolve_window(arch_max=None, profile_suggest=None,
                          mem_cap=None, config_override=None) is None


def test_usable_and_gate():
    assert usable_window(65536, 8192) == 57344
    assert usable_window(None) is None
    assert is_local_usable(57344, 16384) is True
    assert is_local_usable(12000, 16384) is False  # 低于门禁走在线
    assert is_local_usable(None, 16384) is False


def test_check_effective():
    assert check_effective(65536, 65536) == "ok"
    assert check_effective(65536, 4096) == "truncated"  # 静默截断
    assert check_effective(None, 4096) == "unknown"
    assert check_effective(65536, None) == "unknown"
