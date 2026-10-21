"""任务配置链路试跑（dry-run）：每个任务模板必须带黄金样本过真链路。

为什么需要它（2026-10-21 的教训）：
单测验证的是引擎（store/编排/验收器），冒烟只跑 daily_report 一个计划。
但生产里翻车的从来不是引擎，是**货物**——某个任务的"模板指令 + 验收规则
+ JSON schema"三件套互相矛盾（10-21 housing_rr：模板教模型'查不到写数据
缺失'，schema 却要求 rr_pct>=0.1，模型照模板填 0 → 整卡打回重跑）。
三件套是配置即代码，配置必须像代码一样被链路级测试覆盖。

守卫规则：
- templates/*.yaml 里每发现一个模板，必须出现在 GOLDEN（有黄金样本，
  跑真链路到 DONE）或 DEBT（显式债务登记）之一；
- 新模板两处都不在 → 第一个测试直接红："配置未经链路验证不许上线"。

黄金样本 = 该任务产物"最抠门但完全合规"的形状：stub 模型按它写产物，
真验收 + 真 schema 全过 → 任务到 DONE。任何一环断裂（模板产物路径写错、
schema 收紧、验收规则改了没人同步、四件套缺胳膊少腿）这里先红，
生产不再当测试场。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import executor, scheduler
from core.orchestrator import Orchestrator
from core.store import Store
from core.verify_orchestrator import VerifyPool
from tests.test_housing_cadence import _rr
from tests.test_orchestrator import _root

ROOT_TPL = Path(__file__).resolve().parents[1]
DATE = "2026-10-21"

# ---------- 黄金样本：产物名 → 内容（chat 时由 stub 写入日期目录） ----------


def _md_with_mermaid(title: str) -> str:
    chart = ("```mermaid\nxychart-beta\n    x-axis [\"a\", \"b\", \"c\"]\n"
             "    y-axis \"pct\" 0 --> 10\n    bar [4, 3, 2]\n```\n")
    pad = "<!-- pad -->\n" * 80          # 凑 bytes 门槛（>=800）
    return f"# {title}\n\n口径：stub 黄金样本（挂牌口径，统计期写明）。\n\n{chart}\n{pad}"


def _fresh_sources() -> list[dict]:
    # freshness 用 now 而不是写死日期：写死会变成"一年后恒红"的定时炸弹。
    pub = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return [{
        "source_id": "s1",
        "url": "https://nanjing.ke.com/zufang/",
        "title": "贝壳租房·南京",
        "snippet": "stub 黄金样本检索片段：小区级挂牌租金与单价",
        "published_at": pub,
        "source_tier": "B", "tier": "B",
        "tier_reason": "平台挂牌数据（小区级租售比无 A 级官方源）",
    }]


def _golden_rr() -> dict[str, str]:
    """housing_rr_fiveday：三城×20条全字段正数 + 四件套 + mermaid 正文。"""
    data = _rr(as_of=DATE)
    claims = [{"claim_id": "c1", "text": "南京租售比 Top1 小区（黄金样本）",
               "source_ids": ["s1"]}]
    return {
        "t_rr_top20.md": _md_with_mermaid("三城租售比Top20（黄金样本）"),
        "t_rr_top20.data.json": json.dumps(data, ensure_ascii=False),
        "t_rr_top20.sources.json": json.dumps(_fresh_sources(),
                                              ensure_ascii=False),
        "t_rr_top20.claims.json": json.dumps(claims, ensure_ascii=False),
    }


# 模板文件名 → 黄金样本工厂。新增任务时在这里登记，测试会带它过真链路。
GOLDEN: dict[str, object] = {
    "housing_rr_fiveday.yaml": _golden_rr,
}

# 存量模板的债务登记（补齐黄金样本后从这里删除对应条目）。
DEBT: dict[str, str] = {
    "daily_plan.yaml": "已有 tests/test_smoke_e2e.py 四场景真链路覆盖（等效黄金样本）",
    "finance_daily.yaml": "债务：待补黄金样本（2026-10-21 登记）",
    "hardware_daily.yaml": "债务：待补黄金样本（2026-10-21 登记）",
    "housing_daily.yaml": "债务：待补黄金样本（2026-10-21 登记）",
    "housing_fortnight.yaml": "债务：待补黄金样本（2026-10-21 登记）",
    "housing_monthly.yaml": "债务：待补黄金样本（2026-10-21 登记）",
    "policy_daily.yaml": "债务：待补黄金样本（2026-10-21 登记）",
}


# ---------- 链路机器（与 test_smoke_e2e 同构：真 store/编排/验收，stub 模型） ----------


class _GoldenProvider:
    """stub 模型：把黄金样本逐字写到日期目录。产物形状必须合规——
    测出来的失败必须是被测系统的，不是 stub 交白卷的。"""

    name = "golden"

    def __init__(self, artifacts: dict[str, str], date_dir: Path):
        self._artifacts = artifacts
        self._date_dir = date_dir

    def chat(self, prompt, budget):
        self._date_dir.mkdir(parents=True, exist_ok=True)
        for name, content in self._artifacts.items():
            (self._date_dir / name).write_text(content, encoding="utf-8")
        return {"content": "# golden\n产物已按黄金样本写入。",
                "usage": {"prompt_tokens": 100, "completion_tokens": 40}}


def _stub_search(*args, **kwargs):
    """检索替身：预取返回 []（四件套由黄金样本自己落），工具层给一条够用的。"""
    if (args and isinstance(args[0], str)) or kwargs.get("max_results") is not None:
        return _fresh_sources()
    return []


def _run_chain(root: Path, s: Store, plan_id: str, artifacts: dict[str, str],
               max_ticks: int = 30) -> dict[str, str]:
    """跑编排+验收直到收敛，返回 {task_id: status}。"""
    date_dir = root / "artifacts" / DATE
    pool = VerifyPool(s, workers=2)
    o = Orchestrator(root, executor_fn=None, verify_pool=pool)

    def dispatch(s_, plan_id_, task_id, run_id, owner=None):
        executor.run_task(s_, plan_id_, task_id, run_id,
                          owner=owner or "golden",
                          provider=_GoldenProvider(artifacts, date_dir),
                          search_fn=_stub_search,
                          final_window=65536)

    o.executor_fn = dispatch

    async def drive():
        async with pool:
            for _ in range(max_ticks):
                o.tick()
                st = {t["status"] for t in s.list_plan_tasks(plan_id)}
                if st and st <= {"DONE", "SKIPPED", "SKIPPED_CACHED",
                                 "DEAD_LETTER", "CANCELLED"}:
                    break
                await anyio.sleep(0.05)

    anyio.run(drive)
    return {t["task_id"]: t["status"] for t in s.list_plan_tasks(plan_id)}


# ---------- 守卫 ----------


def test_every_plan_template_is_covered_or_registered() -> None:
    """templates/*.yaml 必须人人有归属：GOLDEN 或 DEBT，二选一。"""
    discovered = {p.name for p in (ROOT_TPL / "templates").glob("*.yaml")}
    assert discovered <= set(GOLDEN) | set(DEBT), (
        f"新任务模板 {sorted(discovered - set(GOLDEN) - set(DEBT))} "
        "既没有黄金样本也没有债务登记——配置未经链路验证不许上线。"
        "在 GOLDEN 登记黄金样本（推荐，一次性 20 行），或在 DEBT 写明限期。"
    )


@pytest.mark.parametrize("tpl", sorted(GOLDEN))
def test_template_dryrun_reaches_done(tpl: str, tmp_path: Path) -> None:
    """黄金样本必须真的过链路：模板展开 → 执行 → 验收 → 全员 DONE。

    红了先看失败任务是哪个状态：DEAD_LETTER 说明验收/schema/模板三件套
    有矛盾或产物路径对不上——这正是本测试要拦的"配置面数据流断裂"。
    """
    root = _root(tmp_path)
    s = Store(root)
    job_id = tpl.removesuffix(".yaml")
    created, _ = scheduler.ensure_plan(s, DATE, f"templates/{tpl}",
                                       job_id=job_id)
    assert created == "created"
    plan_id = f"{job_id}_{DATE}"

    artifacts = GOLDEN[tpl]()
    statuses = _run_chain(root, s, plan_id, artifacts)

    assert statuses, "计划里没有任何任务，模板可能解析失败"
    not_done = {tid: st for tid, st in statuses.items() if st != "DONE"}
    if not_done:
        recent = [(e.get("task_id"), e.get("to_s"))
                  for e in s.recent_events(plan_id, limit=10)]
        assert not not_done, (
            f"{tpl} 黄金样本没能走通链路: {not_done}——最近事件: {recent}。"
            "三件套（模板/验收/schema）与产物路径的矛盾在这里修，别等生产。"
        )
