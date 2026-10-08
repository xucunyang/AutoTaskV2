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
| `MINIMAX_BASE_URL` | MiniMax 端点（已含 `/v1`） | `gateway.yaml` `minimax.base_url` |
| `MINIMAX_MODEL` | MiniMax 模型 ID，**区分大小写** | `gateway.yaml` `minimax.model` |
| `MINIMAX_API_KEY` | MiniMax 密钥 | `gateway.yaml` `api_key_env` |
| `MINIMAX_API_KEY` | MiniMax web_search（M Plan 套餐自带搜索额度） | `search.yaml` `provider: minimax` 时的 `api_key_env` |
| `TAVILY_API_KEY` | Tavily 检索源（备胎） | `search.yaml` 切到 `provider: tavily` 时启用 |
| `BOCHA_API_KEY` | 博查检索（备胎） | `search.yaml` 切到 `provider: bocha` 时启用 |
| `ALERT_WEBHOOK_URL` | 告警推送（可选） | `core/notifier.py` 直读环境变量 |

> 填在线模型名时两个都要填。留空不会报错，而是把空串当模型名发出去，表现为"模型不存在"。
> `tests/test_dotenv.py` 会校验模板与 yaml 的引用对得上，改配置时留意。

### 降级链与 MiniMax

`config/gateway.yaml` 的 `chains` 段给每一档配一条"按顺序试"的组合：

```yaml
chains:
  simple_chain:  [minimax, local]
  medium_chain:  [minimax, flash]
  complex_chain: [minimax, pro]
```

语义：

- **有额度时**优先走 MiniMax（一个模型吃三档，token 套餐比按量付费划算）
- **额度耗尽时**自动回到原来的落点：simple→local、medium→flash、complex→pro
- **每一跳各有独立熔断状态**，某一跳挂掉只影响它自己
- **只有限流/配额类错误才降级**。模型名写错、上下文超长、鉴权失败**不降级**——换一家也是错的，白白把所有 provider 的熔断都打一遍

启用方式：`config/gateway.yaml` 里 `models.minimax.enabled: true`，并在 `.env` 填三项。**没配 key 时它不会被注册，三条链自动塌缩成 `[local]` / `[flash]` / `[pro]`**，行为与加它之前一致。

```bash
# .env
MINIMAX_BASE_URL=https://api.minimax.cn/v1
MINIMAX_MODEL=MiniMax-M3      # 模型 ID 区分大小写
MINIMAX_API_KEY=<你的token套餐key>
```

> `base_url` 必须是 **OpenAI 兼容**端点（`.../v1`）。另一个 `https://api.minimax.cn/anthropic` 是 Anthropic 协议端点，路径 `/anthropic/v1/messages`，与本项目的 `openai_compat` provider 不通用，填错表现为 404。
> `path` 与 `base_url` 的 `/v1` 不能叠加：`base_url` 带 `/v1` 时 `path` 写 `/chat/completions`。

**"本地不可用"有两种，不能混为一谈：**

| 情况 | 处理 | 理由 |
|---|---|---|
| 过载 / 冷却 | 整条链照走，**保留 local** | minimax 不行还能回落本地，不必付钱 |
| 窗口装不下 | 走 `{chain}__online` 变体，**摘掉 local** | 兜底那跳必然超限失败，留着是死路 |

降级会留痕：返回结果里带 `served_by`（实际服务的那一跳）和 `fell_from`（被跳过的），所以"这个产物其实是 4B 写的"查得到。

`simple` 档配了 `extra_body: { thinking: { type: disabled } }`——简单任务不需要长思考。厂商特有参数只放行白名单键，yaml 不能变成任意 JSON 注入口。



| 文件 | 管什么 |
|---|---|
| `config/schedule.yaml` | cron、并发档位、governor、租约 TTL、锁存活、老化策略 |
| `config/gateway.yaml` | 模型注册表、复杂度映射、**降级链**、隐私路由、上下文窗口档位 |
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

## 每日自动触发

七条链都由 **Windows 计划任务** 拉起，不依赖谁开着终端：

