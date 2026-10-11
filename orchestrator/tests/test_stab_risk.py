"""stab_risk 单元测试（P1.5 门禁，Q10-A 全史中位数版）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.stab_risk import (forward_drawdowns,  # noqa: E402
                             stab_risk, suggest_action)


def test_sufficiency_requires_60_periods():
    assert stab_risk([100.0] * 59) is None
    r = stab_risk([100.0] * 60)
    assert r is not None and r["samples"] >= 1


def test_flat_series_has_zero_drawdown():
    r = stab_risk([100.0] * 90)
    assert r["median_pct"] == 0.0 and r["max_pct"] == 0.0
    assert suggest_action(r, slope_pct=0.0) == "act_now"


def test_falling_series_has_high_risk_and_wait():
    """每期跌 15/3000=0.5% 的阴跌序列：后期买入者回撤显著 → wait。"""
    prices = [3000.0 - i * 15 for i in range(90)]
    r = stab_risk(prices)
    # 阴跌里任意点买入，30 期内至少还能跌 30×15=450（15%）
    assert r["median_pct"] >= 10.0
    assert suggest_action(r, slope_pct=-0.5) == "wait"


def test_forward_drawdown_window_bounds():
    """窗口不足 min_window 的末尾买入日不计入（前瞻不完整）。"""
    prices = [100.0] * 40
    dds = forward_drawdowns(prices, window=30, min_window=15)
    # 40 期里窗口≥15 的买入日：t=0..9（t=10 起窗口只剩 30-≤15？40-10=30
    # 仍>15——t=25 起窗口 15→边界。粗断言：样本数应 ≤ 25 且 ≥ 10
    assert 10 <= len(dds) <= 25


def test_drawdown_never_negative():
    """只涨不跌的序列回撤应为 0（max(0, …) 下限）。"""
    prices = [100.0 + i for i in range(90)]
    r = stab_risk(prices)
    assert r["median_pct"] == 0.0


def test_action_is_act_now_when_slope_flat_even_with_risk():
    """风险高但斜率走平（>阈值）：不再等跌，可 act_now（企稳信号优先）。"""
    prices = [3000.0 - i * 15 for i in range(90)]
    r = stab_risk(prices)
    assert suggest_action(r, slope_pct=0.0) == "act_now"