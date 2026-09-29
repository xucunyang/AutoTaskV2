"""存储事实源：SQLite(WAL)单写串行 + 状态机强制校验（Phase1§2）。

唯一写入口 transition()；裸sqlite3禁散落各处（评审M6），lease/enqueue/scheduler
一律走 Store 方法。读并发靠WAL，写串行靠线程锁+BEGIN IMMEDIATE+busy_timeout。
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from schemas.transitions import (
    ALLOWED,
    YIELD_REASONS,
    SESSION_SWITCH_LIMIT,
    is_allowed,
    is_self_yield,
)
from core.utils import now_utc_iso, atomic_write_json, append_jsonl, jlog

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
CREATE TABLE IF NOT EXISTS schema_version(version INT PRIMARY KEY, applied_at TEXT);
CREATE TABLE IF NOT EXISTS plans(plan_id TEXT PRIMARY KEY, template TEXT, status TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  plan_id TEXT NOT NULL, task_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','BLOCKED','READY','RUNNING','SUBMITTED','VERIFYING','WAITING_APPROVAL','RETRY','FAILED','DONE','DEAD_LETTER','CANCELLED','SKIPPED','SKIPPED_CACHED')),
  version INT NOT NULL DEFAULT 0, fencing_token INT NOT NULL DEFAULT 0,
  owner TEXT, lease_until TEXT,
  idempotency_key TEXT UNIQUE NOT NULL, inputs_hash TEXT NOT NULL,
  last_success_hash TEXT,
  attempts INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 3,
  session_switch_total INT NOT NULL DEFAULT 0,
  priority INT NOT NULL DEFAULT 10 CHECK(priority BETWEEN 0 AND 10),
  source TEXT NOT NULL DEFAULT 'daily' CHECK(source IN ('daily','pipeline')),
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  cancel_requested INT NOT NULL DEFAULT 0,
  privacy TEXT NOT NULL DEFAULT 'public', complexity TEXT NOT NULL DEFAULT 'simple',
  needs_web INT NOT NULL DEFAULT 0 CHECK(needs_web IN (0,1)),
  freshness TEXT NOT NULL DEFAULT 'none' CHECK(freshness IN ('none','recent','strict')),
  enqueued_at TEXT NOT NULL,
  shard_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(plan_id, task_id));
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  plan_id TEXT NOT NULL, task_id TEXT NOT NULL, from_s TEXT NOT NULL, to_s TEXT NOT NULL,
  run_id TEXT NOT NULL, agent TEXT NOT NULL, payload TEXT);
CREATE TABLE IF NOT EXISTS metrics(ts TEXT NOT NULL, plan_id TEXT NOT NULL, task_id TEXT NOT NULL,
  name TEXT NOT NULL, value REAL NOT NULL, run_id TEXT NOT NULL,
  span TEXT, duration_ms REAL);
CREATE INDEX IF NOT EXISTS idx_tasks_sched ON tasks(status, priority, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_plan ON tasks(plan_id,status);
CREATE INDEX IF NOT EXISTS idx_events_plan ON events(plan_id, seq);
"""


class StoreError(Exception):
    pass


class IllegalTransition(StoreError):
    pass


class Conflict(StoreError):
    """乐观锁版本冲突，调用方可重试。"""


class StaleOwner(StoreError):
    """fencing不匹配：旧owner的写被丢弃。"""


class AlreadyExists(StoreError):
    def __init__(self, task: dict):
        super().__init__("already_exists")
        self.task = task


def shanghai_date() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


