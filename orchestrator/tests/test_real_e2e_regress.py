"""真实端到端跑出来的两个 bug 的回归测试。

这两个都是"测试全绿但生产必炸"的类型，所以测试必须照着**生产的
输入形状**写，而不是照着测试里好用的形状写。

1. route() 用 getattr 取字段 → 传 dict 时静默当 simple → 全路由到本地小模型
2. 终态转移用任务开始时的旧 version → 心跳续租把它推高 → 任务永久卡在RUNNING
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import gateway
from core import executor
from core.store import Store


# ---------------------------------------------------------------- Bug 1

def _shipped_ctx():
    import yaml
    cfg_path = Path(__file__).resolve().parents[1] / "config" / "gateway.yaml"
    return gateway.build_ctx(yaml.safe_load(cfg_path.read_text(encoding="utf-8")))


def test_route_reads_complexity_from_plain_dict():
    """store.get_task() 返回的就是 dict。getattr(dict,...) 取不到属性，
    会静默返回默认的simple——所有任务都去本地小模型。"""
    ctx = _shipped_ctx()
    assert gateway.route({"complexity": "medium", "privacy": "public"},
                         ctx)["model"] == "flash"
    assert gateway.route({"complexity": "complex", "privacy": "public"},
                         ctx)["model"] == "pro"
    assert gateway.route({"complexity": "simple", "privacy": "public"},
                         ctx)["model"] == "local"


def test_route_reads_privacy_from_plain_dict():
    ctx = _shipped_ctx()
    r = gateway.route({"complexity": "complex", "privacy": "secret"}, ctx)
    assert r["model"] == "local" and r["reason"] == "privacy_secret"


def test_route_works_for_objects_too():
    """对象路径不能因为修了 dict 就退化（Phase0 TaskShard 是Pydantic对象）。"""
    class Shard:
        complexity = "complex"
        privacy = "public"
    assert gateway.route(Shard(), _shipped_ctx())["model"] == "pro"


def test_full_task_dict_routes_correctly():
    """最贴近生产：store.get_task() 的真实返回形状。

    注意 complexity/privacy 是**顶层**字段（_row_to_task 把它们从shard
    提升上来了），但同时shard里也有一份。照真实形状写，别照想当然的形状。
    """
    ctx = _shipped_ctx()
    task = {
        "task_id": "t1", "plan_id": "p1", "status": "RUNNING", "version": 7,
        "fencing_token": 3, "owner": "orchestrator-main", "lease_until": "...",
        "attempts": 0, "max_attempts": 3, "priority": 5, "seq": 1,
        "complexity": "complex", "privacy": "public", "needs_web": False,
        "shard": {"complexity": "complex", "privacy": "public",
                  "objective": "x", "outputs": []},
    }
    assert gateway.route(task, ctx)["model"] == "pro"
    assert gateway.route({**task, "complexity": "medium"}, ctx)["model"] == "flash"
    assert gateway.route({**task, "complexity": "simple"}, ctx)["model"] == "local"


def test_dict_without_complexity_key_falls_back_to_simple():
    """真的缺字段时退回simple是可以接受的（保守），但不能是'其实有
    字段却读不到'——上面那条就是防后者。"""
    ctx = _shipped_ctx()
    assert gateway.route({"privacy": "public"}, ctx)["model"] == "local"


# ---------------------------------------------------------------- Bug 2

def _store_with_running(tmp_path):
    import json

    from core import enqueue
    s = Store(tmp_path)
    s.ensure_plan("p1", template="daily")
    shard = enqueue.validate(json.dumps({
        "task_id": "t1", "plan_id": "p1", "objective": "写个文件",
        "outputs": ["artifacts/a.md"], "complexity": "simple",
    }, ensure_ascii=False).encode("utf-8"), root=tmp_path)
    st, task = s.insert_task(shard)
    assert st == "created"
    s.transition("p1", "t1", "READY", agent="o", run_id="r",
                 expect_version=task["version"])
    task = s.get_task("p1", "t1")           # READY 转移本身会推高version
    s.transition("p1", "t1", "RUNNING", agent="o", run_id="r",
                 expect_version=task["version"], lease_op="acquire")
    return s, s.get_task("p1", "t1")


def test_stale_version_would_be_rejected(tmp_path):
    """先证明这个坑真实存在：心跳续租会推高version。"""
    s, task = _store_with_running(tmp_path)
    v0 = task["version"]
    s.lease_update("p1", "t1", owner="o",
                   lease_until="2030-01-01T00:00:00+00:00", bump_fencing=True)
    v1 = s.get_task("p1", "t1")["version"]
    assert v1 > v0, "心跳续租不改version的话这个测试就没意义"
    with pytest.raises(Exception):
        s.transition("p1", "t1", "SUBMITTED", agent="o", run_id="r",
                     expect_version=v0,
                     expect_fencing=s.get_task("p1", "t1")["fencing_token"])


def test_fresh_helper_sees_heartbeat_bump(tmp_path):
    s, task = _store_with_running(tmp_path)
    s.lease_update("p1", "t1", owner="o",
                   lease_until="2030-01-01T00:00:00+00:00", bump_fencing=True)
    ver, fen = executor._fresh(s, "p1", "t1")
    assert ver == s.get_task("p1", "t1")["version"] > task["version"]


def test_fail_lands_after_heartbeat_renewals(tmp_path):
    """核心回归：心跳续租若干次后失败，任务必须落到FAILED而不是卡住。"""
    s, task = _store_with_running(tmp_path)
    for _ in range(3):
        s.lease_update("p1", "t1", owner="orchestrator-main",
                       lease_until="2030-01-01T00:00:00+00:00",
                       bump_fencing=True)
    executor._fail(s, "p1", "t1", "orchestrator-main", "r", task, "模拟失败")
    assert s.get_task("p1", "t1")["status"] == "FAILED", "任务卡在RUNNING了"


def test_fail_on_missing_task_raises_lost_ownership(tmp_path):
    s, _ = _store_with_running(tmp_path)
    with pytest.raises(executor.LostOwnership):
        executor._fresh(s, "p1", "nonexistent")


def test_long_task_reaches_submitted(tmp_path):
    """模拟一个跑过心跳周期的任务：终态必须写得进去。"""
    s, task = _store_with_running(tmp_path)
    for _ in range(2):
        s.lease_update("p1", "t1", owner="orchestrator-main",
                       lease_until="2030-01-01T00:00:00+00:00",
                       bump_fencing=True)
    ver, fen = executor._fresh(s, "p1", "t1")
    s.transition("p1", "t1", "SUBMITTED", agent="orchestrator-main", run_id="r",
                 expect_version=ver, expect_fencing=fen,
                 handoff={"task_id": "t1", "status": "SUBMITTED",
                          "artifacts": [], "self_test": []})
    assert s.get_task("p1", "t1")["status"] == "SUBMITTED"


def test_fresh_still_respects_fencing(tmp_path):
    """重读version不能变成"绕过 fencing"——租约被抢仍要失败。"""
    s, task = _store_with_running(tmp_path)
    other = s.get_task("p1", "t1")["fencing_token"]
    s.lease_update("p1", "t1", owner="thief",
                   lease_until="2030-01-01T00:00:00+00:00", bump_fencing=True)
    ver, fen = executor._fresh(s, "p1", "t1")
    assert fen > other
    with pytest.raises(Exception):
        s.transition("p1", "t1", "SUBMITTED", agent="orchestrator-main",
                     run_id="r", expect_version=ver, expect_fencing=other)
