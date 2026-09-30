"""真实端到端：建任务 → 编排派发 → tool loop 写产物 → 验收 → 落状态。

用真实 .env 里的 key 和真实模型，不打桩。这是唯一能发现"代码能跑但代码不对"
的方式——本轮就靠它抓到两个单测抓不到的真bug（见下）。

用法：
    python scripts/e2e_real.py                # 跑 simple/medium/complex 各一个
    python scripts/e2e_real.py --keep         # 保留上一轮产物（默认先清）

它抓过的真bug（2026-09-29/30）：
1. route() 用 getattr 取 complexity，编排层传的是 dict → 静默当simple →
   全部路由到本地小模型。单测全用带属性的对象，所以全绿。
2. 终态转移沿用任务开始时的 version，而心跳续租每次都把version+1 →
   跑超过一个心跳周期的任务永远到不了SUBMITTED，卡在RUNNING。
   单测的假provider秒回，测不到。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue, executor, gateway          # noqa: E402
from core.orchestrator import Orchestrator            # noqa: E402
from core.store import Store                          # noqa: E402

SHARDS = [
    {"complexity": "simple", "needs_web": False,
     "objective": "把三个字'探针通'写入 artifacts/simple_out.md，然后读回确认。",
     "outputs": ["artifacts/simple_out.md"],
     "budget": {"max_steps": 6, "timeout_s": 240}},
    {"complexity": "medium", "needs_web": True,
     "objective": "检索 DeepSeek V4 Pro 的产品定位，把结论写入 artifacts/medium_out.md，"
                  "并在文末列出参考来源。",
     "outputs": ["artifacts/medium_out.md"],
     "budget": {"max_steps": 8, "timeout_s": 300}},
    {"complexity": "complex", "needs_web": False,
     "objective": "分析论断'简单任务用大模型是浪费'的漏洞，写入 artifacts/complex_out.md。",
     "outputs": ["artifacts/complex_out.md"],
     "budget": {"max_steps": 8, "timeout_s": 420}},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="不清上一轮产物（默认清，避免旧文件让验收假通过）")
    args = ap.parse_args()

    for sh in SHARDS:
        for o in sh["outputs"]:
            p = ROOT / o
            if not args.keep and p.exists():
                p.unlink()

    store = Store(ROOT)
    plan_id = f"e2e_real_{int(time.time())}"
    store.ensure_plan(plan_id, template="daily")
    print("plan:", plan_id)

    ids = []
    for sh in SHARDS:
        raw = dict(sh, plan_id=plan_id, task_id=f"t_{sh['complexity']}")
        if raw["needs_web"]:
            raw.setdefault("freshness", "recent")
        shard = enqueue.validate(json.dumps(raw, ensure_ascii=False).encode("utf-8"),
                                 root=ROOT)
        status, task = store.insert_task(shard)
        assert status == "created", f"{status}: {task}"
        ids.append((task["task_id"], shard["complexity"]))
        print(f"  enqueue {task['task_id']} complexity={shard['complexity']}")

    orch = Orchestrator(ROOT, executor_fn=executor.run_task)
    print("模型注册表:", sorted(orch.models))
    print("检索器:", type(orch._search_provider).__name__)

    ok = 0
    for tid, complexity in ids:
        orch.refresh_ready(plan_id)
        ctx = gateway.build_ctx(orch.gw_cfg)
        r = gateway.route(store.get_task(plan_id, tid), ctx)
        print(f"\n----- {tid} complexity={complexity} -> 路由 {r['model']} "
              f"({r['reason']})")
        t0 = time.monotonic()
        orch._dispatch_one(store.get_task(plan_id, tid))
        dt = time.monotonic() - t0
        after = store.get_task(plan_id, tid)
        sh = after["shard"]
        print(f"  outcome={after['status']}  用时 {dt:.1f}s")
        for o in sh.get("outputs") or []:
            p = ROOT / o
            mf = p.parent / f"{p.stem}.manifest.json"
            if p.exists() and p.stat().st_size > 0:
                print(f"  [产物] {o}  {p.stat().st_size}B  {p.read_text(encoding='utf-8', errors='replace')[:60]!r}")
                if mf.exists():
                    m = json.loads(mf.read_text(encoding="utf-8"))
                    print(f"         manifest sha={m.get('sha256', '')[:12]} bytes={m.get('bytes')}")
            else:
                print(f"  [产物] {o} 缺失")
        if after["status"] == "SUBMITTED":
            ok += 1

    print(f"\n=== 汇总：{ok}/{len(ids)} 提交成功 ===")
    for row in store.list_plan_tasks(plan_id):
        sh = row.get("shard") or {}
        print(f"  {row['task_id']:<10} {row['status']:<10} c={sh.get('complexity')}")
    return 0 if ok == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
