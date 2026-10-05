"""桌面通知（弹框 + 声音）：XML 形状、编码、回退链、绝不抛异常。

跑批通知是**旁路**——它坏了不能连累报告。所以这里最看重两条：
1. 给 PowerShell 的脚本必须纯 ASCII（中文走命令行/管道必踩编码坑），
   中文经 base64 内联；
2. 超时/异常一律返回 False，绝不冒泡到 run_daily。
"""
import base64
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest

from core import winnotify as wn


class FakeRunner:
    """按调用次序返回预设结果，记录每次收到的脚本。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append({"cmd": cmd, "input": kw.get("input")})
        if not self.results:
            raise AssertionError("runner 被多调了一次")
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _proc(rc=0, out=b"TOAST_OK\n"):
    return subprocess.CompletedProcess(["powershell.exe"], rc, out, b"")


def _b64_in(script: str) -> str:
    m = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
    assert m, "脚本里找不到 base64 负载"
    return m.group(1)


def test_toast_xml_carries_title_body_and_sound():
    xml = wn.toast_xml("标题 & <标签>", "第一行\n第二行", fail=True)
    root = ET.fromstring(xml)                      # 解析得了 = 转义正确
    ns = {"t": "toast"}
    texts = [n.text for n in root.findall(".//text")]
    assert texts == ["标题 & <标签>", "第一行", "第二行"]
    audio = root.find("audio")
    assert audio is not None
    assert audio.get("src") == wn.SOUND_FAIL       # 失败要比成功响
    assert audio.get("loop") == "true"
    assert root.get("scenario") == "reminder"      # 停在屏幕上等人看


def test_success_uses_default_sound():
    root = ET.fromstring(wn.toast_xml("t", "b", fail=False))
    assert root.find("audio").get("src") == wn.SOUND_OK
    assert root.find("audio").get("loop") == "false"


def test_powershell_script_is_pure_ascii():
    """中文必须走 base64：脚本本身只能是 ASCII，否则管道编码会毁掉它。"""
    xml = wn.toast_xml("每日日报完成 ✅", "用时 12.3 分钟 · 完成 4", fail=False)
    script = wn._toast_script(xml)
    script.encode("ascii")                         # 非 ASCII 会直接抛
    assert _b64_in(script) == base64.b64encode(
        xml.encode("utf-8")).decode("ascii")
    # 回来必须还是原来那段中文
    roundtrip = base64.b64decode(_b64_in(script)).decode("utf-8")
    assert "✅" in roundtrip and "用时 12.3 分钟" in roundtrip
    assert "powershell.exe" in script


def test_notify_success_uses_single_powershell_call(monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: True)
    runner = FakeRunner(_proc(out=b"TOAST_OK\n"))
    assert wn.notify("标题", "正文", runner=runner) is True
    assert len(runner.calls) == 1
    assert runner.calls[0]["cmd"][0] == "powershell.exe"
    assert runner.calls[0]["cmd"][-1] == "-"
    assert runner.calls[0]["input"].decode("ascii")   # 必须是 ASCII 脚本


def test_notify_falls_back_to_balloon(monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: True)
    runner = FakeRunner(_proc(rc=1, out=b"boom"),
                        _proc(out=b"BALLOON_OK\n"))
    assert wn.notify("标题", "正文", runner=runner) is True
    assert len(runner.calls) == 2
    assert b"ShowBalloonTip" in runner.calls[1]["input"]


def test_notify_returns_false_when_both_paths_fail(monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: True)
    runner = FakeRunner(_proc(rc=1, out=b"toast dead"),
                        _proc(rc=1, out=b"balloon dead"))
    assert wn.notify("标题", "正文", runner=runner) is False


def test_notify_swallows_timeout(monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: True)
    runner = FakeRunner(subprocess.TimeoutExpired("powershell.exe", 15),
                        subprocess.TimeoutExpired("powershell.exe", 15))
    assert wn.notify("标题", "正文", runner=runner) is False


def test_notify_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: False)
    runner = FakeRunner()          # 只要被调用就会 AssertionError
    assert wn.notify("标题", "正文", runner=runner) is False
    assert runner.calls == []


def test_summary_message_by_exit_code():
    cases = {
        0: ("跑完了", False),
        1: ("没跑完", True),
        2: ("死信", True),
        3: ("起不来", True),
    }
    for rc, (word, fail) in cases.items():
        title, body, got_fail = wn.daily_summary_message(
            "daily_report", "2026-10-05", rc=rc, done=4, dead=0,
            not_terminal=[], elapsed_min=12.34)
        assert got_fail is fail, rc
        assert word in title and "daily_report" in title
        assert "2026-10-05" in title
        assert "12.3 分钟" in body and "完成 4" in body


def test_summary_message_lists_unfinished_tasks():
    _, body, fail = wn.daily_summary_message(
        "housing_daily", "2026-10-05", rc=1, done=3, dead=0,
        not_terminal=["t3_report", "t4_rollup"], elapsed_min=9.0)
    assert fail is True
    assert "未终态 2" in body
    assert "t3_report" in body and "t4_rollup" in body


def test_notify_summary_respects_disabled_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: True)
    runner = FakeRunner()
    assert wn.notify_summary(tmp_path, "daily_report", "2026-10-05", rc=0,
                             done=4, dead=0, not_terminal=[],
                             elapsed_min=1.0, enabled=False) is False
    assert runner.calls == []


def test_notify_summary_logs_success(tmp_path, monkeypatch):
    monkeypatch.setattr(wn, "_is_windows", lambda: True)
    monkeypatch.setattr(wn, "notify", lambda *a, **k: True)
    assert wn.notify_summary(tmp_path, "daily_report", "2026-10-05", rc=0,
                             done=4, dead=0, not_terminal=[],
                             elapsed_min=1.0) is True
    log = tmp_path / "logs" / "structured.jsonl"
    assert log.is_file()
    assert "winnotify_sent" in log.read_text(encoding="utf-8")


@pytest.mark.parametrize("rc", [0, 1, 2, 3])
def test_notify_summary_always_calls_notify(tmp_path, monkeypatch, rc):
    """四种结局都要弹，一条都不能漏（漏了就等于没人知道）。"""
    seen = {}

    def _fake(title, body, *, fail=False, **k):
        seen.update(title=title, body=body, fail=fail)
        return True

    monkeypatch.setattr(wn, "notify", _fake)
    assert wn.notify_summary(tmp_path, "housing_daily", "2026-10-05", rc=rc,
                             done=3, dead=1 if rc == 2 else 0,
                             not_terminal=[] if rc == 0 else ["t3"],
                             elapsed_min=5.0) is True
    assert seen["fail"] is (rc != 0)
    assert "housing_daily" in seen["title"]
