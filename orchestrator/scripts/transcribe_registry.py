# -*- coding: utf-8 -*-
"""提案 → 注册表誊写：status=通过 且 item.jd.com 直链的提案进 backfill 注册表。

复用提案里的 channel/vendor（补链已把店名判定带全），identity/url/sp_name/
anchor 原样带过；按 URL 去重（注册表已有的跳过）；jingfen 未补链的留出
（回填 trend 拿不到点，等补链后下批誊）。

用法：python -X utf8 scripts/transcribe_registry.py [--dry]
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REG = ROOT / "config" / "backfill_manmanbuy.yaml"
PROP = ROOT / "data" / "manmanbuy_proposals.yaml"


def main(argv: list[str] | None = None) -> int:
    dry = "--dry" in (argv or [])
    reg = yaml.safe_load(REG.read_text(encoding="utf-8"))
    props = yaml.safe_load(PROP.read_text(encoding="utf-8")) or []
    have_urls = {it.get("url") for it in reg.get("listings", [])}
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    add, skip_jf, skip_dup = [], 0, 0
    for p in props:
        if p.get("status") != "通过":
            continue
        url = p.get("url") or ""
        if "item.jd.com" not in url:
            skip_jf += 1
            continue
        if url in have_urls:
            skip_dup += 1
            continue
        ident = p.get("identity") or {}
        add.append({
            "identity": {"category": ident.get("category", ""),
                         "series": ident.get("series", ""),
                         "spec": ident.get("spec", "")},
            "vendor": (p.get("vendor") or "").strip(),
            "channel": p.get("channel") or "待核",
            "url": url,
            "sp_name": (p.get("sp_name") or "").strip(),
            "anchor": p.get("anchor") or "销量",
            "verified": now,
        })
    if not add:
        print(f"无新增（jingfen 未补链 {skip_jf} 条，注册表已有 {skip_dup} 条）")
        return 0
    print(f"新增 {len(add)} 条 listing：")
    for it in add:
        ident = it["identity"]
        print(f"  + [{ident['category']}] {ident['series']} {ident['spec']}"
              f" · {it['channel']}"
              f"{' · ' + it['vendor'] if it['vendor'] else ''} · {it['url']}")
    print(f"jingfen 未补链留出 {skip_jf} 条 · 注册表已有跳过 {skip_dup} 条")
    if dry:
        print("（--dry 未写盘）")
        return 0
    reg["listings"] = (reg.get("listings") or []) + add
    REG.write_text(yaml.safe_dump(reg, allow_unicode=True, sort_keys=False,
                                  width=100), encoding="utf-8")
    print(f"已写盘：注册表共 {len(reg['listings'])} 条 listing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
