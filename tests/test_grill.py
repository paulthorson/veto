"""Tests for grill.py — per-application grilling. Hermetic, no network."""

import argparse
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import grill


JOB = {
    "title": "Backend Engineer",
    "company": "Acme Corp",
    "board": "greenhouse",
    "location": "Austin, TX",
    "description": (
        "Acme Corp is hiring a Backend Engineer.\n"
        "Requirements:\n"
        "- 5+ years of experience with Python\n"
        "- experience with Kubernetes\n"
        "- strong SQL skills\n"
        "Nice to have:\n"
        "- experience with Rust\n"
        "This is a hybrid role in Austin, TX."
    ),
}

PROFILE = {
    "full_name": "Test User",
    "email": "user@example.com",
    "location": "New York, NY",
    "skills": ["Python", "SQL"],
    "experience": [
        {
            "title": "Backend Developer",
            "company": "Initech",
            "description": "Built APIs with Python and SQL.",
        }
    ],
    "certifications": [],
}


def _fresh_sessions(testcase):
    tmp = Path(tempfile.mkdtemp(prefix="grill-test-"))
    testcase.addCleanup(lambda: [p.unlink(missing_ok=True)
                                 for p in tmp.iterdir()] or tmp.rmdir())
    patcher = mock.patch.object(grill, "SESSIONS_FILE", tmp / "sessions.json")
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return tmp


