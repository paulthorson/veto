#!/usr/bin/env python3
"""Unit tests for soft_skills.py: training module (no network).

All file I/O goes to temporary directories — the real
soft_skill_sessions.json / soft_skills.json are never touched.

Run: cd ~/workspace/job-apply-mcp && .venv/bin/python -m pytest tests/test_soft_skills.py -q
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import soft_skills as ss  # noqa: E402

PROFILE = {
    "full_name": "Ada Lovelace",
    "target_titles": ["Senior Backend Engineer"],
    "minimum_salary": 160000,
    "experience": [
        {
            "title": "Backend Engineer",
            "company": "Initech",
            "description": "Rebuilt billing pipeline, cut p99 latency 40%.",
        }
    ],
}

STRONG_STAR = (
    "When I joined Initech the billing pipeline was timing out nightly. "
    "I was responsible for reliability of that system. I led a rewrite of "
    "the consumer, added backpressure, and shipped it in six weeks. As a "
    "result p99 latency dropped 40% and we saved $2M a year."
)
WEAK_ANSWER = "Um, like, we basically did stuff and it was, you know, fine."


class PatchedPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._sess = mock.patch.object(
            ss, "SESSIONS_PATH", Path(self.tmp.name) / "sessions.json")
        self._prog = mock.patch.object(
            ss, "PROGRESS_PATH", Path(self.tmp.name) / "progress.json")
        self._sess.start(); self._prog.start()
        self.addCleanup(self._sess.stop); self.addCleanup(self._prog.stop)


class TestCatalog(PatchedPaths):
    def test_six_areas(self):
        self.assertEqual(len(ss.SKILL_AREAS), 6)
        for name in ("communication_clarity", "star_storytelling",
                     "leadership_influence", "negotiation",
                     "active_listening", "executive_presence"):
            self.assertIn(name, ss.SKILL_AREAS)
            self.assertIn("description", ss.SKILL_AREAS[name])
            self.assertIn("good_looks_like", ss.SKILL_AREAS[name])

    def test_difficulties(self):
        self.assertEqual(tuple(ss.DIFFICULTIES), ("firm", "hardball", "brutal"))


class TestAssess(PatchedPaths):
    def test_all_strong(self):
        r = ss.assess("negotiation", ["C", "C", "C"])
        self.assertEqual(r["level"], "strong")
        self.assertEqual(r["score_pct"], 100)

    def test_all_developing(self):
        r = ss.assess("negotiation", ["A", "A", "A"])
        self.assertEqual(r["level"], "developing")

    def test_mixed_practicing(self):
        r = ss.assess("negotiation", ["A", "B", "C"])
        self.assertEqual(r["level"], "practicing")
        self.assertEqual(len(r["per_question"]), 3)
        self.assertTrue(all("feedback" in q for q in r["per_question"]))
        self.assertIn("next_step", r)

    def test_numeric_answers(self):
        r = ss.assess("negotiation", [2, 2, 2])
        self.assertEqual(r["level"], "strong")

    def test_bad_area(self):
        with self.assertRaises(ValueError):
            ss.assess("mind_reading", ["A", "A", "A"])

    def test_wrong_count(self):
        with self.assertRaises(ValueError):
            ss.assess("negotiation", ["A", "A"])

    def test_bad_choice(self):
        with self.assertRaises(ValueError):
            ss.assess("negotiation", ["A", "D", "C"])

    def test_every_area_has_three_questions(self):
        for area in ss.SKILL_AREAS:
            r = ss.assess(area, ["B", "B", "B"])
            self.assertEqual(len(r["per_question"]), 3)


class TestDrillCoach(PatchedPaths):
    def test_coach_prompt_uses_profile(self):
        d = ss.drill("star_storytelling", PROFILE, mode="coach")
        self.assertIn("Initech", d["prompt"])
        self.assertEqual(d["rounds_total"], 1)

    def test_coach_prompt_generic_without_experience(self):
        d = ss.drill("negotiation", {"target_titles": ["PM"]}, mode="coach")
        self.assertIn("session_id", d)
        # no invented company
        self.assertNotIn("Initech", d["prompt"])

    def test_review_closes_coach_session(self):
        d = ss.drill("negotiation", PROFILE, mode="coach")
        r = ss.review_drill(d["session_id"], STRONG_STAR)
        self.assertTrue(r["done"])
        self.assertIn("coach", r)
        self.assertIn("debrief", r)

    def test_strong_beats_weak(self):
        d1 = ss.drill("star_storytelling", PROFILE)
        d2 = ss.drill("star_storytelling", PROFILE)
        s1 = ss.review_drill(d1["session_id"], STRONG_STAR)["coach"]["score"]
        s2 = ss.review_drill(d2["session_id"], WEAK_ANSWER)["coach"]["score"]
        self.assertGreater(s1, s2)

    def test_filler_and_numbers_detected(self):
        d = ss.drill("star_storytelling", PROFILE)
        r = ss.review_drill(d["session_id"], WEAK_ANSWER)
        analysis = r["coach"]["analysis"]
        self.assertGreater(analysis["filler_count"], 0)
        self.assertFalse(analysis["has_numbers"])

    def test_unknown_session(self):
        with self.assertRaises(ValueError):
            ss.review_drill("ss_nope", "hello")

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            ss.drill("negotiation", PROFILE, mode="sparring")


class TestAdversarialDrill(PatchedPaths):
    def test_three_rounds(self):
        d = ss.drill("star_storytelling", PROFILE, mode="adversarial",
                     difficulty="hardball")
        self.assertEqual(d["rounds_total"], 3)
        r1 = ss.review_drill(d["session_id"], WEAK_ANSWER)
        self.assertFalse(r1["done"])
        self.assertIn("coach", r1)
        self.assertIn("adversary", r1)
        # adversary challenges vagueness / missing numbers
        self.assertIn("vague", r1["adversary"].lower()
                      + r1["coach"]["feedback"][1].lower())
        r2 = ss.review_drill(d["session_id"], STRONG_STAR)
        self.assertFalse(r2["done"])
        r3 = ss.review_drill(d["session_id"], STRONG_STAR)
        self.assertTrue(r3["done"])
        self.assertIn("debrief", r3)
        with self.assertRaises(ValueError):
            ss.review_drill(d["session_id"], "one more")

    def test_difficulty_changes_opener(self):
        openers = {
            diff: ss.drill("negotiation", PROFILE, mode="adversarial",
                           difficulty=diff)["prompt"]
            for diff in ss.DIFFICULTIES
        }
        self.assertEqual(len(set(openers.values())), 3)

    def test_sessions_persisted(self):
        d = ss.drill("negotiation", PROFILE, mode="adversarial")
        data = json.loads((Path(self.tmp.name) / "sessions.json").read_text())
        self.assertIn(d["session_id"], data)


class TestNegotiation(PatchedPaths):
    def _play(self, difficulty="firm", msgs=None):
        msgs = msgs or [
            (165000, "Based on market data for this role and level, plus the "
                      "billing pipeline rebuild where I cut p99 latency 40% "
                      "and saved $2M, I'm looking for $165k."),
            (160000, "I led a team of five and shipped the new platform. "
                      "Levels.fyi puts this role at $160-180k. Can we meet "
                      "at $160k?"),
            (158000, "I'm excited about the team. $158k gets this done "
                      "today."),
        ]
        s = ss.negotiation_sim(140000, 160000, PROFILE, difficulty)
        out = [s]
        for counter, msg in msgs:
            out.append(ss.negotiation_round(s["session_id"], counter, msg))
        return out

    def test_full_flow(self):
        out = self._play()
        final = out[-1]
        self.assertTrue(final["done"])
        self.assertIn("final_offer", final)
        self.assertIn("summary", final)
        self.assertGreaterEqual(final["final_offer"], 140000)

    def test_coach_blocks_labeled(self):
        out = self._play()
        for r in out[1:]:
            coach = r["coach"]
            self.assertIn("adversary_move", coach)
            self.assertIn("adversary_move_explained", coach)
            self.assertIn("your_counter", coach)
            self.assertIn("try_next", coach)
            self.assertIn("adversary", r)

    def test_hardball_differs_from_firm(self):
        firm = self._play("firm")[-1]["summary"]
        brutal = self._play("brutal")[-1]["summary"]
        # brutal concedes less for identical play
        self.assertLessEqual(brutal["closed_at"], firm["closed_at"])

    def test_bid_against_yourself_flagged(self):
        s = ss.negotiation_sim(140000, 160000, PROFILE, "firm")
        r = ss.negotiation_round(s["session_id"], 130000, "I'll take less.")
        joined = " ".join(r["coach"]["your_counter"]).lower()
        self.assertIn("bid against yourself", joined)

    def test_bad_difficulty(self):
        with self.assertRaises(ValueError):
            ss.negotiation_sim(140000, 160000, PROFILE, "savage")

    def test_bad_offer(self):
        with self.assertRaises(ValueError):
            ss.negotiation_sim(-5, 160000)

    def test_unknown_session(self):
        with self.assertRaises(ValueError):
            ss.negotiation_round("ss_nope", 150000, "hi")

    def test_opener_breaks_character_with_coach_note(self):
        s = ss.negotiation_sim(140000, 160000, PROFILE, "hardball")
        self.assertIn("[COACH]", s["coach_note"])


class TestAdversaryProfessional(PatchedPaths):
    """The adversary attacks the argument, never the person."""

    def _all_adversary_text(self):
        texts = []
        for diff in ss.DIFFICULTIES:
            for area in ss.SKILL_AREAS:
                texts.append(ss._ADVERSARY_OPENERS[diff][area])
                fake = {"has_numbers": False, "we_statements": 5,
                        "i_statements": 1, "word_count": 30,
                        "star": {"result": {"present": False}},
                        "filler_count": 4}
                texts.append(ss._adversary_followup(fake, diff, 2))
        for diff in ss.DIFFICULTIES:
            s = ss.negotiation_sim(140000, 160000, PROFILE, diff)
            texts.append(s["adversary"])
            for rnd, (counter, msg) in enumerate([
                (165000, "Market data says $165k."),
                (160000, "I led the rebuild."),
                (158000, "Let's close."),
            ]):
                r = ss.negotiation_round(s["session_id"], counter, msg)
                texts.append(r["adversary"])
        return texts

    def test_no_banned_tokens(self):
        for t in self._all_adversary_text():
            low = t.lower()
            for banned in ss._BANNED_TOKENS:
                self.assertNotIn(banned, low, f"banned token in: {t[:80]}")

    def test_no_second_person_attacks(self):
        # "you are ..." personal judgments are out of bounds
        for t in self._all_adversary_text():
            self.assertNotRegex(
                t.lower(),
                r"\byou are (a |an )?(weak|bad|terrible|poor|wrong)\b",
            )


class TestProgress(PatchedPaths):
    def test_log_and_progress(self):
        ss.log_practice("negotiation", 60, kind="drill-coach")
        ss.log_practice("negotiation", 80, kind="drill-coach")
        p = ss.progress()
        n = p["areas"]["negotiation"]
        self.assertEqual(n["reps"], 2)
        self.assertEqual(n["average"], 70)
        self.assertEqual(n["best"], 80)
        self.assertEqual(p["total_reps"], 2)

    def test_trend_improving(self):
        for s in (50, 55, 75, 80):
            ss.log_practice("negotiation", s)
        self.assertEqual(ss.progress()["areas"]["negotiation"]["trend"],
                         "improving")

    def test_trend_not_started(self):
        p = ss.progress()
        self.assertEqual(p["areas"]["negotiation"]["trend"], "not started")

    def test_bad_area(self):
        with self.assertRaises(ValueError):
            ss.log_practice("telepathy", 50)

    def test_bad_score(self):
        with self.assertRaises(ValueError):
            ss.log_practice("negotiation", 150)

    def test_drill_auto_logs(self):
        d = ss.drill("negotiation", PROFILE, mode="coach")
        ss.review_drill(d["session_id"], STRONG_STAR)
        self.assertEqual(ss.progress()["areas"]["negotiation"]["reps"], 1)


class TestWiring(PatchedPaths):
    def test_register_tools(self):
        registered = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        ss.register_tools(FakeMCP())
        for name in ("soft_skill_areas", "assess_soft_skill", "start_drill",
                     "review_drill_answer", "start_negotiation",
                     "negotiation_counter", "log_soft_skill_practice",
                     "soft_skill_progress"):
            self.assertIn(name, registered)
        out = registered["soft_skill_areas"]()
        self.assertEqual(len(out["areas"]), 6)
        out = registered["assess_soft_skill"]("negotiation", ["C", "C", "C"])
        self.assertEqual(out["level"], "strong")

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        handlers = ss.register_cli(sub)
        self.assertEqual(list(handlers), ["soft-skills"])
        args = parser.parse_args(
            ["soft-skills", "assess", "negotiation", "C", "C", "C"])
        self.assertEqual(args.action, "assess")
        self.assertEqual(handlers["soft-skills"](args), 0)
        args = parser.parse_args(["soft-skills", "progress"])
        self.assertEqual(handlers["soft-skills"](args), 0)
        args = parser.parse_args(
            ["soft-skills", "negotiate", "--offer", "140000",
             "--target", "160000", "--difficulty", "firm"])
        self.assertEqual(handlers["soft-skills"](args), 0)


if __name__ == "__main__":
    unittest.main()
