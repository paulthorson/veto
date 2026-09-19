#!/usr/bin/env python3
"""Unit tests for ai_proficiency.py (no network).

All store I/O is redirected to a temporary directory — the real
``ai_proficiency.json`` is never touched.

Run:  cd ~/workspace/veto && .venv/bin/python -m unittest discover -s tests
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import ai_proficiency as ap  # noqa: E402


def _correct_answers(track):
    out = []
    for q in ap.TRACKS[track]["diagnostic"]:
        if q["kind"] == "mcq":
            out.append(q["answer"])
        else:
            out.append("I would " + ", ".join(q["concepts"]) + ".")
    return out


class StoreMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = ap.STORE_PATH
        ap.STORE_PATH = Path(self._tmp.name) / "ai_proficiency.json"

    def tearDown(self):
        ap.STORE_PATH = self._orig
        self._tmp.cleanup()


class TestTracks(unittest.TestCase):
    def test_seven_tracks(self):
        self.assertEqual(
            set(ap.TRACKS),
            {"engineer", "product", "data", "design", "marketing", "sales",
             "generalist"})

    def test_each_track_has_skills_diagnostic_lessons(self):
        for tid, t in ap.TRACKS.items():
            self.assertTrue(t["skills"], tid)
            self.assertEqual(len(t["diagnostic"]), 5, tid)
            kinds = [q["kind"] for q in t["diagnostic"]]
            self.assertEqual(kinds.count("mcq"), 4, tid)
            self.assertEqual(kinds.count("free"), 1, tid)
            self.assertTrue(6 <= len(t["lessons"]) <= 10, tid)

    def test_diagnostic_mcqs_wellformed(self):
        for tid, t in ap.TRACKS.items():
            for q in t["diagnostic"]:
                if q["kind"] == "mcq":
                    self.assertEqual(len(q["choices"]), 4, q["id"])
                    self.assertIn(q["answer"], range(4), q["id"])
                    self.assertTrue(q["why"], q["id"])
                else:
                    self.assertTrue(len(q["concepts"]) >= 3, q["id"])

    def test_every_track_has_ethics_lesson(self):
        for tid, t in ap.TRACKS.items():
            ethics = [ls for ls in t["lessons"]
                      if ls["skill"] == "AI ethics & disclosure"]
            self.assertEqual(len(ethics), 1, tid)
            body = ethics[0]["body"].lower()
            self.assertIn("disclos", body)
            self.assertIn("never invent", body)

    def test_lesson_ids_unique_per_track(self):
        for tid, t in ap.TRACKS.items():
            ids = [ls["id"] for ls in t["lessons"]]
            self.assertEqual(len(ids), len(set(ids)), tid)
            for ls in t["lessons"]:
                self.assertIn("exercise", ls, ls["id"])
                self.assertIn("check", ls["exercise"], ls["id"])
                self.assertIn("hint", ls["exercise"], ls["id"])


class TestDiagnose(StoreMixin):
    def test_all_correct_is_advanced(self):
        res = ap.diagnose("engineer", _correct_answers("engineer"))
        self.assertEqual(res["level"], "advanced")
        self.assertEqual(res["score"], 5.0)
        self.assertEqual(res["gaps"], [])

    def test_all_wrong_is_foundations_with_gaps(self):
        res = ap.diagnose("engineer", [0, 0, 0, 0, "dunno"])
        # Q1's answer might be 0 for some track; engineer d1 answer is 1.
        self.assertEqual(res["level"], "foundations")
        self.assertTrue(len(res["gaps"]) >= 4)
        for g in res["gaps"]:
            self.assertIn("skill", g)
            self.assertIn("lesson_id", g)

    def test_letter_answers_accepted(self):
        answers = ["B", "B", "B", "B",
                   "I would test and verify and be specific about edge cases."]
        res = ap.diagnose("engineer", answers)
        self.assertEqual(res["level"], "advanced")

    def test_partial_is_practitioner(self):
        # 2 mcq right + weak free text -> ~2.0 -> practitioner
        res = ap.diagnose("engineer", [1, 1, 0, 0, "maybe test"])
        self.assertEqual(res["level"], "practitioner")

    def test_wrong_answer_count_is_error(self):
        res = ap.diagnose("engineer", [1, 2])
        self.assertIn("error", res)

    def test_unknown_track_is_error(self):
        res = ap.diagnose("wizard", [1, 1, 1, 1, "x"])
        self.assertIn("error", res)

    def test_level_persisted(self):
        ap.diagnose("product", _correct_answers("product"))
        store = ap._load_store()
        self.assertEqual(store["tracks"]["product"]["level"], "advanced")


class TestPlan(unittest.TestCase):
    def test_start_here_by_level(self):
        self.assertEqual(ap.plan("engineer", "foundations")["start_here"], 0)
        self.assertEqual(ap.plan("engineer", "practitioner")["start_here"], 2)
        self.assertEqual(ap.plan("engineer", "advanced")["start_here"], 4)

    def test_bad_level_is_error(self):
        self.assertIn("error", ap.plan("engineer", "guru"))

    def test_lessons_have_estimates(self):
        for ls in ap.plan("design", "foundations")["lessons"]:
            self.assertGreaterEqual(ls["est_minutes"], 5)


class TestExercises(StoreMixin):
    def test_exercise_then_pass(self):
        lesson = ap.TRACKS["engineer"]["lessons"][0]
        sess = ap.exercise("engineer", lesson["id"])
        self.assertIn("session_id", sess)
        self.assertNotIn("keywords", sess["exercise_prompt"])
        # Answer hitting the check keywords.
        res = ap.submit_exercise(
            sess["session_id"],
            "Write a Python function that parses date strings from a CSV "
            "column and skips empty cells gracefully.")
        self.assertTrue(res["passed"], res)
        self.assertEqual(res["next_lesson"],
                         ap.TRACKS["engineer"]["lessons"][1]["id"])

    def test_weak_answer_fails_with_feedback(self):
        lesson = ap.TRACKS["engineer"]["lessons"][0]
        sess = ap.exercise("engineer", lesson["id"])
        res = ap.submit_exercise(sess["session_id"], "idk, code stuff")
        self.assertFalse(res["passed"])
        self.assertIn("Hint", res["feedback"])

    def test_ethics_exercise_checks_honesty(self):
        tid = "generalist"
        lesson = next(ls for ls in ap.TRACKS[tid]["lessons"]
                      if ls["skill"] == "AI ethics & disclosure")
        sess = ap.exercise(tid, lesson["id"])
        good = ap.submit_exercise(
            sess["session_id"],
            "No — inventing a job title is dishonest. I should review AI "
            "drafts and only keep what is true about my experience.")
        self.assertTrue(good["passed"], good)

        sess2 = ap.exercise(tid, lesson["id"])
        bad = ap.submit_exercise(
            sess2["session_id"],
            "Sure, embellish a little, everyone does it.")
        self.assertFalse(bad["passed"])

    def test_unknown_session_is_error(self):
        self.assertIn("error", ap.submit_exercise("nope", "x"))

    def test_double_submit_is_error(self):
        lesson = ap.TRACKS["sales"]["lessons"][0]
        sess = ap.exercise("sales", lesson["id"])
        ap.submit_exercise(sess["session_id"], "research news pain sources")
        again = ap.submit_exercise(sess["session_id"], "research news")
        self.assertIn("error", again)

    def test_group_check_requires_both_groups(self):
        tid = "generalist"
        lesson = next(ls for ls in ap.TRACKS[tid]["lessons"]
                      if ls["id"] == "ge-l5")
        sess = ap.exercise(tid, lesson["id"])
        # Mentions automation but no human checkpoint -> fail.
        res = ap.submit_exercise(
            sess["session_id"],
            "I would automate my status updates with AI drafting.")
        self.assertFalse(res["passed"])
        sess2 = ap.exercise(tid, lesson["id"])
        res2 = ap.submit_exercise(
            sess2["session_id"],
            "I would automate my status updates with AI drafting, and I "
            "review each one before it is sent.")
        self.assertTrue(res2["passed"], res2)


class TestProgress(StoreMixin):
    def test_progress_records_practice(self):
        lesson = ap.TRACKS["engineer"]["lessons"][0]
        sess = ap.exercise("engineer", lesson["id"])
        ap.submit_exercise(
            sess["session_id"],
            "Write a Python function that parses date strings from a CSV "
            "column and handles empty cells.")
        prog = ap.progress()
        eng = prog["tracks"]["engineer"]
        self.assertEqual(eng["completed_count"], 1)
        self.assertEqual(prog["total_completed"], 1)
        self.assertGreaterEqual(prog["streak_days"], 1)
        self.assertIn("not a credential", prog["note"])

    def test_progress_empty_store(self):
        prog = ap.progress()
        self.assertEqual(prog["total_completed"], 0)
        self.assertEqual(prog["streak_days"], 0)
        for tid, tp in prog["tracks"].items():
            self.assertIsNone(tp["level"])
            self.assertFalse(tp["diagnosed"])


class TestPluginWiring(unittest.TestCase):
    def test_register_tools(self):
        registered = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        ap.register_tools(FakeMCP())
        for name in ("ai_tracks", "ai_diagnose", "ai_submit_diagnosis",
                     "ai_plan", "ai_lesson", "ai_exercise",
                     "ai_submit_exercise", "ai_progress"):
            self.assertIn(name, registered)

    def test_register_cli(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        handlers = ap.register_cli(sub)
        self.assertEqual(list(handlers), ["ai-skills"])
        args = parser.parse_args(["ai-skills", "plan", "--track", "engineer"])
        self.assertEqual(args.action, "plan")
        self.assertEqual(args.track, "engineer")

    def test_registered_tools_call_through(self):
        import tempfile
        orig = ap.STORE_PATH
        tmp = tempfile.TemporaryDirectory()
        ap.STORE_PATH = Path(tmp.name) / "ai_proficiency.json"
        try:
            registered = {}

            class FakeMCP:
                def tool(self):
                    def deco(fn):
                        registered[fn.__name__] = fn
                        return fn
                    return deco

            ap.register_tools(FakeMCP())
            tracks = registered["ai_tracks"]()
            self.assertEqual(len(tracks), 7)
            diag = registered["ai_diagnose"]("engineer")
            self.assertEqual(len(diag["questions"]), 5)
            # Answer key must be withheld from the diagnose output.
            self.assertNotIn("'answer':", str(diag["questions"]))
            prog = registered["ai_progress"]()
            self.assertIn("note", prog)
        finally:
            ap.STORE_PATH = orig
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
