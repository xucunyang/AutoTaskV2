"""Phase0 §5 + DoD第4项：任务卡白名单渲染 + token<2k门禁 + 不含敏感字段。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.task_card import (
    CARD_FORBIDDEN,
    CARD_WHITELIST,
    estimate_tokens,
    render_task_card,
)

ROOT = Path(__file__).resolve().parents[1]

ACCEPTANCE = [
    {"type": "file_exists", "path": "artifacts/2026-09-29/t1_collect.csv",
     "rule_id": "rule_0"},
    {"type": "row_count", "path": "artifacts/2026-09-29/t1_collect.csv",
     "min": 1, "rule_id": "rule_1"},
]
BUDGET = {"max_steps": 20, "timeout_s": 900}
INPUTS = [{
    "path": "artifacts/2026-09-29/in.csv", "sha256": "abc123", "bytes": 1024,
    "rows": 42,
    "preview": [f"line{i}" for i in range(20)],   # 故意给20行，应只渲染5行
}]


def _card(**over):
    kw = dict(task_id="t1_collect", plan_id="daily_report_2026-09-29",
              run_id="r1", objective="采集数据，输出 collect.csv",
              idempotency_key="p1/t1/hash", outputs=["artifacts/2026-09-29/t1_collect.csv"],
              acceptance=ACCEPTANCE, budget=BUDGET, inputs=INPUTS)
    kw.update(over)
    return render_task_card(ROOT, **kw)


def test_card_contains_whitelisted_fields():
    c = _card()
    for expect in ["t1_collect", "daily_report_2026-09-29", "r1",
                   "采集数据", "artifacts/2026-09-29/t1_collect.csv",
                   "abc123", "20", "900"]:
        assert expect in c, expect


def test_card_preview_capped_at_5_lines():
    """Phase0§5：preview前5行×500字。给了20行也只能给5行，否则token炸。"""
    c = _card()
    for i in range(5):
        assert f"line{i}" in c
    for i in range(5, 12):
        assert f"line{i}" not in c


def test_card_token_budget_under_2k():
    """DoD门禁：渲染后token估算<2k。"""
    assert estimate_tokens(_card()) < 2000


def test_card_token_budget_under_2k_with_checkpoint_and_web():
    c = _card(needs_web=True, freshness="strict",
              key_questions=["要点1", "要点2", "要点3"],
              checkpoint={"task_id": "t1_collect", "plan_id": "p1", "step": 7,
                          "cursor": "row-1200", "done_steps": [1, 2, 3, 4, 5]})
    assert "row-1200" in c
    assert "要点1" in c
    assert estimate_tokens(c) < 2000


def test_card_never_leaks_forbidden_fields():
    """禁给master全文/其他任务分片/全量events/大文件内容。
    模板里"禁止：读master全量…"是禁令文本本身，不是泄漏的数据。"""
    c = _card()
    for leaked in ["master.json", "CREATED->READY", "fencing_token",
                   "attempts", "t2_clean", "depends_on", "line6", "line7"]:
        assert leaked not in c, leaked
    # 禁令本身必须在场（说明约束传达到了模型）
    assert "禁止：读master全量" in c


def test_forbidden_field_arg_raises():
    for bad in CARD_FORBIDDEN:
        with pytest.raises(ValueError, match="forbidden_field_in_card"):
            _card(extra={bad: "leaked"})


def test_needs_web_adds_no_param_knowledge_clause():
    """Phase0§5：needs_web=true必须加注禁参数内知识。"""
    c = _card(needs_web=True, freshness="recent")
    assert "禁止使用模型参数内知识" in c
    assert "[未覆盖]" in c
    assert "recent" in c
    # needs_web=false不加
    assert "禁止使用模型参数内知识" not in _card()


def test_needs_web_requires_freshness():
    """needs_web=true但freshness=none时不可渲染（与TaskShard交叉校验一致）。"""
    with pytest.raises(ValueError):
        _card(needs_web=True, freshness="none")


def test_whitelist_is_explicit():
    """白名单常量本身要被审：任务卡只能有这些顶层键。"""
    assert "objective" in CARD_WHITELIST
    assert "master" not in CARD_WHITELIST
    assert "events" not in CARD_WHITELIST
    assert not set(CARD_WHITELIST) & set(CARD_FORBIDDEN)


def test_empty_inputs_renders_placeholder():
    c = _card(inputs=[])
    assert "（无输入产物）" in c
    assert estimate_tokens(c) < 2000
