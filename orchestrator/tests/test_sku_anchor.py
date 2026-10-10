"""sku_anchor 锚点基底写入测试（Q14 拍板）。

锁住：写入了正确的 display_name → perf_anchor 映射、幂等（已设值
不重复写）、dry-run 不改库。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sku_db import connect, seed_entities  # noqa: E402
from tools.sku_anchor import ANCHORS, apply_anchors  # noqa: E402

SEED = ROOT / "config/contracts/hardware_entities.yaml"


@pytest.fixture()
def db(tmp_path):
    con = connect(tmp_path / "sku.db")
    seed_entities(con, SEED)
    yield con
    con.close()


def test_anchor_targets_exist_in_seed(db):
    """锚点表的每个名字都必须在种子清单里——否则是「找不到基底」的
    早期发现机制。"""
    names = {r["display_name"] for r in
             db.execute("SELECT display_name FROM entity").fetchall()}
    for d in ANCHORS:
        assert d in names, f"锚点目标 {d!r} 不在种子清单里"


def test_apply_sets_anchor(db):
    r = apply_anchors(db)
    # set 元素按 SELECT 返回序（DB row 序），不是字典定义序——用集合比较
    assert sorted(r["set"]) == sorted(ANCHORS)
    # skipped_no_match 应覆盖种子清单里所有「不在字典中」的实体
    all_names = {row["display_name"] for row in
                 db.execute("SELECT display_name FROM entity").fetchall()}
    expected_skipped = sorted(all_names - set(ANCHORS))
    assert sorted(r["skipped_no_match"]) == expected_skipped
    for d in ANCHORS:
        row = db.execute("SELECT perf_anchor FROM entity WHERE display_name=?",
                         (d,)).fetchone()
        assert row[0] == ANCHORS[d]


def test_apply_clears_orphan_anchor(db):
    """字典外的旧锚点（如 P0 早期写错的 5060 Ti 16GB）必须清空。

    perf_anchor 字段三态：1.0 = 基底；>0 且 !=1 = 倍数；空 = 待 P1.5。
    字典是权威——字典外的值要么是误设要么是过时的，要么 P1.5 补。
    清空是最安全的默认（不会让「基底」和「非基底」共用 1.0 混淆）。
    """
    db.execute(
        "UPDATE entity SET perf_anchor=1.0 WHERE display_name='RTX 5060 Ti 16GB'")
    db.commit()
    r = apply_anchors(db)
    assert "RTX 5060 Ti 16GB" in r["cleared"]
    row = db.execute(
        "SELECT perf_anchor FROM entity WHERE display_name='RTX 5060 Ti 16GB'"
        ).fetchone()
    assert row[0] is None              # 清空了


def test_apply_is_idempotent(db):
    apply_anchors(db)
    r2 = apply_anchors(db)
    assert r2["set"] == []                                  # 全部已在 → 跳过
    assert r2["cleared"] == []                              # 没字典外值 → 不清
    assert sorted(r2["already_set"]) == sorted(ANCHORS)    # 字典序稳定比较


def test_apply_dry_run_does_not_write(db):
    r = apply_anchors(db, dry_run=True)
    # 字典在/字典外分别断言：字典内的应有「set」报告但库不动；字典外的
    # 若有值会被「cleared」报告但库不动
    assert sorted(r["set"]) == sorted(ANCHORS)
    for d in ANCHORS:
        row = db.execute("SELECT perf_anchor FROM entity WHERE display_name=?",
                         (d,)).fetchone()
        assert row[0] is None              # 但实际不写