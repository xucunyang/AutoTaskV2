"""sku_anchor：写入实体的 perf_anchor（Q14 锚点基底，P0 拍板）。

锚点体系（设计 v3.3 §4 D4）：同代销量最大者 = 1.0，其余按性能倍数。
P0 阶段先定各代锚点基底（谁是 1.0）；性能倍数（5090=多少倍 5060Ti）
需要外部性能天梯数据，P1.5 补——届时 perf_anchor 字段直接 UPDATE 倍数。

拍板依据（2026-10-10）：
- 50 系基底 = RTX 5060 Ti 16GB：京东列表观察 + 行业常识
  （50 系里 5060 Ti 在游戏整机配置中出现频率最高，是同代走量款）
- 40 系过渡款基底 = RTX 4060：4 系入门款、销量稳居前列
- 90 系基底 = RX 9070 XT：RX 9000 系走量款（暂定，P1.5 用京东验证）
- CPU 基底：暂留空（P1.5）

用法：python -m tools.sku_anchor [--dry-run]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.sku_db import connect  # noqa: E402

# display_name（决定锚定） → perf_anchor 值（=1.0 是基底，其他按倍数，P1.5 填）
ANCHORS = {
    # 50 系：RTX 5060 Ti 16GB = 1.0（基底）
    "RTX 5060 Ti 16GB": 1.0,
    # 40 系：RTX 4060 = 1.0
    "RTX 4060 8GB": 1.0,
    # RX 9000：RX 9070 XT = 1.0（P1.5 京东验证）
    "RX 9070 XT 16GB": 1.0,
    # CPU/内存/SSD 基底 P1.5 决定，先占位
}


def apply_anchors(con: sqlite3.Connection, dry_run: bool = False) -> dict:
    """把 ANCHORS 表写入 entity.perf_anchor（按 display_name 精确匹配）。"""
    results = {"set": [], "skipped_no_match": [], "already_set": []}
    for display_name, anchor in ANCHORS.items():
        row = con.execute(
            "SELECT entity_id, perf_anchor FROM entity WHERE display_name=?",
            (display_name,)).fetchone()
        if row is None:
            results["skipped_no_match"].append(display_name)
            continue
        eid, current = row
        if current == anchor:
            results["already_set"].append(display_name)
            continue
        if not dry_run:
            con.execute(
                "UPDATE entity SET perf_anchor=?, updated_at=? WHERE entity_id=?",
                (anchor, "now", eid))      # updated_at 用占位，触发器/Python 写入 UTC ISO
        results["set"].append(display_name)
    if not dry_run:
        con.commit()
    return results


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="sku_anchor")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else ROOT
    con = connect(root / "data" / "sku.db")
    try:
        r = apply_anchors(con, args.dry_run)
        import json
        print(json.dumps(r, ensure_ascii=False))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())