| 任务 | 触发（Asia/Shanghai） | 干什么 |
|---|---|---|
| `\AutoTaskV2\daily_report` | 每天 08:00 | `scripts/run_daily.py --job daily_report`（宏观 5 项 → `summary.json` / `report.md` / `charts.md`） |
| `\AutoTaskV2\finance_daily` | 每天 08:30 | `--job finance_daily`（行情 + 理财 → `finance_report.md` / `finance_summary.json`） |
| `\AutoTaskV2\housing_daily` | 每天 10:00 | `--job housing_daily`（**只跑徐州南京**日频 → `t2_xuzhou_nanjing.md` / `t2_xz_nj.data.json` / `charts_housing.md` / `t4_investment_watch.md`） |
| `\AutoTaskV2\hardware_daily` | 每天 14:00 | `--job hardware_daily`（显卡内存价格 + 硬件新闻 → `hardware_report.md` / `hardware_summary.json`）。t2_hw_news 走专用 `acceptance_hardware_t2.yaml`（**没有** source_quality：硬件消费品的来源就是市场定价的科技媒体，A 类天然不存在——金融街口径用错地方就会死信，见 `schemas/acceptance_hardware_t2.yaml`） |
| `\AutoTaskV2\policy_daily` | 每天 16:00 | `--job policy_daily`（新政清单 + 废止动态 + 影响分析 → `policy_report.md` / `policy_summary.json`）。**排最后是有意的**：部委/国务院的当日文件多在下午上网，早上跑只能捡前一天 |
| `\AutoTaskV2\housing_fortnight` | **每月 1 日与 15 日 06:00** | `--job housing_fortnight`（六边形城市 → `t3_hexagon_cities.md`） |
| `\AutoTaskV2\housing_monthly` | **每月 8 日 06:40** | `--job housing_monthly`（房贷贴息分线能级 + 亮眼榜 → `t1_tier.md` / `t1_spotlight.json` / `m_monthly_trend.md`） |

### 地产为什么拆成三条链（2026-10-07）

原先 `housing_daily` 一天跑 5 个任务。用户的判断是**其中两条根本不该每天跑**：

> 「关于六边形房价生活城市这个任务按一个月或者半个月执行一次即可。看每天都没有什么变化，都是统计上个月份的」
> 「还有房贷贴息的这个任务，关于总体趋势，每个月来统计的」
> 「南京徐州对比的重点关注每天的交易量和平均价，交易比较活跃的小区，按照同区级别对比」

`房价收入比 / 租售比 / 成交量环比同比 / 库存去化周期` 全是**月度甚至季度口径**的数据。放在每日链里不会报错，也不会缺产物——它只会每天抄上一期那批数交一次差，正是"每天执行完数据都一样，没有什么意义"。于是：

| | 节奏 | 任务 | 为什么 |
|---|---|---|---|
| `housing_daily` | 每天 10:00 | `t2_xz_nj` → `t5_charts` → `t4_rollup` | 日成交量与日均价**逐日会动**，只有这条值得日更 |
| `housing_fortnight` | 每月 1 日、15 日 06:00 | `t3_hexagon` | 六边形的三维评分底层数据半月一变 |
| `housing_monthly` | 每月 8 日 06:40 | `t1_tier` → `m_rollup` | 定 8 号而非 1 号：月初各城市住建局/统计局的上月数据还没出，1 号跑必然只能抄上上月的数 |

拆链带出两个必须一并解决的坑：

1. **跨链依赖**。`t5_charts` / `t4_rollup` 原来 `depends_on: [t1_tier, t2_xz_nj, t3_hexagon]`。t1/t3 挪走之后**不再与日更同日落盘**，依赖留着就变成每天等一个"今天不会出现"的产物——表现是永远 PENDING，**没有报错也没有死信**，只是当天的报告永远出不来。判据由 `tests/test_housing_cadence.py` 钉成「依赖目标必须存在于同一模板内」。
2. **触发器形状**。`DaysInterval=14` 看着像"半个月"，其实在 31 天的月份会跑成 1 号、16 号、31 号**三次**。Windows 里"每月 1 号和 15 号"只能用 `<ScheduleByMonth><DaysOfMonth><Day>1</Day><Day>15</Day>` 表达，`tests/test_scheduled_task_xml.py` 把这两条与三条日更链的触发器分开断言。

