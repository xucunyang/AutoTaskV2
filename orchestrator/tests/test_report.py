"""Phase3 §3.1/§3.3 + 附录C：三层报告、队列看板、Prometheus。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import report as rp
from core import verify_orchestrator as vo
from core.store import Store
from tests.test_orchestrator import _add, _root
from tests.test_verifier import DATE, _art, _to_done, _to_verifying


def _seed(root: Path, s: Store) -> None:
    s.ensure_plan(f"daily_report_{DATE}")
    pid = f"daily_report_{DATE}"
    _add(s, "t1", plan_id=pid, priority=10, outputs=[f"artifacts/{DATE}/o.csv"],
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/o.csv",
                      "rule_id": "r0"}])
    _art(root, f"artifacts/{DATE}/o.csv")
    _to_done(s, pid, "t1")
    _add(s, "t2", plan_id=pid, priority=0, source="pipeline",
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/x.csv",
                      "rule_id": "r0"}])
    s.transition(pid, "t2", "READY", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "t2")["version"])
    s.transition(pid, "t2", "RUNNING", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "t2")["version"],
                 lease_op="acquire")
    s.transition(pid, "t2", "FAILED", agent="sub", run_id="r",
                 expect_version=s.get_task(pid, "t2")["version"],
                 role="subagent", payload={"error": "boom"})


# ---------- plan级 summary ----------

def test_plan_summary_index_not_content_copy(tmp_path):
    """核心是"只链不搬"：产物只给链接，不把内容复制进汇总。"""
    root = _root(tmp_path)
    s = Store(root)
    _seed(root, s)
    md = rp.render_plan_summary(s, f"daily_report_{DATE}")
    assert "t1" in md and "t2" in md
    assert f"artifacts/{DATE}/o.csv" in md       # 链接
    assert "本文件只做索引" in md                 # 明示不搬内容


def test_plan_summary_failure_on_top(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    _seed(root, s)
    md = rp.render_plan_summary(s, f"daily_report_{DATE}")
    assert "## 失败（置顶）" in md
    fail_line = md.index("t2")
    assert fail_line < md.index("## 任务分解视图")   # 失败在分解视图之前


def test_plan_summary_has_decomposition_view(tmp_path):
    """附录C：分解视图要有slice_rationale。"""
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "s1", plan_id=pid, slice_rationale="先拿一手政策原文",
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/a.csv",
                      "rule_id": "r0"}])
    md = rp.render_plan_summary(s, pid)
    assert "## 任务分解视图" in md
    assert "拆分依据: 先拿一手政策原文" in md


def test_write_plan_summary_files(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    _seed(root, s)
    out = rp.write_plan_summary(s, f"daily_report_{DATE}")
    assert Path(out["md"]).exists() and Path(out["json"]).exists()
    data = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
    assert data["total"] == 2
    assert data["counts"]["DONE"] == 1
    assert data["counts"]["FAILED"] == 1


# ---------- 调研质量章节 ----------

def test_research_chapter_reports_a_ratio(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "w1", plan_id=pid, needs_web=True, freshness="recent",
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/a.csv",
                      "rule_id": "r0"}])
    d = root / "artifacts" / DATE
    d.mkdir(parents=True, exist_ok=True)
    (d / "w1.sources.json").write_text(json.dumps([
        {"source_id": "s1", "url": "https://a.com", "source_tier": "A",
         "tier_reason": "官方"},
        {"source_id": "s2", "url": "https://b.com", "source_tier": "C",
         "tier_reason": "自媒体"}], ensure_ascii=False), encoding="utf-8")
    md = rp.render_plan_summary(s, pid)
    assert "## 调研质量" in md
    assert "A类占比 50%" in md
    assert "无理由条目不计入" in md


def test_research_chapter_flags_missing_four_set(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "w1", plan_id=pid, needs_web=True, freshness="recent",
         acceptance=[{"type": "file_exists", "path": f"artifacts/{DATE}/a.csv",
                      "rule_id": "r0"}])
    md = rp.render_plan_summary(s, pid)
    assert "缺 sources.json" in md


# ---------- 队列看板 ----------

def test_queue_board_groups_by_priority(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    for i, pr in enumerate((0, 0, 10)):
        tid = f"q{i}"
        _add(s, tid, plan_id=pid, priority=pr,
             acceptance=[{"type": "file_exists", "path": "x", "rule_id": "r0"}])
        s.transition(pid, tid, "READY", agent="o", run_id="r",
                     expect_version=s.get_task(pid, tid)["version"])
    board = rp.queue_board(s, rp.load_cfg(root))
    assert board["ready_total"] == 3
    assert board["ready_by_priority"] == {0: 2, 10: 1}
    assert "oldest_wait_s" in board
    assert "session_switch_total" in board


def test_queue_board_detects_blocking(tmp_path):
    """队列阻塞要能从看板看出来（§3.1看板独立章节）。"""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "q1", plan_id=pid, acceptance=[{"type": "file_exists", "path": "x",
                                           "rule_id": "r0"}])
    s.transition(pid, "q1", "READY", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "q1")["version"])
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    con = sqlite3.connect(str(s.db_path))
    try:
        con.execute("UPDATE tasks SET updated_at=? WHERE task_id='q1'", (old,))
        con.commit()
    finally:
        con.close()
    board = rp.queue_board(s, rp.load_cfg(root))
    assert board["oldest_wait_s"] >= 300
    assert board["queue_blocked"] is True


# ---------- 日报 ----------

def test_daily_report_is_one_screen_index(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    _seed(root, s)
    md = rp.render_daily(s, DATE)
    assert "# 日报" in md
    assert "## 算力画像" in md
    assert "## 质量告警" in md
    assert "## 弱建议（调参）" in md
    assert "## 计划索引" in md
    assert "只链不搬" in md
    assert len(md.splitlines()) < 80          # 一屏


def test_daily_quality_flags_dead_letter(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    _add(s, "d1", plan_id=pid, max_attempts=1,
         acceptance=[{"type": "file_exists", "path": "x", "rule_id": "r0"}])
    from core import lease as lease_mod

    def move(to_s, **kw):
        cur = s.get_task(pid, "d1")          # 每次重读，别手算version
        return s.transition(pid, "d1", to_s, agent="o", run_id="r",
                            expect_version=cur["version"], **kw)

    move("READY")
    move("RUNNING", lease_op="acquire")
    cur = s.get_task(pid, "d1")
    s.transition(pid, "d1", "FAILED", agent="sub", run_id="r",
                 expect_version=cur["version"], role="subagent",
                 payload={"error": "boom"})
    move("RETRY", payload={"reason": "backoff"})
    move("READY")
    move("RUNNING", lease_op="acquire")
    cur = s.get_task(pid, "d1")
    s.transition(pid, "d1", "FAILED", agent="sub", run_id="r",
                 expect_version=cur["version"], role="subagent",
                 payload={"error": "boom2"})
    lease_mod.escalate_dead_letters(s, run_id="r")
    assert s.get_task(pid, "d1")["status"] == "DEAD_LETTER"
    md = rp.render_daily(s, DATE)
    assert "P0" in md and "死信" in md


def test_daily_tuning_hints(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{DATE}"
    s.ensure_plan(pid)
    for i in range(4):
        _add(s, f"h{i}", plan_id=pid, acceptance=[{"type": "file_exists",
                                                   "path": "x", "rule_id": "r0"}])
        s.transition(pid, f"h{i}", "READY", agent="o", run_id="r",
                     expect_version=s.get_task(pid, f"h{i}")["version"])
    import sqlite3
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
    con = sqlite3.connect(str(s.db_path))
    try:
        con.execute("UPDATE tasks SET updated_at=? WHERE task_id LIKE 'h%'", (old,))
        con.commit()
    finally:
        con.close()
    md = rp.render_daily(s, DATE)
    assert "调大模型context_window" in md or "调度阻塞" in md


# ---------- Prometheus ----------

def test_prometheus_export(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    _seed(root, s)
    s.record_metric("execute", 1234.0, plan_id="p1", task_id="t1")
    s.record_metric("cache_hit", 1, plan_id="p1", task_id="t1")
    text = rp.render_prometheus(s)
    assert "orchestrator_execute_count{plan_id=\"p1\"} 1" in text
    assert "orchestrator_execute_sum" in text
    assert "orchestrator_tasks_by_status{status=\"DONE\"} 1" in text
    assert "orchestrator_queue_ready" in text


def test_prometheus_metric_names_sanitized(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    s.record_metric("weird name/dash", 1.0)
    text = rp.render_prometheus(s)
    assert "orchestrator_weird_name_dash_count" in text
    # 指标名里不能有空格/斜杠（否则Prometheus解析失败）
    for line in text.splitlines():
        name = line.split("{")[0].split(" ")[0]
        assert " " not in name and "/" not in name


def test_write_daily_and_prom(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    _seed(root, s)
    out = rp.write_daily(s, DATE)
    assert Path(out["daily"]).exists() and Path(out["prom"]).exists()
    assert Path(out["prom"]).read_text(encoding="utf-8").strip()


# ---------- 阈值可配（设计§3.3） ----------

def test_thresholds_read_from_config(tmp_path):
    root = _root(tmp_path)
    cfg_path = root / "config" / "report.yaml"
    cfg_path.write_text("top_n: 3\nqueue_block_p95_s: 999\n",
                        encoding="utf-8")
    cfg = rp.load_cfg(root)
    assert cfg["top_n"] == 3
    assert cfg["queue_block_p95_s"] == 999
    assert cfg["tuning"]["source_tier_warn"] == 0.50   # 缺项回退默认


def test_missing_config_falls_back_to_defaults(tmp_path):
    root = tmp_path
    cfg = rp.load_cfg(root)
    assert cfg["top_n"] == 5
    assert cfg["tuning"]["execute_s_warn"] == 120


def test_shanghai_date_conversion(tmp_path):
    """DB存UTC，展示+8：跨零点的那天不能算错。"""
    from datetime import datetime, timezone
    # UTC 2026-09-28T16:30 → 上海 2026-09-29
    assert rp.shanghai_date("2026-09-28T16:30:00+00:00") == "2026-09-29"
    assert rp.shanghai_date("2026-09-29T03:30:00+00:00") == "2026-09-29"
    assert rp.shanghai_date("garbage") == rp.shanghai_date()
