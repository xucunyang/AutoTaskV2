"""薄 tool loop 的测试（用户 2026-09-29 决策：自己做，不拉 CC/opencode）。

重点不在"能不能调通"，而在**边界**：
- 沙箱是否真的关得住（路径越界/命令越权）
- 水位线是否在循环内就拦住（否则 session 让出设计没有落点）
- 模型死循环时是否有硬上限
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import tools
from core.executor import MAX_TOOL_STEPS, SessionYield, _tool_loop


# ---------------------------------------------------------------- 沙箱

def test_write_cannot_escape_root(tmp_path):
    """../ 必须拒绝。模型会试，不能只靠"它应该不会"。"""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("不该被读到或覆盖", encoding="utf-8")
    tb = tools.ToolBox(root)
    with pytest.raises(tools.ToolDenied, match="escapes_root"):
        tb.execute("atomic_write", {"path": "../secret.txt", "content": "x"})
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "不该被读到或覆盖"


def test_write_rejects_absolute_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tb = tools.ToolBox(root)
    with pytest.raises(tools.ToolDenied, match="absolute_path"):
        tb.execute("atomic_write", {"path": str(tmp_path / "x.md"), "content": "y"})


def test_read_cannot_escape_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.md").write_text("外部内容", encoding="utf-8")
    tb = tools.ToolBox(root)
    with pytest.raises(tools.ToolDenied, match="escapes_root"):
        tb.execute("read_range", {"path": "../outside.md"})


def test_nested_dotdot_inside_root_is_allowed(tmp_path):
    """artifacts/../state/x 这种最终还在root内的应该放行——
    只按字符串判前缀会误杀，那是另一头的错。"""
    root = tmp_path / "root"
    (root / "artifacts").mkdir(parents=True)
    tb = tools.ToolBox(root)
    out = tb.execute("atomic_write", {"path": "artifacts/../ok.md", "content": "hi"})
    assert out.startswith("OK")
    assert (root / "ok.md").read_text(encoding="utf-8") == "hi"


def test_cmd_not_in_allowlist_is_denied(tmp_path):
    tb = tools.ToolBox(tmp_path)
    out = tb.execute("run_cmd", {"cmd": "curl http://evil.example.com"})
    assert "cmd_not_allowed" in out
    for bad in ("rm -rf /", "del /", "format c:", "powershell -c x",
                "bash -c x", "sh -c x", "cmd /c x"):
        assert "cmd_not_allowed" in tb.execute("run_cmd", {"cmd": bad})


def test_shell_operators_rejected_even_for_allowed_binary(tmp_path):
    """白名单里的python也不能借管道/重定向/子shell跑别的。"""
    tb = tools.ToolBox(tmp_path)
    for bad in ("python -c print(1) | more", "python -c x > out.txt",
                "python -c x && python -c y", "python -c `whoami`",
                "python -c $(whoami)", "python -c x; whoami"):
        assert "not_allowed" in tb.execute("run_cmd", {"cmd": bad}), bad


def test_run_cmd_actually_runs_allowlisted(tmp_path):
    """白名单不是摆设：能真跑并拿到退出码。"""
    tb = tools.ToolBox(tmp_path)
    out = tb.execute("run_cmd", {"cmd": f'{sys.executable} -c print(1+1)'})
    assert "exit=0" in out and "2" in out


def test_write_is_atomic_no_tmp_left(tmp_path):
    """原子写：不留.tmp残骸，且不留半截文件。"""
    root = tmp_path / "root"
    root.mkdir()
    tb = tools.ToolBox(root)
    tb.execute("atomic_write", {"path": "artifacts/a.md", "content": "完整"})
    assert not list(root.rglob("*.tmp"))
    assert (root / "artifacts" / "a.md").read_text(encoding="utf-8") == "完整"


def test_oversize_write_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tb = tools.ToolBox(root)
    out = tb.execute("atomic_write", {"path": "big.md",
                                      "content": "x" * (tools.MAX_WRITE_BYTES + 1)})
    assert "too_large" in out
    assert not (root / "big.md").exists()


def test_read_range_paginates(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    p = root / "log.txt"
    p.write_text("\n".join(f"line{i}" for i in range(500)), encoding="utf-8")
    tb = tools.ToolBox(root)
    first = tb.execute("read_range", {"path": "log.txt", "offset": 0, "limit": 3})
    assert first == "line0\nline1\nline2"
    second = tb.execute("read_range", {"path": "log.txt", "offset": 3, "limit": 2})
    assert second == "line3\nline4"


def test_read_missing_file_is_soft_error(tmp_path):
    """读不到不是异常：让模型自己换个路径，而不是打断整个循环。"""
    root = tmp_path / "root"
    root.mkdir()
    out = tools.ToolBox(root).execute("read_range", {"path": "nope.md"})
    assert "file_not_found" in out


def test_unknown_tool_reports_choices(tmp_path):
    out = tools.ToolBox(tmp_path).execute("rm_rf", {})
    assert "unknown_tool" in out and "atomic_write" in out


def test_search_unconfigured_does_not_look_like_empty_results(tmp_path):
    """未配检索时必须明确说"没有检索"，不能回空列表——
    空列表会被模型读成"网上没这资料"，然后拿假结论去写报告。"""
    out = tools.ToolBox(tmp_path).execute("search", {"query": "x"})
    assert "search_unavailable" in out


def test_every_tool_call_is_audited(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tb = tools.ToolBox(root)
    tb.execute("atomic_write", {"path": "a.md", "content": "x"})
    tb.execute("read_range", {"path": "a.md"})
    assert [c["tool"] for c in tb.calls] == ["atomic_write", "read_range"]


def test_audit_does_not_store_full_content(tmp_path):
    """审计里存全文=日志膨胀+可能带敏感内容。只存形状。"""
    root = tmp_path / "root"
    root.mkdir()
    tb = tools.ToolBox(root)
    tb.execute("atomic_write", {"path": "a.md", "content": "y" * 5000})
    assert tb.calls[0]["result_len"] < 100


# ---------------------------------------------------------------- 循环

class ScriptedProvider:
    """按剧本返回 tool_calls，最后一轮给无工具的收尾回复。"""
    supports_tools = True

    def __init__(self, script):
        self.script = list(script)
        self.seen_messages = []
        self.calls = 0

    def chat(self, prompt, budget, *, messages=None, tools=None, **kw):
        self.calls += 1
        self.seen_messages = list(messages or [])
        assert tools, "工具循环必须把schemas传给模型，否则它无从选工具"
        step = self.script.pop(0) if self.script else {}
        return {"content": step.get("content", ""),
                "usage": step.get("usage", {"prompt_tokens": 10,
                                            "completion_tokens": 5}),
                "tool_calls": step.get("tool_calls", [])}


def _tc(name, **args):
    return [{"id": f"c{args.get('_i', 0)}", "name": name, "arguments": args}]


def test_loop_executes_tool_then_finishes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "1", "name": "atomic_write",
                         "arguments": {"path": "out.md", "content": "产物"}}]},
        {"content": "写完了"},
    ])
    tb = tools.ToolBox(root)
    resp, steps = _tool_loop(provider, "任务卡", None, tb, {}, 8192)
    assert resp["content"] == "写完了"
    assert (root / "out.md").read_text(encoding="utf-8") == "产物"
    assert steps == 1


def test_loop_stops_at_max_steps(tmp_path):
    """模型反复调同一工具时必须有硬上限，不能无限烧token。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": str(i), "name": "read_range",
                         "arguments": {"path": "x.md"}}]}
        for i in range(50)
    ])
    tb = tools.ToolBox(root)
    resp, steps = _tool_loop(provider, "任务卡", None, tb, {}, 8192)
    assert steps == MAX_TOOL_STEPS
    assert provider.calls == MAX_TOOL_STEPS


