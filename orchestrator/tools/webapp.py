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
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from tools.sku_db import DB_PATH, connect  # noqa: E402
from tools.trend_calc import (  # noqa: E402
    _aggregate_series as aggregate_series,
    _series_with_attrs as series_with_attrs,
    l0_metrics,
)

STATIC = Path(__file__).resolve().parent / "webapp_static"

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
                                 {"condition": "全新", "source": "自采",
                                  "vendor": v})
        if rows:
            tracks[v] = [[d, p] for d, p in rows]
    return tracks


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
            "perf_anchor FROM entity WHERE active=1 ORDER BY entity_id"):
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
            "identity": identity,
            "perf_anchor": row["perf_anchor"],
            "n_periods": len(series),
            "series": [[d, p] for d, p in series],
            "tracks": _entity_tracks(con, eid, as_of),
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
    return FileResponse(STATIC / "index.html")


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
        slim = [{k: v for k, v in e.items() if k not in ("series", "tracks")}
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
