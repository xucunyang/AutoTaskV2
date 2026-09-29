# 架构评审意见：生产级视角（Blocker / Major / Minor）

评审对象：`task.md` + `design/plans/IMPLEMENTATION_PLAN.md` + `design/phases/00 + Phase0~4` + `orchestrator/`现有初稿
评审立场：单机SQLite+APScheduler直达生产，FIFO+优先级抢占为新增核心诉求
结论：方向成立，但有3个Blocker必须改，否则高优抢占和FIFO语义无法兑现；另有9个Major建议改，6个Minor优化。

---

## Blocker（不改不能开工）

### B1. `queue_items`与`tasks`双表冗余，会写出不一致
现状：Phase1同时有`queue_items(status/seq)`和`tasks(priority/seq)`，两表都要维护入队、派发、完成，崩溃时必然对不上（例如queue已DONE但task仍RUNNING）。
修改：
- 二选一。推荐**单表制**：删`queue_items`，只留`tasks`为唯一队列。管道入口直接`INSERT tasks(PENDING)`，`inbox`文件本身就是ingress log（`inbox/done|error`即凭据），不需要第二张队列表。
- 若坚持双表，必须定义`queue_items`为只追加的ingress log（只INSERT不UPDATE），`tasks`为执行态，两者以`idempotency_key`关联，`queue.status`只由`enqueue`写一次，后续只读。用事务保证`INSERT queue + INSERT task`原子，但绝不双向同步状态。
- 同步改Phase0的`QueueItem`定义和Phase1 DDL、Phase4的replay断言（只replay tasks+events）。

### B2. 抢占与租约回收是两条互斥的写路径，会打架
现状：`recover_expired_leases(RUNNING→RETRY)`和`force_revoke(RUNNING→PREEMPTED)`都会对同一RUNNING行加`fencing+1`，60s宽限 vs 300s ttl vs 30s心跳三套计时器无优先级，旧owner、心跳线程、主循环三方并发写必出`Conflict/StaleOwner`误杀。
修改：
- 统一为**单一状态机出口**：`RUNNING`只允许→`SUBMITTED/FAILED/PREEMPTED/CANCELLED`，取消`RUNNING→RETRY`直跳。过期回收也先走`RUNNING→PREEMPTED(reason=lease_expired)`再`PREEMPTED→READY/RETRY`，`attempts`是否+1由`reason`决定（抢占不加，过期加）。
- `cancel_requested`从BOOL字段升级为**显式事件**：`events`记`CANCEL_REQUESTED{reason, by}`，`transition`增加`expect_cancel`校验，心跳线程只读不写。
- 时间参数收敛到一处`config/schedule.yaml`：`lease_ttl=120s, heartbeat=30s, cancel_grace=30s(管道高优) / 60s(默认), poll_normal=10s, poll_fast=2s`，并在Phase1给出对照表，消除三处魔法数字打架。

### B3. 全局`ORDER BY priority,seq`与DAG依赖冲突，高优可能永远不可派发却持续触发抢占
现状：Phase2先`maybe_preempt`再`refresh_ready`，且`peek_high_priority_ready`只看READY。高优任务若`depends_on`未满足（仍BLOCKED），它不在READY里，但直觉上用户仍期望“高优链路优先”。反之若高优READY了却因依赖阻塞低优的下游，会出现抢占了低优但高优仍跑不起来，白白中断。
修改：
- 抢占触发条件改为**可派发高优**：`peek`必须查`READY`且`依赖已满足`，BLOCKED高优不触发抢占，只触发依赖链加速（优先派发其上游）。
- 主循环顺序固定为：`ingest → refresh_ready(全plan) → apply_aging → maybe_preempt(基于刷新后的READY) → dispatch`。当前伪代码顺序是错的。
- `pick_victim`加约束：不抢占被高优依赖链上的任务（例如高优t2依赖低优t1的产物，抢t1等于自杀）。实现：victim候选排除`high_transitive_deps`。

