# ai-usage-waybar

Waybar module tracking AI coding-plan usage: **Anthropic (Claude), Codex
(ChatGPT), Z.AI, Kimi, MiniMax, DeepSeek, OpenCode, Cursor** — plan-quota
windows (5h/week/month %) and prepaid money ($ balance / spend) in one widget,
with desktop notifications on threshold crossings.

Single Python file, stdlib only, no daemon.

```
Claude:0/100%  Codex:10%  DeepSeek:$42.10  OpenCode:$1.23
```

Quota segments show `short/week` when the plan has a weekly window
(`Claude:41/100%` = 5h window at 41%, week at 100%); plans without one keep a
single worst-window number. A segment whose window has hit 100% also shows the
time to reset — `Claude:0/100% (in 2h 5m)` — naming the soonest-resetting
exhausted window. The tooltip lists every window with its reset time and a
countdown — `5h: 4% → 23:26 (in 1h 42m)` — wherever the provider exposes
one.

The widget rotates one provider at a time (carousel) at a fixed size — give the
module a `min-width` in the CSS so the bar never jumps; the tooltip always
carries every provider with all windows, reset times, and a burn line
(`burn (5h): 15m +2% · 30m +3% … 24h +6% · 7d +12% … 30d +40%`) computed from
locally persisted samples — ~45 s resolution for the last day, hourly points
for a month. Lookbacks: 15/30/45/60 m, 4/8/12/24 h, 7/14/30 d; a point shows
once the bar has been running long enough to have history for it. Disable with
`"carousel": {"enabled": false}` to get the classic one-line summary.

**Click/scroll control:** left click or wheel up = next provider, right click
or wheel down = previous. Clicking pins the provider (auto-rotation pauses,
tooltip shows your position, e.g. `2/7`); keep cycling to move the pin. Add
`"signal": 8` and the click handlers to your waybar config for instant
response:

```jsonc
"custom/aiusage": {
  "exec": "~/.local/bin/ai_usage_waybar.py",
  "return-type": "json",
  "interval": 5,
  "signal": 8,
  "tooltip": true,
  "on-click": "~/.local/bin/ai_usage_waybar.py --cycle-next",
  "on-click-right": "~/.local/bin/ai_usage_waybar.py --cycle-prev",
  "on-scroll-up": "~/.local/bin/ai_usage_waybar.py --cycle-next",
  "on-scroll-down": "~/.local/bin/ai_usage_waybar.py --cycle-prev"
}
```

(`signal: 8` is optional — without it the change lands on the next 5 s tick.
If you change `SIGNAL` in the script, change it here too.)

Hover shows every window with reset times; bar turns `warn`/`crit` as quotas
fill (configurable). `notify-send` fires once per threshold crossing and
re-arms after recovery.

## Providers & credentials

Keys are read from env vars, falling back to opencode's `auth.json`
(`~/.local/share/opencode/auth.json`). Nothing is stored in this repo.

| Provider | Needs | Where to get it | Notes |
|---|---|---|---|
| Anthropic | nothing extra | run `claude` once (OAuth file read directly) | unofficial usage endpoint, cached ≥5 min (it rate-limits hard) |
| Codex | nothing extra | run `codex login` | same endpoint the Codex CLI polls; also shows credit balance |
| Z.AI | `ZAI_API_KEY` | Z.AI coding-plan key | sent **without** `Bearer` prefix (their quirk) |
| Kimi | `KIMI_API_KEY` | **Kimi Code console** key (`sk-kimi-…`) — *not* a platform.kimi.com key (401) | 5h + weekly windows (`limit_7d`); a 100% week shows the 5h as 100% too |
| MiniMax | `MINIMAX_API_KEY` | **Subscription Key** from console → Plan Details (a standard API key gets error 2057) | per-model 5h windows (no weekly limit); the API reports remaining %, shown as used |
| DeepSeek | `DEEPSEEK_API_KEY` | platform.deepseek.com | official balance endpoint; spend rate derived from local samples |
| OpenCode | nothing | local session data | **local spend estimate only** — no balance API exists upstream |
| Cursor | enable in config | local Cursor install | **experimental**: unofficial dashboard API via local session token; breaks when Cursor changes billing; off by default |

## Install

```sh
cp ai_usage_waybar.py ~/.local/bin/ && chmod +x ~/.local/bin/ai_usage_waybar.py
mkdir -p ~/.config/ai-usage-waybar && cp config.example.json ~/.config/ai-usage-waybar/config.json
```

Waybar config:

```jsonc
"custom/aiusage": {
  "exec": "~/.local/bin/ai_usage_waybar.py",
  "return-type": "json",
  "interval": 5,
  "tooltip": true
}
```

