// opencode-cc-alert: push OpenCode provider errors to Feishu via cc-push.
// No npm imports - plain default export (same shape as phone-notify), so it
// loads without @opencode/plugin (which cannot be installed on this machine).
//
// Detectors:
//   1. ctx.session.hook("retry")  - provider failures w/ status/type/message
//   2. ctx.event.subscribe        - fallback for session/error-shaped events
// Patterns: 401/403/429/451, auth, quota, geo-block, model unknown, expired.

import { appendFileSync, existsSync, readFileSync, writeFileSync, mkdirSync } from "node:fs"
import { spawn } from "node:child_process"
import { tmpdir, homedir } from "node:os"
import { join } from "node:path"
import { fileURLToPath } from "node:url"

const PATTERNS: ReadonlyArray<{ id: string; re: RegExp; label: string; emoji: string }> = [
  { id: "auth", re: /(?:\b401\b|unauthori[sz]ed|authentication.{0,20}(?:required|fail)|reauth|please.{0,10}sign.?in|login.{0,10}required|api[_\s-]?key.{0,10}(?:invalid|incorrect|missing)|invalid.{0,10}api[_\s-]?key)/i, label: "需要登录或 API Key 无效", emoji: "[AUTH]" },
  { id: "forbidden", re: /(?:\b403\b|forbidden|access.{0,10}denied|permission.{0,10}denied)/i, label: "权限被拒绝", emoji: "[403]" },
  { id: "ratelimit", re: /(?:\b429\b|rate[_\s-]?limit|too many requests)/i, label: "速率限制", emoji: "[429]" },
  { id: "geo", re: /(?:\b451\b|geo[_\s-]?block|not available in (?:your )?(?:country|region)|region.{0,10}restrict|unsupported.{0,10}region|country.{0,10}not supported)/i, label: "当前地区不可用", emoji: "[GEO]" },
  { id: "quota", re: /(quota|usage.{0,5}limit|insufficient.{0,10}(?:quota|balance|credit)|billing|payment.{0,5}required|用量上限|额度)/i, label: "额度/余额用完", emoji: "[QUOTA]" },
  { id: "model", re: /(model.{0,20}(?:not (?:found|exist|available))|unsupported model|unknown model|invalid model|model.{0,10}deprecated|模型不可用)/i, label: "模型不可用", emoji: "[MODEL]" },
  { id: "session", re: /(login.{0,10}expired|session.{0,10}expired|credentials.{0,10}(?:invalid|expired)|token.{0,10}expired)/i, label: "登录/会话过期", emoji: "[EXPIRED]" },
]

function classify(text: string) {
  if (!text) return null
  for (const p of PATTERNS) if (p.re.test(text)) return p
  return null
}

function loadConfig(): any {
  try {
    // config.json sits next to this file (works through the junction too)
    const here = fileURLToPath(import.meta.url)
    const path = join(dirnameSafe(here), "config.json")
    if (existsSync(path)) return JSON.parse(readFileSync(path, "utf8"))
  } catch {}
  return {}
}

function dirnameSafe(p: string): string {
  const i = Math.max(p.lastIndexOf("\\"), p.lastIndexOf("/"))
  return i > 0 ? p.slice(0, i) : p
}

const flog = (line: string) => {
  try {
    appendFileSync(join(tmpdir(), "opencode-cc-alert.log"), `${new Date().toISOString()} ${line}\n`)
  } catch {}
}

// Presence gate: same convention as android-phone SKILL.md - if the user's
// keyboard/mouse idle time is below the threshold they are at the computer
// and will see the error in the TUI, so a Feishu push is noise.
function getIdleSeconds(cfg: any): Promise<number | null> {
  return new Promise((resolve) => {
    try {
      const script = cfg.userIdlePath || join(homedir(), ".local", "bin", "user-idle.ps1")
      if (!existsSync(script)) return resolve(null)
      const child = spawn(
        "powershell.exe",
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
        { windowsHide: true },
      )
      let out = ""
      child.stdout?.on("data", (d: Buffer) => (out += String(d)))
      child.on("error", () => resolve(null))
      child.on("exit", () => {
        const n = parseInt(String(out).trim(), 10)
        resolve(Number.isFinite(n) ? n : null)
      })
      const t = setTimeout(() => {
        try { child.kill() } catch {}
        resolve(null)
      }, 5000)
      child.on("exit", () => clearTimeout(t))
    } catch {
      resolve(null)
    }
  })
}

async function pushIfAway(message: string, cfg: any, src: string) {
  if (cfg.idleCheck === false) return push(message, cfg)
  const threshold = Number(cfg.idleThresholdSec ?? 180)
  const idle = await getIdleSeconds(cfg)
  if (idle !== null && idle < threshold) {
    flog(`skip push (${src}): user at computer (idle ${idle}s < ${threshold}s)`)
    return
  }
  flog(`push (${src}): user away (idle=${idle ?? "n/a"}s >= ${threshold}s)`)
  push(message, cfg)
}

function push(message: string, cfg: any) {
  try {
    if (!cfg.sessionKey) {
      console.warn("[cc-alert] no sessionKey in config.json; skip push")
      return
    }
    const tmp = join(tmpdir(), "opencode-cc-alert.txt")
    writeFileSync(tmp, message, "utf8")
    const pushPath = cfg.ccPushPath || join(homedir(), ".local", "bin", "cc-push.ps1")
    // cc-push.ps1 takes a single arg (file path or text); the target session
    // is baked into the script itself and matches config.json.
    const child = spawn(
      "powershell.exe",
      ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", pushPath, tmp],
      { windowsHide: true, detached: true, stdio: "ignore" },
    )
    child.on("error", (e: unknown) => console.warn("[cc-alert] spawn error:", e))
    child.unref()
  } catch (e) {
    console.warn("[cc-alert] push failed:", e)
  }
}

