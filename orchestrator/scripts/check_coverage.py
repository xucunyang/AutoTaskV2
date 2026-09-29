"""覆盖率硬线门禁（Phase4 §2，V7§12.8）：store+lease+verifier+gateway+planner+search ≥80%。

为什么是独立脚本而不是一条pytest用例：
那条用例会在测试进程里再跑一遍全量测试，而全量测试又包含它自己——
无限递归，跑不完。所以覆盖率门禁属于"放行检查"，不放进单测。

只统计设计点名的六个模块，不用全仓库平均：
全仓库平均会被样板代码稀释，关键路径有没有洞反而看不出来。

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

MODULES = ("core.store", "core.lease", "core.verifier", "core.gateway",
           "core.planner", "core.search")
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
    for m in MODULES:
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
    covered = {k: v for k, v in rows.items() if k in MODULES or
               k.startswith("core.search")}
    total = sum(covered.values()) / max(1, len(covered))
    print("=== 覆盖率明细 ===")
    for k in sorted(covered):
        flag = "ok " if covered[k] >= min_pct else "LOW"
        print(f"  {flag} {k:<24} {covered[k]:.1f}%")
    print(f"  ---> 六模块平均 {total:.1f}%（门槛 {min_pct:.0f}%）")
    failed = proc.returncode != 0
    if total < min_pct:
        print(f"FAIL: 覆盖率 {total:.1f}% < {min_pct:.0f}%")
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
