"""webapp 单元测试（TODO-A Web MVP 门禁）。

只测确定性层（build_board / ro_connect / _latest_json）——HTTP 路由是
薄封装。铁律验证点：query_only 连接写事务必被拒（Web 永不写库）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import webapp  # noqa: E402
from tools.sku_db import connect  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    """2 实体带序列（A：6 期双 vendor；B：2 期）+ 1 无数据实体 +
    1 条二手观测（aggregate_scope 切轨验证）。"""
    con = connect(tmp_path / "sku.db")
    now = "2026-10-11T00:00:00+00:00"
    for name, ident in (
            ("RTX 5060 Ti 8GB", {"category": "显卡", "series": "RTX 5060 Ti",
                                 "spec": "8GB"}),
            ("RTX 4060 8GB", {"category": "显卡", "series": "RTX 4060",
                              "spec": "8GB"}),
            ("RTX 5090 32GB", {"category": "显卡", "series": "RTX 5090",
                               "spec": "32GB"})):
        con.execute(
            "INSERT INTO entity(domain, identity_json, display_name,"
            " created_at, updated_at) VALUES('hardware',?,?,?,?)",
            (json.dumps(ident, ensure_ascii=False), name, now, now))
    # A：6 期，两 vendor（七彩虹 5 期 + 微星 3 期）
    for i, d in enumerate(range(5, 11)):
        day = f"2026-10-{d:02d}"
        con.execute(
            "INSERT INTO observation(entity_id, obs_date, collected_on,"
            " low_price, attrs_json, created_at)"
            " VALUES(1,?,?,?,'{\"condition\":\"全新\",\"source\":\"自采\","
            "\"vendor\":\"七彩虹\"}',?)", (day, day, 3000 - i * 10, now))
    for d, p in ((8, 3100), (9, 3090), (10, 3080)):
        day = f"2026-10-{d:02d}"
        con.execute(
            "INSERT INTO observation(entity_id, obs_date, collected_on,"
            " low_price, attrs_json, created_at)"
            " VALUES(1,?,?,?,'{\"condition\":\"全新\",\"source\":\"自采\","
            "\"vendor\":\"微星\"}',?)", (day, day, p, now))
    # A 的二手观测（切轨：不得混入主序列）
    con.execute(
        "INSERT INTO observation(entity_id, obs_date, collected_on,"
        " low_price, attrs_json, created_at)"
        " VALUES(1,'2026-10-10','2026-10-10',999,"
        "'{\"condition\":\"二手\",\"source\":\"自采\",\"vendor\":\"七彩虹\"}',?)",
        (now,))
    # B：2 期（L0 门槛不足）
    for d, p in ((9, 2500), (10, 2490)):
        day = f"2026-10-{d:02d}"
        con.execute(
            "INSERT INTO observation(entity_id, obs_date, collected_on,"
            " low_price, attrs_json, created_at)"
            " VALUES(2,?,?,?,'{\"condition\":\"全新\",\"source\":\"自采\","
            "\"vendor\":\"七彩虹\"}',?)", (day, day, p, now))
    con.commit()
    yield con
    con.close()


def test_build_board_lists_all_entities_with_counts(db):
    board = webapp.build_board(db, root=ROOT)
    assert board["counts"]["total"] == 3
    assert board["counts"]["with_data"] == 2
    assert board["counts"]["l0_ok"] == 1        # 只有 A 够 5 期


def test_l0_metrics_only_at_sufficiency(db):
    board = webapp.build_board(db, root=ROOT)
    by_id = {e["entity_id"]: e for e in board["entities"]}
    a, b, c = by_id[1], by_id[2], by_id[3]
    assert a["metrics"]["label"] in LABEL_NAMES
    assert a["metrics"]["last_price"] == 2950.0
    assert b["metrics"] is None and b["n_periods"] == 2       # Q12 占位
    assert c["n_periods"] == 0 and c["series"] == []


LABEL_NAMES = {"fast_drop", "drop", "stable", "rise", "fast_rise"}


def test_series_respects_aggregate_scope(db):
    """二手 999 元绝不能混进主序列（切轨，v3.3 M1/M2）。"""
    board = webapp.build_board(db, root=ROOT)
    a = next(e for e in board["entities"] if e["entity_id"] == 1)
    prices = [p for _, p in a["series"]]
    assert 999 not in prices
    assert min(prices) == 2950.0


def test_vendor_tracks_split(db):
    board = webapp.build_board(db, root=ROOT)
    a = next(e for e in board["entities"] if e["entity_id"] == 1)
    assert set(a["tracks"]) == {"七彩虹", "微星"}
    assert len(a["tracks"]["微星"]) == 3
    assert len(a["tracks"]["七彩虹"]) == 6


def test_track_series_also_filtered(db):
    """分轨序列同样只认 全新+自采（二手 999 不得出现在任何轨）。"""
    board = webapp.build_board(db, root=ROOT)
    a = next(e for e in board["entities"] if e["entity_id"] == 1)
    for track in a["tracks"].values():
        assert all(p > 1000 for _, p in track)


def test_board_entities_carry_domain_group(db):
    """R20：实体带 domain 与二级分组（硬件=category）。"""
    board = webapp.build_board(db, root=ROOT)
    a = next(e for e in board["entities"] if e["entity_id"] == 1)
    assert a["domain"] == "hardware"
    assert a["group"] == "显卡"                      # identity.category


def test_entity_points_returns_trace_fields(db):
    """R21 数据点溯源：某日全部原始观测带店铺/平台/链接/券/采集时间。"""
    import json as _json
    # 10-10 有三笔（七彩虹全新 2950 + 微星全新 3080 + 二手 999）——溯源要全部列出
    pts = webapp.entity_points(db, 1, "2026-10-10")
    assert len(pts["points"]) == 3
    p = pts["points"][0]                              # 按 low_price 排序，999 在前
    assert p["low_price"] == 999.0
    assert p["condition"] == "二手" and p["vendor"] == "七彩虹"
    assert {q["vendor"] for q in pts["points"]} == {"七彩虹", "微星"}
    # 补一笔带完整店铺字段的观测验证链接透传 + img_url（R22）
    db.execute(
        "INSERT INTO observation(entity_id, obs_date, collected_on,"
        " low_price, attrs_json, shop_name, shop_platform, shop_url,"
        " source_url, coupons, raw_json, created_at)"
        " VALUES(1,'2026-10-09','2026-10-11',2960,'{\"condition\":\"全新\","
        "\"source\":\"自采\",\"vendor\":\"微星\"}','微星京东自营',"
        "'京东','https://item.jd.com/1.html','https://w.cn','[]',"
        "'{\"img_url\":\"/img/2026-10-09/ms.png\"}',?)", (now_,))
    db.commit()
    pts = webapp.entity_points(db, 1, "2026-10-09")
    p = next(q for q in pts["points"] if q["shop_name"])
    assert p["shop_url"] == "https://item.jd.com/1.html"
    assert p["shop_platform"] == "京东" and p["shop_name"] == "微星京东自营"
    assert p["img_url"] == "/img/2026-10-09/ms.png"      # R22 透传


now_ = "2026-10-11T00:00:00+00:00"


def test_series_only_match_tolerates_space_variants(tmp_path):
    """「RTX 5090 D」vs「RTX 5090D」是自然变体——series-only 兜底要
    剥空格比较（2026-10-11 子 Agent 采集实测踩中）。"""
    from tools.sku_db import connect
    from tools.sku_ingest import _match_entity, Contract

    con = connect(tmp_path / "sku.db")
    now = "2026-10-11T00:00:00+00:00"
    for ident in ({"category": "显卡", "series": "RTX 5090D", "spec": "32GB"},
                  {"category": "显卡", "series": "RTX 5090D V2", "spec": "24GB"}):
        con.execute(
            "INSERT INTO entity(domain, identity_json, display_name,"
            " created_at, updated_at) VALUES('hardware',?,?,?,?)",
            (json.dumps(ident, ensure_ascii=False),
             ident["series"] + " " + ident["spec"], now, now))
    con.commit()
    contract = Contract.load(ROOT, "hardware")
    hit = _match_entity(con, contract, {"category": "显卡",
                                        "series": "RTX 5090 D V2",
                                        "spec": "24GB"})
    assert hit is not None and "V2" in hit["display_name"]
    hit2 = _match_entity(con, contract, {"category": "显卡",
                                         "series": "RTX 5090 D",
                                         "spec": "32GB"})
    assert hit2 is not None and "V2" not in hit2["display_name"]
    con.close()


def test_ro_connect_rejects_writes(tmp_path):
    """铁律：Web 连接上任何写事务必须在 SQLite 层被拒。"""
    con = webapp.ro_connect(tmp_path / "sku.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE x(a)")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO entity(domain) VALUES('hardware')")
    finally:
        con.close()


def test_latest_json_picks_newest(tmp_path):
    (tmp_path / "artifacts" / "2026-10-10").mkdir(parents=True)
    (tmp_path / "artifacts" / "2026-10-12").mkdir(parents=True)
    (tmp_path / "artifacts" / "2026-10-10" / "trend_snapshot.json").write_text(
        '{"date":"2026-10-10"}', encoding="utf-8")
    (tmp_path / "artifacts" / "2026-10-12" / "trend_snapshot.json").write_text(
        '{"date":"2026-10-12"}', encoding="utf-8")
    assert webapp._latest_json(tmp_path, "trend_snapshot.json")["date"] == \
        "2026-10-12"
    assert webapp._latest_json(tmp_path, "hw_recommend.json") is None


def _seed_proposals(root: Path):
    """提案审核测试夹具：2 条待核（含遴选证据字段 trial/remark）。"""
    import yaml
    p = root / "data"
    p.mkdir(parents=True, exist_ok=True)
    (p / "manmanbuy_proposals.yaml").write_text(yaml.safe_dump([
        {"proposed_at": "2026-10-16T10:00:00",
         "identity": {"category": "CPU", "series": "7800X3D", "spec": ""},
         "anchor": "销量", "url": "https://item.jd.com/1.html",
         "sp_name": "AMD 7800X3D 盒装", "vendor": "", "channel": "待核",
         "price": "2549", "sales": "20001",
         "trial": "gwd-verified（101 点）", "remark": "盒装", "status": "待核"},
        {"proposed_at": "2026-10-16T10:00:00",
         "identity": {"category": "显卡", "series": "RTX 5050", "spec": "8GB"},
         "anchor": "价格", "url": "https://item.jd.com/2.html",
         "sp_name": "七彩虹 5050", "vendor": "", "channel": "待核",
         "price": "3249", "sales": "5000", "status": "待核"},
    ], allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_review_updates_channel_status_and_preserves_evidence(tmp_path, monkeypatch):
    """审核只改 channel/vendor/status(+reviewed_at)，trial/remark 等证据保留。"""
    _seed_proposals(tmp_path)
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    d = webapp.api_mmb_review({
        "identity": {"category": "CPU", "series": "7800X3D", "spec": ""},
        "anchor": "sales", "channel": "自营", "vendor": "AMD 旗舰店",
        "status": "通过"})
    assert d["ok"] and d["pending"] == 1 and d["total"] == 2
    import yaml
    items = yaml.safe_load((tmp_path / "data" / "manmanbuy_proposals.yaml")
                           .read_text(encoding="utf-8"))
    hit = next(x for x in items
               if x["identity"]["series"] == "7800X3D")
    assert (hit["channel"], hit["vendor"], hit["status"]) == ("自营", "AMD 旗舰店", "通过")
    assert hit["trial"] == "gwd-verified（101 点）" and hit["remark"] == "盒装"
    assert "reviewed_at" in hit
    # 三线新值同样接受（用户 10-16 定稿：自营/官旗/三方店铺）
    d2 = webapp.api_mmb_review({
        "identity": {"category": "显卡", "series": "RTX 5050", "spec": "8GB"},
        "anchor": "价格", "channel": "官旗", "vendor": "七彩虹旗舰店",
        "status": "通过"})
    assert d2["ok"] and d2["pending"] == 0


def test_mmb_review_auto_and_batch(tmp_path, monkeypatch):
    """自动预审：店名判自营/官旗→直接通过；批量提交：勾选卡片一键通过。"""
    import yaml
    _seed_proposals(tmp_path)
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    # 预填 2 条店名可判定的：自营（可自动过）+ 三方店铺（不可自动过，留人工）
    path = tmp_path / "data" / "manmanbuy_proposals.yaml"
    items = yaml.safe_load(path.read_text(encoding="utf-8"))
    items[0]["channel"], items[0]["vendor"] = "自营", "京东自营"
    items[1]["channel"], items[1]["vendor"] = "三方店铺", "某数码专营店"
    path.write_text(yaml.safe_dump(items, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    # proposals 端点报 auto_n=1（只自营/官旗计入）
    d = webapp.api_mmb_proposals()
    assert d["auto_n"] == 1
    # 自动预审：只过自营那条
    r = webapp.api_mmb_review_auto()
    assert r["ok"] and r["auto_passed"] == 1 and r["pending"] == 1
    cur = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert cur[0]["status"] == "通过" and "自动预审" in cur[0]["remark"]
    assert cur[0]["trial"] == "gwd-verified（101 点）"   # 证据保留
    assert cur[1]["status"] == "待核"
    # 再跑一次幂等：没有新可自动过的
    assert webapp.api_mmb_review_auto()["auto_passed"] == 0
    # 批量提交：三方那条按卡片所选渠道通过
    r2 = webapp.api_mmb_review_batch({"items": [{
        "identity": {"category": "显卡", "series": "RTX 5050", "spec": "8GB"},
        "anchor": "价格", "channel": "三方店铺", "vendor": "某数码专营店"}]})
    assert r2["ok"] and r2["passed"] == 1 and r2["pending"] == 0
    cur = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert cur[1]["status"] == "通过" and cur[1]["channel"] == "三方店铺"
    # 批量校验：非法渠道整体 422；空 items 422；不存在条目 404
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        webapp.api_mmb_review_batch({"items": [{
            "identity": {"category": "显卡", "series": "RTX 5050", "spec": "8GB"},
            "anchor": "价格", "channel": "杂牌"}]})
    assert e.value.status_code == 422
    with pytest.raises(HTTPException):
        webapp.api_mmb_review_batch({"items": []})
    with pytest.raises(HTTPException) as e:
        webapp.api_mmb_review_batch({"items": [{
            "identity": {"category": "显卡", "series": "不存在", "spec": ""},
            "anchor": "价格", "channel": "自营"}]})
    assert e.value.status_code == 404


def test_proposals_enrich_img_from_raw(tmp_path, monkeypatch):
    """提案列表带回填留档里的 spPic（identity 匹配，零预算）。"""
    import json
    _seed_proposals(tmp_path)
    art = tmp_path / "artifacts" / "2026-10-17"
    art.mkdir(parents=True)
    (art / "backfill_manmanbuy_raw.json").write_text(json.dumps({
        "fetched_at": "x", "listings": [
            {"identity": {"category": "CPU", "series": "7800X3D", "spec": ""},
             "url": "https://item.jd.com/1.html", "sp_name": "AMD 7800X3D 盒装",
             "channel": "自营", "vendor": "",
             "trend": {"datePrice": [[1, "2549"]], "spPic": "https://img.example/x.jpg"}},
        ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    d = webapp.api_mmb_proposals()
    hit = next(x for x in d["items"] if x["identity"]["series"] == "7800X3D")
    assert hit["img"] == "https://img.example/x.jpg"
    other = next(x for x in d["items"] if x["identity"]["series"] == "RTX 5050")
    assert "img" not in other


def test_read_log_any_encodings(tmp_path):
    """日志编码自适应：UTF-8 / UTF-16(PS Tee-Object) / BOM / GBK 都能解。"""
    f = tmp_path / "x.log"
    f.write_bytes("✓ 配对成功：影驰自营".encode("utf-16"))
    assert "配对成功" in webapp._read_log_any(f)
    f.write_bytes("✓ 配对成功".encode("utf-8-sig"))
    assert "配对成功" in webapp._read_log_any(f)
    f.write_bytes("配对成功-影驰自营".encode("gbk"))
    assert "配对成功" in webapp._read_log_any(f)
    f.write_bytes("✓ 配对成功".encode("utf-8"))
    assert "配对成功" in webapp._read_log_any(f)


def test_review_rejects_bad_values(tmp_path, monkeypatch):
    """channel/status 非法值 422；不存在的条目 404。"""
    from fastapi import HTTPException
    _seed_proposals(tmp_path)
    monkeypatch.setattr(webapp, "ROOT", tmp_path)
    base = {"identity": {"category": "CPU", "series": "7800X3D", "spec": ""},
            "anchor": "销量"}
    with pytest.raises(HTTPException) as e:
        webapp.api_mmb_review({**base, "channel": "拼多多", "status": "通过"})
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        webapp.api_mmb_review({**base, "channel": "自营", "status": "随便"})
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        webapp.api_mmb_review({
            "identity": {"category": "CPU", "series": "不存在", "spec": ""},
            "anchor": "销量", "channel": "自营", "status": "通过"})
    assert e.value.status_code == 404


def test_mmb_searchbox_gated_lock_held_becomes_429(monkeypatch):
    """searchbox 走 mmb 闸门（AGENTS §二.13）：渠道被占→429 而不是打过去。"""
    from contextlib import contextmanager
    from fastapi import HTTPException

    from channel_gate import LockHeld

    class FakeGate:
        budget = 60

        @contextmanager
        def session(self, purpose, force=False):
            raise LockHeld("mmb", {"pid": 999, "purpose": "backfill",
                                   "started_at": "x"})
            yield None                       # pragma: no cover

    monkeypatch.setattr(webapp, "_mmb_gate", lambda: FakeGate())
    with pytest.raises(HTTPException) as e:
        webapp.api_mmb_search(q="5060")
    assert e.value.status_code == 429
    assert "渠道忙" in e.value.detail
