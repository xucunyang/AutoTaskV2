"""用现有 qwen3.5:4b 试三条不改模型的路，看哪条能让 simple 任务可靠。

背景：短提示下 3/3 能正确调工具，完整任务卡下生成坏 JSON 被 ollama 400。
假设：
  A 基线      完整卡 + 4工具                       （已知失败）
  B think关   完整卡 + 4工具 + think:false        思维模型关掉思考能否稳住JSON
  C 精简工具  完整卡 + 只给 atomic_write/read_range 工具schema本身可能是负担
  D 精简卡    短卡 + 只给 atomic_write            4B可能吃不下那么多约束
每组跑N次，报告：调用成功率 / 工具参数是否为合法dict / 实际写出文件 / 耗时
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue                                   # noqa: E402
from core.executor import build_card                      # noqa: E402
from core.store import Store                              # noqa: E402
from core.tools import TOOL_SCHEMAS                       # noqa: E402

MODEL = "qwen3.5:4b"
N = 3
URL = "http://localhost:11434/api/chat"


def call(messages, tools, num_ctx=16384, think=None, timeout=180):
    body = {"model": MODEL, "messages": messages, "tools": tools,
            "stream": False, "options": {"num_ctx": num_ctx}}
    if think is not None:
        body["think"] = think
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    return d, time.monotonic() - t0


def real_card():
    """真实渲染的任务卡，和生产走同一条路。

    store 放在真实 root（因为要读 templates/），但**不插任务**——
    只借渲染这条路，避免污染真实状态库。
    """
    import shutil
    import tempfile
    td = Path(tempfile.mkdtemp())
    try:
        (td / "templates").mkdir()
        for f in (ROOT / "templates").glob("*"):
            shutil.copy2(f, td / "templates" / f.name)
        s = Store(td)
        s.ensure_plan("p1", template="daily")
        shard = enqueue.validate(json.dumps({
            "task_id": "t1", "plan_id": "p1", "complexity": "simple",
            "objective": "把三个字'探针通'写入 artifacts/bench_simple.md，然后读回确认。",
            "outputs": ["artifacts/bench_simple.md"],
            "budget": {"max_steps": 6, "timeout_s": 240},
        }, ensure_ascii=False).encode("utf-8"), root=td)
        _st, task = s.insert_task(shard)
        return build_card(s, task, "bench")
    finally:
        shutil.rmtree(td, ignore_errors=True)


SHORT_CARD = """任务：把三个字'探针通'写入 artifacts/bench_simple.md。

用 atomic_write 工具写这个文件，content 正好是：探针通
写完用 read_range 读回来确认。"""

MIN_TOOLS = [t for t in TOOL_SCHEMAS
             if t["function"]["name"] in ("atomic_write", "read_range")]


def trial(label, card, tools, think=None):
    ok_call = ok_args = 0
    times = []
    detail = []
    for i in range(N):
        try:
            d, dt = call([{"role": "user", "content": card}], tools, think=think)
            times.append(dt)
            ok_call += 1
            m = d.get("message") or {}
            tc = m.get("tool_calls") or []
            if tc:
                a = (tc[0].get("function") or {}).get("arguments")
                if isinstance(a, dict) and a.get("content"):
                    ok_args += 1
                    detail.append(f"{tc[0]['function']['name']}"
                                  f"({str(a.get('path'))})")
                else:
                    detail.append(f"参数非dict/空:{type(a).__name__}")
            else:
                detail.append("无tool_calls")
        except urllib.error.HTTPError as e:
            detail.append(f"HTTP{e.code}:"
                          f"{e.read().decode('utf-8','replace')[:40]}")
        except Exception as e:
            detail.append(f"{type(e).__name__}:{str(e)[:40]}")
    avg = sum(times) / len(times) if times else 0
    print(f"{label:<28} 调用 {ok_call}/{N}  合法参数 {ok_args}/{N}  "
          f"均耗时 {avg:5.1f}s")
    for d0 in detail:
        print(f"      {d0}")


if __name__ == "__main__":
    card = real_card()
    print(f"真实任务卡 {len(card)} 字符 / 4个工具schema "
          f"{len(json.dumps(TOOL_SCHEMAS))} 字符\n")
    trial("A 基线 完整卡+4工具", card, TOOL_SCHEMAS)
    trial("B 完整卡+4工具 think=false", card, TOOL_SCHEMAS, think=False)
    trial("C 完整卡+2工具 think=false", card, MIN_TOOLS, think=False)
    trial("D 短卡+2工具 think=false", SHORT_CARD, MIN_TOOLS, think=False)
    trial("E 完整卡+4工具 think=true", card, TOOL_SCHEMAS, think=True)
