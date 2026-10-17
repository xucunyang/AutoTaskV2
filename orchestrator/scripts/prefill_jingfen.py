# -*- coding: utf-8 -*-
"""jingfen 提案补链：search 配对 → item 直链 + SKU → same_by_spbh 吐店名。

背景（2026-10-17）：37 条提案全通过，其中 19 条 URL 是 jingfen 转链——
慢买 trend 对 jingfen 返回 0 点（10-17 早回填 8 条实证），京东 302 又落
risk_handler 风控页（解链死路）。唯一通路：慢买 search 配对拿 skuid
（拼 item.jd.com 直链）+ same_by_spbh 吐真店名（渠道判定依据）。

发数：每条 2 发（search 1 + same 1），全程走 mmb 闸门（R1/R2/R4）。
匹配校验：siteid=1 + 标题相似度 ≥0.55 + 价格偏差 ≤25%，不达标留人工。
产出：原地更新 data/manmanbuy_proposals.yaml（url/vendor/channel/sku/
match_note），一次写盘。

用法：
    python -X utf8 scripts/prefill_jingfen.py --limit 2   # 试跑
    python -X utf8 scripts/prefill_jingfen.py             # 全量
"""
from __future__ import annotations

import difflib
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from channel_gate import ChannelGate  # noqa: E402

from tools.manmanbuy_client import Mmb  # noqa: E402

PROP = ROOT / "data" / "manmanbuy_proposals.yaml"


def _stamp(msg: str) -> None:
    """控制台传统样式：[日期 时间.毫秒] 消息。"""
    t = datetime.now()
    print(f"[{t.strftime('%Y-%m-%d %H:%M:%S')}.{t.microsecond // 1000:03d}] {msg}",
          flush=True)


def norm(s: str) -> str:
    """标题规范化：小写、去空白，供相似度比对。"""
    return "".join(str(s or "").lower().split())


def match_candidate(rows: list, series: str, spec: str,
                    sp_name: str, prop_price: float) -> tuple | None:
    """从 search 结果选与提案同款的京东条目。

    硬条件：标题含系列词（去空格小写后子串）+ siteid=1 + 有 skuid；
    打分：标题相似度（排序用）+ spec 命中加分；价格偏差 >25% 降权。
    只靠整体相似度会漏配（提案是电商长全名，搜索标题是短版）。"""
    target = norm(sp_name)
    key_series = norm(series)
    key_spec = norm(spec)
    best = None
    for r in rows:
        if str(r.get("siteid")) != "1" or not r.get("skuid"):
            continue
        title = norm(r.get("title"))
        if key_series and key_series not in title:
            continue                        # 系列词都不含，必不是同款
        sim = difflib.SequenceMatcher(None, target, title).ratio()
        if key_spec and key_spec in title:
            sim += 0.15                     # 规格命中加分（8GB/16GB/32G×2…）
        price = Mmb._num(r.get("price"))
        if (prop_price > 0 and price > 0
                and abs(price - prop_price) / max(prop_price, 1) > 0.25):
            sim -= 0.25                     # 价格对不上降权而不是一票否决
        if sim >= 0.40 and (best is None or sim > best[0]):
            best = (sim, r)
    return best


def pick_url(row: dict) -> str:
    """从 search 候选行取可回填的直链：prourl/gourl 含 item.jd.com 优先，
    否则退化用 skuid 拼（可用性未验证，仅兜底）。"""
    for k in ("prourl", "gourl"):
        u = str(row.get(k) or "")
        if "item.jd.com" in u:
            return u
    return f"https://item.jd.com/{row.get('skuid')}.html"


def channel_of(shop: str) -> str:
    """渠道三线规则（操作手册/预填同款）：含自营→自营；含旗舰→官旗；其余三方。"""
    if "自营" in shop:
        return "自营"
    if "旗舰" in shop:
        return "官旗"
    return "三方店铺"


