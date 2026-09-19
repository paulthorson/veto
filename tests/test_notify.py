#!/usr/bin/env python3
"""Tests for the notification module (notify.py).

Stdlib unittest only. No network (urllib is mocked), no real env
dependence (env vars monkeypatched), log file redirected to a temp dir.

Run:  cd ~/workspace/veto && python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import notify


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "notifications.jsonl"
        self.addCleanup(self.tmp.cleanup)
        self.log_patch = mock.patch.object(notify, "NOTIFY_LOG", self.log)
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        # Isolate prefs too: no prefs file in tmp -> factory defaults.
        self.prefs_patch = mock.patch.object(
            notify, "NOTIFY_PREFS", Path(self.tmp.name) / "prefs.json"
        )
        self.prefs_patch.start()
        self.addCleanup(self.prefs_patch.stop)
        # Start with no sinks configured.
        self.env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        os.environ.pop("NTFY_TOPIC", None)
        os.environ.pop("JOB_MCP_WEBHOOK", None)

    def _log_entries(self):
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def _enabled_prefs(self, *channels):
        """Prefs with the given remote channels explicitly enabled."""
        prefs = notify.default_prefs()
        prefs["channels"] = ["log", *channels]
        return prefs

    def test_always_writes_local_log(self):
        result = notify.send("Hi", "body text")
        self.assertEqual(result, {"ok": True, "delivered_via": ["log"]})
        entries = self._log_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Hi")
        self.assertEqual(entries[0]["body"], "body text")
        self.assertIn("ts", entries[0])

    # --- default-off (§3.1) ------------------------------------------------

    def test_default_prefs_have_no_remote_channels(self):
        prefs = notify.default_prefs()
        self.assertEqual(prefs["channels"], ["log"])
        self.assertIsNone(prefs["ntfy_topic"])

    def test_no_post_without_configuration(self):
        # Even with env vars set, default prefs mean no remote POST happens.
        os.environ["NTFY_TOPIC"] = "my-topic"
        os.environ["JOB_MCP_WEBHOOK"] = "https://example.com/hook"

        def boom(req, timeout=None):
            raise AssertionError("must not POST anywhere")

        with mock.patch("urllib.request.urlopen", boom):
            result = notify.send("Hi", "body text")
        self.assertEqual(result, {"ok": True, "delivered_via": ["log"]})

    # --- ntfy delivery (explicit opt-in + generated topic) -----------------

    def test_ntfy_delivery(self):
        prefs = self._enabled_prefs("ntfy")
        prefs["ntfy_topic"] = notify.generate_ntfy_topic()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["title"] = req.headers.get("Title")
            captured["timeout"] = timeout
            return _FakeResponse(200)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = notify.send("T", "B", prefs=prefs)
        self.assertEqual(result["delivered_via"], ["log", "ntfy"])
        self.assertEqual(captured["url"], f"https://ntfy.sh/{prefs['ntfy_topic']}")
        self.assertEqual(captured["title"], "T")
        self.assertEqual(captured["timeout"], 5)

    def test_ntfy_delivery_via_setup(self):
        # setup_ntfy() stores a generated topic; delivery uses it.
        result = notify.setup_ntfy()
        topic = result["topic"]
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResponse(200)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            sent = notify.send("T", "B")
        self.assertEqual(sent["delivered_via"], ["log", "ntfy"])
        self.assertEqual(captured["url"], f"https://ntfy.sh/{topic}")

    def test_high_entropy_env_topic_honored(self):
        # Migration path: a high-entropy NTFY_TOPIC still works.
        topic = notify.generate_ntfy_topic()
        os.environ["NTFY_TOPIC"] = topic
        prefs = self._enabled_prefs("ntfy")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _FakeResponse(200)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = notify.send("T", "B", prefs=prefs)
        self.assertEqual(result["delivered_via"], ["log", "ntfy"])
        self.assertEqual(captured["url"], f"https://ntfy.sh/{topic}")

    def test_guessable_env_topic_rejected(self):
        os.environ["NTFY_TOPIC"] = "my-topic"
        prefs = self._enabled_prefs("ntfy")

        def boom(req, timeout=None):
            raise AssertionError("must not POST a guessable topic")

        with mock.patch("urllib.request.urlopen", boom):
            result = notify.send("T", "B", prefs=prefs)
        self.assertEqual(result["delivered_via"], ["log"])
        self.assertTrue(result.get("sink_warnings"))
        self.assertIn("guessable", result["sink_warnings"][0])

    def test_webhook_delivery(self):
        os.environ["JOB_MCP_WEBHOOK"] = "https://example.com/hook"
        prefs = self._enabled_prefs("webhook")
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(200)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            result = notify.send("T", "B", prefs=prefs)
        self.assertEqual(result["delivered_via"], ["log", "webhook"])
        self.assertEqual(captured["url"], "https://example.com/hook")
        self.assertEqual(captured["payload"]["title"], "T")
        self.assertEqual(captured["payload"]["body"], "B")

    def test_channel_selection(self):
        topic = notify.generate_ntfy_topic()
        os.environ["NTFY_TOPIC"] = topic
        os.environ["JOB_MCP_WEBHOOK"] = "https://example.com/hook"
        prefs = self._enabled_prefs("ntfy", "webhook")
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(200)
        ):
            ntfy_only = notify.send("T", "B", channel="ntfy", prefs=prefs)
            webhook_only = notify.send("T", "B", channel="webhook", prefs=prefs)
        self.assertEqual(ntfy_only["delivered_via"], ["log", "ntfy"])
        self.assertEqual(webhook_only["delivered_via"], ["log", "webhook"])

    def test_sink_failure_never_raises(self):
        os.environ["NTFY_TOPIC"] = notify.generate_ntfy_topic()
        os.environ["JOB_MCP_WEBHOOK"] = "https://example.com/hook"
        prefs = self._enabled_prefs("ntfy", "webhook")

        def boom(req, timeout=None):
            raise OSError("network down")

        with mock.patch("urllib.request.urlopen", boom):
            result = notify.send("T", "B", prefs=prefs)  # must not raise
        self.assertEqual(result, {"ok": True, "delivered_via": ["log"]})

    def test_non_2xx_is_not_counted(self):
        os.environ["NTFY_TOPIC"] = notify.generate_ntfy_topic()
        prefs = self._enabled_prefs("ntfy")
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(500)
        ):
            result = notify.send("T", "B", prefs=prefs)
        self.assertEqual(result["delivered_via"], ["log"])

    # --- setup-ntfy (§3.3, §3.4) --------------------------------------------

    def test_setup_ntfy_generates_high_entropy_topic(self):
        import re

        result = notify.setup_ntfy()
        topic = result["topic"]
        self.assertRegex(topic, r"^[A-Za-z0-9\-_]{43}$")
        # Every setup generates a fresh topic.
        self.assertNotEqual(topic, notify.setup_ntfy()["topic"])

    def test_setup_ntfy_rejects_user_supplied_topic(self):
        with self.assertRaises(ValueError):
            notify.setup_ntfy(topic="myjobsearch")

    def test_setup_ntfy_enables_channel_and_stores_topic(self):
        result = notify.setup_ntfy()
        prefs = notify.load_prefs()
        self.assertIn("ntfy", prefs["channels"])
        self.assertEqual(prefs["ntfy_topic"], result["topic"])

    def test_setup_ntfy_warns_about_public_topics(self):
        result = notify.setup_ntfy()
        warning = result["warning"].lower()
        self.assertIn("public", warning)
        self.assertIn("anyone", warning)
        self.assertIn("no account", warning)


if __name__ == "__main__":
    unittest.main()
