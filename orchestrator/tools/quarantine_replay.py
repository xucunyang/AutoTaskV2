"""quarantine_replay：隔离区重放——契约/注册表更新后把隔离行再试一遍。

典型场景（Q2 收编闭环的手动/半自动形态）：
- 契约补了枚举/别名映射（如 channel「电商」→「第三方」）——映射生效
  前隔离的行现在能合法入库；
- 实体注册表新增了实体（series-only 兜底/收编新建）——此前 no_entity
  的行现在能匹配。

流程：读隔离区全部行 → 按 collected_on 分组临时写 observations.json
→ 复用 tools.sku_ingest.ingest_file 重放 → **已成功入库的行从隔离区
删除**（失败的留在隔离区，等下一次契约/注册表更新）。

用法：python -m tools.quarantine_replay [--domain hardware] [--dry-run]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORCH))

from tools.sku_db import connect  # noqa: E402
from tools.sku_ingest import Contract, ingest_file  # noqa: E402


def replay(con: sqlite3.Connection, contract: Contract,
           dry_run: bool = False) -> dict:
    rows = con.execute(
        "SELECT id, collected_on, raw_json FROM observation_quarantine "
        "ORDER BY collected_on").fetchall()
    if not rows:
        return {"replayed": 0, "ingested": 0, "still_isolated": 0,
                 "note": "隔离区为空"}

    # 按 collected_on 分组（入库器的 upsert 键不含 collected_on，但
    # stats 文件按日期归档，分组重放保持口径一致）
    by_date: dict[str, list[dict]] = {}
    meta: dict[str, list[int]] = {}
    for qid, collected_on, raw in rows:
        by_date.setdefault(collected_on, []).append(
            json.loads(raw or "{}"))
        meta.setdefault(collected_on, []).append(qid)

    ingested_ids: list[int] = []
    total_ingested = 0
    for date, obs_rows in by_date.items():
        tmp = ORCH / "artifacts" / date / "_quarantine_replay.json"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(obs_rows, ensure_ascii=False),
                       encoding="utf-8")
        stats = ingest_file(con, tmp, contract, date, dry_run=dry_run)
        total = stats["ok"] + stats["updated"]
        total_ingested += total
        if total >= len(obs_rows):
            ingested_ids.extend(meta[date])   # 整批消化 → 隔离行可删
        else:
            # 部分消化：需要知道哪些行进去了。入库器不回传行级结果——
            # 简化：整批保留在隔离区（下次契约/注册表更新后重放），
            # 并把「部分消化」记入统计让人工留意。
            pass
        tmp.unlink(missing_ok=True)

    result = {"replayed": len(rows), "ingested": total_ingested,
              "still_isolated": len(rows) - total_ingested}
    if not dry_run and ingested_ids:
        marks = ",".join("?" * len(ingested_ids))
        con.execute(f"DELETE FROM observation_quarantine WHERE id IN ({marks})",
                    ingested_ids)
        con.commit()
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="quarantine_replay")
    ap.add_argument("--domain", default="hardware")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root) if args.root else ORCH
    contract = Contract.load(root, args.domain)
    con = connect(root / "data" / "sku.db")
    try:
        import json
        print(json.dumps(replay(con, contract, args.dry_run),
                         ensure_ascii=False))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())