"""混沌/恢复演练（Phase1§6 DoD）：kill -9 无半写、备份可恢复、seq不回退。"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from core.store import Store

REPO = Path(__file__).resolve().parent.parent


def _child_code(body: str) -> str:
    """子进程脚本：sys.argv[1]为测试隔离root，绝不触碰生产库。"""
    header = (f"import os, sys, time\n"
              f"from pathlib import Path\n"
              f"sys.path.insert(0, {str(REPO)!r})\n"
              f"from core.store import Store\n"
              f"root = Path(sys.argv[1])\n"
              f"store = Store(root)\n")
    return header + textwrap.dedent(body).strip("\n") + "\n"


def _wait_line(proc, expect: str) -> None:
    """读子进程首行；失败时把stderr带进断言信息（否则子进程错误不可见）。"""
    line = proc.stdout.readline()
    if line.strip() != expect:
        proc.kill()
        _out, err = proc.communicate(timeout=20)
        raise AssertionError(f"expected {expect!r}, got {line!r}; child stderr:\n{err[-1500:]}")


def test_kill9_before_commit_leaves_no_half_write(tmp_path):
    """子进程在BEGIN IMMEDIATE后、COMMIT前被kill：重启后无半写、replay一致。"""
    code = _child_code("""
        con = store._connect()
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO tasks(plan_id,task_id,status,version,fencing_token,"
                    " idempotency_key,inputs_hash,attempts,max_attempts,"
                    " session_switch_total,priority,source,cancel_requested,privacy,"
                    " complexity,needs_web,freshness,enqueued_at,shard_json,updated_at)"
                    " VALUES ('p1','ghost','PENDING',0,0,'k_ghost','',0,3,0,10,'daily',0,"
                    "'public','simple',0,'none',datetime('now'),'{}',datetime('now'))")
        print("WROTE", flush=True)
        time.sleep(30)   # 停在未提交事务上被kill
    """)
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_line(proc, "WROTE")
    proc.kill()
    proc.wait(timeout=30)

    s = Store(tmp_path)   # 重启：migrate 幂等 + 恢复打开
    assert s.get_task("p1", "ghost") is None      # 未提交 → 完全不存在
    assert s.integrity_check()["integrity"] == "ok"
    assert s.replay("p1")["ok"]


def test_kill9_after_commit_survives_restart(tmp_path):
    """COMMIT后崩溃：行还在，rebuild_export可补齐导出，replay一致。"""
    code = _child_code("""
        s2 = Store(root)
        s2.ensure_plan("p9", template="t")
        s2.insert_task({"task_id": "kept", "plan_id": "p9", "status": "PENDING",
                        "idempotency_key": "k9", "objective": "o"})
        print("COMMITTED", flush=True)
        time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _wait_line(proc, "COMMITTED")
    proc.kill()
    proc.wait(timeout=30)
    s = Store(tmp_path)   # 重启
    t = s.get_task("p9", "kept")
    assert t is not None and t["status"] == "PENDING"
    assert s.integrity_check()["integrity"] == "ok"
    assert s.replay("p9")["ok"]
    assert s.rebuild_export("p9") == 1
    assert (s.export_dir / "tasks" / "kept.json").exists()


def test_seq_never_goes_backwards(tmp_path):
    s = Store(tmp_path)
    s.ensure_plan("p1")
    seen = []
    for i in range(5):
        _st, row = s.insert_task({"task_id": f"t{i}", "plan_id": "p1",
                                  "status": "PENDING", "idempotency_key": f"k{i}",
                                  "objective": "o"})
        seen.append(row["seq"])
    assert seen == sorted(seen) and len(set(seen)) == 5
    restarted = Store(tmp_path)          # 模拟重启
    _st, row = restarted.insert_task({"task_id": "t9", "plan_id": "p1",
                                      "status": "PENDING", "idempotency_key": "k9",
                                      "objective": "o"})
    assert row["seq"] > max(seen)         # 重启后不回退不丢


def test_backup_is_idempotent_and_restorable(tmp_path):
    s = Store(tmp_path)
    s.ensure_plan("pb", template="t")
    s.insert_task({"task_id": "bt", "plan_id": "pb", "status": "PENDING",
                   "idempotency_key": "kb", "objective": "o"})
    b1 = s.backup()
    assert b1.exists()
    b2 = s.backup()                      # 同日重复备份不报错也不覆盖
    assert b1 == b2
    con = sqlite3.connect(str(b1))
    try:
        n = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        uv = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    assert n == 1 and uv >= 1
    # 备份可独立打开：拷到新root的state/state.db继续跑
    fresh = tmp_path / "restored"
    (fresh / "state").mkdir(parents=True)
    import shutil
    shutil.copy(str(b1), str(fresh / "state" / "state.db"))
    rs = Store(fresh)
    assert rs.get_task("pb", "bt") is not None
    assert rs.integrity_check()["integrity"] == "ok"
    assert rs.replay("pb")["ok"]


def test_db_locked_retries_then_succeeds(tmp_path):
    """busy_timeout + 指数退避：外部持锁时写入最终成功（§异常矩阵 DB locked）。"""
    s = Store(tmp_path)
    s.ensure_plan("p1")
    blocker = sqlite3.connect(str(s.db_path), timeout=0.1, isolation_level=None)
    blocker.execute("PRAGMA busy_timeout=100;")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO plans(plan_id,template,status,created_at,updated_at)"
                    " VALUES ('blocker','t','ACTIVE','x','x')")
    import threading
    result = {}

    def _writer():
        try:
            result["row"] = s.insert_task({"task_id": "w", "plan_id": "p1",
                                           "status": "PENDING",
                                           "idempotency_key": "kw",
                                           "objective": "o"})[1]
        except Exception as e:  # noqa: BLE001
            result["err"] = repr(e)

    th = threading.Thread(target=_writer)
    th.start()
    time.sleep(1.5)
    blocker.rollback()
    blocker.close()
    th.join(timeout=30)
    assert "row" in result, result
    assert result["row"]["task_id"] == "w"
