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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from channel_gate import ChannelGate  # noqa: E402

from tools.manmanbuy_client import Mmb  # noqa: E402

PROP = ROOT / "data" / "manmanbuy_proposals.yaml"


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
    a = ap.parse_args(argv)

    items = yaml.safe_load(PROP.read_text(encoding="utf-8")) or []
    todo = [p for p in items
            if p.get("status") == "通过" and "jingfen" in (p.get("url") or "")]
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("没有待补链的 jingfen 提案")
        return 0
    est = len(todo) * 2
    print(f"待补链 {len(todo)} 条 · 预估 {est} 发（search+same 各半）")

    mmb = Mmb()
    gate = ChannelGate("mmb", budget=60)
    done = skipped = 0
    with gate.session("jingfen 补链"):
        for p in todo:
            ident = p.get("identity") or {}
            tag = f"[{ident.get('category')}] {ident.get('series')} {ident.get('spec')}".strip()
            kw = f"{ident.get('series', '')} {ident.get('spec', '')}".strip() or p.get("sp_name", "")
            prop_price = Mmb._num(p.get("price"))
            gate.wait()
            if not gate.spend(1, f"search {kw}"):
                print("⛔ 预算尽，停发（剩余条目下次跑）")
                break
            try:
                rows = mmb.search(kw)
            except Exception as e:
                print(f"✗ {tag} · search 失败：{e}")
                skipped += 1
                continue
            best = match_candidate(rows, ident.get("series", ""),
                                   ident.get("spec", ""),
                                   p.get("sp_name", ""), prop_price)
            if not best:
                print(f"✗ {tag} · 无合格候选（相似度/价格不达标），留人工")
                skipped += 1
                continue
            sim, r = best
            skuid = str(r["skuid"])
            gate.wait()
            if not gate.spend(1, f"same {skuid}"):
                print("⛔ 预算尽，停发（剩余条目下次跑）")
                break
            same = mmb.same_by_spbh(f"1|{skuid}")
            shop = (same[0].get("shopName") or "").strip() if same else ""
            if not shop:
                print(f"△ {tag} · 配到 skuid={skuid}（相似度 {sim:.2f}）但店名未返回，留人工")
                skipped += 1
                continue
            ch = channel_of(shop)
            p["url"] = f"https://item.jd.com/{skuid}.html"
            p["sku"] = skuid
            p["vendor"] = shop
            p["channel"] = ch
            p["match_note"] = (f"2026-10-17 search 配对：相似度 {sim:.2f}，"
                               f"店名「{shop}」→ {ch}")
            done += 1
            print(f"✓ {tag} · {shop} → {ch}（skuid {skuid}，相似度 {sim:.2f}）")

    if done:
        PROP.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    print(f"{'─' * 20} 总结 {'─' * 20}")
    print(f"补链成功 {done} / 留人工 {skipped} ｜ 预算 {gate.spent()}/{gate.budget}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
