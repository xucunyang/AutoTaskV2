# 看板子系统设计：SKU 价格管线（老看板 :8787 + 双轨 Streamlit）

> 成文：2026-10-19。依据：代码逐端点核对提取（orchestrator/tools/webapp.py、webapp_static/index.html、
> tools/manmanbuy_backfill.py、tools/sku_db.py、scripts/auto_anchor.py、scripts/prefill_jingfen.py）
> + 需求台账 `~/.ai-memory/requirements/manmanbuy.md`（决策/教训/待办全量映射，见 §11 追溯表）。
> 维护纪律：**新增功能必须先改本文档再动代码**（用户 10-19 口径：连接点要打通，文档是连接点的载体）。

## 1. 目标与非目标

目标：
- 单机自用的 SKU（显卡/CPU/主板/内存/SSD）价格看板：实体维度聚合多渠道历史价格，人只做"加锚 + 审核"两个动作。
- 加锚 → 采集 → 点亮的全程用户可见、可主动触发（§5.5 核心用户旅程，10-19 痛点）。
- 一切外部访问经 channel-gate 治理（单写者锁 + 拟人节奏 + 预算观测哨）。

非目标：
- 多用户/鉴权（127.0.0.1 本机使用）。
- 移动端适配。
- Streamlit（:8788）在本期完全对齐 :8787——双轨过渡期，:8787 为全功能基准，:8788 跟进核心流（§10）。

## 2. 演进时间线（设计决策的来龙去脉）

| 日期 | 里程碑 | 对设计的影响 |
|---|---|---|
| 10-14 | 慢买逆向突破（爱加密 dex-only、RN bundle 明文）；回填管线程序化；双锚定稿（销量+价格） | client 四接口收编；每实体双锚规则 |
| 10-15 | 首次 4030 封禁与实验；R36 URL 完整性防线；R37 周采样 | 风控模型起步：**4030=账号×PC通道标记** |
| 10-16 | 《操作手册》成文；channel-gate 独立包；schtasks 06:30 自动链；提案审核卡片化 | 治理机制化（锁+节奏+预算） |
| 10-17 | gwdang 顶班轨；实体匹配根治（键序无关+spec 细化）；锚 CRUD v1；直加锚；任务台账 | 多渠道分账；CRUD 骨架 |
| 10-18 | 实体 CRUD 补全；弹窗统一层；断点续采+降级续跑；预算 80→验证→移除；品牌/品类贯通 | 可靠性三件套；契约三道防线 |
| 10-19 | 直加锚 vendor 契约化（第二例崩链）；一键补锚双层根因修复（15 提案）；**新增 SKU 用户旅程立项** | 本文档成文；§5.5 立项 |

## 3. 模块清单与职责（11 模块 × 28 端点）

UI 面板（index.html，`.mmb-panel` 弹窗卡片体系 + header + nav/wall + cmp 对比悬浮窗）：

| # | 模块 | 端点 | CRUD | 落点 |
|---|---|---|---|---|
| 1 | 实体墙/左列导航 | `GET /api/board` `GET /api/entities` | R | DB entity+observation |
| 2 | 曲线预览弹窗（mmb-modal） | `GET /api/mmb/trend` `GET /api/gwd/trend` | R 外部 | 慢买/购物党实时 |
| 3 | 点位溯源弹窗（modal） | `GET /api/entity/{id}/points?date=` | R | DB observation 明细（店/链接/券） |
| 4 | 搜索面板（mmb-panel） | `GET /api/mmb/search` | R 外部（人工免预算） | 慢买，不落盘 |
| 5 | 直加锚（anchor-add-panel） | `POST /api/mmb/add-anchor` | C | 注册表（same_by_spbh→siteSpuid 直链） |
| 6 | 锚管理（anchors-panel） | `GET /api/anchors` `POST /api/anchors/upsert` `POST /api/anchors/retire` | R/C+U/软D | 注册表 |
| 7 | 实体管理🧬（entity-panel） | `POST /api/entity/create·update·retire` `GET /api/entity-options` `GET /api/contract/enums` | C/U/软D | DB entity + 种子同步 + 注册表换绑 |
| 8 | 提案卡（prop-panel） | `GET /api/mmb/proposals` `POST /api/mmb/review` `/review/batch` `/review/auto` `POST /api/mmb/propose` | R/审核 | data/manmanbuy_proposals.yaml → 转正入注册表 |
| 9 | 任务流水（tasklog-panel） | `GET /api/tasklog` | R | reports/taskboard.json |
| 10 | ⚡一键补锚 | `POST /api/mmb/auto-anchor` | C（子进程） | 提案 yaml（零观测实体批量配对） |
| 11 | 对比悬浮窗（cmp-panel） | `GET /api/recommend` `GET /api/snapshot` | R | DB/快照 |
| — | 静态资源 | `GET /` `GET /img/{date}/{fname}` | R | webapp_static / artifacts |

