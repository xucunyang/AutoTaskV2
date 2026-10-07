"""三条 housing 链的节奏与依赖——2026-10-07 用户拍板后钉下来的东西。

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


@pytest.mark.parametrize("job", ["housing_monthly", "housing_fortnight"])
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
    cities = [{"city": nm, "districts": [
        {"district": f"{nm}{i}区", "transactions": 10 + i, "avg_price_per_sqm": 20000 + i}
        for i in range(3)]} for nm in ("南京", "徐州")]
    base = {"as_of": "2026-10-08", "data_period": "2026-10-07", "cities": cities,
            "hot_communities": [{"city": "南京", "district": "鼓楼区", "name": f"小区{i}",
                                 "transactions_30d": 5, "avg_price_per_sqm": 60000}
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


def _valid(schema_name: str, data) -> bool:
    return not list(Draft7Validator(_schema(schema_name)).iter_errors(data))


def test_xz_schema_accepts_a_faithful_answer():
    assert _valid("housing_xznj.schema.json", _xz())


@pytest.mark.parametrize("label,data", [
    ("只给一座城", _xz(cities=[{"city": "南京", "districts": [
        {"district": f"鼓楼区{i}", "transactions": 5, "avg_price_per_sqm": 60000}
        for i in range(3)]}])),
    ("两座城都写南京", _xz(cities=[
        {"city": "南京", "districts": [{"district": f"区{i}", "transactions": 5,
                                       "avg_price_per_sqm": 60000} for i in range(3)]}
        for _ in range(2)])),
    ("城区不足 3 个（同级别区对照没做）", _xz(cities=[
        {"city": nm, "districts": [{"district": f"{nm}区{i}", "transactions": 5,
                                    "avg_price_per_sqm": 20000} for i in range(2)]}
        for nm in ("南京", "徐州")])),
    ("活跃小区不足 4 条", _xz(hot_communities=_xz()["hot_communities"][:3])),
    ("成交套数写成字符串", _xz(cities=[
        {"city": nm, "districts": [{"district": f"{nm}区{i}", "transactions": "5",
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