---

## Major（强烈建议改，否则生产会痛）

### M1. seq分配必须用AUTOINCREMENT，`max(seq)+1`并发必重
现状只写“全局自增”，未给实现。SQLite下`SELECT max+1`两并发必拿同一seq，FIFO即破。
改：单列`global_seq(id INTEGER PRIMARY KEY AUTOINCREMENT, ...)`或`tasks.seq`直接用AUTOINCREMENT别名，`enqueue`只INSERT不指定seq，返回`lastrowid`。重启不回退天然满足。Phase1补DDL和`next_seq()`删除。

### M2. 轮询 relay 无法兑现“及时中断”，需事件唤醒
10s轮询+2s快路径最坏延迟仍`2s+5s(步间检查)+60s宽限=67s`，与“<15s”DoD矛盾。且inbox 5s扫+APScheduler cron都是轮询，高优路径全是轮询叠加。
改：最小事件机制——`enqueue/inbox`写入后`touch orchestrator/wakeup.flag` + 主循环用`wait(timeout)`替代`sleep`，被touch立即醒；`cancel_requested`由心跳长轮询改为执行器每步前`SELECT`（已是步间检查，保留）+ 主循环`request_cancel`后直接`SIG`-free的DB通知（单机可用`threading.Event`，多进程用flag文件）。DoD的15s改为`p95<15s, p99<30s`，CI用模拟时钟断言而非 wall-clock。

### M3. 可抢占必须以幂等为前置，否则重跑有副作用
现状`preemptible=true`默认给Daily，但Daily的采集/写产物若非幂等（如append写），被抢占续跑会 double-write。
改：Phase0加`idempotent: bool`字段，`preemptible=true`要求`idempotent=true`，`enqueue`校验拒绝`preemptible && !idempotent`。非幂等任务只能`preemptible=false`，高优来时只能排队不能抢它（`pick_victim`跳过）。Phase4加`test_preempt_requires_idempotent`。

### M4. 低优反复被抢会活锁，需抢占预算
保留原seq回队 + 高优连续到来 = 同一低优被抢N次永不完成。
改：每任务加`preempt_count`，`>3`则本次不再被选为victim（换次优victim），且`preempt_count>=3`的任务下次派发带`anti_starve_boost`（临时priority-1）。`AGED`只提升一次不够，改为每次老化检查都可提升一档（10→5→3），上限定3。Phase2 §4重写，Phase3 metrics加`preempt_count histogram`。

### M5. 预留槽逻辑与ORDER BY矛盾，需两阶段查询
“至少1槽给低优”无法用单条`ORDER BY`表达。
改：明确实现为两阶段：`slot_high = max_workers-1`查`priority<=4`，`slot_low = 1`查`priority>=5`（若低优等待>5min）。无高优时低优可用全槽。Phase2伪代码补`get_ready_ordered_split()`，Phase4加预留槽单测。

### M6. Store连接/线程模型缺失，SQLite WAL单写者会被打爆
现状`transition()`伪代码无连接池、无`threading.local`、无`BEGIN IMMEDIATE`重试。主循环+心跳+verifier+enqueue四线程并发写必`database is locked`。
改：Phase1补`Store`类设计：单写线程（写队列串行化）+ WAL读并发；`busy_timeout=5000` + 写重试3次指数退避；所有写走`with store.write_txn():`上下文；`export`移出事务（已做是对的，但要明确export失败只告警不回滚）。给出`Store`接口（`transition/get_task/list_ready/insert_task`），为将来切PG留抽象层，而不是直接裸`sqlite3`散落各处。

### M7. Verifier同步阻塞主循环，高优验证会排队
`dispatch_verify`若同步跑`pytest 120s`，主循环卡死，高优来了也派不出。
改：Verifier独立worker池（`max_verify_workers=2`），主循环只做`SUBMITTED→VERIFYING`占位，实际`verify()`异步执行，完成后回调`VERIFYING→DONE/RETRY`。高优任务的验证优先级同样`priority`排序。Phase2主循环拆`dispatch_verify_async`，Phase3补verifier队列积压指标。

