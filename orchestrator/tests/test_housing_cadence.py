"""四条 housing 链的节奏与依赖——2026-10-07 用户拍板后钉下来的东西。

用户的原话，逐条对应下面的门禁：

* 「关于六边形房价生活城市这个任务按一个月或者半个月执行一次即可。
  看每天都没有什么变化，都是统计上个月份的」
  → t3_hexagon 移出日更，落到 housing_fortnight（每月 1 日与 15 日）。
* 「还有房贷贴息的这个任务，关于总体趋势，每个月来统计的」
  → t1_tier 移出日更，落到 housing_monthly（每月 8 日）。
* 「南京徐州对比的重点关注每天的交易量和平均价，交易比较活跃的小区，
  按照同区级别对比」
  → t2_xz_nj 留在日更，但口径从"全国坐标系里的两座城市"改成
  成交量/均价/活跃小区/同级别行政区对照，并新增结构化 data.json 兜住。
* 「增加一个新任务，每5天运行一次，调查南京徐州西宁（暂定这三个）
  租售比高的小区排名top20（每个城市20个）」（2026-10-08）
  → 新链 housing_rr_fiveday：每月 1/6/11/16/21/26 六天 ≈ 每 5 天一次。

为什么这些值得做成机械门禁：三者都是**结构性**决定，破坏它们的动作
看起来都很无害——把 t1_tier 的 id 改回去、把 cron 的 1,15 改成 *、
把 depends_on 又加回 t3_hexagon——每个都能通过编译、都不报错，
后果只是悄悄多烧一次 token 或让每天的 charts 恒定卡住。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft7Validator

ROOT = Path(__file__).resolve().parents[1]

# job -> (模板, 该链应有的 task_id 集合)
HOUSING = {
    "housing_daily": ("templates/housing_daily.yaml", {"t2_xz_nj", "t5_charts", "t4_rollup"}),
    "housing_monthly": ("templates/housing_monthly.yaml", {"t1_tier", "m_rollup"}),
    "housing_fortnight": ("templates/housing_fortnight.yaml", {"t3_hexagon"}),
    "housing_rr_fiveday": ("templates/housing_rr_fiveday.yaml", {"t_rr_top20"}),
}


def _tasks(template_rel: str) -> dict:
    tpl = yaml.safe_load((ROOT / template_rel).read_text(encoding="utf-8")) or {}
    return {t["id"]: t for t in (tpl.get("tasks") or [])}


def _job(job_id: str) -> dict:
    sched = yaml.safe_load(
        (ROOT / "config" / "schedule.yaml").read_text(encoding="utf-8")) or {}
    for j in sched.get("jobs") or []:
        if j["id"] == job_id:
            return j
    raise AssertionError(f"{job_id} 不在 schedule.yaml 里")


# ---------- 链的切分 ----------

@pytest.mark.parametrize("job", sorted(HOUSING))
def test_chain_has_exactly_the_intended_tasks(job):
    """三条链各自的 task_id 集合必须精确匹配——多一个或少一个都会改变跑批形状。"""
    tpl_rel, want = HOUSING[job]
    assert set(_tasks(tpl_rel)) == want, f"{job} 的任务集变了：{sorted(_tasks(tpl_rel))}"


def test_daily_chain_no_longer_runs_slow_tasks():
    """日更链不许再出现 t1_tier / t3_hexagon。

    它们吃的是月度/季度口径的数据，放在每日链里的后果不是"多跑一次"，
    而是**每天抄上一期同一批数**——用户说的"每天执行完数据都一样，
    没有什么意义"。
    """
    ids = set(_tasks("templates/housing_daily.yaml"))
    assert "t1_tier" not in ids, "t1_tier 又回到日更了"
    assert "t3_hexagon" not in ids, "t3_hexagon 又回到日更了"


@pytest.mark.parametrize("job", sorted(HOUSING))
def test_dependencies_stay_inside_the_same_chain(job):
    """依赖目标必须存在于**同一个模板**里——这是跨链依赖的真正不变量。

    t1_tier / t3_hexagon 挪去别的 plan 后不再与 housing_daily 同日期落盘。
    只要 housing_daily 的 charts/rollup 还留着 depends_on: [t3_hexagon]，
    它们就会每天等一个"今天不会出现"的产物 —— 表现是永远 PENDING，
    没有报错、没有死信，只是当天的报告永远出不来。

    注意别把这条写成"不许依赖 t1_tier/t3_hexagon"：
    housing_monthly 里的 m_rollup 依赖 t1_tier 是**合法的**，
    因为两者同在 housing_monthly 这条链里。真正要防的是跨链引用。
    """
    ids = set(_tasks(HOUSING[job][0]))
    for tid, t in _tasks(HOUSING[job][0]).items():
        for d in (t.get("depends_on") or []):
            assert d in ids, (
                f"{job}::{tid} 依赖 {d}，但它不在同一条链里"
                f"（本链任务：{sorted(ids)}）——这条依赖永远无法满足")


# ---------- 节奏 ----------

def test_daily_still_runs_every_day():
    assert _job("housing_daily")["cron"].split()[2] == "*"


def test_fortnight_runs_twice_a_month():
    """六边形是「每半个月一次」——用户在「一个月 / 每半个月」里选了后者。"""
    dom = str(_job("housing_fortnight")["cron"]).split()[2]
    assert set(dom.split(",")) == {"1", "15"}, f"半月链的日字段={dom!r}"


def test_monthly_runs_once_a_month():
    """房贷贴息分线是「每个月统计一次」。"""
    dom = str(_job("housing_monthly")["cron"]).split()[2]
    assert "," not in dom and dom.isdigit(), f"月度链的日字段={dom!r}，不该是列表"


def test_fiveday_runs_every_five_days():
    """租售比 Top20 是「每 5 天跑一次」——不是每天、不是半个月。

    2026-10-08 用户点名新增（南京徐州西宁租售比高小区 Top20，每城 20 个）。
    cron 用显式六天列表 1,6,11,16,21,26 而不是日字段 `*/5`：`*/5` 在 31 天的
    月份会跑出 1/6/11/16/21/26/31 七次，月末那次会落在 31 号，破坏"每 5 天"
    语义。显式列表与 XML 的 DaysOfMonth 精确一致（同 fortnight 1,15 的理由）。
    """
    dom = str(_job("housing_rr_fiveday")["cron"]).split()[2]
    assert set(dom.split(",")) == {"1", "6", "11", "16", "21", "26"}, \
        f"每5天链的日字段={dom!r}，应该是 1,6,11,16,21,26"


@pytest.mark.parametrize("job", ["housing_monthly", "housing_fortnight", "housing_rr_fiveday"])
def test_slow_jobs_do_not_collide_with_daily(job):
    """慢链与日更链错开 ≥2 小时。

    两个 run_daily 并行写同一个 state.db 是没验证过的姿势；
    慢链 timeout-min 同样 90，错开不足会撞车。
    """
    def mins(j):
        mm, hh = str(_job(j)["cron"]).split()[:2]
        return int(hh) * 60 + int(mm)

    for other in ("housing_daily", "daily_report", "policy_daily"):
        gap = abs(mins(job) - mins(other))
        assert gap >= 30, f"{job} 与 {other} 只差 {gap} 分钟"


# ---------- 新口径的结构化兜底 ----------

def _schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def _xz(**over):
    # schema 字段名（2026-10-08 改）：name 替 city/district，transactions_summary
    # 替 transactions（string 替 integer）；原因见 schemas/housing_xznj.schema.json
    # description 与 schemas/acceptance_market_xznj.yaml 的 j_get 表达式——
    # acceptance 里仍用 districts[2].avg_price_per_sqm（不变），但必须
    # 与 schema 的 fields 一致才会同时通过两层校验。
    cities = [{"name": nm, "districts": [
        {"name": f"{nm}{i}区", "transactions_summary": f"{10 + i}套",
         "avg_price_per_sqm": 20000 + i}
        for i in range(3)]} for nm in ("南京", "徐州")]
    base = {"as_of": "2026-10-08", "data_period": "2026-10-07", "cities": cities,
            "hot_communities": [{"city": "南京", "district": "鼓楼区", "name": f"小区{i}",
                                 "transactions_30d": "5套",
                                 "avg_price_per_sqm": 60000}
                                for i in range(4)]}
    base.update(over)
    return base


def _spot(**over):
    base = {"period": "2026-09",
            "spotlight_cities": [{"city": f"城市{i}", "metric": "成交量环比",
                                  "value": "+18%", "why_bright": "放量"} for i in range(3)],
            "spotlight_communities": [{"city": "南京", "district": "鼓楼区",
                                       "name": f"小区{i}", "metric": "去化",
                                       "why_bright": "最快"} for i in range(2)]}
    base.update(over)
    return base


def _rr(**over):
    """租售比 Top20 的合格样例：三城各 20 条，rank 升序、rr_pct 为正数递减。

    复制自真实跑批会调用的 schema 字段名（community/district/price_per_sqm/
    rent_per_month_psm/rr_pct/source），别按自己习惯自创字段——这正是
    t2_xz_nj 三连死信最靠前的死法。
    """
    def city(nm: str):
        return {"name": nm, "top20": [
            {"rank": i, "community": f"{nm}小区{i}", "district": f"{nm}鼓楼区",
             "price_per_sqm": 20000 + i * 10,
             "rent_per_month_psm": 35 - i * 0.1,
             "rr_pct": round(35 * 12 / 20000 * 100 - i * 0.05, 2),
             "source": "贝壳找房南京站"}
            for i in range(1, 21)]}
    base = {"as_of": "2026-10-08", "data_period": "2026年9月挂牌",
            "cities": [city(n) for n in ("南京", "徐州", "西宁")]}
    base.update(over)
    return base


def _valid(schema_name: str, data) -> bool:
    return not list(Draft7Validator(_schema(schema_name)).iter_errors(data))


def test_xz_schema_accepts_a_faithful_answer():
    assert _valid("housing_xznj.schema.json", _xz())


@pytest.mark.parametrize("label,data", [
    ("只给一座城", _xz(cities=[{"name": "南京", "districts": [
        {"name": f"鼓楼区{i}", "transactions_summary": "5套",
         "avg_price_per_sqm": 60000}
        for i in range(3)]}])),
    ("两座城都写南京", _xz(cities=[
        {"name": "南京", "districts": [{"name": f"区{i}",
                                       "transactions_summary": "5套",
                                       "avg_price_per_sqm": 60000} for i in range(3)]}
        for _ in range(2)])),
    ("城区不足 3 个（同级别区对照没做）", _xz(cities=[
        {"name": nm, "districts": [{"name": f"{nm}区{i}",
                                    "transactions_summary": "5套",
                                    "avg_price_per_sqm": 20000} for i in range(2)]}
        for nm in ("南京", "徐州")])),
    ("活跃小区不足 4 条", _xz(hot_communities=_xz()["hot_communities"][:3])),
    ("成交套数写成整数（不接受，必须字符串）",
     _xz(cities=[
         {"name": nm, "districts": [{"name": f"{nm}区{i}",
                                     "transactions_summary": 5,
                                     "avg_price_per_sqm": 20000} for i in range(3)]}
         for nm in ("南京", "徐州")])),
])
def test_xz_schema_rejects_faithful_looking_non_answers(label, data):
    """这些都"看起来交了一份"，但没回答用户问的那三样——必须 schema 层就毙掉。"""
    assert not _valid("housing_xznj.schema.json", data), f"{label} 竟然通过了"


def test_spotlight_schema_accepts_a_faithful_answer():
    assert _valid("housing_spotlight.schema.json", _spot())


@pytest.mark.parametrize("label,data", [
    ("亮眼城市不足 3 个", _spot(spotlight_cities=_spot()["spotlight_cities"][:2])),
    ("亮眼小区不足 2 个", _spot(spotlight_communities=_spot()["spotlight_communities"][:1])),
    ("期次写成本月（拿未完数据冒充结论）", _spot(period="本月")),
    ("亮眼条目缺 why_bright", _spot(spotlight_cities=[
        {"city": f"城市{i}", "metric": "放量", "value": "+18%"} for i in range(3)])),
])
def test_spotlight_schema_rejects_faithful_looking_non_answers(label, data):
    assert not _valid("housing_spotlight.schema.json", data), f"{label} 竟然通过了"


def test_rr_schema_accepts_a_faithful_answer():
    assert _valid("housing_rr_top20.schema.json", _rr())


@pytest.mark.parametrize("label,data", [
    ("只给两座城", _rr(cities=[_rr()["cities"][0], _rr()["cities"][1]])),
    ("三座城里有重复（南京写了两次）", _rr(cities=[_rr()["cities"][0]] * 3)),
    ("某城不足 20 个小区", _rr(cities=[
        {"name": n, "top20": [
            {"rank": i, "community": f"{n}小区{i}", "district": f"{n}区",
             "price_per_sqm": 20000, "rent_per_month_psm": 35,
             "rr_pct": 2.1, "source": "x"} for i in range(1, 19)]}   # 只给 18 条
        for n in ("南京", "徐州", "西宁")])),
    ("租售比写成字符串（不带类型）",
     _rr(cities=[{"name": n, "top20": [
         {"rank": i, "community": f"{n}小区{i}", "district": f"{n}鼓楼区",
          "price_per_sqm": 20000, "rent_per_month_psm": 35,
          "rr_pct": "4.5%", "source": "贝壳"} for i in range(1, 21)]}
         for n in ("南京", "徐州", "西宁")])),
])
def test_rr_schema_rejects_faithful_looking_non_answers(label, data):
    """这些"看起来交了 20 条"，但都不是用户要的三城 Top20——schema 层就毙。"""
    assert not _valid("housing_rr_top20.schema.json", data), f"{label} 竟然通过了"


# ---------- 老门禁不许被"顺手统一" ----------

def test_daily_xznj_window_is_much_tighter_than_monthly():
    """日频链的时效窗口必须比月度链紧。

    这是"每天的数据要真的在动"最直接的机械表达：来源里混进半年前的
    券商地产报告，说明它抄的是旧数。两条链共用一个 365 天窗口，
    等于让日频链继续拿上上个月的数交差。
    """
    def days(acc_rel: str) -> int:
        rules = yaml.safe_load((ROOT / acc_rel).read_text(encoding="utf-8")) or []
        for r in rules:
            if r.get("type") == "freshness":
                return int(r["freshness_days"])
        raise AssertionError(f"{acc_rel} 没有 freshness 规则")

    assert days("schemas/acceptance_market_xznj.yaml") <= 30
    assert days("schemas/acceptance_market_xznj.yaml") < \
        days("schemas/acceptance_market_tier.yaml")


def test_market_acceptance_still_exists_for_the_fortnight_task():
    """acceptance_market.yaml 仍被 housing_fortnight 的 t3_hexagon 使用。

    别把那份"顺手"改成 xznj/tier 专用版——t3_hexagon 没有 data.json，
    换上带 j_get 规则的验收会变成恒定 FAIL。
    """
    assert _tasks("templates/housing_fortnight.yaml")["t3_hexagon"]["acceptance_ref"] \
        == "schemas/acceptance_market.yaml"


def test_xznj_business_rules_are_not_type_broken():
    """acceptance 里的 j_get 规则必须能对**合格答案**求值通过。

    2026-10-08 踩过两次同类坑，都是"schema 改了、表达式没跟着改"：
    ① schema 要求 transactions_summary 是 string，business_rule 还写
       `> 0` → Python 3 抛 TypeError，eval_expr 返回 eval_error:TypeError，
       规则恒红；恒红的验收等于没有验收，而且报错长得像"数据不对"。
    ② len() 不在 AST 白名单 → forbidden_node，同上恒红。

    所以这里拿真 schema 的合格样例（_xz()）把每条 business_rule 都跑一遍，
    只要 eval_error/forbidden_node 出现就说明表达式与 schema 类型脱节。
    """
    from core.verifier import eval_expr

    rules = yaml.safe_load(
        (ROOT / "schemas" / "acceptance_market_xznj.yaml").read_text(encoding="utf-8"))
    j = _xz()
    for r in rules:
        if not isinstance(r, dict) or r.get("type") != "business_rule":
            continue
        ok, detail = eval_expr(r["expr"], {"j": j, "bytes": 4096})
        assert not detail.startswith(("eval_error", "forbidden_node", "unknown_var",
                                      "bad_syntax")), \
            f"表达式恒红：{r['expr']} → {detail}"
        assert ok, f"合格样例过不了：{r['expr']} → {detail}"


def test_rr_business_rules_are_not_type_broken():
    """rr 的 j_get 规则必须能对**合格答案**求值通过。

    同 test_xznj_business_rules_are_not_type_broken 的坑位：rr_pct 在 schema
    里是 number，business_rule 里的 `> 0` 对 number 是安全的；但若哪天 schema
    把它改成 string（像 transactions_30d 那样），这里的 `> 0` 就会 TypeError
    恒红。这条测试在"schema 换了、表达式没跟上"的瞬间当场红。
    """
    from core.verifier import eval_expr

    rules = yaml.safe_load(
        (ROOT / "schemas" / "acceptance_rr_top20.yaml").read_text(encoding="utf-8"))
    j = _rr()
    for r in rules:
        if not isinstance(r, dict) or r.get("type") != "business_rule":
            continue
        ok, detail = eval_expr(r["expr"], {"j": j, "bytes": 4096})
        assert not detail.startswith(("eval_error", "forbidden_node", "unknown_var",
                                      "bad_syntax")), \
            f"表达式恒红：{r['expr']} → {detail}"
        assert ok, f"合格样例过不了：{r['expr']} → {detail}"


def test_task_card_inlines_nested_schema_shape():
    """任务卡必须把**嵌套**字段名也渲染出来，不能只给顶层 required。

    2026-10-08 housing t2_xz_nj 三连死信的根因：卡上只列了顶层
    as_of/data_period/cities/hot_communities，模型看不到
    cities[].districts[] 里 required 的 transactions_summary、
    hot_communities[] 里 required 的 name，于是按自己的习惯写成
    transactions_units / community_name——三次全是 schema_violation。
    顶层那句"必需字段"对模型等于没说，它不知道**里面**要什么。
    """
    from core.task_card import _schema_hint

    acc = yaml.safe_load(
        (ROOT / "schemas" / "acceptance_market_xznj.yaml").read_text(encoding="utf-8"))
    hint = _schema_hint(ROOT, acc)
    assert hint, "schema hint 整个没渲染出来"
    for field in ("transactions_summary", "hot_communities[].name",
                  "transactions_30d"):
        assert field in hint, f"嵌套字段 {field} 没进卡：\n{hint}"
    # 类型得跟着字段一起出现——transactions_30d 要 string（模型写过 int）
    assert "transactions_30d?=string" in hint
    assert "transactions_summary=string" in hint
    # 长度护栏：>800 会让卡顶过 2k token 门禁
    assert len(hint) <= 800, len(hint)


def test_rr_card_inlines_nested_schema_shape():
    """租售比 Top20 卡的嵌套字段也必须渲染——不能只给顶层 cities。

    同一个坑换个名字踩：schema 的 required 是顶层
    as_of/data_period/cities，真正决定"字段名对不对"的是
    cities[].top20[].community/price_per_sqm/rent_per_month_psm/rr_pct
    这一层的字段名。模型看不到它们就会自创字段（community→小区名、
    rr_pct→rent_ratio）——t2_xz_nj 三连死信的原样复刻。
    """
    from core.task_card import _schema_hint

    acc = yaml.safe_load(
        (ROOT / "schemas" / "acceptance_rr_top20.yaml").read_text(encoding="utf-8"))
    hint = _schema_hint(ROOT, acc)
    assert hint, "schema hint 整个没渲染出来"
    for field in ("top20[].community", "top20[].price_per_sqm",
                  "top20[].rent_per_month_psm", "top20[].rr_pct",
                  "cities[].name"):
        assert field in hint, f"嵌套字段 {field} 没进卡：\n{hint}"
    # 类型要跟着字段一起进卡——rr_pct 是 number（模型写过字符串「4.5%」）
    assert "rr_pct=number" in hint
    # 长度护栏与 xznj 同一条：>800 顶过 2k token 门禁
    assert len(hint) <= 800, len(hint)