**本设计新增（§5.5 旅程落地）**：`POST /api/mmb/collect-now`（立即采集指定锚）+ backfill `run --urls` 参数。

## 4. 数据存放点与实体模型（六个事实源）

| # | 存放点 | 关键内容 | 写入者 |
|---|---|---|---|
| ① | DB `data/sku.db`（WAL） | `entity`(entity_id, domain, identity_json, display_name, **brand**, aliases, perf_anchor, active,…) / `observation`(entity_id, obs_date, low_price, shop_name, source_url, … UNIQUE(entity_id,obs_date,shop_platform,shop_name)) / `entity_lineage` | 实体 CRUD、ingest |
| ② | 种子 `config/contracts/hardware_entities.yaml` | 实体引导清单（identity/display_name/brand/aliases）；re-seed 唯一来源 | seed 脚本、实体 create/update **必须同步** |
| ③ | 注册表 `config/backfill_manmanbuy.yaml` | listings：identity/url/sp_name/vendor/channel/anchor/verified/source/retired | 锚 upsert、直加锚、提案转正、实体 update 换绑 |
| ④ | 提案 `data/manmanbuy_proposals.yaml` | 待核/已核提案（补链候选） | propose、auto_anchor、review |
| ⑤ | `reports/taskboard.json` | 任务台账（步骤 n/m/子进度） | wrapper、补链、立即采集 |
| ⑥ | `artifacts/{date}/` | backfill_manmanbuy_raw.json（**增量缓存**）、ingest_stats、trend_snapshot.json | 回填链、trend_calc |

identity = `{category, series, spec}`，键序=契约声明序（键序无关匹配已于 10-17 根治，e53a76a）。
品牌（brand/vendor）是**展示维度**，不参与 identity 匹配；词表=契约 `hardware.yaml attrs.vendor.values`。

## 5. CRUD 矩阵与校验规则（每个入口的守门人）

### 5.1 实体（SKU）
| 操作 | 入口 | 校验 | 联动（缺一即断链） |
|---|---|---|---|
| C | 🧬面板/Streamlit → `/api/entity/create` | category∈5 品类枚举；series/spec 必填；brand∈契约词表（可空）；重复 identity active→409 | INSERT DB + **追加种子** |
| U | 🧬面板 → `/api/entity/update` | 同上（category/series/spec/brand/display 全字段可改，缺省=原值）；新 identity 撞车→409 | UPDATE DB + **注册表旧 identity 换绑** + **种子同名条目改写**（历史 observation 不动，按 entity_id 归属） |
| D | 🧬面板 → `/api/entity/retire` | 无（软删） | active=0，observation 保留 |
| R | 墙/导航/下拉 | —— | `/api/entity-options` 返回 id/name/identity/brand（表单预填源） |

