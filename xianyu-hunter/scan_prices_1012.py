# -*- coding: utf-8 -*-
"""从 10-12 采价 OCR JSON 里抽取价格候选，用于核验交接文档里的结论。"""
import json, re, glob, os, sys

TEMP = os.path.join(os.environ["TEMP"], "opencode")

PRICE = re.compile(r"(?:[¥￥]\s*\d[\d,]{2,}|\b\d{3,5}(?:\.\d{1,2})?\b)")

def texts(node):
    """递归收集所有 Text 字符串"""
    out = []
    if isinstance(node, dict):
        t = node.get("Text")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
        for v in node.values():
            out.extend(texts(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(texts(v))
    return out

def main():
    files = sys.argv[1:]
    if not files:
        files = sorted(glob.glob(os.path.join(TEMP, "*.json")),
                       key=os.path.getmtime, reverse=True)
    for f in files:
        name = os.path.basename(f)
        if not re.search(r"(jd_|pdd_|honor_)", name):
            continue
        try:
            data = json.load(open(f, "r", encoding="utf-8-sig"))
        except Exception as e:
            print("!! %s 解析失败 %s" % (name, e))
            continue
        tt = texts(data)
        joined = " ".join(tt)
        # 只关心含 17 Pro Max / iPhone 的页面
        if "17" not in joined:
            continue
        prices = []
        for m in PRICE.finditer(joined):
            s = m.group(0).strip()
            if s not in prices:
                prices.append(s)
        print("=" * 78)
        print("%s   (%d 文本节点)" % (name, len(tt)))
        print("  价格候选: %s" % ("  ".join(prices) if prices else "无"))
        # 标题/店铺线索
        clues = [t for t in tt if re.search(r"(Pro Max|苹果|Apple|自营|百亿补贴|券后|准新|全新)", t)]
        seen = []
        for c in clues:
            if c not in seen:
                seen.append(c)
        for c in seen[:14]:
            print("   - %s" % c)

if __name__ == "__main__":
    main()
