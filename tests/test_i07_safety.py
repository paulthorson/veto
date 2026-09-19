#!/usr/bin/env python3
"""Tests for Initiative 07 safety controls: block, report, rate limits,
deletion, and the segregation guard (epic 6).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import mentors  # noqa: E402
from initiatives.i07 import consent, safety, session_kit  # noqa: E402


def _mentor_profile(i: int = 0) -> dict:
    return {
        "name": f"Mentor {i}",
        "industry": "software",
        "role": "Staff Engineer",
        "seniority": "ic5+",
        "topics": ["interviewing"],
        "linkedin_url": f"https://www.linkedin.com/in/synthetic-mentor-{i}",
        "availability": "Fridays",
        "max_mentees": 3,
        "bio": "I help with interviews.",
    }


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        mentors.MENTORS_FILE = tmp / "mentors.json"
        consent.HANDSHAKES_FILE = tmp / "handshakes.json"
        consent.AUDIT_FILE = tmp / "consent_audit.jsonl"
        safety.SAFETY_FILE = tmp / "safety.json"
        safety.REPORTS_FILE = tmp / "reports.jsonl"
        session_kit.SESSIONS_FILE = tmp / "sessions.json"
        safety.reset_rate_limits()
        self.mentor_id = mentors.mentor_opt_in(_mentor_profile(0))["card"]["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def _request(self, **kw):
        kw.setdefault("mentor_id", self.mentor_id)
        kw.setdefault("mentee_id", "me")
        kw.setdefault("mentee_label", "PT")
        kw.setdefault("goal", "Break into staff engineering")
        kw["synthetic"] = True
        return consent.request_introduction(**kw)

    # -- block -----------------------------------------------------------------

    def test_block_prevents_request(self):
        self.assertTrue(safety.block_actor("me", self.mentor_id)["ok"])
        res = self._request()
        self.assertFalse(res["ok"])
        self.assertIn("block", res["error"])

    def test_block_is_directional_for_check(self):
        self.assertTrue(safety.block_actor("me", self.mentor_id)["ok"])
        self.assertTrue(safety.is_blocked("me", self.mentor_id))
        self.assertFalse(safety.is_blocked(self.mentor_id, "me"))
        # ...but consent is refused if EITHER side blocked the other.
        check = safety.consent_block_check("me", self.mentor_id)
        self.assertTrue(check["blocked"])
        check2 = safety.consent_block_check(self.mentor_id, "me")
        self.assertTrue(check2["blocked"])

    def test_block_withdraws_pending_handshake(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        out = safety.block_actor("me", self.mentor_id)
        self.assertIn(hs_id, out["withdrawn_handshakes"])
        got = consent.get_handshake(hs_id, "me")
        self.assertEqual(got["handshake"]["state"], "withdrawn")
        # The audit honestly records a system withdrawal, not the mentee.
        trail = consent.audit_trail(hs_id)
        withdrawals = [e for e in trail if e["event"] == "withdraw"]
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(withdrawals[0]["actor"], "system")

    def test_withdraw_rejects_unknown_actor(self):
        res = self._request()
        r = consent.withdraw(res["handshake"]["id"], "intruder")
        self.assertFalse(r["ok"])

    def test_block_self_rejected(self):
        res = safety.block_actor("me", "me")
        self.assertFalse(res["ok"])

    def test_unblock(self):
        safety.block_actor("me", self.mentor_id)
        self.assertTrue(safety.unblock_actor("me", self.mentor_id)["ok"])
        self.assertFalse(safety.is_blocked("me", self.mentor_id))
        self.assertTrue(self._request()["ok"])

    # -- report + quarantine ----------------------------------------------------

    def test_report_bad_category_rejected(self):
        res = safety.report("me", self.mentor_id, "rude")
        self.assertFalse(res["ok"])

    def test_two_distinct_reporters_quarantine(self):
        r1 = safety.report("alice", self.mentor_id, "spam", detail="d1")
        self.assertTrue(r1["ok"])
        self.assertFalse(r1["quarantined"])
        self.assertFalse(safety.is_quarantined(self.mentor_id))
        r2 = safety.report("bob", self.mentor_id, "spam", detail="d2")
        self.assertTrue(r2["ok"])
        self.assertTrue(r2["quarantined"])
        self.assertTrue(safety.is_quarantined(self.mentor_id))

    def test_same_reporter_twice_does_not_quarantine(self):
        safety.report("alice", self.mentor_id, "spam")
        r = safety.report("alice", self.mentor_id, "harassment")
        self.assertTrue(r["ok"])
        self.assertFalse(r["quarantined"])

    def test_quarantined_mentor_blocked_from_consent(self):
        safety.report("alice", self.mentor_id, "spam")
        safety.report("bob", self.mentor_id, "spam")
        res = self._request()
        self.assertFalse(res["ok"])
        self.assertIn("safety review", res["error"])

    def test_quarantine_withdraws_pending_handshakes(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        safety.report("alice", self.mentor_id, "spam")
        safety.report("bob", self.mentor_id, "spam")
        got = consent.get_handshake(hs_id, "me")
        self.assertEqual(got["handshake"]["state"], "withdrawn")

    def test_clear_quarantine_requires_human(self):
        safety.report("alice", self.mentor_id, "spam")
        safety.report("bob", self.mentor_id, "spam")
        bad = safety.clear_quarantine(self.mentor_id, reviewed_by="")
        self.assertFalse(bad["ok"])
        good = safety.clear_quarantine(self.mentor_id, reviewed_by="operator")
        self.assertTrue(good["ok"])
        self.assertFalse(safety.is_quarantined(self.mentor_id))

    # -- rate limits --------------------------------------------------------------

    def test_token_bucket_exhaustion(self):
        limits = {"ping": (2, 0.0)}
        self.assertTrue(safety.check_rate_limit("u", "ping", limits=limits))
        self.assertTrue(safety.check_rate_limit("u", "ping", limits=limits))
        self.assertFalse(safety.check_rate_limit("u", "ping", limits=limits))

    def test_rate_limit_scoped_per_actor(self):
        limits = {"ping": (1, 0.0)}
        self.assertTrue(safety.check_rate_limit("u1", "ping", limits=limits))
        self.assertTrue(safety.check_rate_limit("u2", "ping", limits=limits))

    # -- corrupt safety state: fail closed, fail LOUD -----------------------------

    def test_corrupt_safety_json_fails_closed_loud(self):
        safety.block_actor("me", self.mentor_id)
        safety.SAFETY_FILE.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(safety.SafetyStateError):
            safety._load_safety()
        # Fail closed: every read is deny, never permissive.
        with self.assertLogs("veto-mcp.i07.safety", level="ERROR"):
            self.assertTrue(safety.is_blocked("me", self.mentor_id))
        self.assertTrue(safety.is_quarantined(self.mentor_id))
        check = safety.consent_block_check("me", self.mentor_id)
        self.assertTrue(check["blocked"])
        self.assertIn("unreadable", check["error"])
        # discovery_exclusions refuses to return a (possibly empty) set.
        with self.assertRaises(safety.SafetyStateError):
            safety.discovery_exclusions("me")
        # Writes refuse too: the corrupt file is never overwritten by empty state.
        with self.assertRaises(safety.SafetyStateError):
            safety.block_actor("me", "someone-else")
        self.assertEqual(
            safety.SAFETY_FILE.read_text(encoding="utf-8"), "{not valid json"
        )

    def test_corrupt_non_object_safety_json_fails_closed(self):
        safety.SAFETY_FILE.write_text('["not", "an", "object"]', encoding="utf-8")
        with self.assertRaises(safety.SafetyStateError):
            safety._load_safety()
        self.assertTrue(safety.is_blocked("me", self.mentor_id))

    def test_corrupt_safety_json_invalid_utf8_fails_closed(self):
        # Regression: invalid UTF-8 bytes raised a raw UnicodeDecodeError
        # from _load_safety, which the discovery choke point
        # (mentors._safety_exclusions) swallowed via `except Exception ->
        # return set()` — fail OPEN, surfacing blocked and quarantined
        # mentors in discovery. It must be SafetyStateError so discovery
        # is refused instead.
        safety.SAFETY_FILE.write_bytes(b'{"blocked": \xff\xfe invalid')
        with self.assertRaises(safety.SafetyStateError):
            safety._load_safety()
        with self.assertRaises(safety.SafetyStateError):
            safety.discovery_exclusions("me")
        # The discovery choke point re-raises SafetyStateError (fail
        # closed) instead of returning an empty exclusion set.
        with self.assertRaises(safety.SafetyStateError):
            mentors._safety_exclusions()
        # Fail-closed reads stay deny, never permissive.
        self.assertTrue(safety.is_blocked("me", self.mentor_id))
        self.assertTrue(safety.is_quarantined(self.mentor_id))
        # The corrupt bytes are never overwritten by an empty state.
        self.assertEqual(
            safety.SAFETY_FILE.read_bytes(), b'{"blocked": \xff\xfe invalid'
        )

    def test_corrupt_safety_json_wrong_typed_values_fail_closed(self):
        # Regression: valid JSON with null/wrong-typed values sailed past
        # setdefault (which never replaces an EXISTING key) and blew up
        # downstream as AttributeError — again swallowed by the discovery
        # choke point's `except Exception -> return set()` (fail open).
        bad_states = [
            '{"blocked": null, "quarantined": {}}',
            '{"blocked": {}, "quarantined": null}',
            '{"blocked": ["not", "a", "dict"]}',
            '{"blocked": {"me": null}}',
            '{"blocked": {"me": {"m1": "not-an-entry-dict"}}}',
            '{"quarantined": {"m1": ["not", "a", "record"]}}',
            '{"blocked": "nope", "quarantined": {}}',
        ]
        for bad in bad_states:
            with self.subTest(bad=bad):
                safety.SAFETY_FILE.write_text(bad, encoding="utf-8")
                with self.assertRaises(safety.SafetyStateError):
                    safety._load_safety()
                with self.assertRaises(safety.SafetyStateError):
                    safety.discovery_exclusions("me")
                with self.assertRaises(safety.SafetyStateError):
                    mentors._safety_exclusions()
        # Sanity: a well-shaped state still loads (validation is not
        # over-strict), including the empty shape from a fresh install.
        safety.SAFETY_FILE.write_text(
            json.dumps(
                {
                    "blocked": {"me": {"m1": {"at": "t", "reason": ""}}},
                    "quarantined": {"m2": {"at": "t", "status": "pending_human_review"}},
                }
            ),
            encoding="utf-8",
        )
        state = safety._load_safety()
        self.assertIn("m1", state["blocked"]["me"])
        self.assertIn("m2", state["quarantined"])

    # -- structural fail-closed: no corruption class escapes (round 3) -----------

    def _matchmake_refuses(self):
        """matchmake must REFUSE (raise SafetyStateError), never return matches."""
        with self.assertRaises(safety.SafetyStateError):
            mentors.matchmake(
                {
                    "industry": "software",
                    "target_role": "Engineer",
                    "topics_ranked": ["interviewing"],
                    "goal": "x",
                },
                consent_preview=True,
            )

    def test_deeply_nested_json_fails_closed_not_recursion_error(self):
        # Round-2 KICK_BACK regression: pathologically nested JSON raised a
        # raw RecursionError from json.loads — a non-SafetyStateError that
        # the discovery choke point (mentors._safety_exclusions) swallows
        # via `except Exception -> return set()` (fail OPEN), silently
        # surfacing blocked/quarantined mentors in discovery. The structural
        # backstop in _load_safety must convert it to SafetyStateError.
        depth = 25_000
        safety.SAFETY_FILE.write_text("[" * depth + "]" * depth, encoding="utf-8")
        # assertRaises(SafetyStateError) FAILS if a raw RecursionError
        # escapes instead (it is not a SafetyStateError subclass).
        with self.assertRaises(safety.SafetyStateError):
            safety._load_safety()
        # End to end: the choke point refuses (raises) instead of
        # returning an empty exclusion set...
        with self.assertRaises(safety.SafetyStateError):
            mentors._safety_exclusions()
        # ...and matchmake refuses instead of returning blocked mentors.
        self._matchmake_refuses()

    def test_safety_json_path_is_directory_fails_closed(self):
        # Round-2 KICK_BACK regression: read_text on a directory raised a
        # raw IsADirectoryError (an OSError) — same swallowed-fail-open
        # path as the RecursionError case. Must become SafetyStateError.
        safety.SAFETY_FILE.mkdir()
        with self.assertRaises(safety.SafetyStateError):
            safety._load_safety()
        with self.assertRaises(safety.SafetyStateError):
            safety.discovery_exclusions("me")
        with self.assertRaises(safety.SafetyStateError):
            mentors._safety_exclusions()
        self._matchmake_refuses()

    def test_unreadable_safety_json_fails_closed(self):
        # Round-2 KICK_BACK regression: a chmod-000 safety.json raised a
        # raw PermissionError — same swallowed-fail-open path. Must become
        # SafetyStateError.
        if os.geteuid() == 0:
            self.skipTest("chmod 000 does not block root; cannot simulate unreadable")
        safety.SAFETY_FILE.write_text(
            json.dumps({"blocked": {}, "quarantined": {}}), encoding="utf-8"
        )
        safety.SAFETY_FILE.chmod(0)
        try:
            with self.assertRaises(safety.SafetyStateError):
                safety._load_safety()
            with self.assertRaises(safety.SafetyStateError):
                mentors._safety_exclusions()
            self._matchmake_refuses()
            # Fail-closed reads stay deny, never permissive, with a loud
            # log line on the deny path.
            with self.assertLogs("veto-mcp.i07.safety", level="ERROR"):
                self.assertTrue(safety.is_blocked("me", self.mentor_id))
            self.assertTrue(safety.is_quarantined(self.mentor_id))
        finally:
            safety.SAFETY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_corruption_battery_every_class_raises_safety_state_error(self):
        # META-TEST (anti-enumerative-regression): every corruption class
        # in this battery must raise SafetyStateError from _load_safety AND
        # be refused (raise) by the discovery choke point. Rounds 1-2
        # patched named cases and still KICK_BACK'd on residual classes;
        # this test fails the suite if any future change — or any new
        # read/parse path — lets a class escape as its native exception.
        def make_unreadable(path: Path) -> None:
            path.write_text(
                json.dumps({"blocked": {}, "quarantined": {}}), encoding="utf-8"
            )
            path.chmod(0)

        depth = 25_000
        cases = [
            ("garbage_bytes", lambda p: p.write_bytes(b"\x00\x01\x02\xff garbage")),
            ("invalid_utf8", lambda p: p.write_bytes(b'{"blocked": \xff\xfe invalid')),
            ("unparseable_json", lambda p: p.write_text("{not valid json")),
            ("truncated_json", lambda p: p.write_text('{"blocked": {"me": ')),
            ("empty_file", lambda p: p.write_text("")),
            ("non_object_top_level", lambda p: p.write_text('["not", "an", "object"]')),
            ("null_blocked", lambda p: p.write_text('{"blocked": null, "quarantined": {}}')),
            ("null_quarantined", lambda p: p.write_text('{"blocked": {}, "quarantined": null}')),
            ("wrong_typed_blocked", lambda p: p.write_text('{"blocked": "nope"}')),
            (
                "wrong_typed_entry",
                lambda p: p.write_text('{"blocked": {"me": {"m1": "not-a-dict"}}}'),
            ),
            ("deeply_nested_json", lambda p: p.write_text("[" * depth + "]" * depth)),
            ("path_is_directory", lambda p: p.mkdir()),
        ]
        # chmod 000 does not block root, so that case only runs non-root.
        if os.geteuid() != 0:
            cases.append(("unreadable_chmod_000", make_unreadable))

        def reset() -> None:
            p = safety.SAFETY_FILE
            if p.is_dir() and not p.is_symlink():
                p.rmdir()
            elif p.exists() or p.is_symlink():
                p.unlink()

        for name, setup in cases:
            with self.subTest(corruption=name):
                reset()
                setup(safety.SAFETY_FILE)
                try:
                    with self.assertRaises(safety.SafetyStateError):
                        safety._load_safety()
                    # The choke point must refuse (raise), never return an
                    # empty exclusion set (fail open).
                    with self.assertRaises(safety.SafetyStateError):
                        mentors._safety_exclusions()
                finally:
                    if name == "unreadable_chmod_000":
                        safety.SAFETY_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
                    reset()

    def test_delete_mentor_data_corrupt_safety_state_refuses_with_zero_side_effects(self):
        # Regression: delete_mentor_data deleted the card, scrubbed notes,
        # tombstoned handshakes, and rewrote reports BEFORE reaching
        # _load_safety(), then crashed — card gone, report/audit scrub
        # skipped (CONTRACTS §9 "refuse entirely" violated).
        res = self._request()
        hs_id = res["handshake"]["id"]
        self.assertTrue(
            consent.mentor_respond(hs_id, "approve", channel="linkedin_dm")["ok"]
        )
        note_text = "discussed salary bands and my layoff"
        self.assertTrue(session_kit.add_note(hs_id, "me", note_text)["ok"])
        safety.block_actor("someone", self.mentor_id, reason="test reason")

        audit_before = consent.AUDIT_FILE.read_text(encoding="utf-8")
        sessions_before = session_kit.SESSIONS_FILE.read_text(encoding="utf-8")
        handshakes_before = consent.HANDSHAKES_FILE.read_text(encoding="utf-8")
        corrupt = b'{"blocked": \xff\xfe invalid'
        safety.SAFETY_FILE.write_bytes(corrupt)

        with self.assertRaises(safety.SafetyStateError):
            safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)

        # ZERO mutation: card intact, notes intact, no tombstone, reports
        # file untouched, audit trail byte-identical, corrupt safety file
        # not overwritten by an empty state.
        self.assertIn(self.mentor_id, mentors._load_directory())
        self.assertFalse(consent._load()[hs_id].get("deleted"))
        self.assertIn("salary bands", session_kit.SESSIONS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(
            session_kit.SESSIONS_FILE.read_text(encoding="utf-8"), sessions_before
        )
        self.assertEqual(
            consent.HANDSHAKES_FILE.read_text(encoding="utf-8"), handshakes_before
        )
        self.assertEqual(consent.AUDIT_FILE.read_text(encoding="utf-8"), audit_before)
        self.assertFalse(safety.REPORTS_FILE.exists())
        self.assertEqual(safety.SAFETY_FILE.read_bytes(), corrupt)

    def test_delete_mentor_data_corrupt_reports_refuses_with_zero_side_effects(self):
        # A corrupt reports log must also refuse before mutating anything.
        res = self._request()
        hs_id = res["handshake"]["id"]
        safety.REPORTS_FILE.write_text(
            '{"reporter_id": "alice", "reported_id": "x"}\n{corrupt\n',
            encoding="utf-8",
        )
        reports_before = safety.REPORTS_FILE.read_text(encoding="utf-8")
        handshakes_before = consent.HANDSHAKES_FILE.read_text(encoding="utf-8")

        with self.assertRaises(safety.SafetyStateError):
            safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)

        self.assertIn(self.mentor_id, mentors._load_directory())
        self.assertFalse(consent._load()[hs_id].get("deleted"))
        self.assertEqual(
            consent.HANDSHAKES_FILE.read_text(encoding="utf-8"), handshakes_before
        )
        self.assertEqual(
            safety.REPORTS_FILE.read_text(encoding="utf-8"), reports_before
        )

    def test_corrupt_reports_jsonl_line_fails_closed(self):
        # Regression: list_reports silently SKIPPED corrupt lines, so
        # _distinct_reporters could undercount and miss the 2-reporter
        # quarantine tripwire. Decision: fail closed (raise), never skip —
        # a corrupt reports file is unreadable safety state, i.e. deny.
        safety.REPORTS_FILE.write_text(
            json.dumps({"reporter_id": "alice", "reported_id": self.mentor_id})
            + "\n{not valid json\n",
            encoding="utf-8",
        )
        with self.assertRaises(safety.SafetyStateError):
            safety.list_reports()
        # The tripwire computation refuses rather than undercounting.
        with self.assertRaises(safety.SafetyStateError):
            safety._distinct_reporters(self.mentor_id)
        # A non-dict entry (valid JSON, wrong shape) is corrupt too.
        safety.REPORTS_FILE.write_text('"just a string"\n', encoding="utf-8")
        with self.assertRaises(safety.SafetyStateError):
            safety.list_reports()
        # Invalid UTF-8 bytes are corrupt too.
        safety.REPORTS_FILE.write_bytes(b"\xff\xfe not utf-8\n")
        with self.assertRaises(safety.SafetyStateError):
            safety.list_reports()

    def test_reports_jsonl_tolerates_blank_lines_but_not_corrupt_ones(self):
        # Blank lines stay harmless; only real content is validated.
        safety.REPORTS_FILE.write_text(
            "\n"
            + json.dumps({"reporter_id": "alice", "reported_id": self.mentor_id})
            + "\n\n",
            encoding="utf-8",
        )
        entries = safety.list_reports()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["reporter_id"], "alice")

    # -- quarantine anti-griefing ------------------------------------------------

    def test_quarantine_marks_pending_human_review(self):
        safety.report("alice", self.mentor_id, "spam")
        safety.report("bob", self.mentor_id, "spam")
        status = safety.quarantine_status(self.mentor_id)
        self.assertTrue(status["quarantined"])
        self.assertEqual(status["status"], "pending_human_review")
        self.assertEqual(status["review_gate"], "paul_and_independent_reviewer")
        self.assertIsNone(status["reviewed_by"])
        self.assertEqual(status["reporters"], ["alice", "bob"])
        # A single self-asserted reporter can never trigger it.
        other = "sockpuppet-target"
        safety.report("mallory", other, "spam")
        self.assertFalse(safety.is_quarantined(other))

    # -- rate limits: wired, not dead config --------------------------------------

    def test_rate_limit_entries_are_all_enforced(self):
        for action in safety.DEFAULT_LIMITS:
            self.assertIn(action, ("report", "block", "clear_quarantine"))

    def test_block_enforces_rate_limit(self):
        old = safety.DEFAULT_LIMITS["block"]
        safety.DEFAULT_LIMITS["block"] = (1, 0.0)
        try:
            self.assertTrue(safety.block_actor("me", "x-block-1")["ok"])
            res = safety.block_actor("me", "x-block-2")
            self.assertFalse(res["ok"])
            self.assertIn("rate limit", res["error"])
        finally:
            safety.DEFAULT_LIMITS["block"] = old

    def test_report_enforces_rate_limit(self):
        old = safety.DEFAULT_LIMITS["report"]
        safety.DEFAULT_LIMITS["report"] = (1, 0.0)
        try:
            self.assertTrue(safety.report("mallory", self.mentor_id, "spam")["ok"])
            res = safety.report("mallory", self.mentor_id, "harassment")
            self.assertFalse(res["ok"])
            self.assertIn("rate limit", res["error"])
        finally:
            safety.DEFAULT_LIMITS["report"] = old

    # -- deletion -------------------------------------------------------------------

    def test_delete_mentor_data_erases_everything(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        safety.block_actor("someone", self.mentor_id, reason="test reason")
        out = safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)
        self.assertTrue(out["ok"])
        self.assertTrue(out["removed"]["card"])
        self.assertEqual(out["removed"]["handshakes"], 1)
        # Card gone from directory.
        self.assertNotIn(self.mentor_id, mentors._load_directory())
        # Handshake is a tombstone with no PII.
        store = consent._load()
        tomb = store[hs_id]
        self.assertTrue(tomb.get("deleted"))
        self.assertNotIn("goal", tomb)
        self.assertNotIn("mentee_label", tomb)
        # Audit trail keeps a tombstone entry, not PII.
        trail = consent.audit_trail()
        self.assertTrue(any(e["event"] == "deletion" for e in trail))
        # Block entry survives (id only), reason scrubbed.
        self.assertTrue(safety.is_blocked("someone", self.mentor_id))

    def test_delete_requires_requester(self):
        res = safety.delete_mentor_data(self.mentor_id, requested_by="")
        self.assertFalse(res["ok"])

    def test_delete_mentor_data_rejects_stranger_and_leaves_data_intact(self):
        # Regression: delete_mentor_data only rejected an EMPTY
        # requested_by, so requested_by="random-stranger" returned ok True
        # and erased the mentor card, both parties' session notes, the
        # handshake, and audit entries — defeating the gated scrub path via
        # the sanctioned "internal system" call.
        res = self._request()
        hs_id = res["handshake"]["id"]
        self.assertTrue(
            consent.mentor_respond(hs_id, "approve", channel="linkedin_dm")["ok"]
        )
        note_text = "discussed salary bands and my layoff"
        self.assertTrue(session_kit.add_note(hs_id, "me", note_text)["ok"])

        out = safety.delete_mentor_data(self.mentor_id, requested_by="random-stranger")
        self.assertFalse(out["ok"])
        self.assertIn("error", out)
        # A party to the handshake (the mentee) is also not authorized.
        out2 = safety.delete_mentor_data(self.mentor_id, requested_by="me")
        self.assertFalse(out2["ok"])
        # Nothing touched: card, notes, handshake, and block state intact.
        self.assertIn(self.mentor_id, mentors._load_directory())
        self.assertFalse(consent._load()[hs_id].get("deleted"))
        sessions_raw = session_kit.SESSIONS_FILE.read_text(encoding="utf-8")
        self.assertIn("salary bands", sessions_raw)

        # The data subject themselves can still erase.
        ok = safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)
        self.assertTrue(ok["ok"])
        self.assertNotIn(self.mentor_id, mentors._load_directory())
        self.assertTrue(consent._load()[hs_id].get("deleted"))
        sessions_raw = session_kit.SESSIONS_FILE.read_text(encoding="utf-8")
        self.assertNotIn("salary bands", sessions_raw)

    def test_delete_mentee_data(self):
        self._request()
        out = safety.delete_mentee_data("me", requested_by="me")
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"]["handshakes"], 1)
        for hs in consent._load().values():
            self.assertNotIn("goal", hs)

    def test_delete_mentor_scrubs_notes_before_tombstone(self):
        # Regression: notes were scrubbed AFTER tombstoning, so the lookup
        # by mentor_id found nothing and note text survived verbatim.
        res = self._request()
        hs_id = res["handshake"]["id"]
        self.assertTrue(
            consent.mentor_respond(hs_id, "approve", channel="linkedin_dm")["ok"]
        )
        note_text = "discussed salary bands and my layoff"
        self.assertTrue(session_kit.add_note(hs_id, "me", note_text)["ok"])
        out = safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"]["notes"], 1)
        sessions_raw = session_kit.SESSIONS_FILE.read_text(encoding="utf-8")
        self.assertNotIn("salary bands", sessions_raw)
        self.assertNotIn("layoff", sessions_raw)
        # Tombstone still has no PII either.
        store = consent._load()
        self.assertNotIn("goal", store[hs_id])

    def test_delete_mentee_data_scrubs_audit_goal(self):
        # Regression: the mentee's goal persisted verbatim in
        # consent_audit.jsonl after deletion.
        self._request()  # goal = "Break into staff engineering"
        out = safety.delete_mentee_data("me", requested_by="me")
        self.assertTrue(out["ok"])
        self.assertGreater(out["removed"]["audit_entries_scrubbed"], 0)
        raw = consent.AUDIT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("Break into staff engineering", raw)
        # The chain of evidence survives: timestamps/events/actors intact,
        # erasure receipt present, hash chain still verifies.
        trail = consent.audit_trail()
        self.assertTrue(any(e["event"] == "deletion" for e in trail))
        self.assertTrue(all(e.get("entry_hash") for e in trail))
        verify = consent.verify_audit()
        self.assertTrue(verify["ok"], verify)

    def test_delete_mentor_data_scrubs_audit_and_keeps_chain(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        out = safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)
        self.assertTrue(out["ok"])
        self.assertGreater(out["removed"]["audit_entries_scrubbed"], 0)
        raw = consent.AUDIT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("Break into staff engineering", raw)
        verify = consent.verify_audit()
        self.assertTrue(verify["ok"], verify)

    # -- discovery exclusions ------------------------------------------------------

    def test_discovery_exclusions_union_blocked_and_quarantined(self):
        other = mentors.mentor_opt_in(
            {
                "name": "Other",
                "industry": "software",
                "role": "Engineer",
                "seniority": "ic4",
                "topics": ["interviewing"],
                "linkedin_url": "https://www.linkedin.com/in/synthetic-other",
                "max_mentees": 3,
            }
        )["card"]["id"]
        safety.block_actor("me", self.mentor_id)
        safety.report("alice", other, "spam")
        safety.report("bob", other, "spam")
        excluded = safety.discovery_exclusions("me")
        self.assertIn(self.mentor_id, excluded)
        self.assertIn(other, excluded)

    def test_excluded_mentors_hidden_from_matchmake(self):
        safety.block_actor("me", self.mentor_id)
        excluded = safety.discovery_exclusions("me")
        res = mentors.matchmake(
            {
                "industry": "software",
                "target_role": "Engineer",
                "topics_ranked": ["interviewing"],
                "goal": "x",
            },
            consent_preview=True,
            exclude_mentor_ids=excluded,
        )
        self.assertEqual(res["matches"], [])
        self.assertIn("exclusions", res["guidance"])

    # -- segregation ---------------------------------------------------------------
    # (section header restored after exclusion tests were inserted above)

    def test_segregation_check_clean(self):
        self._request()
        out = safety.segregation_check()
        self.assertTrue(out["ok"], out["violations"])
        self.assertEqual(out["violations"], [])

    def test_segregation_check_catches_card_name_reference(self):
        real_base = mentors.BASE_DIR
        tmp_base = Path(self._tmp.name) / "fakeproj"
        tmp_base.mkdir()
        try:
            mentors.BASE_DIR = tmp_base
            # The card name ("Mentor 0") must be caught, per the docstring.
            (tmp_base / "applications.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "app-1",
                            "note": f"spoke with Mentor 0 ({self.mentor_id})",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            out = safety.segregation_check()
            self.assertFalse(out["ok"])
            self.assertTrue(any("Mentor 0" in v for v in out["violations"]))
            self.assertTrue(any(self.mentor_id in v for v in out["violations"]))
        finally:
            mentors.BASE_DIR = real_base

    def test_segregation_check_catches_app_id_in_session_notes(self):
        real_base = mentors.BASE_DIR
        tmp_base = Path(self._tmp.name) / "fakeproj2"
        tmp_base.mkdir()
        real_sessions = session_kit.SESSIONS_FILE
        try:
            mentors.BASE_DIR = tmp_base
            (tmp_base / "applications.json").write_text(
                json.dumps([{"id": "app-xyz-9", "title": "Staff Engineer"}]),
                encoding="utf-8",
            )
            session_kit.SESSIONS_FILE = tmp_base / "sessions.json"
            (tmp_base / "sessions.json").write_text(
                json.dumps({"hs-1": {"notes": [{"text": "follow up on app-xyz-9"}]}}),
                encoding="utf-8",
            )
            out = safety.segregation_check()
            self.assertFalse(out["ok"])
            self.assertTrue(any("app-xyz-9" in v for v in out["violations"]))
        finally:
            mentors.BASE_DIR = real_base
            session_kit.SESSIONS_FILE = real_sessions


if __name__ == "__main__":
    unittest.main()
