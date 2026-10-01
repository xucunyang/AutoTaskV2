"""真实日流水：中国2026年9月30日的房贷贴息政策影响分析。

完整链路（全部真实，不打桩）：
  planner（flash真拆分）→ 入库 → 编排派发 → 真模型执行
  （local/flash/pro + Tavily检索）→ 验收（调研四件套）
  → plan 摘要 → 日报

跑在独立目录 runs/china_20260930/ 下，不污染 orchestrator/ 的生产状态。
key 从 orchestrator/.env 读（代码位置决定），
gateway/search.yaml 从本目录的 config/ 读。

注：博查 key 已无额度（403 package quota），本次用 Tavily。

用法：
    python runs/china_20260930/run.py
输出：
    runs/china_20260930/artifacts/{date}/cn_final_report.md  ← 最终分析报告
    runs/china_20260930/reports/{plan}.summary.md            ← plan 摘要
"""
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ORCH = REPO / "orchestrator"
RUN = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCH))

import yaml                                                 # noqa: E402

from core import enqueue, gateway, planner                   # noqa: E402
from core import providers as pv                             # noqa: E402
from core import report as rp                                # noqa: E402
from core import verify_orchestrator as vmod                 # noqa: E402
from core.executor import run_task                           # noqa: E402
from core.orchestrator import Orchestrator                   # noqa: E402
from core.store import Store                                 # noqa: E402

QUESTION = ("中国2026年9月30日的房贷贴息政策的影响分析。"
            "要求：先查截至2026年9月30日现行的房贷相关政策"
            "（LPR、公积金利率、存量房贷调整、首付比例、各地购房补贴等），"
            "再查房价、成交量、居民杠杆率等数据，最后给出政策效果与市场走势判断。"
            "所有事实必须有来源。")
DATE = "2026-09-30"
MAX_TASKS = 3
TICK_BUDGET = 120
TICK_SLEEP = 2


def setup():
    for sub in ("config", "templates", "schemas"):
        dst = RUN / sub
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(ORCH / sub, dst)
    # 运行态目录每次清掉重来：planner 的 plan_id 是问题文本的哈希，
    # 重跑会撞上同一个 plan。不清的话上轮卡住的状态会污染这轮。
    for sub in ("state", "artifacts", "reports", "logs", "events", "inbox"):
        d = RUN / sub
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    print(f"run root: {RUN}（运行态已清空）", flush=True)


def real_splitter(prompt: str) -> str:
    """用真 flash 做 planner 拆分。"""
    cfg = yaml.safe_load((RUN / "config" / "gateway.yaml").read_text(
        encoding="utf-8"))
    models = pv.build_models(cfg)
    flash = models.get("flash") or models.get("pro")
    assert flash is not None, "没有可用的在线模型"
    full = (prompt + "\n注意：只输出JSON，不要解释。任务数不超过"
            f"{MAX_TASKS}个，必须形成可执行的DAG（含depends_on）。"
            "至少一个 needs_web=true 的检索任务（freshness 用 recent）。"
            "complexity 按需分配：检索与整理用 simple/medium，综合判断用 complex。")
    # 推理模型会先输出 reasoning_content，max_tokens 必须留够正文。
    # 之前设 2000 被 reasoning 吃光，content 为空。
    r = flash.chat(full, {"max_tokens": 8000})
    text = r.get("content") or ""
    print(f"  拆分响应 usage={r.get('usage')} content_len={len(text)} "
          f"reasoning_len={len(r.get('thinking') or '')}", flush=True)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        print(f"  拆分输出非JSON，前200字: {text[:200]!r}", flush=True)
        raise ValueError(f"拆分输出不是JSON: {text[:200]}")
    return text[s:e + 1]


def add_research_acceptance(store, plan_id, key_questions):
    """给 needs_web 的任务挂上4条调研验收（认 task_id 命名的四件套）。"""
    import sqlite3
    rules = [
        {"type": "source_traceable", "rule_id": "r_trace"},
        {"type": "source_quality", "rule_id": "r_qual",
         "source_tier_min_ratio": {"A": 0.4}},
        {"type": "freshness", "rule_id": "r_fresh", "freshness_days": 365},
        {"type": "coverage", "rule_id": "r_cover",
         "key_questions": key_questions},
    ]
    con = sqlite3.connect(str(store.db_path))
    try:
        for t in store.list_plan_tasks(plan_id):
            sh = dict(t.get("shard") or {})
            if not sh.get("needs_web"):
                continue
            sh["acceptance"] = rules
            sh["key_questions"] = key_questions
            con.execute("UPDATE tasks SET shard_json=? "
                        "WHERE plan_id=? AND task_id=?",
                        (json.dumps(sh, ensure_ascii=False), plan_id,
                         t["task_id"]))
            print(f"  验收已挂到 {t['task_id']}: "
                  f"{[r['type'] for r in rules]}", flush=True)
        con.commit()
    finally:
        con.close()


