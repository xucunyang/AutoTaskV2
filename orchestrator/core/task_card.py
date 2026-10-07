"""任务卡渲染（Phase0§5 + DoD第4项）。

只给白名单字段；禁给master全文、其他任务分片、全量events、大文件内容。
needs_web=true时追加"仅基于检索资料"注记（禁参数内知识）。
渲染后token估算必须<2k（DoD门禁），超出说明喂多了，白名单失效。
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from core.search.pipeline import FRESHNESS_DAYS
from schemas.models import Checkpoint

# Phase0§5白名单：只有这些能进任务卡
CARD_WHITELIST = ("task_id", "plan_id", "run_id", "objective",
                  "inputs_manifest", "acceptance", "budget", "outputs",
                  "idempotency_key", "checkpoint_cursor", "needs_web",
                  "freshness", "key_questions")
# 显式禁止出现在任务卡里的字段（防误扩张）
CARD_FORBIDDEN = ("master", "events", "shard", "all_tasks", "plan_tasks",
                  "depends_on", "handoff", "attempts", "fencing_token")


def _env(root: Path) -> Environment:
    return Environment(loader=FileSystemLoader(str(root / "templates")),
                       undefined=StrictUndefined, autoescape=False,
                       keep_trailing_newline=False)


def build_manifest(artifact) -> dict:
    """只保留路径+哈希+preview前5行（Phase0§5：禁止索要全量）。

    artifact 可以是 dict（已有 manifest 字段）或 str（路径，自行计算）。
    housing_daily 这类汇总任务，模板里 inputs 写的是路径字符串；
    真正的 manifest 字段只有产物落盘时才填（Executor 写 manifest 阶段）。
    两路都支持才不会在 build_card 阶段就炸。
    """
    if isinstance(artifact, str):
        p = Path(artifact)
        if not p.exists():
            return {"path": artifact, "sha256": None, "bytes": 0,
                    "rows": None, "preview": []}
        from core.utils import sha256_file
        info = sha256_file(p)
        return {"path": artifact, "sha256": info.get("sha256"),
                "bytes": info.get("bytes"), "rows": info.get("rows"),
                "preview": (info.get("preview") or [])[:5]}
    return {"path": artifact.get("path"), "sha256": artifact.get("sha256"),
            "bytes": artifact.get("bytes"), "rows": artifact.get("rows"),
            "preview": (artifact.get("preview") or [])[:5]}


def _rule_value(acceptance, rule_type: str, key: str):
    """从内嵌验收里取某条规则的参数（取不到返回None）。

    卡片必须用**模型能执行的单位**复述验收真正在查的数字。验收写的是
    `freshness_days: 365`，卡片原来只渲染 `时效性要求=recent`——模型既没有
    时钟、也不知道今天几号，无从换算成"到底哪天之前的来源算超期"。
    实测 2026-10-04：t3_hexagon 交了 2025-10-01（368天，差3天）和 2020 年的
    来源，stale=8/12=0.67 卡在 limit=0.3，连挂三轮。
    """
    for rule in (acceptance or []):
        if isinstance(rule, dict) and rule.get("type") == rule_type:
            return rule.get(key)
    return None


def _rule_id(acceptance, rule_type: str) -> str:
    """某类规则在验收里的实际 rule_id（`rule_3`），取不到返回空串。

    卡片原来把时效写死成"硬验收 rule_3"、分级写死成"rule_4"——那两个编号
    只是**恰好**等于 acceptance_market.yaml 里的下标。换一份验收（规则条数
    不同）或调整规则顺序，卡上就会指向另一条规则，模型照着编号去核对，
    核对的却是不相干的那条。编号必须跟着规则走，不能写死。
    """
    for i, rule in enumerate(acceptance or []):
        if isinstance(rule, dict) and rule.get("type") == rule_type:
            return rule.get("rule_id") or f"rule_{i}"
    return ""


def _schema_hint(root: Path, acceptance) -> str:
    """把 json_schema 规则的 schema_ref **内联成形状**渲染进卡，取不到就返回空串。

    实测 2026-10-05：acceptance_t3 只写 `schema_ref: schemas/summary.schema.json`，
    卡上于是只有这一行**路径**，objective 又只说"date + rows 必填"——模型自己把
    rows 理解成城市明细数组（schema 实际要 integer 行数），连挂两轮进死信。
    路径不是形状：模型不会为了核对一个类型专门去读文件，类型得摆在它眼前。

    三条护栏：
    - 读不到文件/不是合法 JSON → 返回空串，卡上仍保留原路径，绝不把卡渲染炸掉；
    - hint 超过 800 字就放弃内联（大 schema 会把卡片顶过 2k token 门禁）；
    - 只展开 required 字段（连同 schema 里的 description），optional 不进卡。
    """
    for i, rule in enumerate(acceptance or []):
        if not isinstance(rule, dict) or rule.get("type") != "json_schema":
            continue
        ref = str(rule.get("schema_ref") or "")
        if not ref:
            continue
        try:
            data = json.loads((root / ref).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):        # schema 文件不是对象 → 不猜，保持原样
            continue
        props = data.get("properties") or {}
        required = data.get("required") or list(props)
        fields = []                            # (字段名, 类型, description)
        for k in required:
            spec = props.get(k)
            if isinstance(spec, dict) and spec.get("type"):
                desc = str(spec.get("description") or "").strip()[:48]
                fields.append((k, str(spec["type"]), desc))
        if not fields:
            continue
        # 形状示例：required 按类型占位，一眼看出 rows 该写标量还是数组
        sample = "{" + ", ".join(
            f'"{k}": "<{t}>"' if t == "string" else f'"{k}": <{t}>'
            for k, t, _d in fields) + "}"
        # description 是"这个字段到底放什么"的唯一说明，必须跟着进卡——
        # 只给类型的话，模型知道 rows 要整数，却不知道该填什么整数。
        kinds = "；".join(f"{k}={t}" + (f" {d}" if d else "")
                          for k, t, d in fields)
        hint = (f"{ref}（{rule.get('rule_id') or f'rule_{i}'}）必需字段"
                f"（缺一个或类型不对=整条 schema_violation）：\n"
                f"  {sample}\n  {kinds}")
        scalars = [k for k, t, _d in fields
                   if t in ("integer", "number", "boolean")]
        if scalars:
            hint += ("\n  " + "、".join(scalars) +
                     " 写成对应标量（整数/小数/布尔），不是数组或对象。")
        # 字符串值里最常见也最致命的写法错误：裸的英文双引号。
        # 2026-10-06 实测三次——hardware_summary.json 的
        # `列示为"未查到具体"，只能按缺数据处理`、policy 的
        # `《城市更新"十五五"规划》`：引号一没转义，整份文件 json.loads
        # 直接炸（bad_json），schema_violation 根本轮不到跑，而报错
        # 长得像"字段写错了"。中文引号「」是正解（跑通的那版
        # key_findings 用的就是全角引号）。
        # 刻意写得很短：任务卡离 2k token 门禁只剩 250 余量。
        hint += ('\n  字符串值内部不要有裸的英文双引号 "，引用改用中文「」，'
                 '否则 bad_json。')
        if len(hint) > 800:
            return ""
        return hint
    return ""


def _out_dir(outputs) -> str:
    """四件套的落盘目录 = 声明产物所在目录（`artifacts/2026-10-03`）。

    没声明产物时退化成 `artifacts/<日期>` 这样的文字提示——总比留空让模型
    自己猜好，它猜的结果实测就是写到 `artifacts/` 顶层。
    """
    outs = [str(o).replace("\\", "/") for o in (outputs or [])]
    if outs and "/" in outs[0]:
        return outs[0].rsplit("/", 1)[0]
    return "artifacts/<日期>"


# 四件套的精确形状说明，只在 needs_web 的任务卡里出现。
# 它必须和 verifier._load_four_set 实际读的形状一致——这是同一份契约的
# 两面（"卡上承诺的" vs "验收检查的"）。改了一边必须改另一边，
# 否则会出现"按卡写了却过不了验收"，而模型完全不知道为什么。
#
# **必须带完整目录**：原来只写 `{task_id}.sources.json`，没说写到哪，
# 模型就按 tool_spec_prompt 的示例落到了 `artifacts/` 顶层——实测 2026-10-03
# 重跑时三个任务全把 sources 写进顶层、claims 一个都没写，验收齐刷刷
# no_sources_json/no_claims_json。验收只在 `artifacts/{date}/` 下找
# （verifier 的 normalize_path 也拒绝其它位置），所以路径必须逐字给全。
FOUR_SET_SCHEMA = """\
四件套**必须**写到与 outputs 相同的日期目录（`{out_dir}/`），
不要写到 `artifacts/` 顶层（验收只在日期目录下找）。

