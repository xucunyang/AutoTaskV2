# 落地实施方案总纲领 第4版（IMPLEMENTATION PLAN V4）

> 延续 V3，仅§7算力分配重写 + §10增补 governor 行 + §12更新。其余§1-6、§8-9、§11与 V3 一致（V3仍有效，冲突以本版为准）。
> 确认状态：V3待确认第2项（汇总链入输出报告）✅通过，第3项（阈值）✅通过，第1项算力分配按本版动态方案重议。

## 7. 并发与动态算力（替代V3 §7后半，AnyIO选型不变）

**结构（已定，不再议）**：主Agent全局唯一单实例（防双主脑裂，即两个调度者同时写坏数据），并发在子Agent层，用AnyIO协程（轻量并发，单线程轮流跑任务的机制，跑在asyncio后端上）。阻塞调用包`to_thread（扔线程池跑阻塞活，避免卡住事件循环）`。

**动态配额（本版核心）**：固定`2/1/1`改为按人在否两档，CPU与内存同比例：

| 档位 | 判定 | CPU预算 | 内存预算（16GB本机） | executor（子执行） | verifier（验证） | 本地模型 | 说明 |
|---|---|---|---|---|---|---|---|
| ACTIVE 有人在用 | 有键盘鼠标输入，或 idle（无输入时长）<5min | ≤25%（≈2逻辑核） | 框架≤4GB | 1 | 1（有验收才起，否则让位） | 0（暂停本地，公开任务走在线；隐私排队等空闲） | 占用约1/4 |
| IDLE 空闲 | idle≥5min 且连续2次采样确认 | ≤85%（≈7逻辑核，留1核给系统） | min(12GB, 可用-2GB) | 4 | 2 | 1 | 占到8~9成 |

- 判定编程：Windows用`GetLastInputInfo（系统最后输入时间API）`算idle秒 + `psutil（跨平台系统信息库）`看整机CPU/内存；切IDLE需连续2次采样（防抖），切回ACTIVE立即（一次输入即降档）；每次切换记`GOVERNOR_SWITCH`事件。
- 内存同比例：两档都设`headroom（预留余量）2GB`，派发前查可用内存，不足即停派新任务（跑中的继续），持续不足P1告警；产物坚持流式+preview，不全量进内存。
- 高优破格：ACTIVE下管道高优可临时+1槽（1→2）保SLA 60s，但内存红线不可破；破格记`burst`指标，日报可见。
- 配置（`config/schedule.yaml`）：
```yaml
governor: {idle_after_s: 300, confirm_samples: 2, active_cpu: 0.25, idle_cpu: 0.85, mem_headroom_gb: 2, high_burst: 1}
workers: {executor_active: 1, verifier_active: 1, local_active: 0, executor_idle: 4, verifier_idle: 2, local_idle: 1}
```
`workers.resolve(profile)`按档查表，显式值覆盖；`psutil`进`requirements.txt`，取不到输入API时默认ACTIVE保安全。

## 10. 阈值（V3表增补一行，其余不变）

| governor轮询/防抖 | — | 每15s采样，IDLE需2次确认，切回立即 | 防频繁跳档 |
| ACTIVE/IDLE配额 | 半算力2/1/1 | ACTIVE 1/1/0，IDLE 4/2/1，内存4GB/12GB-2GB余量 | 人在1/4、空闲8~9成 |

## 12. 待确认（V4 review请回）

1. 动态两档（ACTIVE 1/1/0 + IDLE 4/2/1，内存4GB/12GB留2GB，高优可+1破格，idle5min两确认）是否开工？
2. ✅ 汇总链入输出报告（V3§8）通过。
3. ✅ 阈值（V3§10）通过。