def test_loop_budget_max_steps_can_lower_limit(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": str(i), "name": "read_range",
                         "arguments": {"path": "missing.md"}}]}
        for i in range(5)
    ])
    tb = tools.ToolBox(root)
    _resp, steps = _tool_loop(provider, "卡", None, tb,
                              {"budget": {"max_steps": 2}}, 8192)
    assert steps == 2


def test_loop_checks_watermark_every_turn(tmp_path):
    """水位线必须在循环内判：一次chat做完的话session让出就没有落点。
    第一轮就超水位 → 立刻让出，工具不执行。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"usage": {"prompt_tokens": 8000, "completion_tokens": 1},
         "tool_calls": [{"id": "1", "name": "atomic_write",
                         "arguments": {"path": "a.md", "content": "x"}}]},
    ])
    tb = tools.ToolBox(root)
    with pytest.raises(SessionYield) as e:
        _tool_loop(provider, "卡", None, tb, {}, 8192)     # 8000/8192 > 0.8
    assert e.value.reason == "context_full"
    assert not (root / "a.md").exists()


def test_loop_denied_tool_stops_immediately(tmp_path):
    """越权必须停，不能把DENIED回给模型让它换写法再试。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "1", "name": "atomic_write",
                         "arguments": {"path": "../evil.md", "content": "x"}}]},
    ])
    tb = tools.ToolBox(root)
    with pytest.raises(tools.ToolDenied):
        _tool_loop(provider, "卡", None, tb, {}, 8192)


