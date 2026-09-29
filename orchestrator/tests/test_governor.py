"""governor双平台空闲检测单测：分派正确 + 失败保安全。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.governor as G


def test_idle_seconds_returns_nonneg_float():
    v = G.idle_seconds()
    assert isinstance(v, float) and v >= 0.0


def test_platform_dispatch_picks_win_impl():
    # 当前跑在Windows CI/开发机上：分派必须走_win分支且与直接调一致
    if sys.platform == "win32":
        assert G.idle_seconds() == G._idle_win()


def test_idle_mac_graceful_without_ioreg():
    # Windows无ioreg → FileNotFoundError被吞 → 0.0（保安全判ACTIVE）
    if sys.platform == "win32":
        assert G._idle_mac() == 0.0
    else:
        v = G._idle_mac()
        assert isinstance(v, float) and v >= 0.0


def test_governor_sample_manual_override_pins_profile():
    g = G.Governor()
    assert g.sample(override="IDLE")["profile"] == "IDLE"
    assert g.sample(override="ACTIVE")["profile"] == "ACTIVE"
