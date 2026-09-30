"""薄 tool loop 的工具集：受控、窄、可审计。

为什么自己写而不用 CC/opencode（用户 2026-09-29 决策）：
我们这个系统的价值恰恰在"可控的上下文 + 可审计的执行"——
session 让出、checkpoint 续跑、白名单沙箱。通用 CLI agent 是黑盒，
中途只能 kill，状态全丢，水位线和 usage 也拿不到。所以工具层必须
自己说了算，模型只负责"调哪个工具、参数是什么"。

设计原则：**给窄不给宽**
- 只暴露 4 个工具。多一个就多一份模型能犯的错，而现有 4 个已经覆盖
  "读—查—写—验"这条完整链路。
- 写操作只能落在 root 内，且走原子写。模型写的产物要能被 manifest
  验收（sha/bytes），半截文件会伪装成"已产出"。
- 命令只放行白名单前缀，shell=False、列表参数。给 shell 就是把整个
  机器交出去，验收里那条 shell=False 白名单会变成一句空话。
- 每个工具返回纯文本（含成功/失败），模型据此自己纠正，不抛异常打断
  循环——除了路径越界这种"必须停下"的情况。
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

# 命令白名单：只放行这些可执行文件的直接调用。
# 注意不含 shell/powershell/cmd——它们是任意代码执行的入口，
# 加进白名单等于把整个"沙箱"作废。
CMD_ALLOWLIST = ("python", "python3", "py", "pytest", "ruff", "node")

MAX_READ_LINES = 500
MAX_READ_CHARS = 40_000
MAX_WRITE_BYTES = 4 * 1024 * 1024
MAX_OUT_CHARS = 8_000
CMD_TIMEOUT_S = 120


class ToolDenied(RuntimeError):
    """越权（路径逃出root / 命令不在白名单）：必须停下，不能让模型重试绕过。"""


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_range",
            "description": (
                "分页读文件的一小段，避免把大文件整个塞进上下文。"
                "先读头部定位，再按 offset 续读。文件不存在返回空列表。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "相对项目根目录的路径"},
                    "offset": {"type": "integer", "description": "起始行号，从0开始"},
                    "limit": {"type": "integer",
                              "description": f"最多读几行（上限{MAX_READ_LINES}）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "联网检索并返回带出处的结果列表。needs_web 的任务用它取证。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索式"},
                    "max_results": {"type": "integer", "description": "最多几条"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "atomic_write",
            "description": (
                "把内容写入文件（原子写：先写临时文件再改名，"
                "所以不会留下半截文件）。路径必须相对项目根，"
                "不能带 ../ 。任务卡里声明的产物必须用这个工具落盘。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对项目根的写入路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_cmd",
            "description": (
                f"在沙箱里跑一条只读性质的命令并返回输出。只放行 "
                f"{'/'.join( CMD_ALLOWLIST )} 这些可执行文件，"
                "不经 shell，不能用管道/重定向/&&。典型用途：跑测试、"
                "跑 linter、确认自己写的代码能不能跑。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "命令，如 pytest tests/ -q"},
                },
                "required": ["cmd"],
            },
        },
    },
]


def _safe_path(root: Path, rel: str) -> Path:
    """把模型给的相对路径解析到 root 内。越界就拒绝。

    必须用 resolve() 之后再判 is_relative_to：直接拼字符串判前缀会被
    "../" 和符号链接绕过（artifacts/../secrets 看起来还在 root 下）。

    空路径不是越权，是模型忘了填参数——抛 ToolDenied 会把整个循环打断，
    而这类错误模型改一次参数就能纠正，应该软失败让它自己改。
    """
    if not rel or not str(rel).strip():
        raise ValueError("empty_path")
    p = Path(str(rel).strip())
    if p.is_absolute():
        raise ToolDenied(f"absolute_path_not_allowed:{rel}")
    target = (root / p).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        raise ToolDenied(f"path_escapes_root:{rel}") from None
    return target


def _truncate(s: str, n: int = MAX_OUT_CHARS) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n] + f"\n...[已截断，共{len(s)}字符]"


class ToolBox:
    """工具执行器。绑一个 root 和可选的检索函数。

    search_fn 为 None 时 search 工具直接回报"未配置检索"，
    而不是静默返回空列表——空列表会被模型读成"网上没有这资料"，
    然后它会拿这个假结论去写报告。
    """

    def __init__(self, root: str | Path, search_fn=None, *, cwd: str | Path | None = None):
        self.root = Path(root).resolve()
        self.search_fn = search_fn
        self.cwd = Path(cwd) if cwd else self.root
        self.calls: list[dict] = []          # 审计：谁在什么时候调了什么

    def schemas(self) -> list[dict]:
        return TOOL_SCHEMAS

    def execute(self, name: str, args: dict) -> str:
        fn = {
            "read_range": self._read_range,
            "search": self._search,
            "atomic_write": self._atomic_write,
            "run_cmd": self._run_cmd,
        }.get(name)
        if fn is None:
            return f"ERROR unknown_tool:{name}（可用：read_range/search/atomic_write/run_cmd）"
        try:
            out = fn(args or {})
        except ToolDenied as e:
            self.calls.append({"tool": name, "args": _safe_args(args), "error": str(e)})
            raise
        except ValueError as e:
            # 参数不合法（空路径等）：软失败。模型改一次参数就能纠正，
            # 不该像越权那样把整个循环打断。
            out = f"ERROR bad_args:{e}"
        except Exception as e:      # noqa: BLE001
            out = f"ERROR {type(e).__name__}:{e}"
        self.calls.append({"tool": name, "args": _safe_args(args),
                           "result_len": len(out)})
        return out

    # ---- 读 ----
    def _read_range(self, a: dict) -> str:
        p = _safe_path(self.root, str(a.get("path", "")))
        if not p.exists() or not p.is_file():
            return f"ERROR file_not_found:{a.get('path')}"
        offset = max(0, int(a.get("offset") or 0))
        limit = min(MAX_READ_LINES, max(1, int(a.get("limit") or 200)))
        lines: list[str] = []
        size = 0
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i < offset:
                    continue
                if len(lines) >= limit:
                    break
                lines.append(line.rstrip("\n"))
                size += len(line)
                if size >= MAX_READ_CHARS:
                    lines.append("...[单次读取已达字符上限]")
                    break
        if not lines:
            return f"(空：offset={offset} 已在文件末尾，共{_count_lines(p)}行)"
        return "\n".join(lines)

    # ---- 查 ----
    def _search(self, a: dict) -> str:
        if self.search_fn is None:
            return ("ERROR search_unavailable:未配置检索工具。"
                    "不要凭记忆写事实，只能用任务卡里已给的信息。")
        q = str(a.get("query") or "").strip()
        if not q:
            return "ERROR empty_query"
        import json as _json
        res = self.search_fn(q, max_results=int(a.get("max_results") or 5))
        return _truncate(_json.dumps(res, ensure_ascii=False, default=str))

    # ---- 写 ----
    def _atomic_write(self, a: dict) -> str:
        target = _safe_path(self.root, str(a.get("path", "")))
        content = a.get("content")
        if not isinstance(content, str):
            return "ERROR content_must_be_string"
        raw = content.encode("utf-8")
        if len(raw) > MAX_WRITE_BYTES:
            return f"ERROR too_large:{len(raw)}bytes（上限{MAX_WRITE_BYTES}）"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes(raw)
            tmp.replace(target)      # 同一卷内改名是原子的
        except OSError as e:
            tmp.unlink(missing_ok=True)
            return f"ERROR write_failed:{e}"
        return f"OK wrote {a.get('path')} ({len(raw)}bytes)"

    # ---- 跑 ----
    def _run_cmd(self, a: dict) -> str:
        raw = str(a.get("cmd") or "").strip()
        if not raw:
            return "ERROR empty_cmd"
        # 不经 shell：管道/重定向/&&/子命令都当普通参数，不会被解释
        try:
            argv = shlex.split(raw, posix=(sys.platform != "win32"))
        except ValueError as e:
            return f"ERROR bad_quoting:{e}"
        if not argv:
            return "ERROR empty_cmd"
        exe = Path(argv[0]).name.lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe not in CMD_ALLOWLIST:
            return (f"ERROR cmd_not_allowed:{argv[0]}（白名单："
                    f"{', '.join(CMD_ALLOWLIST)}）")
        for bad in ("|", "&&", "||", ";", ">", "<", "`", "$("):
            if bad in raw:
                return f"ERROR shell_operator_not_allowed:{bad}"
        try:
            r = subprocess.run(argv, cwd=self.cwd, shell=False,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=CMD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return f"ERROR timeout_after_{CMD_TIMEOUT_S}s"
        except FileNotFoundError:
            return f"ERROR executable_not_found:{argv[0]}"
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return _truncate(f"exit={r.returncode}\n{out}".strip())


def _count_lines(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _safe_args(args: dict) -> dict:
    """审计日志里不记全文（可能是几十万字），只记形状。"""
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[k] = s if len(s) <= 120 else f"<{len(s)}chars>"
    return out


def tool_spec_prompt() -> str:
    """给不支持原生 tool_calls 的模型的降级说明（文本协议）。"""
    return (
        "可用工具（用JSON调用，不要只描述意图）：\n"
        '{"tool":"read_range","path":"...","offset":0,"limit":200}\n'
        '{"tool":"search","query":"..."}\n'
        '{"tool":"atomic_write","path":"artifacts/x.md","content":"..."}\n'
        '{"tool":"run_cmd","cmd":"pytest tests/ -q"}\n'
        "一次只调一个；写完产物再结束。"
    )
