"""sku_backfill：外部历史数据 → observations.json → 复用入库器。

设计 v3.3 §12「单一入口铁律」：无论网页爬虫、手机采集会话还是人工
整理，产出一律写成 observations.json 格式、经 tools.sku_ingest 入库
（source 轨标明渠道）。本模块提供的是**转换层**——把各渠道的原始
形态（目前先支持「日期+价格」平铺序列，爬虫/手机会话的产出先落成
这种中间格式）转成进料口格式。

用法：
    python -m tools.sku_backfill --input raw.json --entity 23 \\
        --source gwdang --channel 国行单卡 --out 2024-01-01

raw.json 形态（平铺序列，任意渠道都好产出）：
    {"dates": ["2026-01-02", ...], "prices": [3199.0, ...],
     "shop_name": "京东自营", "shop_platform": "京东"}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def convert(raw: dict, *, entity_hint: dict, source: str, channel: str,
            condition: str = "全新",
            extra_attrs: dict | None = None) -> list[dict]:
    """平铺序列 → observations.json 行。不查库——entity 匹配交给入库器
    （单一职责：转换器不懂实体，入库器不懂渠道）。

    extra_attrs：可选追加维度（如 promo_event），**不覆盖**三轨字段
    （condition/channel/source 是 aggregate_scope 分轨命脉，转换器
    保底不让调用方冲掉）。枚举合法性由调用方（渠道适配器）对照契约
    校验后传入。
    """
    dates, prices = raw.get("dates") or [], raw.get("prices") or []
    if len(dates) != len(prices):
        raise ValueError(f"dates/prices 长度不一致: {len(dates)}≠{len(prices)}")
    shop = raw.get("shop") or {}
    rows = []
    for d, p in zip(dates, prices):
        rows.append({
            "identity_hint": entity_hint,
            "attrs_hint": {**(extra_attrs or {}),
                           "condition": condition, "channel": channel,
                           "source": source},
            "low_price": float(p), "raw_price": float(p),
            "price_date": str(d),
            "shop": {"name": shop.get("name") or source,
                     "platform": shop.get("platform") or source,
                     # Q6/R21：shop_url 必须是商品详情页（京东 item URL）——
                     # 溯源弹窗点得回去。2026-10-14 慢慢买接入补上。
                     "url": shop.get("url") or ""},
            "coupons": [], "in_stock": True,
            "source_url": raw.get("source_url") or "",
            "source_tier": "C",
        })
    return rows


def append_rows(out: Path, rows: list[dict]) -> int:
    """追加合并进采集日目录的 observations.json（同日多实体/多渠道
    不互相覆盖）。返回合并后总行数。"""
    existing = json.loads(out.read_text(encoding="utf-8")) if out.exists() else []
    existing.extend(rows)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(existing, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    return len(existing)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="sku_backfill")
    ap.add_argument("--input", required=True, help="raw.json 路径")
    ap.add_argument("--series", required=True, help="规范型号，如 'RTX 5060 Ti'")
    ap.add_argument("--spec", default="", help="规格，如 '8GB'")
    ap.add_argument("--category", default="显卡")
    ap.add_argument("--source", required=True,
                    help="来源轨：gwdang/manmanmai/smzdm/xianyi_app…")
    ap.add_argument("--channel", default="国行单卡")
    ap.add_argument("--date-dir", required=True,
                    help="落到哪个采集日目录（建议用一个回填专用日期段）")
    args = ap.parse_args(argv)

    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    rows = convert(raw, entity_hint={"category": args.category,
                                     "series": args.series,
                                     "spec": args.spec},
                   source=args.source, channel=args.channel)
    out = ROOT / "artifacts" / args.date_dir / "t1_sku_observations.json"
    total = append_rows(out, rows)
    print(f"appended {len(rows)} rows -> {out} (total {total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())