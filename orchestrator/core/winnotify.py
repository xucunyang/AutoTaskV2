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
from xml.sax.saxutils import escape

from core.utils import jlog

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


def toast_xml(title: str, body: str, *, fail: bool = False,
              scenario: str = "reminder") -> str:
    """拼 ToastGeneric XML。body 按行拆成多个 <text>（换行在 toast 里会被吞）。"""
    lines = [ln for ln in (body or "").splitlines() if ln.strip()] or [""]
    texts = f"<text>{escape(title)}</text>"
    texts += "".join(f"<text>{escape(ln)}</text>" for ln in lines)
    audio = SOUND_FAIL if fail else SOUND_OK
    return (
        f'<toast duration="long" scenario="{scenario}">'
        f'<visual><binding template="ToastGeneric">'
        f"{texts}</binding></visual>"
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
           timeout_s: float = DEFAULT_TIMEOUT_S, runner=None) -> bool:
    """弹框 + 声音。任何失败都返回 False，**绝不抛异常**。"""
    if not _is_windows():
        return False
    xml = toast_xml(title, body, fail=fail)
    ok, detail = _run_powershell(_toast_script(xml),
                                 timeout_s=timeout_s, runner=runner)
    if not ok:
        ok, detail2 = _run_powershell(_balloon_script(title, body),
                                      timeout_s=timeout_s, runner=runner)
        detail = f"toast={detail} balloon={detail2}"[:300]
    return ok


def daily_summary_message(job: str, date: str, *, rc: int, done: int,
                          dead: int, not_terminal: list[str],
                          elapsed_min: float) -> tuple[str, str, bool]:
    """按退出码造 (标题, 正文, 是否失败)。退出码语义见 scripts/run_daily.py。"""
    fail = rc != 0
    head = {0: "✅ 跑完了", 1: "⚠️ 没跑完", 2: "💀 有死信",
            3: "❌ 起不来"}.get(rc, "❌ 出错了")
    title = f"{head} {job} · {date}"
    body = (f"用时 {elapsed_min:.1f} 分钟 · 退出码 {rc}\n"
            f"完成 {done} · 死信 {dead} · 未终态 {len(not_terminal)}")
    if not_terminal:
        body += "\n" + "、".join(str(x) for x in not_terminal[:6])
    return title, body, fail


def notify_summary(root, job: str, date: str, *, rc: int, done: int,
                   dead: int, not_terminal: list[str],
                   elapsed_min: float, enabled: bool = True) -> bool:
    """跑批收尾通知的统一入口（带结构化日志，方便事后查为什么没弹）。"""
    if not enabled:
        return False
    title, body, fail = daily_summary_message(
        job, date, rc=rc, done=done, dead=dead,
        not_terminal=not_terminal, elapsed_min=elapsed_min)
    sent = notify(title, body, fail=fail)
    jlog(root, "INFO" if sent else "WARN",
         "winnotify_sent" if sent else "winnotify_failed",
         job=job, date=date, rc=rc, title=title)
    return sent
