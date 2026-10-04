"""验收规则引用的静态文件必须真实存在——否则任务会恒定进死信，而测试还全绿。

2026-10-03 生产事故：`schemas/acceptance_t2.yaml` 里的
`python_test: pytest tests/test_clean.py -q` 在仓库里没有对应文件（顶层连
tests/ 目录都没有），而 h_python_test 以 cwd=repo=store.root.parent 跑，
rc=4 每轮必挂、与模型表现无关 → daily_report_2026-10-03/t2_clean 三轮烧完
进死信 → t3_report 永远 PENDING → 当日日报没生成。

它能活到线上才发现，是因为 tests/test_smoke_e2e.py 在临时 repo 里自己手写了
那个文件——**测试补齐前置条件，就把生产必现的配置错误藏住了**（743 全绿）。

这条 lint 把"验收引用的静态文件必须存在"变成一条会红的断言：
- python_test 的命令行里出现的 .py/.yaml/.json，按生产 cwd（repo）必须找得到
- json_schema 的 schema_ref 按 ctx["root"] 必须找得到

产物类路径（file_exists/row_count 的 artifacts/{date}/...）是运行期生成的，
不在这条检查范围内。
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent                     # 生产里 h_python_test 的 cwd
SCHEMAS = ROOT / "schemas"

sys.path.insert(0, str(ROOT))

_SUFFIXES = (".py", ".yaml", ".json")
ACC_FILES = sorted(SCHEMAS.glob("acceptance_*.yaml"))


def _rules(path: Path) -> list:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [r for r in (data or []) if isinstance(r, dict)]


def test_acceptance_files_present():
    assert ACC_FILES, "schemas/ 下没有 acceptance_*.yaml，lint 空跑等于没测"


def test_python_test_targets_exist_in_repo():
    """python_test 指向的文件按生产 cwd 必须真的存在，否则 rc=4 恒定失败。

    只按 REPO 判，不接受"在 orchestrator/ 下存在"——生产跑命令时的 cwd 就是
    repo，写成 `tests/x.py` 而文件实际在 `orchestrator/tests/x.py` 时，
    测试里的等价断言会绿，生产照样 rc=4（这正是原 bug 的形态）。
    """
    missing = []
    for f in ACC_FILES:
        for rule in _rules(f):
            if rule.get("type") != "python_test":
                continue
            cmd = str(rule.get("cmd") or "")
            for tok in cmd.split():
                if not tok.endswith(_SUFFIXES) or "{" in tok:
                    continue
                if not (REPO / tok).exists():
                    missing.append(f"{f.name}: `{cmd}` → 仓库根下找不到 {tok}")
    assert not missing, (
        "验收命令指向不存在的文件（生产会 rc=4 每轮必挂、任务进死信）:\n"
        + "\n".join(missing))


def test_json_schema_refs_exist():
    """schema_ref 按 ctx['root'] 解析，指向的 schema 必须在。"""
    missing = []
    for f in ACC_FILES:
        for rule in _rules(f):
            if rule.get("type") != "json_schema":
                continue
            ref = str(rule.get("schema_ref") or "")
            if not ref or "{" in ref:
                continue
            if not (ROOT / ref).exists():
                missing.append(f"{f.name}: schema_missing:{ref}")
    assert not missing, "json_schema 规则的 schema 文件不存在:\n" + "\n".join(missing)


def test_no_stale_test_clean_reference():
    """回归锚点：那条打死了 t2_clean 的命令不许再作为验收规则出现。

    只查规则里的 cmd，不查文件全文——yaml 注释要保留事故原委（谁、什么时候、
    为什么删），把历史写进配置正是这次的教训之一。这条钉死的是"具体哪条规则
    杀了生产"，即使哪天那个文件被谁补出来了也一样红。
    """
    hits = [f"{f.name}: {r.get('cmd')}"
            for f in ACC_FILES for r in _rules(f)
            if "test_clean" in str(r.get("cmd") or "")]
    assert not hits, f"验收规则里又出现了 tests/test_clean.py：{hits}"


# ---------- 模板 ↔ 验收 的交叉检查 ----------

TEMPLATES = sorted((ROOT / "templates").glob("*.yaml"))


def _template_tasks(path: Path) -> list:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [t for t in (data.get("tasks") or []) if isinstance(t, dict)]


def test_templates_and_refs_present():
    assert TEMPLATES, "templates/ 下没有 *.yaml"
    assert any(t.get("acceptance_ref") for tpl in TEMPLATES
               for t in _template_tasks(tpl)), "模板任务都没写 acceptance_ref"


def test_acceptance_ref_files_exist():
    """模板引用的验收文件必须在——展开时才读它，缺了直接 FileNotFoundError。

    与 test_python_test_targets_exist_in_repo 是两件事：那条查"规则里跑的
    文件在不在生产 cwd"，这条查"规则本身所在的文件在不在"。
    """
    missing = [f"{tpl.name}: {t.get('id')} → {t.get('acceptance_ref')}"
               for tpl in TEMPLATES for t in _template_tasks(tpl)
               if t.get("acceptance_ref")
               and not (ROOT / str(t["acceptance_ref"])).exists()]
    assert not missing, "acceptance_ref 指向不存在的文件:\n" + "\n".join(missing)


def test_rule_types_registered():
    """每种规则类型都要同时过两道门：Pydantic 的 Literal 和 verifier 的 HANDLERS。

    新增规则类型（如 2026-10-04 的 mermaid）只改 yaml 一边，会在运行期变成
    bad_acceptance_rule / handler_error——前者拦在入队，后者在验收时报
    "找不到handler"，两种都与模型表现无关、却长得像模型的错。
    """
    from core import verifier as vf
    from schemas.models import AcceptanceRule

    types = {r.get("type") for f in ACC_FILES for r in _rules(f)
             if r.get("type")}
    assert types, "没有任何规则类型，lint 空跑等于没测"
    unregistered = sorted(t for t in types if t not in vf.HANDLERS)
    unmodelled = []
    for t in sorted(types):
        try:
            AcceptanceRule(type=t)
        except Exception:            # noqa: BLE001 —— 报错原文比类型名有用
            unmodelled.append(t)
    assert not unregistered, f"规则没进 verifier.HANDLERS: {unregistered}"
    assert not unmodelled, f"规则没进 AcceptanceRule 的 Literal: {unmodelled}"


def test_acceptance_paths_point_at_declared_outputs():
    """file_exists/bytes 这类带 path 的规则，path 必须是**该任务声明过的产物**。

    2026-10-03 的实跑事故就是这条的反面：acceptance_market 写死
    artifacts/{date}/report.md，而 t1/t2/t3 的产物是各自的名字——
    谁顺手写了 report.md，另外两条的产物存在性就一起"通过"，
    等于验收查了一个没人承诺要产出的文件。{output} 按设计展开成 outputs[0]，
    是正解，跳过；其余写死路径必须能在 outputs 里逐字找到。
    """
    bad = []
    for tpl in TEMPLATES:
        for t in _template_tasks(tpl):
            ref = t.get("acceptance_ref")
            if not ref or not (ROOT / str(ref)).exists():
                continue
            outs = {str(o) for o in (t.get("outputs") or [])}
            for r in _rules(ROOT / str(ref)):
                p = r.get("path")
                if p is None or p == "{output}":
                    continue
                if str(p) not in outs:
                    bad.append(f"{tpl.name}:{t.get('id')} 的 {r.get('type')} "
                               f"path={p} 不在 outputs={sorted(outs)} 里")
    assert not bad, "验收查的文件没人声明要产出：\n" + "\n".join(bad)
