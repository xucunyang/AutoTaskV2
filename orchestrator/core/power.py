"""执行期间阻止系统休眠。

为什么需要：每日任务是**定时跑**的，而机器多半在无人值守时段处于
休眠/待机状态。定时器能唤醒机器，但**一旦唤醒后任务要跑十几分钟**，
系统的电源策略会在执行途中再次把它睡回去——于是任务在中途被杀，
表现是"有时候跑完有时候没跑"，且日志断在半路，没有任何报错。

这与 governor 的空闲检测是两件事：
- governor._idle_win 读的是"**用户**多久没动鼠标"，用来决定并发档位；
- 这里挡的是"**系统**要不要睡"，两者判据完全不同，不能互相替代。

实现：Windows SetThreadExecutionState。标志是**线程级**的，
进程退出或调用 ES_CONTINUOUS 且不带任何"要求"标志时自动复位，
所以异常退出不会留下"永远不休眠"的残留状态——这一点比看起来重要，
一个卡死进程把电源策略永久改成"不休眠"是很糟的副作用。

非 Windows 平台降级为 no-op（Mac 的 _idle_mac 已按用户要求暂缓）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

# SetThreadExecutionState 的标志位
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001      # 阻止系统进入睡眠
ES_DISPLAY_REQUIRED = 0x00000002     # 阻止显示器关闭
ES_AWAYMODE_REQUIRED = 0x00000040    # 阻止进入-away 模式

IS_WINDOWS = os.name == "nt"

_state = {"held": False, "supported": IS_WINDOWS, "error": ""}


def _kernel32():
    import ctypes
    return ctypes.windll.kernel32


def keep_awake(display: bool = False) -> bool:
    """阻止本次执行期间休眠。返回是否成功。

    不抛异常：防休眠失败不该让任务跑不下去（那会把"想省点电"
    变成"任务不跑了"）。失败只记 error，由调用方决定要不要告警。
    """
    if not IS_WINDOWS:
        _state["held"] = False
        return False
    try:
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        if display:
            flags |= ES_DISPLAY_REQUIRED
        rc = _kernel32().SetThreadExecutionState(flags)
        _state["held"] = rc != 0
        if not rc:
            _state["error"] = f"SetThreadExecutionState returned 0 (flags={flags})"
            log.warning("keep_awake failed: %s", _state["error"])
        return bool(rc)
    except Exception as e:      # noqa: BLE001
        _state["error"] = f"{type(e).__name__}: {e}"
        log.warning("keep_awake raised: %s", _state["error"])
        return False


def allow_sleep() -> None:
    """释放防休眠。ES_CONTINUOUS 不带任何要求位 = 恢复系统默认行为。"""
    if not IS_WINDOWS or not _state.get("held"):
        return
    try:
        _kernel32().SetThreadExecutionState(ES_CONTINUOUS)
    except Exception as e:      # noqa: BLE001
        log.warning("allow_sleep raised: %s", e)
    _state["held"] = False


class KeepAwake:
    """上下文管理器：进入时阻止休眠，退出时释放。

        with KeepAwake():
            ...跑任务...

    之所以用上下文管理器而不是装饰器：执行路径上有多个 return
    （skipped_terminal / lost / failed），手动 try/finally 很容易漏一处，
    漏了就等于永久不休眠。__exit__ 保证一定释放。

    **保持整个进程存活**：SetThreadExecutionState 是线程级的，
    而执行可能发生在 anyio worker 线程里，那样锁不住主线程的电源请求。
    所以进入时如果不在主线程，就在主线程也持有一份——由 _anchor
    线程负责在退出时释放。
    """

    def __init__(self, display: bool = False):
        self.display = display
        self.ok = False
        self._anchor: threading.Thread | None = None
        self._anchor_stop = threading.Event()
        self._anchor_done = threading.Event()
        self._anchor_ok = False

    def __enter__(self) -> "KeepAwake":
        self.ok = keep_awake(self.display)
        if threading.current_thread() is not threading.main_thread():
            self._anchor = threading.Thread(target=self._anchor_loop,
                                            name="keep-awake-anchor", daemon=True)
            self._anchor.start()
            # 等锚线程真的拿到标志再返回，否则主线程若立刻退出，
            # 锚线程还没执行就被杀，等于没防住
            self._anchor_stop.wait(timeout=3.0)
        return self

    def _anchor_loop(self) -> None:
        self._anchor_ok = keep_awake(self.display)
        self._anchor_stop.set()
        # 周期重设：SetThreadExecutionState 的语义是"当前线程的请求"，
        # 但厂商电源策略与驱动可能重置状态，周期重设更稳。
        # 退出的唯一出口是 _anchor_done，不靠超时。
        while not self._anchor_done.wait(timeout=5.0):
            keep_awake(self.display)

    def __exit__(self, *exc) -> None:
        self._anchor_done.set()
        if self._anchor is not None and self._anchor.is_alive():
            self._anchor.join(timeout=2.0)
        allow_sleep()


def status() -> dict:
    """当前防休眠状态，供 /health 类检查用。"""
    return {"supported": IS_WINDOWS, "held": bool(_state.get("held")),
            "error": _state.get("error", ""), "pid": os.getpid(),
            "python": sys.version.split()[0]}