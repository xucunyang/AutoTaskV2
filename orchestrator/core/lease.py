"""租约：owner+lease_until+fencing，限时持有权（Phase1§3）。

SQL只走 Store.lease_update/transition（禁裸sqlite3散落）；
时间参数走 Store.time_params（集中config/schedule.yaml，缺配置回退默认值）。
脑裂防护：任何写回带expect_fencing，旧owner直接StaleOwner丢弃。
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

from core.store import Store, Conflict, StoreError, StaleOwner


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_expired(lease_until: str | None, now: datetime | None = None) -> bool:
    lu = _parse_utc(lease_until)
    if lu is None:
        return True
    now = now or datetime.now(timezone.utc)
    return lu <= now


def acquire(store: Store, plan_id: str, task_id: str, owner: str,
            ttl: int | None = None, run_id: str = "") -> dict:
    """仅READY或租约过期可拿；成功fencing+1。返回最新任务行。"""
    ttl = ttl if ttl is not None else store.time_params["lease_ttl_s"]
    t = store.get_task(plan_id, task_id)
    if t is None:
        raise StoreError(f"task_not_found:{plan_id}/{task_id}")
    if t["status"] != "READY" and not _is_expired(t["lease_until"]):
        raise Conflict(f"lease_held_by:{t['owner']}")
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    return store.lease_update(plan_id, task_id, owner=owner, lease_until=until,
                              bump_fencing=True,
                              audit_event={"run_id": run_id, "agent": owner,
                                           "payload": {"event": "LEASE_ACQUIRED",
                                                       "ttl_s": ttl}})


def renew(store: Store, plan_id: str, task_id: str, owner: str,
          ttl: int | None = None, run_id: str = "") -> dict:
    """心跳续租：owner必须匹配且fencing必须当前（旧owner直接StaleOwner）。"""
    ttl = ttl if ttl is not None else store.time_params["lease_ttl_s"]
    t = store.get_task(plan_id, task_id)
    if t is None:
        raise StoreError(f"task_not_found:{plan_id}/{task_id}")
    if t["owner"] != owner:
        raise StaleOwner(f"owner_mismatch:{t['owner']}")
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    return store.lease_update(plan_id, task_id, owner=owner, lease_until=until,
                              bump_fencing=True, expect_fencing=t["fencing_token"],
                              audit_event={"run_id": run_id, "agent": owner,
                                           "payload": {"event": "LEASE_RENEWED",
                                                       "ttl_s": ttl}})


def release(store: Store, plan_id: str, task_id: str, owner: str) -> dict:
    """仅owner+fencing匹配才清owner；SUBMITTED后由系统接管不再需要owner。"""
    t = store.get_task(plan_id, task_id)
    if t is None:
        raise StoreError(f"task_not_found:{plan_id}/{task_id}")
    if t["owner"] != owner:
        raise StaleOwner(f"owner_mismatch:{t['owner']}")
    return store.lease_update(plan_id, task_id, owner=None, lease_until=None,
                              bump_fencing=False,
                              expect_fencing=t["fencing_token"])


def recover_expired_leases(store: Store, run_id: str = "",
                           agent: str = "orchestrator") -> list[dict]:
    """扫RUNNING && lease_until<now → FAILED（被动失联，attempts+1在后续RETRY→READY）。
    与主动让出区分（让出不计attempts）。返回回收的任务行；版本冲突跳过下轮。
    FAILED→DEAD_LETTER升级是编排层职责，不在这里做。"""
    cands = [(t["plan_id"], t["task_id"], t["version"], t["lease_until"])
             for t in store.list_by_status("RUNNING")]
    now = datetime.now(timezone.utc)
    recovered = []
    for plan_id, task_id, version, lease_until in cands:
        if not _is_expired(lease_until, now):
            continue
        try:
            t = store.transition(plan_id, task_id, "FAILED", agent=agent,
                                 run_id=run_id, expect_version=version,
                                 payload={"reason": "lease_expired"})
            recovered.append(t)
        except Conflict:
            continue  # 并发改了版本，下轮再扫
    return recovered


def request_cancel(store: Store, plan_id: str, task_id: str, reason: str,
                   expect_version: int, by: str = "orchestrator",
                   run_id: str = "") -> dict:
    """置cancel_requested旗标（SQL在Store.request_cancel内，此处只做语义转发）。

    执行器步间检查旗标 → 存checkpoint → RUNNING→READY（attempts不变）。"""
    return store.request_cancel(plan_id, task_id, reason=reason,
                                expect_version=expect_version, by=by,
                                run_id=run_id)
