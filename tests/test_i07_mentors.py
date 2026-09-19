#!/usr/bin/env python3
"""Tests for Initiative 07 mentors.py extensions: epic 1 (mentor profile
fields) and epic 2 (match questionnaire v2 + consent previews), plus the
WS3 blind-review fixes: hidden-contact sealing, exclusion threading, and
the safety choke point in matchmake.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import mentors  # noqa: E402
from initiatives.i07 import safety as safety_mod  # noqa: E402


def _profile(**over) -> dict:
    p = {
        "name": "Ada Lovelace",
        "industry": "software",
        "role": "Staff Engineer",
        "seniority": "ic5+",
        "topics": ["interviewing", "ai_skills"],
        "linkedin_url": "https://www.linkedin.com/in/ada-lovelace",
        "availability": "Fridays",
        "max_mentees": 3,
        "bio": "I help with interviews.",
    }
    p.update(over)
    return p


class _IsolatedMentors(unittest.TestCase):
    """Every test gets fresh mentor/safety stores; module globals restored."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = {
            "mentors_file": mentors.MENTORS_FILE,
            "outbox_dir": mentors.OUTBOX_DIR,
            "safety_file": safety_mod.SAFETY_FILE,
            "reports_file": safety_mod.REPORTS_FILE,
        }
        mentors.MENTORS_FILE = tmp / "mentors.json"
        mentors.OUTBOX_DIR = tmp / "outbox"
        safety_mod.SAFETY_FILE = tmp / "safety.json"
        safety_mod.REPORTS_FILE = tmp / "reports.jsonl"

    def tearDown(self):
        mentors.MENTORS_FILE = self._orig["mentors_file"]
        mentors.OUTBOX_DIR = self._orig["outbox_dir"]
        safety_mod.SAFETY_FILE = self._orig["safety_file"]
        safety_mod.REPORTS_FILE = self._orig["reports_file"]
        self._tmp.cleanup()

    def _answers(self, **over):
        a = {
            "industry": "software",
            "target_role": "Staff Engineer",
            "topics_ranked": ["interviewing"],
            "goal": "Break into staff engineering",
        }
        a.update(over)
        return a

    def _opt_in(self, own: bool = True, **over) -> dict:
        res = mentors.mentor_opt_in(_profile(**over), own=own)
        self.assertTrue(res["ok"], res)
        return res["card"]


class MentorProfileFieldsTest(_IsolatedMentors):
    def test_opt_in_accepts_new_fields(self):
        card = self._opt_in(
            availability_windows=[{"day": "Friday", "start": "14:00", "end": "16:00"}],
            boundaries={
                "offers": ["mock interviews"],
                "wont": ["referrals to my employer"],
                "note": "No weekends.",
            },
            preferred_contact="linkedin_public",
        )
        self.assertEqual(card["preferred_contact"], "linkedin_public")
        self.assertEqual(card["boundaries"]["wont"], ["referrals to my employer"])
        self.assertEqual(card["availability_windows"][0]["day"], "Friday")

    def test_preferred_contact_defaults_hidden(self):
        card = self._opt_in()
        self.assertEqual(card["preferred_contact"], "hidden")

    def test_bad_preferred_contact_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(preferred_contact="sms"))

    def test_bad_boundaries_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(boundaries={"offers": "not-a-list"}))
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(boundaries={"wont": [""]}))
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(boundaries={"note": "x" * 501}))

    def test_bad_availability_windows_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(availability_windows="fridays"))
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(availability_windows=[{"start": "9"}]))

    def test_card_edit_allows_new_fields(self):
        self._opt_in()
        res = mentors.my_mentor_card(
            {"boundaries": {"offers": ["career advice"], "wont": [], "note": ""}}
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["card"]["boundaries"]["offers"], ["career advice"])

    def test_seniority_none_raises_value_error(self):
        # None is invalid input, not an internal crash: ValueError, never
        # AttributeError.
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(seniority=None))


