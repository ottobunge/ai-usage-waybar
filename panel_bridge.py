#!/usr/bin/env python3
"""panel_bridge — feeds the eww AI-usage panel (flicker-free design).

The eww config (~/.config/eww/eww.yuck) is a STATIC skeleton written once by
`panel_bridge.py --write-yuck`. It declares ONE defpoll that runs
`panel_bridge.py --emit panel_data` every 2s; that emits a flat JSON object
with preformatted fields per provider plus the absolute paths of two freshly
rendered chart PNGs (timestamped filenames so the image widgets reload
without eww recreating the window — that recreation was the flicker).

Usage:
  panel_bridge.py --write-yuck        render + install the static eww.yuck
  panel_bridge.py --cycle-zoom        cycle the quota charts' window
  panel_bridge.py --cycle-tps-zoom    cycle the tok/s chart's window (default 4h)
  panel_bridge.py --emit panel_data   print the live JSON (called by eww)
  panel_bridge.py --once              print the yuck skeleton to stdout
"""
import json
import sys
import math
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_usage_waybar as m  # noqa: E402  (fmt_in)
import tokenflow as tf  # noqa: E402  (CPA token-flow store)

HOME = Path.home()
CACHE = HOME / ".cache/ai-usage-waybar"
CONFIG = HOME / ".config/ai-usage-waybar/config.json"
PLANS = HOME / ".config/ai-usage-waybar/plans.json"
YUCK = HOME / ".config/eww/eww.yuck"
BRIDGE = Path(__file__).resolve()
ZOOM_FILE = CACHE / "zoom"
ZOOM_LEVELS = [(3, "3h"), (6, "6h"), (12, "12h"), (36, "36h"),
               (72, "3d"), (168, "7d")]
DEFAULT_ZOOM = 12  # hours — chart window default
# The tok/s chart has its own window: generation speed is a short-horizon
# signal, so it defaults to 4h and cycles independently of the quota charts.
TPS_ZOOM_FILE = CACHE / "tps-zoom"
TPS_ZOOM_LEVELS = [(1, "1h"), (2, "2h"), (4, "4h"), (8, "8h"), (24, "24h")]
DEFAULT_TPS_ZOOM = 4
PANEL_IDS = ["anthropic", "codex", "zai", "kimi", "minimax", "deepseek"]
PANEL_LABELS = {"anthropic": "Claude", "codex": "Codex", "zai": "Z.AI",
                "kimi": "Kimi", "minimax": "MiniMax", "deepseek": "DeepSeek"}
PROVIDER_COLORS = {
    "anthropic": (243, 139, 168), "codex": (148, 226, 213), "zai": (203, 166, 247),
    "kimi": (249, 226, 175), "minimax": (166, 227, 161), "deepseek": (137, 180, 250),
}
# Token-flow providers: the quota providers plus gateway-only ones (no plan quota).
TOKEN_IDS = PANEL_IDS + ["openrouter"]
TOKEN_LABELS = dict(PANEL_LABELS, openrouter="OpenRouter")
TOKEN_COLORS = dict(PROVIDER_COLORS, openrouter=(250, 179, 135))


def load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def worst_pct(item):
    vals = [w[1] for w in item.get("windows", []) if w[1] is not None]
    return max(vals) if vals else None


def burn_pct_h(samples, pid, now, hours):
    """%/h measured within the current post-reset monotonic segment of the
    lookback window; None when that segment is too short to be meaningful."""
    pool = sorted(samples, key=lambda s: s[0])
    pts = []
    for ts, recs in pool:
        if ts > now or now - ts > (hours + 1) * 3600:
            continue
        wins = recs.get(pid)
        if not wins:
            continue
        v = next((v for v in wins.values() if v is not None), None)
        if v is not None:
            pts.append((ts, v))
    if len(pts) < 2:
        return None
    seg = split_at_resets(pts)[-1]
    dt_h = (seg[-1][0] - seg[0][0]) / 3600
    if dt_h < 1 / 60:
        return None
    return (seg[-1][1] - seg[0][1]) / dt_h


