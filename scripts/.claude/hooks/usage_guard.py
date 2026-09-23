#!/usr/bin/env python3
"""Stop Claude Code before the 5-hour / weekly usage limit is reached.

Registered for UserPromptSubmit and PreToolUse. Data sources, in order:
  1. ~/.claude/usage_cache.json, written by statusline_usage.py from the
     server-side `rate_limits` Claude Code gives the status line.
  2. Fallback (5h only): weighted tokens from ~/.claude/projects/**/*.jsonl.
A stale-but-unexpired server percentage is still used as a lower bound,
because usage in a window only grows until its resets_at.

Usage:
  usage_guard.py            hook mode: prints halt JSON when over threshold
  usage_guard.py --report   human-readable status
Bypass for 1h: touch ~/.claude/usage_guard.off
"""
import glob
import json
import os
import sys
import tempfile
import time
from datetime import datetime

# ---- Config ---------------------------------------------------------------
STOP_AT_5H = 0.90
STOP_AT_WEEKLY = 0.90

# Fallback estimate only. Weighted tokens per 5h window; calibrate with --report.
TOKEN_LIMIT = 30_000_000
# Relative quota cost per token type (API price ratios; unverified proxy).
WEIGHTS = {
    "input": 1.0,
    "output": 5.0,
    "cache_write_5m": 1.25,
    "cache_write_1h": 2.0,
    "cache_read": 0.1,
}
# Per-model multiplier, matched by substring of message.model.
MODEL_MULT = {"opus": 1.0, "sonnet": 0.6, "haiku": 0.2}
DEFAULT_MODEL_MULT = 1.0

WINDOW_SECONDS = 5 * 3600
CACHE_MAX_AGE = 300       # server cache older than this is stale
RESULT_TTL = 30           # reuse the JSONL estimate for this long
LOOKBACK = 24 * 3600      # only scan JSONL files modified within this period
BYPASS_SECONDS = 3600

CLAUDE_DIR = os.path.expanduser("~/.claude")
CACHE_FILE = os.path.join(CLAUDE_DIR, "usage_cache.json")
STATE_FILE = os.path.join(CLAUDE_DIR, "usage_guard_state.json")
BYPASS_FILE = os.path.join(CLAUDE_DIR, "usage_guard.off")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")


