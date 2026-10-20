"""webapp：sku.db 只读 Web 看板（TODO-A Web MVP）。

用户拍板的核心形态（sku-todo.md TODO-A / Q5）：
    每个 SKU 一张独立趋势图，不是榜单表格——曲线自己回答
    「比昨天贵还是便宜」，不需要文字转述。

架构（设计铁律的落地）：
- 数据源只有两个：data/sku.db（SQLite）+ artifacts/*/trend_snapshot.json；
- 本服务**只读**：连接级 `PRAGMA query_only=ON`，任何写事务在 SQLite
  层直接报错——「Web 永不写库」不靠纪律靠机制（单一入口铁律，写路径
  仍然只有 tools.sku_ingest）；
- 指标语义与报告同源：L0/序列聚合直接复用 tools.trend_calc 的函数
  （aggregate_scope 切轨过滤：condition=全新 + source=自采），保证
  Web 上看到的数字和 t4 报告一致，不是两套实现；
- 前端是单文件 index.html（ECharts CDN，无构建链）——「随时改展示
  方式」=改 HTML 刷新即生效，这正是 DB+Web 解耦的意义。

启动：
    python -m tools.webapp                # 127.0.0.1:8787（默认本机）
    python -m tools.webapp --host 0.0.0.0 # 局域网可访问（平板/手机看）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from channel_gate import BudgetExhausted, ChannelGate, LockHeld  # noqa: E402

from tools.sku_db import DB_PATH, connect  # noqa: E402
from tools.sku_ingest import Contract  # noqa: E402
from tools.trend_calc import (  # noqa: E402
    _aggregate_series as aggregate_series,
    _series_with_attrs as series_with_attrs,
    l0_metrics,
)

STATIC = Path(__file__).resolve().parent / "webapp_static"

# 后台脚本进程表（sku-cancel-spec §3.1）：webapp Popen 时入表，cancel 时取
# 结构：{task_id: {"proc": Popen, "script": str, "started_at": float}}
PROCS: dict[str, dict] = {}

app = FastAPI(title="SKU 趋势看板", docs_url=None, redoc_url=None)
# 本地静态资源（echarts 等走 /static/vendor/——消除 CDN 依赖，
# 本地加载秒出；StaticFiles 自带 etag/304 缓存）
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def ro_connect(db_path: Path | None = None) -> sqlite3.Connection:
    """只读连接：query_only 让一切写事务在 SQLite 层被拒。

    用 query_only 而非 file:...?mode=ro：WAL 库的 ro 连接在 -shm 缺失
    时会失败；query_only 在普通连接上给出同级的写保护且 WAL 兼容。
    """
    con = connect(db_path or DB_PATH)
    con.execute("PRAGMA query_only=ON")
    return con


def _as_of() -> str:
    """序列右边界=今天（与 trend_calc --date 同语义）。"""
    return date.today().isoformat()


def _latest_json(root: Path, filename: str) -> dict | None:
    """扫 artifacts/* 取最新的指定产物（verify_snap 同款「跳空」语义：
    旧日期目录里有、新日期没有 → 取有的那个）。"""
    art = root / "artifacts"
    if not art.is_dir():
        return None
    for d in sorted((p for p in art.iterdir() if p.is_dir()), reverse=True):
        f = d / filename
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def _entity_tracks(con: sqlite3.Connection, entity_id: int,
                   as_of: str) -> dict[str, list[list[float]]]:
    """vendor 分轨序列（对比叠加用）。只列有数据的 vendor。"""
    vendors = [r[0] for r in con.execute(
        """
        SELECT DISTINCT json_extract(attrs_json, '$.vendor')
          FROM observation
         WHERE entity_id = ?
           AND json_extract(attrs_json, '$.source') = '自采'
           AND json_extract(attrs_json, '$.condition') = '全新'
           AND json_extract(attrs_json, '$.vendor') IS NOT NULL
        """, (entity_id,))]
    tracks = {}
    for v in vendors:
        rows = series_with_attrs(con, entity_id, as_of,
                                 {"condition": "全新",
                                  "source": ["自采", "manmanmai", "gwdang"],
                                  "vendor": v})
        if rows:
            tracks[v] = [[d, p] for d, p in rows]
    return tracks


def _mmb_lines(con: sqlite3.Connection, entity_id: int,
               as_of: str) -> dict | None:
    """回填双曲线（品类采集规则 v1，2026-10-14 用户定稿）：
    主线 = 销量锚 listing 曲线（市场主流成交价）
    最低价线 = manmanmai 轨全体 min（全网最低地板，≥2 listing 时天然
    对冲单 listing 接盘溢价）。无回填数据返回 None。"""
    base = {"condition": "全新", "source": "manmanmai"}
    lowest = series_with_attrs(con, entity_id, as_of, base)
    if not lowest:
        return None
    sales = series_with_attrs(con, entity_id, as_of, {**base, "anchor": "销量"})
    return {
        "lowest": [[d, p] for d, p in lowest],
        "sales_main": [[d, p] for d, p in sales],
    }


def entity_points(con: sqlite3.Connection, entity_id: int,
                  obs_date: str) -> dict:
    """某实体某日的**全部原始观测**（数据点溯源，R21）。

    曲线上的一个点是当日多笔报价的 min——点开要能看到每一笔：
    来自哪家店（京东/拼多多百亿补贴…）、券后券前、采集时间与
    来源链接。「不一定是真的」的解法是让每笔可追溯，不是让用户
    信任一条聚合线。
    """
    import json as _json
    rows = con.execute(
        """
        SELECT obs_date, low_price, raw_price, metric, attrs_json,
               shop_name, shop_platform, shop_url, source_url,
               source_tier, coupons, collected_on, raw_json
          FROM observation
         WHERE entity_id = ? AND obs_date = ?
         ORDER BY low_price
        """, (entity_id, obs_date)).fetchall()
    points = []
    for r in rows:
        try:
            attrs = _json.loads(r["attrs_json"] or "{}")
        except ValueError:
            attrs = {}
        try:
            coupons = _json.loads(r["coupons"] or "[]")
        except ValueError:
            coupons = []
        # img_url（R22）不在主列里——从 raw_json 透传（采集页的
        # 商品/报价截图 URL，或本地截图的 /img/ 路径）
        try:
            raw = _json.loads(r["raw_json"] or "{}")
        except ValueError:
            raw = {}
        points.append({
            "obs_date": r["obs_date"],
            "low_price": r["low_price"], "raw_price": r["raw_price"],
            "metric": r["metric"],
            "vendor": attrs.get("vendor"), "channel": attrs.get("channel"),
            "condition": attrs.get("condition"),
            "promo_event": attrs.get("promo_event"),
            "source": attrs.get("source"),
            "shop_name": r["shop_name"], "shop_platform": r["shop_platform"],
            "shop_url": r["shop_url"], "source_url": r["source_url"],
            "source_tier": r["source_tier"], "coupons": coupons,
            "collected_on": r["collected_on"],
            "img_url": raw.get("img_url"),
        })
    return {"entity_id": entity_id, "date": obs_date, "points": points}


def build_board(con: sqlite3.Connection, root: Path | None = None,
                as_of: str | None = None) -> dict:
    """一屏全量数据（前端一次 fetch 全渲染）。"""
    as_of = as_of or _as_of()
    root = root or ROOT
    entities = []
    for row in con.execute(
            "SELECT entity_id, domain, display_name, identity_json, "
            "perf_anchor, brand FROM entity WHERE active=1 ORDER BY entity_id"):
        eid = row["entity_id"]
        series = aggregate_series(con, eid, as_of)
        metrics = l0_metrics(series)          # <5 期 → None（Q12 占位）
        try:
            identity = json.loads(row["identity_json"] or "{}")
        except ValueError:
            identity = {}
        # 二级划分（R20）：硬件按 category（显卡/CPU/内存…），
        # 房产按 city，其余回落「其他」
        entities.append({
            "entity_id": eid,
            "domain": row["domain"],
            "group": (identity.get("category") or identity.get("city")
                      or "其他"),
            "display_name": row["display_name"],
            "brand": row["brand"] or "",
            "identity": identity,
            "perf_anchor": row["perf_anchor"],
            "n_periods": len(series),
            "series": [[d, p] for d, p in series],
            "tracks": _entity_tracks(con, eid, as_of),
            "mmb": _mmb_lines(con, eid, as_of),
            "metrics": metrics,
        })
    with_data = [e for e in entities if e["n_periods"] > 0]
    return {
        "as_of_date": as_of,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {"total": len(entities), "with_data": len(with_data),
                   "l0_ok": sum(1 for e in entities if e["metrics"])},
        "snapshot": _latest_json(root, "trend_snapshot.json"),
        "recommend": _latest_json(root, "hw_recommend.json"),
        "entities": entities,
    }


@app.get("/")
def index() -> FileResponse:
    if not (STATIC / "index.html").is_file():
        raise HTTPException(500, "index.html 缺失（webapp_static/）")
    # 禁缓存：页面脚本与数据结构强耦合（2026-10-15 事故——浏览器缓存旧
    # 脚本 + 新数据结构 = 全部曲线画不出来），必须每次回源校验
    return FileResponse(STATIC / "index.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/board")
def api_board() -> dict:
    con = ro_connect()
    try:
        return build_board(con)
    finally:
        con.close()


@app.get("/api/entities")
def api_entities() -> dict:
    con = ro_connect()
    try:
        board = build_board(con)
        slim = [{k: v for k, v in e.items()
                 if k not in ("series", "tracks", "mmb")}
                for e in board["entities"]]
        return {"counts": board["counts"], "entities": slim}
    finally:
        con.close()


@app.get("/api/entity/{entity_id}/series")
def api_series(entity_id: int, all_tracks: bool = False) -> dict:
    con = ro_connect()
    try:
        row = con.execute(
            "SELECT display_name FROM entity WHERE entity_id=? AND active=1",
            (entity_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"entity {entity_id} 不存在")
        as_of = _as_of()
        out = {"entity_id": entity_id, "display_name": row["display_name"],
               "as_of_date": as_of,
               "series": [[d, p] for d, p in
                          aggregate_series(con, entity_id, as_of)],
               "metrics": l0_metrics(
                   aggregate_series(con, entity_id, as_of))}
        if all_tracks:
            out["tracks"] = _entity_tracks(con, entity_id, as_of)
        out["mmb"] = _mmb_lines(con, entity_id, as_of)
        return out
    finally:
        con.close()


@app.get("/api/entity/{entity_id}/points")
def api_points(entity_id: int, date: str) -> dict:
    """数据点溯源（R21）：某实体某日的全部原始观测（店铺/链接/券）。"""
    import re

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(422, "date 须为 YYYY-MM-DD")
    con = ro_connect()
    try:
        return entity_points(con, entity_id, date)
    finally:
        con.close()


@app.get("/img/{date}/{fname}")
def api_img(date: str, fname: str) -> FileResponse:
    """本地截图（R22）：t1 手机采集存的截图，artifacts/{date}/img/ 下。"""
    import re

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) \
            or not re.fullmatch(r"[\w.-]+", fname) \
            or ".." in fname:
        raise HTTPException(422, "bad path")
    f = ROOT / "artifacts" / date / "img" / fname
    if not f.is_file():
        raise HTTPException(404, "no such image")
    return FileResponse(f)


@app.get("/api/snapshot")
def api_snapshot() -> dict:
    snap = _latest_json(ROOT, "trend_snapshot.json")
    if snap is None:
        raise HTTPException(404, "尚无 trend_snapshot.json（先跑 t1b/trend_calc）")
    return snap


@app.get("/api/recommend")
def api_recommend() -> dict:
    rec = _latest_json(ROOT, "hw_recommend.json")
    if rec is None:
        raise HTTPException(404, "尚无 hw_recommend.json（t4 首跑后生成）")
    return rec


# ---------- 慢慢买 searchbox（品类采集规则 v1，2026-10-14 用户定稿） ----------
#
# searchbox 是规则的入口：搜关键词 → 全网价格/销量一览 → 点开历史曲线 →
# 一键把「销量锚 / 价格锚」候选写进提案文件 data/manmanbuy_proposals.yaml
# （gitignore 内）。提案**不直接进注册表**——人工核定 channel 后誊入
# config/backfill_manmanbuy.yaml，注册表仍是唯一采集依据（Web 依旧
# 不写库、不写 config）。
# 本节只有出站 HTTP（慢慢买）与提案文件写入；trend 结果缓存 10 分钟，
# 对慢慢买保持 1 req/多秒 的温柔节奏。

_mmb_client = None
_mmb_trend_cache: dict[str, tuple[float, dict]] = {}
MMB_TREND_TTL = 600
_gwd_trend_cache: dict[str, tuple[float, dict]] = {}
GWD_TREND_TTL = 1800


def _mmb():
    global _mmb_client
    if _mmb_client is None:
        from tools.manmanbuy_client import Mmb
        _mmb_client = Mmb()
        _mmb_client.load_session()   # trend 需要；search 系免登录
    return _mmb_client


def _mmb_gate() -> "ChannelGate":
    """searchbox 走同一把 mmb 闸门锁（单写者串行），但 **免预算**
    （2026-10-17 用户口径：人工逐条操作自带节流，上限只管无人值守
    批量；budget=None → session 不做预算检查）。
    backfill/发现任务持锁期间，看板的慢慢买请求会 429（渠道忙）——这是
    预期行为：当天窗口只归一个任务。"""
    return ChannelGate("mmb", budget=None)


@app.get("/api/mmb/search")
def api_mmb_search(q: str, page: int = 1) -> dict:
    """关键词搜全网商品（慢慢买 getsearchkeylist，免登录）。"""
    q = (q or "").strip()
    if not q:
        raise HTTPException(422, "q 为空")
    try:
        with _mmb_gate().session("webapp searchbox"):
            rows = _mmb().search(q, page)
    except BudgetExhausted as e:
        raise HTTPException(429, f"慢慢买今日预算已尽，明天再来：{e}")
    except LockHeld as e:
        raise HTTPException(429, f"慢慢买渠道忙（另有任务在跑）：{e}")
    except Exception as e:
        raise HTTPException(502, f"慢慢买搜索失败: {e}")
    from tools.manmanbuy_client import Mmb as _M
    results = [{
        "title": r.get("title"),
        "price": _M._num(r.get("price")),
        "mall": r.get("mall"),
        "siteid": str(r.get("siteid") or ""),
        "sales": r.get("sales"),
        "monthComment": r.get("monthComment"),
        "skuid": str(r.get("skuid") or ""),   # 加锚直用（same→siteSpuid 转直链）
        "url": r.get("prourl") or r.get("gourl") or "",
        "is_jd": str(r.get("siteid")) == "1",
    } for r in rows]
    results.sort(key=lambda x: x["price"] or 9e9)   # 价格升序展示
    return {"q": q, "page": page, "results": results}


@app.get("/api/mmb/trend")
def api_mmb_trend(url: str) -> dict:
    """单商品历史曲线预览（需登录态；结果缓存 10 分钟）。"""
    hit = _mmb_trend_cache.get(url)
    if hit and time.time() - hit[0] < MMB_TREND_TTL:
        return hit[1]
    try:
        with _mmb_gate().session("webapp trend"):
            d = _mmb().trend(url)
    except BudgetExhausted as e:
        raise HTTPException(429, f"慢慢买今日预算已尽，明天再来：{e}")
    except LockHeld as e:
        raise HTTPException(429, f"慢慢买渠道忙（另有任务在跑）：{e}")
    except Exception as e:
        raise HTTPException(502, f"慢慢买趋势失败（会话过期请命令行 login）: {e}")
    pts = d.get("datePrice") or []
    out = {
        "url": url, "spName": d.get("spName"), "siteName": d.get("siteName"),
        "currentPrice": d.get("currentPrice"), "lowerPrice": d.get("lowerPrice"),
        "lowerDate": d.get("lowerDate"), "avgPrice60": d.get("avgPrice60"),
        "points": [[time.strftime("%Y-%m-%d", time.localtime(p[0] / 1000)), p[1]]
                   for p in pts],
    }
    _mmb_trend_cache[url] = (time.time(), out)
    return out


@app.get("/api/gwd/trend")
def api_gwd_trend(url: str) -> dict:
    """购物党单商品曲线预览（gwdang 顶班渠道，与 /api/mmb/trend 输出同构）。

    免登录态、不占慢买预算与锁；结果缓存 30 分钟。"""
    import json as _json

    from tools.gwdang_client import Gwdang
    hit = _gwd_trend_cache.get(url)
    if hit and time.time() - hit[0] < GWD_TREND_TTL:
        return hit[1]
    g = Gwdang(pacer=None)        # 预览是人工单发，不需要 Pacer
    if g.load_session():
        pass                       # 有会话最好；匿名也能走 url_to_dp
    try:
        d = g.trend(url)
    except Exception as e:
        raise HTTPException(502, f"购物党趋势失败: {e}")
    pts = d.get("datePrice") or []
    out = {
        "url": url, "spName": d.get("spName"), "siteName": "购物党",
        "currentPrice": d.get("currentPrice"), "lowerPrice": d.get("lowerPrice"),
        "higherPrice": d.get("higherPrice"), "avgPrice60": None,
        "points": [[time.strftime("%Y-%m-%d", time.localtime(p[0] / 1000)), p[1]]
                   for p in pts],
        "raw_json": _json.dumps(d.get("raw") or {}, ensure_ascii=False)[:2000],
    }
    _gwd_trend_cache[url] = (time.time(), out)
    return out


REG_PATH = ROOT / "config" / "backfill_manmanbuy.yaml"


def _reg_load() -> dict:
    import yaml
    return yaml.safe_load(REG_PATH.read_text(encoding="utf-8")) or {}


def _reg_save(reg: dict) -> None:
    """保头部注释写盘（transcribe 同款：只重写 listings 段）。"""
    import yaml
    old = REG_PATH.read_text(encoding="utf-8")
    head = old.split("\nlistings:", 1)[0].rstrip("\n")
    body = yaml.safe_dump({"listings": reg.get("listings") or []},
                          allow_unicode=True, sort_keys=False, width=100,
                          default_flow_style=False).split("\n", 1)[1]
    REG_PATH.write_text(head + "\nlistings:\n" + body, encoding="utf-8")


def _channel_of(shop: str) -> str:
    """渠道三线规则（prefill_jingfen.channel_of 同款）。"""
    if "自营" in shop:
        return "自营"
    if "旗舰" in shop:
        return "官旗"
    return "三方店铺"


def _brand_from_shop(shop: str) -> str:
    """店名 → 契约品牌词（最长匹配；不含品牌词返回空=未设）。
    店名（如 'AMD京东自营旗舰店'）不能直接进注册表 vendor——
    load_registry 契约校验不过会崩掉定时链（10-19 实证）。"""
    if not shop:
        return ""
    legal = Contract.load(ROOT, "hardware").attrs["vendor"]["values"]
    return next((b for b in sorted(legal, key=len, reverse=True) if b in shop), "")


@app.get("/api/contract/enums")
def api_contract_enums() -> dict:
    """契约枚举词表（表单下拉用，10-18）：锚表单的 品牌/渠道/锚类型
    必须从这里选——自由文本会造成注册表校验不过、定时链崩（10-18 实证）。"""
    attrs = Contract.load(ROOT, "hardware").attrs
    return {"vendor": attrs["vendor"]["values"],
            "channel": attrs["channel"]["values"],
            "anchor": attrs["anchor"]["values"],
            "category": ["显卡", "CPU", "主板", "内存", "固态硬盘"]}


@app.get("/api/entity-options")
def api_entity_options() -> dict:
    """轻量实体清单（加锚下拉用）。带 identity+brand 供实体管理预填。"""
    con = ro_connect()
    rows = con.execute(
        "SELECT entity_id, display_name, identity_json, brand FROM entity "
        "WHERE domain='hardware' AND active=1 ORDER BY display_name").fetchall()
    out = []
    for r in rows:
        try:
            ident = json.loads(r["identity_json"] or "{}")
        except ValueError:
            ident = {}
        out.append({"id": r["entity_id"], "name": r["display_name"],
                    "identity": ident, "brand": r["brand"] or ""})
    return {"items": out}


@app.post("/api/mmb/add-anchor")
def api_mmb_add_anchor(payload: dict = Body(...)) -> dict:
    """搜索结果 → 直接入注册表（C of CRUD，2026-10-17 用户口径）。

    人工确认商品后一键加锚：same_by_spbh 取 siteSpuid 拼纯数字京东
    直链（jingfen/加密链三渠道不认，10-17 实证），店名判定渠道，
    identity 取自所选实体。人工操作免预算，占单写者锁串行。"""
    skuid = str(payload.get("skuid") or "").strip()
    eid = int(payload.get("entity_id") or 0)
    anchor = payload.get("anchor") or "销量"
    title = str(payload.get("title") or "")[:120]
    if not skuid or not eid:
        raise HTTPException(422, "skuid / entity_id 必填")
    if anchor not in ("销量", "价格"):
        raise HTTPException(422, "anchor 只能是 销量/价格")
    con = ro_connect()
    erow = con.execute("SELECT identity_json, display_name FROM entity "
                       "WHERE entity_id=?", (eid,)).fetchone()
    if not erow:
        raise HTTPException(404, f"实体 {eid} 不存在")
    identity = json.loads(erow["identity_json"] or "{}")

    with _mmb_gate().session("webapp 加锚"):
        same = _mmb().same_by_spbh(f"1|{skuid}")
    shop = (same[0].get("shopName") or "").strip() if same else ""
    jd = str((same[0].get("siteSpuid") or "")).strip() if same else ""
    if not jd.isdigit():
        raise HTTPException(502, "该候选转不出纯数字京东直链（siteSpuid 缺失），换一个候选")
    url = f"https://item.jd.com/{jd}.html"

    reg = _reg_load()
    listings = reg.get("listings") or []
    if any(it.get("url") == url and not it.get("retired") for it in listings):
        raise HTTPException(409, f"该商品已在注册表（active）：{url}")
    listings.append({
        "identity": identity,
        "vendor": _brand_from_shop(shop),
        "channel": _channel_of(shop),
        "url": url,
        "sp_name": title or shop,
        "anchor": anchor,
        "verified": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "manmanmai",
    })
    reg["listings"] = listings
    _reg_save(reg)
    return {"ok": True, "url": url, "vendor": _brand_from_shop(shop),
            "channel": _channel_of(shop), "entity": erow["display_name"]}


@app.get("/api/anchors")
def api_anchors() -> dict:
    """注册表全量 listing + 实体名解析（锚管理面板，R of CRUD）。"""
    con = ro_connect()
    ents = [(json.loads(r["identity_json"] or "{}"), r["display_name"],
             r["entity_id"])
            for r in con.execute("SELECT identity_json, display_name, entity_id "
                                 "FROM entity WHERE domain='hardware' AND active=1")]
    # 实体粒度观测计数（锚状态徽标）：observation.source_url 存的是慢买页面
    # 而非商品链接（ingest 口径），锚粒度归因需改 ingest——见设计文档 §11 待办
    obs_by_ent = {r["entity_id"]: r["n"] for r in con.execute(
        "SELECT entity_id, COUNT(*) AS n FROM observation GROUP BY entity_id")}
    items = []
    for it in _reg_load().get("listings") or []:
        ident = it.get("identity") or {}
        eid = next((e for ed, dn, e in ents if ed == ident), None)
        name = next((dn for ed, dn, e in ents if ed == ident), "")
        url = it.get("url")
        items.append({
            "url": url, "sp_name": (it.get("sp_name") or "")[:60],
            "vendor": it.get("vendor"), "channel": it.get("channel"),
            "source": it.get("source") or "manmanmai",
            "anchor": it.get("anchor"), "retired": it.get("retired"),
            "retired_reason": it.get("retired_reason"),
            "entity": name or json.dumps(ident, ensure_ascii=False),
            "points": obs_by_ent.get(eid, 0),
        })
    active = sum(1 for x in items if not x["retired"])
    return {"items": items, "active": active}


@app.post("/api/anchors/retire")
def api_anchors_retire(payload: dict = Body(...)) -> dict:
    """下线锚（D of CRUD；手册纪律：retire 保留审计行，不物理删除）。"""
    url = str(payload.get("url") or "")
    reg = _reg_load()
    hit = None
    for it in reg.get("listings") or []:
        if it.get("url") == url and not it.get("retired"):
            it["retired"] = datetime.now().strftime("%Y-%m-%d")
            it["retired_reason"] = str(payload.get("reason") or "人工下线")
            hit = it
    if not hit:
        raise HTTPException(404, f"未找到 active 的该 URL：{url}")
    _reg_save(reg)
    return {"ok": True, "url": url}


@app.post("/api/anchors/upsert")
def api_anchors_upsert(payload: dict = Body(...)) -> dict:
    """新增 / 修改锚（C+U of CRUD，人工入口，2026-10-18 用户要求补齐）。

    original_url 为空 = 新增；给原 URL = 就地修改（可改 URL 本身/归属实体）。
    URL 硬校验：必须是纯数字京东直链——jingfen/加密串三渠道不认（10-17 实证）。
    """
    import re

    url = str(payload.get("url") or "").strip()
    eid = int(payload.get("entity_id") or 0)
    original = str(payload.get("original_url") or "").strip()
    anchor = payload.get("anchor") or "销量"
    if not url or not eid:
        raise HTTPException(422, "url / entity_id 必填")
    sp_name = str(payload.get("sp_name") or "").strip()
    if not sp_name:
        raise HTTPException(422, "商品名（sp_name）必填——锚管理列表与提案卡显示用，"
                                 "填慢买/京东商品标题")
    # 品牌/渠道须在契约词表内：自由文本会让 load_registry 校验崩掉定时链
    # （10-18 实证：'英特尔京东自营' 写入后 06:30 链直接 ValueError）
    attrs = Contract.load(ROOT, "hardware").attrs
    vendor = str(payload.get("vendor") or "").strip()
    if vendor and vendor not in attrs["vendor"]["values"]:
        raise HTTPException(422, f"品牌 {vendor!r} 不在契约词表（请下拉选择）："
                                 f"{'、'.join(attrs['vendor']['values'])}")
    channel = str(payload.get("channel") or "").strip()
    if channel and channel not in attrs["channel"]["values"]:
        raise HTTPException(422, f"渠道 {channel!r} 不在契约词表（请下拉选择）："
                                 f"{'、'.join(attrs['channel']['values'])}")
    # URL 归一化：手机版/带参数一律转电脑版纯直链（10-20 用户实测
    # item.m.jd.com/product/<id>.html 被拒——m 站是合法商品页）
    m = re.search(r"(?:item\.jd\.com|item\.m\.jd\.com/product)/(\d+)\.html", url)
    if not m:
        raise HTTPException(422, "URL 必须是京东商品页（支持 item.jd.com/<数字>.html "
                                 "或手机版 item.m.jd.com/product/<数字>.html；"
                                 "jingfen/加密串三渠道不认，10-17 实证）")
    url = f"https://item.jd.com/{m.group(1)}.html"
    if anchor not in ("销量", "价格"):
        raise HTTPException(422, "anchor 只能是 销量/价格")
    con = ro_connect()
    erow = con.execute("SELECT identity_json FROM entity WHERE entity_id=?",
                       (eid,)).fetchone()
    con.close()
    if not erow:
        raise HTTPException(404, f"实体 {eid} 不存在")
    identity = json.loads(erow["identity_json"] or "{}")

    reg = _reg_load()
    listings = reg.get("listings") or []
    if any(it.get("url") == url and not it.get("retired")
           and it.get("url") != original for it in listings):
        raise HTTPException(409, f"该 URL 已有在册锚：{url}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if original:
        target = next((it for it in listings
                       if it.get("url") == original and not it.get("retired")), None)
        if not target:
            raise HTTPException(404, f"原锚不存在或已下线：{original}")
        target["url"] = url
        target["identity"] = identity
        for k in ("vendor", "channel", "anchor", "sp_name"):
            v = str(payload.get(k) or "").strip()
            if v:
                target[k] = v
        target["anchor"] = anchor
        target["verified"] = now
        mode = "update"
    else:
        listings.append({
            "identity": identity,
            "vendor": str(payload.get("vendor") or "").strip(),
            "channel": str(payload.get("channel") or "自营").strip(),
            "url": url,
            "sp_name": sp_name[:120],
            "anchor": anchor,
            "verified": now,
            "source": "manmanmai",
        })
        reg["listings"] = listings
        mode = "create"
    _reg_save(reg)
    return {"ok": True, "mode": mode, "url": url}


def _validate_category(v: str) -> str:
    if v not in ("显卡", "CPU", "主板", "内存", "固态硬盘"):
        raise HTTPException(422, f"未知品类：{v}（合法：显卡/CPU/主板/内存/固态硬盘）")
    return v


def _validate_brand(v: str) -> str:
    """品牌须在契约 vendor 词表内（锚表单/实体共用一套词表）。空=未设。"""
    v = (v or "").strip()
    if not v:
        return ""
    legal = Contract.load(ROOT, "hardware").attrs["vendor"]["values"]
    if v not in legal:
        raise HTTPException(422, f"品牌 {v!r} 不在契约词表，合法值：{'、'.join(legal)}")
    return v


@app.post("/api/entity/create")
def api_entity_create(payload: dict = Body(...)) -> dict:
    """新建实体（SKU CRUD 之 C，2026-10-18 用户要求补齐）。

    identity = {category, series, spec}；display 缺省 f"{series} {spec}"。
    重复（同 identity 且 active）→ 409 返回已有 entity_id。同步种子文件
    （hardware_entities.yaml），防未来 re-seed 造出重复实体。"""
    import yaml

    ident = {"category": str(payload.get("category") or "").strip(),
             "series": str(payload.get("series") or "").strip(),
             "spec": str(payload.get("spec") or "").strip()}
    if not all(ident.values()):
        raise HTTPException(422, "category / series / spec 必填")
    _validate_category(ident["category"])
    brand = _validate_brand(str(payload.get("brand") or ""))
    ident_json = json.dumps(ident, ensure_ascii=False)
    display = str(payload.get("display_name") or "").strip() \
        or f"{ident['series']} {ident['spec']}".strip()

    con = ro_connect()
    dup = con.execute("SELECT entity_id FROM entity WHERE domain='hardware' "
                      "AND identity_json=? AND active=1", (ident_json,)).fetchone()
    con.close()
    if dup:
        raise HTTPException(409, f"实体已存在（entity_id {dup['entity_id']}）")

    con = connect(DB_PATH)
    cur = con.execute(
        "INSERT INTO entity (domain, identity_json, display_name, brand, "
        "aliases, active, created_at, updated_at) VALUES ('hardware', ?, ?, "
        "?, '[]', 1, datetime('now'), datetime('now'))",
        (ident_json, display, brand))
    con.commit()
    eid = cur.lastrowid
    con.close()

    seed_path = ROOT / "config" / "contracts" / "hardware_entities.yaml"
    seed = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    entry = {"identity": ident, "display_name": display, "aliases": []}
    if brand:
        entry["brand"] = brand
    seed.setdefault("entities", []).append(entry)
    seed_path.write_text(yaml.safe_dump(seed, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
    return {"ok": True, "entity_id": eid, "display_name": display}


@app.post("/api/entity/update")
def api_entity_update(payload: dict = Body(...)) -> dict:
    """修改实体（U）：品类/系列/规格/品牌/显示名全部可改（缺省=保持原值）。
    联动同步：① 注册表中绑旧 identity 的 listing 一并换绑；
    ② 种子文件同 display_name 条目。历史观测不动（按 entity_id 归属）。"""
    import yaml

    eid = int(payload.get("entity_id") or 0)
    con = ro_connect()
    row = con.execute("SELECT identity_json, display_name, brand FROM entity "
                      "WHERE entity_id=? AND domain='hardware'", (eid,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, f"实体 {eid} 不存在")
    old_ident = json.loads(row["identity_json"] or "{}")
    old_display = row["display_name"]
    old_brand = row["brand"] or ""
    new_cat = _validate_category(str(payload.get("category") or "").strip()
                                 or old_ident.get("category", ""))
    new_series = str(payload.get("series") or "").strip() or old_ident.get("series", "")
    new_spec = str(payload.get("spec") or "").strip() or old_ident.get("spec", "")
    new_display = str(payload.get("display_name") or "").strip() or \
        f"{new_series} {new_spec}".strip()
    new_brand = _validate_brand(str(payload.get("brand") or "").strip()
                                or old_brand)
    new_ident = {"category": new_cat, "series": new_series, "spec": new_spec}
    if not all(new_ident.values()):
        raise HTTPException(422, "category / series / spec 不能为空")
    new_json = json.dumps(new_ident, ensure_ascii=False)

    con = ro_connect()
    clash = con.execute("SELECT entity_id FROM entity WHERE domain='hardware' "
                        "AND identity_json=? AND active=1 AND entity_id != ?",
                        (new_json, eid)).fetchone()
    con.close()
    if clash:
        raise HTTPException(409, f"新规格与已有实体冲突（entity_id "
                                 f"{clash['entity_id']}）")

    con = connect(DB_PATH)
    con.execute("UPDATE entity SET identity_json=?, display_name=?, brand=?, "
                "updated_at=datetime('now') WHERE entity_id=?",
                (new_json, new_display, new_brand, eid))
    con.commit()
    con.close()

    # 注册表换绑（旧 identity → 新 identity）
    reg = _reg_load()
    swapped = 0
    for it in reg.get("listings") or []:
        if it.get("identity") == old_ident:
            it["identity"] = new_ident
            swapped += 1
    if swapped:
        _reg_save(reg)
    # 种子同名条目同步
    seed_path = ROOT / "config" / "contracts" / "hardware_entities.yaml"
    seed = yaml.safe_load(seed_path.read_text(encoding="utf-8")) or {}
    for e in seed.get("entities") or []:
        if e.get("display_name") == old_display or e.get("identity") == old_ident:
            e["identity"] = new_ident
            e["display_name"] = new_display
            if new_brand:
                e["brand"] = new_brand
    seed_path.write_text(yaml.safe_dump(seed, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
    return {"ok": True, "entity_id": eid, "display_name": new_display,
            "brand": new_brand, "registry_swapped": swapped}


@app.post("/api/entity/retire")
def api_entity_retire(payload: dict = Body(...)) -> dict:
    """停用实体（D）：active=0 保历史（observation 不动）。"""
    eid = int(payload.get("entity_id") or 0)
    con = connect(DB_PATH)
    cur = con.execute("UPDATE entity SET active=0, updated_at=datetime('now') "
                      "WHERE entity_id=? AND domain='hardware'", (eid,))
    con.commit()
    con.close()
    if cur.rowcount == 0:
        raise HTTPException(404, f"实体 {eid} 不存在")
    return {"ok": True, "entity_id": eid}


@app.post("/api/mmb/propose")
def api_mmb_propose(payload: dict = Body(...)) -> dict:
    """把 searchbox 里勾选的候选追加进提案文件（同实体同锚覆盖旧提案）。"""
    ident = payload.get("identity") or {}
    if not ident.get("series") or not ident.get("spec"):
        raise HTTPException(422, "identity.series / identity.spec 必填（先选实体）")
    if not str(payload.get("url") or "").startswith("http"):
        raise HTTPException(422, "url 缺失")
    if payload.get("anchor") not in ("sales", "price"):
        raise HTTPException(422, "anchor 必须是 sales|price")
    import yaml
    rec = {"proposed_at": datetime.now().isoformat(timespec="seconds"), **payload}
    path = ROOT / "data" / "manmanbuy_proposals.yaml"
    cur = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    items = cur if isinstance(cur, list) else []
    items = [x for x in items
             if not (x.get("identity") == ident
                     and x.get("anchor") == payload.get("anchor"))]
    items.append(rec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return {"ok": True, "total": len(items)}


def _raw_pics() -> dict[str, str]:
    """identity 归一化键 → 商品图（京东图床 spPic）。

    图源 = 最近一期回填留档 backfill_manmanbuy_raw.json（trend 响应自带
    spPic，零预算）；提案的转链/直链本身拿不到图，注册表匹配不到的
    实体前端显示占位块。
    """
    import json
    import re

    pics: dict[str, str] = {}
    art_root = ROOT / "artifacts"
    if not art_root.is_dir():
        return pics
    for dd in sorted(art_root.iterdir(), reverse=True):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dd.name):
            continue
        raw = dd / "backfill_manmanbuy_raw.json"
        if not raw.exists():
            continue
        try:
            data = json.loads(raw.read_text(encoding="utf-8"))
        except Exception:
            continue

        def find_pic(o):
            if isinstance(o, dict):
                if o.get("spPic"):
                    return o["spPic"]
                for v in o.values():
                    r = find_pic(v)
                    if r:
                        return r
            elif isinstance(o, list):
                for v in o:
                    r = find_pic(v)
                    if r:
                        return r
            return None

        for it in data.get("listings", []):
            pic = find_pic(it.get("trend"))
            ident = it.get("identity") or {}
            if pic:
                key = "|".join(str(ident.get(k) or "").lower()
                               for k in ("category", "series", "spec"))
                pics[key] = pic
        if pics:
            return pics
    return {}


def _prop_path():
    return ROOT / "data" / "manmanbuy_proposals.yaml"


@app.get("/api/mmb/proposals")
def api_mmb_proposals() -> dict:
    import yaml
    path = _prop_path()
    if not path.exists():
        return {"items": [], "auto_n": 0}
    cur = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    items = cur if isinstance(cur, list) else []
    pics = _raw_pics()
    for p in items:
        ident = p.get("identity") or {}
        key = "|".join(str(ident.get(k) or "").lower()
                       for k in ("category", "series", "spec"))
        if pics.get(key):
            p["img"] = pics[key]
    # 自动预审候选数：店名已判定为自营/官旗、还挂在待核的
    auto_n = sum(1 for p in items
                 if p.get("status", "待核") == "待核"
                 and p.get("channel") in ("自营", "官旗")
                 and (p.get("vendor") or "").strip())
    return {"items": items, "auto_n": auto_n}


@app.post("/api/mmb/review/auto")
def api_mmb_review_auto() -> dict:
    """自动预审（2026-10-17 用户口径）：渠道预填时店名已判定为
    「自营/官旗」的待核条目直接通过，不再人工过目；判定不了
    （jingfen 转链、三方店铺）的留在待核队列。"""
    import yaml
    path = _prop_path()
    if not path.exists():
        raise HTTPException(404, "暂无提案文件")
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for p in items:
        if (p.get("status", "待核") == "待核"
                and p.get("channel") in ("自营", "官旗")
                and (p.get("vendor") or "").strip()):
            p["status"] = "通过"
            p["reviewed_at"] = now
            p["remark"] = (f"{p.get('remark')}；自动预审：店名含"
                           f"{'自营' if p['channel'] == '自营' else '旗舰'}"
                           if p.get("remark") else
                           f"自动预审：店名判定{p['channel']}（{p['vendor']}）")
            n += 1
    if n:
        path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    pending = sum(1 for x in items if x.get("status", "待核") == "待核")
    return {"ok": True, "auto_passed": n, "pending": pending}


@app.post("/api/mmb/review/batch")
def api_mmb_review_batch(payload: dict = Body(...)) -> dict:
    """批量提交（2026-10-17 用户口径）：看板勾选卡片 → 一键通过。

    body: {items: [{identity, anchor, channel, vendor}]}，status 固定
    「通过」（驳回仍走单条端点）；一次写盘，条目级校验失败整体 422。"""
    import yaml
    rows = payload.get("items") or []
    if not rows:
        raise HTTPException(422, "items 为空")
    for r in rows:
        ident = r.get("identity") or {}
        if not ident.get("series") or r.get("anchor") not in _MMB_ANCHOR_NORM:
            raise HTTPException(422, f"条目缺 identity.series/anchor: {ident}")
        if r.get("channel") not in _MMB_CHANNELS:
            raise HTTPException(422, f"channel 非法: {r.get('channel')!r}")
    path = _prop_path()
    if not path.exists():
        raise HTTPException(404, "暂无提案文件")
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    now = datetime.now().isoformat(timespec="seconds")
    hit_n = 0
    for r in rows:
        ident = r.get("identity") or {}
        anchor = _MMB_ANCHOR_NORM[r["anchor"]]
        hit = None
        for x in items:
            xa = _MMB_ANCHOR_NORM.get(x.get("anchor"))
            if x.get("identity") == ident and xa == anchor:
                hit = x
                break
        if hit is None:
            raise HTTPException(404, f"提案不存在: {ident.get('series')}")
        hit["channel"] = r["channel"]
        if "vendor" in r:
            hit["vendor"] = r["vendor"] or ""
        hit["status"] = "通过"
        hit["reviewed_at"] = now
        hit_n += 1
    path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    pending = sum(1 for x in items if x.get("status", "待核") == "待核")
    return {"ok": True, "passed": hit_n, "total": len(items), "pending": pending}


def _read_log_any(p: Path) -> str:
    """日志文件编码自适应（2026-10-17 乱码修复）。

    reports/ 下的日志来自三种写法：Python 直写（UTF-8）、PowerShell 5.1
    Tee-Object/重定向（UTF-16 LE 带 BOM）、GBK 工具。按 BOM → UTF-8 →
    GBK 顺序探测，谁解得开用谁。"""
    b = p.read_bytes()
    if b.startswith(b"\xff\xfe") or b.startswith(b"\xfe\xff"):
        return b.decode("utf-16")
    if b.startswith(b"\xef\xbb\xbf"):
        return b.decode("utf-8-sig")
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


# ---------- 任务流水（控制台风格，2026-10-17）：只读 reports/*.log + 闸门状态 ----------

@app.post("/api/mmb/collect-now")
def api_mmb_collect_now(payload: dict = Body(...)) -> dict:
    """立即采集指定锚（2026-10-19 新增 SKU 用户旅程 §5.5 设计文档）。

    urls 须已在注册表（404 兜底）；拉起 scripts/collect_now.py 后台子进程
    ——同一 channel-gate 锁+节奏，增量缓存+断点续采，重复点击幂等；
    采完自动 trend_calc 刷新快照。进度看「任务流水」面板，重复点击 409。"""
    import subprocess

    from tools import taskboard
    urls = [str(u).strip() for u in (payload.get("urls") or [])
            if str(u).strip()]
    if not urls:
        raise HTTPException(422, "urls 必填")
    running = [t for t in taskboard.snapshot()
               if t.get("status") == "running"
               and str(t.get("name", "")).startswith("立即采集")]
    if running:
        raise HTTPException(409, "已有「立即采集」在跑，看任务流水进度")
    reg_urls = {it.get("url") for it in _reg_load().get("listings") or []}
    unknown = [u for u in urls if u not in reg_urls]
    if unknown:
        raise HTTPException(404, "以下 URL 不在注册表："
                            + "、".join(unknown[:3])
                            + ("…" if len(unknown) > 3 else ""))
    script = ROOT / "scripts" / "collect_now.py"
    logf = (ROOT / "reports" / "collect_now.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", str(script),
         "--urls", ",".join(urls)],
        cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # 拿到 task_id（taskboard 起了两条同名 running 时取最新那一条）
    new_task = next((t for t in taskboard.snapshot()
                     if t.get("status") == "running"
                     and str(t.get("name", "")).startswith("立即采集")),
                    None)
    task_id = new_task["id"] if new_task else None
    if task_id:
        PROCS[task_id] = {"proc": proc, "script": "collect_now",
                           "started_at": time.time()}
    return {"ok": True, "urls": urls, "task_id": task_id,
            "msg": "已启动采集：完成后看板自动刷新（任务流水可看进度）"}


@app.get("/api/tasklog")
def api_tasklog(file: str = "mmb_backfill_run.log", tail: int = 120) -> dict:
    """任务执行流水：reports/ 下的 .log 尾部 + 渠道闸门实时状态。

    只读、白名单（reports/*.log，拒绝路径穿越）；闸门状态来自
    channel-gate 的锁/预算台账（零缓冲，实时）。"""
    import channel_gate

    tail = max(10, min(int(tail), 500))
    safe = Path(file).name
    if not safe.endswith(".log"):
        raise HTTPException(422, "只允许 .log")
    p = ROOT / "reports" / safe
    lines: list[str] = []
    if p.exists():
        raw = _read_log_any(p).splitlines()
        lines = raw[-tail:]
    lock = channel_gate.gate._read_lock("mmb")
    budget = channel_gate.gate.spent_today("mmb")
    from tools import taskboard
    # 给每个 task 加 cancellable（sku-cancel-spec §3.4）：只有 running 且在 PROCS 表里
    tasks = []
    for t in taskboard.snapshot():
        t2 = dict(t)
        t2["cancellable"] = (t.get("status") == "running"
                             and t.get("id") in PROCS)
        tasks.append(t2)
    return {
        "file": safe, "exists": p.exists(),
        "mtime": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
                  if p.exists() else ""),
        "lines": lines,
        "gate": {"locked_by": (f"pid {lock['pid']} · {lock.get('purpose')}"
                               if lock else None),
                 "budget_used_today": budget},
        "tasks": tasks,
    }


@app.post("/api/task/cancel")
def api_task_cancel(payload: dict = Body(...)) -> dict:
    """取消一个 running 任务（sku-cancel-spec §3.3）。

    流程：taskboard 写 status="signal_sent" → proc.terminate() 发 SIGTERM
    → 后台脚本在下一循环点（≤2s）认信号 → finish("cancelled") → 退出。

    返回：
    - 200: {ok, msg}              已发信号
    - 404:  task_id 找不到（taskboard / PROCS 都没有）
    - 409:  task 不是 running 状态
    """
    from tools import taskboard
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise HTTPException(422, "task_id 必填")
    # 找后台进程
    entry = PROCS.get(task_id)
    target = next((t for t in taskboard.snapshot()
                   if t.get("id") == task_id), None)
    if not target and not entry:
        raise HTTPException(404, f"task_id {task_id} 不存在")
    if target and target.get("status") != "running":
        raise HTTPException(409, f"task 已结束：status={target.get('status')}")
    if not entry:
        # taskboard 还在跑但 PROCS 没了（webapp 重启过）→ 无法 cancel
        raise HTTPException(409, "进程句柄丢失（webapp 重启过），无法远程取消")
    proc = entry["proc"]
    # 在台账里标"已发信号"，让前端显示"取消中"
    try:
        # 用 step(..., status="signal_sent") 不行——step 改的是 step 状态；写死 task 顶层 status
        import json as _json
        board = taskboard._read()
        for t in board.get("tasks", []):
            if t.get("id") == task_id:
                t["status"] = "signal_sent"
                t["summary"] = "已发取消信号，等待子进程退出"
                break
        taskboard._write(board)
    except Exception as e:
        log(f"cancel: 写台账 signal_sent 失败：{e}")
    try:
        proc.terminate()  # Windows: SIGTERM-equivalent
    except Exception as e:
        raise HTTPException(500, f"terminate 失败：{e}")
    # 兜底：3s 后没退就用 kill（不是必然触发——脚本正常认信号就退）
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    # 清理 PROCS 表
    PROCS.pop(task_id, None)
    return {"ok": True, "msg": f"已发取消信号：{entry['script']}",
            "task_id": task_id}


_MMB_ANCHOR_NORM = {"sales": "销量", "price": "价格",
                    "销量": "销量", "价格": "价格"}
# channel 三线（用户 10-16 定稿：自营/官旗/三方店铺；第三方为旧值，兼容接受）
_MMB_CHANNELS = ("自营", "官旗", "三方店铺", "第三方", "待核")
_MMB_STATUS = ("待核", "通过", "驳回")


@app.post("/api/mmb/auto-anchor")
def api_mmb_auto_anchor() -> dict:
    """一键补锚开关（2026-10-17 用户口径：缺锚实体不用手搜）。

    子进程拉起 scripts/auto_anchor.py：零观测实体 → search 配对 →
    siteSpuid 纯数字直链 → 生成待核提案（审核卡勾选把关型号配对）。
    进度写任务台账，「任务流水」面板实时可见；预算尽 fail-closed。"""
    import subprocess

    import channel_gate
    from tools import taskboard
    running = [t for t in taskboard.snapshot()
               if t.get("status") == "running"
               and str(t.get("name", "")).startswith("一键补锚")]
    if running:
        raise HTTPException(409, f"已在跑（{running[0]['name']}），看任务流水进度")
    # R2 上限已于 10-18 移除（d8c353a）：预算无上限，脚本内闸门仍管节奏
    script = ROOT / "scripts" / "auto_anchor.py"
    logf = (ROOT / "reports" / "auto_anchor.log").open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", str(script)],
        cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    new_task = next((t for t in taskboard.snapshot()
                     if t.get("status") == "running"
                     and str(t.get("name", "")).startswith("一键补锚")),
                    None)
    task_id = new_task["id"] if new_task else None
    if task_id:
        PROCS[task_id] = {"proc": proc, "script": "auto_anchor",
                           "started_at": time.time()}
    return {"ok": True, "task_id": task_id,
            "msg": "已启动：进度看「任务流水」，跑完在提案审核卡勾选"}


@app.post("/api/mmb/review")
def api_mmb_review(payload: dict = Body(...)) -> dict:
    """提案审核（2026-10-16）：用户在看板点选 channel/状态，不再手改 YAML。

    只写提案文件（gitignore 内的工作文件，与 /api/mmb/propose 同级；
    Web 依然不写库、不写 config）。identity+anchor 定位条目，其余字段
    原样保留；status=通过 的条目由人工/脚本誊入注册表。
    """
    import yaml
    ident = payload.get("identity") or {}
    anchor = _MMB_ANCHOR_NORM.get(payload.get("anchor"))
    if not ident.get("series") or anchor is None:
        raise HTTPException(422, "identity.series / anchor 必填")
    channel = payload.get("channel", "待核")
    status = payload.get("status", "待核")
    if channel not in _MMB_CHANNELS:
        raise HTTPException(422, f"channel 只能是 {'/'.join(_MMB_CHANNELS)}")
    if status not in _MMB_STATUS:
        raise HTTPException(422, f"status 只能是 {'/'.join(_MMB_STATUS)}")
    path = ROOT / "data" / "manmanbuy_proposals.yaml"
    if not path.exists():
        raise HTTPException(404, "暂无提案文件")
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    hit = None
    for x in items:
        xa = _MMB_ANCHOR_NORM.get(x.get("anchor"))
        if x.get("identity") == ident and xa == anchor:
            hit = x
            break
    if hit is None:
        raise HTTPException(404, "提案条目不存在（先 searchbox 提案或等遴选）")
    hit["channel"] = channel
    if "vendor" in payload:
        hit["vendor"] = payload["vendor"] or ""
    hit["status"] = status
    hit["reviewed_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    pending = sum(1 for x in items if x.get("status", "待核") == "待核")
    return {"ok": True, "total": len(items), "pending": pending}


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(prog="webapp", description="SKU 趋势看板（只读）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="默认本机；局域网看传 0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)
    print(f"SKU 趋势看板: http://{args.host}:{args.port}  "
          f"(DB={DB_PATH} 只读)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
