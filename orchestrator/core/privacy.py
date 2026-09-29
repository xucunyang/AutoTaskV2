"""隐私判定：模板打标优先 + 正则扫描兜底。"""
from __future__ import annotations
import re
# 注：不用\b做数字边界（\b在中文与数字之间不算边界，会漏检），改用数字前后瞻
PATTERNS = {
    "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "bank": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "api_key": re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*\S+"),
    "email": re.compile(r"[\w.-]+@[\w.-]+\.\w+"),
}
def classify(text: str, declared: str = "public") -> tuple[str, list[str]]:
    hits = [k for k, p in PATTERNS.items() if p.search(text or "")]
    if declared == "secret" or hits:
        return "secret", hits
    if declared == "internal":
        return "internal", hits
    return "public", hits

def redact(text: str) -> str:
    out = text or ""
    for p in PATTERNS.values():
        out = p.sub("[REDACTED]", out)
    return out[:2000]
