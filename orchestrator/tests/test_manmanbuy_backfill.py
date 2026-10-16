# -*- coding: utf-8 -*-
"""manmanbuy_backfill 渠道适配器测试（无网络，--from-cache 离线重放）。

锁住的门禁：
1. 按日坍缩：同日多点取最低（契约 aggregate_rule: min），备注随最低点；
2. 三轨 attrs 必带 + promo_event 逐日判定（国补/618/双11）；
3. 幂等：同缓存重跑，文件不膨胀、DB 不重复（upsert + 行级去重）；
4. 注册表校验：坏 channel/坏 url/坏 vendor 在采集前拦下；
5. 端到端：缓存 → observations.json → ingest_file → sku.db，
   source=manmanmai 落库且实体正确匹配。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import manmanbuy_backfill as mbf  # noqa: E402
from tools.sku_db import connect, seed_entities  # noqa: E402
from tools.sku_ingest import Contract  # noqa: E402

SEED = ROOT / "config" / "contracts" / "hardware_entities.yaml"


def _ts(day: str, hour: int = 12) -> int:
    """接口口径的毫秒时间戳（collapse_daily 按 localtime 分日）。"""
    return int(time.mktime(time.strptime(
        f"{day} {hour:02d}:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000


# ---------- 按日坍缩 ----------

def test_collapse_daily_takes_min_with_its_remark():
    pts = [[_ts("2026-05-17", 9), 3347.0, "国补前3347"],
           [_ts("2026-05-17", 21), 2879.1, "购买1件,国补前3347"],
           [_ts("2026-05-18", 10), 2969.0, ""]]
    daily = mbf.collapse_daily(pts)
    assert daily["2026-05-17"] == (2879.1, "购买1件,国补前3347")
    assert daily["2026-05-18"] == (2969.0, "")


def test_promo_from_remark():
    assert mbf.promo_from("国补前3347") == "国补"
    assert mbf.promo_from("618大促到手价2999") == "618"
    assert mbf.promo_from("双11到手价2899") == "双11"
    assert mbf.promo_from("到手价3599.00") == "无"
    assert mbf.promo_from("") == "无"


# ---------- 注册表校验 ----------

def test_load_registry_validates_against_contract():
    reg = mbf.load_registry(Contract.load(ROOT, "hardware"))
    assert reg["source"] == "manmanmai"
    assert len(reg["listings"]) >= 3          # 2026-10-14 首批：8G×1 + 16G×2
    for it in reg["listings"]:
        assert {"category", "series", "spec"} <= set(it["identity"])
        # 锚 URL 两形态：item.jd.com 数字详情页（存量）或 jingfen 采集键（R35）
        assert (it["url"].startswith("https://item.jd.com/")
                or it["url"].startswith("https://jingfen.jd.com/detail/")), it["url"]


@pytest.mark.parametrize("patch", [
    {"channel": "水货"},                       # 不在枚举
    {"url": "https://item.taobao.com/1.htm"},  # 非京东详情页
    {"vendor": "英伟达"},                       # vendor 枚举里没有（NVIDIA 官方不卖卡）
    {"identity": {"category": "显卡", "series": "RTX 5060 Ti"}},  # 缺 spec
    {"anchor": "地板价"},                       # 不在 anchor 枚举（规则 v1）
])
def test_load_registry_rejects_bad_listing(patch, tmp_path):
    base = mbf.load_registry(Contract.load(ROOT, "hardware"))
    bad = dict(base["listings"][0])
    bad.update(patch)
    reg = {"domain": base["domain"], "source": base["source"],
           "listings": base["listings"] + [bad]}
    p = tmp_path / "reg.yaml"
    p.write_text(yaml.safe_dump(reg, allow_unicode=True), encoding="utf-8")
    old = mbf.REGISTRY
    mbf.REGISTRY = p
    try:
        with pytest.raises(ValueError):
            mbf.load_registry(Contract.load(ROOT, "hardware"))
    finally:
        mbf.REGISTRY = old


def test_retired_listing_skipped(tmp_path):
    base = mbf.load_registry(Contract.load(ROOT, "hardware"))
    dead = dict(base["listings"][0], url="https://item.jd.com/999.html", retired="2026-10-01")
    reg = {"domain": base["domain"], "source": base["source"],
           "listings": base["listings"] + [dead]}
    p = tmp_path / "reg.yaml"
    p.write_text(yaml.safe_dump(reg, allow_unicode=True), encoding="utf-8")
    old = mbf.REGISTRY
    mbf.REGISTRY = p
    try:
        reg2 = mbf.load_registry(Contract.load(ROOT, "hardware"))
        assert all(not it.get("retired") for it in reg2["listings"])
        assert len(reg2["listings"]) == len(base["listings"])
    finally:
        mbf.REGISTRY = old


# ---------- 端到端（离线缓存 → 文件 → DB）----------

def _cache() -> dict:
    """两个 listing 的假 trend（含同日多点 + 国补备注）。"""
    trend8g = {"spName": "技嘉 魔鹰 5060Ti 8G",
               "datePrice": [[_ts("2026-05-17", 9), 3347.0, "国补前3347"],
                             [_ts("2026-05-17", 21), 2879.1, "国补"],
                             [_ts("2026-05-18", 10), 2969.0, ""],
                             [_ts("2026-10-14", 9), 4751.0, ""]]}
    trend16g = {"spName": "技嘉 小雕 5060Ti 16G",
                "datePrice": [[_ts("2026-05-17", 9), 4349.2, ""],
                              [_ts("2026-10-14", 9), 4999.0, ""]]}
    return {"fetched_at": "2026-10-14T00:00:00+00:00", "listings": [
        {"identity": {"category": "显卡", "series": "RTX 5060 Ti", "spec": "8GB"},
         "url": "https://item.jd.com/100211023968.html",
         "sp_name": "技嘉（GIGABYTE）5060Ti 8G显卡 魔鹰",
         "channel": "自营", "vendor": "技嘉", "anchor": "销量", "trend": trend8g},
        {"identity": {"category": "显卡", "series": "RTX 5060 Ti", "spec": "16GB"},
         "url": "https://item.jd.com/100164458263.html",
         "sp_name": "技嘉（GIGABYTE）5060Ti 16G显卡 小雕",
         "channel": "自营", "vendor": "技嘉", "anchor": "价格", "trend": trend16g},
    ]}


@pytest.fixture()
def tmp_env(tmp_path):
    con = connect(tmp_path / "data" / "sku.db")
    seed_entities(con, SEED)
    con.close()
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(_cache(), ensure_ascii=False), encoding="utf-8")
    return tmp_path, cache


def _db_counts(db: Path) -> tuple[int, int]:
    con = sqlite3.connect(db)
    try:
        n = con.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
        q = con.execute("SELECT COUNT(*) FROM observation_quarantine").fetchone()[0]
        return n, q
    finally:
        con.close()


def test_run_end_to_end_from_cache(tmp_env, capsys):
    tmp, cache = tmp_env
    rc = mbf.main(["run", "--root", str(tmp), "--from-cache", str(cache),
                   "--date-dir", "2026-10-14"])
    assert rc == 0
    out = capsys.readouterr().out
    obs_file = tmp / "artifacts" / "2026-10-14" / "t1_sku_observations.json"
    rows = json.loads(obs_file.read_text(encoding="utf-8"))
    assert len(rows) == 5                     # 8G: 3 天 + 16G: 2 天 = 5
    r8 = next(r for r in rows if r["identity_hint"]["spec"] == "8GB"
              and r["price_date"] == "2026-05-17")
    # 三轨 + 追加维度
    assert r8["attrs_hint"]["source"] == "manmanmai"
    assert r8["attrs_hint"]["condition"] == "全新"
    assert r8["attrs_hint"]["channel"] == "自营"
    assert r8["attrs_hint"]["vendor"] == "技嘉"
    assert r8["attrs_hint"]["anchor"] == "销量"   # 品类采集规则 v1 双锚
    assert r8["attrs_hint"]["promo_event"] == "国补"
    assert r8["low_price"] == 2879.1          # 同日两点取最低
    assert r8["shop"]["url"] == "https://item.jd.com/100211023968.html"
    # 入库：6 行全部落库，0 隔离
    n, q = _db_counts(tmp / "data" / "sku.db")
    assert (n, q) == (5, 0)
    assert "no_entity" not in out or '"no_entity": 0' in out
    con = sqlite3.connect(tmp / "data" / "sku.db")
    try:
        ident = con.execute(
            "SELECT e.identity_json FROM observation o"
            " JOIN entity e ON e.entity_id=o.entity_id"
            " WHERE o.attrs_json LIKE '%manmanmai%' LIMIT 1").fetchone()[0]
        assert "5060 Ti" in ident
        stats = json.loads((tmp / "artifacts" / "2026-10-14" /
                            "backfill_ingest_stats.json").read_text(encoding="utf-8"))
        assert stats["ingested"] == 5 and stats["isolated"] == 0
    finally:
        con.close()


def test_run_is_idempotent(tmp_env):
    tmp, cache = tmp_env
    for _ in range(2):
        rc = mbf.main(["run", "--root", str(tmp), "--from-cache", str(cache),
                       "--date-dir", "2026-10-14"])
        assert rc == 0
    rows = json.loads((tmp / "artifacts" / "2026-10-14" /
                       "t1_sku_observations.json").read_text(encoding="utf-8"))
    assert len(rows) == 5                     # 文件不膨胀
    n, q = _db_counts(tmp / "data" / "sku.db")
    assert (n, q) == (5, 0)                   # DB 不重复


def test_trend_to_series_rejects_empty():
    with pytest.raises(ValueError, match="无价格点"):
        mbf.trend_to_series({"datePrice": []},
                            {"url": "https://item.jd.com/1.html",
                             "sp_name": "x", "channel": "自营"}, "manmanmai")


# ---------- 节流与双锚发现（品类采集规则 v1） ----------

def test_fetch_sleep_uses_random_range(monkeypatch):
    """节流=区间随机（默认 2~7s）：固定间隔是机器指纹（用户定稿）。"""
    calls = []

    monkeypatch.setattr(mbf.random, "uniform",
                        lambda lo, hi: calls.append((lo, hi)) or 0.0)
    monkeypatch.setattr(mbf.time, "sleep", lambda s: None)

    class FakeMmb:
        def trend(self, url):
            return {"datePrice": []}

    reg = {"listings": [{"identity": {}, "url": f"u{i}", "sp_name": f"s{i}",
                         "channel": "自营", "vendor": ""} for i in range(3)]}
    mbf._fetch_dumps(FakeMmb(), reg, (2.0, 7.0))
    assert calls == [(2.0, 7.0), (2.0, 7.0)]   # 3 个 listing 只有间隔处 sleep


def test_match_spec_guards_wrong_series():
    """规格甄别：5060 混不进 5060 Ti 桶（DDG 教训的程序化防线）。"""
    assert mbf._match_spec("技嘉 RTX 5060 Ti 8G 显卡", "RTX 5060 Ti", "8GB")
    assert not mbf._match_spec("英伟达 RTX 5060 8G 涡轮", "RTX 5060 Ti", "8GB")
    assert not mbf._match_spec("华硕 RTX 5060 Ti O16G", "RTX 5060 Ti", "8GB")


def test_match_spec_blocks_suffix_bleed():
    """后缀串台防线：5070Ti 混进 RTX 5070 桶（search 冒烟实测抓到）。"""
    # 5070Ti 标题混不进 RTX 5070 桶
    assert not mbf._match_spec(
        "七彩虹RTX5070Ti 16G火神AD5070 12G战斧", "RTX 5070", "12GB")
    # 真 5070 12G 通过（品牌词直接贴在型号后：5070SHADOW）
    assert mbf._match_spec(
        "微星(MSI)幻影师 GeForce RTX 5070 12G SHADOW", "RTX 5070", "12GB")
    # 5090D 是独立实体：不混进 RTX 5090 桶，但归属自己的桶
    assert not mbf._match_spec("影驰 RTX 5090D 32G", "RTX 5090", "32GB")
    assert mbf._match_spec("影驰 RTX 5090D 32G", "RTX 5090 D", "32GB")


def test_match_spec_blocks_xt_gre_bleed():
    """批量发现实测：9070XT/9070GRE 混进 RX 9070 桶（xt/gre 补防线）。"""
    assert not mbf._match_spec(
        "讯景 AMD RADEON RX 9070 XT 雪狼 16GB", "RX 9070", "16GB")
    assert not mbf._match_spec(
        "讯景 AMD RADEON RX 9070 GRE 雪狼 12GB", "RX 9070", "16GB")
    # 自带后缀的实体归属自身桶
    assert mbf._match_spec(
        "讯景 AMD RADEON RX 9070 XT 雪狼 16GB", "RX 9070 XT", "16GB")
    assert mbf._match_spec(
        "AMD RADEON RX 9070 GRE 雪狼 12GB", "RX 9070 GRE", "12GB")


def test_is_host_words():
    """整机词防线：联想工作站混进 RTX 5090 桶（批量发现实测抓到）。"""
    assert mbf._is_host("联想 GeekPro 游戏台式电脑主机 RTX 5090")
    assert mbf._is_host("七彩虹 iGame 工作站 RTX 5090D 建模")
    assert mbf._is_host("朴赛英特尔i9 14900K 深度学习双卡GPU服务器 4090/5090")
    assert not mbf._is_host("七彩虹 iGame GeForce RTX 5090 D V2 火神")
    assert not mbf._is_host("技嘉魔鹰 GeForce RTX 5060 Ti 8G")
    # 「台式机显卡」是单卡堆砌写法——讯景雪狼 9070（销量 5000）曾被误杀
    assert not mbf._is_host(
        "讯景(XFX)AMD RADEON RX 9070 雪狼 16GB 白 电竞游戏设计DeepSeek"
        "人工智能AI本地部署台式机显卡")
    assert mbf._is_host("华硕(ASUS)AMD锐龙9图形设计电脑 ET500A 9950X")


def test_match_spec_rejects_multimodel_stuffing():
    """多型号堆砌防线：v5 批量实测抓到的两类混装/堆砌页。"""
    # 斜杠型号列表（4060Ti 16G 桶搜到的技嘉混装页）
    assert not mbf._match_spec(
        "技嘉RTX4060/4070/4070Super/4070TI SUPER/4060Ti 16G 显卡",
        "RTX 4060 Ti", "16GB")
    # 无斜杠但多前缀型号（4070 Super 桶搜到的堆砌页）
    assert not mbf._match_spec(
        "英伟达RTX4090显卡3090涡轮显卡4080S 32G显卡英伟达RTX5090",
        "RTX 4070 Super", "12GB")
    # 正常单卡标题不受影响
    assert mbf._match_spec(
        "华硕（ASUS）白色 DUAL GeForce RTX 4060 Ti O8G 版", "RTX 4060 Ti", "8GB")
    assert mbf._match_spec(
        "微星（MSI）魔龙 GeForce RTX 5060 Ti 16G GAMING", "RTX 5060 Ti", "16GB")


def test_norm_token_unifies_kit_marks():
    """内存套装容量标记归一：×/*/X 三种写法都能对上 spec 里的 ×。
    2026-10-15 内存品类缺口（金百达「32GB(16GBX2)」 vs spec「32GB(16G×2)」）。"""
    spec = "32GB(16G×2)"
    assert mbf._match_spec("金百达 32GB(16GBX2) DDR5 6000 内存条", "DDR5 6000", spec)
    assert mbf._match_spec("金百达 32GB(16GB*2) DDR5 6000 内存条", "DDR5 6000", spec)
    assert mbf._match_spec("金百达 32GB(16GB×2) DDR5 6000 内存条", "DDR5 6000", spec)
    # GPU 侧不回归：装饰性 * 不破坏正常匹配
    assert mbf._match_spec("*限时特惠* 微星 RTX 5070 12G 显卡", "RTX 5070", "12GB")


def test_registry_accepts_jingfen_key(tmp_path_factory):
    """锚 URL 形态收敛：jingfen 采集键 + gourl 溯源都能过注册表校验
    （搜索结果没有京东数字 SKU，v2 批量实测抓到）。"""
    import yaml as _y
    base = mbf.load_registry(Contract.load(ROOT, "hardware"))
    entry = dict(base["listings"][0])
    entry["url"] = "https://jingfen.jd.com/detail/jwQitWLyb63XqHbpMc6lX"
    entry["shop_url"] = ("https://apph5.manmanbuy.com/redirectjd.aspx?"
                         "skuid=3ERAptAX")
    entry["sp_name"] = entry["sp_name"] + "（jingfen键测试）"
    reg = {"domain": base["domain"], "source": base["source"],
           "listings": base["listings"] + [entry]}
    p = tmp_path_factory.mktemp("reg") / "reg.yaml"
    p.write_text(_y.safe_dump(reg, allow_unicode=True), encoding="utf-8")
    old = mbf.REGISTRY
    mbf.REGISTRY = p
    try:
        reg2 = mbf.load_registry(Contract.load(ROOT, "hardware"))
        assert any("jingfen" in it["url"] for it in reg2["listings"])
    finally:
        mbf.REGISTRY = old


def test_discover_cheap_skips_host_filter_when_match_false():
    """品类路径（match=False）不做整机词过滤：台式机内存是正常标题。

    2026-10-16 预扫实测：_is_host 把 DDR5 5600/6400 全部候选误杀归零。
    discover_cheap 走 trend 试采（4030 窗口期不可用），这里只断言过滤
    语义——用 monkeypatched 的 search + trend 构造零网络测试。
    """
    import unittest.mock as mock

    rows = [
        {"title": "金百达48GB(24GBX2)套装 DDR5 5600 台式机内存条",
         "price": "4099", "sales": "100000", "siteid": "1",
         "prourl": "https://jingfen.jd.com/detail/abc_3123456.html"},
        {"title": "联想拯救者原装 16G DDR5 5600 笔记本内存条",
         "price": "1888", "sales": "20001", "siteid": "1",
         "prourl": "https://jingfen.jd.com/detail/def_3123457.html"},
    ]
    with mock.patch.object(mbf.Mmb, "search", return_value=rows), \
         mock.patch.object(mbf.Mmb, "trend",
                           return_value={"datePrice": [[1, 2]] * 60,
                                         "spName": "x", "currentPrice": 1}), \
         mock.patch.object(mbf.Mmb, "load_session", return_value=True), \
         mock.patch("time.sleep", return_value=None):
        # 品类路径：台式机内存留，笔记本被 reject 拦
        r = mbf.discover_cheap("DDR5 5600", "16GBx2", "DDR5 5600",
                               match=False, reject=r"笔记本|服务器",
                               sleep_range=(0, 0))
        assert len(r["candidates"]) == 1
        assert "金百达" in r["candidates"][0]["title"]
        # 显卡路径：同标题被 _is_host 拦（回归旧行为）
        r2 = mbf.discover_cheap("DDR5 5600", "16GBx2", "DDR5 5600",
                                match=True, sleep_range=(0, 0))
        assert r2["candidates"] == []


def test_fail_label_distinguishes_4030_from_session_expired():
    """4030 打印窗口限流（别 login），402/未登录打印会话失效。

    2026-10-16 早 06:30 定时回填撞 4030，旧文案一律「会话失效」误导重登。
    """
    from tools.manmanbuy_client import SessionExpired
    assert "停止一切慢慢买流量" in mbf._fail_label(SessionExpired("...'4030'"))
    assert "联系客服" in mbf._fail_label(SessionExpired("...'4030'"))
    assert "会话失效" in mbf._fail_label(SessionExpired("未登录"))
    assert "会话失效" in mbf._fail_label(SessionExpired("402"))

