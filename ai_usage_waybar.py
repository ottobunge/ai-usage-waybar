#!/usr/bin/env python3
"""ai_usage_waybar — Waybar custom module for AI coding-plan quota & spend.

Providers: Anthropic (Claude), Codex (ChatGPT), Z.AI, Kimi, MiniMax, DeepSeek,
OpenCode (local spend), Cursor (experimental, off by default).

Stdlib only. No keys ever leave this machine; config lives in
~/.config/ai-usage-waybar/config.json (see config.example.json).

  ./ai_usage_waybar.py            # waybar JSON {text, tooltip, class}
  ./ai_usage_waybar.py --json     # machine-readable per-provider dump
  ./ai_usage_waybar.py --selftest # parser checks against embedded fixtures
  ./ai_usage_waybar.py --cycle-next | --cycle-prev  # click/scroll carousel handlers
"""
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "ai-usage-waybar"
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "ai-usage-waybar/config.json"
OPENCODE_AUTH = HOME / ".local/share/opencode/auth.json"
CLAUDE_CRED = HOME / ".claude/.credentials.json"
CODEX_AUTH = HOME / ".codex/auth.json"
CURSOR_DBS = [
    Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "Cursor/User/globalStorage/state.vscdb",
    HOME / ".cursor/User/globalStorage/state.vscdb",
]

# Per-provider minimum seconds between live endpoint hits (cache enforces it).
# Anthropic 429s aggressively — never lower it (skill: ai-plan-usage-tracking).
MIN_INTERVALS = {"anthropic": 300, "codex": 120, "zai": 120, "kimi": 120,
                 "minimax": 120, "deepseek": 600, "opencode": 60, "cursor": 600}

DEFAULTS = {
    "mode": "all",  # "all" or e.g. "single:codex"
    "providers": {},  # e.g. {"cursor": true} to enable experimental ones
    "carousel": {"enabled": True, "seconds": 5},  # rotate one provider per tick
    "notify": {"enabled": True, "quota_warn_pct": 80, "quota_crit_pct": 95,
               "money_warn_below": 10.0},
}
SIGNAL = 8  # waybar "signal": 8 → module re-execs on SIGRTMIN+8 (click/scroll)

OK, WARN, CRIT = "ok", "warn", "crit"
LEVEL_ORDER = {OK: 0, WARN: 1, CRIT: 2}


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH) as f:
            overlay = json.load(f)
        for k, v in overlay.items():
            if k in ("notify", "carousel") and isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:  # bad config must never blank the bar
        print(f"config error: {e}", file=sys.stderr)
    return cfg


def enabled(pid, cfg):
    return bool(cfg["providers"].get(pid, pid != "cursor"))