def reset_in(item):
    epochs = [w[3] for w in item.get("windows", []) if len(w) > 3 and w[3]]
    if not epochs:
        return ""
    return m.fmt_in(min(epochs))


def cost_stats(pid, worst, burn_1h, plans):
    plan = (plans or {}).get(pid) or {}
    cost = plan.get("cost")
    if not cost:
        return ""
    days = plan.get("period_days", 30)
    per_pct = cost / 100.0
    used = (worst or 0) / 100.0 * cost
    rate = (burn_1h or 0) * per_pct
    proj = rate * 24
    parts = [f"${used:,.2f} of ${cost:,.2f}/{days}d"]
    if rate > 0:
        parts.append(f"${rate:,.2f}/h → ${proj:,.2f}/d")
        if (burn_1h or 0) > 0:
            parts.append(f"{100 / burn_1h:,.0f}h to 100% (${cost - used:,.2f} left)")
    return "  ·  ".join(parts)


def esc(s):
    return s.replace('"', "'")


def card_fields(pid, label, item, samples, plans, now):
    """Preformatted display fields for one provider."""
    level = (item or {}).get("level", "off")
    kind = (item or {}).get("kind", "quota")
    f = {"label": label, "klass": f"card {level}", "pct": "?", "bar": 0,
         "reset": "", "info": ""}
    err = (item or {}).get("error")
    if err:
        f.update(pct="err", info=str(err))
        return f
    wins = [(w[0], w[1]) for w in item.get("windows", [])]
    info = []
    if kind == "money":
        bal = next((v for _, v in wins if isinstance(v, (int, float))), None)
        cur = (item or {}).get("currency", "USD")
        sym = {"USD": "$", "EUR": "€"}.get(cur, cur + " ")
        f["pct"] = f"{sym}{bal:,.2f}" if bal is not None else "?"
    else:
        worst = worst_pct(item)
        f["pct"] = f"{worst:.0f}%"
        f["bar"] = int(worst or 0)
        rin = reset_in(item)
        if rin:
            left = 100 - (worst or 0)
            f["reset"] = f"resets in {rin} · {left:.0f}% left"
        info.append("  ·  ".join(f"{w[0]} {w[1]:.0f}%" for w in item.get("windows", [])
                                 if w[1] is not None))
        b1 = burn_pct_h(samples, pid, now, 1)
        b24 = burn_pct_h(samples, pid, now, 24)
        burns = []
        if b1 is not None:
            burns.append(f"{b1:+.1f}%/h")
        if b24 is not None:
            burns.append(f"24h {b24:+.0f}%")
        if burns:
            info.append("  ·  ".join(burns))
        c = cost_stats(pid, worst, b1, plans)
        if c:
            info.append(c)
    for lbl, used, total in item.get("counts") or []:
        if used is None:
            continue
        fu = f"{used:,}" if isinstance(used, int) else used
        ft = f"/{total:,}" if isinstance(total, int) and total else ""
        info.append(f"{lbl}: {fu}{ft}")
    info.extend(item.get("extra") or [])
    f["info"] = "\n".join(x for x in info if x)
    return f