function format(hit: ReturnType<typeof classify>, src: string, extra: Record<string, unknown>): string {
  const lines: string[] = []
  lines.push(`${hit!.emoji} OpenCode ${hit!.label}`)
  lines.push("")
  lines.push(`src:   ${src}`)
  if (extra.sessionID) lines.push(`sess:  ${String(extra.sessionID).slice(0, 20)}`)
  const m = extra.model as any
  if (m && typeof m === "object" && m.providerID) lines.push(`model: ${m.providerID}/${m.id ?? ""}`)
  else if (typeof extra.model === "string") lines.push(`model: ${extra.model}`)
  if (extra.attempt) lines.push(`try:   ${extra.attempt}`)
  lines.push("")
  lines.push("detail:")
  lines.push("  " + String(extra.text ?? "").slice(0, 400).replace(/\s+/g, " ").trim())
  return lines.join("\n")
}

const plugin: any = {
  id: "opencode-cc-alert",
  async setup(ctx: any) {
    if ((plugin as any).__started) return
    ;(plugin as any).__started = true

    const fileCfg = loadConfig()
    const cfg: any = { ...fileCfg, ...((ctx?.options ?? {}) as any) }
    if (cfg.enabled === false) {
      console.log("[cc-alert] disabled by config")
      return
    }
    console.log(`[cc-alert] enabled; push=${cfg.ccPushPath ?? "<default>"} dedupMs=${cfg.dedupMs ?? 60000}`)

    const lastSeen = new Map<string, number>()
    const DEDUP_MS = Number(cfg.dedupMs ?? 60_000)
    const shouldNotify = (sig: string) => {
      const now = Date.now()
      const prev = lastSeen.get(sig)
      if (prev && now - prev < DEDUP_MS) return false
      lastSeen.set(sig, now)
      return true
    }

    const controllers: Array<AbortController> = []
    flog(`plugin setup; hooks=` + [typeof ctx?.session?.hook === "function" ? "session.hook" : "", typeof ctx?.event?.subscribe === "function" ? "event.subscribe" : ""].filter(Boolean).join("+") + `; idleCheck=${cfg.idleCheck !== false} threshold=${cfg.idleThresholdSec ?? 180}s`)

    // 1) http.response hook - THE primary detector.
    // Sees every provider HTTP response. Catches terminal errors that never
    // reach the retry hook (e.g. AI.Error.Authentication geo-block, 401, 403).
    try {
      if (typeof ctx?.session?.hook === "function") {
        await ctx.session.hook("http.response", async (event: any) => {
          try {
            const res = event?.response
            if (!res || typeof res.status !== "number" || res.status < 400) return
            const status = res.status
            let body = ""
            try {
              const clone = res.clone()
              body = String(await clone.text()).slice(0, 600)
            } catch {}
            const hit = classify(`HTTP ${status} ${body} ${event?.kind ?? ""}`)
            if (!hit) return
            const sig = `http:${hit.id}:${status}:${body.slice(0, 60)}`
            if (!shouldNotify(sig)) return
            void pushIfAway(
              format(hit, `HTTP ${status}`, {
                sessionID: event?.sessionID,
                model: event?.model,
                text: body || "(no body)",
              }),
              cfg,
              `http ${status}`,
            )
          } catch (e) {
            flog("http.response hook error: " + String(e))
          }
        })
        flog("http.response hook registered")

        // 2) retry hook - fires when OpenCode classifies a failure as retryable.
        await ctx.session.hook("retry", (event: any) => {
          try {
            const err = event?.error ?? {}
            const msg = String(err.message ?? "")
            const hit = classify(`${err.type ?? ""} ${msg} ${err.status ?? ""}`)
            if (!hit) return
            const sig = `retry:${hit.id}:${err.status ?? ""}:${msg.slice(0, 80)}`
            if (!shouldNotify(sig)) return
            void pushIfAway(
              format(hit, "provider.retry", {
                sessionID: event?.sessionID,
                model: event?.model,
                attempt: event?.attempt,
                text: msg,
              }),
              cfg,
              "retry",
            )
          } catch (e) {
            flog("retry hook error: " + String(e))
          }
        })
        flog("retry hook registered")
      } else {
        flog("ctx.session.hook unavailable; event-stream only")
      }
    } catch (e) {
      flog("failed to register hooks: " + String(e))
    }

    // 3) event stream fallback
    try {
      if (typeof ctx?.event?.subscribe === "function") {
        const controller = new AbortController()
        controllers.push(controller)
        void (async () => {
          try {
            for await (const ev of ctx.event.subscribe({ signal: controller.signal })) {
              const t = String((ev as any)?.type ?? "")
              if (!/error|fail|denied/i.test(t)) continue
              const props = (ev as any)?.properties ?? ev
              const text = JSON.stringify(props).slice(0, 800)
              const hit = classify(text)
              if (!hit) continue
              const sig = `ev:${hit.id}:${t}:${text.slice(0, 60)}`
              if (!shouldNotify(sig)) continue
              void pushIfAway(format(hit, t, { sessionID: (props as any)?.sessionID, text }), cfg, t)
            }
          } catch (e) {
            flog("event stream ended: " + String(e))
          }
        })()
        flog("event stream subscribed")
      }
    } catch (e) {
      flog("failed to subscribe event stream: " + String(e))
    }

    return () => {
      for (const c of controllers) c.abort()
      ;(plugin as any).__started = false
    }
  },
}

export default plugin