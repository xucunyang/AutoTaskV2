# -*- coding: utf-8 -*-
"""human_pace：拟人节奏器（独立通用模块，任何业务可直接调用）。

为什么存在：
    固定间隔是机器指纹（慢慢买 10-15 封禁复盘）；连发不停会踩"频繁连续操作"
    红线（客服原话：多次被封无法处理）。本模块把"像人"的节奏抽象成纯算法：
    - 每请求间隔 = uniform(min_s, max_s) 随机抖动
    - 连续 pause_every 发 → 强制批间休息 uniform(*pause_range)
    - 无状态、无 I/O：sleeper/rng 可注入，测试零真实睡眠

用法（最简）：
    from tools.human_pace import Pacer
    p = Pacer(2, 7, pause_every=10, pause_range=(300, 600))
    for url in urls:
        p.wait()          # 该歇就歇，返回实际等待秒数
        fetch(url)

预置档（新业务先查这里，没有再自定）：
    Pacer.for_channel("mmb")     → (2, 7)   每 10 发歇 300~600s
    Pacer.for_channel("gwdang")  → (4, 12)  每 8 发歇 300~900s（整体拉长）

需要"单写者锁 + 日预算"的完整渠道治理时，用 channel_gate.ChannelGate
（内部组合本模块）；只想要节奏的轻量场景直接用本模块。
"""
from __future__ import annotations

import random
import time

# 渠道预置档：间隔(下,上) / 批间休息(每N发, (下,上))
PRESETS: dict[str, dict] = {
    "mmb": dict(min_s=2, max_s=7, pause_every=10, pause_range=(300, 600)),
    "gwdang": dict(min_s=4, max_s=12, pause_every=8, pause_range=(300, 900)),
}


class Pacer:
    """拟人节奏器。sleeper/rng 可注入（测试传假时钟，零真实睡眠）。"""

    def __init__(self, min_s: float = 3.0, max_s: float = 8.0,
                 pause_every: int | None = None,
                 pause_range: tuple[float, float] | None = None,
                 sleeper=None, rng=None, log=None, name: str = ""):
        assert max_s >= min_s > 0, "间隔上界必须 ≥ 下界 > 0"
        self.min_s, self.max_s = float(min_s), float(max_s)
        self.pause_every = pause_every
        self.pause_range = pause_range
        self._sleep = sleeper or time.sleep
        self._rng = rng or random.uniform
        self._log = log or print
        self.name = name or "pace"
        self.n = 0  # 已放行请求数

    @classmethod
    def for_channel(cls, channel: str, **overrides) -> "Pacer":
        """按渠道取预置档，overrides 可覆盖任意参数。"""
        if channel not in PRESETS:
            raise KeyError(f"无 {channel!r} 预置档，可选：{sorted(PRESETS)}；新渠道请先在 PRESETS 登记档位")
        kw = dict(PRESETS[channel])
        kw.update(overrides)
        return cls(name=channel, **kw)

    def wait(self) -> float:
        """请求前调用：该批休就批休，然后随机间隔。返回本次实际等待秒数。"""
        waited = 0.0
        if self.pause_every and self.n and self.n % self.pause_every == 0:
            if self.pause_range:
                p = self._rng(*self.pause_range)
                self._log(f"  ~ [{self.name}] 批间休息 {p:.0f}s（已连发 {self.n} 发）")
                self._sleep(p)
                waited += p
        d = self._rng(self.min_s, self.max_s)
        self._sleep(d)
        self.n += 1
        return waited + d
