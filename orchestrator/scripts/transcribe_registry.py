# -*- coding: utf-8 -*-
"""提案 → 注册表誊写：status=通过 且 item.jd.com 直链的提案进 backfill 注册表。

复用提案里的 channel/vendor（补链已把店名判定带全），identity/url/sp_name/
anchor 原样带过；按 URL 去重（注册表已有的跳过）；jingfen 未补链的留出
（回填 trend 拿不到点，等补链后下批誊）。

用法：python -X utf8 scripts/transcribe_registry.py [--dry]
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "config" / "backfill_manmanbuy.yaml"
PROP = ROOT / "data" / "manmanbuy_proposals.yaml"


def _brand_values() -> list:
    """契约 vendor 枚举（品牌白名单）。"""
    contract = yaml.safe_load(
        (ROOT / "config" / "contracts" / "hardware.yaml").read_text(encoding="utf-8"))
    return (contract.get("attrs", {}).get("vendor", {}) or {}).get("values", []) or []


def extract_brand(sp_name: str, shop: str, values: list) -> str:
    """店名/sp_name → 契约品牌枚举值（vendor 是品牌维度，不是店名）。
    匹配源两者拼接，含任一枚举品牌即取之；都不含留空（vendor 可空）。"""
    joined = f"{sp_name or ''}{shop or ''}"
    for b in values:
        if b and b != "公版" and b in joined:
            return b
    return ""


def main(argv: list[str] | None = None) -> int:
    dry = "--dry" in (argv or [])
    reg = yaml.safe_load(REG.read_text(encoding="utf-8"))
    props = yaml.safe_load(PROP.read_text(encoding="utf-8")) or []
    have = {it.get("url"): it for it in reg.get("listings", [])}
    brand_values = _brand_values()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    add, upgraded, skip_jf, skip_dup, skip_bad = [], [], 0, 0, 0
    for p in props:
        if p.get("status") != "通过":
            continue
        url = p.get("url") or ""
        if "item.jd.com" not in url:
            skip_jf += 1
            continue
        # 校验器同规则：纯数字 SKU 直链才可回填（慢买 search 的 skuid 是
        # 加密 ID，拼出的 URL trend 解析不了——10-17 实证，等 --fix-url 修复）
        if not re.search(r"item\.jd\.com/\d+\.html$", url):
            skip_bad += 1
            continue
        ident = p.get("identity") or {}
        listing = {
            "identity": {"category": ident.get("category", ""),
                         "series": ident.get("series", ""),
                         "spec": ident.get("spec", "")},
            "vendor": extract_brand(p.get("sp_name"), p.get("vendor"), brand_values),
            "channel": p.get("channel") or "待核",
            "url": url,
            "sp_name": (p.get("sp_name") or "").strip(),
            "anchor": p.get("anchor") or "销量",
            "verified": now,
        }
        # 补链锚（jingfen 出身）慢买多半 0 点——直接走 gwdang 顶班轨（免费）
        if "search 配对" in (p.get("match_note") or ""):
            listing["source"] = "gwdang"
        hit = have.get(url)
        if hit is not None:
            # 同 URL 已在表：只升级 vendor/channel（历史行曾以店名落 vendor）
            if (hit.get("vendor") != listing["vendor"]
                    or hit.get("channel") != listing["channel"]):
                hit["vendor"] = listing["vendor"]
                hit["channel"] = listing["channel"]
                upgraded.append(listing)
            else:
                skip_dup += 1
            continue
        add.append(listing)
    if not add and not upgraded:
        print(f"无新增（jingfen 未补链 {skip_jf} 条，坏 URL 留出 {skip_bad} 条，"
              f"注册表已有 {skip_dup} 条）")
        return 0
    for it in add:
        ident = it["identity"]
        print(f"  + [{ident['category']}] {ident['series']} {ident['spec']}"
              f" · {it['channel']}"
              f"{' · ' + it['vendor'] if it['vendor'] else ''} · {it['url']}")
    for it in upgraded:
        ident = it["identity"]
        print(f"  ↻ [{ident['category']}] {ident['series']} {ident['spec']}"
              f" · vendor/channel → {it['vendor'] or '∅'}/{it['channel']}")
    print(f"新增 {len(add)} 条 · 升级 {len(upgraded)} 条 · jingfen 留出 {skip_jf} · "
          f"坏 URL 留出 {skip_bad} · 无变化 {skip_dup}")
    if dry:
        print("（--dry 未写盘）")
        return 0
    if add:
        reg["listings"] = (reg.get("listings") or []) + add
    # 保头部注释：safe_dump 不留注释——现文件 listings: 之前的头部
    # （甄别纪律注释 + domain/source）原样保留，只重写 listings 段
    old = REG.read_text(encoding="utf-8")
    head = old.split("\nlistings:", 1)[0].rstrip("\n")
    body = yaml.safe_dump({"listings": reg.get("listings") or []},
                          allow_unicode=True, sort_keys=False, width=100,
                          default_flow_style=False).split("\n", 1)[1]
    REG.write_text(head + "\nlistings:\n" + body, encoding="utf-8")
    print(f"已写盘：注册表共 {len(reg.get('listings') or [])} 条 listing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
