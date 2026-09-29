"""入队：CLI + inbox目录投递（Phase1§2.4）。

两条入口同一事务语义（INSERT tasks(PENDING) 单事务）：
  1. CLI：`python -m core.enqueue --file pipeline_task.json --priority 0`
  2. inbox：扫描 `inbox/*.json` → `inbox/processing/` → `done/`（成功）/`error/`（失败）
失败一律走error目录并告警，原文件不丢（M1：输入永不丢）。
入队成功 touch `wakeup.flag`（M2：不等下一轮cron立刻叫主循环）。
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from core.store import Store, AlreadyExists, TaskIdConflict
from core.utils import jlog
from schemas.models import TaskShard

MAX_FILE_BYTES = 100 * 1024          # M9：单任务JSON <100KB
TASK_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
RATE_LIMIT_PER_HOUR = 100             # M9：单实例入队速率上限
SHELL_METACHARS = set(";|&`$><\n\r")  # 禁shell元字符（防注入）
# Phase0§4：cmd只允许白名单前缀，禁任意命令（执行时另需shell=False，见Phase3沙箱）
ALLOWED_CMD_PREFIXES = ("pytest tests/", "python tests/", "python -m pytest tests/")
# business_rule.expr 允许的AST节点（M9：表达式注入防护，白名单而非黑名单）
# 刻意不含 ast.Call/ast.Attribute/ast.Lambda：任何调用或属性链都可能是代码执行
ALLOWED_EXPR_NODES = (ast.Expression, ast.Compare, ast.BoolOp, ast.Name,
                      ast.Constant, ast.Load, ast.And, ast.Or, ast.Not,
                      ast.USub, ast.UAdd, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
                      ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
                      ast.List, ast.Tuple, ast.Dict, ast.Subscript)
ARTIFACT_PATH_RE = re.compile(r"^artifacts/\d{4}-\d{2}-\d{2}/[A-Za-z0-9._\-/]*$")


class EnqueueError(Exception):
    """入队硬校验失败（不可重试，进error目录）。"""


class RateLimited(EnqueueError):
    """速率超限（可退避重试）。"""


def _check_expr(expr: str, where: str) -> None:
    """AST白名单校验：只允许数据比较，不允许函数调用/属性链/导入。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise EnqueueError(f"bad_expr_syntax:{where}:{e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_EXPR_NODES):
            raise EnqueueError(f"bad_expr_node:{where}:{type(node).__name__}")


def validate(raw: bytes, *, root: Path, now: float | None = None) -> dict:
    """全部硬门禁；返回校验后的shard dict。任一不过抛EnqueueError。"""
    if len(raw) > MAX_FILE_BYTES:
        raise EnqueueError(f"file_too_large:{len(raw)}>{MAX_FILE_BYTES}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise EnqueueError(f"bad_json:{e}") from e
    if not isinstance(data, dict):
        raise EnqueueError("not_an_object")

    # 0) 补齐入队通道的固定字段：status由入队决定（PENDING），
    #    idempotency_key 缺省按 {plan_id}:{task_id} 派生（与 scheduler.expand_template 同规则），
    #    否则调用方不写这两个字段就永远过不了Pydantic，模板也不必重复写。
    data.setdefault("status", "PENDING")
    data.setdefault("plan_id", "pipeline")
    if not data.get("idempotency_key"):
        # Phase0§3：idempotency_key={plan_id}/{task_id}/{inputs_hash全hash}
        # （禁截断8位，防碰撞）。有inputs就带上内容哈希：
        # 同样内容重复投递→同键→幂等命中；内容变了→不同键→触发TaskIdConflict
        # 而不是静默复用旧任务（有inputs_hash但键里不含它=换了输入还跑旧数据）。
        ih = data.get("inputs_hash") or ""
        if not ih and data.get("inputs"):
            ih = hashlib.sha256(json.dumps(data["inputs"], sort_keys=True,
                                            ensure_ascii=False).encode("utf-8")).hexdigest()
        data["idempotency_key"] = (f"{data['plan_id']}/{data.get('task_id', '')}"
                                   f"/{ih}" if ih
                                   else f"{data['plan_id']}/{data.get('task_id', '')}")

    # 1) task_id 形态（M9）
    tid = data.get("task_id", "")
    if not TASK_ID_RE.match(str(tid)):
        raise EnqueueError(f"bad_task_id:{tid}")
    # 2) 路径逃逸：所有path必须在 artifacts/{YYYY-MM-DD}/ 之下（防 ../ 与绝对路径）
    for rule in data.get("acceptance", []) or []:
        if not isinstance(rule, dict):
            raise EnqueueError("bad_acceptance_rule")
        p = rule.get("path")
        if p is not None:
            _check_path(p)
        cmd = rule.get("cmd")
        if cmd is not None:
            if SHELL_METACHARS & set(str(cmd)):
                raise EnqueueError(f"shell_metachar_in_cmd:{cmd[:60]}")
            stripped = str(cmd).strip()
            if stripped.startswith("-"):
                raise EnqueueError("cmd_option_injection")
            if not stripped.startswith(ALLOWED_CMD_PREFIXES):
                raise EnqueueError(f"cmd_not_whitelisted:{stripped[:60]}")
        schema_ref = rule.get("schema_ref")
        if schema_ref is not None:
            _check_path(str(schema_ref))
    # 3) business_rule.expr AST白名单（M9）
    for rule in data.get("acceptance", []) or []:
        if isinstance(rule, dict) and rule.get("type") == "business_rule":
            if not rule.get("expr"):
                raise EnqueueError("business_rule_without_expr")
            _check_expr(str(rule["expr"]), rule.get("rule_id") or "rule")
    # 4) needs_web 必带 freshness（V7；TaskShard validator再兜一层）
    if data.get("needs_web") and str(data.get("freshness", "none")) == "none":
        raise EnqueueError("needs_web_requires_freshness")
    # 5) Pydantic 契约
    try:
        shard = TaskShard(**data)
    except Exception as e:
        raise EnqueueError(f"schema_invalid:{str(e)[:300]}") from e
    # 6) 速率限制（M9：入队速率上限，防inbox灌爆）
    _check_rate_limit(root, now=now)
    return shard.model_dump(mode="json")


def _check_path(p: str) -> None:
    """只允许 artifacts/YYYY-MM-DD/ 下的相对路径；挡 ../、绝对路径、盘符。"""
    s = str(p).replace("\\", "/")
    if s.startswith("/") or re.match(r"^[A-Za-z]:", s):
        raise EnqueueError(f"absolute_path_not_allowed:{p}")
    if ".." in s.split("/"):
        raise EnqueueError(f"path_traversal:{p}")
    if not ARTIFACT_PATH_RE.match(s):
        raise EnqueueError(f"path_outside_daily_dir:{p}")


def _check_rate_limit(root: Path, *, now: float | None = None) -> None:
    """按 events/enqueue_log.jsonl 统计最近1小时入队数（跨进程可见，单写仍走Store）。"""
    now = now if now is not None else time.time()
    log = root / "events" / "enqueue_log.jsonl"
    if not log.exists():
        return
    cutoff = now - 3600
    count = 0
    with log.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if float(row.get("ts_epoch", 0)) >= cutoff:
                count += 1
    if count >= RATE_LIMIT_PER_HOUR:
        raise RateLimited(f"rate_limited:{count}/{RATE_LIMIT_PER_HOUR}per_hour")


def _append_rate_log(root: Path, plan_id: str, task_id: str) -> None:
    from core.utils import append_jsonl
    append_jsonl(root / "events" / "enqueue_log.jsonl",
                 {"ts_epoch": time.time(), "plan_id": plan_id,
                  "task_id": task_id})


def touch_wakeup(root: Path) -> None:
    """M2：入队即叫主循环，不等下一轮cron。"""
    p = root / "wakeup.flag"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()), encoding="utf-8")


