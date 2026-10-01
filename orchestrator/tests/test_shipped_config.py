"""入库的配置**必须能被解析**——而且解析失败不许静默。

为什么这条要单独测：`_load_cfg` 为了"配置坏了也别掀翻主循环"而吞掉异常
返回 {}。这在**运行时**是对的（一个坏文件不该让编排器起不来），
但代价是配置坏了会**静默变成默认值**——governor不生效、租约参数全部回落、
锁存活期回落，而没有任何报错。

我本人就踩过：给 schedule.yaml 加 `lock:` 时多缩进了两格，它挂到
flow mapping `workers: {...}` 下面 → 整个文件解析失败 → 而 612 个测试
全绿。因为几乎所有测试都用 tmp 根 + 自己的最小配置，从来没解析过
**生产的** schedule.yaml。

所以这里做两件事：
1. 逐个解析所有入库 YAML（对着真实文件，不是 tmp 副本）
2. 断言 `_load_cfg` 面对坏文件时不是静默返回 {}（见下）
"""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG_DIR = ROOT / "config"
SHIPPED = sorted(CONFIG_DIR.glob("*.yaml")) + sorted(CONFIG_DIR.glob("*.yml"))


def test_there_are_configs_to_check():
    assert SHIPPED, f"{CONFIG_DIR} 下没有配置文件——那说明路径变了"


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.name)
def test_shipped_yaml_parses(path):
    """入库的每个 YAML 都必须能解析。

    这条曾失败：schedule.yaml 里的 governor/lease/lock/poll 被多缩进两格，
    挂在 flow mapping `workers: {...}` 下面，整个文件解析失败。
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        pytest.fail(f"{path.name} 解析失败: {e}")
    assert isinstance(data, dict), f"{path.name} 顶层不是映射: {type(data)}"
    assert data, f"{path.name} 解析结果为空"


def test_schedule_yaml_has_the_keys_we_depend_on():
    """不只是"能解析"，还得真的包含编排器要读的那些键。

    缩进错位最阴的地方在于：文件仍可能解析成功，只是键掉到别处去了。
    """
    data = yaml.safe_load((CONFIG_DIR / "schedule.yaml").read_text(
        encoding="utf-8"))
    for key in ("jobs", "workers", "governor", "lease", "lock", "poll",
                "aging", "session_switch_limit"):
        assert key in data, f"schedule.yaml 缺顶层键 {key}"
    assert data["governor"].get("enabled") is True, \
        "governor 未启用——动态算力档位不会生效"
    assert data["lock"]["ttl_s"] > data["poll"]["normal_s"], \
        "锁存活期必须大于轮询间隔，否则一个卡住的tick就会让别人以为它死了"
    assert data["lease"]["ttl_s"] > data["lease"]["heartbeat_s"], \
        "租约TTL必须大于心跳间隔，否则心跳会白续"


def test_flow_mappings_have_no_nested_keys():
    """flow mapping（{...}）挂不了子键——缩进错位就是这么变成语法错误的。

    直接查原始文本：若某行以 flow mapping 开头且下一行缩进更深，
    那就是缩进错位（无论当前能否解析，换个写法就会炸）。
    """
    lines = (CONFIG_DIR / "schedule.yaml").read_text(
        encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            indent = len(line) - len(line.lstrip())
            is_flow = stripped.endswith("}") and ": {" in stripped
            if is_flow:
                for j in range(i + 1, len(lines)):
                    nxt = lines[j]
                    if not nxt.strip() or nxt.strip().startswith("#"):
                        continue
                    n_indent = len(nxt) - len(nxt.lstrip())
                    if n_indent > indent:
                        pytest.fail(
                            f"schedule.yaml 第{i + 1}行是 flow mapping，"
                            f"但第{j + 1}行缩进更深（{nxt.strip()[:40]!r}）——"
                            "flow mapping 挂不了子键，会解析失败")
                    break


def test_gateway_config_complexity_mapping_is_complete():
    """complexity_models 必须覆盖三档，且指向的模型都在注册表里。

    少配一档会静默落到 default_model（而 default_model 不指 local，
    所以表现是"复杂任务用了便宜模型"而不是报错）。
    """
    data = yaml.safe_load((CONFIG_DIR / "gateway.yaml").read_text(
        encoding="utf-8"))
    models = data.get("models") or {}
    cm = data.get("complexity_models") or {}
    for c in ("simple", "medium", "complex"):
        assert c in cm, f"complexity_models 缺 {c}（会静默落到 default_model）"
        assert cm[c] in models, f"{c} 指向未注册的模型 {cm[c]!r}"
    assert data.get("default_model") in models
    assert data.get("default_model") != "local", \
        "default_model 指向 local = 静默降级到小模型"


def test_load_cfg_does_not_swallow_silently(tmp_path):
    """坏配置必须**看得见**，不能静默变成默认值。

    _load_cfg 现在的行为是"解析失败返回 {}"。对运行时来说这是对的
    （一个坏文件不该让编排器起不来），但代价是配置错误完全隐形。
    这条要求它至少留下痕迹。
    """
    from core import orchestrator as orch
    (tmp_path / "config").mkdir()
    bad = tmp_path / "config" / "schedule.yaml"
    bad.write_text("workers: {a: 1}\n  governor: {b: 2}\n", encoding="utf-8")
    got = orch._load_cfg(tmp_path)
    assert got == {}, "这份YAML本该解析失败"
    # 但必须留下告警痕迹
    logs = tmp_path / "logs" / "structured.jsonl"
    assert logs.exists(), "配置解析失败却没有留痕——它会静默降级成默认值"
    text = logs.read_text(encoding="utf-8", errors="replace")
    assert "config_parse_failed" in text, (
        "配置解析失败没记日志。这等于配置悄悄不生效："
        "governor不启用、租约参数全部回落，而没人知道")


def test_load_cfg_returns_real_values_for_real_config():
    """反过来也验：好的配置必须真被读到（不是只有失败路径被测）。"""
    from core import orchestrator as orch
    got = orch._load_cfg(CONFIG_DIR.parent)
    assert got, "生产的 schedule.yaml 读出来是空的"
    assert got.get("workers", {}).get("reserved_low_slot") == 1
    assert got.get("lock", {}).get("ttl_s") == 90
    assert got.get("governor", {}).get("enabled") is True


def test_load_models_reports_bad_gateway_config(tmp_path):
    """gateway.yaml 坏了也要留痕——同样的道理。

    痕迹可能来自 config_parse_failed（_load_cfg）或 gateway_load_failed
    （_load_models 兜底），两条都算数：要求的是"看得见"，不是某一行日志。
    """
    from core import orchestrator as orch
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "gateway.yaml").write_text(
        "models: {a: {type: ollama}}\n  bad: 1\n", encoding="utf-8")
    assert orch._load_models(tmp_path) == {}
    logs = tmp_path / "logs" / "structured.jsonl"
    assert logs.exists(), "坏gateway配置没有留痕"
    text = logs.read_text(encoding="utf-8", errors="replace")
    assert ("config_parse_failed" in text or "gateway_load_failed" in text), \
        f"坏gateway配置两种痕迹都没有: {text[:200]}"


def test_templates_all_render_shipped_config():
    """生产配置 + 生产模板要能配合渲染（不是各自的孤立单测）。"""
    from core import enqueue
    from core.executor import build_card
    import json as _json
    work = Path(pytest.importorskip("tempfile").mkdtemp())
    try:
        shutil.copytree(ROOT / "templates", work / "templates")
        shutil.copytree(ROOT / "config", work / "config")
        from core.store import Store
        s = Store(work)
        s.ensure_plan("p1", template="daily")
        shard = enqueue.validate(_json.dumps({
            "task_id": "t1", "plan_id": "p1", "objective": "写个文件",
            "outputs": ["artifacts/t1.md"], "complexity": "simple",
        }, ensure_ascii=False).encode("utf-8"), root=work)
        _st, task = s.insert_task(shard)
        card = build_card(s, task, "r")
        assert "atomic_write" in card, "任务卡没告诉模型它有工具"
        assert "{{" not in card, "模板变量没渲染完"
    finally:
        shutil.rmtree(work, ignore_errors=True)