# ---- Server-side data -----------------------------------------------------
def load_server(now, path=None):
    """Return {window: {pct, resets_at, fresh}} for unexpired windows."""
    try:
        with open(path or CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    fresh = now - float(data.get("written_at", 0)) <= CACHE_MAX_AGE
    result = {}
    for key, window in (data.get("rate_limits") or {}).items():
        try:
            pct = float(window["used_percentage"]) / 100
            resets_at = float(window["resets_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if resets_at > now:
            result[key] = {"pct": pct, "resets_at": resets_at, "fresh": fresh}
    return result


# ---- JSONL fallback -------------------------------------------------------
def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def weighted_tokens(usage, model):
    cache = usage.get("cache_creation")
    if isinstance(cache, dict):
        write_5m = cache.get("ephemeral_5m_input_tokens") or 0
        write_1h = cache.get("ephemeral_1h_input_tokens") or 0
    else:
        write_5m, write_1h = usage.get("cache_creation_input_tokens") or 0, 0
    raw = (
        WEIGHTS["input"] * (usage.get("input_tokens") or 0)
        + WEIGHTS["output"] * (usage.get("output_tokens") or 0)
        + WEIGHTS["cache_write_5m"] * write_5m
        + WEIGHTS["cache_write_1h"] * write_1h
        + WEIGHTS["cache_read"] * (usage.get("cache_read_input_tokens") or 0)
    )
    model = (model or "").lower()
    mult = next((m for name, m in MODEL_MULT.items() if name in model), DEFAULT_MODEL_MULT)
    return raw * mult


def load_entries(now, projects_dir=None):
    """Deduped assistant entries: [(timestamp, weighted_tokens)].

    Streaming writes several entries per (message.id, requestId) with growing
    output_tokens, so keep the one with the largest output_tokens.
    """
    cutoff = now - LOOKBACK
    best = {}
    pattern = os.path.join(projects_dir or PROJECTS_DIR, "**", "*.jsonl")
    for path in glob.iglob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") != "assistant":
                        continue
                    msg = d["message"]
                    usage = msg["usage"]
                    ts = parse_ts(d["timestamp"])
                    key = (msg.get("id"), d.get("requestId"))
                    out = usage.get("output_tokens") or 0
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if ts < cutoff or not key[0]:
                    continue
                prev = best.get(key)
                if prev is None or out > prev[1]:
                    best[key] = (min(ts, prev[0]) if prev else ts, out, weighted_tokens(usage, msg.get("model")))
    return [(ts, w) for ts, _, w in best.values()]


def current_window_start(timestamps, now, anchor=None):
    """Window starts at the first message after the previous window ended.

    `anchor` (a known server window start) takes precedence while active.
    """
    if anchor is not None and anchor <= now < anchor + WINDOW_SECONDS:
        return anchor
    start = None
    for ts in sorted(timestamps):
        if ts > now:
            break
        if start is None or ts >= start + WINDOW_SECONDS:
            start = ts
    if start is None or now >= start + WINDOW_SECONDS:
        return None
    return start


def estimate_5h(now, anchor=None, projects_dir=None):
    entries = load_entries(now, projects_dir)
    start = current_window_start([ts for ts, _ in entries], now, anchor)
    if start is None:
        return {"used": 0.0, "start": None, "resets_at": None}
    used = sum(w for ts, w in entries if start <= ts <= now)
    return {"used": used, "start": start, "resets_at": start + WINDOW_SECONDS}


def cached_estimate(now, anchor=None):
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        if now - state["computed_at"] < RESULT_TTL and state.get("anchor") == anchor:
            return state["estimate"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    est = estimate_5h(now, anchor)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".usage_guard_state.", dir=os.path.dirname(STATE_FILE))
        with os.fdopen(fd, "w") as f:
            json.dump({"computed_at": now, "anchor": anchor, "estimate": est}, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass
    return est


# ---- Evaluation -----------------------------------------------------------
def evaluate(now, server=None, estimate=None):
    """Return (checks, notes). Each check: name, pct, resets_at, stop_at, source."""
    server = load_server(now) if server is None else server
    checks, notes = [], []

    five = server.get("five_hour")
    if five and five["fresh"]:
        checks.append({"name": "5-hour", "pct": five["pct"], "resets_at": five["resets_at"],
                       "stop_at": STOP_AT_5H, "source": "server"})
    else:
        anchor = five["resets_at"] - WINDOW_SECONDS if five else None
        est = estimate if estimate is not None else cached_estimate(now, anchor)
        pct = est["used"] / TOKEN_LIMIT if TOKEN_LIMIT else 0.0
        source = "estimate"
        if five and five["pct"] > pct:
            pct, source = five["pct"], "server (stale, lower bound)"
        checks.append({"name": "5-hour", "pct": pct, "resets_at": est["resets_at"] or (five or {}).get("resets_at"),
                       "stop_at": STOP_AT_5H, "source": source, "used": est["used"]})
        notes.append("server 5h data missing or stale; using JSONL estimate")

    week = server.get("seven_day")
    if week:
        checks.append({"name": "weekly", "pct": week["pct"], "resets_at": week["resets_at"], "stop_at": STOP_AT_WEEKLY,
                       "source": "server" if week["fresh"] else "server (stale, lower bound)"})
    else:
        notes.append("no weekly data available; guarding 5h only")
    return checks, notes


def fmt_reset(resets_at, now):
    if not resets_at:
        return "unknown"
    mins = max(0, int((resets_at - now) // 60))
    return f"{datetime.fromtimestamp(resets_at).strftime('%a %H:%M')} (in {mins // 60}h{mins % 60:02d}m)"


def bypass_active(now):
    try:
        return now - os.path.getmtime(BYPASS_FILE) < BYPASS_SECONDS
    except OSError:
        return False


def hook(now):
    if bypass_active(now):
        return
    checks, _ = evaluate(now)
    for c in checks:
        if c["pct"] >= c["stop_at"]:
            reason = (f"Usage guard: {c['name']} limit at {c['pct']:.0%} "
                      f"(threshold {c['stop_at']:.0%}, source: {c['source']}). "
                      f"Resets {fmt_reset(c['resets_at'], now)}. "
                      f"Override for 1h: touch {BYPASS_FILE}")
            print(json.dumps({"continue": False, "stopReason": reason}))
            return


def report(now):
    server = load_server(now)
    checks, notes = evaluate(now, server=server)
    for c in checks:
        flag = "STOP" if c["pct"] >= c["stop_at"] else "ok"
        line = f"{c['name']:7} {c['pct']:6.1%} / {c['stop_at']:.0%}  [{flag}]  source={c['source']}  resets {fmt_reset(c['resets_at'], now)}"
        if "used" in c:
            line += f"  weighted={c['used']:,.0f} / TOKEN_LIMIT={TOKEN_LIMIT:,}"
        print(line)
    for n in notes:
        print(f"note: {n}")

    five = server.get("five_hour")
    if five and five["fresh"] and five["pct"] > 0:
        est = estimate_5h(now, anchor=five["resets_at"] - WINDOW_SECONDS)
        implied = est["used"] / five["pct"]
        print(f"calibration: weighted={est['used']:,.0f} at server {five['pct']:.1%} "
              f"-> implied TOKEN_LIMIT ≈ {implied:,.0f}")
    if bypass_active(now):
        print(f"BYPASS ACTIVE ({BYPASS_FILE})")


if __name__ == "__main__":
    if "--report" in sys.argv[1:]:
        report(time.time())
    else:
        try:
            hook(time.time())
        except BaseException:
            pass
        sys.exit(0)
