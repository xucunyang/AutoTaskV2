---
name: notify-user
description: OpenCode 全局的用户通知机制（飞书推送）：判断用户在不在电脑前、何时该主动推送、cc-push 用法、phone-notify 自动推送说明。当会话需要通知用户（任务完成/设备掉线/需要用户介入）、用户说"推送/飞书通知/联系我"时使用。
---

# 用户通知（飞书推送 / CC connect）

## 定位

这是 **OpenCode 层面**的通用通知能力，适用于任何场景（手机操作/代码任务/采集监控...），与具体领域 skill 无关。核心问题：**用户不在电脑前时，如何让他第一时间知道有事发生**。

## 链路

```
OpenCode session (agent) ─→ cc-push.ps1 ─→ cc-connect send ─→ 飞书 bot chat ─→ 用户手机飞书
```

- `cc-push.ps1`：`~/.local/bin/cc-push.ps1`（内含 sessionKey = 飞书 chat + user）
- `cc-connect`：`~/.local/bin/cc-connect.exe` v1.5.0，桥接消息平台与 AI 客户端
- `cc-connect daemon`：schtasks 后台服务，开机自启；**Status 不是 Running 就推不出去**
- 配置：`~/.cc-connect/config.toml`（飞书 app_id/app_secret、项目、agent 类型）

## 何时推 / 何时不推

**先查用户在不在电脑前**（用户坐在电脑前时推飞书 = 多余通知）：

```powershell
$idle = powershell -NoProfile -File "$env:USERPROFILE\.local\bin\user-idle.ps1"
```

`user-idle.ps1` 用 Win32 `GetLastInputInfo()` 返回系统键鼠空闲秒数——最准的"用户在场"信号（任何程序的键鼠输入都算，操作系统记录，不是猜的）。

| 空闲时间 | 判断 | 动作 |
|---|---|---|
| `< 180` 秒（3 分钟） | 用户在电脑前活跃 | ❌ 不推（他直接看 OpenCode） |
| `>= 180` 秒 | 用户离开电脑 | ✅ 推 |

**例外——需要用户「做物理动作」或「远程决策」的场景，无论键鼠状态都推**（飞书在手机上，弹通知能触达离开电脑的用户）：
- 权限待批准 / 需要远程决策（换设备/改参数/授权）
- 设备掉线、需要插线/重启/到设备面前解锁
- 长任务（>5 分钟）阶段性进度：低频，一个任务最多 1~2 条

**推送内容格式**：`📱 <简短标题> · <一句话说清楚发生了什么 + 我准备做什么> · <要不要你介入>`

```powershell
cc-push "📱 oneplus 平板掉线 · 无线 ADB 重连失败 · 需要你插一下 USB 线"
```

**不要滥用**：常规进度回报不推，每条都推会变噪音；只在「需要用户知道」时推。

## 运维速查

```powershell
# 推送
cc-push "消息内容"

# 查用户键鼠空闲（推不推的判断依据）
powershell -NoProfile -File "$env:USERPROFILE\.local\bin\user-idle.ps1"

# daemon 状态（Running 才能推）
cc-connect daemon status

# daemon 没装时安装
cc-connect daemon install --config "C:\Users\22651\.cc-connect\config.toml"

# 看推送日志
cc-connect daemon logs -n 20
```

## phone-notify plugin 的自动推送（事件驱动，已内置在场判断）

`phone-notify` plugin（`~/.config/opencode/plugins/phone-notify/`）在以下事件**自动**推送：
- 任务完成 / 失败（session idle 时触发）
- 权限请求待批准（可远程批准）

凭证：`%TEMP%\opencode-phone-notify-locks\feishu-secrets.json`（与 cc-connect 同一个飞书 app + 同一个 chat；`loadFeishuSecrets()` 每次推送前重读，改文件即生效不用重启）。

**已内置在场判断**（与 cc-alert 插件同一套约定）：
- 任务完成 / 失败、会话出错 → 键鼠 idle < 180s 跳过推送（用户在电脑前直接看 TUI）
- 权限请求待批准 → **始终推送**（远程审批例外，见上面的例外清单）
- 阈值可用 plugin options `idleThresholdSec` 覆盖（默认 180）；探测失败 fail-open 照常推

## ⚠️ 单向推送，不是双向对话

- 推到飞书后用户看到一条独立消息，**不会**回到任何 OpenCode session
- 用户在飞书点表情/回复 → 进的是飞书 bot 私聊（**不进入**当前 OpenCode session）
- 用户想真正对话：回到 OpenCode session 发消息（PC 端），或用 cc-connect 绑定的"双向通信 session"
- 定位是**"让用户第一时间看到有事发生"**，不是实时聊天
- 离线接收：飞书在线立刻收到；离线时消息保留，上线后可见

## 相关 plugin

| plugin | 用途 | 通道 |
|---|---|---|
| `phone-notify` | 任务完成/失败/权限待批准自动推送 + 双向遥控 | ntfy / 飞书（auto：有飞书凭证优先飞书） |
| `opencode-cc-alert` | Provider 错误推送（401/403/429/451/quota/model not found），内置同款在场判断 | 飞书（走 cc-push.ps1） |
