# -*- coding: utf-8 -*-
"""一键补锚：为「零观测」实体批量生成提案（看板开关调用，2026-10-17）。

目标 = active 实体中 observation 计数为 0 的（从未有锚 / 锚已死）。
每实体 2 发慢买预算：search（相似度配对）→ same_by_spbh 取 siteSpuid
拼纯数字京东直链 → 生成 status=待核 的提案，进看板审核卡（人工把关
型号配对正确性）。看板 POST /api/mmb/auto-anchor 以子进程拉起本脚本；
进度写任务台账，「任务流水」面板可见。预算尽 fail-closed，剩余实体
下次再点（或明天自动）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402

from channel_gate import ChannelGate  # noqa: E402
from tools.manmanbuy_client import Mmb  # noqa: E402
from prefill_jingfen import channel_of, match_candidate  # noqa: E402

PROP = ROOT / "data" / "manmanbuy_proposals.yaml"


def _stamp(msg: str) -> None:
    t = time.time()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))}."
          f"{int(t % 1 * 1000):03d}] {msg}", flush=True)


def find_targets() -> list[dict]:
    """零观测实体 → 补锚目标（含搜索关键词）。"""
    con = sqlite3.connect(ROOT / "data" / "sku.db")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT e.entity_id, e.display_name, e.identity_json
          FROM entity e
         WHERE e.domain = 'hardware' AND e.active = 1
           AND NOT EXISTS (SELECT 1 FROM observation o
                            WHERE o.entity_id = e.entity_id)
         ORDER BY e.entity_id
        """).fetchall()
    out = []
    for r in rows:
        ident = json.loads(r["identity_json"] or "{}")
        kw = f"{ident.get('series', '')} {ident.get('spec', '')}".strip() \
            or r["display_name"]
        out.append({"entity_id": r["entity_id"], "display_name": r["display_name"],
                    "identity": ident, "kw": kw})
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="auto_anchor")
    ap.add_argument("--budget", type=int, default=80,
                    help="今日慢买预算上限（默认 80，闸门 fail-closed）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列目标实体，不发请求")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 条")
    a = ap.parse_args(argv)

    targets = find_targets()
    _stamp(f"零观测实体 {len(targets)} 个："
           + "、".join(t["display_name"][:18] for t in targets))
    if a.dry_run:
        return 0
    if a.limit:
        targets = targets[:a.limit]
    if not targets:
        _stamp("没有需要补锚的实体，收工")
        return 0

    items = yaml.safe_load(PROP.read_text(encoding="utf-8")) or []
    # 同实体已有待核提案 → 跳过（避免重复刷卡）
    pending_keys = {"|".join(str((p.get("identity") or {}).get(k) or "").lower()
                             for k in ("category", "series", "spec"))
                    for p in items if p.get("status", "待核") == "待核"}

    mmb = Mmb()
    if not mmb.load_session():
        _stamp("未登录：先执行 python -m tools.manmanbuy_backfill login")
        return 2
    gate = ChannelGate("mmb", budget=a.budget, log=_stamp)

    from tools import taskboard
    tb_id = taskboard.start("一键补锚（零观测实体）", ["search+same 配对"])
    total = len(targets)
    taskboard.step(tb_id, "search+same 配对", "running", progress=f"0/{total}")

    done = failed = 0
    new_props = []
    with gate.session("一键补锚"):
        for idx, t in enumerate(targets, 1):
            ident = t["identity"]
            key = "|".join(str(ident.get(k) or "").lower()
                           for k in ("category", "series", "spec"))
            tag = f"[{ident.get('category')}] {t['display_name'][:26]}"
            taskboard.step(tb_id, "search+same 配对", "running",
                           detail=tag, progress=f"{idx - 1}/{total}")
            if key in pending_keys:
                _stamp(f"△ {tag} · 已有待核提案，跳过")
                continue
            gate.wait()
            if not gate.spend(1, f"search {t['kw']}"):
                _stamp("⛔ 预算尽，停发（剩余实体下次再点）")
                break
            try:
                rows = mmb.search(t["kw"])
            except Exception as e:
                _stamp(f"✗ {tag} · search 失败：{e}")
                failed += 1
                continue
            best = match_candidate(rows, ident.get("series", ""),
                                   ident.get("spec", ""), "", None)
            if not best:
                _stamp(f"✗ {tag} · 无合格候选（相似度/价格不达标），留人工")
                failed += 1
                continue
            sim, row = best
            skuid = str(row.get("skuid") or "")
            gate.wait()
            if not gate.spend(1, f"same {skuid[:20]}"):
                _stamp("⛔ 预算尽，停发（剩余实体下次再点）")
                break
            same = mmb.same_by_spbh(f"1|{skuid}") if skuid else []
            shop = (same[0].get("shopName") or "").strip() if same else ""
            jd = str((same[0].get("siteSpuid") or "")).strip() if same else ""
            if not jd.isdigit():
                _stamp(f"✗ {tag} · 配到候选但 siteSpuid 未返回，留人工")
                failed += 1
                continue
            ch = channel_of(shop) if shop else "待核"
            new_props.append({
                "proposed_at": datetime.now().isoformat(timespec="seconds"),
                "identity": {"category": ident.get("category", ""),
                             "series": ident.get("series", ""),
                             "spec": ident.get("spec", "")},
                "anchor": "销量",
                "url": f"https://item.jd.com/{jd}.html",
                "sp_name": (row.get("title") or "")[:120],
                "vendor": shop,
                "channel": ch,
                "price": row.get("price") or "",
                "remark": f"一键补锚：相似度 {sim:.2f}（entity_id {t['entity_id']}）",
                "status": "待核",
            })
            done += 1
            _stamp(f"✓ {tag} · {shop or '店名待定'} → {ch}"
                   f"（url item.jd.com/{jd}.html，相似度 {sim:.2f}）")

    if new_props:
        PROP.write_text(yaml.safe_dump(items + new_props, allow_unicode=True,
                                       sort_keys=False), encoding="utf-8")
    taskboard.finish(tb_id, "ok" if not failed else "ok",
                     f"成提案 {done} / 留人工 {failed} · 预算 {gate.spent()}/{gate.budget}")
    _stamp("──────────────────── 总结 ────────────────────")
    _stamp(f"生成提案 {done} · 留人工 {failed} ｜ 预算 {gate.spent()}/{gate.budget}"
           f" ｜ 待核卡里审核，通过后 transcribe+回填自动收尾")
    return 0


if __name__ == "__main__":
    sys.exit(main())
