"""验证器（Phase3 §2）：无验收不DONE，验收本身防注入防flaky误杀。

三条设计要点：
1. **沙箱化**：所有handler都不信任输入。路径必须规范化且不出artifacts/{date}/，
   cmd只走白名单前缀且shell=False，表达式走AST白名单求值（禁eval）。
2. **不短路**：任一rule FAIL仍跑完剩下的，全量报告。短路会让"到底还错了几处"
   看不见，复查得重跑一遍。
3. **局部重入**（§2.2c）：已通过的rule_id记在tasks.verify_progress_json，
   重入时跳过。验收规则天然可重入，所以不需要新状态边。

flaky只重试"测试层"（python_test），且**不计任务attempts**——
测试偶发失败不代表任务失败，把它算进attempts会让好任务被误送死信。
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.search import pipeline as search_pipeline
from core.utils import jlog, now_utc_iso

CMD_WHITELIST = ("pytest tests/", "python tests/", "python -m pytest tests/")
SHELL_METACHARS = set(";|&`$><\n\r")
ALLOWED_EXPR_NODES = (ast.Expression, ast.Compare, ast.BoolOp, ast.Name,
                      ast.Constant, ast.Load, ast.And, ast.Or, ast.Not,
                      ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
                      ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
                      ast.List, ast.Tuple, ast.Dict, ast.Subscript,
                      ast.Call)
# business_rule里允许引用的变量（只读统计值，不是任意属性访问）
EXPR_VARS = {"rows", "null_rate", "bytes", "cols", "files", "value", "j"}
# 表达式可以调用的白名单函数：全部是**只读**的 helper，没有任何 I/O。
# Call 节点进白名单是为了让 j_get(j, path, default) 这种深路径取值能写，
# 同时**不让用户调用 open/__import__/rows.sum/__class__**——后者在
# 求值时会被 Name 拦下（不在 EXPR_VARS），而 j_get 在白名单里可以调。
def _j_get(obj, path, default=None):
    """j_get(obj, ["a", "b"], default)：按 key 链取嵌套 dict/list 的值。

    走 `[]` 而不是 `.get` 是为了**不暴露 Attribute 节点**——Attribute
    一进白名单就立刻能拿到 `__class__`/`__bases__`/`__subclasses__` 这
    链爬对象——这是 Phases 4 沙箱测试一直在拦的注入面。所以内置函数
    帮用户深取值，但**不允许 expr 写 obj.attr**。
    """
    cur = obj
    for k in (path if isinstance(path, (list, tuple)) else [path]):
        if cur is None:
            return default
        try:
            cur = cur[k] if isinstance(cur, dict) else cur[int(k)]
        except (KeyError, IndexError, ValueError, TypeError):
            return default
    return cur


BUILTIN_FNS = {"j_get": _j_get}
# json 注入上限：summary 这种小文件 <32KB，~1MB 足够覆盖任何任务产物
JSON_INJECT_MAX_BYTES = 1 * 1024 * 1024
CMD_OUTPUT_LIMIT = 5000
FRESHNESS_DAYS = {"recent": 730, "strict": 180}
FRESHNESS_MAX_STALE_RATIO = 0.30     # 超期比例>30% → FAIL
A_TIER_MIN_RATIO = 0.20              # A类占比>20%（2026-10-03二次放宽；验收yaml优先）


def rule_id_of(rule: dict, index: int) -> str:
    """rule_id为空时按rule_{index}自动编号（老YAML不break）。"""
    return str(rule.get("rule_id") or f"rule_{index}")


def normalize_path(raw: str, date: str) -> tuple[Path | None, str]:
    """规范化路径并检查边界。返回(绝对路径, 错误原因)。
    越界（..、绝对路径、不在artifacts/{date}/下）直接拒绝并返回原因。"""
    s = str(raw or "").replace("\\", "/")
    if not s:
        return None, "empty_path"
    if s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        return None, f"absolute_path:{s}"
    if ".." in s.split("/"):
        return None, f"path_traversal:{s}"
    if not re.match(r"^artifacts/\d{4}-\d{2}-\d{2}/[A-Za-z0-9._\-/]*$", s):
        return None, f"outside_daily_dir:{s}"
    return Path(s), ""


# ---------- 基础 handlers ----------

def h_file_exists(ctx: dict, rule: dict) -> dict:
    # 注意必须拼root：normalize_path返回的是**相对**路径，
    # 直接 .exists() 会按进程CWD去找，等于"换个启动目录结论就变了"
    p, err = normalize_path(rule.get("path"), ctx["date"])
    if err:
        return {"ok": False, "detail": f"security:{err}"}
    full = ctx["root"] / p
    return {"ok": full.exists(),
            "detail": "" if full.exists() else f"missing:{rule.get('path')}"}


def h_row_count(ctx: dict, rule: dict) -> dict:
    """流式计数，不全量读（大CSV不能为了数行数把它读进内存）。"""
    p, err = normalize_path(rule.get("path"), ctx["date"])
    if err:
        return {"ok": False, "detail": f"security:{err}"}
    full = ctx["root"] / p
    if not full.exists():
        return {"ok": False, "detail": f"missing:{rule.get('path')}"}
    need = int(rule.get("min") or 0)
    n = 0
    with full.open("r", encoding="utf-8", errors="ignore") as f:
        for _ in f:
            n += 1
    rows = max(0, n - 1)          # 减表头
    return {"ok": rows >= need, "detail": f"rows={rows} need>={need}"}


def h_python_test(ctx: dict, rule: dict) -> dict:
    """白名单前缀 + shell=False + 超时。flaky只在这里重试。"""
    cmd = str(rule.get("cmd") or "").strip()
    if not cmd:
        return {"ok": False, "detail": "no_cmd"}
    if SHELL_METACHARS & set(cmd):
        return {"ok": False, "detail": f"security:shell_metachar:{cmd[:40]}"}
    if not cmd.startswith(CMD_WHITELIST):
        return {"ok": False, "detail": f"security:cmd_not_whitelisted:{cmd[:40]}"}
    # 墙钟封顶：timeout_s 由任务自己配，不封顶的话一条规则就能占住
    # verify worker（默认120s）。run_rules 也会下发 _rule_timeout_s 覆盖它。
    timeout = int(ctx.get("_rule_timeout_s") or rule.get("timeout_s")
                  or RULE_TIMEOUT_CAP_S)
    retries = int(rule.get("flaky_retry", 2))     # 默认重试2次
    attempts, out, code = [], "", -1
    for i in range(max(0, retries) + 1):
        try:
            p = subprocess.run(cmd, shell=False, cwd=str(ctx["repo"]),
                               capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8",
                               errors="ignore")
            code = p.returncode
            out = ((p.stdout or "") + (p.stderr or ""))[:CMD_OUTPUT_LIMIT]
        except subprocess.TimeoutExpired:
            code = -9
            out = f"timeout_after_{timeout}s"
        except OSError as e:
            return {"ok": False, "detail": f"spawn_failed:{e}"}
        attempts.append(code)
        if code == 0:
            break
    return {"ok": code == 0, "detail": f"rc={code} tries={len(attempts)} "
                                        f"out={out[-400:]}",
            "flaky": 0 < len(attempts) - 1 <= retries and code == 0}


def h_json_schema(ctx: dict, rule: dict) -> dict:
    ref = str(rule.get("schema_ref") or "")
    if not ref:
        return {"ok": False, "detail": "no_schema_ref"}
    sp = ctx["root"] / ref
    tp = ctx["root"] / str(rule.get("path") or "")
    if not sp.exists():
        return {"ok": False, "detail": f"schema_missing:{ref}"}
    if not tp.exists():
        return {"ok": False, "detail": f"target_missing:{rule.get('path')}"}
    try:
        schema = json.loads(sp.read_text(encoding="utf-8"))
        data = json.loads(tp.read_text(encoding="utf-8"))
    except ValueError as e:
        return {"ok": False, "detail": f"bad_json:{e}"}
    try:
        import jsonschema
    except ImportError:
        return {"ok": False, "detail": "jsonschema_not_installed"}
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as e:
        # str(e) 的首行是 'None of [<data 字典 repr>]'——前 200 字符根本
        # 看不到具体错（10-06 housing t2_xz_nj 三次死信全栽在这：每次只
        # 看到数据字典的 repr，看不到"哪个字段错了、validator 是什么"）。
        # 取 5 条具体错误（path/validator/message）拼成 detail，dead-letter
        # 排查时直接定位是哪个字段错了。
        try:
            v = jsonschema.Draft7Validator(schema)
            errs = list(v.iter_errors(data))[:5]
            detail_lines = [
                f"{'.'.join(str(p) for p in e.absolute_path) or '(root)'}: "
                f"{e.validator} {e.message[:120]}"
                for e in errs]
            detail = "schema_violation:" + " || ".join(detail_lines)
        except Exception:
            detail = f"schema_violation:{str(e)[:200]}"
        return {"ok": False, "detail": detail[:600]}
    except Exception as e:
        return {"ok": False, "detail": f"schema_error:{type(e).__name__}:{str(e)[:200]}"}
    return {"ok": True, "detail": ""}


def eval_expr(expr: str, variables: dict) -> tuple[bool, str]:
    """AST白名单求值，禁eval。"""
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as e:
        return False, f"bad_syntax:{e}"
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_EXPR_NODES):
            return False, f"forbidden_node:{type(node).__name__}"
        if isinstance(node, ast.Name) and node.id not in EXPR_VARS \
                and node.id not in BUILTIN_FNS:
            return False, f"unknown_var:{node.id}"
    env = {**{k: 0 for k in EXPR_VARS},
           **BUILTIN_FNS,
           **(variables or {})}
    try:
        return bool(eval(compile(tree, "<rule>", "eval"), {"__builtins__": {}}, env)), ""
    except Exception as e:  # noqa: BLE001
        return False, f"eval_error:{type(e).__name__}"


def h_business_rule(ctx: dict, rule: dict) -> dict:
    expr = rule.get("expr")
    if not expr:
        return {"ok": False, "detail": "no_expr"}
    variables = dict(ctx.get("variables") or {})
    p, err = normalize_path(rule.get("path"), ctx["date"]) if rule.get("path") else (None, "")
    if err:
        return {"ok": False, "detail": f"security:{err}"}
    if p is not None:
        full = ctx["root"] / p
        if not full.exists():
            return {"ok": False, "detail": f"missing:{rule.get('path')}"}
        variables.setdefault("bytes", full.stat().st_size)
        if full.suffix == ".csv":
            # 先读表头再数行：for循环会把缓冲耗尽，之后再readline()直接
            # 抛"I/O operation on closed file"（踩过）
            with full.open("r", encoding="utf-8", errors="ignore") as f:
                header = f.readline()
                n = 1 + sum(1 for _ in f)
            variables.setdefault("rows", max(0, n - 1))
            variables.setdefault("cols", len(header.split(",")) if header else 0)
        elif full.suffix == ".json":
            # 注入 `j` 让规则能用 j.get('key') 读字段；超限或解析失败用空 dict
            # 继续跑（让其它规则/原因接管，而不是把 business_rule 一棍打死）
            import json as _json
            try:
                if full.stat().st_size <= JSON_INJECT_MAX_BYTES:
                    variables["j"] = _json.loads(
                        full.read_text(encoding="utf-8", errors="ignore"))
                else:
                    variables["j"] = {}
            except Exception:  # noqa: BLE001
                variables["j"] = {}
    ok, detail = eval_expr(expr, variables)
    return {"ok": ok, "detail": detail or f"expr={expr}"}


def lint_chart_block(body: str) -> str | None:
    """mermaid 块的**渲染**判据：返回"这张图渲染不出来"的原因，看不出问题返回 None。

    为什么要有它：`h_mermaid` 原来只数块数，2026-10-05 报告里 13 张图
    12 张挂了同一个错——模型把 y-axis 写成空格分隔：

        y-axis "环比%" -0.5 0.5        解析失败：Expecting 'ARROW_DELIMITER'
        y-axis "环比%" -0.5 --> 0.5    正确

    于是"有 13 张图"和"13 张全渲染失败"同时成立，验收全绿。用 mermaid-cli
    实测过这个错（本机 node22 + mmdc：63 块修复前 FAIL 12、修复后全 PASS）。

    **只判有把握的**，宁可漏判也不误判：
    - y-axis 必须含 `-->`——这是 mermaid 语法强制的范围箭头，上面那个错的
      充分必要条件就是它缺失；带引号/不带引号的标题都不影响这一条。
      （没写 y-axis 的块直接放过：范围轴是可选的。）
    - xychart 的方括号必须配平——`bar [1, 2` 这种截断写法一定解析失败。
    刻意**不**查"有没有 x-axis / 有没有数据项"：那些 mermaid 可能容错，
    误杀会把好图判死，而明早的跑批没人盯着重试（漏判的代价只是"图丑"）。
    """
    if not body.strip():
        return "空块"
    lines = [ln for ln in body.splitlines() if ln.strip()]
    is_xy = bool(re.match(r"\s*xychart", lines[0], re.I))
    if is_xy and body.count("[") != body.count("]"):
        return (f"方括号不配平：[{body.count('[')} 个 [ 对 "
                f"{body.count(']')} 个 ]（块被截断了）")
    for ln in lines:
        if re.match(r"\s*y-axis\b", ln, re.I) and "-->" not in ln:
            return (f'y-axis 缺范围箭头，要写成 y-axis "标签" 最小 --> 最大：'
                    f"{ln.strip()}")
    return None


def h_mermaid(ctx: dict, rule: dict) -> dict:
    """md 里 mermaid **图表**块数量 ≥ min——"报告要有图"的机械判据。

    为什么需要它：原来验收只查"文件在不在/多大/有没有来源"，四份报告全是
    表格和长段文字，没有一处曲线，任务照样全绿——"有数据"和"看得懂"是两件事。

    计数只认**图表类**图（xychart/pie/gantt），mindmap/sequence 这类不算，
    免得模型贴个不相干的块凑数；`blocks` 一并报出来，方便看出"贴了但不会画"。

    2026-10-05 加了 `lint_chart_block`：数进去的必须是**渲染得出来**的图
    （见该函数 docstring）。`detail` 保持 `mermaid_charts= blocks= need>=`
    的老顺序——下游与测试都按子串匹配它，坏块信息追加在末尾。
    """
    p, err = normalize_path(rule.get("path"), ctx["date"])
    if err:
        return {"ok": False, "detail": f"security:{err}"}
    full = ctx["root"] / p
    if not full.exists():
        return {"ok": False, "detail": f"missing:{rule.get('path')}"}
    text = full.read_text(encoding="utf-8", errors="ignore")
    blocks = re.findall(r"```mermaid\s*\n(.*?)```", text, re.S | re.I)
    charts = [b for b in blocks
              if re.search(r"^\s*(xychart|pie|gantt)", b, re.I | re.M)]
    bad = [p for p in (lint_chart_block(b) for b in charts) if p]
    good = len(charts) - len(bad)
    need = int(rule.get("min") or 1)
    detail = f"mermaid_charts={good} blocks={len(blocks)} need>={need}"
    if bad:
        detail += f" bad={len(bad)} first={bad[0]}"
    return {"ok": good >= need, "detail": detail}


# ---------- 调研质量 4 handlers（§2.2b） ----------

def _four_paths(ctx: dict, task_id: str) -> dict[str, Path]:
    """四件套的候选路径（查找顺序见 _load_four_set 的 docstring）。

    单独抽出来，是为了让「读数据」和「读不出来时说清为什么」用**同一套**
    路径规则——两处各找各的，就会出现"文件明明在，detail 却说 no_sources_json"
    这种自相矛盾的话（2026-10-05 排查 t1_stocks 死信时就卡在这）。
    """
    root = Path(ctx["root"])
    declared = [str(o).replace("\\", "/")
                for o in (ctx.get("shard") or {}).get("outputs") or []]

    def _pick(suffix: str) -> Path:
        want = f"{task_id}{suffix}"
        for o in declared:
            # 只认"以 task_id.suffix 结尾"的声明，避免把别的任务文件张冠李戴
            if o.endswith("/" + want) or o == want:
                return root / o
        dirs: list[Path] = []
        for o in declared:                       # 1) 产物父目录（模型实际写的地方）
            dirs.append((root / o).parent)
        pd = plan_date_of(ctx.get("plan_id") or "")   # 2) plan 逻辑日期
        if pd:
            dirs.append(root / "artifacts" / pd)
        dirs.append(root / "artifacts" / str(ctx["date"]))   # 3) 入队日期（老行为）
        seen, uniq = set(), []
        for d in dirs:
            if d not in seen:
                seen.add(d)
                uniq.append(d)
        for d in uniq:                           # 按序找，谁有文件用谁
            if (d / want).exists():
                return d / want
        return uniq[0] / want                    # 都没有 → 返回首选，报"文件不存在"

    return {"sources": _pick(".sources.json"),
            "claims": _pick(".claims.json"),
            "report": _pick(".report.md")}


def _load_four_set(ctx: dict, task_id: str) -> tuple[list, list, str]:
    """读四件套。查找顺序：声明的 outputs → 产物所在目录 → plan 逻辑日期 → ctx 日期。

    为什么不能只按 `artifacts/{date}/` 找（`date` 取的是入队时间）：
    任务入队日和 plan 的**逻辑日期**经常不一致——补跑历史日期、或 plan 建得早
    而任务跑得晚，都会让两者差几天。实测 2026-10-03 就是这样：
    plan 逻辑日期 10-05（acceptance 的 path 在建 plan 时已展开成 10-05，
    所以 file_exists/business_rule 能过），而入队时间是 10-02，
    于是四件套去 `artifacts/2026-10-02/` 找，那里只有 .md 没有 claims/sources，
    三条 source 规则齐刷刷报 no_claims_json/no_sources_json——
    同一次验收里两套日期各走各的，必然自相矛盾。

    按 outputs 的父目录找是最可靠的：模型的 .md / .claims.json / .sources.json
    一定写在同一个目录（它就在那个目录下工作）。
    """
    root = Path(ctx["root"])
    p = _four_paths(ctx, task_id)
    sources = _safe_json(root, p["sources"])
    claims = _safe_json(root, p["claims"])
    rep = p["report"]
    report = rep.read_text(encoding="utf-8", errors="ignore") if rep.exists() else ""
    return _normalize_sources(sources), _normalize_claims(claims), report


def sources_issue_detail(ctx: dict, task_id: str) -> str:
    """sources 读不出条目时，说清到底是「没文件」「文件坏了」还是「形状不对」。

    为什么值得单列：2026-10-05 t1_stocks 死信，detail 只写 `no_sources_json`，
    可 `t1_stocks.sources.json` 就在产物目录里躺着——真实原因是标题里的
    英文双引号没转义（`A股2026"开门红"`），整份 JSON 解析不了，
    `_safe_json` 按"没有可用数据"吞掉并记 WARN。排查时间全花在
    "文件到底在不在"上。三种坏法的修法完全不同：
    没文件 = 模型没交；文件坏了 = 模型交了但写坏（retry 时照着改就行）；
    形状不对 = 交错了结构。**detail 要指向能改的那一层。**
    """
    p = _four_paths(ctx, task_id)["sources"]
    if not p.exists():
        return "no_sources_json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return (f"sources_json_invalid:{type(e).__name__}:{str(e)[:120]} "
                f"@{p.name}")
    if not _normalize_sources(raw):
        return f"sources_json_bad_shape @{p.name}"
    # 走到这说明"文件好好的但 _load_four_set 却读不出条目"——同一套路径和
    # 解析器不该自相矛盾，真出现就是代码有问题，别再套上"没文件"的话术。
    return f"sources_unexplained_empty @{p.name}"


def plan_date_of(plan_id: str) -> str:
    """从 plan_id 尾部取逻辑日期（`housing_daily_2026-10-03` → `2026-10-03`）。

    plan_id 形如 `{job_id}_{YYYY-MM-DD}`；取不到日期返回 ""（调用方跳过这一档）。
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})$", plan_id or "")
    return m.group(1) if m else ""


