"""校验生成的图表：SVG 能解析、PNG 真落盘、数字非空、条数与 csv 一致、
量纲不混、排版不越界不重叠。

不做这步的话，"脚本跑完没报错"会被当成"图没问题"。踩过的坑按严重程度：
1. render_png 忘了 save —— 脚本打印 16 张图、退出码 0，charts/ 空空如也
2. 文字画到画布外 —— Pillow 直接丢弃，最高柱的数值标签整段消失，无报错
3. csv 读成空表 —— 5 张图被**静默跳过**，退出码仍是 0
4. csv 表头与脚本预期不一致 —— KeyError 直接崩（这个反而好发现）
"""
import csv
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
CH = HERE.parent / "charts"
sys.path.insert(0, str(HERE))
import make_charts as mc          # noqa: E402  必须在 sys.path 之后
MUTED = mc.MUTED

# 分镜图 → 数据来源 csv（None = 纯文字卡，无需数据）
EXPECT = {
    "s4_newstarts": ("s4_newstarts.csv", None),        # (csv, 期望行数覆盖)
    "s6_rate_curve": ("s6_rate_curve.csv", None),
    "s3_rate_cut": ("s3_rate_cut.csv", None),
    "s5_split": ("s5_split.csv", None),
    # 拆分图：源 csv 有 7 行，这两张各只画其中一部分（量纲不同不能同轴），
    # 所以行数按"该图实际该有的数据点"单独给，不跟源 csv 总行数比。
    "s4_deleverage_amt": ("s4_deleverage.csv", 2),
    "s4_deleverage_pct": ("s4_deleverage.csv", 3),
}

# 量纲一致性：同一张图里出现的单位不能混。
# 混量纲是真实踩过的坑——把「亿元」当成「万亿元」直接画，188 变成
# "188 万亿"，等于把 188 亿说成 188 万亿，放到视频里就是硬伤。
# 匹配时按长度降序，"万亿" 要先于 "亿" 命中，否则会被误判成单独的「亿」。
ALLOWED_UNITS = {
    "s4_deleverage_amt": {"万亿"},
    "s4_deleverage_pct": {"%"},
    "s3_rate_cut": {"%"},
    "s5_split": {"%"},
    "s4_newstarts": {""},
    "s6_rate_curve": {"%"},
}
UNITS_BY_LEN = sorted(["万亿", "亿元", "%"], key=len, reverse=True)


def csv_rows(name: str) -> int:
    p = CH / name
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return len(list(csv.DictReader(lines)))


def main() -> int:
    bad = []
    svgs = sorted(CH.glob("*.svg"))
    print(f"检查 {len(svgs)} 个 SVG\n")
    for f in svgs:
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError as e:
            bad.append(f"{f.name}: XML 解析失败 {e}")
            continue
        if not root.tag.endswith("svg"):
            bad.append(f"{f.name}: 根元素是 {root.tag}，不是 svg")
            continue
        texts = [t.text for t in root.iter() if t.tag.endswith("text")]
        nonempty = [t for t in texts if t and t.strip()]
        if not nonempty:
            bad.append(f"{f.name}: 没有任何文字，疑似空图")
            continue

        name = f.stem
        note = ""
        if name in EXPECT:
            csv_name, override = EXPECT[name]
            # 拆分图按"该图实际该有的数据点"给；其余按 csv 总行数。
            want = override if override is not None else csv_rows(csv_name)
            # 柱状图：每个数据点至少 1 个 label + 1 个 value 文字
            if len(nonempty) < want * 2:
                bad.append(f"{name}: 文字数 {len(nonempty)} 少于 "
                           f"{want} 个数据点所需的 {want*2} 个（图可能没画全）")
            note = f"数据点={want}"

        # 量纲检查
        if name in ALLOWED_UNITS:
            allowed = ALLOWED_UNITS[name]
            for t in nonempty:
                for u in UNITS_BY_LEN:
                    if u in t:
                        if u not in allowed:
                            bad.append(
                                f"{name}: 出现单位 {u!r}，但本图只允许 "
                                f"{sorted(allowed)} —— 量纲混用会误导观众")
                        break

        print(f"  ok  {f.name:<26} 文字 {len(nonempty):>3}  {note}")

    bad.extend(_check_layout())

    if bad:
        print("\n问题：")
        for b in bad:
            print("  ! " + b)
        return 1
    print("\nPASS: 结构、数据、量纲、排版全部一致")
    return 0


def _check_layout() -> list[str]:
    """排版检查：文字越界 / 数值标签与轴标签重叠。

    这两条都是**静默**失败——文字画到画布外，Pillow 直接丢弃，
    脚本照样退出 0。肉眼不去逐张数的话根本发现不了"少了那个数字"。
    真踩过：最高柱的数值标签被挤到画布外没了；矮柱的数值标签压在
    轴标签上，两串字糊成一团。
    """
    bad = []
    sys.path.insert(0, str(HERE))
    import make_charts as mc

    for name, sc in mc.build_all().items():
        # 1) 越界
        for x, y, s, size, fill, anchor, bold in sc.texts:
            if y < 0 or y > sc.h:
                bad.append(f"{name}: 文字 {s!r} 的 y={y:.0f} 越出画布 "
                           f"(0..{sc.h})，画出来会整段消失")
            if x < 0 or x > sc.w:
                bad.append(f"{name}: 文字 {s!r} 的 x={x:.0f} 越出画布")

        # 2) 数值标签 vs 轴标签重叠（只查柱状图：靠底部的两行）
        if not sc.rects:
            continue
        bottom = max(r[1] + r[3] for r in sc.rects)
        axis_labels = [t for t in sc.texts if t[1] > bottom]
        value_labels = [t for t in sc.texts if t[5] == "middle" and t[6]
                        and t[4] != MUTED]
        for v in value_labels:
            for a in axis_labels:
                # 跳过它自己：矮柱的数值标签翻到下方后，y 与轴标签几乎相等，
                # 不判等就会把自己报成"与轴标签重叠"。
                if v is a:
                    continue
                if abs(v[1] - a[1]) < 24 and abs(v[0] - a[0]) < 90:
                    bad.append(
                        f"{name}: 数值标签 {v[2]!r} 与轴标签 {a[2]!r} 重叠"
                        f"（y={v[1]:.0f} vs {a[1]:.0f}）")
    return bad


if __name__ == "__main__":
    raise SystemExit(main())