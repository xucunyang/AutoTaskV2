# AutoTaskV2

> 文件/DB 是唯一事实源。定时器只负责"准点敲一下"，Agent 只负责"读自己的任务卡 → 干 → 落盘 → 交接"。
> Agent 不做计划拆分、不重试、不判终态、不吞异常——这些全部由框架负责。

一套面向长任务的调度编排框架：定时器展开计划 → 编排器派发任务卡 → 执行器在受控工具集里跑模型 → 验收器独立复核 → 通过才进终态。
每个任务只写自己那一小块状态，可断点续跑、可让出、可并发、可回放。

## 目录

- [快速开始](#快速开始)
- [配置](#配置)
- [常用命令](#常用命令)
- [架构](#架构)
- [状态机](#状态机)
- [模型路由](#模型路由)
- [受控工具集](#受控工具集)
- [检索与调研](#检索与调研)
- [测试与覆盖率](#测试与覆盖率)
- [运维](#运维)
- [目录结构](#目录结构)
- [设计文档](#设计文档)
- [已知限制](#已知限制)

## 快速开始

前置：Python 3.13、Git。本地模型走 Ollama，在线模型与检索需要 API Key。

```bash
git clone https://github.com/xucunyang/AutoTaskV2.git
cd AutoTaskV2/orchestrator

python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env          # 然后填入真实 key；macOS/Linux 用 cp
```

起 Ollama 并确认上下文窗口（**硬性要求**，默认 4096 做调研会直接不可用）：

```powershell
$env:OLLAMA_CONTEXT_LENGTH = "65536"; ollama serve
```

```bash
# 另开一个终端确认 CONTEXT 列不是 4096
ollama ps
```

跑一次冒烟（会真的调模型，需要 key 与 Ollama 都在）：

```bash
python scripts\e2e_real.py
```

常驻运行编排器：

```bash
python -m core.orchestrator --root .
```

## 配置

配置分两层：`config/*.yaml` 是结构与策略（进 git），`.env` 只放密钥和会变的地址（不进 git）。

优先级：**真实环境变量 > `.env`**。所以 CI 或临时调试可以用环境变量盖过文件里的值。

### `.env`

`orchestrator/.env.example` 是模板。每个键都必须在 `config/` 里有对应引用，否则模型名会被展开成空字符串：

| 变量 | 用途 | 引用位置 |
|---|---|---|
| `ONLINE_BASE_URL` | 在线端点，如 `https://api.deepseek.com` | `gateway.yaml` `base_url` |
| `ONLINE_FLASH_MODEL` | medium 档模型名 | `gateway.yaml` `flash.model` |
| `ONLINE_PRO_MODEL` | complex 档模型名 | `gateway.yaml` `pro.model` |
| `ONLINE_API_KEY` | 在线密钥 | `gateway.yaml` `api_key_env` |
| `TAVILY_API_KEY` | 检索源 | `search.yaml` `api_key_env` |
| `BOCHA_API_KEY` | 博查检索（可选） | 启用时改 `search.yaml` `provider: bocha` |
| `ALERT_WEBHOOK_URL` | 告警推送（可选） | `core/notifier.py` 直读环境变量 |

> 填在线模型名时两个都要填。留空不会报错，而是把空串当模型名发出去，表现为"模型不存在"。
> `tests/test_dotenv.py` 会校验模板与 yaml 的引用对得上，改配置时留意。

### 配置文件

| 文件 | 管什么 |
|---|---|
| `config/schedule.yaml` | cron、并发档位、governor、租约 TTL、锁存活、老化策略 |
| `config/gateway.yaml` | 模型注册表、复杂度映射、隐私路由、上下文窗口档位 |
| `config/search.yaml` | 检索 provider、来源分级 A/B/C 判据、每日额度 |
| `config/report.yaml` | 报告渲染阈值与 SLA |

> `schedule.yaml` 里的 `workers` / `governor` / `lease` / `lock` / `poll` 必须是**顶层键**。
> `workers` 是 flow mapping（`{...}`），挂不了子键；一旦缩进错位整个文件解析失败，
> 而加载器会吞掉异常让配置静默退回默认值——612 个测试全绿也发现不了。

## 常用命令

```bash
# 编排器主循环
python -m core.orchestrator --root .
python -m core.orchestrator --root . --once          # 只跑一个 tick，调试用
python -m core.orchestrator --root . --max-ticks 5
python -m core.orchestrator --root . --no-lock       # 绕过单实例锁
python -m core.orchestrator --root . --no-verify-pool # 同步验收，便于看栈

# 计划展开与补跑
python -m core.scheduler --root . --date 2026-10-01
python -m core.scheduler --root . --catchup --lookback-days 3
python -m core.scheduler --root . --dry-run           # 只算不落库
python -m core.scheduler --root . --date 2026-10-01 --rerun t3_report --reason "换检索源重跑"

# 手动投单个任务
python -m core.enqueue --file inbox\task.json --priority 5 --plan-id my_plan

# 单跑一个任务（绕过派发，用于复现）
python -m core.executor --root . --plan-id my_plan --task-id t1_collect

# 动态算力档位（手动 override，测试 governor 用）
python -m core.governor --mode idle      # 满档
python -m core.governor --mode active    # 省档
python -m core.governor --mode auto      # 交回自动
```

## 架构

```text
定时器 Scheduler ──展开模板──▶ Plan + TaskShard
                                 │
                          编排器 Orchestrator
        ingest_inbox → housekeeping → validate_dag → refresh_ready
        → apply_aging → dispatch_split → promote → export
                                 │
                          执行器 Executor
        渲染任务卡 → 检索 → tool loop → atomic_write → 自检 → SUBMITTED
                                 │
                          验收 Verifier（异步池）
                    通过 → DONE ／ 不通过 → RETRY / FAILED
                                 │
                      报告 / 指标 / 告警
```

各层职责边界：

- **Scheduler**：准点展开计划模板。只做触发，不碰任务内容。
- **Orchestrator**：无状态主循环。所有状态变更走 `Store.transition()`，唯一写入口。
- **Executor**：无状态执行。只认自己那张任务卡，不重试、不判终态。
- **Verifier**：独立验收，沙箱化执行检查。不短路——一次跑完告诉你还错了几处。
- **Store**：SQLite（WAL）+ 单表 + 乐观锁 `version`。文件/DB 是唯一事实源。
- **Lease**：租约语义 + 心跳续租。kill -9 后锁会被回收，系统不会永久起不来。

关键不变量：

- 任何状态写入都经过 `transition()`，带乐观锁 + 权限矩阵 + 原因校验。
- 任务卡渲染后 **token < 2k** 是硬门禁（否则上下文窗口会被任务卡本身吃光）。
- 验收被打预算打断时任务**不会**到 `DONE`——不能宣称验完了。
- `plan_id` 取问题哈希，同题不重拆；`inputs_hash` 变了则幂等键失效，不静默复用旧结果。

## 状态机

14 态，定义在 `schemas/transitions.py`。核心几条：

```text
PENDING ──deps 满足──▶ READY ──派发──▶ RUNNING ──完成──▶ SUBMITTED
                        ▲              │                    │
                        │              ├── 让出 ──▶ YIELDING ─┘
                        │              │
RETRY ◀─────────────────┴── 验收不通过 ─┴──▶ FAILED ──▶ DEAD_LETTER
                                                             │
                                          显式 reopen ──────┘──▶ RETRY
```

几条容易被绕过的规则：

- **让出必须回带 `expect_fencing`**，这是脑裂防护的另一半。
- **`RETRY` 必带 `reason`；`DEAD_LETTER` 必带 `reason` + `last_error` + `sop_ref`。**
- **老化只许升不许降**，且一次只升一档（下限 3），用的是 `enqueued_at` 而不是 `updated_at`。
- **死信有出边**：`DEAD_LETTER → RETRY` 只允许显式运维动作，`scheduler --rerun` 走这条路并重置 `attempts`。
- 依赖未满足、并发未够、退避未到点这类**预期内的拒绝**记 `REJECTED` 但不发 P1 告警——否则告警会变成噪音。

## 模型路由

改配置即可切换，不动代码。

`config/gateway.yaml` 里 `models` 是注册表，`complexity_models` 就是路由本身（"配置即路由"）：

```yaml
models:            # 注册表
  local:  { type: ollama,        models: { simple: qwen3.5:4b }, think: false }
  flash:  { type: openai_compat, model: "${ONLINE_FLASH_MODEL}", api_key_env: ONLINE_API_KEY }
  pro:    { type: openai_compat, model: "${ONLINE_PRO_MODEL}",   api_key_env: ONLINE_API_KEY }

complexity_models: # 路由
  { simple: local, medium: flash, complex: pro }

privacy_models:    # 涉密任务强制本地
  { secret: local }
```

当前默认路由：

| 复杂度 | 注册表名 | 实际模型 |
|---|---|---|
| `simple` | `local` | `qwen3.5:4b`（Ollama，`think: false`） |
| `medium` | `flash` | `deepseek-v4-flash` |
| `complex` | `pro` | `deepseek-v4-pro` |

几个要点：

- `local` 开 `think: false`。实测同一任务卡 `think` 未设置 44.7s / `think=false` 13.2s，各 5/5 成功。
  simple 任务不需要长思考，这已配置化。
- 接新厂商/新套餐只要往 `models` 加条目，不改代码。
- 密钥与地址**不写进 yaml**，只写变量名（`api_key_env` / `${VAR}`）。配置进 git 就等于泄密。
- 熔断是真实现：单探针半开、`401` 立即熔断（key 写错不该让前三个任务白失败）、失败路径必定释放信号量。

## 受控工具集

框架自己做薄 tool loop，不拉起通用 CLI agent。理由：session 让出、checkpoint 续跑、白名单沙箱这三项通用 agent 是黑盒，做不到。

只有 4 个工具，`shell` 不进白名单：

| 工具 | 作用 |
|---|---|
| `read_range` | 按行区间读文件，**不整读**（大文件内存与体积无关的前提） |
| `search` | 走检索 provider，带来源分级 |
| `atomic_write` | `os.replace` 原子落盘 |
| `run_cmd` | 白名单前缀命令，`shell=False` |

入队侧另有六道硬门：单任务 < 100KB、`task_id` 正则、路径必须限在 `artifacts/` 下、日期必须 `YYYY-MM-DD`、命令禁 shell 元字符且校验白名单前缀、`business_rule.expr` 走 AST 白名单（禁 `eval`/`Call`/`Attribute`，每小时限 100 条）。

## 检索与调研

provider 二选一：`tavily`（主用）或 `bocha`（博查，需 key）。Exa/Bing 桩已删除，写错名字在构建时就报错而不是运行时炸。

四段式检索：先按 `published_at` 过滤 → 再判来源等级 → 装进分析包 → 产出四件套并做 claims→sources 悬空自检。

来源分级：

| 等级 | 判据 |
|---|---|
| A | 官方机构 / 学术期刊 / 权威学术会议 / 统计口径 / V≥2 溯源 |
| B | 券商研报 / 垂直行业媒体 / 知名智库 / 行业协会 / V≥1 |
| C | 论坛聚合 / 社区问答 / 视频 / 未标注或无引用 |

官方域名先验直接判 A，不调 LLM；平台域名强制逐条判。`tier_reason` 必填——解析失败落 C 而不是猜。

调研任务声明 `needs_web` 时没有检索源就**让出**，不会静默用模型参数内知识糊弄过去。

## 测试与覆盖率

```bash
python -m pytest -q                                  # 全量
python scripts\check_coverage.py                     # 覆盖率门禁（25 模块）
python scripts\check_coverage.py --min 85            # 提高门槛
```

覆盖率门禁是"平均 + 单模块都过线"，不是只看平均：只看平均会让"一个模块 0%、另一个 100%"互相掩盖，而 0% 那个恰恰最危险——那段代码从没被执行过。

当前基线：

| 项 | 值 |
|---|---|
| 测试 | `654 passed` |
| 覆盖率 | 25 模块平均 91.3%，门槛 80%，单模块也须过线 |
| 真实端到端 | `3/3 SUBMITTED`（simple→local / medium→flash / complex→pro） |
| 200MB 大文件 | 3.44M 行 / 3.80s / 峰值内存增量 0.5MB / 任务卡 702 tokens |
| 生产环境已修 bug | 18 |

补充脚本：

```bash
python scripts\e2e_real.py        # 三档复杂度真实模型端到端
python scripts\bigfile_check.py  # 200MB 流式验证（按需，会造大文件）
python scripts\bench_local_loop.py
python scripts\bench_local_model.py
```

混沌测试证据留在 `orchestrator/reports/chaos_*.log` 与 `smoke_baseline.log`（进 git，评审要看）。

## 运维

### 死信重开

见 `docs/sop/dead-letter.md`，里面的命令都是实测过的。

```bash
python -m core.scheduler --root . --date 2026-10-01 --rerun t3_report --reason "换检索源重跑"
```

死信任务走 `reopen_dead_letter()`：进 `RETRY` + `attempts` 重置为 0 + 审计事件 + P1 告警。
**不要手工改库**——会破坏 replay 一致性，而 SOP 本身禁止改库。

### 告警

级别由 `notifier.level_for(kind)` 单点决定，调用方不能自选。P0 白名单只有三项：`dead_letter`、`inconsistent`、`dispatch_sla_breach`。未知 `kind` 默认 P2（宁可进日报也不半夜打电话）。去重窗口 300s，状态落 `state/alerts_state.json` 跨进程生效。

### 备份与自检

```bash
# 迁移前自动 VACUUM 备份；也可手动
python -c "from core.store import Store; ..."
```

`Store.integrity_check()`、`Store.daily_summary()`、`Store.rebuild_export()` 分别用于一致性检查、按上海自然日聚合、以及从 DB 重建导出目录。

导出目录只有 `state/export/plans/{plan_id}.json` 与 `state/export/tasks/*.json`。`master.json` 已删除——它是 V1/V2 的遗留，跨 plan 全局视图走 DB 聚合，不在导出目录里再存一份可能不一致的事实。

### 部署前置

`docs/deploy-prereq.md` 是"机器要满足什么才能跑"的清单（目标 32G Mac / Apple Silicon，开发机 Windows）。最容易踩的一条：Ollama 上下文必须 ≥ 64K，默认 4096 直接不可用。

## 目录结构

```text
AutoTaskV2/
  orchestrator/
    config/        schedule / gateway / search / report
    core/          编排、执行、存储、租约、网关、provider、工具、验收、报告
      search/      tavily / bocha、来源分级、四段式 pipeline
    schemas/       transitions（状态机）、models、验收规则、summary schema
    templates/     计划模板、任务卡模板、报告模板
    state/         orchestrator.db、export/、alerts_state.json  （gitignore）
    artifacts/     产物，按 artifacts/{date}/ 组织      （gitignore）
    logs/ events/ inbox/ backup/                      （gitignore）
    tools/         run_cmd 落地的辅助脚本              （gitignore）
    reports/       验收报告、混沌日志、metrics.prom     （部分进 git）
    scripts/       e2e_real / bigfile_check / check_coverage / bench_*
    tests/         44 个测试文件
    .env.example   模板；.env 本身 gitignore
  design/
    plans/         V1–V7 实施计划（V7 为现行纲领）
    phases/        Phase0–4 设计稿与评审签字
  docs/
    deploy-prereq.md   部署前置条件
    sop/dead-letter.md 死信重开 SOP
  runs/            真实调研产物（不污染生产状态）
  PROGRESS.json    阶段状态、真实流水、生产修复记录
  task.md          V1 需求原稿（部分内容已被 V7 纲领取代）
```

## 设计文档

| 文档 | 内容 |
|---|---|
| `design/plans/IMPLEMENTATION_PLAN_V7.md` | 现行纲领 |
| `design/phases/Phase0-contract-design.md` | 契约、schemas、状态机 |
| `design/phases/Phase1-storage-scheduler-lease-design.md` | 存储、调度、租约 |
| `design/phases/Phase2-orchestrator-executor-design.md` | 编排与执行 |
| `design/phases/Phase3-verifier-observability-design.md` | 验收与可观测 |
| `design/phases/Phase4-verification-chaos-design.md` | 混沌测试、生产补测证据（§6.0 / §6.0.1 / §10） |
| `design/phases/REVIEW-signoff.md` | 评审签字 |
| `PROGRESS.json` | 逐阶段证据、18 个生产 bug 清单、方法论 |

## 真实调研示例

`runs/` 下是端到端真跑（真 planner 拆分、真检索、真模型、真验收）的产物，可直接读：

| 目录 | 内容 |
|---|---|
| `runs/日本房贷贴息分析/` | 日本房贷贴息后经济与房地产市场走势，3 任务全 DONE，报告 5179 字 |
| `runs/中国房贷贴息分析/` | 中国 2026-09-30 房贷贴息政策影响，3 任务全 DONE |
| `runs/中日房贷贴息对比/` | 中日对比报告 + 抖音口播文稿与剪辑建议 |

## 已知限制

- **Mac 真机验证未做**：`core/governor.py` 的 `_idle_mac()` 依赖 macOS 专有的 `ioreg`，Windows 上无法执行。已确认放一边。
- **依赖任务的产物没有自动转成下游 `inputs`**：综合任务拿不到上游完整内容/manifest，只能靠 planner 在任务卡里写清楚。计划中待修。
- **博查额度不足**：当前返回 `403 package quota`，实际调研走 Tavily。
- **`ONLINE_*` 必填**：不填在线模型名会得到"模型不存在"这类难排查的报错。
- Ollama 的 `/v1/chat/completions` 会忽略 `options.num_ctx`。代码层已绕过（探测与调用一律走原生端点），但换部署环境时仍要按 `docs/deploy-prereq.md` 做三方核对。
