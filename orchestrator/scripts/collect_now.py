# -*- coding: utf-8 -*-
"""立即采集指定锚（看板 /api/mmb/collect-now 拉起，2026-10-19 新增 SKU 用户旅程）。

流程：taskboard 登记 → backfill run --urls（同一 channel-gate：锁+节奏；
增量缓存+断点续采，重复点击幂等）→ trend_calc 刷新快照 → 销号。
进度在「任务流水」面板实时可见。"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import taskboard  # noqa: E402


def _run(cmd: list[str]) -> int:
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(prog="collect_now")
    ap.add_argument("--urls", required=True, help="逗号分隔的京东直链")
    a = ap.parse_args()
    urls = [u for u in a.urls.split(",") if u.strip()]
    tb = taskboard.start("立即采集", [f"采集 {len(urls)} 锚", "trend_calc"])
    taskboard.step(tb, f"采集 {len(urls)} 锚", "running")
    py = sys.executable
    rc1 = _run([py, "-X", "utf8", "-u", "-m", "tools.manmanbuy_backfill",
                "run", "--date-dir", time.strftime("%Y-%m-%d"),
                "--urls", ",".join(urls)])
    taskboard.step(tb, f"采集 {len(urls)} 锚",
                   "DONE" if rc1 == 0 else "DEAD_LETTER",
                   detail=f"rc={rc1}")
    taskboard.step(tb, "trend_calc", "running")
    rc2 = _run([py, "-X", "utf8", "-u", "-m", "tools.trend_calc",
                "--date", time.strftime("%Y-%m-%d")])
    taskboard.step(tb, "trend_calc",
                   "DONE" if rc2 == 0 else "DEAD_LETTER", detail=f"rc={rc2}")
    ok = rc1 == 0 and rc2 == 0
    taskboard.finish(tb, "ok" if ok else "failed",
                     f"采集 rc={rc1} / 快照 rc={rc2}")
    print(f"[collect_now] 完成 rc1={rc1} rc2={rc2}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
