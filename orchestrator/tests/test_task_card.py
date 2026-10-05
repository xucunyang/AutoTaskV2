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


def test_card_writable_paths_include_the_four_set():
    """可写路径必须**正面列全**，不能一边要四件套一边说"只能写 outputs"。

    实测 2026-10-03：t3_hexagon 三次都只交了正文 .md、一个 JSON 都没交，
    验收齐刷刷 no_claims_json/no_sources_json 直到 DEAD_LETTER——它其实是
    三条线里最守规矩的：老卡写着"只写 outputs 里的路径，越界会被拒绝并终止
    本次执行"，而四件套根本不在 outputs 里（tools._safe_path 实际只拦逃出
    root，并不拦它）。卡上的规则互相矛盾，模型只能选一条遵守。
    """
    c = _card(task_id="t3", objective="调研",
              outputs=["artifacts/2026-10-01/t3.md"],
              needs_web=True, freshness="recent", key_questions=["问题一"])
    assert "【可写路径】" in c
    assert "artifacts/2026-10-01/t3.sources.json" in c
    assert "artifacts/2026-10-01/t3.claims.json" in c
    assert "no_sources_json/no_claims_json" in c     # 说清代价
    # 排他表述回归：出现即自相矛盾
    assert "且只写" not in c
    assert "只能写" not in c
    assert estimate_tokens(c) < 2000


def test_card_gives_concrete_freshness_cutoff():
    """时效必须翻成**具体日期**，不能留个模型换算不出来的 token。

    实测 2026-10-04：卡上写的是 `时效性要求=recent`，而验收实际按
    freshness_days=365 且不给模型任何时钟——它以为 2025 年的来源算新，
    交了 2025-10-01（368 天，差 3 天）和 2020 年的旧榜，stale=8/12=0.67
    卡在 limit=0.3，t3_hexagon 连挂三轮。数字就在 acceptance 里，
    卡片却没把它翻成人能执行的判据，等于没说。
    """
    from datetime import date, timedelta
    acc = ACCEPTANCE + [
        {"type": "freshness", "freshness_days": 365},
        {"type": "source_quality", "source_tier_min_ratio": {"A": 0.2}},
    ]
    c = _card(task_id="t3", objective="调研",
              outputs=["artifacts/2026-10-04/t3.md"],
              needs_web=True, freshness="recent", key_questions=["问题一"],
              acceptance=acc)
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    assert cutoff in c, "没给截止日期，模型无从判断哪条来源算超期"
    assert date.today().isoformat() in c, "不给今天就没法做差"
    assert "30%" in c, "没说超期比例上限（rule_3 的判据）"
    assert "20%" in c, "没说 A 类门槛（rule_4 的判据）"
    # 无信息量的旧写法不能再出现（只有 token、没有可执行判据）
    assert "时效性要求=recent，" not in c
    assert "时效性要求=recent。" not in c
    assert "时效性要求=recent（365天内" in c
    assert estimate_tokens(c) < 2000


def test_non_web_card_has_no_freshness_block():
    """不需要联网的任务卡不该出现时效判据——多余的约束只会干扰模型。"""
    c = _card(needs_web=False)
    assert "published_at" not in c
    assert "时效（硬验收" not in c
    assert estimate_tokens(c) < 2000


def test_freshness_days_falls_back_when_acceptance_silent():
    """验收没写 freshness_days 时回退到 FRESHNESS_DAYS 映射，不能渲染出空日期。"""
    from datetime import date, timedelta
    c = _card(task_id="t3", objective="调研",
              outputs=["artifacts/2026-10-04/t3.md"],
              needs_web=True, freshness="strict", key_questions=["问题一"])
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    assert cutoff in c
    assert "180" in c


def test_card_shows_chart_block_only_when_mermaid_rule_present():
    """有图表验收才出图表段，没图表要求的任务不塞无关指令。

    反过来更危险：验收要图而卡上没提——正文模型一定会写，图不会，
    结果就是每轮 mermaid 规则挂一次、烧三轮进死信。
    """
    mermaid = {"type": "mermaid", "path": "artifacts/2026-09-29/charts.md",
               "min": 2, "rule_id": "rule_2"}
    c = _card(acceptance=ACCEPTANCE + [mermaid])
    assert "【图表：验收规则 mermaid 要求 2 张起】" in c
    assert "artifacts/2026-09-29/charts.md" in c
    assert "xychart-beta" in c
    # 10-05 的真实故障：卡上只说"要图"却没给骨架，模型把 y-axis 写成空格
    # 分隔（`y-axis "环比%" -0.5 0.5`），13 张图 12 张解析失败。卡必须给出
    # **能直接抄**的正确形状，并把两个坑点名。
    assert 'y-axis "单位" -0.5 --> 0.5' in c, "卡上没给 y-axis 的正确写法"
    assert "0 -> 10" in c, "没点名单箭头这个坑"
    assert estimate_tokens(c) < 2000

    # 没有 mermaid 规则的任务卡不该出现图表段
    assert "【图表：验收规则" not in _card()


