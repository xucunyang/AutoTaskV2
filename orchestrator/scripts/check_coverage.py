"""覆盖率硬线门禁（Phase4 §2，V7§12.8）：关键模块 ≥80%。

为什么是独立脚本而不是一条pytest用例：
那条用例会在测试进程里再跑一遍全量测试，而全量测试又包含它自己——
无限递归，跑不完。所以覆盖率门禁属于"放行检查"，不放进单测。

只统计关键路径模块，不用全仓库平均：
全仓库平均会被样板代码稀释，关键路径有没有洞反而看不出来。

模块清单是**会改变行为**的那些，不是"文件最大"的那些。此前只盯六个，
结果 privacy/timing 长期 0%、governor 59%——而 governor 更糟：
它算出来的profile**编排器从不消费**，等于一整套动态算力机制没接线。
统计口径漏掉的模块不会自己变好，只会一直烂着。

用法：
    python scripts/check_coverage.py            # 打印明细，不达标退出码1
    python scripts/check_coverage.py --min 85   # 自定义门槛
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# 核心契约与决策逻辑
MODULES = ("core.store", "core.lease", "core.verifier", "core.gateway",
           "core.planner", "core.search")
# 2026-09-30 补进门禁：此前完全没被统计，privacy/timing 是0%、
# governor 只有59%。三个都是"触发即改变行为"的模块。
ADDED = ("core.governor", "core.privacy", "core.timing", "core.orchestrator",
         "core.executor", "core.tools", "core.providers", "core.utils",
         "core.report", "core.checkpoint", "core.enqueue", "core.scheduler",
         "core.notifier", "core.verify_orchestrator", "core.task_card",
         "core.context", "core.search")
ALL_MODULES = tuple(dict.fromkeys(MODULES + ADDED))
DEFAULT_MIN = 80.0
# 注意路径里可能有多个分隔符（core\search\base.py），
# 字符类必须同时允许反斜杠和斜杠，否则子模块会被静默丢掉——
# 那样即使 search 覆盖率是0%，门禁也会显示"通过"。
ROW_RE = re.compile(r"^(core[\\/][A-Za-z0-9_.\\/-]+)\s+\d+\s+\d+\s+(\d+)%")


def _norm(name: str) -> str:
    """把模块名归一：core\\verifier.py / core/verifier.py → core.verifier。
    覆盖率报告给的是文件路径，--cov要的是模块名，两边格式不同，
    不归一就永远匹配不上（看着像"没数据"，其实是名字对不上）。"""
    n = name.replace("\\", "/")
    if n.endswith(".py"):
        n = n[:-3]
    return n.replace("/", ".")


def run(root: Path, min_pct: float) -> int:
    cmd = [sys.executable, "-m", "pytest", "tests/", "-q",
           "--cov-report=term", "--cov-fail-under=0", "-p", "no:cacheprovider"]
    for m in ALL_MODULES:
        cmd += [f"--cov={m}"]
    proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    out = proc.stdout
    rows = {}
    for line in out.splitlines():
        m = ROW_RE.match(line.strip())
        if m:
            rows[_norm(m.group(1))] = float(m.group(2))
    if not rows:
        print(out[-3000:])
        print(proc.stderr[-2000:])
        print("FAIL: 没拿到覆盖率数据（pytest-cov 未安装？）")
        return 2
    # core.search 是包，按子模块聚合
    search_mods = [v for k, v in rows.items() if k.startswith("core.search")]
    covered = {k: v for k, v in rows.items() if k in ALL_MODULES or
               k.startswith("core.search")}
    total = sum(covered.values()) / max(1, len(covered))
    print("=== 覆盖率明细 ===")
    for k in sorted(covered):
        flag = "ok " if covered[k] >= min_pct else "LOW"
        print(f"  {flag} {k:<28} {covered[k]:.1f}%")
    print(f"  ---> 平均 {total:.1f}%（门槛 {min_pct:.0f}%，共{len(covered)}个模块）")
    # 单模块也要过线：只看平均会让"一个模块0%、另一个100%"互相掩盖，
    # 而0%那个恰恰是最危险的（它意味着那段代码从没被执行过）。
    low = [k for k, v in covered.items() if v < min_pct]
    failed = proc.returncode != 0
    if total < min_pct:
        print(f"FAIL: 平均覆盖率 {total:.1f}% < {min_pct:.0f}")
        return 1
    if low:
        print(f"FAIL: 以下模块低于门槛: {sorted(low)}")
        return 1
    if failed:
        print("FAIL: 测试本身有失败，先修测试")
        return 1
    print("PASS: 覆盖率达标且测试全绿")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=DEFAULT_MIN)
    args = ap.parse_args()
    root = Path(__file__).resolve().parent.parent
    return run(root, args.min)


if __name__ == "__main__":
    raise SystemExit(main())
