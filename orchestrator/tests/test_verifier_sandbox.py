"""Phase4 §2：把三个**沙箱关键**handler补到有测试。

补测动机不是数字，是这三个恰好是注入面最要紧的：
json_schema（读任意schema文件）、business_rule/eval_expr（表达式求值）。
设计的覆盖率硬线在这里抓到的是真洞——之前9个handler只测了6个。

覆盖率门禁本身在 scripts/check_coverage.py（不能放单测里：
那会在测试进程里再跑全量测试，而全量测试包含它自己，无限递归）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import verifier as vf
from tests.test_orchestrator import _root
from tests.test_verifier import DATE, _art, _ctx


# ---------- json_schema ----------

def test_json_schema_pass(tmp_path):
    root = _root(tmp_path)
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "schemas" / "s.json").write_text(json.dumps({
        "type": "object", "required": ["date", "rows"],
        "properties": {"date": {"type": "string"}, "rows": {"type": "integer"}}}),
        encoding="utf-8")
    _art(root, f"artifacts/{DATE}/summary.json",
         json.dumps({"date": DATE, "rows": 3}))
    got = vf.h_json_schema(_ctx(root), {
        "schema_ref": "schemas/s.json",
        "path": f"artifacts/{DATE}/summary.json", "rule_id": "r"})
    assert got["ok"] is True, got


def test_json_schema_violation(tmp_path):
    root = _root(tmp_path)
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "schemas" / "s.json").write_text(json.dumps({
        "type": "object", "required": ["date", "rows"]}), encoding="utf-8")
    _art(root, f"artifacts/{DATE}/summary.json", json.dumps({"date": DATE}))
    got = vf.h_json_schema(_ctx(root), {
        "schema_ref": "schemas/s.json",
        "path": f"artifacts/{DATE}/summary.json", "rule_id": "r"})
    assert got["ok"] is False
    assert "schema_violation" in got["detail"]


def test_json_schema_missing_ref_and_files(tmp_path):
    root = _root(tmp_path)
    assert vf.h_json_schema(_ctx(root), {})["detail"] == "no_schema_ref"
    got = vf.h_json_schema(_ctx(root), {"schema_ref": "schemas/nope.json"})
    assert got["ok"] is False and "schema_missing" in got["detail"]
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "schemas" / "s.json").write_text("{}", encoding="utf-8")
    got = vf.h_json_schema(_ctx(root), {
        "schema_ref": "schemas/s.json", "path": f"artifacts/{DATE}/nope.json"})
    assert got["ok"] is False and "target_missing" in got["detail"]


def test_json_schema_bad_json(tmp_path):
    root = _root(tmp_path)
    (root / "schemas").mkdir(parents=True, exist_ok=True)
    (root / "schemas" / "s.json").write_text("{坏", encoding="utf-8")
    _art(root, f"artifacts/{DATE}/summary.json", "{}")
    got = vf.h_json_schema(_ctx(root), {
        "schema_ref": "schemas/s.json",
        "path": f"artifacts/{DATE}/summary.json"})
    assert got["ok"] is False and "bad_json" in got["detail"]


# ---------- eval_expr（禁eval的AST白名单） ----------

def test_eval_expr_basic_comparison():
    ok, why = vf.eval_expr("rows > 10", {"rows": 20})
    assert ok is True and why == ""
    ok, _ = vf.eval_expr("rows > 10", {"rows": 5})
    assert ok is False


def test_eval_expr_boolean_and_comparison():
    ok, _ = vf.eval_expr("rows > 10 and bytes < 1000",
                         {"rows": 20, "bytes": 500})
    assert ok is True
    ok, _ = vf.eval_expr("rows > 10 or null_rate < 0.1",
                         {"rows": 5, "null_rate": 0.01})
    assert ok is True


def test_eval_expr_rejects_function_call():
    """表达式求值是最高危的注入面：任何调用都必须拒。"""
    for expr in ["open('/etc/passwd')", "__import__('os').system('x')",
                 "len(rows) > 1", "rows.sum() > 1"]:
        ok, why = vf.eval_expr(expr, {"rows": 1})
        assert ok is False, expr
        assert "forbidden_node" in why, (expr, why)


def test_eval_expr_rejects_attribute_access():
    ok, why = vf.eval_expr("rows.__class__ > 1", {"rows": 1})
    assert ok is False and "forbidden_node" in why


def test_eval_expr_rejects_lambda_and_comprehension():
    for expr in ["(lambda: 1)() > 0", "[x for x in rows]",
                 "{k: 1 for k in rows}"]:
        ok, why = vf.eval_expr(expr, {"rows": [1]})
        assert ok is False and "forbidden_node" in why, expr


def test_eval_expr_rejects_unknown_var():
    """变量白名单：不能引用任意名字探测环境。"""
    ok, why = vf.eval_expr("secret > 1", {})
    assert ok is False and "unknown_var:secret" in why


def test_eval_expr_rejects_bad_syntax():
    ok, why = vf.eval_expr("rows >", {})
    assert ok is False and "bad_syntax" in why


def test_eval_expr_defaults_missing_vars_to_zero():
    """未提供的统计量按0算，而不是KeyError——规则不该因数据缺字段而崩。"""
    ok, _ = vf.eval_expr("rows > 0", {})
    assert ok is False
    ok, _ = vf.eval_expr("rows >= 0", {})
    assert ok is True


# ---------- business_rule ----------

def test_business_rule_on_csv_computes_rows_cols_bytes(tmp_path):
    root = _root(tmp_path)
    _art(root, f"artifacts/{DATE}/d.csv", "a,b,c\n1,2,3\n4,5,6\n")
    got = vf.h_business_rule(_ctx(root), {
        "expr": "rows >= 2 and cols == 3 and bytes > 0",
        "path": f"artifacts/{DATE}/d.csv", "rule_id": "r"})
    assert got["ok"] is True, got


def test_business_rule_fails_when_threshold_not_met(tmp_path):
    root = _root(tmp_path)
    _art(root, f"artifacts/{DATE}/d.csv", "a,b\n1,2\n")
    got = vf.h_business_rule(_ctx(root), {
        "expr": "rows >= 100", "path": f"artifacts/{DATE}/d.csv"})
    assert got["ok"] is False


def test_business_rule_requires_expr(tmp_path):
    root = _root(tmp_path)
    assert vf.h_business_rule(_ctx(root), {})["detail"] == "no_expr"


def test_business_rule_missing_file(tmp_path):
    root = _root(tmp_path)
    got = vf.h_business_rule(_ctx(root), {
        "expr": "rows > 0", "path": f"artifacts/{DATE}/nope.csv"})
    assert got["ok"] is False and "missing" in got["detail"]


def test_business_rule_rejects_path_escape(tmp_path):
    root = _root(tmp_path)
    got = vf.h_business_rule(_ctx(root), {
        "expr": "bytes > 0", "path": "/etc/passwd"})
    assert got["ok"] is False and "security" in got["detail"]


def test_business_rule_works_without_path(tmp_path):
    """不挂文件的规则可以用ctx里注入的统计量。"""
    root = _root(tmp_path)
    ctx = _ctx(root, variables={"rows": 50, "null_rate": 0.01})
    got = vf.h_business_rule(ctx, {"expr": "rows > 10 and null_rate < 0.05"})
    assert got["ok"] is True, got