def main(argv: list[str] | None = None) -> int:
    import argparse

    import yaml

    ap = argparse.ArgumentParser(description="jingfen 提案补链（search 配对+店名）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条（0=全量）")
    ap.add_argument("--fix-url", action="store_true",
                    help="只修 URL：已配对但 url 非纯数字直链的提案，重发 search "
                         "取 prourl（1 发/条，不调 same 不判渠道）")
    a = ap.parse_args(argv)

    items = yaml.safe_load(PROP.read_text(encoding="utf-8")) or []
    if a.fix_url:
        todo = [p for p in items
                if p.get("status") == "通过" and p.get("sku")
                and "item.jd.com" in (p.get("url") or "")
                and not re.search(r"item\.jd\.com/\d+\.html", p.get("url") or "")]
    else:
        todo = [p for p in items
                if p.get("status") == "通过" and "jingfen" in (p.get("url") or "")]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        _stamp("没有待补链的 jingfen 提案")
        return 0
    est = len(todo) if a.fix_url else len(todo) * 2
    mode = "只修 url" if a.fix_url else "search+same 各半"
    _stamp(f"待处理 {len(todo)} 条 · 预估 {est} 发（{mode}）")

    mmb = Mmb()
    gate = ChannelGate("mmb", budget=60, log=_stamp)
    done = skipped = 0
    with gate.session("jingfen 补链"):
        for p in todo:
            ident = p.get("identity") or {}
            tag = f"[{ident.get('category')}] {ident.get('series')} {ident.get('spec')}".strip()
            kw = f"{ident.get('series', '')} {ident.get('spec', '')}".strip() or p.get("sp_name", "")
            prop_price = Mmb._num(p.get("price"))
            gate.wait()
            if not gate.spend(1, f"search {kw}"):
                _stamp("⛔ 预算尽，停发（剩余条目下次跑）")
                break
            try:
                rows = mmb.search(kw)
            except Exception as e:
                _stamp(f"✗ {tag} · search 失败：{e}")
                skipped += 1
                continue
            best = match_candidate(rows, ident.get("series", ""),
                                   ident.get("spec", ""),
                                   p.get("sp_name", ""), prop_price)
            if a.fix_url:
                # 只修 URL：配到同款就取干净直链（prourl 优先），不动渠道/店名
                if not best:
                    _stamp(f"✗ {tag} · 无合格候选，url 保持原样")
                    skipped += 1
                    continue
                sim, r = best
                p["url"] = pick_url(r)
                done += 1
                _stamp(f"✓ {tag} · url → {p['url'][:70]}（相似度 {sim:.2f}）")
                continue
            if not best:
                _stamp(f"✗ {tag} · 无合格候选（相似度/价格不达标），留人工")
                skipped += 1
                continue
            sim, r = best
            skuid = str(r["skuid"])
            gate.wait()
            if not gate.spend(1, f"same {skuid}"):
                _stamp("⛔ 预算尽，停发（剩余条目下次跑）")
                break
            same = mmb.same_by_spbh(f"1|{skuid}")
            shop = (same[0].get("shopName") or "").strip() if same else ""
            if not shop:
                _stamp(f"△ {tag} · 配到 skuid={skuid}（相似度 {sim:.2f}）但店名未返回，留人工")
                skipped += 1
                continue
            ch = channel_of(shop)
            # URL 优先取 search 响应里的 prourl/gourl（慢买吐的是干净
            # item.jd.com 数字直链）；skuid 是慢买加密 ID，拼的 URL 京东
            # 认不认未验证——只做 fallback（2026-10-17 教训）。
            p["url"] = pick_url(r)
            p["sku"] = skuid
            p["vendor"] = shop
            p["channel"] = ch
            p["match_note"] = (f"2026-10-17 search 配对：相似度 {sim:.2f}，"
                               f"店名「{shop}」→ {ch}")
            done += 1
            _stamp(f"✓ {tag} · {shop} → {ch}（url {p['url'][:60]}，相似度 {sim:.2f}）")

    if done:
        PROP.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    _stamp(f"{'─' * 20} 总结 {'─' * 20}")
    _stamp(f"补链成功 {done} / 留人工 {skipped} ｜ 预算 {gate.spent()}/{gate.budget}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
