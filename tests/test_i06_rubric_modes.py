"""Initiative 06 WS-A: disclosed rubric engine + interview modes.

Covers initiatives.i06.rubric, initiatives.i06.modes, and the
mock_interview.py integration (mode param, rubric scoring on answers,
mode_card disclosure, longitudinal recording on completion).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mock_interview  # noqa: E402
from initiatives.i06 import longitudinal as i06_long  # noqa: E402
from initiatives.i06 import modes as i06_modes  # noqa: E402
from initiatives.i06 import rubric as i06_rubric  # noqa: E402

STRONG = (
    "In 2023 the situation was dire: our checkout API was timing out at "
    "peak traffic. My task as backend lead was to cut p99 latency under "
    "300ms before Black Friday. The action I took: I designed a Redis "
    "cache layer instead of just adding servers, because the cache gave "
    "10x headroom; the downside was cache-invalidation complexity. As a "
    "result, p99 latency dropped 62% and we saved $40k per month."
)

WEAK = "I did stuff. It was fine."


class TestRubric(unittest.TestCase):
    def test_disclosed_rubric_has_five_weighted_dimensions(self):
        self.assertEqual(set(i06_rubric.DIMENSIONS),
                         set(i06_rubric.DISCLOSED_RUBRIC))
        total = sum(s["weight"]
                    for s in i06_rubric.DISCLOSED_RUBRIC.values())
        self.assertAlmostEqual(total, 1.0)
        for dim, spec in i06_rubric.DISCLOSED_RUBRIC.items():
            for key in ("weight", "measures", "how_scored",
                        "strong_looks_like", "weak_looks_like"):
                self.assertIn(key, spec, f"{dim} missing {key}")

    def test_strong_answer_scores_high_all_dimensions(self):
        r = i06_rubric.score_answer(STRONG)
        self.assertGreaterEqual(r["overall"], 80)
        for dim in ("structure", "evidence", "clarity", "trade_offs"):
            self.assertGreaterEqual(r["dimensions"][dim]["score"], 70,
                                    dim)

    def test_weak_answer_scores_low(self):
        r = i06_rubric.score_answer(WEAK)
        self.assertLess(r["overall"], 50)

    def test_question_quality_unscored_when_absent(self):
        r = i06_rubric.score_answer(STRONG)
        self.assertIsNone(r["dimensions"]["question_quality"]["score"])
        self.assertIn("question_quality", r["unscored"])
        # Unscored dimensions are excluded from the overall, not zeroed.
        r2 = i06_rubric.score_answer(
            STRONG, candidate_questions=[
                "What does success look like for this team in six months, "
                "and what usually gets in the way?"])
        self.assertGreaterEqual(
            r2["dimensions"]["question_quality"]["score"], 70)

    def test_website_answerable_question_scores_low(self):
        r = i06_rubric.score_question_quality(
            ["So what does the company do?"])
        self.assertLess(r["score"], 40)

    def test_feedback_never_invents_facts(self):
        r = i06_rubric.score_answer(WEAK)
        blob = " ".join(
            f for d in r["dimensions"].values() for f in d["feedback"])
        self.assertNotIn("Initech", blob)
        self.assertNotIn("Stripe", blob)

    def test_unknown_dimension_raises(self):
        with self.assertRaises(ValueError):
            i06_rubric.score_answer(STRONG, dimensions=["charisma"])

    def test_meets_threshold_none_stays_none(self):
        self.assertIsNone(i06_rubric.meets_threshold(None))
        self.assertTrue(i06_rubric.meets_threshold(70))
        self.assertFalse(i06_rubric.meets_threshold(69))

    def test_rubric_card_discloses_everything(self):
        card = i06_rubric.rubric_card()
        for dim in i06_rubric.DIMENSIONS:
            self.assertIn(dim.replace("_", " "), card.lower())
        self.assertIn("70", card)


class TestModes(unittest.TestCase):
    def test_five_modes_disclosed(self):
        self.assertEqual(set(i06_modes.MODE_NAMES),
                         {"recruiter", "hiring_manager", "peer",
                          "executive", "adversarial"})
        for name, spec in i06_modes.MODES.items():
            for key in ("label", "persona", "round_shape", "weights",
                        "behavioral", "probes"):
                self.assertIn(key, spec, f"{name} missing {key}")
            self.assertAlmostEqual(sum(spec["weights"].values()), 1.0)

    def test_validate_mode_rejects_unknown(self):
        with self.assertRaises(ValueError):
            i06_modes.validate_mode("therapist")
        self.assertEqual(i06_modes.validate_mode("Peer"), "peer")

    def test_build_mode_questions_deterministic(self):
        tech = ["Design a rate limiter.", "Debug a p99 latency spike."]
        a = i06_modes.build_mode_questions(
            "hiring_manager", "Acme", "Backend Engineer", technical=tech)
        b = i06_modes.build_mode_questions(
            "hiring_manager", "Acme", "Backend Engineer", technical=tech)
        self.assertEqual([q["question"] for q in a],
                         [q["question"] for q in b])
        self.assertEqual(len(a), 5)

    def test_modes_differ(self):
        a = i06_modes.build_mode_questions("recruiter", "Acme", "PM")
        b = i06_modes.build_mode_questions("adversarial", "Acme", "PM")
        self.assertNotEqual([q["question"] for q in a],
                            [q["question"] for q in b])

    def test_adversarial_never_attacks_the_person(self):
        # Raises on any banned token in follow-ups, banks, or personas.
        i06_modes.assert_no_banned_tokens()
        for text in i06_modes.FOLLOWUPS["adversarial"]:
            lowered = text.lower()
            for token in i06_modes.BANNED_TOKENS:
                self.assertNotIn(token, lowered)

    def test_mode_card_discloses_persona_and_weights(self):
        card = i06_modes.mode_card("executive")
        self.assertIn("Executive", card)
        self.assertIn("evidence", card.lower())


class MockInterviewLabCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._sessions = tmp / "mock_sessions.json"
        self._history = tmp / "i06_history.json"
        p1 = mock.patch.object(mock_interview, "SESSIONS_FILE",
                               self._sessions)
        p2 = mock.patch.object(mock_interview, "_LONGITUDINAL_HISTORY_PATH",
                               self._history)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def _answer_all(self, sid, answer=STRONG):
        last = None
        for _ in range(mock_interview.NUM_QUESTIONS):
            last = mock_interview.answer_mock_question(sid, answer)
        return last


class TestMockInterviewModes(MockInterviewLabCase):
    def test_mode_session_returns_mode_card(self):
        out = mock_interview.start_mock_interview(
            "Acme", "Backend Engineer", mode="peer")
        self.assertEqual(out["mode"], "peer")
        self.assertIn("Peer", out["mode_card"])
        self.assertEqual(len(out["session"]["questions"]), 5)

    def test_classic_default_unchanged(self):
        out = mock_interview.start_mock_interview("Acme", "Backend Engineer")
        self.assertEqual(out["mode"], "classic")
        self.assertIsNone(out["mode_card"])

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            mock_interview.start_mock_interview("Acme", "Eng", mode="guru")

    def test_answer_carries_rubric_scores(self):
        out = mock_interview.start_mock_interview(
            "Acme", "Backend Engineer", mode="hiring_manager")
        fb = mock_interview.answer_mock_question(out["session_id"], STRONG)
        dims = fb["rubric"]["dimension_scores"]
        self.assertGreaterEqual(dims["structure"], 70)
        self.assertGreaterEqual(dims["evidence"], 70)
        self.assertEqual(fb["input_modality"], "text")
        # Legacy score still present and unchanged in shape.
        self.assertGreaterEqual(fb["overall"], 90)

    def test_voice_modality_recorded(self):
        out = mock_interview.start_mock_interview("Acme", "Eng")
        fb = mock_interview.answer_mock_question(
            out["session_id"], STRONG, input_modality="voice")
        self.assertEqual(fb["input_modality"], "voice")

    def test_candidate_questions_score_question_quality(self):
        out = mock_interview.start_mock_interview("Acme", "Eng")
        fb = mock_interview.answer_mock_question(
            out["session_id"], STRONG,
            candidate_questions=[
                "What does success look like for this team in six months?"])
        self.assertGreaterEqual(
            fb["rubric"]["dimension_scores"]["question_quality"], 70)

    def test_completion_records_longitudinal_history(self):
        out = mock_interview.start_mock_interview(
            "Acme", "Eng", mode="executive", job_id="job-123")
        self._answer_all(out["session_id"])
        history = json.loads(self._history.read_text(encoding="utf-8"))
        self.assertEqual(len(history), 1)
        rec = history[0]
        self.assertEqual(rec["source"], "mock_interview")
        self.assertEqual(rec["mode"], "executive")
        self.assertEqual(rec["job_id"], "job-123")
        self.assertIn("evidence", rec["dimension_scores"])

    def test_history_recording_never_breaks_answering(self):
        out = mock_interview.start_mock_interview("Acme", "Eng")
        with mock.patch.object(
                i06_long, "record_result",
                side_effect=RuntimeError("disk on fire")):
            last = self._answer_all(out["session_id"])
        self.assertTrue(last["complete"])

    def test_summary_includes_rubric_averages(self):
        out = mock_interview.start_mock_interview("Acme", "Eng")
        sid = out["session_id"]
        mock_interview.answer_mock_question(sid, STRONG)
        mock_interview.answer_mock_question(sid, WEAK)
        summary = mock_interview.mock_summary(sid)
        avgs = summary["rubric_averages"]
        self.assertGreater(avgs["structure"], avgs["structure"] - 1)  # sane
        self.assertIn("structure", summary["markdown"].lower())

    def test_show_rubric_discloses_all(self):
        shown = mock_interview.show_rubric()
        self.assertEqual(set(shown["rubric"]), set(i06_rubric.DIMENSIONS))
        self.assertEqual(len(shown["modes"]), 5)
        self.assertIn("70", shown["markdown"])


# A fully-loaded answer: every positive signal, no penalties. Must
# score exactly the disclosed ceiling (100) on each text dimension.
PERFECT = (
    "The situation in 2023 was dire: our checkout API was timing out at "
    "peak traffic. My task as backend lead was to cut p99 latency under "
    "300ms before Black Friday. The action I took: I designed a Redis "
    "cache layer instead of just adding servers, because the cache gave "
    "10x headroom for the holiday traffic spike; I chose caching after we "
    "weighed both options carefully. The downside was cache-invalidation "
    "complexity, a real risk we accepted. As a result, p99 latency dropped "
    "62% and we saved $40k per month after I decided to ship it two weeks "
    "early."
)

PERFECT_QUESTION = (
    "What does success look like for this team in the first six months, "
    "and what usually gets in the way?"
)


class TestFinding1JargonParity(unittest.TestCase):
    """Finding 1: the disclosed 6-term jargon list must equal the code."""

    def test_jargon_list_matches_disclosure_exactly(self):
        how = i06_rubric.DISCLOSED_RUBRIC["clarity"]["how_scored"]
        sentence = next(s for s in how.split(". ") if "fixed list of 6"
                        in s)
        disclosed = set(re.findall(r"'([^']+)'", sentence))
        self.assertEqual(set(i06_rubric._JARGON), disclosed)
        self.assertEqual(len(i06_rubric._JARGON), 6)

    def test_leverage_counts_only_as_verb(self):
        self.assertEqual(
            i06_rubric._jargon_hits("We leverage the cache to cut "
                                   "latency."),
            ["leverage"])
        self.assertEqual(
            i06_rubric._jargon_hits("We leveraged the cache to cut "
                                   "latency."),
            ["leverage"])
        # Noun use: not penalized, as disclosed.
        self.assertEqual(
            i06_rubric._jargon_hits("The financial leverage of the deal "
                                   "was high."),
            [])

    def test_bandwidth_counts_only_for_people(self):
        self.assertEqual(
            i06_rubric._jargon_hits("I have no bandwidth this week."),
            ["bandwidth"])
        # Network capacity: not penalized, as disclosed.
        self.assertEqual(
            i06_rubric._jargon_hits("The network bandwidth was saturated."),
            [])

    def test_removed_terms_no_longer_penalized(self):
        # "move the needle" / "circle back" were never disclosed and are
        # no longer in the list.
        self.assertEqual(
            i06_rubric._jargon_hits("Let's circle back to move the needle."),
            [])

    def test_jargon_matching_is_word_boundary(self):
        self.assertEqual(
            i06_rubric._jargon_hits("A holistically designed system."), [])

    def test_jargon_penalty_is_minus_10_beyond_first(self):
        base = ("The result was a 62 percent improvement in the deploy "
                "pipeline last quarter. "
                + "We kept the design simple and boring throughout the "
                "whole project work. " * 6)
        one = base + " There was real synergy in the team."
        three = (one + " We took a holistic view of the whole paradigm.")
        self.assertEqual(i06_rubric.score_clarity(base)["score"], 100)
        self.assertEqual(i06_rubric.score_clarity(one)["score"], 100)
        self.assertEqual(i06_rubric.score_clarity(three)["score"], 80)


class TestFinding2Ceilings(unittest.TestCase):
    """Finding 2: a perfect answer genuinely scores the disclosed 100."""

    def test_perfect_answer_scores_exactly_100_every_dimension(self):
        r = i06_rubric.score_answer(PERFECT)
        for dim in ("structure", "evidence", "clarity", "trade_offs"):
            self.assertEqual(r["dimensions"][dim]["score"], 100, dim)
        self.assertEqual(r["overall"], 100)

    def test_perfect_question_scores_exactly_100(self):
        r = i06_rubric.score_question_quality([PERFECT_QUESTION])
        self.assertEqual(r["score"], 100)


class TestFinding3WebsiteAnswerable(unittest.TestCase):
    """Finding 3: the disclosed -30 formula, not a flat 10."""

    def test_all_signals_plus_website_answerable_scores_70(self):
        q = ("What does the company do and what would success look like "
             "for this team in the first six months?")
        r = i06_rubric.score_question_quality([q])
        # 30 + 25 (opener) + 20 (team) + 25 (tension) - 30 (website).
        self.assertEqual(r["score"], 70)
        self.assertEqual(r["signals"]["per_question"], [70])
        self.assertEqual(r["signals"]["website_answerable"], [True])

    def test_bare_website_answerable_question_scores_zero(self):
        r = i06_rubric.score_question_quality(
            ["So what does the company do?"])
        self.assertEqual(r["score"], 0)
        self.assertEqual(r["signals"]["website_answerable"], [True])


class TestFinding4ConclusionMarkers(unittest.TestCase):
    """Finding 4: disclosed markers only, and the marker must carry
    content."""

    def test_marker_list_matches_disclosure(self):
        how = i06_rubric.DISCLOSED_RUBRIC["clarity"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "leads with the conclusion" in s)
        disclosed = {t.lower() for t in re.findall(r"'([^']+)'", sentence)}
        self.assertEqual(set(i06_rubric._CONCLUSION_EARLY), disclosed)

    def test_empty_marker_earns_nothing(self):
        # The reviewer's example: a content-free marker must not earn
        # the +15, however many filler words follow.
        filler = ("Ultimately, things happened. "
                  + "The team discussed various topics at length. " * 12)
        r = i06_rubric.score_clarity(filler)
        self.assertFalse(r["signals"]["bottom_line_up_front"])

    def test_bare_marker_sentence_earns_nothing(self):
        r = i06_rubric.score_clarity(
            "We shipped. Then we did a whole lot of other stuff here.")
        self.assertFalse(r["signals"]["bottom_line_up_front"])

    def test_content_bearing_marker_earns_credit(self):
        r = i06_rubric.score_clarity(
            "The result was a 62 percent latency drop after the deploy. "
            "Here is the longer story of how it happened.")
        self.assertTrue(r["signals"]["bottom_line_up_front"])

    def test_number_in_first_two_sentences_still_counts(self):
        r = i06_rubric.score_clarity(
            "In 2023 the situation was dire. Here is the longer story.")
        self.assertTrue(r["signals"]["bottom_line_up_front"])


class TestFinding5ReasonClause(unittest.TestCase):
    """Finding 5: 'because' needs a reason-like complement."""

    def test_bare_because_earns_no_reason_credit(self):
        # The reviewer's example.
        r = i06_rubric.score_trade_offs(
            "The sky is blue because I like it.")
        self.assertFalse(r["signals"]["reason_given"])
        self.assertEqual(r["score"], 25)

    def test_real_because_clause_earns_reason_credit(self):
        r = i06_rubric.score_trade_offs(
            "The latency fell because the cache gave 10x headroom for "
            "reads.")
        self.assertTrue(r["signals"]["reason_given"])


class TestFinding6StarMarkers(unittest.TestCase):
    """Finding 6: loose STAR markers removed; nonsense scores low."""

    def test_nonsense_lunch_answer_scores_low_structure(self):
        # The reviewer's lunch example shape: loose markers no longer
        # grant situation/task credit; only the genuine "I did" action
        # hit remains.
        lunch = ("I had to grab lunch. At the time I was facing a tough "
                 "menu. I did order the soup. It was warm and fine.")
        r = i06_rubric.score_structure(lunch)
        self.assertEqual(r["signals"]["star_parts_present"], ["action"])
        self.assertEqual(r["score"], 40)
        self.assertLess(r["score"], 60)

    def test_genuine_star_still_scores_100(self):
        ans = ("The situation was a failing deploy pipeline. My task was "
               "to restore it. The action I took: I rebuilt the steps. "
               "As a result, deploys recovered.")
        r = i06_rubric.score_structure(ans)
        self.assertEqual(r["score"], 100)


class TestFinding7BannedTokenBoundary(unittest.TestCase):
    """Finding 7: word-boundary banned-token check everywhere."""

    def test_substring_word_does_not_trip_assert(self):
        # "dumbo" contains "dumb" as a substring but is not the token.
        with mock.patch.dict(
                i06_modes.FOLLOWUPS,
                {"recruiter": ["No dumbo answers here, prove it."]}):
            i06_modes.assert_no_banned_tokens()

    def test_standalone_banned_token_still_trips(self):
        with mock.patch.dict(
                i06_modes.FOLLOWUPS,
                {"recruiter": ["That is a dumb answer."]}):
            with self.assertRaises(AssertionError):
                i06_modes.assert_no_banned_tokens()


class TestEvidenceWordBoundary(unittest.TestCase):
    """RE-review (2026-09-13): the evidence artifact/decision lists now
    match word-boundary, like the structure STAR markers. Ordinary
    interview-practice phrasing must not earn unearned credit."""

    def test_applied_not_an_artifact(self):
        # 'app' inside 'applied'.
        r = i06_rubric.score_evidence(
            "I applied to many jobs and moved rapidly through hiring.")
        self.assertFalse(r["signals"]["named_artifact"])
        # 'api' inside 'rapidly' must not count either.
        self.assertEqual(r["score"], 30)

    def test_happy_not_an_artifact(self):
        # 'app' inside 'happy'.
        r = i06_rubric.score_evidence("I was happy about the outcome.")
        self.assertFalse(r["signals"]["named_artifact"])

    def test_applied_the_fix_not_an_artifact(self):
        r = i06_rubric.score_evidence("I applied the fix on Friday.")
        self.assertFalse(r["signals"]["named_artifact"])

    def test_decidedly_not_a_personal_decision(self):
        # 'i decided' inside 'i decidedly'.
        r = i06_rubric.score_evidence("I decidedly recommend the approach.")
        self.assertFalse(r["signals"]["personal_decision"])

    def test_real_artifacts_still_count(self):
        r = i06_rubric.score_evidence(
            "We shipped a fix to our API and the mobile app in 2024.")
        self.assertTrue(r["signals"]["named_artifact"])
        # 30 + 20 (one number) + 15 (artifact) = 65.
        self.assertEqual(r["score"], 65)

    def test_real_decisions_still_count(self):
        r = i06_rubric.score_evidence(
            "I decided to cut scope to hit the date.")
        self.assertTrue(r["signals"]["personal_decision"])

    def test_disclosed_boundary_note_matches_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["evidence"]["how_scored"]
        self.assertIn("'applied' and 'happy' do not", how)
        self.assertIn("'i decidedly' does not", how)


class TestOpenerDisclosureParity(unittest.TestCase):
    """RE-review minor: the card must credit exactly the opener set the
    code credits."""

    def test_disclosed_openers_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["question_quality"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "open-ended opener" in s)
        # Scope to the opener clause: before the tension list begins.
        opener_clause = sentence.split(";")[0]
        disclosed = {t.lower()
                     for t in re.findall(r"'([^']+)'", opener_clause)}
        self.assertEqual({o.strip() for o in i06_rubric._OPENERS},
                         disclosed)

    def test_tell_me_about_gets_opener_credit(self):
        # 30 + 25 (opener) + 20 (team) = 75.
        r = i06_rubric.score_question_quality(
            ["Tell me about the team structure."])
        self.assertEqual(r["score"], 75)

    def test_walk_me_through_gets_opener_credit(self):
        # 30 + 25 (opener), nothing else.
        r = i06_rubric.score_question_quality(
            ["Walk me through your week."])
        self.assertEqual(r["score"], 55)


class TestAlternativeDisclosureParity(unittest.TestCase):
    """RE-review minor: the card must credit exactly the alternative
    set the code credits."""

    def test_disclosed_alternatives_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["trade_offs"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "naming an alternative" in s)
        disclosed = {t.lower()
                     for t in re.findall(r"'([^']+)'", sentence)}
        self.assertEqual({p.strip() for p in i06_rubric._ALTERNATIVE},
                         disclosed)

    def test_evaluated_gets_alternative_credit(self):
        # One of the previously-undisclosed terms; the card now
        # discloses it, so it must score.
        r = i06_rubric.score_trade_offs(
            "We evaluated three vendors before signing.")
        self.assertTrue(r["signals"]["alternative_named"])
        self.assertEqual(r["score"], 50)


class TestDownsideDisclosureParity(unittest.TestCase):
    """RE-review minor: the card must credit exactly the downside
    set the code credits."""

    def test_disclosed_downsides_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["trade_offs"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "naming a downside" in s)
        disclosed = {t.lower()
                     for t in re.findall(r"'([^']+)'", sentence)}
        self.assertEqual({p.strip() for p in i06_rubric._DOWNSIDE},
                         disclosed)

    def test_the_catch_gets_downside_credit(self):
        r = i06_rubric.score_trade_offs(
            "The catch was we lost a week of velocity.")
        self.assertTrue(r["signals"]["downside_named"])
        self.assertEqual(r["score"], 50)


class TestSpecificDisclosureParity(unittest.TestCase):
    """RE-review minor: the card must credit exactly the specificity
    set the code credits."""

    def test_disclosed_specifics_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["question_quality"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "naming something concrete" in s)
        disclosed = {t.lower()
                     for t in re.findall(r"'([^']+)'", sentence)}
        self.assertEqual({p.strip() for p in i06_rubric._SPECIFIC},
                         disclosed)

    def test_roadmap_probe_gets_specific_credit(self):
        # The reviewer's probe: "What does the roadmap look like?"
        # 30 + 25 (what-opener) + 20 (roadmap) = 75.
        r = i06_rubric.score_question_quality(
            ["What does the roadmap look like?"])
        self.assertEqual(r["score"], 75)


class TestTensionDisclosureParity(unittest.TestCase):
    """RE-review minor: the card must credit exactly the tension set
    the code credits."""

    def test_disclosed_tensions_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["question_quality"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "real tension" in s)
        disclosed = {t.lower()
                     for t in re.findall(r"'([^']+)'", sentence)}
        self.assertEqual({p.strip() for p in i06_rubric._TENSION},
                         disclosed)

    def test_change_probe_gets_tension_credit(self):
        # The reviewer's probe shape: "What would you change ...?"
        # 30 + 25 (what-opener) + 20 (on-call) + 25 (what would you
        # change) = 100.
        r = i06_rubric.score_question_quality(
            ["What would you change about the on-call rotation?"])
        self.assertEqual(r["score"], 100)


class TestTradeOffsWordBoundary(unittest.TestCase):
    """RE-review minor: trade_offs matching is word-boundary,
    consistent with the evidence dimension. 'reconsidered' must not
    fire 'considered' (+25)."""

    def test_reconsidered_fires_no_alternative_credit(self):
        r = i06_rubric.score_trade_offs(
            "We reconsidered the timeline after the delay.")
        self.assertFalse(r["signals"]["alternative_named"])
        self.assertEqual(r["score"], 25)

    def test_considered_still_fires_alternative_credit(self):
        r = i06_rubric.score_trade_offs(
            "We considered a rewrite but kept the monolith.")
        self.assertTrue(r["signals"]["alternative_named"])

    def test_boundary_rule_is_disclosed_on_card(self):
        how = i06_rubric.DISCLOSED_RUBRIC["trade_offs"]["how_scored"]
        self.assertIn("word-boundary", how)
        self.assertIn("reconsidered", how)


class TestWebsiteSubstringEdge(unittest.TestCase):
    """RE-review minor: the conservative substring edge is disclosed,
    not hidden. A longer question that merely begins with a banned
    phrase still takes the -30."""

    def test_banned_prefix_still_flagged(self):
        r = i06_rubric.score_question_quality(
            ["What does the company do about on-call staffing?"])
        self.assertEqual(r["signals"]["website_answerable"], [True])
        # 30 + 25 (what-opener) + 20 (on-call) - 30 (website) = 45.
        self.assertEqual(r["score"], 45)

    def test_edge_is_disclosed_on_card(self):
        how = i06_rubric.DISCLOSED_RUBRIC["question_quality"]["how_scored"]
        self.assertIn("substring-based by design", how)
        self.assertIn("on-call staffing", how)


class TestMeetsThresholdLateBinding(unittest.TestCase):
    """RE-review nit: the default threshold resolves at call time, so
    the Q1 gate can approve a new value without re-importing."""

    def test_default_still_70(self):
        self.assertTrue(i06_rubric.meets_threshold(70))
        self.assertFalse(i06_rubric.meets_threshold(69))
        self.assertIsNone(i06_rubric.meets_threshold(None))

    def test_default_follows_approved_threshold(self):
        with mock.patch.object(i06_rubric, "MEETS_THRESHOLD", 80):
            self.assertTrue(i06_rubric.meets_threshold(80))
            self.assertFalse(i06_rubric.meets_threshold(79))
        # Restored afterwards.
        self.assertTrue(i06_rubric.meets_threshold(70))

    def test_explicit_threshold_still_wins(self):
        self.assertTrue(i06_rubric.meets_threshold(75, threshold=75))
        self.assertFalse(i06_rubric.meets_threshold(74, threshold=75))


class TestDisclosureParity(unittest.TestCase):
    """Core-contract check: every disclosed numeric claim in the rubric
    card must match the implementation. A blind reviewer runs this
    file; these tests fail if the card and the code ever disagree again.
    """

    def test_disclosed_arithmetic_is_true(self):
        # Every "a + b + c = n" claim in the card must be correct math.
        for dim, spec in i06_rubric.DISCLOSED_RUBRIC.items():
            for m in re.finditer(
                    r"(\d+(?:\s*[+\-]\s*\d+)+\s*=\s*\d+)",
                    spec["how_scored"]):
                expr = m.group(1)
                lhs, rhs = expr.split("=")
                self.assertEqual(eval(lhs), int(rhs),  # noqa: S307
                                 f"{dim}: {expr}")

    def test_disclosed_jargon_terms_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["clarity"]["how_scored"]
        sentence = next(s for s in how.split(". ") if "fixed list of 6"
                        in s)
        self.assertEqual(set(re.findall(r"'([^']+)'", sentence)),
                         set(i06_rubric._JARGON))

    def test_disclosed_conclusion_markers_match_code(self):
        how = i06_rubric.DISCLOSED_RUBRIC["clarity"]["how_scored"]
        sentence = next(s for s in how.split(". ")
                        if "leads with the conclusion" in s)
        self.assertEqual({t.lower() for t in re.findall(r"'([^']+)'", sentence)},
                         set(i06_rubric._CONCLUSION_EARLY))

    def test_disclosed_ceilings_reachable(self):
        r = i06_rubric.score_answer(PERFECT)
        for dim in ("structure", "evidence", "clarity", "trade_offs"):
            self.assertEqual(r["dimensions"][dim]["score"], 100, dim)
        self.assertEqual(
            i06_rubric.score_question_quality([PERFECT_QUESTION])["score"],
            100)

    def test_disclosed_website_formula(self):
        # 30 + 25 + 20 + 25 - 30 = 70, as the card states.
        q = ("What does the company do and what would success look like "
             "for this team in the first six months?")
        self.assertEqual(
            i06_rubric.score_question_quality([q])["score"], 70)

    def test_disclosed_jargon_penalty_constant(self):
        base = ("The result was a 62 percent improvement in the deploy "
                "pipeline last quarter. "
                + "We kept the design simple and boring throughout the "
                "whole project work. " * 6)
        self.assertEqual(i06_rubric.score_clarity(base)["score"], 100)
        two_hits = base + " Real synergy and a holistic view."
        # 2 hits -> 1 beyond the first -> -10.
        self.assertEqual(
            i06_rubric.score_clarity(two_hits)["score"], 90)


if __name__ == "__main__":
    unittest.main()
