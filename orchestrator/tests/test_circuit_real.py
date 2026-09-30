"""2.6 真实熔断：打**真 HTTP 服务器**，不 mock urlopen。

为什么不用 mock：mock 只能验"我抛异常时怎么处理"，验不到
真连接层的行为——连接被拒、429/401/500 的真实响应、并发下的
半开闸门。而这几件事恰恰是熔断最容易错的地方。

用 http.server 起一个真 socket，行为可编程切换（200/500/429/401/断开），
这样测的是真实的 urllib → socket → 响应 全链路。
"""
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.providers import (CircuitOpen, OllamaProvider,          # noqa: E402
                            OpenAICompatProvider, ProviderError)

OK_BODY = {"message": {"content": "ok"}, "prompt_eval_count": 5,
           "eval_count": 1}


class Server:
    """可编程行为的真 HTTP 服务器。"""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.hits = 0
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):      # 静音
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):              # noqa: N802
                with outer.lock:
                    outer.hits += 1
                    m = outer.mode
                ln = int(self.headers.get("Content-Length") or 0)
                if ln:
                    self.rfile.read(ln)
                if m == "slow":
                    # 慢响应：让"探测在飞"这个窗口可观测
                    time.sleep(1.5)
                    self._send(200, OK_BODY)
                elif m == "ok":
                    self._send(200, OK_BODY)
                elif m == "500":
                    self._send(500, {"error": "internal"})
                elif m == "429":
                    self._send(429, {"error": "rate limited"})
                elif m == "401":
                    self._send(401, {"error": {"message": "bad key"}})
                else:
                    self._send(200, OK_BODY)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def set(self, mode):
        with self.lock:
            self.mode = mode

    @property
    def n(self):
        with self.lock:
            return self.hits

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    s = Server("ok")
    yield s
    s.stop()


def _p(url, **kw):
    kw.setdefault("models", {"simple": "m"})
    kw.setdefault("timeout_s", 5)
    return OllamaProvider(base_url=url, **kw)


# ---------------------------------------------------------------- 真连接

def test_circuit_opens_against_real_500s(srv):
    """真 500 连续3次 → 熔断，第4次直接拒绝且**不打服务器**。"""
    p = _p(srv.url, fail_threshold=3, cooldown_s=300)
    srv.set("500")
    for _ in range(3):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    before = srv.n
    with pytest.raises(CircuitOpen):
        p.chat("q", {})
    assert srv.n == before, "熔断后仍在打服务器——熔断等于没用"
    assert p.health()["circuit_opened"] is True


def test_circuit_opens_when_endpoint_refuses_connections():
    """端点直接不在（连不上）→ 也必须熔断，不能每次都去撞墙。"""
    # 找一个确定没人监听的端口
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = _p(f"http://127.0.0.1:{port}", fail_threshold=2, cooldown_s=300)
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    with pytest.raises(CircuitOpen):
        p.chat("q", {})


def test_recovery_after_endpoint_comes_back(srv):
    """真服务器从500恢复成200 → cooldown 后探测成功 → 完全恢复。"""
    p = _p(srv.url, fail_threshold=2, cooldown_s=0.4)
    srv.set("500")
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    with pytest.raises(CircuitOpen):
        p.chat("q", {})
    srv.set("ok")
    time.sleep(0.5)                      # 等 cooldown
    assert p.chat("q", {})["content"] == "ok"
    h = p.health()
    assert h["ok"] is True and h["circuit_opened"] is False


# ---------------------------------------------------------------- 半开

