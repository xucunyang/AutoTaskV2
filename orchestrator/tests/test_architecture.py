"""架构契约测试：分层规则违规 = 本测试失败。

为什么用测试跑 import-linter 而不是只靠 CI：
文档里的架构规则约束不了任何人（包括 AI）——它写代码时上下文里
没有"回忆"，只有眼前的 import 路径。唯一有效的约束是**违规即失败**：
pytest 是每天被跑几十次的命令，把守卫焊进去，就不存在"忘了跑 lint"
这回事。AI 改出循环依赖/层次倒置时，第一次跑测试就会红，它会拿着
报错自己修——这是文档永远给不了的反馈回路。

契约内容见 .importlinter（分层规则 + search 门面规则）。
新增模块时需要在 .importlinter 的 layers 里登记它的层位。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_architecture_contracts() -> None:
    """lint-imports 全部契约必须通过（exit 0）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c",
         "from importlinter.cli import lint_imports_command; "
         "lint_imports_command()"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "架构契约被打破，详见输出：\n"
        f"{result.stdout}\n{result.stderr}\n"
        "修复方向：改 import 走合法方向/门面，或在 .importlinter 登记"
        "新模块的层位（登记前先想清楚它到底属于哪一层）。"
    )
