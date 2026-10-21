"""DAG 校验：环 / 孤儿依赖 / 自依赖（§2：FAILED+告警，不派发）。

为什么从 Orchestrator 类里抽出来：verify_orchestrator.final_consistency
原本用 `Orchestrator.__new__(Orchestrator)` 造半初始化实例来借这一个
方法——循环依赖 + 黑客手法二合一。校验是纯函数逻辑（只吃 store 的
读取接口），不配待在主循环类里。

层次契约里本模块位于数据层（store 同层）：只依赖 utils，谁都可以用。
"""
from __future__ import annotations

from core.utils import jlog


def validate_dag(store, root, plan_id: str) -> list[dict]:
    """校验计划的依赖图。返回问题列表，空列表 = 通过。

    store 只用到一个读取方法 list_plan_tasks——鸭子类型即可，
    不 import core.store，避免数据层内部互相纠缠。
    """
    bad = []
    tasks = {t["task_id"]: t for t in store.list_plan_tasks(plan_id)}
    for t in tasks.values():
        deps = (t["shard"] or {}).get("depends_on") or []
        for d in deps:
            if d == t["task_id"]:
                bad.append({"task_id": t["task_id"], "problem": "self_dependency"})
            elif d not in tasks:
                bad.append({"task_id": t["task_id"],
                            "problem": "missing_dependency", "missing": d})
    # 环检测（DFS三色）
    colour: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = 1
        for d in (tasks.get(node, {}).get("shard", {}) or {}).get("depends_on") or []:
            if d not in tasks:
                continue
            if colour.get(d) == 1:
                bad.append({"task_id": d, "problem": "cycle",
                            "cycle": stack + [node, d]})
            elif colour.get(d, 0) == 0:
                visit(d, stack + [node])
        colour[node] = 2

    for tid in tasks:
        if colour.get(tid, 0) == 0:
            visit(tid, [])
    if bad:
        for b in bad:
            store.alert("P1", "dag_invalid", plan_id=plan_id, **b)
        jlog(root, "WARN", "dag_invalid", plan_id=plan_id, count=len(bad))
    return bad