class QuestionnaireV2Test(_IsolatedMentors):
    def setUp(self):
        super().setUp()
        self._opt_in()

    def test_v2_fields_echoed(self):
        res = mentors.matchmake(
            self._answers(
                context="5y backend, no system design reps",
                urgency="this_week",
                expected_outcome="A system-design study plan",
            )
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["mentee_context"], "5y backend, no system design reps")
        self.assertEqual(res["mentee_urgency"], "this_week")
        self.assertEqual(res["mentee_expected_outcome"], "A system-design study plan")
        self.assertIn("urgent", res["guidance"].lower())

    def test_urgency_defaults_and_validates(self):
        res = mentors.matchmake(self._answers(urgency="ASAP"))
        self.assertEqual(res["mentee_urgency"], "exploring")

    def test_identity_preferences_voluntary_and_unranked(self):
        plain = mentors.matchmake(self._answers())
        with_prefs = mentors.matchmake(
            self._answers(identity_preferences={"prefer": ["women in eng"]})
        )
        self.assertEqual(
            [m["score"] for m in plain["matches"]],
            [m["score"] for m in with_prefs["matches"]],
        )
        self.assertEqual(
            with_prefs["identity_preferences"], {"prefer": ["women in eng"]}
        )
        self.assertIn("never used for ranking", with_prefs["guidance"])

    def test_consent_preview_redacts_contact(self):
        res = mentors.matchmake(self._answers(), consent_preview=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["consent_preview"])
        for m in res["matches"]:
            self.assertNotIn("linkedin_url", m)
            self.assertIn("topics", m)
            self.assertIn("mentor_id", m)

    def test_consent_preview_respects_linkedin_public(self):
        self._opt_in(
            own=False,
            linkedin_url="https://www.linkedin.com/in/grace-hopper",
            preferred_contact="linkedin_public",
        )
        res = mentors.matchmake(self._answers(), consent_preview=True)
        public = [m for m in res["matches"] if m.get("linkedin_url")]
        self.assertTrue(public, "linkedin_public mentor should show URL in preview")

    def test_default_mode_seals_hidden_contact(self):
        # The blind-review blocker: default matchmake() must NOT emit the
        # LinkedIn URL of a hidden mentor (preferred_contact='hidden' is the
        # default at opt-in).
        res = mentors.matchmake(self._answers())
        self.assertFalse(res["consent_preview"])
        self.assertTrue(res["matches"])
        for m in res["matches"]:
            self.assertNotIn("linkedin_url", m)
            self.assertEqual(m["preferred_contact"], "hidden")

    def test_default_mode_shows_public_contact(self):
        public_card = self._opt_in(
            own=False,
            name="GH",
            linkedin_url="https://www.linkedin.com/in/grace-hopper",
            preferred_contact="linkedin_public",
        )
        res = mentors.matchmake(self._answers())
        by_id = {m["mentor_id"]: m for m in res["matches"]}
        self.assertIn(public_card["id"], by_id)
        self.assertEqual(
            by_id[public_card["id"]]["linkedin_url"],
            "https://www.linkedin.com/in/grace-hopper",
        )

    def test_empty_directory_echoes_questionnaire_keys(self):
        # Empty directory: the return carries the questionnaire echo keys,
        # not just matches/guidance.
        mentors.MENTORS_FILE.write_text("{}", encoding="utf-8")
        res = mentors.matchmake(
            self._answers(context="ctx", urgency="this_week"),
            consent_preview=True,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["matches"], [])
        for key in (
            "mentee_goal",
            "mentee_context",
            "mentee_urgency",
            "mentee_expected_outcome",
            "identity_preferences",
            "consent_preview",
        ):
            self.assertIn(key, res, f"empty-dir return missing echo key {key!r}")
        self.assertEqual(res["mentee_context"], "ctx")
        self.assertTrue(res["consent_preview"])


