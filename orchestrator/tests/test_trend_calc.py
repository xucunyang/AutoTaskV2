"""trend_calc 计算器单元测试（P1 门禁）。

锁住：L0 各项公式、sufficiency 门槛、label 分类、contrast 控制变量逻辑。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sku_db import connect, seed_entities  # noqa: E402
from tools.trend_calc import (compute_snapshot, contrast, l0_metrics,  # noqa: E402
                              _aggregate_series, _label, _slope_pct_per_day,
                              _stddev_pct)

SEED = ROOT / "config/contracts/hardware_entities.yaml"


@pytest.fixture()
def db(tmp_path):
    con = connect(tmp_path / "sku.db")
    seed_entities(con, SEED)
    yield con
    con.close()


def _seed_obs(con, eid, rows, *, condition="全新", source="自采",
              shop_platform="京东", shop_name="七彩虹京东自营旗舰店",
              **extra_attrs):
    """rows: list of (date, low_price). 把每日一条写进 observation。"""
    import json
    for d, price in rows:
        con.execute(
            "INSERT INTO observation(entity_id, obs_date, collected_on,"
            " low_price, raw_price, metric, price_unit, attrs_json,"
            " shop_name, shop_platform, shop_url, coupons, in_stock,"
            " source_url, source_tier, raw_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, d, d, price, price, "price", "CNY",
             json.dumps({"condition": condition, "source": source,
                          "vendor": "七彩虹", "channel": "国行单卡",
                          **extra_attrs}, ensure_ascii=False),
             shop_name, shop_platform, "https://x", "[]", 1,
             "https://x", "B", "{}", "now"))


def test_l0_sufficiency_requires_five_periods(db):
    eid = 1   # RTX 5090
    rows = [(f"2026-10-{d:02d}", 3000.0) for d in range(1, 4)]   # 3 期
    _seed_obs(db, eid, rows)
    assert l0_metrics([(d, p) for d, p in rows]) is None


def test_l0_basic_metrics(db):
    """价格从 3000 稳步下降 5 周：d1_pct<0、slope_7<0、label=fast_drop（斜率≤-1%/期/天）。"""
    rows = [(f"2026-10-{d:02d}", 3000 - d * 50) for d in range(1, 8)]   # 7 期
    m = l0_metrics(rows)
    assert m["last_date"] == "2026-10-07"
    assert m["last_price"] == 3000 - 7 * 50
    assert m["d1_pct"] < 0
    assert m["slope_7_pct_per_day"] < 0
    assert m["min_30"] == min(p for _, p in rows)
    assert m["pct_from_30d_low"] == 0.0           # 最后一日 = 30 日最低


def test_l0_label_thresholds():
    assert _label(-2.0, 1.0) == "fast_drop"     # ≤-1%/期
    assert _label(-0.5, 1.0) == "drop"
    assert _label(0.0, 1.0) == "stable"          # |slope|<0.2 且 vol<2
    assert _label(0.1, 5.0) == "rise"           # vol≥2 失效 → 不算 stable
    assert _label(0.5, 1.0) == "rise"
    assert _label(2.0, 1.0) == "fast_rise"


def test_contrast_fixed_vs_vary(db):
    """同型号不同 vendor：七彩虹价低、微星价高，spread_pct 应反映价差。"""
    eid = 1
    base = "2026-10-07"
    _seed_obs(db, eid, [(f"2026-10-{d:02d}", 2900) for d in range(1, 8)])
    # 再写一条同样日期但 vendor=微星、price=3050（更高的轨道）
    import json
    for d in range(1, 8):
        db.execute(
            "INSERT INTO observation(entity_id, obs_date, collected_on,"
            " low_price, raw_price, metric, price_unit, attrs_json,"
            " shop_name, shop_platform, shop_url, coupons, in_stock,"
            " source_url, source_tier, raw_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, f"2026-10-{d:02d}", f"2026-10-{d:02d}", 3050, 3050,
             "price", "CNY",
             json.dumps({"condition": "全新", "source": "自采",
                          "vendor": "微星", "channel": "国行单卡"},
                         ensure_ascii=False),
             "微星旗舰店", "京东", "https://x", "[]", 1,
             "https://x", "B", "{}", "now"))

    c = contrast(db, eid, base, vary_attr="vendor",
                 fixed_attrs={"condition": "全新", "source": "自采"})
    assert c is not None
    assert c["vary"] == "vendor"
    prices = {g["when"]["vendor"]: g["last_price"] for g in c["groups"]}
    assert prices.get("七彩虹") == 2900
    assert prices.get("微星") == 3050
    # spread_pct = (3050-2900)/2900 × 100 ≈ 5.17%
    assert 4.9 < c["spread_pct"] < 5.3


def test_aggregate_filters_source_and_condition(db):
    """聚合序列点只取 source=自采 + condition=全新（契约 default_filter）。"""
    eid = 1
    _seed_obs(db, eid, [(f"2026-10-0{i}", 3000) for i in range(1, 6)])
    # 加一条二手观测同日，但价更低 99%：应被 default_filter 排除
    import json
    for d in range(1, 6):
        db.execute(
            "INSERT INTO observation(entity_id, obs_date, collected_on,"
            " low_price, raw_price, metric, price_unit, attrs_json,"
            " shop_name, shop_platform, shop_url, coupons, in_stock,"
            " source_url, source_tier, raw_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, f"2026-10-0{d}", f"2026-10-0{d}", 1.0, 1.0,
             "price", "CNY",
             json.dumps({"condition": "二手", "source": "自采",
                          "vendor": "闲鱼", "channel": "二手"},
                         ensure_ascii=False),
             "闲鱼个人", "xianyu_app", "https://x", "[]", 1,
             "https://x", "C", "{}", "now"))
    series = _aggregate_series(db, eid, "2026-10-05")
    assert all(p == 3000 for _, p in series), \
        f"二手价 1 应被 default_filter 排除，实际序列点={[p for _, p in series]}"


def test_slope_percent_per_day(db):
    """价格每天降 1 元，5 天：slope_per_period=-1 / mean≈-1.65% 每日。"""
    series = [(f"2026-10-0{i}", 60 - i) for i in range(1, 6)]   # 60,59,58,57,56
    s = _slope_pct_per_day([s for s, _ in series], [p for _, p in series])
    assert -2.0 < s < -1.5                     # 大致每天降 ~1.7%


def test_stddev_pct_is_zero_for_constant():
    assert _stddev_pct([100, 100, 100]) == 0.0
    assert _stddev_pct([100, 110, 90]) > 5.0


def test_snapshot_includes_mmb_sales_track_with_stab_risk(db):
    """L1 解锁（2026-10-15）：compute_snapshot 遍历全实体，manmanmai 销量锚
    轨出 mmb 块（L0 + stab_risk，≥60 期）；无回填数据的实体无 mmb 键。"""
    # 实体 1 挂 70 天 manmanmai 销量锚序列（前 40 天缓跌 3000→2600，后 30 天横盘）
    rows = [(f"2026-08-{d:02d}", 3000 - d * 10) for d in range(1, 11)]
    rows += [(f"2026-09-{d:02d}", 2900 - d * 10) for d in range(1, 31)]
    rows += [(f"2026-10-{d:02d}", 2600.0) for d in range(1, 31)]
    assert len(rows) == 70
    _seed_obs(db, 1, rows, source="manmanmai", anchor="销量")
    # 干扰项：同实体 manmanmai 价格锚序列（不同店名满足唯一约束；
    # 不该进 mmb 销量主线）
    _seed_obs(db, 1, rows[:10], source="manmanmai", anchor="价格",
              shop_name="其他店")
    # 自采轨一条（自采 L0 照旧独立计算，不受 mmb 影响；换店名防撞唯一约束）
    _seed_obs(db, 1, rows[:5], shop_name="自采店")

    snap = compute_snapshot(db, "2026-10-31")
    e1 = next(e for e in snap["entities"] if e["entity_id"] == 1)
    assert "mmb" in e1
    assert e1["mmb"]["n_periods"] == 70          # 只有销量锚进主线
    assert e1["mmb"]["metrics"] is not None
    assert e1["mmb"]["metrics"]["last_price"] == 2600.0
    sr = e1["mmb"]["stab_risk"]
    assert sr is not None and sr["median_pct"] >= 0
    assert e1["mmb"]["stab_action"] in ("wait", "act_now")
    # 下跌后横盘：高点的买入日有回撤，斜率走平 → act_now（阈值见模块注释）
    assert e1["mmb"]["stab_action"] == "act_now"
    # 无 manmanmai 数据的实体：无 mmb 键，不报错
    e_other = next(e for e in snap["entities"] if e["entity_id"] != 1)
    assert "mmb" not in e_other