class Store:
    """单写串行Store。root=orchestrator目录；测试传tmp_path隔离生产库。"""

    def __init__(self, root: str | Path, session_switch_limit: int | None = None):
        self.root = Path(root)
        self.db_path = self.root / "state" / "state.db"
        self.export_dir = self.root / "state" / "export"
        self.checkpoint_dir = self.root / "state" / "checkpoints"
        self.events_dir = self.root / "events"
        self.backup_dir = self.root / "backup"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self.session_switch_limit = (
            session_switch_limit
            if session_switch_limit is not None
            else self._load_switch_limit()
        )
        self.migrate()

    def _load_switch_limit(self) -> int:
        return int(self.time_params.get("session_switch_limit", SESSION_SWITCH_LIMIT))

    @property
    def time_params(self) -> dict:
        """时间参数集中config/schedule.yaml（评审B2）；缺配置回退设计默认值。"""
        if not hasattr(self, "_time_params"):
            defaults = {"lease_ttl_s": 120, "heartbeat_s": 30,
                        "cancel_grace_high_s": 30, "cancel_grace_default_s": 60,
                        "session_switch_limit": SESSION_SWITCH_LIMIT}
            try:
                import yaml
                cfg = yaml.safe_load(
                    (self.root / "config" / "schedule.yaml").read_text(encoding="utf-8")
                )
                lease = cfg.get("lease", {}) or {}
                params = dict(defaults)
                params["lease_ttl_s"] = int(lease.get("ttl_s", 120))
                params["heartbeat_s"] = int(lease.get("heartbeat_s", 30))
                params["cancel_grace_high_s"] = int(lease.get("cancel_grace_high_s", 30))
                params["cancel_grace_default_s"] = int(
                    lease.get("cancel_grace_default_s", 60))
                params["session_switch_limit"] = int(
                    cfg.get("session_switch_limit", SESSION_SWITCH_LIMIT))
                self._time_params = params
            except Exception:
                self._time_params = defaults
        return self._time_params

    def lease_update(self, plan_id: str, task_id: str, *, owner: str | None,
                     lease_until: str | None, bump_fencing: bool,
                     expect_version: int | None = None,
                     expect_fencing: int | None = None,
                     audit_event: dict | None = None) -> dict:
        """租约写专用通道（lease.py唯一调用方）：owner/lease_until更新+fencing控制。
        audit_event非空时记同状态审计行（from==to，replay跳过）。"""
        def _fn(con):
            cur = con.execute(
                "SELECT status,version,fencing_token,owner FROM tasks"
                " WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if cur is None:
                raise StoreError(f"task_not_found:{plan_id}/{task_id}")
            s, v, f, _owner = cur
            if expect_version is not None and v != expect_version:
                raise Conflict(f"version expect={expect_version} actual={v}")
            if expect_fencing is not None and f != expect_fencing:
                raise StaleOwner(f"fencing expect={expect_fencing} actual={f}")
            now = now_utc_iso()
            con.execute(
                "UPDATE tasks SET owner=?, lease_until=?, fencing_token=?,"
                " version=?, updated_at=? WHERE plan_id=? AND task_id=?",
                (owner, lease_until, f + (1 if bump_fencing else 0), v + 1,
                 now, plan_id, task_id),
            )
            if audit_event is not None:
                con.execute(
                    "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,"
                    "agent,payload) VALUES (?,?,?,?,?,?,?,?)",
                    (now, plan_id, task_id, s, s,
                     audit_event.get("run_id", ""), audit_event.get("agent", ""),
                     json.dumps(audit_event.get("payload", {}), ensure_ascii=False)),
                )
            return con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()

        return self._row_to_task(self._write_txn(_fn))

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        return con

    def migrate(self) -> int:
        """建表（幂等）+ user_version递增；返回当前版本。"""
        con = self._connect()
        try:
            con.executescript(DDL)
            v = con.execute("PRAGMA user_version;").fetchone()[0]
            if v < SCHEMA_VERSION:
                con.execute(
                    "INSERT OR IGNORE INTO schema_version(version, applied_at)"
                    " VALUES (?, ?)",
                    (SCHEMA_VERSION, now_utc_iso()),
                )
                con.execute(f"PRAGMA user_version={SCHEMA_VERSION};")
                con.commit()
            return max(v, SCHEMA_VERSION)
        finally:
            con.close()

    # ---------- 读路径 ----------

    def get_task(self, plan_id: str, task_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_task(row)
        finally:
            con.close()

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["shard"] = json.loads(d.get("shard_json") or "{}")
        except ValueError:
            d["shard"] = {}
        return d

    def request_cancel(self, plan_id: str, task_id: str, reason: str,
                       expect_version: int, by: str = "orchestrator",
                       run_id: str = "") -> dict:
        """置cancel_requested=1（version比对）+记同状态CANCEL_REQUESTED审计行。
        执行器步间检查该旗标后存checkpoint主动让出（RUNNING→READY）。"""
        def _fn(con):
            cur = con.execute(
                "SELECT status,version,shard_json FROM tasks"
                " WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if cur is None:
                raise StoreError(f"task_not_found:{plan_id}/{task_id}")
            s, v, shard_json = cur
            if v != expect_version:
                raise Conflict(f"version expect={expect_version} actual={v}")
            try:
                shard = json.loads(shard_json or "{}")
            except ValueError:
                shard = {}
            shard["cancel_requested"] = True
            now = now_utc_iso()
            con.execute(
                "UPDATE tasks SET cancel_requested=1, shard_json=?, version=?,"
                " updated_at=? WHERE plan_id=? AND task_id=?",
                (json.dumps(shard, ensure_ascii=False), v + 1, now,
                 plan_id, task_id),
            )
            con.execute(
                "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,agent,payload)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, plan_id, task_id, s, s, run_id, by,
                 json.dumps({"event": "CANCEL_REQUESTED", "reason": reason},
                            ensure_ascii=False)),
            )
            return con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()

        return self._row_to_task(self._write_txn(_fn))

    def list_by_status(self, status: str) -> list[dict]:
        """按状态列候选（供lease回收扫描用；排序固定seq保证可重放）。"""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY seq ASC", (status,)
            ).fetchall()
            return [self._row_to_task(r) for r in rows]
        finally:
            con.close()

    def list_ready_ordered(self, limit: int = 10, priority_min: int | None = None,
                           priority_max: int | None = None) -> list[dict]:
        """唯一派发查询：ORDER BY priority ASC, seq ASC（禁内存二次排序）。"""
        sql = "SELECT * FROM tasks WHERE status='READY'"
        args: list = []
        if priority_min is not None:
            sql += " AND priority>=?"
            args.append(priority_min)
        if priority_max is not None:
            sql += " AND priority<=?"
            args.append(priority_max)
        sql += " ORDER BY priority ASC, seq ASC LIMIT ?"
        args.append(limit)
        con = self._connect()
        try:
            return [self._row_to_task(r)
                    for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    def recent_events(self, plan_id: str, limit: int = 50) -> list[dict]:
        con = self._connect()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM events WHERE plan_id=? ORDER BY seq DESC LIMIT ?",
                (plan_id, limit),
            ).fetchall()]
        finally:
            con.close()

    # ---------- 写路径 ----------

    def _write_txn(self, fn, retries: int = 3):
        """单写串行 + BEGIN IMMEDIATE + 指数退避重试database is locked。"""
        with self._write_lock:
            delay = 0.05
            for attempt in range(retries):
                con = self._connect()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    out = fn(con)
                    con.commit()
                    return out
                except sqlite3.OperationalError as e:
                    try:
                        con.rollback()
                    except Exception:
                        pass
                    if "locked" in str(e).lower() and attempt < retries - 1:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    jlog(self.root, "ERROR", "db_write_failed",
                         error=str(e)[:500])
                    raise
                finally:
                    con.close()
            raise StoreError("db_write_failed_after_retries")

    def ensure_plan(self, plan_id: str, template: str = "") -> bool:
        """INSERT OR IGNORE；True=新建，False=已存在skip（幂等）。"""
        now = now_utc_iso()

        def _fn(con):
            cur = con.execute(
                "INSERT OR IGNORE INTO plans(plan_id, template, status,"
                " created_at, updated_at) VALUES (?,?, 'ACTIVE',?,?)",
                (plan_id, template, now, now),
            )
            return cur.rowcount > 0

        return self._write_txn(_fn)

    def insert_task(self, shard: dict) -> tuple[str, dict]:
        """INSERT tasks(PENDING…)；idempotency_key冲突返回("already_exists", 旧行)。"""
        now = now_utc_iso()
        row = {
            "plan_id": shard["plan_id"],
            "task_id": shard["task_id"],
            "status": shard.get("status", "PENDING"),
            "version": 0,
            "fencing_token": 0,
            "owner": None,
            "lease_until": None,
            "idempotency_key": shard["idempotency_key"],
            "inputs_hash": shard.get("inputs_hash", ""),
            "last_success_hash": shard.get("last_success_hash"),
            "attempts": shard.get("attempts", 0),
            "max_attempts": shard.get("max_attempts", 3),
            "session_switch_total": 0,
            "priority": shard.get("priority", 10),
            "source": shard.get("source", "daily"),
            "cancel_requested": 0,
            "privacy": shard.get("privacy", "public"),
            "complexity": shard.get("complexity", "simple"),
            "needs_web": 1 if shard.get("needs_web") else 0,
            "freshness": shard.get("freshness", "none"),
            "enqueued_at": now,
            "shard_json": json.dumps(shard, ensure_ascii=False),
            "updated_at": now,
        }

        def _fn(con):
            try:
                con.execute(
                    "INSERT INTO tasks(plan_id,task_id,status,version,fencing_token,"
                    " owner,lease_until,idempotency_key,inputs_hash,last_success_hash,"
                    " attempts,max_attempts,session_switch_total,priority,source,"
                    " cancel_requested,privacy,complexity,needs_web,freshness,"
                    " enqueued_at,shard_json,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(row.values()),
                )
            except sqlite3.IntegrityError:
                old = con.execute(
                    "SELECT * FROM tasks WHERE idempotency_key=?",
                    (row["idempotency_key"],),
                ).fetchone()
                if old is not None:
                    raise AlreadyExists(self._row_to_task(old))
                raise
            con.execute(
                "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,agent,payload)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, row["plan_id"], row["task_id"], "CREATED", row["status"],
                 shard.get("run_id", ""), "store",
                 json.dumps({"idempotency_key": row["idempotency_key"]},
                            ensure_ascii=False)),
            )
            return con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (row["plan_id"], row["task_id"]),
            ).fetchone()

        try:
            new_row = self._write_txn(_fn)
        except AlreadyExists as e:
            return "already_exists", e.task
        task = self._row_to_task(new_row)
        self._export_task(task)
        return "created", task

    def transition(self, plan_id: str, task_id: str, to_s: str, *, agent: str,
                   run_id: str, expect_version: int, expect_fencing: int | None = None,
                   handoff: dict | None = None, payload: dict | None = None,
                   lease_op: str | None = None) -> dict:
        """唯一状态跃迁入口。lease_op∈{acquire,renew,revoke}时fencing+1（评审Minor2），
        普通跃迁只+version；RETRY→READY时attempts+1；其余attempts不变。"""
        t0 = time.monotonic()
        payload = dict(payload or {})

        def _fn(con):
            cur = con.execute(
                "SELECT status,version,fencing_token,session_switch_total,"
                " attempts,shard_json FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if cur is None:
                raise StoreError(f"task_not_found:{plan_id}/{task_id}")
            s, v, f, switch, attempts, shard_json = cur
            if not is_allowed(s, to_s):
                raise IllegalTransition(f"{s}->{to_s}")
            if v != expect_version:
                raise Conflict(f"version expect={expect_version} actual={v}")
            if expect_fencing is not None and f != expect_fencing:
                raise StaleOwner(f"fencing expect={expect_fencing} actual={f}")
            new_attempts = attempts
            new_switch = switch
            if to_s == "READY" and s == "RUNNING":  # 让出边（V7§2.9）
                reason = payload.get("reason")
                if reason not in YIELD_REASONS:
                    raise IllegalTransition(f"bad_yield_reason:{reason}")
                if not self._checkpoint_exists(task_id):
                    raise IllegalTransition("yield_without_checkpoint")
                try:
                    long_running = (json.loads(shard_json or "{}")
                                    .get("long_running", False))
                except ValueError:
                    long_running = False
                if not long_running:
                    raise IllegalTransition("short_task_no_yield")
                if switch >= self.session_switch_limit:
                    raise IllegalTransition("session_switch_fused")
                new_switch = switch + 1
            if s == "RETRY" and to_s == "READY":
                new_attempts = attempts + 1  # +1只在此边，全程一次
            new_fencing = f + (1 if lease_op in ("acquire", "renew", "revoke") else 0)
            new_shard = shard_json
            if handoff is not None:
                try:
                    shard_obj = json.loads(shard_json or "{}")
                except ValueError:
                    shard_obj = {}
                shard_obj["handoff"] = handoff
                new_shard = json.dumps(shard_obj, ensure_ascii=False)
            now = now_utc_iso()
            con.execute(
                "UPDATE tasks SET status=?, version=?, fencing_token=?,"
                " attempts=?, session_switch_total=?, shard_json=?, updated_at=?"
                " WHERE plan_id=? AND task_id=?",
                (to_s, v + 1, new_fencing, new_attempts, new_switch,
                 new_shard, now, plan_id, task_id),
            )
            con.execute(
                "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,agent,payload)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, plan_id, task_id, s, to_s, run_id, agent,
                 json.dumps(payload, ensure_ascii=False)),
            )
            dt_ms = (time.monotonic() - t0) * 1000
            con.execute(
                "INSERT INTO metrics(ts,plan_id,task_id,name,value,run_id,span,duration_ms)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, plan_id, task_id, "transition", dt_ms, run_id,
                 f"{s}->{to_s}", dt_ms),
            )
            return con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()

        try:
            new_row = self._write_txn(_fn)
        except IllegalTransition as e:
            self._record_rejected_outside(plan_id, task_id, to_s, agent,
                                          run_id, str(e), payload)
            jlog(self.root, "WARN", "transition_rejected",
                 plan_id=plan_id, task_id=task_id, to=to_s, error=str(e)[:200])
            raise
        except StaleOwner as e:
            self._record_rejected_outside(plan_id, task_id, to_s, agent,
                                          run_id, str(e), payload)
            jlog(self.root, "WARN", "stale_owner",
                 plan_id=plan_id, task_id=task_id, error=str(e)[:200])
            raise
        task = self._row_to_task(new_row)
        self._export_task(task)
        self._append_event_jsonl(plan_id, task_id)
        return task

    def _record_rejected_outside(self, plan_id, task_id, to_s, agent, run_id,
                                   kind, payload):
        """失败事务已回滚，另开事务记REJECTED（审计可查，replay跳过）。"""
        def _fn(con):
            cur = con.execute(
                "SELECT status FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if cur is None:
                return
            con.execute(
                "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,agent,payload)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now_utc_iso(), plan_id, task_id, cur[0], "REJECTED", run_id,
                 agent, json.dumps({"attempted": to_s, "rejected": kind,
                                    **(payload or {})}, ensure_ascii=False)),
            )
        try:
            self._write_txn(_fn)
        except Exception:
            pass  # 审计行写失败不掩盖原异常

    def _checkpoint_exists(self, task_id: str) -> bool:
        return (self.checkpoint_dir / f"{task_id}.json").exists()

    # ---------- 导出/恢复 ----------

    def _export_task(self, task: dict) -> None:
        atomic_write_json(self.export_dir / "tasks" / f"{task['task_id']}.json", task)

    def _append_event_jsonl(self, plan_id: str, task_id: str) -> None:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM events WHERE plan_id=? AND task_id=?"
                " ORDER BY seq DESC LIMIT 5",
                (plan_id, task_id),
            ).fetchall()
        finally:
            con.close()
        path = self.events_dir / f"{shanghai_date()}.jsonl"
        for r in reversed(rows[-1:]):
            append_jsonl(path, dict(r))

    def rebuild_export(self, plan_id: str) -> int:
        """从DB全量重建导出（含master.json）；返回任务数。"""
        con = self._connect()
        try:
            tasks = [self._row_to_task(r) for r in con.execute(
                "SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))]
            plan = con.execute(
                "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        finally:
            con.close()
        for t in tasks:
            self._export_task(t)
        atomic_write_json(self.export_dir / "master.json", {
            "plan_id": plan_id,
            "plan": dict(plan) if plan else None,
            "tasks": [{"task_id": t["task_id"], "status": t["status"],
                       "priority": t["priority"], "seq": t["seq"]} for t in tasks],
            "rebuilt_at": now_utc_iso(),
        })
        return len(tasks)

    def replay(self, plan_id: str) -> dict:
        """从events重放校验DB快照一致；跳过REJECTED/同状态审计行/CREATED外所有行按序应用。"""
        con = self._connect()
        try:
            evs = con.execute(
                "SELECT task_id,from_s,to_s FROM events WHERE plan_id=? ORDER BY seq",
                (plan_id,),
            ).fetchall()
            db = {r["task_id"]: r["status"] for r in con.execute(
                "SELECT task_id,status FROM tasks WHERE plan_id=?", (plan_id,))}
        finally:
            con.close()
        state: dict[str, str] = {}
        for e in evs:
            tid, fr, to = e["task_id"], e["from_s"], e["to_s"]
            if to == "REJECTED" or fr == to:
                continue
            state[tid] = to  # CREATED行：from_s=CREATED，直接落to_s
        mismatches = [{"task_id": tid, "replayed": state.get(tid), "db": db.get(tid)}
                      for tid in set(state) | set(db)
                      if state.get(tid) != db.get(tid)]
        return {"ok": not mismatches, "mismatches": mismatches,
                "checked": len(set(state) | set(db))}

    # ---------- 备份/迁移 ----------

    def backup(self) -> Path:
        """VACUUM INTO每日拷贝（上海自然日）；返回备份路径。"""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"state-{shanghai_date()}.db"
        con = self._connect()
        try:
            con.execute(f"VACUUM INTO '{dest}'")
        finally:
            con.close()
        return dest
