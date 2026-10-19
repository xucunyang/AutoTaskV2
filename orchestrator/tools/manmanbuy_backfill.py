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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from channel_gate import ChannelGate  # 独立包（pip install -e E:\code\channel-gate）  # noqa: E402
from channel_gate.gate import BudgetExhausted  # noqa: E402
from tools.manmanbuy_client import Mmb, SessionExpired  # noqa: E402
from tools.sku_backfill import append_rows, convert  # noqa: E402
from tools.sku_db import connect  # noqa: E402
from tools.sku_ingest import Contract, ingest_file  # noqa: E402

REGISTRY = ROOT / "config" / "backfill_manmanbuy.yaml"
SOURCE_URL = "https://tool.manmanbuy.com/HistoryLowest.aspx"
# 锚的 URL 形态见 discover 附近的 TRACKED_URL 注释；注册表校验同一规则
JD_ITEM = re.compile(
    r"^https://(item\.jd\.com/\d+\.html|jingfen\.jd\.com/detail/)")

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
        # per-listing 数据源（10-17 gwdang 顶班）：缺省 manmanmai
        if it.get("source", "manmanmai") not in contract.attrs["source"]["values"]:
            raise ValueError(f"listings[{i}] source={it.get('source')!r} 不合法")
        vendor = it.get("vendor")
        if vendor and vendor not in contract.attrs["vendor"]["values"]:
            raise ValueError(f"listings[{i}] vendor={vendor!r} 不合法")
        anchor = it.get("anchor")
        if anchor and anchor not in contract.attrs.get("anchor", {}).get("values", []):
            raise ValueError(f"listings[{i}] anchor={anchor!r} 不合法")
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
                 # 溯源优先显式 shop_url（如 redirectjd 跳转页），否则采集键
                 "url": listing.get("shop_url") or listing["url"]},
        # 数据来源页：慢买 tool 页 / 购物党趋势页（gwdang 顶班轨）
        "source_url": (SOURCE_URL if source == "manmanmai"
                       else "https://www.gwdang.com/trend"),
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


def _ts() -> str:
    """日志行时间戳（控制台传统样式：日期 时间.毫秒）。"""
    t = datetime.now()
    return f"{t.strftime('%Y-%m-%d %H:%M:%S')}.{t.microsecond // 1000:03d}"


def _stamp(msg: str) -> None:
    """闸门日志统一走时间戳前缀（ChannelGate log= 注入）。"""
    print(f"[{_ts()}] {msg}", flush=True)


_GWD = None   # gwdang 客户端单例（_gwd() 惰性初始化）


def _gwd():
    """gwdang 客户端（惰性单例）：慢买 0 点锚的顶班数据源。

    节流走 gwdang 预置档（Pacer 4~12s/每 8 发歇 5~15 分钟），不占 mmb
    闸门预算——两渠道两本账（操作手册 §7）。"""
    global _GWD
    if _GWD is None:
        from tools.gwdang_client import Gwdang
        from channel_gate import Pacer
        _GWD = Gwdang(pacer=Pacer.for_channel("gwdang"))
        _GWD.load_session()   # 无会话匿名跑：url_to_dp 可用，data_www 可能 403
    return _GWD


def _notify_feishu(msg: str) -> None:
    """飞书推送（cc-push）：预算到顶等需要人工决策的场景，无论用户是否在
    电脑前都推（远程决策例外）。best-effort，失败静默（通知不阻塞采集）。"""
    try:
        ps = Path.home() / ".local" / "bin" / "cc-push.ps1"
        if not ps.exists():
            return
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(ps), msg], capture_output=True, timeout=30)
    except Exception:
        pass


