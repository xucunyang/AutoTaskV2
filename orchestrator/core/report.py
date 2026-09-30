"""报告层（Phase3 §3.1/§3.3 + 附录C）。

三层结构的核心是**只链不搬**：
  任务级 artifacts/{date}/{task}/tuning.md  自包含诊断（每行带说明列）
  plan级   reports/{plan_id}.summary.md      只做索引，一行一任务+链接
  日报     reports/YYYY-MM-DD.md             一屏总览+算力画像+弱建议+质量告警

为什么必须分层：把模型输出原样搬进汇总，汇总就会变成没人看的大文件。
日报要能在手机上一屏看完，所以它只放"要不要点进去看"的信息。

数据来源**只走DB聚合**，不读聊天记录——聊天记录不是审计依据。
时区按上海自然日切分：DB存UTC，展示+8。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.utils import atomic_write_json, now_utc_iso

SHANGHAI = timezone(timedelta(hours=8))
SHANGHAI_DATE = "%Y-%m-%d"
# 看板与调优阈值全部读config/report.yaml（设计§3.3：阈值可配不改代码）
DEFAULTS = {"top_n": 5, "slow_task_s": 120, "queue_block_p95_s": 60,
            "verify_rule_slow_s": 60, "export_slow_ms": 500,
            "high_prio_sla_s": 60,
            "tuning": {"execute_s_warn": 120, "llm_share_warn": 0.80,
                       "ctx_switch_warn": 3, "ctx_overflow_warn": 0,
                       "local_share_warn": 0.5, "source_tier_warn": 0.50}}


def load_cfg(root: str | Path) -> dict:
    try:
        import yaml
        raw = yaml.safe_load((Path(root) / "config" / "report.yaml")
                             .read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if k != "tuning"})
    cfg["tuning"] = {**DEFAULTS["tuning"], **(raw.get("tuning") or {})}
    return cfg


def shanghai_date(ts: str | None = None) -> str:
    if ts:
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(SHANGHAI).strftime(SHANGHAI_DATE)
        except ValueError:
            pass
    return datetime.now(SHANGHAI).strftime(SHANGHAI_DATE)


# ---------- 聚合（只读DB） ----------

def collect_plan(store, plan_id: str) -> dict:
    tasks = store.list_plan_tasks(plan_id)
    counts: dict[str, int] = defaultdict(int)
    rows = []
    for t in tasks:
        counts[t["status"]] += 1
        sh = t["shard"] or {}
        rows.append({
            "task_id": t["task_id"], "status": t["status"],
            "priority": t["priority"], "attempts": t["attempts"],
            "max_attempts": t["max_attempts"],
            "session_switch_total": t["session_switch_total"],
            "source": t["source"], "seq": t["seq"],
            "objective": sh.get("objective", "")[:200],
            "needs_web": sh.get("needs_web", False),
            "outputs": sh.get("outputs") or [],
            "depends_on": sh.get("depends_on") or [],
            "slice_rationale": sh.get("slice_rationale", ""),
            "enqueued_at": t["enqueued_at"], "updated_at": t["updated_at"],
            # 产物目录按"这条任务自己的入队日"分，不是plan的首条任务日：
            # 跨零点的plan里，后者会让后半夜的任务在报告里永远显示"缺四件套"。
            "artifacts_date": shanghai_date(t["enqueued_at"]),
        })
    metrics = _metrics_of(store, plan_id)
    return {"plan_id": plan_id, "date": shanghai_date(
        rows[0]["enqueued_at"] if rows else None),
        "total": len(rows), "counts": dict(counts), "tasks": rows,
        "metrics": metrics}


def _metrics_of(store, plan_id: str) -> dict:
    con = store._connect()
    try:
        rows = con.execute(
            "SELECT name, COUNT(*) AS n, AVG(value) AS avg, MAX(value) AS max"
            " FROM metrics WHERE plan_id=? GROUP BY name", (plan_id,)).fetchall()
    finally:
        con.close()
    return {r["name"]: {"n": r["n"], "avg": r["avg"], "max": r["max"]}
            for r in rows}


def queue_board(store, cfg: dict) -> dict:
    """队列看板：READY按priority分组/最老等待/调度延迟/老化次数/让出次数。"""
    ready = store.list_by_status("READY")
    by_prio: dict[int, int] = defaultdict(int)
    now = datetime.now(timezone.utc)
    oldest_s = 0.0
    for t in ready:
        by_prio[int(t["priority"])] += 1
        oldest_s = max(oldest_s, (now - _parse(t["updated_at"])).total_seconds())
    running = len(store.list_by_status("RUNNING"))
    switches = sum(t["session_switch_total"]
                   for t in store.list_plan_tasks_all()
                   if t["status"] not in ("DONE", "DEAD_LETTER", "CANCELLED"))
    dw = store.metric_stat("dispatch_wait")
    return {
        "ready_total": len(ready),
        "ready_by_priority": dict(sorted(by_prio.items())),
        "running": running,
        "oldest_wait_s": int(oldest_s),
        "queue_blocked": oldest_s > cfg["queue_block_p95_s"],
        "dispatch_wait_avg_s": round(dw.get("avg") or 0.0, 2),
        "session_switch_total": switches,
        "cache_hit": store.metric_stat("cache_hit").get("n", 0),
    }


def _parse(ts: str | None) -> datetime:
    try:
        dt = datetime.fromisoformat(ts or "")
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


# ---------- plan级 summary（只做索引） ----------

def render_plan_summary(store, plan_id: str, cfg: dict | None = None) -> str:
    cfg = cfg or load_cfg(store.root)
    d = collect_plan(store, plan_id)
    counts = d["counts"]
    failed = [t for t in d["tasks"] if t["status"] in ("FAILED", "DEAD_LETTER")]
    top_n = cfg["top_n"]
    slow = sorted([t for t in d["tasks"] if _elapsed_s(t) >= cfg["slow_task_s"]],
                  key=lambda t: -_elapsed_s(t))[:top_n]
    lines = [f"# Plan {plan_id} 摘要（{d['date']}）", "",
             f"- 任务 {d['total']}｜DONE {counts.get('DONE', 0)}"
             f"｜FAILED {counts.get('FAILED', 0)}"
             f"｜DEAD_LETTER {counts.get('DEAD_LETTER', 0)}"
             f"｜CACHED {counts.get('SKIPPED_CACHED', 0)}",
             f"- 重试总数 {sum(t['attempts'] for t in d['tasks'])}"
             f"｜session切换 {sum(t['session_switch_total'] for t in d['tasks'])}",
             ""]
    if failed:
        lines += ["## 失败（置顶）", ""]
        for t in failed:
            lines.append(f"- `{t['task_id']}` {t['status']} "
                         f"attempts={t['attempts']}/{t['max_attempts']} "
                         f"{t['objective'][:60]}")
        lines.append("")
    if slow:
        lines += [f"## 耗时 Top{len(slow)}", ""]
        for t in slow:
            lines.append(f"- `{t['task_id']}` {_elapsed_s(t):.0f}s {t['status']}")
        lines.append("")
    lines += ["## 任务分解视图", ""]
    for t in d["tasks"]:
        dep = ("← " + ",".join(t["depends_on"])) if t["depends_on"] else ""
        links = " ".join(f"[{Path(o).name}]({o})" for o in t["outputs"])
        lines.append(f"- `{t['task_id']}` [{t['status']}] {t['objective'][:40]} {dep}")
        if t["slice_rationale"]:
            lines.append(f"  - 拆分依据: {t['slice_rationale']}")
        if links:
            lines.append(f"  - 产物: {links}")
    lines += ["", "## 调研质量", ""]
    lines += _research_lines(store, d)
    lines += ["", "> 本文件只做索引；任务级调优诊断见各任务 tuning.md。"]
    return "\n".join(lines)


def _find_sources(store, task_id: str, date: str) -> Path | None:
    """找 {task_id}.sources.json。

    先按该任务自己的入队日找（正常路径）；找不到再退回 plan 的日期，
    最后扫最近几天。原因是产物目录按入队日分，而任务可能因重试/跨零点
    在别的日期补写——报告不该因为跨天就把已产出的四件套判成"缺"。
    """
    root = Path(store.root) / "artifacts"
    for cand in (root / date, root / shanghai_date()):
        p = cand / f"{task_id}.sources.json"
        if p.exists():
            return p
    for d in sorted((p for p in root.glob("*") if p.is_dir()),
                    reverse=True)[:7]:
        p = d / f"{task_id}.sources.json"
        if p.exists():
            return p
    return None


def _research_lines(store, d: dict) -> list[str]:
    rows = []
    for t in d["tasks"]:
        if not t["needs_web"]:
            continue
        src = _find_sources(store, t["task_id"],
                            t.get("artifacts_date") or d["date"])
        if src is None:
            rows.append(f"- `{t['task_id']}` 缺 sources.json（未产出四件套）")
            continue
        try:
            sources = json.loads(src.read_text(encoding="utf-8"))
        except ValueError:
            rows.append(f"- `{t['task_id']}` sources.json 解析失败")
            continue
        counted = [s for s in sources if str(s.get("tier_reason", "")).strip()]
        a = sum(1 for s in counted if s.get("tier") in ("A", None)
                and s.get("source_tier") == "A")
        ratio = a / len(counted) if counted else 0.0
        rows.append(f"- `{t['task_id']}` A类占比 {ratio:.0%}"
                    f"（{a}/{len(counted)}，无理由条目不计入）")
    return rows or ["- 本plan无needs_web任务"]


def _elapsed_s(t: dict) -> float:
    return max(0.0, (_parse(t["updated_at"]) - _parse(t["enqueued_at"])).total_seconds())


# ---------- 日报（一屏总览） ----------

def render_daily(store, date: str | None = None, cfg: dict | None = None) -> str:
    cfg = cfg or load_cfg(store.root)
    date = date or shanghai_date()
    plans = [p["plan_id"] for p in store.list_plans()
             if p["plan_id"] != "orchestrator_lock" and date in p["plan_id"]]
    if not plans:
        plans = [p["plan_id"] for p in store.list_plans()
                 if p["plan_id"] != "orchestrator_lock"][:1]
    agg = {"total": 0, "DONE": 0, "FAILED": 0, "DEAD_LETTER": 0,
           "SKIPPED_CACHED": 0, "retries": 0, "switches": 0}
    task_rows = []
    for pid in plans:
        d = collect_plan(store, pid)
        agg["total"] += d["total"]
        for k in ("DONE", "FAILED", "DEAD_LETTER", "SKIPPED_CACHED"):
            agg[k] += d["counts"].get(k, 0)
        agg["retries"] += sum(t["attempts"] for t in d["tasks"])
        agg["switches"] += sum(t["session_switch_total"] for t in d["tasks"])
        for t in d["tasks"]:
            task_rows.append((pid, t))
    board = queue_board(store, cfg)
    quality = _daily_quality(store, task_rows)
    lines = [f"# 日报 {date}", "",
             f"- 任务 {agg['total']}｜DONE {agg['DONE']}｜FAILED {agg['FAILED']}"
             f"｜死信 {agg['DEAD_LETTER']}｜缓存跳过 {agg['SKIPPED_CACHED']}",
             f"- 重试 {agg['retries']}｜session切换 {agg['switches']}"
             f"｜缓存命中 {board['cache_hit']}",
             "",
             "## 算力画像", "",
             f"- RUNNING {board['running']}｜READY {board['ready_total']}"
             f"（按优先级 {board['ready_by_priority']}）",
             f"- 最老等待 {board['oldest_wait_s']}s"
             f"{'（队列阻塞）' if board['queue_blocked'] else ''}",
             f"- 平均调度延迟 {board['dispatch_wait_avg_s']}s",
             "",
             "## 质量告警", ""]
    lines += quality or ["- 无"]
    lines += ["", "## 弱建议（调参）", ""]
    lines += _tuning_hints(agg, board, quality, cfg)
    lines += ["", "## 计划索引", ""]
    for pid in plans:
        lines.append(f"- [{pid}]({pid}.summary.md) → {collect_plan(store, pid)['total']} 个任务")
    lines += ["", "> 只链不搬：任务级诊断见各 tuning.md，汇总不复制模型输出。"]
    return "\n".join(lines)


def _daily_quality(store, task_rows) -> list[str]:
    out = []
    for pid, t in task_rows:
        if t["status"] == "DEAD_LETTER":
            out.append(f"- [{'P0'}] `{t['task_id']}` 进死信")
        if t["session_switch_total"] > 3:
            out.append(f"- `{t['task_id']}` session切换{t['session_switch_total']}次"
                       f"（拆分偏粗或窗口偏小）")
    return out


def _tuning_hints(agg, board, quality, cfg) -> list[str]:
    t = cfg["tuning"]
    hints = []
    if agg["switches"] > 0 and agg["switches"] >= t["ctx_switch_warn"]:
        hints.append(f"- session切换偏多（{agg['switches']}次）："
                     f"考虑把长任务拆更细，或调大模型context_window")
    if board["ready_total"] > 50:
        hints.append(f"- READY积压 {board['ready_total']}：考虑调高max_workers")
    if board["oldest_wait_s"] > cfg["high_prio_sla_s"]:
        hints.append(f"- 最老等待 {board['oldest_wait_s']}s 超高优SLA：查调度阻塞")
    if not hints:
        hints.append("- 无需调参")
    return hints


# ---------- Prometheus 文本（§3.1） ----------

def render_prometheus(store) -> str:
    """导出Prometheus文本格式供Grafana。只导数值型metrics，不导个人数据。"""
    con = store._connect()
    try:
        rows = con.execute(
            "SELECT name, plan_id, COUNT(*) AS n, SUM(value) AS total,"
            " AVG(value) AS avg, MAX(value) AS max FROM metrics"
            " GROUP BY name, plan_id ORDER BY name").fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        name = _safe_metric(r["name"])
        labels = f'plan_id="{_esc(r["plan_id"])}"'
        out.append(f"orchestrator_{name}_count{{{labels}}} {r['n']}")
        out.append(f"orchestrator_{name}_sum{{{labels}}} {r['total']:.4f}")
        out.append(f"orchestrator_{name}_avg{{{labels}}} {r['avg']:.4f}")
    for status, n in _status_counts(store).items():
        out.append(f'orchestrator_tasks_by_status{{status="{status}"}} {n}')
    out.append(f"orchestrator_queue_ready {_ready_count(store)}")
    return "\n".join(out) + "\n"


def _safe_metric(name: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(name))


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _status_counts(store) -> dict:
    con = store._connect()
    try:
        return {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()}
    finally:
        con.close()


def _ready_count(store) -> int:
    return len(store.list_by_status("READY"))


# ---------- 落盘 ----------

def write_plan_summary(store, plan_id: str, cfg: dict | None = None) -> dict:
    cfg = cfg or load_cfg(store.root)
    d = collect_plan(store, plan_id)
    out_dir = Path(store.root) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_plan_summary(store, plan_id, cfg)
    md_path = out_dir / f"{plan_id}.summary.md"
    md_path.write_text(md, encoding="utf-8")
    json_path = out_dir / f"{plan_id}.summary.json"
    atomic_write_json(json_path, d)
    return {"md": str(md_path), "json": str(json_path), "plan_id": plan_id,
            "tasks": d["total"]}


def write_daily(store, date: str | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or load_cfg(store.root)
    date = date or shanghai_date()
    out_dir = Path(store.root) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{date}.md"
    p.write_text(render_daily(store, date, cfg), encoding="utf-8")
    prom = out_dir / "metrics.prom"
    prom.write_text(render_prometheus(store), encoding="utf-8")
    return {"daily": str(p), "prom": str(prom), "date": date}
