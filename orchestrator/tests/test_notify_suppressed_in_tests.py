"""测试**绝不许**往用户桌面弹真通知——2026-10-06 事故的回归钉子。

那天用户贴来一张截图：「❌ 起不来 housing_daily · 2026-10-05，退出码 3」。
查下来生产五条链全部 LastResult=0，压根没坏；真凶是
`tests/test_run_daily.py::test_unknown_job_fails_loudly`——它故意传拼错的
`--job housing_dayly` 验证退出码 3，而那条分支会调 `winnotify.notify()`，
于是**每跑一次全量 pytest 就弹一次假警报**，当天弹了 15 次。

这类 bug 特别难靠"看日志"发现：通知本身没有异常、没有堆栈，
唯一痕迹是 `wpndatabase.db` 里的 ArrivalTime。所以钉在**行为**上：
直接跑那条会弹窗的测试，然后断言通知函数没被真正调用。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import winnotify as wn  # noqa: E402


def test_suppressed_by_env_flag(monkeypatch):
    monkeypatch.setenv(wn.SUPPRESS_ENV, "1")
    assert wn.suppressed() is True


def test_suppressed_detects_pytest_even_without_env(monkeypatch):
    """兜住**子进程**：只认环境变量的话，从测试里 spawn 出来的
    run_daily 会照样弹窗——它有自己的进程，也有自己的环境。"""
    monkeypatch.delenv(wn.SUPPRESS_ENV, raising=False)
    # pytest 自己会设 PYTEST_CURRENT_TEST，这里显式模拟以免依赖实现细节
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "unit (call)")
    assert wn.suppressed() is True


def test_not_suppressed_in_normal_run(monkeypatch):
    monkeypatch.delenv(wn.SUPPRESS_ENV, raising=False)
    monkeypatch.delenv(wn.FORCE_ENV, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert wn.suppressed() is False


def test_force_beats_suppress(monkeypatch):
    """`AUTOTASK_FORCE_NOTIFY` 必须**压过**静默，且压得过 PYTEST_CURRENT_TEST。

    tests/test_winnotify.py 整个模块靠它才能测到"真的会弹"——
    它没法靠 delenv PYTEST_CURRENT_TEST 脱身，因为 pytest 在 call 阶段
    会再设一次（踩过：删了 env 变量，8 条用例照样全被短路）。
    force 不压过静默的话，那边就只能靠 monkeypatch 改函数本身，
    改函数等于把闸一起换掉——就又变成"测了个寂寞"了。
    """
    monkeypatch.setenv(wn.FORCE_ENV, "1")
    monkeypatch.setenv(wn.SUPPRESS_ENV, "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "unit (call)")
    assert wn.suppressed() is False


def test_notify_short_circuits_when_suppressed(monkeypatch):
    """闸必须真的**拦住**，而不是"记个日志然后照弹"。"""
    monkeypatch.setenv(wn.SUPPRESS_ENV, "1")
    calls: list[str] = []
    monkeypatch.setattr(
        wn, "_run_powershell",
        lambda *a, **k: (calls.append("ps") or (True, "_OK")))
    assert wn.notify("t", "b", fail=True) is False
    assert calls == [], "静默时仍然去起 PowerShell 了——闸没拦住"


def test_notify_summary_does_not_log_failure_when_suppressed(tmp_path, monkeypatch):
    """静默不是"发送失败"。记成 winnotify_failed 会让断言结构化日志的
    测试以为通知链路坏了，而它其实是主动不弹——两种故障要分得开。"""
    monkeypatch.setenv(wn.SUPPRESS_ENV, "1")
    assert wn.notify_summary(tmp_path, "housing_daily", "2026-10-06",
                             rc=0, done=3, dead=0, not_terminal=[], elapsed_s=72.0
                             ) is False
    log = (tmp_path / "logs" / "structured.jsonl")
    txt = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "winnotify_suppressed" in txt, f"没记 suppressed：{txt[:300]}"
    assert "winnotify_failed" not in txt, "静默被误记成发送失败"


def test_the_offending_path_no_longer_pops(tmp_path, monkeypatch):
    """端到端：走当年弹窗的那条代码路径，断言 notify 没被调用。

    这里直接调 `run_daily_main` 而不是去 import 那条测试函数——
    capsys 没法手工构造（`CaptureFixture.__init__` 要 request），
    而且要验的是**行为**（会不会弹），不是某条测试的写法。
    钉的是这条路径：哪天有人删了 winnotify 的闸，这里会红。
    """
    from scripts.run_daily import main as run_daily_main
    from tests.test_run_daily import _root, DATE

    _root(tmp_path)
    # 探针放在 _run_powershell 而不是 notify：**闸就在 notify 里面**，
    # 把 notify 换掉等于把闸一起换掉了，测了个寂寞。
    # 真正会弹窗的动作是拉起 PowerShell 送 WinRT toast——它没被调用，
    # 就说明用户桌面确实什么都没发生。
    calls: list[str] = []
    monkeypatch.setattr(
        wn, "_run_powershell",
        lambda *a, **k: (calls.append("ps") or (True, "_OK")))

    rc = run_daily_main(["--root", str(tmp_path), "--date", DATE,
                         "--job", "housing_dayly", "--dry-run"])
    assert rc == 3, "退出码语义变了，这条路径的原始意图要先保住"
    assert calls == [], "unknown_job 路径仍然拉起了 PowerShell（= 仍然在弹）"