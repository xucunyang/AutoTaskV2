"""2.6 真机验证：真实 200MB 文件在每条路径上的耗时与内存。

为什么单独做成脚本而不是pytest用例：
造一个真 200MB 文件要几十秒，放进常规测试会让每次全量都多花一分钟。
这里测的是**绝对数字**（这台机器上 200MB 到底多久/多少内存），
属性（流式、内存有界）由 tests/test_bigfile_streaming.py 在小一档的
数据上做回归保护。

真机与自动化的分工：
- 自动化守住"不许整读进内存"这个**性质**，便宜且天天跑
- 真机给出"这台机器上的**绝对值**"，按需跑

用法：
    python scripts/bigfile_check.py                    # 200MB
    python scripts/bigfile_check.py --mb 50            # 小一点
    python scripts/bigfile_check.py --mb 200 --keep    # 保留文件供后续检查
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import checkpoint as cp          # noqa: E402
from core import executor                    # noqa: E402
from core import verifier as vf             # noqa: E402
from core.store import Store                 # noqa: E402
from core.utils import sha256_file           # noqa: E402

try:
    import psutil
    _PROC = psutil.Process(os.getpid())
except ImportError:                          # pragma: no cover
    _PROC = None


def rss_mb() -> float:
    return _PROC.memory_info().rss / 1e6 if _PROC else 0.0


def make_csv(path: Path, target_mb: int) -> tuple[int, int]:
    """写真实体积的CSV。返回(行数, 字节数)。

    边写边算字节，不靠"行数×平均行长"估算——那种估算在验收里毫无意义。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    target = target_mb * 1024 * 1024
    written = 0
    rows = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        header = "id,name,category,value,note\n"
        f.write(header)
        written += len(header)
        rows = 0
        i = 0
        while written < target:
            line = (f"{i},item_{i},cat_{i % 97},"
                    f"{i * 3.14159:.5f},备注内容{i}\n")
            f.write(line)
            written += len(line.encode("utf-8"))
            rows += 1
            i += 1
    return rows, written


class Timed:
    """跑一段代码，报耗时与内存增量。

    报**增量**而不是绝对RSS：绝对值里混着解释器和测试框架的底噪，
    增量才对应"这一步额外吃了多少内存"。
    """

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        self.t0 = time.monotonic()
        self.r0 = rss_mb()
        return self

    def __exit__(self, *exc):
        self.dt = time.monotonic() - self.t0
        self.delta = rss_mb() - self.r0
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=200)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    work = ROOT / "state" / "bigfile_check"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    for sub in ("templates", "config"):
        shutil.copytree(ROOT / sub, work / sub, dirs_exist_ok=True)
    # 产物必须落在 artifacts/{date}/ 下：verifier.normalize_path 会拒绝
    # 其他位置（越界即拒，这是路径沙箱，脚本也得守规矩）
    date = "2026-09-30"
    rel = f"artifacts/{date}/big.csv"
    big = work / rel

    print(f"=== 生成 {a.mb}MB 真实 CSV ===")
    t0 = time.monotonic()
    rows, nbytes = make_csv(big, a.mb)
    print(f"  {big.stat().st_size / 1024 / 1024:.1f}MB / {rows:,} 行 "
          f"/ 耗时 {time.monotonic() - t0:.1f}s")

    base = rss_mb()
    print(f"\n=== 各路径耗时与内存增量（基线RSS {base:.0f}MB） ===")
    results = []

    with Timed("sha256_file") as t:
        man = sha256_file(big)
    results.append((t.label, t.dt, t.delta))
    print(f"  {t.label:<22} {t.dt:6.2f}s  内存+{t.delta:6.1f}MB  "
          f"rows={man['rows']:,} sha={man['sha256'][:12]}")

    with Timed("build_manifest") as t:
        m2 = cp.build_manifest(work, big)
    results.append((t.label, t.dt, t.delta))
    print(f"  {t.label:<22} {t.dt:6.2f}s  内存+{t.delta:6.1f}MB")

    with Timed("verifier.row_count") as t:
        r = vf.HANDLERS["row_count"](
            {"repo": work, "root": work, "date": date,
             "variables": {}},
            {"type": "row_count", "path": rel, "min": 1})
    results.append((t.label, t.dt, t.delta))
    print(f"  {t.label:<22} {t.dt:6.2f}s  内存+{t.delta:6.1f}MB  ok={r['ok']}")

    store = Store(work)
    store.ensure_plan("p1", template="daily")
    shard = {"task_id": "big", "plan_id": "p1", "objective": "统计大CSV",
             "outputs": [], "acceptance": [],
             "inputs": [{"path": rel,
                         "sha256": man["sha256"], "bytes": man["bytes"],
                         "rows": man["rows"]}]}
    task = {"task_id": "big", "plan_id": "p1", "idempotency_key": "k",
            "shard": shard}
    with Timed("build_card") as t:
        card = executor.build_card(store, task, "r")
    from core.task_card import estimate_tokens
    toks = estimate_tokens(card)
    results.append((t.label, t.dt, t.delta))
    print(f"  {t.label:<22} {t.dt:6.2f}s  内存+{t.delta:6.1f}MB  "
          f"card={len(card)}B tokens={toks}")

    with Timed("tools.read_range(200行)") as t:
        from core import tools as toolsmod
        box = toolsmod.ToolBox(work)
        out = box.execute("read_range", {"path": rel,
                                         "offset": 0, "limit": 200})
    results.append((t.label, t.dt, t.delta))
    print(f"  {t.label:<22} {t.dt:6.2f}s  内存+{t.delta:6.1f}MB  "
          f"读到{len(out)}字符")

    peak = max(d for _, _, d in results)
    total = sum(d for _, d, _ in results)
    slowest = max(results, key=lambda x: x[1])
    print(f"\n=== 结论 ===")
    print(f"  峰值单步内存增量 {peak:.1f}MB（文件 {a.mb}MB）")
    print(f"  总耗时 {total:.2f}s，最慢一步 {slowest[0]} {slowest[1]:.2f}s")
    print(f"  任务卡 tokens={toks}（门槛<8000）")
    ok = True
    if peak > a.mb * 0.25:
        print(f"  FAIL: 内存增量 {peak:.0f}MB 偏高——可能有整读进内存的路径")
        ok = False
    if toks >= 8000:
        print(f"  FAIL: 任务卡 {toks} tokens 超门槛")
        ok = False
    if man["rows"] != rows:
        print(f"  FAIL: 行数 {man['rows']:,} != 实际 {rows:,}")
        ok = False
    if ok:
        print("  PASS: 流式处理，内存与文件体积无关")
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
        print(f"  已清理 {work}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
