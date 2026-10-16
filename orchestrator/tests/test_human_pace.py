# -*- coding: utf-8 -*-
"""human_pace.Pacer 单测：注入式时钟，零真实睡眠。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.human_pace import PRESETS, Pacer  # noqa: E402


class FakeClock:
    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, s: float):
        self.sleeps.append(s)

    def rng(self, a: float, b: float) -> float:
        return (a + b) / 2  # 稳定可断言：取中点


def test_interval_jitter_between_min_and_max():
    fc = FakeClock()
    p = Pacer(2, 7, sleeper=fc.sleep, rng=fc.rng)
    for _ in range(5):
        p.wait()
    assert fc.sleeps == [4.5] * 5          # 中点 = (2+7)/2
    assert p.n == 5


def test_batch_pause_every_n():
    fc = FakeClock()
    p = Pacer(2, 7, pause_every=10, pause_range=(300, 600),
              sleeper=fc.sleep, rng=fc.rng)
    for _ in range(10):
        p.wait()
    assert fc.sleeps == [4.5] * 10          # 前 10 发只有间隔
    p.wait()                                # 第 11 发前触发批间休息
    assert fc.sleeps[10] == 450.0           # 批休 = (300+600)/2，先于本次间隔
    assert fc.sleeps[-1] == 4.5


def test_no_pause_below_threshold():
    fc = FakeClock()
    p = Pacer(1, 2, pause_every=100, pause_range=(300, 600),
              sleeper=fc.sleep, rng=fc.rng)
    for _ in range(50):
        p.wait()
    assert all(s < 3 for s in fc.sleeps)    # 从未触发批休


def test_for_channel_presets():
    fc = FakeClock()
    m = Pacer.for_channel("mmb", sleeper=fc.sleep, rng=fc.rng)
    g = Pacer.for_channel("gwdang", sleeper=fc.sleep, rng=fc.rng)
    assert (m.min_s, m.max_s) == (2, 7) and m.pause_every == 10
    assert (g.min_s, g.max_s) == (4, 12) and g.pause_every == 8
    # 整体拉长：gwdang 任何间隔下界都高于 mmb 上界的一半
    assert g.min_s > m.min_s
    assert set(PRESETS) == {"mmb", "gwdang"}


def test_for_channel_unknown_rejects():
    try:
        Pacer.for_channel("nope")
        assert False, "应当拒绝未知渠道"
    except KeyError:
        pass
