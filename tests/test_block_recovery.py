#!/usr/bin/env python3
"""Tests for the circuit-breaker recovery path (compliance.py).

``compliance.record_block`` trips a 1h/doubling/24h-capped cooldown with
no decay path. ``compliance.record_search_success`` closes the gap: a
window of consecutive genuine successful fetches clears the block and
resets the cooldown. These tests cover:

  (a) blocks escalate to the 24h cap,
  (b) sustained successes clear the block and reset the cooldown,
  (c) intermittent failures still escalate (a block resets the streak),
  (d) a provider records successes only on genuine fetches (never on
      403/429/CAPTCHA) — tested at the provider level before the
      provider was deleted; the contract is preserved by the server-level
      wiring tests below, and
  (e) server.search_jobs wires successful provider searches into the
      recovery counter.

All state is pointed at a temp compliance.json; all HTTP is mocked.
"""

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import compliance


def _block_info(board, path):
    return compliance.load_state(path)["blocks"][board]


class ComplianceRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cpath = Path(self.tmp.name) / "compliance.json"
        state = compliance._default_state()
        state["risk_acknowledged"] = True
        compliance.save_state(state, self.cpath)
        patcher = mock.patch.object(compliance, "COMPLIANCE_FILE", self.cpath)
        self.addCleanup(patcher.stop)
        patcher.start()

    # -- (a) escalation -------------------------------------------------

    def test_block_cooldown_escalates_to_24h_cap(self):
        expected = [3600, 7200, 14400, 28800, 57600, 86400, 86400]
        for want in expected:
            self.assertEqual(compliance.record_block("glassdoor"), want)
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 7)
        self.assertIsNotNone(info["blocked_until"])

    # -- (b) sustained successes clear the block -----------------------

    def test_three_consecutive_successes_clear_block(self):
        compliance.record_block("glassdoor")
        allowed, reason = compliance.check_search_allowed("glassdoor")
        self.assertFalse(allowed)
        self.assertIn("cooling down", reason)

        # Two successes are not enough: the block stands.
        self.assertFalse(compliance.record_search_success("glassdoor"))
        self.assertFalse(compliance.record_search_success("glassdoor"))
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 1)
        self.assertIsNotNone(info["blocked_until"])
        allowed, _ = compliance.check_search_allowed("glassdoor")
        self.assertFalse(allowed)

        # The third consecutive success clears the block and resets the
        # cooldown.
        self.assertTrue(compliance.record_search_success("glassdoor"))
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 0)
        self.assertIsNone(info["blocked_until"])
        self.assertEqual(info["successes"], 0)
        allowed, _ = compliance.check_search_allowed("glassdoor")
        self.assertTrue(allowed)

    # -- (f) observability -------------------------------------------------

    def test_status_summary_surfaces_recovery_streak(self):
        summary = compliance.status_summary()
        board = summary["boards"]["glassdoor"]
        self.assertEqual(board["recovery_successes"], 0)
        self.assertEqual(board["recovery_window"], compliance.RECOVERY_SUCCESS_WINDOW)

        compliance.record_block("glassdoor")
        compliance.record_search_success("glassdoor")
        board = compliance.status_summary()["boards"]["glassdoor"]
        self.assertEqual(board["recovery_successes"], 1)
        self.assertEqual(board["consecutive_blocks"], 1)

        # Recovery clears the block and the streak together.
        for _ in range(compliance.RECOVERY_SUCCESS_WINDOW - 1):
            compliance.record_search_success("glassdoor")
        board = compliance.status_summary()["boards"]["glassdoor"]
        self.assertEqual(board["recovery_successes"], 0)
        self.assertEqual(board["consecutive_blocks"], 0)

    def test_block_after_recovery_restarts_at_base_cooldown(self):
        compliance.record_block("glassdoor")
        for _ in range(compliance.RECOVERY_SUCCESS_WINDOW):
            compliance.record_search_success("glassdoor")
        # Recovery cleared the history, so the next block starts at 1h.
        self.assertEqual(compliance.record_block("glassdoor"), 3600)
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 1)

    # -- (c) intermittent failures still escalate -----------------------

    def test_block_resets_success_streak_and_escalation_continues(self):
        compliance.record_block("glassdoor")  # 1h, consecutive=1
        compliance.record_search_success("glassdoor")
        compliance.record_search_success("glassdoor")
        # A new block breaks the streak and escalates.
        self.assertEqual(compliance.record_block("glassdoor"), 7200)
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 2)
        self.assertEqual(info["successes"], 0)
        allowed, reason = compliance.check_search_allowed("glassdoor")
        self.assertFalse(allowed)
        self.assertIn("cooling down", reason)
        # Recovery still needs a full fresh window afterwards.
        compliance.record_search_success("glassdoor")
        compliance.record_search_success("glassdoor")
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 2)  # not cleared yet
        self.assertTrue(compliance.record_search_success("glassdoor"))
        info = _block_info("glassdoor", self.cpath)
        self.assertEqual(info["consecutive"], 0)

    def test_success_without_block_history_is_noop(self):
        self.assertFalse(compliance.record_search_success("greenhouse"))
        # No block history means no state churn at all: nothing persisted.
        st = compliance.load_state(self.cpath)
        self.assertNotIn("greenhouse", st["blocks"])


