"""全环节计时：monotonic span，写metrics(span,duration_ms)。"""
from __future__ import annotations
import time
from contextlib import contextmanager
_spans: list[dict] = []

@contextmanager
def span(name: str, plan_id: str = "", task_id: str = "", run_id: str = "", record: list | None = None):
    t0 = time.monotonic()
    try:
        yield
    finally:
        dt_ms = (time.monotonic() - t0) * 1000
        rec = {"span": name, "plan_id": plan_id, "task_id": task_id, "run_id": run_id, "duration_ms": dt_ms}
        (record if record is not None else _spans).append(rec)

def summarize(records: list[dict], top_n: int = 5) -> dict:
    by_task: dict[str, float] = {}
    for r in records:
        by_task[r.get("task_id", "?")] = by_task.get(r.get("task_id", "?"), 0) + r.get("duration_ms", 0)
    top = sorted(by_task.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    return {"top": top, "count": len(records)}