The script self-throttles per provider (Anthropic 300 s, most others 120 s,
DeepSeek 600 s), so the bar can poll freely; responses are cached on disk and
served stale rather than dropping segments on network errors.

## Config

`~/.config/ai-usage-waybar/config.json` (all optional):

```jsonc
{
  "mode": "all",                  // or "single:codex"
  "providers": {"cursor": true},  // enable experimental cursor segment
  "carousel": {"enabled": true, "seconds": 5},
  "notify": {
    "enabled": true,
    "quota_warn_pct": 80,
    "quota_crit_pct": 95,
    "money_warn_below": 10.0
  }
}
```

## Eww side panel (optional)

A full-height eww panel (a side column on any Wayland compositor eww supports): per-provider cards (usage,
reset countdown, burn rate, request/plan counters, plan cost), a 24h usage
chart with a line per provider plus the summed total, and a per-provider
speed (%/h) chart with current values.

```sh
cp panel_bridge.py aiq.py ~/.local/bin/     # stdlib-only, python3
cp ~/.local/bin/aiq.py ~/.local/bin/aiq     # short command name
panel_bridge.py --write-yuck                # writes ~/.config/eww/eww.yuck
eww daemon && eww open ai-panel             # window `ai-panel`
```

The yuck is a static skeleton; a single `defpoll` runs
`panel_bridge.py --emit panel_data` every 2s and feeds all values (including
timestamped chart PNG paths, so images refresh without eww recreating the
window). Plan costs are optional per-provider entries in
`~/.config/ai-usage-waybar/plans.json`, e.g.
`{"anthropic": {"cost": 100, "period_days": 30}}`; with a burn rate present
the card derives $/h, projected $/day and time-to-100%. Pick the monitor and
panel height in `config.json`: `"panel": {"monitor": 0, "height": 1080}`.
To keep it running, wrap `eww daemon` in a systemd user unit (run
`panel_bridge.py --write-yuck` in `ExecStartPre`, `eww open ai-panel` in
`ExecStartPost`).

### CLI + REST API

`aiq` is the Quotio-style command surface over the same data:

```sh
aiq                 # table: provider, usage, reset, burn, cost
aiq json            # full payload as JSON
aiq serve           # loopback REST API (127.0.0.1:8319)
#   GET /usage              live payload (add ?images=1 for chart paths)
#   GET /providers/<id>     one provider
#   GET /history?hours=N    sampled series for your own charts
#   GET /range              zoom levels + active range
```

It only reads the on-disk caches, so it needs no provider keys and no
graphical session — a plain systemd user unit running `aiq serve` works.

## Token flow & generation speed (CLIProxyAPI)

If your CLIs route through a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
gateway, `tokenflow.py` records per-request tokens, latency and time-to-first-token
and the eww panel charts **generation speed (tok/s) per provider**.

CPA queues one usage record per request (needs `observability.usage.usage-statistics-enabled: true`
and a management key). The queue is *pop-on-read* with short retention
(`redis-usage-queue-retention-seconds`, max 3600), so run exactly one collector:

```sh
cp tokenflow.py ~/.local/bin/ && chmod +x ~/.local/bin/tokenflow.py
CLIPROXY_URL=http://127.0.0.1:8317 CLIPROXY_MGMT_KEY=<management-key> tokenflow.py collect
tokenflow.py stats 1        # or: aiq tokens 1
```

Run `tokenflow.py collect` as a systemd user unit (`Restart=always`, with the
env vars above in an `EnvironmentFile`) so it keeps draining.
Records land in `~/.cache/ai-usage-waybar/tokenflow.db` (counters and timings only —
no prompts or keys; 30-day retention). Speed = output tokens / generation seconds
(`latency - ttft`) over streamed, successful requests; non-stream requests count toward
volume only. `aiq serve` adds `GET /tokens?hours=N` (totals plus tokens/min and tok/s series).
The tok/s chart has its own window (default 4h; click its title to cycle 1h/2h/4h/8h/24h).
Its x-axis always spans the history that exists, so zoom levels longer than the stored
data stay locked (the title shows when the next one unlocks); with under an hour of
data the window is simply all of it.
A second collector waits on a lock instead of stealing records. The panel body scrolls,
so the extra chart never overflows a short monitor.

## Debug & test

```sh
ai_usage_waybar.py --selftest   # parser checks, no network
ai_usage_waybar.py --json       # per-provider dump (no secrets in output)
```

## Privacy

This repo contains no keys, tokens, or account identifiers. Credentials stay
in `~/.claude`, `~/.codex`, `~/.local/share/opencode`, your environment, or
`~/.config/ai-usage-waybar/` — all git-ignored or outside the repo. Only
usage percentages/balances are fetched; no prompts or message content ever
touch these endpoints.

## License

MIT
