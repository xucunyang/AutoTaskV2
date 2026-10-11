// opencode-cc-alert: push OpenCode provider errors to Feishu via cc-push
// Loaded globally from ~/.config/opencode/plugins/opencode-cc-alert (junction
// into this repo), so it fires for every session on this machine.
//
// Patterns cover: auth/login required, 401/403/429/451, quota, geo-block,
// model unknown/deprecated, expired session.

import { Plugin } from "@opencode/plugin"
import { spawn } from "node:child_process"
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs"
import { dirname, join } from "node:path"
import { tmpdir, homedir } from "node:os"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))

const PATTERNS: ReadonlyArray<{ id: string; re: RegExp; label: string; emoji: string }> = [
  { id: "auth", re: /(?:\b401\b|unauthori[sz]ed|authentication.{0,20}(?:required|fail)|reauth|please.{0,10}sign.?in|login.{0,10}required|api[_\s-]?key.{0,10}(?:invalid|incorrect|missing)|invalid.{0,10}api[_\s-]?key)/i, label: "需要登录或 API Key 无效", emoji: "[AUTH]" },
  { id: "forbidden", re: /(?:\b403\b|forbidden|access.{0,10}denied|permission.{0,10}denied)/i, label: "权限被拒绝", emoji: "[403]" },
  { id: "ratelimit", re: /(?:\b429\b|rate[_\s-]?limit|too many requests)/i, label: "速率限制", emoji: "[429]" },
  { id: "geo", re: /(?:\b451\b|geo[_\s-]?block|not available in (?:your )?(?:country|region)|region.{0,10}restrict|unsupported.{0,10}region|country.{0,10}not supported)/i, label: "当前地区不可用", emoji: "[GEO]" },
  { id: "quota", re: /(quota|usage.{0,5}limit|insufficient.{0,10}(?:quota|balance|credit)|billing|payment.{0,5}required)/i, label: "额度/余额用完", emoji: "[QUOTA]" },
  { id: "model", re: /(model.{0,20}(?:not (?:found|exist|available))|unsupported model|unknown model|invalid model|model.{0,10}deprecated)/i, label: "模型不可用", emoji: "[MODEL]" },
  { id: "session", re: /(login.{0,10}expired|session.{0,10}expired|credentials.{0,10}(?:invalid|expired)|token.{0,10}expired)/i, label: "登录/会话过期", emoji: "[EXPIRED]" },
]

function classify(text: string) {
  if (!text) return null
  for (const p of PATTERNS) if (p.re.test(text)) return p
  return null
}

type AlertConfig = {
  enabled?: boolean
  ccPushPath?: string
  sessionKey?: string
  dedupMs?: number
}

function loadConfig(): AlertConfig {
  const path = join(HERE, "config.json")
  if (existsSync(path)) {
    try {
      return JSON.parse(readFileSync(path, "utf8"))
    } catch {}
  }
  return {}
}

function push(message: string, cfg: AlertConfig) {
  const tmp = join(tmpdir(), "opencode-cc-alert.txt")
  try {
    mkdirSync(dirname(tmp), { recursive: true })
    writeFileSync(tmp, message, "utf8")
  } catch {}
  const pushPath = cfg.ccPushPath || join(homedir(), ".local", "bin", "cc-push.ps1")
  if (!cfg.sessionKey) {
    // Without sessionKey the cc-push default points at the original test chat.
    // We still send so the user notices the plugin fired.
    console.warn("[cc-alert] no sessionKey in config.json, skipping push")
    return
  }
  try {
    const child = spawn(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", pushPath, "-s", cfg.sessionKey, tmp],
      { windowsHide: true, detached: true, stdio: "ignore" },
    )
    child.unref()
  } catch (e) {
    console.warn("[cc-alert] spawn failed:", e)
  }
}

function format(hit: ReturnType<typeof classify>, src: string, extra: Record<string, unknown>): string {
  const lines: string[] = []
  lines.push(`${hit!.emoji} OpenCode ${hit!.label}`)
  lines.push("")
  lines.push(`src:    ${src}`)
  if (extra.sessionID) lines.push(`sess:   ${String(extra.sessionID).slice(0, 16)}`)
  if (extra.model && typeof extra.model === "object") {
    const m = extra.model as { providerID?: string; id?: string }
    if (m.providerID) lines.push(`model:  ${m.providerID}/${m.id ?? ""}`)
  } else if (typeof extra.model === "string") {
    lines.push(`model:  ${extra.model}`)
  }
  if (extra.attempt) lines.push(`attempt:${extra.attempt}`)
  lines.push("")
  lines.push("detail:")
  lines.push("  " + String(extra.text ?? "").slice(0, 400).replace(/\s+/g, " ").trim())
  return lines.join("\n")
}

export default Plugin.define({
  id: "opencode-cc-alert",

  async setup(ctx) {
    const fileCfg = loadConfig()
    const cfg: AlertConfig = { ...fileCfg, ...((ctx.options ?? {}) as AlertConfig) }
    if (cfg.enabled === false) {
      console.log("[cc-alert] disabled by config")
      return
    }
    console.log(`[cc-alert] enabled; push=${cfg.ccPushPath ?? "<default>"} dedupMs=${cfg.dedupMs ?? 60_000}`)

    const lastSeen = new Map<string, number>()
    const DEDUP_MS = Number(cfg.dedupMs ?? 60_000)

    const shouldNotify = (sig: string) => {
      const now = Date.now()
      const prev = lastSeen.get(sig)
      if (prev && now - prev < DEDUP_MS) return false
      lastSeen.set(sig, now)
      return true
    }

    // Primary: provider retry hook (most reliable signal for model/provider issues)
    await ctx.session.hook("retry", (event) => {
      const err = (event as any).error ?? {}
      const msg = String(err.message ?? "")
      const hit = classify(`${err.type ?? ""} ${msg} ${err.status ?? ""}`)
      if (!hit) return
      const sig = `retry:${hit.id}:${err.status ?? ""}:${msg.slice(0, 80)}`
      if (!shouldNotify(sig)) return
      push(
        format(hit, "provider.retry", {
          sessionID: (event as any).sessionID,
          model: (event as any).model,
          attempt: (event as any).attempt,
          text: msg,
          status: err.status,
          type: err.type,
        }),
        cfg,
      )
    })

    // Secondary: event stream - catches session-level errors that don't go through retry
    const controller = new AbortController()
    void (async () => {
      try {
        for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
          const t = String((ev as any).type ?? "")
          if (!/error|fail|denied/i.test(t)) continue
          const props = (ev as any).properties ?? ev
          const text = JSON.stringify(props).slice(0, 800)
          const hit = classify(text)
          if (!hit) continue
          const sig = `ev:${hit.id}:${t}:${text.slice(0, 60)}`
          if (!shouldNotify(sig)) continue
          push(
            format(hit, t, {
              sessionID: (props as any).sessionID,
              text,
            }),
            cfg,
          )
        }
      } catch (e) {
        console.warn("[cc-alert] event stream ended:", e)
      }
    })()

    return () => controller.abort()
  },
})