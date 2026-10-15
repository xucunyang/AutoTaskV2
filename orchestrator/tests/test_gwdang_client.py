# -*- coding: utf-8 -*-
"""gwdang_client 单元测试（无网络）。

锁住的坑（2026-10-15 首采实测）：
1. data_www 的 series 是多段嵌套窗口（30/90/180/360 天），价格单位是**分**，
   时间是**秒**——归一化必须取最长段、÷100、×1000；
2. 输出与 Mmb.trend 同构（datePrice=[[毫秒,元],…]），坏输入宁返回空不抛；
3. Cookie 会话文件必须在 data/（gitignore 内）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import gwdang_client as gc  # noqa: E402


def _payload():
    return {
        "series": [
            {"period": 90, "min": 400000, "current": 499900,
             "data": [{"x": 1760457600, "y": 434900}]},
            {"period": 360, "max": 509900, "max_stamp": 1778256000,
             "min": 391410, "min_stamp": 1763049600,
             "original": 499900, "current": 499900, "trend": 0,
             "data": [{"x": 1760457600, "y": 434900},
                      {"x": 1763049600, "y": 391410},
                      {"x": 1791993600, "y": 499900}]},
        ],
        "first_inc_time": 1760457600,
    }


def test_normalize_picks_longest_segment_and_scales():
    d = gc.normalize_trend(_payload())
    assert len(d["datePrice"]) == 3                      # 取 360 天段，不是 90 天段
    assert d["datePrice"][0] == [1760457600000, 4349.0]  # 秒→毫秒，分→元
    assert d["datePrice"][1][1] == 3914.1
    assert d["currentPrice"] == 4999.0
    assert d["lowerPrice"] == 3914.1
    assert d["higherPrice"] == 5099.0
    assert d["periodDays"] == 360


def test_normalize_bad_inputs_never_raise():
    for bad in (None, {}, {"series": []}, {"series": [{}]},
                {"series": [{"data": "junk"}]}, "junk", 42):
        assert gc.normalize_trend(bad) == {}


def test_normalize_drops_malformed_points():
    p = {"series": [{"period": 30, "data": [
        {"x": 100, "y": 2000}, "junk", {"x": "bad", "y": 1}, {"x": 300},
        {"x": 400, "y": 5000}]}]}
    d = gc.normalize_trend(p)
    assert d["datePrice"] == [[100000, 20.0], [400000, 50.0]]


def test_session_file_lives_under_data():
    """Cookie 是运行时态：必须在 data/（gitignore 内），不得进版本库。"""
    assert gc.SESSION_FILE.parent.name == "data"


def test_load_session_false_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "SESSION_FILE", tmp_path / "nope.json")
    assert gc.Gwdang().load_session() is False


def test_load_session_sets_cookie_and_validates_token(tmp_path, monkeypatch):
    f = tmp_path / "gwdang_session.json"
    f.write_text('{"cookies": [[".gwdang.com", "GWD_USER_TOKEN", "tok"]]}',
                 encoding="utf-8")
    monkeypatch.setattr(gc, "SESSION_FILE", f)
    g = gc.Gwdang()
    assert g.load_session() is True
    assert any(c.name == "GWD_USER_TOKEN" for c in g.cj)
