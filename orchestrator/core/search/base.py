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
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
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


class ExaProvider(SearchProvider):
    """预留桩：接口已定，实现待接。"""
    name = "exa"

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        raise NotImplementedError("exa_not_implemented")


class BingProvider(SearchProvider):
    """预留桩：接口已定，实现待接。"""
    name = "bing"

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        raise NotImplementedError("bing_not_implemented")


PROVIDERS = {p.name: p for p in (TavilyProvider, ExaProvider, BingProvider)}


def build(cfg: dict) -> SearchProvider:
    """按config/search.yaml的provider字段造实例。"""
    name = (cfg or {}).get("provider", "tavily")
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"unknown_search_provider:{name}")
    return cls(timeout_s=int((cfg or {}).get("timeout_s", 30)))


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
