# -*- coding: utf-8 -*-
"""R37 续·全品类锚点发现 runner（一体化慢速版）。

阶段1：等 trend 恢复（10 分钟一探，上限 6 小时）。
阶段2：GPU 7 实体补价格锚 + 非显卡 32 实体找首锚（含 5 个既有 CPU/内存/
       SSD 种子）——全部 match=False（品类标题匹配不了 series 全名，靠
       keyword + 人工遴选）、慢速档 (25,50)s、逐实体退避重试。

产物：artifacts/2026-10-15/discover_batch_v8/<分类>_<slug>.json
（含候选标题/价格/销量/试采点数），完成后由人工遴选入注册表。"""
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(r"E:\code\AutoTaskByMuseSpark1.3\orchestrator")
sys.path.insert(0, str(ROOT))
from tools.manmanbuy_backfill import SessionExpired, discover_cheap  # noqa: E402

STABLE = "https://item.jd.com/100164458263.html"
SLOW = (25, 50)

# ---- 目标构造：从实体目录读全部实体，按品类生成关键词与过滤规则 ----
cat = yaml.safe_load((ROOT / "config" / "contracts" / "hardware_entities.yaml")
                     .read_text(encoding="utf-8"))
GPU = {(e["identity"]["series"], e["identity"].get("spec") or "")
       for e in cat["entities"] if e["identity"]["category"] == "显卡"}

KW = {  # (category, series) -> 搜索关键词（spec 口径在标题里写法太多，靠人工遴选）
    ("CPU", "9800X3D"): "9800X3D", ("CPU", "9700X"): "9700X",
    ("CPU", "9600X"): "9600X", ("CPU", "7800X3D"): "7800X3D",
    ("CPU", "9950X3D"): "9950X3D", ("CPU", "12600KF"): "12600KF",
    ("CPU", "14600KF"): "14600KF", ("CPU", "14700K"): "14700K",
    ("CPU", "14900K"): "14900K", ("CPU", "245K"): "Ultra 245K",
    ("CPU", "265K"): "Ultra 265K", ("CPU", "270K"): "Ultra 270K",
    ("CPU", "285K"): "Ultra 285K",
    # 既有种子的长名 series（2026-10-15 事故修复：无关键词会被静默跳过）
    ("CPU", "Ryzen 7 9800X3D"): "9800X3D",
    ("CPU", "Ryzen 7 9700X"): "9700X",
    ("CPU", "Ryzen 5 9600X"): "9600X",
    ("CPU", "Core i5-14600KF"): "14600KF",
    ("CPU", "Core Ultra 7 265K"): "Ultra 265K",
    ("内存", "DDR5 6000"): "DDR5 6000 16G 内存",
    ("内存", "DDR5 5600"): "DDR5 5600 16G 内存",
    ("内存", "DDR5 6400"): "DDR5 6400 16G 内存",
    ("内存", "DDR5 7200"): "DDR5 7200 内存",
    ("内存", "DDR5 8000"): "DDR5 8000 内存",
    ("内存", "DDR5 6400"): "DDR5 6400 64G 内存",   # 64G 条目与 16G 条目 series 相同，后写覆盖
    ("固态硬盘", "致态 TiPlus7100"): "致态 TiPlus7100 1TB",
    ("固态硬盘", "三星 990 EVO"): "三星 990 EVO 1TB",
    ("固态硬盘", "WD SN770"): "WD SN770 1TB",
    ("固态硬盘", "致态 Ti600s"): "致态 Ti600s 512G",
    ("固态硬盘", "宏碁掠夺者"): "宏碁掠夺者 2TB",
    ("主板", "B650M"): "B650M 主板", ("主板", "B760M"): "B760M 主板",
    ("主板", "B850M"): "B850M 主板", ("主板", "X870E"): "X870E 主板",
    ("主板", "Z890"): "Z890 主板",
    ("主板", "Z890 Pacific"): "Z890 Pacific 主板",
    ("主板", "Z890 Vertex"): "Z890 Vertex 主板",
    ("主板", "Z890 GAMING X"): "Z890 GAMING X 主板",
}
REJECT = {
    "CPU": r"主板|套装|整机|水冷|散热",
    "内存": r"笔记本|TF|存储卡|服务器|DDR4",
    "固态硬盘": r"硬盘盒|移动硬|笔记本|SATA",
    "主板": r"套装",
}

