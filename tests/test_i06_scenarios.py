"""Initiative 06 WS-C: soft-skill lab scenarios.

Covers initiatives.i06.scenarios and the soft_skills.py lab
integration (kinds/start/review/summary + longitudinal recording).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import soft_skills as ss  # noqa: E402
from initiatives.i06 import scenarios as SC  # noqa: E402


class TestScenarios(unittest.TestCase):
    def test_four_kinds_disclosed(self):
        self.assertEqual(set(SC.SCENARIO_KINDS),
                         {"conflict", "influence", "negotiation",
                          "ambiguous_stakeholder"})
        for kind, spec in SC.SCENARIOS.items():
            for key in ("title", "setup", "good_looks_like", "rounds"):
                self.assertIn(key, spec, f"{kind} missing {key}")
            self.assertEqual(len(spec["rounds"]), 3)

    def test_validate_kind_rejects_unknown(self):
        with self.assertRaises(ValueError):
            SC.validate_kind("smalltalk")

    def test_no_banned_tokens_anywhere(self):
        SC.assert_no_banned_tokens()

    def test_evaluate_round_signals(self):
        good = ("From their perspective, Sam is worried about the Friday "
                "deadline. I propose we ship the safe path now and put "
                "the shortcut behind a flag: I will own the flag removal "
                "by Friday.")
        out = SC.evaluate_round("conflict", 0, good)
        self.assertTrue(out["signals"]["perspective_taking"]["passed"])
        self.assertIn("structure", out["rubric"]["dimension_scores"])

        blaming = "You always ignore the risks, Sam. This is your fault."
        out2 = SC.evaluate_round("conflict", 0, blaming)
        self.assertFalse(out2["signals"]["no_blame"]["passed"])

    def test_evaluate_round_bad_index_raises(self):
        with self.assertRaises(ValueError):
            SC.evaluate_round("conflict", 9, "x")

    def test_negotiation_self_bid_flagged(self):
        out = SC.evaluate_round(
            "negotiation", 0,
            "I'll lower our commitment, we can go lower if needed.")
        self.assertFalse(out["signals"]["no_self_bid"]["passed"])

    def test_ambiguous_clarify_before_solve(self):
        out = SC.evaluate_round(
            "ambiguous_stakeholder", 0,
            "Help me understand: when you say better, do you mean faster "
            "loads or clearer numbers? Who is this for, and what does "
            "success look like?")
        self.assertTrue(
            out["signals"]["clarifying_questions"]["passed"])

    def test_scenario_card_discloses_scoring(self):
        card = SC.scenario_card("influence")
        self.assertIn("Setup", card)
        self.assertIn("Good looks like", card)
        # The card must disclose every round's signal names with
        # candidate-facing guidance (finding 1): no hidden triggers.
        for kind in SC.SCENARIO_KINDS:
            card = SC.scenario_card(kind)
            spec = SC.SCENARIOS[kind]
            for rnd in spec["rounds"]:
                for name in rnd["signals"]:
                    label = name.replace("_", " ")
                    self.assertIn(label, card,
                                  f"signal {name!r} missing from {kind} card")
                    guidance = SC._SIGNAL_GUIDANCE[name]
                    self.assertIn(guidance, card,
                                  f"guidance for {name!r} missing from "
                                  f"{kind} card")


class LabCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        p1 = mock.patch.object(ss, "_LAB_SESSIONS_PATH",
                               tmp / "lab_sessions.json")
        p2 = mock.patch.object(ss, "_LAB_LONGITUDINAL_HISTORY_PATH",
                               tmp / "hist.json")
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        self._hist = tmp / "hist.json"


class TestLabIntegration(LabCase):
    def test_kinds_lists_four(self):
        kinds = ss.scenario_kinds()
        self.assertEqual(len(kinds["kinds"]), 4)
        self.assertTrue(all("card" in k for k in kinds["kinds"]))

    def test_start_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            ss.start_scenario("karaoke")

    def test_full_run_records_longitudinal(self):
        s = ss.start_scenario("conflict")
        self.assertEqual(s["round"], 1)
        self.assertIn("Setup", s["card"])
        answers = [
            ("From their perspective, Sam is worried about the Friday "
             "deadline. As I see it, the shortcut risks an outage."),
            ("I propose we ship the safe path now and put the shortcut "
             "behind a flag. I will own the flag removal by Friday."),
            ("Agreed: I own the flag removal by Friday, Sam owns the "
             "load test by Monday. We ship the safe path."),
        ]
        last = None
        for i, a in enumerate(answers):
            last = ss.review_scenario(s["session_id"], a)
            if i < 2:
                self.assertFalse(last["done"])
                self.assertIn("next_prompt", last)
        self.assertTrue(last["done"])
        self.assertIn("debrief", last)
        self.assertEqual(last["debrief"]["rounds_total"], 3)

        import json
        history = json.loads(self._hist.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["source"], "soft_skill_scenario")
        self.assertEqual(history[0]["mode"], "conflict")

    def test_review_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            ss.review_scenario("nope", "answer")

    def test_review_complete_session_raises(self):
        s = ss.start_scenario("influence")
        for _ in range(3):
            ss.review_scenario(s["session_id"], "a decent answer here")
        with self.assertRaises(ValueError):
            ss.review_scenario(s["session_id"], "one more")

    def test_summary(self):
        s = ss.start_scenario("negotiation")
        ss.review_scenario(s["session_id"], "first answer here")
        summary = ss.scenario_summary(s["session_id"])
        self.assertFalse(summary["done"])
        self.assertEqual(len(summary["rounds"]), 1)
        self.assertIsNone(summary["debrief"])

    def test_voice_modality_recorded(self):
        s = ss.start_scenario("conflict")
        out = ss.review_scenario(s["session_id"], "an answer",
                                 input_modality="voice")
        self.assertEqual(out["input_modality"], "voice")


class TestFindingsFixes(unittest.TestCase):
    """Regression tests for the blind-review KICK_BACK findings."""

    def test_clarify_which_without_question_fails(self):
        # Bare "which" inside declarative prose must NOT earn the
        # clarifying_questions signal (finding 2).
        out = SC.evaluate_round(
            "ambiguous_stakeholder", 0,
            "I think we should build it with React which is fine.")
        self.assertFalse(
            out["signals"]["clarifying_questions"]["passed"])

    def test_clarify_real_question_passes(self):
        out = SC.evaluate_round(
            "ambiguous_stakeholder", 0,
            "Which users rely on this today? What does success look "
            "like for them?")
        self.assertTrue(
            out["signals"]["clarifying_questions"]["passed"])

    def test_clarify_wh_opener_without_mark_passes(self):
        # "which" as a genuine question word counts even without "?".
        out = SC.evaluate_round(
            "ambiguous_stakeholder", 0,
            "Which team owns this dashboard today")
        self.assertTrue(
            out["signals"]["clarifying_questions"]["passed"])

    def test_blameless_passes_no_blame(self):
        # "blameless" must not trigger the "blame" signal (finding 3).
        out = SC.evaluate_round(
            "conflict", 0,
            "I propose we run a blameless postmortem and ship the safe "
            "path.")
        self.assertTrue(out["signals"]["no_blame"]["passed"])

    def test_blame_language_fails_no_blame(self):
        out = SC.evaluate_round(
            "conflict", 0,
            "You always blame the team when it slips.")
        self.assertFalse(out["signals"]["no_blame"]["passed"])

    def test_misconfigured_signal_raises_value_error(self):
        # A new signal with None phrases and no dedicated handler is a
        # config bug -> descriptive ValueError, not AssertionError
        # (finding 7).
        with self.assertRaises(ValueError):
            SC._check_signal("brand_new_signal", None, "some answer")
        with self.assertRaises(ValueError):
            SC._check_signal("clarifying_questions", None, "some answer")

    def test_question_quality_deliverable(self):
        # The disclosed question_quality focus on ambiguous_stakeholder
        # round 0 must be reachable via the entry point (finding 5).
        q = ["What does success look like for the sales team in the "
             "first month?"]
        out = SC.evaluate_round("ambiguous_stakeholder", 0,
                                "A short framing answer.",
                                candidate_questions=q)
        self.assertIsNotNone(
            out["rubric"]["dimension_scores"]["question_quality"])
        # Without candidate questions it stays unscored (None), never 0.
        out2 = SC.evaluate_round("ambiguous_stakeholder", 0,
                                 "A short framing answer.")
        self.assertIsNone(
            out2["rubric"]["dimension_scores"]["question_quality"])


if __name__ == "__main__":
    unittest.main()
