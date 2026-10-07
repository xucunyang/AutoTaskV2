"""每日政策链（policy_daily）门禁：用户点名的需求必须变成机械断言。

2026-10-06 新增。需求原文是"调研近期要执行的新政策，包含各个方面，
比如三农、普通工薪阶层等各方面的政策，以及取消的政策等，同时要调研
其可能产生的影响"。这句话拆成四个**可判定**的交付要求：

1. 新政清单      → t1_policy_new.csv
2. 取消/废止政策 → t2_policy_repeal.md
3. 覆盖面（"各个方面"）→ t3 的 domain_coverage，且必须含三农与工薪社保
4. 影响          → t3 的 impact_assessment（人群+方向+量级）

只把它们写进 objective 等于"靠记性"——objective 是自然语言，模型可以
漂亮地绕过。hardware_daily 的教训（2026-10-06，t2 卡 source_quality 烧死信
之后才建专用 acceptance）说明：判断必须落在 acceptance 里。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
TPL = ROOT / "templates" / "policy_daily.yaml"

sys.path.insert(0, str(ROOT))


def _rules(name: str) -> list[dict]:
    data = yaml.safe_load((SCHEMAS / name).read_text(encoding="utf-8"))
    return [r for r in (data or []) if isinstance(r, dict)]


def _template() -> dict:
    return yaml.safe_load(TPL.read_text(encoding="utf-8")) or {}


def _task(tid: str) -> dict:
    for t in _template().get("tasks") or []:
        if t.get("id") == tid:
            return t
    raise AssertionError(f"policy_daily.yaml 里没有任务 {tid}")


# ---------- 三个任务分别对应用户的三块要求 ----------

def test_three_tasks_cover_new_repeal_and_impact():
    ids = [t.get("id") for t in _template().get("tasks") or []]
    assert ids == ["t1_policy_new", "t2_policy_repeal", "t3_policy_impact"], ids
    assert _task("t1_policy_new")["outputs"] == ["artifacts/{date}/t1_policy_new.csv"]
    assert _task("t2_policy_repeal")["outputs"] == ["artifacts/{date}/t2_policy_repeal.md"]
    assert _task("t3_policy_impact")["outputs"] == [
        "artifacts/{date}/policy_summary.json",
        "artifacts/{date}/policy_report.md",
    ]


def test_impact_task_waits_for_both_upstreams():
    """影响分析必须同时看新政与废止——只看一半的影响判断是错的。"""
    assert sorted(_task("t3_policy_impact")["depends_on"]) == [
        "t1_policy_new", "t2_policy_repeal"]


# ---------- 政策域保留 source_quality（与硬件链相反，且是有意的） ----------

def test_policy_collection_keeps_source_quality():
    """hardware 摘掉 source_quality 是因为 A 类在硬件消费品里天然不存在；
    政策原文就在 gov.cn/部委官网，A 类本就 plentiful——**不许顺手统一掉**。
    """
    for name in ("acceptance_policy_t1.yaml", "acceptance_policy_t2.yaml"):
        types = [r.get("type") for r in _rules(name)]
        for needed in ("source_traceable", "freshness", "source_quality"):
            assert needed in types, f"{name} 缺 {needed}"


def test_policy_rollup_has_no_source_rules():
    """汇总任务 needs_web:false、不产 sources.json，加 source_* 只会永远 FAIL
    （acceptance_rollup 的教训）。"""
    types = [r.get("type") for r in _rules("acceptance_policy_t3.yaml")]
    for banned in ("source_traceable", "freshness", "source_quality"):
        assert banned not in types, f"汇总任务不该有 {banned}"


# ---------- 覆盖面：把"三农/工薪阶层"钉成断言而不是叮嘱 ----------

def test_impact_acceptance_requires_sannong_and_gongsheng():
    """三农与工薪是**下限**而非全集（见 test_domain_coverage_demands_breadth）。
    schema 只管类型，管不住"答非所问"：一份 domain_coverage=["宏观","金融"] 的
    摘要类型完全合法，却连普通人相关的领域都没碰。所以保留这两条逐字断言，
    当作"广度之外再加一道普通人相关的地板"。"""
    exprs = [r.get("expr") for r in _rules("acceptance_policy_t3.yaml")
             if r.get("type") == "business_rule"]
    joined = " | ".join(e for e in exprs if e)
    assert "'三农' in j_get(j, ['domain_coverage'], [])" in joined, \
        f"没有逐字查三农：{exprs}"
    assert "'工薪社保' in j_get(j, ['domain_coverage'], [])" in joined, \
        f"没有逐字查工薪社保：{exprs}"


def test_repeal_count_may_be_zero_but_must_be_integer():
    """取消政策"可能真的没有"——硬卡 >=1 会逼模型编造废止条目凑数，
    那比交白卷更坏（读者会拿这条去改行为）。所以下限是 0，
    但类型必须是 integer（schema 管），且模板要求如实说明。"""
    schema = json.loads((SCHEMAS / "policy_summary.schema.json")
                        .read_text(encoding="utf-8"))
    props = schema["properties"]
    assert props["policies_repealed"]["minimum"] == 0
    assert props["policies_repealed"]["type"] == "integer"
    assert props["policies_new"]["minimum"] == 1
    assert props["impact_assessment"]["minItems"] >= 2


def test_domain_coverage_demands_breadth_not_just_two_words():
    """用户原话："包含各个方面，比如三农、普通工薪阶层等各方面的政策"。

    「比如…等」= 三农/工薪是**举例**，不是全集。2026-10-06 第一版把这两个词
    硬卡成唯一门禁，等于把实例当需求——覆盖面反而窄了。现在真正被卡的是
    **广度**：≥5 个不同领域；那两个词只是**下限**（不能连涉农、涉职工权益
    都不报），不是天花板。

    minItems 5 是这里唯一能机械判定的"各个领域"——它拦得住
    domain_coverage=["三农","工薪社保","宏观"] 这种凑数式复述。
    """
    schema = json.loads((SCHEMAS / "policy_summary.schema.json")
                        .read_text(encoding="utf-8"))
    dc = schema["properties"]["domain_coverage"]
    assert dc["minItems"] >= 5, f"覆盖面门槛只有 {dc['minItems']}，'各个方面'没被卡住"

    # 模板那份领域清单是检索地图，得真的够宽（≥10 个方向），不能只有三四个
    tpl = (ROOT / "templates" / "policy_daily.yaml").read_text(encoding="utf-8")
    markers = ["三农", "工薪社保", "住房", "教育", "医疗健康", "税收", "环保",
               "产业与科技", "交通物流", "能源", "对外贸易", "区域", "法治", "公共安全"]
    missing = [m for m in markers if m not in tpl]
    assert not missing, f"模板的领域清单缺：{missing}（用户要的是'各个方面'）"


def test_schema_actually_rejects_narrow_coverage():
    """光在文档里写 minItems 不算数——得确认 jsonschema 真会拒。
    「三农+工薪+宏观」这种凑数式复述必须被挡住，那才是"没扩散到其它领域"。"""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS / "policy_summary.schema.json")
                        .read_text(encoding="utf-8"))
    narrow = dict(_GOOD, domain_coverage=["三农", "工薪社保", "宏观"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(narrow, schema)
    # 合规那份（5 个领域）必须过
    jsonschema.validate(_GOOD, schema)


# ---------- 关键：这些 expr 必须真能在沙箱里跑 ----------

def _eval_exprs(exprs: list[str], summary: dict) -> list[bool]:
    from core.verifier import eval_expr
    return [eval_expr(e, {"j": summary})[0] for e in exprs]


_GOOD = {
    "date": "2026-10-06", "data_period": "2026-10-06",
    "policies_new": 12, "policies_repealed": 0,
    "domain_coverage": ["三农", "工薪社保", "住房", "税收", "环保与能耗"],
    "impact_assessment": ["提高种粮补贴 → 农户 → 利好 → 每亩约 +X 元（推断）",
                          "调整社保缴费基数 → 在职职工 → 成本增减 → 月到手约 -Y 元"],
    "key_findings": ["本期无废止政策（已核 gov.cn 政策库）"],
}


def test_policy_business_rules_are_valid_in_the_sandbox():
    """**这条是本文件最要紧的**。

    business_rule 的 expr 走 eval_expr 的 AST 白名单。表达式写坏的后果是
    **永久 FAIL**——但报错长得像"模型没写对"，于是人会去改模型、改 prompt、
    重跑三遍，最后才发现是 yaml 里一个非法节点。这就是 hardware_t2 死信那类
    "与模型表现无关、却长得像模型的错"。

    所以这里拿真实 expr 真的求值一遍：合法的返回 True；
    顺带确认沙箱没被这些表达式撑开（它们只用 j_get + in + 常量）。
    """
    exprs = [r.get("expr") for r in _rules("acceptance_policy_t3.yaml")
             if r.get("type") == "business_rule" and str(r.get("expr", "")).find("j_get") >= 0]
    assert len(exprs) == 3, f"期望 3 条用 j_get 的业务规则，实际 {len(exprs)}：{exprs}"
    assert _eval_exprs(exprs, _GOOD) == [True, True, True], \
        "合规摘要竟然没过门禁——表达式本身有问题"


@pytest.mark.parametrize("mutate,expect_fail_at", [
    (lambda s: s.update(policies_new=0), 0),
    (lambda s: s.update(domain_coverage=["住房", "税收"]), 1),
    (lambda s: s.update(domain_coverage=["三农", "住房"]), 2),
])
def test_policy_business_rules_catch_the_real_failure_modes(mutate, expect_fail_at):
    """反例必须真的被拦住——不然这些规则只是装饰。"""
    bad = json.loads(json.dumps(_GOOD))
    mutate(bad)
    exprs = [r.get("expr") for r in _rules("acceptance_policy_t3.yaml")
             if r.get("type") == "business_rule" and str(r.get("expr", "")).find("j_get") >= 0]
    results = _eval_exprs(exprs, bad)
    assert results[expect_fail_at] is False, \
        f"反例没被第 {expect_fail_at} 条拦住：{exprs} → {results}"


def test_missing_domain_coverage_key_does_not_crash():
    """j_get 的 default 是 []，字段整个缺失时 in 返回 False 而不是抛异常——
    抛异常会被 eval_expr 吞成 eval_error，规则同样判 FAIL，但 detail 会
    误导人去查表达式而不是查产物。"""
    expr = "'三农' in j_get(j, ['domain_coverage'], [])"
    from core.verifier import eval_expr
    ok, detail = eval_expr(expr, {"j": {"date": "2026-10-06"}})
    assert ok is False and not detail.startswith("eval_error"), detail