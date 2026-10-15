# -*- coding: utf-8 -*-
"""把 10-12 京东 OCR JSON 按行重建，打印含价格的行 + 上下文行，用于人工定性。"""
import json, os, re, sys

TEMP = os.path.join(os.environ["TEMP"], "opencode")
PRICE = re.compile(r"[¥￥]\s*\d")


def load_lines(path):
    data = json.load(open(path, "r", encoding="utf-8-sig"))
    lines = []  # [(y, x, text)]
    for blk in data if isinstance(data, list) else [data]:
        if not isinstance(blk, dict):
            continue
        for w in blk.get("Words", []) or []:
            r = w.get("BoundingRect") or {}
            t = (w.get("Text") or "").strip()
            if not t:
                continue
            lines.append((r.get("Top", 0), r.get("Left", 0), t))
    # 按 y 聚行（容差 12px）
    lines.sort(key=lambda z: (z[0], z[1]))
    rows, cur, cy = [], [], None
    for y, x, t in lines:
        if cy is None or abs(y - cy) <= 12:
            cur.append((x, t)); cy = y if cy is None else cy
        else:
            rows.append((cy, sorted(cur, key=lambda z: z[0])))
            cur, cy = [(x, t)], y
    if cur:
        rows.append((cy, sorted(cur, key=lambda z: z[0])))
    return [(y, "".join(t for _, t in cs)) for y, cs in rows]


def main():
    for name in sys.argv[1:]:
        path = os.path.join(TEMP, name)
        if not os.path.exists(path):
            print("!! 缺文件 %s" % name); continue
        rows = load_lines(path)
        print("=" * 90)
        print("### %s   共 %d 行" % (name, len(rows)))
        hits = [i for i, (_, txt) in enumerate(rows) if PRICE.search(txt)]
        print("含价格行: %s" % [i for i in hits])
        for i in hits:
            lo, hi = max(0, i - 2), min(len(rows), i + 2)
            print("-" * 70)
            for j in range(lo, hi):
                mark = ">>" if j == i else "  "
                print("%s y=%-5d %s" % (mark, rows[j][0], rows[j][1]))
        print()


if __name__ == "__main__":
    main()
