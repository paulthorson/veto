#!/usr/bin/env python3
"""Tests for Initiative 09 epic 6: the reliability scorecard and the
provider-health contract interface (an adapter over Initiative 03's
in-tree ``provider_health.py``; falls back to a local JSONL store when
03's module is unavailable).

No network; the health store is a temp JSONL file. Asserts streak math
(in calendar DAYS, matching 03), honest "no data" rows, the explicit
``unknown`` state on schema drift (never rendered as healthy), and the
four scorecard columns.
"""

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from providers import health_contract, scorecard
from providers.health_contract import (
    FileHealthStore,
    HealthSnapshot,
    record_fetch,
)


class HealthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileHealthStore(Path(self.tmp.name) / "health.jsonl")

    def test_record_and_latest_roundtrip(self):
        snap = record_fetch(
            self.store, "greenhouse", success=True,
            capability_label="search+details",
            budget_used=12, budget_total=500,
        )
        self.assertTrue(snap.success)
        self.assertEqual(snap.status, "ok")
        # Day-unit streaks: no failure to recover from yet, so 0 days.
        self.assertEqual(snap.recovery_streak, 0)
        latest = self.store.latest("greenhouse")
        self.assertEqual(latest.capability_label, "search+details")

    def test_streak_math(self):
        # recovery_streak is CALENDAR DAYS (matching 03), not attempts:
        # successes with nothing to recover from leave it at 0.
        first = record_fetch(self.store, "lever", success=True)
        second = record_fetch(self.store, "lever", success=True)
        self.assertEqual(first.recovery_streak, 0)
        self.assertEqual(second.recovery_streak, 0)
        third = record_fetch(self.store, "lever", success=False,
                             error_state="http_429")
        self.assertEqual(third.recovery_streak, 0)
        self.assertEqual(third.consecutive_failures, 1)
        self.assertEqual(third.error_state, "http_429")
        record_fetch(self.store, "lever", success=False,
                     error_state="http_429")
        fourth = record_fetch(self.store, "lever", success=True)
        self.assertEqual(fourth.recovery_streak, 1)  # one recovery day
        self.assertEqual(fourth.consecutive_failures, 0)
        self.assertEqual(fourth.error_state, "")
        # Same-day recovery does not double-count the day.
        fifth = record_fetch(self.store, "lever", success=True)
        self.assertEqual(fifth.recovery_streak, 1)

    def test_last_success_carries_forward(self):
        first = record_fetch(self.store, "ashby", success=True)
        time.sleep(0.01)
        second = record_fetch(self.store, "ashby", success=False,
                              error_state="timeout")
        self.assertEqual(second.last_success_ts, first.last_success_ts)

    def test_unknown_provider_has_no_snapshot(self):
        self.assertIsNone(self.store.latest("nope"))

    def test_unknown_snapshot_preserves_counters(self):
        # An UNKNOWN observation is neither success nor failure: the
        # fallback store carries every counter forward untouched.
        first = record_fetch(self.store, "ashby", success=True)
        record_fetch(self.store, "ashby", success=False,
                     error_state="timeout")
        unknown = HealthSnapshot(
            provider="ashby", fetched_at=time.time(), success=False,
            status="unknown", error_state="unknown:status=None")
        self.store.record(unknown)
        latest = self.store.latest("ashby")
        self.assertEqual(latest.status, "unknown")
        self.assertFalse(latest.success)
        self.assertEqual(latest.last_success_ts, first.last_success_ts)
        self.assertEqual(latest.consecutive_failures, 1)
        self.assertEqual(latest.recovery_streak, 0)

    def test_scoreboard_never_renders_unknown_as_healthy(self):
        unknown = HealthSnapshot(
            provider="greenhouse", fetched_at=time.time(), success=False,
            status="unknown", error_state="unknown:status=None")
        self.store.record(unknown)
        rows = scorecard.scoreboard(self.store)
        gh = next(r for r in rows if r["provider"] == "greenhouse")
        self.assertNotEqual(gh["status"], "ok")
        # Fail-safe today: renders "error" until scorecard.py adds an
        # explicit "unknown" row status (flagged to its owner).

    def test_record_fetch_derives_status(self):
        ok = record_fetch(self.store, "greenhouse", success=True)
        self.assertEqual(ok.status, "ok")
        err = record_fetch(self.store, "lever", success=False,
                           error_state="timeout")
        self.assertEqual(err.status, "error")

    def test_store_is_local_jsonl(self):
        record_fetch(self.store, "greenhouse", success=True)
        path = Path(self.tmp.name) / "health.jsonl"
        self.assertTrue(path.exists())
        raw = json.loads(path.read_text().strip().splitlines()[-1])
        self.assertEqual(raw["provider"], "greenhouse")

    def test_snapshot_protocol_fields(self):
        snap = HealthSnapshot(provider="x", fetched_at=time.time(),
                              success=True)
        d = snap.to_dict()
        for key in ("status", "freshness_s", "error_state",
                    "last_success_ts", "capability_label", "budget_used",
                    "budget_total", "cooldown_until", "captcha_state",
                    "recovery_streak", "last_recovery_day",
                    "consecutive_failures"):
            self.assertIn(key, d, key)


class ScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileHealthStore(Path(self.tmp.name) / "health.jsonl")

    def test_no_data_rows_are_honest(self):
        rows = scorecard.scoreboard(self.store)
        names = {r["provider"] for r in rows}
        self.assertIn("greenhouse", names)
        gh = next(r for r in rows if r["provider"] == "greenhouse")
        self.assertEqual(gh["status"], "no data")
        self.assertEqual(gh["last_success"], "never")
        # Unknowns are unknowns, never "healthy".
        self.assertNotEqual(gh["status"], "ok")

    def test_recorded_provider_shows_four_columns(self):
        record_fetch(self.store, "lever", success=True,
                     capability_label="search+details")
        rows = scorecard.scoreboard(self.store)
        lv = next(r for r in rows if r["provider"] == "lever")
        self.assertEqual(lv["status"], "ok")
        self.assertEqual(lv["freshness"], "fresh")
        self.assertNotEqual(lv["last_success"], "never")
        self.assertEqual(lv["capability"], "search+details")
        self.assertEqual(lv["error_state"], "—")

    def test_error_row_shows_error_state(self):
        record_fetch(self.store, "ziprecruiter", success=False,
                     error_state="captcha", captcha_state="blocking")
        rows = scorecard.scoreboard(self.store)
        zr = next(r for r in rows if r["provider"] == "ziprecruiter")
        self.assertEqual(zr["status"], "error")
        self.assertEqual(zr["error_state"], "captcha")
        self.assertEqual(zr["captcha"], "blocking")

    def test_manifest_completeness_flag(self):
        rows = scorecard.scoreboard(self.store)
        for r in rows:
            if r["provider"] in ("greenhouse", "lever", "ashby"):
                self.assertTrue(r["manifest_complete"], r["provider"])

    def test_terminal_render_is_a_table(self):
        text = scorecard.render_terminal(scorecard.scoreboard(self.store))
        self.assertIn("provider", text)
        self.assertIn("capability", text)
        self.assertIn("local-first", text)

    def test_json_output_parses(self):
        rows = scorecard.scoreboard(self.store)
        parsed = json.loads(scorecard.to_json(rows))
        self.assertEqual(len(parsed), len(rows))


