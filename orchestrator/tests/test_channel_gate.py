# -*- coding: utf-8 -*-
"""channel_gate 单测：锁/预算/节奏组合，tmp 状态目录，零真实睡眠与零网络。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import channel_gate as cg  # noqa: E402
from tools.channel_gate import (BudgetExhausted, ChannelGate, LockHeld,  # noqa: E402
                                acquire, lock_path, release)


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHANNEL_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


class FakePacer:
    """替换 gate.pacer，记录 wait 次数，不睡。"""

    def __init__(self):
        self.n = 0

    def wait(self):
        self.n += 1
        return 0.5


def _quiet(monkeypatch):
    monkeypatch.setattr(cg, "print", lambda *a, **k: None, raising=False)


def test_lock_roundtrip(state, monkeypatch):
    _quiet(monkeypatch)
    with cg.lock("mmb", "测试任务"):
        assert lock_path("mmb").exists()
        h = cg._read_lock("mmb")
        assert h["purpose"] == "测试任务" and h["pid"] == cg.os.getpid()
    assert not lock_path("mmb").exists()          # 退出即释放


def test_lock_refuses_when_held_by_other(state, monkeypatch):
    _quiet(monkeypatch)
    import subprocess, sys
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_path("mmb").parent.mkdir(parents=True)
        lock_path("mmb").write_text(json.dumps(
            {"pid": other.pid, "purpose": "别人的任务", "started_at": "t"}), encoding="utf-8")
        with pytest.raises(LockHeld) as ei:
            acquire("mmb", "我的任务")
        assert "别人的任务" in str(ei.value)
    finally:
        other.terminate()
        other.wait(timeout=10)


def test_lock_steals_dead_pid(state, monkeypatch):
    _quiet(monkeypatch)
    lock_path("mmb").parent.mkdir(parents=True)
    lock_path("mmb").write_text(json.dumps(
        {"pid": 999999999, "purpose": "死进程", "started_at": "t"}), encoding="utf-8")
    h = acquire("mmb", "新任务")                   # 死 pid → 自动抢占
    assert h["purpose"] == "新任务"


def test_lock_force_takeover(state, monkeypatch):
    _quiet(monkeypatch)
    lock_path("mmb").parent.mkdir(parents=True)
    lock_path("mmb").write_text(json.dumps(
        {"pid": 999999999, "purpose": "占着", "started_at": "t"}), encoding="utf-8")
    acquire("mmb", "强制接管", force=True)
    assert cg._read_lock("mmb")["purpose"] == "强制接管"


def test_release_refuses_foreign(state, monkeypatch, capsys):
    lock_path("mmb").parent.mkdir(parents=True)
    lock_path("mmb").write_text(json.dumps(
        {"pid": 1, "purpose": "别家", "started_at": "t"}), encoding="utf-8")
    assert release("mmb") is False                 # pid=1 非本进程（本进程 pid≠1）
    assert lock_path("mmb").exists()
    assert release("mmb", force=True) is True


def test_budget_fail_closed(state, monkeypatch):
    _quiet(monkeypatch)
    gate = ChannelGate("mmb", budget=3)
    gate.pacer = FakePacer()
    with gate.session("预算测试"):
        assert gate.spend(2, "a") is True
        assert gate.spend(1, "b") is True          # 恰好用满
        assert gate.spend(1, "c") is False         # 第 4 发拒发
        assert gate.spent() == 3 and gate.remaining() == 0
    with pytest.raises(BudgetExhausted):           # 预算尽后再 session 直接拒
        with gate.session("再来"):
            pass


def test_budget_persisted_across_instances(state, monkeypatch):
    _quiet(monkeypatch)
    g1 = ChannelGate("mmb", budget=10)
    g1.pacer = FakePacer()
    with g1.session("第一批"):
        g1.spend(4)
    g2 = ChannelGate("mmb", budget=10)             # 新实例读同一台账
    g2.pacer = FakePacer()
    assert g2.spent() == 4 and g2.remaining() == 6


def test_pace_via_gate(state, monkeypatch):
    _quiet(monkeypatch)
    gate = ChannelGate("mmb", budget=99)
    fake = FakePacer()
    gate.pacer = fake
    with gate.session("节奏"):
        for _ in range(5):
            gate.wait()
            gate.spend(1)
    assert fake.n == 5


def test_session_purpose_and_lock_file_locations(state, monkeypatch):
    _quiet(monkeypatch)
    gate = ChannelGate("gwdang", budget=None)      # gwdang 无预算也能用
    gate.pacer = FakePacer()
    with gate.session("gwd 采集"):
        assert lock_path("gwdang").exists()
        gate.spend(1)
    assert cg.spent_today("gwdang") == 1
    assert not lock_path("gwdang").exists()
