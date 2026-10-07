"""Windows 计划任务定义文件的门禁：编码、结构、两个 job 的时间错开。

三个都是真实踩过的坑，每个对应一条断言：

1. **编码**：`schtasks /create /xml` 只吃 UTF-16（UTF-8 直接报
   "无法切换编码"）。用文本编辑工具改这个文件会把它悄悄转成 UTF-8，
   表现是"文件看着没问题，注册时才炸"。所以断言 BOM 与声明一致。
2. **schema**：`WakeToRun` / `StartWhenAvailable` / `RunOnlyIfNetworkAvailable`
   属于 `<Settings>`，**不是** `<CalendarTrigger>` 的子元素。放进 trigger
   会报 "contains an unexpected node"。这条最初的手写 XML 就是这么废掉的。
3. **时间错开**：两个 run_daily 各自的 tick 看的是全库 active plans，
   同时跑会互相派发、重复验收。daily 08:00、housing 10:00（> timeout-min 90）。
"""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
# 每天跑一次的链：ScheduleByDay + DaysInterval=1
DAILY_JOBS = ("daily_report", "housing_daily", "policy_daily")
# 2026-10-07 新增的慢链：**不是**每天跑，触发器换成 ScheduleByMonth +
# DaysOfMonth。Windows 的 ScheduleByDay/DaysInterval 表达不出"每月 1 号和 15 号"
# （DaysInterval=14 在 31 天的月份会跑成 1/16/31 三次），
# 所以这两条与上面三条的触发器形状本就不同，断言必须分开写。
CALENDAR_JOBS = {
    # job -> 每月第几天
    "housing_fortnight": {"1", "15"},
    "housing_monthly": {"8"},
}
JOBS = DAILY_JOBS + tuple(CALENDAR_JOBS)
# 只能出现在 <Settings> 里，放进 trigger 就是 schema 错误
SETTINGS_ONLY = ("WakeToRun", "StartWhenAvailable", "RunOnlyIfNetworkAvailable")


def _load(job: str) -> Path:
    p = SCRIPTS / f"{job}_task.xml"
    assert p.is_file(), f"缺少计划任务定义 {p}"
    return p


def _tree(job: str) -> ET.Element:
    return ET.parse(_load(job)).getroot()


def _args(job: str) -> str:
    node = _tree(job).find(".//t:Arguments", NS)
    assert node is not None and node.text, f"{job} 的 Arguments 为空"
    return node.text


def test_xml_is_utf16_as_schtasks_requires():
    """UTF-8 的 XML schtasks 拒绝加载；BOM 与声明必须都是 UTF-16。"""
    for job in JOBS:
        raw = _load(job).read_bytes()
        assert raw[:2] == b"\xff\xfe", \
            f"{job}_task.xml 不是 UTF-16LE（schtasks 只吃 UTF-16）"
        decl = raw[:80].decode("utf-16-le", errors="replace")
        assert 'encoding="UTF-16"' in decl, \
            f"{job}_task.xml 的编码声明与实际字节不符：{decl.splitlines()[0]}"


def test_arguments_carry_the_right_job():
    """--job 必须和文件名/任务名一致：错一个字母就是错的 plan_id。"""
    for job in JOBS:
        args = _args(job)
        assert "run_daily.py" in args, f"{job} 没有调 run_daily"
        assert f"--job {job}" in args, f"{job} 的 --job 对不上任务名"
        assert args.count("--job") == 1


def test_trigger_has_no_settings_only_elements():
    for job in JOBS:
        root = _tree(job)
        trig = root.find(".//t:Triggers", NS)
        assert trig is not None, f"{job} 没有 Triggers"
        for tag in SETTINGS_ONLY:
            assert trig.find(f".//t:{tag}", NS) is None, (
                f"{job}: {tag} 属于 <Settings>，放进 <Triggers> 会被 "
                f"schtasks 判为 unexpected node")
        settings = root.find("t:Settings", NS)
        assert settings is not None, f"{job} 没有 Settings"
        for tag in ("WakeToRun", "StartWhenAvailable"):
            node = settings.find(f"t:{tag}", NS)
            assert node is not None and node.text == "true", \
                f"{job}: Settings/{tag} 必须为 true（唤醒 + 错过补跑）"


