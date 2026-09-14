#!/usr/bin/env python3
"""Offline tests for the mentor matchmaking enhancements.

Covers: training-triggered mentor suggestions (with stubbed
soft_skills / ai_proficiency progress), first-session agendas, two-sided
ratings, and the mentee prep gate.
"""

from __future__ import annotations

import argparse
import copy
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
        "topics": ["salary_negotiation", "interviewing"],
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

    def _seed_mentor(self, **overrides):
        res = mentors.mentor_opt_in(_profile(**overrides), own=False)
        self.assertTrue(res["ok"])
        return res["card"]["id"]


class _StubSoftSkills:
    """Stub soft_skills module with configurable progress()."""
    data = {
        "areas": {
            "negotiation": {"reps": 5, "average": 52, "best": 60,
                            "trend": "declining"},
            "star_storytelling": {"reps": 3, "average": 85, "best": 90,
                                  "trend": "steady"},
        },
        "total_reps": 8,
        "note": "",
    }

    @staticmethod
    def progress():
        return copy.deepcopy(_StubSoftSkills.data)


class _StubAIProficiency:
    """Stub ai_proficiency module with configurable progress()."""
    data = {
        "tracks": {
            "engineer": {
                "label": "Engineer", "level": "foundations", "diagnosed": True,
                "completed": [], "completed_count": 0, "total_lessons": 7,
                "last_practice": None,
            },
        },
        "total_completed": 0,
        "streak_days": 0,
        "note": "",
    }

    @staticmethod
    def progress():
        return copy.deepcopy(_StubAIProficiency.data)


def _stubbed(soft=True, ai=True):
    """Start patchers for mentors' training-module imports.

    Returns the started patchers; callers must call ``patch.stopall()``
    when done.
    """
    started = []
    started.append(patch.object(
        mentors, "soft_skills", _StubSoftSkills if soft else None).start())
    started.append(patch.object(
        mentors, "ai_proficiency", _StubAIProficiency if ai else None).start())
    return started


class _StubData:
    """Temporarily swap stub progress data, restoring it afterwards."""

    def __init__(self, soft_data=None, ai_data=None):
        self.soft_data = soft_data
        self.ai_data = ai_data
        self._orig_soft = copy.deepcopy(_StubSoftSkills.data)
        self._orig_ai = copy.deepcopy(_StubAIProficiency.data)

    def __enter__(self):
        if self.soft_data is not None:
            _StubSoftSkills.data = copy.deepcopy(self.soft_data)
        if self.ai_data is not None:
            _StubAIProficiency.data = copy.deepcopy(self.ai_data)
        return self

    def __exit__(self, *exc):
        _StubSoftSkills.data = self._orig_soft
        _StubAIProficiency.data = self._orig_ai
        return False


class SuggestTests(_TempStore):
    def test_weak_areas_detected_with_evidence(self):
        self._seed_mentor()
        _stubbed()
        try:
            res = mentors.suggest_mentors_from_training()
        finally:
            patch.stopall()
        self.assertTrue(res["ok"])
        weak = {w["area"]: w for w in res["weak_areas"]}
        self.assertIn("negotiation", weak)
        self.assertIn("52/100", weak["negotiation"]["evidence"])
        self.assertIn("trending down", weak["negotiation"]["evidence"])
        # Strong area is NOT flagged.
        self.assertNotIn("star_storytelling", weak)
        # AI foundations track is flagged.
        self.assertTrue(any(w["source"] == "ai_proficiency"
                            for w in res["weak_areas"]))
        self.assertIn("salary_negotiation", res["suggested_topics"])
        self.assertIn("ai_skills", res["suggested_topics"])

    def test_matches_reuse_matchmake_per_topic(self):
        mentor_id = self._seed_mentor()
        _stubbed()
        try:
            res = mentors.suggest_mentors_from_training()
        finally:
            patch.stopall()
        matches = res["matches_by_topic"]["salary_negotiation"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["mentor_id"], mentor_id)
        self.assertIn("prep", matches[0])
        # Goal is derived from real evidence, not invented.
        self.assertNotIn("mentee_goal", res)
        self.assertIn("negotiation", res["weak_areas"][0]["evidence"])

    def test_empty_directory_never_invents_mentors(self):
        _stubbed()
        try:
            res = mentors.suggest_mentors_from_training()
        finally:
            patch.stopall()
        self.assertTrue(res["ok"])
        for matches in res["matches_by_topic"].values():
            self.assertEqual(matches, [])

    def test_no_training_history_suggests_drill_first(self):
        empty_soft = {"areas": {}, "total_reps": 0, "note": ""}
        empty_ai = {"tracks": {}, "total_completed": 0,
                    "streak_days": 0, "note": ""}
        with _StubData(soft_data=empty_soft, ai_data=empty_ai):
            _stubbed()
            try:
                res = mentors.suggest_mentors_from_training()
            finally:
                patch.stopall()
        self.assertTrue(res["ok"])
        self.assertEqual(res["weak_areas"], [])
        self.assertIn("drill", res["guidance"].lower())

    def test_strong_history_everywhere(self):
        strong_soft = {
            "areas": {"negotiation": {"reps": 6, "average": 88, "best": 95,
                                      "trend": "improving"}},
            "total_reps": 6, "note": "",
        }
        strong_ai = {
            "tracks": {"engineer": {"label": "Engineer", "level": "advanced",
                                    "diagnosed": True, "completed": [],
                                    "completed_count": 6, "total_lessons": 7,
                                    "last_practice": None}},
            "total_completed": 6, "streak_days": 3, "note": "",
        }
        with _StubData(soft_data=strong_soft, ai_data=strong_ai):
            _stubbed()
            try:
                res = mentors.suggest_mentors_from_training()
            finally:
                patch.stopall()
        self.assertTrue(res["ok"])
        self.assertEqual(res["weak_areas"], [])
        self.assertIn("strong", res["guidance"].lower())

    def test_missing_modules_degrade_gracefully(self):
        _stubbed(soft=False, ai=False)
        try:
            res = mentors.suggest_mentors_from_training()
        finally:
            patch.stopall()
        self.assertTrue(res["ok"])
        self.assertEqual(res["weak_areas"], [])
        self.assertIn("unavailable_modules", res)
        self.assertIn("soft_skills", res["unavailable_modules"])
        self.assertIn("ai_proficiency", res["unavailable_modules"])

    def test_weak_area_to_topics_mapping(self):
        for area, topics in mentors.WEAK_AREA_TO_TOPICS.items():
            for t in topics:
                self.assertIn(t, mentors.TOPICS, f"{area} -> {t}")
        self.assertIn("salary_negotiation",
                      mentors.WEAK_AREA_TO_TOPICS["negotiation"])


