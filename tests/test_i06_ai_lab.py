"""Initiative 06 WS-D: AI proficiency lab exercises.

Covers initiatives.i06.ai_lab (specs + deterministic check) and the
ai_proficiency.py lab integration (tracks/exercise/submit,
longitudinal recording, wiring).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import ai_proficiency as ap  # noqa: E402
from initiatives.i06 import ai_lab as AL  # noqa: E402


class TestLabSpecs(unittest.TestCase):
    def test_tracks_and_types(self):
        self.assertEqual(len(AL.lab_tracks()), 6)
        for row in AL.lab_tracks():
            self.assertEqual(set(row["exercise_types"]),
                             {"ethics", "evaluation", "failure_analysis"})

    def test_every_track_has_all_three(self):
        for track in AL.LAB_TRACKS:
            for kind in ("ethics", "evaluation", "failure_analysis"):
                got = AL.get_lab_exercise(track, kind)
                self.assertIn("brief", got)
                self.assertIn("task", got)
                self.assertIn("good_looks_like", got)
                # Check spec is never disclosed.
                self.assertNotIn("check", got)
                self.assertNotIn("groups", str(got))

    def test_validate_rejects_unknown(self):
        with self.assertRaises(ValueError):
            AL.validate_lab_track("wizard")
        with self.assertRaises(ValueError):
            AL.validate_exercise_type("trivia")

    def test_check_passes_with_concepts_and_words(self):
        response = ("I would never use customer production data or PII in "
                    "a third-party prompt because we cannot control how "
                    "that data is retained. The policy must forbid it "
                    "explicitly. I would disclose AI assistance on my code "
                    "so reviewers know what to scrutinize. Use redacted "
                    "or synthetic data instead of the real customer "
                    "records, and remind teammates privately when they "
                    "cross the line.")
        out = AL.check_lab_exercise("engineer", "ethics", response)
        self.assertTrue(out["passed"])
        self.assertGreaterEqual(out["words"], out["min_words"])

    def test_check_fails_on_short_or_thin_answer(self):
        out = AL.check_lab_exercise("engineer", "ethics", "sounds good")
        self.assertFalse(out["passed"])

    def test_check_does_not_reveal_answer_key(self):
        out = AL.check_lab_exercise("engineer", "ethics", "x" * 500)
        self.assertNotIn("customer data", str(out).lower().replace(
            "groups", ""))


class LabCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_store = ap.STORE_PATH
        self._orig_hist = ap._LAB_LONGITUDINAL_HISTORY_PATH
        ap.STORE_PATH = Path(self._tmp.name) / "ai_proficiency.json"
        ap._LAB_LONGITUDINAL_HISTORY_PATH = Path(self._tmp.name) / "hist.json"
        self.addCleanup(self._restore)

    def _restore(self):
        ap.STORE_PATH = self._orig_store
        ap._LAB_LONGITUDINAL_HISTORY_PATH = self._orig_hist


def _good_response(track, kind):
    # A thorough response covering the exercise's concept groups.
    blobs = {
        ("engineer", "ethics"):
            "No customer data or production data or PII goes into "
            "third-party prompts; our policy forbids it because we cannot "
            "control retention or who sees it. I would disclose AI "
            "assistance on any code I submit for review so reviewers know "
            "what to scrutinize. Use redacted or synthetic data instead of "
            "real customer records, and remind the teammate privately "
            "rather than shaming them in the channel.",
        ("sales", "failure_analysis"):
            "The summary hallucinated an invented discount that was "
            "completely ungrounded in the call. I will apologize to the "
            "customer and make it right, honoring the price or offering a "
            "goodwill gesture. Going forward: mandatory rep review and "
            "sign-off on every summary, verify all numbers and quotes "
            "against the call notes, and never send summaries without "
            "explicit human approval from the rep.",
        ("generalist", "evaluation"):
            "I will trial the triage tool for one week and spot check a "
            "sample of messages every single day to see what it got "
            "wrong. I will track the miss rate on truly urgent items "
            "specifically, and stop using the tool if the false negative "
            "rate crosses my threshold; the test period ends after the "
            "trial week and I will decide then whether to keep it.",
    }
    return blobs.get((track, kind),
                     " ".join(g[0] for g in
                              AL.EXERCISES[track][kind]["check"]["groups"])
                     + " " + "detail " * 40)


class TestLabIntegration(LabCase):
    def test_lab_tracks(self):
        self.assertEqual(len(ap.lab_tracks()), 6)

    def test_exercise_unknown_returns_error(self):
        self.assertIn("error", ap.lab_exercise("wizard", "ethics"))
        self.assertIn("error", ap.lab_exercise("engineer", "trivia"))

    def test_submit_unknown_session_error(self):
        self.assertIn("error", ap.submit_lab_exercise("nope", "x"))

    def test_submit_twice_error(self):
        s = ap.lab_exercise("engineer", "ethics")
        ap.submit_lab_exercise(s["session_id"], _good_response("engineer", "ethics"))
        res = ap.submit_lab_exercise(s["session_id"], "again")
        self.assertIn("error", res)

    def test_pass_records_completion_and_history(self):
        s = ap.lab_exercise("sales", "failure_analysis")
        res = ap.submit_lab_exercise(s["session_id"],
                                     _good_response("sales", "failure_analysis"))
        self.assertTrue(res["passed"])
        self.assertIn("not a credential", res["feedback"].lower())
        import json
        store = json.loads(ap.STORE_PATH.read_text(encoding="utf-8"))
        labs = [c for c in store["completions"] if c.get("kind") == "lab"]
        self.assertEqual(len(labs), 1)
        hist = json.loads(Path(self._tmp.name, "hist.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["source"], "ai_lab")

    def test_fail_gives_actionable_feedback(self):
        s = ap.lab_exercise("engineer", "ethics")
        res = ap.submit_lab_exercise(s["session_id"], "sounds fine")
        self.assertFalse(res["passed"])
        self.assertIn("groups", res["feedback"])

    def test_voice_modality_recorded(self):
        s = ap.lab_exercise("generalist", "evaluation")
        res = ap.submit_lab_exercise(
            s["session_id"], _good_response("generalist", "evaluation"),
            input_modality="voice")
        self.assertEqual(res["input_modality"], "voice")


class TestWordBoundaryMatching(unittest.TestCase):
    """Regression tests: substring false positives must not count."""

    def test_lie_does_not_match_client_believe_relies(self):
        for decoy in ("the client trusts us",
                      "we believe in transparency",
                      "the team relies on templates"):
            self.assertFalse(AL._keyword_hit("lie", decoy), decoy)
        self.assertTrue(AL._keyword_hit("lie", "do not lie to prospects"))
        self.assertTrue(AL._keyword_hit("lie", "he lies constantly"))

    def test_ground_does_not_match_background(self):
        self.assertFalse(AL._keyword_hit(
            "ground", "in the background of this incident"))
        self.assertTrue(AL._keyword_hit(
            "ground", "ground the summary in retrieved sources"))
        self.assertTrue(AL._keyword_hit(
            "ground", "grounding and grounded generation"))

    def test_stem_keywords_allow_inflections(self):
        self.assertTrue(AL._keyword_hit("hallucinat", "hallucinated a refund"))
        self.assertTrue(AL._keyword_hit("retriev", "retrieval augmented"))
        self.assertTrue(AL._keyword_hit("verif", "verified personalization"))
        self.assertTrue(AL._keyword_hit("apolog", "apologize to the customer"))

    def test_phrase_keywords_need_the_whole_phrase(self):
        self.assertTrue(AL._keyword_hit(
            "customer data", "no customer data in prompts"))
        self.assertFalse(AL._keyword_hit(
            "customer data", "no customer production data in prompts"))
        self.assertFalse(AL._keyword_hit("must not", "we must act now"))
        self.assertTrue(AL._keyword_hit(
            "must not", "the policy says we must not do it"))

    def test_sales_ethics_decoy_response_fails(self):
        # Stuffed with the old false-positive decoys (client/believes/
        # relies); long enough on words but must not pass.
        decoy = ("Every client believes our outreach because each client "
                 "relies on our templates. I believe the client relies on "
                 "us to write carefully. Clients believe what they read, "
                 "and each client relies on that. ")
        out = AL.check_lab_exercise("sales", "ethics", decoy * 8)
        self.assertGreaterEqual(out["words"], out["min_words"])
        self.assertFalse(out["passed"])
        self.assertEqual(out["groups_hit"], 0)

    def test_engineer_failure_analysis_background_decoy(self):
        # "background" must not satisfy the grounding group.
        decoy = ("In the background of this incident the background systems "
                 "kept running while the background jobs retried. ")
        response = decoy * 8
        groups = AL.EXERCISES["engineer"]["failure_analysis"]["check"]["groups"]
        self.assertFalse(any(AL._keyword_hit(kw, response.lower())
                             for kw in groups[2]))
        out = AL.check_lab_exercise("engineer", "failure_analysis", response)
        self.assertFalse(out["passed"])


class TestAllExerciseSpecs(unittest.TestCase):
    """Standard pass/fail paths across all 18 exercise specs."""

    def _concept_response(self, track, kind):
        groups = AL.EXERCISES[track][kind]["check"]["groups"]
        return (" ".join(g[0] for g in groups) + " " + "detail " * 40)

    def test_all_specs_pass_with_concept_coverage(self):
        for track in AL.LAB_TRACKS:
            for kind in ("ethics", "evaluation", "failure_analysis"):
                out = AL.check_lab_exercise(
                    track, kind, self._concept_response(track, kind))
                self.assertTrue(out["passed"], f"{track}/{kind}")
                self.assertEqual(out["groups_hit"], out["groups_total"])
                self.assertEqual(out["missed_group_descriptions"], [])

    def test_all_specs_fail_when_thin(self):
        for track in AL.LAB_TRACKS:
            for kind in ("ethics", "evaluation", "failure_analysis"):
                out = AL.check_lab_exercise(track, kind, "sounds good")
                self.assertFalse(out["passed"], f"{track}/{kind}")

    def test_word_count_alone_does_not_pass(self):
        for track in AL.LAB_TRACKS:
            for kind in ("ethics", "evaluation", "failure_analysis"):
                out = AL.check_lab_exercise(
                    track, kind, "detail " * 60)
                self.assertFalse(out["passed"], f"{track}/{kind}")


class TestSingleSourceOfTruth(unittest.TestCase):
    def test_lab_tracks_derived_from_exercises(self):
        self.assertEqual(AL.LAB_TRACKS, tuple(AL.EXERCISES))
        self.assertEqual(AL.LAB_TRACKS, tuple(AL.EXERCISES.keys()))
        for t in AL.LAB_TRACKS:
            self.assertEqual(AL.validate_lab_track(t), t)

    def test_lab_tracks_rows_well_formed(self):
        for row in AL.lab_tracks():
            self.assertIsInstance(row["track"], str)
            self.assertIsInstance(row["exercise_types"], list)
            self.assertTrue(all(isinstance(k, str)
                                for k in row["exercise_types"]))


class TestMissedGroupDescriptions(unittest.TestCase):
    def test_missed_groups_described_generically(self):
        out = AL.check_lab_exercise("engineer", "ethics", "sounds good")
        self.assertEqual(out["groups_missed"], out["groups_total"])
        self.assertEqual(len(out["missed_group_descriptions"]),
                         out["groups_missed"])
        blob = " ".join(out["missed_group_descriptions"]).lower()
        for group in AL.EXERCISES["engineer"]["ethics"]["check"]["groups"]:
            for kw in group:
                self.assertNotIn(kw, blob, f"keyword {kw!r} leaked")

    def test_partial_miss_describes_only_missed(self):
        # Hits 3 of 4 engineer/ethics groups; exactly one description.
        response = ("No customer data or production data or PII in prompts. "
                    "I would disclose AI assistance on my code. "
                    "Use redacted or synthetic data instead. "
                    + "detail " * 40)
        out = AL.check_lab_exercise("engineer", "ethics", response)
        self.assertTrue(out["passed"])
        self.assertEqual(out["groups_missed"], 1)
        self.assertEqual(len(out["missed_group_descriptions"]), 1)


class TestKnownCheckerLimits(unittest.TestCase):
    def test_negation_is_not_detected_documented_limitation(self):
        # The checker measures keyword presence, not reasoning: a negated
        # mention still satisfies the group. Documented in the module
        # docstring; pinned here so the claim stays true.
        text = ("We should not disclose anything and must not disclose "
                "data to anyone. " * 10).lower()
        self.assertTrue(AL._keyword_hit("disclose", text))


if __name__ == "__main__":
    unittest.main()