def test_principal_is_an_account_not_the_machine_name():
    """UserId 写成机器名 XUNCJ 时注册报"账户不存在"——必须是 SID 或 DOMAIN\\user。"""
    for job in JOBS:
        uid = _tree(job).findtext(".//t:UserId", default="", namespaces=NS)
        assert uid, f"{job} 没有 UserId"
        assert uid.startswith("S-1-5-21-") or "\\" in uid, \
            f"{job}: UserId={uid!r} 看起来是机器名而不是账户"


def test_jobs_do_not_overlap():
    """daily 先跑（timeout-min 90 → 最迟 09:30 退出），housing 才起跑。"""
    hours = {}
    for job in DAILY_JOBS:
        sb = _tree(job).findtext(".//t:StartBoundary", default="", namespaces=NS)
        assert len(sb) >= 13 and sb[4] == "-" and sb[7] == "-", \
            f"{job}: StartBoundary 格式不对：{sb!r}"
        hours[job] = int(sb[11:13])
    assert hours["daily_report"] < hours["housing_daily"], \
        f"两个 job 的触发时间重叠：{hours}"
    assert hours["housing_daily"] - hours["daily_report"] >= 2, \
        f"daily 的 --timeout-min 90 需要至少 1.5h 错开，实际只差 " \
        f"{hours['housing_daily'] - hours['daily_report']}h：{hours}"
    # 同一天各跑一次
    for job in DAILY_JOBS:
        root = _tree(job)
        assert root.findtext(".//t:DaysInterval", namespaces=NS) == "1", \
            f"{job} 不是每天一次"


def test_slow_jobs_use_schedule_by_month():
    """慢链必须挂在 DaysOfMonth 上，不能是 ScheduleByDay。

    这条钉的是 2026-10-07 用户拍板的那件事：六边形城市「每半个月一次」、
    房贷贴息分线「每个月统计一次」——因为它们吃的是月度/季度口径的数据，
    每天跑等于每天抄上一期那批数（"每天执行完数据都一样，没有什么意义"）。

    写成 ScheduleByDay+DaysInterval=14 看着像半个月，其实不是：
    31 天的月份会跑成 1 号、16 号、31 号**三次**，
    而 ScheduleByMonth + DaysOfMonth 才是精确的"每月 1 号和 15 号"。
    """
    for job, want_days in CALENDAR_JOBS.items():
        root = _tree(job)
        trig = root.find(".//t:Triggers", NS)
        assert trig is not None, f"{job} 没有 Triggers"
        by_day = trig.find(".//t:ScheduleByDay", NS)
        assert by_day is None, (
            f"{job} 还在用 ScheduleByDay——DaysInterval=14 在 31 天的月份"
            f"会跑三次，不是半个月")
        by_month = trig.find(".//t:ScheduleByMonth", NS)
        assert by_month is not None, f"{job} 没有 ScheduleByMonth"
        got = {d.text for d in by_month.findall(".//t:Day", NS)}
        assert got == want_days, (
            f"{job} 的 DaysOfMonth={sorted(got)}，与 schedule.yaml 声明的"
            f" cron 不一致（应含 {sorted(want_days)}）")


def test_slow_jobs_are_not_in_daily_cron():
    """慢链在 schedule.yaml 里也不能写成 "* * *"。

    XML 对了但 cron 写错的情况同样致命——谁哪天启了 APScheduler 守护进程，
    就会按 cron 把月线当日线跑一遍（这正是 housing 曾长期写着 08:00 的坑）。
    """
    import yaml
    sched = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "schedule.yaml")
        .read_text(encoding="utf-8")) or {}
    by_id = {j["id"]: j for j in (sched.get("jobs") or [])}
    for job, want_days in CALENDAR_JOBS.items():
        assert job in by_id, f"{job} 不在 schedule.yaml 里"
        dom = str(by_id[job].get("cron") or "").split()
        assert len(dom) == 5, f"{job} 的 cron 形状不对：{by_id[job]['cron']!r}"
        assert dom[2] != "*", (
            f"{job} 的 cron={by_id[job]['cron']!r} 是每天跑——"
            f"与「{job} 走慢节奏」的约定冲突")
        assert set(dom[2].split(",")) == want_days, (
            f"{job} 的 cron 日字段={dom[2]!r} 与 XML 的 DaysOfMonth 不一致")
