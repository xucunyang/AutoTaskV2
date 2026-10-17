# -*- coding: utf-8 -*-
"""任务台账：长任务的注册/步骤进度/完成，看板「任务流水」渲染整体把控感。

解决「不知道正在做什么、整体有哪些任务、执行到多少了」：驱动脚本
（wrapper/补链/重放）启动时 start() 登记，每个步骤 step() 汇报，
收尾 finish()；看板 /api/tasklog 把台账和日志尾部一起吐给前端。

- 台账文件 reports/taskboard.json（原子写：tmp + os.replace）
- 进程死掉的 running 任务下次 start 时标记 killed（泄漏自愈）
- 路径可用环境变量 TASKBOARD_FILE 重定向（测试）
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEEP_DONE = 12   # 已完成任务最多留 12 条（新顶旧）


def _board() -> Path:
    env = os.environ.get("TASKBOARD_FILE")
    return Path(env) if env else ROOT / "reports" / "taskboard.json"


def _read() -> dict:
    p = _board()
    if not p.exists():
        return {"tasks": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": []}


def _write(data: dict) -> None:
    p = _board()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)      # 原子替换，读侧永远看到完整 JSON


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def start(name: str, steps: list[str], pid: int | None = None) -> str:
    """登记新任务：steps 为步骤名列表。返回 task_id。"""
    data = _read()
    tasks = data.get("tasks", [])
    keep_done, stuck = [], []
    for t in tasks:
        if t.get("status") == "running":
            stuck.append({**t, "status": "killed",
                          "summary": t.get("summary") or "进程退出未收尾（自动标记）"})
        else:
            keep_done.append(t)
    task_id = f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}"
    task = {
        "id": task_id, "name": name, "status": "running",
        "pid": pid or os.getpid(),
        "steps": [{"name": s, "status": "pending", "rc": None,
                   "seconds": None, "detail": ""} for s in steps],
        "current": None, "started_at": _now(), "finished_at": None,
        "summary": "",
    }
    _write({"tasks": [task] + stuck + keep_done[:KEEP_DONE]})
    return task_id


def step(task_id: str, name: str, status: str = "running",
         detail: str = "", rc: int | None = None, seconds: float | None = None,
         progress: str = "") -> None:
    """汇报某步骤：status ∈ pending/running/ok/fail/skip；progress 是
    步骤内子进度（如「12/22」），只影响展示。"""
    data = _read()
    for t in data.get("tasks", []):
        if t.get("id") != task_id or t.get("status") != "running":
            continue
        for s in t["steps"]:
            if s["name"] == name:
                s["status"] = status
                s["detail"] = detail
                if rc is not None:
                    s["rc"] = rc
                if seconds is not None:
                    s["seconds"] = round(seconds, 1)
            elif (s["name"] != name and s["status"] == "running"
                    and status == "running"):
                s["status"] = "skip"       # 新 running 步把旧的 running 挤成 skip
        t["current"] = name if status == "running" else t.get("current")
        t["progress"] = progress
        break
    _write(data)


def finish(task_id: str, status: str, summary: str = "") -> None:
    data = _read()
    for t in data.get("tasks", []):
        if t.get("id") == task_id:
            t["status"] = status            # ok / fail
            t["finished_at"] = _now()
            t["summary"] = summary
            break
    _write(data)


def snapshot() -> list[dict]:
    """看板用：running 在前，其余按原序（新→旧）。"""
    tasks = _read().get("tasks", [])
    return ([t for t in tasks if t.get("status") == "running"]
            + [t for t in tasks if t.get("status") != "running"])
