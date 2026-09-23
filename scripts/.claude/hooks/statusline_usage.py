#!/usr/bin/env python3
"""Status line that also caches server-side rate-limit data for usage_guard.py.

Claude Code passes `rate_limits.five_hour` / `rate_limits.seven_day`
({used_percentage, resets_at}) to the status line command on stdin (Pro/Max,
after the first API response). Hooks do not receive this data, so we persist
it to CACHE_FILE atomically (temp file + rename) for the guard to read.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime

CACHE_FILE = os.path.expanduser("~/.claude/usage_cache.json")


def write_cache(rate_limits, path=CACHE_FILE, now=None):
    payload = {"written_at": now if now is not None else time.time(), "rate_limits": rate_limits}
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".usage_cache.", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fmt_window(label, window):
    pct = window.get("used_percentage")
    if pct is None:
        return None
    text = f"{label} {pct:.0f}%"
    resets_at = window.get("resets_at")
    if resets_at:
        text += " ↻" + datetime.fromtimestamp(resets_at).strftime("%a %H:%M" if label == "7d" else "%H:%M")
    return text


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(f"[{model}]")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")

    rate_limits = data.get("rate_limits") or {}
    windows = {k: v for k, v in rate_limits.items() if k in ("five_hour", "seven_day") and isinstance(v, dict)}
    if windows:
        try:
            write_cache(windows)
        except Exception:
            pass
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            if key in windows:
                text = fmt_window(label, windows[key])
                if text:
                    parts.append(text)
    print(" · ".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
