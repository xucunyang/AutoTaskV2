"""Pydantic契约：所有落盘JSON的唯一校验源（V7：让出边+调研三件套）。"""
from __future__ import annotations
from datetime import datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, model_validator

class Budget(BaseModel):
    """任务级执行限制：步数+时长上限。
    上下文上限不在任务级——改为provider级context_window（模型窗口），
    80%水位线判定在Provider.chat()返回的usage.prompt_tokens上做。"""
    max_steps: int = 20
    timeout_s: int = 900

class ArtifactRef(BaseModel):
    path: str
    sha256: Optional[str] = None
    bytes: Optional[int] = None
    rows: Optional[int] = None

class AcceptanceRule(BaseModel):
    type: Literal["file_exists", "row_count", "python_test", "json_schema", "business_rule",
                  "source_traceable", "freshness", "source_quality", "coverage"]
    path: Optional[str] = None
    cmd: Optional[str] = None
    min: Optional[int] = None
    schema_ref: Optional[str] = None
    expr: Optional[str] = None
    timeout_s: int = 120
    freshness_days: Optional[int] = None
    source_tier_min_ratio: Optional[dict] = None
    key_questions: Optional[List[str]] = None
    # V7局部重入定位键：verified_rules 存已通过的rule_id；为空时verifier按rule_{index}自动编号
    rule_id: Optional[str] = None

class Handoff(BaseModel):
    done: bool
    summary: str = Field(max_length=2000)
    artifact_refs: List[str] = []
    tests_passed: bool = False
    tokens_used: int = 0
    open_issues: List[str] = []

class TaskShard(BaseModel):
    task_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    plan_id: str
    status: str
    version: int = 0
    fencing_token: int = 0
    owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    idempotency_key: str
    inputs_hash: str = ""
    last_success_hash: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    session_switch_total: int = 0
    objective: str
    inputs: List[ArtifactRef] = []
    outputs: List[str] = []
    depends_on: List[str] = []
    acceptance: List[AcceptanceRule] = []
    handoff: Optional[Handoff] = None
    budget: Budget = Budget()
    priority: int = Field(default=10, ge=0, le=10)
    source: Literal["daily", "pipeline"] = "daily"
    cancel_requested: bool = False
    enqueued_at: Optional[datetime] = None
    privacy: Literal["public", "internal", "secret"] = "public"
    complexity: Literal["simple", "medium", "complex"] = "simple"
    needs_web: bool = False
    freshness: Literal["none", "recent", "strict"] = "none"
    slice_rationale: str = ""
    key_questions: List[str] = []
    # V7让出前置：planner判定预期耗时长短（+允许任务卡手工覆盖）；
    # long_running=false拒让出（短任务撞线走失败重试+自动置True自学习，见Phase1§2.2）
    long_running: bool = False

    @model_validator(mode="after")
    def _needs_web_requires_freshness(self):
        if self.needs_web and self.freshness == "none":
            raise ValueError("needs_web=true时freshness必须为recent/strict")
        return self

class Source(BaseModel):
    """结构化来源（sources.json一行一条）：分级结果落盘可复核。"""
    source_id: str
    url: str
    title: str
    snippet: str = ""
    published_at: Optional[str] = None
    source_tier: Literal["A", "B", "C"]
    tier_reason: str = Field(min_length=1)

class Claim(BaseModel):
    """结论-证据映射（claims.json）：无来源=幻觉，验收拒收。"""
    claim_id: str
    text: str
    source_ids: List[str] = Field(min_length=1)
    verdict: Literal["supported", "unsupported"] = "supported"

class ArtifactManifest(BaseModel):
    path: str
    sha256: str
    bytes: int
    rows: Optional[int] = None
    preview: List[str] = []

class Checkpoint(BaseModel):
    task_id: str
    plan_id: str
    step: int = 0
    cursor: str = ""
    tokens_used: int = 0
    updated_at: str = ""
    summary: List[str] = []
    artifacts_partial: List[ArtifactRef] = []
    done_steps: List[int] = []
    generation: int = 0
