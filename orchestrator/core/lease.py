"""租约：owner+lease_until+fencing，限时持有权（Phase1§3）。

SQL只走 Store.lease_update/transition（禁裸sqlite3散落）；
时间参数走 Store.time_params（集中config/schedule.yaml，缺配置回退默认值）。
脑裂防护：任何写回带expect_fencing，旧owner直接StaleOwner丢弃。
"""
from __future__ import annotations
import json
import threading
from datetime import datetime, timedelta, timezone

from core.store import Store, Conflict, StoreError, StaleOwner, IllegalTransition
from core.utils import jlog


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


class Heartbeat:
    """子Agent独立线程：每heartbeat_s续ttl_s（Phase2§3/§5）。

    三条硬要求：
    1. 续租必须带回fencing——旧owner续租会被StaleOwner拒，这是脑裂下唯一防线；
    2. 续租失败（StaleOwner/DB错）必须**停心跳**并置lost标志，
       否则线程会一直对一个已经不属于自己的任务续租，制造假象；
    3. 线程必须daemon且可join——执行器finally里要停它，进程退出不能被它挂住。
    首个续租延迟一个周期再发：acquire刚成功时租约是满的，立即续是浪费。
    """

    def __init__(self, store: Store, plan_id: str, task_id: str, owner: str,
                 run_id: str = "", interval_s: int | None = None,
                 ttl_s: int | None = None):
        self.store = store
        self.plan_id = plan_id
        self.task_id = task_id
        self.owner = owner
        self.run_id = run_id
        self.interval_s = interval_s or store.time_params["heartbeat_s"]
        self.ttl_s = ttl_s or store.time_params["lease_ttl_s"]
        self.lost = False            # 续租失败→置位，执行器据此放弃写回
        self.beats = 0
        self.last_error: str = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _loop(self) -> None:
        consecutive_errors = 0
        while not self._stop.wait(self.interval_s):
            try:
                renew(self.store, self.plan_id, self.task_id, owner=self.owner,
                      ttl=self.ttl_s, run_id=self.run_id)
                self.beats += 1
                consecutive_errors = 0
            except StaleOwner as e:
                # 租约已被别人抢走：立刻停，别再续，也别假装还活着
                self.lost = True
                self.last_error = f"stale_owner:{e}"
                self.store.alert("P1", "heartbeat_lost", plan_id=self.plan_id,
                                 task_id=self.task_id, owner=self.owner,
                                 error=self.last_error)
                return
            except Exception as e:   # DB抖动等
                self.last_error = str(e)[:200]
                consecutive_errors += 1
                jlog(self.store.root, "WARN", "heartbeat_error",
                     plan_id=self.plan_id, task_id=self.task_id,
                     error=self.last_error)
                if consecutive_errors >= 3:
                    # 连续失败：租约大概率已过期被别人拿走，停止续租并置lost，
                    # 让执行器主动放弃写回（写回也会被fencing拒，但早失败更省资源）
                    self.lost = True
                    return

    def start(self) -> "Heartbeat":
        self._thread = threading.Thread(target=self._loop, name="hb",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> "Heartbeat":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def escalate_dead_letters(store: Store, run_id: str = "",
                          agent: str = "orchestrator",
                          sop_ref: str = "") -> list[dict]:
    """Phase2§3：RETRY **或** FAILED 且 attempts 已达上限 → DEAD_LETTER + P0告警。

    attempts只在RETRY→READY时+1，所以判据是"已经重试过max_attempts次"
    （attempts >= max_attempts）。死信三必填（Phase0§2.3）：
    reason/last_error/sop_ref——没有SOP链接的死信等于没人知道怎么处理。

    **为什么 RETRY 也要扫**（2026-10-03 加）：验收失败走 VERIFYING→RETRY，
    压根不经过 FAILED。原来只扫 FAILED，于是"验收一直失败"的任务
    没有任何放弃路径——实测 daily_report_2026-10-05/t1_tier attempts=7
    （上限 3）还在退避-重跑，每轮一次完整 LLM 调用，且永远进不了死信、
    没人会去看它。FAILED 和 RETRY 是两条平行的失败路径，出口必须同一个。
    """
    if not sop_ref:
        sop_ref = "docs/sop/dead-letter.md"
    out = []
    for status in ("FAILED", "RETRY"):
        for t in store.list_by_status(status):
            got = dead_letter_if_exhausted(store, t, run_id=run_id,
                                           agent=agent, sop_ref=sop_ref)
            if got is not None:
                out.append(got)
    return out


def dead_letter_if_exhausted(store: Store, task: dict, *, run_id: str = "",
                             agent: str = "orchestrator",
                             sop_ref: str = "") -> dict | None:
    """attempts 达上限才升死信；否则/并发冲突/状态已变 → None。

    escalate_dead_letters 和 promote_retries 共用这一个出口：
    判据和死信三必填只在一处，两条路径不会各判各的。
    """
    if not sop_ref:
        sop_ref = "docs/sop/dead-letter.md"
    if task["attempts"] < task["max_attempts"]:
        return None
    reason = _last_error_of(store, task)
    try:
        got = store.transition(
            task["plan_id"], task["task_id"], "DEAD_LETTER", agent=agent,
            run_id=run_id, expect_version=task["version"],
            payload={"reason": "max_attempts_exceeded",
                     "last_error": reason or "unknown",
                     "sop_ref": sop_ref,
                     "attempts": task["attempts"],
                     "max_attempts": task["max_attempts"]})
    except (Conflict, IllegalTransition):
        return None            # 并发改了版本/已被处理，下轮再扫
    store.alert("P0", "dead_letter", plan_id=task["plan_id"],
                task_id=task["task_id"], attempts=task["attempts"],
                last_error=(reason or "unknown")[:200])
    return got


def _last_error_of(store: Store, task: dict) -> str:
    """从该任务最近事件里翻出最后一条错误。

    key顺序有讲究：error/last_error 是执行器写的原始异常；detail 是验收写的
    `rule_2:no_claims_json;...`（带具体哪条规则挂了）；reason 是
    `verify_failed`/`backoff_elapsed` 这类枚举词，**信息量最低，放最后**——
    死信里的 last_error 要能直接看出该查什么，写个 "verify_failed" 等于没写。
    """
    evs = store.recent_events(task["plan_id"], limit=50)
    mine = [e for e in evs if e["task_id"] == task["task_id"]]
    for e in mine:
        try:
            payload = json.loads(e["payload"] or "{}")
        except ValueError:
            continue
        for key in ("error", "last_error", "detail", "reason"):
            if payload.get(key):
                return str(payload[key])[:2000]
    return ""


def backoff_delay_s(attempts: int, base: int = 60, cap: int = 3600) -> int:
    """Phase0§2.1指数退避 60*2^attempts，capped。

    上限很重要：不封顶的话attempts=12就是6.8小时，一条重试链能把队列堵死。
    """
    if attempts < 0:
        attempts = 0
    return min(base * (2 ** attempts), cap)


def request_cancel(store: Store, plan_id: str, task_id: str, reason: str,
                   expect_version: int, by: str = "orchestrator",
                   run_id: str = "") -> dict:
    """置cancel_requested旗标（SQL在Store.request_cancel内，此处只做语义转发）。

    执行器步间检查旗标 → 存checkpoint → RUNNING→READY（attempts不变）。"""
    return store.request_cancel(plan_id, task_id, reason=reason,
                                expect_version=expect_version, by=by,
                                run_id=run_id)
