"""Phase1 §2.3b 复查：迁移路径（备份、增量补列）与读路径聚合。"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.store import SCHEMA_VERSION, Store


def _make_v1_db(root: Path) -> None:
    """造一个"v1时代"的库：有tasks表但没有verify_progress_json列。"""
    db = root / "state"
    db.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db / "state.db"))
    try:
        con.executescript("""
            CREATE TABLE schema_version(version INT PRIMARY KEY, applied_at TEXT);
            CREATE TABLE tasks(
              plan_id TEXT NOT NULL, task_id TEXT NOT NULL,
              status TEXT NOT NULL, version INT NOT NULL DEFAULT 0,
              fencing_token INT NOT NULL DEFAULT 0, owner TEXT, lease_until TEXT,
              idempotency_key TEXT UNIQUE NOT NULL, inputs_hash TEXT NOT NULL,
              attempts INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 3,
              priority INT NOT NULL DEFAULT 10, source TEXT NOT NULL DEFAULT 'daily',
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              enqueued_at TEXT NOT NULL, shard_json TEXT NOT NULL,
              updated_at TEXT NOT NULL, UNIQUE(plan_id, task_id));
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
              plan_id TEXT NOT NULL, task_id TEXT NOT NULL, from_s TEXT NOT NULL,
              to_s TEXT NOT NULL, run_id TEXT NOT NULL, agent TEXT NOT NULL, payload TEXT);
            CREATE TABLE metrics(ts TEXT NOT NULL, plan_id TEXT NOT NULL,
              task_id TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL,
              run_id TEXT NOT NULL, span TEXT, duration_ms REAL);
            INSERT INTO schema_version(version, applied_at) VALUES (1, 'old');
            PRAGMA user_version=1;
        """)
        con.execute(
            "INSERT INTO tasks(plan_id,task_id,status,idempotency_key,inputs_hash,"
            "enqueued_at,shard_json,updated_at) VALUES"
            " ('p1','legacy','PENDING','k1','h1','t','{}','t')")
        con.commit()
    finally:
        con.close()


def test_migration_adds_missing_column_to_legacy_db(tmp_path):
    """老库缺列是最阴的故障：建表语句全带IF NOT EXISTS，一路绿灯，
    直到运行期某条查询才 'no such column' 炸。启动时就该补上。"""
    _make_v1_db(tmp_path)
    s = Store(tmp_path)                      # 触发migrate
    con = sqlite3.connect(str(s.db_path))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks);")}
        uv = con.execute("PRAGMA user_version;").fetchone()[0]
    finally:
        con.close()
    assert "verify_progress_json" in cols
    assert uv == SCHEMA_VERSION
    # 原有数据没丢
    assert s.get_task("p1", "legacy") is not None
    # 新列可写
    got = s.update_verify_progress("p1", "legacy", ["rule_0"], expect_version=0)
    assert got["verify_progress"]["verified_rules"] == ["rule_0"]


def test_migration_is_idempotent(tmp_path):
    s = Store(tmp_path)
    for _ in range(3):
        Store(tmp_path)
    con = sqlite3.connect(str(s.db_path))
    try:
        uv = con.execute("PRAGMA user_version;").fetchone()[0]
        cols = [r[1] for r in con.execute("PRAGMA table_info(tasks);")]
    finally:
        con.close()
    assert uv == SCHEMA_VERSION
    assert cols.count("verify_progress_json") == 1   # 没被补两次


def test_real_migration_takes_backup_first(tmp_path):
    """Phase1§2.3b：真迁移前必须先VACUUM留底。"""
    _make_v1_db(tmp_path)
    Store(tmp_path)
    backups = list((tmp_path / "backup").glob("state-*.db"))
    assert len(backups) == 1
    con = sqlite3.connect(str(backups[0]))
    try:
        # 备份的是迁移前的库：还没补列，但数据在
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks);")}
        n = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        con.close()
    assert "verify_progress_json" not in cols
    assert n == 1


def test_fresh_db_does_not_backup(tmp_path):
    """全新库没有"迁移"，不该产生空备份。"""
    Store(tmp_path)
    assert list((tmp_path / "backup").glob("state-*.db")) == []


def test_no_backup_when_version_current(tmp_path):
    """版本已是最新就不备份——每次启动都VACUUM纯属浪费。"""
    Store(tmp_path)
    Store(tmp_path)
    assert list((tmp_path / "backup").glob("state-*.db")) == []