def _safe_json(root: Path, p: Path):
    """读 JSON。文件不存在 → []；**内容坏了也 → []** 并记 WARN。

    坏 JSON 曾让 handler 抛 JSONDecodeError，被 run_rules 兜成
    `handler_error:JSONDecodeError:...` —— 失败信息指向解析器而不是
    "哪个文件坏了"，排查时会先怀疑代码。坏文件是模型的产物问题，
    应该按"没有可用数据"处理，并把坏文件路径写进日志让人一眼看到。
    """
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        jlog(root, "WARN", "four_set_invalid_json", path=str(p),
             error=str(e)[:200])
        return []


def _normalize_sources(raw) -> list[dict]:
    """把模型常写的两种变形收敛到标准形状。

    容忍的（结构明确、无歧义）：
    - 顶层包一层 {"sources": [...]} → 取出数组
    - 条目用 "id" 而不是 "source_id" → 改名（同一含义）
    不容忍的（需要判断，不能代做）：
    - 缺 tier_reason → 留空，让 source_quality 按"无理由"排除。
      代写理由等于伪造分级依据。
    - published_at 只有年份 → 留原样，让 freshness 按"无日期"计。
      脑补月份等于伪造时效。
    """
    if isinstance(raw, dict):
        for key in ("sources", "results", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        s = dict(s)
        if "source_id" not in s and "id" in s:
            s["source_id"] = s["id"]
        out.append(s)
    return out


def _normalize_claims(raw) -> list[dict]:
    if isinstance(raw, dict):
        for key in ("claims", "results", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        c = dict(c)
        if "claim_id" not in c and "id" in c:
            c["claim_id"] = c["id"]
        out.append(c)
    return out


def h_source_traceable(ctx: dict, rule: dict) -> dict:
    """幻觉拒收：每条claim的source_ids非空且都存在于sources.json，URL合法。"""
    task_id = ctx["task_id"]
    sources, claims, _ = _load_four_set(ctx, task_id)
    if not claims:
        return {"ok": False, "detail": "no_claims_json"}
    if not sources:
        # claims 在、sources 却读不出来 → 十有八九是 sources.json 写坏了。
        # 不拦这一下会掉进下面的 validate_claims：每条 claim 的 source_id
        # 都"找不到来源"，报成 hallucination —— 把人引去查"claim 挂错来源"，
        # 而真正要改的是那个解析不了的文件。
        return {"ok": False, "detail": sources_issue_detail(ctx, task_id)}
    bad = search_pipeline.validate_claims(claims, sources)
    bad_urls = [s.get("url", "") for s in sources
                if not str(s.get("url", "")).startswith(("http://", "https://"))]
    if bad or bad_urls:
        return {"ok": False, "detail": f"hallucination:{bad[:5]}"
                                       f"{';bad_url:' + str(bad_urls[:3]) if bad_urls else ''}",
                "candidates": ["INCONSISTENT"] if bad else []}
    return {"ok": True, "detail": f"claims={len(claims)} sources={len(sources)}"}


def h_freshness(ctx: dict, rule: dict) -> dict:
    """超期比例>30% → FAIL。published_at缺失不计入A类统计，标[无时间]P2。"""
    task_id = ctx["task_id"]
    sources, _, _ = _load_four_set(ctx, task_id)
    if not sources:
        return {"ok": False, "detail": sources_issue_detail(ctx, task_id)}
    days = int(rule.get("freshness_days")
               or FRESHNESS_DAYS.get(ctx.get("freshness", "none"), 730))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    stale, no_date, dated = 0, 0, 0
    for s in sources:
        dt = search_pipeline.parse_date(s.get("published_at"))
        if dt is None:
            no_date += 1
        else:
            dated += 1
            if dt < cutoff:
                stale += 1
    if dated == 0:
        return {"ok": False, "detail": f"no_dated_sources:{no_date}无时间"}
    ratio = stale / dated
    if no_date:
        jlog(ctx["root"], "WARN", "source_no_published_at", plan_id=ctx["plan_id"],
             task_id=task_id, count=no_date)
        ctx.get("notifier") and ctx["notifier"].p2(
            "source_no_published_at", plan_id=ctx["plan_id"], task_id=task_id,
            body=f"{no_date}条来源无published_at")
    return {"ok": ratio <= FRESHNESS_MAX_STALE_RATIO,
            "detail": f"stale={stale}/{dated} ratio={ratio:.2f} "
                      f"limit={FRESHNESS_MAX_STALE_RATIO} no_date={no_date}"}


def h_source_quality(ctx: dict, rule: dict) -> dict:
    """A类占比阈值（默认 30%，yaml 可覆盖）。tier_reason 为空的条目不参与统计（保证分级有据）。"""
    task_id = ctx["task_id"]
    sources, _, _ = _load_four_set(ctx, task_id)
    if not sources:
        return {"ok": False, "detail": sources_issue_detail(ctx, task_id)}
    min_ratio = float((rule.get("source_tier_min_ratio") or {}).get("A", A_TIER_MIN_RATIO))
    counted = [s for s in sources if str(s.get("tier_reason", "")).strip()]
    if not counted:
        return {"ok": False, "detail": "no_tier_reason_entries"}
    a = sum(1 for s in counted if s.get("source_tier") == "A"
            or s.get("tier") == "A")
    ratio = a / len(counted)
    # 用 ≥ 而非严格 >：bar=0.20 时 20% 应判 PASS。严格大于会在"刚好达标"边界
    # 误杀整批任务（实测 1A/5C=0.20 仍然 FAIL，而实际生产里来源比例是离散整数，
    # 边界样本很常见）。
    return {"ok": ratio >= min_ratio,
            "detail": f"A={a}/{len(counted)} ratio={ratio:.2f} "
                      f"bar={min_ratio} (excluded_no_reason="
                      f"{len(sources) - len(counted)})"}


def _question_keywords(q: str) -> list[str]:
    """从 key_question 提实质关键词。

    原来要求"问题原文出现在报告里"或"标题以问题前12字开头"——
    但 key_questions 是完整疑问句（77字、结尾是"是什么？"），
    而自然的报告标题是陈述短语。两者**天然对不上**，除非模型逐字抄题。
    按原文匹配等于在奖励"复制粘贴"，而不是"覆盖了问题"。
    """
    import re
    s = str(q or "")
    # 去掉时间状语前缀（截至...，/截至该时点，）
    s = re.sub(r"^截至[^，,]*[，,]", "", s)
    # 去掉疑问尾巴
    s = re.sub(r"(是什么|是否一致|如何|吗|呢|什么|为什么|有哪些)[？?。]*$", "", s)
    # 切出中英文词与数字词
    toks = re.findall(r"[A-Za-z]+|[0-9]+(?:\.[0-9]+)?%?|[\u4e00-\u9fff]{2,}", s)
    stop = {"中国", "的", "等", "及", "与", "和", "在", "对", "将", "已", "有",
            "相关", "具体", "内容", "表现", "如何", "是否", "一致", "上述",
            "该", "时点", "市场", "数据", "政策", "可能", "国家", "方面"}
    out = []
    for t in toks:
        t = t.strip("，,。、；;：:（）()")
        # 去掉黏着的虚词（"…的具体内容" → "具体内容"，再被 stop 滤掉）
        t = re.sub(r"^(的|之|等|与|和|在|对|及)+", "", t)
        t = re.sub(r"(的|等|与|和|情况|问题)+$", "", t)
        if len(t) >= 2 and t not in stop and t not in out:
            out.append(t)
    return out


def h_coverage(ctx: dict, rule: dict) -> dict:
    """key_questions逐条检查report.md有无对应非空section。"""
    task_id = ctx["task_id"]
    questions = (rule.get("key_questions")
                 or (ctx.get("shard") or {}).get("key_questions") or [])
    if not questions:
        return {"ok": False, "detail": "no_key_questions"}
    _, _, report = _load_four_set(ctx, task_id)
    if not report:
        return {"ok": False, "detail": "no_report_md"}
    missing = []
    for q in questions:
        head = str(q).strip()
        if not head:
            continue
        # 1) 原文出现即算（模型逐字抄题的情况）
        if head in report:
            continue
        # 2) 短问题（<=6字）只认原文或标题前缀：它们本身就是标题级别的短语，
        # 关键词重叠对它们太粗——"要点2"和"要点1"共享"要点"，
        # 按重叠算会误判为已覆盖。
        if len(head) <= 6:
            if not re.search(r"^#{1,6}\s*.*" + re.escape(head),
                             report, re.M):
                missing.append(head)
            continue
        # 3) 长问题按实质关键词重叠度（见 _question_keywords 的说明）
        if re.search(r"^#{1,6}\s*.*" + re.escape(head[:12]), report, re.M):
            continue
        keys = _question_keywords(head)
        if not keys:
            continue
        hit = sum(1 for k in keys if k in report)
        if hit / len(keys) < 0.5:
            missing.append(head)
    return {"ok": not missing, "detail": f"unanswered={missing[:5]}",
            "missing": missing}


# 四件套校验顺序（§2.2c）：格式→链接→时效→等级→完整度
RESEARCH_ORDER = ("json_schema", "source_traceable", "freshness",
                  "source_quality", "coverage")

HANDLERS = {
    "file_exists": h_file_exists,
    "row_count": h_row_count,
    "python_test": h_python_test,
    "json_schema": h_json_schema,
    "business_rule": h_business_rule,
    "mermaid": h_mermaid,
    "source_traceable": h_source_traceable,
    "freshness": h_freshness,
    "source_quality": h_source_quality,
    "coverage": h_coverage,
}

# kind → 产物文件名后缀（调研四件套类规则默认按 task_id 命名）
SUFFIX_BY_KIND = {"json_schema": None, "source_traceable": "sources",
                  "freshness": "sources", "source_quality": "sources",
                  "coverage": "report"}


# 整轮验收的墙钟预算。单条rule各有 timeout_s，但**没有整轮上限**——
# 一个任务声明10条会挂的 python_test，就是 10×timeout 的占用，
# 而 verify worker 是被占住的，后面的任务全在排队。
# 预算耗尽后剩下的rule不跑、也不算失败，只标 not_evaluated 等下一轮。
VERIFY_BUDGET_S = 300
# 单条rule的墙钟上限（秒）。规则的 timeout_s 由任务自己配，
# 不封顶的话一个写错的任务就能让验收worker占住一小时。
RULE_TIMEOUT_CAP_S = 120


def run_rules(ctx: dict, rules: list[dict], skip: set[str] | None = None,
              budget_s: float = VERIFY_BUDGET_S) -> list[dict]:
    """跑全部rule（不短路）。skip里的rule_id视为已通过（局部重入）。

    整轮受 budget_s 约束：超预算后剩余rule标 not_evaluated。
    **not_evaluated 既不算失败也不进 verified_rules**——
    算失败会把任务无端打成 RETRY（下一轮重跑同样的慢rule，死循环）；
    进账本则等于"验过了"，那是在撒谎。两条路都不能走。
    """
    skip = skip or set()
    out = []
    t_start = time.monotonic()
    for orig_index, rule in _ordered(rules):
        # rule_id用**原始序号**编号：重排不能改变rule_id，
        # 否则verified_rules账本里的id下次就对不上了
        rid = rule_id_of(rule, orig_index)
        if rid in skip:
            out.append({"rule_id": rid, "type": rule.get("type"), "ok": True,
                        "skipped": True, "detail": "already_verified"})
            continue
        elapsed = time.monotonic() - t_start
        if elapsed >= budget_s:
            out.append({"rule_id": rid, "type": rule.get("type"), "ok": False,
                        "not_evaluated": True,
                        "detail": f"verify_budget_exceeded({elapsed:.0f}s"
                                  f">={budget_s:.0f}s)",
                        "duration_ms": 0})
            continue
        fn = HANDLERS.get(str(rule.get("type")))
        t0 = time.monotonic()
        if fn is None:
            res = {"ok": False, "detail": f"unknown_rule_type:{rule.get('type')}"}
        else:
            rctx = dict(ctx)
            # 单rule墙钟封顶，防止任务把 timeout_s 配成很大
            rule_cap = min(float(rule.get("timeout_s") or RULE_TIMEOUT_CAP_S),
                           RULE_TIMEOUT_CAP_S)
            rctx["_rule_timeout_s"] = rule_cap
            try:
                res = fn(rctx, rule)
            except Exception as e:      # noqa: BLE001 单条rule异常不能带崩整轮
                res = {"ok": False, "detail": f"handler_error:{type(e).__name__}:{e}"[:300]}
        res.update({"rule_id": rid, "type": rule.get("type"),
                    "duration_ms": round((time.monotonic() - t0) * 1000, 2)})
        out.append(res)
    return out


def _ordered(rules: list[dict]) -> list[tuple[int, dict]]:
    """调研四件套按固定顺序（格式→链接→时效→等级→完整度），其余保持原序。
    返回(原始序号, rule)，供rule_id稳定编号。"""
    idx = {k: i for i, k in enumerate(RESEARCH_ORDER)}

    def key(pair):
        orig, rule = pair
        t = str(rule.get("type"))
        return (0, idx[t], orig) if t in idx else (1, 0, orig)

    return sorted(list(enumerate(rules)), key=key)


def make_ctx(store, plan_id: str, task_id: str, date: str, *,
             notifier=None) -> dict:
    task = store.get_task(plan_id, task_id) or {}
    shard = task.get("shard") or {}
    return {
        "root": Path(store.root), "repo": Path(store.root).parent,
        "plan_id": plan_id, "task_id": task_id, "date": date,
        "freshness": shard.get("freshness", "none"),
        "shard": shard, "store": store, "notifier": notifier,
        "variables": {},
    }