def enqueue_file(store: Store, path: str | Path, *, priority: int | None = None,
                 plan_id: str | None = None) -> tuple[str, dict]:
    """读文件 → 硬校验 → 单事务INSERT。
    返回("inserted", 新行) 或 ("already_exists", 旧行)（幂等重投）。"""
    root = Path(store.root)
    p = Path(path)
    raw = p.read_bytes()
    data = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(data, dict):
        raise EnqueueError("not_an_object")
    if priority is not None:
        data["priority"] = priority
    if plan_id is not None:
        data["plan_id"] = plan_id
    data.setdefault("plan_id", "pipeline")
    # 重新序列化再过一次校验（priority/plan_id可能被CLI覆盖；status/idempotency_key
    # 由validate()统一补齐，调用方不必重复写）
    try:
        shard = validate(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                         root=root)
    except EnqueueError as e:
        # Phase0§3：校验失败记SCHEMA_REJECT（where=ingress），不派发
        store.record_schema_reject(str(data.get("plan_id", "?")),
                                   str(data.get("task_id", "?")), "ingress", str(e))
        raise
    try:
        status, row = store.insert_task(shard)
    except AlreadyExists as e:
        return "already_exists", e.task
    except TaskIdConflict as e:
        store.record_schema_reject(shard["plan_id"], shard["task_id"],
                                   "ingress", str(e))
        raise EnqueueError(str(e)) from e
    if status == "created":
        _append_rate_log(root, shard["plan_id"], shard["task_id"])
        touch_wakeup(root)
        jlog(root, "INFO", "enqueued", plan_id=shard["plan_id"],
             task_id=shard["task_id"], priority=shard["priority"],
             source=shard["source"])
        status = "inserted"
    return status, row


