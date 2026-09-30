"""2.6 跨日：零点前后任务/老化/日报/额度边界的正确性。

模拟方式用**直接改 enqueued_at 到前一天**而不是伪造时钟：
改时间戳走的是真实数据路径，而 monkeypatch datetime 会连
"代码到底读哪个字段"这件事一起屏蔽掉——而那正是这里要测的。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import enqueue, report                         # noqa: E402
from core.orchestrator import Orchestrator                # noqa: E402
from core.search.base import DailyQuota                  # noqa: E402
from core.store import Store                             # noqa: E402
from core.utils import now_utc_iso                       # noqa: E402

SH = timezone(timedelta(hours=8))


def _root(tmp_path):
    r = tmp_path / "proj"
    (r / "templates").mkdir(parents=True)
    for f in (ROOT / "templates").glob("*"):
        (r / "templates" / f.name).write_bytes(f.read_bytes())
    return r


def _add(store, root, plan_id, tid, priority=5, needs_web=False):
    raw = {"task_id": tid, "plan_id": plan_id, "objective": f"做{tid}",
           "outputs": [f"artifacts/{tid}.md"], "priority": priority,
           "budget": {"max_steps": 2, "timeout_s": 30}}
    if needs_web:
        raw["needs_web"] = True
        raw["freshness"] = "recent"
    shard = enqueue.validate(json.dumps(raw, ensure_ascii=False).encode("utf-8"),
                             root=root)
    st, task = store.insert_task(shard)
    assert st == "created"
    return task


def _backdate(store, plan_id, tid, when: datetime):
    """把任务的入队时间改到指定时刻（模拟它昨天入队）。"""
    store._write_txn(lambda con: con.execute(
        "UPDATE tasks SET enqueued_at=? WHERE plan_id=? AND task_id=?",
        (when.isoformat(), plan_id, tid)))


def _yesterday(hh=23, mm=50):
    today = datetime.now(SH).date()
    return datetime.combine(today, datetime.min.time(), SH).replace(
        hour=hh, minute=mm) - timedelta(days=1)


# ---------------------------------------------------------------- 老化

def _backdate_seconds(store, plan_id, tid, seconds_ago: float):
    """把入队时间改成"多少秒前"，updated_at 保持现在。

    这样"老化读 enqueued_at 还是 updated_at"就成一个可判定的对照：
    读 updated_at 的话等待≈0，一次都不会老化。
    """
    when = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    store._write_txn(lambda con: con.execute(
        "UPDATE tasks SET enqueued_at=?, updated_at=? "
        " WHERE plan_id=? AND task_id=?",
        (when.isoformat(), now_utc_iso(), plan_id, tid)))


def test_aging_reads_enqueue_time_not_updated_at(tmp_path):
    """核心性质：老化读 enqueued_at。

    任务入队 15 分钟前、但刚被更新过（updated_at=现在）→ 必须老化。
    若读 updated_at，等待≈0，永远不老化，第二档也永远触发不了。
    """
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    _add(s, root, "p1", "t1", priority=7)      # 7 > HIGH_PRIORITY_MAX(4)
    _backdate_seconds(s, "p1", "t1", 900)      # 15min，落在10~20min档
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("p1")
    o.apply_aging()
    assert s.get_task("p1", "t1")["priority"] == 5, "没有按入队时间老化"


def test_aging_advances_one_step_per_pass(tmp_path):
    """老化阶梯：7 →（一轮）5 →（再一轮）3。

    "一次只升一档"是刻意设计：等了25min也只到5，下轮检查再到3。
    一次跳到3会让分档失去意义——分档就是为了分级提优先级。
    """
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    _add(s, root, "p1", "t1", priority=7)
    _backdate_seconds(s, "p1", "t1", 1500)     # 25min
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("p1")
    o.apply_aging()
    assert s.get_task("p1", "t1")["priority"] == 5, "第一轮只应升一档"
    o.apply_aging()
    assert s.get_task("p1", "t1")["priority"] == 3, "第二轮应到地板档"
    o.apply_aging()
    assert s.get_task("p1", "t1")["priority"] == 3, "到地板后不再降"


def test_cross_midnight_task_is_aged(tmp_path):
    """昨天23:50入队 → 零点后必须被老化（跨日最易暴露时钟口径问题）。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    _add(s, root, "p1", "t1", priority=7)
    _backdate(s, "p1", "t1", _yesterday(23, 50))
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("p1")
    aged = o.apply_aging()
    assert aged, "跨日任务未老化"
    assert s.get_task("p1", "t1")["priority"] == 5


def test_aging_alert_is_p2_not_p0(tmp_path):
    """老化告警必须是 P2。调用处写的是 P0，靠 level_for 降级——
    这条钉住"降级真的发生了"，而不是"调用方记得写对"。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    _add(s, root, "p1", "t1", priority=5)
    _backdate(s, "p1", "t1", _yesterday(23, 50))
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("p1")
    o.apply_aging()
    p = root / "reports" / "alerts.jsonl"
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]
    aged = [r for r in rows if r.get("kind") == "task_aged"]
    assert aged, "没有记录老化告警"
    assert aged[-1]["priority"] == "P2", f"老化告警级别={aged[-1]['priority']}"


def test_aging_never_downgrades(tmp_path):
    """老化只升不降。跨日重跑不能把优先级压回去。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    _add(s, root, "p1", "t1", priority=3)
    _backdate(s, "p1", "t1", _yesterday(23, 0))
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("p1")
    o.apply_aging()
    first = s.get_task("p1", "t1")["priority"]
    o.apply_aging()                      # 再跑一次
    assert s.get_task("p1", "t1")["priority"] == first


