"""trend_calc：基础趋势与控制变量对比的确定性计算器。

设计：docs/design/sku-timeseries-db-v3.3.md §4 D5、§9 指标层。

P1 阶段只实现 L0（d1_pct / slope_7 / vol_7 / min_30 / pct_from_low / label）
+ contrast（控制变量对比，固定其余维度变一个维度）。L1/L2 与
应用契约在 P1.5/P2 阶段独立写（每指标一个 handler）。

输出：artifacts/{date}/trend_snapshot.json，含 per-entity L0 字段 +
contrasts 段。t4 任务读它写趋势报告，模型只解读不改数（设计铁律）。
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.sku_db import connect  # noqa: E402
from tools.stab_risk import stab_risk  # noqa: E402


# --------------------------------------------------------------------------- L0

def _aggregate_series(con: sqlite3.Connection, entity_id: int,
                     date: str) -> list[tuple[str, float]]:
    """取某实体当日的（entity_id, 自采双平台, condition=全新）聚合序列点。

    自采 = 慢买（manmanmai）+ 购物党顶班（gwdang）两条自采渠道都算——
    2026-10-17 修复：CPU/内存全 gwdang 行导致看板 0 点。契约
    aggregate_scope.default_filter 同步（daily MIN 跨平台取低）。
    序列点 = 该日 default_filter 内观测的 low_price min。
    """
    rows = con.execute(
        """
        SELECT obs_date, MIN(low_price)
          FROM observation
         WHERE entity_id = ?
           AND json_extract(attrs_json, '$.source') IN
               ('自采', 'manmanmai', 'gwdang')
           AND json_extract(attrs_json, '$.condition') = '全新'
           AND obs_date <= ?
         GROUP BY obs_date
         ORDER BY obs_date
        """,
        (entity_id, date)).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def _mmb_sales_series(con: sqlite3.Connection, entity_id: int,
                      date: str) -> list[tuple[str, float]]:
    """manmanmai 销量锚主线序列（R35 回填轨，2026-10-15 L1 解锁）。

    销量锚 = search_top 销量第一 listing 的历史价（市场主流成交价），
    attrs: source=manmanmai + anchor=销量。自采轨清脏后近空，指标层的
    真实数据源就是这条轨。"""
    rows = con.execute(
        """
        SELECT obs_date, MIN(low_price)
          FROM observation
         WHERE entity_id = ?
           AND json_extract(attrs_json, '$.source') = 'manmanmai'
           AND json_extract(attrs_json, '$.anchor') = '销量'
           AND obs_date <= ?
         GROUP BY obs_date
         ORDER BY obs_date
        """,
        (entity_id, date)).fetchall()
    return [(r[0], float(r[1])) for r in rows]


def l0_metrics(series: list[tuple[str, float]]) -> dict | None:
    """单序列 L0 指标。序列不足 5 期返回 None（sufficiency 不达标）。"""
    if len(series) < 5:
        return None
    prices = [p for _, p in series]
    last_date, last_price = series[-1]
    prev_price = series[-2][1] if len(series) >= 2 else last_price

    d1_pct = (last_price - prev_price) / prev_price * 100 if prev_price else 0.0

    n = min(7, len(prices))
    recent7 = prices[-n:]
    avg7 = sum(recent7) / n
    slope7_pct_per_day = _slope_pct_per_day([s for s, _ in series[-7:]], recent7)
    vol7 = _stddev_pct(recent7)

    n30 = min(30, len(prices))
    recent30 = prices[-n30:]
    min30 = min(recent30)
    pct_from_low = (last_price - min30) / min30 * 100 if min30 else 0.0

    label = _label(slope7_pct_per_day, vol7)

    return {
        "d1_pct": round(d1_pct, 2),
        "slope_7_pct_per_day": round(slope7_pct_per_day, 3),
        "vol_7_pct": round(vol7, 2),
        "avg_7": round(avg7, 2),
        "min_30": round(min30, 2),
        "pct_from_30d_low": round(pct_from_low, 2),
        "last_price": last_price,
        "last_date": last_date,
        "label": label,
        "sufficiency": f"OK({len(series)})",
    }


def _slope_pct_per_day(dates: list[str], prices: list[float]) -> float:
    """最小二乘斜率（%/期）。简化：把日期换成「期序号」线性拟合。"""
    if len(dates) < 2:
        return 0.0
    x = list(range(len(dates)))
    y = prices
    n = len(x)
    mean_x, mean_y = sum(x) / n, sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den = sum((xi - mean_x) ** 2 for xi in x)
    if den == 0:
        return 0.0
    slope_per_period = num / den                  # 元/期
    avg = mean_y
    if avg == 0:
        return 0.0
    return slope_per_period / avg * 100            # %/期


def _stddev_pct(prices: list[float]) -> float:
    """变异系数 = stdev / mean × 100。"""
    if not prices:
        return 0.0
    mean = sum(prices) / len(prices)
    if mean == 0:
        return 0.0
    var = sum((p - mean) ** 2 for p in prices) / len(prices)
    return math.sqrt(var) / mean * 100


def _label(slope_pct: float, vol_pct: float) -> str:
    if slope_pct <= -1.0:
        return "fast_drop"
    if slope_pct < -0.2:
        return "drop"
    if abs(slope_pct) < 0.2 and vol_pct < 2.0:
        return "stable"
    if slope_pct < 1.0:
        return "rise"
    return "fast_rise"


# --------------------------------------------------------------- contrast（对比）

def contrast(con: sqlite3.Connection, entity_id: int, date: str,
             vary_attr: str, fixed_attrs: dict) -> dict | None:
    """控制变量对比：固定其余维度，变一个维度（如「同型号不同 vendor」）。

    对每个 vary_attr 的取值（契约里 attrs[vary_attr].values）取一条序列，
    算 last_price + slope_7 + vol_7 + label，输出 spread_pct。
    """
    import yaml
    contract_p = ROOT / "config" / "contracts" / "hardware.yaml"
    contract = yaml.safe_load(contract_p.read_text(encoding="utf-8"))
    domain = contract.get("domain", "hardware")
    vary_values = contract["attrs"].get(vary_attr, {}).get("values") or []
    groups = []
    for v in vary_values:
        attrs_filter = dict(fixed_attrs)
        attrs_filter[vary_attr] = v
        rows = _series_with_attrs(con, entity_id, date, attrs_filter)
        if not rows:
            continue
        m = l0_metrics(rows)
        if m is None:
            continue
        groups.append({"when": {vary_attr: v},
                       "last_price": m["last_price"],
                       "slope_7_pct_per_day": m["slope_7_pct_per_day"],
                       "vol_7_pct": m["vol_7_pct"],
                       "label": m["label"]})
    if not groups:
        return None
    prices = [g["last_price"] for g in groups]
    spread_pct = (max(prices) - min(prices)) / min(prices) * 100 if min(prices) else 0
    return {"fixed": fixed_attrs, "vary": vary_attr,
            "groups": groups, "spread_pct": round(spread_pct, 2)}


def _series_with_attrs(con: sqlite3.Connection, entity_id: int, date: str,
                       attrs_filter: dict) -> list[tuple[str, float]]:
    """按 attrs 过滤后取聚合序列（min low_price per obs_date）。

    过滤值支持标量（=）和列表（IN）——自采双平台口径用 IN。
    """
    clauses, params = [], []
    for k, v in attrs_filter.items():
        if isinstance(v, (list, tuple, set)):
            vals = list(v)
            clauses.append("json_extract(attrs_json, '$.%s') IN (%s)"
                           % (k, ",".join("?" * len(vals))))
            params.extend(vals)
        else:
            clauses.append(f"json_extract(attrs_json, '$.{k}') = ?")
            params.append(v)
    json_filters = " AND ".join(clauses)
    rows = con.execute(
        f"""
        SELECT obs_date, MIN(low_price) FROM observation
         WHERE entity_id = ? AND obs_date <= ? AND {json_filters}
         GROUP BY obs_date ORDER BY obs_date
        """, [entity_id, date] + params).fetchall()
    return [(r[0], float(r[1])) for r in rows]


# --------------------------------------------------------------- snapshot

def compute_snapshot(con: sqlite3.Connection, date: str) -> dict:
    """生成 trend_snapshot.json：每个实体的 L0 字段 + 选定 contrasts。

    2026-10-15 起遍历全部实体：自采轨（契约默认过滤）照旧出 L0；
    manmanmai 销量锚回填轨另出 `mmb` 块——L0 + stab_risk（≥60 期才出，
    Q10-A 门槛）。stab_action 阈值按 stab_risk 模块定义：中位回撤>10%
    且斜率未走平（slope<-0.2，与 _label 的 stable 边界一致）→ wait。
    contrast 范围：vary=vendor、condition=全新、source=自采、shop_platform
    自营——以「同型号不同 AIC 品牌走量厂商」为代表对比。
    """
    snap = {"date": date, "domain": "hardware",
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "entities": [], "contrasts": []}
    for row in con.execute(
            "SELECT entity_id, display_name, perf_anchor FROM entity "
            "ORDER BY entity_id").fetchall():
        eid, name, anchor = row["entity_id"], row["display_name"], row["perf_anchor"]
        series = _aggregate_series(con, eid, date)
        entry = {"entity_id": eid, "display_name": name,
                 "perf_anchor": anchor,
                 "n_periods": len(series),
                 "metrics": l0_metrics(series)}
        mmb = _mmb_sales_series(con, eid, date)
        if mmb:
            mm = {"n_periods": len(mmb), "metrics": l0_metrics(mmb)}
            sr = stab_risk([p for _, p in mmb])
            if sr:
                mm["stab_risk"] = sr
                slope = (mm["metrics"] or {}).get("slope_7_pct_per_day") or 0.0
                mm["stab_action"] = ("wait" if sr["median_pct"] > 10
                                     and slope < -0.2 else "act_now")
            entry["mmb"] = mm
        snap["entities"].append(entry)
    # contrast 示例：基底实体的 vendor 对比
    if snap["entities"]:
        base_eid = snap["entities"][0]["entity_id"]
        c = contrast(con, base_eid, date,
                     vary_attr="vendor",
                     fixed_attrs={"condition": "全新", "source": "自采"})
        if c:
            snap["contrasts"].append(c)
    return snap


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="trend_calc")
    ap.add_argument("--date", required=True)
    ap.add_argument("--root", default=None)
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else ROOT
    con = connect(root / "data" / "sku.db")
    try:
        snap = compute_snapshot(con, args.date)
        out = root / "artifacts" / args.date / "trend_snapshot.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        # 校验：模型读到的时间戳就是今天（设计 D6 + v3.3 B2 验收：t4 拒绝旧快照）
        print(f"snapshot date={snap['date']} entities={len(snap['entities'])} "
              f"contrasts={len(snap['contrasts'])} -> {out}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())