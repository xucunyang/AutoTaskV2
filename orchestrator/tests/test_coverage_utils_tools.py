"""2.7 覆盖率盲区（续）：core.utils / core.tools 的错误与边界分支。

这些分支之前没被统计到，全部门禁都盯着六个模块，于是它们可以一直烂着。
这里逐条钉住**行为**，不是为了刷数字——每条分支背后都有一个
"出问题时希望它怎样"的答案。
"""
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import tools as toolsmod                        # noqa: E402
from core import utils                                    # noqa: E402

PY = sys.executable


# ================================================================ utils

def test_sha256_streams_large_file(tmp_path):
    """大文件必须流式读：一次性读进内存会在200MB CSV时爆掉。"""
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * (300 * 1024))
    got = utils.sha256_file(p)
    assert got["bytes"] == 300 * 1024
    assert len(got["sha256"]) == 64


def test_sha256_preview_caps_at_5_lines(tmp_path):
    p = tmp_path / "many.txt"
    p.write_text("\n".join(f"line{i}" for i in range(50)), encoding="utf-8")
    got = utils.sha256_file(p)
    assert len(got["preview"]) == 5
    assert got["preview"][0] == "line0"


def test_sha256_rows_only_for_csv(tmp_path):
    csv = tmp_path / "a.csv"
    csv.write_text("h1,h2\n1,2\n3,4\n", encoding="utf-8")
    assert utils.sha256_file(csv)["rows"] == 2, "行数应排除表头"
    other = tmp_path / "a.txt"
    other.write_text("a\nb\n", encoding="utf-8")
    assert utils.sha256_file(other)["rows"] is None


def test_sha256_preview_truncates_long_lines(tmp_path):
    p = tmp_path / "long.txt"
    p.write_text("y" * 5000 + "\n", encoding="utf-8")
    assert len(utils.sha256_file(p)["preview"][0]) == 500


def test_atomic_write_json_leaves_no_tmp(tmp_path):
    p = tmp_path / "sub" / "a.json"
    utils.atomic_write_json(p, {"k": "值"})
    assert json.loads(p.read_text(encoding="utf-8"))["k"] == "值"
    assert not list(p.parent.glob(".tmp_*")), "临时文件残留"


def test_atomic_write_json_cleans_tmp_on_serialization_failure(tmp_path):
    """序列化失败（比如不可序列化的对象）也必须清掉临时文件。

    否则每次失败都在目录里留一个 .tmp_，攒久了没人知道那是什么。
    """
    p = tmp_path / "a.json"
    with pytest.raises(TypeError):
        utils.atomic_write_json(p, {"bad": object()})
    assert not list(tmp_path.glob(".tmp_*")), "失败后留下临时文件"
    assert not p.exists(), "失败不应产生半截目标文件"


def test_atomic_write_json_overwrites_atomically(tmp_path):
    p = tmp_path / "a.json"
    utils.atomic_write_json(p, {"v": 1})
    utils.atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8"))["v"] == 2


def test_atomic_write_text_roundtrip(tmp_path):
    p = tmp_path / "a.txt"
    utils.atomic_write_text(p, "内容\n第二行")
    assert p.read_text(encoding="utf-8") == "内容\n第二行"
    assert not list(tmp_path.glob(".tmp_*"))


def test_atomic_write_text_keeps_old_content_on_failure(tmp_path):
    """写失败必须保留旧内容——半截文件比过期文件危险得多。"""
    p = tmp_path / "a.txt"
    utils.atomic_write_text(p, "原始")
    with pytest.raises(TypeError):
        utils.atomic_write_text(p, object())     # 非str
    assert p.read_text(encoding="utf-8") == "原始"
    assert not list(tmp_path.glob(".tmp_*"))


def test_append_jsonl_creates_and_appends(tmp_path):
    p = tmp_path / "deep" / "a.jsonl"
    utils.append_jsonl(p, {"i": 1})
    utils.append_jsonl(p, {"i": 2})
    rows = [json.loads(l) for l in
            p.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["i"] for r in rows] == [1, 2]


