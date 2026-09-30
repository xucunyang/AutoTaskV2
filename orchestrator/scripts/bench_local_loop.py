"""本地模型在**生产 tool loop**（core.executor._tool_loop）下的可靠性。

重要：这里**不复制循环逻辑**，直接用生产代码 + 生产 provider。
之前这个脚本自己复刻了一份循环，于是它测的一直是"旧代码的副本"，
修好生产代码它还是报同样的错——白花一轮时间。
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue                                  # noqa: E402
from core import providers as pv                          # noqa: E402
from core import tools as toolsmod                        # noqa: E402
from core.executor import build_card, _tool_loop          # noqa: E402
from core.store import Store                              # noqa: E402

OUT = "artifacts/bench_simple.md"
CARD_JSON = {
    "task_id": "t1", "plan_id": "p1", "complexity": "simple",
    "objective": f"把三个字'探针通'写入 {OUT}，然后读回确认。",
    "outputs": [OUT], "budget": {"max_steps": 6, "timeout_s": 240},
}


def make_store(td: Path):
    (td / "templates").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "templates").glob("*"):
        shutil.copy2(f, td / "templates" / f.name)
    s = Store(td)
    s.ensure_plan("p1", template="daily")
    shard = enqueue.validate(json.dumps(dict(CARD_JSON, plan_id="p1"),
                                       ensure_ascii=False).encode("utf-8"),
                              root=td)
    _st, task = s.insert_task(shard)
    return s, build_card(s, task, "bench")


def bench(label, think, n):
    ok_n = wrote_n = 0
    times, why_bad = [], {}
    for _i in range(n):
        td = Path(tempfile.mkdtemp())
        try:
            s, card = make_store(td)
            p = pv.OllamaProvider(base_url="http://localhost:11434",
                                  models={"simple": "qwen3.5:4b"},
                                  think=think, timeout_s=180,
                                  max_concurrency=1)
            box = toolsmod.ToolBox(td)
            t0 = time.monotonic()
            try:
                _resp, steps = _tool_loop(p, card, None, box, {}, 16384)
                why = f"loop_end steps={steps}"
            except Exception as e:                       # noqa: BLE001
                why = f"{type(e).__name__}:{str(e)[:60]}"
            times.append(time.monotonic() - t0)
            f = td / OUT
            wrote = f.exists() and f.stat().st_size > 0
            wrote_n += wrote
            if not wrote:
                why_bad[why] = why_bad.get(why, 0) + 1
            else:
                ok_n += 1
        finally:
            shutil.rmtree(td, ignore_errors=True)
    print(f"\n{label}")
    print(f"  落盘成功 {ok_n}/{n}   均耗时 {sum(times)/len(times):.1f}s")
    for why, c in sorted(why_bad.items(), key=lambda kv: -kv[1]):
        print(f"    失败 {c}次: {why}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=5)
    ap.add_argument("--only", choices=["think_off", "think_default"])
    a = ap.parse_args()
    print(f"模型 qwen3.5:4b  生产 _tool_loop  每组 {a.n} 次")
    if a.only != "think_off":
        bench("think 未设置（默认）", None, a.n)
    if a.only != "think_default":
        bench("think=false", False, a.n)
