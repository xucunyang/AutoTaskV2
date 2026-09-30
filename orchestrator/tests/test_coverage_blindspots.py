"""2.7 覆盖率盲区：privacy / timing / governor / 检索provider 四个模块。

这些模块此前**门禁管不到**（check_coverage.py 只盯六个模块），
于是 privacy 和 timing 长期 0%、governor 只有 59%。
其中 governor 更严重：它算出来的 profile **编排器从不消费**，
ACTIVE/IDLE 只是配置文件里的两组静态数字。
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import governor as gov                             # noqa: E402
from core import privacy, timing                              # noqa: E402
from core import search as search_pkg                         # noqa: E402
from core.search import base as sb                            # noqa: E402


# ================================================================ privacy

def test_privacy_detects_chinese_id_card():
    assert privacy.classify("身份证 110101199003078515")[0] == "secret"


def test_privacy_detects_phone_with_digit_boundaries():
    """必须用前后瞻而不是 \\b：\\b 在中文与数字之间不算边界，会漏检。"""
    assert privacy.classify("电话 13812345678 找他")[0] == "secret"
    # 手机号嵌在更长数字里不该被当成手机号（避免误伤）
    lvl, hits = privacy.classify("编号 9138123456780000012")
    assert "phone" not in hits, f"长数字里的手机号被误判: {hits}"
    assert "bank" in hits or "id_card" in hits


def test_privacy_detects_id_card_not_as_phone():
    _, hits = privacy.classify("110101199003078515")
    assert "id_card" in hits and "phone" not in hits, hits


def test_privacy_detects_api_key_and_email():
    assert privacy.classify("api_key: sk-abc123")[0] == "secret"
    assert privacy.classify("联系 a.b@example.com")[0] == "secret"


def test_privacy_declared_secret_wins_without_hits():
    lvl, hits = privacy.classify("完全普通的一段话", declared="secret")
    assert lvl == "secret" and hits == []


def test_privacy_internal_stays_internal_without_hits():
    lvl, _ = privacy.classify("内部资料", declared="internal")
    assert lvl == "internal"


def test_privacy_public_when_nothing_matches():
    lvl, hits = privacy.classify("今天天气不错")
    assert lvl == "public" and hits == []


def test_privacy_redact_removes_all_patterns():
    out = privacy.redact("id=110101199003078515 phone=13812345678 "
                         "api_key=sk-xyz a@b.com")
    assert "110101199003078515" not in out
    assert "13812345678" not in out
    assert "sk-xyz" not in out
    assert "a@b.com" not in out
    assert "REDACTED" in out


def test_privacy_redact_truncates():
    """脱敏结果要限长：否则redact本身变成把全文吐出去的旁路。"""
    out = privacy.redact("x" * 5000)
    assert len(out) <= 2000


def test_privacy_handles_empty_input():
    assert privacy.classify("")[0] == "public"
    assert privacy.redact("") == ""


# ================================================================ timing

def test_timing_span_records_duration():
    rec = []
    with timing.span("llm", plan_id="p", task_id="t", run_id="r", record=rec):
        time.sleep(0.02)
    assert len(rec) == 1
    got = rec[0]
    assert got["span"] == "llm" and got["task_id"] == "t"
    assert got["duration_ms"] >= 15, f"计时刻度不对: {got}"


def test_timing_span_records_on_exception():
    """异常路径也必须留下耗时——出错的span恰恰最需要被观测。"""
    rec = []
    with pytest.raises(ValueError):
        with timing.span("boom", record=rec):
            raise ValueError("x")
    assert rec and rec[0]["span"] == "boom"


def test_timing_summarize_groups_by_task_and_sorts():
    rec = [{"task_id": "a", "duration_ms": 10},
           {"task_id": "b", "duration_ms": 50},
           {"task_id": "a", "duration_ms": 30},
           {"task_id": "", "duration_ms": 5}]
    got = timing.summarize(rec, top_n=2)
    assert got["count"] == 4
    assert got["top"][0] == ("b", 50), "应按累计耗时排序"
    assert dict(got["top"])["a"] == 40, "同任务应累加而不是取max"


def test_timing_summarize_top_n_limits():
    rec = [{"task_id": f"t{i}", "duration_ms": i} for i in range(10)]
    assert len(timing.summarize(rec, top_n=3)["top"]) == 3


def test_timing_summarize_empty():
    assert timing.summarize([]) == {"top": [], "count": 0}


# ================================================================ governor

class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, level, kind, **kw):
        self.sent.append((level, kind, kw))


def test_active_is_default_and_caps_executor():
    g = gov.Governor(idle_after_s=300, confirm=2)
    st = g.sample(idle_s=0, avail_gb=16)
    assert st["profile"] == "ACTIVE"
    assert st["workers"]["executor"] == 1
    assert st["by"] == "auto"


def test_idle_requires_consecutive_confirmations():
    """一次空闲不够，要连续 confirm_samples 次——防止路过一下就切满载。"""
    g = gov.Governor(idle_after_s=300, confirm=2)
    g.sample(idle_s=0, avail_gb=16)          # 先在ACTIVE
    assert g.sample(idle_s=999, avail_gb=16)["profile"] == "ACTIVE"
    assert g.sample(idle_s=999, avail_gb=16)["profile"] == "IDLE"


def test_input_drops_back_to_active_immediately():
    g = gov.Governor(idle_after_s=300, confirm=2)
    g.sample(idle_s=999, avail_gb=16)
    g.sample(idle_s=999, avail_gb=16)
    assert g.profile == "IDLE"
    assert g.sample(idle_s=0, avail_gb=16)["profile"] == "ACTIVE", \
        "有人开始用电脑就必须立刻降档"


def test_idle_hits_reset_when_input_returns():
    g = gov.Governor(idle_after_s=300, confirm=2)
    g.sample(idle_s=999, avail_gb=16)
    g.sample(idle_s=0, avail_gb=16)          # 打断
    assert g.sample(idle_s=999, avail_gb=16)["profile"] == "ACTIVE"
    assert g.sample(idle_s=999, avail_gb=16)["profile"] == "IDLE"


def test_memory_caps_executor_count():
    """核心修复：内存不足要**真的**裁掉执行器。

    原实现只算了个 mem_ok 布尔值就丢掉，mem_cap_gb 从没被用过——
    一台内存紧张的机器照样按IDLE派4个执行器，配置里的12GB上限纯属摆设。
    指标算出来不用比不算更糟：它让人以为有防护。
    """
    g = gov.Governor(idle_after_s=1, confirm=1)
    st = g.sample(idle_s=999, avail_gb=32)         # 内存充裕
    assert st["workers"]["executor"] == 4
    st2 = g.sample(idle_s=999, avail_gb=4.0)       # 只剩4GB，扣掉headroom≈2
    assert st2["workers"]["executor"] <= 2, \
        f"内存不足却仍派{st2['workers']['executor']}个执行器"
    # mem_ok 与 worker 数是两件事：mem_ok问"还够不够干活"，
    # worker 数是"最多能开几个"。4GB仍算够干活，所以 mem_ok=True。
    assert st2["mem_ok"] is True
    assert st2["workers"]["executor"] < st["workers"]["executor"], \
        "内存变紧后执行器数应下降"


def test_memory_starved_never_goes_negative():
    g = gov.Governor()
    st = g.sample(idle_s=0, avail_gb=0.1)
    assert st["workers"]["executor"] >= 0
    assert st["mem_ok"] is False
    assert st["burst_allowed"] is False


def test_burst_allowed_follows_memory():
    g = gov.Governor()
    assert g.sample(idle_s=0, avail_gb=16)["burst_allowed"] is True
    assert g.sample(idle_s=0, avail_gb=2.0)["burst_allowed"] is False


def test_manual_override_pins_profile():
    g = gov.Governor(idle_after_s=1, confirm=1)
    st = g.sample(idle_s=0, avail_gb=16, override="IDLE")
    assert st["profile"] == "IDLE" and st["by"] == "manual"


def test_manual_mode_pauses_auto_monitoring():
    """设计§7：手动模式期间自动档位监控暂停。

    不清零 hits 的话，从手动切回自动的第一下就可能因为之前攒的
    hits 立刻跳IDLE——等于手动设定被无视。
    """
    g = gov.Governor(idle_after_s=1, confirm=2)
    g.sample(idle_s=999, avail_gb=16)          # 攒到1个hit
    g.sample(idle_s=999, avail_gb=16, override="ACTIVE")   # 切手动 → 应清零
    assert g._idle_hits == 0
    # 切回自动，第一次只攒hit不该直接跳IDLE
    assert g.sample(idle_s=999, avail_gb=16)["profile"] == "ACTIVE"
    assert g.sample(idle_s=999, avail_gb=16)["profile"] == "IDLE"


def test_override_file_roundtrip(tmp_path, monkeypatch):
    f = tmp_path / "governor.override"
    monkeypatch.setattr(gov, "OVERRIDE_FILE", f)
    assert gov.read_override() == "AUTO"          # 无文件=自动
    gov.write_override("idle", "手工")
    assert gov.read_override() == "IDLE"
    gov.write_override("active", "在用")
    assert gov.read_override() == "ACTIVE"
    f.write_text("garbage", encoding="utf-8")
    assert gov.read_override() == "AUTO", "无法识别的内容应退回AUTO"


def test_override_survives_unreadable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gov, "OVERRIDE_FILE", tmp_path / "nope.override")
    assert gov.read_override() == "AUTO"


def test_switch_emits_alert_with_by():
    """档位变化要留痕，否则事后没人知道那段时间为什么跑得慢。"""
    n = FakeNotifier()
    g = gov.Governor(idle_after_s=1, confirm=1, notifier=n)
    g.sample(idle_s=0, avail_gb=16)               # ACTIVE，无切换
    assert not n.sent
    g.sample(idle_s=999, avail_gb=16)             # →IDLE
    assert n.sent and n.sent[0][1] == "governor_switch"
    assert n.sent[0][2]["prev"] == "ACTIVE"
    assert n.sent[0][2]["now"] == "IDLE"


def test_no_duplicate_alert_without_switch():
    n = FakeNotifier()
    g = gov.Governor(idle_after_s=1, confirm=1, notifier=n)
    g.sample(idle_s=0, avail_gb=16)
    g.sample(idle_s=999, avail_gb=16)
    g.sample(idle_s=999, avail_gb=16)             # 已在IDLE，不该再报
    assert len([s for s in n.sent if s[1] == "governor_switch"]) == 1


def test_alert_failure_does_not_break_sampling():
    class Boom(FakeNotifier):
        def send(self, *a, **k):
            raise RuntimeError("告警通道挂了")
    g = gov.Governor(idle_after_s=1, confirm=1, notifier=Boom())
    g.sample(idle_s=0, avail_gb=16)
    st = g.sample(idle_s=999, avail_gb=16)        # 切换时告警会炸
    assert st["profile"] == "IDLE", "告警失败不能影响档位决策"


def test_idle_seconds_never_negative():
    for f in (gov._idle_win, gov._idle_mac):
        assert f() >= 0.0


def test_mac_idle_parsing(monkeypatch):
    """ioreg输出解析：纳秒→秒。"""
    out = '"HIDIdleTime" = 1234567890123\n'
    monkeypatch.setattr(gov.sys, "platform", "darwin")
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: type("R", (), {"stdout": out})())
    assert gov.idle_seconds() == pytest.approx(1234.56789, rel=1e-3)


def test_mac_idle_falls_back_to_zero(monkeypatch):
    """ioreg缺失/超时时必须回0（保安全判ACTIVE），不能抛。"""
    def boom(*a, **k):
        raise FileNotFoundError("ioreg")
    monkeypatch.setattr(gov.sys, "platform", "darwin")
    monkeypatch.setattr("subprocess.run", boom)
    assert gov.idle_seconds() == 0.0


def test_mac_idle_garbage_does_not_crash(monkeypatch):
    monkeypatch.setattr(gov.sys, "platform", "darwin")
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: type("R", (), {"stdout": "no marker\n"})())
    assert gov.idle_seconds() == 0.0


def test_unknown_platform_returns_zero(monkeypatch):
    monkeypatch.setattr(gov.sys, "platform", "linux")
    assert gov.idle_seconds() == 0.0


def test_mem_info_falls_back_when_psutil_missing(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    got = gov.mem_info()
    assert got["total_gb"] > 0 and got["avail_gb"] > 0, \
        "psutil缺失时应回退到保守假设，而不是崩掉"


# ================================================================ 检索provider

class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_build_reads_all_config_fields(monkeypatch):
    """build() 之前只传 timeout_s，api_key_env/base_url/path 全被忽略——
    改配置不生效而运维以为改过了。少传配置比不读配置更危险。"""
    monkeypatch.setenv("MY_KEY", "sk-xyz")
    p = sb.build({"provider": "tavily", "timeout_s": 11,
                  "api_key_env": "MY_KEY", "base_url": "https://h.example",
                  "path": "/v2/search"},
                 env={"MY_KEY": "sk-xyz"})
    assert p.api_key == "sk-xyz"
    assert p.base_url == "https://h.example"
    assert p.path == "/v2/search"
    assert p.timeout_s == 11


def test_build_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown_search_provider"):
        sb.build({"provider": "nope"})


def test_bocha_is_available_and_parsed(monkeypatch):
    """博查响应兼容Bing格式（data.webPages.value）。"""
    payload = {"data": {"webPages": {"value": [
        {"name": "标题A", "url": "https://a.com", "summary": "摘要A",
         "siteName": "A站", "dateLastCrawled": "2026-09-01T00:00:00Z"},
        {"name": "标题B", "url": "https://b.com", "snippet": "片段B"},
    ]}}}
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["headers"] = dict(req.header_items())
        return FakeResp(payload)

    monkeypatch.setattr("core.search.base.urllib.request.urlopen", fake_urlopen)
    p = sb.BochaProvider(api_key="bk-test", base_url="https://api.bochaai.com")
    got = p.search("关键词", top_k=5)
    assert len(got) == 2
    assert got[0].url == "https://a.com"
    assert got[0].snippet == "摘要A", "应优先用summary（比snippet信息量大）"
    assert got[0].source_id == "A站"
    assert seen["headers"].get("Authorization") == "Bearer bk-test"
    assert seen["body"]["query"] == "关键词"
    assert seen["body"]["count"] == 5


def test_bocha_clamps_count_to_50(monkeypatch):
    seen = {}
    monkeypatch.setattr("core.search.base.urllib.request.urlopen",
                        lambda req, timeout=None: (
                            seen.setdefault("body",
                                            json.loads(req.data.decode())),
                            FakeResp({"data": {"webPages": {"value": []}}}))[1])
    sb.BochaProvider(api_key="k").search("q", top_k=999)
    assert seen["body"]["count"] <= 50, "博查单次上限50条"


def test_bocha_uses_summary_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr("core.search.base.urllib.request.urlopen",
                        lambda req, timeout=None: (
                            seen.setdefault("body",
                                            json.loads(req.data.decode())),
                            FakeResp({"data": {"webPages": {"value": []}}}))[1])
    sb.BochaProvider(api_key="k", summary=False, freshness="oneWeek").search("q")
    assert seen["body"]["summary"] is False
    assert seen["body"]["freshness"] == "oneWeek"


def test_bocha_empty_result_is_not_an_error(monkeypatch):
    monkeypatch.setattr("core.search.base.urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp(
                            {"data": {"webPages": {"value": []}}}))
    assert sb.BochaProvider(api_key="k").search("q") == []


def test_bocha_tolerates_missing_fields(monkeypatch):
    """博查偶尔把 errors 一起带回来，此时 webpages 为空。"""
    monkeypatch.setattr("core.search.base.urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp(
                            {"errors": [{"message": "quota"}]}))
    assert sb.BochaProvider(api_key="k").search("q") == []


def test_tavily_parses_results(monkeypatch):
    payload = {"results": [
        {"url": "https://x.com", "title": "T", "content": "C",
         "published_date": "2026-09-01"},
    ]}
    monkeypatch.setattr("core.search.base.urllib.request.urlopen",
                        lambda req, timeout=None: FakeResp(payload))
    got = sb.TavilyProvider(api_key="tk").search("q", top_k=3)
    assert got[0].url == "https://x.com" and got[0].snippet == "C"
    assert got[0].published_at == "2026-09-01"


def test_tavily_missing_key_raises():
    """api_key="" 必须真的表示"无密钥"，不能被 .env 里的真 key 填上。

    构造函数原来写 `api_key or os.environ.get(...)`，于是传空串也会拿到
    真 key —— 缺密钥这条分支在有 .env 的机器上**永远测不到**。
    """
    assert sb.TavilyProvider(api_key="").api_key == ""
    with pytest.raises(RuntimeError, match="missing_api_key"):
        sb.TavilyProvider(api_key="").search("q")


def test_bocha_missing_key_raises():
    assert sb.BochaProvider(api_key="").api_key == ""
    with pytest.raises(RuntimeError, match="missing_api_key"):
        sb.BochaProvider(api_key="").search("q")


def test_api_key_none_falls_back_to_env(monkeypatch):
    """api_key=None 才是"去 .env 找"，与空串语义相反。"""
    monkeypatch.setenv("TAVILY_API_KEY", "from-env")
    assert sb.TavilyProvider(api_key=None).api_key == "from-env"
    assert sb.TavilyProvider(api_key="explicit").api_key == "explicit"


def test_daily_quota_blocks_over_limit():
    q = sb.DailyQuota(per_day=2, day="2000-01-01")
    assert q.take() and q.take()
    assert q.take() is False, "超限应拒绝而不是继续烧钱"
    assert q.used == 2, "被拒绝的调用不应计入已用"


def test_daily_quota_resets_next_day():
    q = sb.DailyQuota(per_day=1, day="2000-01-01")
    assert q.take() is True
    assert q.take() is False
    q.day = "2000-01-02"
    assert q.take() is True, "新的一天应重置额度"


def test_search_package_exposes_pipeline():
    """包级导出：调用方按 core.search.X 引用，缺了会 AttributeError。"""
    assert hasattr(search_pkg, "build")
