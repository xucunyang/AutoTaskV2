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
    assert r["set"] == list(ANCHORS)        # 按 ANCHORS 字面定义顺序
    assert r["skipped_no_match"] == []
    for d in ANCHORS:
        row = db.execute("SELECT perf_anchor FROM entity WHERE display_name=?",
                         (d,)).fetchone()
        assert row[0] == ANCHORS[d]


def test_apply_is_idempotent(db):
    apply_anchors(db)
    r2 = apply_anchors(db)
    assert r2["set"] == []                                  # 全部已在 → 跳过
    assert sorted(r2["already_set"]) == sorted(ANCHORS)    # 字典序稳定比较


def test_apply_dry_run_does_not_write(db):
    r = apply_anchors(db, dry_run=True)
    assert r["set"] == list(ANCHORS)        # 仍报告会写
    for d in ANCHORS:
        row = db.execute("SELECT perf_anchor FROM entity WHERE display_name=?",
                         (d,)).fetchone()
        assert row[0] is None              # 但实际不写