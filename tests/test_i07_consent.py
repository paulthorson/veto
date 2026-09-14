#!/usr/bin/env python3
"""Tests for the two-sided consent handshake (Initiative 07, epic 3).

Covers the proof of value: a match cannot become an introduction without
explicit consent from both people, and either person can withdraw.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import mentors  # noqa: E402
from initiatives.i07 import consent  # noqa: E402


def _mentor_profile(i: int = 0, **over) -> dict:
    p = {
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
    p.update(over)
    return p


class ConsentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        mentors.MENTORS_FILE = tmp / "mentors.json"
        consent.HANDSHAKES_FILE = tmp / "handshakes.json"
        consent.AUDIT_FILE = tmp / "consent_audit.jsonl"
        from initiatives.i07 import safety

        safety.SAFETY_FILE = tmp / "safety.json"
        safety.REPORTS_FILE = tmp / "reports.jsonl"
        safety.reset_rate_limits()
        self.safety = safety
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

    # -- request validation -------------------------------------------------

    def test_request_without_goal_is_rejected(self):
        res = consent.request_introduction(
            self.mentor_id, "me", "PT", "", synthetic=True
        )
        self.assertFalse(res["ok"])
        self.assertIn("goal", res["error"])

    def test_request_without_label_is_rejected(self):
        res = self._request(mentee_label="")
        self.assertFalse(res["ok"])

    def test_request_unknown_mentor_rejected(self):
        res = self._request(mentor_id="nope")
        self.assertFalse(res["ok"])

    def test_request_creates_awaiting_mentor(self):
        res = self._request()
        self.assertTrue(res["ok"])
        self.assertFalse(res["sent"])
        self.assertIn("Nothing was sent", res["note"])
        hs = res["handshake"]
        self.assertEqual(hs["state"], "awaiting_mentor")
        self.assertIsNotNone(hs["mentee_consented_at"])
        self.assertIsNone(hs["mentor_consented_at"])
        self.assertTrue(hs["id"].startswith("SYNTHETIC-"))

    def test_duplicate_pending_rejected(self):
        self.assertTrue(self._request()["ok"])
        res = self._request()
        self.assertFalse(res["ok"])
        self.assertIn("already pending", res["error"])

    def test_daily_rate_limit(self):
        ids = []
        for i in range(consent.MAX_REQUESTS_PER_DAY):
            mid = mentors.mentor_opt_in(_mentor_profile(i + 1))["card"]["id"]
            ids.append(mid)
            res = self._request(mentor_id=mid)
            self.assertTrue(res["ok"], res)
        mid = mentors.mentor_opt_in(_mentor_profile(99))["card"]["id"]
        res = self._request(mentor_id=mid)
        self.assertFalse(res["ok"])
        self.assertIn("rate limit", res["error"])

    # -- revelation is sealed until mutual -----------------------------------

    def test_reveal_refused_before_mutual(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        for actor in ("me", self.mentor_id):
            r = consent.reveal_contact(hs_id, actor)
            self.assertFalse(r["ok"], actor)
            self.assertIn("sealed", r["error"])

    def test_mentor_respond_requires_channel(self):
        res = self._request()
        r = consent.mentor_respond(res["handshake"]["id"], "approve", channel="")
        self.assertFalse(r["ok"])
        self.assertIn("channel", r["error"])

    def test_mentor_respond_bad_decision(self):
        res = self._request()
        r = consent.mentor_respond(
            res["handshake"]["id"], "maybe", channel="email"
        )
        self.assertFalse(r["ok"])

    def test_approve_reaches_mutual_and_unseals(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        r = consent.mentor_respond(hs_id, "approve", channel="linkedin_dm")
        self.assertTrue(r["ok"])
        self.assertEqual(r["handshake"]["state"], "mutual")
        self.assertIsNotNone(r["handshake"]["mentor_consented_at"])

        mentee_view = consent.reveal_contact(hs_id, "me")
        self.assertTrue(mentee_view["ok"])
        self.assertIn("linkedin_url", mentee_view["mentor_contact"])
        mentor_view = consent.reveal_contact(hs_id, self.mentor_id)
        self.assertTrue(mentor_view["ok"])
        self.assertIn("mentee_contact", mentor_view)

    def test_non_party_cannot_reveal(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        r = consent.reveal_contact(hs_id, "stranger")
        self.assertFalse(r["ok"])

    # -- decline + cooldown ---------------------------------------------------

    def test_decline_and_cooldown(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        r = consent.mentor_respond(hs_id, "decline", channel="email")
        self.assertTrue(r["ok"])
        self.assertEqual(r["handshake"]["state"], "declined")
        res2 = self._request()
        self.assertFalse(res2["ok"])
        self.assertIn("cooldown", res2["error"])

    # -- withdrawal: either party, any stage ----------------------------------

    def test_mentee_can_withdraw_pending(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        r = consent.withdraw(hs_id, "me", reason="changed my mind")
        self.assertTrue(r["ok"])
        self.assertEqual(r["handshake"]["state"], "withdrawn")

    def test_mentor_can_withdraw_after_mutual_and_seals_reveal(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        self.assertTrue(consent.reveal_contact(hs_id, "me")["ok"])
        r = consent.withdraw(hs_id, self.mentor_id)
        self.assertTrue(r["ok"])
        r2 = consent.reveal_contact(hs_id, "me")
        self.assertFalse(r2["ok"])
        self.assertIn("sealed", r2["error"])

    def test_withdraw_twice_rejected(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.withdraw(hs_id, "me")
        r = consent.withdraw(hs_id, "me")
        self.assertFalse(r["ok"])

    def test_non_party_cannot_withdraw(self):
        res = self._request()
        r = consent.withdraw(res["handshake"]["id"], "stranger")
        self.assertFalse(r["ok"])

    # -- expiry ----------------------------------------------------------------

    def test_approve_consumes_capacity(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        before = mentors.remaining_capacity(
            mentors._load_directory()[self.mentor_id]
        )
        consent.mentor_respond(hs_id, "approve", channel="email")
        after = mentors.remaining_capacity(
            mentors._load_directory()[self.mentor_id]
        )
        self.assertEqual(after, before - 1)

    def test_approve_refused_when_capacity_filled(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        # Fill the mentor's capacity out from under the pending request.
        directory = mentors._load_directory()
        directory[self.mentor_id]["mentee_count"] = 99
        mentors._save_directory(directory)
        r = consent.mentor_respond(hs_id, "approve", channel="email")
        self.assertFalse(r["ok"])
        self.assertIn("capacity", r["error"])

    def test_lazy_expiry_on_read(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        store = consent._load()
        past = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        store[hs_id]["expires_at"] = past
        consent._save(store)
        # No explicit expire_stale call: the read itself enforces the TTL.
        got = consent.get_handshake(hs_id, "me")
        self.assertEqual(got["handshake"]["state"], "expired")

    def test_expire_stale(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        store = consent._load()
        past = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        store[hs_id]["expires_at"] = past
        consent._save(store)
        out = consent.expire_stale()
        self.assertIn(hs_id, out["expired"])
        got = consent.get_handshake(hs_id, "me")
        self.assertEqual(got["handshake"]["state"], "expired")
        # Re-request allowed after expiry.
        res2 = self._request()
        self.assertTrue(res2["ok"], res2)

    # -- audit trail ------------------------------------------------------------

    def test_audit_trail_records_transitions(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        consent.withdraw(hs_id, "me")
        trail = consent.audit_trail(hs_id)
        events = [e["event"] for e in trail]
        self.assertEqual(events, ["request", "mentor_approve", "withdraw"])

    def test_audit_has_no_delete_api(self):
        for name in dir(consent):
            self.assertNotIn("delete", name.lower().replace("_audit", ""),
                             f"unexpected mutating audit API: {name}")

    # -- previews ----------------------------------------------------------------

    def test_redacted_preview_hides_contact_by_default(self):
        card = mentors._load_directory()[self.mentor_id]
        preview = consent.redacted_preview(card)
        self.assertNotIn("linkedin_url", preview)
        self.assertIn("topics", preview)

    def test_redacted_preview_respects_linkedin_public_opt_in(self):
        mid = mentors.mentor_opt_in(
            _mentor_profile(7, preferred_contact="linkedin_public")
        )["card"]["id"]
        card = mentors._load_directory()[mid]
        preview = consent.redacted_preview(card)
        self.assertIn("linkedin_url", preview)

    # -- blind-review KICK_BACK regression tests --------------------------------

    def test_preview_scrubs_contact_patterns_in_bio(self):
        # Blocker repro: a phone number in the bio was returned verbatim in
        # the preview, letting a party be contacted without mutual consent.
        card = mentors._load_directory()[self.mentor_id]
        card["bio"] = "Reach me anytime at +1-555-0199 or jane@example.com. See https://example.com/x"
        card["availability"] = "Fridays; call 555-1234 any time"
        card["reasons"] = "top match; email jane@example.com for details"
        preview = consent.redacted_preview(card)
        self.assertNotIn("+1-555-0199", preview["bio"])
        self.assertNotIn("jane@example.com", preview["bio"])
        self.assertNotIn("https://example.com/x", preview["bio"])
        self.assertNotIn("555-1234", preview["availability"])
        self.assertNotIn("jane@example.com", preview["reasons"])
        self.assertIn(consent.SCRUB_MARKER, preview["bio"])
        self.assertIn(consent.SCRUB_MARKER, preview["availability"])
        self.assertIn(consent.SCRUB_MARKER, preview["reasons"])
        # Non-contact prose survives the scrub.
        self.assertIn("Reach me anytime", preview["bio"])

    def test_preview_scrubs_list_fields_and_leaves_scalars(self):
        card = mentors._load_directory()[self.mentor_id]
        card["topics"] = ["interviewing", "text me at 555-9876 anytime"]
        card["remaining_capacity"] = 2
        card["score"] = 0.9
        preview = consent.redacted_preview(card)
        # Round-10: topics is a CLOSED vocabulary — a hostile entry that
        # is not a TOPICS member is dropped, not scrubbed. The contact
        # never reaches the preview either way.
        self.assertEqual(preview["topics"], ["interviewing"])
        self.assertNotIn("555-9876", str(preview["topics"]))
        self.assertEqual(preview["remaining_capacity"], 2)
        self.assertEqual(preview["score"], 0.9)

    def test_preview_drops_hostile_topics_entry(self):
        # A non-vocabulary topics entry is dropped by construction —
        # stronger than scrubbing: no hostile string survives at all.
        card = mentors._load_directory()[self.mentor_id]
        card["topics"] = ["jane@example.com", "interviewing", "555-0132"]
        preview = consent.redacted_preview(card)
        self.assertEqual(preview["topics"], ["interviewing"])

    def test_parse_ts_never_raises(self):
        self.assertIsNone(consent._parse_ts(None))
        self.assertIsNone(consent._parse_ts(12345))
        self.assertIsNone(consent._parse_ts("not-a-timestamp"))
        self.assertIsNotNone(consent._parse_ts("2026-09-13T00:00:00+00:00"))

    def test_corrupt_expires_at_fails_closed_not_crash(self):
        # Review probe: expires_at=None raised uncaught TypeError and
        # crashed expire_stale/get_handshake/mentor_respond/request_introduction.
        res = self._request()
        hs_id = res["handshake"]["id"]
        store = consent._load()
        store[hs_id]["expires_at"] = None
        consent._save(store)
        out = consent.expire_stale()          # must not raise
        self.assertTrue(out["ok"])
        self.assertIn(hs_id, out["expired"])
        got = consent.get_handshake(hs_id, "me")   # must not raise
        self.assertEqual(got["handshake"]["state"], "expired")

    def test_corrupt_updated_at_keeps_cooldown_enforced(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "decline", channel="email")
        store = consent._load()
        store[hs_id]["updated_at"] = None     # TypeError territory, pre-fix
        consent._save(store)
        res2 = self._request()                # must not crash...
        self.assertFalse(res2["ok"])           # ...and must stay refused
        self.assertIn("cooldown", res2["error"])

    def test_corrupt_created_at_counts_toward_rate_limit(self):
        for i in range(consent.MAX_REQUESTS_PER_DAY):
            mid = mentors.mentor_opt_in(_mentor_profile(i + 1))["card"]["id"]
            self.assertTrue(self._request(mentor_id=mid)["ok"])
        store = consent._load()
        for hs in store.values():
            hs["created_at"] = None            # unreadable timestamps...
        consent._save(store)
        mid = mentors.mentor_opt_in(_mentor_profile(99))["card"]["id"]
        res = self._request(mentor_id=mid)    # ...count as recent, no crash
        self.assertFalse(res["ok"])
        self.assertIn("rate limit", res["error"])

    def test_mentor_withdraw_of_pending_arms_cooldown(self):
        # Bypass repro: mentor WITHDRAW of a pending request used to let the
        # mentee re-request immediately, laundering the decline cooldown.
        res = self._request()
        hs_id = res["handshake"]["id"]
        r = consent.withdraw(hs_id, self.mentor_id, reason="too busy")
        self.assertTrue(r["ok"])
        self.assertEqual(r["handshake"]["state"], "withdrawn")
        res2 = self._request()
        self.assertFalse(res2["ok"])
        self.assertIn("cooldown", res2["error"])

    def test_mentee_withdraw_of_own_pending_does_not_arm_cooldown(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        r = consent.withdraw(hs_id, "me", reason="changed my mind")
        self.assertTrue(r["ok"])
        res2 = self._request()
        self.assertTrue(res2["ok"], res2)

    def test_withdraw_declined_or_expired_refused(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "decline", channel="email")
        r = consent.withdraw(hs_id, "me")
        self.assertFalse(r["ok"])
        self.assertIn("nothing to withdraw", r["error"])
        res2 = self._request(mentor_id=mentors.mentor_opt_in(_mentor_profile(5))["card"]["id"])
        hs_id2 = res2["handshake"]["id"]
        store = consent._load()
        past = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        store[hs_id2]["expires_at"] = past
        consent._save(store)
        consent.expire_stale()
        r2 = consent.withdraw(hs_id2, "me")
        self.assertFalse(r2["ok"])
        self.assertIn("nothing to withdraw", r2["error"])

    def test_audit_hash_chain_verifies(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        v = consent.verify_audit()
        self.assertTrue(v["ok"], v)
        self.assertGreaterEqual(v["checked"], 2)

    def test_audit_forged_line_detected(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        lines = consent.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        import json as _json

        forged = _json.loads(lines[-1])
        forged["detail"] = "forged: consent invented"   # edit, keep the hash
        lines[-1] = _json.dumps(forged)
        consent.AUDIT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        v = consent.verify_audit()
        self.assertFalse(v["ok"])
        self.assertEqual(v["failed_line"], len(lines))
        self.assertIn("altered", v["reason"])

    def test_audit_reordered_lines_detected(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        lines = consent.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)
        lines[0], lines[1] = lines[1], lines[0]
        consent.AUDIT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        v = consent.verify_audit()
        self.assertFalse(v["ok"])
        self.assertIn("prev_hash", v["reason"])

    def test_audit_hashless_line_rejected(self):
        res = self._request()
        self.assertTrue(res["ok"])
        with consent.AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write('{"event": "sneaky", "no_hashes": true}\n')
        v = consent.verify_audit()
        self.assertFalse(v["ok"])
        self.assertIn("no hash fields", v["reason"])

    def test_corrupt_store_fails_closed_loudly(self):
        self.assertTrue(self._request()["ok"])
        consent.HANDSHAKES_FILE.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(consent.CorruptStoreError):
            consent._load()
        res = consent.get_handshake("whatever", "me")
        self.assertFalse(res["ok"])
        self.assertIn("corrupt", res["error"])
        with self.assertRaises(consent.CorruptStoreError):
            consent.list_handshakes()
        # request_introduction also refuses instead of operating on garbage.
        res2 = self._request(mentor_id=mentors.mentor_opt_in(_mentor_profile(3))["card"]["id"])
        self.assertFalse(res2["ok"])
        self.assertIn("corrupt", res2["error"])

    def test_store_write_is_atomic(self):
        res = self._request()
        self.assertTrue(res["ok"])
        tmp_files = list(Path(consent.HANDSHAKES_FILE).parent.glob("*.tmp"))
        self.assertEqual(tmp_files, [])
        import json as _json

        _json.loads(consent.HANDSHAKES_FILE.read_text(encoding="utf-8"))

    def test_withdrawn_handshake_details_in_audit(self):
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.withdraw(hs_id, self.mentor_id, reason="capacity")
        trail = consent.audit_trail(hs_id)
        self.assertEqual(trail[-1]["event"], "withdraw")
        self.assertIn("cooldown_armed=1", trail[-1]["detail"])

    # -- fresh blind re-review (2026-09-13) regression tests -----------------

    def test_preview_scrubs_bare_domain_urls(self):
        # Fresh-blocker repro: _URL_RE required https?:// or www., so a
        # scheme-less domain like linkedin.com/in/jane-doe leaked verbatim.
        card = mentors._load_directory()[self.mentor_id]
        card["availability"] = "linkedin.com/in/jane-doe for fast replies"
        card["bio"] = "Find me at jane.me or my portfolio site.io/path please"
        card["reasons"] = "top match; see example.dev/work for portfolio"
        preview = consent.redacted_preview(card)
        for field, leaked in (
            ("availability", "linkedin.com/in/jane-doe"),
            ("bio", "jane.me"),
            ("bio", "site.io/path"),
            ("reasons", "example.dev/work"),
        ):
            self.assertNotIn(leaked, preview[field], f"{field} leaked {leaked}")
        self.assertIn(consent.SCRUB_MARKER, preview["availability"])
        self.assertIn(consent.SCRUB_MARKER, preview["bio"])
        # Non-contact prose survives.
        self.assertIn("for fast replies", preview["availability"])

    def test_preview_scrubs_bare_domain_but_keeps_linkedin_public_opt_in(self):
        # The linkedin_public opt-in URL must still pass through untouched.
        mid = mentors.mentor_opt_in(
            _mentor_profile(8, preferred_contact="linkedin_public",
                            bio="Also on linkedin.com/in/jane-doe")
        )["card"]["id"]
        card = mentors._load_directory()[mid]
        preview = consent.redacted_preview(card)
        self.assertNotIn("linkedin.com/in/jane-doe", preview["bio"])
        self.assertEqual(preview["linkedin_url"], card["linkedin_url"])

    def test_preview_scrubs_contact_patterns_in_structured_fields(self):
        # Fresh-blocker repro: PREVIEW_SCRUB_FIELDS covered only 5 fields;
        # name="jane.doe@example.com" and role="Call me: 555-0100" showed
        # verbatim in the preview via ordinary mentor_opt_in.
        mid = mentors.mentor_opt_in(
            _mentor_profile(9, name="jane.doe@example.com",
                            role="Call me: 555-0100")
        )["card"]["id"]
        card = mentors._load_directory()[mid]
        preview = consent.redacted_preview(card)
        self.assertNotIn("jane.doe@example.com", preview["name"])
        self.assertNotIn("555-0100", preview["role"])
        self.assertIn(consent.SCRUB_MARKER, preview["name"])
        self.assertIn(consent.SCRUB_MARKER, preview["role"])
        self.assertIn("Call me:", preview["role"])

    def test_preview_scrub_leaves_non_text_scalars_alone(self):
        card = mentors._load_directory()[self.mentor_id]
        card["remaining_capacity"] = 2
        card["score"] = 0.9
        preview = consent.redacted_preview(card)
        self.assertEqual(preview["remaining_capacity"], 2)
        self.assertEqual(preview["score"], 0.9)

    # -- blind re-review #2 (2026-09-13): uppercase + intl leaks -------

    def test_preview_scrubs_uppercase_bare_domains(self):
        # _URL_RE was compiled without re.IGNORECASE: LINKEDIN.COM and
        # LinkedIn.Com leaked verbatim pre-consent.
        card = mentors._load_directory()[self.mentor_id]
        card["bio"] = "Find me at LINKEDIN.COM/in/jane-doe today"
        card["availability"] = "or at LinkedIn.Com/in/jane-doe tomorrow"
        preview = consent.redacted_preview(card)
        self.assertNotIn("LINKEDIN.COM/in/jane-doe", preview["bio"])
        self.assertNotIn("LinkedIn.Com/in/jane-doe", preview["availability"])
        self.assertIn(consent.SCRUB_MARKER, preview["bio"])
        self.assertIn(consent.SCRUB_MARKER, preview["availability"])
        self.assertIn("today", preview["bio"])

    def test_preview_scrubs_uppercase_scheme_and_www(self):
        # Uppercase scheme (HTTPS://) and WWW. prefix also bypassed _URL_RE.
        card = mentors._load_directory()[self.mentor_id]
        card["bio"] = "see HTTPS://LINKEDIN.COM/in/jane-doe or WWW.LINKEDIN.COM/in/jane-doe"
        preview = consent.redacted_preview(card)
        self.assertNotIn("HTTPS://LINKEDIN.COM/in/jane-doe", preview["bio"])
        self.assertNotIn("WWW.LINKEDIN.COM/in/jane-doe", preview["bio"])
        self.assertEqual(preview["bio"].count(consent.SCRUB_MARKER), 2)

    def test_preview_scrubs_international_phone(self):
        # _PHONE_RE covered only US 10-digit / 7-digit; "+44 20 7946 0018"
        # leaked verbatim pre-consent.
        card = mentors._load_directory()[self.mentor_id]
        card["bio"] = "Call +44 20 7946 0018 now"
        preview = consent.redacted_preview(card)
        self.assertNotIn("+44 20 7946 0018", preview["bio"])
        self.assertIn(consent.SCRUB_MARKER, preview["bio"])
        self.assertIn("now", preview["bio"])
        # Ordinary numbers (years, quantities) must NOT be scrubbed.
        card2 = mentors._load_directory()[self.mentor_id]
        card2["bio"] = "I have 10 years of experience, shipped 2.0 in 2024"
        preview2 = consent.redacted_preview(card2)
        self.assertEqual(preview2["bio"], card2["bio"])
        self.assertNotIn(consent.SCRUB_MARKER, preview2["bio"])

    def test_preview_scrubs_nested_containers(self):
        # Defense in depth: dict values / nested lists in a scrubbed field
        # must not bypass the scrub loop. Not reachable via validated
        # mentor_opt_in; this pins the recursive behavior.
        card = mentors._load_directory()[self.mentor_id]
        card["reasons"] = {
            "note": "see LINKEDIN.COM/in/jane-doe",
            "alt": ["call +44 20 7946 0018", 42],
        }
        preview = consent.redacted_preview(card)
        self.assertNotIn("LINKEDIN.COM/in/jane-doe", preview["reasons"]["note"])
        self.assertNotIn("+44 20 7946 0018", preview["reasons"]["alt"][0])
        self.assertEqual(preview["reasons"]["alt"][1], 42)
        self.assertIn(consent.SCRUB_MARKER, preview["reasons"]["note"])

    def test_verify_audit_guarantee_limits_documented(self):
        # The chain attests the integrity of PRIOR entries. It is silent
        # against tail truncation and appended self-consistent forgery —
        # documented limits, not bugs. This test pins that behavior so a
        # future review cannot re-report it as a fresh blocker.
        res = self._request()
        hs_id = res["handshake"]["id"]
        consent.mentor_respond(hs_id, "approve", channel="email")
        lines = consent.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 2)

        # (a) Tail truncation: deleting the newest line verifies cleanly.
        consent.AUDIT_FILE.write_text(
            "\n".join(lines[:-1]) + "\n", encoding="utf-8"
        )
        v = consent.verify_audit()
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["checked"], len(lines) - 1)

        # (b) Appended self-consistent forgery: an attacker with fs write
        # access chains their own entry onto the (truncated) tail hash.
        import hashlib
        import json as _json

        cur_lines = consent.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        tail = _json.loads(cur_lines[-1])
        forged = {
            "at": "2026-09-13T23:59:59+00:00",
            "event": "mentor_approve",
            "handshake_id": "hs-forged",
            "actor": "attacker",
            "detail": "forged with valid hashes",
            "prev_hash": tail["entry_hash"],
        }
        forged["entry_hash"] = hashlib.sha256(
            consent._canonical(forged).encode("utf-8")
        ).hexdigest()
        with consent.AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(forged) + "\n")
        v2 = consent.verify_audit()
        self.assertTrue(v2["ok"], v2)

        # And the narrowed guarantee still holds: editing a prior entry is
        # detected.
        lines = consent.AUDIT_FILE.read_text(encoding="utf-8").splitlines()
        prior = _json.loads(lines[0])
        prior["detail"] = "edited after the fact"
        lines[0] = _json.dumps(prior)
        consent.AUDIT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        v3 = consent.verify_audit()
        self.assertFalse(v3["ok"])
        self.assertEqual(v3["failed_line"], 1)


    # -- blind re-review #3 (2026-09-13): intl phone shapes + new-gTLD domains --

    def test_preview_scrubs_international_phone_corpus(self):
        # The international alternative enumerated digit-group shapes
        # (\d{1,4} groups, 3-3-4 US) and failed open: India 5+5, UK
        # parenthesized, Brazil 2+5+4 leaked verbatim; Kenya mangled to
        # "+[redacted]56". The pattern is now shape-agnostic, anchored on
        # the leading "+" (or a parenthesized area code).
        corpus = [
            "India: +91 98765 43210.",
            "London: (020) 7946 0018.",
            "Brazil: +55 11 91234 5678.",
            "Nairobi: +254 722 123456.",
            "Call +44 20 7946 0018 now",
            "Call +1 555 0199 now",
            "Call +33 6 12 34 56 78",
            "Call +49 170 1234567",
            "Call +81 90 1234 5678",
        ]
        card = mentors._load_directory()[self.mentor_id]
        for text in corpus:
            card["bio"] = text
            preview = consent.redacted_preview(card)
            for token in ("+91", "98765", "020", "7946", "+55", "91234",
                          "+254", "722", "123456", "+44", "+33", "+49",
                          "+81", "170", "1234567"):
                if token in text:
                    self.assertNotIn(
                        token, preview["bio"],
                        f"leaked from {text!r}: {preview['bio']!r}",
                    )
            self.assertIn(consent.SCRUB_MARKER, preview["bio"], text)
        # The Kenya mangling must be gone: no digit tail survives.
        card["bio"] = "Nairobi: +254 722 123456."
        self.assertEqual(
            consent.redacted_preview(card)["bio"], "Nairobi: [redacted]."
        )
        # Ordinary numbers still untouched.
        for prose in (
            "10 years of experience, shipped 2.0 in 2024",
            "version 3.12",
            "100% remote, 40h/week",
            "score +5 in 2024",
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose)
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])

    def test_preview_scrubs_new_gtld_bare_domains(self):
        # The TLD allowlist omitted current gTLDs, so
        # "myportfolio.xyz" and "example.design/page" leaked verbatim.
        # Bare domains are now detected TLD-agnostically (letter-only
        # final label), so no allowlist can fail open again.
        card = mentors._load_directory()[self.mentor_id]
        card["bio"] = "see myportfolio.xyz for times"
        card["availability"] = "example.design/page has my calendar"
        preview = consent.redacted_preview(card)
        self.assertNotIn("myportfolio.xyz", preview["bio"])
        self.assertNotIn("example.design", preview["availability"])
        self.assertIn(consent.SCRUB_MARKER, preview["bio"])
        self.assertIn(consent.SCRUB_MARKER, preview["availability"])
        self.assertIn("for times", preview["bio"])
        # Version numbers and numeric dot-forms must NOT over-scrub.
        card["bio"] = "version 3.12, see section 4.5, shipped 2.0 in 2024"
        preview = consent.redacted_preview(card)
        self.assertEqual(preview["bio"], card["bio"])
        self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])

    # -- blind re-review #4 (2026-09-13): parenthesized single-group leak,
    # 00-prefix intl leak, signed-decimal over-scrub --

    def test_preview_scrubs_parenthesized_single_trailing_group(self):
        # Blocker 1: the parens alternative required >= 2 digit groups
        # after the parens and refused a missing space before "(".
        # "(555) 0199", "(020) 794600", "Call(020) 7946 0018" leaked
        # verbatim through redacted_preview.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "US parens 7-digit: (555) 0199",
            "London: (020) 794600.",
            "Call(020) 7946 0018",
            "London: (020) 7946 0018.",
        ):
            card["bio"] = text
            preview = consent.redacted_preview(card)
            for run in ("555", "0199", "020", "794600", "7946", "0018"):
                if run in text:
                    self.assertNotIn(
                        run, preview["bio"],
                        f"leaked from {text!r}: {preview['bio']!r}",
                    )
            self.assertIn(consent.SCRUB_MARKER, preview["bio"], text)
        # Guards: bare "(555)" and prose parens must NOT scrub; a year in
        # parens followed by a short count must NOT scrub either.
        for prose in (
            "the code (555) is just an area code",
            "call me (tomorrow) please",
            "founded in (2020) 5 people joined",
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose)
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])

    def test_preview_scrubs_double_zero_intl_prefix(self):
        # Blocker 2: the international alternative was anchored on "+"
        # only, so the ITU "00" prefix ("0044 20 7946 0018",
        # "0044.20.7946.0018") leaked verbatim, and "0049 170 1234567"
        # mangled to "0[redacted]567" (digits destroyed, not a clean
        # full match).
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "Zero-zero prefix: 0044 20 7946 0018",
            "0044.20.7946.0018",
            "Dial 0049 170 1234567 anytime",
        ):
            card["bio"] = text
            preview = consent.redacted_preview(card)
            for run in ("0044", "20", "7946", "0018", "0049", "170",
                        "1234567"):
                if run in text:
                    self.assertNotIn(
                        run, preview["bio"],
                        f"leaked from {text!r}: {preview['bio']!r}",
                    )
            self.assertIn(consent.SCRUB_MARKER, preview["bio"], text)
        # Clean full match: no digit tail may survive the marker.
        card["bio"] = "Dial 0049 170 1234567 anytime"
        self.assertEqual(
            consent.redacted_preview(card)["bio"], "Dial [redacted] anytime"
        )
        # Guards: short / non-phone "00" strings must NOT scrub.
        for prose in (
            "007",
            "00100",
            "000",
            "year 2000",
            "shipped in 2024",
            "100% remote",
            "version 2.0",
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose)
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])

    def test_preview_signed_decimals_not_over_scrubbed(self):
        # Minor: "+3.5" matched the intl alternative (+\d{1,4} + ".5"
        # group) and over-scrubbed to "rated [redacted] stars".
        card = mentors._load_directory()[self.mentor_id]
        for prose in (
            "rated +3.5 stars this year",
            "score +2.0",
            "v+1.5x model",
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose)
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])
        # Real international numbers still scrub.
        card["bio"] = "Call +1 (555) 123-4567 now"
        preview = consent.redacted_preview(card)
        self.assertNotIn("+1 (555) 123-4567", preview["bio"])
        self.assertIn(consent.SCRUB_MARKER, preview["bio"])

    # -- blind re-review #5 (2026-09-13): IDD separator leaks, missing
    # 011 branch, bare country-code leaks, alternation-order mangling --

    def _assert_fully_scrubbed(self, card, text, field="bio"):
        card[field] = text
        preview = consent.redacted_preview(card)
        got = preview[field]
        self.assertNotIn(text, got, f"leaked verbatim: {got!r}")
        for ch in got:
            self.assertFalse(
                ch.isdigit(),
                f"digit {ch!r} survived the scrub of {text!r}: {got!r}",
            )
        self.assertIn(consent.SCRUB_MARKER, got, text)

    def test_preview_scrubs_idd_prefix_with_separator(self):
        # Family A: the round-4 "00" branch required digits to immediately
        # follow the literal "00", so any separator between the IDD prefix
        # and the country code broke the branch and leaked verbatim.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "Call me: 00 44 20 7946 0018",
            "Call me: 00-44-20-7946-0018",
            "Call me: 00.44.20.7946.0018",
            "Call me: 00 (44) 20 7946 0018",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_scrubs_011_idd_prefix(self):
        # Family B: the NANP IDD prefix "011" (functional twin of "00")
        # had no branch at all and leaked verbatim.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "Call me: 011 44 20 7946 0018",
            "Call me: 011-44-20-7946-0018",
            "Call me: 011.44.20.7946.0018",
            "Call me: 011 (44) 20 7946 0018",
            "Call me: 011442079460018",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_scrubs_bare_country_code(self):
        # Family C: a complete dialable number with no IDD/"+ " prefix
        # (prepending "+" reaches the person) leaked verbatim.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "Call me: 44 20 7946 0018",
            "Call me: 44-20-7946-0018",
            "Call me: 44.20.7946.0018",
            "Call me: 1 415 555 0132",
            "Call me: 020 7946 0018",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_idd_alternation_leaves_no_digit_fragments(self):
        # The shorter 10-digit branch matched the PREFIX of longer IDD /
        # parenthesized numbers, leaving digit tails by the marker:
        # "00442079460018" -> "[redacted]0018", "(020)79460018" ->
        # "[redacted]8", "011442079460018" -> "[redacted]60018".
        # Longer/more-specific branches now win; the whole run scrubs.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "00442079460018",
            "(020)79460018",
            "011442079460018",
            "14155550132",
        ):
            card["bio"] = text
            self.assertEqual(
                consent.redacted_preview(card)["bio"],
                consent.SCRUB_MARKER,
                f"mangling survived for {text!r}",
            )

    def test_preview_r5_guards_stay_byte_identical(self):
        # The round-4 over-scrub guards must be unchanged by the new
        # IDD/bare-cc branches: short "00" strings, years, versions,
        # signed decimals, prose parens, and zip+4 stay untouched.
        card = mentors._load_directory()[self.mentor_id]
        for prose in (
            "007",
            "00100",
            "000",
            "year 2000",
            "shipped in 2024",
            "2024",
            "100% remote",
            "100% remote 40h/week",
            "version 2.0",
            "version 3.12",
            "2.0",
            "section 4.5",
            "Ph.D.",
            "M.D.",
            "score +5 in 2024",
            "rated +3.5 stars this year",
            "score +2.0",
            "v+1.5x model",
            "+3.5",
            "+2.0",
            "+1.5x",
            "the code (555) is just an area code",
            "(555)",
            "call me (tomorrow) please",
            "(tomorrow)",
            "founded in (2020) 5 people joined",
            "(2020) 5",
            "2000",
            "zip 94110-1234 here",
            "Room 101, floor 2, building 3, suite 4000",
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose, f"over-scrubbed: {prose!r}")
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])

    def test_preview_r5_leaks_scrubbed_in_every_field(self):
        # The leaks were demonstrated through bio; the scrub loop covers
        # every text-rendered field, so pin availability/reasons/topics/
        # rating_summary too.
        card = mentors._load_directory()[self.mentor_id]
        cases = (
            ("availability", "Call me: 011 44 20 7946 0018"),
            ("reasons", "Call me: 011-44-20-7946-0018"),
            ("topics", "Call me: 44 20 7946 0018"),
            ("rating_summary", "Call me: 44-20-7946-0018"),
        )
        for field, text in cases:
            self._assert_fully_scrubbed(card, text, field=field)

    def test_preview_scrubs_contiguous_digit_runs_round6(self):
        # Round-6 KICK_BACK: contiguous digit runs (copy-pasted from a
        # phone; mobile/WhatsApp dialing works with the "+" or "00"
        # prefix omitted) leaked verbatim for every length except 10.
        # The new 7-15 contiguous branch scrubs them with zero digits
        # surviving; 10-digit behavior is unchanged by construction.
        card = mentors._load_directory()[self.mentor_id]
        for field, text in (
            ("bio", "my number is 491712345678 call anytime"),      # +49 mobile
            ("availability", "my number is 5511912345678 call anytime"),  # BR
            ("reasons", "my number is 919876543210 call anytime"),  # IN
            ("topics", "my number is 442079460018 call anytime"),    # UK
            ("rating_summary", "my number is 254722123456 call anytime"),  # KE
            ("bio", "my number is 81234567 call anytime"),          # SG local
            ("availability", "my number is 5550132 call anytime"),  # 7-digit
            ("reasons", "call 77777777777 now"),                    # 11 digits
        ):
            self._assert_fully_scrubbed(card, text, field=field)

    def test_preview_contiguous_run_length_sweep(self):
        # 7-15 digit contiguous runs scrub (each length embedded in
        # prose); 1-6 digit runs survive untouched.
        card = mentors._load_directory()[self.mentor_id]
        for n in range(7, 16):
            self._assert_fully_scrubbed(card, f"reach me at {'1' * n} after six")
        for n in range(1, 7):
            text = f"call {'5' * n} now"
            card["bio"] = text
            self.assertEqual(consent.redacted_preview(card)["bio"], text)

    def test_preview_scrubs_extension_tails(self):
        # Extension digits glued to a number leaked behind the marker
        # ("415 555 0132 x123" -> "[redacted] x123"); the ext pass only
        # fires adjacent to an actual [redacted] marker, so prose like
        # "section 4.5 x2" or "extra 5" is never touched.
        card = mentors._load_directory()[self.mentor_id]
        for text, kept in (
            ("415 555 0132 x123", ""),
            ("call +1 (415) 555-0132 ext. 42 today", "today"),
            ("call 415-555-0132 EXT 7 ok", "ok"),
            ("my line 5550132x99 is direct", "is direct"),
        ):
            card["bio"] = text
            preview = consent.redacted_preview(card)["bio"]
            self.assertNotIn(text, preview, f"leaked verbatim: {preview!r}")
            for ch in preview:
                self.assertFalse(ch.isdigit(), f"digit {ch!r} survived: {preview!r}")
            self.assertIn(consent.SCRUB_MARKER, preview)
            if kept:
                self.assertIn(kept, preview)
        for prose in ("see section 4.5 x2 of the report", "for extra 5 points see appendix"):
            card["bio"] = prose
            self.assertEqual(consent.redacted_preview(card)["bio"], prose)

    def test_preview_scrubs_string_values_in_numeric_fields(self):
        # Defense in depth: remaining_capacity/score are now in the scrub
        # loop. Genuine scalars pass through unchanged; a contact string
        # stuffed into one (hand-edited store) is scrubbed like any field.
        card = mentors._load_directory()[self.mentor_id]
        card["remaining_capacity"] = "2 (call 555-0100)"
        card["score"] = "0.9 — jane@example.com"
        preview = consent.redacted_preview(card)
        self.assertNotIn("555-0100", preview["remaining_capacity"])
        self.assertNotIn("jane@example.com", preview["score"])
        self.assertIn(consent.SCRUB_MARKER, preview["remaining_capacity"])
        self.assertIn(consent.SCRUB_MARKER, preview["score"])

    # -- blind re-review #7 (2026-09-13): unicode-separator and
    # zero-width KICK_BACK. Every separator class was ASCII-only [-.\s],
    # so smart-punctuation dashes/dots split digit groups below the
    # 7-digit floor and full numbers leaked verbatim; zero-width
    # codepoints split digit runs while rendering adjacently. --

    def _assert_scrubbed_no_invisibles(self, card, text, field="bio"):
        # Like _assert_fully_scrubbed, plus: zero-width codepoints must be
        # consumed into the marker, never left surviving beside it.
        card[field] = text
        preview = consent.redacted_preview(card)
        got = preview[field]
        self.assertNotIn(text, got, f"leaked verbatim: {got!r}")
        for ch in got:
            self.assertFalse(
                ch.isdigit(), f"digit {ch!r} survived: {got!r}")
            self.assertNotIn(
                ch, "\u200b\u200c\u200d\ufeff\u00ad",
                f"invisible U+{ord(ch):04X} survived by the marker: {got!r}",
            )
        self.assertIn(consent.SCRUB_MARKER, got, text)

    def test_preview_scrubs_unicode_separators(self):
        # Blocker 1: smart-punctuation dashes/dots (iOS/macOS/Word
        # autocorrect) are routine input, not exotic — each must separate
        # digit groups exactly like its ASCII twin.
        card = mentors._load_directory()[self.mentor_id]
        for sep in (
            "\u2010",  # HYPHEN
            "\u2011",  # NON-BREAKING HYPHEN
            "\u2012",  # FIGURE DASH
            "\u2013",  # EN DASH
            "\u2014",  # EM DASH
            "\u2212",  # MINUS SIGN
            "\uff0d",  # FULLWIDTH HYPHEN-MINUS
            "\u00b7",  # MIDDLE DOT
            "\u2027",  # HYPHENATION POINT
        ):
            self._assert_scrubbed_no_invisibles(
                card, f"call 555{sep}0132 now")
        for text in (
            "call 415\u2013555\u20130132 now",          # en dashes, NANP
            "call +1\u2013415\u2013555\u20130132 now",  # +-branch dashes
            "call 0044\u201320\u20137946\u20130018 now",  # IDD dashes
            "call 415-555\u20130132 now",              # mixed ASCII + dash
            "call 00\u201344\u201320\u20137946\u20130018 now",  # IDD token
            "call 44\u201320\u20137946\u20130018 now",  # bare-cc dashes
            "call +44\u00b720\u00b77946\u00b70018 now",  # +-branch dots
            "call (415)\u2013555\u20130132 now",        # paren + dashes
        ):
            self._assert_scrubbed_no_invisibles(card, text)

    def test_preview_scrubs_zero_width_splits(self):
        # Blocker 2: zero-width/invisible codepoints split digit runs while
        # the preview renders the digits adjacently ("555\u200b0132"
        # displays as "5550132"). The invisible chars must be consumed
        # into the marker, not left behind.
        card = mentors._load_directory()[self.mentor_id]
        for zw in (
            "\u200b",  # ZERO WIDTH SPACE
            "\u200c",  # ZERO WIDTH NON-JOINER
            "\u200d",  # ZERO WIDTH JOINER
            "\ufeff",  # ZERO WIDTH NO-BREAK SPACE
            "\u00ad",  # SOFT HYPHEN
        ):
            self._assert_scrubbed_no_invisibles(
                card, f"call 555{zw}0132 now")
        # Zero-width inside a contiguous run of every length 7-15.
        for n in range(7, 16):
            digits = "5" * n
            self._assert_scrubbed_no_invisibles(
                card, f"num {digits[: n // 2]}\u200b{digits[n // 2:]} end")
        # Zero-width inside an IDD run.
        self._assert_scrubbed_no_invisibles(
            card, "call 0044\u200b20\u200b7946\u200b0018 now")
        # Prose around the redaction keeps its own unicode punctuation
        # byte-identical — only the number becomes the marker.
        card["bio"] = "hey \u2014 call 555\u200b0132 \u2014 bye"
        self.assertEqual(
            consent.redacted_preview(card)["bio"],
            "hey \u2014 call [redacted] \u2014 bye",
        )

    def test_preview_r7_guards_stay_byte_identical(self):
        # The widened separator classes only ADD codepoints; none of the
        # guards contain unicode separators or zero-width codepoints, so
        # they must stay byte-identical by construction.
        card = mentors._load_directory()[self.mentor_id]
        for prose in (
            "007",
            "00100",
            "000",
            "2000",
            "2024",
            "94110-1234",
            "+3.5",
            "+3\u00b75",
            "version 3.12",
            "section 4.5 x2",
            "extra 5",
            "(555)",
            "(tomorrow)",
            "(2020) 5",
            "Ph.D.",
            "M.D.",
            "100% remote",
            "e.g.",
            "i.e.",
            "Meet at 00 44 on 2024 05 13",
            "call 555\u200b01 now",  # 5 digits: below the 7 floor
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose, f"over-scrubbed: {prose!r}")
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])


    # -- blind re-review #8 (2026-09-13): CJK/fullwidth separators, separated
    # 2-group and 3+-group 7-15-digit runs, ext tails with non-space
    # separators, glued-1 7-digit local, word-glued 7-digit local --

    def test_preview_r8_cjk_separators_scrubbed(self):
        # Round-8 KICK_BACK B1: the CJK/fullwidth separator set
        # (U+3001 U+3002 U+FF61 U+FF64 U+FF0E U+FF0C U+FF1A) leaked every
        # branch verbatim. Pinned across all five text-rendered fields.
        card = mentors._load_directory()[self.mentor_id]
        seps = "、。，､．，："
        cases = [
            "call 415{0}555{0}0132 now",
            "call 0044{0}20{0}7946{0}0018 now",  # IDD
            "call +44{0}20{0}7946{0}0018 now",  # +-branch
            "call 020{0}7946{0}0018 now",  # bare-cc
            "call (020){0}7946{0}0018 now",  # paren
            "call 555{0}0132 now",  # 7-digit local
        ]
        texts = [t.format(s) for t in cases for s in seps]
        for field in (
            "bio", "availability", "reasons", "topics", "rating_summary"
        ):
            for text in texts:
                self._assert_fully_scrubbed(card, text, field=field)

    def test_preview_r8_two_group_runs_scrubbed(self):
        # Round-8 KICK_BACK B2: separated 2-group 7-15-digit runs in
        # groupings other than 3+4 / 3+3+4 leaked verbatim ("5555-0132"
        # is the standard 8-digit local format in SG/AU/HK, and the
        # contiguous branch already scrubs "55550132" — the separated
        # twin leaking was an internal contradiction). The whole run
        # including separators is consumed: no surviving-digit manglings.
        card = mentors._load_directory()[self.mentor_id]
        for n1, n2 in ((4, 4), (5, 4), (6, 4), (7, 4), (4, 5), (5, 5)):
            d1, d2 = "5" * n1, "0" * n2
            for text in (
                f"call {d1}-{d2} now",
                f"call {d1} {d2} now",
                f"call {d1}–{d2} now",
            ):
                self._assert_fully_scrubbed(card, text)
        # En-dash-split sweep, 7-15 digits (round-8 probe: 7-10 verbatim,
        # 11-15 mangled with surviving digits).
        for n in range(7, 16):
            d = "5" * n
            self._assert_fully_scrubbed(card, f"num {d[:4]}–{d[4:]} end")

    def test_preview_r8_three_group_runs_scrubbed(self):
        # Round-8 KICK_BACK B2, 3+-group family: bare-cc only covers
        # first groups of 1-3 digits, so "5555-555-0132" mangled to
        # "5555-[redacted]" (4 digits surviving by the marker).
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call 5555-555-0132 now",
            "call 5555 555 0132 now",
            "call 55555-555-0132 now",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r8_zip_plus_four_guard_holds(self):
        # The 5+4 shape is the canonical US ZIP+4: it stays byte-identical
        # UNLESS the second group has a leading zero (trunk-style local
        # part, e.g. "55555-0132"), which scrubs.
        card = mentors._load_directory()[self.mentor_id]
        for prose in ("94110-1234", "zip 94110-1234 here"):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose, f"over-scrubbed: {prose!r}")
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])
        self._assert_fully_scrubbed(card, "call 55555-0132 now")
        self._assert_fully_scrubbed(card, "call 55555-0000 now")

    def test_preview_r8_extension_tails_with_unicode_separators(self):
        # Round-8 KICK_BACK B3: _EXT_TAIL_RE only allowed \s between the
        # marker and the extension, so "— x123", "–x123", "×123" (U+00D7)
        # and zero-width-glued "x123" left extension digits surviving.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call 555-0132 — x123 now",
            "call 555-0132–x123 now",
            "call 555-0132 ×123 now",
            "call 555-0132\u200bx123 now",
            "call 555-0132 x 123 now",
            "call 555-0132、x123 now",
            "call 415 555 0132 ext. 99 now",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r8_glued_one_token_scrubbed(self):
        # Round-8 KICK_BACK B4: "1555-0132" (1 glued to a 7-digit local
        # with a separator, dialable as 1-555-0132) leaked verbatim while
        # "1-555-0132", "1 555 0132" and "15550132" all scrubbed.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call 1555-0132 now",
            "call 1-555-0132 now",
            "call 1 555 0132 now",
            "call 15550132 now",
            "call 1-415-555-0132 now",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r8_word_glued_seven_digit_scrubbed(self):
        # Round-8 KICK_BACK B5: the 7-digit branch used \b, so a word char
        # glued to the number ("a555-0132", CJK "电话555-0132" — realistic
        # in CJK bios with no space after "phone:") leaked verbatim while
        # the CJK-glued 10-digit was caught via (?<!\d).
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call 电话555-0132 now",
            "call a555-0132 now",
            "call\u200b555-0132 now",
            "call 电话415-555-0132 now",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r8_long_runs_consume_zero_width_residue(self):
        # Round-8 M1: a >15-digit run must never leave a digit fragment
        # by the marker ("4155550132<ZW>123456" scrubbed to
        # "[redacted]<ZW>123456", 6 digits surviving).
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call 4155550132\u200b123456 now",  # 16 digit-codepoints
            "call 4155550132123456 now",  # 16 contiguous digits
            "call 4155550132\u200b12345 now",  # 15: tail-consumed
            "call 5555-0132\u200b123456 now",
            "call 415-555-0132\u200b99 now",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r9_zw_inside_phone_groups_scrubbed(self):
        # Round-9 B1: a zero-width codepoint INSIDE a digit group
        # ("55\u200b55-0132" displays as "5555-0132") must not split the
        # group below the match floor — the whole number, including the
        # invisible char, is consumed into the marker.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "num 55\u200b55-0132 end",
            "num 5555-01\u200b32 end",
            "num 155\u200b5-0132 end",
            "num 55\u200c55-0132 end",  # ZW non-joiner
            "num 55\u200d55-0132 end",  # ZW joiner
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r9_zw_inside_bare_domain_scrubbed(self):
        # Round-9 B2: a zero-width codepoint inside a bare-domain label
        # ("linkedin\u200b.com") must not split the URL into a verbatim
        # leak — the whole domain, including the invisible char, is
        # consumed into the marker.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "see linkedin\u200b.com/in/jane-doe bye",
            "see linkedin.\u200bcom/in/jane-doe bye",
            "see link\u200bedin.com/in/jane-doe bye",
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r9_zw_inside_email_scrubbed(self):
        # Round-9 B3: a zero-width codepoint inside an email address
        # ("jane@example\u200b.com", "jane\u200bdoe@example.com",
        # "jane@\u200bexample.com") must not split the address into a
        # surviving name fragment beside the marker.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "contact jane@example\u200b.com bye",
            "contact jane\u200bdoe@example.com bye",
            "contact jane@\u200bexample.com bye",
            "contact j\u200ba\u200bne@example.com bye",  # multi-ZW
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r9_no_zw_adjacent_to_marker(self):
        # Round-9: no zero-width codepoint may survive adjacent to a
        # [redacted] marker — verified by codepoint, not just by digit
        # absence. ("call\u200b555-0132" must scrub to "call [redacted]",
        # not "call\u200b[redacted]".)
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call\u200b555-0132 now",
            "call 555-0132\u200b now",
            "see \u200blinkedin.com/in/x bye",
            "contact \u200bjane@example.com bye",
        ):
            card["bio"] = text
            got = consent.redacted_preview(card)["bio"]
            self.assertIn(consent.SCRUB_MARKER, got, text)
            for m in __import__("re").finditer(r"\[redacted\]", got):
                s, e = m.start(), m.end()
                if s > 0:
                    self.assertNotIn(
                        got[s - 1], "\u200b\u200c\u200d\ufeff\u00ad",
                        f"ZW before marker in {got!r} (from {text!r})",
                    )
                if e < len(got):
                    self.assertNotIn(
                        got[e], "\u200b\u200c\u200d\ufeff\u00ad",
                        f"ZW after marker in {got!r} (from {text!r})",
                    )

    def test_preview_r9_zw_phone_probes(self):
        # Round-9 invented probes: IDD, bare country code,
        # parenthesized, one-token, and CJK-glued shapes with ZW inside.
        card = mentors._load_directory()[self.mentor_id]
        for text in (
            "call 00 4\u200b4 20 7946 0018 now",  # IDD
            "call 44 20\u200b7946 0018 now",  # bare cc
            "call (02\u200b0) 7946 0018 now",  # paren
            "call 1-\u200b415-555-0132 now",  # one-token
            "call \u7535\u8bdd55\u200b55-0132 now",  # CJK glued
            "see https://exam\u200bple.com/page bye",  # schemed URL
        ):
            self._assert_fully_scrubbed(card, text)

    def test_preview_r9_zw_pathological_timing(self):
        # Round-9: no catastrophic backtracking. A ZW-bomb ("1" + 20000
        # ZWSPs) and 2000-group inputs must complete quickly (the ZW-bomb
        # timed out at 60s+ before the linear-time rewrite).
        card = mentors._load_directory()[self.mentor_id]
        import time

        for text in (
            "1" + "\u200b" * 20000,
            "\u200b" * 20000,
            "-".join(["12"] * 2000),
            "\u200b".join(["12"] * 2000),
        ):
            card["bio"] = text
            t0 = time.perf_counter()
            consent.redacted_preview(card)
            dt = time.perf_counter() - t0
            self.assertLess(
                dt, 5.0, f"pathological input took {dt:.2f}s: {text[:40]!r}"
            )

    def test_preview_r8_guards_stay_byte_identical(self):
        # The round-8 widenings only ADD matches for dialable shapes; the
        # full guard battery stays byte-identical, including the ZIP+4
        # guard against the new 2-group branch and the date guard
        # ("2024 05 13") against the new 3+-group branch.
        card = mentors._load_directory()[self.mentor_id]
        for prose in (
            "007",
            "00100",
            "000",
            "2000",
            "2024",
            "94110-1234",
            "zip 94110-1234 here",
            "+3.5",
            "+3·5",
            "+3。5",  # fullwidth decimal still cannot complete +-shape
            "version 3.12",
            "section 4.5 x2",
            "extra 5",
            "(555)",
            "(tomorrow)",
            "(2020) 5",
            "Ph.D.",
            "M.D.",
            "100% remote",
            "e.g.",
            "i.e.",
            "Meet at 00 44 on 2024 05 13",
            "call 555\u200b01 now",  # 5 digits: below the 7 floor
            "hey — call me — bye",
        ):
            card["bio"] = prose
            preview = consent.redacted_preview(card)
            self.assertEqual(preview["bio"], prose, f"over-scrubbed: {prose!r}")
            self.assertNotIn(consent.SCRUB_MARKER, preview["bio"])


    # -- round-10: normalize-then-detect rework -------------------------------

    def _bio_preview(self, text):
        card = mentors._load_directory()[self.mentor_id]
        card["bio"] = text
        return consent.redacted_preview(card)["bio"]

    def _assert_bio_scrubbed(self, text):
        import unicodedata

        got = self._bio_preview(text)
        self.assertIn(
            consent.SCRUB_MARKER, got, f"leaked verbatim: {text!r}"
        )
        self.assertNotIn(text, got, f"leaked verbatim: {text!r}")
        for ch in got:
            self.assertNotIn(
                unicodedata.category(ch),
                ("Cf", "Mn", "Me"),
                f"invisible U+{ord(ch):04X} survived: {got!r}",
            )
        return got

    def test_r10_unicode_idn_homoglyph_emails(self):
        # Adversary phase-1, family A: fullwidth/IDN/homoglyph emails.
        for text in (
            "reach me at josé@exämple.com for details",
            "email jane@münchen.de today",
            "contact jane@examρle.com now",  # Greek rho
            "see jane@example.οm today",  # Greek omicron
            "visit jane@example.c0m now",  # zero-for-o
            "mail jane@exámple.com please",
            "mail jane@example.сom now",  # Cyrillic es
            "mail jane@example.ϲom now",  # Greek lunate sigma
            "email jane＠example．com now",  # fullwidth @ and dot
            "email jane@example。com now",  # ideographic full stop
            "email jane＠example｡com now",  # halfwidth ideographic stop
            "write jane+work@example.com now",
            "find me jane.doe%40example.com",  # url-encoded @
        ):
            self._assert_bio_scrubbed(text)

    def test_r10_invisible_codepoints(self):
        # Adversary phase-1, family B: the WHOLE Cf/Mn/Me class, not a
        # hardcoded handful. No invisible may survive beside the marker.
        for text in (
            "call 555\u20600132 now",  # word joiner
            "call 555\u200e0132 now",  # LRM
            "call 555\u200f0132 now",  # RLM
            "call 555\u180e0132 now",  # Mongolian vowel separator
            "call 555\u20640132 now",  # invisible plus
            "call 555\ufe0e0132 now",  # variation selector
            "call 5\u200b5\u200b5\u200b0\u200b1\u200b3\u200b2 now",
            "jane\u2060doe@example.com",
            "jane@example\u200b.com",
            "jane@\u200bexample.com",
            "jane\u200b@example.com",
            "jane@example.com\u200b",
            "see linkedin\u200b.com/in/x",
            "see linkedin.\u200bcom/in/x",
            "j\u2064ane@example.com",  # invisible times
            "reach \U000e0020jane@example.com",  # tag char
        ):
            self._assert_bio_scrubbed(text)

    def test_r10_bidi_withholds_whole_field(self):
        # Adversary phase-1, family C: bidi OVERRIDES (U+202A-U+202E)
        # reorder VISUAL text, so span detection cannot see what the
        # reader sees — the whole field is withheld, not
        # character-scrubbed. (Round 10 narrowed this: the modern
        # isolate controls U+2066-U+2069 are the recommended mechanism
        # and no longer withhold — see
        # test_r10_bidi_isolates_scrub_normally.)
        for text in (
            "call 555-0132 \u202e",
            "nothing to hide \u202b here",
            "sneaky \u202a text",
        ):
            self.assertEqual(
                self._bio_preview(text), consent.SCRUB_MARKER, text
            )

    def test_r10_bidi_isolates_scrub_normally(self):
        # Round 10, A1: U+2066-U+2069 (bidi isolates) are the
        # Unicode-recommended mechanism, appear in legitimate RTL text,
        # and do not reorder hostilely — so they must NOT withhold the
        # field. Contact patterns are scrubbed normally around them.
        out = self._bio_preview("\u2066call 555-0132")
        self.assertNotIn("555-0132", out)
        self.assertIn(consent.SCRUB_MARKER, out)
        # Legitimate RTL text with isolates passes through untouched.
        rtl = "Based in \u2066\u0627\u0644\u0642\u0627\u0647\u0631\u0629\u2069, 10 years exp"
        self.assertEqual(self._bio_preview(rtl), rtl)

    def test_r10_prose_obfuscation(self):
        # Adversary phase-1, family D: human-readable obfuscation.
        for text in (
            "jane at example dot com",
            "jane[at]example[dot]com",
            "jane(at)example(dot)com",
            "email jane @ example . com",
            "see linkedin[.]com/in/x",
            "see linkedin(dot)com/in/x",
            "hxxp://linkedin[.]com/in/x",
            "hxxps://linkedin[.]com/in/x",
            "see linkedin(.)com/in/x",
            "see linkedin{.}com/in/x",
            "find example dot com slash jane",
            "ping linkedin dot com",
            "go to linked\u200bin[.]com",
            "see [linkedin](https://linkedin.com/in/x)",
        ):
            self._assert_bio_scrubbed(text)

    def test_r10_spelled_and_vanity_numbers(self):
        # Adversary phase-1, family E.
        for text in (
            "call five five five 0132",
            "dial five five five zero one three two",
            "call 1-800-FLOWERS today",
            "ring five 555-0132 now",
        ):
            self._assert_bio_scrubbed(text)

    def test_r10_messaging_handles(self):
        # Adversary phase-1, family F.
        for text in (
            "Telegram: @janedoe_dev",
            "Signal: @janedoe.42",
            "Skype me at live:janedoe123",
            "Discord: janedoe#1234",
            "ping @janedoe_dev on tg",
        ):
            self._assert_bio_scrubbed(text)

    def test_r10_structural_recursion_gaps(self):
        # Adversary phase-1, family G: dict keys and sets/frozensets.
        card = mentors._load_directory()[self.mentor_id]
        card["reasons"] = {
            "jane@example.com": "clean",
            "nested": {"deep": "call 555-0132"},
        }
        preview = consent.redacted_preview(card)
        self.assertNotIn("jane@example.com", str(preview["reasons"]))
        self.assertIn(consent.SCRUB_MARKER, str(preview["reasons"]))
        self.assertIn(consent.SCRUB_MARKER, preview["reasons"]["nested"]["deep"])

        card["reasons"] = {"s": "clean", "t": "call 555-0132"}
        preview = consent.redacted_preview(card)
        self.assertIn(consent.SCRUB_MARKER, str(preview["reasons"]))
        self.assertEqual(preview["reasons"]["t"], "call [redacted]")

    def test_r10_scrub_value_covers_sets_and_keys(self):
        out = consent._scrub_value({"jane@example.com", "call 555-0132", "clean"})
        self.assertTrue(isinstance(out, set))
        self.assertTrue(any(consent.SCRUB_MARKER in v for v in out))
        self.assertFalse(any("555-0132" in v for v in out))
        out = consent._scrub_value({"jane@example.com": "x"})
        self.assertIn(consent.SCRUB_MARKER, list(out)[0])

    def test_r10_misc_leaks(self):
        # Adversary phase-1, family H.
        for text in (
            "server at 192.168.0.1 down",
            "server at 10.0.0.1:8080 down",
            "mail jane\n@example.com",
            "call (0)20 7946 0018",
            "see jane_doe@example-site.com",
        ):
            self._assert_bio_scrubbed(text)

    def test_r10_extended_guards_stay_byte_identical(self):
        # Guards the adversary named explicitly, plus neighbors of the
        # new detectors: none may change by a single byte.
        for prose in (
            "ZIP 94110-1234 ok",
            "in 2024 we grew",
            "version 3.12 released",
            "call 911 now",
            "price $19.99 only",
            "met on 2024-05-13",
            "grew +3.5 percent",
            "delta -2.0 ok",
            "count 007 agents",
            "room 101 ok",
            "1,000,000 users",
            "meet @ noon",
            "meet @ home",
            "the dot-com bubble",
            "email me at jane. Thanks",
            "look at this. It is good",
            "section 4.5 x2",
            "v2.0 shipped",
            "2024 05 13 date",
            "00 44 code",
            "nine one one",
            "extra extent",
            "I ordered 1800 flowers",
            "telegram me at home",
            "relive:abc",
            "C# guide",
            "issue #123",
            "a.b",
            "the the the",
            "Ph.D.",
            "100% remote",
            "e.g. this",
        ):
            self.assertEqual(self._bio_preview(prose), prose, f"over-scrubbed: {prose!r}")

    def _write_block_state(self, blocker, blocked):
        import json

        self.safety.SAFETY_FILE.write_text(
            json.dumps(
                {
                    "blocked": {
                        blocker: {
                            blocked: {
                                "at": "2026-09-13T00:00:00+00:00",
                                "reason": "test",
                            }
                        }
                    },
                    "quarantined": {},
                }
            )
        )

    def test_r10_blocked_mentor_response_refused_pending(self):
        # UX MAJOR-2 / Contract s11: a blocked pair cannot answer a
        # handshake. The response is refused BEFORE any mutation or
        # capacity consumption; the handshake stays pending.
        res = self._request()
        hid = res["handshake"]["id"]
        before = mentors.remaining_capacity(
            mentors._load_directory()[self.mentor_id]
        )
        # Block directly in safety state (safety.block() would withdraw
        # the pending handshake itself; we need it still pending).
        self._write_block_state("me", self.mentor_id)
        for decision in ("approve", "decline"):
            res = consent.mentor_respond(hid, decision, channel="linkedin_dm")
            self.assertFalse(res["ok"], decision)
            self.assertIn("block", res["error"].lower(), decision)
        hs = consent.get_handshake(hid, "me")["handshake"]
        self.assertEqual(hs["state"], "awaiting_mentor")
        after = mentors.remaining_capacity(
            mentors._load_directory()[self.mentor_id]
        )
        self.assertEqual(before, after)
        # The refusal is audited.
        events = [e["event"] for e in consent.audit_trail(hid)]
        self.assertIn("respond_refused", events)

    def test_r10_corrupt_safety_refuses_response(self):
        # Fail closed: unreadable safety state refuses the response.
        res = self._request()
        hid = res["handshake"]["id"]
        self.safety.SAFETY_FILE.write_text("{not json")
        res = consent.mentor_respond(hid, "approve", channel="linkedin_dm")
        self.assertFalse(res["ok"])
        self.assertIn("unreadable", res["error"].lower())
        hs = consent.get_handshake(hid, "me")["handshake"]
        self.assertEqual(hs["state"], "awaiting_mentor")

    def test_r10_mutual_handshake_under_block_shows_safety_hold(self):
        # UX MAJOR-1: a displayed "mutual" handshake whose reads are
        # safety-sealed must say so instead of implying a live connection.
        res = self._request()
        hid = res["handshake"]["id"]
        ok = consent.mentor_respond(hid, "approve", channel="linkedin_dm")
        self.assertTrue(ok["ok"])
        self._write_block_state("me", self.mentor_id)
        view = consent.get_handshake(hid, "me")["handshake"]
        self.assertEqual(view["state"], "mutual")
        self.assertTrue(view.get("safety_hold"))
        self.assertIn("refused", view.get("safety_hold_note", ""))
        # And the seal itself still holds on reveal.
        rev = consent.reveal_contact(hid, "me")
        self.assertFalse(rev["ok"])

    def test_r10_post_mutual_withdraw_warns_about_session_access(self):
        # UX MAJOR-3: withdrawing after mutual says at confirm time that
        # session reads will refuse.
        res = self._request()
        hid = res["handshake"]["id"]
        self.assertTrue(consent.mentor_respond(hid, "approve", channel="x")["ok"])
        res = consent.withdraw(hid, "me", reason="done")
        self.assertTrue(res["ok"])
        self.assertIn("note", res)
        self.assertIn("session", res["note"].lower())
        # Withdrawing a pending request carries no such note.
        res2 = self._request(mentee_id="me2")
        res2 = consent.withdraw(res2["handshake"]["id"], "me2")
        self.assertTrue(res2["ok"])
        self.assertNotIn("note", res2)

    def test_r10_mentee_can_review_own_contact_path(self):
        # UX MINOR-4: the person who entered the data can review it.
        res = self._request()
        hid = res["handshake"]["id"]
        view = consent.get_handshake(hid, "me")["handshake"]
        self.assertIn("mentee_contact_path", view)
        # The mentor still cannot see it pre-consent.
        mview = consent.get_handshake(hid, self.mentor_id)["handshake"]
        self.assertNotIn("mentee_contact_path", mview)

    def test_r10_pathological_timing(self):
        # The new pipeline is linear-time: ZW-bombs, dash-runs, and
        # dot-runs complete far under the 5s budget.
        import time

        for text in (
            "1" + "\u200b" * 20000,
            "\u200b" * 20000,
            "-".join(["12"] * 2000),
            "\u200b".join(["12"] * 2000),
            "a." * 5000,
            "call 555" + "\u2060" * 20000 + "0132 now",
        ):
            t0 = time.perf_counter()
            consent._scrub_contact_patterns(text)
            dt = time.perf_counter() - t0
            self.assertLess(dt, 5.0, f"too slow ({dt:.2f}s): {text[:40]!r}")

    # -- round-10 adversary findings --------------------------------------

    def test_r10_confusable_folding_structural(self):
        # L1: folding is driven by UTS #39 confusables.txt (generated
        # data), not a hand-enumerated table. Each spoof below places
        # the homoglyph at the position of the letter it LOOKS like.
        for text in (
            "Telegra\u043c: janedoe",    # Cyrillic em -> m
            "sig\u043fal: @joe",         # Cyrillic en -> H
            "s\u043aype: janedoe",       # Cyrillic ka -> k
            "\u043dxxp://evil.com",      # Cyrillic en -> H (scheme)
            "li\u0475e:janedoe123",      # izhitsa -> v
            "\u04cfive:janedoe",         # palochka -> l
            "dis\u03f2ord: janedoe",     # lunate sigma -> c
            "disc\u0585rd: janedoe",     # Armenian oh -> o
            "t\u0261: janedoe",          # script g -> g
            "jane at example \u0501ot com",  # Komi de -> d
            "\u0455ignal: @janedoe",     # dze -> s
            # Not among the 10 probe codepoints: the table is the whole
            # confusables file, not an enumeration.
            "\U0001d413elegram: janedoe",  # math bold T -> T
            "\u24e3elegram: @joe",         # circled t -> t
        ):
            got = self._assert_bio_scrubbed(text)
            self.assertNotIn("janedoe", got.lower())
            self.assertNotIn("@joe", got)

    def test_r10_seq_fold_after_normalization(self):
        # L2: "%40" folding runs AFTER NFKD + invisible-strip, so a
        # fullwidth percent or a ZWSP-split sequence still rejoins.
        for text in (
            "jane\uff0540example.com",
            "jane%\u200b40example.com",
        ):
            got = self._assert_bio_scrubbed(text)
            self.assertNotIn("example.com", got)

    def test_r10_blank_separators(self):
        # L3: braille blanks / Hangul fillers render as spaces but are
        # not Cf/Mn/Me and not \s — treated as separators.
        for text in (
            "Call 555\u28000132",
            "jane\u2800at\u2800example\u2800dot\u2800com",
            "Call 555\u31640132",
            "Call 555\u115f0132",
        ):
            got = self._assert_bio_scrubbed(text)
            self.assertNotIn("0132", got)

    def test_r10_bare_domain_with_port(self):
        # L4: a :port does not stop a bare domain from being a URL.
        for text in (
            "Visit example.com:8080",
            "see example.com:8080/path",
        ):
            got = self._assert_bio_scrubbed(text)
            self.assertNotIn("example.com", got)

    def test_r10_optin_url_with_contact_fails_closed(self):
        # L5: the linkedin_public opt-in covers the URL, not a contact
        # smuggled in its query string or path.
        base = {
            "id": "m-x",
            "name": "X",
            "preferred_contact": "linkedin_public",
        }
        for url in (
            "https://linkedin.com/in/jane?x=jane@example.com",
            "https://linkedin.com/in/jane@example.com",
        ):
            out = consent.redacted_preview({**base, "linkedin_url": url})[
                "linkedin_url"
            ]
            self.assertEqual(out, consent.SCRUB_MARKER, url)
        good = "https://linkedin.com/in/janedoe"
        self.assertEqual(
            consent.redacted_preview({**base, "linkedin_url": good})[
                "linkedin_url"
            ],
            good,
        )

    def test_r10_vanity_without_separators(self):
        # L6: letter-glued and space-separated vanity forms scrub; the
        # lowercase quantity ("1800 flowers delivered") stays verbatim.
        for text in (
            "Call 1800FLOWERS",
            "Call 1-800FLOWERS",
            "Call 1 800 FLOWERS",
            "Call 1-800-FLOWERS",
        ):
            got = self._assert_bio_scrubbed(text)
            self.assertNotIn("flowers", got.lower())
        self.assertEqual(
            self._bio_preview("1800 flowers delivered"),
            "1800 flowers delivered",
        )

    def test_r10_handles_short_cjk_nameis_wechat(self):
        # L7: 4-char bare handles, CJK-adjacent @handles, "name is"
        # proximity, and the wechat keyword. Guards hold.
        for text in (
            "ping @jane",
            "\u5fae\u4fe1@janedoe_dev",
            "my skype name is janedoe",
            "WeChat: janedoe2024",
            "Telegram: @joe",
        ):
            self._assert_bio_scrubbed(text)
        self.assertEqual(self._bio_preview("meet @ noon"), "meet @ noon")
        self.assertEqual(
            self._bio_preview("telegram me at home"), "telegram me at home"
        )

    def test_r10_phone_slash_ext_tails(self):
        # L8: "/" separators, "#" extensions, full "-digits" tails.
        got = self._assert_bio_scrubbed("Call 415/555/0132")
        self.assertNotIn("0132", got)
        got = self._assert_bio_scrubbed("Call 555-0132 #456")
        self.assertNotIn("#456", got)
        self.assertNotIn("0132", got)
        got = self._assert_bio_scrubbed("Call 555-0132 x12-34")
        self.assertNotIn("-34", got)
        self.assertNotIn("0132", got)
        self._assert_bio_scrubbed("five/five/five/oh/one/three/two")
        self.assertEqual(
            self._bio_preview("section 4.5 x2"), "section 4.5 x2"
        )

    def test_r10_digit_shadow_in_prose_detectors(self):
        # L9: the 0->o / 4->a shadow feeds the at/dot + obfuscated-dot
        # detectors (it must not live in the fold table, where it would
        # corrupt digit detection).
        for text in (
            "jane at example d0t com",
            "jane 4t example dot com",
        ):
            got = self._assert_bio_scrubbed(text)
            self.assertNotIn("example", got)

    def test_r10_container_keys_stay_hashable(self):
        # R1: scrubbing never produces unhashable dict keys or set
        # members — no TypeError crash (DoS).
        out = consent._scrub_value({("a", "b"): "x"})
        self.assertEqual(out, {("a", "b"): "x"})
        hash(next(iter(out)))
        out = consent._scrub_value(frozenset({("a",)}))
        self.assertEqual(out, frozenset({("a",)}))
        out = consent._scrub_value(frozenset({"coaching", "jane@example.com"}))
        self.assertEqual(out, frozenset({"coaching", consent.SCRUB_MARKER}))
        out = consent._scrub_value({("a", "jane@example.com"): "x"})
        key = next(iter(out))
        hash(key)
        self.assertNotIn("jane@example.com", str(key))

    def test_r10_deep_nesting_and_bytes(self):
        # R2: depth-capped recursion (no RecursionError DoS); bytes
        # carrying contacts fail closed, clean bytes pass through.
        deep = "jane@example.com"
        for _ in range(3000):
            deep = [deep]
        consent._scrub_value(deep)  # must not raise
        self.assertEqual(
            consent._scrub_value(b"jane@example.com"), b"[redacted]"
        )
        self.assertEqual(consent._scrub_value(b"nothing here"), b"nothing here")
        self.assertEqual(
            consent._scrub_value(b"\xff\xfe invalid"), b"\xff\xfe invalid"
        )

    def test_r10_null_party_id_fails_closed(self):
        # G8: a hand-edited store with a null party id must not let
        # str(None)=="None" sail through the block check.
        res = self._request()
        hid = res["handshake"]["id"]
        store = consent._load()
        store[hid]["mentee_id"] = None
        consent._save(store)
        r = consent.mentor_respond(hid, "approve", channel="email")
        self.assertFalse(r["ok"])
        self.assertIn("party id", r["error"])
        # Same gate on the reveal path.
        store[hid]["state"] = "mutual"
        consent._save(store)
        r = consent.reveal_contact(hid, self.mentor_id)
        self.assertFalse(r["ok"])
        self.assertIn("party id", r["error"])

    # -- blind re-review #11 (2026-09-14): bidi-override gate on the
    # linkedin_public opt-in URL. The §3 bidi fail-closed gate lived
    # only in the free-text scrub path: a bidi-wrapped reversed contact
    # in linkedin_url passed the opt-in prefix match and the contact
    # checks on the logical-order string, so the preview returned it
    # verbatim — while the reader SEES the contact. The opt-in URL now
    # fails closed on bidi overrides before the LinkedIn match.

    def test_r11_optin_url_with_rlo_override_withheld(self):
        # Blocker repro: "https://linkedin.com/in/\u202e" + reversed
        # "jane@example.com" + "\u202c" passed every logical-order check
        # and leaked — the reader SEES the email.
        base = {
            "id": "m-x",
            "name": "X",
            "preferred_contact": "linkedin_public",
        }
        url = "https://linkedin.com/in/\u202e" + "jane@example.com"[::-1] + "\u202c"
        out = consent.redacted_preview({**base, "linkedin_url": url})[
            "linkedin_url"
        ]
        self.assertEqual(out, consent.SCRUB_MARKER, repr(url))
        self.assertNotIn("jane@example.com", out)

    def test_r11_optin_url_with_lre_override_withheld(self):
        # The LRE (U+202A) variant leaked identically pre-fix.
        base = {
            "id": "m-x",
            "name": "X",
            "preferred_contact": "linkedin_public",
        }
        url = "https://linkedin.com/in/\u202a" + "jane@example.com"[::-1] + "\u202c"
        out = consent.redacted_preview({**base, "linkedin_url": url})[
            "linkedin_url"
        ]
        self.assertEqual(out, consent.SCRUB_MARKER, repr(url))
        self.assertNotIn("jane@example.com", out)

    def test_r11_optin_url_without_bidi_still_passes(self):
        # No regression on the legitimate opt-in path: a URL with no
        # bidi overrides and no contact payload passes through.
        base = {
            "id": "m-x",
            "name": "X",
            "preferred_contact": "linkedin_public",
        }
        for url in (
            "https://linkedin.com/in/janedoe",
            "https://www.linkedin.com/in/jane-doe-123",
        ):
            out = consent.redacted_preview({**base, "linkedin_url": url})[
                "linkedin_url"
            ]
            self.assertEqual(out, url, repr(url))


if __name__ == "__main__":
    unittest.main()