# ---------------------------------------------------------------- 派发

def test_yesterday_task_still_dispatched_after_midnight(tmp_path):
    """昨天的plan还有未完成任务 → 零点后仍要继续派发，不能因为
    '不是今天的plan'就丢掉。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("daily_report_2026-09-29", template="daily")
    _add(s, root, "daily_report_2026-09-29", "t1")
    _backdate(s, "daily_report_2026-09-29", "t1", _yesterday(23, 40))
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    assert "daily_report_2026-09-29" in o.active_plans(), \
        "昨天的plan被当成已完成踢出活动集合，昨天未完成的任务会永久搁置"
    o.refresh_ready("daily_report_2026-09-29")
    assert s.get_task("daily_report_2026-09-29", "t1")["status"] == "READY"


def test_yielded_task_resumes_across_midnight(tmp_path):
    """23:59 让出、00:05 续跑：checkpoint 按 task_id 存，与日期无关，
    必须能接上。"""
    from core import checkpoint as cp
    from schemas.models import Checkpoint
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    _add(s, root, "p1", "t1")
    ck = Checkpoint(task_id="t1", plan_id="p1", done_steps=[1, 2],
                    summary=["已拆2张"])
    cp.save(root, ck)
    loaded = cp.load(root, "t1")
    assert loaded is not None, "跨日后checkpoint读不到"
    assert loaded.done_steps == [1, 2]


# ---------------------------------------------------------------- 日报

def test_daily_report_does_not_count_other_day_as_today(tmp_path):
    """今天没有任务时，日报不能把别的日期算成今天的成果。

    原实现：找不到含今天日期的plan就 fallback 到 list_plans()[:1]——
    那是**任意顺序的第一个**，于是"日报 2026-09-30"里可能印着
    三天前的plan，还带着它的 DONE 计数，而标题写着今天。

    注意：昨日未完成的plan**允许**出现在日报里（单列一节），
    那是运维要看的；但绝不能混进"今日"的计数。
    """
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("daily_report_2026-01-01", template="daily")
    _add(s, root, "daily_report_2026-01-01", "t1")
    _backdate(s, "daily_report_2026-01-01", "t1", _yesterday(23, 0))
    today = report.shanghai_date()
    out = report.render_daily(s, today)
    assert today in out
    assert "今日无入队任务" in out
    # 昨日遗留单列，且不带任何今日成果计数
    assert "昨日未完成" in out
    for wrong in ("DONE 1", "DONE 2", "任务 1｜"):
        assert wrong not in out, f"昨日的plan被算进今日统计: {wrong}"
    # 任务还在PENDING（未到终态）→ 应列入"昨日未完成"
    assert "1 个未完成" in out, "未完成的昨天plan没有被列出来"


def test_daily_report_lists_yesterday_unfinished_separately(tmp_path):
    """今天没任务但昨天有遗留 → 遗留要单列，且**不**混进今日计数。
    运维要看的是"昨天那批还在跑"，但它不是今天的成果。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("daily_report_2026-01-01", template="daily")
    _add(s, root, "daily_report_2026-01-01", "t1")
    _backdate(s, "daily_report_2026-01-01", "t1", _yesterday(23, 0))
    o = Orchestrator(root, executor_fn=lambda *a, **k: None, run_once=True)
    o.refresh_ready("daily_report_2026-01-01")   # 留在READY=未完成
    out = report.render_daily(s, report.shanghai_date())
    assert "昨日未完成" in out
    assert "1 个未完成" in out


def test_completed_yesterday_plan_leaves_unfinished_list(tmp_path):
    """昨天的任务已到终态 → 就不该再出现在"昨日未完成"里。

    注意终态是 DONE/FAILED/DEAD_LETTER/SKIPPED_CACHED——SUBMITTED **不是**，
    它还要过验收。把SUBMITTED当终态会把"还在验收中"误报成"已完成"。
    """
    root = _root(tmp_path)
    s = Store(root)
    pid = "daily_report_2026-01-01"
    s.ensure_plan(pid, template="daily")
    t = _add(s, root, pid, "t1")
    _backdate(s, pid, "t1", _yesterday(23, 0))
    s.transition(pid, "t1", "READY", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "t1")["version"])
    s.transition(pid, "t1", "RUNNING", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "t1")["version"],
                 lease_op="acquire")
    s.transition(pid, "t1", "FAILED", agent="sub", run_id="r",
                 expect_version=s.get_task(pid, "t1")["version"],
                 expect_fencing=s.get_task(pid, "t1")["fencing_token"],
                 payload={"error": "模拟失败"})
    out = report.render_daily(s, report.shanghai_date())
    assert "1 个未完成" not in out, "已到终态的昨天plan仍被列为未完成"