# ---------------------------------------------------------------------------
# Server-level wiring: search_jobs does NOT record recovery successes
# itself. Providers record genuine fetches at their own call sites (post
# block/CAPTCHA checks), and those recordings flow through
# server.search_jobs into the recovery counter. A provider whose fetch was
# pushed back (403/429/CAPTCHA) returns [] without raising and records a
# block, never a success -- the server must not count such a fetch.
# ---------------------------------------------------------------------------


class _RecordingStubProvider:
    name = "greenhouse"

    def search(self, query, location, limit, remote_only):
        # Mirrors a real provider: only genuine fetches are recorded.
        compliance.record_search_success(self.name)
        return [{
            "id": "greenhouse:test-1",
            "title": "Backend Engineer",
            "company": "Acme Corp",
            "location": "New York, NY",
            "url": "https://example.com/j/1",
            "board": self.name,
        }]


class _BlockedStubProvider:
    name = "greenhouse"

    def search(self, query, location, limit, remote_only):
        # Mirrors a scraping-tier provider on 403/429/CAPTCHA: record a
        # block, return [] without raising, never record a success.
        compliance.record_block(self.name)
        return []


class ServerRecoveryWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cpath = Path(self.tmp.name) / "compliance.json"
        compliance.save_state(compliance._default_state(), self.cpath)
        cpatcher = mock.patch.object(compliance, "COMPLIANCE_FILE", self.cpath)
        self.addCleanup(cpatcher.stop)
        cpatcher.start()
        self.addCleanup(mock.patch.stopall)

        import server

        self.server = server
        stub = _RecordingStubProvider()
        mock.patch.dict(server.PROVIDERS, {"greenhouse": stub}).start()
        # Keep the test hermetic: adjudication always allows; the gate
        # under test is compliance.check_search_allowed.
        mock.patch.object(
            server, "_risk_policy",
            mock.Mock(**{"adjudicate_search.return_value": {
                "allowed": True, "reason": "", "tier": "official",
            }}),
        ).start()

    def _expire_cooldown(self):
        st = compliance.load_state(self.cpath)
        st["blocks"]["greenhouse"]["blocked_until"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        compliance.save_state(st, self.cpath)

    def test_successful_searches_clear_block_via_server(self):
        compliance.record_block("greenhouse")
        self._expire_cooldown()  # cooldown elapsed; history remains

        for _ in range(compliance.RECOVERY_SUCCESS_WINDOW - 1):
            jobs = self.server.search_jobs(query="engineer", location="New York",
                                           board="greenhouse", limit=5)
            self.assertEqual(len(jobs), 1)
        info = _block_info("greenhouse", self.cpath)
        self.assertEqual(info["consecutive"], 1)  # not cleared yet

        jobs = self.server.search_jobs(query="engineer", location="New York",
                                       board="greenhouse", limit=5)
        self.assertEqual(len(jobs), 1)
        info = _block_info("greenhouse", self.cpath)
        self.assertEqual(info["consecutive"], 0)
        self.assertIsNone(info["blocked_until"])


    def test_blocked_search_does_not_count_toward_recovery_via_server(self):
        # Defect regression: a 429/CAPTCHA'd fetch through the real
        # server.search_jobs wiring must leave `successes` at 0 and never
        # clear the block.
        compliance.record_block("greenhouse")
        self._expire_cooldown()  # cooldown elapsed; history remains

        with mock.patch.dict(self.server.PROVIDERS,
                             {"greenhouse": _BlockedStubProvider()}):
            for _ in range(compliance.RECOVERY_SUCCESS_WINDOW):
                jobs = self.server.search_jobs(
                    query="engineer", location="New York",
                    board="greenhouse", limit=5)
                self.assertEqual(jobs, [])
        info = _block_info("greenhouse", self.cpath)
        self.assertEqual(info["successes"], 0)
        self.assertEqual(info["consecutive"], 4)  # 1 + 3 new blocks
        self.assertIsNotNone(info["blocked_until"])

    def test_server_does_not_record_successes_itself(self):
        # A provider that returns normally without recording any success
        # must not gain recovery credit from the server.
        import types
        plain = types.SimpleNamespace(
            name="greenhouse",
            search=lambda *a, **k: [{"board": "greenhouse"}],
        )
        compliance.record_block("greenhouse")
        self._expire_cooldown()
        with mock.patch.dict(self.server.PROVIDERS, {"greenhouse": plain}):
            jobs = self.server.search_jobs(query="engineer", location="New York",
                                           board="greenhouse", limit=5)
            self.assertEqual(len(jobs), 1)
        info = _block_info("greenhouse", self.cpath)
        self.assertEqual(info["successes"], 0)
        self.assertEqual(info["consecutive"], 1)  # still blocked


if __name__ == "__main__":
    unittest.main()
