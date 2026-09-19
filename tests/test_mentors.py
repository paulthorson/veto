#!/usr/bin/env python3
"""Offline tests for the mentor matchmaking module."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import mentors


def _profile(**overrides):
    base = {
        "name": "Ada L.",
        "industry": "fintech",
        "role": "Senior Backend Engineer",
        "seniority": "ic4",
        "topics": ["interviewing", "career_switch"],
        "linkedin_url": "https://www.linkedin.com/in/adala",
        "availability": "2 calls/month",
        "max_mentees": 2,
        "bio": "Staff SWE who has done 200+ interviews.",
    }
    base.update(overrides)
    return base


class _TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_file = mentors.MENTORS_FILE
        self._orig_outbox = mentors.OUTBOX_DIR
        mentors.MENTORS_FILE = Path(self.tmp.name) / "mentors.json"
        mentors.OUTBOX_DIR = Path(self.tmp.name) / "outbox"

    def tearDown(self):
        mentors.MENTORS_FILE = self._orig_file
        mentors.OUTBOX_DIR = self._orig_outbox
        self.tmp.cleanup()


class OptInTests(_TempStore):
    def test_opt_in_round_trip(self):
        res = mentors.mentor_opt_in(_profile())
        self.assertTrue(res["ok"])
        card = res["card"]
        self.assertTrue(card["id"].startswith("mentor-"))
        self.assertEqual(card["mentee_count"], 0)
        self.assertTrue(card["own"])
        disk = json.loads(mentors.MENTORS_FILE.read_text())
        self.assertIn(card["id"], disk)

    def test_initials_allowed_for_privacy(self):
        res = mentors.mentor_opt_in(_profile(name="A.L."))
        self.assertTrue(res["ok"])
        self.assertEqual(res["card"]["name"], "A.L.")

    def test_bad_linkedin_url_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(linkedin_url="https://example.com/ada"))

    def test_unknown_topic_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(topics=["mind_reading"]))

    def test_empty_topics_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(topics=[]))

    def test_non_positive_max_mentees_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(max_mentees=0))

    def test_missing_field_rejected(self):
        p = _profile()
        del p["industry"]
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(p)

    def test_bad_seniority_rejected(self):
        with self.assertRaises(ValueError):
            mentors.mentor_opt_in(_profile(seniority="guru"))

    def test_opt_out_removes_own_card(self):
        mentors.mentor_opt_in(_profile())
        res = mentors.mentor_opt_out()
        self.assertTrue(res["ok"])
        self.assertEqual(mentors._load_directory(), {})

    def test_opt_out_without_card(self):
        res = mentors.mentor_opt_out()
        self.assertFalse(res["ok"])

    def test_card_show_and_edit(self):
        mentors.mentor_opt_in(_profile())
        shown = mentors.my_mentor_card()
        self.assertTrue(shown["ok"])
        edited = mentors.my_mentor_card({"availability": "1 call/week"})
        self.assertEqual(edited["card"]["availability"], "1 call/week")
        bad = lambda: mentors.my_mentor_card({"topics": ["nope"]})
        self.assertRaises(ValueError, bad)

    def test_card_without_opt_in(self):
        self.assertFalse(mentors.my_mentor_card()["ok"])


class MatchmakeTests(_TempStore):
    def setUp(self):
        super().setUp()
        self.ada_id = mentors.mentor_opt_in(_profile())["card"]["id"]
        self.bob_id = mentors.mentor_opt_in(
            _profile(
                name="B.O.",
                industry="healthcare",
                role="Data Scientist",
                seniority="ic5+",
                topics=["ai_skills", "networking"],
                linkedin_url="https://www.linkedin.com/in/bob-data",
                max_mentees=1,
            ),
            own=False,
        )["card"]["id"]

    def test_ranks_best_match_first(self):
        res = mentors.matchmake({
            "industry": "fintech",
            "target_role": "Backend Engineer",
            "topics_ranked": ["career_switch", "interviewing", "networking"],
            "goal": "land a fintech role by December",
        })
        self.assertTrue(res["ok"])
        self.assertEqual(res["matches"][0]["mentor_id"], self.ada_id)
        self.assertLessEqual(res["matches"][0]["score"], 100)
        self.assertGreaterEqual(res["matches"][-1]["score"], 0)
        reasons = res["matches"][0]["reasons"]
        self.assertTrue(any("fintech" in r.lower() or "industry" in r for r in reasons))

    def test_rank_weighted_topics(self):
        career_first = mentors.matchmake({
            "industry": "fintech", "target_role": "",
            "topics_ranked": ["career_switch", "networking"],
            "goal": "g",
        })
        network_first = mentors.matchmake({
            "industry": "healthcare", "target_role": "",
            "topics_ranked": ["networking", "career_switch"],
            "goal": "g",
        })

        def score(res, mid):
            return next(m for m in res["matches"] if m["mentor_id"] == mid)["score"]

        # Career-switch prioritized: Ada (fintech + career_switch) beats Bob.
        self.assertGreater(score(career_first, self.ada_id),
                           score(career_first, self.bob_id))
        # Networking prioritized + healthcare: Bob beats Ada.
        self.assertGreater(score(network_first, self.bob_id),
                           score(network_first, self.ada_id))
        # Ada's own score drops when her priority topic moves down the ranking.
        self.assertGreater(score(career_first, self.ada_id),
                           score(network_first, self.ada_id))

    def test_full_capacity_mentor_never_surfaced(self):
        mentors.record_outreach(self.bob_id)  # bob max_mentees=1 -> full
        res = mentors.matchmake({
            "industry": "healthcare", "target_role": "Data Scientist",
            "topics_ranked": ["ai_skills"], "goal": "g",
        })
        ids = [m["mentor_id"] for m in res["matches"]]
        self.assertNotIn(self.bob_id, ids)

    def test_empty_directory_guidance(self):
        with tempfile.TemporaryDirectory() as other:
            mentors.MENTORS_FILE = Path(other) / "empty.json"
            res = mentors.matchmake({"industry": "x", "target_role": "y",
                                     "topics_ranked": [], "goal": "g"})
            self.assertEqual(res["matches"], [])
            self.assertIn("guidance", res)

    def test_role_proximity_partial_credit(self):
        close = mentors.matchmake({
            "industry": "", "target_role": "Senior Backend Engineer",
            "topics_ranked": [], "goal": "g",
        })
        far = mentors.matchmake({
            "industry": "", "target_role": "Registered Nurse",
            "topics_ranked": [], "goal": "g",
        })
        close_ada = next(m for m in close["matches"] if m["mentor_id"] == self.ada_id)
        far_ada = next(m for m in far["matches"] if m["mentor_id"] == self.ada_id)
        self.assertGreater(close_ada["score"], far_ada["score"])


class ConnectionDraftTests(_TempStore):
    def setUp(self):
        super().setUp()
        self.ada_id = mentors.mentor_opt_in(_profile())["card"]["id"]

    def test_draft_under_300_chars_and_labeled(self):
        res = mentors.connection_draft(
            self.ada_id, "Alex T",
            "land my first senior backend role at a fintech company",
        )
        self.assertTrue(res["ok"])
        self.assertIn("DRAFT", res["status"])
        self.assertLessEqual(len(res["note"]), 300)
        self.assertEqual(res["note_length"], len(res["note"]))
        # Hidden contact (the default): the URL is sealed until mutual
        # consent — never returned here.
        self.assertNotIn("mentor_linkedin_url", res)
        self.assertTrue(res.get("contact_sealed"))
        self.assertIn("consent", res["instructions"].lower())

    def test_draft_public_mentor_keeps_url(self):
        pub_id = mentors.mentor_opt_in(
            _profile(preferred_contact="linkedin_public")
        )["card"]["id"]
        res = mentors.connection_draft(
            pub_id, "Alex T",
            "land my first senior backend role at a fintech company",
        )
        self.assertTrue(res["ok"])
        self.assertIn("linkedin.com", res["mentor_linkedin_url"])
        self.assertIn("send this yourself", res["instructions"].lower())

    def test_long_goal_gets_trimmed_not_dropped(self):
        goal = "get hired as a senior backend engineer at a well-funded fintech " * 20
        res = mentors.connection_draft(self.ada_id, "Alex T", goal)
        self.assertTrue(res["ok"])
        self.assertLessEqual(len(res["note"]), 300)
        self.assertIn("Alex T", res["note"])

    def test_unknown_mentor(self):
        res = mentors.connection_draft("mentor-deadbeef0000", "P", "g")
        self.assertFalse(res["ok"])

    def test_missing_name_or_goal(self):
        self.assertFalse(mentors.connection_draft(self.ada_id, "", "g")["ok"])
        self.assertFalse(mentors.connection_draft(self.ada_id, "P", " ")["ok"])

    def test_no_capacity_no_draft(self):
        mentors.record_outreach(self.ada_id)
        mentors.record_outreach(self.ada_id)  # max_mentees=2 -> full
        res = mentors.connection_draft(self.ada_id, "P", "g")
        self.assertFalse(res["ok"])


class RecordOutreachTests(_TempStore):
    def test_decrements_capacity(self):
        mid = mentors.mentor_opt_in(_profile())["card"]["id"]
        res = mentors.record_outreach(mid)
        self.assertTrue(res["ok"])
        self.assertEqual(res["remaining_capacity"], 1)

    def test_unknown_mentor(self):
        self.assertFalse(mentors.record_outreach("mentor-nope")["ok"])


class PublishTests(_TempStore):
    def test_unconfirmed_never_writes(self):
        mentors.mentor_opt_in(_profile())
        res = mentors.publish_mentor_card(confirmed=False)
        self.assertTrue(res["ok"])
        self.assertFalse(res["published"])
        self.assertIn("preview", res)
        self.assertIn("next_steps", res)
        self.assertFalse(any(mentors.OUTBOX_DIR.glob("*.json")))

    def test_confirmed_writes_outbox(self):
        card_id = mentors.mentor_opt_in(_profile())["card"]["id"]
        res = mentors.publish_mentor_card(confirmed=True)
        self.assertTrue(res["published"])
        path = Path(res["path"])
        self.assertTrue(path.exists())
        written = json.loads(path.read_text())
        self.assertEqual(written["id"], card_id)
        self.assertNotIn("own", written)

    def test_no_card_no_publish(self):
        self.assertFalse(mentors.publish_mentor_card(confirmed=True)["ok"])


class WiringTests(unittest.TestCase):
    def test_topics_constant(self):
        for t in ("interviewing", "salary_negotiation", "career_switch",
                  "leadership", "executive_presence", "ai_skills",
                  "networking", "resume_craft"):
            self.assertIn(t, mentors.TOPICS)

    def test_register_cli_shape(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mapping = mentors.register_cli(sub)
        self.assertEqual(set(mapping), {"mentors"})
        self.assertTrue(callable(mapping["mentors"]))

    def test_register_tools_registers(self):
        calls = []

        class FakeMcp:
            def tool(self):
                def deco(fn):
                    calls.append(fn.__name__)
                    return fn
                return deco

        mentors.register_tools(FakeMcp())
        for name in ("mentor_opt_in", "mentor_opt_out", "my_mentor_card",
                     "mentor_match", "mentor_connection_draft",
                     "mentor_record_outreach", "mentor_publish"):
            self.assertIn(name, calls)

    def test_cli_end_to_end(self):
        import argparse

        with tempfile.TemporaryDirectory() as d:
            mentors.MENTORS_FILE = Path(d) / "m.json"
            try:
                parser = argparse.ArgumentParser()
                sub = parser.add_subparsers()
                handlers = mentors.register_cli(sub)
                args = parser.parse_args([
                    "mentors", "opt-in",
                    "--profile", json.dumps(_profile()),
                    "--json",
                ])
                self.assertEqual(handlers["mentors"](args), 0)
            finally:
                mentors.MENTORS_FILE = mentors.BASE_DIR / "mentors.json"


if __name__ == "__main__":
    unittest.main()
