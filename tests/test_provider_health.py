#!/usr/bin/env python3
"""Tests for provider_health.py (Initiative 03: provider health board).

Stdlib unittest only. Store redirected to a temp dir; no network.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import provider_health


class _IsoMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "provider_health.json"
        patch = mock.patch.object(provider_health, "DEFAULT_STORE", self.store)
        patch.start()
        self.addCleanup(patch.stop)


class TestRecordFetch(_IsoMixin, unittest.TestCase):
    def test_success_marks_last_fetch_and_clears_failures(self):
        provider_health.record_fetch("greenhouse", False)
        provider_health.record_fetch("greenhouse", False)
        row = provider_health.record_fetch("greenhouse", True)["entry"]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertIsNotNone(row["last_successful_fetch"])
        self.assertEqual(row["recovery_streak_days"], 1)

    def test_failures_accumulate(self):
        for _ in range(3):
            provider_health.record_fetch("lever", False)
        row = provider_health.provider_status("lever")
        self.assertEqual(row["consecutive_failures"], 3)
        self.assertEqual(row["status"], "degraded")

    def test_ok_provider_status(self):
        provider_health.record_fetch("ashby", True)
        row = provider_health.provider_status("ashby")
        self.assertEqual(row["status"], "ok")

    def test_empty_provider_rejected(self):
        with self.assertRaises(ValueError):
            provider_health.record_fetch("", True)

    def test_recovery_streak_counts_once_per_day(self):
        provider_health.record_fetch("lever", False)
        provider_health.record_fetch("lever", True)
        provider_health.record_fetch("lever", False)
        provider_health.record_fetch("lever", True)
        row = provider_health.provider_status("lever")
        # Same calendar day: still one recovery day, not two.
        self.assertEqual(row["recovery_streak_days"], 1)


class TestCaptcha(_IsoMixin, unittest.TestCase):
    def test_challenged_sets_cooldown(self):
        provider_health.record_fetch("greenhouse", True)
        provider_health.record_captcha("greenhouse", "challenged",
                                       cooldown_seconds=600)
        row = provider_health.provider_status("greenhouse")
        self.assertEqual(row["captcha_state"], "challenged")
        self.assertEqual(row["status"], "cooling")
        self.assertGreater(row["cooldown_remaining_s"], 0)

    def test_blocked_status(self):
        provider_health.record_captcha("glassdoor", "blocked")
        row = provider_health.provider_status("glassdoor")
        self.assertEqual(row["status"], "blocked")
        allowed, reason = provider_health.fetch_allowed("glassdoor")
        self.assertFalse(allowed)
        self.assertEqual(reason, "captcha_blocked")

    def test_clear_resets(self):
        provider_health.record_captcha("greenhouse", "challenged")
        provider_health.record_captcha("greenhouse", "clear")
        row = provider_health.provider_status("greenhouse")
        self.assertEqual(row["captcha_state"], "clear")
        self.assertEqual(row["cooldown_remaining_s"], 0)

    def test_bad_state_rejected(self):
        with self.assertRaises(ValueError):
            provider_health.record_captcha("greenhouse", "melted")

    def test_clean_fetch_clears_transient_challenge(self):
        provider_health.record_captcha("greenhouse", "challenged",
                                       cooldown_seconds=0)
        provider_health.record_fetch("greenhouse", True)
        row = provider_health.provider_status("greenhouse")
        self.assertEqual(row["captcha_state"], "clear")


class TestBudget(_IsoMixin, unittest.TestCase):
    def test_budget_exhaustion_blocks_fetch(self):
        provider_health.set_budget("greenhouse", 2)
        provider_health.record_fetch("greenhouse", True)
        provider_health.record_fetch("greenhouse", True)
        row = provider_health.provider_status("greenhouse")
        self.assertTrue(row["budget"]["exhausted"])
        allowed, reason = provider_health.fetch_allowed("greenhouse")
        self.assertFalse(allowed)
        self.assertEqual(reason, "budget_exhausted")

    def test_no_budget_means_unlimited(self):
        provider_health.record_fetch("greenhouse", True)
        allowed, _ = provider_health.fetch_allowed("greenhouse")
        self.assertTrue(allowed)

    def test_negative_budget_rejected(self):
        with self.assertRaises(ValueError):
            provider_health.set_budget("greenhouse", -1)


class TestBoard(_IsoMixin, unittest.TestCase):
    def test_board_sorts_worst_first(self):
        provider_health.record_fetch("ok-board", True)
        for _ in range(3):
            provider_health.record_fetch("sad-board", False)
        provider_health.record_captcha("blocked-board", "blocked")
        b = provider_health.board()
        self.assertEqual(
            [r["provider"] for r in b["providers"]],
            ["blocked-board", "sad-board", "ok-board"],
        )
        self.assertEqual(b["counts"]["blocked"], 1)
        self.assertEqual(b["counts"]["degraded"], 1)
        self.assertEqual(b["counts"]["ok"], 1)

    def test_empty_board(self):
        b = provider_health.board()
        self.assertEqual(b["providers"], [])

    def test_degraded_providers(self):
        for _ in range(3):
            provider_health.record_fetch("sad-board", False)
        provider_health.record_fetch("ok-board", True)
        degraded = provider_health.degraded_providers()
        self.assertEqual([r["provider"] for r in degraded], ["sad-board"])

    def test_corrupt_store_loads_empty(self):
        self.store.write_text("{nope", encoding="utf-8")
        self.assertEqual(provider_health.load_store(), {})


class TestWatchHook(_IsoMixin, unittest.TestCase):
    def test_check_watch_records_health_when_asked(self):
        import watch

        w = {"name": "t", "query": "x", "location": "", "board": "greenhouse",
             "filters": {}}
        with mock.patch.object(provider_health, "DEFAULT_STORE", self.store):
            watch.check_watch(w, lambda **kw: [{"id": "1"}], record_health=True)
        row = provider_health.provider_status("greenhouse")
        self.assertEqual(row["total_fetches"], 1)
        self.assertEqual(row["status"], "ok")

    def test_check_watch_failure_records_health(self):
        import watch

        w = {"name": "t", "query": "x", "location": "", "board": "lever",
             "filters": {}}

        def boom(**kw):
            raise RuntimeError("down")

        with mock.patch.object(provider_health, "DEFAULT_STORE", self.store):
            result = watch.check_watch(w, boom, record_health=True)
        self.assertIn("error", result)
        row = provider_health.provider_status("lever")
        self.assertEqual(row["consecutive_failures"], 1)

    def test_check_watch_default_records_nothing(self):
        import watch

        w = {"name": "t", "query": "x", "location": "", "board": "ashby",
             "filters": {}}
        with mock.patch.object(provider_health, "DEFAULT_STORE", self.store):
            watch.check_watch(w, lambda **kw: [])
        # Store file never created: no health side effects by default.
        self.assertFalse(self.store.exists())


class TestCli(unittest.TestCase):
    def test_register_cli(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = provider_health.register_cli(sub)
        self.assertIn("health", handlers)
        args = parser.parse_args(["health", "board"])
        self.assertEqual(args.func.__name__, "_cli_board")
        # Bare `health` shows the board too.
        args = parser.parse_args(["health"])
        self.assertEqual(args.func.__name__, "_cli_board")

    def test_record_requires_ok_or_fail(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        provider_health.register_cli(sub)
        args = parser.parse_args(["health", "record", "--provider", "x"])
        self.assertEqual(args.func(args), 2)


if __name__ == "__main__":
    unittest.main()
