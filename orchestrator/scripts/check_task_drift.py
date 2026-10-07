"""核对**已注册**的 Windows 计划任务与 schedule.yaml / XML 是否一致。

排查 2026-10-06 用户贴的那张 toast 时写的：
「❌ 起不来 housing_daily · 2026-10-05 / 退出码 3：schedule.yaml 里没有这个
job_id / 已知 job: daily_report, housing_daily」。

已知 job 只有两个 = 那是 2026-10-05 的 schedule.yaml（finance/hardware 还没加）。
而 `--job` 传的值又在"已知 job"里还报错 → 说明传进去的是**拼错的那个**
（housing_dayly，少个 i）：template_for 找不到 → 退出码 3。

关键在于：**这类错误 tests 抓不到**。
- test_every_job_has_matching_scheduled_task_xml 只扫磁盘上的 XML；
- 那个拼错的任务是**注册进 Windows 的**，磁盘上没有对应 XML，
  于是"孤儿 XML"那条也发现不了（那条查的是反方向）。

所以只能直接问 Windows：注册了几个、每个的 Arguments 长什么样。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

TASK_PATH = "\\AutoTaskV2\\"


PS1 = r"""
$ErrorActionPreference = 'Stop'
Get-ScheduledTask -TaskPath '\AutoTaskV2\' | ForEach-Object {
    $t = $_
    $a = $t.Actions | Where-Object { $_.Execute -like '*python*' } | Select-Object -First 1
    [pscustomobject]@{
        Name = $t.TaskName
        Args = [string]$a.Arguments
    }
} | ConvertTo-Json -Compress
"""


def _registered() -> dict[str, str]:
    """{task_name: Arguments}。取不到就返回空 dict（不是本机 Windows 时跳过）。

    **必须走 .ps1 + `-File`，不能拼 `powershell -Command`**：
    python → bash/PowerShell → powershell 三层引号会把 `$_` 吃掉
    （实测报"无法将 Where-Object 识别为 cmdlet"，而脚本本身看着完全正常，
    失败信息还指不到真正原因）。交接文档 §7 记着同一条坑。
    """
    import tempfile
    with tempfile.TemporaryDirectory(prefix="drift_") as td:
        ps1 = Path(td) / "probe.ps1"
        ps1.write_text(PS1, encoding="utf-8-sig")   # 带 BOM：PS 5.1 认中文/引号
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    txt = (out.stdout or "").strip()
    if not txt:
        return {}
    try:
        data = json.loads(txt)
    except ValueError:
        return {}
    if isinstance(data, dict):          # 只有一个任务时不是数组
        data = [data]
    return {str(d.get("Name")): str(d.get("Args") or "") for d in data}


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8"))
    jobs = {j["id"]: j for j in (cfg.get("jobs") or [])}
    reg = _registered()

    if not reg:
        print("取不到已注册任务（非 Windows / 无权限），跳过。")
        return 0

    problems: list[str] = []
    for name, args in sorted(reg.items()):
        if name not in jobs:
            problems.append(f"注册了 {name!r}，但 schedule.yaml 里没有这个 job"
                            f" —— 它每次触发都会退出码 3（正是那张 toast 的来源）")
            continue
        xml = ROOT / "scripts" / f"{name}_task.xml"
        if not xml.exists():
            problems.append(f"{name}: 已注册但磁盘上没有 {xml.name}（改不回去、审不了）")
        if f"--job {name}" not in args:
            problems.append(f"{name}: 注册的 Arguments 里没有 '--job {name}'：{args[:120]}")

    for job_id in sorted(jobs):
        if job_id not in reg:
            problems.append(f"schedule.yaml 有 {job_id}，但 Windows 里没注册 —— 这条链永远不会自动跑")

    print(f"schedule.yaml 的 job：{', '.join(sorted(jobs))}")
    print(f"已注册的任务　　　：{', '.join(sorted(reg))}")
    print()
    if problems:
        print("发现不一致：")
        for p in problems:
            print(f"  ✗ {p}")
        print(json.dumps({"ok": False, "problems": problems}, ensure_ascii=False, indent=2))
        return 1
    print("✅ 注册任务与 schedule.yaml / XML 完全一致，没有会退出码 3 的错配")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())