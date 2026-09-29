"""Phase3 §3.2：告警分级与去重。P0只有三项是硬约束，不是建议。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.notifier import P0_KINDS, AlertPolicy, Notifier, level_for
from core.store import Store
from tests.test_orchestrator import _root


def _notifier(tmp_path, **kw):
    root = _root(tmp_path)
    return Notifier(root, printer=lambda *a: None, **kw)


def _alerts(root: Path) -> str:
    p = root / "reports" / "alerts.jsonl"
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ---------- P0 只有三项 ----------

def test_p0_kinds_are_exactly_three():
    """设计§9定版：P0=DEAD_LETTER / INCONSISTENT / 高优SLA超时。
    多一条就稀释真告警的注意力，所以这是**白名单**不是"重要的事标P0"。"""
    assert P0_KINDS == {"dead_letter", "inconsistent", "dispatch_sla_breach"}


def test_level_for_three_kinds():
    for k in ("dead_letter", "inconsistent", "dispatch_sla_breach"):
        assert level_for(k) == "P0"


def test_level_for_p1_kinds():
    for k in ("task_failed", "lease_expired", "queue_backlog",
              "session_switch_fused", "planner_split_failed", "dag_invalid",
              "illegal_transition", "heartbeat_lost"):
        assert level_for(k) == "P1", k


def test_level_for_p2_kinds():
    for k in ("task_aged", "skipped_cached", "source_no_published_at",
              "yield_reason", "schema_reject"):
        assert level_for(k) == "P2", k


def test_unknown_kind_defaults_to_p2():
    """未知kind默认最低级：宁可进日报也不要半夜打电话。"""
    assert level_for("whatever_new_thing") == "P2"
    assert level_for("") == "P2"


def test_requesting_p0_for_non_p0_kind_is_downgraded(tmp_path):
    """调用方不能自选级别——否则哪天图省事写P0就破防了。"""
    n = _notifier(tmp_path)
    r = n.send("P0", "task_aged", task_id="t1", plan_id="p1", body="老化")
    assert r["sent"] is True
    assert r["level"] == "P2"
    assert r["requested"] == "P0"
    assert r["downgraded"] is True
    assert '"priority": "P2"' in _alerts(tmp_path)


def test_aging_is_p2_not_p0(tmp_path):
    """我在Phase2把老化发成P0，违反设计§3.2（老化属日报聚合）。
    这条测试就是防止它再退化回去。"""
    root = _root(tmp_path)
    s = Store(root)
    s.alert("P0", "task_aged", plan_id="p1", task_id="t1")
    text = _alerts(root)
    assert '"priority": "P2"' in text
    assert '"priority": "P0"' not in text


# ---------- 去重（评审M8：同task+reason 5min合并） ----------

def test_same_key_within_window_is_deduped(tmp_path):
    n = _notifier(tmp_path, policy=AlertPolicy(dedup_window_s=300))
    t0 = 1000.0
    a = n.send("P1", "task_failed", plan_id="p1", task_id="t1", now=t0)
    b = n.send("P1", "task_failed", plan_id="p1", task_id="t1", now=t0 + 60)
    c = n.send("P1", "task_failed", plan_id="p1", task_id="t1", now=t0 + 299)
    assert a["sent"] is True
    assert b["sent"] is False and "dedup" in b["reason"]
    assert c["sent"] is False
    lines = [x for x in _alerts(tmp_path).splitlines() if x.strip()]
    assert len(lines) == 1


def test_dedup_expires_after_window(tmp_path):
    n = _notifier(tmp_path, policy=AlertPolicy(dedup_window_s=300))
    n.send("P1", "task_failed", plan_id="p1", task_id="t1", now=1000.0)
    later = n.send("P1", "task_failed", plan_id="p1", task_id="t1", now=1400.0)
    assert later["sent"] is True


def test_different_task_or_kind_not_deduped(tmp_path):
    n = _notifier(tmp_path)
    assert n.send("P1", "task_failed", plan_id="p1", task_id="t1")["sent"]
    assert n.send("P1", "task_failed", plan_id="p1", task_id="t2")["sent"]
    assert n.send("P1", "lease_expired", plan_id="p1", task_id="t1")["sent"]


def test_hourly_cap(tmp_path):
    """去重窗口过了也不无限发：每小时上限兜底。"""
    n = _notifier(tmp_path, policy=AlertPolicy(dedup_window_s=1, max_per_hour=3))
    sent = 0
    t = 1000.0
    for i in range(6):
        r = n.send("P1", "task_failed", plan_id="p1", task_id="t1",
                   now=t + i * 5)     # 每5s一条，窗口1s
        if r["sent"]:
            sent += 1
    assert sent == 3
    assert "hourly_cap" in n.send("P1", "task_failed", plan_id="p1",
                                  task_id="t1", now=t + 100)["reason"]


def test_suppressed_alerts_leave_trace(tmp_path):
    """被去重的也要留痕，否则事后查"为什么没告警"没线索。"""
    n = _notifier(tmp_path)
    n.send("P1", "task_failed", plan_id="p1", task_id="t1")
    n.send("P1", "task_failed", plan_id="p1", task_id="t1")
    supp = tmp_path / "reports" / "alerts_suppressed.jsonl"
    assert supp.exists()
    row = json.loads(supp.read_text(encoding="utf-8").splitlines()[0])
    assert row["kind"] == "task_failed"
    assert "dedup" in row["suppressed_reason"]


# ---------- 状态跨进程有效 ----------

def test_dedup_state_survives_new_notifier(tmp_path):
    """去重状态落盘而非内存：多实例各写内存等于没去重。"""
    a = _notifier(tmp_path)
    assert a.send("P1", "task_failed", plan_id="p1", task_id="t1")["sent"]
    b = _notifier(tmp_path)          # 全新实例（模拟另一进程/重启后）
    assert b.send("P1", "task_failed", plan_id="p1", task_id="t1")["sent"] is False
    assert (tmp_path / "state" / "alerts_state.json").exists()


def test_corrupt_state_does_not_block(tmp_path):
    p = tmp_path / "state" / "alerts_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{坏", encoding="utf-8")
    n = _notifier(tmp_path)
    assert n.send("P1", "task_failed", plan_id="p1", task_id="t1")["sent"]


# ---------- 便捷入口 ----------

def test_convenience_helpers(tmp_path):
    n = _notifier(tmp_path)
    assert n.p0("dead_letter", plan_id="p", task_id="t")["level"] == "P0"
    assert n.p1("lease_expired", plan_id="p", task_id="t")["level"] == "P1"
    assert n.p2("task_aged", plan_id="p", task_id="t")["level"] == "P2"


def test_store_alert_routes_through_policy(tmp_path):
    """Store.alert只负责转发，级别判定必须落在Notifier一处。"""
    root = _root(tmp_path)
    s = Store(root)
    r1 = s.alert("P0", "dead_letter", plan_id="p1", task_id="t1")
    assert r1["level"] == "P0" and r1["sent"]
    r2 = s.alert("P0", "task_aged", plan_id="p1", task_id="t1")
    assert r2["level"] == "P2"
    assert r2["sent"]        # 不同kind，不被去重


def test_alert_body_keeps_context(tmp_path):
    n = _notifier(tmp_path)
    n.p1("task_failed", plan_id="p1", task_id="t1", run_id="r-9",
         body="boom")
    row = json.loads(_alerts(tmp_path).splitlines()[0])
    assert row["run_id"] == "r-9" and row["body"] == "boom"
    assert row["priority"] == "P1"
