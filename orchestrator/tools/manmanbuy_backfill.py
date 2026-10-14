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
import random
import re
import sys
import time
import urllib.request
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


def _fetch_dumps(mmb: Mmb, registry: dict, sleep_range: tuple[float, float]) -> dict:
    dumps = {"fetched_at": datetime.now(timezone.utc).isoformat(),
             "listings": []}
    for i, it in enumerate(registry["listings"]):
        if i:
            # 节流=区间内随机（默认 2~7s）：固定间隔是可被识别的机器指纹，
            # 随机化更像人；区间上限可按需放宽（--sleep-max）
            time.sleep(random.uniform(*sleep_range))
        trend = mmb.trend(it["url"])
        dumps["listings"].append({
            "identity": it["identity"], "url": it["url"],
            "sp_name": it["sp_name"], "channel": it["channel"],
            "vendor": it.get("vendor") or "", "trend": trend})
        print(f"  fetched {it['sp_name'][:30]}: {len(trend.get('datePrice') or [])} 点")
    return dumps


def run(date_dir: str, root: Path = ROOT, from_cache: str | None = None,
        dry_run: bool = False, skip_ingest: bool = False,
        sleep_min: float = 2.0, sleep_max: float = 7.0,
        only: str | None = None) -> int:
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
        dumps = _fetch_dumps(mmb, registry, (sleep_min, sleep_max))
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


# ---------- discover：search 驱动的双锚候选发现（品类采集规则 v1） ----------
#
# 规则（2026-10-14 用户定稿）：每实体挂两个锚——
#   销量锚 = search_top 规格匹配里的销量第一（市场主流成交价，走量款）
#   价格锚 = compare_price 价格升序里的第一个正规链接（R29 最低价准则地板）
# 双锚都要求：正规渠道站内（siteid 1京东/10天猫，拼多多/抖音低价链不进锚）
# + 规格子串匹配（防「RTX 5060 混进 5060 Ti 桶」的 DDG 教训）。
# 人工只剩一件事：给候选定渠道档位（自营/第三方）后打勾入注册表。

GOOD_SITEIDS = {"1", "10"}          # 1=京东 10=天猫
_ANCHOR_SLEEP = (2.0, 7.0)


def _norm_token(s: str) -> str:
    """标题/规格归一：小写、去空格、8GB→8G，供子串匹配。"""
    return (s or "").lower().replace(" ", "").replace("gb", "g")


def _match_spec(title: str, series: str, spec: str) -> bool:
    t = _norm_token(title)
    s = _norm_token(series)
    if s not in t or _norm_token(spec) not in t:
        return False
    # 后缀串台防线（2026-10-14 冒烟实测抓到：5070Ti 混进 RTX 5070 桶——
    # 归一化后 rtx5070ti 包含 rtx5070 子串）。系列名紧跟 ti/super/pro/
    # v2/d 的判定为异款；系列名自身带该后缀（如 RTX 5090 D）不拦。
    i = t.find(s)
    if i != -1:
        rest = t[i + len(s):]
        for suf in ("ti", "super", "pro", "v2", "d"):
            if not s.endswith(suf) and rest.startswith(suf):
                return False
    return True


def _resolve_item_url(url: str) -> str:
    """jingfen.jd.com 推广短链 → 最终 item.jd.com/数字.html（跟随跳转）。

    解析不出就原样返回（trend 也吃短链，但注册表只收稳定详情页 URL）。
    """
    if "item.jd.com" in (url or ""):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
        return final if "item.jd.com" in final else url
    except Exception:
        return url


