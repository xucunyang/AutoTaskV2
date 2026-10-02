"""视频素材（图表卡片）的结构与数据一致性校验。

放在 orchestrator/tests 里，是因为视频素材一旦数字错了是**对外发布**
的错误：口播和图不一致，评论区当场就有人指出来；而图表是脚本生成的，
"跑完没报错"完全不代表画对了。

这些图进 git（可复现、可审阅），所以值得有测试锁住。
"""
from __future__ import annotations

import csv
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

VIDEO = Path(__file__).resolve().parents[2] / "video" / "中日房贷贴息对比"
CHART_DIR = VIDEO / "charts"
CHARTS = CHART_DIR          # 旧名保留，避免下面的用例全改
GEN = VIDEO / "scripts" / "make_charts.py"
CHECK = VIDEO / "scripts" / "check_charts.py"

pytestmark = pytest.mark.skipif(
    not CHARTS.exists(), reason="视频素材目录不存在（该目录是可选产物）")


def _texts(svg: Path) -> list[str]:
    root = ET.parse(svg).getroot()
    return [t.text for t in root.iter()
            if t.tag.endswith("text") and t.text and t.text.strip()]


def _csv_rows(name: str) -> list[dict]:
    p = CHARTS / name
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


# ---------------------------------------------------------------- 生成器

def test_generated_svg_are_wellformed_xml():
    """每张图都必须是完整 SVG 文档（有 <svg> 根元素）。

    真实踩过：_frame 只吐 <defs>…片段、收尾拼 </svg> 却没有开头，
    15 张图全是坏的，而生成脚本退出码是 0——浏览器会容错渲染，
    所以肉眼和退出码都发现不了，只有解析器能发现。
    """
    svgs = sorted(CHARTS.glob("*.svg"))
    assert len(svgs) >= 10, f"只找到 {len(svgs)} 张图，生成可能没跑全"
    for f in svgs:
        root = ET.parse(f).getroot()          # 解析失败即 test 失败
        assert root.tag.endswith("svg"), f"{f.name} 根元素是 {root.tag}"


def test_every_chart_has_readable_text():
    """不能有空图——全是非文字元素意味着观众看到一片空白。"""
    for f in sorted(CHARTS.glob("*.svg")):
        assert _texts(f), f"{f.name} 没有任何文字"


def test_png_files_exist_for_every_chart():
    """剪映吃不了 SVG，必须有 PNG。

    这条是真实踩过的：render_png 建完 Image 忘了 img.save()，
    脚本照样打印"共 16 张"、退出码 0，而 charts/ 里一个 PNG 都没有。
    所以校验必须落到"文件真的在磁盘上"，不能信生成脚本的自述。
    """
    names = sorted(f.stem for f in CHARTS.glob("*.svg"))
    assert names, "没有 SVG 可查"
    for n in names:
        p = CHARTS / f"{n}.png"
        assert p.exists(), f"{n}.png 不存在（剪映无法导入）"
        assert p.stat().st_size > 0, f"{n}.png 是空文件"


def test_png_has_correct_size_and_is_not_blank():
    """尺寸必须是 1080x1350，且不能是一张纯色图。"""
    from PIL import Image
    for f in sorted(CHARTS.glob("*.png")):
        im = Image.open(f)
        assert im.size == (1080, 1350), f"{f.name} 尺寸 {im.size} 不对"
        im.load()
        # 纯色/空白 = 渲染失败。多种颜色说明有抗锯齿文字或图形。
        colors = im.convert("RGB").getcolors(maxcolors=1 << 20)
        assert colors and len(colors) > 20, \
            f"{f.name} 只有 {len(colors) if colors else 0} 种颜色，疑似空白图"


def test_svg_and_png_are_generated_from_one_layout():
    """两种格式必须来自同一份 Scene，排版才可能一致。

    布局只写一遍是刻意的：SVG 与 PNG 各写一份画图逻辑的话，
    字号、坐标、基线换算迟早会漂（SVG 的 y 是基线、PIL 是顶边，
    差一个 ascender，不换算整段字会低一大截）。
    """
    sys.path.insert(0, str(VIDEO / "scripts"))
    import make_charts as mc
    scenes = mc.build_all()
    assert len(scenes) >= 10
    for name, sc in scenes.items():
        assert sc.texts, f"{name} 场景里没有任何文字"
        assert (CHART_DIR / f"{name}.svg").exists()
        assert (CHART_DIR / f"{name}.png").exists()


