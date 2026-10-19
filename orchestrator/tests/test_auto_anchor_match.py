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


def test_empty_sp_name_does_not_kill_sim():
    """auto_anchor 场景：无提案名 → 键组命中即门槛，不被 sim>=0.40 刷掉。"""
    assert _pick("Core Ultra 7 270K") == "102"
