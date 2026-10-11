"""验收表达式沙箱门禁（恒红规则拦截）。

背景（2026-10-11 t1b/t4 双双踩雷）：业务规则表达式跑在 AST 白名单
沙箱里——BinOp（算术）与 len() 都不在白名单，写了就恒红，任务会
被无差别推进死信（acceptance_t2 教训的变体）。本测试扫全部
acceptance_*.yaml 的 expr，在沙箱里干跑一遍：
  - forbidden_node / unknown_var / bad_syntax → 一律 FAIL（规则本身
    不可满足，与数据无关）；
  - eval_error 允许（stub 数据形态可能不齐，说明表达式能解析、
    语义要靠真实数据验收）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.verifier import EXPR_VARS, eval_expr  # noqa: E402

STUB_VARS = {**{k: 0 for k in EXPR_VARS if k != "j"},
             "rows": [], "j": {}}
BAD_PREFIXES = ("forbidden_node", "unknown_var", "bad_syntax")


def _all_exprs():
    for f in sorted((ROOT / "schemas").glob("acceptance_*.yaml")):
        rules = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        for i, rule in enumerate(rules if isinstance(rules, list) else []):
            if isinstance(rule, dict) and rule.get("type") == "business_rule" \
                    and rule.get("expr"):
                yield f.name, i, rule["expr"]


def test_no_acceptance_expr_uses_forbidden_nodes():
    offenders = []
    for fname, idx, expr in _all_exprs():
        ok, detail = eval_expr(expr, dict(STUB_VARS))
        if not ok and detail.startswith(BAD_PREFIXES):
            offenders.append(f"{fname}#{idx}: {expr} -> {detail}")
    assert not offenders, "恒红规则（沙箱不可执行）:\n" + "\n".join(offenders)


def test_sku_ingest_rules_pass_on_cold_start_stats():
    """冷启动现实（13 行入 5 行隔离 4 行）必须过——校准后的门槛。"""
    j = {"ok": 5, "updated": 0, "quarantined": 0, "no_entity": 4,
         "ingested": 5, "isolated": 4}
    ok1, d1 = eval_expr('j_get(j, ["ingested"], 0) >= 5', {"j": j})
    ok2, d2 = eval_expr('j_get(j, ["isolated"], 0) <= j_get(j, ["ingested"], 0)',
                        {"j": j})
    assert ok1 and not d1, d1
    assert ok2 and not d2, d2
    # 隔离多于入库 → rule_2 拦（透明规则要能红）
    ok3, _ = eval_expr('j_get(j, ["isolated"], 0) <= j_get(j, ["ingested"], 0)',
                       {"j": {"ingested": 2, "isolated": 4}})
    assert not ok3


def test_hw_t4_recommend_rule():
    """decisions 键存在即可（空数组合法——无显著变化日）；缺键/缺日期要红。"""
    expr = 'j_get(j, ["date"], "") != "" and "decisions" in j'
    ok, d = eval_expr(expr, {"j": {"date": "2026-10-11", "decisions": []}})
    assert ok, d
    ok2, _ = eval_expr(expr, {"j": {"date": "2026-10-11"}})
    assert not ok2                      # 缺 decisions 键
    ok3, _ = eval_expr(expr, {"j": {"decisions": []}})
    assert not ok3                      # 缺日期


def test_python_test_whitelist_covers_first_party_tools():
    """t4 的 verify_snap cmd 必须过白名单（2026-10-11 实测：白名单只有
    tests/ 前缀，tools/ 的第一方校验脚本恒红——第三颗同质地雷）。"""
    from core.verifier import CMD_WHITELIST, SHELL_METACHARS

    cmd = "python -X utf8 orchestrator/tools/verify_snap.py"
    assert cmd.startswith(CMD_WHITELIST)
    assert not (SHELL_METACHARS & set(cmd))     # 无 shell 注入面
    # 白名单不放开任意命令：裸 python 与任意路径仍被拒
    assert not "python evil.py".startswith(CMD_WHITELIST)
    assert not "python -X utf8 C:/Temp/x.py".startswith(CMD_WHITELIST)


def test_sku_ingest_stats_writes_derived_fields(tmp_path):
    """入库器 stats 必须带 ingested/isolated 预计算字段（验收依赖）。"""
    import json

    from tools.sku_db import connect
    from tools.sku_ingest import Contract, ingest_file

    con = connect(tmp_path / "sku.db")
    now = "2026-10-11T00:00:00+00:00"
    con.execute(
        "INSERT INTO entity(domain, identity_json, display_name,"
        " created_at, updated_at) VALUES('hardware',?,?,?,?)",
        (json.dumps({"category": "显卡", "series": "RTX 5060 Ti",
                     "spec": "8GB"}), "RTX 5060 Ti 8GB", now, now))
    obs_file = tmp_path / "obs.json"
    obs_file.write_text(json.dumps([
        {"identity_hint": {"category": "显卡", "series": "RTX 5060 Ti",
                           "spec": "8GB"},
         "attrs_hint": {"condition": "全新", "channel": "自营",
                        "source": "自采"},
         "low_price": 2859, "price_date": "2026-10-11",
         "shop": {"name": "影驰旗舰店", "platform": "京东"}},
        # 一行缺 shop → bad_row
        {"identity_hint": {"category": "显卡", "series": "RTX 5060 Ti",
                           "spec": "8GB"},
         "attrs_hint": {"condition": "全新", "channel": "自营",
                        "source": "自采"},
         "low_price": 2860, "price_date": "2026-10-11"},
    ], ensure_ascii=False), encoding="utf-8")
    contract = Contract.load(ROOT, "hardware")
    stats = ingest_file(con, obs_file, contract, "2026-10-11")
    assert stats["ingested"] == stats["ok"] + stats["updated"] == 1
    assert stats["bad_row"] == 1
    assert stats["isolated"] == 0
    con.close()
