"""Phase0 DoD: 非法handoff/manifest被拒 + 契约字段校验（V7）。

依据：V7§4（task_id正则/priority 0..10/schema_ref改名/needs_web/freshness/调研三件套）+ Phase0设计§3。
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schemas.models import (
    AcceptanceRule,
    ArtifactManifest,
    ArtifactRef,
    Budget,
    Checkpoint,
    Claim,
    Handoff,
    Source,
    TaskShard,
)


def _minimal_shard(**overrides):
    base = {
        "task_id": "t1_collect",
        "plan_id": "daily_report_2026-09-28",
        "status": "PENDING",
        "idempotency_key": "daily_report_2026-09-28/t1_collect/abc123",
        "objective": "采集数据",
    }
    base.update(overrides)
    return TaskShard(**base)


def test_task_id_regex():
    _minimal_shard(task_id="abc_123")
    for bad in ("ABC", "has-hyphen", "", "a" * 65, "has space", "中文"):
        with pytest.raises(ValidationError):
            _minimal_shard(task_id=bad)


def test_priority_enum_0_to_10():
    for p in (0, 4, 5, 10):
        assert _minimal_shard(priority=p).priority == p
    for bad in (-1, 11, 100):
        with pytest.raises(ValidationError):
            _minimal_shard(priority=bad)


def test_acceptance_schema_ref_renamed():
    """V7§11修复③：schema→schema_ref，与Pydantic父类属性重名告警消除。"""
    assert "schema_ref" in AcceptanceRule.model_fields
    assert "schema" not in AcceptanceRule.model_fields
    r = AcceptanceRule(type="json_schema", path="artifacts/2026-09-28/summary.json",
                       schema_ref="schemas/summary.schema.json")
    assert r.schema_ref == "schemas/summary.schema.json"


def test_illegal_handoff_rejected():
    """非法handoff被拒：summary超2000字。"""
    with pytest.raises(ValidationError):
        Handoff(done=True, summary="x" * 2001)
    ok = Handoff(done=True, summary="清洗完成")
    assert ok.done is True


def test_illegal_manifest_rejected():
    """非法manifest被拒：缺必填sha256/bytes。"""
    with pytest.raises(ValidationError):
        ArtifactManifest(path="artifacts/2026-09-28/t1_collect.csv")
    ok = ArtifactManifest(path="artifacts/2026-09-28/t1_collect.csv",
                          sha256="0" * 64, bytes=12, rows=3)
    assert ok.rows == 3


def test_budget_no_max_tokens():
    """V7：Budget删max_tokens（上下文上限改为provider级context_window）"""
    assert "max_tokens" not in Budget.model_fields
    b = Budget()
    assert b.max_steps == 20
    assert b.timeout_s == 900


def test_shard_v7_fields():
    """V7新字段：needs_web/freshness/slice_rationale/key_questions/session_switch_total"""
    s = _minimal_shard(needs_web=True, freshness="recent",
                       slice_rationale="政策更新需检索最新公告",
                       key_questions=["要点1", "要点2"])
    assert s.needs_web is True
    assert s.freshness == "recent"
    assert s.slice_rationale
    assert len(s.key_questions) == 2
    assert s.session_switch_total == 0
    for bad in ("daily", "today", ""):
        with pytest.raises(ValidationError):
            _minimal_shard(freshness=bad)


def test_needs_web_requires_freshness():
    """V7：needs_web=true时freshness=none拒（Phase1§2.4 enqueue硬校验的模型层 enforcement）"""
    with pytest.raises(ValidationError):
        _minimal_shard(needs_web=True, freshness="none")
    _minimal_shard(needs_web=True, freshness="strict")
    _minimal_shard()  # 默认needs_web=False+freshness=none合法


def test_long_running_and_rule_id():
    """V7：long_running默认False（planner判+手工覆盖）；rule_id为空合法（verifier按index自动编号）"""
    assert _minimal_shard().long_running is False
    assert _minimal_shard(long_running=True).long_running is True
    r = AcceptanceRule(type="file_exists", path="a.csv")
    assert r.rule_id is None
    r2 = AcceptanceRule(type="row_count", path="a.csv", rule_id="r2")
    assert r2.rule_id == "r2"


def test_source_requires_tier_reason():
    """V7：Source缺tier_reason拒（分级理由必落盘可复核）"""
    with pytest.raises(ValidationError):
        Source(source_id="s1", url="https://example.com/a",
               title="示例", source_tier="A")
    ok = Source(source_id="s1", url="https://example.com/a",
                title="示例", source_tier="A", tier_reason="官方政府公告")
    assert ok.tier_reason == "官方政府公告"


def test_claim_requires_source_ids():
    """V7：Claim空source_ids拒（无来源=幻觉）"""
    with pytest.raises(ValidationError):
        Claim(claim_id="c1", text="结论", source_ids=[])
    ok = Claim(claim_id="c1", text="结论", source_ids=["s1"])
    assert ok.source_ids == ["s1"]


def test_checkpoint_v7_fields():
    """V7：Checkpoint扩字段（summary/artifacts_partial/done_steps/generation）"""
    ck = Checkpoint(task_id="t1", plan_id="p1",
                    summary=["第1步完成"],
                    artifacts_partial=[ArtifactRef(path="a.csv", sha256="0" * 64)],
                    done_steps=[0, 1], generation=2)
    assert ck.generation == 2
    assert ck.done_steps == [0, 1]
