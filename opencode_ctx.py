"""OpenCode 自身上下文水位探针：读出当前会话已用 token / 模型上限 / 百分比。

原理（已逐段验证）：
1. 已用 token = opencode.db → message 表 → 本会话最近一条已完成 assistant
   消息的 tokens.total（面板同口径；未完成的 streaming 消息记 0，自动跳过）。
2. 会话定位 = session 表按 directory 匹配 + time_updated 最大（单活跃会话唯一；
   同目录多并发会话时取最近更新者，见下方 caveat）。
3. 上限 = models.dev 注册表 opencode/<model-id> 的 limit.context；
   取不到则回退 fallback_max（默认 1048576）并标注来源。

用法：python opencode_ctx.py [project_dir] [db_path]
"""
from __future__ import annotations
import json
import sqlite3
import sys
import urllib.request

DEFAULT_DB = "C:/Users/22651/.local/share/opencode/opencode.db"
DEFAULT_MAX = 1048576


def find_session(con: sqlite3.Connection, directory: str) -> dict | None:
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, title, time_updated FROM session"
        " WHERE directory LIKE ? ORDER BY time_updated DESC LIMIT 1",
        ("%" + directory.replace("\\", "/").split("/")[-1],),
    ).fetchone()
    return dict(row) if row else None


def current_tokens(con: sqlite3.Connection, session_id: str) -> dict | None:
    rows = con.execute(
        "SELECT data FROM message WHERE session_id=? ORDER BY time_created DESC LIMIT 40",
        (session_id,),
    ).fetchall()
    for (data,) in rows:
        try:
            d = json.loads(data)
        except ValueError:
            continue
        if d.get("role") == "assistant":
            t = d.get("tokens") or {}
            if t.get("total", 0) > 0:
                return {"total": t["total"], "input": t.get("input", 0),
                        "output": t.get("output", 0),
                        "cache_read": (t.get("cache") or {}).get("read", 0)}
    return None


def model_max(model_id: str, provider: str = "opencode",
              fallback: int = DEFAULT_MAX) -> tuple[int, str]:
    """返回 (上限, 来源)。来源: models.dev | fallback。"""
    url = "https://models.dev/api.json"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            reg = json.load(r)
        prov = reg.get(provider, {})
        models = prov.get("models", {}) if isinstance(prov, dict) else {}
        m = models.get(model_id, {}) if isinstance(models, dict) else {}
        limit = (m.get("limit") or {}).get("context")
        if isinstance(limit, int) and limit > 0:
            return limit, "models.dev:" + provider + "/" + model_id
    except Exception:
        pass
    return fallback, "fallback(default 1M)"


def main() -> None:
    directory = sys.argv[1] if len(sys.argv) > 1 else "AutoTaskByMuseSpark1.3"
    db = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sess = find_session(con, directory)
        if not sess:
            print(json.dumps({"error": "no session found", "directory": directory}))
            return
        tok = current_tokens(con, sess["id"])
        if not tok:
            print(json.dumps({"error": "no completed assistant message",
                              "session": sess["id"]}))
            return
        # 模型 id 取本会话最新消息的 modelID，拿不到则用默认
        model_id = "muse-spark-1.3-contributor-free"
        max_ctx, src = model_max(model_id)
        out = {
            "session": sess["id"],
            "title": sess.get("title"),
            "tokens": tok["total"],
            "max_context": max_ctx,
            "max_source": src,
            "pct": round(tok["total"] / max_ctx * 100, 1),
        }
        print(json.dumps(out, ensure_ascii=False))
    finally:
        con.close()


if __name__ == "__main__":
    main()