def test_tool_error_is_fed_back_not_raised(tmp_path):
    """普通工具失败（如文件不存在）要回填给模型自己纠正，不打断循环。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "1", "name": "read_range",
                         "arguments": {"path": "missing.md"}}]},
        {"content": "那个文件确实不存在，我改用任务卡里的信息"},
    ])
    tb = tools.ToolBox(root)
    resp, steps = _tool_loop(provider, "卡", None, tb, {}, 8192)
    assert steps == 1
    tool_msgs = [m for m in provider.seen_messages if m["role"] == "tool"]
    assert tool_msgs and "file_not_found" in tool_msgs[0]["content"]


def test_tool_call_id_is_preserved_for_tool_messages(tmp_path):
    """tool消息的tool_call_id必须与assistant的tool_calls对上，
    否则多数兼容端点会报400。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "call_abc123", "name": "atomic_write",
                         "arguments": {"path": "a.md", "content": "x"}}]},
        {"content": "done"},
    ])
    tb = tools.ToolBox(root)
    _tool_loop(provider, "卡", None, tb, {}, 8192)
    assert provider.seen_messages[-1]["tool_call_id"] == "call_abc123"


def test_provider_without_tools_kwarg_falls_back_to_single_chat(tmp_path):
    """老桩provider只接(prompt,budget)：不该崩，退回单轮。"""
    class Old:
        def chat(self, prompt, budget):
            return {"content": "老路径", "usage": {"prompt_tokens": 5}}
    root = tmp_path / "root"
    root.mkdir()
    resp, steps = _tool_loop(Old(), "卡", None, tools.ToolBox(root), {}, 8192)
    assert resp["content"] == "老路径" and steps == 0


def test_write_pressure_reminder_after_idle_rounds(tmp_path):
    """连续只检索不写 → 必须注入写提醒，且提醒只出现一次。

    真实案例：flash 曾连调 12 次 search，一个字没写，把整轮预算烧光。
    任务卡的文字约束压不住，需要结构性的推力。
    """
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": str(i), "name": "search",
                         "arguments": {"query": f"q{i}"}}]}
        for i in range(12)
    ])
    tb = tools.ToolBox(root, search_fn=lambda q, max_results=5: [])
    shard = {"outputs": ["artifacts/out.md"]}
    _resp, steps = _tool_loop(provider, "卡", None, tb, shard, 8192)
    assert steps == MAX_TOOL_STEPS
    reminders = [m for m in provider.seen_messages
                 if "执行提醒" in str(m.get("content") or "")]
    assert len(reminders) == 1, f"提醒应恰好出现一次: {len(reminders)}"
    assert "artifacts/out.md" in reminders[0]["content"]


def test_no_reminder_when_model_writes(tmp_path):
    """正常写产物的流程不应被打扰。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "1", "name": "search",
                         "arguments": {"query": "q"}}]},
        {"tool_calls": [{"id": "2", "name": "atomic_write",
                         "arguments": {"path": "out.md", "content": "x"}}]},
        {"content": "done"},
    ])
    tb = tools.ToolBox(root, search_fn=lambda q, max_results=5: [])
    _tool_loop(provider, "卡", None, tb, {"outputs": ["out.md"]}, 8192)
    reminders = [m for m in provider.seen_messages
                 if "执行提醒" in str(m.get("content") or "")]
    assert not reminders


def test_no_reminder_without_declared_outputs(tmp_path):
    """没有声明产物时不提醒——没东西可写，提醒只会污染上下文。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": str(i), "name": "search",
                         "arguments": {"query": f"q{i}"}}]}
        for i in range(6)
    ])
    tb = tools.ToolBox(root, search_fn=lambda q, max_results=5: [])
    _tool_loop(provider, "卡", None, tb, {}, 8192)
    reminders = [m for m in provider.seen_messages
                 if "执行提醒" in str(m.get("content") or "")]
    assert not reminders


# ---------------------------------------------------------------- 方言