### M8. 告警设计只有分级无去重/静默，必成告警风暴
每次抢占P1 + 每次RETRY P1，管道突发100任务即100条群消息。
改：Phase3补`AlertPolicy{dedup_window=5min, max_per_hour per level, silence_for SKIPPED_CACHED}`，`Notifier.send`先查`alerts.jsonl`去重，同`task_id+reason`5min内合并。P0电话只留`DEAD_LETTER/INCONSISTENT/高优SLA超时`三项，其余降P1。

### M9. 安全与配额：inbox是外部输入，无设限必被打穿
`inbox/*.json`任意大小、任意路径、任意cmd（经acceptance透传）= RCE+磁盘爆。
改：Phase1补`enqueue`硬限制：文件`<100KB`、`task_id ^[a-z0-9_]{1-64}$`、路径必须`artifacts/{date}/`内、`acceptance.cmd`白名单校验前置（不在enqueue拦，后面全白搭）、单用户/单小时入队上限（如100）。`business_rule`求值器给出AST白名单节点清单（`Compare/BoolOp/Name/Constant`仅此四种）。

---

## Minor（顺手改）

1. `idempotency_key={plan}/{task}/{inputs_hash[:8]}`截断到8 hex只有32bit，管道高频下碰撞不可忽略。改16位或全hash，DoD加碰撞单测。
2. `fencing_token`每次transition都+1（含READY→RUNNING正常派发），会导致正常派发也 bump fencing，心跳比对噪音大。改只有`lease相关跃迁（acquire/renew/revoke/preempt）`才+1，普通状态跃迁只+version。
3. `SKIPPED_CACHED`判定缺`last_success_inputs_hash`存储位置。补`tasks.last_success_hash`列，Phase1 DDL漏了。
4. 时区只写“内部UTC展示上海”是空话。补：DB全UTC ISO，cron按Asia/Shanghai解析，export附`timezone`字段，日报考勤按上海自然日切分（`events`按上海日期分区而非UTC）。
5. `state.db`备份/SLA零提及。补：每次`ensure_plan`前`VACUUM INTO backup/state-YYYYMMDD.db`，WAL损坏走`backup + events jsonl重放`，Phase4加恢复演练。
6. Schema migration零设计。补`schema_version`表 + `migrate()`（`PRAGMA user_version`递增），Phase1 DoD加`v1→v2`演练。

---

## 建议的文档修改清单（可直接执行）

- `00-overview`：原则7补“单表队列 + 可派发高优才抢占 + 幂等才可抢”三约束；风险表加“活锁/告警风暴/单写者瓶颈”三行。
- `Phase0`：删`QueueItem.status`可变语义（只追加）；加`idempotent/preempt_count/last_success_hash`；`ALLOWED`加`RUNNING→PREEMPTED`权限矩阵（仅orchestrator）；`priority`改为枚举`0..10`+校验。
- `Phase1`：DDL按M1/M6/B1重写；`transition`补`Store`类签名；时间参数表收敛；`enqueue`补硬限制；补`schema_version+migrate+backup`。
- `Phase2`：主循环顺序修正（refresh→aging→preempt→dispatch）；`maybe_preempt`按B3+M4+M5重写；执行器补“步间检查 hook 位置”（tool调用后+每5s wall-clock双条件）。
- `Phase3`：Verifier异步化；AlertPolicy去重；metrics补retention（只留30天，日表分区）。
- `Phase4`：DoD数字统一（8项混沌不是5项）；时间断言改p95/p99；加`test_preempt_requires_idempotent/test_seq_autoincrement/test_reserved_slot`；冒烟明确用隔离测试DB（`state_test.db`），禁污染生产库。