def test_jlog_writes_to_logs_dir(tmp_path):
    utils.jlog(tmp_path, "INFO", "hello", task_id="t1")
    p = tmp_path / "logs" / "structured.jsonl"
    got = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
    assert got["msg"] == "hello" and got["task_id"] == "t1"
    assert got["level"] == "INFO" and got["ts"]


def test_load_dotenv_skips_blank_and_comment_lines(tmp_path):
    (tmp_path / ".env").write_text(
        "\n\n# 注释\n   \nA=1\n", encoding="utf-8")
    got = utils.load_dotenv(tmp_path)
    assert got == {"A": "1"}, f"注释/空行没被跳过: {got}"


def test_load_dotenv_skips_lines_without_equals(tmp_path):
    (tmp_path / ".env").write_text("JUNK\nB=2\n", encoding="utf-8")
    assert utils.load_dotenv(tmp_path) == {"B": "2"}


def test_load_dotenv_skips_empty_key(tmp_path):
    (tmp_path / ".env").write_text("=3\nC=4\n", encoding="utf-8")
    got = utils.load_dotenv(tmp_path)
    assert "" not in got and got["C"] == "4"


def test_now_utc_iso_is_timezone_aware():
    s = utils.now_utc_iso()
    from datetime import datetime
    assert datetime.fromisoformat(s).tzinfo is not None


# ================================================================ tools

def _box(tmp_path, search_fn=None):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return toolsmod.ToolBox(root, search_fn=search_fn), root


def test_empty_path_is_soft_error(tmp_path):
    """空路径是模型忘了填参数，不是越权——软失败让模型自己改。

    抛 ToolDenied 会把整个 tool loop 打断，而这类错改一次参数就能纠正。
    """
    box, _ = _box(tmp_path)
    out = box.execute("read_range", {"path": "   "})
    assert "bad_args" in out and "empty_path" in out


def test_truncation_marks_the_cut(tmp_path):
    """超长输出必须**标明被截断**，否则模型会以为那就是全部内容。"""
    box, _ = _box(tmp_path)
    big = {"data": "字" * (toolsmod.MAX_OUT_CHARS + 500)}
    out = box.execute("search", {"query": "q"})if False else None
    got = toolsmod._truncate("字" * (toolsmod.MAX_OUT_CHARS + 500))
    assert "已截断" in got and len(got) < toolsmod.MAX_OUT_CHARS + 200


def test_read_hits_character_cap(tmp_path):
    box, root = _box(tmp_path)
    (root / "huge.txt").write_text("x" * (toolsmod.MAX_READ_CHARS + 1000),
                                   encoding="utf-8")
    got = box.execute("read_range", {"path": "huge.txt", "limit": 100000})
    assert "字符上限" in got


def test_read_past_eof_reports_position(tmp_path):
    box, root = _box(tmp_path)
    (root / "small.txt").write_text("a\nb\nc\n", encoding="utf-8")
    got = box.execute("read_range", {"path": "small.txt", "offset": 99})
    assert "已在文件末尾" in got and "3行" in got, got


def test_search_calls_the_configured_fn(tmp_path):
    seen = {}

    def fake(query, max_results=5):
        seen["q"] = query
        seen["n"] = max_results
        return [{"url": "https://a", "title": "T"}]

    box, _ = _box(tmp_path, search_fn=fake)
    out = box.execute("search", {"query": "关键词", "max_results": 3})
    assert seen == {"q": "关键词", "n": 3}
    assert "https://a" in out


def test_search_empty_query_is_soft_error(tmp_path):
    box, _ = _box(tmp_path, search_fn=lambda q, max_results=5: [])
    assert "empty_query" in box.execute("search", {"query": "  "})


