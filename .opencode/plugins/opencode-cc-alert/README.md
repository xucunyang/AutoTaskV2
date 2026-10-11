# opencode-cc-alert

Global OpenCode plugin that pushes **system-level provider errors** to your phone via Feishu + cc-connect, so you know the moment your agent hits:

- Auth / API key problems (`401`, "please sign in", "invalid api key")
- Permission denied (`403`, "access denied")
- Rate limiting (`429`, "rate limit")
- Geo-blocking (`451`, "not available in your country/region")
- Quota / billing exhaustion ("quota", "insufficient credits", "billing required")
- Model not found / deprecated / unsupported
- Session / token expired

It uses two hooks from the official `Plugin.define` API:

- `ctx.session.hook("retry", ...)` - fires whenever the provider classifies a retryable failure
- `ctx.event.subscribe(...)` - catches other `session.error` / `message.error` shaped events

## How it works

1. Plugin loaded globally by OpenCode (every session, every project on this machine)
2. When a provider returns a known error pattern, it writes a one-liner to a tmp file and spawns `cc-push.ps1` to forward it to your Feishu chat (the same one used by cc-connect)
3. Dedup window (default 60s) prevents the same error from spamming you
4. The detection runs at the plugin layer, so it works **regardless of which session / project / agent** was active

## Install (this machine)

The plugin lives in this repo under `.opencode/plugins/opencode-cc-alert/`. To activate it globally:

```powershell
# Create junction (no admin needed for directory junctions)
if (Test-Path "$env:USERPROFILE\.config\opencode\plugins\opencode-cc-alert") {
    Remove-Item "$env:USERPROFILE\.config\opencode\plugins\opencode-cc-alert" -Recurse -Force
}
cmd /c "mklink /J `"$env:USERPROFILE\.config\opencode\plugins\opencode-cc-alert`" `"$PWD\.opencode\plugins\opencode-cc-alert`""
```

After restarting OpenCode, the plugin loads automatically. Look for `[cc-alert] enabled; ...` in the OpenCode log.

## Install (new machine)

```bash
git clone <this-repo>
cd <this-repo>
powershell -Command "cmd /c 'mklink /J \"%USERPROFILE%\.config\opencode\plugins\opencode-cc-alert\" \"%CD%\.opencode\plugins\opencode-cc-alert\"'"
# Install cc-push (or symlink the existing one):
cp C:\path\to\cc-push.ps1 ~/.local/bin/cc-push.ps1
# Make sure cc-connect is running
cc-connect
```

Then restart OpenCode. Same plugin, same config, same chat.

## Config

`config.json`:

| Field | Required | Notes |
|---|---|---|
| `enabled` | no (default true) | Set to `false` to disable |
| `sessionKey` | yes | Your cc-connect `feishu:<chat>:<user>` triple |
| `ccPushPath` | no (default `~/.local/bin/cc-push.ps1`) | Path to the push helper |
| `dedupMs` | no (default 60000) | Suppress identical alerts inside this window |

Override any of these via `ctx.options` in `~/.config/opencode/opencode.jsonc`:

```jsonc
{
  "plugins": [
    {
      "package": "~/.config/opencode/plugins/opencode-cc-alert",
      "options": { "enabled": true, "dedupMs": 30000 }
    }
  ]
}
```

## Testing

The plugin fires only on real provider errors. To verify the pipeline works without breaking your config:

1. Temporarily edit `config.json` -> set `dedupMs: 0`
2. Send a request with an invalid model name (e.g. `claude-nonexistent`)
3. Check Feishu - you should see a `[MODEL] OpenCode 模型不可用` message

## Limitations

- Windows-only (uses PowerShell + `cc-push.ps1`). Linux/macOS needs a different push helper.
- The retry hook only fires on **provider** errors. Bugs in your agent code, MCP failures, and tool errors are not covered here. Add more hooks as needed.
- "Model not found" patterns are best-effort - some providers use non-standard phrases. Extend `PATTERNS` in `index.ts` to match.