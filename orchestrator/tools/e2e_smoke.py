"""e2e_smoke：P1 管道端到端验证（t1b → trend_calc → verify_snap）。

跑法：python -X utf8 tools/e2e_smoke.py [--date 2026-10-08]

做什么：
1. 造真实形态的样本观测（5 期 × 3 基底实体 × 含双 vendor 触发 contrast）
   写入 artifacts/{date}/t1_sku_observations.json；
2. 走 tools.sku_ingest 入库（真实 data/sku.db，upsert 幂等——重跑安全）；
3. 走 tools.trend_calc 出快照；
4. 走 tools.verify_snap.check 做一致性校验（与 t4 验收同一条代码路径）。

全过打印各步统计；任何一步断言失败即非 0 退出。

注意：样本观测会进入真实 data/sku.db（作为序列的早期期数，垫
sufficiency）——这是有意为之（用户批准的首次入库），upsert 幂等
保证重跑不产生重复点。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORCH))

from tools.sku_db import connect  # noqa: E402
from tools.sku_ingest import Contract, ingest_file  # noqa: E402
from tools.verify_snap import check  # noqa: E402


def _make_sample_rows() -> list[dict]:
    """5 期 × 3 基底实体（其中 9070XT 双 vendor 触发 contrast）。"""
    dates = [("2026-10-04", 2899.0), ("2026-10-05", 2909.0),
             ("2026-10-06", 2889.0), ("2026-10-07", 2879.0),
             ("2026-10-08", 2859.0)]
    rows = []

    def add(series: str, spec: str, vendor: str, shop: str, price: float,
            d: str) -> None:
        rows.append({
            "identity_hint": {"category": "显卡", "series": series,
                              "spec": spec},
            "attrs_hint": {"condition": "全新", "channel": "国行单卡",
                           "source": "自采", "vendor": vendor},
            "low_price": price, "raw_price": price + 120,
            "price_date": d,
            "shop": {"name": shop, "platform": "京东", "url": "https://x"},
            "coupons": [], "in_stock": True,
            "source_url": "https://x", "source_tier": "B",
        })

    for d, p in dates:
        add("RTX 5060 Ti", "8GB", "七彩虹", "七彩虹京东自营旗舰店", p, d)
        add("RTX 4060", "8GB", "微星", "微星旗舰店", p - 300, d)
        add("RX 9070 XT", "16GB", "蓝宝石", "蓝宝石旗舰店", p + 1300, d)
        add("RX 9070 XT", "16GB", "华擎", "华擎旗舰店", p + 1400, d)
    return rows


def run(date: str = "2026-10-08") -> int:
    obs = ORCH / "artifacts" / date / "t1_sku_observations.json"
    obs.parent.mkdir(parents=True, exist_ok=True)
    obs.write_text(json.dumps(_make_sample_rows(), ensure_ascii=False,
                              indent=1), encoding="utf-8")
    print(f"[1/3] observations written: {obs}")

    contract = Contract.load(ORCH, "hardware")
    con = connect(ORCH / "data" / "sku.db")
    try:
        stats = ingest_file(con, obs, contract, date, dry_run=False)
        con.commit()
    finally:
        con.close()
    print(f"[2/3] ingest: {stats}")
    assert stats["ok"] + stats["updated"] >= 10, f"入库行数不足: {stats}"

    from tools.trend_calc import compute_snapshot
    con = connect(ORCH / "data" / "sku.db")
    try:
        snap = compute_snapshot(con, date)
    finally:
        con.close()
    snap_path = ORCH / "artifacts" / date / "trend_snapshot.json"
    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    filled = [e for e in snap["entities"] if e.get("metrics")]
    print(f"[3/3] snapshot: entities={len(snap['entities'])} "
          f"with_metrics={len(filled)} contrasts={len(snap['contrasts'])}")
    assert filled, "没有实体的 metrics 被填上（L0 失效）"
    sample = filled[0]
    print(f"  sample: {sample['display_name']} -> {sample['metrics']}")

    ok, detail = check(ORCH)
    print(f"[verify_snap] {'PASS' if ok else 'FAIL'} {detail}")
    assert ok, detail
    print("E2E SMOKE PASS")
    return 0


if __name__ == "__main__":
    # 支持两种姿势：tools/e2e_smoke.py 2026-10-08 或 --date 2026-10-08
    argv = sys.argv[1:]
    date = "2026-10-08"
    if argv:
        date = argv[1] if argv[0] == "--date" else argv[0]
    sys.exit(run(date))