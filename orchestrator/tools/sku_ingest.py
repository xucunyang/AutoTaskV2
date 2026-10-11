"""sku_ingest：observations.json → sku.db 的确定性入库器。

设计：docs/design/sku-timeseries-db-v3.3.md（P0 交付物）。

三条铁律（设计 §3/§4 D4）：
1. **模型不写库**——本脚本是唯一的写库入口（含手机采集/爬虫回填，
   所有渠道终归 observations.json，v3.3 B3 单一入口）；
2. **契约裁决**——identity_hint 只是模型的提示，匹配不上就进隔离区
   （identity 严出）；attrs 的键必须在契约声明内（宽进的防线是
   键白名单，不是拒收）；
3. **幂等**——唯一键 (entity_id, obs_date, shop_platform, shop_name)
   冲突走 upsert，同日重跑不产生重复序列点。

用法：
    python -m tools.sku_ingest --date 2026-10-10 [--dry-run] [--root PATH]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.sku_db import connect  # noqa: E402


# ---------------------------------------------------------------- 契约

class Contract:
    """域契约的运行时形态（yaml 是唯一源，Q1 拍板）。"""

    def __init__(self, data: dict):
        self.domain = data["domain"]
        self.identity_keys: list[str] = list(data["identity"])
        self.attrs: dict = data.get("attrs") or {}
        self.metrics: dict = data.get("metrics") or {}
        self.propose_threshold = float(data.get("propose_threshold", 0.8))
        # 枚举别名映射（v3.3 实测：模型发明「京东自营/渠道参考价」等
        # 自然变体——映射比逐词扩枚举可持续）
        self.attr_value_maps: dict = data.get("attr_value_maps") or {}

    @classmethod
    def load(cls, root: Path, domain: str) -> "Contract":
        p = root / "config" / "contracts" / f"{domain}.yaml"
        if not p.exists():
            raise FileNotFoundError(f"契约不存在: {p}")
        import yaml
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")))


# ------------------------------------------------------------ 归一化

def normalize_name(raw: str) -> str:
    """契约 normalizers 的实现（确定性，无模型参与）。

    strip_paren_suffix / fullwidth_to_halfwidth / collapse_spaces /
    ti_case / gb_unit——顺序即契约声明序。
    """
    s = str(raw or "").strip()
    # 剥渠道/成色括号后缀：(京东全新)、（丐版国内）…（中英文括号都认，
    # 只剥尾部连续的括号段——型号中段的括号如 Ti(Pro) 极罕见，认了）
    while True:
        m = re.search(r"[（(][^（）()]*[)）]\s*$", s)
        if not m:
            break
        s = s[:m.start()].rstrip()
    # 全角字母数字 → 半角
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c
                for c in s)
    s = re.sub(r"\s+", " ", s)
    # ti 大小写：5060ti/5060TI/5060 ti → 5060 Ti（紧跟数字/空格的词尾 ti；
    # \b 在「0→t」之间不成立（都是 word char），必须用 lookbehind）
    s = re.sub(r"(?i)(?<=\d)\s*ti\b", " Ti", s)
    s = re.sub(r"(?i)(?<= )ti\b", "Ti", s)
    # 显存单位：8G/8g/8G → 8GB（数字+G 词尾）
    s = re.sub(r"(\d)\s*[Gg]\b", r"\1GB", s)
    return s.strip()


# ------------------------------------------------------------ 校验

def _bad_price(v) -> str | None:
    """价格数值域：非负有限数。上游的 50% 偏离告警在指标层做，
    这里只拦明显坏值（负数/零/非数）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "bad_price:not_a_number"
    if f <= 0 or f != f or f in (float("inf"), float("-inf")):
        return "bad_price:out_of_domain"
    return None


