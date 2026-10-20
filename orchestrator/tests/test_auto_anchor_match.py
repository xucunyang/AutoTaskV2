# -*- coding: utf-8 -*-
"""match_candidate 键组匹配回归（10-19：19/19 全灭两根因——
① 实体名带 Core/Ryzen 前缀而搜索标题是短版 ② auto_anchor 无提案名 sim 恒 0）"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.prefill_jingfen import match_candidate  # noqa: E402


def _row(title, price=1999.0, skuid="123", siteid="1"):
    return {"title": title, "price": price, "skuid": skuid, "siteid": siteid}


_ROWS = [
    _row("英特尔(Intel)酷睿Ultra5 245K 盒装处理器", 1899.0, "101"),
    _row("英特尔(Intel)酷睿Ultra7 270K 盒装CPU 处理器", 2499.0, "102"),
    _row("AMD锐龙7 9800X3D 游戏处理器 8核16线程 盒装CPU", 2899.0, "103"),
    _row("技嘉RTX 5060 Ti 魔鹰 8G 显卡", 3599.0, "104"),
    _row("英特尔酷睿 i5-14600KF 处理器", 1599.0, "105"),
]


def _pick(series, spec="", aliases=None):
    best = match_candidate(_ROWS, series, spec, "", None, aliases=aliases)
    return best[1]["skuid"] if best else None


def test_cpu_short_title_matches_without_core_prefix():
    assert _pick("Core Ultra 7 270K", "", ["270KF", "Ultra7 270K"]) == "102"
    assert _pick("Core Ultra 5 245K") == "101"


def test_ryzen_short_title_matches():
    assert _pick("Ryzen 7 9800X3D", "", ["9800X3D"]) == "103"


def test_gpu_and_i_series_still_match():
    assert _pick("RTX 5060 Ti", "8GB") == "104"
    assert _pick("Core i5-14600KF") == "105"


def test_alias_rescues_cold_series_name():
    assert _pick("Ultra7 270KF 特殊写法", "", ["270K"]) == "102"


def test_blacklist_rejects_bundles_and_peripherals():
    """10-20 实测：显示器配 245K、B860M 套装配 285K、工作站配 4060Ti。"""
    rows = [
        _row("酷开(coocaa)创维酷开24.5英寸2K高清120Hz 1ms显示器 适配Ultra5 245K",
             699.0, "201"),
        _row("七彩虹(Colorful)BATTLE-AX B860M-W 搭Ultra9 285K 主板CPU套装",
             4299.0, "202"),
        _row("联想开天M90h 国产信创台式工作站 RTX 4060 Ti 16GB", 9999.0, "203"),
        _row("英特尔酷睿Ultra7 270K/265K/250K/245K 盒装", 2499.0, "204"),
    ]
    assert match_candidate(rows, "Core Ultra 5 245K", "", "", None) is None
    assert match_candidate(rows, "Core Ultra 9 285K", "", "",
                           None, aliases=["285K"]) is None
    assert match_candidate(rows, "RTX 4060 Ti", "16GB", "", None) is None
    assert match_candidate(rows, "Core Ultra 7 265K", "", "",
                           None, aliases=["265K"]) is None  # 多 SKU 页不可作锚


def test_short_alias_and_multisku_page_rejected():
    """短别名 '285k' 会命中套装页（黑名单拦）；多 SKU 斜杠页不可作单品锚。"""
    rows = [_row("七彩虹B860M-W搭Intel Ultra9 285K 高端套装", 4299.0, "301")]
    assert match_candidate(rows, "Core Ultra 9 285K", "", "",
                           None, aliases=["285K"]) is None
    multi = [
        _row("英特尔酷睿Ultra7 270K/265K/250K/245K 盒装", 2499.0, "302"),
        _row("英特尔(Intel)酷睿Ultra7 270K 盒装CPU 处理器", 2499.0, "303"),
    ]
    # 265K 实体（别名 265k）应跳过多 SKU 页、命中纯 270K……不对——应命中
    # 含 265k 的正规单品；此页若无单品 265K 则留人工
    assert match_candidate(multi, "Core Ultra 7 265K", "", "",
                           None, aliases=["265K"]) is None


def test_empty_sp_name_does_not_kill_sim():
    """auto_anchor 场景：无提案名 → 键组命中即门槛，不被 sim>=0.40 刷掉。"""
    assert _pick("Core Ultra 7 270K") == "102"
