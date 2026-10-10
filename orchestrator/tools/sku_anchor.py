"""sku_anchor：写入实体的 perf_anchor（Q14 锚点基底，P0 拍板）。

锚点体系（设计 v3.3 §4 D4）：同代销量最大者 = 1.0，其余按性能倍数。
P0 阶段先定各代锚点基底（谁是 1.0）；性能倍数（5090=多少倍 5060Ti）
需要外部性能天梯数据，P1.5 补——届时 perf_anchor 字段直接 UPDATE 倍数。

拍板依据（2026-10-10）：
- 50 系基底 = RTX 5060 Ti **8GB**：走量甜品（16GB 偏贵不是走量款，用户纠正）
  · **2026-10-10 京东实证**：影驰 5060Ti 圣刃 8G 自营旗舰已售 2万+
    （同列表其他 5060Ti 整机/配置已售 4000-1万，8G 单卡领先 1-2 倍；
    这是 Q4 相对锚点体系最直接的销量代理实证）
- 40 系过渡款基底 = RTX 4060：4 系入门款、销量稳居前列
- 90 系基底 = RX 9070 XT：RX 9000 系走量款（暂定，P1.5 用京东验证）
- CPU 基底：暂留空（P1.5）
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
    # 50 系：RTX 5060 Ti **8GB** = 1.0（走量甜品款）
    #   （2026-10-10 用户纠正：16GB 偏贵不是走量款，8GB 才是同代锚点）
    "RTX 5060 Ti 8GB": 1.0,
    # 40 系：RTX 4060 = 1.0
    "RTX 4060 8GB": 1.0,
    # RX 9000：RX 9070 XT = 1.0（P1.5 京东验证）
    "RX 9070 XT 16GB": 1.0,
    # CPU/内存/SSD 基底 P1.5 决定，先占位
}


def apply_anchors(con: sqlite3.Connection, dry_run: bool = False) -> dict:
    """把 ANCHORS 表写入 entity.perf_anchor（按 display_name 精确匹配）。

    同时清空**字典外的**实体的 perf_anchor——锚点字典是权威来源，
    字典外的旧值要么是误设要么是过时的示例值，要么等 P1.5 补倍数。
    这避免了「基底」和「非基底」共用 perf_anchor 字段造成的混淆：
    1.0 是基底，>0 且 !=1.0 是倍数，空值是「待 P1.5」。
    """
    anchor_set = set(ANCHORS)
    results = {"set": [], "skipped_no_match": [], "already_set": [],
               "cleared": []}
    rows = con.execute(
        "SELECT entity_id, display_name, perf_anchor FROM entity").fetchall()
    for eid, display_name, current in rows:
        target = ANCHORS.get(display_name)
        if target is None:
            # 字典外的实体：如果当前有值且不在字典中，清掉（清旧）
            if current is not None:
                if not dry_run:
                    con.execute(
                        "UPDATE entity SET perf_anchor=NULL, updated_at=? "
                        "WHERE entity_id=?",
                        ("now", eid))
                results["cleared"].append(display_name)
            else:
                results["skipped_no_match"].append(display_name)
            continue
        if current == target:
            results["already_set"].append(display_name)
            continue
        if not dry_run:
            con.execute(
                "UPDATE entity SET perf_anchor=?, updated_at=? WHERE entity_id=?",
                (target, "now", eid))
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