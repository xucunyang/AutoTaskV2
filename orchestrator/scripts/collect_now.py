# -*- coding: utf-8 -*-
"""立即采集指定锚（看板 /api/mmb/collect-now / 采集队列 worker 拉起）。

流程：taskboard 登记 →（可选）等 channel-gate 单写者锁 → backfill run --urls
（同一 channel-gate：锁+节奏；增量缓存+断点续采，重复点击幂等）→ trend_calc
刷新快照 → 销号。进度在「任务流水」面板实时可见。

--wait-lock-min（10-24 采集队列）：锁被占（如「一键补锚」在跑）时排队等锁
而不是立即失败——每 15s 探一次锁，taskboard 实时显示被谁占用/已等多久。"""
import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import taskboard  # noqa: E402
from channel_gate import gate  # noqa: E402

# 取消协议（sku-cancel-spec §3.2）：webapp 发 SIGTERM → 此处 set → 每个长步骤前判
_CANCELLED = False


def _on_sig(signum, frame):
    global _CANCELLED
    _CANCELLED = True


def _cancelled(tb_id: str, where: str) -> None:
    taskboard.finish(tb_id, "cancelled", f"在 {where} 收到取消信号")
    print(f"[collect_now] ⚠ 取消于 {where}", flush=True)
    sys.exit(0)


def _probe_lock() -> bool:
    """试取 mmb 单写者锁：True=当前空闲（取了立即还，不占用）。"""
    try:
        gate.acquire("mmb", "立即采集 探锁")
        gate.release("mmb")
        return True
    except gate.LockHeld:
        return False


def _lock_holder() -> str:
    """读锁文件里占用方的 purpose（显示用），读不到返回空。"""
    try:
        d = json.loads(gate.lock_path("mmb").read_text(encoding="utf-8"))
        return str(d.get("purpose") or "")
    except Exception:
        return ""


def _wait_lock(tb: str, step: str, wait_min: float) -> bool:
    """等锁循环：每 15s 探一次，detail 实时刷新；wait_min<=0 只试一次。"""
    n = 0
    deadline = time.time() + max(0.0, wait_min) * 60
    while True:
        if _CANCELLED:
            _cancelled(tb, "等锁")
        if _probe_lock():
            if n:
                taskboard.step(tb, step, "running", detail=f"等锁 {n * 15}s 后空闲")
            return True
        if wait_min <= 0 or time.time() >= deadline:
            taskboard.step(tb, step, "DEAD_LETTER",
                           detail=f"等锁超时（被「{_lock_holder()}」占用）")
            return False
        n += 1
        taskboard.step(tb, step, "running",
                       detail=f"排队等锁：被「{_lock_holder()}」占用，已等 {n * 15}s")
        time.sleep(15)


def _run(cmd: list[str], tb_id: str, step_name: str) -> int:
    """subprocess.call，长跑前/后判取消。子进程本身不会响应 SIGTERM
    （subprocess.call 是同步阻塞到子进程退出），所以取消在 while
    外层判；这是 collect_now 的简化（取自 step 粒度）。"""
    if _CANCELLED:
        _cancelled(tb_id, step_name)
    try:
        return subprocess.call(cmd, cwd=str(ROOT))
    except Exception:
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="collect_now")
    ap.add_argument("--urls", required=True, help="逗号分隔的京东直链")
    ap.add_argument("--wait-lock-min", type=float, default=0.0,
                    help="锁被占时排队等待的分钟数（0=立即失败，兼容旧调用）")
    a = ap.parse_args()
    urls = [u for u in a.urls.split(",") if u.strip()]
    step = f"采集 {len(urls)} 锚"
    tb = taskboard.start("立即采集", [step, "trend_calc"])
    taskboard.step(tb, step, "running")

    # 注册取消信号（Windows 上 SIGTERM = terminate()，默认能收到）
    try:
        signal.signal(signal.SIGTERM, _on_sig)
        signal.signal(signal.SIGINT, _on_sig)
    except (ValueError, OSError):
        pass  # 非主线程或平台不支持时跳过

    if _CANCELLED:
        _cancelled(tb, "采集前")

    if not _wait_lock(tb, step, a.wait_lock_min):
        taskboard.finish(tb, "failed",
                         f"通道被占，等锁 {a.wait_lock_min:g} 分钟超时——稍后重试")
        print("[collect_now] 等锁超时", flush=True)
        return 1

    py = sys.executable
    rc1 = _run([py, "-X", "utf8", "-u", "-m", "tools.manmanbuy_backfill",
                "run", "--date-dir", time.strftime("%Y-%m-%d"),
                "--urls", ",".join(urls)], tb, step)
    if rc1 != 0 and not _probe_lock():
        # 竞速失败：探锁时空闲、backfill 取锁前被抢（概率低）→ 重新排队再试一次
        taskboard.step(tb, step, "running", detail="backfill 撞锁，重新排队再试一次")
        if _wait_lock(tb, step, max(a.wait_lock_min, 10.0)):
            rc1 = _run([py, "-X", "utf8", "-u", "-m", "tools.manmanbuy_backfill",
                        "run", "--date-dir", time.strftime("%Y-%m-%d"),
                        "--urls", ",".join(urls)], tb, step)
    if _CANCELLED:
        _cancelled(tb, f"{step} 后")
    taskboard.step(tb, step,
                   "DONE" if rc1 == 0 else "DEAD_LETTER",
                   detail=f"rc={rc1}")
    taskboard.step(tb, "trend_calc", "running")
    rc2 = _run([py, "-X", "utf8", "-u", "-m", "tools.trend_calc",
                "--date", time.strftime("%Y-%m-%d")], tb, "trend_calc")
    if _CANCELLED:
        _cancelled(tb, "trend_calc")
    taskboard.step(tb, "trend_calc",
                   "DONE" if rc2 == 0 else "DEAD_LETTER", detail=f"rc={rc2}")
    ok = rc1 == 0 and rc2 == 0
    taskboard.finish(tb, "ok" if ok else "failed",
                     f"采集 rc={rc1} / 快照 rc={rc2}")
    print(f"[collect_now] 完成 rc1={rc1} rc2={rc2}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
