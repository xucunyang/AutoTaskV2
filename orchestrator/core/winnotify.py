"""Windows 桌面通知：弹框 + 声音。

**为什么需要它**：计划任务在 08:00 跑批时没人盯着终端。退出码、
`reports/*.log`、`logs/structured.jsonl` 全都等于没发生——用户明确
要求"像 Windows 通知一样有弹框和声音"，控制台打印不算通知。

**选型**（都不需要装任何模块）：
1. 首选 WinRT Toast（`Windows.UI.Notifications`）。PowerShell 5.1 直接
   访问 WinRT，用 WindowsPowerShell 的 AUMID 发送（标题栏显示
   "Windows PowerShell"，无所谓，要的是弹框和提示音）。
2. Toast 失败回退 `NotifyIcon.ShowBalloonTip`：老式气泡，带系统提示音，
   至少保证"有动静"。两条路都不通才返回 False。

**三条硬约束**：
- 通知是**旁路**：全程超时保护（默认 15s），任何异常都吞掉返回 False，
  绝不能把 run_daily 拖住或挂死——报告没跑完是因为通知失败，不可接受。
- 传给 PowerShell 的脚本必须**纯 ASCII**：中文经命令行/管道都会踩编码坑。
  做法是把 toast XML UTF-8 → base64 后内联进脚本，PowerShell 侧解码。
- 失败比成功更响：成功用 `Notification.Default`，非 0 退出用
  `Notification.LoopingAlarm`（loop=false 只响一遍，但音色明显不同）。
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

from core.utils import jlog

# 测试时**绝不**弹真通知。2026-10-06 实测的教训：
# `tests/test_run_daily.py::test_unknown_job_fails_loudly` 故意用
# `--job housing_dayly`（拼错）跑 run_daily 来验证退出码 3，而那条路径会
# 调 notify()——于是**每跑一次全量 pytest，就往用户桌面弹一次
# "❌ 起不来 housing_dayly"**。当天弹了 15 次，用户以为生产在报错，
# 排查了半天：通知库里 ArrivalTime 一路排到当天 15:56，
# 而生产五条链全部 LastResult=0。
#
# 通知是"找人"的东西，测试弹它等于**用假警报训练用户忽略真警报**——
# 这比不弹更糟。所以这里做成硬闸：环境变量或 pytest 命中就短路返回。
SUPPRESS_ENV = "AUTOTASK_NO_NOTIFY"
FORCE_ENV = "AUTOTASK_FORCE_NOTIFY"


def suppressed() -> bool:
    """要不要静默（测试/演练）。

    判定顺序是有意的，**force 必须在最前**：
    - `AUTOTASK_FORCE_NOTIFY`：显式强制真弹。给 `tests/test_winnotify.py`
      用——那个模块测的就是通知本身，得证明"闸关掉之后真的会弹"。
      不给这个开关的话，它只能在 setup 里 delenv `PYTEST_CURRENT_TEST`，
      而 pytest 在 **call 阶段会再设一次**，删了也没用（踩过）。
    - `AUTOTASK_NO_NOTIFY`：`tests/conftest.py` 给每条测试设的全局静默。
      它走 os.environ，**子进程自动继承**，所以从测试里 spawn 出来的
      run_daily 一样被拦住。
    - `PYTEST_CURRENT_TEST`：兜底。防的是"没加载我们的 conftest"的情况
      （别的 runner、别人的 checkout）——那种时候环境变量没人设。
    """
    if os.environ.get(FORCE_ENV):
        return False
    if os.environ.get(SUPPRESS_ENV):
        return True
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))

# WindowsPowerShell 的 AUMID：没注册自己的 AppUserModelId 时用它，
# CreateToastNotifier 接受的是 AUMID 而不是任意字符串。
POWERSHELL_AUMID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
    "\\WindowsPowerShell\\v1.0\\powershell.exe"
)
SOUND_OK = "ms-winsoundevent:Notification.Default"
SOUND_FAIL = "ms-winsoundevent:Notification.LoopingAlarm"
DEFAULT_TIMEOUT_S = 15.0

_PS_PREAMBLE = (
    "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))\n"
)


def _is_windows() -> bool:
    return os.name == "nt"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _link_uri(path) -> str:
    """本地路径 → `file:///` URI（给 toast 的 protocol 按钮跳转用）。

    `Path.as_uri()` 会把空格/中文/`#` 逐段百分号编码——不编码的话点了没反应，
    而"点了没反应"是最难排查的一种失败。

    两道闸，任何一道不过就返回空串、调用方据此**不渲染这个按钮**：
    - 空路径：`Path("").resolve()` 会解析成**当前工作目录**，等于凭空造出一个
      指向 CWD 的按钮；
    - 文件不存在：按钮只该指向真存在的东西（run_daily 的 report_links 已经
      筛过一遍，这里是最后一道，防文件在通知发出前被删）。
    """
    if not str(path or "").strip():
        return ""
    p = Path(path)
    if not p.exists():
        return ""
    try:
        return p.resolve().as_uri()
    except (OSError, ValueError):
        return ""


def _esc_attr(text: str) -> str:
    """属性值转义：`escape` 的第二个参数必须是 dict（传字符串会在运行期
    AttributeError，而 toast_xml 在 notify 的 try 之外——通知就直接炸了）。"""
    return escape(str(text), {'"': "&quot;", "'": "&apos;"})


def _actions_xml(links) -> str:
    """把 (标签, 路径) 列表拼成 toast 的按钮。XML 元素顺序必须是
    visual → actions → audio，插错位置整个 toast 都不显示。"""
    items = []
    for label, path in list(links or [])[:3]:
        uri = _link_uri(path)
        if not uri:
            continue
        items.append(
            f'<action content="{_esc_attr(label)}"'
            f' activationType="protocol"'
            f' arguments="{_esc_attr(uri)}"/>')
    return f"<actions>{''.join(items)}</actions>" if items else ""


def toast_xml(title: str, body: str, *, fail: bool = False,
              scenario: str = "reminder", links=None) -> str:
    """拼 ToastGeneric XML。body 按行拆成多个 <text>（换行在 toast 里会被吞）。

    links=[(标签, 本地路径), ...] 会变成可点的按钮（activationType=protocol
    → 调系统默认程序打开该文件/目录）。用户明确要"通知里带报告链接方便打开"，
    只把路径写成一行文字是不够的：toast 里的文字点不动，得有按钮。
    """
    lines = [ln for ln in (body or "").splitlines() if ln.strip()] or [""]
    texts = f"<text>{escape(title)}</text>"
    texts += "".join(f"<text>{escape(ln)}</text>" for ln in lines)
    audio = SOUND_FAIL if fail else SOUND_OK
    return (
        f'<toast duration="long" scenario="{scenario}">'
        f'<visual><binding template="ToastGeneric">'
        f"{texts}</binding></visual>"
        f"{_actions_xml(links)}"
        f'<audio src="{audio}" loop="{"true" if fail else "false"}"/>'
        f"</toast>"
    )


def _ps_script(inner: str) -> str:
    """把 ASCII 脚本包成 powershell -Command - 能吃的一段。"""
    return "$ErrorActionPreference='Stop'\n" + inner


def _toast_script(xml_text: str) -> str:
    return _ps_script(
        _PS_PREAMBLE.format(b64=_b64(xml_text)) +
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null\n"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
        "ContentType=WindowsRuntime]|Out-Null\n"
        "$d=New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$d.LoadXml($s)\n"
        "$t=New-Object Windows.UI.Notifications.ToastNotification $d\n"
        f"$a='{POWERSHELL_AUMID}'\n"
        "[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier($a).Show($t)\n"
        "Write-Output 'TOAST_OK'\n"
    )


def _balloon_script(title: str, body: str) -> str:
    return _ps_script(
        _PS_PREAMBLE.format(b64=_b64(f"{title}\n{body}")) +
        "$p=$s -split \"`n\",2\n"
        "Add-Type -AssemblyName System.Windows.Forms\n"
        "Add-Type -AssemblyName System.Drawing\n"
        "$n=New-Object System.Windows.Forms.NotifyIcon\n"
        "$n.Icon=[System.Drawing.SystemIcons]::Information\n"
        "$n.Visible=$true\n"
        "$n.ShowBalloonTip(10000,$p[0],$p[1],"
        "[System.Windows.Forms.ToolTipIcon]::Info)\n"
        "Start-Sleep -Seconds 11\n"
        "$n.Dispose()\n"
        "Write-Output 'BALLOON_OK'\n"
    )


def _run_powershell(script: str, *, timeout_s: float,
                    runner=None) -> tuple[bool, str]:
    """跑一段 ASCII 脚本。runner 可注入（测试用），返回 (ok, 输出摘要)。"""
    run = runner or subprocess.run
    try:
        proc = run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", "-"],
            input=script.encode("ascii"),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{type(e).__name__}: {e}"[:300]
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode == 0 and out.endswith("_OK"), out[-300:]


def notify(title: str, body: str, *, fail: bool = False,
           timeout_s: float = DEFAULT_TIMEOUT_S, runner=None,
           links=None) -> bool:
    """弹框 + 声音。任何失败都返回 False，**绝不抛异常**。

    links=[(标签, 路径), ...] 会在 toast 上变成可点按钮（打开该文件/目录）；
    走气泡回退时没有按钮，但正文里的路径仍在（正文由调用方拼好）。
    """
    if not _is_windows():
        return False
    if suppressed():
        return False
    try:
        xml = toast_xml(title, body, fail=fail, links=links)
    except Exception:
        xml = None                    # 拼 XML 出错也不许冒泡：通知是旁路
    ok, detail = (False, "toast_xml_failed")
    if xml:
        ok, detail = _run_powershell(_toast_script(xml),
                                     timeout_s=timeout_s, runner=runner)
    if not ok:
        ok, detail2 = _run_powershell(_balloon_script(title, body),
                                      timeout_s=timeout_s, runner=runner)
        detail = f"toast={detail} balloon={detail2}"[:300]
    return ok


def _fmt_elapsed(seconds: float) -> str:
    """把秒数格式化成 HH:MM:SS（>1h）或 MM:SS.s（<1h）。

    用户原话："最好时间精准到秒，不然归属问题不好定位"——
    跑批死信排查经常要回答"是哪次重试用光了 timeout"，
    0.3 分钟 = 18 秒 与 8.7 分钟 = 522 秒 在"分钟粒度"下都被压成"X.X 分钟"，
    看不出"是不是刚好卡到 90 分钟 timeout"。

    所以这里按规模分两种格式：
      - <1h：MM:SS.s（带小数秒，5min 内的偏差看得清）
      - ≥1h：HH:MM:SS（整数秒，省字节）
    """
    s = max(0.0, float(seconds))
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s - h * 3600 - m * 60
    if h > 0:
        return f"{h:d}:{m:02d}:{int(sec):02d}"
    return f"{m:02d}:{sec:04.1f}"


# job id → 通知用中文名。**只影响给人看的展示层**（通知标题 / 桌面
# marker 首行），不动 plan_id、文件命名、CLI 参数——那三处必须保持英文
# id 才能和 schedule.yaml / state.db / 计划任务对得上。
# 2026-10-08 用户要求：通知标题别露英文 job id，housing_monthly 乍一看
# 不知道是啥。未知 job 回退原值（配置错/拼写错时至少能看出是哪个 id）。
JOB_ZH = {
    "daily_report": "每日综合日报",
    "housing_daily": "每日房产跟踪",
    "housing_fortnight": "房产半月报",
    "housing_rr_fiveday": "租售比Top20",
    "housing_monthly": "房贷贴息月报",
    "finance_daily": "金融日报",
    "finance_postclose": "收市金融日报",
    "hardware_daily": "每日硬件跟踪",
    "policy_daily": "每日政策跟踪",
}


def zh_job_label(job: str) -> str:
    """job id → 中文名；不在表内回退原值（不猜不编）。"""
    return JOB_ZH.get(job, job)


def daily_summary_message(job: str, date: str, *, rc: int, done: int,
                          dead: int, not_terminal: list[str],
                          elapsed_s: float,
                          finished_at: str = "",
                          links=None) -> tuple[str, str, bool]:
    """按退出码造 (标题, 正文, 是否失败)。退出码语义见 scripts/run_daily.py。

    links 同时进正文：气泡回退没有按钮，路径必须在正文里才点得开/复制得走。

    `finished_at` 是**跑批结束时刻**（SH 时区，HH:MM:SS），让人能从通知弹
    出时刻反推跑批结束点：与"用时 X"对起来就能定位到哪一段卡住，是 timeout 在
    杀还是 verifier 重试烧光。"13:00 跑完"+"用时 90:00"="00:00 起的跑批"，
    "00:30 跑完"+"用时 90:00"="前一天 23:00 起的"。
    """
    fail = rc != 0
    head = {0: "✅ 跑完了", 1: "⚠️ 没跑完", 2: "💀 有死信",
            3: "❌ 起不来"}.get(rc, "❌ 出错了")
    title = f"{head} {zh_job_label(job)} · {date}"
    header_line1 = f"用时 {_fmt_elapsed(elapsed_s)} · 退出码 {rc}"
    if finished_at:
        header_line1 += f" · 结束 {finished_at}"
    body = (f"{header_line1}\n"
            f"完成 {done} · 死信 {dead} · 未终态 {len(not_terminal)}")
    if not_terminal:
        body += "\n" + "、".join(str(x) for x in not_terminal[:6])
    for label, path in list(links or [])[:3]:
        body += f"\n{label}: {path}"
    return title, body, fail


def notify_summary(root, job: str, date: str, *, rc: int, done: int,
                   dead: int, not_terminal: list[str],
                   elapsed_s: float, enabled: bool = True,
                   finished_at: str = "",
                   links=None) -> bool:
    """跑批收尾通知的统一入口（带结构化日志，方便事后查为什么没弹）。

    finished_at 透传给 daily_summary_message（跑批结束时刻 SH HH:MM:SS）。
    2026-10-08：这个函数曾因合并事故裂成两半——上半段带 finished_at 但
    函数体在 daily_summary_message 调用处截断（没有 send / 没有 return），
    下半段是完整旧版覆盖了它、却没有 finished_at 参数。结果 pytest 全绿
    （测试只按签名直调 daily_summary_message），真跑 run_daily 时 notify 在
    收尾一刻 TypeError。这条注释是钉子：签名与函数体必须是一个整体。
    """
    if not enabled:
        return False
    if suppressed():
        # 不走 notify() 的失败分支：测试里把"弹不出去"记成 winnotify_failed
        # 会让断言结构化日志的测试以为通知链路坏了，而它其实是主动静默。
        jlog(root, "INFO", "winnotify_suppressed", job=job, date=date,
             reason=SUPPRESS_ENV)
        return False
    title, body, fail = daily_summary_message(
        job, date, rc=rc, done=done, dead=dead,
        not_terminal=not_terminal, elapsed_s=elapsed_s,
        finished_at=finished_at, links=links)
    sent = notify(title, body, fail=fail, links=links)
    jlog(root, "INFO" if sent else "WARN",
         "winnotify_sent" if sent else "winnotify_failed",
         job=job, date=date, rc=rc, title=title,
         links=[str(p) for _lab, p in (links or [])][:3])
    return sent