def http_json(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


# --- cache (also the 429 shield) -------------------------------------------

def _cache_path(name):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{name}.json"


def cache_get(name, max_age):
    try:
        with open(_cache_path(name)) as f:
            d = json.load(f)
        if time.time() - d["ts"] <= max_age:
            return d["data"]
    except Exception:
        pass
    return None


def cache_put(name, data):
    try:
        with open(_cache_path(name), "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
    except Exception:
        pass


def fetch_cached(name, fn):
    """Run provider fn, but serve cache when fresher than MIN_INTERVALS[name];
    on failure fall back to stale cache rather than dropping the segment."""
    cached = cache_get(name, MIN_INTERVALS[name])
    if cached is not None:
        return cached
    result = fn()
    if not result.get("error"):
        cache_put(name, result)
    elif (stale := cache_get(name, 10 ** 9)) is not None:
        stale = dict(stale)
        stale["stale"] = True
        return stale
    return result


# --- shared helpers ---------------------------------------------------------

def fmt_ts(v):
    """Best-effort human time from epoch seconds, epoch ms, or ISO string."""
    if v in (None, "", 0):
        return ""
    try:
        if isinstance(v, (int, float)):
            ts = v / 1000 if v > 10 ** 12 else v
            dt = datetime.fromtimestamp(ts)
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone()
        now = datetime.now()
        day = f"{dt:%a %H:%M}" if dt.date() != now.date() else f"{dt:%H:%M}"
        return day
    except Exception:
        return str(v)


def epoch_of(v):
    """Best-effort epoch seconds from epoch s, epoch ms, or ISO string."""
    if v in (None, "", 0):
        return None
    try:
        if isinstance(v, (int, float)):
            return v / 1000 if v > 10 ** 12 else float(v)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fmt_in(epoch, now=None):
    """Countdown until an epoch: '45m', '3h 12m', '2d 4h'."""
    now = time.time() if now is None else now
    if not epoch:
        return ""
    s = epoch - now
    if s <= 0:
        return "now"
    m, h = int(s // 60), int(s // 3600)
    if m < 1:
        return "<1m"
    if h < 1:
        return f"{m}m"
    if h < 24:
        return f"{h}h {int(s % 3600 // 60)}m"
    return f"{int(s // 86400)}d {h % 24}h"


def pct(v):
    try:
        return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return None


def opencode_keys():
    try:
        with open(OPENCODE_AUTH) as f:
            d = json.load(f)
        return {k: v.get("key") for k, v in d.items() if isinstance(v, dict) and v.get("key")}
    except Exception:
        return {}


def key_for(env_name, oc_names):
    key = os.environ.get(env_name)
    if key:
        return key
    keys = opencode_keys()
    for n in oc_names:
        if keys.get(n):
            return keys[n]
    return None


def item(pid, label, kind, windows, error=None, extra=None, currency="USD", counts=None):
    """windows: list of (name, used_pct, reset_str[, reset_epoch]).
    counts: list of (label, used, total_or_None) — raw usage counters where the
    provider exposes them (plan units, request counts), for the panel."""
    levels = []
    cfgn = load_config()["notify"]
    for w in windows:
        p = w[1]
        if p is None:
            continue
        levels.append(CRIT if p >= cfgn["quota_crit_pct"]
                      else WARN if p >= cfgn["quota_warn_pct"] else OK)
    level = max(levels, key=LEVEL_ORDER.get) if levels else OK
    return {"id": pid, "label": label, "kind": kind, "windows": windows,
            "level": level, "error": error, "extra": extra or [], "currency": currency,
            "counts": counts or []}


# --- providers --------------------------------------------------------------

def provider_anthropic():
    try:
        with open(CLAUDE_CRED) as f:
            tok = json.load(f).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        tok = None
    if not tok:
        return item("anthropic", "Claude", "quota", [], error="no OAuth token (run `claude` once)")
    try:
        _, d = http_json("https://api.anthropic.com/api/oauth/usage",
                         {"Authorization": f"Bearer {tok}",
                          "anthropic-beta": "oauth-2025-04-20"})
    except urllib.error.HTTPError as e:
        return item("anthropic", "Claude", "quota", [], error=f"HTTP {e.code}")
    except Exception as e:
        return item("anthropic", "Claude", "quota", [], error=str(e))
    windows = []
    by_kind = {l.get("kind"): l for l in d.get("limits", []) if isinstance(l, dict)}
    for kind, label in (("session", "5h"), ("weekly_all", "week")):
        l = by_kind.get(kind)
        if l is not None:
            windows.append((label, pct(l.get("percent")), fmt_ts(l.get("resets_at")),
                            epoch_of(l.get("resets_at"))))
    if not windows:  # older shape
        for key, label in (("five_hour", "5h"), ("seven_day", "week")):
            w = d.get(key) or {}
            if w.get("utilization") is not None:
                windows.append((label, pct(w["utilization"]), fmt_ts(w.get("resets_at")),
                                epoch_of(w.get("resets_at"))))
    scoped = [f"{l['scope']['model']['display_name']}: {l['percent']:.0f}%"
              for l in d.get("limits", [])
              if l.get("kind") == "weekly_scoped" and (l.get("scope") or {}).get("model")]
    return item("anthropic", "Claude", "quota", windows, extra=scoped)


def provider_codex():
    try:
        with open(CODEX_AUTH) as f:
            tok = json.load(f).get("tokens", {}).get("access_token")
    except Exception:
        tok = None
    if not tok:
        return item("codex", "Codex", "quota", [], error="no OAuth token (run `codex login`)")
    try:
        _, d = http_json("https://chatgpt.com/backend-api/wham/usage",
                         {"Authorization": f"Bearer {tok}", "User-Agent": "codex_cli_rs"})
    except urllib.error.HTTPError as e:
        return item("codex", "Codex", "quota", [], error=f"HTTP {e.code}")
    except Exception as e:
        return item("codex", "Codex", "quota", [], error=str(e))
    windows = []
    rl = d.get("rate_limit") or {}
    for wkey in ("primary_window", "secondary_window"):
        w = rl.get(wkey) or {}
        secs = w.get("limit_window_seconds")
        if secs and w.get("used_percent") is not None:
            label = "5h" if secs <= 21600 else "week"
            windows.append((label, pct(w["used_percent"]), fmt_ts(w.get("reset_at")),
                            epoch_of(w.get("reset_at"))))
    extra = []
    credits = d.get("credits") or {}
    if credits.get("has_credits"):
        extra.append(f"credits: {float(credits.get('balance', 0)):,.0f}")
    return item("codex", "Codex", "quota", windows, extra=extra)


def provider_zai():
    key = key_for("ZAI_API_KEY", ("zai-coding-plan", "zai"))
    if not key:
        return item("zai", "Z.AI", "quota", [], error="no key (ZAI_API_KEY or opencode auth.json)")
    base = os.environ.get("ZAI_BASE_URL", "https://api.z.ai")
    try:
        _, d = http_json(f"{base}/api/monitor/usage/quota/limit",
                         {"Authorization": key})  # raw key, NO Bearer prefix
    except urllib.error.HTTPError as e:
        return item("zai", "Z.AI", "quota", [], error=f"HTTP {e.code}")
    except Exception as e:
        return item("zai", "Z.AI", "quota", [], error=str(e))
    windows = []
    # 2026-10: the quota payload nests limits under "data"; accept both shapes.
    limits = (d.get("data") or {}).get("limits") or d.get("limits") or []
    counts = []
    for l in limits:
        if not isinstance(l, dict):
            continue
        if l.get("type") == "TIME_LIMIT" and l.get("remaining") is not None:
            allowance = l.get("usage") or 0
            counts.append((f"month units ({l.get('number')}{'m' if l.get('unit') == 5 else '?'})",
                           max(allowance - l["remaining"], 0), allowance))
    for l in limits:
        if not isinstance(l, dict):
            continue
        unit, num = l.get("unit"), l.get("number")
        label = "5h" if unit == 3 or num == 5 else "week" if unit == 6 or num == 1 else f"unit{unit}"
        windows.append((label, pct(l.get("percentage")), fmt_ts(l.get("nextResetTime")),
                        epoch_of(l.get("nextResetTime"))))
    return item("zai", "Z.AI", "quota", windows, counts=counts)


def kimi_windows(usages):
    """Kimi usage payload -> window tuples. The weekly key is limit_7d on the
    current API shape (limit_week on the old one); they are the same window."""
    windows, seen = [], set()
    for key_, label in (("limit_5h", "5h"), ("limit_7d", "week"), ("limit_week", "week"),
                        ("limit_month_total", "month"), ("limit_month_code", "month-code")):
        u = usages.get(key_) if isinstance(usages, dict) else None
        if not isinstance(u, dict) or u.get("used_ratio") is None or label in seen:
            continue
        seen.add(label)
        windows.append((label, pct(float(u["used_ratio"]) * 100), fmt_ts(u.get("reset_time")),
                        epoch_of(u.get("reset_time"))))
    return windows


def provider_kimi():
    key = key_for("KIMI_API_KEY", ("kimi", "kimi-coding"))
    if not key:
        return item("kimi", "Kimi", "quota", [],
                    error="no key (KIMI_API_KEY = sk-kimi-… console key, not platform key)")
    try:
        _, d = http_json("https://api.kimi.com/coding/v1/usages",
                         {"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as e:
        return item("kimi", "Kimi", "quota", [],
                    error=f"HTTP {e.code}" + (" (401 = wrong key type; need Kimi Code console key)"
                                              if e.code == 401 else ""))
    except Exception as e:
        return item("kimi", "Kimi", "quota", [], error=str(e))
    usages = d.get("usages") or {}
    windows = kimi_windows(usages)
    if not windows and isinstance(d.get("limits"), list):
        for l in d["limits"]:
            if isinstance(l, dict) and l.get("limit"):
                windows.append((str(l.get("window", "?")),
                                pct(100.0 * float(l.get("usage", 0)) / float(l["limit"])),
                                fmt_ts(l.get("reset_time")), epoch_of(l.get("reset_time"))))
    return item("kimi", "Kimi", "quota", windows)


def provider_minimax():
    key = key_for("MINIMAX_API_KEY", ("minimax",))
    if not key:
        return item("minimax", "MiniMax", "quota", [],
                    error="no key (MINIMAX_API_KEY = Subscription Key from console/plan)")
    base = os.environ.get("MINIMAX_BASE_URL", "https://www.minimax.io")
    try:
        _, d = http_json(f"{base}/v1/token_plan/remains",
                         {"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as e:
        hint = " (2057 = standard key, need Subscription Key)" if e.code in (400, 401, 403) else ""
        return item("minimax", "MiniMax", "quota", [], error=f"HTTP {e.code}{hint}")
    except Exception as e:
        return item("minimax", "MiniMax", "quota", [], error=str(e))
    # Per-model 5h-style interval windows; the API reports
    # *_remaining_percent (REMAINING), the bar shows USED, so invert.
    # The payload also carries weekly_* fields, but MiniMax plans are
    # interval-only — no weekly limit — so they are not shown.
    windows, extras = [], []
    for m in d.get("model_remains") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("model_name", "?"))
        span = max(int(m.get("end_time", 0)) - int(m.get("start_time", 0)), 0)
        if span > 10 ** 6:  # epoch milliseconds
            span //= 1000
        rem = m.get("current_interval_remaining_percent")
        if isinstance(rem, (int, float)):
            windows.append((f"{name} {span // 3600}h".strip(), pct(100.0 - float(rem)),
                            fmt_ts(m.get("end_time")), epoch_of(m.get("end_time"))))
    if not windows:
        extras.append(f"unparsed keys: {sorted(d)[:6]}")
    counts = [(f"{m.get('model_name', '?')} req", m.get("current_interval_usage_count"),
               m.get("current_interval_total_count"))
              for m in (d.get("model_remains") or []) if isinstance(m, dict)]
    return item("minimax", "MiniMax", "quota", windows, extra=extras, counts=counts)


def provider_deepseek(state):
    key = key_for("DEEPSEEK_API_KEY", ("deepseek",))
    if not key:
        return item("deepseek", "DeepSeek", "money", [], error="no key (DEEPSEEK_API_KEY)")
    try:
        _, d = http_json("https://api.deepseek.com/user/balance",
                         {"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as e:
        return item("deepseek", "DeepSeek", "money", [], error=f"HTTP {e.code}")
    except Exception as e:
        return item("deepseek", "DeepSeek", "money", [], error=str(e))
    infos = d.get("balance_infos") or []
    info = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else None)
    if not info:
        return item("deepseek", "DeepSeek", "money", [], error="no balance_infos")
    try:
        bal = float(info["total_balance"])
    except (KeyError, TypeError, ValueError):
        return item("deepseek", "DeepSeek", "money", [], error="bad balance payload")
    # spend-rate = diff of persisted samples (no history endpoint exists)
    samples = state.setdefault("deepseek_samples", [])
    now = time.time()
    if not samples or now - samples[-1][0] > 3600:
        samples.append([now, bal])
        del samples[:-50]
    spent = days = None
    if len(samples) >= 2:
        t0, b0 = samples[0]
        spent, days = b0 - bal, max((now - t0) / 86400, 1e-9)
    cfgn = load_config()["notify"]
    level = CRIT if not d.get("is_available") or bal <= 0 \
        else WARN if bal < cfgn["money_warn_below"] else OK
    extra = [f"spend ${spent:.2f} over {days:.1f}d (≈${spent / days:.2f}/d)"] if spent is not None else []
    return {"id": "deepseek", "label": "DeepSeek", "kind": "money",
            "windows": [("balance", bal, "")], "level": level,
            "error": None, "extra": extra, "currency": info.get("currency", "USD")}


def provider_opencode():
    """Local session-spend estimate — opencode has NO balance API (open FR)."""
    root = Path(os.environ.get("OPENCODE_DATA_DIR",
                               HOME / ".local/share/opencode"))
    today = datetime.now().strftime("%Y-%m-%d")
    total, n = 0.0, 0
    for p in root.glob("**/storage/session/message/*.json"):
        try:
            d = json.loads(p.read_text())
            if str(d.get("time", {}).get("created", "")).startswith(today):
                total += float(d.get("cost", 0) or 0)
                n += 1
        except Exception:
            continue
    if n == 0:
        return item("opencode", "OpenCode", "money", [],
                    error="no local session data today (spend estimate only)")
    return {"id": "opencode", "label": "OpenCode", "kind": "money",
            "windows": [("today", total, "")], "level": OK,
            "error": None, "extra": [f"{n} messages (local estimate, not account balance)"],
            "currency": "USD"}


def provider_cursor():
    """Experimental: unofficial dashboard API via local session token.
    ponytail: Cursor ships no individual API; this breaks on their billing
    changes — failures are isolated to this segment."""
    db = next((p for p in CURSOR_DBS if p.exists()), None)
    if not db:
        return item("cursor", "Cursor", "money", [], error="state.vscdb not found (Cursor not installed?)")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = dict(con.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%'").fetchall())
        con.close()
    except Exception as e:
        return item("cursor", "Cursor", "money", [], error=f"sqlite: {e}")
    token = rows.get("cursorAuth/accessToken")
    if not token:
        return item("cursor", "Cursor", "money", [], error="no accessToken in state.vscdb")
    uid = None
    for k, v in rows.items():
        if "user" in k.lower() and isinstance(v, str) and 6 < len(v) < 128 and "::" not in v:
            uid = v
            break
    if not uid:
        return item("cursor", "Cursor", "money", [],
                    error="no userId found; update provider_cursor() for your install")
    cookie = f"WorkosCursorSessionToken={uid}%3A%3A{token}"
    try:
        _, d = http_json(f"https://www.cursor.com/api/usage?userId={uid}",
                         {"Cookie": cookie})
    except urllib.error.HTTPError as e:
        return item("cursor", "Cursor", "money", [], error=f"HTTP {e.code} (endpoints move; re-derive)")
    except Exception as e:
        return item("cursor", "Cursor", "money", [], error=str(e))
    # defensive: sum chargedCents if the shape matches, else show raw keys
    spent = limit = None

    def walk(o):
        nonlocal spent, limit
        if isinstance(o, dict):
            if isinstance(o.get("chargedCents"), (int, float)):
                spent = (spent or 0) + o["chargedCents"] / 100
            for k, v in o.items():
                if "monthlylimit" in k.replace("_", "").lower() and isinstance(v, (int, float)):
                    limit = v
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(d)
    if spent is None:
        return item("cursor", "Cursor", "money", [],
                    error=f"unparsed keys: {sorted(d)[:6] if isinstance(d, dict) else type(d).__name__}")
    extra = [f"limit ${limit:.2f}" if limit else "limit unknown"]
    return {"id": "cursor", "label": "Cursor", "kind": "money",
            "windows": [("spent", spent, "")], "level": OK,
            "error": None, "extra": extra, "currency": "USD"}


# --- rendering & notifications ----------------------------------------------

PROVIDERS = [("anthropic", "A"), ("codex", "X"), ("zai", "Z"), ("kimi", "K"),
             ("minimax", "M"), ("deepseek", "D"), ("opencode", "O"), ("cursor", "U")]


def gather(cfg, state):
    out = []
    for pid, _ in PROVIDERS:
        if not enabled(pid, cfg):
            continue
        if pid == "deepseek":
            r = fetch_cached(pid, lambda: provider_deepseek(state))
        elif pid == "anthropic":
            r = fetch_cached(pid, provider_anthropic)
        elif pid == "codex":
            r = fetch_cached(pid, provider_codex)
        elif pid == "zai":
            r = fetch_cached(pid, provider_zai)
        elif pid == "kimi":
            r = fetch_cached(pid, provider_kimi)
        elif pid == "minimax":
            r = fetch_cached(pid, provider_minimax)
        elif pid == "opencode":
            r = fetch_cached(pid, provider_opencode)
        elif pid == "cursor":
            r = fetch_cached(pid, provider_cursor)
        else:
            continue
        out.append(r)
    return out


def fmt_money(v, cur):
    sym = "$" if cur == "USD" else f"{cur} "
    return f"{sym}{v:,.2f}"


def bar_text(windows):
    """Segment numbers for a quota provider: 'short/week' when the plan has a
    weekly window (e.g. Claude 0/100%), else the worst single window. A
    weekly cap at 100% blocks the short windows too, so they read 100."""
    week = next((w[1] for w in windows if "week" in w[0] and w[1] is not None), None)
    if week is None:
        return f"{max((w[1] for w in windows if w[1] is not None), default=0):.0f}%"
    short = next((w[1] for w in windows
                  if "week" not in w[0] and w[1] is not None), None)
    if short is not None and week >= 100:
        short = 100.0  # no capacity left regardless of the short window
    if short is None:
        return f"{week:.0f}%"
    return f"{short:.0f}/{week:.0f}%"


def exhausted_reset_suffix(windows):
    """' (in 2h 5m)' naming the soonest reset among windows at 100% used."""
    full = [w for w in windows
            if w[1] is not None and w[1] >= 100 and len(w) > 3 and w[3]]
    if not full:
        return ""
    left = fmt_in(min(w[3] for w in full))
    return f" (in {left})" if left and left != "now" else ""


def segment_text(it):
    """One bar segment for a provider ('Claude:0/100%' / 'DeepSeek:$9.37')."""
    if it["kind"] == "quota":
        return f"{it['label']}:{bar_text(it['windows'])}" + exhausted_reset_suffix(it["windows"])
    v = it["windows"][0][1]
    return f"{it['label']}:{fmt_money(v, it['currency'])}"


TRENDS = (  # (label, age seconds, match tolerance seconds) — tolerances are
    # tighter than the sampling spacing so a lookback only matches genuinely
    # old samples; otherwise every point reads +0% against "almost now".
    ("15m", 15 * 60, 120), ("30m", 30 * 60, 120), ("45m", 45 * 60, 120),
    ("60m", 60 * 60, 120), ("4h", 4 * 3600, 600), ("8h", 8 * 3600, 600),
    ("12h", 12 * 3600, 600), ("24h", 24 * 3600, 600),
    ("7d", 7 * 86400, 3600), ("14d", 14 * 86400, 3600), ("30d", 30 * 86400, 3600),
)


def trend_text(samples, pid, windows, now=None):
    """Burn of the provider's first usable window vs age-matched old samples."""
    now = time.time() if now is None else now
    win = next((w for w in windows if w[1] is not None), None)
    if win is None or not samples:
        return ""
    label, cur = win[0], win[1]
    parts = []
    for name, age, tol in TRENDS:
        target = now - age
        cand = [(s[0], s[1][pid][label]) for s in samples
                if abs(s[0] - target) <= tol and label in s[1].get(pid, {})]
        if cand:
            _, base = min(cand, key=lambda p: abs(p[0] - target))
            parts.append(f"{name} {cur - base:+.0f}%")
    if parts:
        return f"burn ({label}): " + " · ".join(parts)
    if any(pid in s[1] for s in samples):
        return f"burn ({label}): collecting history…"
    return ""


def record_usage_samples(items, state):
    """Persist per-provider window percentages so tooltips can show burn.

    Two tiers: ~45 s spacing for the last day (15m..24h lookbacks) and the
    freshest sample per hour for a month (7d/14d/30d)."""
    now = time.time()
    recs = {it["id"]: {w[0]: w[1] for w in it["windows"]}
            for it in items if not it["error"] and it["kind"] == "quota"}
    if not recs:
        return
    samples = state.setdefault("usage_samples", [])
    if not samples or now - samples[-1][0] >= 45:
        samples.append([now, recs])
    del samples[:-1980]  # ≈24 h at 45 s spacing
    hourly = state.setdefault("usage_samples_hourly", [])
    if not hourly or int(hourly[-1][0] // 3600) != int(now // 3600):
        hourly.append([now, recs])
    else:
        hourly[-1] = [now, recs]
    del hourly[:-745]  # ≈30 days of hourly points


def window_text(w, now=None):
    """One tooltip window line: '5h: 4% → 23:26 (in 1h 42m)'."""
    s = f"{w[0]}: {w[1]:.0f}%"
    if w[2]:
        s += f" → {w[2]}"
    left = fmt_in(w[3], now=now) if len(w) > 3 else ""
    if left:
        s += f" (in {left})"
    return s


def render(items, cfg, samples=None):
    mode = cfg["mode"]
    if mode.startswith("single:"):
        want = mode.split(":", 1)[1]
        items = [i for i in items if i["id"] == want] or items
    tips = ["<b>AI plan usage</b>"]
    texts, classes = [], []
    for it in items:
        label = it["label"]
        if it["error"]:
            tips.append(f"\n{label}: <i>{it['error']}</i>")
            continue
        stale = " (cached)" if it.get("stale") else ""
        if it["kind"] == "quota":
            w = "  ".join(window_text(x) for x in it["windows"])
        else:
            v = it["windows"][0][1]
            w = f"{it['windows'][0][0]}: {fmt_money(v, it['currency'])}"
        texts.append(segment_text(it))
        tips.append(f"\n<b>{label}</b>{stale}  {w}")
        tips += [f"  {e}" for e in it["extra"]]
        if it["kind"] == "quota":
            burn = trend_text(samples or [], it["id"], it["windows"])
            if burn:
                tips.append(f"  {burn}")
        classes.append(it["level"])
    # Carousel: the widget shows one provider per tick at a fixed size (set a
    # min-width in the CSS); the tooltip always carries every provider. A
    # click/scroll pin overrides the auto-rotation until changed again.
    car = cfg.get("carousel") or {}
    if car.get("enabled", True) and texts:
        pin = load_selection()
        pin_idx = next((j for j, it in enumerate(items)
                        if not it.get("error") and it["id"] == pin), None)
        if pin_idx is not None:
            text, cls = texts[pin_idx], classes[pin_idx]
            tips.insert(1, f"<i>{pin_idx + 1}/{len(texts)} · click or scroll to cycle</i>")
        else:
            secs = max(int(car.get("seconds", 5) or 5), 1)
            shown = int(time.time() / secs) % len(texts)
            text, cls = texts[shown], classes[shown]
    else:
        text = "  ".join(texts) or "AI:—"
        cls = max(classes, key=lambda c: LEVEL_ORDER[c]) if classes else OK
    return {"text": text, "tooltip": "\n".join(tips), "class": cls}


def notify(items, cfg, state):
    n = cfg["notify"]
    if not n.get("enabled") or not items:
        return
    nstate = state.setdefault("notified", {})
    for it in items:
        if it["error"]:
            continue
        if it["kind"] == "quota":
            worst = max((w[1] for w in it["windows"] if w[1] is not None), default=0)
            label = next((f"{w[0]} {worst:.0f}%" for w in it["windows"] if w[1] == worst), "")
            level = CRIT if worst >= n["quota_crit_pct"] else WARN if worst >= n["quota_warn_pct"] else OK
            msg = f"{it['label']} {label}"
        else:
            bal = it["windows"][0][1]
            level = CRIT if bal <= 0 else WARN if bal < n["money_warn_below"] else OK
            msg = f"{it['label']} balance {fmt_money(bal, it['currency'])}"
        prev = nstate.get(it["id"], OK)
        if LEVEL_ORDER[level] > LEVEL_ORDER[prev] and level in (WARN, CRIT):
            try:  # no notification daemon → never kill the widget
                subprocess.run(["notify-send", "-a", "ai-usage-waybar",
                                "-u", "critical" if level == CRIT else "normal",
                                "AI plan usage", msg], check=False, timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
        elif level == OK and prev != OK:
            nstate.pop(it["id"], None)  # re-arm after recovery
            continue
        nstate[it["id"]] = level


def load_state():
    try:
        with open(CACHE_DIR / "state.json") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    """Merge-and-save. A process that loaded state minutes ago (an exec doing
    slow live calls, a manual run) must never clobber newer history the bar
    wrote in the meantime — union the sample lists by timestamp instead of
    replacing them."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            disk = load_state()
        except Exception:
            disk = {}
        for key, keep, bucket in (("usage_samples", 1980, None),
                                  ("usage_samples_hourly", 745, 3600),
                                  ("deepseek_samples", 50, None)):
            mine = state.get(key)
            theirs = (disk or {}).get(key)
            if mine is None:
                if theirs:
                    state[key] = theirs[-keep:]
                continue
            if theirs:
                by_ts = {s[0]: s for s in theirs}
                by_ts.update({s[0]: s for s in mine})
                merged = [by_ts[t] for t in sorted(by_ts)]
            else:
                merged = list(mine)
            if bucket:  # overlapping writers inject same-hour entries; keep the freshest
                by_bucket = {}
                for s in merged:
                    by_bucket[int(s[0] // bucket)] = s
                merged = [by_bucket[b] for b in sorted(by_bucket)]
            state[key] = merged[-keep:]
        tmp = CACHE_DIR / f"state.json.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, CACHE_DIR / "state.json")
    except Exception:
        pass


# --- carousel click/scroll cycling ------------------------------------------
#
# Without user input the widget auto-rotates (render's time bucket). A click
# or wheel event pins a provider: --cycle-next / --cycle-prev move the pin and
# nudge waybar via SIGRTMIN+SIGNAL so the change is instant. The pin lives in
# its own file — untouched by save_state's merge-on-save sample machinery.

def cycle_idx(idx, n, direction):
    return (idx + (1 if direction == "next" else -1)) % n


def load_selection():
    try:
        with open(CACHE_DIR / "selection.json") as f:
            return json.load(f).get("id")
    except Exception:
        return None


def cycle_selection(cfg, direction):
    if not (cfg.get("carousel") or {}).get("enabled", True):
        return None
    saved = load_state().get("carousel_ids")
    ids: list[str] = saved if isinstance(saved, list) and saved else \
        [pid for pid, _ in PROVIDERS if enabled(pid, cfg)]
    if not ids:
        return None
    cur = load_selection()
    nxt = ids[cycle_idx(ids.index(cur) if cur in ids else 0, len(ids), direction)]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_DIR / f"selection.json.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"id": nxt}, f)
        os.replace(tmp, CACHE_DIR / "selection.json")
    except Exception:
        pass
    try:  # waybar re-execs now; if this misses, its next tick picks it up
        subprocess.run(["pkill", f"-RTMIN+{SIGNAL}", "waybar"], check=False,
                       timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass
    return nxt


# --- selftest ---------------------------------------------------------------

def selftest():
    cfgn = {"quota_warn_pct": 80, "quota_crit_pct": 95, "money_warn_below": 10.0}
    # anthropic limits[] shape
    a = {"limits": [
        {"kind": "session", "percent": 41.5, "resets_at": "2026-10-27T18:00:00+00:00"},
        {"kind": "weekly_all", "percent": 100, "resets_at": "2026-10-28T22:00:00+00:00",
         "severity": "critical"},
        {"kind": "weekly_scoped", "percent": 6,
         "scope": {"model": {"display_name": "Fable"}}}]}
    wins = {(l["kind"]): l for l in a["limits"]}
    assert wins["weekly_all"]["percent"] == 100
    assert wins["session"]["percent"] == 41.5
    # codex windows: 604800 = week, 21600 = 5h; 5h may be absent
    c = {"rate_limit": {"primary_window": {"used_percent": 8,
         "limit_window_seconds": 604800, "reset_at": 1793599689}}}
    w = c["rate_limit"]["primary_window"]
    assert ("week" if w["limit_window_seconds"] > 21600 else "5h") == "week"
    # zai unit mapping
    assert ("5h" if 3 == 3 else "week") == "5h" and ("week" if 6 == 6 else "5h") == "week"
    # kimi used_ratio 0..1
    assert pct(0.87 * 100) == 87.0
    # bar text: plans with a weekly window show 5h/week, others the worst window
    assert bar_text([("5h", 41.0, ""), ("week", 100.0, "")]) == "100/100%"
    assert bar_text([("5h", 41.0, ""), ("week", 99.0, "")]) == "41/99%"
    assert bar_text([("week", 10.0, "")]) == "10%"
    assert bar_text([("5h", 41.0, "")]) == "41%"
    assert bar_text([("5h", None, ""), ("week", 6.0, "")]) == "6%"
    # kimi: the weekly key is limit_7d on the current API shape, limit_week on the old
    kw = kimi_windows({"limit_5h": {"used_ratio": 0, "reset_time": "2026-10-27T18:18:16Z"},
                       "limit_7d": {"used_ratio": 1, "reset_time": "2026-10-30T18:18:16Z"}})
    assert [w[0] for w in kw] == ["5h", "week"]
    assert kw[1][1] == 100.0 and len(kw[1]) == 4 and kw[1][3] is not None
    assert kimi_windows({"limit_week": {"used_ratio": 0.5}})[0][0] == "week"
    assert kimi_windows({}) == []
    # zai nests limits under data; accept both shapes
    z = {"data": {"limits": [{"unit": 6, "percentage": 12}]}}
    assert z["data"]["limits"][0]["percentage"] == 12
    # minimax reports REMAINING percents; the bar shows USED (inverted)
    assert pct(100.0 - 94) == 6.0
    assert "week" in "M2 week" and "week" not in "M2 4h"
    # trend: burn vs the nearest sample at each lookback (incl. long tiers)
    now = 10 ** 9
    s = [[now - 900, {"x": {"5h": 10.0}}],           # exact 15m
         [now - 3600, {"x": {"5h": 5.0}}],           # exact 60m
         [now - 14400, {"x": {"5h": 2.0}}],          # exact 4h
         [now - 14 * 86400, {"x": {"5h": 1.0}}]]     # exact 14d
    t = trend_text(s, "x", [("5h", 12.0, "")], now=now)
    assert "15m +2%" in t and "60m +7%" in t and "4h +10%" in t and "14d +11%" in t
    assert "30m" not in t and "45m" not in t and "7d" not in t  # no age-matched samples
    # a too-recent sample must not feed a lookback: still collecting
    assert trend_text([[now - 720, {"x": {"5h": 1.0}}]], "x",
                      [("5h", 1.0, "")], now=now) == "burn (5h): collecting history…"
    assert trend_text(s, "missing", [("5h", 12.0, "")], now=now) == ""
    # countdown formatting + epoch parsing
    now = 10 ** 9
    assert fmt_in(now + 3 * 3600 + 12 * 60, now=now) == "3h 12m"
    assert fmt_in(now + 2 * 86400 + 4 * 3600, now=now) == "2d 4h"
    assert fmt_in(now + 45 * 60, now=now) == "45m"
    assert fmt_in(now - 5, now=now) == "now"
    assert epoch_of(1793599689000) == 1793599689.0
    assert epoch_of("2026-10-27T18:00:00+00:00") is not None
    assert window_text(("5h", 4.0, "23:26", now + 3600 + 42 * 60), now=now) == "5h: 4% → 23:26 (in 1h 42m)"
    assert window_text(("week", 100.0, "", None)) == "week: 100%"
    # state round-trip under a temp cache dir (atomic replace path)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        global CACHE_DIR
        _orig = CACHE_DIR
        CACHE_DIR = Path(td)
        try:
            save_state({"usage_samples": [[1, {"x": {"5h": 1.0}}], [2, {"x": {"5h": 2.0}}]]})
            assert load_state()["usage_samples"] == [[1, {"x": {"5h": 1.0}}], [2, {"x": {"5h": 2.0}}]]
            # a stale snapshot (missing newer samples) must not erase them
            save_state({"usage_samples": [[2, {"x": {"5h": 2.0}}], [3, {"x": {"5h": 3.0}}]]})
            assert [x[0] for x in load_state()["usage_samples"]] == [1, 2, 3]
            save_state({"usage_samples_hourly": [[3601, {"x": {}}]]})
            save_state({"usage_samples_hourly": [[7100, {"x": {}}]]})
            got = load_state()["usage_samples_hourly"]
            assert [x[0] for x in got] == [7100]  # same hour bucket → freshest wins
        finally:
            CACHE_DIR = _orig
        # the tmp file is per-process: concurrent writers never share it
        assert "getpid" in save_state.__code__.co_names
    # carousel segment text
    assert segment_text({"label": "Claude", "kind": "quota",
                         "windows": [("5h", 0.0, ""), ("week", 100.0, "")]}) == "Claude:100/100%"
    # exhausted windows surface the time to reset in the segment
    import time as _t
    assert exhausted_reset_suffix([("week", 100.0, "", _t.time() + 3 * 3600)]) == " (in 2h 59m)"
    assert exhausted_reset_suffix([("week", 99.9, "", _t.time() + 3 * 3600)]) == ""
    assert exhausted_reset_suffix([("week", 100.0, "", None)]) == ""
    assert segment_text({"label": "Claude", "kind": "quota",
                         "windows": [("5h", 0.0, ""), ("week", 100.0, "", _t.time() + 3 * 3600)]}) == \
        "Claude:100/100% (in 2h 59m)"
    # carousel click/scroll: wrap-around both directions
    ids = ["a", "b", "c"]
    assert ids[cycle_idx(2, 3, "next")] == "a" and ids[cycle_idx(0, 3, "prev")] == "c"
    assert ids[cycle_idx(1, 3, "next")] == "c" and ids[cycle_idx(0, 3, "next")] == "b"
    # pin: render shows the pinned provider and its position; no pin → rotation
    with tempfile.TemporaryDirectory() as td2:
        CACHE_DIR = Path(td2)
        try:
            (CACHE_DIR / "selection.json").write_text('{"id": "codex"}')
            cfgc = {"mode": "all", "carousel": {"enabled": True, "seconds": 5}}
            its = [
                {"id": "anthropic", "label": "Claude", "kind": "quota",
                 "windows": [("5h", 10.0, "")], "level": OK, "error": None,
                 "extra": [], "currency": "USD"},
                {"id": "codex", "label": "Codex", "kind": "quota",
                 "windows": [("week", 20.0, "")], "level": OK, "error": None,
                 "extra": [], "currency": "USD"},
                {"id": "zai", "label": "Z.AI", "kind": "quota", "windows": [],
                 "level": OK, "error": "no key", "extra": [], "currency": "USD"},
            ]
            r = render(its, cfgc)
            assert r["text"] == "Codex:20%", r["text"]  # pinned; error slots skipped
            assert "2/2 · click or scroll to cycle" in r["tooltip"]
            (CACHE_DIR / "selection.json").unlink()
            r2 = render(its, cfgc)
            assert r2["text"].startswith(("Claude:", "Codex:"))  # auto-rotation
            assert "click or scroll" not in r2["tooltip"]
        finally:
            CACHE_DIR = _orig
    # deepseek money level
    lvl = CRIT if False else WARN if 5.0 < cfgn["money_warn_below"] else OK
    assert lvl == WARN
    # level ordering
    assert max([OK, WARN], key=LEVEL_ORDER.get) == WARN
    assert max([OK, CRIT], key=LEVEL_ORDER.get) == CRIT
    print("selftest ok")


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        selftest()
        return
    cfg = load_config()
    for flag, direction in (("--cycle-next", "next"), ("--cycle-prev", "prev")):
        if flag in args:  # click/scroll path: move the pin, no network
            cycle_selection(cfg, direction)
            return
    state = load_state()
    items = gather(cfg, state)
    record_usage_samples(items, state)
    state["carousel_ids"] = [i["id"] for i in items if not i["error"]]
    if "--json" in args:
        print(json.dumps(items, indent=2, default=str))
        return
    notify(items, cfg, state)
    save_state(state)
    pool = state.get("usage_samples", []) + state.get("usage_samples_hourly", [])
    print(json.dumps(render(items, cfg, pool)))


if __name__ == "__main__":
    main()