class HiddenContactSealTest(_IsolatedMentors):
    def setUp(self):
        super().setUp()
        self.hidden_card = self._opt_in()
        self.public_card = self._opt_in(
            own=False,
            name="GH",
            linkedin_url="https://www.linkedin.com/in/grace-hopper",
            preferred_contact="linkedin_public",
        )

    def test_matchmake_seals_hidden_in_both_modes(self):
        for preview in (False, True):
            res = mentors.matchmake(self._answers(), consent_preview=preview)
            by_id = {m["mentor_id"]: m for m in res["matches"]}
            self.assertNotIn("linkedin_url", by_id[self.hidden_card["id"]])
            self.assertIn("linkedin_url", by_id[self.public_card["id"]])

    def test_connection_draft_seals_hidden_url(self):
        d = mentors.connection_draft(
            self.hidden_card["id"], "Alex", "get better at interviews"
        )
        self.assertTrue(d["ok"])
        self.assertNotIn("mentor_linkedin_url", d)
        self.assertTrue(d.get("contact_sealed"))
        self.assertIn("consent", d["instructions"].lower())
        # No URL string may leak anywhere in the payload.
        self.assertNotIn(
            "linkedin.com", json.dumps(d), "hidden mentor URL leaked in draft"
        )

    def test_connection_draft_public_keeps_url(self):
        d = mentors.connection_draft(
            self.public_card["id"], "Alex", "get better at interviews"
        )
        self.assertTrue(d["ok"])
        self.assertEqual(
            d["mentor_linkedin_url"], "https://www.linkedin.com/in/grace-hopper"
        )

    def test_publish_mentor_card_seals_url_on_write(self):
        # The hidden mentor is "me" for publishing: opt in as own card.
        mentors.mentor_opt_out(self.hidden_card["id"])
        own = mentors.mentor_opt_in(_profile(own=True))
        self.assertEqual(own["card"]["preferred_contact"], "hidden")

        res = mentors.publish_mentor_card(confirmed=True)
        self.assertTrue(res["published"])
        written = json.loads(Path(res["path"]).read_text(encoding="utf-8"))
        self.assertEqual(written["preferred_contact"], "hidden")
        self.assertNotIn(
            "linkedin_url", written,
            "published card declares hidden contact but leaks the URL",
        )

    def test_publish_preview_discloses_seal(self):
        mentors.mentor_opt_out(self.hidden_card["id"])
        mentors.mentor_opt_in(_profile(own=True))
        res = mentors.publish_mentor_card(confirmed=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["published"])
        self.assertNotIn("linkedin_url", res["preview"])
        self.assertIn("contact_note", res["preview"])


class ExclusionTest(_IsolatedMentors):
    def setUp(self):
        super().setUp()
        self.card = self._opt_in()
        self.mid = self.card["id"]

    def test_matchmake_honors_explicit_exclude_mentor_ids(self):
        res = mentors.matchmake(self._answers(), exclude_mentor_ids=[self.mid])
        self.assertTrue(res["ok"])
        self.assertNotIn(
            self.mid, [m["mentor_id"] for m in res["matches"]]
        )
        # ... while a plain call still surfaces them (no safety exclusion).
        res2 = mentors.matchmake(self._answers())
        self.assertIn(self.mid, [m["mentor_id"] for m in res2["matches"]])

    def test_choke_point_block_then_plain_matchmake(self):
        # THE WS2 choke point, verified empirically: block, then call plain
        # matchmake (no exclude_mentor_ids) — the blocked mentor is absent.
        safety_mod.block_actor("me", self.mid, reason="review repro")
        res = mentors.matchmake(self._answers())
        self.assertTrue(res["ok"])
        self.assertNotIn(self.mid, [m["mentor_id"] for m in res["matches"]])

    def test_choke_point_quarantine_then_plain_matchmake(self):
        safety_mod.report("r1", self.mid, "spam")
        safety_mod.report("r2", self.mid, "spam")
        self.assertTrue(safety_mod.is_quarantined(self.mid))
        res = mentors.matchmake(self._answers())
        self.assertNotIn(self.mid, [m["mentor_id"] for m in res["matches"]])

    def test_choke_point_opt_out(self):
        safety_mod.block_actor("me", self.mid, reason="review repro")
        res = mentors.matchmake(
            self._answers(), apply_safety_exclusions=False
        )
        self.assertIn(self.mid, [m["mentor_id"] for m in res["matches"]])

    def test_suggest_forwards_exclusions(self):
        seen: dict = {}

        def spy(answers, **kwargs):
            seen.update(kwargs)
            return {"ok": True, "matches": []}

        orig_match = mentors.matchmake
        orig_weak = mentors._training_weak_areas
        mentors.matchmake = spy  # type: ignore[assignment]
        mentors._training_weak_areas = lambda: (  # type: ignore[assignment]
            [{"area": "negotiation", "topics": ["salary_negotiation"]}],
            [],
            True,
        )
        try:
            mentors.suggest_mentors_from_training(exclude_mentor_ids=[self.mid])
        finally:
            mentors.matchmake = orig_match  # type: ignore[assignment]
            mentors._training_weak_areas = orig_weak  # type: ignore[assignment]
        self.assertEqual(seen.get("exclude_mentor_ids"), [self.mid])

    def test_direct_id_tools_reject_blocked_loudly(self):
        safety_mod.block_actor("me", self.mid, reason="review repro")
        for fn, args in (
            (mentors.connection_draft, (self.mid, "Alex", "goal")),
            (mentors.session_agenda, (self.mid, "goal")),
            (mentors.record_outreach, (self.mid,)),
        ):
            res = fn(*args)
            self.assertFalse(res["ok"], f"{fn.__name__} did not refuse")
            self.assertIn("blocked", res["error"])

    def test_direct_id_tools_reject_quarantined_loudly(self):
        safety_mod.report("r1", self.mid, "spam")
        safety_mod.report("r2", self.mid, "spam")
        for fn, args in (
            (mentors.connection_draft, (self.mid, "Alex", "goal")),
            (mentors.session_agenda, (self.mid, "goal")),
            (mentors.record_outreach, (self.mid,)),
        ):
            res = fn(*args)
            self.assertFalse(res["ok"], f"{fn.__name__} did not refuse")
            self.assertIn("quarantin", res["error"])

    def test_direct_id_tools_still_work_when_clean(self):
        self.assertTrue(
            mentors.connection_draft(self.mid, "Alex", "goal")["ok"]
        )
        self.assertTrue(mentors.session_agenda(self.mid, "goal")["ok"])
        self.assertTrue(mentors.record_outreach(self.mid)["ok"])


