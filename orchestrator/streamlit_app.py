# -*- coding: utf-8 -*-
"""SKU 价格看板 · Streamlit 版（双轨过渡 v1，2026-10-17 用户定稿）。

跑法：cd orchestrator && python -m streamlit run streamlit_app.py --server.port 8788
与旧 :8787（FastAPI+原生JS）双轨过渡，稳定后旧页退役。

设计口径（与 tools/webapp.py 保持一致）：
- 自采主序列 source IN (自采, manmanmai, gwdang) + condition=全新
- 注册表/提案 YAML 直读写（保头部注释）；锚下线 = retired 保留审计
- 人工操作免预算（只占单写者锁）
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "sku.db"
REG = ROOT / "config" / "backfill_manmanbuy.yaml"
PROP = ROOT / "data" / "manmanbuy_proposals.yaml"
TASKBOARD = ROOT / "reports" / "taskboard.json"
SOURCE_IN = "(自采, manmanmai, gwdang)"

st.set_page_config(page_title="SKU 价格看板 · Streamlit", layout="wide")

# ---------------------------------------------------------------- helpers


def ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def reg_load() -> dict:
    return yaml.safe_load(REG.read_text(encoding="utf-8")) or {}


def reg_save(reg: dict) -> None:
    old = REG.read_text(encoding="utf-8")
    head = old.split("\nlistings:", 1)[0].rstrip("\n")
    body = yaml.safe_dump({"listings": reg.get("listings") or []},
                          allow_unicode=True, sort_keys=False, width=100,
                          default_flow_style=False).split("\n", 1)[1]
    REG.write_text(head + "\nlistings:\n" + body, encoding="utf-8")


def channel_of(shop: str) -> str:
    if "自营" in shop:
        return "自营"
    if "旗舰" in shop:
        return "官旗"
    return "三方店铺"


@st.cache_data(ttl=60)
def entities_df() -> pd.DataFrame:
    con = ro()
    rows = con.execute(
        """
        SELECT e.entity_id, e.display_name,
               json_extract(e.identity_json, '$.category') cat,
               COUNT(o.id) n, MAX(o.obs_date) latest,
               MIN(o.low_price) min_p
        FROM entity e LEFT JOIN observation o ON o.entity_id = e.entity_id
        WHERE e.domain='hardware' AND e.active=1
        GROUP BY e.entity_id ORDER BY cat, e.entity_id
        """).fetchall()
    con.close()
    df = pd.DataFrame([dict(r) for r in rows])
    df.columns = ["ID", "实体", "品类", "数据点", "最新数据", "历史最低"]
    return df


@st.cache_data(ttl=60)
def entity_series(eid: int) -> list[tuple[str, float]]:
    con = ro()
    rows = con.execute(
        f"""
        SELECT obs_date, MIN(low_price) p FROM observation
         WHERE entity_id=? AND obs_date <= date('now','localtime')
           AND json_extract(attrs_json,'$.source') IN {SOURCE_IN}
           AND json_extract(attrs_json,'$.condition')='全新'
         GROUP BY obs_date ORDER BY obs_date
        """, (eid,)).fetchall()
    con.close()
    return [(r["obs_date"], float(r["p"])) for r in rows]


def entities_map() -> dict:
    """identity_json 原文 → 实体名。"""
    con = ro()
    m = {r["identity_json"]: r["display_name"]
         for r in con.execute("SELECT identity_json, display_name FROM entity "
                              "WHERE domain='hardware' AND active=1")}
    con.close()
    return m


def raw_pics() -> dict[str, str]:
    """identity 键 → 商品图（回填留档 spPic，零预算）。"""
    pics: dict[str, str] = {}
    art = ROOT / "artifacts"
    for dd in sorted((p for p in art.iterdir()
                      if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
                     reverse=True)[:3]:
        raw = dd / "backfill_manmanbuy_raw.json"
        if not raw.exists():
            continue
        try:
            data = json.loads(raw.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in data.get("listings") or []:
            tr = it.get("trend") or {}
            pic = tr.get("spPic") or (tr.get("raw") or {}).get("spPic")
            ident = it.get("identity") or {}
            key = "|".join(str(ident.get(k) or "").lower()
                           for k in ("category", "series", "spec"))
            if pic and key not in pics:
                pics[key] = pic
    return pics


@st.cache_resource
def get_mmb():
    from tools.manmanbuy_client import Mmb
    m = Mmb()
    m.load_session()
    return m


# ---------------------------------------------------------------- sidebar
PAGES = ["📊 实体总览", "⚓ 锚管理", "📝 提案审核", "🔍 搜索加锚", "🧰 任务流水"]
page = st.sidebar.radio("页面", PAGES, index=0)
st.sidebar.caption("SKU 价格管线 · Streamlit 双轨 v1\n\n数据：data/sku.db（只读）\n注册表/提案：YAML 直读写\n\n口径：自采双平台（慢买+购物党）· 全新 · 每日最低")

# ---------------------------------------------------------------- 总览
if page == PAGES[0]:
    st.title("📊 实体总览")
    df = entities_df()
    cats = ["全部"] + sorted(df["品类"].dropna().unique().tolist())
    cat = st.selectbox("品类", cats)
    view = df if cat == "全部" else df[df["品类"] == cat]
    lit = int((view["数据点"] > 0).sum())
    st.metric("点亮 / 总数", f"{lit} / {len(view)}")
    st.dataframe(view, use_container_width=True, height=430, hide_index=True)

    st.subheader("实体曲线")
    name = st.selectbox("选择实体", view["实体"].tolist())
    if name:
        eid = int(view.loc[view["实体"] == name, "ID"].iloc[0])
        series = entity_series(eid)
        if series:
            dates, prices = zip(*series)
            fig = go.Figure(go.Scatter(x=list(dates), y=list(prices),
                                       mode="lines", name=name))
            fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10),
                              yaxis_title="每日最低价（自采双平台）")
            st.plotly_chart(fig, use_container_width=True)
            last, prev = prices[-1], prices[-8] if len(prices) >= 8 else prices[0]
            c1, c2, c3 = st.columns(3)
            c1.metric("最新价", f"¥{last:g}")
            c2.metric("历史最低", f"¥{min(prices):g}")
            c3.metric("近7日", f"{(last - prev) / prev * 100:+.1f}%" if prev else "—")
        else:
            st.info("该实体暂无数据（走「⚡一键补锚」或搜索直加锚）")

# ---------------------------------------------------------------- 锚管理
elif page == PAGES[1]:
    st.title("⚓ 锚管理（注册表 listing）")
    emap = entities_map()
    reg = reg_load()
    rows = []
    for it in reg.get("listings") or []:
        ident = it.get("identity") or {}
        rows.append({
            "URL": it.get("url"), "实体": emap.get(json.dumps(ident, ensure_ascii=False),
                                                  json.dumps(ident, ensure_ascii=False)),
            "店名": it.get("vendor") or "", "渠道": it.get("channel") or "待核",
            "锚类型": it.get("anchor") or "销量",
            "来源": it.get("source") or "manmanmai",
            "商品名": (it.get("sp_name") or "")[:44],
            "已下线": bool(it.get("retired")),
        })
    show_ret = st.checkbox("显示已下线")
    view = pd.DataFrame(rows)
    if not show_ret:
        view = view[~view["已下线"]]
    st.caption(f"在册 {sum(1 for x in rows if not x['已下线'])} / 共 {len(rows)}"
               " · 直接改单元格（店名/渠道/锚类型），勾「标记下线」= retire，改完点保存")
    edited = st.data_editor(
        view, use_container_width=True, height=520, hide_index=True,
        disabled=["URL", "实体", "来源", "商品名", "已下线"],
        column_config={
            "渠道": st.column_config.SelectboxColumn(
                options=["自营", "官旗", "三方店铺", "待核"]),
            "锚类型": st.column_config.SelectboxColumn(options=["销量", "价格"]),
        })
    if st.button("💾 保存修改（含下线标记）", type="primary"):
        changed, retired_n = 0, 0
        for _, row in edited.iterrows():
            for it in reg.get("listings") or []:
                if it.get("url") != row["URL"]:
                    continue
                if it.get("retired"):
                    continue
                for field, col in (("vendor", "店名"), ("channel", "渠道"),
                                   ("anchor", "锚类型")):
                    new = row[col]
                    if new and it.get(field) != new:
                        it[field] = new
                        changed += 1
                if row["标记下线"]:
                    it["retired"] = datetime.now().strftime("%Y-%m-%d")
                    it["retired_reason"] = "人工下线（Streamlit 锚管理）"
                    retired_n += 1
        reg_save(reg)
        st.success(f"已保存：字段修改 {changed} 处 · 下线 {retired_n} 条")
        st.rerun()

# ---------------------------------------------------------------- 提案审核
elif page == PAGES[2]:
    st.title("📝 提案审核")
    items = (yaml.safe_load(PROP.read_text(encoding="utf-8")) or []) \
        if PROP.exists() else []
    pending = [(i, p) for i, p in enumerate(items)
               if (p.get("status") or "待核") == "待核"]
    st.caption(f"待核 {len(pending)} / 共 {len(items)}")
    pics = raw_pics()
    picks: list[tuple[int, str, str]] = []
    for i, p in pending:
        ident = p.get("identity") or {}
        key = "|".join(str(ident.get(k) or "").lower()
                       for k in ("category", "series", "spec"))
        img = pics.get(key)
        c_img, c_body = st.columns([1, 4])
        with c_img:
            if img:
                try:
                    st.image(img, width=120)
                except Exception:
                    st.caption("（图挂了）")
        with c_body:
            st.markdown(f"**{p.get('sp_name') or p.get('url')}**  ")
            st.caption(f"¥{p.get('price') or '?'} · {p.get('vendor') or '店名待定'}"
                       f" · {p.get('channel') or '待核'} · {p.get('remark') or ''}")
            ch = st.selectbox("渠道", ["自营", "官旗", "三方店铺"],
                              key=f"ch{i}",
                              index=["自营", "官旗", "三方店铺"].index(
                                  p["channel"]) if p.get("channel") in (
                                  "自营", "官旗", "三方店铺") else 2)
            picks.append((i, ch, p.get("vendor") or ""))
        st.divider()
    if st.button(f"✅ 批量通过 {len(pending)} 条", type="primary",
                 disabled=not pending):
        now = datetime.now().isoformat(timespec="seconds")
        for i, ch, vendor in picks:
            items[i]["status"] = "通过"
            items[i]["channel"] = ch
            if vendor:
                items[i]["vendor"] = vendor
            items[i]["reviewed_at"] = now
        PROP.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        st.success(f"已通过 {len(picks)} 条（明天自动链 transcribe 誊入注册表）")
        st.rerun()

# ---------------------------------------------------------------- 搜索加锚
elif page == PAGES[3]:
    st.title("🔍 搜索加锚（人工免预算）")
    q = st.text_input("慢慢买搜全网", placeholder="例：RTX 5070 12G")
    if st.button("搜") and q:
        with st.spinner("搜索中…"):
            rows = get_mmb().search(q)
        out = [{"title": r.get("title"), "price": r.get("price"),
                "sales": r.get("sales"), "mall": r.get("mall"),
                "skuid": r.get("skuid"),
                "jd": str(r.get("siteid")) == "1"} for r in rows]
        st.session_state["search_rows"] = out
    rows = st.session_state.get("search_rows") or []
    if rows:
        df = pd.DataFrame(rows)
        event = st.dataframe(df, use_container_width=True, height=380,
                             hide_index=True, on_select="rerun",
                             selection_mode="single-row")
        sel = event.selection.rows or []
        if sel:
            row = rows[sel[0]]
            st.info(f"选中：{(row['title'] or '')[:70]}")
            opts = entities_df()
            name = st.selectbox("归属实体", opts["实体"].tolist())
            anchor = st.radio("锚类型", ["销量", "价格"], horizontal=True)
            if st.button("⚡ 直加入注册表", type="primary"):
                from channel_gate import ChannelGate
                eid = int(opts.loc[opts["实体"] == name, "ID"].iloc[0])
                ident = ro().execute(
                    "SELECT identity_json FROM entity WHERE entity_id=?",
                    (eid,)).fetchone()[0]
                gate = ChannelGate("mmb", budget=None)
                with gate.session("streamlit 加锚"):
                    same = get_mmb().same_by_spbh(f"1|{row['skuid']}")
                shop = (same[0].get("shopName") or "").strip() if same else ""
                jd = str((same[0].get("siteSpuid") or "")).strip() if same else ""
                if not jd.isdigit():
                    st.error("该候选转不出纯数字直链，换一个")
                else:
                    reg = reg_load()
                    url = f"https://item.jd.com/{jd}.html"
                    if any(it.get("url") == url and not it.get("retired")
                           for it in reg.get("listings") or []):
                        st.warning("该商品已在注册表")
                    else:
                        reg.setdefault("listings", []).append({
                            "identity": json.loads(ident), "vendor": shop,
                            "channel": channel_of(shop), "url": url,
                            "sp_name": (row["title"] or "")[:120],
                            "anchor": anchor,
                            "verified": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "source": "manmanmai"})
                        reg_save(reg)
                        st.success(f"✅ 已入册：{name} ← {shop or '店名待定'}"
                                   f"（{channel_of(shop)}）{url}")

# ---------------------------------------------------------------- 任务流水
else:
    st.title("🧰 任务流水")
    if st.button("⚡ 一键补锚（零观测实体批量配对）"):
        from tools import taskboard
        running = [t for t in taskboard.snapshot()
                   if t.get("status") == "running"
                   and str(t.get("name", "")).startswith("一键补锚")]
        if running:
            st.warning("已在跑，看下方任务卡")
        else:
            logf = (ROOT / "reports" / "auto_anchor.log").open("ab")
            subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u",
                 str(ROOT / "scripts" / "auto_anchor.py")],
                cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            st.success("已启动，任务卡如下（点「刷新」看进度）")
    if st.button("🔄 刷新"):
        st.rerun()
    tasks = (json.loads(TASKBOARD.read_text(encoding="utf-8")).get("tasks", [])
             if TASKBOARD.exists() else [])
    for t in tasks[:8]:
        icon = {"running": "🟢", "ok": "✅", "fail": "❌"}.get(t.get("status"), "☠️")
        okn = sum(1 for s in t.get("steps", []) if s.get("status") == "ok")
        cur = next((s for s in t.get("steps", []) if s.get("status") == "running"), {})
        st.markdown(f"{icon} **{t.get('name')}** — 步骤 {okn}/{len(t.get('steps', []))}"
                    f" · {cur.get('name', t.get('current') or '')}"
                    f"{(' (' + t.get('progress', '') + ')') if t.get('progress') else ''}"
                    f"{' — ' + t['summary'] if t.get('summary') else ''}")
    st.subheader("日志尾部")
    logs = sorted((ROOT / "reports").glob("*.log"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    pick = st.selectbox("日志文件", [p.name for p in logs][:12])
    if pick:
        text = (ROOT / "reports" / pick).read_bytes().decode("utf-8", errors="replace")
        st.code("\n".join(text.splitlines()[-40:]) or "（空）", language="text")