def _fetch_dumps(mmb: "Mmb | None", registry: dict,
                 sleep_range: tuple[float, float],
                 gate=None, cache_path: Path | None = None,
                 done_urls: set | None = None,
                 prev_listings: list | None = None) -> dict:
    """采集全部锚 → dumps。增量落盘（10-18 教训：采完才落盘，中断=全丢）：
    每锚采集后即整体写 cache_path；done_urls 里的锚（当日已留档）断点跳过。
    慢买预算耗尽时**只停慢买轨**，gwdang 免费锚照采（跨渠道独立账）。
    mmb=None（预算尽降级/离线）同理：慢买锚全部顺延。"""
    done_urls = done_urls or set()
    todo = [x for x in registry["listings"] if x["url"] not in done_urls]
    n_gwd = sum(1 for x in todo if (x.get("source") or "manmanmai") == "gwdang")
    if mmb is None:
        print(f"── 采集 {len(registry['listings'])} 锚"
              f"（慢买轨不可用：{len(todo) - n_gwd} 锚顺延 / gwdang 顶班 {n_gwd}）──")
    else:
        print(f"── 采集 {len(registry['listings'])} 锚"
              f"（慢买 {len(todo) - n_gwd} / gwdang 顶班 {n_gwd}）──")
    if done_urls:
        print(f"[{_ts()}] ↩ 断点续采：{len(done_urls)} 锚当日已有留档，跳过")
    dumps = {"fetched_at": datetime.now(timezone.utc).isoformat(),
             "listings": list(prev_listings or [])}

    def _flush():
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(dumps, ensure_ascii=False, indent=1),
                                  encoding="utf-8")

    _flush()
    stopped = 0
    mmb_starved = False
    for i, it in enumerate(todo):
        src = it.get("source") or "manmanmai"
        if src != "gwdang" and (mmb is None or mmb_starved):
            stopped += 1
            continue            # 慢买不可用/预算尽：慢买锚顺延，gwdang 免费锚照采
        if src == "gwdang":
            # gwdang 轨：自己的 Pacer 节流，不占 mmb 预算（单写者按渠道分账）
            _gwd().pacer.wait()
        elif gate is not None:
            # 治理闸门（R1/R2/R4 机制化）：拟人间隔+批间休息+预算扣减，超了 fail-closed
            gate.wait()
            if not gate.spend(1, it.get("sp_name", "")):
                mmb_starved = True
                print(f"[{_ts()}] ⛔ 今日慢买预算已尽：剩余慢买锚顺延明天"
                      f"（gwdang 锚照采，已采数据已增量落盘）")
                stopped += 1
                continue
        elif i:
            # 节流=区间内随机（默认 2~7s）：固定间隔是可被识别的机器指纹，
            # 随机化更像人；区间上限可按需放宽（--sleep-max）
            time.sleep(random.uniform(*sleep_range))
        name = it["sp_name"][:30]
        try:
            trend = (_gwd().trend(it["url"]) if src == "gwdang"
                     else mmb.trend(it["url"]))
        except SessionExpired:
            raise   # 全局会话死，整批无意义，交上层提示人工 login
        except Exception as e:
            # 单 listing 瞬时失败（风控/空响应）只跳过本轮，不炸整批
            print(f"[{_ts()}] ✗ {name} · 瞬时失败：{e}")
            continue
        n = len(trend.get("datePrice") or [])
        if not n:
            # 0 点可能是瞬时空响应（10-18 实证：270K Plus 首采 0 点，
            # 手动重打同接口 207 点）→ 退避重试一次再定论
            time.sleep(random.uniform(6, 10))
            try:
                trend = (_gwd().trend(it["url"]) if src == "gwdang"
                         else mmb.trend(it["url"]))
                n = len(trend.get("datePrice") or [])
            except Exception:
                pass
        dumps["listings"].append({
            "identity": it["identity"], "url": it["url"],
            "sp_name": it["sp_name"], "channel": it["channel"],
            "vendor": it.get("vendor") or "", "source": src,
            "trend": trend})
        _flush()                # 增量落盘：中断已采部分不丢
        if n:
            print(f"[{_ts()}] ✓ {name} · {n} 点{'（gwdang）' if src == 'gwdang' else ''}")
        else:
            print(f"[{_ts()}] ✗ {name} · 0 点（listing 疑似失效，跳过）")
    if stopped:
        print(f"[{_ts()}] ■ 慢买预算顺延 {stopped} 锚（明天自动续）")
        _notify_feishu(f"📱 慢买日预算已用尽 · {stopped} 锚顺延明天 · "
                       f"想今天采完请到 OpenCode 授权临时调高预算")
    return dumps


