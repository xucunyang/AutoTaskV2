"""Phase0 DoD: 同优先级FIFO、跨优先级高优先行。

依据：V6§2原则7（单表队列 ORDER BY priority ASC, seq ASC，seq由AUTOINCREMENT分配禁max+1）
+ Phase1设计§2.1派发排序。Phase1的store.py到来前，此处用SQLite内存库锁定排序契约。
"""
import sqlite3


def _db():
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE tasks("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, priority INT NOT NULL)"
    )
    return con


def _ordered(con):
    return [r[0] for r in con.execute(
        "SELECT task_id FROM tasks ORDER BY priority ASC, seq ASC")]


def test_same_priority_fifo():
    """同priority按seq先入先出。"""
    con = _db()
    for tid in ("a1", "a2", "a3"):
        con.execute("INSERT INTO tasks(task_id, priority) VALUES (?, ?)", (tid, 10))
    assert _ordered(con) == ["a1", "a2", "a3"]


def test_high_priority_first_across_priorities():
    """跨优先级：管道高优(0..4)插队到Daily低优(10)之前，高优内仍FIFO。"""
    con = _db()
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('daily1', 10)")
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('daily2', 10)")
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('pipe1', 0)")
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('pipe2', 2)")
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('normal', 5)")
    assert _ordered(con) == ["pipe1", "pipe2", "normal", "daily1", "daily2"]


def test_seq_autoincrement_no_reuse():
    """seq由AUTOINCREMENT分配：删除后新行seq不回退不重用（评审M1）。"""
    con = _db()
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('a', 10)")
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('b', 10)")
    before = [r[0] for r in con.execute("SELECT seq FROM tasks ORDER BY seq")]
    assert before[0] < before[1]
    con.execute("DELETE FROM tasks WHERE task_id='b'")
    con.execute("INSERT INTO tasks(task_id, priority) VALUES ('c', 10)")
    seq_c = con.execute("SELECT seq FROM tasks WHERE task_id='c'").fetchone()[0]
    assert seq_c > before[1]