class AgendaTests(_TempStore):
    def test_agenda_structure(self):
        mentor_id = self._seed_mentor()
        res = mentors.session_agenda(mentor_id, "land a staff engineer offer")
        self.assertTrue(res["ok"])
        md = res["markdown"]
        for section in ("0:00–0:05", "0:05–0:20", "0:20–0:25", "0:25–0:30"):
            self.assertIn(section, md)
        self.assertIn("Ada L.", md)
        self.assertIn("land a staff engineer offer", md)
        self.assertIn("template", res["template_note"].lower())

    def test_agenda_questions_come_from_mentor_topics(self):
        mentor_id = self._seed_mentor()
        res = mentors.session_agenda(mentor_id, "negotiate better")
        self.assertTrue(res["ok"])
        # salary_negotiation is the mentor's first topic.
        self.assertIn("anchor", res["markdown"].lower())

    def test_agenda_unknown_mentor(self):
        res = mentors.session_agenda("mentor-nope", "some goal")
        self.assertFalse(res["ok"])
        self.assertIn("unknown", res["error"].lower())

    def test_agenda_requires_goal(self):
        mentor_id = self._seed_mentor()
        res = mentors.session_agenda(mentor_id, "   ")
        self.assertFalse(res["ok"])


class RatingTests(_TempStore):
    def test_rate_from_mentee_side(self):
        mentor_id = self._seed_mentor()
        res = mentors.rate_mentorship(mentor_id, 4, "great first call")
        self.assertTrue(res["ok"])
        self.assertEqual(res["rating_summary"]["mentee"],
                         {"count": 1, "average": 4.0})
        self.assertEqual(res["rating_summary"]["mentor"],
                         {"count": 0, "average": None})

    def test_two_sided_ratings(self):
        mentor_id = self._seed_mentor()
        mentors.rate_mentorship(mentor_id, 4, by="mentee")
        mentors.rate_mentorship(mentor_id, 5, by="mentee")
        mentors.rate_mentorship(mentor_id, 5, "prepared mentee", by="mentor")
        directory = json.loads(mentors.MENTORS_FILE.read_text())
        summary = mentors._rating_summary(directory[mentor_id])
        self.assertEqual(summary["mentee"], {"count": 2, "average": 4.5})
        self.assertEqual(summary["mentor"], {"count": 1, "average": 5.0})

    def test_invalid_stars_rejected(self):
        mentor_id = self._seed_mentor()
        for bad in (0, 6, -1, "5", 4.5, True, None):
            res = mentors.rate_mentorship(mentor_id, bad)
            self.assertFalse(res["ok"], f"stars={bad!r} should be rejected")

    def test_invalid_by_rejected(self):
        mentor_id = self._seed_mentor()
        res = mentors.rate_mentorship(mentor_id, 4, by="stranger")
        self.assertFalse(res["ok"])

    def test_unknown_mentor_rejected(self):
        res = mentors.rate_mentorship("mentor-nope", 4)
        self.assertFalse(res["ok"])

    def test_matchmake_includes_ratings_and_prep(self):
        mentor_id = self._seed_mentor()
        mentors.rate_mentorship(mentor_id, 5, by="mentee")
        res = mentors.matchmake({
            "industry": "fintech",
            "target_role": "backend engineer",
            "topics_ranked": ["salary_negotiation"],
            "goal": "negotiate better",
        })
        self.assertTrue(res["ok"])
        match = res["matches"][0]
        self.assertEqual(match["ratings"]["mentee"],
                         {"count": 1, "average": 5.0})
        self.assertIn("prep", match)
        self.assertIn("negotiation", match["prep"].lower())

    def test_card_output_includes_rating_summary(self):
        mentor_id = self._seed_mentor(
            linkedin_url="https://www.linkedin.com/in/owncard")
        mentors.mentor_opt_in(_profile(
            linkedin_url="https://www.linkedin.com/in/owncard2",
            name="Own"), own=True)
        own = [c for c in json.loads(
            mentors.MENTORS_FILE.read_text()).values() if c.get("own")][0]
        mentors.rate_mentorship(own["id"], 3, by="mentor")
        shown = mentors.my_mentor_card()["card"]
        self.assertEqual(shown["rating_summary"]["mentor"],
                         {"count": 1, "average": 3.0})

    def test_ratings_survive_card_edit(self):
        res = mentors.mentor_opt_in(_profile(
            linkedin_url="https://www.linkedin.com/in/editme"), own=True)
        mentor_id = res["card"]["id"]
        mentors.rate_mentorship(mentor_id, 5, by="mentee")
        updated = mentors.my_mentor_card({"bio": "new bio"})
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["card"]["bio"], "new bio")
        self.assertEqual(updated["card"]["rating_summary"]["mentee"],
                         {"count": 1, "average": 5.0})
        # And it persisted to disk.
        disk = json.loads(mentors.MENTORS_FILE.read_text())
        self.assertEqual(len(disk[mentor_id]["ratings"]["mentee"]), 1)