# 键冲突处理：内存 (DDR5 6400) 出现两次（16G×2 / 64G×2）——按 spec 区分目标
targets = []
seen_key = set()
for e in cat["entities"]:
    ident = e["identity"]
    c, s, sp = ident["category"], ident["series"], ident.get("spec") or ""
    if c == "显卡":
        continue
    key = (c, s, sp)
    if key in seen_key:
        continue
    seen_key.add(key)
    kw = KW.get((c, s))
    if kw is None:
        print(f"!! 无关键词，跳过 {key}")
        continue
    if c == "内存" and sp.startswith("64"):
        kw = "DDR5 6400 64G 内存"
    if c == "内存" and s == "DDR5 6400" and not sp.startswith("64"):
        kw = "DDR5 6400 16G 内存"
    targets.append({"cat": c, "series": s, "spec": sp, "kw": kw,
                    "display": e.get("display_name") or s})

# GPU 7 实体补价格锚（match=True 走原防线）
GPU_PRICE = [("RTX 5050", "8GB"), ("RTX 5060", "8GB"), ("RTX 5070", "12GB"),
             ("RTX 5090D V2", "24GB"), ("RX 9060 XT", "16GB"),
             ("RX 9060 XT", "8GB"), ("RX 9070", "16GB")]
GK = {"RTX 5050": ["显卡 RTX 5050 8G", "RTX 5050 8G 显卡"],
      "RTX 5060": ["显卡 RTX 5060 8G"], "RTX 5070": ["显卡 RTX 5070 12G"],
      "RTX 5090D V2": ["显卡 RTX 5090D V2 24G"],
      "RX 9060 XT": ["显卡 RX 9060 XT 8G", "RX 9060 XT 8G 显卡"],
      "RX 9070": ["RX 9070 16G 显卡", "RX 9070 显卡"]}

# ---- 阶段1：等恢复 ----
from tools.manmanbuy_client import Mmb  # noqa: E402
mmb = None
for round_no in range(1, 37):
    if round_no > 1:
        time.sleep(600)
    if mmb is None:
        mmb = Mmb()
        if not mmb.load_session():
            print("未登录，退出", flush=True)
            sys.exit(2)
    try:
        t = mmb.trend(STABLE)
        pts = len(t.get("datePrice") or [])
    except Exception as e:
        pts = -1
        print(f"[{time.strftime('%H:%M')}] 探测异常: {e}", flush=True)
        continue
    print(f"[{time.strftime('%H:%M')}] round{round_no}: {pts} 点", flush=True)
    if pts >= 50:
        print("trend 已恢复，开始发现", flush=True)
        break
else:
    print("6 小时未恢复，退出", flush=True)
    sys.exit(1)

def gpu_kws(series, spec):
    g = spec.replace("GB", "G")
    if series == "RX 9070":
        return ["RX 9070 16G 显卡", "RX 9070 显卡"]
    return [f"显卡 {series} {g}", f"{series} {g} 显卡"]

# ---- 阶段2：发现 ----
out_dir = ROOT / "artifacts" / "2026-10-15" / "discover_batch_v8"
out_dir.mkdir(parents=True, exist_ok=True)

def run_one(slug, series, spec, kw, category, match, reject):
    try:
        r = discover_cheap(series, spec, kw, pages=1, top_cheap=3,
                           sleep_range=SLOW, match=match, reject=reject)
    except SessionExpired as e:
        print(f"  ! SessionExpired（{e}）", flush=True)
        r = None
    except Exception as e:
        print(f"  ! 异常（{e}）", flush=True)
        r = None
    r = r or {"keyword": kw, "candidates": []}
    r.update({"category": category, "series": series, "spec": spec,
              "keyword_used": kw})
    (out_dir / f"{slug}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(r.get("candidates") or [])
    print(f"  -> {n} 候选", flush=True)
    return n

done = 0
# 2a. GPU 7 价格锚
for series, spec in GPU_PRICE:
    slug = f"显卡_{series}_{spec}".replace(" ", "")
    if (out_dir / f"{slug}.json").exists():
        done += 1
        continue
    print(f"\n[GPU] {series} {spec}", flush=True)
    n = 0
    for kw in gpu_kws(series, spec):
        n = run_one(slug, series, spec, kw, "显卡", True, None)
        if n:
            break
        time.sleep(random.uniform(20, 40))
    done += 1
    time.sleep(random.uniform(8, 15))

# 2b. 非显卡 32 实体首锚
for tgt in targets:
    slug = f"{tgt['cat']}_{tgt['series']}_{tgt['spec']}".replace(" ", "")
    slug = slug.replace("(", "").replace(")", "").replace("×", "x")
    if (out_dir / f"{slug}.json").exists():
        done += 1
        continue
    print(f"\n[{tgt['cat']}] {tgt['series']} {tgt['spec']}", flush=True)
    run_one(slug, tgt["series"], tgt["spec"], tgt["kw"], tgt["cat"],
            False, REJECT.get(tgt["cat"]))
    done += 1
    time.sleep(random.uniform(8, 15))

print(f"\n===== 全部完成 {done} 实体，候选已落盘 {out_dir} =====", flush=True)
