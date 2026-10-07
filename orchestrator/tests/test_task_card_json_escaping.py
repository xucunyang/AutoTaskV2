"""任务卡必须教模型「JSON 字符串里不能有裸双引号」——实测三次的生产教训。

2026-10-06 一天之内，三条链、三次同样的死法：

| 链 | 文件 | 原文 |
|---|---|---|
| finance | t1_stocks.sources.json | `A股2026"开门红"` |
| hardware | hardware_summary.json | `当前与上期价格列示为"未查到具体"，只能按缺数据处理` |
| policy（首跑） | t1_policy_new.sources.json | `《城市更新"十五五"规划》` |

裸的英文双引号一出现，**整份文件 json.loads 直接炸**：三条引用类规则
（source_traceable / freshness / source_quality）齐刷刷挂，报的还是
`sources_json_invalid` / `bad_json`——长得像"模型字段写错了"，
于是人会去改 prompt、重跑、加重试，而不是想到"引号没转义"。

**为什么改卡片而不是改模板**：`objective` 在 plan 创建时就 bake 进
`shard_json` 了，改模板对**已在途**的任务不生效（2026-10-06 hardware
链已经吃过一次这个亏）。而任务卡是**每次派发实时渲染**的，改完立刻
对下一次重试生效，不用重建 plan。

**为什么是这两处**：needs_web 的任务一定会写 sources.json / claims.json
（都在 FOUR_SET_SCHEMA 里说清怎么写）；带 json_schema 验收的任务一定会写
schema 校验的那个 .json（走 _schema_hint）。两个入口覆盖了全部 JSON 产物。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.task_card import (  # noqa: E402
    FOUR_SET_SCHEMA, estimate_tokens, render_task_card,
)

MARK = "裸的英文双引号"


def _card(root: Path | None = None, **over) -> str:
    kw = dict(
        task_id="t1_demo", plan_id="p1", run_id="r1",
        objective="做一件事", idempotency_key="k",
        outputs=["artifacts/2026-10-06/t1_demo.csv"],
        acceptance=[{"type": "file_exists", "path": "{output}", "rule_id": "rule_0"},
                    {"type": "source_traceable", "rule_id": "rule_3"},
                    {"type": "freshness", "freshness_days": 180, "rule_id": "rule_4"},
                    {"type": "source_quality",
                     "source_tier_min_ratio": {"A": 0.2}, "rule_id": "rule_5"}],
        budget={"max_steps": 10, "timeout_s": 600},
        needs_web=True, freshness="recent",
        key_questions=["q1"],
    )
    kw.update(over)
    # root 必须是真的 orchestrator 目录——任务卡模板 task_card.j2 就住在
    # 它的 templates/ 下，用 tmp_path 会 TemplateNotFound。
    return render_task_card(root or ROOT, **kw)


def test_four_set_schema_carries_the_escaping_rule():
    assert MARK in FOUR_SET_SCHEMA, (
        "四件套说明里没有 JSON 转义约束——needs_web 任务一定会写 "
        "sources.json/claims.json，这里就是它们唯一的写法说明")


def test_needs_web_card_tells_the_model_about_bare_quotes():
    card = _card()
    assert MARK in card, "needs_web 的任务卡没有转义约束"
    assert "「」" in card, "只说不要裸引号却没说替代写法，模型可能直接删掉引号"


def test_json_schema_card_tells_the_model_about_bare_quotes():
    """带 json_schema 的任务（汇总类）产物是 schema 校验的 .json，
    它不走 needs_web 四件套——那条约束必须由 _schema_hint 补上。
    直接用仓库里真实存在的 schema，顺带证明这条路在生产形状下真能渲染。"""
    card = _card(acceptance=[{"type": "json_schema",
                             "path": "artifacts/2026-10-06/hardware_summary.json",
                             "schema_ref": "schemas/hardware_summary.schema.json",
                             "rule_id": "rule_3"}],
                 needs_web=False, freshness="none")
    assert MARK in card, "json_schema 任务卡没有转义约束"


def test_escaping_rule_does_not_blow_the_token_budget():
    """卡片每行都是 token。这条约束是"救命的"，但不能把卡顶过门禁——
    顶过了就是另一种失败（上下文被挤爆 → 让出 → session_switch_fused）。"""
    card = _card()
    toks = estimate_tokens(card)
    assert toks < 2000, f"加了转义约束后任务卡涨到 {toks} tokens"