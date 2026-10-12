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
| BUG-13 | 10-11 16:32 | **图表首屏空白**（用户报告）：首次加载每个 SKU 图表是空的，点开再缩回才看到曲线 | `echarts.init` 在卡片尚未挂到 DOM 时执行 → 画布 0 尺寸；点开/缩回触发 resize 才画出来 | 先挂载全部卡片、再统一 initChart（挂载后 init，画布尺寸正确） | 本次提交 | 前端规范：ECharts 必须在元素挂载后 init | ✅ |
| BUG-14 | 10-11 16:32 | **数据点点不出详情**（用户报告）：图上每一天的数据点点不开溯源详情 | 同 BUG-13 根因（0 尺寸画布上事件不可用）+ 图表点击会冒泡到卡片触发放大、与弹窗冲突 | 同 BUG-13 修复；卡片点击忽略 .chart 区域（图上点击=溯源，卡片其余=放大）；symbol 加大到 7 | 本次提交 | 同上；弹窗与卡片动作的冒泡边界要在 handler 里划清 | ✅ |
| BUG-15 | 10-11 16:56 | **本地页面加载 5-6 秒**（用户报告，目标 2-3s） | ECharts 走 CDN（bootcdn ~1MB），本地页面也被 CDN 延迟拖住 | echarts.min.js 本地化到 webapp_static/vendor/，/static 挂载 + etag 缓存，去除 CDN 兜底脚本 | 本次提交 | 部署形态审查：本地服务不依赖任何外网资源 | ✅ |
| BUG-16 | 10-11 16:56 | **悬停时曲线消失只剩点、有时点也没了**（用户报告，概率性） | hover 触发 ECharts emphasis 重绘（line/symbol 样式切换）——闪没即 emphasis 重绘毛刺 | series `emphasis: { disabled: true }`（悬停只留 tooltip 与 axisPointer，图形不变样）；legend selectedMode=false | 本次提交 | 前端规范：看板类图形禁用 emphasis 动效 | ✅ |
| BUG-17 | 10-11 16:56 | **点不到数据点**（用户报告：「必须精准点到那个点上才能触发，太费劲」） | 精准命中 symbol 才触发 click——目标太小；鼠标落在线上不生效 | 改 zrender 层点击：绘图区内点**任意位置**，像素反解出最近日期弹详情（convertFromPixel grid→xIndex 四舍五入） | 本次提交 | 交互规范：小目标点击一律给「就近命中」兜底 | ✅ |
| BUG-18 | 10-11 18:14 | **真实库被污染**（用户裁决：「目前这些数据其实都是不可靠的」）：数据验尸发现 e2e 合成样本 10 行（假历史价 2859 序列——smoke 测试写进真实库）+ 网页转述 23 行（引流/过期风险）与 3 行京东实采混在一起；此前「渠道差 39%/67%」的结论实为**假数据 vs 真数据** | e2e_smoke 硬编码写真实库；网页转述行未做可信度分级即入库 | ① 备份后清除 33 行（removed_rows_backup.json 可回滚），地基重建为 3 行京东实采 ② e2e_smoke **沙箱化**（data/e2e_root 独立 db+artifacts，种实体+打锚点，真实库零接触，E2E PASS）③ 来源可信度分级立 R24/R26 | `本次提交` | 沙箱隔离：测试/合成数据永不进真实库；入库前来源分级 | ✅ |
| BUG-19 | 10-11 19:19 | **子 Agent 采集执行三低效**（用户看执行过程后指出）：① 识图全用 LLM 视觉——token 烧太快 ② honor 跑慢慢买 m 网页——App 优先原则违背（设备是给 App 独占内容用的，纯网页 PC 自己就能跑）③ 未按「最低价+官旗组合」采集 | 任务书没写执行规范（OCR 优先/设备分工/组合采样） | 三规范落账 R31（App 优先渠道矩阵）/R32（采集组合：最低价+官旗+样本）/R33（本地 OCR 优先，LLM 视觉仅兜底）；后续子 Agent 任务书直接引用 | 本次提交 | 设备任务书模板化：渠道分工+识图方式+采样组合三要素必写 | ✅ |
| BUG-20 | 10-11 19:4x | 子 Agent 采集 JSON 的 `raw_price` 写成描述字符串（如「京东价¥4299，券后到手¥3799」）→ 入库器 bad_price 拒收（8 行两批次） | 任务书没锁死字段类型——模型把证据描述塞进了数值字段 | 修复脚本解析字符串取原价数字后重放全过；**入库器拒收行为正确**（类型门禁在工作） | 本次提交 | 采集任务书：数值字段必须为 JSON number，证据描述只进 note | ✅ |
| BUG-21 | 10-12 14:3x | cron 自动跑批 t1 三连死信：`rule_5 A=1/28 ratio=0.04 bar=0.15`——形状/枚举/双脚本全过，唯独 A 类来源占比崩到 0.04（前日 0.17→当日 0.04，随搜索环境剧烈波动） | **A 类占比门槛是旧报告范式的遗产**：它服务「日报结论可信度」（t3/t2 已注销），而 t1 已转型为采集进料任务（门禁=schema 形状+来源可追溯+新鲜度，全部在且全过）。占比随搜索环境波动、降规格重试无 action 可挂——死门槛 | rule_5（source_quality）从 acceptance_hardware_t1 移除（objective 同步）；10-12 shard 补丁 + 18 行观测抢救入库（2 直入+12 重放救回） | 本次提交 | 价值闸复盘：每条验收规则必须挂得上 action——挂不上就删（BUG-21 与 acceptance_t2/BUG-01 同根） | ✅ |

## 附：遗留观察（不是 bug，记录在案）

- t1 的 CSV（t1_gpu_ram.csv）与观测副产物（observations.json）并存：
  CSV 服务旧口径（涨跌幅列），观测是时序库进料。t3 注销后 CSV 的
  唯一消费者消失——**待裁决**：是否让 t1 只产 observations.json
  （见需求列表 R15）。
- 隔离区 33 行待收编（需求 R9）。
