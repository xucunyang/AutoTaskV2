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
    TERMINAL,
    YIELD_REASONS,
    SESSION_SWITCH_LIMIT,
    SYSTEM_ONLY,
    ORCH_ONLY_TRANSITIONS,
    SELF_YIELD_TRANSITIONS,
    ROLES,
    SUBAGENT_TRANSITIONS,
    VERIFIER_TRANSITIONS,
    is_allowed,
    is_self_yield,
    role_allowed,
)
from core.utils import now_utc_iso, atomic_write_json, append_jsonl, jlog

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 2

# 增量迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 所以新增字段必须显式声明在这里，靠 ALTER TABLE 补上。
# 漏了一条=老库上直接 "no such column" 崩在运行期，而不是启动期。
ADDED_COLUMNS: dict[str, str] = {
    "verify_progress_json": "TEXT",          # v1→v2：V7 VERIFYING局部重入账本
}

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
  -- V7§3 LLM语义评审局部重入：存verified_rules已通过的rule_id，requeue_verify只重跑未过的
  verify_progress_json TEXT,
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


class TaskIdConflict(StoreError):
    """同一(plan_id,task_id)已存在但inputs_hash不同：不是重复投递，是输入变了。
    与AlreadyExists严格区分——前者可静默幂等，后者必须让人看见。"""


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
        """建表（幂等）+ 补列 + user_version升级。

        顺序：先探测旧版本 → 若是真迁移则**先备份**（Phase1§2.3b
        "启动/迁移前 VACUUM INTO backup"）→ 建表 → ALTER补列 → 记版本。
        老库缺列是最阴的故障：建表语句全带IF NOT EXISTS，一路绿灯，
        直到运行期某条查询才 "no such column" 炸。"""
        exists_before = self.db_path.exists()
        old_version = 0
        if exists_before:
            probe = sqlite3.connect(str(self.db_path), timeout=5.0)
            try:
                old_version = probe.execute("PRAGMA user_version;").fetchone()[0]
            finally:
                probe.close()
        if exists_before and 0 < old_version < SCHEMA_VERSION:
            # 真迁移才备份：每次启动都备份没有意义（同日幂等），但迁移前必须留底
            try:
                self.backup()
            except Exception as e:      # 备份失败不能挡住启动，但要喊出来
                jlog(self.root, "ERROR", "pre_migrate_backup_failed",
                     from_version=old_version, to_version=SCHEMA_VERSION,
                     error=str(e)[:300])
        con = self._connect()
        try:
            con.executescript(DDL)
            added = self._add_missing_columns(con)
            v = con.execute("PRAGMA user_version;").fetchone()[0]
            if v < SCHEMA_VERSION:
                con.execute(
                    "INSERT OR IGNORE INTO schema_version(version, applied_at)"
                    " VALUES (?, ?)",
                    (SCHEMA_VERSION, now_utc_iso()),
                )
                con.execute(f"PRAGMA user_version={SCHEMA_VERSION};")
                con.commit()
            if added:
                jlog(self.root, "WARN", "schema_columns_added", columns=added,
                     from_version=old_version, to_version=SCHEMA_VERSION)
            return max(v, SCHEMA_VERSION)
        finally:
            con.close()

    @staticmethod
    def _add_missing_columns(con: sqlite3.Connection) -> list[str]:
        """给已存在的tasks表补新增列。返回实际补了哪些（迁移审计）。"""
        have = {r[1] for r in con.execute("PRAGMA table_info(tasks);").fetchall()}
        added = []
        for col, decl in ADDED_COLUMNS.items():
            if col in have:
                continue
            con.execute(f"ALTER TABLE tasks ADD COLUMN {col} {decl};")
            added.append(col)
        if added:
            con.commit()
        return added

    def update_priority(self, plan_id: str, task_id: str, new_priority: int,
                        *, expect_version: int, reason: str = "aging",
                        waited_s: int = 0) -> dict:
        """老化提升优先级的**唯一**合法写口（Phase2§4）。

        只允许调高紧急度（数值变小），不允许把任务"降级"变慢——
        降级会让等待中的任务被无限推后，正好和防饿死的目标相反。
        每次提升记AGED审计行，便于复盘"这个任务等了多久被提了几次"。
        """
        if not isinstance(new_priority, int) or not 0 <= new_priority <= 10:
            raise IllegalTransition(f"bad_priority:{new_priority}")

        def _fn(con):
            cur = con.execute(
                "SELECT status,version,priority FROM tasks"
                " WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if cur is None:
                raise StoreError(f"task_not_found:{plan_id}/{task_id}")
            s, v, p = cur
            if v != expect_version:
                raise Conflict(f"version expect={expect_version} actual={v}")
            if new_priority > p:
                raise IllegalTransition(f"priority_downgrade_forbidden:{p}->{new_priority}")
            if s != "READY":
                raise IllegalTransition(f"aging_only_when_ready:{s}")
            now = now_utc_iso()
            con.execute(
                "UPDATE tasks SET priority=?, version=?, updated_at=?"
                " WHERE plan_id=? AND task_id=?",
                (new_priority, v + 1, now, plan_id, task_id))
            con.execute(
                "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,agent,payload)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (now, plan_id, task_id, s, s, "aging", "orchestrator",
                 json.dumps({"event": "AGED", "reason": reason,
                             "from_priority": p, "to_priority": new_priority,
                             "waited_s": waited_s}, ensure_ascii=False)))
            return con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id)).fetchone()

        return self._row_to_task(self._write_txn(_fn))

    def record_metric(self, name: str, value: float, *, plan_id: str = "-",
                      task_id: str = "-", run_id: str = "", span: str = "",
                      duration_ms: float | None = None) -> None:
        """独立事务写metrics。给"写不进主表也要留痕"的场景用：
        DB锁失败、备份失败这类——记录本身也可能失败，失败只记日志不抛。"""
        try:
            con = self._connect()
            try:
                con.execute(
                    "INSERT INTO metrics(ts,plan_id,task_id,name,value,run_id,span,"
                    "duration_ms) VALUES (?,?,?,?,?,?,?,?)",
                    (now_utc_iso(), plan_id, task_id, name, value, run_id, span,
                     duration_ms))
                con.commit()
            finally:
                con.close()
        except Exception as e:
            jlog(self.root, "WARN", "metric_write_failed", name=name,
                 error=str(e)[:200])

    def daily_summary(self, plan_id: str, events_limit: int = 50) -> dict:
        """Phase1§2.3读路径：日报只读 plan摘要+状态计数+最近N条events，
        不给全量。日报渲染（Phase3）与飞书推送都走这一个聚合口。"""
        con = self._connect()
        try:
            plan = con.execute("SELECT * FROM plans WHERE plan_id=?",
                               (plan_id,)).fetchone()
            counts = {r["status"]: r["n"] for r in con.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE plan_id=?"
                " GROUP BY status", (plan_id,))}
            evs = [dict(r) for r in con.execute(
                "SELECT * FROM events WHERE plan_id=? ORDER BY seq DESC LIMIT ?",
                (plan_id, events_limit)).fetchall()]
            total = con.execute("SELECT COUNT(*) FROM tasks WHERE plan_id=?",
                                (plan_id,)).fetchone()[0]
        finally:
            con.close()
        return {"plan_id": plan_id,
                "plan": dict(plan) if plan else None,
                "task_total": total,
                "status_counts": counts,
                "recent_events": evs}   # 倒序：最新在前

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
        try:
            d["verify_progress"] = json.loads(d.get("verify_progress_json") or "{}")
        except ValueError:
            d["verify_progress"] = {}
        return d

    def update_verify_progress(self, plan_id: str, task_id: str,
                               verified_rules: list[str], *,
                               expect_version: int) -> dict:
        """V7§3：VERIFYING中记录已通过的rule_id（局部重入的进度账本）。
        与status解耦，写回带expect_version防并发覆盖。"""
        def _fn(con):
            cur = con.execute(
                "SELECT version,verify_progress_json FROM tasks"
                " WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if cur is None:
                raise StoreError(f"task_not_found:{plan_id}/{task_id}")
            v, raw = cur
            if v != expect_version:
                raise Conflict(f"version expect={expect_version} actual={v}")
            try:
                progress = json.loads(raw or "{}")
            except ValueError:
                progress = {}
            progress["verified_rules"] = sorted(set(verified_rules))
            progress["updated_at"] = now_utc_iso()
            blob = json.dumps(progress, ensure_ascii=False)
            con.execute(
                "UPDATE tasks SET verify_progress_json=?, version=?, updated_at=?"
                " WHERE plan_id=? AND task_id=?",
                (blob, v + 1, now_utc_iso(), plan_id, task_id),
            )
            return con.execute(
                "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()

        return self._row_to_task(self._write_txn(_fn))

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

    def list_plans(self) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute("SELECT * FROM plans ORDER BY plan_id ASC").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def list_plan_tasks(self, plan_id: str) -> list[dict]:
        """同plan全部任务（依赖门禁/CANCELLED级联需要看兄弟任务状态）。"""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM tasks WHERE plan_id=? ORDER BY seq ASC", (plan_id,)
            ).fetchall()
            return [self._row_to_task(r) for r in rows]
        finally:
            con.close()

    @staticmethod
    def _unsatisfied_deps_on(con, plan_id: str, task_id: str, *,
                             shard_json: str | None = None,
                             strict: bool = False) -> list[dict]:
        """同连接内查依赖（必须在写事务里用同连接，避免嵌套连接与锁竞争）。"""
        if shard_json is None:
            row = con.execute(
                "SELECT shard_json FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            if row is None:
                raise StoreError(f"task_not_found:{plan_id}/{task_id}")
            shard_json = row[0]
        try:
            deps = json.loads(shard_json or "{}").get("depends_on") or []
        except ValueError:
            deps = []
        if not deps:
            return []
        sibs = {r["task_id"]: r["status"] for r in con.execute(
            "SELECT task_id,status FROM tasks WHERE plan_id=?", (plan_id,))}
        ok = {"DONE"} if strict else {"DONE", "SKIPPED", "SKIPPED_CACHED"}
        return [{"task_id": d, "status": sibs.get(d, "MISSING")}
                for d in deps if sibs.get(d) not in ok]

    def unsatisfied_dependencies(self, plan_id: str, task_id: str, *,
                                 strict: bool = False) -> list[dict]:
        """返回未满足的依赖（Phase0§2.3）。
        默认视 SKIPPED/SKIPPED_CACHED 为满足（任务被跳过不再挡路）；
        strict=True 时只认 DONE，让跳过也变下游阻塞（模板可开）。"""
        con = self._connect()
        try:
            return self._unsatisfied_deps_on(con, plan_id, task_id, strict=strict)
        finally:
            con.close()

    CANCELABLE_FROM_NOT_STARTED = ("PENDING", "BLOCKED", "READY")

    def cancel_cascade(self, plan_id: str, task_id: str, reason: str, *,
                       agent: str = "orchestrator", run_id: str = "",
                       start_version: int | None = None) -> list[dict]:
        """Phase0§2.3：取消上游级联取消**未启动**的下游。
        目标本身按跃迁表语义取消（RUNNING→CANCELLED 表里是合法边）；
        但级联只继续往下走未启动的（PENDING/BLOCKED/READY），
        已开跑的下游不强改——强改会丢现场，应走request_cancel协作中断。
        逐个走transition，每个都留审计行。"""
        cancelled = []
        seen = {task_id}
        # 目标：按表取消
        t = self.get_task(plan_id, task_id)
        if t is not None and t["status"] not in TERMINAL and t["status"] != "CANCELLED":
            payload = {"reason": reason}
            try:
                cancelled.append(self.transition(
                    plan_id, task_id, "CANCELLED", agent=agent, run_id=run_id,
                    expect_version=start_version if start_version is not None
                    else t["version"], payload=payload))
            except (Conflict, IllegalTransition):
                pass
        # 级联：只吃未启动下游
        frontier = list(self._dependents(plan_id, task_id))
        while frontier:
            tid = frontier.pop(0)
            if tid in seen:
                continue
            seen.add(tid)
            d = self.get_task(plan_id, tid)
            if d is None or d["status"] not in self.CANCELABLE_FROM_NOT_STARTED:
                continue          # 已启动/终态：不级联，留给协作中断
            try:
                cancelled.append(self.transition(
                    plan_id, tid, "CANCELLED", agent=agent, run_id=run_id,
                    expect_version=d["version"],
                    payload={"reason": reason, "cascaded_from": task_id}))
            except (Conflict, IllegalTransition):
                continue
            frontier.extend(self._dependents(plan_id, tid))
        return cancelled

    def _dependents(self, plan_id: str, task_id: str) -> list[str]:
        """谁依赖我（反向边）。"""
        con = self._connect()
        try:
            out = []
            for r in con.execute(
                "SELECT task_id,shard_json FROM tasks WHERE plan_id=?", (plan_id,)
            ).fetchall():
                try:
                    deps = json.loads(r["shard_json"] or "{}").get("depends_on") or []
                except ValueError:
                    deps = []
                if task_id in deps:
                    out.append(r["task_id"])
            return out
        finally:
            con.close()

    def record_schema_reject(self, plan_id: str, task_id: str, where: str,
                             error: str, *, agent: str = "system",
                             run_id: str = "", payload: dict | None = None) -> None:
        """Phase0§3：启动/派发/写回三处model_validate失败记SCHEMA_REJECT，不派发。
        独立事务写（同状态审计行，replay跳过），不依赖任务当前是否可跃迁。"""
        body = {"event": "SCHEMA_REJECT", "where": where, "error": error[:500],
                **(payload or {})}
        now = now_utc_iso()
        con = self._connect()
        try:
            row = con.execute(
                "SELECT status FROM tasks WHERE plan_id=? AND task_id=?",
                (plan_id, task_id),
            ).fetchone()
            s = row[0] if row else "UNKNOWN"
            self._write_lock.acquire()
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO events(ts,plan_id,task_id,from_s,to_s,run_id,"
                    "agent,payload) VALUES (?,?,?,?,?,?,?,?)",
                    (now, plan_id, task_id, s, s, run_id, agent,
                     json.dumps(body, ensure_ascii=False)),
                )
                con.commit()
            finally:
                self._write_lock.release()
        finally:
            con.close()
        jlog(self.root, "WARN", "schema_reject", plan_id=plan_id,
             task_id=task_id, where=where, error=error[:200])

    def alert(self, priority: str, kind: str, **fields) -> None:
        """Phase0§2.2 的P1告警通道：写events/ALERTS.jsonl（追加、不去重，
        去重属Phase3告警收敛），同时落结构化日志。"""
        row = {"ts": now_utc_iso(), "priority": priority, "kind": kind, **fields}
        append_jsonl(self.events_dir / "ALERTS.jsonl", row)
        jlog(self.root, "ERROR" if priority in ("P0", "P1") else "WARN",
             f"alert_{priority}", kind=kind, **{k: v for k, v in fields.items()})

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
                    locked = "locked" in str(e).lower()
                    if locked:
                        # 每次重试都留一笔：锁争用是运维要看的信号，
                        # 全部重试成功也不该在指标上消失（Phase1§5）
                        self.record_metric("db_lock_retry", 1,
                                           span=f"attempt{attempt}")
                    if locked and attempt < retries - 1:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    jlog(self.root, "ERROR", "db_write_failed",
                         error=str(e)[:500])
                    if locked:
                        # Phase1§5：重试耗尽要记metrics并告警
                        self.record_metric("db_lock_exhausted", 1,
                                           span=f"after{retries}retries")
                        self.alert("P1", "db_write_failed", error=str(e)[:300],
                                   retries=retries)
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
        """INSERT tasks(PENDING…)；idempotency_key冲突返回("already_exists", 旧行)；
        同(plan_id,task_id)但inputs_hash不同抛TaskIdConflict（输入变了，不是重投）。"""
        now = now_utc_iso()
        inputs_hash = shard.get("inputs_hash") or ""
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
                # 幂等键没命中但(plan_id,task_id)撞了：输入变了，不是重复投递。
                # 静默当already_exists会让"换了输入还跑旧任务"这种bug永远看不见。
                twin = con.execute(
                    "SELECT * FROM tasks WHERE plan_id=? AND task_id=?",
                    (row["plan_id"], row["task_id"]),
                ).fetchone()
                if twin is not None:
                    raise TaskIdConflict(
                        f"inputs_changed:{row['plan_id']}/{row['task_id']} "
                        f"old_inputs_hash={twin['inputs_hash']} "
                        f"new_inputs_hash={row['inputs_hash']}")
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
                   lease_op: str | None = None,
                   role: str = "orchestrator") -> dict:
        """唯一状态跃迁入口。lease_op∈{acquire,renew,revoke}时fencing+1（评审Minor2），
        普通跃迁只+version；RETRY→READY时attempts+1；其余attempts不变。

        role∈{orchestrator,subagent,verifier}：权限矩阵在store强制（Phase0§2.2），
        子Agent/Verifier越权写系统跃迁一律IllegalTransition+REJECTED。"""
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
            ok, why = role_allowed(role, s, to_s)
            if not ok:
                raise IllegalTransition(why)
            if v != expect_version:
                raise Conflict(f"version expect={expect_version} actual={v}")
            if expect_fencing is not None and f != expect_fencing:
                raise StaleOwner(f"fencing expect={expect_fencing} actual={f}")
            new_attempts = attempts
            new_switch = switch
            if to_s == "READY" and s == "RUNNING":  # 让出边（V7§2.9）
                # 主动让出只有持有当前租约的owner能写：必须回带fencing，
                # 否则脑裂时旧owner能让掉新owner正在跑的任务（Phase0§2.2）
                if expect_fencing is None:
                    raise IllegalTransition("yield_requires_fencing")
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
            if to_s == "RETRY" and not payload.get("reason"):
                # Phase0§2.3：重试必须带reason，否则事后无法判断该重试什么
                raise IllegalTransition("retry_requires_reason")
            if to_s == "DEAD_LETTER":
                # 死信必须带reason+last_error+人工SOP链接，否则无人知道怎么处理
                missing = [k for k in ("reason", "last_error", "sop_ref")
                           if not payload.get(k)]
                if missing:
                    raise IllegalTransition(f"dead_letter_missing:{missing}")
            if s == "RETRY" and to_s == "READY":
                new_attempts = attempts + 1  # +1只在此边，全程一次
            if to_s == "READY" and s in ("PENDING", "BLOCKED"):
                # Phase0§2.3：依赖未全终态不得进READY（否则日报DAG乱序派发）
                blocking = self._unsatisfied_deps_on(
                    con, plan_id, task_id, shard_json=shard_json,
                    strict=bool(payload.get("strict_depends")))
                if blocking:
                    raise IllegalTransition(
                        f"deps_unsatisfied:{[b['task_id'] for b in blocking]}")
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
            # Phase0§2.2：非法跃迁/越权是P1（有入队方在等这个状态）
            self.alert("P1", "illegal_transition", plan_id=plan_id,
                       task_id=task_id, to=to_s, role=role, error=str(e)[:300])
            raise
        except StaleOwner as e:
            self._record_rejected_outside(plan_id, task_id, to_s, agent,
                                          run_id, str(e), payload)
            jlog(self.root, "WARN", "stale_owner",
                 plan_id=plan_id, task_id=task_id, error=str(e)[:200])
            self.alert("P1", "stale_owner_write", plan_id=plan_id,
                       task_id=task_id, to=to_s, role=role, error=str(e)[:300])
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
        """从DB全量重建导出（含master）；返回任务数。
        master按plan_id分文件：多plan并存时（daily+多管道）后写的不能覆盖先写的。"""
        con = self._connect()
        try:
            tasks = [self._row_to_task(r) for r in con.execute(
                "SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (plan_id,))]
            plan = con.execute(
                "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            all_plans = [dict(r) for r in con.execute(
                "SELECT plan_id,template,status,created_at FROM plans"
                " ORDER BY plan_id ASC").fetchall()]
        finally:
            con.close()
        for t in tasks:
            self._export_task(t)
        body = {
            "plan_id": plan_id,
            "plan": dict(plan) if plan else None,
            "tasks": [{"task_id": t["task_id"], "status": t["status"],
                       "priority": t["priority"], "seq": t["seq"]} for t in tasks],
            "rebuilt_at": now_utc_iso(),
        }
        atomic_write_json(self.export_dir / "plans" / f"{plan_id}.json", body)
        # 索引文件：只列有哪些plan，各plan自己的细节在plans/{plan_id}.json
        atomic_write_json(self.export_dir / "master.json", {
            "rebuilt_at": body["rebuilt_at"],
            "plans": all_plans,
            "index": "plans/{plan_id}.json",
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
        """VACUUM INTO每日拷贝（上海自然日）；已存在则幂等返回（不覆盖当日备份）。
        VACUUM INTO要求目标不存在，故不能无条件重复调用。"""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"state-{shanghai_date()}.db"
        if dest.exists():
            jlog(self.root, "INFO", "backup_skipped_exists", path=str(dest))
            return dest
        con = self._connect()
        try:
            con.execute("VACUUM INTO ?", (str(dest),))
        finally:
            con.close()
        jlog(self.root, "INFO", "backup_created", path=str(dest))
        return dest

    def integrity_check(self) -> dict:
        """PRAGMA integrity_check + foreign/一致性快检（混沌后恢复演练用）。"""
        con = self._connect()
        try:
            row = con.execute("PRAGMA integrity_check;").fetchone()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            return {"integrity": row[0],
                    "tasks": con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                    "events": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "seq_max": con.execute(
                        "SELECT COALESCE(MAX(seq),0) FROM tasks").fetchone()[0]}
        finally:
            con.close()
