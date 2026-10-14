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
        assert it["url"].startswith("https://item.jd.com/")


@pytest.mark.parametrize("patch", [
    {"channel": "水货"},                       # 不在枚举
    {"url": "https://item.taobao.com/1.htm"},  # 非京东详情页
    {"vendor": "英伟达"},                       # vendor 枚举里没有（NVIDIA 官方不卖卡）
    {"identity": {"category": "显卡", "series": "RTX 5060 Ti"}},  # 缺 spec
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
         "channel": "自营", "vendor": "技嘉", "trend": trend8g},
        {"identity": {"category": "显卡", "series": "RTX 5060 Ti", "spec": "16GB"},
         "url": "https://item.jd.com/100164458263.html",
         "sp_name": "技嘉（GIGABYTE）5060Ti 16G显卡 小雕",
         "channel": "自营", "vendor": "技嘉", "trend": trend16g},
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

