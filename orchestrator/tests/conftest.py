r"""测试期全局静默桌面通知。

2026-10-06 的事故链（用户先看到的现象 → 真实原因）：

    截图：「❌ 起不来 housing_dayly · 2026-10-05 / 退出码 3」
    ↓ 查 \AutoTaskV2\ 五个任务：全部 LastResult=0，生产没坏
    ↓ 查 reports/run_daily_*.log：没有 unknown_job，也没这个 typo 的日志
    ↓ 读 Windows 通知库 wpndatabase.db：同一条 toast **投递了 15 次**，
      最新一条当天 15:56 —— 不是旧卡，是真的在反复弹
    ↓ 定位 tests/test_run_daily.py::test_unknown_job_fails_loudly：
      它故意传拼错的 `--job housing_dayly` 验证退出码 3，
      而那条分支会调 winnotify.notify() —— **每跑一次全量 pytest 弹一次**
    ↓ 实验确认：单独跑那一条测试，通知库立刻多出一条

为什么必须在这里拦，而不是"让那条测试自己注意"：
弹窗是**找人**的东西。测试拿真弹窗去吓用户，等于用假警报训练人
忽略真警报——真出事那天，人已经条件反射地当噪声划掉了。
而且下一条测试再出一个弹窗，很难联想到"这是上一条测试留下的"。

两道闸（core.winnotify.suppressed）都要有：
- 这里设环境变量，覆盖**本进程**的测试；
- winnotify 认 PYTEST_CURRENT_TEST，覆盖**子进程**里跑的 run_daily。
只设环境变量，子进程那条路径会漏。
"""
from __future__ import annotations

import os

import pytest

from core.winnotify import SUPPRESS_ENV


@pytest.fixture(autouse=True)
def _no_desktop_popups(monkeypatch):
    """每条测试都静默桌面通知——不管它自己有没有这个念头。"""
    monkeypatch.setenv(SUPPRESS_ENV, "1")