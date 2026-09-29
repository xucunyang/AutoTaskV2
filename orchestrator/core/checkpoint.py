"""检查点与缓存（Phase2§6）。

设计里最重要的一条：**恢复精度不依赖LLM自我总结**。
- 结构化事实（artifacts_partial/done_steps/cursor）落文件，是续跑依据；
- summary是LLM生成的辅助提示，只作参考，丢了不影响恢复正确性。

cache_hit判据是三项全中：inputs_hash==last_success_hash、产物文件还在、
sha还匹配。三项缺一不可——只比hash会让"文件被人删了"照样命中缓存，
结果是下游读到不存在的产物。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.utils import atomic_write_json, now_utc_iso, sha256_file
from schemas.models import Checkpoint

MAX_PREVIEW_CHARS = 500   # Phase0§5：preview每行截500字


def checkpoint_path(root: str | Path, task_id: str) -> Path:
    return Path(root) / "state" / "checkpoints" / f"{task_id}.json"


def load(root: str | Path, task_id: str) -> Checkpoint | None:
    """读检查点；不存在或损坏返回None（损坏不能炸掉续跑，当作从头开始）。"""
    p = checkpoint_path(root, task_id)
    if not p.exists():
        return None
    try:
        return Checkpoint(**json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, TypeError):
        return None


def save(root: str | Path, ckpt: Checkpoint) -> Path:
    """覆盖写检查点（每次让出/预算检查都覆盖）。"""
    ckpt.updated_at = now_utc_iso()
    p = checkpoint_path(root, ckpt.task_id)
    atomic_write_json(p, ckpt.model_dump(mode="json"))
    return p


def bump_generation(existing: Checkpoint | None, task_id: str,
                    plan_id: str) -> Checkpoint:
    """新session续跑：generation+1，其余字段沿用。"""
    if existing is None:
        return Checkpoint(task_id=task_id, plan_id=plan_id, generation=0)
    return existing.model_copy(update={"generation": existing.generation + 1})


def resume_payload(ckpt: Checkpoint) -> dict:
    """注入新session的内容（§6b恢复协议）。
    分成"结构化事实"和"辅助提示"两段返回，调用方必须先拼事实——
    顺序错了模型会先读摘要再读事实，被摘要带偏。"""
    return {
        "cursor": ckpt.cursor,
        "done_steps": list(ckpt.done_steps),
        "artifacts_partial": [a.model_dump(mode="json")
                              for a in ckpt.artifacts_partial],
        "step": ckpt.step,
        "generation": ckpt.generation,
        "summary_hint": list(ckpt.summary),   # 明确标注是hint，不是准
    }


def build_manifest(root: str | Path, path: str | Path) -> dict:
    """产物manifest：sha256/bytes/rows/preview前5行（Phase2§5/§6）。"""
    info = sha256_file(path)
    info["preview"] = [p[:MAX_PREVIEW_CHARS] for p in info.get("preview", [])]
    return info


def cache_hit(root: str | Path, task: dict) -> tuple[bool, str]:
    """判定能否SKIPPED_CACHED。返回(是否命中, 原因)。

    三项全中才命中：inputs_hash相同 + 上次成功hash已记 + 产物存在且sha匹配。
    任何一项不满足都算未命中——宁可多跑一次，也不能让下游读到不存在的产物。
    """
    inputs_hash = task.get("inputs_hash") or ""
    last = task.get("last_success_hash")
    if not inputs_hash:
        return False, "no_inputs_hash"
    if not last:
        return False, "no_last_success"
    if inputs_hash != last:
        return False, "inputs_changed"
    outputs = (task.get("shard", {}) or {}).get("outputs") or []
    if not outputs:
        return False, "no_outputs"
    root = Path(root)
    for out in outputs:
        p = root / out
        if not p.exists():
            return False, f"missing_artifact:{out}"
        try:
            digest = sha256_file(p)["sha256"]
        except OSError as e:
            return False, f"unreadable:{out}:{e}"
        if digest != last:
            return False, f"artifact_sha_mismatch:{out}"
    return True, "inputs_and_artifacts_match"


def record_success(store, plan_id: str, task_id: str, inputs_hash: str, *,
                   agent: str = "executor", run_id: str = "",
                   extra: dict | None = None) -> dict:
    """成功后记last_success_hash（cache_hit的数据来源）+saved_tokens指标位。"""
    def _fn(con):
        cur = con.execute(
            "SELECT version,status FROM tasks WHERE plan_id=? AND task_id=?",
            (plan_id, task_id),
        ).fetchone()
        if cur is None:
            raise StoreError(f"task_not_found:{plan_id}/{task_id}")
        v, s = cur
        con.execute(
            "UPDATE tasks SET last_success_hash=?, version=?, updated_at=?"
            " WHERE plan_id=? AND task_id=?",
            (inputs_hash, v + 1, now_utc_iso(), plan_id, task_id),
        )
        con.execute(
            "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,agent,payload)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now_utc_iso(), plan_id, task_id, s, s, run_id, agent,
             json.dumps({"event": "SUCCESS_RECORDED",
                         "inputs_hash": inputs_hash,
                         **(extra or {})}, ensure_ascii=False)),
        )
        return con.execute(
            "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
            (plan_id, task_id),
        ).fetchone()

    return store._row_to_task(store._write_txn(_fn))


from core.store import StoreError  # noqa: E402  (循环引用：store不依赖本模块)


def prune_checkpoints(root: str | Path, keep_task_ids: set[str]) -> list[str]:
    """删掉不属于任何已知任务的检查点（任务被删/改名后残留）。
    返回被删的文件名。删除前不动仍被引用的——误删会让人丢现场。"""
    d = Path(root) / "state" / "checkpoints"
    if not d.exists():
        return []
    removed = []
    for p in sorted(d.glob("*.json")):
        if p.stem not in keep_task_ids:
            p.unlink()
            removed.append(p.stem)
    return removed