def test_half_open_allows_exactly_one_probe(srv):
    """cooldown 之后**只放一个探测**在飞。

    设计§附录A 写的是"cooldown结束放一个探测请求（半开）"。
    但原实现只比较时间，没有闸门——cooldown 一过，**所有**并发调用
    都能同时打过去。对一个刚刚恢复（或刚刚挂掉）的端点，
    这就是标准的惊群：熔断的意义就是别在恢复瞬间压垮它。

    注意不能断言"最终只成功1个"：探测一旦成功，熔断就关闭，
    排队的请求理应继续放行（端点已恢复，挡住才是错的）。
    要断言的是**探测在飞期间**只有1个请求到达服务器。
    """
    p = _p(srv.url, fail_threshold=2, cooldown_s=0.4, max_concurrency=8)
    srv.set("500")
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    time.sleep(0.5)
    srv.set("slow")                  # 探测会挂1.5s，窗口可观测
    results = []
    baseline = srv.n                 # 必须取delta：前面500阶段已经打过2次

    def worker():
        try:
            p.chat("q", {})
            results.append("ok")
        except CircuitOpen:
            results.append("refused")
        except ProviderError:
            results.append("err")

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    time.sleep(0.7)                  # 探测仍在飞
    in_flight = srv.n - baseline
    for t in ts:
        t.join(timeout=30)
    assert in_flight == 1, (
        f"探测在飞时放行了{in_flight}个请求（应只有1个）——"
        "cooldown 后没有半开闸门，等于惊群")
    assert results.count("refused") >= 6, f"其余请求应被拒绝: {results}"
    # 端点恢复后流量应能继续（端点已好，挡住才是错的）
    assert results.count("ok") >= 1


def test_failed_probe_reopens_circuit_immediately(srv):
    """半开探测失败 → 必须**立即**重新熔断。

    原实现：探测失败只让 fails+1，而阈值是3 → 要连败3次才重新打开，
    中间那两次等于"完全放开"。对一个持续故障的端点，
    这会把熔断退化成随机失败。
    """
    p = _p(srv.url, fail_threshold=3, cooldown_s=0.4)
    srv.set("500")
    for _ in range(3):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    time.sleep(0.5)
    # 探测仍然失败
    with pytest.raises(ProviderError):
        p.chat("q", {})
    # 紧接着的调用必须被熔断拒绝，而不是继续打
    with pytest.raises(CircuitOpen):
        p.chat("q", {})


# ---------------------------------------------------------------- 错误分类

def test_bad_key_opens_circuit_without_waiting_for_threshold(srv):
    """401 是配置错误，重试毫无意义 → 第一次就该熔断并说清原因。

    等三次才熔断的代价是：key 写错时，前三个任务会各自失败一次，
    而错误信息还被包在"online_failed"里，看不出是key的问题。
    """
    p = _p(srv.url, fail_threshold=3, cooldown_s=300)
    srv.set("401")
    with pytest.raises(ProviderError) as e:
        p.chat("q", {})
    assert "401" in str(e.value), f"错误信息没带上状态码: {e.value}"
    with pytest.raises(CircuitOpen):
        p.chat("q", {}), "key错误时不该继续打"
    assert srv.n == 1, f"401后仍打了{srv.n}次"


def test_rate_limit_is_distinguishable(srv):
    """429 要能被识别出来——它和"服务挂了"是两种运维动作。"""
    p = _p(srv.url, fail_threshold=3, cooldown_s=300)
    srv.set("429")
    with pytest.raises(ProviderError) as e:
        p.chat("q", {})
    assert "429" in str(e.value), f"限流错误没被识别: {e.value}"


def test_success_resets_failure_count(srv):
    """偶发失败不该累积。中间成功一次，失败计数要清零。"""
    p = _p(srv.url, fail_threshold=3, cooldown_s=300)
    srv.set("500")
    with pytest.raises(ProviderError):
        p.chat("q", {})
    srv.set("ok")
    assert p.chat("q", {})["content"] == "ok"
    srv.set("500")
    for _ in range(2):
        with pytest.raises(ProviderError):
            p.chat("q", {})
    # 之前成功过 → 计数从0重新算，两次失败还没到3次
    assert p.health()["circuit_opened"] is False, \
        "成功后的失败仍被累积，偶发抖动会误触发熔断"