用户点名的徐州南京三件事——**成交量、均价、活跃小区、同级别行政区对照**——不是写进 objective 的叮嘱，而是新增结构化产物 `t2_xz_nj.data.json` + `schemas/housing_xznj.schema.json`（`cities[].districts` 逐区 `minItems:3` 且两城都必须在、`hot_communities` `minItems:4`）。同理 `t1_spotlight.json` 把「重点关注表现突出的城市或者小区，把亮眼的部分罗列出来」变成可判定字段。日频链的 `freshness` 也因此从 365 天收到 **30 天**——这条链吃日频数据，来源里混进半年前的券商报告就说明它在抄旧数。

七条链**共用** `artifacts/{date}/` 目录，但**产物名一个都不许撞车**——2026-10-05 实测过撞车的后果：housing 的 `t5_charts` 把 daily 的 `charts.md` 覆盖了，日报正文里贴的 13 张图全是另一条链画的。现在由 `tests/test_job_matrix.py` 把「产物路径两两不相交」「cron 与 XML 的 StartBoundary 一致」钉成门禁，谁改漏一边当场红。

政策链与硬件链是**两条相反的来源口径**，别顺手统一掉：硬件拿掉 `source_quality`（A 类官方源在硬件消费品领域天然不存在），政策**保留**它（政策原文就在 gov.cn / 各部委官网，A 类本该占大头，`acceptance_policy_t1/t2` 阈值 A≥20%）。用户点名的"三农""普通工薪阶层"不是写进 objective 的叮嘱，而是 `acceptance_policy_t3.yaml` 里三条 `j_get` 业务规则的**逐字断言**（`schema` 只管类型，管不住 `domain_coverage=["宏观","金融"]` 这种类型完全合法却答非所问的摘要）——`tests/test_policy_daily.py` 会拿真实 expr 真的求值一遍，防止表达式写坏变成"永久 FAIL 但报错像模型的错"。

股票链特意排在 **08:30**（A 股 09:30 才开盘）：`t1_stocks` 采到的必然是**各市场各自的最近行情交易日**——A 股用 9-30 节前最后，**美股必须用 10-05 美东**（北京时间 10-06 凌晨 04:00 已出齐；不能套 A 股 9-30 糊弄），港股同节假期取港股最近交易日。这是口径不是缺陷，模板 objective 里写死了，免得模型把"今天的涨跌"编出来或把全市场统一成一个日期。`finance_summary.data_period` 现在是 per-market 对象（`{A股, 港股, 美股}`），验收按 `j_get(j, ["data_period","美股"], "") >= "2026-10-05"` 卡死（见 `schemas/finance_summary.schema.json` + `acceptance_finance_t3.yaml`）。

一次 `run_daily` 串起三件事：**建 plan**（`ensure_plan`，幂等）→ **跑编排器**（VerifyPool + tick，直到全部任务进终态）→ **退出并交退出码**，让计划任务拿到干净的结果：

| 退出码 | 含义 |
|---|---|
| 0 | 全部进终态且无死信 |
| 1 | 还有任务在途（90 分钟墙钟到点，一次跑不完） |
| 2 | 有死信，按 `docs/sop/dead-letter.md` 处理 |
| 3 | 配置/存储层错误，含 `--job` 在 `schedule.yaml` 里查不到 |

**2 优先于 1**：死信和在途同时存在时报 2（`run_daily.exit_code`）。死信是系统主动放弃的那一条，报 1 会把失败压成"再等等就好"的语气，监控和通知也就看不见了。

**卡死会提前收工**（`run_daily.stuck_on_dead`）：死信会把下游依赖挡住，那些任务永远停在 `PENDING`——只认"waiting 空 or 墙钟到点"的话要干等满 90 分钟。判据是三条同时成立才算卡死：至少一个死信、剩余任务里没有一个在自己动（RUNNING/SUBMITTED/VERIFYING/RETRY）、每个剩余任务的依赖链上都能追溯到死信。命中就直接退出，把机器让给下一个 job（退出码仍按死信报 2）。

job 之间要**错开**不是随手定的：`tick()` 看的是全库 `active_plans`，同时跑会互相派发对方的任务、重复验收（派发有 `expect_version` 兜底不会串，但白干活还多出版本冲突重试）。`schedule.yaml` 里 08:00 / 08:30 / 10:00 / 14:00 的间距由 `test_job_matrix::test_jobs_are_spread_out_in_time` 保证 ≥30 分钟。

