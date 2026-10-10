"""sku_backfill 转换器测试（P0 回填通道门禁）。

锁住：平铺序列 → observations.json 行的字段完整性（source/condition/
channel 三轨必带——缺了入库器会整行隔离，回填等于白做）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sku_backfill import convert  # noqa: E402


def test_convert_shapes_flat_series():
    rows = convert(
        {"dates": ["2026-01-02", "2026-01-03"], "prices": [3199.0, 3099.0],
         "shop": {"name": "京东自营", "platform": "京东"},
         "source_url": "https://gwdang.example/x"},
        entity_hint={"category": "显卡", "series": "RTX 5060 Ti",
                     "spec": "8GB"},
        source="gwdang", channel="国行单卡")
    assert len(rows) == 2
    r = rows[0]
    # 三轨字段必带（aggregate_scope 分轨维度，缺了入库器整行隔离）
    assert r["attrs_hint"]["condition"] == "全新"
    assert r["attrs_hint"]["channel"] == "国行单卡"
    assert r["attrs_hint"]["source"] == "gwdang"
    assert r["identity_hint"] == {"category": "显卡", "series": "RTX 5060 Ti",
                                  "spec": "8GB"}
    assert r["low_price"] == 3199.0 and r["price_date"] == "2026-01-02"
    assert r["shop"]["platform"] == "京东"


def test_convert_rejects_length_mismatch():
    with pytest.raises(ValueError, match="长度不一致"):
        convert({"dates": ["2026-01-02"], "prices": [1.0, 2.0]},
                entity_hint={"category": "显卡", "series": "X", "spec": ""},
                source="gwdang", channel="国行单卡")


def test_convert_appends_to_existing_file(tmp_path, monkeypatch):
    """同一天回填多个实体：追加合并，不互相覆盖。"""
    import tools.sku_backfill as bf
    monkeypatch.setattr(bf, "ROOT", tmp_path)
    out = tmp_path / "artifacts" / "2026-01-01" / "t1_sku_observations.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps([{"old": True}]), encoding="utf-8")
    (tmp_path / "raw.json").write_text(json.dumps(
        {"dates": ["2026-01-02", "2026-01-03"], "prices": [3199.0, 3099.0]}),
        encoding="utf-8")
    rc = bf.main(["--input", str(tmp_path / "raw.json"), "--series", "RTX 5060 Ti",
                  "--spec", "8GB", "--source", "gwdang",
                  "--date-dir", "2026-01-01"])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 3          # 1 旧 + 2 新
    assert data[0] == {"old": True}
    assert data[1]["identity_hint"]["series"] == "RTX 5060 Ti"
