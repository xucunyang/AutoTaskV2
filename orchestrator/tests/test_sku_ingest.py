"""sku_ingest 入库器单元测试（P0 门禁）。

锁住的不变量（每条对应设计文档的一个拍板/质检修正）：
1. 契约裁决：identity 键不匹配/未知 attr 键/required 缺失 → 隔离区
2. 幂等：同文件重跑不产生重复观测（D7）
3. 归一化：渠道后缀剥离、ti/GB 归一（Q6 前置：序列身份稳定）
4. 价格日不晚于采集日（Q6）；坏价进隔离
5. 别名兜底：『5060Ti 8G』命中 RTX 5060 Ti 8GB 实体
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sku_db import connect, seed_entities  # noqa: E402
from tools.sku_ingest import (Contract, ingest_file,  # noqa: E402
                              normalize_name, validate_row)

SEED = ROOT / "config" / "contracts" / "hardware_entities.yaml"
CONTRACT = Contract.load(ROOT, "hardware")


@pytest.fixture()
def db(tmp_path):
    con = connect(tmp_path / "sku.db")
    seed_entities(con, SEED)
    yield con
    con.close()


def _row(**over):
    base = {
        "identity_hint": {"category": "显卡", "series": "RTX 5060 Ti",
                          "spec": "8GB"},
        "attrs_hint": {"vendor": "七彩虹", "condition": "全新",
                       "channel": "国行单卡", "source": "自采"},
        "low_price": 2899.0, "raw_price": 3199.0,
        "price_date": "2026-10-10",
        "shop": {"name": "七彩虹京东自营旗舰店", "platform": "京东",
                 "url": "https://example.com"},
        "coupons": [{"name": "满3000减200", "amount": 200}],
        "in_stock": True, "source_url": "https://s.example/1",
        "source_tier": "B",
    }
    base.update(over)
    return base


def _write_obs(tmp_path, rows, date="2026-10-10"):
    p = tmp_path / "artifacts" / date
    p.mkdir(parents=True, exist_ok=True)
    f = p / "t1_sku_observations.json"
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return f


# ---------------------------------------------------------------- 归一化

def test_normalize_strips_channel_suffix_and_unifies_units():
    assert normalize_name("RTX 5060ti 8G(京东全新)") == "RTX 5060 Ti 8GB"
    assert normalize_name("GeForce RTX 5090（美行新卡）") == "GeForce RTX 5090"
    assert normalize_name("5060TI 8g") == "5060 Ti 8GB"


# ---------------------------------------------------------------- 校验

def test_validate_rejects_unknown_attr_key():
    _, reason = validate_row(_row(attrs_hint={
        "condition": "全新", "channel": "国行单卡", "source": "自采",
        "color": "黑色"}), CONTRACT, "2026-10-10")
    assert reason and reason.startswith("unknown_attr_key")


def test_validate_requires_aggregate_scope_dims():
    """condition/source 是 aggregate_scope 分轨维度（M1/M2），缺失即隔离。"""
    _, reason = validate_row(_row(attrs_hint={"condition": "全新"}),
                             CONTRACT, "2026-10-10")
    assert reason == "missing_required_attr:channel"


def test_validate_rejects_price_date_after_collection():
    _, reason = validate_row(_row(price_date="2026-10-11"),
                             CONTRACT, "2026-10-10")
    assert reason.startswith("date_after_collection")


def test_validate_rejects_bad_price():
    _, reason = validate_row(_row(low_price=-5), CONTRACT, "2026-10-10")
    assert reason.startswith("bad_price")


def test_validate_rejects_attr_value_drift():
    _, reason = validate_row(_row(attrs_hint={
        "condition": "全新新", "channel": "国行单卡", "source": "自采"}),
        CONTRACT, "2026-10-10")
    assert reason.startswith("attr_value_not_allowed")


# ---------------------------------------------------------------- 入库

def test_ingest_happy_path_and_idempotent(db, tmp_path):
    f = _write_obs(tmp_path, [_row()])
    s1 = ingest_file(db, f, CONTRACT, "2026-10-10")
    assert s1["ok"] == 1
    n1 = db.execute("SELECT count(*) FROM observation").fetchone()[0]
    # 同文件重跑：upsert，不重复
    s2 = ingest_file(db, f, CONTRACT, "2026-10-10")
    n2 = db.execute("SELECT count(*) FROM observation").fetchone()[0]
    assert n2 == n1 == 1
    assert s2["ok"] + s2["updated"] == 1


def test_ingest_unknown_entity_goes_to_quarantine(db, tmp_path):
    f = _write_obs(tmp_path, [_row(identity_hint={
        "category": "显卡", "series": "RTX 6090", "spec": "32GB"})])
    s = ingest_file(db, f, CONTRACT, "2026-10-10")
    assert s["no_entity"] == 1
    q = db.execute("SELECT reason FROM observation_quarantine").fetchone()
    assert q["reason"] == "no_entity"
    assert db.execute("SELECT count(*) FROM observation").fetchone()[0] == 0


def test_ingest_alias_matches_entity(db, tmp_path):
    """『5060Ti 8G』归一化后走别名兜底命中 5060 Ti 8GB。"""
    f = _write_obs(tmp_path, [_row(identity_hint={
        "category": "显卡", "series": "5060Ti 8G", "spec": "8GB"})])
    s = ingest_file(db, f, CONTRACT, "2026-10-10")
    assert s["ok"] == 1, s


def test_ingest_bad_row_isolated_with_reason(db, tmp_path):
    f = _write_obs(tmp_path, [_row(low_price="abc")])
    s = ingest_file(db, f, CONTRACT, "2026-10-10")
    assert s["bad_row"] == 1
    q = db.execute("SELECT reason FROM observation_quarantine").fetchone()
    assert q["reason"].startswith("bad_price")


def test_ingest_dry_run_leaves_no_trace(db, tmp_path):
    f = _write_obs(tmp_path, [_row()])
    ingest_file(db, f, CONTRACT, "2026-10-10", dry_run=True)
    assert db.execute("SELECT count(*) FROM observation").fetchone()[0] == 0
    assert db.execute(
        "SELECT count(*) FROM observation_quarantine").fetchone()[0] == 0
