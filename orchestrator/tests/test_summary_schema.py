"""summary.json 的 schema 合同：能过验收的必须是"有内容的摘要"，不是空壳。

2026-10-05 一天里这个文件栽了两次，方向相反：

1. schema 只要求 `date + rows`，模型把 rows 写成城市明细数组
   （[{城市:北京,...}]）→ rule_2 schema_violation ×2 → DEAD_LETTER；
2. 人工重开、卡片内联形状后，它交了 `{"date": "...", "rows": 69}`——
   过验收了，但昨天那份 12KB 的能级统计/关键结论/数据期次全没了。

**合同松，模型就交空壳**：验收只认 schema，schema 只有两个字段，那它
交两个字段是"完成任务"。所以这份测试直接拿 jsonschema 对着真 schema 判，
把"测试全绿但产物是空的"这条退路堵死——verifier 走的也是同一个库。
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jsonschema  # noqa: E402  verifier.h_json_schema 用的同一个库

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "summary.schema.json")
                    .read_text(encoding="utf-8"))


def _validate(obj) -> None:
    jsonschema.validate(obj, SCHEMA)


def test_acceptance_t3_points_at_this_schema():
    """合同只有一份：acceptance_t3 引用的必须就是这里测的这个文件。"""
    acc = yaml.safe_load((ROOT / "schemas" / "acceptance_t3.yaml")
                         .read_text(encoding="utf-8"))
    rule = next(r for r in acc if r["type"] == "json_schema")
    assert rule["schema_ref"] == "schemas/summary.schema.json"
    assert rule.get("rule_id") is None or rule["rule_id"] == "rule_2", \
        "卡上按 rule_{i} 渲染编号，规则编号变了要同步"


def test_hollow_two_field_summary_is_rejected():
    """10-05 那个 34 字节空壳必须被判失败——它过了验收才是问题。"""
    with pytest.raises(jsonschema.ValidationError):
        _validate({"date": "2026-10-05", "rows": 69})


def test_rows_as_city_array_is_rejected():
    """10-05 连挂两轮进死信的写法：rows 里塞城市明细。"""
    with pytest.raises(jsonschema.ValidationError) as e:
        _validate({"date": "2026-10-05", "data_period": "2026-08",
                   "rows": [{"城市": "北京", "城市能级": "一线"}],
                   "tier_summary": {"一线": {}},
                   "key_findings": ["结论"]})
    assert "rows" in str(e.value)


def test_findings_must_be_non_empty_array_of_strings():
    """key_findings 是摘要的灵魂：空数组、或塞一段整文都不算数。"""
    base = {"date": "2026-10-05", "data_period": "2026-08", "rows": 69,
            "tier_summary": {"一线": {}}}
    with pytest.raises(jsonschema.ValidationError):
        _validate({**base, "key_findings": []})
    with pytest.raises(jsonschema.ValidationError):
        _validate({**base, "key_findings": ["a", 2]})


def test_full_summary_passes():
    """按 10-04 的真实产物裁一刀：合格摘要必须过，别把门收成谁都进不来。"""
    _validate({
        "date": "2026-10-04",
        "data_period": "2026-08",
        "rows": 75,
        "tier_summary": {
            "一线": {"new_home_mom_pct": 0.1, "city_count_in_clean": 4},
            "二线": {"new_home_mom_pct": -0.1, "city_count_in_clean": 31},
        },
        "key_findings": [
            "一线4城新房环比均值0.13%，整体维持微涨。",
            "70城中环比上涨或持平城市数：新房21个、二手房8个。",
        ],
        # 额外字段一律放行（schema 没禁 additionalProperties）
        "source_csv": "artifacts/2026-10-04/t2_clean.csv",
    })


def test_schema_description_is_usable_as_card_copy():
    """description 会被逐字渲染进任务卡，太长就是纯噪音（卡有 2k token 门禁）。"""
    for key in SCHEMA["required"]:
        desc = str((SCHEMA["properties"].get(key) or {}).get("description") or "")
        assert desc, f"{key} 没有 description：卡上只能给类型，模型不知道填什么"
        assert len(desc) <= 48, f"{key} 的 description {len(desc)} 字，超了卡上会被截断"
