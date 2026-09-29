"""Phase2 §6：检查点落盘/续跑/cache_hit判定。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import checkpoint as cp
from core.store import Store
from schemas.models import ArtifactRef, Checkpoint


def _artifact(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_save_and_load_roundtrip(tmp_path):
    c = Checkpoint(task_id="t1", plan_id="p1", step=3, cursor="row-100",
                   done_steps=[1, 2], generation=1,
                   summary=["已读前100行"],
                   artifacts_partial=[ArtifactRef(path="a.csv", sha256="s1")])
    cp.save(tmp_path, c)
    got = cp.load(tmp_path, "t1")
    assert got.cursor == "row-100"
    assert got.done_steps == [1, 2]
    assert got.artifacts_partial[0].sha256 == "s1"
    assert got.updated_at                       # save时自动盖时间戳


def test_load_returns_none_when_absent_or_corrupt(tmp_path):
    assert cp.load(tmp_path, "nope") is None
    p = cp.checkpoint_path(tmp_path, "broken")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    # 损坏不能炸掉续跑，当作从头开始
    assert cp.load(tmp_path, "broken") is None


def test_bump_generation_keeps_fields(tmp_path):
    c = Checkpoint(task_id="t1", plan_id="p1", step=5, cursor="x",
                   done_steps=[1, 2, 3])
    nxt = cp.bump_generation(c, "t1", "p1")
    assert nxt.generation == 1
    assert nxt.done_steps == [1, 2, 3]        # 结构化事实必须沿用
    assert nxt.cursor == "x"
    # 从None起步是第0代
    assert cp.bump_generation(None, "t1", "p1").generation == 0


def test_resume_payload_separates_facts_from_hints(tmp_path):
    """§6：恢复精度不依赖LLM总结——事实与提示必须分开标，
    否则模型会先读摘要再读事实，被摘要带偏。"""
    c = Checkpoint(task_id="t1", plan_id="p1", cursor="row-50",
                   done_steps=[1], generation=2, summary=["模型自述:大概读了50行"],
                   artifacts_partial=[ArtifactRef(path="p.csv", sha256="s")])
    payload = cp.resume_payload(c)
    assert payload["cursor"] == "row-50"
    assert payload["done_steps"] == [1]
    assert payload["artifacts_partial"][0]["sha256"] == "s"
    assert payload["summary_hint"][0].startswith("模型自述")
    assert "summary_hint" in payload          # 名字自带"只是提示"的暗示


def test_build_manifest_caps_preview(tmp_path):
    p = _artifact(tmp_path, "artifacts/2026-09-29/big.txt", "x" * 3000)
    m = cp.build_manifest(tmp_path, p)
    assert m["bytes"] == 3000
    assert len(m["preview"]) <= 5
    assert all(len(line) <= 500 for line in m["preview"])


# ---------- cache_hit ----------

def _task(inputs_hash="", last="", outputs=None):
    return {"inputs_hash": inputs_hash, "last_success_hash": last,
            "shard": {"outputs": outputs or []}}


def test_cache_hit_requires_all_three_conditions(tmp_path):
    out = "artifacts/2026-09-29/o.csv"
    _artifact(tmp_path, out, "hello")
    from core.utils import sha256_file
    digest = sha256_file(tmp_path / out)["sha256"]
    hit, why = cp.cache_hit(tmp_path, _task(digest, digest, [out]))
    assert hit is True, why
    # inputs_hash 与 last 不同 → 不命中
    assert cp.cache_hit(tmp_path, _task("other", digest, [out]))[0] is False
    # 没记last → 不命中
    assert cp.cache_hit(tmp_path, _task(digest, "", [out]))[0] is False
    # 没声明outputs → 不命中
    assert cp.cache_hit(tmp_path, _task(digest, digest, []))[0] is False


def test_cache_hit_false_when_artifact_deleted(tmp_path):
    """只比hash会让"文件被人删了"照样命中缓存，下游读到不存在的产物。"""
    out = "artifacts/2026-09-29/o.csv"
    p = _artifact(tmp_path, out, "hello")
    from core.utils import sha256_file
    digest = sha256_file(p)["sha256"]
    assert cp.cache_hit(tmp_path, _task(digest, digest, [out]))[0] is True
    p.unlink()
    hit, why = cp.cache_hit(tmp_path, _task(digest, digest, [out]))
    assert hit is False and "missing_artifact" in why


def test_cache_hit_false_when_artifact_modified(tmp_path):
    out = "artifacts/2026-09-29/o.csv"
    p = _artifact(tmp_path, out, "hello")
    from core.utils import sha256_file
    digest = sha256_file(p)["sha256"]
    p.write_text("tampered", encoding="utf-8")
    hit, why = cp.cache_hit(tmp_path, _task(digest, digest, [out]))
    assert hit is False and "sha_mismatch" in why


def test_cache_hit_checks_every_output(tmp_path):
    a = "artifacts/2026-09-29/a.csv"
    b = "artifacts/2026-09-29/b.csv"
    _artifact(tmp_path, a, "x")
    from core.utils import sha256_file
    da = sha256_file(tmp_path / a)["sha256"]
    # 声明了两个产物，第二个根本没生成 → 不能算命中
    assert cp.cache_hit(tmp_path, _task(da, da, [a, b]))[0] is False


def test_record_success_writes_last_success_hash(tmp_path):
    s = Store(tmp_path)
    s.insert_task({"task_id": "t1", "plan_id": "p1", "status": "PENDING",
                   "idempotency_key": "k1", "inputs_hash": "h1",
                   "objective": "o", "outputs": []})
    got = cp.record_success(s, "p1", "t1", "h1", run_id="r")
    assert got["last_success_hash"] == "h1"
    payloads = [json.loads(e["payload"]) for e in s.recent_events("p1")]
    assert any(p.get("event") == "SUCCESS_RECORDED" for p in payloads)
    assert s.replay("p1")["ok"]


def test_record_success_bumps_version_and_last_wins(tmp_path):
    """record_success是编排侧独占写，不带expect_version（没有并发写者），
    但每次都必须version+1，否则会与别的写回撞乐观锁。"""
    s = Store(tmp_path)
    s.insert_task({"task_id": "t1", "plan_id": "p1", "status": "PENDING",
                   "idempotency_key": "k1", "inputs_hash": "h1", "objective": "o"})
    v0 = s.get_task("p1", "t1")["version"]
    g1 = cp.record_success(s, "p1", "t1", "h2")
    assert g1["version"] == v0 + 1
    g2 = cp.record_success(s, "p1", "t1", "h3", extra={"attempt": 2})
    assert g2["version"] == v0 + 2
    assert g2["last_success_hash"] == "h3"      # 后写为准
    assert g2["status"] == "PENDING"            # 不改状态


def test_prune_keeps_referenced_checkpoints(tmp_path):
    cp.save(tmp_path, Checkpoint(task_id="keep", plan_id="p1"))
    cp.save(tmp_path, Checkpoint(task_id="orphan", plan_id="p1"))
    removed = cp.prune_checkpoints(tmp_path, {"keep"})
    assert removed == ["orphan"]
    assert cp.load(tmp_path, "keep") is not None
