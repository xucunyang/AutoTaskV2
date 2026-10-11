# Bug fix 清单（可追溯）

> 原则（用户定调）：「整个项目可追溯、可扩展，知道每一行是为什么。」
> 每个 bug 记录：症状 → 根因 → 修复 → 提交 → 预防。新 bug 从表尾追加，
> 永不删行（注销/回滚在状态列标注）。

| 编号 | 日期 | 症状 | 根因 | 修复 | 提交 | 预防 | 状态 |
|---|---|---|---|---|---|---|---|
| BUG-01 | 10-04 | acceptance_t2 三连死信：验收规则引用不存在的 module/test_func 形态 | 验收规则写了沙箱不支持的形态——不可满足的规则恒定推进死信 | 改 cmd 子进程形态 | （10-04 提交链） | 坑#4：不可满足的规则恒红；test_acceptance_exprs 系统门禁 | ✅ |
| BUG-02 | 10-11 | t1 三轮死信：observations.json 形状全错（observed_at 代替 price_date、shop 写字符串） | `_schema_hint` 只认顶层对象 schema，sku_observations.schema.json 是顶层数组 → 卡上零形状 → 模型盲写 | 数组根取 items 做形状根 | `e9cdeb9` | test_task_card 数组根用例；坑#3 | ✅ |
| BUG-03 | 10-11 | t1 连续发明 channel 变体（电商平台自营→京东自营→渠道参考价）每轮被拒 | 枚举语义模糊 + 校验只有硬匹配无归一 | 契约 attr_value_maps 别名映射，入库器枚举校验前应用 | `ad108b1` | test_validate_maps_channel_alias | ✅ |
| BUG-04 | 10-11 | t1b 入库后无当日快照，t4 会读旧快照 | t1b objective 漏写 trend_calc（单脚本卡） | 模板改双脚本（ingest → calc 顺序不可颠倒） | `ad108b1` | TODO-0 全链验证 | ✅ |
| BUG-05 | 10-11 | **双层枚举死锁**：模型写「京东自营」被 Schema 硬枚举拒，别名映射轮不到生效——五轮死信总根因 | 同一语义枚举存在于 Schema（硬匹配）与契约（别名归一）两处，违反单一事实源 | Schema 去 condition/channel 枚举（形状归 Schema，语义归契约）；threshold 允许 null | `9ee4043` | 坑#17；Schema description 写明分权 | ✅ |
| BUG-06 | 10-11 | A 类占比门槛 0.2，模型五轮实测 0.17-0.18 永远差一点 | 阈值拍脑袋，未随检索环境（比价站主导+京东验证墙）校准 | 降 0.15，objective 同步 | `9ee4043` | 坑#18：连续卡线的阈值要么降要么改实现，不赌下一轮 | ✅ |
| BUG-07 | 10-11 | t1b 首跑验收 `forbidden_node:BinOp`——规则出生即恒红 | 验收表达式写了算术（`ok+updated`、`2*(...)`)，AST 沙箱禁止 BinOp | 入库器 stats 预计算 ingested/isolated，表达式只比较 | `7b5d083` | test_acceptance_exprs 扫全部验收 expr | ✅ |
| BUG-08 | 10-11 | t4 验收 `len(...)` 恒红（且语义上 `>=0` 本就恒真，笔误级） | len 不在沙箱 BUILTIN_FNS（只有 j_get） | 改 `"decisions" in j`（In 节点在白名单） | `7b5d083` | 同 BUG-07 系统门禁 | ✅ |
| BUG-09 | 10-11 | t4 verify_snap cmd `security:cmd_not_whitelisted` | python_test 白名单只放行 tests/ 前缀，tools/ 第一方校验脚本永不可执行 | CMD_WHITELIST 增 `python -X utf8 orchestrator/tools/` 精确前缀 | `9419ad6` | whitelist 正反测试；坑：改进程内常量需重启驱动进程 | ✅ |
| BUG-10 | 10-11 | 14:00 cron 空转：t1 还是 DEAD 时 cron 触发 run_daily 秒退 | 前一会话的 reopen 发生在 cron 之后（时序错位，非代码 bug） | 手动触发补跑；次日 cron 恢复自动 | （无代码变更） | 运维：reopen 后确认下一次 cron 时序 | ✅ |
| BUG-11 | 10-11 | **旧日报无决策价值**（用户裁决）：hardware_report.md 是均价表+新闻摘要，看不出昨天价格、看不出趋势；且均价/周环比把不同渠道成色混算（5090 报 46000、周环比+69% 即混轨产物） | t3 是 sku.db 诞生前的旧任务，与「每品一图、曲线回答涨跌」的目标设计冲突；口径混轨违反 aggregate_scope 设计 | **t3 注销**（模板移除，acceptance 文件保留备查）；职能由 t4（趋势+推荐）+ Web 看板承接 | 本次提交 | 需求列表 R10；价值闸：每个产出必须挂决策动作（value-lens） | ✅ |
| BUG-12 | 10-11 | verify_snap 空快照 0 检查假 PASS | 冷启动日 calc 产出无可检实体的空快照，一致性校验空转 | latest_snapshot 跳过无可检内容的快照 | `2ea5202` | 测试覆盖 | ✅ |

## 附：遗留观察（不是 bug，记录在案）

- t1 的 CSV（t1_gpu_ram.csv）与观测副产物（observations.json）并存：
  CSV 服务旧口径（涨跌幅列），观测是时序库进料。t3 注销后 CSV 的
  唯一消费者消失——**待裁决**：是否让 t1 只产 observations.json
  （见需求列表 R15）。
- 隔离区 33 行待收编（需求 R9）。
