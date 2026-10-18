"""sku.db：通用实体时序观测库的建库与种子工具。

设计文档：docs/design/sku-timeseries-db-v3.3.md（P0 交付物）。

为什么单独一个模块而不塞进 sku_ingest：
建库/种子是**一次性运维动作**，入库是每日动作——生命周期不同，
放一起会让"每天跑的代码"背上"只在建库时需要的逻辑"。

契约（config/contracts/*.yaml）是维度知识的唯一源（Q1 拍板：
yaml 为源、DB 不存维度声明），本模块建表时不做契约校验，
校验发生在入库器加载契约时——建库早于契约定稿也能跑。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "sku.db"

# v3.3 §5 DDL。entity_lineage 是质检修正 B1 的载体：修饰维度升格为
# 身份时实体换 id，但 observation 的外键永不改行——新实体经 lineage
# 声明继承旧实体的观测，查询层按 lineage 展开。
SCHEMA = """
CREATE TABLE IF NOT EXISTS entity (
  entity_id     INTEGER PRIMARY KEY,
  domain        TEXT NOT NULL,
  identity_json TEXT NOT NULL,     -- 键序=契约声明序
  display_name  TEXT NOT NULL,     -- 展示名（确定性拼接）
  brand         TEXT NOT NULL DEFAULT '',  -- 品牌（契约 vendor 词表，10-18）
  aliases       TEXT,              -- JSON：别名（归一化规则外的人工兜底）
  perf_anchor   REAL,              -- 相对锚点（Q4：同代销量最大者=1.0，可空待定基底）
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT, updated_at TEXT,
  UNIQUE(domain, identity_json)
);
CREATE TABLE IF NOT EXISTS observation (
  id INTEGER PRIMARY KEY,
  entity_id INTEGER NOT NULL REFERENCES entity(entity_id),
  obs_date TEXT NOT NULL,          -- 价格日（页面口径，Q6 拍板的序列轴）
  collected_on TEXT NOT NULL,      -- 采集日
  low_price REAL, raw_price REAL,
  metric TEXT NOT NULL DEFAULT 'price',
  price_unit TEXT NOT NULL DEFAULT 'CNY',
  attrs_json TEXT,                 -- 修饰维度（键须在契约 attr 内）
  shop_name TEXT, shop_platform TEXT, shop_url TEXT,
  coupons TEXT, in_stock INTEGER,
  source_url TEXT, source_tier TEXT, raw_json TEXT,
  created_at TEXT,
  UNIQUE(entity_id, obs_date, shop_platform, shop_name)
);
CREATE INDEX IF NOT EXISTS idx_obs_entity_date
  ON observation(entity_id, obs_date);
CREATE TABLE IF NOT EXISTS observation_quarantine (
  id INTEGER PRIMARY KEY, collected_on TEXT, reason TEXT,
  raw_json TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS entity_lineage (
  entity_id INTEGER NOT NULL,
  inherits_from INTEGER NOT NULL,
  dimension_change TEXT,
  created_at TEXT,
  PRIMARY KEY (entity_id, inherits_from)
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """开连接并建表（幂等）。WAL 留读并发（设计 D1）。"""
    p = db_path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    # WAL：趋势计算（读）与人工查询可并行，不被入库（写）锁死
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def seed_entities(con: sqlite3.Connection, seed_path: Path) -> int:
    """从 yaml 种子文件导入首发实体清单（Q7：~30 个主流 SKU）。

    种子文件结构与契约同目录（config/contracts/*_entities.yaml），
    每条：identity（按契约键序）+ display_name + 可选 aliases。
    幂等：UNIQUE(domain, identity_json) 冲突即跳过（不覆盖别名与
    perf_anchor——种子重跑不该把人工维护的锚点抹掉）。
    """
    import yaml
    from datetime import datetime, timezone

    data = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    domain = data["domain"]
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for item in data["entities"]:
        ident = json.dumps(item["identity"], ensure_ascii=False,
                           sort_keys=False)   # 键序=契约声明序，不排序
        try:
            con.execute(
                "INSERT INTO entity(domain, identity_json, display_name,"
                " aliases, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (domain, ident, item["display_name"],
                 json.dumps(item.get("aliases", []), ensure_ascii=False),
                 now, now))
            n += 1
        except sqlite3.IntegrityError:
            continue          # 已存在：保留库里的（别名/锚点可能被人工改过）
    con.commit()
    return n


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="sku_db")
    ap.add_argument("action", choices=["init", "seed", "stats"])
    ap.add_argument("--domain", default="hardware", help="seed 的域")
    args = ap.parse_args(argv)

    con = connect()
    if args.action == "init":
        print(f"schema ready: {DB_PATH}")
    elif args.action == "seed":
        seed = ROOT / "config" / "contracts" / f"{args.domain}_entities.yaml"
        if not seed.exists():
            print(f"种子文件不存在: {seed}")
            return 2
        n = seed_entities(con, seed)
        print(f"seeded {n} new entities ({args.domain})")
    else:
        for row in con.execute(
                "SELECT (SELECT count(*) FROM entity) AS entities,"
                " (SELECT count(*) FROM observation) AS observations,"
                " (SELECT count(*) FROM observation_quarantine) AS quarantined"):
            print(dict(row))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
