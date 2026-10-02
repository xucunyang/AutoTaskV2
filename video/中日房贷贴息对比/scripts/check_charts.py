"""校验生成的 SVG：能解析、数字非空、条数与 csv 行数一致。

不做这步的话，"脚本跑完没报错"会被当成"图没问题"——
上一版就是这样：csv 表头与脚本预期不一致，KeyError 直接崩；
再上一版更糟，csv 读成空表，5 张图被**静默跳过**，而退出码是 0。
"""
import csv
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
CH = HERE.parent / "charts"

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

    if bad:
        print("\n问题：")
        for b in bad:
            print("  ! " + b)
        return 1
    print("\nPASS: 全部 SVG 结构与数据一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())