# -*- coding: utf-8 -*-
"""manmanbuy_client 单元测试（无网络）。

锁住的坑（2026-10-14 首采实测）：
1. datePrice 无外括号字符串必须补括号整体 loads——直接 loads 必败落空
   （交接文档 §5.3 血泪清单第 3 条，工具第一版就栽在这里）；
2. 签名与键序无关、空值跳过、输出大写十六进制；
3. ticket 右旋 4 位。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import manmanbuy_client as mc  # noqa: E402


def test_parse_date_price_string_without_outer_brackets():
    s = '[1730000000000, 3599.0, "到手价3599.00"],[1730100000000, 3499.0]'
    pts = mc.parse_date_price(s)
    assert len(pts) == 2
    assert pts[0][0] == 1730000000000 and pts[0][1] == 3599.0
    assert pts[0][2] == "到手价3599.00"


def test_parse_date_price_list_passthrough_and_filters():
    pts = mc.parse_date_price([[1, 2.0], [3, 4.0, "x"], "junk", [5]])
    assert pts == [[1, 2.0], [3, 4.0, "x"]]


def test_parse_date_price_never_raises():
    """坏输入宁可返回空列表（丢曲线）也不中断整批采集。"""
    for bad in (None, "", "   ", "[1,2,", "{not json}", 123):
        assert mc.parse_date_price(bad) == []


def test_rotate4():
    assert mc.rotate4("12345678") == "56781234"


def test_sign_tool_key_order_insensitive():
    a = mc.Mmb._sign_tool({"method": "getHistoryTrend", "key": "X", "t": "1"})
    b = mc.Mmb._sign_tool({"t": "1", "key": "X", "method": "getHistoryTrend"})
    assert a == b
    assert len(a) == 32 and a == a.upper()


def test_sign_tool_skips_empty_values():
    """空值不参与签名（接口对空 param 的既定语义）。"""
    with_empty = mc.Mmb._sign_tool({"method": "m", "key": "X", "t": "1", "extra": ""})
    without = mc.Mmb._sign_tool({"method": "m", "key": "X", "t": "1"})
    assert with_empty == without


def test_session_file_lives_under_data():
    """Cookie 是运行时态：必须在 data/（gitignore 内），不得进版本库。"""
    assert mc.SESSION_FILE.parent.name == "data"
