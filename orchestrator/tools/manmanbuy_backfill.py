# -*- coding: utf-8 -*-
"""manmanbuy_backfill：慢慢买历史价格 → sku.db 的渠道适配器（R27）。

管线（全链确定性，零模型参与；URL 甄别在注册表里人工一次性完成）：

    config/backfill_manmanbuy.yaml（人工甄别过的 listing）
      → tools/manmanbuy_client.Mmb.trend(url)          采集
      → collapse_daily()                               按日坍缩（同日多点取最低）
      → tools/sku_backfill.convert()                   进料口格式（三轨 attrs）
      → append_rows(artifacts/{date}/t1_sku_observations.json)
      → tools/sku_ingest.ingest_file()                 唯一写库入口

分轨保证：source=manmanmai（注册表固定），主序列
aggregate_scope.default_filter 只认 source=自采——回填数据自动隔离在
主序列外，只进 manmanmai 轨（stab_risk/wait_value 消费，§9.5 矩阵）。

用法：
    python -m tools.manmanbuy_backfill login                # 一次性，密码不落盘
    python -m tools.manmanbuy_backfill run                  # 全量注册 listing
    python -m tools.manmanbuy_backfill run --dry-run        # 只看转换结果不写库
    python -m tools.manmanbuy_backfill run --from-cache X.json   # 离线重放
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from tools.manmanbuy_client import Mmb, SessionExpired  # noqa: E402
from tools.sku_backfill import append_rows, convert  # noqa: E402
from tools.sku_db import connect  # noqa: E402
from tools.sku_ingest import Contract, ingest_file  # noqa: E402

REGISTRY = ROOT / "config" / "backfill_manmanbuy.yaml"
SOURCE_URL = "https://tool.manmanbuy.com/HistoryLowest.aspx"
JD_ITEM = re.compile(r"^https://item\.jd\.com/\d+\.html$")

# 促销事件识别（契约 promo_event 枚举的确定性映射；备注是慢慢买
# 逐点自带的价格备注，如「国补前3347」「到手价3599.00」）
PROMO_PATTERNS = (("国补", "国补"), ("618", "618"), ("双11", "双11"),
                  ("以旧换新", "以旧换新"))


def promo_from(remark: str) -> str:
    for pat, label in PROMO_PATTERNS:
        if pat in (remark or ""):
            return label
    return "无"


def load_registry(contract: Contract | None = None) -> dict:
    """注册表 → 校验后的配置。甄别错误的 listing 在这里就拦下，
    不等入库器隔离区（回填是批量行为，坏行要防患于未然）。

    契约与注册表都属代码资产（config/），--root 只重定向
    artifacts/data 运行产物，不影响它们。"""
    contract = contract or Contract.load(ROOT, "hardware")
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    if reg.get("domain") != contract.domain:
        raise ValueError(f"注册表 domain={reg.get('domain')!r} 与契约不符")
    source = reg.get("source")
    if source not in contract.attrs["source"]["values"]:
        raise ValueError(f"source={source!r} 不在契约枚举里")
    out = []
    for i, it in enumerate(reg.get("listings") or []):
        if it.get("retired"):
            continue
        ident = it.get("identity") or {}
        missing = [k for k in contract.identity_keys if k not in ident]
        if missing:
            raise ValueError(f"listings[{i}] identity 缺键: {missing}")
        if not JD_ITEM.match(it.get("url") or ""):
            raise ValueError(f"listings[{i}] url 非京东详情页: {it.get('url')!r}")
        if it.get("channel") not in contract.attrs["channel"]["values"]:
            raise ValueError(f"listings[{i}] channel={it.get('channel')!r} 不合法")
        vendor = it.get("vendor")
        if vendor and vendor not in contract.attrs["vendor"]["values"]:
            raise ValueError(f"listings[{i}] vendor={vendor!r} 不合法")
        out.append(it)
    if not out:
        raise ValueError("注册表没有可用 listing")
    return {"domain": reg["domain"], "source": source, "listings": out}


def collapse_daily(points: list[list]) -> dict[str, tuple[float, str]]:
    """[[毫秒ts, 价格, 备注], …] → {YYYY-MM-DD: (当日最低, 备注)}。

    同日多点多出现在盘中调价——序列口径是「当日最低」（契约
    aggregate_rule: min），取最低点的备注一并保留。
    """
    by_day: dict[str, tuple[float, str]] = {}
    for pt in points:
        ts, price = pt[0], float(pt[1])
        remark = str(pt[2]) if len(pt) > 2 else ""
        day = time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
        cur = by_day.get(day)
        if cur is None or price < cur[0]:
            by_day[day] = (price, remark)
    return by_day


def trend_to_series(trend: dict, listing: dict, source: str) -> dict:
    """trend 响应 → convert() 吃的平铺序列。"""
    daily = collapse_daily(trend.get("datePrice") or [])
    if not daily:
        raise ValueError(f"{listing['url']} 无价格点（listing 可能已失效）")
    days = sorted(daily)
    return {
        "dates": days,
        "prices": [daily[d][0] for d in days],
        "remarks": [daily[d][1] for d in days],
        "shop": {"name": listing["sp_name"], "platform": "京东",
                 "url": listing["url"]},
        "source_url": SOURCE_URL,
        "_source_track": source,
    }


def dedupe_rows(new_rows: list[dict], existing_rows: list[dict]) -> list[dict]:
    """同键（店铺×日期×价格）已在文件里 → 跳过。run 重跑幂等：
    文件不膨胀，DB 靠 upsert 兜底。"""
    def key(r):
        return (r["shop"]["name"], r["shop"]["platform"],
                r["price_date"], r["low_price"])
    seen = {key(r) for r in existing_rows}
    return [r for r in new_rows if key(r) not in seen]


def _fetch_dumps(mmb: Mmb, registry: dict, sleep_s: float) -> dict:
    dumps = {"fetched_at": datetime.now(timezone.utc).isoformat(),
             "listings": []}
    for i, it in enumerate(registry["listings"]):
        if i:
            time.sleep(sleep_s)   # 交接文档：1 请求/2 秒，未见过封锁
        trend = mmb.trend(it["url"])
        dumps["listings"].append({
            "identity": it["identity"], "url": it["url"],
            "sp_name": it["sp_name"], "channel": it["channel"],
            "vendor": it.get("vendor") or "", "trend": trend})
        print(f"  fetched {it['sp_name'][:30]}: {len(trend.get('datePrice') or [])} 点")
    return dumps


def run(date_dir: str, root: Path = ROOT, from_cache: str | None = None,
        dry_run: bool = False, skip_ingest: bool = False,
        sleep_s: float = 2.0, only: str | None = None) -> int:
    # 契约/注册表属代码资产，固定从模块 ROOT 读；root 只重定向产物
    contract = Contract.load(ROOT, "hardware")
    registry = load_registry(contract)
    if only:
        registry["listings"] = [it for it in registry["listings"]
                                if only in it["url"] or only in it["sp_name"]]
        if not registry["listings"]:
            print(f"--only {only!r} 未命中任何 listing")
            return 2

    if from_cache:
        dumps = json.loads(Path(from_cache).read_text(encoding="utf-8"))
        print(f"[cache] {from_cache}（{len(dumps['listings'])} listing）")
    else:
        mmb = Mmb()
        if not mmb.load_session():
            print("未登录：先执行 python -m tools.manmanbuy_backfill login")
            return 2
        print(f"[fetch] {len(registry['listings'])} listing…")
        dumps = _fetch_dumps(mmb, registry, sleep_s)
        cache_path = root / "artifacts" / date_dir / "backfill_manmanbuy_raw.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(dumps, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        print(f"  raw 留档 -> {cache_path}")

    obs_path = root / "artifacts" / date_dir / "t1_sku_observations.json"
    existing = json.loads(obs_path.read_text(encoding="utf-8")) if obs_path.exists() else []
    total_added = 0
    for it in dumps["listings"]:
        try:
            series = trend_to_series(it["trend"], it, registry["source"])
        except ValueError as e:
            print(f"  ! 跳过 {it['sp_name'][:28]}: {e}")
            continue
        rows = convert(series, entity_hint=it["identity"],
                       source=registry["source"], channel=it["channel"],
                       extra_attrs=({"vendor": it["vendor"]}
                                    if it.get("vendor") else {}))
        # 促销事件逐日判定（convert 是批量口径，逐行覆写 optional 维度）
        for row, remark in zip(rows, series["remarks"]):
            row["attrs_hint"]["promo_event"] = promo_from(remark)
        rows = dedupe_rows(rows, existing)
        if not dry_run:
            existing = existing + rows
            append_rows(obs_path, rows)
        total_added += len(rows)
        print(f"  {it['sp_name'][:28]}: +{len(rows)} 行"
              f"（{series['dates'][0]} ~ {series['dates'][-1]}）")
    if dry_run:
        print(f"[dry-run] 共 {total_added} 行，未写文件未入库")
        return 0

    if skip_ingest:
        print(f"[skip-ingest] {total_added} 行已追加 {obs_path}")
        return 0

    con = connect(root / "data" / "sku.db")
    try:
        stats = ingest_file(con, obs_path, contract, date_dir)
    finally:
        con.close()
    stats_path = root / "artifacts" / date_dir / "backfill_ingest_stats.json"
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps({**stats, "date": date_dir, "channel_adapter": "manmanbuy"},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ingest] {json.dumps(stats, ensure_ascii=False)}")
    print(f"  stats -> {stats_path}")
    return 0


def login(user: str | None = None) -> int:
    import getpass
    if not user:
        user = input("慢慢买手机号: ").strip()
    pwd = getpass.getpass("密码（不回显、不落盘）: ")
    ok = Mmb().login(user, pwd)
    print("登录:", "成功，会话已存 data/manmanbuy_session.json" if ok else "失败")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="manmanbuy_backfill")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="人工登录一次（密码不落盘）")
    lg.add_argument("--user", default=None)
    rn = sub.add_parser("run", help="按注册表采集→转换→入库")
    rn.add_argument("--date-dir", default=time.strftime("%Y-%m-%d"),
                    help="采集日目录（默认今天；价格日不得晚于此日，Q6）")
    rn.add_argument("--root", default=None)
    rn.add_argument("--from-cache", default=None, help="离线重放 raw 留档")
    rn.add_argument("--dry-run", action="store_true")
    rn.add_argument("--skip-ingest", action="store_true")
    rn.add_argument("--only", default=None, help="URL/名称子串过滤")
    rn.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args(argv)
    if args.cmd == "login":
        return login(args.user)
    root = Path(args.root) if args.root else ROOT
    try:
        return run(args.date_dir, root=root, from_cache=args.from_cache,
                   dry_run=args.dry_run, skip_ingest=args.skip_ingest,
                   sleep_s=args.sleep, only=args.only)
    except SessionExpired as e:
        print(f"会话失效：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