def test_rule_ids_in_card_follow_acceptance_order():
    """卡上的"硬验收 rule_N"必须是该规则**实际**的编号。

    原来写死 rule_3/rule_4（恰好等于 acceptance_market 的下标）。换成
    acceptance_t1 这种 6 条规则的验收后，时效实际是 rule_4——写死就会指向
    另一条规则，模型照着编号核对的却是不相干的那条。
    """
    acceptance = [
        {"type": "file_exists", "path": "artifacts/2026-09-29/t1.csv",
         "rule_id": "rule_0"},
        {"type": "row_count", "path": "artifacts/2026-09-29/t1.csv",
         "min": 20, "rule_id": "rule_1"},
        {"type": "business_rule", "expr": "cols >= 5",
         "path": "artifacts/2026-09-29/t1.csv", "rule_id": "rule_2"},
        {"type": "source_traceable", "rule_id": "rule_3"},
        {"type": "freshness", "freshness_days": 365, "rule_id": "rule_4"},
        {"type": "source_quality",
         "source_tier_min_ratio": {"A": 0.2}, "rule_id": "rule_5"},
    ]
    c = _card(objective="采集数据", needs_web=True, freshness="recent",
              acceptance=acceptance,
              outputs=["artifacts/2026-09-29/t1.csv"],
              key_questions=["最新一期数据是多少？"])
    assert "时效（硬验收 rule_4）" in c, c
    assert "分级（硬验收 rule_5）" in c, c
    assert "时效（硬验收 rule_3）" not in c
    # 门槛数字本身也要照抄验收
    assert "≥ 20%" in c and "365" in c


def test_json_schema_ref_is_inlined_as_shape():
    """schema_ref 只是一行**路径**，形状必须连类型一起渲染进卡。

    实测 2026-10-05：t3_report 连挂两轮进死信——卡上只给
    `schema_ref: schemas/summary.schema.json`，objective 又只说"date + rows
    必填"，模型把 rows 写成城市明细数组（schema 实际要 integer 行数）。
    模型不会为了核对一个类型专门去读文件，类型得摆在它眼前。
    """
    acc = ACCEPTANCE + [
        {"type": "json_schema", "path": "artifacts/2026-10-05/summary.json",
         "schema_ref": "schemas/summary.schema.json", "rule_id": "rule_2"},
    ]
    c = _card(task_id="t3_report", objective="生成日报",
              outputs=["artifacts/2026-10-05/summary.json"], acceptance=acc)
    assert "schemas/summary.schema.json（rule_2）必需字段" in c
    assert '"rows": <integer>' in c, "形状示例里必须能看出 rows 是标量"
    assert "rows=integer" in c
    assert "不是数组" in c
    assert estimate_tokens(c) < 2000


def test_schema_hint_falls_back_when_ref_unreadable():
    """schema 文件读不到时退回"只给路径"，绝不能把整张卡渲染炸掉。"""
    acc = ACCEPTANCE + [{"type": "json_schema", "path": "artifacts/x.json",
                         "schema_ref": "schemas/nope.schema.json"}]
    c = _card(acceptance=acc)
    assert "【JSON 形状" not in c
    assert estimate_tokens(c) < 2000


def test_card_without_json_schema_has_no_shape_block():
    """没有 json_schema 规则的任务不该被塞一段无关形状说明（每行都是 token）。"""
    assert "【JSON 形状" not in _card()


def test_t3_real_acceptance_says_rows_is_integer():
    """回归门禁：拿真 acceptance_t3 渲染，卡上必须点明 rows 是整数、编号对得上。"""
    import yaml
    acc = yaml.safe_load((ROOT / "schemas" / "acceptance_t3.yaml")
                         .read_text(encoding="utf-8"))
    c = _card(task_id="t3_report", objective="生成日报",
              outputs=["artifacts/2026-10-05/summary.json",
                       "artifacts/2026-10-05/report.md"],
              acceptance=acc)
    assert "rows=integer" in c, c
    assert "不是数组" in c
    assert "rule_2" in c, "卡上的编号要和 verifier 报的 rule_2 对得上"
    # 必需字段与 description 必须一起进卡：只给类型，模型知道 rows 要整数，
    # 却不知道该填什么整数、也不知道 data_period/tier_summary 存在。
    assert "data_period" in c and "tier_summary" in c and "key_findings" in c
    assert "数据期次" in c, "schema 的 description 没进卡 = 字段含义丢了"
    assert estimate_tokens(c) < 2000