### 5.2 锚
| 操作 | 入口 | 校验 | 备注 |
|---|---|---|---|
| C | 直加锚 `/api/mmb/add-anchor` | skuid/entity_id 必填；same_by_spbh 转 siteSpuid 纯数字直链（转不出→502）；**vendor=_brand_from_shop(店名)**（最长匹配契约词，提不出留空）；channel=_channel_of 三线规则 | 搜索结果一键入册 |
| C+U | 锚管理 `/api/anchors/upsert` | URL 纯数字直链正则→422；重复 active URL→409；**sp_name 必填**→422；vendor/channel∈契约词表→422；实体存在→404 | original_url 非空=就地改（可改 URL/归属实体） |
| 软D | `/api/anchors/retire` | 找不到 active URL→404 | 保留审计行（retired+reason） |
| R | `/api/anchors` | —— | 全量 listing+实体名解析（**本次新增：per-URL 观测计数 has_data/points**） |

### 5.3 提案（补链候选）
生成：`/api/mmb/propose`（jingfen 链）、`/api/mmb/auto-anchor`（⚡零观测实体批量）。
审核：`review`（单条）/`review/batch`（批量通过）/`review/auto`（自动预审）→ status=通过 → `scripts/transcribe_registry.py` 誊入注册表（**extract_brand：店名/sp_name→契约品牌，提不出留空**）。
转正后进 §5.5 旅程的"锚·无数据"态。

### 5.4 契约词表三道防线（10-18/19 两例崩链的设计回答）
1. **表单层**：品牌/渠道一律下拉（`/api/contract/enums`），不收自由文本；
2. **后端层**：upsert/add-anchor 422 硬校验；直加锚店名自动映射品牌；
3. **管线层**：`load_registry` 对每行做契约校验，违例 **fail-fast 拒绝整批**（宁可明早不跑，不让脏数据入库）。
实证：`英特尔京东自营`（10-18）、`AMD京东自营旗舰店`（10-19）两例均被第 3 道拦下——第 1/2 道补齐后此类问题归零。

## 5.5 核心用户旅程：新增一个 SKU（10-19 痛点的正面回答）

### 状态机
```
搜索/表单加锚 ──→ 【锚·无数据】──⚡立即采集──→ 【采集中】──┬→ 有曲线 → 入库+快照 → ✅实体点亮（上移到「有数据」层）
      │                    ↑                              └→ 无价格点 → ⚠️警示核实链接（保留，次日链自动重试）
      └─ 不点击则次日 06:30 定时链兜底（同一状态机，只是慢）
```

### 触点 1：加锚成功卡（解决"链接加完不知道去哪了"）
直加锚/锚管理保存成功 → 弹窗卡片（layerOpen 体系）：
- 内容：商品名、归属实体、渠道、锚类型、"位置：锚管理面板（带 🆕 标）"
- 动作：**[⚡ 立即采集]**（默认主按钮）/[查看锚管理]/[完成]

### 触点 2：⚡立即采集（解决"想主动采不知道从哪触发"）
- `POST /api/mmb/collect-now {urls:[...]}` → 校验 URL 在注册表（404 兜底）→ 查重（已有"立即采集"在跑→409）→
  子进程拉起 `scripts/collect_now.py --urls url1,url2`（复用 auto-anchor 的 Popen 模式，CREATE_NO_WINDOW）
- 子进程：taskboard 登记「立即采集」→ `backfill run --date-dir 今日 --urls …`（走同一 channel-gate：锁+拟人节奏；
  增量缓存+断点续采幂等，重复点击无害）→ `trend_calc` 刷新快照 → taskboard 销号
- **前端轮询** `/api/tasklog`（5s）→ 任务完成 → toast + `location.reload()` 自动刷新看板（实体上移可见）
- 锁冲突：定时链/补锚正在跑（mmb 单写者锁被占）→ 卡片提示"XX 执行中，完成后次日链/稍后再试"，不硬抢