def validate_row(row: dict, contract: Contract, collected_on: str
                 ) -> tuple[dict | None, str | None]:
    """一行观测的校验与整形。返回 (整形行, 失败原因)。

    失败原因进隔离区（reason 列），成功行由调用方匹配实体后入库。
    """
    hints = row.get("identity_hint")
    if not isinstance(hints, dict) or not hints:
        return None, "no_identity_hint"
    # identity 键必须与契约声明一致（多键/少键都是坏行）
    if set(hints) != set(contract.identity_keys):
        return None, (f"identity_keys_mismatch:want={contract.identity_keys} "
                      f"got={sorted(hints)}")

    attrs = dict(row.get("attrs_hint") or {})
    unknown = sorted(set(attrs) - set(contract.attrs))
    if unknown:
        return None, f"unknown_attr_key:{unknown}"
    # required 级 attrs 缺失 → 隔离（aggregate_scope 依赖 condition/source
    # 分轨，缺了它们序列口径就废了——v3.3 M1/M2）
    for key, spec in contract.attrs.items():
        if spec.get("level") == "required" and not attrs.get(key):
            return None, f"missing_required_attr:{key}"
    # 枚举校验（值漂移拦截）：先应用契约别名映射（「京东自营」→「自营」
    # 这类自然变体——模型会发明变体，映射比逐词扩枚举可持续），
    # 映射后合法则替换入库值，仍不合法才隔离
    for key, val in attrs.items():
        allowed = (contract.attrs.get(key) or {}).get("values")
        if allowed and val not in allowed:
            mapped = (contract.attr_value_maps.get(key) or {}).get(val)
            if mapped and mapped in allowed:
                attrs[key] = mapped
            else:
                return None, f"attr_value_not_allowed:{key}={val}"

    lp, rp = row.get("low_price"), row.get("raw_price")
    err = _bad_price(lp) or (_bad_price(rp) if rp is not None else None)
    if err:
        return None, err

    obs_date = str(row.get("price_date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", obs_date):
        return None, f"bad_date:{obs_date!r}"
    if obs_date > collected_on:       # Q6：价格日不得晚于采集日
        return None, f"date_after_collection:{obs_date}>{collected_on}"

    shop = row.get("shop") or {}
    if not shop.get("platform") or not shop.get("name"):
        return None, "missing_shop_fields"

    metric = row.get("metric") or "price"
    if metric not in contract.metrics:
        return None, f"unknown_metric:{metric}"

    return {
        "identity_hint": {k: normalize_name(v) for k, v in hints.items()},
        "attrs": attrs, "low_price": float(lp),
        "raw_price": float(rp) if rp is not None else None,
        "obs_date": obs_date, "metric": metric,
        "shop_name": str(shop["name"]), "shop_platform": str(shop["platform"]),
        "shop_url": str(shop.get("url") or ""),
        "coupons": json.dumps(row.get("coupons") or [], ensure_ascii=False),
        "in_stock": 1 if row.get("in_stock", True) else 0,
        "source_url": str(row.get("source_url") or ""),
        "source_tier": str(row.get("source_tier") or ""),
    }, None


# ------------------------------------------------------------ 入库

def _match_entity(con: sqlite3.Connection, contract: Contract,
                  hint: dict) -> sqlite3.Row | None:
    """identity_hint → entity。精确匹配（键序一致）优先，别名兜底。

    归一化后的 hint 仍匹配不上 → None（调用方进隔离区，收编提案
    闭环 v3.3 M3 由后续的收编器处理，本模块只负责隔离）。
    """
    ident = json.dumps(hint, ensure_ascii=False)
    row = con.execute(
        "SELECT * FROM entity WHERE domain=? AND identity_json=?",
        (contract.domain, ident)).fetchone()
    if row:
        return row
    # 别名兜底：别名本身也归一化（「5060Ti 8G」入库时被归一成什么形态，
    # 比较就在同一形态下做）；series 与 series+spec 两种形态都试——
    # 种子别名有的带规格有的不带，宽进两侧。
    probe = {normalize_name(hint.get("series") or ""),
             normalize_name(f"{hint.get('series') or ''} {hint.get('spec') or ''}")}
    for ent in con.execute(
            "SELECT * FROM entity WHERE domain=?", (contract.domain,)):
        aliases = {normalize_name(a) for a in json.loads(ent["aliases"] or "[]")}
        if probe & aliases:
            return ent
    return None


def ingest_file(con: sqlite3.Connection, obs_path: Path, contract: Contract,
                collected_on: str, dry_run: bool = False) -> dict:
    """入库一个 observations.json。返回计数（供验收与日报报数）。"""
    rows = json.loads(obs_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).isoformat()
    stats = {"ok": 0, "updated": 0, "quarantined": 0, "no_entity": 0,
             "bad_row": 0}

    for raw in rows:
        shaped, reason = validate_row(raw, contract, collected_on)
        if shaped is None:
            stats["bad_row"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO observation_quarantine"
                    "(collected_on, reason, raw_json, created_at)"
                    " VALUES(?,?,?,?)",
                    (collected_on, reason,
                     json.dumps(raw, ensure_ascii=False), now))
            continue

        ent = _match_entity(con, contract, shaped["identity_hint"])
        if ent is None:
            stats["no_entity"] += 1
            if not dry_run:
                con.execute(
                    "INSERT INTO observation_quarantine"
                    "(collected_on, reason, raw_json, created_at)"
                    " VALUES(?,?,?,?)",
                    (collected_on, "no_entity",
                     json.dumps(raw, ensure_ascii=False), now))
            continue

        if not dry_run:
            cur = con.execute(
                "INSERT INTO observation(entity_id, obs_date, collected_on,"
                " low_price, raw_price, metric, price_unit, attrs_json,"
                " shop_name, shop_platform, shop_url, coupons, in_stock,"
                " source_url, source_tier, raw_json, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(entity_id, obs_date, shop_platform, shop_name)"
                " DO UPDATE SET low_price=excluded.low_price,"
                " raw_price=excluded.raw_price, attrs_json=excluded.attrs_json,"
                " coupons=excluded.coupons, in_stock=excluded.in_stock,"
                " collected_on=excluded.collected_on,"
                " raw_json=excluded.raw_json",
                (ent["entity_id"], shaped["obs_date"], collected_on,
                 shaped["low_price"], shaped["raw_price"], shaped["metric"],
                 (contract.metrics.get(shaped["metric"]) or {}).get("unit", "CNY"),
                 json.dumps(shaped["attrs"], ensure_ascii=False),
                 shaped["shop_name"], shaped["shop_platform"], shaped["shop_url"],
                 shaped["coupons"], shaped["in_stock"], shaped["source_url"],
                 shaped["source_tier"],
                 json.dumps(raw, ensure_ascii=False), now))
            stats["ok" if cur.rowcount == 1 else "updated"] += 1
        else:
            stats["ok"] += 1      # dry_run 只验不算

    if not dry_run:
        con.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="sku_ingest")
    ap.add_argument("--date", required=True, help="采集日 YYYY-MM-DD")
    ap.add_argument("--domain", default="hardware")
    ap.add_argument("--root", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else ROOT
    obs = root / "artifacts" / args.date / "t1_sku_observations.json"
    if not obs.exists():
        print(f"observations 文件不存在: {obs}")
        return 2
    contract = Contract.load(root, args.domain)
    con = connect(root / "data" / "sku.db")
    try:
        stats = ingest_file(con, obs, contract, args.date, args.dry_run)
        # stats 落盘由确定性代码完成（t1b 验收读它）：模型转抄 stdout
        # 不可靠——数字必须从产生者直达消费者
        stats_path = root / "artifacts" / args.date / "t1b_ingest_stats.json"
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(
            json.dumps({**stats, "date": args.date, "domain": args.domain,
                        "dry_run": args.dry_run}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