def scan_inbox(store: Store, inbox: str | Path | None = None) -> dict:
    """扫inbox/*.json：processing→(done|error)。返回各目录计数。"""
    root = Path(store.root)
    inbox = Path(inbox) if inbox else root / "inbox"
    for sub in ("processing", "done", "error"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    counts = {"seen": 0, "inserted": 0, "already_exists": 0, "error": 0}
    for src in sorted(inbox.glob("*.json")):
        counts["seen"] += 1
        proc = inbox / "processing" / src.name
        try:
            shutil.move(str(src), str(proc))   # 先占有，避免并发重复拾取
        except OSError as e:                   # 已有同名processing（上轮残留）
            counts["error"] += 1
            jlog(root, "WARN", "inbox_claim_failed", file=src.name,
                 error=str(e)[:200])
            continue
        try:
            status, _row = enqueue_file(store, proc)
            counts[status] += 1                # inserted / already_exists 都进done
            shutil.move(str(proc), str(inbox / "done" / src.name))
        except EnqueueError as e:
            counts["error"] += 1
            shutil.move(str(proc), str(inbox / "error" / src.name))
            jlog(root, "WARN", "enqueue_rejected", file=src.name, error=str(e)[:300])
        except Exception as e:               # DB/IO等意外错误也不吞
            counts["error"] += 1
            shutil.move(str(proc), str(inbox / "error" / src.name))
            jlog(root, "ERROR", "enqueue_failed", file=src.name, error=str(e)[:300])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="core.enqueue")
    ap.add_argument("--file", required=True)
    ap.add_argument("--priority", type=int, default=None)
    ap.add_argument("--plan-id", default=None)
    args = ap.parse_args(argv)
    store = Store(Path(__file__).resolve().parent.parent)
    try:
        status, row = enqueue_file(store, args.file, priority=args.priority,
                                   plan_id=args.plan_id)
    except EnqueueError as e:
        print(f"REJECTED: {e}")
        return 2
    print(json.dumps({"status": status, "plan_id": row["plan_id"],
                      "task_id": row["task_id"], "priority": row["priority"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