已知余量：daily 自带 `timeout-min 90`，但常态 10–12 分钟就退出（08:12 前），到 08:30 还有 18 分钟余量；万一 daily 越跑越久挤进 finance 的窗口，两条链会短暂重叠（`expect_version` 兜底不会串，只是白干活）。housing 10:00、hardware 14:00 与前后都差 ≥90 分钟。

`WakeToRun` + `StartWhenAvailable` 让机器在睡/关机时也会被叫醒补跑，**错过就补、不跳过这一期**。

### 跑完会弹通知（弹框 + 声音）

跑批在 08:00 发生时没人盯着终端——退出码、日志、结构化事件全都等于没发生。所以 `run_daily` 收尾时会发一条 Windows 桌面通知（`core/winnotify.py`）：

- 通道：WinRT Toast，失败回退 `NotifyIcon` 气泡；**不装任何模块**，全程 15s 超时保护，通知坏了绝不连累跑批
- 音效：退出码 0 用 `Notification.Default`，非 0 用 `Notification.LoopingAlarm`——**失败必须比成功更响**
- 内容：`✅ 跑完了 daily_report · 2026-10-05` / `用时 12:22.3 · 完成 4 · 死信 0 · 未终态 1`
  - **用时精确到秒**（`_fmt_elapsed`）：<1h 用 `MM:SS.s`、≥1h 用 `HH:MM:SS`。原 "X.X 分钟" 在跑批死信排查时**看不清是不是 timeout 在杀**——89:58 与 90:01 都被压成 90.0 分钟。改秒级后能直接看出"是 90min timeout 杀的"还是"verifier 重试把时间烧光的"还是"模型卡在搜索"
- **链接**：toast 上带可点按钮（`file:///` + `activationType="protocol"`）——`打开日报`、`打开投资观察`、`打开产物目录`，点一下就用系统默认程序打开（四条链的按钮文案各不相同：股票链是 `股票日报`、硬件链是 `硬件日报`、housing 的图表是 `图表`）；正文里同时印一份绝对路径，气泡回退没有按钮时还能复制。只挑**本 plan 声明的、已落盘的**正文文件，按 `LINK_PRIORITY`（各链的汇总正文 → housing 的投资观察 → 图表）取前两个：`artifacts/{date}/` 是四条链共用目录，按目录乱链会点开别人家的报告（`scripts/run_daily.py::report_links`）
- 关掉它：`run_daily --no-notify`
- 弹框标题显示 **Windows PowerShell**（借用它的 AUMID，本模块没注册自己的）；开了专注助手或锁屏会被压掉
- **桌面冗余 marker**（`scripts/run_daily.py::_write_desktop_marker`）：人在电脑前未必能看到 toast——锁屏 `duration=long` 也只 25 秒，过期就只剩通知中心里的卡片，多数人不会去翻。`~/Desktop/AutoTask_{job}_{date}.txt` 写一份 ASCII 摘要（`[OK]/[DEAD]`、每任务状态、报告路径、SOP 一句 指引），与 toast **并行**而不是替代：toast 弹过就删，文件在桌面上能看若干天。Win `~/Desktop` 找不到就降级 `~/OneDrive/Desktop`，再不行 `~/`，写失败不让 notify 链路炸

```powershell
# 看状态：state / 下次触发 / 上次结果（267011 = 还没跑过）
Get-ScheduledTask -TaskPath '\AutoTaskV2\' | Get-ScheduledTaskInfo

# 手动补跑今天（注册时 StartBoundary 在未来，注册本身不会顺带触发）
schtasks /run /tn "AutoTaskV2\daily_report"
schtasks /run /tn "AutoTaskV2\finance_daily"     # 等上一条跑完再跑
schtasks /run /tn "AutoTaskV2\housing_daily"     # 同上
schtasks /run /tn "AutoTaskV2\hardware_daily"    # 同上
schtasks /run /tn "AutoTaskV2\policy_daily"      # 同上
# 慢链的手动补跑：--date 决定 plan 的逻辑日期，改日就等于补跑那一天
schtasks /run /tn "AutoTaskV2\housing_monthly"
schtasks /run /tn "AutoTaskV2\housing_fortnight"

# 跑完看日志（计划任务的 stdout 没人收，run_daily 自己落了一份）
Get-Content "reports\run_daily_daily_report_2026-10-05.log" -Tail 30

# 换机器 / 改路径后重新注册（XML 必须是 UTF-16，UTF-8 会被拒）
schtasks /create /tn "AutoTaskV2\daily_report"   /xml scripts\daily_report_task.xml /f
schtasks /create /tn "AutoTaskV2\finance_daily"  /xml scripts\finance_daily_task.xml /f
schtasks /create /tn "AutoTaskV2\housing_daily"  /xml scripts\housing_daily_task.xml /f
schtasks /create /tn "AutoTaskV2\hardware_daily" /xml scripts\hardware_daily_task.xml /f
schtasks /create /tn "AutoTaskV2\policy_daily"   /xml scripts\policy_daily_task.xml   /f
schtasks /create /tn "AutoTaskV2\housing_monthly"  /xml scripts\housing_monthly_task.xml  /f
schtasks /create /tn "AutoTaskV2\housing_fortnight" /xml scripts\housing_fortnight_task.xml /f
```

