"""通用工具：UTC时间、哈希、原子写、结构化日志、.env加载。"""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def load_dotenv(root: str | Path = ".", filename: str = ".env",
                override: bool = False) -> dict:
    """读 .env 到 os.environ（不引第三方依赖）。

    为什么不用纯环境变量：换设备时环境变量要重设一遍，.env 跟着仓库走更省事。
    优先级：**真实环境变量 > .env**（不override），
    这样 CI/临时调试仍可用环境变量盖过文件里的值。

    查找两处（后者优先）：`orchestrator/.env`（配置与代码同侧）与仓库根 `.env`。
    .env 本身在 .gitignore 里，.env.example 入库当模板。
    """
    p = Path(root)
    candidates = [p / filename]
    if p.name == "orchestrator":
        candidates.append(p.parent / filename)
    out: dict[str, str] = {}
    for path in reversed(candidates):        # 先根后orchestrator，后者覆盖
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if not key:
                continue
            out[key] = val
            if override or key not in os.environ:
                os.environ[key] = val
    return out

def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha256_file(path: str | Path, limit_preview: bool = False):
    h = hashlib.sha256()
    size = 0
    preview: list[str] = []
    p = Path(path)
    with p.open("rb") as f:
        # 流式读，避免大文件进内存
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    # preview只读前5行文本
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= 5:
                    break
                preview.append(line.rstrip("\n")[:500])
    except Exception:
        pass
    rows = None
    if p.suffix == ".csv":
        try:
            with p.open("r", encoding="utf-8", errors="ignore") as f:
                rows = max(0, sum(1 for _ in f) - 1)
        except Exception:
            rows = None
    return {"sha256": h.hexdigest(), "bytes": size, "rows": rows, "preview": preview}

def atomic_write_json(path: str | Path, obj: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def atomic_write_text(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

def append_jsonl(path: str | Path, obj: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def jlog(root: str | Path, level: str, msg: str, **kw) -> None:
    append_jsonl(Path(root) / "logs" / "structured.jsonl",
                 {"ts": now_utc_iso(), "level": level, "msg": msg, **kw})