class _FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class ToolWiringTest(_IsolatedMentors):
    def setUp(self):
        super().setUp()
        self._opt_in()

    def test_register_tools_mentor_match_defaults_safe(self):
        mcp = _FakeMCP()
        mentors.register_tools(mcp)
        match_fn = mcp.tools["mentor_match"]
        res = match_fn("software", "Staff Engineer", ["interviewing"], "goal")
        self.assertTrue(res["ok"])
        self.assertTrue(
            res["consent_preview"], "mentor_match must default to the SAFE mode"
        )
        for m in res["matches"]:
            self.assertNotIn("linkedin_url", m)

    def test_register_tools_mentor_match_forwards_exclusions(self):
        mcp = _FakeMCP()
        mentors.register_tools(mcp)
        match_fn = mcp.tools["mentor_match"]
        card = self._opt_in(own=False, name="GH")
        res = match_fn(
            "software",
            "Staff Engineer",
            ["interviewing"],
            "goal",
            consent_preview=False,
            exclude_mentor_ids=[card["id"]],
        )
        ids = [m["mentor_id"] for m in res["matches"]]
        self.assertNotIn(card["id"], ids)

    def _run_cli_match(self, *argv) -> dict:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mentors.register_cli(sub)
        ns = parser.parse_args(["mentors", "match", *argv])
        buf = io.StringIO()
        with redirect_stdout(buf):
            mentors.cmd_mentors(ns)
        return json.loads(buf.getvalue())

    def test_cli_match_action_defaults_safe(self):
        res = self._run_cli_match(
            "--industry", "software",
            "--target-role", "Staff Engineer",
            "--topics", "interviewing",
            "--goal", "goal",
        )
        self.assertTrue(res["ok"])
        self.assertTrue(
            res["consent_preview"], "CLI match must default to the SAFE mode"
        )
        for m in res["matches"]:
            self.assertNotIn("linkedin_url", m)

    def test_cli_match_action_exclude_flag(self):
        card = self._opt_in(own=False, name="GH")
        res = self._run_cli_match(
            "--industry", "software",
            "--target-role", "Staff Engineer",
            "--topics", "interviewing",
            "--goal", "goal",
            "--exclude-mentor-ids", card["id"],
        )
        ids = [m["mentor_id"] for m in res["matches"]]
        self.assertNotIn(card["id"], ids)

    def test_cli_match_action_no_consent_preview_opt_out(self):
        public = self._opt_in(
            own=False,
            name="GH",
            linkedin_url="https://www.linkedin.com/in/grace-hopper",
            preferred_contact="linkedin_public",
        )
        res = self._run_cli_match(
            "--industry", "software",
            "--target-role", "Staff Engineer",
            "--topics", "interviewing",
            "--goal", "goal",
            "--no-consent-preview",
        )
        self.assertFalse(res["consent_preview"])
        by_id = {m["mentor_id"]: m for m in res["matches"]}
        self.assertEqual(
            by_id[public["id"]]["linkedin_url"],
            "https://www.linkedin.com/in/grace-hopper",
        )


if __name__ == "__main__":
    unittest.main()
