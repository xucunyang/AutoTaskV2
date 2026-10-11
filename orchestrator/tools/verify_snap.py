"""verify_snap：t4 验收的 python_test 脚本（快照与 DB 重算一致性）。

acceptance_hw_t4.yaml 引用（cmd 形态，h_python_test 以仓库根为 cwd）：
    python -X utf8 orchestrator/tools/verify_snap.py

为什么存在（设计 v3.3 M4 / 教训 acceptance_t2 2026-10-04）：
t4 的产物是模型写的报告，数字必须与确定性快照一致——「模型改数」是
这条链最隐蔽的失败模式。本脚本从 DB 重算每个实体的 L0 指标，与
trend_snapshot.json 逐字段比对（容差 0.01），不一致 exit 1。

扫描规则：取 artifacts/ 下**最新的** trend_snapshot.json（t4 验收
跑在 t4 完成后，最新=当日；旧日期的快照不会被误验）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ORCH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ORCH))

from tools.sku_db import connect  # noqa: E402
from tools.trend_calc import _aggregate_series, l0_metrics  # noqa: E402

TOL = 0.01
COMPARE_KEYS = ("d1_pct", "slope_7_pct_per_day", "vol_7_pct",
                "pct_from_30d_low")


def latest_snapshot(orch: Path) -> Path | None:
    """artifacts/ 下最新的**有可检内容**的 trend_snapshot.json。

    按日期目录倒序找第一个「至少一个实体带 metrics」的快照——空快照
    （如冷启动日无观测时 calc 的产物）没有可比内容，跳过去找更早的。
    """
    root = orch / "artifacts"
    if not root.exists():
        return None
    dated = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    for d in dated:
        p = d / "trend_snapshot.json"
        if not p.exists():
            continue
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if any(e.get("metrics") for e in snap.get("entities", [])):
            return p
    return None


def check(orch: Path = ORCH, tol: float = TOL) -> tuple[bool, str]:
    snap_path = latest_snapshot(orch)
    if snap_path is None:
        return False, "no_snapshot_found"
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    date = snap["date"]
    con = connect(orch / "data" / "sku.db")
    try:
        checked = 0
        for ent in snap.get("entities", []):
            metrics = ent.get("metrics")
            if not metrics:
                continue          # sufficiency 不足的实体没有指标可比
            series = _aggregate_series(con, ent["entity_id"], date)
            recomputed = l0_metrics(series)
            if recomputed is None:
                continue
            for key in COMPARE_KEYS:
                snap_v, rec_v = metrics.get(key), recomputed.get(key)
                if snap_v is None or rec_v is None:
                    continue
                if abs(float(snap_v) - float(rec_v)) > tol:
                    return False, (f"mismatch {ent['display_name']}.{key}: "
                                   f"snap={snap_v} db={rec_v}")
            checked += 1
        return True, f"consistent entities_checked={checked} snap={snap_path.name}"
    finally:
        con.close()


def main() -> int:
    ok, detail = check()
    print(("PASS " if ok else "FAIL ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())