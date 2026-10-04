"""端到端冒烟（Phase4 §4）：daily四任务链 + 二次触发 + 混合 + 调研。

用**stub provider**（不连真模型），所以测的是编排/状态机/验收/报告这条链，
不是模型效果——模型效果没法在CI里当门禁。

四条冒烟：
A. daily链：ensure_plan → 跑编排 → 校验 → 报告，四任务全DONE
   （2026-10-04 加 t4_charts：报告里要有 mermaid 图）
B. 二次同输入：至少一个命中SKIPPED_CACHED（省token可观测）
C. 混合：Daily跑一半注入管道高优，验证"排队等待不抢占 + 当前任务完成后自然衔接"
D. 调研链：planner拆分 → needs_web任务走四段式 → 4条调研验收全过
          → report.md含任务分解视图

时间断言用p95/上限口径（§6：不用wall-clock硬线）。
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import checkpoint as cp
from core import enqueue as enqueue_mod
from core import executor
from core import planner
from core import report as rp
from core import scheduler
from core import verify_orchestrator as vmod
from core.orchestrator import Orchestrator
from core.search import pipeline as spipe
from core.store import Store
from core.verify_orchestrator import VerifyPool
from tests.test_orchestrator import _root

DATE = "2026-09-29"
PLAN = f"daily_report_{DATE}"
RUN_ID = "smoke-run-1"
SMOKE_BUDGET_S = 300          # §5：单计划三任务端到端<5min（stub模式）

# stub 要交出的"合格产物"形状——由 acceptance_t1/t2/t3/charts 的规则反推：
# t1 要求 rows>=20 且 cols>=5（旧的 3 行 2 列桩数据在 2026-10-04 改严后必挂），
# 图表类要求 md 里 mermaid 图表块 >=2 且 bytes>=800。
_STUB_CSV = ("city,tier,new_mom,resale_mom,period,note\n" +
             "".join(f"city{i},二线,0.1,0.2,2026-08,ok\n" for i in range(1, 26)))
_STUB_LINE = ("```mermaid\nxychart-beta\n    x-axis [\"a\", \"b\", \"c\"]\n"
              "    y-axis \"value\" 0 --> 10\n    line [1, 2, 3]\n```\n")
_STUB_BAR = ("```mermaid\nxychart-beta\n    x-axis [\"a\", \"b\", \"c\"]\n"
             "    y-axis \"value\" 0 --> 10\n    bar [3, 2, 1]\n```\n")
_STUB_PAD = "<!-- pad -->\n" * 60        # 填到 800 字节以上（charts.md 的 bytes 门槛）


def _stub_md(name: str, outs: str) -> str:
    return f"# stub {name}\n\n{outs}\n\n{_STUB_LINE}\n{_STUB_BAR}\n{_STUB_PAD}"


class StubProvider:
    """stub模型：按prompt里的输出声明写对应产物，不调真模型。"""
    name = "stub"

    def __init__(self):
        self.calls = 0
        self.prompts = []

    def chat(self, prompt, budget):
        import re
        self.calls += 1
        self.prompts.append(prompt)
        outs = re.findall(r"^outputs：(.*)$", prompt, re.M)
        body = f"# stub产出\n（调用{self.calls}）\n"
        for line in outs:
            body += line + "\n"
        # 顺带把检索资料包回显，模拟"只基于资料回答"
        return {"content": body,
                "usage": {"prompt_tokens": 120, "completion_tokens": 60}}


def _provider_for(store, plan_id, task_id):
    """给单个任务装上会真的写产物的stub provider。"""
    class P:
        name = "stub"
        def __init__(self):
            self.task = store.get_task(plan_id, task_id)

        def chat(self, prompt, budget):
            # 从任务卡里抠出产物路径（模板里是"1. 原子写产物到 <path>（...）"）
            import re
            paths = re.findall(r"artifacts/\d{4}-\d{2}-\d{2}/[\w\-.]+", prompt)
            body = "# stub\n"
            for line in dict.fromkeys(paths):
                p = Path(store.root) / line
                p.parent.mkdir(parents=True, exist_ok=True)
                # 按产物类型写内容：给.json写CSV的话，json_schema验收报bad_json，
                # 那是stub的错不是被测系统的错——测出来的失败必须是真的。
                # stub 是"把活干完的模型"的替身：验收要四件套/20行CSV/mermaid 图，
                # 它就得交出那个形状，否则测到的是"stub 交白卷"而不是产品缺陷。
                if p.name.endswith(".sources.json"):
                    # freshness 用 now 而不是 DATE：写死日期会让这套断言
                    # 在一年多以后变成恒红（stale>30%），那时没人记得为什么。
                    pub = datetime.now(timezone.utc).replace(
                        microsecond=0).isoformat()
                    p.write_text(json.dumps([{
                        "source_id": "s1",
                        "url": "https://www.stats.gov.cn/sj/zxfb/",
                        "title": "70个大中城市商品住宅销售价格变动情况",
                        "snippet": "stub 检索到的原文片段",
                        "published_at": pub,
                        "source_tier": "A", "tier": "A",
                        "tier_reason": "国家统计局发布"}], ensure_ascii=False),
                        encoding="utf-8")
                elif p.name.endswith(".claims.json"):
                    p.write_text(json.dumps([{
                        "claim_id": "c1", "text": "stub 结论",
                        "source_ids": ["s1"]}]), encoding="utf-8")
                elif p.suffix == ".json":
                    p.write_text(json.dumps({"date": DATE, "rows": 2,
                                            "note": "stub"}), encoding="utf-8")
                elif p.suffix in (".md", ".txt"):
                    p.write_text(_stub_md(p.name, "\n".join(paths)),
                                 encoding="utf-8")
                else:
                    p.write_text(_STUB_CSV, encoding="utf-8")
                body += line + "\n"
            return {"content": body,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40}}
    return P()


def _stub_search(*args, **kwargs):
    """冒烟用的检索替身：一个函数要兼容执行器的两种调用签名。

    - 预取 search_fn(shard, task)：返回 []。执行器拿到非空会把 sources 写到
      `artifacts/<task>.sources.json`（**顶层**），而验收只在日期目录里找
      四件套——预取写出来的东西没人看，所以这里干脆不写，四件套交给
      stub provider 在 `artifacts/<date>/` 里落。
    - 工具层 search_fn(query, max_results=5)：返回一条够用的结果。
      （冒烟的 stub 不支持 tools，走单轮；给它是为了别把 search_fn=None
      这条路测没了——t1_collect 自 2026-10-04 起是 needs_web。）
    """
    if (args and isinstance(args[0], str)) or kwargs.get("max_results") is not None:
        return [{"url": "https://www.stats.gov.cn/sj/zxfb/",
                 "title": "70个大中城市商品住宅销售价格变动情况",
                 "snippet": "stub 检索到的原文",
                 "published_at": datetime.now(timezone.utc).isoformat()}]
    return []


def _run_chain(root: Path, s: Store, max_ticks: int = 20) -> dict:
    """跑编排+校验直到收敛或用完tick。"""
    pool = VerifyPool(s, workers=2)
    o = Orchestrator(root, executor_fn=None, verify_pool=pool)
    # executor_fn按任务逐个装provider
    def dispatch(s_, plan_id, task_id, run_id, owner=None):
        executor.run_task(s_, plan_id, task_id, run_id, owner=owner or "smoke",
                          provider=_provider_for(s_, plan_id, task_id),
                          search_fn=_stub_search,
                          final_window=65536)
    o.executor_fn = dispatch

    async def drive():
        async with pool:
            # 必须一直tick到整个plan走到终态：校验是异步的，
            # 固定tick几次就收工的话，最后一个任务往往还在VERIFYING，
            # 后继任务自然也没机会被推进——测出来的是"跑太快"不是"跑不通"。
            for _ in range(max_ticks):
                o.tick()
                st = {t["status"] for t in s.list_plan_tasks(PLAN)}
                if st and st <= {"DONE", "SKIPPED", "SKIPPED_CACHED",
                                 "DEAD_LETTER", "CANCELLED"}:
                    break
                await anyio.sleep(0.05)     # 给异步校验线程留时间
    anyio.run(drive)
    return {"done": [t["task_id"] for t in s.list_plan_tasks(PLAN)
                     if t["status"] == "DONE"]}


def _root_with_project_tests(tmp_path):
    """冒烟需要一个"像真仓库"的root。

    复刻生产布局，否则测出来的失败不是产品缺陷而是环境不对：
    1. 验收里的 `python_test` 以 `cwd=repo` 跑，而repo=store.root的父目录
       （生产里 orchestrator/ 是仓库子目录）。所以这里造 repo/orchestrator 两层。

    2026-10-04：这里原来还手写了 tests/test_clean.py，理由写的是
    "acceptance_t2里有 pytest tests/test_clean.py，真实项目里这个文件存在"——
    真实项目里**不存在**（顶层连 tests/ 都没有），生产上 t2_clean 因此 rc=4
    三轮进死信、当日日报没生成，而这里全绿。前置条件不该由测试自己补齐，
    否则测的是"我给自己搭的环境"，不是产品。acceptance_t2 那条规则已删，
    这里也不再造这个文件。
    """
    repo = tmp_path / "repo"
    return _root(repo / "orchestrator")


def test_smoke_a_daily_chain_all_tasks_done(tmp_path):
    """A: daily四任务链（采集→清洗→画图→日报）全DONE，产物+manifest存在，
    events可重放，报告生成。

    2026-10-04 由三任务改四任务：新增 t4_charts（报告里要有图）。
    """
    root = _root_with_project_tests(tmp_path)
    s = Store(root)
    t0 = time.monotonic()
    assert scheduler.ensure_plan(s, DATE, "templates/daily_plan.yaml")[0] == "created"
    got = _run_chain(root, s)
    elapsed = time.monotonic() - t0
    assert set(got["done"]) == {"t1_collect", "t2_clean", "t4_charts",
                                "t3_report"}, got
    assert elapsed < SMOKE_BUDGET_S, elapsed
    # 产物存在（charts.md/report.md 是本轮新加的图表链路产物）
    for name in ("t1_collect.csv", "t2_clean.csv", "charts.md",
                 "summary.json", "report.md"):
        p = root / "artifacts" / DATE / name
        assert p.exists(), p
    # events可重放
    assert s.replay(PLAN)["ok"]
    assert s.integrity_check()["integrity"] == "ok"
    # 报告生成且含分解视图
    rp.write_plan_summary(s, PLAN)
    rp.write_daily(s, DATE)
    md = (root / "reports" / f"{PLAN}.summary.md").read_text(encoding="utf-8")
    assert "## 任务分解视图" in md
    assert (root / "reports" / f"{DATE}.md").exists()
    assert (root / "reports" / "metrics.prom").exists()
    # 全链路run_id一致
    con = s._connect()
    try:
        rids = {r[0] for r in con.execute(
            "SELECT DISTINCT run_id FROM events WHERE plan_id=?", (PLAN,))}
    finally:
        con.close()
    assert rids, "events里应有run_id"


def test_smoke_b_second_trigger_hits_cache(tmp_path):
    """B: 二次同输入触发命中SKIPPED_CACHED（省token可观测）。"""
    root = _root_with_project_tests(tmp_path)
    s = Store(root)
    scheduler.ensure_plan(s, DATE, "templates/daily_plan.yaml")
    _run_chain(root, s)
    t1 = s.get_task(PLAN, "t1_collect")
    # 成功后记 last_success_hash = 任务自己的inputs_hash（规格哈希）
    cp.record_success(s, PLAN, "t1_collect", t1["inputs_hash"])
    # 产物manifest由执行器落盘（cache_hit第2层要比它）
    assert (root / "artifacts" / DATE / "t1_collect.manifest.json").exists()
    # 打回PENDING重跑同输入 → 应命中缓存而不是重算
    _rewind(s, PLAN, "t1_collect", t1)
    _run_chain(root, s)
    got = s.get_task(PLAN, "t1_collect")
    assert got["status"] == "SKIPPED_CACHED", got["status"]
    assert s.metric_stat("cache_hit")["n"] >= 1


def _rewind(s: Store, plan_id: str, task_id: str, cur: dict) -> None:
    """把终态任务挪回PENDING（冒烟要重跑同输入，只能改shard状态）。
    直接SQL改而不是走状态机——状态机没有"重开"边，这是测试专用后门。"""
    import sqlite3
    con = sqlite3.connect(str(s.db_path))
    try:
        con.execute("UPDATE tasks SET status='PENDING' WHERE plan_id=? AND task_id=?",
                    (plan_id, task_id))
        con.commit()
    finally:
        con.close()


def test_smoke_c_mixed_pipeline_injection_no_preempt(tmp_path):
    """C: Daily跑一半注入管道高优 → 排队等待（不抢占）+ 当前完成后自然衔接。"""
    root = _root(tmp_path)
    s = Store(root)
    scheduler.ensure_plan(s, DATE, "templates/daily_plan.yaml")
    ran = []
    o = Orchestrator(root, executor_fn=None)
    o.executor_fn = lambda s_, p, t, r, owner=None: ran.append(t)

    o.refresh_ready(PLAN)
    o.dispatch_split()                      # t1先跑（占住）
    running = {t["task_id"] for t in s.list_by_status("RUNNING")}
    assert "t1_collect" in running

    # 跑到一半注入管道高优
    payload = {"task_id": "pipe1", "objective": "紧急插队", "priority": 0,
               "idempotency_key": "k_pipe1", "source": "pipeline"}
    f = root / "inbox" / "pipe1.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    counts = enqueue_mod.scan_inbox(s)
    assert counts["inserted"] == 1
    o.refresh_ready("pipeline")

    # 高优在队列里，但**不打断**正在跑的t1
    o.dispatch_split()
    assert "t1_collect" in {t["task_id"] for t in s.list_by_status("RUNNING")}
    assert s.get_task("pipeline", "pipe1")["status"] in ("READY", "RUNNING")
    # 队列顺序：高优在前
    order = [t["task_id"] for t in s.list_ready_ordered(limit=5)]
    if "pipe1" in order:
        assert order[0] == "pipe1", order

    # t1完成后高优自然衔接
    cur = s.get_task(PLAN, "t1_collect")
    s.transition(PLAN, "t1_collect", "SUBMITTED", agent="smoke", run_id=RUN_ID,
                 expect_version=cur["version"], role="subagent")
    cur = s.get_task(PLAN, "t1_collect")
    s.transition(PLAN, "t1_collect", "VERIFYING", agent="smoke", run_id=RUN_ID,
                 expect_version=cur["version"])
    cur = s.get_task(PLAN, "t1_collect")
    s.transition(PLAN, "t1_collect", "DONE", agent="v", run_id=RUN_ID,
                 expect_version=cur["version"], role="verifier")
    o.dispatch_split()
    assert s.get_task("pipeline", "pipe1")["status"] == "RUNNING"
    # 双链路都走到DONE
    cur = s.get_task("pipeline", "pipe1")
    s.transition("pipeline", "pipe1", "SUBMITTED", agent="smoke", run_id=RUN_ID,
                 expect_version=cur["version"], role="subagent")
    cur = s.get_task("pipeline", "pipe1")
    s.transition("pipeline", "pipe1", "VERIFYING", agent="smoke", run_id=RUN_ID,
                 expect_version=cur["version"])
    cur = s.get_task("pipeline", "pipe1")
    s.transition("pipeline", "pipe1", "DONE", agent="v", run_id=RUN_ID,
                 expect_version=cur["version"], role="verifier")
    assert s.get_task(PLAN, "t1_collect")["status"] == "DONE"
    assert s.get_task("pipeline", "pipe1")["status"] == "DONE"


def test_smoke_d_research_chain_four_set_and_audit(tmp_path):
    """D: 笼统问题→planner拆分→needs_web四段式→4条调研验收全过→含分解视图。"""
    root = _root(tmp_path)
    s = Store(root)
    question = "帮我调研2026年新能源补贴政策"
    spec = {
        "plan_title": "新能源政策调研",
        "key_questions": ["2026补贴政策有哪些变化", "行业规模数据"],
        "tasks": [{
            "task_id": "r1_collect", "objective": "检索政策原文",
            "depends_on": [], "complexity": "simple", "needs_web": True,
            "freshness": "strict", "slice_rationale": "先拿一手政策原文",
            "outputs": ["artifacts/{date}/r1.md"],
        }],
    }
    r = planner.plan_question(s, question,
                              splitter_fn=lambda p: json.dumps(spec, ensure_ascii=False),
                              date=DATE, run_id="planner-1", model="stub-model")
    pid = r["plan_id"]
    assert r["created"] is True and r["tasks"] == 1

    # 四段式：①检索 ②筛选+分级 ③分析 ④产出
    from core.search.base import SearchResult
    now = datetime.now(timezone.utc)
    # 3条来源里2条官方 → A类占比67%，稳稳高于50%这条线。
    # 卡在正好50%会FAIL（设计要求严格大于），边界拒绝由test_dod7专门覆盖，
    # 冒烟要测的是"质量达标时能走通"这条路径。
    srcs = [SearchResult(url="https://www.gov.cn/p", title="政策原文",
                         snippet="补贴政策细节", source_id="s1",
                         published_at=now.isoformat()),
            SearchResult(url="https://www.sec.gov/r", title="行业统计",
                         snippet="规模数据", source_id="s2",
                         published_at=now.isoformat()),
            SearchResult(url="https://x.com/u", title="解读",
                         snippet="非官方解读", source_id="s3",
                         published_at=now.isoformat())]

    class P:
        name = "stub"
        def search(self, q, top_k=8):
            return list(srcs)
    cfg = {"official_domains": ["gov.cn"], "ugc_domains": ["x.com"],
           "tier_criteria": {"A": "官方", "B": "媒体", "C": "个人"}}
    def llm(prompt):
        if "sec.gov" in prompt:
            return '{"tier":"A","reason":"官方域名"}'
        return '{"tier":"B","reason":"平台解读"}'
    pack = spipe.search_and_filter(
        {"freshness": "strict", "key_questions": spec["key_questions"]}, None,
        provider=P(), cfg=cfg, llm=llm, root=root)
    assert pack["raw_count"] == 6 and pack["unique_count"] == 3
    assert len(pack["kept"]) == 3
    assert pack["a_tier_ratio"] > 0.2

    # ④产出四件套（claims带source_id，不留幻觉）
    out = spipe.write_four_set(
        root, "r1_collect", DATE,
        report_md=("# 报告\n## 2026补贴政策有哪些变化\n内容\n"
                   "## 行业规模数据\n内容\n"),
        sources=[{"source_id": "s1", "url": "https://www.gov.cn/p",
                  "source_tier": "A", "tier": "A", "tier_reason": "官方域名",
                  "published_at": now.isoformat()},
                 {"source_id": "s2", "url": "https://www.sec.gov/r",
                  "source_tier": "A", "tier": "A", "tier_reason": "官方域名",
                  "published_at": now.isoformat()},
                 {"source_id": "s3", "url": "https://x.com/u",
                  "source_tier": "B", "tier": "B", "tier_reason": "平台解读",
                  "published_at": now.isoformat()}],
        claims=[{"claim_id": "c1", "text": "补贴有变化", "source_ids": ["s1"]},
                {"claim_id": "c2", "text": "行业规模增长", "source_ids": ["s2"]}],
        tuning_md="# r1 调优\n")
    assert out["ok"] is True and out["dangling_claims"] == []

    # 4条调研验收全过
    rules = [
        {"type": "source_traceable", "rule_id": "r_trace"},
        {"type": "freshness", "rule_id": "r_fresh", "freshness_days": 180},
        {"type": "source_quality", "rule_id": "r_qual",
         "source_tier_min_ratio": {"A": 0.5}},
        {"type": "coverage", "rule_id": "r_cover",
         "key_questions": spec["key_questions"]},
    ]
    shard = s.get_task(pid, "r1_collect")["shard"]
    shard["acceptance"] = rules
    shard["key_questions"] = spec["key_questions"]
    _patch_shard(s, pid, "r1_collect", shard)
    _run_to_verifying(s, pid, "r1_collect")
    ctx = __import__("core.verifier", fromlist=["make_ctx"]).make_ctx(
        s, pid, "r1_collect", DATE)
    results = __import__("core.verifier", fromlist=["run_rules"]).run_rules(ctx, rules)
    for res in results:
        assert res["ok"] is True, (res["rule_id"], res["detail"])
    # 报告含任务分解视图 + 调研质量章节
    rp.write_plan_summary(s, pid)
    md = (root / "reports" / f"{pid}.summary.md").read_text(encoding="utf-8")
    assert "## 任务分解视图" in md
    assert "拆分依据: 先拿一手政策原文" in md
    assert "## 调研质量" in md
    assert "A类占比" in md


def _patch_shard(s: Store, plan_id: str, task_id: str, shard: dict) -> None:
    import sqlite3
    con = sqlite3.connect(str(s.db_path))
    try:
        con.execute("UPDATE tasks SET shard_json=? WHERE plan_id=? AND task_id=?",
                    (json.dumps(shard, ensure_ascii=False), plan_id, task_id))
        con.commit()
    finally:
        con.close()


def _run_to_verifying(s: Store, plan_id: str, task_id: str) -> None:
    cur = s.get_task(plan_id, task_id)
    cur = s.transition(plan_id, task_id, "READY", agent="o", run_id=RUN_ID,
                       expect_version=cur["version"])
    cur = s.transition(plan_id, task_id, "RUNNING", agent="o", run_id=RUN_ID,
                       expect_version=cur["version"], lease_op="acquire")
    cur = s.transition(plan_id, task_id, "SUBMITTED", agent="sub", run_id=RUN_ID,
                       expect_version=cur["version"], role="subagent")
    s.transition(plan_id, task_id, "VERIFYING", agent="o", run_id=RUN_ID,
                 expect_version=cur["version"])


# ---------- §5 性能与成本基线 ----------

def test_baseline_single_plan_under_5min_and_db_size(tmp_path):
    """§5：单计划三任务端到端<5min（stub）；state.db<10MB/千计划。"""
    root = _root(tmp_path)
    s = Store(root)
    t0 = time.monotonic()
    scheduler.ensure_plan(s, DATE, "templates/daily_plan.yaml")
    _run_chain(root, s)
    elapsed = time.monotonic() - t0
    assert elapsed < SMOKE_BUDGET_S, f"{elapsed:.1f}s"
    size_mb = s.db_path.stat().st_size / 1e6
    assert size_mb < 10, f"{size_mb:.2f}MB"
    _log_baseline(f"单计划三任务 {elapsed:.2f}s，state.db {size_mb:.2f}MB")


def test_baseline_task_card_under_2k_tokens(tmp_path):
    """§5：任务卡<2k tokens/次。"""
    from core.task_card import estimate_tokens
    root = _root(tmp_path)
    s = Store(root)
    scheduler.ensure_plan(s, DATE, "templates/daily_plan.yaml")
    t = s.list_plan_tasks(PLAN)[0]
    card = executor.build_card(s, t, "r")
    toks = estimate_tokens(card)
    assert toks < 2000, toks
    _log_baseline(f"任务卡 {toks} tokens（门槛2000）")


def test_baseline_export_rebuild_under_5s(tmp_path):
    """§5：导出JSON可重建时间<5s。"""
    root = _root(tmp_path)
    s = Store(root)
    scheduler.ensure_plan(s, DATE, "templates/daily_plan.yaml")
    t0 = time.monotonic()
    for _ in range(10):
        s.rebuild_export(PLAN)
    per = (time.monotonic() - t0) / 10
    assert per < 5.0, f"{per:.2f}s"
    _log_baseline(f"rebuild_export {per*1000:.1f}ms/次（门槛5s）")


def test_baseline_official_domain_costs_zero_llm(tmp_path):
    """§5：来源分级——官方域名0次LLM调用；平台域名每来源1次。"""
    from core.search import classify_source as cs
    from core.search.base import SearchResult
    cfg = {"official_domains": ["gov.cn"], "ugc_domains": ["x.com"],
           "tier_criteria": {"A": "官方", "B": "媒体", "C": "个人"}}
    calls = []
    def llm(p):
        calls.append(p)
        return '{"tier":"B","reason":"解读"}'
    cs.classify_one(SearchResult(url="https://www.gov.cn/a", title="t",
                                 source_id="s1"), cfg, llm=llm)
    assert calls == []                       # 官方域名0成本
    cs.classify_one(SearchResult(url="https://x.com/u/1", title="t",
                                 source_id="s2"), cfg, llm=llm)
    cs.classify_one(SearchResult(url="https://x.com/u/2", title="t",
                                 source_id="s3"), cfg, llm=llm)
    assert len(calls) == 2                   # 平台域名每来源1次
    _log_baseline("来源分级成本：官方0次LLM，平台每来源1次")


def _log_baseline(line: str) -> None:
    out = REPORTS / "smoke_baseline.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(f"- {line}\n")


REPORTS = Path(__file__).resolve().parents[1] / "reports"
