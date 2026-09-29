# 部署前置条件（deploy-prereq）

> 本文件是"机器准备好"的检查清单，与 `design/`（系统设计）平级、职责分离：
> `design/` 回答"系统长什么样"，本文件回答"机器要满足什么才能跑"。
> 部署目标：32G Mac（Apple Silicon 统一内存）；开发机：Windows。

## 1. Ollama 上下文必须 ≥ 64K（硬性）

- 原因：Ollama 官方明确要求 web search / agent / coding 类任务至少 64K；默认只有 4096，做调研直接不可用。
- Mac（launchctl 方式，Ollama.app 不读 shell 环境变量）：
  ```bash
  launchctl setenv OLLAMA_CONTEXT_LENGTH 65536
  # 重启 Ollama.app 后生效，用 ollama ps 确认 CONTEXT 列
  ```
- Windows（开发机）：
  ```powershell
  $env:OLLAMA_CONTEXT_LENGTH = "65536"; ollama serve
  ```
- 验证：`ollama ps` 看 CONTEXT 列；再调一次 `/api/show` 核对 `model_info.*.context_length`。

## 2. 已知坑：OpenAI 兼容端点不支持设 num_ctx

- 社区反复报告（ollama issue #10974）：`/v1/chat/completions` 忽略 `options.num_ctx`，
  服务端可能静默按 4096 跑，而客户端完全感知不到。
- 对策（代码层已落实，见 `core/context.py`）：
  1. 探测与调用一律走原生端点（`/api/show` 探测、`/api/chat` 调用并显式传 `options.num_ctx`）；
  2. 每次装载后对比 `resolved`（我们算的） vs `effective`（服务端实际采纳），
     不一致 → P1 告警 + 自动下调水位线。

## 3. 启动自检三方核对（必做）

| 来源 | 怎么拿 | 不一致时 |
|---|---|---|
| 探测值 | `/api/show` → `model_info.*.context_length`（架构上限）| 以最小值为准 |
| 配置值 | `gateway.yaml` 的 `context_window` / `context_profiles` | 同上 |
| 实测值 | 发一次探针请求，看 `prompt_eval_count` 与报错边界 | 同上 |

三方取 `min()`，结果落 `state/model_profiles.json` + `metrics(context_window_detected)`。
`context_length_exceeded` 一旦出现 → 强制让出（`reason=context_overflow`）+ 水位线自适应下调。

## 4. 14B@64K 内存账（32G Mac，单并发前提）

```
权重 Q4_K_M      8.4 GB
KV cache 64K    12.0 GB
─────────────────────────
合计            20.4 GB / 32 GB，余 ~10G（系统5G + 框架1G + 余量4G）✓
```

- 前提：`local_concurrency=1`（IDLE 档现状）。**改并发前必须重算 `resolve_window()`**，
  开 2 并发 KV 翻倍到 24G，直接爆。
- 7B@64K 合计约 9G，非常宽裕；32B 走在线（门禁自动判）。

## 5. governor 空闲检测是双平台实现

| 平台 | 实现 | 说明 |
|---|---|---|
| Windows | `GetLastInputInfo`（现有） | 精确 |
| Mac | `ioreg -c IOHIDSystem` 读 `HIDIdleTime`（纳秒） | 无额外依赖，精度略逊；失败/超时2s → 返回 0.0（保安全判 ACTIVE），并提示用 `--mode idle` 手动指定 |

- 背景：`ctypes.windll` 只存在于 Windows，Mac 上原代码抛异常被吞掉后永远返回 0.0，
  会导致永远判 ACTIVE → `local:0` → 公开任务全走在线烧钱。部署 Mac 必修。
- 守住"零额外依赖"原则（不用 pyobjc），代价是精度略低 + 手动 override 兜底。

## 6. 双平台差异对照

| 项 | Windows（开发） | Mac（部署） |
|---|---|---|
| Ollama 上下文配置 | `$env:OLLAMA_CONTEXT_LENGTH` | `launchctl setenv`（App 不读 shell 配置）|
| 空闲检测 | Win32 API | `ioreg HIDIdleTime` |
| 路径 | `pathlib` 已跨平台，无需处理 | 同左 |
| 原子写 | `os.replace` 跨平台 OK | 同左 |
| 并发控制 | SQLite `BEGIN IMMEDIATE`，无需文件锁 | 同左 |
| 时区 | `zoneinfo` 标准库 | 同左 |

## 7. 部署检查清单（上线前逐项打勾）

- [ ] `OLLAMA_CONTEXT_LENGTH=65536` 已设且 `ollama ps` 确认
- [ ] `/api/show` 探测值 = 预期架构上限
- [ ] 启动自检三方核对一致，无 P1 告警
- [ ] governor 在 Mac 上能切到 IDLE（`idle` 5min 后看档位）
- [ ] `context_window_effective` 复查通过（启动自检 + 每次切档）
- [ ] 在线 API Key 已配（`ONLINE_API_KEY` 环境变量），`gateway.yaml` 无明文 key
