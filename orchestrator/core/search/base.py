"""搜索插件层（Phase2 §5b/附录D）：接口 + 三个实现桩。

只定义契约与可离线验证的逻辑，真实网络调用由各provider自己实现。
SearchResult里published_at是**原文时间**——时效性验收（recent=730天/strict=180天）
全靠它，检索层拿不到原文时间就等于没法验收新鲜度。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ""
    published_at: str | None = None      # 原文时间，ISO或None（未知）
    source_id: str = ""


class SearchProvider:
    name = "base"

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        raise NotImplementedError

    def health(self) -> dict:
        return {"ok": True}


class TavilyProvider(SearchProvider):
    """首个实现。用urllib而非requests，少一个依赖。"""

    name = "tavily"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 timeout_s: int = 30, path: str = "/search"):
        from core.utils import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2])
        # 用 `is None` 而不是 `or`：传空串表示"**明确**不给密钥"，
        # 传 None 才表示"去 .env 找"。原来写成 `api_key or os.environ.get(...)`，
        # 结果 api_key="" 也会被 .env 里的真 key 填上——于是既没法在程序里
        # 强制无密钥（缺密钥分支永远测不到），也没法用构造参数覆盖 .env。
        if api_key is None:
            api_key = os.environ.get("TAVILY_API_KEY", "")
        self.api_key = api_key
        self.base_url = (base_url or "https://api.tavily.com").rstrip("/")
        self.timeout_s = timeout_s
        self.path = path if path.startswith("/") else f"/{path}"

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        if not self.api_key:
            raise RuntimeError("tavily_missing_api_key")
        body = json.dumps({"api_key": self.api_key, "query": query,
                           "max_results": top_k}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{self.path}", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out = []
        for i, item in enumerate(data.get("results", []) or []):
            out.append(SearchResult(
                url=item.get("url", ""), title=item.get("title", ""),
                snippet=item.get("content", "") or "",
                published_at=item.get("published_date")))
        return out


class BochaProvider(SearchProvider):
    """博查 Web Search（https://api.bochaai.com/v1/web-search）。

    响应格式**兼容 Bing Search API**：网页在 data.webPages.value，
    图片在 data.images.value。博查是国内少数提供长文本摘要（summary=true）
    的搜索源，做A类分级时比只有 snippet 的源好用。
    """

    name = "bocha"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 timeout_s: int = 30, path: str = "/v1/web-search",
                 summary: bool = True, freshness: str = "noLimit"):
        from core.utils import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2])
        if api_key is None:            # 同 Tavily：空串=明确无密钥
            api_key = os.environ.get("BOCHA_API_KEY", "")
        self.api_key = api_key
        self.base_url = (base_url or "https://api.bochaai.com").rstrip("/")
        self.timeout_s = timeout_s
        self.path = path if path.startswith("/") else f"/{path}"
        self.summary = summary
        self.freshness = freshness

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        if not self.api_key:
            raise RuntimeError("bocha_missing_api_key")
        body = json.dumps({"query": query, "count": max(1, min(top_k, 50)),
                           "summary": self.summary,
                           "freshness": self.freshness}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{self.path}", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _parse_bocha(data, top_k)


def _parse_bocha(data: dict, top_k: int) -> list[SearchResult]:
    """Bing兼容形状解析。博查偶尔把 errors 一起带回来，此时 webpages 为空，
    返回空列表而不是抛错——上层会看到"没检索到"并告警，比崩掉好。"""
    pages = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    out: list[SearchResult] = []
    for item in pages[:top_k]:
        summary = item.get("summary") or item.get("snippet") or ""
        out.append(SearchResult(
            url=item.get("url", ""), title=item.get("name", "") or "",
            snippet=summary,
            published_at=item.get("dateLastCrawled"),
            source_id=item.get("siteName", "") or ""))
    return out


# 就两家，不留"以后再接"的桩。Exa/Bing 曾以 NotImplementedError 桩的形式
# 留在 PROVIDERS 里——于是配置里写 provider: bing 不会报错，
# 而是**运行时**才炸。配置里能选一个必崩的选项，本身就是个坑。
# 将来真要第三家时再加类+注册，一共两处。
PROVIDERS = {p.name: p for p in (TavilyProvider, BochaProvider)}


def build(cfg: dict, env=None) -> SearchProvider:
    """按 config/search.yaml 造检索实例。

    配置字段要真传进去。之前只传了 timeout_s，api_key_env/base_url/path
    全被忽略——于是 search.yaml 里改端点、改key变量名都不生效，
    而运维会以为已经改过了。少传配置比不读配置更危险：
    它让配置文件看起来是有效的。
    """
    cfg = cfg or {}
    name = cfg.get("provider", "tavily")
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"unknown_search_provider:{name}"
                         f"(available={sorted(PROVIDERS)})")
    import os as _os
    env = env if env is not None else _os.environ
    kw: dict = {"timeout_s": int(cfg.get("timeout_s", 30))}
    akey = cfg.get("api_key_env")
    if akey:
        kw["api_key"] = env.get(akey, "")
    if cfg.get("base_url"):
        kw["base_url"] = cfg["base_url"]
    if cfg.get("path"):
        kw["path"] = cfg["path"]
    if cls is BochaProvider:
        kw.setdefault("summary", bool(cfg.get("summary", True)))
        kw.setdefault("freshness", cfg.get("freshness", "noLimit"))
    try:
        return cls(**kw)
    except TypeError:
        # 兼容备用构造：只传 timeout。当前两家都支持全参数，
        # 这条是保险丝不是主路。
        return cls(timeout_s=kw["timeout_s"])


@dataclass
class DailyQuota:
    """日额度：超了要P1告警而不是静默继续烧钱（附录D）。"""
    per_day: int = 500
    used: int = 0
    day: str = ""
    _history: list = field(default_factory=list)

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d", time.localtime())
        if self.day != today:
            self.day = today
            self.used = 0
            self._history = []

    def take(self, n: int = 1) -> bool:
        """占额度；返回False表示已超限（调用方应告警并停止检索）。"""
        self._roll()
        if self.used + n > self.per_day:
            return False
        self.used += n
        self._history.append(n)
        return True

    @property
    def exhausted(self) -> bool:
        self._roll()
        return self.used >= self.per_day
