"""四条每日链的矩阵一致性：产物不撞车、cron 与计划任务对得上。

三条都不是理论问题，是 2026-10-05 实测出来的：

1. `charts.md` 曾被 daily(08:00) 与 housing(10:00) **共用**：后跑的把先跑的
   覆盖掉——housing 的 t5_charts 把该文件标题改成了 housing_daily，而 daily 的
   report.md（当天 16:01 补跑时贴图）里的 13 张图与 housing 的 charts.md
   **逐字相同**，日报正文贴的是另一条链画的图，daily 自己的产物已被冲没。
2. `schedule.yaml` 里 housing 的 cron 长期写着 08:00，而 Windows 计划任务
   的 StartBoundary 一直是 10:00——谁哪天启了 APScheduler 守护进程，
   两条链就会挤在 8 点同时跑。
3. 计划任务 XML 必须 UTF-16，且 WakeToRun/StartWhenAvailable 决定"电脑还在睡
   时到底会不会自己醒"，抄漏一个就是"睡过头"。

所以这三条都做成机械门禁，不靠记性。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _schedule() -> dict:
    p = ROOT / "config" / "schedule.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _templates() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in sorted((ROOT / "templates").glob("*.yaml")):
        out[p.name] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return out


def test_no_two_tasks_share_an_output_path():
    """任何两个任务都不许声明同一个产物路径——共用 = 后跑的覆盖先跑的。"""
    owner: dict[str, tuple[str, str]] = {}
    for name, tpl in _templates().items():
        for t in tpl.get("tasks") or []:
            for o in t.get("outputs") or []:
                if o in owner:
                    prev = owner[o]
                    pytest.fail(
                        f"{o} 被两条链同时声明产出：{prev[0]}::{prev[1]} 与 "
                        f"{name}::{t.get('id')}。共用路径会让后跑的覆盖先跑的"
                        f"（2026-10-05 实测：日报里的图成了另一条链的图）")
                owner[o] = (name, str(t.get("id")))
    assert len(owner) >= 15, f"只扫到 {len(owner)} 条产物，模板解析可能空跑"


def test_housing_no_longer_writes_daily_charts():
    """分家的回归钉子：housing 不许再碰 daily 的 charts.md。"""
    tpl = _templates().get("housing_daily.yaml") or {}
    outs = [o for t in tpl.get("tasks") or [] for o in t.get("outputs") or []]
    assert "artifacts/{date}/charts.md" not in outs, "housing 又写回 charts.md 了"
    assert "artifacts/{date}/charts_housing.md" in outs, "housing 的图表产物改名丢了"
    # daily 的图表产物保持原名，别被"顺手统一"掉
    daily = _templates().get("daily_plan.yaml") or {}
    d_outs = [o for t in daily.get("tasks") or [] for o in t.get("outputs") or []]
    assert "artifacts/{date}/charts.md" in d_outs, "daily 的 charts.md 不见了"


def test_every_job_has_matching_scheduled_task_xml():
    """schedule.yaml 的每个 job 都要有计划任务 XML，且 cron 与 StartBoundary 一致。"""
    jobs = _schedule().get("jobs") or []
    assert len(jobs) >= 4, f"schedule.yaml 只有 {len(jobs)} 个 job，解析空跑？"
    for job in jobs:
        xml = ROOT / "scripts" / f"{job['id']}_task.xml"
        assert xml.exists(), f"{job['id']} 缺计划任务 XML：{xml.name}"
        raw = xml.read_bytes()
        assert raw[:2] in (b"\xff\xfe", b"\xfe\xff"), \
            f"{xml.name} 必须是 UTF-16（含中文，UTF-8 会被 schtasks 读坏）"
        text = raw.decode("utf-16")
        mm, hh = str(job["cron"]).split()[:2]
        expect = f"T{int(hh):02d}:{int(mm):02d}:00"
        assert expect in text, (
            f"{job['id']} 的 cron={job['cron']} 与 XML StartBoundary 对不上"
            f"（XML 里找不到 {expect}）——改了一边忘另一边，实测踩过")
        assert f"--job {job['id']}" in text, f"{xml.name} 里没有 --job {job['id']}"
        # 这两条是"电脑 8 点还在睡"的全部指望：错过要补跑、睡着要唤醒
        assert "<WakeToRun>true</WakeToRun>" in text, f"{xml.name} 没勾唤醒计算机"
        assert "<StartWhenAvailable>true</StartWhenAvailable>" in text, \
            f"{xml.name} 没勾「错过时尽快补跑」"


def _standalone_wrapper_xmls() -> set[str]:
    """不走编排器的独立 wrapper 任务名 = make_task_xml._CUSTOM_ARGS 的键。

    importlib 按文件加载（scripts/ 无 __init__，与 test_mmb_backfill_daily
    同模式）；make_task_xml 顶层只有数据定义，import 无副作用。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "make_task_xml", ROOT / "scripts" / "make_task_xml.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod._CUSTOM_ARGS)


def test_no_orphan_scheduled_task_xml():
    """反过来：磁盘上不该躺着没人调度的孤儿 XML。

    例外：不走编排器的独立 wrapper（make_task_xml._CUSTOM_ARGS，如
    mmb_backfill_daily）——没有 template/plan，schedule.yaml 是 APScheduler
    语义（template/max_workers/catchup），硬塞进去会同时炸掉模板存在性与
    cron 对齐两条。豁免条件钉死：action 里必须**不**含 run_daily——一旦调
    编排器就必须进 schedule.yaml 走矩阵三件套，没有第三条路。
    """
    ids = {j["id"] for j in _schedule().get("jobs") or []}
    standalone = _standalone_wrapper_xmls()
    for xml in sorted((ROOT / "scripts").glob("*_task.xml")):
        name = xml.name[: -len("_task.xml")]
        if name in standalone:
            text = xml.read_bytes().decode("utf-16")
            assert "run_daily.py" not in text, (
                f"{xml.name} 调编排器却不在 schedule.yaml 里——要么进矩阵，"
                f"要么删 XML，两者必居其一")
            continue
        assert name in ids, f"{xml.name} 对应的 job 不在 schedule.yaml 里（孤儿任务）"


def test_schedule_templates_exist():
    """schedule 指向的模板必须真实存在——指错了就是那个 job 静默不跑。"""
    for job in _schedule().get("jobs") or []:
        tpl = ROOT / str(job.get("template"))
        assert tpl.exists(), f"{job['id']} 的模板不存在：{tpl}"


def test_jobs_are_spread_out_in_time():
    """相邻 job 至少错开 30 分钟：两个 run_daily 并行写 state.db 没验证过。"""
    jobs = _schedule().get("jobs") or []
    # cron 是 "分 时 * * *"——先分钟后小时，写反了会算出 1808 这种值
    times = sorted(int(hh) * 60 + int(mm)
                   for mm, hh in (str(j["cron"]).split()[:2] for j in jobs))
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= 30, (
        f"有两个 job 挤在 30 分钟内：{times}（gap={gaps}）")
