#!/usr/bin/env python3
"""Fail-safe tests for the provider-health contract (Initiative 09).

Rule 1 hardening: every path where the health picture can go wrong must
degrade LOUDLY and honestly, never silently and never green.

Covers: unknown never renders healthy, no-write-on-unknown, the loud
fallback when Initiative 03's module is missing OR broken, streak math
(calendar days), snapshot_from_status drift handling, strict captcha
drift, the explicit block-cooldown default, cooldown cleared on success,
and atomic store compaction.

No network; the health store is a temp JSONL file.
"""

import io
import json
import logging
import os
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from providers import health_contract
from providers.health_contract import (
    DEFAULT_BLOCK_COOLDOWN_S,
    FileHealthStore,
    HealthSnapshot,
    ProviderHealthAdapter,
    record_fetch,
    snapshot_from_status,
)


def _unknown(provider="greenhouse"):
    return HealthSnapshot(
        provider=provider,
        fetched_at=time.time(),
        success=False,
        status="unknown",
        error_state="unknown:status=None",
    )


class UnknownNeverHealthyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileHealthStore(Path(self.tmp.name) / "health.jsonl")

    def test_unknown_snapshot_is_not_success(self):
        snap = snapshot_from_status("lever", {"consecutive_failures": 9})
        self.assertEqual(snap.status, "unknown")
        self.assertFalse(snap.success)

    def test_unknown_never_renders_healthy_on_scoreboard(self):
        from providers import scorecard

        self.store.record(_unknown("greenhouse"))
        rows = scorecard.scoreboard(self.store)
        gh = next(r for r in rows if r["provider"] == "greenhouse")
        self.assertEqual(gh["status"], "unknown")
        self.assertNotEqual(gh["status"], "ok")

    def test_no_data_row_is_not_healthy(self):
        from providers import scorecard

        rows = scorecard.scoreboard(self.store)
        gh = next(r for r in rows if r["provider"] == "greenhouse")
        self.assertEqual(gh["status"], "no data")
        self.assertNotEqual(gh["status"], "ok")


class NoWriteOnUnknownTests(unittest.TestCase):
    def test_adapter_refuses_unknown_snapshot(self):
        """An unknown snapshot is not an observed outcome: the adapter
        writes nothing, rather than inventing a fetch in 03's store."""
        calls = []
        fake_ph = mock.Mock()
        fake_ph.load_store.side_effect = lambda: calls.append("load") or {}
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=fake_ph
        ):
            adapter = ProviderHealthAdapter()
            adapter.record(_unknown("lever"))
        fake_ph.record_fetch.assert_not_called()
        fake_ph.record_captcha.assert_not_called()
        self.assertEqual(calls, [])

    def test_adapter_raises_when_nothing_to_write_to(self):
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=None
        ):
            with self.assertRaises(RuntimeError):
                ProviderHealthAdapter().record(
                    HealthSnapshot(
                        provider="x", fetched_at=0.0, success=True, status="ok"
                    )
                )

    def test_file_store_carries_counters_through_unknown(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = FileHealthStore(Path(tmp.name) / "health.jsonl")
        first = record_fetch(store, "ashby", success=True)
        record_fetch(store, "ashby", success=False, error_state="timeout")
        store.record(_unknown("ashby"))
        latest = store.latest("ashby")
        self.assertEqual(latest.status, "unknown")
        self.assertFalse(latest.success)
        self.assertEqual(latest.last_success_ts, first.last_success_ts)
        self.assertEqual(latest.consecutive_failures, 1)


class LoudFallbackTests(unittest.TestCase):
    def test_broken_module_import_falls_back_loudly(self):
        """A BROKEN provider_health (SyntaxError — the named failure mode)
        must not crash: it logs loudly and falls back, like a missing one."""
        tmpdir = TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        bad = Path(tmpdir.name) / "provider_health.py"
        bad.write_text("def broken(:\n  this is not python\n")
        # Evict the real module and shadow it with the broken file so the
        # import genuinely raises SyntaxError.
        modules = dict(sys.modules)
        modules.pop("provider_health", None)
        with mock.patch.dict(
            sys.modules, modules, clear=True
        ), mock.patch.object(
            sys, "path", [tmpdir.name] + sys.path
        ), self.assertLogs(
            "veto-mcp.providers.health", level="ERROR"
        ) as logs:
            module = health_contract._provider_health_module()
        self.assertIsNone(module)
        self.assertTrue(
            any("FAILED TO IMPORT" in line for line in logs.output),
            logs.output,
        )

    def test_import_error_still_falls_back(self):
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=None
        ), self.assertLogs(
            "veto-mcp.providers.health", level="ERROR"
        ):
            store = health_contract.default_store()
        self.assertIsInstance(store, FileHealthStore)

    def test_default_store_fallback_warns_on_stderr(self):
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=None
        ):
            buf = io.StringIO()
            with redirect_stderr(buf):
                store = health_contract.default_store()
        self.assertIsInstance(store, FileHealthStore)
        self.assertIn("LOCAL JSONL FALLBACK", buf.getvalue())

    def test_adapter_read_path_api_drift_returns_no_data(self):
        """Changed 03 API (load_store blows up) degrades to loud 'no data',
        never a dashboard-crashing traceback."""
        fake_ph = mock.Mock()
        fake_ph.load_store.side_effect = RuntimeError("renamed API")
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=fake_ph
        ), self.assertLogs(
            "veto-mcp.providers.health", level="ERROR"
        ) as logs:
            adapter = ProviderHealthAdapter()
            self.assertIsNone(adapter.latest("greenhouse"))
            self.assertEqual(adapter.providers(), [])
        self.assertTrue(
            any("read path failed" in line for line in logs.output),
            logs.output,
        )


class StreakMathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileHealthStore(Path(self.tmp.name) / "health.jsonl")

    def test_recovery_streak_is_calendar_days(self):
        record_fetch(self.store, "lever", success=True)
        record_fetch(self.store, "lever", success=True)
        record_fetch(self.store, "lever", success=False, error_state="429")
        recovered = record_fetch(self.store, "lever", success=True)
        self.assertEqual(recovered.recovery_streak, 1)
        # Same-day recovery does not double-count the day.
        again = record_fetch(self.store, "lever", success=True)
        self.assertEqual(again.recovery_streak, 1)

    def test_consecutive_failures_reset_on_success(self):
        record_fetch(self.store, "x", success=False, error_state="a")
        record_fetch(self.store, "x", success=False, error_state="b")
        ok = record_fetch(self.store, "x", success=True)
        self.assertEqual(ok.consecutive_failures, 0)

    def test_cooldown_cleared_on_success(self):
        # A successful fetch ends any cooldown: a stale cooldown_until
        # must not keep the row "cooling down" after recovery.
        record_fetch(
            self.store, "x", success=False, error_state="429",
            cooldown_until=time.time() + 3600,
        )
        ok = record_fetch(self.store, "x", success=True)
        self.assertEqual(ok.cooldown_until, 0.0)
        self.assertFalse(ok.is_cooling_down)


class DriftHandlingTests(unittest.TestCase):
    def test_absent_status_is_unknown_not_ok(self):
        snap = snapshot_from_status("lever", {"consecutive_failures": 9})
        self.assertEqual(snap.status, "unknown")
        self.assertFalse(snap.success)
        self.assertIn("unknown", snap.error_state)

    def test_unrecognized_status_is_unknown_not_ok(self):
        snap = snapshot_from_status("lever", {"status": "napping"})
        self.assertEqual(snap.status, "unknown")
        self.assertFalse(snap.success)
        self.assertIn("napping", snap.error_state)

    def test_recognized_statuses_map_exactly(self):
        base = {"consecutive_failures": 0, "captcha_state": "clear"}
        ok = snapshot_from_status("x", {**base, "status": "ok"})
        self.assertEqual((ok.status, ok.success), ("ok", True))
        for drifted in ("cooling", "degraded", "blocked"):
            snap = snapshot_from_status("x", {**base, "status": drifted})
            self.assertEqual(snap.status, "error")
            self.assertFalse(snap.success)

    def test_unknown_captcha_state_is_strict_not_silent_none(self):
        # An unrecognized captcha_state is never silently coerced to
        # "none" (cleared): loud warning + fail-safe "blocking".
        with self.assertLogs(
            "veto-mcp.providers.health", level="WARNING"
        ) as logs:
            snap = snapshot_from_status(
                "x", {"status": "ok", "captcha_state": "mystery"}
            )
        self.assertEqual(snap.captcha_state, "blocking")
        self.assertTrue(
            any("unrecognized" in line for line in logs.output), logs.output
        )

    def test_missing_captcha_state_defaults_to_none(self):
        snap = snapshot_from_status("x", {"status": "ok"})
        self.assertEqual(snap.captcha_state, "none")

    def test_known_captcha_states_map_exactly(self):
        cases = {"clear": "none", "challenged": "seen", "blocked": "blocking"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                snap = snapshot_from_status(
                    "x", {"status": "ok", "captcha_state": raw}
                )
                self.assertEqual(snap.captcha_state, expected)

    def test_parse_ts_rejects_garbage_quietly(self):
        self.assertEqual(health_contract._parse_ts("not-a-date"), 0.0)
        self.assertEqual(health_contract._parse_ts(None), 0.0)
        self.assertEqual(health_contract._parse_ts({"weird": 1}), 0.0)


class CooldownDefaultTests(unittest.TestCase):
    def test_default_block_cooldown_is_documented_and_named(self):
        # No silent operational effect: the write-path default is a named,
        # documented constant (1h = the circuit-breaker's base cooldown).
        import compliance

        self.assertEqual(
            DEFAULT_BLOCK_COOLDOWN_S,
            int(compliance.BLOCK_COOLDOWN_BASE_SECONDS),
        )

    def test_block_without_explicit_cooldown_uses_documented_default(self):
        calls = {}
        fake_ph = mock.Mock()

        def _captcha(provider, state, cooldown_seconds=None):
            calls["cooldown_seconds"] = cooldown_seconds

        fake_ph.record_captcha.side_effect = _captcha
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=fake_ph
        ):
            snap = HealthSnapshot(
                provider="glassdoor",
                fetched_at=0.0,
                success=False,
                status="error",
                captcha_state="blocking",
                cooldown_until=0.0,  # no explicit cooldown
            )
            ProviderHealthAdapter().record(snap)
        self.assertEqual(
            calls["cooldown_seconds"], DEFAULT_BLOCK_COOLDOWN_S
        )

    def test_explicit_cooldown_overrides_default(self):
        calls = {}
        fake_ph = mock.Mock()

        def _captcha(provider, state, cooldown_seconds=None):
            calls["cooldown_seconds"] = cooldown_seconds

        fake_ph.record_captcha.side_effect = _captcha
        with mock.patch.object(
            health_contract, "_provider_health_module", return_value=fake_ph
        ):
            snap = HealthSnapshot(
                provider="glassdoor",
                fetched_at=0.0,
                success=False,
                status="error",
                captcha_state="blocking",
                cooldown_until=time.time() + 1800,
            )
            ProviderHealthAdapter().record(
                snap, default_block_cooldown_s=999
            )
        self.assertGreater(calls["cooldown_seconds"], 1700)
        self.assertLess(calls["cooldown_seconds"], 1801)


class AtomicCompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "health.jsonl"
        self.store = FileHealthStore(self.path)

    def _fill(self, n_lines):
        with self.path.open("w", encoding="utf-8") as fh:
            for i in range(n_lines):
                snap = HealthSnapshot(
                    provider=f"p{i % 3}",
                    fetched_at=float(i),
                    success=True,
                    status="ok",
                )
                fh.write(json.dumps(snap.to_dict()) + "\n")

    def test_compaction_keeps_latest_per_provider(self):
        self._fill(health_contract.FALLBACK_MAX_LINES + 10)
        self.store._compact_if_needed()
        lines = self.path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 3)
        providers = {json.loads(line)["provider"] for line in lines}
        self.assertEqual(providers, {"p0", "p1", "p2"})

    def test_crash_during_compact_cannot_wipe_history(self):
        # os.replace fails mid-compact: the original file must survive
        # intact (the old non-atomic rewrite truncated first, so a crash
        # there wiped all history).
        self._fill(health_contract.FALLBACK_MAX_LINES + 10)
        before = self.path.read_text()
        with mock.patch.object(os, "replace", side_effect=OSError("disk")):
            self.store._compact_if_needed()
        self.assertEqual(self.path.read_text(), before)

    def test_no_stray_temp_files_after_compact(self):
        self._fill(health_contract.FALLBACK_MAX_LINES + 10)
        self.store._compact_if_needed()
        stray = [
            p.name
            for p in Path(self.tmp.name).iterdir()
            if p.name.endswith(".compact.tmp")
        ]
        self.assertEqual(stray, [])


class CapabilityLabelDriftTests(unittest.TestCase):
    def test_aspirational_labels_removed(self):
        # "linkedin"/"indeed" were aspirational (no provider module, no
        # manifest, no evidence): they must not certify capabilities.
        self.assertNotIn("linkedin", health_contract.CAPABILITY_LABELS)
        self.assertNotIn("indeed", health_contract.CAPABILITY_LABELS)

    def test_registered_providers_have_labels(self):
        for name in (
            "greenhouse",
            "lever",
            "ashby",
            "adzuna",
            "glassdoor",
            "ats_direct",
        ):
            self.assertIn(name, health_contract.CAPABILITY_LABELS, name)

    def test_labels_match_contract_kit_manifests(self):
        # Every label key is a registered manifest name (one registry
        # story); the scorecard never labels an unregistered interface.
        from providers import _contract

        for name in health_contract.CAPABILITY_LABELS:
            self.assertIsNotNone(_contract.manifest_for(name), name)


if __name__ == "__main__":
    unittest.main()
