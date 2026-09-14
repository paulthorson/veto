#!/usr/bin/env python3
"""Tests for notify.py notification controls (Initiative 03).

Covers: prefs load/save/set, quiet-hours windows (incl. cross-midnight),
per-event opt-in, digest queue + flush, channel allowlist, and the
backward-compatible ``send()`` contract.

Stdlib unittest only. No network; prefs/digest/log files redirected to a
temp dir; time is injected, never real.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import notify


def _local(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute,
                    tzinfo=ZoneInfo("America/New_York"))


class _IsoMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmpdir = Path(self.tmp.name)
        self.prefs_path = tmpdir / "prefs.json"
        self.digest_path = tmpdir / "digest.json"
        self.log_path = tmpdir / "notifications.jsonl"
        for attr, path in (
            ("NOTIFY_PREFS", self.prefs_path),
            ("DIGEST_FILE", self.digest_path),
            ("NOTIFY_LOG", self.log_path),
        ):
            patch = mock.patch.object(notify, attr, path)
            patch.start()
            self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("NTFY_TOPIC", None)
        os.environ.pop("JOB_MCP_WEBHOOK", None)


class TestPrefs(_IsoMixin, unittest.TestCase):
    def test_defaults(self):
        prefs = notify.load_prefs()
        # Remote sinks default OFF (§3.1).
        self.assertEqual(prefs["channels"], ["log"])
        self.assertIsNone(prefs["ntfy_topic"])
        self.assertIsNone(prefs["quiet_hours"])
        self.assertEqual(prefs["digest_cadence"], "immediate")
        self.assertTrue(all(prefs["events"].values()))

    def test_set_pref_roundtrip(self):
        prefs = notify.set_pref("quiet_hours", "22:00-07:00")
        self.assertEqual(prefs["quiet_hours"],
                         {"start": "22:00", "end": "07:00"})
        self.assertEqual(notify.load_prefs()["quiet_hours"],
                         {"start": "22:00", "end": "07:00"})

    def test_set_pref_channels(self):
        prefs = notify.set_pref("channels", "log,ntfy")
        self.assertEqual(prefs["channels"], ["log", "ntfy"])
        with self.assertRaises(ValueError):
            notify.set_pref("channels", "smoke-signals")

    def test_set_pref_bad_quiet_hours(self):
        with self.assertRaises(ValueError):
            notify.set_pref("quiet_hours", "25:00-07:00")
        with self.assertRaises(ValueError):
            notify.set_pref("quiet_hours", "whenever")

    def test_set_pref_event_opt_out(self):
        prefs = notify.set_pref("event:reply_received", "off")
        self.assertFalse(prefs["events"]["reply_received"])
        self.assertTrue(prefs["events"]["brief_ready"])
        with self.assertRaises(ValueError):
            notify.set_pref("event:nope", "off")

    def test_set_pref_cadence(self):
        self.assertEqual(
            notify.set_pref("digest_cadence", "daily")["digest_cadence"], "daily"
        )
        with self.assertRaises(ValueError):
            notify.set_pref("digest_cadence", "hourly")

    def test_corrupt_prefs_fall_back_to_defaults(self):
        self.prefs_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(notify.load_prefs(), notify.default_prefs())


class TestQuietHours(_IsoMixin, unittest.TestCase):
    def _prefs(self, window):
        prefs = notify.default_prefs()
        prefs["quiet_hours"] = window
        return prefs

    def test_inside_window(self):
        prefs = self._prefs({"start": "22:00", "end": "07:00"})
        self.assertTrue(notify.in_quiet_hours(_local(2026, 9, 13, 23, 30), prefs))
        self.assertTrue(notify.in_quiet_hours(_local(2026, 9, 14, 6, 59), prefs))

    def test_outside_window(self):
        prefs = self._prefs({"start": "22:00", "end": "07:00"})
        self.assertFalse(notify.in_quiet_hours(_local(2026, 9, 13, 12, 0), prefs))
        self.assertFalse(notify.in_quiet_hours(_local(2026, 9, 14, 7, 0), prefs))

    def test_non_midnight_crossing_window(self):
        prefs = self._prefs({"start": "12:00", "end": "13:00"})
        self.assertTrue(notify.in_quiet_hours(_local(2026, 9, 13, 12, 30), prefs))
        self.assertFalse(notify.in_quiet_hours(_local(2026, 9, 13, 14, 0), prefs))

    def test_disabled(self):
        prefs = notify.default_prefs()
        self.assertFalse(notify.in_quiet_hours(_local(2026, 9, 13, 23, 30), prefs))


class TestSendControls(_IsoMixin, unittest.TestCase):
    def _log_entries(self):
        if not self.log_path.exists():
            return []
        return [json.loads(l) for l in
                self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_event_opt_out_logs_only(self):
        notify.set_pref("event:reply_received", "off")
        result = notify.send("Reply!", "body", event="reply_received")
        self.assertEqual(result["delivered_via"], ["log"])
        self.assertEqual(result["suppressed"], "event_opted_out")
        self.assertEqual(len(self._log_entries()), 1)

    def test_unknown_event_disabled(self):
        # Unknown events are disabled (fail-closed, §3.2): logged only.
        self.assertFalse(notify.event_enabled("not-a-real-event"))
        self.assertFalse(notify.event_enabled(None))
        result = notify.send("Hi", "body", event="not-a-real-event")
        self.assertEqual(result["delivered_via"], ["log"])
        self.assertEqual(result["suppressed"], "event_opted_out")

    def test_quiet_hours_queues(self):
        notify.set_pref("quiet_hours", "22:00-07:00")
        night = _local(2026, 9, 13, 23, 0)
        result = notify.send("Late", "news", now=night)
        self.assertTrue(result.get("queued"))
        self.assertEqual(result["digest_depth"], 1)
        self.assertEqual(result["delivered_via"], ["log"])
        # Local log still records immediately for auditability.
        self.assertEqual(len(self._log_entries()), 1)

    def test_daily_cadence_queues(self):
        notify.set_pref("digest_cadence", "daily")
        day = _local(2026, 9, 13, 12, 0)
        result = notify.send("Noon", "news", now=day)
        self.assertTrue(result.get("queued"))

    def test_flush_digest_delivers(self):
        notify.set_pref("quiet_hours", "22:00-07:00")
        night = _local(2026, 9, 13, 23, 0)
        notify.send("One", "first", now=night)
        notify.send("Two", "second", now=night)
        morning = _local(2026, 9, 14, 8, 0)
        result = notify.flush_digest(now=morning)
        self.assertEqual(result["items"], 2)
        self.assertEqual(len(notify._load_digest()), 0)

    def test_flush_digest_writes_local_log(self):
        # The always-log contract holds for digest delivery too.
        notify.set_pref("quiet_hours", "22:00-07:00")
        night = _local(2026, 9, 13, 23, 0)
        notify.send("One", "first", now=night)
        morning = _local(2026, 9, 14, 8, 0)
        result = notify.flush_digest(now=morning)
        self.assertIn("log", result["delivered_via"])
        entries = self._log_entries()
        self.assertTrue(any(e["title"].startswith("Veto digest") for e in entries))

    def test_flush_digest_opted_out_logs_and_clears(self):
        notify.set_pref("event:digest", "off")
        notify.send("One", "first")
        notify.set_pref("digest_cadence", "daily")
        notify.send("Two", "second")
        result = notify.flush_digest(force=True)
        self.assertEqual(result["suppressed"], "event_opted_out")
        self.assertEqual(len(notify._load_digest()), 0)

    def test_flush_during_quiet_hours_defers_unless_forced(self):
        notify.set_pref("quiet_hours", "22:00-07:00")
        night = _local(2026, 9, 13, 23, 0)
        notify.send("One", "first", now=night)
        deferred = notify.flush_digest(now=night)
        self.assertEqual(deferred.get("deferred"), "quiet_hours")
        self.assertEqual(len(notify._load_digest()), 1)
        forced = notify.flush_digest(now=night, force=True)
        self.assertEqual(forced["items"], 1)
        self.assertEqual(len(notify._load_digest()), 0)

    def test_channel_disabled_suppresses_push_but_logs(self):
        notify.set_pref("channels", "log")
        result = notify.send("Hi", "body", channel="ntfy")
        self.assertEqual(result["suppressed"], "channel_disabled")
        self.assertEqual(len(self._log_entries()), 1)

    def test_backward_compat_no_prefs_file(self):
        # No prefs file: behaves exactly like the old send().
        result = notify.send("Hi", "body text")
        self.assertEqual(result, {"ok": True, "delivered_via": ["log"]})


class TestCliRegistration(unittest.TestCase):
    def test_register_cli(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = notify.register_cli(sub)
        self.assertIn("notify", handlers)
        args = parser.parse_args(["notify", "prefs", "--json"])
        self.assertEqual(args.func.__name__, "_cli_prefs")

    def test_setup_ntfy_cli_rejects_user_topic(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        notify.register_cli(sub)
        args = parser.parse_args(["notify", "setup-ntfy", "--topic",
                                  "myjobsearch"])
        self.assertEqual(args.func(args), 2)

    def test_setup_ntfy_cli_prints_privacy_warning(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        notify.register_cli(sub)
        args = parser.parse_args(["notify", "setup-ntfy"])
        fake = {"ok": True, "topic": "t" * 43,
                "warning": "Privacy warning: topics are PUBLIC"}
        buf = io.StringIO()
        with mock.patch.object(notify, "setup_ntfy", return_value=fake), \
                redirect_stdout(buf):
            rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertIn("PUBLIC", buf.getvalue())
        self.assertIn("t" * 43, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