`scripts/*.xml` 是 `Export-ScheduledTask` 从**注册成功的任务**导出来的，不是手写的——手写的版本被 `schtasks` 拒了三种错（UTF-8 编码、`WakeToRun` 放进 `<CalendarTrigger>`、`UserId` 写成机器名），教训都在 `tests/test_scheduled_task_xml.py` 里成了门禁。finance / hardware 两份是**从 daily 的 XML 克隆**的（`Temp\opencode\make_task_xml.py`）：只换任务名、起始时刻、命令行参数与描述，`WakeToRun` / `StartWhenAvailable` / `ExecutionTimeLimit` 这些节点保证逐字节一致，不靠手抄。housing_monthly / housing_fortnight 由 `orchestrator\_gen_task_xml.py` 同法生成，且换掉了触发器节点。

不想用 Windows 计划任务的话，等价的常驻写法是 `python -m core.scheduler --root .`（APScheduler 按 `schedule.yaml` 的 cron 建 plan，启动时 `--catchup` 补过去 3 天）+ `python -m core.orchestrator --root .`（派发执行）。两者都活着才算自动：**cron 只建计划，编排器只干活**。

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
- **DONE 也是有显式出边的**：`DONE → RETRY` 不允许 `transition()`，必须走 `Store.reopen_done()`/`scheduler --rerun`——验收全绿但产物口径写错了（如 2026-10-06 finance 的 `data_period=2026-09-30` 全市场统一）不能改 body、也不能改 DB，只能 `reopen_done` 重跑。门槛与 `reopen_dead_letter` 对齐：必须带 `reason` + `sop_ref`、重置 `attempts`、写审计，alert 级别 P2——**不可漏不可半夜打电话**
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

