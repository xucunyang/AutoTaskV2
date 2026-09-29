# 落地实施方案总纲领 第5版（IMPLEMENTATION PLAN V5）

> 延续 V4，仅动态算力一节重写（人在用降档 + 手动满负载）。其余与 V3/V4 一致（队列/抢占/网关默认关/报告链接/阈值/验证均不变，冲突以本版为准）。

## 7. 动态算力 V5（替代V4 §7）

两档变三源：自动侦测（auto）+ 手动指定（manual），手动优先。

| 档位 | 判定 | CPU | 内存（16GB本机） | executor | verifier | 本地模型 | 说明 |
|---|---|---|---|---|---|---|---|
| ACTIVE 有人在用 | 有输入或idle<5min，或手动`active` | ≤12.5%（≈1逻辑核） | 框架≤2GB | 与验证共享1槽（执行优先，验证排队） | 同左（不同时跑） | 0（公开走在线） | 比V4再降一半，约1/8 |
| IDLE 空闲 | idle≥5min连2次确认，或手动`idle` | ≤85%（≈7核） | min(12GB，可用-2GB) | 4 | 2 | 1 | 8~9成，V4不变 |

- 手动触发（与自动侦测并列）：`python -m core.governor --mode idle|active|auto` 写 `orchestrator/state/governor.override`（内容`idle/active/auto`+时间+原因），主循环每次采样先读该文件；`idle`即“用户说不用了，全负载跑”，等同IDLE配额且不受输入影响；`active`即强制低占用；`auto`恢复侦测。切换记`GOVERNOR_SWITCH{by: auto|manual}`事件，重启后override文件仍有效（防忘）。
- 自动侦测：`GetLastInputInfo（系统最后输入API）`+`psutil（系统信息库）`，切IDLE需2次确认，切回ACTIVE立即；`headroom（预留余量）2GB`不足停派新任务；高优ACTIVE下可+1破格（内存红线不破）。
- 配置：`governor: {idle_after_s: 300, confirm_samples: 2, active_cpu: 0.125, idle_cpu: 0.85, mem_headroom_gb: 2, high_burst: 1}` + `workers: {executor_active: 1(shared), verifier_active: 0, local_active: 0, executor_idle: 4, verifier_idle: 2, local_idle: 1}`。ACTIVE下执行与验证互斥共享1槽（`total_active=1`）。

## 12. 待确认（V5）

1. ACTIVE降到1槽共享（~1/8）+ 手动`idle/active/auto`三态，确认开工？