class ProviderHealthAdapterTests(unittest.TestCase):
    """The 03→09 adapter: Initiative 03's provider_health.py mapped onto
    HealthSnapshot. Patches 03's DEFAULT_STORE to a temp file.

    Skipped when 03's file is not in the tree (uncommitted) — the adapter
    then exercises its FileHealthStore fallback instead.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import provider_health  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(
                "provider_health (Initiative 03) not in tree; "
                "adapter falls back to FileHealthStore"
            )

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import provider_health
        self._patch = mock.patch.object(
            provider_health, "DEFAULT_STORE",
            Path(self.tmp.name) / "provider_health.json",
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.adapter = health_contract.ProviderHealthAdapter()

    def test_success_maps_to_ok_snapshot(self):
        import provider_health
        provider_health.record_fetch("greenhouse", True)
        snap = self.adapter.latest("greenhouse")
        self.assertIsNotNone(snap)
        self.assertTrue(snap.success)
        # Asserts the mapping result itself: the adapter resolved 03's
        # "ok" status onto the snapshot's explicit status field, with no
        # error state carried over.
        self.assertEqual(snap.status, "ok")
        self.assertEqual(snap.error_state, "")
        self.assertNotEqual(snap.last_success_ts, 0.0)
        self.assertEqual(snap.capability_label, "search+details")
        self.assertEqual(snap.captcha_state, "none")

    def test_absent_status_maps_to_unknown_not_ok(self):
        """Regression (Rule 1 veto repro): a row without 'status' must not
        become success=True / render green."""
        snap = health_contract.snapshot_from_status(
            "lever", {"consecutive_failures": 9, "captcha_state": "clear"})
        self.assertFalse(snap.success)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("unknown", snap.error_state)

    def test_unrecognized_status_maps_to_unknown(self):
        snap = health_contract.snapshot_from_status(
            "lever", {"status": "napping", "consecutive_failures": 0,
                      "captcha_state": "clear"})
        self.assertFalse(snap.success)
        self.assertEqual(snap.status, "unknown")
        self.assertIn("napping", snap.error_state)

    def test_recognized_statuses_map(self):
        base = {"consecutive_failures": 0, "captcha_state": "clear"}
        ok = health_contract.snapshot_from_status(
            "x", {**base, "status": "ok"})
        self.assertEqual((ok.status, ok.success), ("ok", True))
        for drifted in ("cooling", "degraded", "blocked"):
            snap = health_contract.snapshot_from_status(
                "x", {**base, "status": drifted})
            self.assertEqual(snap.status, "error")
            self.assertFalse(snap.success)

    def test_captcha_blocked_maps(self):
        import provider_health
        provider_health.record_fetch("ziprecruiter", False, note="denied")
        provider_health.record_captcha("ziprecruiter", "blocked")
        snap = self.adapter.latest("ziprecruiter")
        self.assertFalse(snap.success)
        self.assertEqual(snap.captcha_state, "blocking")
        self.assertIn("captcha", snap.error_state)

    def test_degraded_maps_failure_count(self):
        import provider_health
        for _ in range(3):
            provider_health.record_fetch("lever", False)
        snap = self.adapter.latest("lever")
        self.assertEqual(snap.consecutive_failures, 3)
        self.assertIn("failure", snap.error_state)

    def test_unknown_provider_returns_none(self):
        self.assertIsNone(self.adapter.latest("nope"))

    def test_adapter_record_delegates_to_03(self):
        import provider_health
        snap = health_contract.HealthSnapshot(
            provider="ashby", fetched_at=0.0, success=True, status="ok")
        self.adapter.record(snap)
        row = provider_health.provider_status("ashby")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["consecutive_failures"], 0)

    def test_adapter_record_passes_budget_and_cooldown(self):
        """budget_total / cooldown_until are no longer silently dropped:
        they land in 03's store via set_budget / cooldown."""
        import provider_health
        snap = health_contract.HealthSnapshot(
            provider="lever", fetched_at=0.0, success=False, status="error",
            error_state="http_429", budget_total=40,
            cooldown_until=time.time() + 7200)
        self.adapter.record(snap)
        row = provider_health.provider_status("lever")
        self.assertEqual(row["status"], "cooling")
        self.assertGreater(row["cooldown_remaining_s"], 7000)
        self.assertEqual(row["budget"]["daily_limit"], 40)

    def test_adapter_record_passes_captcha_cooldown(self):
        import provider_health
        snap = health_contract.HealthSnapshot(
            provider="ziprecruiter", fetched_at=0.0, success=False,
            status="error", error_state="captcha",
            captcha_state="blocking", cooldown_until=time.time() + 1800)
        self.adapter.record(snap)
        row = provider_health.provider_status("ziprecruiter")
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["captcha_state"], "blocked")
        self.assertGreater(row["cooldown_remaining_s"], 1700)

    def test_adapter_record_refuses_unknown_snapshots(self):
        """An UNKNOWN snapshot is not an observed outcome: the adapter
        writes nothing to 03's store rather than inventing a fetch."""
        snap = health_contract.snapshot_from_status(
            "lever", {"consecutive_failures": 9, "captcha_state": "clear"})
        self.assertEqual(snap.status, "unknown")
        self.adapter.record(snap)
        self.assertIsNone(self.adapter.latest("lever"))

    def test_scoreboard_reads_through_adapter(self):
        import provider_health
        provider_health.record_fetch("greenhouse", True)
        rows = scorecard.scoreboard(self.adapter)
        gh = next(r for r in rows if r["provider"] == "greenhouse")
        self.assertEqual(gh["status"], "ok")
        self.assertNotEqual(gh["last_success"], "never")

    def test_snapshot_from_status_field_mapping(self):
        row = {
            "status": "cooling", "cooldown_remaining_s": 100,
            "cooldown_reason": "http_429", "captcha_state": "clear",
            "consecutive_failures": 1, "recovery_streak_days": 2,
            "last_successful_fetch": "2026-09-13T10:00:00+00:00",
            "budget": {"used_today": 5, "daily_limit": 40},
        }
        snap = health_contract.snapshot_from_status("ziprecruiter", row)
        self.assertEqual(snap.error_state, "http_429")
        self.assertEqual(snap.budget_used, 5)
        self.assertEqual(snap.budget_total, 40)
        self.assertEqual(snap.recovery_streak, 2)
        self.assertTrue(snap.is_cooling_down)


if __name__ == "__main__":
    unittest.main()
