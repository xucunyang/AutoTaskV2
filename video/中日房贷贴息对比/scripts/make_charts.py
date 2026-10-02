"""渲染分镜图表卡片（SVG + PNG，免版权、与口播数字严格一致）。

为什么自制而不是找素材图：
1. 口播里的每个数字都有出处（见 charts/数据说明.md），自制图能保证
   图上数字 = 口播数字 = 报告数字三者一致。用网上的图必然对不上，
   而对不上的图在评论区就会被指出来。
2. 版权干净，不需要标注来源（政策文件除外）。

**为什么同时出 SVG 和 PNG**：
剪映吃不了 SVG，必须转成位图。而这台机器上没有 rsvg-convert /
Inkscape / ImageMagick（`convert` 那个是 Windows 的磁盘转换工具，
名字撞车了，不是 ImageMagick），Chrome 无头截图又会挂住。
所以直接用 Pillow 渲染。

**关键设计：布局只写一遍，两个渲染器共用**。
早先的写法是"拼 SVG 字符串"，要出 PNG 就得再写一份画图逻辑——
两份实现迟早对不上（尺寸、字号、换行位置都会漂）。
现在所有绘图函数往 Scene 里塞图元，SVG 和 PNG 各消费同一个 Scene，
所以两种格式的排版必然一致。

用法：
    python scripts/make_charts.py            # 生成全部 SVG + PNG
    python scripts/make_charts.py --only s4  # 只生成某张
    python scripts/make_charts.py --fmt png  # 只要位图
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
CH = HERE.parent / "charts"

# 抖音竖屏 1080x1920，但数据卡片横版更易读；这里用 1080x1350（4:5）
W, H = 1080, 1350

BG_TOP = "#12151c"
BG_BOT = "#0f1115"
FG = "#ffffff"
MUTED = "#8b93a1"
ACCENT = "#ff4d4f"      # 红：关键/风险
COOL = "#4d9bff"        # 蓝：日本/数据
WARM = "#ffb020"        # 黄：中国/强调
GREEN = "#22c55e"       # 绿：结论

# 柱状图底部两行文字的行距（相对绘图区底边）。数值标签与轴标签**必须**
# 分属两行：负值柱的底边就是绘图区底边，标签若按"底边 + 偏移"放，
# 正好落在轴标签那一行，两串字糊成一团。
VALUE_LABEL_DY = 56
AXIS_LABEL_DY = 116

FONT_STACK = "Source Han Sans SC, Microsoft YaHei, sans-serif"

# Windows 自带中文字体。优先微软雅黑（竖屏小字可读性最好），
# 退回黑体。都不在才报错——绝不能静默出方块图。
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]
_FONT_BOLD_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
]


def _find_font(candidates: list[str]) -> str:
    for p in candidates:
        if Path(p).exists():
            return p
    raise FileNotFoundError(
        "找不到中文字体，渲染出来会是方块。候选：" + ", ".join(candidates))


# ===========================================================================
# 场景图：布局的唯一真相
# ===========================================================================

class Scene:
    """一个图元集合。SVG 与 PNG 两个渲染器消费同一份，保证排版一致。"""

    def __init__(self, w: int = W, h: int = H):
        self.w, self.h = w, h
        self.rects: list[tuple] = []
        self.lines: list[tuple] = []
        self.texts: list[tuple] = []

    def rect(self, x, y, w, h, fill, rx=0):
        self.rects.append((x, y, w, h, fill, rx))

    def line(self, x1, y1, x2, y2, stroke, width=2):
        self.lines.append((x1, y1, x2, y2, stroke, width))

    def text(self, x, y, s, size, fill, anchor="start", bold=False):
        self.texts.append((x, y, str(s), size, fill, anchor, bold))

    def strings(self) -> list[str]:
        return [t[2] for t in self.texts]

    def units(self) -> set[str]:
        """图上出现的量纲，用于校验同一张图不混量纲。"""
        out = set()
        for s in self.strings():
            for u in ("万亿", "亿元", "%"):
                if u in s:
                    out.add(u)
        return out


# ===========================================================================
# SVG 渲染
# ===========================================================================

XML_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" '
            f'height="{H}" viewBox="0 0 {W} {H}">')
XML_TAIL = "</svg>"


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(sc: Scene) -> str:
    p = [XML_HEAD,
         f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
         f'<stop offset="0%" stop-color="{BG_TOP}"/>'
         f'<stop offset="100%" stop-color="{BG_BOT}"/>'
         f'</linearGradient></defs>',
         f'<rect width="{sc.w}" height="{sc.h}" fill="url(#bg)"/>']
    for x, y, w, h, fill, rx in sc.rects:
        r = f' rx="{rx}"' if rx else ""
        p.append(f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" '
                 f'height="{h:.0f}" fill="{fill}"{r}/>')
    for x1, y1, x2, y2, stroke, wdt in sc.lines:
        p.append(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" '
                 f'y2="{y2:.0f}" stroke="{stroke}" stroke-width="{wdt}"/>')
    for x, y, s, size, fill, anchor, bold in sc.texts:
        weight = ' font-weight="bold"' if bold else ""
        anc = f' text-anchor="{anchor}"' if anchor != "start" else ""
        p.append(f'<text x="{x:.0f}" y="{y:.0f}"{anc} font-family="{FONT_STACK}" '
                 f'font-size="{size:.0f}"{weight} fill="{fill}">{esc(s)}</text>')
    p.append(XML_TAIL)
    return "\n".join(p)


# ===========================================================================
# PNG 渲染
# ===========================================================================

def render_png(sc: Scene, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (sc.w, sc.h), BG_BOT)
    d = ImageDraw.Draw(img)
    # 竖直渐变（SVG 那边的 linearGradient 必须一一对应）
    for y in range(sc.h):
        t = y / max(sc.h - 1, 1)
        c = tuple(int(BG_TOP[i:i+2], 16) for i in (1, 3, 5))
        c2 = tuple(int(BG_BOT[i:i+2], 16) for i in (1, 3, 5))
        d.line([(0, y), (sc.w, y)],
               fill=tuple(int(a + (b - a) * t) for a, b in zip(c, c2)))

    for x, y, w, h, fill, rx in sc.rects:
        d.rounded_rectangle([x, y, x + w, y + h], radius=rx,
                            fill=fill)

    for x1, y1, x2, y2, stroke, wdt in sc.lines:
        d.line([(x1, y1), (x2, y2)], fill=stroke, width=int(wdt))

    font_path = _find_font(_FONT_CANDIDATES)
    bold_path = _find_font(_FONT_BOLD_CANDIDATES)
    cache: dict = {}
    for x, y, s, size, fill, anchor, bold in sc.texts:
        key = (int(size), bold)
        if key not in cache:
            fp = bold_path if bold else font_path
            cache[key] = ImageFont.truetype(fp, int(size))
        f = cache[key]
        # SVG 的 y 是**基线**，PIL 的 y 是**顶边**，差一个 ascender。
        # 不换算的话 PNG 里整段字会比 SVG 低一大截，两种格式对不上。
        bb = d.textbbox((0, 0), s, font=f)
        wpx = bb[2] - bb[0]
        asc = bb[1]
        if anchor == "middle":
            tx = x - wpx / 2
        elif anchor == "end":
            tx = x - wpx
        else:
            tx = x
        d.text((tx, y - asc), s, font=f, fill=fill)

    img.save(path, "PNG")
    # 存完立刻验一遍存在且非空。少这一句的话，"忘了 save"这种错误
    # 会一路静默到 main 里的 print——脚本打印 16 张图、退出码 0，
    # 而 charts/ 里一个文件都没有。Pillow 不会替你检查这件事。
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"PNG 落盘失败：{path}")


# ===========================================================================
# 绘图原语
# ===========================================================================

def _bg(sc: Scene) -> None:
    """背景已在 render_* 里铺好，这里保留接口以便将来加纹理。"""


def _frame(sc: Scene, title: str, subtitle: str = "") -> None:
    sc.text(70, 120, title, 52, FG, bold=True)
    if subtitle:
        sc.text(70, 175, subtitle, 30, MUTED)


def _footer(sc: Scene, note: str = "数据来源：公开政策文件与统计") -> None:
    sc.text(70, H - 60, note, 24, MUTED)


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


def _num(v: float) -> str:
    """去掉无意义的小数尾巴：188.0 → 188，-12.1 → -12.1"""
    return str(int(v)) if float(v).is_integer() else str(v)


def _bar_chart(name: str, title: str, rows: list[tuple[str, float]], unit: str,
               color: str = COOL, subtitle: str = "", highlight: str = "",
               fmt: str = "{label} {v}{unit}", label_gap: int = 26) -> Scene:
    """竖条图。负值用红色（那是跌/减，不是好数字）。

    数值标签的位置是这套图里最容易出错的地方。原来固定
    "正值放柱顶上方 20px、负值放柱底下方 50px"，于是：
      - 柱子特别高时，标签会跑到图外或压住轴标签（-1.03万亿 压"住户贷款"）
      - 柱子特别矮时，标签和轴标签重叠（-21% 压"开发端投资同比"）
    现在按"标签底边必须离开轴线一个身位"来推位置，越界就翻到另一侧。
    """
    sc = Scene()
    if not rows:
        return sc
    top, bottom = 300, H - 300
    vals = [v for _, v in rows]
    lo, hi = min(0, min(vals)), max(vals)
    # 顶部留一条标签带：柱子最高的那个，标签要画在它上方。
    # 之前 top 直接当绘图区上界，最高柱的标签被挤到画布外——图上就
    # 少了那个数字，而且没有任何报错（文字画到 y<0 会被 Pillow 丢掉）。
    pad_top = 70
    span = (hi - lo) or 1
    plot_h = (bottom - top) - pad_top
    zero_y = bottom - (0 - lo) / span * plot_h
    step = (W - 200) / len(rows)
    bw = min(step * 0.6, 150)
    _frame(sc, title, subtitle)
    sc.line(90, zero_y, W - 90, zero_y, MUTED, 2)

    for i, (lab, v) in enumerate(rows):
        cx = 100 + step * i + step / 2
        y = bottom - (v - lo) / span * plot_h
        c = ACCENT if v < 0 else color
        if highlight and lab == highlight:
            c = WARM
        sc.rect(cx - bw / 2, min(y, zero_y), bw, max(abs(bottom - y), 1), c, 6)

        head, _, sub = fmt.partition("\n")
        vs = head.replace("{label}", lab).replace(
            "{v}", _num(v)).replace("{unit}", unit)
        # 数值标签位置：正柱放柱顶上方；负柱放柱底下方**但仍在轴标签之上**。
        #
        # 踩过的坑：负柱的 y 恰好是绘图区底边 bottom，若按"y + 偏移"放，
        # 就正好落在轴标签那一行（bottom+62 附近），两串字糊成一团
        # （'新房销售面积 -12.1%' 压住 '新房销售面积同比'）。
        # 所以轴标签统一下移，给数值标签留出独立的一行。
        vy = y - label_gap if v >= 0 else bottom + VALUE_LABEL_DY
        sc.text(cx, vy, vs, 36, c, "middle", True)
        sc.text(cx, bottom + AXIS_LABEL_DY, lab, 24, MUTED, "middle")
        if sub:
            sc.text(cx, bottom + AXIS_LABEL_DY + 34, sub.replace("{label}", lab),
                    22, MUTED, "middle")
    _footer(sc)
    return sc


def _text_w(s: str, size: float) -> float:
    """估算文字宽度（px）。取不到字体时按"中文全角、其余半角"估。

    宁可高估：估窄了会让标签伸进柱子里（真实踩过），估宽了只是
    多留点空隙。
    """
    try:
        from PIL import ImageFont
        f = ImageFont.truetype(_find_font(_FONT_CANDIDATES), int(size))
        bb = f.getbbox(s)
        return bb[2] - bb[0]
    except Exception:
        w = 0.0
        for ch in str(s):
            w += size if ord(ch) > 0x2E80 else size * 0.55
        return w


def _cards(name: str, title: str, items: list[tuple[str, str]], subtitle: str = "",
           color: str = WARM) -> Scene:
    """条件卡：逐行出现用（分镜2/6）。"""
    sc = Scene()
    _frame(sc, title, subtitle)
    y = 280
    for k, v in items:
        sc.rect(90, y, W - 180, 120, "#1b1f28", 16)
        sc.rect(90, y, 6, 120, color, 3)
        sc.text(130, y + 72, k, 38, MUTED)
        sc.text(W - 130, y + 78, v, 46, color, "end", True)
        y += 150
    _footer(sc)
    return sc


def _big_statement(name: str, line1: str, line2: str, note: str = "",
                   color: str = GREEN) -> Scene:
    """纯字幕卡：停顿帧用。"""
    sc = Scene()
    sc.text(W / 2, H / 2 - 40, line1, 80, FG, "middle", True)
    sc.text(W / 2, H / 2 + 70, line2, 54, color, "middle", True)
    if note:
        _footer(sc, note)
    return sc


def _flow(name: str, title: str, nodes: list[str], subtitle: str = "") -> Scene:
    """流程图：需求侧补贴这类"钱往哪流"用。"""
    sc = Scene()
    _frame(sc, title, subtitle)
    n = len(nodes)
    bw = (W - 160) / n - 40
    for i, t in enumerate(nodes):
        x = 80 + i * ((W - 160) / n)
        c = COOL if i == 0 else (WARM if i == n - 1 else MUTED)
        sc.rect(x, 420, bw, 130, "#1b1f28", 18)
        sc.rect(x, 420, 6, 130, c, 3)
        sc.text(x + bw / 2, 500, t, 34, c, "middle", True)
        if i < n - 1:
            sc.text(x + bw + 8, 500, "→", 40, MUTED)
    sc.text(W / 2, 700, "总需求没有增加，只是转移了支付时点", 40, ACCENT,
            "middle", True)
    _footer(sc)
    return sc


# ===========================================================================
# 分镜清单
# ===========================================================================

def build_all() -> dict:
    """返回 {名字: Scene}。校验脚本也用它，所以布局错了一处就都发现。"""
    out: dict = {}

    # ---- 任务一 · 日本 ----
    out["s3_demand_side"] = _flow(
        "s3_demand_side", "需求侧补贴：钱换了口袋",
        ["财政", "购房者", "房价"], "总需求未变")

    rows = [(r["year"], float(r["value"])) for r in _load_csv("s4_newstarts.csv")
            if r.get("value")]
    out["s4_newstarts"] = _bar_chart(
        "s4_newstarts", "日本新开工户数（万户/年）", rows, "",
        COOL, "贴息三十年，峰值后再未回去", fmt="{v}")

    out["s5_divide"] = _cards(
        "s5_divide", "东京圈 vs 地方城市", [
            ("东京圈", "人口流入 · 仍在涨"),
            ("地方城市", "人口流出 · 空置 13–14%"),
            ("空置房", "约 900 万套"),
        ], "同一个国家，两种命运", COOL)

    rows = [(r["year"], float(r["value_pct"]))
            for r in _load_csv("s6_rate_curve.csv") if r.get("value_pct")]
    out["s6_rate_curve"] = _bar_chart(
        "s6_rate_curve", "利率越低，贴息越有效", rows, "%",
        COOL, "2023 起抬头，补贴相对意义下降", fmt="{v}{unit}")

    out["s7_sawtooth"] = _cards(
        "s7_sawtooth", "政策到期 = 一波抢购", [
            ("到期前", "抢购升温"),
            ("到期当月", "集中成交"),
            ("到期后 1–2 季", "回落"),
        ], "锯齿状，不是趋势", WARM)

    out["s8_lesson"] = _big_statement(
        "s8_lesson", "贴息改变的是", "购房时点 ≠ 购房总量", color=GREEN)

    # ---- 任务二 · 中国 ----
    out["s1_policy"] = _cards(
        "s1_policy", "财金〔2026〕95 号", [
            ("发布", "2026-09-29"),
            ("首次", "中央给房贷贴息"),
            ("定位", "暂定一年"),
            ("到期", "2027 年 9 月"),
        ], "中国房贷政策的重要一步", ACCENT)

    out["s2_conditions"] = _cards(
        "s2_conditions", "贴息条件", [
            ("房屋", "仅首套"),
            ("面积", "≤ 120 ㎡"),
            ("总价", "≤ 150 万元"),
            ("贴息", "1 个百分点"),
            ("年限", "最长 5 年"),
            ("单户上限", "100 万元"),
        ], "门槛清晰，不是普惠")

    rows = [(r["item"], float(r["rate_pct"]))
            for r in _load_csv("s3_rate_cut.csv") if r.get("rate_pct")]
    out["s3_rate_cut"] = _bar_chart(
        "s3_rate_cut", "贴息把利率拉低 1 个百分点", rows, "%",
        WARM, "3.1% → 2.1%", fmt="{v}{unit}")

    rows = _load_csv("s4_deleverage.csv")

    def _amt(r):
        v = float(r["value"])
        return v / 10000 if r["unit"] == "亿元" else v   # 亿元 → 万亿元

    # 这里刻意**不把金额和比率画在同一根轴上**：数据混了万亿元/亿元/%
    # 三种量纲，-1.03万亿 与 -12.1% 同轴时柱子高度完全不可比
    # （1.03 会把 12.1 压成看不见），而观众会照柱子高低下结论。
    out["s4_deleverage_amt"] = _bar_chart(
        "s4_deleverage_amt", "居民贷款：前 8 个月增量（万亿元）",
        [(r["item"].replace("_前8月增量", ""), _amt(r)) for r in rows
         if r.get("unit") in ("万亿元", "亿元")],
        "", COOL, "住户贷款 −1.03 万亿，中长期仅 +188 亿", fmt="{v} 万亿")

    # 房贷利率不放进来：3.1%/2.1% 是另一个话题（分镜3 单独讲），
    # 混进"去杠杆"会让人误以为利率也在跌。
    out["s4_deleverage_pct"] = _bar_chart(
        "s4_deleverage_pct", "市场端：新房与开发端双跌",
        [(r["item"], float(r["value"])) for r in rows
         if r.get("unit") == "%" and "利率" not in r["item"]],
        "%", WARM, "新房跌 · 开发端跌 · 二手逆势涨", fmt="{v}{unit}")

    out["s5_split"] = _bar_chart(
        "s5_split", "以价换量：需求跑去二手",
        [(r["market"], float(r["change_pct"]))
         for r in _load_csv("s5_split.csv") if r.get("change_pct")],
        "%", WARM, "新房跌，二手涨", fmt="{v}{unit}")

    out["s6_math"] = _cards(
        "s6_math", "算一笔账", [
            ("100 万 × 1% × 5 年", "≈ 省 5 万"),
            ("中央 : 地方", "90% : 10%"),
            ("上海月供差", "远不止 5 万"),
        ], "力度有限，且集中在刚需", WARM)

    out["s9_headwind"] = _cards(
        "s9_headwind", "三逆风", [
            ("利率", "已经到底"),
            ("居民", "主动去杠杆"),
            ("开发端", "投资 −21%"),
        ], "贴息是顺风工具，逆风时打折", ACCENT)

    out["s10_purpose"] = _big_statement(
        "s10_purpose", "社会政策 + 预期管理", "不是宏观刺激", color=GREEN)

    out["s11_expiry"] = _cards(
        "s11_expiry", "最大的变数", [
            ("2026-09", "政策发布"),
            ("2027-09", "到期"),
            ("续不续", "暂定一年"),
        ], "到期前见分晓", ACCENT)

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只生成名字含该串的图")
    ap.add_argument("--fmt", choices=["both", "svg", "png"], default="both")
    args = ap.parse_args()

    CH.mkdir(parents=True, exist_ok=True)
    want_png = args.fmt in ("both", "png")
    want_svg = args.fmt in ("both", "svg")
    print(f"渲染分镜图表（{'+'.join(f for f, on in
                                   (('SVG', want_svg), ('PNG', want_png)) if on)}）：")

    n = 0
    for name, sc in build_all().items():
        if args.only and args.only not in name:
            continue
        if want_svg:
            (CH / f"{name}.svg").write_text(render_svg(sc), encoding="utf-8")
            print(f"  {name}.svg")
        if want_png:
            render_png(sc, CH / f"{name}.png")
            print(f"  {name}.png")
        n += 1
    print(f"共 {n} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())