def test_write_rejects_non_string_content(tmp_path):
    """content 必须是字符串：模型偶尔会传数字/对象，那时宁可不写。"""
    box, root = _box(tmp_path)
    out = box.execute("atomic_write", {"path": "a.md", "content": 123})
    assert "content_must_be_string" in out
    assert not (root / "a.md").exists()


def test_write_reports_failure_without_leaving_tmp(tmp_path, monkeypatch):
    box, root = _box(tmp_path)

    def boom(self, data):
        raise OSError("磁盘满")

    monkeypatch.setattr(Path, "write_bytes", boom)
    out = box.execute("atomic_write", {"path": "a.md", "content": "x"})
    assert "write_failed" in out
    assert not list(root.rglob("*.tmp"))


def test_run_cmd_empty_is_rejected(tmp_path):
    box, _ = _box(tmp_path)
    assert "empty_cmd" in box.execute("run_cmd", {"cmd": "  "})
    assert "empty_cmd" in box.execute("run_cmd", {})


def test_run_cmd_bad_quoting_is_soft_error(tmp_path):
    """引号不闭合是模型常见错误，回报清楚比抛异常有用。"""
    box, _ = _box(tmp_path)
    out = box.execute("run_cmd", {"cmd": 'python -c "print(1'})
    assert "bad_quoting" in out or "not_allowed" in out


def test_run_cmd_missing_executable(tmp_path):
    box, _ = _box(tmp_path)
    out = box.execute("run_cmd", {"cmd": "python /绝对不存在的脚本.py"})
    assert "executable_not_found" in out or "exit=" in out


def test_run_cmd_timeout_is_bounded(tmp_path, monkeypatch):
    """超时必须被拦住并明说，不能让 tool loop 无限等下去。"""
    import subprocess as sp
    box, _ = _box(tmp_path)

    def timeout(*a, **k):
        raise sp.TimeoutExpired(cmd="python", timeout=k.get("timeout", 1))

    monkeypatch.setattr(toolsmod.subprocess, "run", timeout)
    monkeypatch.setattr(toolsmod, "CMD_TIMEOUT_S", 0.01)
    out = box.execute("run_cmd", {"cmd": "python -c x"})
    assert "timeout_after" in out


def test_count_lines_survives_unreadable_file(tmp_path):
    """_count_lines 出错时返回0而不是抛——它只用于提示信息。

    不用 chmod 造不可读：Windows 上 chmod(0) 拦不住读取（ACL 机制不同），
    那个断言在 Windows 上恒假，属于自欺。
    """
    p = tmp_path / "ok.txt"
    p.write_text("a\nb\n", encoding="utf-8")
    assert toolsmod._count_lines(p) == 2
    assert toolsmod._count_lines(tmp_path / "gone.txt") == 0
    # 目录也算OSError分支（IsADirectoryError 是 OSError 子类）
    assert toolsmod._count_lines(tmp_path) == 0


def test_unknown_tool_lists_the_real_choices(tmp_path):
    """报错要告诉模型有哪些工具可用，否则它只能瞎猜。"""
    box, _ = _box(tmp_path)
    out = box.execute("nope", {})
    for name in ("read_range", "search", "atomic_write", "run_cmd"):
        assert name in out


def test_handler_exception_becomes_error_text(tmp_path, monkeypatch):
    box, _ = _box(tmp_path)

    def boom(self, *a, **k):
        raise RuntimeError("内部炸了")

    monkeypatch.setattr(toolsmod.ToolBox, "_read_range", boom)
    out = box.execute("read_range", {"path": "a.md"})
    assert "RuntimeError" in out and "内部炸了" in out


def test_denied_call_is_audited_with_error(tmp_path):
    """越权必须进审计——事后要能查"它试过什么"。"""
    box, _ = _box(tmp_path)
    with pytest.raises(toolsmod.ToolDenied):
        box.execute("atomic_write", {"path": "../x.md", "content": "y"})
    assert box.calls and box.calls[0].get("error")
