#!/usr/bin/env python3
"""aiq — Quotio-style CLI + loopback REST API for ai-usage-waybar.

Commands:
  aiq                 pretty table: provider, windows, resets, burn, cost
  aiq json            the full panel payload as JSON
  aiq tokens [hours]  per-provider token flow + generation speed (via CPA)
  aiq serve [--port N] [--host H]   REST API (default 127.0.0.1:8319):
      GET /usage                  live payload (all providers + costs)
      GET /usage?images=1         payload including local chart PNG paths
      GET /providers/<id>         one provider's fields
      GET /history?hours=N        sampled series (ts + per-provider %) for charts
      GET /tokens?hours=N         token flow: per-provider totals, tok/s,
                                  plus tokens/min and tok/s series
      GET /range                  current zoom levels list + active range

Stdlib only. Reads the same caches the waybar module and eww panel write;
never touches provider API keys.
"""
import json
import sys
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_bridge as pb  # noqa: E402
import tokenflow as tf  # noqa: E402

PORT_DEFAULT = 8319


def usage_payload(include_images=False):
    data = pb.panel_payload(time.time())
    if not include_images:
        data = {k: v for k, v in data.items() if not k.startswith("img_")}
    return data


def history(hours):
    state = pb.load_json(pb.CACHE / "state.json") or {}
    samples = state.get("usage_samples") or []
    hourly = state.get("usage_samples_hourly")
    pool = sorted((samples or []) + (hourly or []), key=lambda s: s[0])
    now = time.time()
    out = []
    for ts, recs in pool:
        if now - ts > hours * 3600:
            continue
        entry = {"ts": int(ts), "providers": {}, "total": 0}
        for pid, wins in sorted(recs.items()):
            vals = [v for v in wins.values() if v is not None]
            if vals:
                entry["providers"][pid] = round(max(vals), 1)
                entry["total"] += sum(vals)
        entry["total"] = round(entry["total"], 1)
        out.append(entry)
    return out


def tokens(hours):
    now = time.time()
    db = tf.connect(readonly=True)
    try:
        return {"range_hours": hours,
                "providers": tf.provider_stats(db, now, hours),
                "series": {"tokens_per_min": tf.rate_series(db, hours, now),
                           "tok_per_s": tf.speed_series(db, hours, now)}}
    finally:
        if db:
            db.close()


def providers_table(data):
    rows = []
    for p in pb.PANEL_IDS:
        g = lambda k: data.get(f"{p}_{k}", "")  # noqa: E731
        if data.get(f"{p}_info", "") and g("pct") == "err":
            rows.append((g("label"), "err", "", "", g("info")))
            continue
        wins = [k for k in (data.get(f"{p}_info", "") or "").split("\n")
                if "%" in k and "·" in k]
        rows.append((g("label"), g("pct"), g("reset"), g("reset")
                     and "", (data.get(f"{p}_info", "") or "").replace("\n", " | ")))
    return rows


def print_table(data):
    rows = providers_table(data)
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    print(f"AI usage — window {data.get('range', '?')}  ({time.strftime('%H:%M')})")
    for r in rows:
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(r, widths)))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/usage":
                self._send(200, usage_payload(q.get("images", ["0"])[0] == "1"))
            elif u.path.startswith("/providers/"):
                pid = u.path.rsplit("/", 1)[1]
                data = usage_payload()
                keys = [k for k in data if k.startswith(pid + "_")]
                if not keys:
                    self._send(404, {"error": f"unknown provider {pid}",
                                     "known": pb.PANEL_IDS})
                else:
                    self._send(200, {k[len(pid) + 1:]: data[k] for k in keys})
            elif u.path == "/history":
                hours = min(float(q.get("hours", ["36"])[0]), 24 * 30)
                self._send(200, {"range_hours": hours,
                                 "points": history(hours)})
            elif u.path == "/tokens":
                hours = min(float(q.get("hours", ["1"])[0]), 24 * 30)
                self._send(200, tokens(hours))
            elif u.path == "/range":
                self._send(200, {"levels": [lbl for _, lbl in pb.ZOOM_LEVELS],
                                 "active": pb.zoom_label(pb.read_zoom())})
            elif u.path == "/":
                self._send(200, {"service": "aiq",
                                 "endpoints": ["/usage", "/usage?images=1",
                                               "/providers/<id>", "/history?hours=N",
                                               "/tokens?hours=N", "/range"]})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never leak a traceback into the connection
            self._send(500, {"error": str(e)})

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # quiet


def main():
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv or not argv:
        if argv and argv[0] in ("--help", "-h"):
            pass
        elif not argv:
            print_table(usage_payload())
            return
    if argv[0] == "json":
        print(json.dumps(usage_payload(), indent=1))
    elif argv[0] == "tokens":
        tf.print_stats(float(argv[1]) if len(argv) > 1 else 1)
    elif argv[0] == "serve":
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else PORT_DEFAULT
        host = argv[argv.index("--host") + 1] if "--host" in argv else "127.0.0.1"
        print(f"aiq serving on http://{host}:{port}", flush=True)
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
