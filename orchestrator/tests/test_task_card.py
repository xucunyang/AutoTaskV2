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


def test_card_inputs_as_string_paths():
    """汇总类任务（housing_daily/t4_rollup）的 inputs 是路径字符串而非 dict 列表。
    此前 build_manifest 强解 str.get() 抛 AttributeError，build_card 直接炸。
    修后：字符串路径 → 自取 sha + preview；dict 路径 → 取已有字段。"""
    # 写一个真文件作为 inputs
    src = ROOT / "artifacts" / "2026-09-29" / "in.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("line0\nline1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    c = _card(inputs=[str(src)])
    assert "in.md" in c
    # 文件存在时 sha256 应被填入
    assert c.count("sha256") >= 1
    # preview 应被截到 5 行
    assert "line0" in c and "line5" not in c


def test_card_inputs_missing_path_does_not_crash():
    """inputs 路径不存在时，build_manifest 返回 sha256=None，模板仍能渲染。"""
    c = _card(inputs=["artifacts/2099-01-01/never.md"])
    assert "never.md" in c


def test_card_token_budget_under_2k_with_checkpoint_and_web():
    c = _card(needs_web=True, freshness="strict",
              key_questions=["要点1", "要点2", "要点3"],
              checkpoint={"task_id": "t1_collect", "plan_id": "p1", "step": 7,
                          "cursor": "row-1200", "done_steps": [1, 2, 3, 4, 5]})
    assert "row-1200" in c
    assert "要点1" in c
    assert estimate_tokens(c) < 2000


def test_needs_web_card_contains_four_set_schema():
    """needs_web 的任务卡必须写明四件套的精确形状。

    之前卡上只说"写到 outputs"，形状全靠模型猜——结果模型包了一层
    {"sources":...}、用 id 不用 source_id，验收全挂。
    契约不能只存在代码里。
    """
    from core.task_card import render_task_card
    card = _card(
        task_id="t1", objective="调研",
        outputs=["artifacts/2026-10-01/t1.report.md"],
        needs_web=True, freshness="recent",
        key_questions=["问题一"])
    assert "source_id" in card
    assert "tier_reason" in card
    assert "顶层数组" in card
    assert "source_ids" in card


def test_four_set_paths_carry_the_date_dir():
    """四件套必须给**逐字完整路径**，不能只给文件名。

    实测 2026-10-03 重跑：卡上写的是 `{task_id}.sources.json`（没说目录），
    三个任务全把 sources 写进 `artifacts/` 顶层、claims 一个都没写，
    验收齐刷刷 no_sources_json/no_claims_json。验收只在
    `artifacts/{date}/` 下找文件，所以路径必须带日期目录。
    """
    card = _card(task_id="t1", objective="调研",
                 outputs=["artifacts/2026-10-01/t1.report.md"],
                 needs_web=True, freshness="recent",
                 key_questions=["问题一"])
    assert "artifacts/2026-10-01/t1.sources.json" in card
    assert "artifacts/2026-10-01/t1.claims.json" in card
    assert "不要写到 `artifacts/` 顶层" in card
    # 单独给文件名（没有目录）的旧写法不能再出现
    assert "`t1.sources.json` 必须" not in card


def test_four_set_dir_falls_back_without_outputs():
    """没声明产物时也要给个目录提示，而不是留空让模型自己猜。"""
    from core.task_card import _out_dir
    assert _out_dir(["artifacts/2026-10-01/t1.md"]) == "artifacts/2026-10-01"
    assert _out_dir([]) == "artifacts/<日期>"


def test_non_web_card_has_no_four_set_schema():
    """不需要检索的任务卡不该有四件套说明——多余的约束只会干扰模型。"""
    c = _card(needs_web=False)
    assert "四件套" not in c
    assert "source_id" not in c


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


def test_card_demands_atomic_write_for_summary_tasks():
    """2026-10-03 实测：t4_rollup 这类 needs_web=false 汇总任务，模型连跑 4 轮
    只调 read_range/run_cmd，回复 19 个字就结束，判 no_artifacts_produced。
    任务卡必须把"回复/读完不算完成"写死，且汇总任务不能被排除在这条之外。"""
    c = _card(needs_web=False)
    assert "atomic_write" in c
    assert "回复不是产物" in c
    assert "no_artifacts_produced" in c
    assert "汇总/对比/复述类任务" in c
    # 加了这么多硬要求，token 门禁仍要过
    assert estimate_tokens(c) < 2000
