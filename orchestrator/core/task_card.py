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
不要写到 `artifacts/` 顶层——验收只在日期目录下找文件，写错目录等于没写。

`{out_dir}/{task_id}.sources.json` 必须是**顶层数组**（不要包在 {"sources":...} 里），
每个元素：
{"source_id": "s1", "url": "https://...", "title": "...",
 "snippet": "摘要（search 返回的原文，不要自己编）",
 "published_at": "2026-09-15T00:00:00+00:00（完整日期；只写年份会被判无日期）",
 "source_tier": "A", "tier": "A",
 "tier_reason": "为什么是A（一句话，不写会被排除出A类统计）"}
字段名必须用 source_id（不是 id），tier_reason 不能为空。
A = 官方机构/政府公告/持牌金融机构；B = 主流媒体；C = 个人号/论坛。

`{out_dir}/{task_id}.claims.json` 必须是**顶层数组**，每个元素：
{"claim_id": "c1", "text": "一句话结论", "source_ids": ["s1", "s2"]}
每条 claim 的 source_ids 必须非空且指向 sources.json 里存在的 source_id。
没有来源支撑的结论不要写进 claims（写了会被判幻觉）。
这两个文件**缺一个就整条任务判失败**（no_sources_json / no_claims_json）。

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
        # out_dir：四件套的落盘目录 = 声明产物所在目录。模型要的是**逐字路径**，
        # 不是"推断一下"——推断错就写到 artifacts/ 顶层，验收直接 no_claims_json。
        "four_set_schema": FOUR_SET_SCHEMA.replace(
            "{task_id}", task_id).replace("{out_dir}", _out_dir(outputs)),
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