def build_series(samples, hourly, now, hours=DEFAULT_ZOOM):
    """Summed-across-providers series as [(ts, value)] plus its speed."""
    pool = sorted((samples or []) + (hourly or []), key=lambda s: s[0])
    recent = [s for s in pool if now - s[0] <= hours * 3600]
    pts = []
    for ts, recs in recent:
        vals = []
        for pid, wins in recs.items():
            v = next((v for v in wins.values() if v is not None), None)
            if v is not None:
                vals.append(v)
        if vals:
            pts.append((ts, sum(vals)))
    if len(pts) < 2:
        return [], []
    step = max(len(pts) // 160, 1)
    kept = pts[::step]
    if kept[-1][0] != pts[-1][0]:
        kept.append(pts[-1])
    return kept, speed_from_pts(kept)


def provider_series(samples, hourly, pid, now, hours=DEFAULT_ZOOM):
    """[(ts, worst-window-pct)] for one provider over the lookback window."""
    pool = sorted((samples or []) + (hourly or []), key=lambda s: s[0])
    pts = []
    for ts, recs in pool:
        if now - ts > hours * 3600:
            continue
        wins = recs.get(pid)
        if not wins:
            continue
        vals = [v for v in wins.values() if v is not None]
        if vals:
            pts.append((ts, max(vals)))
    return pts


def split_at_resets(pts):
    """[(ts, v)] -> list of monotonically non-decreasing segments; a usage
    reset (value drop) starts a new segment instead of a fake -100%/h cliff."""
    segs, cur = [], []
    for p in pts:
        if cur and p[1] < cur[-1][1]:
            segs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segs.append(cur)
    return segs


def resample(pts, now, hours, slots=160, max_span=2 * 3600):
    """[(ts, v)] -> `slots` values on a time grid ending at `now`. Interior
    slots are linearly interpolated between neighbouring samples so a sparse
    (hourly) stretch stays one continuous line; a stretch with no sample for
    more than `max_span` seconds is a real gap (None); a usage reset (value
    drop) holds the old value and then falls in one step. The grid sits on
    absolute wall-clock boundaries so existing points don't shift between
    refreshes; only the final slot (= now) moves."""
    import bisect
    step = hours * 3600 / slots
    times = [(int(now // step) - (slots - 1) + i) * step for i in range(slots)]
    times[-1] = now
    ts_list = [t for t, _ in pts]
    out = []
    for t in times:
        i = bisect.bisect_right(ts_list, t)
        if i == 0:
            out.append(None)
            continue
        t0, v0 = pts[i - 1]
        if i == len(pts):
            out.append(v0 if t - t0 <= max_span else None)
            continue
        t1, v1 = pts[i]
        if t1 - t0 > max_span:
            out.append(None)
        elif v1 < v0:
            out.append(v0)
        else:
            out.append(v0 + (v1 - v0) * (t - t0) / (t1 - t0))
    return out


def smooth(vals, radius=2):
    """Moving average that ignores None and keeps None where the input is None."""
    out = []
    for i, v in enumerate(vals):
        if v is None:
            out.append(None)
            continue
        win = [x for x in vals[max(0, i - radius):i + radius + 1] if x is not None]
        out.append(sum(win) / len(win))
    return out


def rate_from_slots(vals, step_h, radius=2):
    """/h derivative of a resampled series; resets and gaps count as no change."""
    rate = [None]
    for a, b in zip(vals, vals[1:]):
        rate.append(None if a is None or b is None else max(b - a, 0) / step_h)
    return smooth(rate, radius)


def speed_from_pts(pts, max_points=160):
    """/h derivative of a [(ts, value)] series. The series is downsampled to
    ~max_points first so intervals are minutes-wide (45 s raw sampling would
    both skip the dt gate and produce rounding-jitter spikes); intervals
    spanning a usage reset (value drop) are skipped rather than counted as
    -N%/h."""
    step = max(len(pts) // max_points, 1)
    kept = pts[::step]
    if kept[-1][0] != pts[-1][0]:
        kept.append(pts[-1])
    speed = []
    for i in range(1, len(kept)):
        dt_h = (kept[i][0] - kept[i - 1][0]) / 3600
        delta = kept[i][1] - kept[i - 1][1]
        if dt_h >= 1 / 120 and delta >= 0:
            speed.append(round(delta / dt_h, 2))
    return speed


# --- tiny 3x5 pixel font for chart axis labels -------------------------------
FONT = {
    "0": ["XXX", "X.X", "X.X", "X.X", "XXX"],
    "1": [".X.", "XX.", ".X.", ".X.", "XXX"],
    "2": ["XXX", "..X", "XXX", "X..", "XXX"],
    "3": ["XXX", "..X", "XXX", "..X", "XXX"],
    "4": ["X.X", "X.X", "XXX", "..X", "..X"],
    "5": ["XXX", "X..", "XXX", "..X", "XXX"],
    "6": ["XXX", "X..", "XXX", "X.X", "XXX"],
    "7": ["XXX", "..X", "..X", "..X", "..X"],
    "8": ["XXX", "X.X", "XXX", "X.X", "XXX"],
    "9": ["XXX", "X.X", "XXX", "..X", "XXX"],
    "%": ["X.X", "..X", ".X.", "X..", "X.X"],
    "/": ["..X", "..X", ".X.", "X..", "X.."],
    "h": ["X..", "X..", "XXX", "X.X", "X.X"],
    "$": [".X.", "XXX", "X..", "XXX", ".X."],
    "-": ["...", "...", "XXX", "...", "..."],
    ".": ["...", "...", "...", "...", ".X."],
    "k": ["X..", "X.X", "XX.", "X.X", "X.X"],
    "t": [".X.", "XXX", ".X.", ".X.", ".XX"],
    "s": [".XX", "X..", ".X.", "..X", "XX."],
    "m": ["...", "...", "X.X", "XXX", "X.X"],
    ":": ["...", ".X.", "...", ".X.", "..."],
    " ": ["...", "...", "...", "...", "..."],
}


def draw_text(rows, x, y, text, rgb, scale=2):
    for ci, ch in enumerate(text):
        glyph = FONT.get(ch.lower() if ch not in FONT else ch)
        if glyph is None:
            continue
        for gy, grow in enumerate(glyph):
            for gx, gch in enumerate(grow):
                if gch != "X":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px, py = x + (ci * 4 + gx) * scale + dx, y + gy * scale + dy
                        if 0 <= px < len(rows[0]) and 0 <= py < len(rows):
                            rows[py][px] = rgb


def write_png(path, w, h, rgb_rows):
    """Minimal stdlib PNG writer: rgb_rows = list of (r, g, b) tuples per pixel."""
    import struct
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rgb_rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def _blend(rows, x, y, rgb, a):
    if a <= 0 or not (0 <= y < len(rows) and 0 <= x < len(rows[0])):
        return
    r0 = rows[y][x]
    rows[y][x] = (int(r0[0] + (rgb[0] - r0[0]) * a), int(r0[1] + (rgb[1] - r0[1]) * a),
                  int(r0[2] + (rgb[2] - r0[2]) * a))


def aa_line(rows, x0, y0, x1, y1, rgb, width=2.0, alpha=1.0):
    """Anti-aliased line segment (coverage from distance to the segment)."""
    half = width / 2 + 0.5
    dx, dy = x1 - x0, y1 - y0
    len2 = dx * dx + dy * dy or 1.0
    for py in range(int(min(y0, y1) - half), int(max(y0, y1) + half) + 1):
        for px in range(int(min(x0, x1) - half), int(max(x0, x1) + half) + 1):
            t = max(0.0, min(1.0, ((px + 0.5 - x0) * dx + (py + 0.5 - y0) * dy) / len2))
            d = math.hypot(px + 0.5 - (x0 + t * dx), py + 0.5 - (y0 + t * dy))
            _blend(rows, px, py, rgb, min(1.0, half - d) * alpha)


def aa_dot(rows, cx, cy, rgb, r=2.4):
    for py in range(int(cy - r - 1), int(cy + r + 2)):
        for px in range(int(cx - r - 1), int(cx + r + 2)):
            d = math.hypot(px + 0.5 - cx, py + 0.5 - cy)
            _blend(rows, px, py, rgb, min(1.0, r + 0.5 - d))


def draw_multi_chart(series, out_path, w, h, unit="%", ymin=None, ymax=None,
                     dots=False, max_gap=None, dim_gap=None, drop_alpha=None):
    """series: list of (values, rgb); values are on a shared time grid, None =
    no sample in that slot; an optional third element is the set of indices that
    get a dot when dots=True. Lines are anti-aliased. max_gap: samples more than
    this many slots apart are not joined; dim_gap: joins spanning more than
    this many slots (data was missing in between) are drawn faint; drop_alpha:
    a falling segment (usage reset) is drawn at this opacity. dots=True marks
    every real sample so a lone one is still visible."""
    pad_l, pad_r, pad_t, pad_b = 48, 6, 10, 6
    all_vals = [v for item in series for v in item[0] if v is not None] or [0, 0]
    lo = ymin if ymin is not None else min(all_vals)
    hi = ymax if ymax is not None else max(all_vals)
    if hi - lo < 1e-9:
        hi = lo + 1
    rows = [[(30, 32, 46)] * w for _ in range(h)]
    for frac in (0.0, 0.5, 1.0):
        y = pad_t + round((h - pad_t - pad_b) * (1 - frac))
        for xx in range(pad_l, w - pad_r, 1 if frac == 0.0 else 3):  # dashed, base solid
            rows[y][xx] = (49, 50, 68)
        val = lo + (hi - lo) * frac
        draw_text(rows, 2, y - 5, f"{val:.0f}{unit}", (166, 173, 200), scale=2)

    def to_pts(vals):
        n = max(len(vals), 2)
        return [(pad_l + i * (w - pad_l - pad_r - 1) / (n - 1),
                 pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo)), i, v)
                for i, v in enumerate(vals) if v is not None]

    for item in series:
        vals, rgb = item[0], item[1]
        marks = item[2] if len(item) > 2 else None  # indices that get a dot
        pts = to_pts(vals)
        for i in range(len(pts) - 1):
            (x0, y0, i0, v0), (x1, y1, i1, v1) = pts[i], pts[i + 1]
            if max_gap is not None and i1 - i0 > max_gap:
                continue  # too far apart to claim a trend: leave the dots alone
            alpha = 1.0
            if dim_gap is not None and i1 - i0 > dim_gap:
                alpha = 0.4
            if drop_alpha is not None and v1 < v0:
                alpha = drop_alpha
            aa_line(rows, x0, y0, x1, y1, rgb, 2.0, alpha)
        if dots or len(pts) == 1:
            for x, y, i, _ in pts:
                if marks is None or i in marks:
                    aa_dot(rows, x, y, rgb)
    write_png(out_path, w, h, rows)


def read_zoom():
    try:
        hours = int(ZOOM_FILE.read_text().strip())
        return hours if any(h == hours for h, _ in ZOOM_LEVELS) else DEFAULT_ZOOM
    except Exception:
        return DEFAULT_ZOOM


def read_tps_zoom():
    try:
        hours = int(TPS_ZOOM_FILE.read_text().strip())
        return hours if any(h == hours for h, _ in TPS_ZOOM_LEVELS) else DEFAULT_TPS_ZOOM
    except Exception:
        return DEFAULT_TPS_ZOOM


def tps_zoom_label(hours):
    return next((lbl for h, lbl in TPS_ZOOM_LEVELS if h == hours), f"{hours}h")


def tps_view(db, now):
    """Effective window for the tok/s chart given the history we actually have.

    A zoom level longer than the stored data stays locked, so the line always
    spans the chart left to right. While there is less than the smallest level
    of data, the window is simply all the data. Returns a dict: hours, label,
    unlocked (levels, ascending), next_lock ((hours, unlock epoch) or None)."""
    first = tf.first_ts(db)
    span = (now - first) / 3600 if first else 0.0
    unlocked = [h for h, _ in TPS_ZOOM_LEVELS if h <= span]
    if unlocked:
        want = read_tps_zoom()
        hours = max(h for h in unlocked if h <= want)
        label = tps_zoom_label(hours)
    else:  # less than the smallest level: show everything we have
        hours = max(span, 1 / 60) + 1 / 3600  # pad 1s so the oldest sample is inside
        label = f"{max(round(span * 60), 1)}m"
    locked = [h for h, _ in TPS_ZOOM_LEVELS if h > span]
    next_lock = (locked[0], first + locked[0] * 3600) if locked and first else None
    return {"hours": hours, "label": label, "unlocked": unlocked, "next_lock": next_lock}


def tps_title(view, now):
    t = f"generation speed — tok/s ({view['label']}, latest (max))"
    if len(view["unlocked"]) > 1:
        t += " · click = zoom"
    if view["next_lock"]:
        h, epoch = view["next_lock"]
        t += f" · {tps_zoom_label(h)} in {m.fmt_in(epoch, now)}"
    return t


def zoom_label(hours):
    return next((lbl for h, lbl in ZOOM_LEVELS if h == hours), f"{hours}h")


def chart_series(now, hours=DEFAULT_ZOOM, slots=160):
    state = load_json(CACHE / "state.json") or {}
    samples = state.get("usage_samples") or []
    hourly = state.get("usage_samples_hourly")
    step_h = hours / slots
    total_pts, _ = build_series(samples, hourly, now, hours)
    usage, speeds = [], []
    for p in PANEL_IDS:
        pts = provider_series(samples, hourly, p, now, hours)
        if len(pts) >= 2:
            vals = resample(pts, now, hours, slots)
            usage.append((vals, PROVIDER_COLORS[p]))
            speeds.append((rate_from_slots(vals, step_h), PROVIDER_COLORS[p]))
    if total_pts:
        vals = resample(total_pts, now, hours, slots)
        usage.append((vals, (235, 235, 245)))
        speeds.append((rate_from_slots(vals, step_h), (235, 235, 245)))
    return usage, speeds


def token_speed_series(db, now, hours=DEFAULT_ZOOM):
    """[(tok/s per slot with idle slots at 0, rgb, real-sample indices)] for every
    provider that has at least one streamed sample in the window."""
    by_provider = tf.speed_series(db, hours, now)
    out = []
    for p in TOKEN_IDS:
        if p not in by_provider:
            continue
        raw = by_provider[p]
        real = {i for i, v in enumerate(raw) if v is not None}
        # no tokens flowing = 0 tok/s: the line drops to the baseline instead of
        # leaving a hole; only the real samples get a dot
        out.append(([v or 0 for v in smooth_present(raw)], TOKEN_COLORS[p], real))
    return out


def smooth_present(vals):
    """3-point [1,2,1] smoothing over adjacent real samples only, so isolated
    bursts keep their value while neighbouring ones stop jittering."""
    out = []
    for i, v in enumerate(vals):
        if v is None:
            out.append(None)
            continue
        num, den = 2 * v, 2
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals) and vals[j] is not None:
                num, den = num + vals[j], den + 1
        out.append(round(num / den, 1))
    return out


def render_charts(now, chart_dir, hours=DEFAULT_ZOOM, tdb=None, tps_hours=DEFAULT_TPS_ZOOM):
    """Render the charts to timestamped files; prune old; return the paths.
    tdb is the token-flow store (None = no data yet); tps_hours its window."""
    chart_dir.mkdir(parents=True, exist_ok=True)
    usage, speeds = chart_series(now, hours)
    ts = int(now)
    total_png = chart_dir / f"total-{ts}.png"
    speed_png = chart_dir / f"speed-{ts}.png"
    tps_png = chart_dir / f"tps-{ts}.png"
    draw_multi_chart(usage, total_png, 610, 120, unit="%", ymin=0, drop_alpha=0.3)
    draw_multi_chart(speeds, speed_png, 610, 90, unit="%/h", ymin=0)
    draw_multi_chart(token_speed_series(tdb, now, tps_hours), tps_png, 610, 90,
                     unit="t/s", ymin=0, dots=True)
    for prefix in ("total-", "speed-", "tps-"):
        olds = sorted(chart_dir.glob(prefix + "*.png"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in olds[2:]:
            old.unlink(missing_ok=True)
    return total_png, speed_png, tps_png


def tps_legend(db, now, hours):
    """{provider: (latest tok/s, max tok/s)} over the chart window, from the
    same buckets the chart draws, so the legend always matches the lines."""
    out = {}
    for p, vals in tf.speed_series(db, hours, now).items():
        real = [v for v in vals if v is not None]
        if p in TOKEN_IDS and real:
            out[p] = (real[-1], max(real))
    return out


def panel_payload(now):
    state = load_json(CACHE / "state.json") or {}
    plans = load_json(PLANS) or {}
    samples = state.get("usage_samples") or []
    hourly = state.get("usage_samples_hourly")
    hours = read_zoom()
    out = {"range": zoom_label(hours)}
    for p in PANEL_IDS:
        item = load_json(CACHE / f"{p}.json")
        item = item.get("data") if isinstance(item, dict) else None
        f = card_fields(p, PANEL_LABELS[p], item, samples, plans, now)
        for k, v in f.items():
            out[f"{p}_{k}"] = v
        pts = provider_series(samples, hourly, p, now, hours)
        sp = speed_from_pts(pts) if len(pts) >= 2 else []
        out[f"{p}_speed"] = f"{sp[-1]:+.1f}%/h" if sp else ""
    tdb = tf.connect(readonly=True)
    view = tps_view(tdb, now)
    total_png, speed_png, tps_png = render_charts(now, CACHE / "graphs", hours,
                                                  tdb, view["hours"])
    out["img_total"] = str(total_png)
    out["img_speed"] = str(speed_png)
    out["img_tps"] = str(tps_png)
    out["tps_title"] = tps_title(view, now)
    tps = tps_legend(tdb, now, view["hours"])
    if tdb:
        tdb.close()
    for p in TOKEN_IDS:
        out[f"{p}_tps"] = (
            f"{TOKEN_LABELS[p]} {tps[p][0]:.0f}t/s ({tps[p][1]:.0f})" if p in tps else "")
    out["clock"] = time.strftime("%H:%M:%S")
    return out


LEGEND = " ".join(
    f'(box :orientation "h" :space-evenly false '
    f'(box :class "ldot" :style "background-color: #{r:02x}{g:02x}{b:02x};") '
    f'(label :class "lname" :text "{PANEL_LABELS[p]}"))'
    for p, (r, g, b) in PROVIDER_COLORS.items())

TPS_ROW = " ".join(
    f'(label :class "lname" :style "color: #{r:02x}{g:02x}{b:02x}; margin-left: 12px;" '
    f':text {{panel_data.{p}_tps}})'
    for p, (r, g, b) in TOKEN_COLORS.items())

SPEED_ROW = " ".join(
    f'(label :class "lname" :style "color: #{r:02x}{g:02x}{b:02x}; margin-left: 12px;" '
    f':text {{panel_data.{p}_speed}})'
    for p, (r, g, b) in PROVIDER_COLORS.items())


def panel_geometry():
    """(monitor index, height px) from config.json "panel", else (0, 1080)."""
    cfg = (load_json(CONFIG) or {}).get("panel") or {}
    try:
        return int(cfg.get("monitor", 0)), int(cfg.get("height", 1080))
    except (TypeError, ValueError):
        return 0, 1080


def render_yuck(initial_json="{}"):
    """The STATIC skeleton — written once, never rewritten while running."""
    monitor, height = panel_geometry()
    cards = []
    for p in PANEL_IDS:
        d = f"panel_data.{p}_"
        cards.append(f"""  (box :class {{{d}klass}} :orientation "v" :space-evenly false
    (box :orientation "h" :space-evenly false :hexpand true
      (label :halign "start" :hexpand true :class "name" :text {{{d}label}})
      (label :halign "end" :hexpand false :class "pct" :text {{{d}pct}}))
    (progress :value {{{d}bar}} :max 100 :class "bar")
    (label :halign "start" :class "reset" :text {{{d}reset}})
    (label :halign "start" :class "info" :text {{{d}info}}))""")
    return f"""
(defpoll panel_data :interval "2s" :initial "{initial_json}" `{BRIDGE} --emit panel_data`)
(defwindow ai-panel :monitor {monitor} :stacking "fg"
  :geometry (geometry :x "0px" :y "0px" :width "640px" :height "{height}px" :anchor "top right")
  (box :orientation "v" :space-evenly false :class "root"
    (box :orientation "h" :space-evenly false :hexpand true
      (label :halign "start" :class "title" :text "AI usage")
      (label :halign "end" :hexpand false :class "clock"
        :text {{panel_data.clock}}))
    (scroll :vscroll true :hscroll false :vexpand true
     (box :orientation "v" :space-evenly false :class "body"
""" + "\n".join(cards) + f"""
    (button :class "gtitle" :onclick "{BRIDGE} --cycle-zoom"
      (label :halign "start" :class "gtitle"
        :text "usage — ${{panel_data.range}}, worst window per provider · click = zoom"))
    (box :orientation "h" :space-evenly false :class "legend" {LEGEND})
    (box :class "graph" (image :path {{panel_data.img_total}} :image-width 610 :image-height 120))
    (button :class "gtitle" :onclick "{BRIDGE} --cycle-zoom"
      (label :halign "start" :class "gtitle"
        :text "speed — %/h per provider (${{panel_data.range}}) · click = zoom"))
    (box :class "graph" (image :path {{panel_data.img_speed}} :image-width 610 :image-height 90))
    (box :orientation "h" :halign "end" :space-evenly false :class "legend" {SPEED_ROW})
    (button :class "gtitle" :onclick "{BRIDGE} --cycle-tps-zoom"
      (label :halign "start" :class "gtitle"
        :text {{panel_data.tps_title}}))
    (box :class "graph" (image :path {{panel_data.img_tps}} :image-width 610 :image-height 90))
    (box :orientation "h" :halign "end" :space-evenly false :class "legend" {TPS_ROW})))))
"""


def main():
    argv = sys.argv[1:]
    if "--write-yuck" in argv:
        payload = panel_payload(time.time())  # real values become :initial
        initial = json.dumps(payload, separators=(",", ":")).replace('"', '\\"')
        YUCK.write_text(render_yuck(initial))
        print(f"wrote {YUCK}")
        return
    if "--cycle-zoom" in argv:
        levels = [h for h, _ in ZOOM_LEVELS]
        cur = read_zoom()
        nxt = levels[(levels.index(cur) + 1) % len(levels)]
        ZOOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        ZOOM_FILE.write_text(str(nxt))
        print(f"zoom: {zoom_label(cur)} -> {zoom_label(nxt)}")
        return
    if "--cycle-tps-zoom" in argv:
        tdb = tf.connect(readonly=True)
        view = tps_view(tdb, time.time())
        if tdb:
            tdb.close()
        levels = view["unlocked"]
        if len(levels) < 2:
            print(f"tps zoom: only {view['label']} of data so far — nothing to cycle")
            return
        cur = max(h for h in levels if h <= read_tps_zoom())
        nxt = levels[(levels.index(cur) + 1) % len(levels)]
        TPS_ZOOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        TPS_ZOOM_FILE.write_text(str(nxt))
        print(f"tps zoom: {tps_zoom_label(cur)} -> {tps_zoom_label(nxt)}")
        return
    if "--emit" in argv:
        name = argv[argv.index("--emit") + 1]
        if name == "panel_data":
            print(json.dumps(panel_payload(time.time()), separators=(",", ":")))
        else:
            print(json.dumps({}))
        return
    if "--once" in argv:
        print(render_yuck())
        return
    print(__doc__)


if __name__ == "__main__":
    main()
