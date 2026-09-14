"""Unit tests for mock_interview.py: mock interview loop (no network).

The sessions file is redirected to a tmp dir per test; briefs network
paths are monkeypatched. Nothing here touches the network or imports
server.py at module load.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mock_interview  # noqa: E402

STRONG_ANSWER = (
    "In 2023 the situation was dire: our checkout API was timing out at "
    "peak traffic and the on-call context was brutal. My task as the "
    "backend lead was to cut p99 latency under 300ms before Black Friday. "
    "The action I took: I designed a Redis cache layer, I implemented "
    "request coalescing, and I led the rollout across three services. As "
    "a result, p99 latency dropped 62% and we saved $40k per month in "
    "infrastructure costs."
)

WE_HEAVY_ANSWER = (
    "We faced a tough challenge when the main system went down during a "
    "big launch. We decided to rewrite the failing service together and "
    "we shipped the replacement over one long weekend. The result was a "
    "stable launch and happy customers."
)

FAKE_PROFILE = {
    "full_name": "Ada Lovelace",
    "experience": [
        {
            "title": "Backend Engineer",
            "company": "Initech",
            "start": "2020-01",
            "end": "",
            "description": "Rebuilt billing, cutting p99 latency by 40%.",
        }
    ],
    "achievements": [],
}


class MockInterviewTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._sessions_file = Path(self._tmp.name) / "mock_sessions.json"
        self._patcher = mock.patch.object(
            mock_interview, "SESSIONS_FILE", self._sessions_file)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _start(self, **kwargs):
        kwargs.setdefault("company", "Acme Corp")
        kwargs.setdefault("role", "Senior Backend Engineer")
        kwargs.setdefault("profile", FAKE_PROFILE)
        return mock_interview.start_mock_interview(**kwargs)

    def _answer_all(self, session_id, answer=STRONG_ANSWER):
        last = None
        for _ in range(mock_interview.NUM_QUESTIONS):
            last = mock_interview.answer_mock_question(session_id, answer)
        return last


class TestStart(MockInterviewTestCase):
    def test_chat_returns_five_questions_and_first(self):
        out = self._start(channel="chat")
        session = out["session"]
        self.assertEqual(len(session["questions"]), 5)
        self.assertEqual([q["id"] for q in session["questions"]],
                         ["q1", "q2", "q3", "q4", "q5"])
        kinds = {q["kind"] for q in session["questions"]}
        self.assertTrue({"behavioral", "technical", "company"} <= kinds)
        self.assertEqual(out["first_question"]["id"], "q1")
        self.assertIn("mock_answer", out["note"])

    def test_questions_deterministic(self):
        a = self._start()["session"]["questions"]
        b = self._start()["session"]["questions"]
        self.assertEqual([q["question"] for q in a],
                         [q["question"] for q in b])

    def test_role_bank_matches_backend(self):
        out = self._start(role="Senior Backend Engineer")
        joined = " ".join(q["question"] for q in out["session"]["questions"])
        self.assertIn("rate limiter", joined)

    def test_role_bank_matches_manager(self):
        out = self._start(role="Engineering Manager")
        joined = " ".join(q["question"] for q in out["session"]["questions"])
        self.assertIn("tech debt", joined)

    def test_unknown_role_gets_fallback(self):
        out = self._start(role="Chief Vibes Officer")
        joined = " ".join(q["question"] for q in out["session"]["questions"])
        self.assertIn("hardest technical problem", joined)

    def test_company_question_names_company(self):
        out = self._start(company="Acme Corp")
        company_qs = [q for q in out["session"]["questions"]
                      if q["kind"] == "company"]
        self.assertTrue(company_qs)
        self.assertIn("Acme Corp", company_qs[0]["question"])

    def test_whatsapp_returns_outbound_routing_not_a_send(self):
        out = self._start(channel="whatsapp")
        self.assertEqual(out["deliverable_in"], "whatsapp_side_chat")
        self.assertIn("Mock interview", out["outbound_message"])
        # All five questions are in the outbound text.
        for i in range(1, 6):
            self.assertIn(f"{i}. ", out["outbound_message"])
        # Honest about delivery: side chat only, never claims it sent.
        self.assertIn("side chat", out["note"])
        self.assertIn("did not send", out["note"])

    def test_invalid_channel_raises(self):
        with self.assertRaises(ValueError):
            self._start(channel="sms")

    def test_session_persisted_to_json(self):
        out = self._start()
        data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
        self.assertIn(out["session_id"], data)
        self.assertEqual(data[out["session_id"]]["company"], "Acme Corp")

    def test_star_hints_come_only_from_profile(self):
        out = self._start()
        hints = out["session"]["star_story_hints"]
        self.assertTrue(hints)
        self.assertEqual(hints[0]["company"], "Initech")

    def test_no_profile_means_no_hints_not_fabricated(self):
        out = mock_interview.start_mock_interview(
            "Acme", "Backend Engineer", profile={})
        self.assertEqual(out["session"]["star_story_hints"], [])

    def test_job_id_reuses_prep_questions(self):
        fake_prep = {
            "likely_questions": [
                "Walk me through your background.",
                "Why Acme Corp?",
                "Tell me about a time you led a project.",
                "How do you debug latency spikes?",
                "Describe a disagreement with a teammate.",
                "What is your greatest weakness?",
            ]
        }
        with mock.patch.object(mock_interview.briefs, "prep_interview",
                               return_value=fake_prep):
            out = self._start(job_id="lever:abc123")
        session = out["session"]
        self.assertEqual(session["question_source"], "prep_interview")
        self.assertEqual(len(session["questions"]), 5)
        self.assertIn("Why Acme Corp?", session["questions"][1]["question"])

    def test_job_id_prep_failure_falls_back_to_bank(self):
        with mock.patch.object(
                mock_interview.briefs, "prep_interview",
                side_effect=RuntimeError("boom")):
            out = self._start(job_id="lever:abc123")
        self.assertEqual(out["session"]["question_source"], "question_bank")
        self.assertEqual(len(out["session"]["questions"]), 5)


class TestAnswer(MockInterviewTestCase):
    def test_strong_answer_scores_high(self):
        out = self._start()
        fb = mock_interview.answer_mock_question(
            out["session_id"], STRONG_ANSWER)
        self.assertEqual(fb["question_id"], "q1")
        self.assertEqual(fb["question_index"], 1)
        self.assertEqual(fb["star"]["coverage"], "4/4")
        self.assertTrue(fb["has_numbers"])
        self.assertGreaterEqual(fb["overall"], 90)
        self.assertEqual(fb["answered"], 1)
        self.assertFalse(fb["complete"])
        self.assertEqual(fb["next_question"]["id"], "q2")

    def test_short_answer_scores_low_with_length_feedback(self):
        out = self._start()
        fb = mock_interview.answer_mock_question(out["session_id"], "I did stuff.")
        self.assertLess(fb["overall"], 50)
        self.assertTrue(any("too short" in line for line in fb["feedback"]))

    def test_we_heavy_answer_flagged(self):
        out = self._start()
        fb = mock_interview.answer_mock_question(
            out["session_id"], WE_HEAVY_ANSWER)
        self.assertTrue(any("Ownership" in line for line in fb["feedback"]))

    def test_missing_star_parts_named(self):
        out = self._start()
        fb = mock_interview.answer_mock_question(
            out["session_id"], "I built a cache. It was fast. 50ms.")
        self.assertIn("situation", " ".join(fb["feedback"]).lower())
        self.assertLess(fb["overall"], 90)

    def test_no_numbers_gets_specificity_feedback(self):
        out = self._start()
        fb = mock_interview.answer_mock_question(
            out["session_id"], WE_HEAVY_ANSWER)
        self.assertFalse(fb["has_numbers"])
        self.assertTrue(any("no numbers" in line.lower()
                            for line in fb["feedback"]))

    def test_feedback_never_invents_candidate_facts(self):
        # Profile mentions Initech; feedback must only discuss the answer.
        out = self._start()
        sid = out["session_id"]
        for _ in range(mock_interview.NUM_QUESTIONS):
            fb = mock_interview.answer_mock_question(sid, WE_HEAVY_ANSWER)
        summary = mock_interview.mock_summary(sid)
        for line in fb["feedback"]:
            self.assertNotIn("Initech", line)
        for tip in summary["top_tips"]:
            self.assertNotIn("Initech", tip)

    def test_answers_advance_in_order_then_complete(self):
        out = self._start()
        sid = out["session_id"]
        last = self._answer_all(sid)
        self.assertTrue(last["complete"])
        self.assertEqual(last["answered"], 5)
        self.assertIsNone(last["next_question"])
        self.assertEqual(
            [q for q in (mock_interview._get_session(sid)["answers"])],
            ["q1", "q2", "q3", "q4", "q5"])

    def test_answering_complete_session_raises(self):
        out = self._start()
        self._answer_all(out["session_id"])
        with self.assertRaises(ValueError):
            mock_interview.answer_mock_question(out["session_id"], "more")

    def test_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            mock_interview.answer_mock_question("nope123", "answer")


class TestSummary(MockInterviewTestCase):
    def test_summary_scores_and_tips(self):
        out = self._start()
        sid = out["session_id"]
        mock_interview.answer_mock_question(sid, STRONG_ANSWER)
        mock_interview.answer_mock_question(sid, WE_HEAVY_ANSWER)
        summary = mock_interview.mock_summary(sid)
        self.assertEqual(summary["answered"], 2)
        self.assertEqual(summary["total"], 5)
        self.assertFalse(summary["complete"])
        self.assertEqual(len(summary["per_question"]), 5)
        self.assertEqual(len(summary["top_tips"]), 2)
        self.assertEqual(summary["unanswered"], ["q3", "q4", "q5"])
        scored = [p["overall"] for p in summary["per_question"]
                  if p["answered"]]
        self.assertEqual(summary["overall_score"],
                         round(sum(scored) / len(scored)))
        self.assertIn("# Mock interview summary", summary["markdown"])

    def test_summary_empty_session(self):
        out = self._start()
        summary = mock_interview.mock_summary(out["session_id"])
        self.assertEqual(summary["overall_score"], 0)
        self.assertEqual(len(summary["top_tips"]), 2)
        self.assertEqual(len(summary["unanswered"]), 5)

    def test_summary_complete_session(self):
        out = self._start()
        self._answer_all(out["session_id"])
        summary = mock_interview.mock_summary(out["session_id"])
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["unanswered"], [])
        self.assertGreater(summary["overall_score"], 0)

    def test_summary_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            mock_interview.mock_summary("nope123")


class TestWiring(MockInterviewTestCase):
    def test_register_tools(self):
        registered = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        mock_interview.register_tools(FakeMCP())
        self.assertEqual(
            set(registered),
            {"start_mock_interview", "answer_mock_question", "mock_summary"})

        # Tools call the real implementations (bound at registration time,
        # same pattern as briefs.py/grill.py).
        out = registered["start_mock_interview"](
            "Acme", "Backend Engineer", channel="chat")
        fb = registered["answer_mock_question"](
            out["session_id"], STRONG_ANSWER)
        self.assertIn("overall", fb)
        summary = registered["mock_summary"](out["session_id"])
        self.assertIn("top_tips", summary)
        self.assertIn("error", registered["answer_mock_question"](
            "bad-id", "x"))

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        handlers = mock_interview.register_cli(sub)
        self.assertEqual(
            set(handlers),
            {"mock-start", "mock-answer", "mock-summary"})
        args = parser.parse_args(
            ["mock-start", "Acme", "Backend Engineer",
             "--channel", "whatsapp", "--job-id", "lever:abc"])
        self.assertEqual(args.command, "mock-start")
        self.assertEqual(args.channel, "whatsapp")
        self.assertEqual(args.job_id, "lever:abc")
        args = parser.parse_args(["mock-answer", "sid123", "my answer"])
        self.assertEqual(args.session_id, "sid123")
        args = parser.parse_args(["mock-summary", "sid123"])
        self.assertEqual(args.command, "mock-summary")


if __name__ == "__main__":
    unittest.main()
