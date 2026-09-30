"""大文件流式处理的**性质**回归保护（2.6 真机的自动化那半）。

真机绝对值由 scripts/bigfile_check.py 按需跑（造200MB要几十秒，
不该进常规测试）。这里守住的是那个会随代码改动而失效的**性质**：
处理大文件时内存占用必须与文件体积无关。

"无关"怎么测才靠谱：不能只看绝对内存（解释器底噪几十MB会淹没差异），
也不能只跑一个大尺寸（一次侥幸说明不了问题）。做法是**两个尺寸对比**——
如果代码哪天改成整读，20MB和200MB的内存增量会同步暴涨，比值立刻拉开。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import checkpoint as cp                        # noqa: E402
from core import executor as ex                          # noqa: E402
from core import tools as toolsmod                       # noqa: E402
from core import verifier as vf                          # noqa: E402
from core.store import Store                              # noqa: E402
from core.utils import sha256_file                       # noqa: E402

DATE = "2026-09-30"

try:
    import psutil
    _PROC = psutil.Process(os.getpid())
except ImportError:                                       # pragma: no cover
    _PROC = None


def rss_mb() -> float:
    return _PROC.memory_info().rss / 1e6 if _PROC else 0.0


def _rss_grows_by(action, floor_mb: float = 6.0):
    """跑action，返回内存增量；用"多跑几次取最大"压住GC的不确定性。"""
    gc_collect = __import__("gc")
    gc_collect.collect()
    base = rss_mb()
    peak = 0.0
    for _ in range(3):
        action()
        peak = max(peak, rss_mb() - base)
    # RSS 只涨不跌是常态（分配器不还给OS），所以给一个地板：
    # 小文件那档的固定开销不该被算成"大文件吃内存"
    return max(peak, 0.0), floor_mb


def make_csv(path: Path, target_mb: float) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    target = int(target_mb * 1024 * 1024)
    written = 0
    rows = 0
    with path.open("w", encoding="utf-8", newline="") as f:
        head = "id,name,value,note\n"
        f.write(head)
        written += len(head)
        i = 0
        while written < target:
            line = f"{i},item_{i},{i * 3.14159:.5f},备注{i}\n"
            f.write(line)
            written += len(line.encode("utf-8"))
            rows += 1
            i += 1
    return rows


@pytest.fixture(scope="module")
def sizes(tmp_path_factory):
    """造两档尺寸，供"内存与体积无关"的对比使用。"""
    base = tmp_path_factory.mktemp("bigfile")
    out = {}
    for mb in (4, 40):
        root = base / f"r{mb}"
        (root / "templates").mkdir(parents=True)
        for f in (ROOT / "templates").glob("*"):
            (root / "templates" / f.name).write_bytes(f.read_bytes())
        rel = f"artifacts/{DATE}/big.csv"
        rows = make_csv(root / rel, mb)
        out[mb] = (root, rel, rows)
    return out


def test_sha256_memory_independent_of_size(sizes):
    """sha256 必须流式。整读的话40MB那档的增量会是4MB那档的10倍。"""
    deltas = {}
    for mb, (root, rel, _rows) in sizes.items():
        big = root / rel
        assert big.stat().st_size >= mb * 1024 * 1024 * 0.9
        d, _ = _rss_grows_by(lambda p=big: sha256_file(p))
        deltas[mb] = d
    small, big = deltas[4], deltas[40]
    # 文件大了10倍，内存增量不该跟着涨10倍。留宽松余量只为抗GC噪声，
    # 真正的"整读"会超出这个上限一个数量级。
    assert big < small + 8.0, (
        f"内存随文件体积增长：4MB→{small:.1f}MB, 40MB→{big:.1f}MB")


def test_manifest_memory_independent_of_size(sizes):
    deltas = {}
    for mb, (root, rel, _rows) in sizes.items():
        d, _ = _rss_grows_by(lambda p=root / rel: cp.build_manifest(root, p))
        deltas[mb] = d
    assert deltas[40] < deltas[4] + 8.0, \
        f"build_manifest 疑似整读：{deltas}"


def test_row_count_memory_independent_of_size(sizes):
    """verifier 的 row_count 也要流式——它数的是全文件行数。"""
    deltas = {}
    for mb, (root, rel, _rows) in sizes.items():
        ctx = {"repo": root, "root": root, "date": DATE, "variables": {}}
        rule = {"type": "row_count", "path": rel, "min": 1}
        d, _ = _rss_grows_by(
            lambda c=ctx, r=rule: vf.HANDLERS["row_count"](c, r))
        deltas[mb] = d
    assert deltas[40] < deltas[4] + 8.0, \
        f"row_count 疑似整读：{deltas}"


def test_read_range_returns_bounded_slice_not_whole_file(sizes):
    """read_range 无论文件多大都只返回请求的那点。

    这是工具层唯一的读入口，模型就靠它避免把大文件塞进上下文。
    """
    for mb, (root, rel, _rows) in sizes.items():
        box = toolsmod.ToolBox(root)
        out = box.execute("read_range", {"path": rel, "offset": 0, "limit": 200})
        assert len(out) <= toolsmod.MAX_READ_CHARS
        assert len(out) < 200 * 200, f"{mb}MB 那档返回了过多内容"
        # 明确要求一个超出文件末尾的 offset，不该把整文件倒出来
        tail = box.execute("read_range", {"path": rel, "offset": 10 ** 9})
        assert "已在文件末尾" in tail


def test_task_card_stays_small_for_big_inputs(sizes):
    """40MB 产物的任务卡仍要远低于 8k tokens。

    prompt 里只放 路径+哈希+行数+前5行preview，不放内容——
    放进去的话一个40MB文件就能把上下文撑爆，而撑爆的代价是整轮返工。
    """
    from core.task_card import estimate_tokens
    for mb, (root, rel, _rows) in sizes.items():
        s = Store(root)
        s.ensure_plan("p1", template="daily")
        man = sha256_file(root / rel)
        task = {"task_id": "big", "plan_id": "p1", "idempotency_key": "k",
                "shard": {"objective": "统计大CSV", "outputs": [],
                          "acceptance": [],
                          "inputs": [{"path": rel, "sha256": man["sha256"],
                                      "bytes": man["bytes"],
                                      "rows": man["rows"]}]}}
        card = ex.build_card(s, task, "r")
        toks = estimate_tokens(card)
        assert toks < 8000, f"{mb}MB 产物让任务卡涨到 {toks} tokens"
        assert rel in card, "至少要给模型产物路径"


def test_big_file_rows_and_sha_are_correct(sizes):
    """内存省了不能牺牲正确性：行数与sha必须和真实文件对得上。"""
    for mb, (root, rel, rows) in sizes.items():
        got = sha256_file(root / rel)
        assert got["rows"] == rows, f"{mb}MB: 行数 {got['rows']} != {rows}"
        assert len(got["sha256"]) == 64
        # 两次算出的sha必须一致（不能是占位或随时间变化）
        assert got["sha256"] == sha256_file(root / rel)["sha256"]


def test_big_file_scan_time_is_reasonable(sizes):
    """40MB 全量扫描应该在几秒内完成。

    用宽松阈值（10s）：它防的是"退化成 O(n²) 或逐行重开文件"这类
    结构性退化，不是这台机器的具体速度。
    """
    root, rel, _rows = sizes[40]
    t0 = time.monotonic()
    sha256_file(root / rel)
    dt = time.monotonic() - t0
    assert dt < 10.0, f"40MB 扫描耗时 {dt:.1f}s，疑似结构性退化"