def test_labels_stay_inside_canvas_and_clear_of_axis_labels():
    """数值标签不得越出画布、也不得压住轴标签。

    两类都是**静默**失败：文字画到 y<0 会被 Pillow 直接丢弃
    （最高柱的数值标签整段消失，脚本退出码仍是 0）；标签与轴标签
    同高则两串字糊成一团。都必须靠检查发现，肉眼逐张数不可靠。
    """
    sys.path.insert(0, str(VIDEO / "scripts"))
    import make_charts as mc
    for name, sc in mc.build_all().items():
        for x, y, s, size, fill, anchor, bold in sc.texts:
            assert 0 <= y <= sc.h, f"{name}: 文字 {s!r} y={y} 越出画布"
            assert 0 <= x <= sc.w, f"{name}: 文字 {s!r} x={x} 越出画布"
        if not sc.rects:
            continue
        bottom = max(r[1] + r[3] for r in sc.rects)
        axis = [t for t in sc.texts if t[1] > bottom]
        vals = [t for t in sc.texts if t[5] == "middle" and t[6]
                and t[4] != mc.MUTED]
        for v in vals:
            for a in axis:
                if v is a:
                    continue
                if abs(v[1] - a[1]) < 24 and abs(v[0] - a[0]) < 90:
                    raise AssertionError(
                        f"{name}: 数值标签 {v[2]!r} 压住轴标签 {a[2]!r}")


def test_generator_is_reproducible(tmp_path):
    """重跑生成器必须得到同样的结果（素材要能复现、可审阅）。

    图表进了 git，如果生成器输出会漂移，那 git 里的图和脚本就对不上，
    审阅时看到的和观众看到的可能不是同一张。
    """
    before = {f.name: f.read_bytes() for f in CHARTS.glob("*.svg")}
    r = subprocess.run([sys.executable, str(GEN)], cwd=str(VIDEO),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    after = {f.name: f.read_bytes() for f in CHARTS.glob("*.svg")}
    assert before.keys() == after.keys()
    for name in before:
        assert before[name] == after[name], f"{name} 两次生成结果不同"


def test_checker_passes_on_current_assets():
    r = subprocess.run([sys.executable, str(CHECK)], cwd=str(VIDEO),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


# ---------------------------------------------------------------- 数据一致性

def test_newstarts_chart_shows_every_year_in_csv():
    rows = _csv_rows("s4_newstarts.csv")
    texts = " ".join(_texts(CHARTS / "s4_newstarts.svg"))
    for r in rows:
        assert r["year"] in texts, f"图上缺 {r['year']}"


def test_deleverage_amount_chart_does_not_mislabel_units():
    """亿元必须换算成万亿元，不能直接当万亿画。

    真实踩过：188 亿被渲染成「188 万亿」，等于在视频里把 188 亿说成
    188 万亿——放出去就是硬伤，且量级差 1e4，肉眼不容易发现。

    只查**数据标签**（柱子上的数值），不查副标题：副标题里
    "中长期仅 +188 亿" 是正确的人话，穷举字符串匹配会误报。
    """
    root = ET.parse(CHARTS / "s4_deleverage_amt.svg").getroot()
    labels = [t.text for t in root.iter() if t.tag.endswith("text")
              and t.text and t.text.strip()]
    # 柱子数值标签形如 "-1.03 万亿" / "0.0188 万亿"（不带中文名）
    data_labels = [t for t in labels
                   if t.endswith("万亿") and len(t) < 20 and "中长期" not in t]
    assert data_labels, "没找到数据标签，判定逻辑本身可能已失效"
    for t in data_labels:
        assert "188" not in t or "0.0188" in t, \
            f"{t!r}：188 亿元不应直接标成万亿"
    assert any("0.0188" in t for t in data_labels), \
        f"188 亿元应换算为 0.0188 万亿元，实际标签 {data_labels}"
    assert any("-1.03" in t for t in data_labels)


def test_pct_chart_excludes_rate_rows():
    """房贷利率不放进"去杠杆"图：混进去会让人以为利率也在跌。

    利率有独立分镜（贴息降息），这里画了会讲错一件事。
    """
    texts = " ".join(_texts(CHARTS / "s4_deleverage_pct.svg"))
    assert "利率" not in texts, "去杠杆图里不该出现房贷利率"
    assert "-12.1" in texts and "10.6" in texts and "-21" in texts


def test_no_chart_mixes_units():
    """同一张图里不能混多种量纲——柱子高度会不可比。"""
    for f in sorted(CHARTS.glob("*.svg")):
        t = " ".join(_texts(f))
        has_pct = "%" in t
        has_amt = ("万亿" in t) or ("亿元" in t)
        assert not (has_pct and has_amt), f"{f.name} 混了百分比与金额量纲"


def test_policy_card_matches_script_numbers():
    """条件卡必须与 douyin_script.md 里的口径一致（120㎡/150万/1pp/5年/100万）。"""
    t = " ".join(_texts(CHARTS / "s2_conditions.svg"))
    for must in ("120", "150", "1 个百分点", "5 年", "100"):
        assert must in t, f"条件卡缺 {must}"