### 触点 3：锚管理行内状态（一眼看清哪些锚还黑着）
- 每行徽标：`有数据·N点`（绿）/`无数据`（灰）——数据来自 `/api/anchors` 的 per-URL observation 计数（source_url 联查）
- 行内按钮：**⚡采**（无数据行重点显示）；右键菜单见 §5.6

### 触点 4：右键菜单（10-19 增补需求：侧边列表快速操作）
- **左列导航实体项**（含「锚未覆盖」分组）：`⚡采集此实体`（取该实体全部无数据锚 URLs，无则置灰）、`⚓在锚管理中查看`（开面板+高亮该实体行）、`📌定位到看板卡片`（focusCard）
- **锚管理行**：`✏️修改`、`⚡采集此锚`、`🌐打开商品页`（新标签）、`⛔下线`
- 实现：全局唯一 `#ctx-menu`（fixed 定位），contextmenu 事件构建+定位；click/scroll/Escape 关闭；与 layerOpen 的 400ms 防误关窗兼容

### 异常路径（已内建，不再需要人肉排查）
| 情形 | 系统行为 | 用户看到 |
|---|---|---|
| 瞬时空响应 | 0 点退避 6~10s 重试一次（ad1745f） | 多数无感 |
| 链接真无数据 | 重试仍 0 点 → 入库跳过 + 徽标停留"无数据" | 触点 2 黄条：核实链接（浏览器打开验证） |
| 预算/锁 | gate fail-closed / 409 | 卡片明示原因与下次自动时机 |
| 中断（关机/重启） | 增量缓存+断点续采 | 已采部分零损失，重跑自动续 |

## 5.6 数据流转主线（看板为什么"每天自己会动"）

```
schtasks 06:30 → mmb_backfill_daily.py（150min/步上限）
  ① fix_url（软步，失败不断链）  ② transcribe（软步，提案转正）
  ③ backfill run（硬步）：load_registry 契约校验 → channel-gate（锁+节奏）
     → 慢买轨（gate.wait+spend）与 gwdang 轨（自有 Pacer）并行分账
     → 每锚采集即 _flush 增量缓存 → BudgetExhausted 降级（慢买顺延/gwd 照采）
     → ingest（按 identity 归实体，隔离区兜底） 
  ④ trend_calc（硬步）→ 快照 → 看板 /api/board
人工旁路：⚡立即采集（§5.5）= 同一管线带 --urls 过滤，即点即采
```

## 6. 契约与词表

- `config/contracts/hardware.yaml` attrs：vendor 23 词 / channel 8 词 / anchor(销量/价格) / source(自采/manmanmai/gwdang…)；
  identity_keys 声明 identity 键序。
- 品类枚举（显卡/CPU/主板/内存/固态硬盘）目前内置于 `_validate_category`——**演进项**：收编进契约 attrs 统一管理。
- 词表变更流程：改契约 yaml → `/api/contract/enums` 自动生效 → 表单下拉/后端校验/管线校验三层同步（无代码改动）。

## 7. 风控与预算（channel-gate 集成）

| 机制 | 现行口径（10-18/19 定稿） |
|---|---|
| 单写者锁 | 平台内（慢买）串行；跨渠道（慢买×购物党）各占各锁并行 |
| 节奏 R4 | 慢买间隔 5~20s 随机 + 连发 10 歇 5~10min；gwdang 4~12s + 8 发歇 5~15min |
| 日预算 | **无上限**（10-18 双批实证 72→92 跨 80 零信号后移除，d8c353a）；≥120 发/天飞书观测哨一次；出风控信号恢复 80 |
| 人工 searchbox | 免预算（自带节流，只占锁）——预算是防批量失控，不是防人手 |
| 预算尽降级 | BudgetExhausted 不炸链：慢买顺延明日、gwdang 照采、缓存照入库（2d69e99） |
| 4030 模型 | 账号×PC 通道标记（App 独立计数；换 IP/会话/指纹无效）→ 红线唯一权威=《操作手册》 |

## 8. 可靠性设计（10-18 三连败催生的三件套 + 一）

