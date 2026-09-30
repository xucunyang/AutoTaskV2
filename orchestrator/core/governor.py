"""动态算力总督V5：ACTIVE(人在,~1/8共享1槽) / IDLE(空闲,85%) + 手动override三态。"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path

IDLE_AFTER_S = 300
CONFIRM = 2
OVERRIDE_FILE = Path(__file__).resolve().parents[1] / "state" / "governor.override"

def cpu_total() -> int:
    return os.cpu_count() or 8

def idle_seconds() -> float:
    """最后输入距今秒数（双平台分派）；未知平台/失败默认0（保安全判ACTIVE）。"""
    if sys.platform == "win32":
        return _idle_win()
    if sys.platform == "darwin":
        return _idle_mac()
    return 0.0

def _idle_win() -> float:
    """Windows：GetLastInputInfo；失败默认0。"""
    try:
        import ctypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return max(0.0, ms / 1000.0)
    except Exception:
        pass
    return 0.0

def _idle_mac(timeout_s: float = 2.0) -> float:
    """Mac：ioreg读HIDIdleTime（纳秒→秒），无额外依赖；
    失败/超时返回0.0（保安全判ACTIVE，提示用--mode idle手动指定）。"""
    import subprocess
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        ).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                ns = int(line.split("=")[-1].strip())
                return max(0.0, ns / 1e9)
    except Exception:
        pass
    return 0.0

def mem_info() -> dict:
    """返回 {total_gb, avail_gb}；优先psutil，失败回退16GB假设保安全。"""
    try:
        import psutil
        m = psutil.virtual_memory()
        return {"total_gb": m.total / 1e9, "avail_gb": m.available / 1e9}
    except Exception:
        return {"total_gb": 16.0, "avail_gb": 4.0}
PROFILES = {
    # ACTIVE执行与验证共享1槽（total_active=1），约1/8 CPU，内存≤2GB
    "ACTIVE": {"executor": 1, "verifier": 0, "local": 0, "mem_cap_gb": 2.0, "shared_slot": 1},
    "IDLE": {"executor": 4, "verifier": 2, "local": 1, "mem_cap_gb": 12.0, "shared_slot": 0},
}
HEADROOM_GB = 2.0


def _fit_workers(profile: str, workers: dict, avail_gb: float,
                 cpu_n: int | None = None) -> dict:
    """按内存/算力裁剪 worker 数。

    设计§7 表格里 IDLE 档的 executor 是"**算力-headroom**"——不是写死4个。
    原实现只算了个 `mem_ok` 布尔值就丢掉，`mem_cap_gb` 从没被用过：
    于是一台内存紧张的机器照样按IDLE派4个执行器，而配置里那12GB
    上限纯属摆设。指标算出来不用，比不算更糟——它让人以为有防护。
    """
    w = dict(workers)
    n = cpu_n if cpu_n is not None else cpu_total()
    # 内存：留够headroom之后还能吃下几个执行器（按每槽1GB估）
    budget = avail_gb - HEADROOM_GB
    by_mem = max(0, int(budget // 1.0)) if budget > 0 else 0
    # 算力：留一个给系统本身
    by_cpu = max(0, n - 1)
    w["executor"] = max(0, min(w["executor"], by_mem, by_cpu))
    if by_mem < w["executor"] or by_cpu < w["executor"]:
        w["trimmed_by"] = {"mem_cap": by_mem, "cpu_cap": by_cpu}
    return w


def read_override() -> str:
    """manual三态：idle=强制满负载，active=强制低占用，auto/无文件=自动侦测。"""
    try:
        if OVERRIDE_FILE.exists():
            v = OVERRIDE_FILE.read_text(encoding="utf-8").strip().lower()
            if v.startswith("idle"):
                return "IDLE"
            if v.startswith("active"):
                return "ACTIVE"
    except Exception:
        pass
    return "AUTO"


def write_override(mode: str, reason: str = "") -> Path:
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_FILE.write_text(f"{mode}\n{time.strftime('%Y-%m-%dT%H:%M:%S')} {reason}\n", encoding="utf-8")
    return OVERRIDE_FILE


class Governor:
    def __init__(self, idle_after_s: int = IDLE_AFTER_S, confirm: int = CONFIRM,
                 headroom_gb: float = HEADROOM_GB, notifier=None):
        self.idle_after_s = idle_after_s
        self.confirm = confirm
        self.headroom_gb = headroom_gb
        self.notifier = notifier
        self._idle_hits = 0
        self.profile = "ACTIVE"

    def sample(self, idle_s: float | None = None, avail_gb: float | None = None,
               override: str | None = None) -> dict:
        """每15s调一次；manual优先：IDLE/ACTIVE pin住，AUTO走侦测。

        手动模式下**暂停自动侦测的累计**（设计§7："手动模式期间自动档位
        监控暂停"）。不清零的话，从手动切回自动的第一下就可能因为
        之前攒的 hits 立刻跳IDLE——等于手动设定被无视。
        """
        manual = override if override is not None else read_override()
        prev = self.profile
        by = "auto"
        if manual in ("IDLE", "ACTIVE"):
            self.profile = manual
            self._idle_hits = 0          # 暂停自动档位监控
            by = "manual"
        else:
            idle_s = idle_s if idle_s is not None else idle_seconds()
            if idle_s >= self.idle_after_s:
                self._idle_hits += 1
            else:
                self._idle_hits = 0
                self.profile = "ACTIVE"  # 有输入立即降档
            if self._idle_hits >= self.confirm:
                self.profile = "IDLE"
        mem = mem_info()
        avail = avail_gb if avail_gb is not None else mem["avail_gb"]
        w = _fit_workers(self.profile, PROFILES[self.profile].copy(), avail)
        mem_ok = avail > self.headroom_gb and avail - self.headroom_gb >= 1.0
        switched = self.profile != prev
        out = {"profile": self.profile, "workers": w, "by": by,
               "mem_ok": mem_ok, "avail_gb": round(avail, 2),
               "burst_allowed": mem_ok, "switched": switched, "prev": prev,
               "ts": time.time()}
        if switched and self.notifier is not None:
            # 档位变化要留痕：否则事后没人知道为什么那段时间跑得慢
            try:
                self.notifier.send(
                    "P2" if self.profile == "IDLE" else "P1",
                    "governor_switch", by=by, prev=prev, now=self.profile,
                    avail_gb=out["avail_gb"], executor=w["executor"])
            except Exception:
                pass
        return out
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="手动触发算力档位")
    ap.add_argument("--mode", choices=["idle", "active", "auto"], required=True,
                    help="idle=我不用了全负载跑，active=我在用低占用，auto=恢复自动侦测")
    a = ap.parse_args()
    if a.mode == "auto":
        try:
            OVERRIDE_FILE.unlink(missing_ok=True)
        except TypeError:
            if OVERRIDE_FILE.exists():
                OVERRIDE_FILE.unlink()
        print("override cleared -> AUTO")
    else:
        p = write_override(a.mode, "manual cli")
        print(f"override -> {a.mode.upper()} ({p})")
