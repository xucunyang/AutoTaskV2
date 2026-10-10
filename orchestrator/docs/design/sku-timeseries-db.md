# SKU 时序观测库设计 · 版本索引

> 本文件只做目录与版本史，**不再承载正文**。每次修订新增一份
> `-vX.Y.md` 存档，永不覆盖——迭代过程中的设计推演本身是资产。

## 当前评审稿

**[sku-timeseries-db-v3.3.md](sku-timeseries-db-v3.3.md)** ——
**实施启动稿**：Q1-Q15 全部拍板 + 实施前质检修正完毕
（4 矛盾：二手污染 min 聚合/回填混轨/模型闭环 vs 不碰 DB/mermaid
验收 vs 增量链接；5 边界：升格外键/快照时机/手机入口/区划变更/
占位预估；4 细化：契约 schema/指标适用矩阵/收编提案格式/变化阈值）。
P0 可开工。

## 版本史与各版亮点

| 版本 | 文件 | 当轮评审意见 | 该版的关键增量 |
|---|---|---|---|
| v1 | [sku-timeseries-db-v1.md](sku-timeseries-db-v1.md) | 初稿（用户提出 SKU 级逐日记录/趋势/推荐/跨域范式） | 三天实测数据诊断（SKU 命名漂移/口径混杂/模型口算环比）；模型-确定性代码分工铁律；隔离区与宁缺毋滥；t1b 入库三案对比；回填方案 |
| v2 | [sku-timeseries-db-v2.md](sku-timeseries-db-v2.md) | 模块化/抽象化，不许为单一域定制（显卡品牌型号 vs 房产小区楼层同机制） | 维度模型三层（identity/attrs/聚合粒度）；dimension 字典外置域知识；contrast 控制变量对比；修饰维度可升格、数据永不搬迁；「楼市接入零核心代码」验收标准 |
| v3 | [sku-timeseries-db-v3.md](sku-timeseries-db-v3.md) | 发掘新价值（人性弱点视角）+ 背刺指数方向用反的修正 | 指标层 L0/L1/L2（原子→派生→复合）；行为指标全部事前化（stab_risk/offer_percentile 等 7 项）；推荐 action 化；sufficiency 分级；fomo/arb/stabilization 跨域信号 |
| v3.1 | [sku-timeseries-db-v3.1.md](sku-timeseries-db-v3.1.md) | ①流量维度问题（对冲非唯一立场）②防臃肿：interface/implementation ③维度宁多勿少 ④术语不清导致评审困难 | 双层视图（decision/narrative，情绪准入降为观察项 O1）；域契约文件（yaml 为源=interface，EAV/Schema Registry 对应）；attrs 宽进/identity 严出；geo 行政区划组+全新二手体系+condition/grade 跨域通用维度；新增贬值速度/国补前后价/供需温度指标；Q13 爬取回填独立成题 |
| v3.2 | [sku-timeseries-db-v3.2.md](sku-timeseries-db-v3.2.md) | Q1-Q15 逐题拍板（yaml为源/模型自动闭环收编/楼层分段/**相对锚点体系（同代销量最大=1.0，房产主流价格区间为锚）**/每实体一图/价格日为轴/**无变化链接前日报告**/先全史中位数后特征匹配/等权/显示占位/**采集渠道矩阵+荣耀采集机手机兜底**/**锚点=手机看京东天猫旗舰店销量定基底**/**Q15 关闭不预设SOP**） | §14 改为决策记录；开放问题清零；§15 实施解锁——P0 可启动 |
| v3.3 | [sku-timeseries-db-v3.3.md](sku-timeseries-db-v3.3.md) | 实施前质检：自相矛盾/边界/待细化三项审查 | **aggregate_scope 切轨声明**（修二手污染 min 与回填混轨）；收编=提案JSON+脚本执行（判断执行分离，保住不碰DB铁律）；entity_lineage 血缘表（升格不断外键）；t1b 双脚本（ingest+calc）+快照时间戳验收；mermaid 验收与增量链接的兼容规则+显著变化阈值入契约；**指标×metric×source 适用矩阵**（含 basis 标注）；契约 yaml 完整 schema 蓝本；observations.json 单一入口铁律——P0 启动稿 |

## 版本约定

- 修订 = 新增 `-vX.Y.md`，并在本表追加一行；发现上一版有错的修正
  也走新版本，错误版本保留（历史推演有价值）。
- 「当前评审稿」指针只在本文件里更新。