def run(date_dir: str, root: Path = ROOT, from_cache: str | None = None,
        dry_run: bool = False, skip_ingest: bool = False,
        sleep_min: float = 5.0, sleep_max: float = 20.0,
        only: str | None = None, gate: "ChannelGate | None" = None,
        gwd_only: bool = False, mmb_blocked: bool = False,
        urls: str | None = None) -> int:
    # 契约/注册表属代码资产，固定从模块 ROOT 读；root 只重定向产物
    contract = Contract.load(ROOT, "hardware")
    registry = load_registry(contract)
    if gwd_only:
        # 只采 gwdang 顶班锚（不占慢买预算，10-17 重放用）
        registry["listings"] = [it for it in registry["listings"]
                                if (it.get("source") or "manmanmai") == "gwdang"]
        if not registry["listings"]:
            print("--gwd-only 未命中任何 gwdang listing")
            return 2
    if only:
        registry["listings"] = [it for it in registry["listings"]
                                if only in it["url"] or only in it["sp_name"]]
        if not registry["listings"]:
            print(f"--only {only!r} 未命中任何 listing")
            return 2
    if urls:
        url_set = {u.strip() for u in urls.split(",") if u.strip()}
        registry["listings"] = [it for it in registry["listings"]
                                if it["url"] in url_set]
        if not registry["listings"]:
            print("--urls 未命中任何 listing（确认 URL 已入注册表）")
            return 2

    if from_cache:
        dumps = json.loads(Path(from_cache).read_text(encoding="utf-8"))
        print(f"[cache] {from_cache}（{len(dumps['listings'])} listing）")
    else:
        mmb = None
        if not gwd_only and not mmb_blocked:
            mmb = Mmb()
            if not mmb.load_session():
                print("未登录：先执行 python -m tools.manmanbuy_backfill login")
                return 2
        if mmb_blocked:
            print(f"[{_ts()}] ⛔ 慢买轨今日不可用（预算尽）：{sum(1 for it in registry['listings'] if (it.get('source') or 'manmanmai') != 'gwdang')} 锚顺延明天，gwdang 免费锚照采")
        print(f"[{_ts()}] ▶ 开始采集（{len(registry['listings'])} 锚）")
        cache_path = root / "artifacts" / date_dir / "backfill_manmanbuy_raw.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 断点续采（10-18 教训：两次中断 48 发全丢）：当日缓存里已有的锚跳过
        prev_listings: list = []
        done_urls: set = set()
        if cache_path.exists():
            try:
                prev_listings = (json.loads(cache_path.read_text(encoding="utf-8"))
                                 .get("listings") or [])
                reg_urls = {it["url"] for it in registry["listings"]}
                done_urls = {x["url"] for x in prev_listings} & reg_urls
                prev_listings = [x for x in prev_listings if x["url"] in done_urls]
            except Exception:
                prev_listings, done_urls = [], set()
        dumps = _fetch_dumps(mmb, registry, (sleep_min, sleep_max),
                             gate=None if mmb_blocked else gate,
                             cache_path=cache_path, done_urls=done_urls,
                             prev_listings=prev_listings)
        cache_path.write_text(json.dumps(dumps, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        # 无上限模式的观测哨：日发 ≥120 提醒一次（不阻断）
        if gate is not None and gate.budget is None and gate.spent() >= 120:
            marker = root / "artifacts" / date_dir / ".budget_notify_done"
            if not marker.exists():
                _notify_feishu(f"📱 慢买今日已发 {gate.spent()} 发（无上限模式观测哨）·"
                               f"节奏红线仍生效·出风控信号会自动回退 80 上限")
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(_ts(), encoding="utf-8")
        print(f"[{_ts()}] ◇ raw 留档 -> {cache_path}")

    obs_path = root / "artifacts" / date_dir / "t1_sku_observations.json"
    existing = json.loads(obs_path.read_text(encoding="utf-8")) if obs_path.exists() else []
    # 注册表是真源：缓存/留档里缺的 channel/vendor/anchor 按 URL 从注册表补齐
    reg_by_url = {it["url"]: it for it in registry["listings"]}
    total_added = 0
    for it in dumps["listings"]:
        reg = reg_by_url.get(it["url"]) or {}
        it = {**it, **{k: v for k, v in reg.items() if v and not it.get(k)}}
        # per-listing 数据源（gwdang 顶班轨优先，缺省慢买）
        src = it.get("source") or registry["source"]
        try:
            series = trend_to_series(it["trend"], it, src)
        except ValueError as e:
            print(f"[{_ts()}] − {it['sp_name'][:28]} · 无价格点未入库（{e}）")
            continue
        extra = ({"vendor": it["vendor"]} if it.get("vendor") else {}) \
            | ({"anchor": it["anchor"]} if it.get("anchor") else {})
        rows = convert(series, entity_hint=it["identity"],
                       source=src, channel=it["channel"],
                       extra_attrs=extra)
        # 促销事件逐日判定（convert 是批量口径，逐行覆写 optional 维度）
        for row, remark in zip(rows, series["remarks"]):
            row["attrs_hint"]["promo_event"] = promo_from(remark)
        rows = dedupe_rows(rows, existing)
        if not dry_run:
            existing = existing + rows
            append_rows(obs_path, rows)
        total_added += len(rows)
        print(f"[{_ts()}] + {it['sp_name'][:28]} · +{len(rows)} 行"
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
    # 人话版入库统计（JSON 明细在 stats 文件里）
    print(f"[{_ts()}] ◆ 入库：新 {stats.get('ingested', 0)} / 更新 {stats.get('updated', 0)}"
          f" / 隔离 {stats.get('quarantined', 0)} / 无实体 {stats.get('no_entity', 0)}")
    print(f"[{_ts()}] ◆ stats 明细 -> {stats_path}")

    # ── 总结一行看全 ──
    got = sum(1 for x in dumps["listings"] if x.get("trend", {}).get("datePrice"))
    zero = len(dumps["listings"]) - got
    if from_cache:
        budget_txt = "cache"
    else:
        budget_txt = (f"预算 {gate.spent()}/{gate.budget}" if gate else
                      f"节奏 {sleep_min}~{sleep_max}s")
    print(f"{'─' * 28} 总结 {'─' * 28}")
    print(f"[{_ts()}] ■ 采集 {len(dumps['listings'])} 锚（有曲线 {got} / 无点 {zero}）"
          f" ｜ 行 +{total_added} ｜ 入库 新{stats.get('ingested', 0)} 隔离{stats.get('quarantined', 0)}"
          f" ｜ {budget_txt}")
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

GOOD_SITEIDS = {"1", "10"}          # 1=京东 10=天猫（discover 只取京东；天猫 uland 转链 v2 再议）
_ANCHOR_SLEEP = (2.0, 7.0)


def _norm_token(s: str) -> str:
    """标题/规格归一：小写、去空格、8GB→8G、×/*→x（内存套装容量三种写法
    「16G×2 / 16g*2 / 16GX2」归一成 16gx2，2026-10-15 内存品类缺口），
    供子串匹配。"""
    return (s or "").lower().replace(" ", "").replace("gb", "g") \
        .replace("×", "x").replace("*", "x")


def _match_spec(title: str, series: str, spec: str) -> bool:
    t = _norm_token(title)
    s = _norm_token(series)
    if s not in t or _norm_token(spec) not in t:
        return False
    # 后缀串台防线（2026-10-14 冒烟实测抓到：5070Ti 混进 RTX 5070 桶——
    # 归一化后 rtx5070ti 包含 rtx5070 子串）。系列名紧跟下列后缀判定为
    # 异款；系列名自身带该后缀（如 RTX 5090 D、RX 9070 XT/GRE）不拦。
    i = t.find(s)
    if i != -1:
        rest = t[i + len(s):]
        for suf in ("ti", "super", "pro", "v2", "d", "xt", "gre"):
            if not s.endswith(suf) and rest.startswith(suf):
                return False
    # 多型号堆砌防线（2026-10-15 v5 批量实测抓到）：标题塞多个型号的
    # 混装/堆砌页不是单卡 listing——「RTX4090…3090涡轮…4080S…RTX509」
    # 「RTX4060/4070/4070Super/…」两类。判据：斜杠相邻型号，或 ≥2 个
    # 带前缀（rtx/gtx/rx）的不同型号。
    if re.search(r"\d{3,4}\w{0,6}/\s*\d{3,4}", t):
        return False
    models = set(re.findall(r"(?:rtx|gtx|rx)\s?\d{3,4}\w{0,4}", t))
    if len(models) >= 2:
        return False
    return True


# 整机词黑名单（R28 教训的采集侧防线）：标题含这些词 = 整机/工作站/
# 关键词堆砌页，不是单卡 listing——RTX 5090 桶曾搜出联想工作站。
# 2026-10-15 补充：服务器 入列；「台式机显卡/电脑独显」是单卡的堆砌
# 写法（讯景雪狼 9070 销量 5000 曾被误杀），紧跟显卡/独显时不算整机。
_HOSTWORDS = ("主机", "整机", "工作站", "笔记本", "服务器",
              "台式机", "电脑", "水冷", "显示器", "电源")


def _is_host(title: str) -> bool:
    t = title or ""
    # 机器词紧跟（独）显卡 → 单卡堆砌写法，先摘除再判
    t = re.sub(r"(台式机|台式电脑|电脑|台式)(?=独?显卡)", "", t)
    return any(w in t for w in _HOSTWORDS)


def _fail_label(e: SessionExpired) -> str:
    """SessionExpired 文案区分（操作手册 §2 信号处置表，2026-10-16 红线版）：
    4030 = **已被封**——立即停止一切慢慢买流量（探测也是消费），联系客服，
    勿重试/换 IP/重登；只有 402/未登录才是真会话失效（可自动 relogin）。"""
    if "4030" in str(e):
        return ("⛔⛔⛔ 已触发封禁（4030）——立即停止一切慢慢买流量"
                "（探测也是消费！），勿重试勿换 IP，联系客服解封："
                "账号 15850730219。详情见《慢慢买操作手册.md》§2")
    return f"会话失效：{e}"


# 锚的 URL 形态（2026-10-14 实测收敛）：搜索结果没有京东数字 SKU
# （skuid/itemid 都是慢慢买内部编码，gourl/union 链全是客户端 JS 跳转，
# 服务端解析无解）——而慢慢买曲线的键就是它爬虫认识的 URL 本身
# （prourl/jingfen 链实测 410 点全曲线）。所以：
#   采集键 url = prourl（jingfen.jd.com/detail/...）
#   溯源 shop_url = gourl（redirectjd.aspx，官方跳转页，浏览器直达商品页）
# 注册表校验用顶部同规则的 JD_ITEM。


def _jd_url(it) -> str:
    """京东站候选的采集键：prourl 优先，gourl 兜底；非京东站返回空。"""
    if str(it.get("siteid") or "") != "1":
        return ""
    return it.get("prourl") or it.get("gourl") or ""


def discover(series: str, spec: str, keyword: str | None = None,
             category: str = "显卡", top_n: int = 5) -> dict:
    """双锚候选发现：search 驱动，输出候选表（含试采点数）。

    keyword 默认按 R28 搜索规范拼（大类前缀+系列+规格）；人工复核后
    把打勾的行誊进 config/backfill_manmanbuy.yaml。

    锚的硬门槛（2026-10-14 批量实测教训的防线）：
    * 仅京东站（siteid=1）——天猫 uland 转链页溯源不达标，v2 再议；
    * 标题不含整机词（联想工作站曾混进 RTX 5090 桶）；
    * 销量锚取 sales 第一、价格锚要求 sales>0（¥2121 的 0 销量关键词
      堆砌页曾混进 5060 Ti 16GB 最低价）；
    * URL：采集键=prourl（jingfen 链，慢慢买爬虫原生键）、溯源=gourl
      （官方跳转页）——skuid/itemid 是内部编码构造不出 item.jd.com，
      短链重定向又被京东风控页截住（v2 批量实测）。
    """
    if not spec:
        raise ValueError("spec 不能为空（--spec，如 8GB/16GB）")
    kw = keyword or f"{category} {series} {spec}"
    mmb = Mmb()
    if not mmb.load_session():
        raise SessionExpired("未登录：先执行 python -m tools.manmanbuy_backfill login")

    def _ok(it, need_sales=False):
        url = _jd_url(it)
        if not url or _is_host(it.get("title") or ""):
            return False
        if not _match_spec(it.get("title") or "", series, spec):
            return False
        return (not need_sales) or mmb._num(it.get("sales")) > 0

    tops = mmb.search_top(kw, top_n)
    time.sleep(random.uniform(*_ANCHOR_SLEEP))
    lows = mmb.compare_price(kw, 12)
    sales_anchor = next(filter(_ok, tops), None)
    price_anchor = next((it for it in lows if _ok(it, need_sales=True)
                         and it.get("prourl") != (sales_anchor or {}).get("prourl")), None)

    out = {"keyword": kw, "series": series, "spec": spec,
           "generated_at": datetime.now(timezone.utc).isoformat(),
           "candidates": {}}
    for tag, it in (("sales", sales_anchor), ("price", price_anchor)):
        if not it:
            out["candidates"][tag] = None
            print(f"[{tag}] 无合格候选（京东站+规格匹配+整机词过滤后为空）")
            continue
        url = _jd_url(it)
        time.sleep(random.uniform(*_ANCHOR_SLEEP))
        try:
            trend = mmb.trend(url)
            if not isinstance(trend, dict):
                raise ValueError(f"trend 返回非 dict: {type(trend).__name__}")
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
            "url": url,                                    # 采集键（prourl）
            "shop_url": it.get("gourl") or url,            # 溯源（官方跳转页）
            "trend_points": pts,
            "current_price": cur}
        c = out["candidates"][tag]
        print(f"[{tag}] {c['sp_name']} | ¥{c['price']} 销量:{c['sales']}"
              f" | {c['url'][:48]} | 曲线 {pts} 点")
    return out


def _price_num(price) -> float:
    """『￥3,799.00』→ 3799.0；解析不了返回 inf（排序沉底）。"""
    m = re.sub(r"[^0-9.]", "", str(price or ""))
    try:
        return float(m)
    except ValueError:
        return float("inf")


def discover_cheap(series: str, spec: str, keyword: str,
                   pages: int = 2, top_cheap: int = 3,
                   min_points: int = 50,
                   sleep_range: tuple[float, float] | None = None,
                   match: bool = True, reject: str | None = None) -> dict:
    """价格锚发现（2026-10-15）：search 结果里挑最便宜的合格单卡。

    为什么不走 compare_price：该接口条目**没有 prourl**（无法做趋势键）
    且标题多为堆砌页——实测解剖见 2026-10-15 会话记录。search 条目带
    prourl 且曲线完整，最低价的合格单卡即是可持续的价格锚。

    sleep_range：试采间隔覆写（默认 _ANCHOR_SLEEP 2~7s；被 trend 风控
    限流时传 (30,60) 之类慢速档）。
    match=False：跳过 _match_spec 规格校验（CPU/内存/SSD/主板等品类的
    series 全名在标题里匹配不上，靠 keyword 搜索 + 人工遴选兜底）。
    reject：标题命中该正则的候选直接排除（如 CPU 的 r"主板|套装|整机"）。

    返回 {"keyword", "series", "spec", "candidates": [...]}，candidates
    按价格升序，每条含试采点数（< min_points 丢弃）。
    """
    sl = sleep_range or _ANCHOR_SLEEP
    mmb = Mmb()
    if not mmb.load_session():
        raise SessionExpired("未登录：先执行 python -m tools.manmanbuy_backfill login")

    seen, items = set(), []
    for page in range(1, pages + 1):
        rows = mmb.search(keyword, page)
        for it in rows or []:
            u = _jd_url(it)
            if not u or u in seen:
                continue
            seen.add(u)
            t = it.get("title") or ""
            # 整机词只在 match=True（显卡路径）生效：非显卡品类（match=False）
            # 的标题里「台式机/电脑」是正常描述词（台式机内存条），整机过滤
            # 靠 reject 正则（2026-10-16 预扫实测：_is_host 误杀全部内存候选）。
            if match and _is_host(t):
                continue
            if match and not _match_spec(t, series, spec):
                continue
            if reject and re.search(reject, t):
                continue
            if mmb._num(it.get("sales")) <= 0:
                continue
            items.append({**it, "url": u})
        if items:
            break                     # 首页有货就不再翻页（控制请求量）
        time.sleep(random.uniform(*sl))

    items.sort(key=lambda it: _price_num(it.get("price")))
    cands = []
    for it in items[:top_cheap]:
        time.sleep(random.uniform(*sl))
        try:
            trend = mmb.trend(it["url"])
            if not isinstance(trend, dict):
                raise ValueError("trend 返回非 dict")
            pts = len(trend.get("datePrice") or [])
            sp_name = (trend.get("spName") or it.get("title") or "")[:40]
            cur = trend.get("currentPrice")
        except SessionExpired:
            raise
        except Exception:
            pts, sp_name, cur = 0, (it.get("title") or "")[:40], None
        if pts < min_points:
            print(f"  · 廉价候选点数不足（{pts}<{min_points}）：{sp_name[:36]}")
            continue
        cands.append({
            "anchor": "price", "title": (it.get("title") or "")[:60],
            "sp_name": sp_name, "mall": it.get("mall"),
            "siteid": str(it.get("siteid") or ""),
            "price": it.get("price"), "sales": it.get("sales"),
            "url": it["url"], "shop_url": it.get("gourl") or it["url"],
            "trend_points": pts, "current_price": cur})
        print(f"  ✓ 价格锚候选: {sp_name[:36]} | ¥{it.get('price')} "
              f"销量:{it.get('sales')} | {pts} 点")
    return {"keyword": keyword, "series": series, "spec": spec,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "candidates": cands}


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
    rn.add_argument("--urls", default=None,
                    help="逗号分隔的精确 URL 列表（立即采集：只采这些锚，"
                         "2026-10-19 新增 SKU 用户旅程）")
    rn.add_argument("--gwd-only", action="store_true",
                    help="只采 gwdang 顶班锚（source=gwdang，不占慢买预算）")
    rn.add_argument("--budget", type=int, default=None,
                    help="今日慢买预算上限（默认无上限：10-18 双批实证 72→92 零风控，"
                         "用户授权移除；节奏红线 R4 不变，≥120 发/天飞书提醒；"
                         "出现风控信号立即恢复 80）")
    rn.add_argument("--sleep-min", type=float, default=5.0,
                    help="节流随机下限秒（默认 5）")
    rn.add_argument("--sleep-max", type=float, default=20.0,
                    help="节流随机上限秒（默认 20；固定间隔是机器指纹，随机化更像人）")
    dv = sub.add_parser("discover", help="双锚候选发现（search 驱动，产出候选表）")
    dv.add_argument("--series", required=True, help="如 'RTX 5070'")
    dv.add_argument("--spec", required=True, help="如 12GB")
    dv.add_argument("--category", default="显卡")
    dv.add_argument("--keyword", default=None, help="覆盖默认搜索词（默认 R28 规范拼接）")
    dv.add_argument("--top-n", type=int, default=5)
    dv.add_argument("--out", default=None, help="候选表落盘路径（默认 artifacts/今日/backfill_discover_*.json）")
    args = ap.parse_args(argv)
    if args.cmd == "login":
        with ChannelGate("mmb").session("login"):
            return login(args.user)
    if args.cmd == "discover":
        gate = ChannelGate("mmb", budget=80, log=_stamp)
        try:
            with gate.session("discover"):
                result = discover(args.series, args.spec, keyword=args.keyword,
                                  category=args.category, top_n=args.top_n)
        except SessionExpired as e:
            print(_fail_label(e))
            return 2
        out = args.out or str(ROOT / "artifacts" / time.strftime("%Y-%m-%d")
                              / f"backfill_discover_{args.series}_{args.spec}.json".replace(" ", ""))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        print(f"候选表 -> {out}（人工核定 channel 后誊入 config/backfill_manmanbuy.yaml）")
        return 0
    root = Path(args.root) if args.root else ROOT
    if args.from_cache:
        # 离线重放：零 mmb 流量，不占锁不占预算（测试走这里）
        return run(args.date_dir, root=root, from_cache=args.from_cache,
                   dry_run=args.dry_run, skip_ingest=args.skip_ingest,
                   sleep_min=args.sleep_min, sleep_max=args.sleep_max,
                   only=args.only, gwd_only=args.gwd_only,
                   urls=args.urls)
    if args.gwd_only:
        # gwd 独立管线：不碰慢买锁/预算——跨渠道允许并行（串行只限平台内）
        return run(args.date_dir, root=root, from_cache=args.from_cache,
                   dry_run=args.dry_run, skip_ingest=args.skip_ingest,
                   sleep_min=args.sleep_min, sleep_max=args.sleep_max,
                   only=args.only, gwd_only=args.gwd_only,
                   urls=args.urls)
    gate = ChannelGate("mmb", budget=args.budget, log=_stamp)   # R2：None=无上限（节奏红线仍在）
    try:
        with gate.session("run 采集"):
            return run(args.date_dir, root=root, from_cache=args.from_cache,
                       dry_run=args.dry_run, skip_ingest=args.skip_ingest,
                       sleep_min=args.sleep_min, sleep_max=args.sleep_max,
                       only=args.only, gate=gate, gwd_only=args.gwd_only,
                       urls=args.urls)
    except BudgetExhausted:
        # 预算入口即尽（2026-10-18 实例：session() 进入即 fail-closed 抛异常，
        # 把 gwdang 免费轨和缓存入库也炸掉了）→ 降级：慢买轨顺延，零消费部分照做
        print("[mmb] 今日预算已尽：慢买轨顺延明天，gwdang 免费锚 + 已缓存数据照常处理")
        _notify_feishu(f"📱 慢买日预算已用尽（{gate.budget}/{gate.budget}）· "
                       f"链降级续跑（gwdang/入库照常）· 想今天继续采慢买请到 OpenCode 授权调高")
        return run(args.date_dir, root=root, dry_run=args.dry_run,
                   skip_ingest=args.skip_ingest,
                   sleep_min=args.sleep_min, sleep_max=args.sleep_max,
                   only=args.only, gwd_only=args.gwd_only, mmb_blocked=True,
                   urls=args.urls)
    except SessionExpired as e:
        print(_fail_label(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
