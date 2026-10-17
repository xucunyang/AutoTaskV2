# -*- coding: utf-8 -*-
"""慢慢买每日增量回填 + L1 快照刷新（\\AutoTaskV2\\mmb_backfill_daily）。

为什么独立于 run_daily/编排器：回填是单命令数据采集（注册表 19 listing
各拉当日价，2~7s 节流），不需要模板/计划/验收链——10-14 用户裁决注销
hardware_daily 报告链后，硬件域的「日常」只剩纯数据采集与指标刷新，
这条任务就是它（R38 配套，2026-10-15 挂 schtasks）。

链路：
  1. python -m tools.manmanbuy_backfill run   采集→转换→入库（会话过期退 2）
  2. python -m tools.trend_calc --date 今天    本地重算 L0/stab_risk 快照
  3. 成功 toast（入库行数）；失败 toast 带日志路径（会话过期提示 login）

日志：reports/mmb_backfill_{date}.log——计划任务的 stdout 没人收（踩过）。
命令行参数原样透传给 run 子命令（如 --only 9070 测试单 listing）。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STEP_TIMEOUT_S = 60 * 75          # 单步上限 75 分钟（10-17：全量 32 锚含 gwdang 轨
                                  # 22 请求×批间休息会跨 40 分钟，被杀过一次）
LOG_DIR: Path | None = None       # 测试缝：覆写后日志改落别处（默认 reports/）


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=STEP_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") \
            if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") \
            if isinstance(e.stderr, bytes) else (e.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, f"{out}\n[超时]", err)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    today = time.strftime("%Y-%m-%d")
    log_path = (LOG_DIR or ROOT / "reports") / f"mmb_backfill_{today}.log"
    t0 = time.time()
    steps = [
        ("fix_url", [sys.executable, "-X", "utf8", "-u",
                     str(ROOT / "scripts" / "prefill_jingfen.py"), "--fix-url"], True),
        ("transcribe", [sys.executable, "-X", "utf8", "-u",
                        str(ROOT / "scripts" / "transcribe_registry.py")], True),
        ("backfill", [sys.executable, "-X", "utf8", "-u",
                      "-m", "tools.manmanbuy_backfill", "run",
                      "--date-dir", today, *argv], False),
        ("trend_calc", [sys.executable, "-X", "utf8", "-u",
                        "-m", "tools.trend_calc", "--date", today], False),
    ]
    outs, failed = [], None
    for name, cmd, soft in steps:
        p = _run(cmd)
        outs.append(f"$ {' '.join(cmd)}\n{p.stdout}{p.stderr}"
                    f"[{name}] rc={p.returncode}")
        if p.returncode != 0:
            if soft:
                # 软步骤（fix_url/transcribe）：记日志继续——回填不受影响
                continue
            failed = (name, p.returncode)
            break
    dt = time.time() - t0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} "
                f"耗时 {dt:.0f}s =====\n" + "\n".join(outs) + "\n")

    from core import winnotify
    if failed:
        name, rc = failed
        tail = (outs[-1][-260:]).replace("\n", " ")
        # 4030 = 已封禁（操作手册 §2）：全停+联系客服；勿重试/换 IP/重登
        if rc == 2 and "4030" in "\n".join(outs):
            hint = "⛔ 已触发封禁（4030）：立即停止一切慢慢买流量，联系客服解封（账号 15850730219）"
        elif rc == 2:
            hint = "会话过期：跑 python -m tools.manmanbuy_backfill login 重登"
        else:
            hint = f"退出码 {rc}"
        winnotify.notify(
            f"每日回填失败：{name}",
            f"{hint} {tail} 日志：{log_path}",
            fail=True, links=[("打开日志", str(log_path))])
        return rc or 1

    added = sum(int(m) for m in re.findall(r"\+(\d+) 行", "\n".join(outs)))
    winnotify.notify(
        "慢慢买每日回填完成",
        f"新增 {added} 行，L1 快照已刷新（{today}，耗时 {dt:.0f}s）",
        links=[("打开日志", str(log_path))])
    return 0


if __name__ == "__main__":
    sys.exit(main())