def discover(series: str, spec: str, keyword: str | None = None,
             category: str = "显卡", top_n: int = 5) -> dict:
    """双锚候选发现：search 驱动，输出候选表（含试采点数）。

    keyword 默认按 R28 搜索规范拼（大类前缀+系列+规格）；人工复核后
    把打勾的行誊进 config/backfill_manmanbuy.yaml。
    """
    if not spec:
        raise ValueError("spec 不能为空（--spec，如 8GB/16GB）")
    kw = keyword or f"{category} {series} {spec}"
    mmb = Mmb()
    if not mmb.load_session():
        raise SessionExpired("未登录：先执行 python -m tools.manmanbuy_backfill login")

    def _ok(it):
        return (str(it.get("siteid")) in GOOD_SITEIDS
                and _match_spec(it.get("title") or "", series, spec))

    tops = mmb.search_top(kw, top_n)
    time.sleep(random.uniform(*_ANCHOR_SLEEP))
    lows = mmb.compare_price(kw, 12)
    sales_anchor = next(filter(_ok, tops), None)
    price_anchor = next(filter(_ok, lows), None)

    out = {"keyword": kw, "series": series, "spec": spec,
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "candidates": {}}
    for tag, it in (("sales", sales_anchor), ("price", price_anchor)):
        if not it:
            out["candidates"][tag] = None
            continue
        raw_url = it.get("prourl") or it.get("gourl") or ""
        url = _resolve_item_url(raw_url)
        time.sleep(random.uniform(*_ANCHOR_SLEEP))
        try:
            trend = mmb.trend(url)
            pts = len(trend.get("datePrice") or [])
            sp_name = (trend.get("spName") or it.get("title") or "")[:40]
            cur = trend.get("currentPrice")
        except SessionExpired:
            raise
        except Exception as e:
            pts, sp_name, cur = 0, (it.get("title") or "")[:40], None
            print(f"  ! {tag} 锚试采失败: {e}")
        out["candidates"][tag] = {
            "anchor": tag, "title": (it.get("title") or "")[:60],
            "sp_name": sp_name, "mall": it.get("mall"),
            "siteid": str(it.get("siteid") or ""),
            "price": it.get("price"), "sales": it.get("sales"),
            "monthComment": it.get("monthComment"),
            "prourl": raw_url, "resolved_url": url, "trend_points": pts,
            "current_price": cur}
        c = out["candidates"][tag]
        print(f"[{tag}] {c['sp_name']} | ¥{c['price']} 销量:{c['sales']}"
              f" | {c['resolved_url'][:48]} | 曲线 {pts} 点")
    return out


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
    rn.add_argument("--sleep-min", type=float, default=2.0,
                    help="节流随机下限秒（默认 2）")
    rn.add_argument("--sleep-max", type=float, default=7.0,
                    help="节流随机上限秒（默认 7；固定间隔是机器指纹，随机化更像人）")
    dv = sub.add_parser("discover", help="双锚候选发现（search 驱动，产出候选表）")
    dv.add_argument("--series", required=True, help="如 'RTX 5070'")
    dv.add_argument("--spec", required=True, help="如 12GB")
    dv.add_argument("--category", default="显卡")
    dv.add_argument("--keyword", default=None, help="覆盖默认搜索词（默认 R28 规范拼接）")
    dv.add_argument("--top-n", type=int, default=5)
    dv.add_argument("--out", default=None, help="候选表落盘路径（默认 artifacts/今日/backfill_discover_*.json）")
    args = ap.parse_args(argv)
    if args.cmd == "login":
        return login(args.user)
    if args.cmd == "discover":
        try:
            result = discover(args.series, args.spec, keyword=args.keyword,
                              category=args.category, top_n=args.top_n)
        except SessionExpired as e:
            print(f"会话失效：{e}")
            return 2
        out = args.out or str(ROOT / "artifacts" / time.strftime("%Y-%m-%d")
                              / f"backfill_discover_{args.series}_{args.spec}.json".replace(" ", ""))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"候选表 -> {out}（人工核定 channel 后誊入 config/backfill_manmanbuy.yaml）")
        return 0
    root = Path(args.root) if args.root else ROOT
    try:
        return run(args.date_dir, root=root, from_cache=args.from_cache,
                   dry_run=args.dry_run, skip_ingest=args.skip_ingest,
                   sleep_min=args.sleep_min, sleep_max=args.sleep_max,
                   only=args.only)
    except SessionExpired as e:
        print(f"会话失效：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
