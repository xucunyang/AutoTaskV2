"""动态算力总督V5：ACTIVE(人在,~1/8共享1槽) / IDLE(空闲,85%) + 手动override三态。"""
from __future__ import annotations
import os
import time
from pathlib import Path

IDLE_AFTER_S = 300
CONFIRM = 2
OVERRIDE_FILE = Path(__file__).resolve().parents[1] / "state" / "governor.override"

def cpu_total() -> int:
    return os.cpu_count() or 8

def idle_seconds() -> float:
    """Windows最后输入距今秒数；非Windows/失败默认0（保安全判ACTIVE）。"""
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
    def __init__(self, idle_after_s: int = IDLE_AFTER_S, confirm: int = CONFIRM):
        self.idle_after_s = idle_after_s
        self.confirm = confirm
        self._idle_hits = 0
        self.profile = "ACTIVE"

    def sample(self, idle_s: float | None = None, avail_gb: float | None = None,
               override: str | None = None) -> dict:
        """每15s调一次；manual优先：IDLE/ACTIVE pin住，AUTO走侦测。"""
        manual = override if override is not None else read_override()
        by = "auto"
        if manual in ("IDLE", "ACTIVE"):
            self.profile = manual
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
        w = PROFILES[self.profile].copy()
        mem_ok = avail > HEADROOM_GB and avail - HEADROOM_GB >= 1.0
        return {"profile": self.profile, "workers": w, "by": by,
                "mem_ok": mem_ok, "avail_gb": round(avail, 2),
                "burst_allowed": True, "ts": time.time()}


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
