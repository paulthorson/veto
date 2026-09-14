#!/usr/bin/env python3
"""Tests for the Initiative 07 session kit (epic 4): agenda, consent-gated
context packet, question builder, notes, follow-up, feedback, corruption
safety, privacy, and block/quarantine gating.
"""

from __future__ import annotations

import json
import logging
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


class SessionKitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        mentors.MENTORS_FILE = tmp / "mentors.json"
        consent.HANDSHAKES_FILE = tmp / "handshakes.json"
        consent.AUDIT_FILE = tmp / "consent_audit.jsonl"
        safety.SAFETY_FILE = tmp / "safety.json"
        safety.REPORTS_FILE = tmp / "reports.jsonl"
        safety.reset_rate_limits()
        session_kit.SESSIONS_FILE = tmp / "sessions.json"
        self.mentor_id = mentors.mentor_opt_in(_mentor_profile(0))["card"]["id"]

    def tearDown(self):
        self._tmp.cleanup()

    def _mutual_handshake(self, **kw):
        kw.setdefault("mentor_id", self.mentor_id)
        kw.setdefault("mentee_id", "me")
        kw.setdefault("mentee_label", "PT")
        kw.setdefault("goal", "Break into staff engineering")
        kw.setdefault("expected_outcome", "A study plan")
        kw["synthetic"] = True
        res = consent.request_introduction(**kw)
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        return hs_id

    def _pending_handshake(self):
        res = consent.request_introduction(
            self.mentor_id, "me", "PT", "goal here", synthetic=True
        )
        return res["handshake"]["id"]

    # -- consent gating ---------------------------------------------------------

    def test_context_packet_refused_before_mutual(self):
        hs_id = self._pending_handshake()
        res = session_kit.context_packet(hs_id, "me")
        self.assertFalse(res["ok"])
        self.assertIn("mutual", res["error"])

    def test_notes_refused_before_mutual(self):
        hs_id = self._pending_handshake()
        self.assertFalse(session_kit.add_note(hs_id, "me", "hello")["ok"])
        self.assertFalse(session_kit.get_notes(hs_id, "me")["ok"])

    def test_feedback_refused_before_mutual(self):
        hs_id = self._pending_handshake()
        res = session_kit.session_feedback(hs_id, "me", 5)
        self.assertFalse(res["ok"])

    def test_context_packet_full_after_mutual(self):
        hs_id = self._mutual_handshake()
        res = session_kit.context_packet(hs_id, "me")
        self.assertTrue(res["ok"])
        packet = res["packet"]
        self.assertIn("linkedin_url", packet["mentor"])
        self.assertEqual(packet["mentee"]["expected_outcome"], "A study plan")
        self.assertIn("mentor_consented_at", packet["consent_receipt"])

    # -- agenda ------------------------------------------------------------------

    def test_agenda_includes_expected_outcome(self):
        hs_id = self._mutual_handshake()
        res = session_kit.build_agenda(hs_id, "me")
        self.assertTrue(res["ok"])
        self.assertIn("A study plan", res["agenda"])

    # -- question builder ----------------------------------------------------------

    def test_question_builder(self):
        res = session_kit.question_builder(["interviewing"], goal="staff promo", n=3)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["questions"]), 3)
        self.assertEqual(res["questions"][0]["topic"], "goal")

    def test_question_builder_bad_topic(self):
        res = session_kit.question_builder(["skydiving"])
        self.assertFalse(res["ok"])

    # -- notes ---------------------------------------------------------------------

    def test_notes_append_only(self):
        hs_id = self._mutual_handshake()
        self.assertTrue(session_kit.add_note(hs_id, "me", "n1", kind="takeaway")["ok"])
        self.assertTrue(session_kit.add_note(hs_id, "me", "n2", kind="action")["ok"])
        got = session_kit.get_notes(hs_id, "me")
        self.assertEqual(len(got["notes"]), 2)
        self.assertEqual(got["notes"][0]["kind"], "takeaway")

    def test_note_requires_text(self):
        hs_id = self._mutual_handshake()
        self.assertFalse(session_kit.add_note(hs_id, "me", "  ")["ok"])

    # -- follow-up -------------------------------------------------------------------

    def test_followup_plan_uses_notes(self):
        hs_id = self._mutual_handshake()
        session_kit.add_note(hs_id, "me", "Do 3 mocks", kind="action")
        res = session_kit.followup_plan(hs_id, "me")
        self.assertTrue(res["ok"])
        self.assertIn("Do 3 mocks", res["plan"])
        self.assertIn("thank-you", res["plan"])

    # -- feedback ----------------------------------------------------------------------

    def test_feedback_feeds_ratings(self):
        hs_id = self._mutual_handshake()
        res = session_kit.session_feedback(hs_id, "me", 5, note="great session")
        self.assertTrue(res["ok"])
        self.assertEqual(res["side"], "mentee")
        summary = res["rating_summary"]
        self.assertEqual(summary["mentee"]["count"], 1)

    def test_feedback_bad_score_rejected(self):
        hs_id = self._mutual_handshake()
        self.assertFalse(session_kit.session_feedback(hs_id, "me", 6)["ok"])
        self.assertFalse(session_kit.session_feedback(hs_id, "me", 0)["ok"])

    def test_mentor_side_feedback(self):
        hs_id = self._mutual_handshake()
        res = session_kit.session_feedback(hs_id, self.mentor_id, 4)
        self.assertTrue(res["ok"])
        self.assertEqual(res["side"], "mentor")

    # -- feedback actor gating (fail closed) -------------------------------------------

    def test_feedback_rejects_owner_superuser(self):
        """'owner' is admitted by consent.get_handshake but is not a party:
        it must be rejected loudly, never mislabeled as a mentor rating."""
        hs_id = self._mutual_handshake()
        res = session_kit.session_feedback(hs_id, "owner", 5, note="sneaky")
        self.assertFalse(res["ok"])
        self.assertIn("not a party", res["error"])
        # No bogus mentor-side rating was written.
        card = mentors._load_directory()[self.mentor_id]
        summary = mentors._rating_summary(card)
        self.assertEqual(summary["mentor"]["count"], 0)
        self.assertEqual(summary["mentee"]["count"], 0)

    def test_feedback_rejects_random_actor(self):
        hs_id = self._mutual_handshake()
        res = session_kit.session_feedback(hs_id, "stranger", 5)
        self.assertFalse(res["ok"])
        self.assertIn("not a party", res["error"])

    def test_session_record_persisted_before_rating(self):
        """The session record must exist even when the rating write fails
        (no orphan ratings; record-first ordering)."""
        hs_id = self._mutual_handshake()
        real_rate = mentors.rate_mentorship
        mentors.rate_mentorship = lambda *a, **k: {"ok": False, "error": "boom"}
        try:
            res = session_kit.session_feedback(hs_id, "me", 5)
        finally:
            mentors.rate_mentorship = real_rate
        self.assertFalse(res["ok"])
        # The session record was still persisted first.
        store = session_kit._load()
        self.assertIn(hs_id, store)
        self.assertIn("created_at", store[hs_id])
        # And no rating leaked into the mentor card.
        card = mentors._load_directory()[self.mentor_id]
        self.assertEqual(mentors._rating_summary(card)["mentee"]["count"], 0)

    # -- corruption: fail loud, never overwrite -----------------------------------------

    def test_corrupt_store_fails_loud_and_is_not_overwritten(self):
        """Reviewer's exact repro: corrupt sessions.json, then a write.
        Reads refuse loudly; the corrupt file is never overwritten."""
        hs_id = self._mutual_handshake()
        self.assertTrue(session_kit.add_note(hs_id, "me", "original note")["ok"])

        corrupt = b'{"unclosed": [garbage'
        session_kit.SESSIONS_FILE.write_bytes(corrupt)

        with self.assertLogs("job-apply-mcp.i07.session_kit", level="ERROR"):
            got = session_kit.get_notes(hs_id, "me")
        self.assertFalse(got["ok"])
        self.assertIn("corrupt", got["error"])

        write_res = session_kit.add_note(hs_id, "me", "new note after corruption")
        self.assertFalse(write_res["ok"])
        self.assertIn("corrupt", write_res["error"])

        # The file was NOT overwritten: original bytes intact, no data loss.
        self.assertEqual(session_kit.SESSIONS_FILE.read_bytes(), corrupt)

    def test_corrupt_store_blocks_session_read_paths(self):
        """Corrupt sessions.json: every path that reads the store refuses
        loudly (context_packet is unaffected — it reads the mentor card,
        not the session store)."""
        hs_id = self._mutual_handshake()
        session_kit.SESSIONS_FILE.write_text("{bad json", encoding="utf-8")
        for res in (
            session_kit.get_notes(hs_id, "me"),
            session_kit.followup_plan(hs_id, "me"),
            session_kit.session_feedback(hs_id, "me", 4),
        ):
            self.assertFalse(res["ok"])
            self.assertIn("corrupt", res["error"])

    def test_save_is_atomic_no_tmp_left(self):
        hs_id = self._mutual_handshake()
        self.assertTrue(session_kit.add_note(hs_id, "me", "atomic note")["ok"])
        self.assertFalse((session_kit.SESSIONS_FILE.parent / "sessions.json.tmp").exists())
        # File parses cleanly.
        data = json.loads(session_kit.SESSIONS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data[hs_id]["notes"][0]["text"], "atomic note")

    # -- context packet privacy --------------------------------------------------------

    def test_context_packet_strips_raw_ratings(self):
        """The packet must carry only rating_summary; the raw ratings dict
        (individual notes, by, at) never leaves the device."""
        hs_id = self._mutual_handshake()
        private_note = "PRIVATE-CANDID-NOTE-xyz123"
        session_kit.session_feedback(hs_id, "me", 2, note=private_note)
        # Confirm the raw rating really is on the card (test would be vacuous).
        card = mentors._load_directory()[self.mentor_id]
        self.assertIn("ratings", card)

        res = session_kit.context_packet(hs_id, "me")
        self.assertTrue(res["ok"])
        packet = res["packet"]
        self.assertNotIn("ratings", packet["mentor"])
        self.assertIn("rating_summary", packet["mentor"])
        # No individual note text, rater, or timestamp anywhere in the packet.
        dumped = json.dumps(packet)
        self.assertNotIn(private_note, dumped)

    def test_context_packet_has_no_ratings_key_anywhere(self):
        hs_id = self._mutual_handshake()
        session_kit.session_feedback(hs_id, self.mentor_id, 4, note="mentor note here")
        packet = session_kit.context_packet(hs_id, "me")["packet"]

        def walk(node):
            if isinstance(node, dict):
                self.assertNotIn("ratings", node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(packet)

    # -- block / quarantine gating --------------------------------------------------------

    def test_blocked_party_cannot_read_kit(self):
        """A block between the parties locks the whole kit even after mutual
        consent: packet, notes, agenda, follow-up, feedback are all refused."""
        hs_id = self._mutual_handshake()
        session_kit.add_note(hs_id, "me", "before block")
        self.assertTrue(safety.block_actor("me", self.mentor_id)["ok"])
        for res in (
            session_kit.context_packet(hs_id, "me"),
            session_kit.get_notes(hs_id, "me"),
            session_kit.build_agenda(hs_id, "me"),
            session_kit.followup_plan(hs_id, "me"),
            session_kit.session_feedback(hs_id, "me", 5),
        ):
            self.assertFalse(res["ok"])
            self.assertIn("block", res["error"])

    def test_blocked_by_other_party_also_refused(self):
        hs_id = self._mutual_handshake()
        self.assertTrue(safety.block_actor(self.mentor_id, "me")["ok"])
        res = session_kit.get_notes(hs_id, "me")
        self.assertFalse(res["ok"])
        self.assertIn("block", res["error"])

    def test_quarantined_mentor_cannot_read_kit(self):
        hs_id = self._mutual_handshake()
        safety.report("reporter-a", self.mentor_id, "spam")
        safety.report("reporter-b", self.mentor_id, "spam")
        self.assertTrue(safety.is_quarantined(self.mentor_id))
        res = session_kit.get_notes(hs_id, "me")
        self.assertFalse(res["ok"])
        self.assertIn("safety review", res["error"])
        res = session_kit.context_packet(hs_id, "me")
        self.assertFalse(res["ok"])

    def test_unblocked_party_regains_access(self):
        hs_id = self._mutual_handshake()
        safety.block_actor("me", self.mentor_id)
        self.assertFalse(session_kit.get_notes(hs_id, "me")["ok"])
        self.assertTrue(safety.unblock_actor("me", self.mentor_id)["ok"])
        self.assertTrue(session_kit.get_notes(hs_id, "me")["ok"])

    # -- scrub (audited, actor-gated erasure) ----------------------------------------------

    def test_scrub_requires_party_actor(self):
        hs_id = self._mutual_handshake()
        session_kit.add_note(hs_id, "me", "secret note")
        with self.assertRaises(PermissionError):
            session_kit.scrub_mentor_notes(self.mentor_id, actor="stranger")
        # Nothing was deleted.
        self.assertEqual(len(session_kit.get_notes(hs_id, "me")["notes"]), 1)

    def test_scrub_allowed_for_mentor_and_mentee(self):
        hs_id = self._mutual_handshake()
        session_kit.add_note(hs_id, "me", "secret note")
        n = session_kit.scrub_mentor_notes(self.mentor_id, actor=self.mentor_id)
        self.assertEqual(n, 1)
        self.assertEqual(session_kit.get_notes(hs_id, "me")["notes"], [])

        hs_id2 = self._mutual_handshake(mentee_id="other")
        session_kit.add_note(hs_id2, "other", "other note")
        n = session_kit.scrub_mentor_notes(self.mentor_id, actor="other")
        self.assertEqual(n, 1)

    def test_scrub_audits_each_deletion_and_leaves_rating_store(self):
        """Contract: session records deleted + audited; the mentor card's
        raw ratings store in mentors.json is explicitly NOT touched here."""
        hs_id = self._mutual_handshake()
        session_kit.add_note(hs_id, "me", "note one")
        session_kit.session_feedback(hs_id, "me", 5, note="candid note")

        before = [e for e in consent.audit_trail() if e["event"] == "session_notes_scrubbed"]
        n = session_kit.scrub_mentor_notes(self.mentor_id, actor=self.mentor_id)
        self.assertEqual(n, 1)
        after = [e for e in consent.audit_trail() if e["event"] == "session_notes_scrubbed"]
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1]["handshake_id"], hs_id)
        self.assertEqual(after[-1]["actor"], self.mentor_id)

        # Session record is gone...
        self.assertEqual(session_kit.get_notes(hs_id, "me")["notes"], [])
        # ...but the mentor-card rating store persists (explicit contract:
        # full erasure goes through safety.delete_mentor_data).
        card = mentors._load_directory()[self.mentor_id]
        self.assertIn("ratings", card)
        self.assertEqual(mentors._rating_summary(card)["mentee"]["count"], 1)

    def test_safety_delete_mentor_data_still_works_without_actor(self):
        """Back-compat: safety.delete_mentor_data calls scrub without an
        explicit actor; that internal path acts as the data subject."""
        hs_id = self._mutual_handshake()
        session_kit.add_note(hs_id, "me", "secret note")
        res = safety.delete_mentor_data(self.mentor_id, requested_by=self.mentor_id)
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"]["notes"], 1)
        # Full erasure: the card (ratings included) is gone too.
        self.assertNotIn(self.mentor_id, mentors._load_directory())


if __name__ == "__main__":
    unittest.main()