class PrepTests(_TempStore):
    def test_prep_for_every_topic(self):
        for topic in mentors.TOPICS:
            res = mentors.prep_for_topic(topic)
            self.assertTrue(res["ok"], topic)
            self.assertTrue(res["prep"])
            self.assertIn("recommend", res["note"].lower())

    def test_prep_unknown_topic(self):
        res = mentors.prep_for_topic("mind_reading")
        self.assertFalse(res["ok"])

    def test_prep_points_at_real_practice(self):
        self.assertIn("negotiation_sim",
                      mentors.prep_for_topic("salary_negotiation")["prep"])
        self.assertIn("star_storytelling",
                      mentors.prep_for_topic("interviewing")["prep"])
        self.assertIn("ai-skills",
                      mentors.prep_for_topic("ai_skills")["prep"])

    def test_connection_draft_includes_prep_checklist(self):
        mentor_id = self._seed_mentor()
        res = mentors.connection_draft(mentor_id, "Pat", "negotiate better")
        self.assertTrue(res["ok"])
        self.assertIn("prep_checklist", res)
        self.assertTrue(len(res["prep_checklist"]) >= 1)
        self.assertIn("prep_note", res)
        self.assertIn("not required", res["prep_note"])

    def test_prep_is_recommendation_not_block(self):
        # A mentee with zero practice history can still get a draft.
        mentor_id = self._seed_mentor()
        res = mentors.connection_draft(mentor_id, "Pat", "negotiate better")
        self.assertTrue(res["ok"])
        self.assertIn("DRAFT", res["status"])


class WiringTests(_TempStore):
    def test_register_cli_new_actions(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mapping = mentors.register_cli(sub)
        args = parser.parse_args(["mentors", "suggest-from-training"])
        self.assertEqual(args.action, "suggest-from-training")
        for action in ("suggest-from-training", "agenda", "rate", "prep"):
            args = parser.parse_args(["mentors", action])
            self.assertEqual(args.action, action)
        self.assertEqual(set(mapping), {"mentors"})

    def test_cli_suggest_from_training_end_to_end(self):
        self._seed_mentor()
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = mentors.register_cli(sub)
        args = parser.parse_args(
            ["mentors", "suggest-from-training", "--json"])
        _stubbed()
        try:
            self.assertEqual(handlers["mentors"](args), 0)
        finally:
            patch.stopall()

    def test_cli_agenda_rate_prep_end_to_end(self):
        mentor_id = self._seed_mentor()
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = mentors.register_cli(sub)

        args = parser.parse_args(
            ["mentors", "agenda", "--mentor-id", mentor_id,
             "--mentee-goal", "get promoted", "--json"])
        self.assertEqual(handlers["mentors"](args), 0)

        args = parser.parse_args(
            ["mentors", "rate", "--mentor-id", mentor_id,
             "--stars", "5", "--by", "mentee"])
        self.assertEqual(handlers["mentors"](args), 0)
        disk = json.loads(mentors.MENTORS_FILE.read_text())
        self.assertEqual(len(disk[mentor_id]["ratings"]["mentee"]), 1)

        args = parser.parse_args(["mentors", "prep", "--topic", "leadership"])
        self.assertEqual(handlers["mentors"](args), 0)

    def test_register_tools_registers_new_tools(self):
        calls = []

        class FakeMcp:
            def tool(self):
                def deco(fn):
                    calls.append(fn.__name__)
                    return fn
                return deco

        mentors.register_tools(FakeMcp())
        for name in ("suggest_mentors_from_training", "session_agenda",
                     "rate_mentorship", "prep_for_topic"):
            self.assertIn(name, calls)


if __name__ == "__main__":
    unittest.main()