启用 MiniMax 后，三档的实际落点由 `chains` 决定（见[降级链与 MiniMax](#降级链与-minimax)），`complexity_models` 指向的是链名。

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

入队侧另有六道硬门：单任务 < 100KB、`task_id` 正则、路径必须限在 `artifacts/` 下、日期必须 `YYYY-MM-DD`、命令禁 shell 元字符且校验白名单前缀、`business_rule.expr` 走 AST 白名单（禁 `eval`/`Attribute`——但放 `Call` 是为 `j_get(j, [...], default)` 这种深路径取值 helper，能取嵌套 key 而不被用来爬 `__class__`/`__bases__`，每 100 次/小时计 100 条）。

## 检索与调研

provider 三选一：`minimax`（主用，M Plan 套餐自带搜索额度）/ `tavily`（海外源，需 key）/ `bocha`（博查，需 key）。**配置里写错名字在构建时就报错**而不是运行时炸——`sb.PROVIDERS` 现在锁死三家，新增桩会破坏这条门禁（`tests/test_coverage_blindspots.py::test_removed_providers_are_truly_gone`）。

| provider | 何时用 | 改什么 |
|---|---|---|
| `minimax` | 国内 / M Plan 套餐 / 主用（默认） | `search.yaml` `provider: minimax` + `api_key_env: MINIMAX_API_KEY` |
| `tavily` | 海外检索 / 已有 Tavily key | `provider: tavily` + `api_key_env: TAVILY_API_KEY` |
| `bocha` | 国内 / 国内源 / 已有博查 key | `provider: bocha` + `api_key_env: BOCHA_API_KEY` |

**MiniMax web_search 是 Server Tool**（不是独立搜索 API）：发一个带 `tools: [{type: "web_search_20250305"}]` 的 Anthropic Messages API 请求，模型在服务端搜索，从响应的 `web_search_tool_result` 内容块里**解析出原始搜索结果**。每次搜索是一次完整 LLM 调用，比 Tavily/Bocha 贵但 M Plan 套餐自带搜索额度。代价对比：MiniMax 按 token、Tavily 按次数，对"每天 5 条链 × 30 次搜索"的小量场景 token 成本可控。代码在 `core/search/base.py::MiniMaxProvider` + `_parse_minimax_response`，测试覆盖 5 条（mock urlopen，不打真 API）。

四段式检索：先按 `published_at` 过滤 → 再判来源等级 → 装进分析包 → 产出四件套并做 claims→sources 悬空自检。

来源分级：

| 等级 | 判据 |
|---|---|
| A | 官方机构 / 学术期刊 / 权威学术会议 / 统计口径 / V≥2 溯源 |
| B | 券商研报 / 垂直行业媒体 / 知名智库 / 行业协会 / V≥1 |
| C | 论坛聚合 / 社区问答 / 视频 / 未标注或无引用 |

官方域名先验直接判 A，不调 LLM；平台域名强制逐条判。`tier_reason` 必填——解析失败落 C 而不是猜。

**A 类 20% 这条门槛对政策类有效、对硬件消费品无效**——见 `acceptance_hardware_t2.yaml` 的反面论证：硬件消费品的来源就是市场定价的科技媒体（B 级），A 类天然不存在，套"金融街官方源"会死信（2026-10-06 hardware 首跑 3 次尝试全挂在 rule_4 source_quality 上）。`test_hardware_daily_drops_source_quality_in_t2` 把这条钉死：hardware_t2 **没有** source_quality。

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
| 测试 | `899 passed` |
| 覆盖率 | 25 模块平均 91.4%，门槛 80%，单模块也须过线 |
| 真实端到端 | `3/3 SUBMITTED`（simple 7.4s / medium 75.4s / complex 41.2s，均由 MiniMax-M3 执行） |
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

### DONE 重开（产物口径写错时）

`--rerun` 对 `DONE` 也生效——`Store.reopen_done()` 路径（`ac30820` 加的）。验收全绿但产物实际口径错的（如 finance 的 `data_period` 全市场统一、housing 用了错误的报告口径），覆盖在 2026-10-06 finance 重跑中实测可用。门槛与 `reopen_dead_letter` 对齐：`reason` + `sop_ref` + `attempts=0` + alert P2（**不可漏、但不会半夜响**——见 `alerts.notifier.level_for`）。

```bash
python -m core.scheduler --root . --date 2026-10-06 --rerun t1_stocks \
    --reason "美股改 10-05 数据口径"
python -m core.scheduler --root . --date 2026-10-06 --rerun t3_finance_report \
    --reason "data_period 改 per-market 对象，需重写 summary"
```

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
      search/      tavily / bocha / minimax、来源分级、四段式 pipeline
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
- **检索当前走 MiniMax**（`dcd83c2`）：Tavily 月度 1000 额度 10-07 烧光后切到 MiniMax M Plan，套餐自带搜索额度不烧钱。Bocha / Tavily 是备胎——`search.yaml` 改 `provider:` 即可切换。
- **MiniMax web_search 是 M Plan 独占**：其他厂商的按量计费 API Key 不能走这条路径。生产环境若不在 M Plan 内，要么申请 M Plan、要么切回 Tavily。
- **`ONLINE_*` 必填**：不填在线模型名会得到"模型不存在"这类难排查的报错。
- Ollama 的 `/v1/chat/completions` 会忽略 `options.num_ctx`。代码层已绕过（探测与调用一律走原生端点），但换部署环境时仍要按 `docs/deploy-prereq.md` 做三方核对。
- **桌面 marker 文件名长度**：`AutoTask_{job}_{date}.txt` 在 `job_id` 异常长时会超 Win 文件名 255 字符限制。当前 job_id 都 ≤18，留余量充足，但**新增 job 时留意**——参见 `tests/test_run_daily.py` 里"ASCII 安全"那条断言。
