#!/usr/bin/env python3
"""tokenflow — token-flow accounting for the CLIProxyAPI (CPA) gateway.

CPA emits one usage record per proxied request (tokens in/out/reasoning/
cached, latency, time-to-first-token, provider, model). The records sit in an
in-memory queue on the gateway and are *popped* by the management API, so
exactly one collector must drain them. This module is that collector plus the
aggregations the panel and `aiq` read: tokens/min and generation speed
(output tokens per second of generation time) per provider.

Usage:
  tokenflow.py collect            drain the queue forever into the SQLite store
  tokenflow.py stats [hours]      per-provider table (default 1h)
  tokenflow.py --selftest         parser + aggregation checks, no network

Environment (same env file the panel uses):
  CLIPROXY_MGMT_KEY   management key (required for `collect`)
  CLIPROXY_URL        gateway base URL (default http://127.0.0.1:8317)

Stdlib only. Stores no prompts, keys, or headers — only counters and timings.
"""
import fcntl
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DB_PATH = HOME / ".cache/ai-usage-waybar/tokenflow.db"
URL = os.environ.get("CLIPROXY_URL", "http://127.0.0.1:8317").rstrip("/")
POLL_S = 15
BATCH = 500
KEEP_DAYS = 30
BACKOFF_MAX_S = 600  # CPA bans an IP for 30 min after repeated bad-key attempts

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT,
  input INTEGER, output INTEGER, reasoning INTEGER, cached INTEGER,
  total INTEGER, latency_ms INTEGER, ttft_ms INTEGER, stream INTEGER,
  failed INTEGER, client_ip TEXT);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
"""

# Generation time: streamed requests exclude time-to-first-token (prompt
# processing); non-stream requests have no usable TTFT and are left out of the
# speed figures (they'd understate it) but still count toward volume.
GEN_MS = "(latency_ms - ttft_ms)"
SPEED_WHERE = ("stream = 1 AND failed = 0 AND ttft_ms > 0 "
               "AND ttft_ms < latency_ms AND output >= 10 AND "
               f"{GEN_MS} >= 300")


def panel_id(provider):
    """CPA provider string -> the panel's provider id."""
    p = (provider or "unknown").lower()
    if p.startswith("openai-compatible-"):
        p = p[len("openai-compatible-"):]
    return {"claude": "anthropic"}.get(p, p)


def parse_ts(s):
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


def to_row(rec):
    """CPA usage record (dict) -> store row tuple, or None if unusable."""
    if not isinstance(rec, dict) or "tokens" not in rec:
        return None
    t = rec["tokens"] or {}
    ts = parse_ts(rec.get("timestamp", ""))
    rid = rec.get("execution_id") or f"{rec.get('request_id', '')}@{ts}"
    return (rid, ts, panel_id(rec.get("provider")), rec.get("model") or "",
            int(t.get("input_tokens") or 0), int(t.get("output_tokens") or 0),
            int(t.get("reasoning_tokens") or 0), int(t.get("cached_tokens") or 0),
            int(t.get("total_tokens") or 0), int(rec.get("latency_ms") or 0),
            int(rec.get("ttft_ms") or 0), int(bool(rec.get("stream"))),
            int(bool(rec.get("failed"))), rec.get("client_ip") or "")


def connect(path=DB_PATH, readonly=False):
    if readonly:
        if not Path(path).exists():
            return None
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
    return db