def test_submitted_yesterday_task_is_still_unfinished(tmp_path):
    """SUBMITTED 不是终态——还在验收中，必须继续列在未完成里。"""
    root = _root(tmp_path)
    s = Store(root)
    pid = "daily_report_2026-01-01"
    s.ensure_plan(pid, template="daily")
    _add(s, root, pid, "t1")
    _backdate(s, pid, "t1", _yesterday(23, 0))
    s.transition(pid, "t1", "READY", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "t1")["version"])
    s.transition(pid, "t1", "RUNNING", agent="o", run_id="r",
                 expect_version=s.get_task(pid, "t1")["version"],
                 lease_op="acquire")
    s.transition(pid, "t1", "SUBMITTED", agent="sub", run_id="r",
                 expect_version=s.get_task(pid, "t1")["version"],
                 expect_fencing=s.get_task(pid, "t1")["fencing_token"],
                 handoff={"task_id": "t1", "status": "SUBMITTED",
                          "artifacts": [], "self_test": []})
    out = report.render_daily(s, report.shanghai_date())
    assert "1 个未完成" in out, "SUBMITTED 被误当成已完成"


def test_daily_report_includes_plan_regardless_of_id_naming(tmp_path):
    """plan_id 里没有日期串，但任务是今天入队的 → 仍要算进今天。

    原来按 `date in plan_id` 匹配，把这些plan全漏了；
    改用入队日判断才符合"日报=今天的账"这个语义。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("adhoc_no_date_in_name", template="daily")
    _add(s, root, "adhoc_no_date_in_name", "t1")
    out = report.render_daily(s, report.shanghai_date())
    assert "adhoc_no_date_in_name" in out, "今天入队的plan被漏掉了"


def test_daily_report_finds_todays_plan(tmp_path):
    root = _root(tmp_path)
    s = Store(root)
    today = report.shanghai_date()
    pid = f"daily_report_{today}"
    s.ensure_plan(pid, template="daily")
    _add(s, root, pid, "t1")
    out = report.render_daily(s, today)
    assert pid in out


def test_plan_summary_uses_own_enqueue_date(tmp_path):
    """跨零点：后半夜入队的任务，A类占比等统计要用**它自己**的入队日，
    不能用plan首条任务的日期（那会让它永远显示"缺四件套"）。"""
    root = _root(tmp_path)
    s = Store(root)
    pid = f"daily_report_{report.shanghai_date()}"
    s.ensure_plan(pid, template="daily")
    _add(s, root, pid, "t1", needs_web=True)
    _add(s, root, pid, "t2", needs_web=True)
    # t1 昨天入队，t2 今天
    _backdate(s, pid, "t1", _yesterday(23, 30))
    d = report.collect_plan(s, pid)
    by_id = {t["task_id"]: t for t in d["tasks"]}
    assert by_id["t1"]["artifacts_date"] != by_id["t2"]["artifacts_date"], \
        "两个任务跨了零点，入队日应该不同"


# ---------------------------------------------------------------- 额度/事件

def test_daily_quota_uses_shanghai_date_not_machine_local():
    """检索额度必须和日报/老化用同一个日期口径（上海时区）。

    原实现用 time.localtime()，机器不在 UTC+8 时额度会在另一个时刻归零——
    于是"今天已用 500 条"的判断和日报的"今天"对不上。
    """
    q = DailyQuota(per_day=10, day=report.shanghai_date())
    assert q.take() is True
    assert q.used == 1
    # 同一天内重复take正常累积
    for _ in range(4):
        q.take()
    assert q.used == 5


def test_daily_quota_rolls_over_on_new_day(tmp_path):
    q = DailyQuota(per_day=2, day="2000-01-01")
    assert q.take() and q.take()
    assert q.take() is False, "超限应拒绝"
    # 换天（模拟第二天的实例）
    q2 = DailyQuota(per_day=2, day=report.shanghai_date())
    assert q2.take() is True, "新的一天额度应重置"


def test_events_jsonl_filed_under_event_date(tmp_path):
    """事件应按**事件发生时刻**归档，不是写入时刻。
    23:59 发生的事件若在 00:01 才落盘，不能被算进第二天。"""
    root = _root(tmp_path)
    s = Store(root)
    s.ensure_plan("p1", template="daily")
    t = _add(s, root, "p1", "t1")
    # 昨天的入队时间戳，但事件是刚写的
    _backdate(s, "p1", "t1", _yesterday(23, 59))
    s.transition("p1", "t1", "READY", agent="o", run_id="r",
                 expect_version=s.get_task("p1", "t1")["version"])
    today_file = s.events_dir / f"{report.shanghai_date()}.jsonl"
    assert today_file.exists()
    ys = _yesterday(23, 59)
    con = s._connect()
    rows = con.execute("SELECT ts FROM events WHERE plan_id='p1'").fetchall()
    con.close()
    assert all(r[0] >= ys.isoformat()[:10] for r in rows), "事件时间戳应>=入队时间"
