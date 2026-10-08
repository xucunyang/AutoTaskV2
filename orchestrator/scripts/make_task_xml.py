# -*- coding: utf-8 -*-
"""生成 Windows 计划任务定义 XML（UTF-16LE + BOM）。

**为什么必须用脚本生成，不能用文本编辑器直接写**
`schtasks /create /xml` 只吃 UTF-16，UTF-8 直接报"无法切换编码"。而任何
文本编辑工具保存时都会把它悄悄转回 UTF-8——表现是"文件看着没问题，
注册时才炸"。`tests/test_scheduled_task_xml.py::test_xml_is_utf16_as_schtasks_requires`
就是为这条留的门禁。

**触发器为什么分两种**
- 每天跑的链：`<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>`
- 按月/按半月跑的链：`<ScheduleByMonth><DaysOfMonth><Day>N</Day>...</DaysOfMonth>`

`DaysInterval=14` 看着像"半个月"，其实是错的：它从 StartBoundary 起每 14 天触发，
在 31 天的月份会跑成 1 号、16 号、31 号**三次**。Windows 里"每月 1 号和 15 号"
只能用 DaysOfMonth 精确表达。`MonthsOfYear` 留空 = 每年每月都跑。

**用法**（在 orchestrator/ 下）：
    python -X utf8 scripts/make_task_xml.py              # 生成全部
    python -X utf8 scripts/make_task_xml.py housing_daily   # 只生成一条

改完记得重新注册：
    schtasks /create /tn "\\AutoTaskV2\\<job>" /xml scripts\\<job>_task.xml /f
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent

PYTHON = r"D:\development\program\Scoop\apps\python313\current\python.exe"
ROOT = r"E:\code\AutoTaskByMuseSpark1.3\orchestrator"
SID = "S-1-5-21-3262635161-2430874123-1807270149-1001"

_DAILY = """    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""

_CALENDAR = """    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>
{days}
        </DaysOfMonth>
      </ScheduleByMonth>
    </CalendarTrigger>"""

_HEAD = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>\\AutoTaskV2\\{job}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>true</WakeToRun>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
{trigger}
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>"{root}\\scripts\\run_daily.py" --root "{root}" --job {job} --timeout-min 90</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

# job -> (描述, StartBoundary, 每月第几天 或 None=每天)
SPECS = {
    "housing_daily": (
        "AutoTaskV2 每日地产跟踪（run_daily --job housing_daily）",
        "2026-10-06T10:00:00+08:00", None),
    "housing_fortnight": (
        "AutoTaskV2 六边形战士城市（半月，run_daily --job housing_fortnight）",
        "2026-10-15T06:00:00+08:00", (1, 15)),
    "housing_rr_fiveday": (
        "AutoTaskV2 三城租售比高小区 Top20（每5天，run_daily --job housing_rr_fiveday）",
        "2026-10-11T11:00:00+08:00", (1, 6, 11, 16, 21, 26)),
    "housing_monthly": (
        "AutoTaskV2 房贷贴息分线城市月度跟踪（run_daily --job housing_monthly）",
        "2026-10-08T06:40:00+08:00", (8,)),
}


def render(job: str) -> str:
    description, start, days = SPECS[job]
    if days is None:
        trigger = _DAILY.format(start=start)
    else:
        trigger = _CALENDAR.format(
            start=start,
            days="\n".join(f"          <Day>{d}</Day>" for d in days))
    return _HEAD.format(description=description, sid=SID, trigger=trigger,
                        python=PYTHON, root=ROOT, job=job)


def main(argv: list[str]) -> int:
    jobs = argv[1:] or list(SPECS)
    bad = [j for j in jobs if j not in SPECS]
    if bad:
        print(f"未知 job：{bad}；可用：{list(SPECS)}")
        return 2
    for job in jobs:
        out = SCRIPTS / f"{job}_task.xml"
        # utf-16 = UTF-16LE + BOM，正是 schtasks 要的字节序
        out.write_bytes(render(job).encode("utf-16"))
        raw = out.read_bytes()
        ok = raw[:2] == b"\xff\xfe"
        print(f"{out.name}: {len(raw)} bytes, BOM={raw[:2].hex()} "
              f"{'OK' if ok else 'BAD——不是 UTF-16LE，schtasks 会拒'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))