1. **增量落盘**：每锚采集后整体写当日 raw 缓存（_flush）——中断只丢最后一锚（10-18 两次中断 48 发全丢 → 10-18/19 两次中断零损失，实证）；
2. **断点续采**：重跑跳过当日已留档锚（done_urls ∩ 注册表）；`--urls` 过滤与之正交（重复点击幂等）；
3. **降级续跑**：预算尽/入口即尽 → 慢买轨顺延，零消费部分（gwdang/入库/快照）照做；
4. **0 点退避重试**：瞬时空响应 6~10s 重试一次再定论（270K Plus 误判教训，ad1745f）。

## 9. 前端交互规约（:8787）

- 弹窗卡片体系：`.mmb-panel`（侧滑面板）+ `.modal`（居中卡片），统一 `layerOpen`（互斥+点外关闭+canvas 400ms 防误关）；
- **条目级显示/修改一律弹窗卡片**（10-18 用户约定）；
- 右键菜单：全局唯一实例，见 §5.5 触点 4；
- 刷新策略：采集任务完成后整页刷新（数据量小，reload 成本可接受；后续可演进为局部 re-render）；
- 已知债务：图表 canvas 点击冒泡的 suppress 窗口（400ms）是经验值；`focusCard`/`anchorsLoad` 为菜单动作的复用点。

## 10. 双轨过渡（:8788 Streamlit）

- :8787 = 全功能基准（本文档主体）；:8788 已有：总览表/曲线/锚管理 data_editor/提案卡批量/搜索加锚/任务流水/新建实体。
- 跟进顺序：§5.5 旅程（立即采集+状态徽标）→ Phase 2（图表溯源点击/vendor 对比轨/周采样）→ 旧 :8787 退役判定。
- 退役条件：:8788 覆盖 :8787 全部高频操作连续两周无回退诉求。

## 11. 已知问题与演进（台账 §二 全量映射，2026-10-19 时点）

| 台账项 | 状态 | 设计承载 |
|---|---|---|
| Streamlit Phase 2 | 待办 | §10 |
| 19 无数据实体收口 | **15 提案已生成待核 + 2 真无候选**（10-19 匹配根治后实弹） | §5.3/§5.5：审核通过→转正→⚡立即采集 |
| 一键补锚匹配 | 已根治（键组=原名+去品牌变体+别名；279929d） | §5.5 触点 2 同源逻辑 |
| gwdang 搜索接口逆向 | 待办（发现入口目前仅慢买） | 演进：双渠道发现 |
| 锚粒度数据归因 | 待办（observation.source_url 存慢买页面非商品链接，锚徽标暂用实体粒度计数） | 演进：ingest 落 listing url → 徽标升级锚粒度 |
| Z890 Pacific/Vertex 撞 siteSpuid | 低优，等曲线商品名 | 待办，不动设计 |
| backfill 4030 大字报警 | 低优（整批中止够用） | §7 |
| jingfen 残余锚 id29 | 观察中，失效即直加锚换 | §5.2 直加锚即兜底路径 |
| 270K Plus 归属 A/B | 待用户拍板（改名/独立实体） | §5.1 实体 U 一步可完成 |

## 12. 决策追溯表（台账 §三 → 本文档）

| 决策（台账 §三） | 落点 |
|---|---|
| 风控红线 R1~R6 唯一权威=手册 | §7（数值不复制，只记集成点） |
| searchbox 免预算 | §7 |
| 串行=平台内，跨渠道并行分账 | §7 / §5.6 双轨采集 |
| gwdang 顶班免费 | §5.6 |
| 锚 URL 唯一正解=siteSpuid 纯数字直链 | §5.2 直加锚 502 兜底、upsert 正则 |
| 双锚定稿（销量+价格） | §5.2 anchor 字段 |
| 并发采集否决 | §7 单写者锁 |
| 双轨过渡 | §10 |
| 逆向结论当天回写 | 维护纪律（文档头） |