class PrefsTest(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("prefs.PREFS_FILE", Path(tmp) / "nope.json"):
                prefs = grill.get_grill_prefs()
        self.assertEqual(prefs["grill_channel"], "chat")
        self.assertTrue(prefs["grill_on_apply"])

    def test_custom_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "preferences.json"
            pf.write_text(json.dumps(
                {"grill_channel": "chat", "grill_on_apply": False}))
            with mock.patch("prefs.PREFS_FILE", pf):
                prefs = grill.get_grill_prefs()
        self.assertEqual(prefs["grill_channel"], "chat")
        self.assertFalse(prefs["grill_on_apply"])

        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "preferences.json"
            pf.write_text(json.dumps({"grill_channel": "carrier-pigeon"}))
            with mock.patch("prefs.PREFS_FILE", pf):
                self.assertEqual(
                    grill.get_grill_prefs()["grill_channel"], "chat")


class QuestionGenTest(unittest.TestCase):
    def test_quantify_kind(self):
        qs = grill.generate_questions(JOB, PROFILE)
        quantify = [q for q in qs if q["kind"] == "quantify"]
        self.assertTrue(quantify, "expected a quantify question")
        py = [q for q in quantify if q["topic"] == "python"]
        self.assertTrue(py)
        # Cites only that the skill is listed; asks for scale.
        self.assertIn("without numbers", py[0]["question"].lower())

    def test_gap_kind(self):
        qs = grill.generate_questions(JOB, PROFILE)
        gaps = [q for q in qs if q["kind"] == "gap"]
        self.assertTrue(any(q["topic"] == "kubernetes" for q in gaps))
        k8s = next(q for q in gaps if q["topic"] == "kubernetes")
        # Open question, never an accusation; always OK to say none.
        self.assertIn("fine to say none", k8s["question"].lower())
        self.assertNotIn("you have no", k8s["question"].lower())

    def test_logistics_kind(self):
        qs = grill.generate_questions(JOB, PROFILE)
        logistics = [q for q in qs if q["kind"] == "logistics"]
        self.assertTrue(logistics, "expected a logistics question")
        self.assertTrue(any("hybrid" in q["question"].lower()
                            for q in logistics))

    def test_logistics_skipped_when_covered(self):
        covered = dict(PROFILE, remote_preference="hybrid",
                       willing_to_relocate=True)
        qs = grill.generate_questions(JOB, covered)
        logistics = [q for q in qs if q["kind"] == "logistics"]
        self.assertEqual(logistics, [])

    def test_motivation_always_present_and_last(self):
        qs = grill.generate_questions(JOB, PROFILE)
        self.assertEqual(qs[-1]["kind"], "motivation")
        self.assertIn("Acme Corp", qs[-1]["question"])
        self.assertIn("cover letter", qs[-1]["question"].lower())

    def test_max_cap(self):
        qs = grill.generate_questions(JOB, PROFILE, max_questions=3)
        self.assertLessEqual(len(qs), 3)

    def test_must_have_ranked_first(self):
        # Rust is nice-to-have, Kubernetes is required: k8s must come first.
        qs = grill.generate_questions(JOB, PROFILE, max_questions=5)
        kinds_topics = [(q["kind"], q["topic"]) for q in qs]
        k8s_i = next(i for i, (_, t) in enumerate(kinds_topics)
                     if t == "kubernetes")
        rust_i = next((i for i, (_, t) in enumerate(kinds_topics)
                       if t == "rust"), None)
        if rust_i is not None:
            self.assertLess(k8s_i, rust_i)

    def test_deterministic(self):
        a = grill.generate_questions(JOB, PROFILE)
        b = grill.generate_questions(JOB, PROFILE)
        self.assertEqual(a, b)

    def test_question_shape(self):
        for q in grill.generate_questions(JOB, PROFILE):
            self.assertEqual(set(q.keys()), {"id", "question", "kind", "topic"})
            self.assertTrue(q["id"].startswith("q"))
            self.assertIn(q["kind"],
                          {"quantify", "gap", "logistics", "motivation"})

    def test_no_invented_facts(self):
        """Every number in a question must already exist in the JD/profile.

        Questions may quote the JD and cite listed profile skills, but must
        never introduce new numbers, durations, or scale claims.
        """
        qs = grill.generate_questions(JOB, PROFILE)
        source = (json.dumps(JOB) + json.dumps(PROFILE)).lower()
        for q in qs:
            for num in re.findall(r"\d[\d,]*(?:\.\d+)?", q["question"]):
                self.assertIn(num, source,
                              f"invented number {num!r} in: {q['question']}")

    def test_no_presumed_facts(self):
        """Gap questions must not assert negative facts about the user."""
        qs = grill.generate_questions(JOB, PROFILE)
        banned = ["you have no", "you lack", "you don't have",
                  "you've never", "you have never"]
        for q in qs:
            low = q["question"].lower()
            for phrase in banned:
                self.assertNotIn(phrase, low, q["question"])

    def test_empty_job_still_motivates(self):
        qs = grill.generate_questions({}, PROFILE)
        self.assertTrue(any(q["kind"] == "motivation" for q in qs))


class SessionTest(unittest.TestCase):
    def setUp(self):
        _fresh_sessions(self)
        self.started = grill.start_grill("job-1", JOB, PROFILE)

    def test_start_returns_session(self):
        self.assertIn("session_id", self.started)
        self.assertTrue(self.started["questions"])
        msg = self.started["outbound_message"]
        self.assertIn("Acme Corp", msg)
        self.assertIn("1.", msg)
        self.assertIn("Reply with the question number", msg)

    def test_lifecycle_to_complete(self):
        for q in self.started["questions"]:
            res = grill.record_answer("job-1", q["id"], f"answer to {q['id']}")
        self.assertTrue(res["complete"])
        self.assertEqual(res["answered"], res["total"])
        status = grill.grill_status("job-1")
        self.assertTrue(status["complete"])
        self.assertEqual(len(status["qa_pairs"]), res["total"])

    def test_partial_status(self):
        q0 = self.started["questions"][0]
        grill.record_answer("job-1", q0["id"], "my answer")
        status = grill.grill_status("job-1")
        self.assertFalse(status["complete"])
        self.assertEqual(status["answered"], 1)
        self.assertEqual(len(status["qa_pairs"]), 1)
        question, answer = status["qa_pairs"][0]
        self.assertEqual(question, q0["question"])
        self.assertEqual(answer, "my answer")

    def test_get_qa_pairs_order(self):
        qs = self.started["questions"]
        grill.record_answer("job-1", qs[1]["id"], "second")
        grill.record_answer("job-1", qs[0]["id"], "first")
        pairs = grill.get_qa_pairs("job-1")
        self.assertEqual([a for _, a in pairs], ["first", "second"])

    def test_unknown_session_and_question(self):
        with self.assertRaises(ValueError):
            grill.record_answer("nope", "q1", "x")
        with self.assertRaises(ValueError):
            grill.record_answer("job-1", "q99", "x")
        status = grill.grill_status("nope")
        self.assertFalse(status["complete"])
        self.assertIn("error", status)
        self.assertEqual(grill.get_qa_pairs("nope"), [])

    def test_cancel(self):
        self.assertTrue(grill.cancel_grill("job-1"))
        self.assertFalse(grill.cancel_grill("job-1"))
        self.assertIn("error", grill.grill_status("job-1"))

    def test_restart_replaces(self):
        first = self.started["session_id"]
        second = grill.start_grill("job-1", JOB, PROFILE)["session_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(grill.grill_status("job-1")["answered"], 0)


class ChannelTest(unittest.TestCase):
    def setUp(self):
        _fresh_sessions(self)
        grill.start_grill("job-1", JOB, PROFILE)

    def test_whatsapp_routing(self):
        routed = grill.outbound_for_channel("job-1", "whatsapp")
        self.assertEqual(routed["deliverable_in"], "whatsapp_side_chat")
        self.assertIn("1.", routed["message"])
        self.assertIn("WhatsApp side chat", routed["note"])

    def test_chat_routing(self):
        routed = grill.outbound_for_channel("job-1", "chat")
        self.assertEqual(routed["deliverable_in"], "current_chat")
        self.assertTrue(routed["message"])

    def test_off_routing(self):
        routed = grill.outbound_for_channel("job-1", "off")
        self.assertEqual(routed["deliverable_in"], "none")
        self.assertEqual(routed["message"], "")

    def test_no_session_routing(self):
        routed = grill.outbound_for_channel("missing", "whatsapp")
        self.assertEqual(routed["deliverable_in"], "none")

    def test_default_channel_from_prefs(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "preferences.json"
            pf.write_text(json.dumps({"grill_channel": "chat"}))
            with mock.patch("prefs.PREFS_FILE", pf):
                routed = grill.outbound_for_channel("job-1")
        self.assertEqual(routed["deliverable_in"], "current_chat")


class GmailChannelTest(unittest.TestCase):
    def setUp(self):
        _fresh_sessions(self)
        self.started = grill.start_grill("job-1", JOB, PROFILE)

    def test_gmail_routing(self):
        routed = grill.outbound_for_channel("job-1", "gmail")
        self.assertEqual(routed["deliverable_in"], "gmail")
        self.assertEqual(routed["to"], "user@example.com")
        self.assertIn("[grill:", routed["subject"])
        self.assertIn("Backend Engineer", routed["subject"])
        self.assertIn("Acme Corp", routed["subject"])
        self.assertIn("before I tailor your application", routed["subject"])
        self.assertIn("1.", routed["body"])
        self.assertIn("Reply with numbered answers", routed["body"])
        self.assertIn("explicit go-ahead", routed["note"])

    def test_gmail_subject_tag_roundtrip(self):
        subject = grill.outbound_for_channel("job-1", "gmail")["subject"]
        tag = re.search(r"\[grill:([0-9a-f]{12})\]", subject)
        self.assertIsNotNone(tag)
        self.assertEqual(tag.group(1), self.started["session_id"])

    def test_gmail_pref_default_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "preferences.json"
            pf.write_text(json.dumps({"grill_channel": "gmail"}))
            with mock.patch("prefs.PREFS_FILE", pf):
                routed = grill.outbound_for_channel("job-1")
        self.assertEqual(routed["deliverable_in"], "gmail")


class ParseReplyTest(unittest.TestCase):
    def test_numbered_formats(self):
        body = (
            "Here are my answers!\n"
            "1. Python at 10k requests/day\n"
            "2) No Kubernetes experience\n"
            "3: happy to travel\n"
            "Q4: Acme's infra blog got me interested\n"
        )
        parsed = grill.parse_reply_body(body)
        self.assertEqual(parsed["q1"], "Python at 10k requests/day")
        self.assertEqual(parsed["q2"], "No Kubernetes experience")
        self.assertEqual(parsed["q3"], "happy to travel")
        self.assertEqual(parsed["q4"],
                         "Acme's infra blog got me interested")

    def test_multiline_answers_folded(self):
        body = ("1. First line of the answer\n"
                "continued on the next line\n"
                "and one more\n"
                "2. Second answer")
        parsed = grill.parse_reply_body(body)
        self.assertEqual(parsed["q1"],
                         "First line of the answer\n"
                         "continued on the next line\n"
                         "and one more")
        self.assertEqual(parsed["q2"], "Second answer")

    def test_freeform_and_quoted_history_ignored(self):
        body = (
            "Thanks for the questions, here goes:\n"
            "1. my answer\n"
            "\n"
            "On Tue, Sep 9, 2026 at 9:00 PM Muse wrote:\n"
            "> 1. What's the largest scale...\n"
            "> 2. Any experience with...\n"
            "1. this is the quoted question, not an answer\n"
            "-- \n"
            "Sent from my iPhone\n"
            "1. this is the signature, not an answer\n"
        )
        parsed = grill.parse_reply_body(body)
        self.assertEqual(parsed, {"q1": "my answer"})

    def test_skip_is_an_answer(self):
        parsed = grill.parse_reply_body("1. skip\n2. real answer")
        self.assertEqual(parsed["q1"], "skip")
        self.assertEqual(parsed["q2"], "real answer")

    def test_empty_body(self):
        self.assertEqual(grill.parse_reply_body(""), {})
        self.assertEqual(grill.parse_reply_body("no numbers here"), {})

    def test_single_line_inline_markers(self):
        # The exact shape from the 2026-09-10 live test: one line, no
        # line breaks between answers.
        parsed = grill.parse_reply_body(
            "1. Lots 2. 15GB 3.2GB 4.30GB 5. great and know alot")
        self.assertEqual(parsed, {
            "q1": "Lots",
            "q2": "15GB",
            "q3": "2GB",
            "q4": "30GB",
            "q5": "great and know alot",
        })

    def test_inline_markers_require_sequence(self):
        # Stray decimals must not trigger the inline fallback.
        self.assertEqual(
            grill.parse_reply_body("I have 2.5 years of experience"), {})
        # Out-of-order markers: the line parser claims the leading "2."
        # as q2 and folds the rest into its answer; the inline fallback
        # (ascending 1..n only) stays out of it.
        self.assertEqual(grill.parse_reply_body("2. second 1. first"),
                         {"q2": "second 1. first"})


class IngestReplyTest(unittest.TestCase):
    def setUp(self):
        _fresh_sessions(self)
        self.started = grill.start_grill("job-1", JOB, PROFILE)
        self.n = len(self.started["questions"])

    def test_ingest_records_and_reports(self):
        body = "1. 10k rps\n2. none, happy to learn\n"
        result = grill.ingest_reply("job-1", body)
        self.assertEqual(result["recorded"],
                         {"q1": "10k rps", "q2": "none, happy to learn"})
        self.assertEqual(result["unparsed"], [])
        self.assertEqual(result["answered"], 2)
        self.assertEqual(result["total"], self.n)
        pairs = grill.get_qa_pairs("job-1")
        self.assertEqual(len(pairs), 2)

    def test_ingest_to_complete(self):
        body = "\n".join(
            f"{i + 1}. answer {i + 1}"
            for i in range(self.n))
        result = grill.ingest_reply("job-1", body)
        self.assertTrue(result["complete"])
        self.assertTrue(grill.grill_status("job-1")["complete"])

    def test_unparsed_numbers_reported(self):
        result = grill.ingest_reply("job-1", "1. ok\n9. stray answer\n")
        self.assertEqual(result["recorded"], {"q1": "ok"})
        self.assertEqual(result["unparsed"], ["q9"])

    def test_messy_real_world_reply(self):
        body = (
            "Hi! Answers below.\n\n"
            "1) Python — peaked at ~50k requests/day across 3 services.\n"
            "Some extra context on the same answer, second line.\n\n"
            "Q2: skip\n"
            "3: I can do hybrid, live 20 min from Austin.\n\n"
            "Thanks!\n"
            "-- \nSent from my iPhone\n"
        )
        result = grill.ingest_reply("job-1", body)
        self.assertIn("q1", result["recorded"])
        self.assertIn("50k requests/day", result["recorded"]["q1"])
        self.assertIn("extra context", result["recorded"]["q1"])
        self.assertEqual(result["recorded"]["q2"], "skip")
        self.assertEqual(result["recorded"]["q3"],
                         "I can do hybrid, live 20 min from Austin.")

    def test_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            grill.ingest_reply("nope", "1. x")
    def test_register_tools(self):
        seen = []

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen.append(fn.__name__)
                    return fn
                return deco

        grill.register_tools(FakeMCP())
        self.assertEqual(seen, ["grill_start", "grill_answer", "grill_status",
                                "grill_ingest_reply"])

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        handlers = grill.register_cli(sub)
        self.assertEqual(
            set(handlers),
            {"grill-start", "grill-answer", "grill-status",
             "grill-ingest-reply"})
        for name, handler in handlers.items():
            self.assertTrue(callable(handler), name)
        # Parsers accept the documented flags.
        ns = parser.parse_args(["grill-start", "job-1"])
        self.assertEqual(ns.job_id, "job-1")
        ns = parser.parse_args(
            ["grill-answer", "job-1", "q1", "my answer"])
        self.assertEqual((ns.question_id, ns.answer), ("q1", "my answer"))
        ns = parser.parse_args(["grill-status", "job-1"])
        self.assertEqual(ns.job_id, "job-1")
        ns = parser.parse_args(
            ["grill-ingest-reply", "job-1", "--body", "1. hi"])
        self.assertEqual(ns.body, "1. hi")


class GrillStartForJobIdTest(unittest.TestCase):
    def test_uses_injected_fetch(self):
        _fresh_sessions(self)
        with mock.patch("server._load_saved_profile",
                         return_value=PROFILE):
            result = grill.grill_start_for_job_id(
                "greenhouse:abc",
                fetch_details=lambda jid: dict(JOB, job_id=jid))
        self.assertTrue(result["questions"])
        self.assertIn("Acme Corp", result["outbound_message"])

    def test_fetch_error_raises(self):
        _fresh_sessions(self)
        with mock.patch("server._load_saved_profile",
                         return_value=PROFILE):
            with self.assertRaises(ValueError):
                grill.grill_start_for_job_id(
                    "greenhouse:abc",
                    fetch_details=lambda jid: {"error": "boom"})


if __name__ == "__main__":
    unittest.main()