def ensure_four_set_outputs(store, plan_id, date):
    """保证 needs_web 的任务声明了四件套文件。

    四件套的文件名是按 **task_id** 定的（验收 handler 读的就是
    `{task_id}.sources.json` 等），不是按 planner 起的输出名。
    planner 给的 `t1_policy.md` 这类名字验收认不出来——
    必须补上 task_id 命名的三个文件，否则验收必报 no_sources_json。
    """
    import sqlite3
    con = sqlite3.connect(str(store.db_path))
    try:
        for t in store.list_plan_tasks(plan_id):
            sh = dict(t.get("shard") or {})
            if not sh.get("needs_web"):
                continue
            tid = t["task_id"]
            outs = [str(o) for o in sh.get("outputs") or []]
            for suffix in (".sources.json", ".claims.json", ".report.md"):
                want = f"artifacts/{date}/{tid}{suffix}"
                if want not in outs:
                    outs.append(want)
            sh["outputs"] = outs
            con.execute("UPDATE tasks SET shard_json=? "
                        "WHERE plan_id=? AND task_id=?",
                        (json.dumps(sh, ensure_ascii=False), plan_id, tid))
            print(f"  {tid} outputs: {outs}", flush=True)
        con.commit()
    finally:
        con.close()


def drive_verify(store, pid):
    """把 SUBMITTED→VERIFYING，并对所有 VERIFYING 跑验收。

    tick() 只管派发（READY→RUNNING→SUBMITTED）和重排，不管验收完成。
    不在这里驱动的话，第一个任务会永远卡在 VERIFYING，
    它的下游（depends_on）也永远等不到解锁——整个 DAG 停摆。
    """
    moved = []
    for t in store.list_plan_tasks(pid):
        if t["status"] == "SUBMITTED":
            cur = store.get_task(pid, t["task_id"])
            try:
                store.transition(pid, t["task_id"], "VERIFYING",
                                 agent="orchestrator", run_id="verify-cn",
                                 expect_version=cur["version"])
                moved.append(t["task_id"])
            except Exception as e:                      # noqa: BLE001
                print(f"  {t['task_id']} 进VERIFYING失败: {e}", flush=True)
    for t in store.list_plan_tasks(pid):
        if t["status"] != "VERIFYING":
            continue
        tid = t["task_id"]
        try:
            out = vmod.verify(store, pid, tid, "verify-cn")
            cur2 = store.get_task(pid, tid)
            print(f"  {tid}: verify={out} -> {cur2['status']}", flush=True)
        except Exception as e:                          # noqa: BLE001
            print(f"  {tid}: 验收异常 {type(e).__name__}: {str(e)[:150]}",
                  flush=True)
    return moved


def main() -> int:
    setup()
    store = Store(RUN)

    print("\n=== 1/5 planner 拆分（flash） ===", flush=True)
    last_err = None
    for attempt in range(2):
        try:
            r = planner.plan_question(
                store, QUESTION, splitter_fn=real_splitter, date=DATE,
                run_id=f"planner-cn-{attempt}", model="flash")
            last_err = None
            break
        except Exception as e:                              # noqa: BLE001
            last_err = e
            print(f"  拆分第{attempt + 1}次失败: {e}", flush=True)
            time.sleep(3)
    if last_err is not None:
        print(f"拆分失败，停止: {last_err}", flush=True)
        return 1
    pid = r["plan_id"]
    print(f"  plan={pid} tasks={r['tasks']}", flush=True)
    print(f"  key_questions={r['key_questions']}", flush=True)
    for t in store.list_plan_tasks(pid):
        sh = t.get("shard") or {}
        print(f"    - {t['task_id']} c={sh.get('complexity')} "
              f"web={sh.get('needs_web')} deps={sh.get('depends_on')} "
              f"out={sh.get('outputs')}", flush=True)

    ensure_four_set_outputs(store, pid, DATE)
    add_research_acceptance(store, pid, r["key_questions"])

    print("\n=== 2/5 编排派发 + 执行 + 验收 ===", flush=True)
    orch = Orchestrator(RUN, executor_fn=run_task)
    print(f"  模型: {sorted(orch.models)} "
          f"检索: {type(orch._search_provider).__name__}", flush=True)
    t0 = time.monotonic()
    for i in range(TICK_BUDGET):
        orch.tick()
        # 每轮派发后立刻驱动验收，否则 VERIFYING 会堵住整个 DAG
        drive_verify(store, pid)
        rows = store.list_plan_tasks(pid)
        states = {t["status"] for t in rows}
        done = all(s in ("DONE", "FAILED", "DEAD_LETTER", "SKIPPED",
                         "SKIPPED_CACHED", "CANCELLED") for s in states)
        if i % 10 == 0 or done:
            by = {}
            for t in rows:
                by[t["status"]] = by.get(t["status"], 0) + 1
            print(f"  tick{i}: {by}  已用 {time.monotonic() - t0:.0f}s",
                  flush=True)
        if done:
            break
        time.sleep(TICK_SLEEP)
    else:
        print("  tick预算耗尽仍有未终态任务", flush=True)
    for t in store.list_plan_tasks(pid):
        print(f"    - {t['task_id']}: {t['status']}", flush=True)

    print("\n=== 3/5 验收扫尾 ===", flush=True)
    drive_verify(store, pid)
    for t in store.list_plan_tasks(pid):
        print(f"  {t['task_id']}: {t['status']}", flush=True)

    print("\n=== 4/5 报告 ===", flush=True)
    rp.write_plan_summary(store, pid)
    print(f"  plan摘要: reports/{pid}.summary.md", flush=True)
    daily = rp.render_daily(store, DATE)
    (RUN / "reports" / f"daily_{DATE}.md").write_text(daily, encoding="utf-8")
    print(f"  日报: reports/daily_{DATE}.md", flush=True)

    print("\n=== 5/5 产物清单 ===", flush=True)
    arts = sorted((RUN / "artifacts").rglob("*"))
    for p in arts:
        if p.is_file():
            print(f"  {p.relative_to(RUN)}  {p.stat().st_size}B", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
