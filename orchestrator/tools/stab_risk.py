"""stab_risk：背刺风险指标（L1，Q10-A 全史中位数版）。

定义（事前视角，value-lens 闸门后）：
    若**今日买入**，历史上处于「全部历史买入日」的人，买入后 30 期内
    最大回撤的中位数（与 p75）。

对冲的心理：嗔（怕买贵）。输出挂 action：中位回撤 >10% 且斜率未走平
→ wait；否则 act_now（这只是默认阈值，报告可加其他证据调整）。

数学：
    forward_drawdown(t) = max(0, (price(t) - min(price(t..t+30))) / price(t))
    stab_risk_median = median(forward_drawdown over all t with t+30 数据)

边界：序列 <60 期 → sufficiency 不足，返回 None（调用方显示占位，
Q12 拍板）。末尾 30 期内的买入日没有完整前瞻窗口——仍参与统计但
窗口截短到可用期数（min(p_t..t_end)），这会低估近端回撤，故对
「窗口不足 15 期」的买入日不计入（窗口至少半满才有代表性）。
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path


def forward_drawdowns(prices: list[float], window: int = 30,
                      min_window: int = 15) -> list[float]:
    """每个买入日的 forward 最大回撤（比例，0~1）。

    窗口不足 min_window 的末尾买入日不计入（前瞻不完整，统计无代表
    性——宁可少样本不可坏样本）。
    """
    out = []
    n = len(prices)
    for i in range(n):
        end = min(n, i + window + 1)
        if end - i <= min_window:
            break
        lowest = min(prices[i:end])
        dd = (prices[i] - lowest) / prices[i] if prices[i] else 0.0
        out.append(max(0.0, dd))
    return out


def stab_risk(prices: list[float], window: int = 30,
              min_history: int = 60) -> dict | None:
    """返回 {'median_pct', 'p75_pct', 'max_pct', 'samples'}；不足返回 None。

    门槛 min_history=60 期（Q10-A：全史中位数版的最低样本）。
    """
    if len(prices) < min_history:
        return None
    dds = forward_drawdowns(prices, window=window)
    if not dds:
        return None
    return {
        "median_pct": round(statistics.median(dds) * 100, 2),
        "p75_pct": round(statistics.quantiles(dds, n=4)[2] * 100, 2)
        if len(dds) >= 4 else round(max(dds) * 100, 2),
        "max_pct": round(max(dds) * 100, 2),
        "samples": len(dds),
    }


def suggest_action(risk: dict, slope_pct: float,
                   wait_slope_threshold: float = -0.2) -> str:
    """按风险与趋势挂动作（value-lens 准入：每个指标必须挂 action）。

    中位回撤 >10%（历史买的人多半被套）且斜率未走平（≤阈值）
    → wait；其余 → act_now。
    """
    if risk["median_pct"] > 10.0 and slope_pct <= wait_slope_threshold:
        return "wait"
    return "act_now"


if __name__ == "__main__":
    # 自检：平稳序列 vs 下跌序列的背刺风险应显著不同
    flat = [3000.0] * 90
    falling = [3000.0 - i * 15 for i in range(90)]      # 每期跌 15
    print("flat  :", stab_risk(flat))
    print("falling:", stab_risk(falling))