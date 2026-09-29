# 落地实施方案（已落盘，可执行）

> 主题：文件/DB为唯一事实源，调度器独立于Agent，主Agent只编排，子Agent无状态执行，所有状态可恢复、可幂等、可校验。
> 技术栈锁定：Python + SQLite(WAL) + APScheduler，生产级直达
> 优先级：P1 上下文/成本 + P2 校验/可观测优先

## 状态防忘机制（本文件即执行入口）

- `PROGRESS.json`：唯一进度事实源，每完成一个Phase必须更新`status=done + updated_at + evidence`
- `orchestrator/state.db`：运行时事实源，JSON导出在`state/export/`
- `orchestrator/events/YYYY-MM-DD.jsonl` + `logs/structured.jsonl`：全量追溯
- 任何Agent重启：先读`PROGRESS.json` + `state.db`重建上下文，不依赖聊天记忆

## Phase划分

- [ ] Phase0 契约：transitions + pydantic models + acceptance schema
- [ ] Phase1 存储调度并发：store.py(SQLite事务+导出) + lease.py(租约+围栏) + scheduler.py(持久化+补跑)
- [ ] Phase2 执行：orchestrator.py(主循环+DAG校验) + executor.py(任务卡+预算+缓存+检查点)
- [ ] Phase3 校验观测：verifier.py(沙箱验收) + notifier.py(告警) + daily_report(日报)
- [ ] Phase4 验证：单元+混沌+端到端冒烟

## 目录终态

```
orchestrator/
  config/schedule.yaml
  templates/daily_plan.yaml, task_card.j2, daily_report.md.j2
  schemas/models.py, transitions.py, acceptance_*.yaml
  core/store.py, lease.py, scheduler.py, orchestrator.py, executor.py, verifier.py, notifier.py, utils.py
  state.db  # WAL唯一写源
  state/export/master.json, tasks/*.json  # 只读导出
  state/checkpoints/{task_id}.json
  artifacts/{date}/{task_id}.* + manifest.json
  events/YYYY-MM-DD.jsonl
  logs/structured.jsonl
  reports/YYYY-MM-DD.md
```

## 关键设计（已定稿，直接按此实现）

1. 写路径：`BEGIN IMMEDIATE; 校验ALLOWED跃迁+version+fencing; UPDATE tasks version+1 fencing+1; INSERT events; COMMIT; 原子导出JSON(tmp+fsync+rename)`
2. 租约：`owner + lease_until(UTC) + fencing_token递增`，心跳30s续60s，过期回收为RETRY/DEAD_LETTER，写回比对fencing防脑裂
3. 调度：APScheduler SQLAlchemyJobStore + coalesce=true + max_instances=1 + misfire_grace_time=3600，plan_id幂等，支持catchup/dry-run/rerun
4. 上下文：任务卡只给objective+manifest(路径+sha256+preview5行)+acceptance+预算，>200KB只给preview，需全量用read_range
5. 验收：白名单shell=False+timeout+业务规则安全表达式，区分test_flaky_retry vs task_retry(指数退避)
6. 可观测：run_id全链路，metrics进DB+Prometheus文本，告警P0/P1/P2分级，日报自动生成