def store(db, records):
    rows = [r for r in map(to_row, records) if r]
    db.executemany("INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return len(rows)


# --- aggregation --------------------------------------------------------------

def first_ts(db):
    """Timestamp of the oldest stored record, or None when the store is empty."""
    if db is None:
        return None
    row = db.execute("SELECT MIN(ts) FROM requests").fetchone()
    return row[0] if row else None


def rate_series(db, hours, now, buckets=60):
    """{provider: [tokens/min per bucket]} (zero-filled), oldest first."""
    if db is None:
        return {}
    span = hours * 3600
    width = span / buckets
    start = now - span
    out = {}
    for pid, b, tokens in db.execute(
            "SELECT provider, CAST((ts - ?) / ? AS INT), SUM(output) FROM requests "
            "WHERE ts >= ? AND ts <= ? GROUP BY 1, 2", (start, width, start, now)):
        if 0 <= b < buckets:
            out.setdefault(pid, [0.0] * buckets)[b] = tokens / (width / 60)
    return out


def speed_series(db, hours, now, buckets=60):
    """{provider: [tok/s per bucket, None where no streamed traffic]}, oldest
    first and time-aligned with rate_series. Each point is total tokens / total
    generation seconds in the bucket."""
    if db is None:
        return {}
    span = hours * 3600
    width = span / buckets
    start = now - span
    out = {}
    for pid, b, tok, ms in db.execute(
            f"SELECT provider, CAST((ts - ?) / ? AS INT), SUM(output), SUM({GEN_MS}) "
            f"FROM requests WHERE ts >= ? AND ts <= ? AND {SPEED_WHERE} "
            "GROUP BY 1, 2 ORDER BY 2", (start, width, start, now)):
        if ms:
            if 0 <= b < buckets:
                out.setdefault(pid, [None] * buckets)[b] = round(tok / (ms / 1000), 1)
    return out


def provider_stats(db, now, hours=1):
    """{provider: {tps, tok_h, requests, in_tok, out_tok, cached_tok}}.
    tps is the token-weighted generation speed over the window."""
    if db is None:
        return {}
    since = now - hours * 3600
    stats = {}
    for pid, n, i, o, c in db.execute(
            "SELECT provider, COUNT(*), SUM(input), SUM(output), SUM(cached) "
            "FROM requests WHERE ts >= ? AND ts <= ? GROUP BY 1", (since, now)):
        stats[pid] = {"requests": n, "in_tok": i or 0, "out_tok": o or 0,
                      "cached_tok": c or 0, "tok_h": (o or 0) / hours, "tps": None}
    for pid, tok, ms in db.execute(
            f"SELECT provider, SUM(output), SUM({GEN_MS}) FROM requests "
            f"WHERE ts >= ? AND ts <= ? AND {SPEED_WHERE} GROUP BY 1", (since, now)):
        if ms and pid in stats:
            stats[pid]["tps"] = tok / (ms / 1000)
    return stats


def fmt_tokens(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{n:.0f}"


# --- collector ----------------------------------------------------------------

def pop_queue(key, count=BATCH):
    req = urllib.request.Request(
        f"{URL}/v8/management/observability/usage/queue?count={count}",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} tokenflow: {msg}", flush=True)


def collect():
    key = os.environ.get("CLIPROXY_MGMT_KEY")
    if not key:
        log("CLIPROXY_MGMT_KEY is not set")
        sys.exit(1)
    db = connect()
    # The queue is pop-on-read: a second collector would steal half the records.
    # Hold an exclusive lock for the process lifetime; a duplicate waits here.
    lock = open(DB_PATH.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another collector is running; standing by")
        fcntl.flock(lock, fcntl.LOCK_EX)
    last_prune, failures = 0, 0
    log(f"draining {URL} every {POLL_S}s into {DB_PATH}")
    while True:
        try:
            while True:  # keep popping while batches come back full
                records = pop_queue(key)
                n = store(db, records)
                if n:
                    log(f"+{n} records")
                if len(records) < BATCH:
                    break
            failures = 0
            delay = POLL_S
        except urllib.error.HTTPError as e:
            failures += 1
            # Never retry a rejected key quickly: repeated 401s trigger a ban.
            delay = BACKOFF_MAX_S if e.code in (401, 403) else min(POLL_S * 2 ** failures, BACKOFF_MAX_S)
            log(f"HTTP {e.code}; retry in {delay}s")
        except Exception as e:
            failures += 1
            delay = min(POLL_S * 2 ** failures, BACKOFF_MAX_S)
            log(f"{type(e).__name__}: {e}; retry in {delay}s")
        if time.time() - last_prune > 86400:
            db.execute("DELETE FROM requests WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
            db.commit()
            last_prune = time.time()
        time.sleep(delay)


# --- CLI ----------------------------------------------------------------------

def print_stats(hours):
    now = time.time()
    stats = provider_stats(connect(readonly=True), now, hours)
    if not stats:
        print(f"no token data in the last {hours:g}h (is `tokenflow.py collect` running?)")
        return
    print(f"token flow — last {hours:g}h")
    print(f"  {'provider':<12}{'reqs':>6}{'out':>9}{'in':>9}{'cached':>9}{'tok/s':>8}")
    for pid, s in sorted(stats.items(), key=lambda kv: -kv[1]["out_tok"]):
        tps = f"{s['tps']:.0f}" if s["tps"] else "-"
        print(f"  {pid:<12}{s['requests']:>6}{fmt_tokens(s['out_tok']):>9}"
              f"{fmt_tokens(s['in_tok']):>9}{fmt_tokens(s['cached_tok']):>9}{tps:>8}")


def selftest():
    rec = {"timestamp": "2026-10-30T03:09:42.78756684+01:00", "latency_ms": 2602,
           "ttft_ms": 1099, "stream": True, "failed": False, "client_ip": "192.0.2.10",
           "tokens": {"input_tokens": 19, "output_tokens": 115, "reasoning_tokens": 25,
                      "cached_tokens": 0, "total_tokens": 134},
           "provider": "openai-compatible-zai", "model": "glm-5.3-flash",
           "execution_id": "e1", "api_key": "SENTINEL-not-a-real-key"}
    row = to_row(rec)
    assert row[0] == "e1" and row[2] == "zai" and row[5] == 115, row
    assert "SENTINEL-not-a-real-key" not in repr(row)
    assert panel_id("claude") == "anthropic" and panel_id("codex") == "codex"
    assert to_row({"nope": 1}) is None
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    now = row[1] + 60
    nonstream = dict(rec, execution_id="e2", stream=False)
    tiny = dict(rec, execution_id="e3", tokens=dict(rec["tokens"], output_tokens=3))
    assert store(db, [rec, rec, nonstream, tiny]) == 4  # dup id ignored by the PK
    s = provider_stats(db, now)["zai"]
    assert s["requests"] == 3 and s["out_tok"] == 115 * 2 + 3, s
    assert abs(s["tps"] - 115 / 1.503) < 0.5, s  # only e1 qualifies for speed
    assert speed_series(db, 1, now, buckets=4)["zai"] == [None, None, None, 76.5]
    assert sum(rate_series(db, 1, now, buckets=4)["zai"]) > 0
    assert first_ts(db) == row[1] and first_ts(None) is None
    assert fmt_tokens(1234) == "1.2k" and fmt_tokens(5_600_000) == "5.6M"
    print("tokenflow selftest ok")


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["collect"]:
        collect()
    elif argv[:1] == ["stats"]:
        print_stats(float(argv[1]) if len(argv) > 1 else 1)
    elif "--selftest" in argv:
        selftest()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
