# -*- coding: utf-8 -*-
"""Parse pad scan XML dumps into a readable 64G-focused lead report.

Usage:
  python pad_leads.py <tag> <kw1,kw2,...> [outfile]

Why XML and not the .txt dumps: PowerShell 5.1 `>` redirection writes GBK,
which corrupts every CJK char in the parsed text. XML pulled via adb is
always valid UTF-8, so it is the only trustworthy source.
"""
import io
import os
import re
import sys

SCANS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scans')

NODE = re.compile(
    r'<node[^>]*text="([^"]*)"[^>]*content-desc="([^"]*)"[^>]*'
    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')


def clean(s):
    """Strip zero-width / bidi junk that Xianyu sprinkles into descriptions."""
    return re.sub(u'[\u200b-\u200f\ufeff\u2060]', '', s)


def load(tag, kw):
    """Yield (price, description) pairs for one keyword across its screens."""
    safe = re.sub(r'[^A-Za-z0-9]', '_', kw)
    seen = set()
    for s in range(1, 6):
        path = os.path.join(SCANS, '%s_%s_s%d.xml' % (tag, safe, s))
        if not os.path.exists(path):
            continue
        xml = io.open(path, encoding='utf-8', errors='replace').read()

        nodes = []
        for m in NODE.finditer(xml):
            val = clean(m.group(1)) or clean(m.group(2))
            x1, y1, x2, y2 = (int(m.group(i)) for i in (3, 4, 5, 6))
            if val:
                nodes.append((y1, y2, x1, val))

        # price = yuan marker node followed by numeric siblings on same line
        prices = []
        for idx, (y1, y2, x1, val) in enumerate(nodes):
            if val != u'\u00a5':
                continue
            num, mult = '', 1
            for j in range(idx + 1, min(idx + 6, len(nodes))):
                nxt = nodes[j]
                if abs(nxt[0] - y1) > 30:
                    break
                t = nxt[3]
                if re.match(r'^[\d\.]+$', t):
                    num += t
                elif t == u'\u4e07':
                    mult = 10000
                    break
            if num:
                prices.append((y1, y2, float(num) * mult))

        descs = [n for n in nodes
                 if len(n[3]) > 50
                 and not re.match(u'^[\\d\\.\u4e07\u00a5]+$', n[3])]

        for (py1, _py2, price) in prices:
            best, best_gap = '', 10 ** 9
            for (_dy1, dy2, _dx, dv) in descs:
                gap = py1 - dy2
                if 0 <= gap < 220 and gap < best_gap:
                    best_gap, best = gap, dv
            if not best:
                continue
            key = best[:45]
            if key in seen:
                continue
            seen.add(key)
            yield price, best


def classify(desc):
    """Return rule flags. Handles negation: '非监管机' is GOOD, not MDM."""
    flags = []
    low = desc.lower()
    # MDM: positive mention only. '非监管机' / '不是MDM' are selling points.
    mdm = False
    for kw in (u'\u76d1\u7ba1', u'\u7ed5\u8fc7'):
        for m in re.finditer(re.escape(kw), desc):
            start = max(0, m.start() - 6)
            ctx = desc[start:m.end() + 2]
            if u'\u975e' not in ctx and u'\u4e0d\u662f' not in ctx and u'\u65e0' not in ctx:
                mdm = True
    if 'mdm' in low:
        idx = low.find('mdm')
        if u'\u975e' not in desc[max(0, idx - 6):idx] and u'\u4e0d' not in desc[max(0, idx - 6):idx]:
            mdm = True
    if mdm:
        flags.append('MDM!')
    if u'\u65e0\u5934' in desc:
        flags.append('HEADLESS!')
    if any(k in desc for k in (u'\u5e93\u5b58', u'\u6279\u53d1', u'\u6392\u884c\u699c')):
        flags.append('BULK!')
    if any(k in desc for k in (u'\u4e13\u8425', u'\u5e97\u94fa', u'\u4f18\u9009')):
        flags.append('SHOP?')
    if '64g' in low:
        flags.append('64G')
    return flags


def main():
    tag = sys.argv[1]
    kws = sys.argv[2].split(',')
    out_path = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        SCANS, '%s_leads.md' % tag)

    out = [u'# Pad widescan leads (%s)\n' % tag]
    stats = {'total': 0, '64g': 0, 'blocked': 0}
    for kw in kws:
        rows = list(load(tag, kw))
        if not rows:
            out.append(u'\n## %s -> (no results)\n' % kw)
            continue
        out.append(u'\n## %s -> %d items\n' % (kw, len(rows)))
        rows.sort(key=lambda r: r[0])
        for price, desc in rows:
            flags = classify(desc)
            stats['total'] += 1
            if '64G' in flags:
                stats['64g'] += 1
            if any(f.endswith('!') for f in flags):
                stats['blocked'] += 1
            out.append(u'- **%s** `%s` %s\n'
                       % (format(price, '.0f'),
                          '/'.join(flags) or '-',
                          desc[:190].replace('\n', ' ')))
    out.append(u'\n---\ntotal=%d  with-64G=%d  auto-blocked=%d\n'
               % (stats['total'], stats['64g'], stats['blocked']))
    io.open(out_path, 'w', encoding='utf-8').write(u''.join(out))
    print('written %s' % out_path)
    print('total=%d with64G=%d blocked=%d'
          % (stats['total'], stats['64g'], stats['blocked']))


if __name__ == '__main__':
    main()
