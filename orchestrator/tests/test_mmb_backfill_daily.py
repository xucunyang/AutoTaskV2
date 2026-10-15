# -*- coding: utf-8 -*-
"""mmb_backfill_daily 每日回填包装器的门禁（2026-10-15 挂 schtasks 前）。

_zero-API_：全部用 monkeypatched 的 _run 假结果，不碰网络、不碰真库——
run 子命令 --only 未命中时在会话加载前直接返回 rc=2，本 wrapper 的失败
分支正好走这条；成功分支靠 --from-cache + --dry-run 同样零网络。

锁住的三件事：
1. 失败分支：backfill 退 2 → 不再跑 trend_calc，弹 fail 通知带 login 提示，
   wrapper 原样返回 2（计划任务靠退出码判别）。
2. 成功分支：两步都跑，通知正文带入库行数（从 "+N 行" 输出行汇总）。
3. 命令行参数原样透传给 run 子命令（如 --only 做单 listing 冒烟）。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "mmb_backfill_daily", SCRIPTS / "mmb_backfill_daily.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    mod = _load_wrapper()
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path)
    calls = {"run": [], "notify": []}

    def fake_run(cmd):
        calls["run"].append(cmd)
        return calls["fake_result"]

    def fake_notify(title, body, **kw):
        calls["notify"].append((title, body, kw))
        return True

    monkeypatch.setattr(mod, "_run", fake_run)
    import core.winnotify as wn
    monkeypatch.setattr(wn, "notify", fake_notify)
    return mod, calls, tmp_path


def _ok(cmd, out=""):
    return subprocess.CompletedProcess(cmd, 0, out, "")


def test_session_expired_stops_pipeline_with_login_hint(ctx):
    """rc=2 = 会话过期：trend_calc 不跑，通知带 login 提示，返回 2。"""
    mod, calls, tmp = ctx
    calls["fake_result"] = subprocess.CompletedProcess(
        ["backfill"], 2, "未登录：先执行 python -m tools.manmanbuy_backfill login", "")
    assert mod.main([]) == 2
    assert len(calls["run"]) == 1            # 第二步没跑
    assert len(calls["notify"]) == 1
    title, body, kw = calls["notify"][0]
    assert kw.get("fail") is True
    assert "login" in body
    assert (tmp / f"mmb_backfill_{__import__('time').strftime('%Y-%m-%d')}.log").is_file()


def test_success_notifies_added_rows(ctx):
    """两步 rc=0：通知正文汇总入库行数，返回 0。"""
    import unittest.mock as mock

    mod, calls, _tmp = ctx
    queue = [
        _ok(["backfill"], "技嘉卡: +410 行（2024-01-01 ~ 2026-10-15）\n"),
        _ok(["trend"], "snapshot date=2026-10-15 entities=36 contrasts=0"),
    ]

    def fake_run2(cmd):
        calls["run"].append(cmd)
        return queue.pop(0)

    with mock.patch.object(mod, "_run", side_effect=fake_run2):
        assert mod.main([]) == 0
    assert len(calls["run"]) == 2
    title, body, kw = calls["notify"][0]
    assert kw.get("fail", False) is False
    assert "新增 410 行" in body


def test_argv_passthrough_to_run(ctx):
    """--only 等参数原样进 backfill 命令行。"""
    import unittest.mock as mock

    mod, calls, _tmp = ctx
    with mock.patch.object(
            mod, "_run", return_value=_ok(["x"], "")) as m:
        mod.main(["--only", "9070"])
        cmd = m.call_args_list[0][0][0]
    assert "--only" in cmd and "9070" in cmd
    trend_cmd = m.call_args_list[1][0][0]
    assert any("trend_calc" in str(a) for a in trend_cmd)
