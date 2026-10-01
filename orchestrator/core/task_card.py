"""任务卡渲染（Phase0§5 + DoD第4项）。

只给白名单字段；禁给master全文、其他任务分片、全量events、大文件内容。
needs_web=true时追加"仅基于检索资料"注记（禁参数内知识）。
渲染后token估算必须<2k（DoD门禁），超出说明喂多了，白名单失效。
"""
from __future__ import annotations

import math
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

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


def build_manifest(artifact: dict) -> dict:
    """只保留路径+哈希+preview前5行（Phase0§5：禁止索要全量）。"""
    return {"path": artifact.get("path"), "sha256": artifact.get("sha256"),
            "bytes": artifact.get("bytes"), "rows": artifact.get("rows"),
            "preview": (artifact.get("preview") or [])[:5]}


# 四件套的精确形状说明，只在 needs_web 的任务卡里出现。
# 它必须和 verifier._load_four_set 实际读的形状一致——这是同一份契约的
# 两面（"卡上承诺的" vs "验收检查的"）。改了一边必须改另一边，
# 否则会出现"按卡写了却过不了验收"，而模型完全不知道为什么。
FOUR_SET_SCHEMA = """\
`{task_id}.sources.json` 必须是**顶层数组**（不要包在 {"sources":...} 里），
每个元素：
{"source_id": "s1", "url": "https://...", "title": "...",
 "snippet": "摘要（search 返回的原文，不要自己编）",
 "published_at": "2026-09-15T00:00:00+00:00（完整日期；只写年份会被判无日期）",
 "source_tier": "A", "tier": "A",
 "tier_reason": "为什么是A（一句话，不写会被排除出A类统计）"}
字段名必须用 source_id（不是 id），tier_reason 不能为空。
A = 官方机构/政府公告/持牌金融机构；B = 主流媒体；C = 个人号/论坛。

`{task_id}.claims.json` 必须是**顶层数组**，每个元素：
{"claim_id": "c1", "text": "一句话结论", "source_ids": ["s1", "s2"]}
每条 claim 的 source_ids 必须非空且指向 sources.json 里存在的 source_id。
没有来源支撑的结论不要写进 claims（写了会被判幻觉）。

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
        "idempotency_key": idempotency_key,
        "budget": budget,
        "outputs": "、".join(outputs) if outputs else "（未声明）",
        "needs_web": needs_web, "freshness": freshness,
        "key_questions": key_questions or [],
        "cmd_allowlist": cmd_allowlist,
        "four_set_schema": FOUR_SET_SCHEMA.replace("{task_id}", task_id),
    }
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
                 f"时效性要求={freshness}。")
    return card


def estimate_tokens(text: str) -> int:
    """粗估token：CJK按1字1token，ASCII按4字符1token（保守上界）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f")
    other = len(text) - cjk
    return int(cjk + math.ceil(other / 4))
