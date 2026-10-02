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
CHARTS = VIDEO / "charts"
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