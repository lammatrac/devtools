#!/usr/bin/env python3
"""Tests for usage_guard.py / statusline_usage.py. Run: python3 test_usage_guard.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import statusline_usage  # noqa: E402
import usage_guard as ug  # noqa: E402

NOW = 1_790_000_000.0
H = 3600


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def entry(ts, msg_id, out=100, inp=1000, model="claude-opus-5", req="req_1"):
    return json.dumps({
        "type": "assistant", "timestamp": iso(ts), "requestId": req,
        "message": {"id": msg_id, "model": model,
                    "usage": {"input_tokens": inp, "output_tokens": out,
                              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
    })


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.claude = os.path.join(self.home, ".claude")
        self.projects = os.path.join(self.claude, "projects", "proj")
        os.makedirs(os.path.join(self.projects, "sess", "subagents"))

    def tearDown(self):
        self.tmp.cleanup()

    def write_jsonl(self, lines, name="s.jsonl"):
        path = os.path.join(self.projects, name)
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def weight(self, out=100, inp=1000):
        return inp * ug.WEIGHTS["input"] + out * ug.WEIGHTS["output"]


class TestJsonl(Fixture):
    def test_dedupe_keeps_largest_output(self):
        self.write_jsonl([entry(NOW - 60, "m1", out=16), entry(NOW - 59, "m1", out=325),
                          entry(NOW - 30, "m2", out=10)])
        est = ug.estimate_5h(NOW, projects_dir=os.path.dirname(self.projects))
        self.assertAlmostEqual(est["used"], self.weight(325) + self.weight(10))

    def test_subagent_files_scanned_and_deduped_globally(self):
        self.write_jsonl([entry(NOW - 60, "m1")])
        self.write_jsonl([entry(NOW - 60, "m1"), entry(NOW - 50, "m3")], name="sess/subagents/agent-x.jsonl")
        est = ug.estimate_5h(NOW, projects_dir=os.path.dirname(self.projects))
        self.assertAlmostEqual(est["used"], 2 * self.weight())

    def test_malformed_lines_ignored(self):
        self.write_jsonl([
            "not json at all",
            '{"type": "assistant", "usage": ',                           # truncated
            json.dumps({"type": "assistant", "message": {"usage": {}}}),  # no timestamp
            json.dumps({"type": "assistant", "timestamp": "garbage", "message": {"id": "x", "usage": {}}}),
            json.dumps({"type": "user", "message": {"usage": {"input_tokens": 9}}}),
            entry(NOW - 10, "ok"),
        ])
        est = ug.estimate_5h(NOW, projects_dir=os.path.dirname(self.projects))
        self.assertAlmostEqual(est["used"], self.weight())

    def test_window_rollover(self):
        # t0 opens a window; t0+1h is in it; t0+6h opens a new one.
        t0 = NOW - 7 * H
        self.write_jsonl([entry(t0, "a"), entry(t0 + H, "b"), entry(t0 + 6 * H, "c", out=7)])
        est = ug.estimate_5h(NOW, projects_dir=os.path.dirname(self.projects))
        self.assertEqual(est["start"], t0 + 6 * H)
        self.assertEqual(est["resets_at"], t0 + 11 * H)
        self.assertAlmostEqual(est["used"], self.weight(7))

    def test_no_active_window(self):
        self.write_jsonl([entry(NOW - 6 * H, "old")])
        est = ug.estimate_5h(NOW, projects_dir=os.path.dirname(self.projects))
        self.assertEqual(est["used"], 0.0)
        self.assertIsNone(est["resets_at"])

    def test_anchor_overrides_window_start(self):
        self.write_jsonl([entry(NOW - 4 * H, "a"), entry(NOW - H, "b")])
        est = ug.estimate_5h(NOW, anchor=NOW - 2 * H, projects_dir=os.path.dirname(self.projects))
        self.assertAlmostEqual(est["used"], self.weight())

    def test_model_multiplier(self):
        u = {"input_tokens": 1000, "output_tokens": 0}
        self.assertAlmostEqual(ug.weighted_tokens(u, "claude-sonnet-5"), 1000 * ug.MODEL_MULT["sonnet"])


class TestEvaluate(unittest.TestCase):
    def srv(self, pct, fresh=True, resets=NOW + H):
        return {"pct": pct, "resets_at": resets, "fresh": fresh}

    def triggered(self, checks):
        return [c["name"] for c in checks if c["pct"] >= c["stop_at"]]

    def test_fresh_5h_triggers(self):
        checks, notes = ug.evaluate(NOW, server={"five_hour": self.srv(0.91)})
        self.assertEqual(self.triggered(checks), ["5-hour"])
        self.assertIn("no weekly data available; guarding 5h only", notes)

    def test_weekly_triggers_independently(self):
        checks, _ = ug.evaluate(NOW, server={"five_hour": self.srv(0.2), "seven_day": self.srv(0.95)})
        self.assertEqual(self.triggered(checks), ["weekly"])

    def test_below_threshold(self):
        checks, _ = ug.evaluate(NOW, server={"five_hour": self.srv(0.89), "seven_day": self.srv(0.5)})
        self.assertEqual(self.triggered(checks), [])

    def test_stale_falls_back_to_estimate(self):
        est = {"used": ug.TOKEN_LIMIT * 0.95, "start": NOW - H, "resets_at": NOW + 4 * H}
        checks, _ = ug.evaluate(NOW, server={"five_hour": self.srv(0.10, fresh=False)}, estimate=est)
        self.assertEqual(checks[0]["source"], "estimate")
        self.assertEqual(self.triggered(checks), ["5-hour"])

    def test_stale_server_used_as_lower_bound(self):
        est = {"used": 0.0, "start": None, "resets_at": None}
        checks, _ = ug.evaluate(NOW, server={"five_hour": self.srv(0.92, fresh=False)}, estimate=est)
        self.assertEqual(checks[0]["source"], "server (stale, lower bound)")
        self.assertEqual(self.triggered(checks), ["5-hour"])


class TestEndToEnd(Fixture):
    def run_hook(self, *args):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run([sys.executable, os.path.join(HERE, "usage_guard.py"), *args],
                              env=env, capture_output=True, text=True, timeout=10)

    def write_cache(self, rate_limits, written_at=None):
        statusline_usage.write_cache(rate_limits, path=os.path.join(self.claude, "usage_cache.json"),
                                     now=written_at if written_at is not None else time.time())

    def test_halt_json_when_over(self):
        self.write_cache({"five_hour": {"used_percentage": 93, "resets_at": time.time() + H},
                          "seven_day": {"used_percentage": 40, "resets_at": time.time() + 48 * H}})
        r = self.run_hook()
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertIs(out["continue"], False)
        self.assertIn("5-hour limit at 93%", out["stopReason"])
        self.assertIn("Resets", out["stopReason"])

    def test_silent_when_under(self):
        self.write_cache({"five_hour": {"used_percentage": 10, "resets_at": time.time() + H}})
        r = self.run_hook()
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_expired_window_ignored(self):
        self.write_cache({"five_hour": {"used_percentage": 99, "resets_at": time.time() - 1}})
        r = self.run_hook()
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_bypass_file(self):
        self.write_cache({"five_hour": {"used_percentage": 99, "resets_at": time.time() + H}})
        open(os.path.join(self.claude, "usage_guard.off"), "w").close()
        self.assertEqual(self.run_hook().stdout, "")

    def test_corrupt_cache_never_crashes(self):
        with open(os.path.join(self.claude, "usage_cache.json"), "w") as f:
            f.write('{"written_at": "nope", "rate_limits": [1, 2')
        r = self.run_hook()
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_missing_claude_dir_never_crashes(self):
        env = dict(os.environ, HOME=os.path.join(self.home, "nonexistent"))
        r = subprocess.run([sys.executable, os.path.join(HERE, "usage_guard.py")],
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_statusline_writes_cache_and_prints(self):
        payload = {"model": {"display_name": "Opus"},
                   "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": time.time() + H}}}
        env = dict(os.environ, HOME=self.home)
        r = subprocess.run([sys.executable, os.path.join(HERE, "statusline_usage.py")], input=json.dumps(payload),
                           env=env, capture_output=True, text=True, timeout=10)
        self.assertIn("5h 42%", r.stdout)
        with open(os.path.join(self.claude, "usage_cache.json")) as f:
            self.assertEqual(json.load(f)["rate_limits"]["five_hour"]["used_percentage"], 42)
        self.assertEqual([n for n in os.listdir(self.claude) if n.startswith(".usage_cache.")], [])


if __name__ == "__main__":
    unittest.main()
