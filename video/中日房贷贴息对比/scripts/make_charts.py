"""渲染分镜图表卡片（SVG，免版权、与口播数字严格一致）。

为什么自制而不是找素材图：
1. 口播里的每个数字都有出处（见 charts/数据说明.md），自制图能保证
   图上数字 = 口播数字 = 报告数字三者一致。用网上的图必然对不上，
   而对不上的图在评论区就会被指出来。
2. 版权干净，不需要标注来源（政策文件除外）。

用法：
    python scripts/make_charts.py            # 生成全部 SVG
    python scripts/make_charts.py --only s4  # 只生成某张

输出：同目录 *.svg，1080x1080 或按分镜比例。
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
CH = HERE.parent / "charts"

# 抖音竖屏 1080x1920，但数据卡片横版更易读；这里用 1080x1350（4:5）
W, H = 1080, 1350

BG = "#0f1115"
FG = "#ffffff"
MUTED = "#8b93a1"
ACCENT = "#ff4d4f"      # 红：关键/风险
COOL = "#4d9bff"        # 蓝：日本/数据
WARM = "#ffb020"        # 黄：中国/强调
GREEN = "#22c55e"       # 绿：结论

# 所有输出必须是**完整 SVG 文档**：有 <svg> 根元素和 xml 声明。
# 之前 _frame 只吐出一段 <defs>…片段，拼接后没有根元素，
# 15 张图全是坏的——而脚本退出码是 0，肉眼也看不出来（浏览器会尝试容错渲染）。
# scripts/check_charts.py 用 ElementTree 解析才发现。
XML_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
            f'height="{H}" viewBox="0 0 {W} {H}">')
XML_TAIL = "</svg>"


def esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _frame(title: str, subtitle: str = "") -> str:
    return f'''<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#12151c"/><stop offset="100%" stop-color="{BG}"/>
  </linearGradient>
</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/>
<text x="70" y="120" font-family="Source Han Sans SC, Microsoft YaHei, sans-serif"
      font-size="52" font-weight="bold" fill="{FG}">{esc(title)}</text>
{f'<text x="70" y="175" font-family="Source Han Sans SC, sans-serif" font-size="30" fill="{MUTED}">{esc(subtitle)}</text>' if subtitle else ''}'''


def _footer(note: str = "数据来源：公开政策文件与统计") -> str:
    return (f'<text x="70" y="{H-60}" font-family="Source Han Sans SC, sans-serif" '
            f'font-size="24" fill="{MUTED}">{esc(note)}</text>')


def _write(name: str, body: str) -> None:
    """写出完整 SVG 文档。

    body 是片段（各绘图函数只管内容），**根元素在这里补**。
    之前每个函数各自拼 `</svg>` 收尾却没有 `<svg>` 开头，产出全是坏文件。
    收尾也统一在这里加，调用方不该再自己拼——否则又是一处能拼错的地方。
    """
    CH.mkdir(parents=True, exist_ok=True)
    if body.rstrip().endswith(XML_TAIL):
        body = body.rstrip()[: -len(XML_TAIL)]
    (CH / f"{name}.svg").write_text(
        XML_HEAD + body.rstrip() + "\n" + XML_TAIL, encoding="utf-8")
    print(f"  {name}.svg")


def _load_csv(name: str) -> list[dict]:
    """读 charts/*.csv。跳过 # 开头的行。

    这些 csv 里带注释块，记录"这个数字能不能上字幕"。所以解析必须
    容忍注释——之前没跳过时整表读空，图表被静默跳过（不报错），
    表现是"脚本跑成功了但少了几张图"。
    """
    p = CH / name
    if not p.exists():
        return []
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def _bar_chart(name: str, title: str, rows: list[tuple[str, float]], unit: str,
               color: str = COOL, subtitle: str = "", highlight: str = "",
               fmt: str = "{label} {v}{unit}") -> None:
    """竖条图。负值用红色（那是跌/减，不是好数字）。

    fmt 控制标签排版。默认把 label 和数值**拼在一行**，容易挤在一起
    （标签长、数值也不短时直接压字）；传 "{v}{unit}\n{label}" 之类
    可分开。value_label 单独可覆盖某一条。
    """
    if not rows:
        return
    top, bottom = 280, H - 200
    vals = [v for _, v in rows]
    lo, hi = min(0, min(vals)), max(vals)
    span = (hi - lo) or 1
    zero_y = bottom - (0 - lo) / span * (bottom - top)
    step = (W - 200) / len(rows)
    bw = min(step * 0.6, 150)
    s = _frame(title, subtitle)
    s += f'<line x1="90" y1="{zero_y:.0f}" x2="{W-90}" y2="{zero_y:.0f}" stroke="{MUTED}" stroke-width="2"/>'
    for i, (lab, v) in enumerate(rows):
        cx = 100 + step * i + step / 2
        y = bottom - (v - lo) / span * (bottom - top)
        c = ACCENT if v < 0 else color
        if highlight and lab == highlight:
            c = WARM
        s += (f'<rect x="{cx-bw/2:.0f}" y="{min(y,zero_y):.0f}" width="{bw:.0f}" '
              f'height="{max(abs(bottom-y),1):.0f}" fill="{c}" rx="6"/>')
        vy = y - 20 if v >= 0 else y + 50
        head, _, sub = fmt.partition("\n")
        s += (f'<text x="{cx:.0f}" y="{vy:.0f}" text-anchor="middle" '
              f'font-family="Source Han Sans SC, sans-serif" font-size="36" '
              f'font-weight="bold" fill="{c}">'
              f'{esc(head.replace("{label}", lab).replace("{v}", _num(v)).replace("{unit}", unit))}</text>')
        s += (f'<text x="{cx:.0f}" y="{bottom+54:.0f}" text-anchor="middle" '
              f'font-family="Source Han Sans SC, sans-serif" font-size="24" '
              f'fill="{MUTED}">{esc(lab)}</text>')
        if sub:
            s += (f'<text x="{cx:.0f}" y="{bottom+88:.0f}" text-anchor="middle" '
                  f'font-family="Source Han Sans SC, sans-serif" font-size="22" '
                  f'fill="{MUTED}">{esc(sub.replace("{label}", lab))}</text>')
    s += _footer()
    _write(name, s + XML_TAIL)


def _num(v: float) -> str:
    """去掉无意义的小数尾巴：188.0 → 188，-12.1 → -12.1"""
    return str(int(v)) if float(v).is_integer() else str(v)


def _cards(name: str, title: str, items: list[tuple[str, str]], subtitle: str = "",
           color: str = WARM) -> None:
    """条件卡：逐行出现用（分镜2/6）。"""
    s = _frame(title, subtitle)
    y = 280
    for k, v in items:
        s += (f'<rect x="90" y="{y}" width="{W-180}" height="120" rx="16" '
              f'fill="#1b1f28" stroke="{color}" stroke-width="2"/>')
        s += (f'<text x="130" y="{y+72}" font-family="Source Han Sans SC, sans-serif" '
              f'font-size="38" fill="{MUTED}">{esc(k)}</text>')
        s += (f'<text x="{W-130}" y="{y+78}" text-anchor="end" '
              f'font-family="Source Han Sans SC, sans-serif" font-size="46" '
              f'font-weight="bold" fill="{color}">{esc(v)}</text>')
        y += 150
    s += _footer()
    _write(name, s + "</svg>")


def _big_statement(name: str, line1: str, line2: str, note: str = "",
                   color: str = GREEN) -> None:
    """纯字幕卡：停顿帧用。"""
    s = (f'<rect width="{W}" height="{H}" fill="{BG}"/>'
         f'<text x="{W//2}" y="{H//2-40}" text-anchor="middle" '
         f'font-family="Source Han Sans SC, sans-serif" font-size="80" '
         f'font-weight="bold" fill="{FG}">{esc(line1)}</text>'
         f'<text x="{W//2}" y="{H//2+70}" text-anchor="middle" '
         f'font-family="Source Han Sans SC, sans-serif" font-size="54" '
         f'fill="{color}">{esc(line2)}</text>')
    if note:
        s += _footer(note)
    _write(name, s + "</svg>")


def _flow(name: str, title: str, nodes: list[str], subtitle: str = "") -> None:
    """流程图：需求侧补贴这类"钱往哪流"用。"""
    s = _frame(title, subtitle)
    n = len(nodes)
    bw = (W - 160) / n - 40
    for i, t in enumerate(nodes):
        x = 80 + i * ((W - 160) / n)
        c = COOL if i == 0 else (WARM if i == n - 1 else MUTED)
        s += (f'<rect x="{x:.0f}" y="420" width="{bw:.0f}" height="130" rx="18" '
              f'fill="#1b1f28" stroke="{c}" stroke-width="3"/>')
        s += (f'<text x="{x+bw/2:.0f}" y="500" text-anchor="middle" '
              f'font-family="Source Han Sans SC, sans-serif" font-size="34" '
              f'fill="{c}">{esc(t)}</text>')
        if i < n - 1:
            ax = x + bw + 8
            s += (f'<text x="{ax+12:.0f}" y="500" font-family="sans-serif" '
                  f'font-size="40" fill="{MUTED}">→</text>')
    s += (f'<text x="{W//2}" y="700" text-anchor="middle" '
          f'font-family="Source Han Sans SC, sans-serif" font-size="40" '
          f'fill="{ACCENT}">总需求没有增加，只是转移了支付时点</text>')
    s += _footer()
    _write(name, s + "</svg>")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只生成名字含该串的图")
    args = ap.parse_args()

    def want(n: str) -> bool:
        return not args.only or args.only in n

    print("渲染分镜图表：")

    # 任务一
    if want("s3_demand_side"):
        _flow("s3_demand_side", "需求侧补贴：钱换了口袋",
              ["财政", "购房者", "房价"], "总需求未变")

    if want("s4_newstarts"):
        rows = [(r["year"], float(r["value"]))
                for r in _load_csv("s4_newstarts.csv") if r.get("value")]
        _bar_chart("s4_newstarts", "日本新开工户数（万户/年）", rows, "",
                   COOL, "贴息三十年，峰值后再未回去")

    if want("s5_divide"):
        _cards("s5_divide", "东京圈 vs 地方城市", [
            ("东京圈", "人口流入 · 仍在涨"),
            ("地方城市", "人口流出 · 空置 13–14%"),
            ("空置房", "约 900 万套"),
        ], "同一个国家，两种命运", COOL)

    if want("s6_rate_curve"):
        rows = [(r["year"], float(r["value_pct"]))
                for r in _load_csv("s6_rate_curve.csv") if r.get("value_pct")]
        _bar_chart("s6_rate_curve", "利率越低，贴息越有效", rows, "%",
                   COOL, "2023 起抬头，补贴相对意义下降")

    if want("s7_sawtooth"):
        _cards("s7_sawtooth", "政策到期 = 一波抢购", [
            ("到期前", "抢购升温"),
            ("到期当月", "集中成交"),
            ("到期后 1–2 季", "回落"),
        ], "锯齿状，不是趋势", WARM)

    if want("s8_lesson"):
        _big_statement("s8_lesson", "贴息改变的是", "购房时点 ≠ 购房总量",
                       color=GREEN)

    # 任务二
    if want("s1_policy"):
        _cards("s1_policy", "财金〔2026〕95 号", [
            ("发布", "2026-09-29"),
            ("首次", "中央给房贷贴息"),
            ("定位", "暂定一年"),
            ("到期", "2027 年 9 月"),
        ], "中国房贷政策的重要一步", ACCENT)

    if want("s2_conditions"):
        _cards("s2_conditions", "贴息条件", [
            ("房屋", "仅首套"),
            ("面积", "≤ 120 ㎡"),
            ("总价", "≤ 150 万元"),
            ("贴息", "1 个百分点"),
            ("年限", "最长 5 年"),
            ("单户上限", "100 万元"),
        ], "门槛清晰，不是普惠")

    if want("s3_rate_cut"):
        rows = [(r["item"], float(r["rate_pct"])) for r in _load_csv("s3_rate_cut.csv")]
        _bar_chart("s3_rate_cut", "贴息把利率拉低 1 个百分点", rows, "%",
                   WARM, "3.1% → 2.1%")

    if want("s4_deleverage"):
        # 这里刻意**不用一根柱图**装所有数：数据里混了「万亿元」「亿元」
        # 「%」三种量纲，-1.03万亿 和 -12.1% 画在同一根坐标轴上，
        # 柱子高度完全不可比（1.03 会把 12.1 压成看不见），而观众
        # 会照着柱子高低下结论——那是纯粹的误导。拆成两张图。
        rows = _load_csv("s4_deleverage.csv")

        def _amt(r):
            v = float(r["value"])
            return v / 10000 if r["unit"] == "亿元" else v   # 亿元 → 万亿元

        amt = [(r["item"].replace("_前8月增量", ""), _amt(r)) for r in rows
               if r.get("unit") in ("万亿元", "亿元")]
        _bar_chart("s4_deleverage_amt", "居民贷款：前 8 个月增量（万亿元）",
                   amt, "", COOL,
                   "住户贷款 −1.03 万亿，中长期仅 +188 亿",
                   fmt="{v} 万亿")
        # 房贷利率不画进来：3.1%/2.1% 是另一个话题（分镜3 单独讲），
        # 混在"去杠杆"里会让人误以为利率也在跌。
        pct = [(r["item"], float(r["value"])) for r in rows
               if r.get("unit") == "%" and "利率" not in r["item"]]
        _bar_chart("s4_deleverage_pct", "市场端：新房与开发端双跌", pct, "%",
                   WARM, "新房跌 · 开发端跌 · 二手逆势涨",
                   fmt="{v}{unit}")

    if want("s5_split"):
        rows = [(r["market"], float(r["change_pct"]))
                for r in _load_csv("s5_split.csv") if r.get("change_pct")]
        _bar_chart("s5_split", "以价换量：需求跑去二手", rows, "%",
                   WARM, "新房跌，二手涨")

    if want("s6_math"):
        _cards("s6_math", "算一笔账", [
            ("100 万 × 1% × 5 年", "≈ 省 5 万"),
            ("中央 : 地方", "90% : 10%"),
            ("上海月供差", "远不止 5 万"),
        ], "力度有限，且集中在刚需", WARM)

    if want("s9_headwind"):
        _cards("s9_headwind", "三逆风", [
            ("利率", "已经到底"),
            ("居民", "主动去杠杆"),
            ("开发端", "投资 −21%"),
        ], "贴息是顺风工具，逆风时打折", ACCENT)

    if want("s10_purpose"):
        _big_statement("s10_purpose", "社会政策 + 预期管理",
                       "不是宏观刺激", color=GREEN)

    if want("s11_expiry"):
        _cards("s11_expiry", "最大的变数", [
            ("2026-09", "政策发布"),
            ("2027-09", "到期"),
            ("续不续", "暂定一年"),
        ], "到期前见分晓", ACCENT)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())