`{out_dir}/{task_id}.sources.json` 必须是**顶层数组**（不要包在 {"sources":...} 里），
每个元素：
{"source_id": "s1", "url": "https://...", "title": "...",
 "snippet": "摘要（search 原文，不要编）",
 "published_at": "2026-09-15T00:00:00+00:00（完整日期；只写年份会被判无日期）",
 "source_tier": "A", "tier": "A",
 "tier_reason": "为什么是A（一句话，不写会被排除出A类统计）"}
字段名必须用 source_id（不是 id）。
A = 官方机构/政府公告/持牌金融机构；B = 主流媒体；C = 个人号/论坛。

`{out_dir}/{task_id}.claims.json` 必须是**顶层数组**，每个元素：
{"claim_id": "c1", "text": "一句话结论", "source_ids": ["s1", "s2"]}
每条 claim 的 source_ids 必须非空且指向 sources.json 里存在的 id。
没有来源支撑的结论不要写进 claims（判幻觉）。
这两个文件**缺一个就判失败**（no_sources_json / no_claims_json）。

写 .json 时字符串值内部不要有裸的英文双引号 "，要引用改用中文「」（否则 bad_json）。
"""

# 只给产物是 .md 的任务看（coverage 规则查的就是它）。csv/json 任务贴这句
# 是纯噪音——卡片每行都是 token，t1_collect 的卡曾因此顶到 2275 > 2k 门禁。
REPORT_MD_NOTE = """
`{task_id}.report.md` 是给人看的正文，按 key_questions 分节，
每节标题必须包含对应问题的关键词（coverage 验收按标题匹配）。
"""


def render_task_card(root: str | Path, *, task_id: str, plan_id: str, run_id: str,
                     objective: str, idempotency_key: str, outputs: list,
                     acceptance: list, budget: dict, inputs: list | None = None,
                     checkpoint: dict | None = None, needs_web: bool = False,
                     freshness: str = "none", key_questions: list | None = None,
                     cmd_allowlist: str = "python / pytest / ruff / node",
                     extra: dict | None = None) -> str:
    """渲染任务卡。inputs只给manifest（不读文件全文）。

    extra是给后续阶段预留的扩展位；任何落在CARD_FORBIDDEN里的key直接拒，
    防止有人顺手把master/events/兄弟分片塞进上下文（Phase0§5明令禁止）。"""
    root = Path(root)
    shard_like = {"task_id": task_id, "plan_id": plan_id, "run_id": run_id,
                  "objective": objective, "idempotency_key": idempotency_key,
                  "outputs": outputs, "budget": budget}
    for forbidden in CARD_FORBIDDEN:
        if forbidden in shard_like:      # 防御：调用方误传敏感字段直接炸
            raise ValueError(f"forbidden_field_in_card:{forbidden}")
    for key in (extra or {}):
        if key in CARD_FORBIDDEN:
            raise ValueError(f"forbidden_field_in_card:{key}")
    manifest = [build_manifest(a) for a in (inputs or [])]
    ctx = {
        "task_id": task_id, "plan_id": plan_id, "run_id": run_id,
        "objective": objective,
        "inputs_manifest": yaml.safe_dump(manifest, allow_unicode=True,
                                          sort_keys=False).strip()
                          if manifest else "（无输入产物）",
        "acceptance_yaml": yaml.safe_dump(acceptance, allow_unicode=True,
                                          sort_keys=False).strip(),
        # json_schema 的形状内联进卡（schema_ref 只是路径，模型不会照着路径去读）
        "schema_hint": _schema_hint(root, acceptance),
        "idempotency_key": idempotency_key,
        "budget": budget,
        "outputs": "、".join(outputs) if outputs else "（未声明）",
        "needs_web": needs_web, "freshness": freshness,
        "key_questions": key_questions or [],
        "cmd_allowlist": cmd_allowlist,
        # out_dir：四件套的落盘目录 = 声明产物所在目录。模型要的是**逐字路径**，
        # 不是"推断一下"——推断错就写到 artifacts/ 顶层，验收直接 no_claims_json。
        # 同时它也是任务卡里"可写路径"的正面清单（原来写的是"只能写 outputs"，
        # 和四件套要求直接打架，实测把 t3 三次全打死）。
        "out_dir": _out_dir(outputs),
        "four_set_schema": (FOUR_SET_SCHEMA
                            + (REPORT_MD_NOTE
                               if any(str(o).endswith((".md", ".txt"))
                                      for o in (outputs or [])) else "")
                            ).replace("{task_id}", task_id).replace(
                                "{out_dir}", _out_dir(outputs)),
    }
    # 图表验收：卡上必须说清"要几张图、画到哪个文件"，否则模型根本不知道
    # 这轮还要交图（正文它一定会写，图不会）。只在验收真有 mermaid 规则时
    # 才渲染——没有图表要求的任务不该被塞一段无关指令。
    mermaid_rules = [r for r in (acceptance or [])
                     if isinstance(r, dict) and r.get("type") == "mermaid"]
    ctx["mermaid_required"] = bool(mermaid_rules)
    ctx["mermaid_min"] = int(mermaid_rules[0].get("min") or 1) if mermaid_rules else 0
    ctx["mermaid_target"] = (str(mermaid_rules[0].get("path")
                                 or (outputs[0] if outputs else ""))
                             if mermaid_rules else "")
    if needs_web:
        # 把验收的两个硬数字翻成模型**能执行**的单位：
        #   freshness → 具体截止日期（模型没有时钟，"recent" 换算不出日期）
        #   A类门槛 → 百分比（tier 判级规则在 search.yaml，这里只给阈值）
        days = _rule_value(acceptance, "freshness", "freshness_days")
        days = int(days or FRESHNESS_DAYS.get(freshness, 730))
        today = date.today()
        ctx["freshness_days"] = days
        ctx["freshness_today"] = today.isoformat()
        ctx["freshness_cutoff"] = (today - timedelta(days=days)).isoformat()
        ratio = _rule_value(acceptance, "source_quality",
                            "source_tier_min_ratio")
        if isinstance(ratio, dict):          # 验收写的是 {A: 0.2}
            ratio = ratio.get("A")
        ctx["has_tier_bar"] = ratio is not None
        ctx["tier_bar"] = f"{float(ratio):.0%}" if ratio is not None else ""
        # 规则编号跟着验收走（原来写死 rule_3/rule_4，换份验收就指错规则）
        ctx["freshness_rule_id"] = _rule_id(acceptance, "freshness")
        ctx["tier_rule_id"] = _rule_id(acceptance, "source_quality")
    if needs_web and freshness == "none":
        # 与TaskShard._needs_web_requires_freshness同一条约束，只是提前到渲染口
        raise ValueError("needs_web=true要求freshness为recent/strict")
    if checkpoint:
        ck = Checkpoint(**{k: v for k, v in checkpoint.items()
                           if k in Checkpoint.model_fields})
        ctx["checkpoint_cursor"] = ck.cursor
        ctx["checkpoint_done_steps"] = ck.done_steps
    else:
        ctx["checkpoint_cursor"] = ""
        ctx["checkpoint_done_steps"] = []
    ctx.update(extra or {})
    card = _env(root).get_template("task_card.j2").render(**ctx)
    if needs_web:
        card += ("\n【检索约束】本任务needs_web=true：仅基于检索资料回答，"
                 "资料未覆盖的部分标注[未覆盖]，禁止使用模型参数内知识。"
                 f"时效性要求={freshness}（{ctx['freshness_days']}天内，"
                 f"published_at ≥ {ctx['freshness_cutoff']}）。")
    return card


def estimate_tokens(text: str) -> int:
    """粗估token：CJK按1字1token，ASCII按4字符1token（保守上界）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    other = len(text) - cjk
    return int(cjk + math.ceil(other / 4))
