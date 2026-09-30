"""core.search 包门面：把常用符号导到包级。

之前 __init__.py 是空的，调用方必须知道去 core.search.base 里找 build——
包名已经表达了"搜索"，再要求调用方记住子模块名只是增加记忆负担。
顺带让包本身也进覆盖率统计（空文件不算模块）。
"""
from core.search.base import (PROVIDERS, BochaProvider, DailyQuota,   # noqa: F401
                              SearchProvider, SearchResult, TavilyProvider,
                              build)
from core.search.classify_source import (classify_all,                     # noqa: F401
                                        classify_one, classify_with_cache,
                                        domain_of, parse_tier_reply,
                                        prior_tier)
from core.search.pipeline import (build_analysis_context,                  # noqa: F401
                                  search_and_filter, write_four_set)

__all__ = [
    "PROVIDERS", "BochaProvider", "DailyQuota", "SearchProvider",
    "SearchResult", "TavilyProvider", "build",
    "classify_all", "classify_one", "classify_with_cache", "domain_of",
    "parse_tier_reply", "prior_tier",
    "build_analysis_context", "search_and_filter", "write_four_set",
]