def test_ollama_tool_call_arguments_are_dict_not_string():
    """Ollama 要 arguments 是 **dict**。写成JSON字符串它会直接 400
    （"Value looks like object, but can't find closing '}' symbol"）。

    这个错误极难定位的原因：第一步不回填所以正常，产物也真的写出来了，
    到第二步才炸——看起来像"模型生成的JSON坏了"，其实是我们回填错格式。
    """
    from core.providers import OllamaProvider
    calls = [{"id": "c1", "name": "atomic_write",
              "arguments": {"path": "a.md", "content": "x"}}]
    got = OllamaProvider(base_url="http://x").encode_assistant_tool_calls(calls)
    assert got[0]["function"]["arguments"] == {"path": "a.md", "content": "x"}
    assert isinstance(got[0]["function"]["arguments"], dict)


def test_openai_compat_arguments_are_json_string():
    """OpenAI 兼容端点相反：要 JSON 字符串。"""
    from core.providers import OpenAICompatProvider
    calls = [{"id": "c1", "name": "atomic_write",
              "arguments": {"path": "a.md", "content": "x"}}]
    got = OpenAICompatProvider(base_url="http://x",
                               model="m").encode_assistant_tool_calls(calls)
    assert isinstance(got[0]["function"]["arguments"], str)
    assert json.loads(got[0]["function"]["arguments"])["path"] == "a.md"


def test_loop_uses_provider_dialect_when_echoing(tmp_path):
    """循环回填 assistant 消息时必须走 provider 自己的编码，
    不能写死 OpenAI 形状。"""
    root = tmp_path / "root"
    root.mkdir()
    seen = {}

    class DialectProvider:
        def chat(self, prompt, budget, *, messages=None, tools=None, **kw):
            seen["msgs"] = list(messages or [])
            if len(seen["msgs"]) == 1:
                return {"content": "", "usage": {"prompt_tokens": 5},
                        "tool_calls": [{"id": "c1", "name": "atomic_write",
                                        "arguments": {"path": "a.md", "content": "x"}}]}
            return {"content": "done", "usage": {"prompt_tokens": 8},
                    "tool_calls": []}

        def encode_assistant_tool_calls(self, calls):
            return [{"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": dict(c["arguments"])}}
                    for c in calls]

    _tool_loop(DialectProvider(), "卡", None, tools.ToolBox(root), {}, 8192)
    asst = [m for m in seen["msgs"] if m["role"] == "assistant"][0]
    assert isinstance(asst["tool_calls"][0]["function"]["arguments"], dict)


def test_stub_provider_without_encoder_falls_back_to_openai(tmp_path):
    """没有 encode 方法的桩provider不能崩，退回OpenAI形状。"""
    root = tmp_path / "root"
    root.mkdir()
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "1", "name": "atomic_write",
                         "arguments": {"path": "a.md", "content": "x"}}]},
        {"content": "done"},
    ])
    _tool_loop(provider, "卡", None, tools.ToolBox(root), {}, 8192)
    asst = [m for m in provider.seen_messages if m["role"] == "assistant"][0]
    assert isinstance(asst["tool_calls"][0]["function"]["arguments"], str)


def test_ollama_think_flag_is_configurable(monkeypatch):
    """think=false 是顶字段（不是options里），且可配置：
    实测同一任务卡 think=false 6.6s / 默认 21.4s，正确性相同。"""
    import json as _json
    from core.providers import OllamaProvider
    sent = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return _json.dumps({"message": {"content": "ok"},
                                "prompt_eval_count": 1}).encode()

    def fake_urlopen(req, timeout=None):
        sent["body"] = _json.loads(req.data.decode("utf-8"))
        return R()

    monkeypatch.setattr("core.providers.urllib.request.urlopen", fake_urlopen)
    OllamaProvider(base_url="http://x", models={"simple": "m"},
                   think=False).chat("hi", {})
    assert sent["body"].get("think") is False
    assert "think" not in sent["body"].get("options", {})

    OllamaProvider(base_url="http://x", models={"simple": "m"},
                   think=None).chat("hi", {})
    assert "think" not in sent["body"], "None时不该下发该字段"


def test_resume_payload_is_passed_to_model(tmp_path):
    """续跑时checkpoint必须进messages，否则'断点续跑'只是状态机上的说法。"""
    from schemas.models import Checkpoint
    root = tmp_path / "root"
    root.mkdir()
    ck = Checkpoint(task_id="t1", plan_id="p1", done_steps=[1, 2],
                    summary=["已拆2张"])
    provider = ScriptedProvider([{"content": "继续"}])
    _tool_loop(provider, "卡", ck, tools.ToolBox(root), {}, 8192)
    assert "已拆2张" in json.dumps(provider.seen_messages, ensure_ascii=False)
