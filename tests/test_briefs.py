"""Unit tests for briefs.py: company briefs + interview prep (no network).

The web fetch and the job-details lookup are both injected/monkeypatched,
so nothing here touches the network or imports server.py.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import briefs  # noqa: E402


def fake_fetch_factory(results):
    def _fetch(query):
        return [dict(r) for r in results]
    return _fetch


FAKE_RESULTS = [
    {
        "title": "Acme Corp — builds rockets for logistics",
        "url": "https://example.com/acme",
        "snippet": (
            "Acme Corp builds reusable rockets for logistics. The company "
            "raised $50M in Series B funding and now has 450 employees. "
            "Bootstrapped early, now venture backed."
        ),
    },
    {
        "title": "Acme Corp raises Series B",
        "url": "https://example.com/acme-news",
        "snippet": "Acme Corp announced a $50M Series B round led by Example VC.",
    },
    {
        "title": "Acme Corp interview process",
        "url": "https://example.com/acme-interviews",
        "snippet": (
            "The Acme Corp interview process has 4 rounds: recruiter screen, "
            "technical deep-dive, system design interview, and a final "
            "culture-fit interview with the founders."
        ),
    },
]

FAKE_JOB = {
    "title": "Senior Backend Engineer",
    "company": "Acme Corp",
    "location": "Remote",
    "description": (
        "We are hiring a Senior Backend Engineer. You must have 5+ years of "
        "Python experience and have led distributed systems projects at "
        "scale. You should be comfortable with ambiguity in a fast-paced "
        "startup environment."
    ),
    "requirements": ["5+ years Python", "Led distributed systems at scale"],
}

FAKE_PROFILE = {
    "full_name": "Ada Lovelace",
    "target_salary": "$200k",
    "salary_floor": "$160k",
    "experience": [
        {
            "title": "Backend Engineer",
            "company": "Initech",
            "start": "2020-01",
            "end": "",
            "description": (
                "Rebuilt the billing pipeline, cutting p99 latency by 40% "
                "and saving $2M per year in infra costs."
            ),
        }
    ],
    "achievements": [
        {
            "role": "Backend Engineer",
            "company": "Initech",
            "achievement": "Scaled the API to 10x traffic with zero downtime.",
        }
    ],
}


class TestCompanyBrief(unittest.TestCase):
    def test_populates_fields_from_fetch(self):
        brief = briefs.company_brief("Acme Corp", fetch=fake_fetch_factory(FAKE_RESULTS))
        self.assertEqual(brief["company"], "Acme Corp")
        self.assertIn("rockets", brief["what_they_do"])
        self.assertNotEqual(brief["funding_stage"], briefs.UNVERIFIED)
        self.assertIn("Series B", brief["funding_stage"])
        self.assertNotEqual(brief["headcount_hint"], briefs.UNVERIFIED)
        self.assertIn("450", brief["headcount_hint"])
        self.assertTrue(brief["recent_news"])
        self.assertTrue(all(n["url"].startswith("http") for n in brief["recent_news"]))
        hints = brief["interview_process_hints"]
        self.assertIsInstance(hints, list)
        self.assertTrue(any("interview" in h.lower() for h in hints))
        self.assertTrue(brief["sources"])
        self.assertTrue(brief["verified"])
        self.assertIn("# Company brief", brief["markdown"])

    def test_empty_fetch_marks_everything_unverified(self):
        brief = briefs.company_brief("Acme Corp", fetch=fake_fetch_factory([]))
        self.assertEqual(brief["what_they_do"], briefs.UNVERIFIED)
        self.assertEqual(brief["funding_stage"], briefs.UNVERIFIED)
        self.assertEqual(brief["headcount_hint"], briefs.UNVERIFIED)
        self.assertEqual(brief["recent_news"], [])
        self.assertEqual(brief["interview_process_hints"], briefs.UNVERIFIED)
        self.assertEqual(brief["sources"], [])
        self.assertFalse(brief["verified"])

    def test_failing_fetch_is_unverified_not_crash(self):
        def boom(query):
            raise RuntimeError("blocked")

        brief = briefs.company_brief("Acme Corp", fetch=boom)
        self.assertEqual(brief["funding_stage"], briefs.UNVERIFIED)
        self.assertFalse(brief["verified"])

    def test_never_invents_funding_or_headcount(self):
        bland = [
            {"title": "Acme Corp", "url": "https://example.com/a",
             "snippet": "Acme Corp makes software. Contact us for a demo."}
        ]
        brief = briefs.company_brief("Acme Corp", fetch=fake_fetch_factory(bland))
        self.assertEqual(brief["funding_stage"], briefs.UNVERIFIED)
        self.assertEqual(brief["headcount_hint"], briefs.UNVERIFIED)

    def test_empty_company_name(self):
        brief = briefs.company_brief("", fetch=fake_fetch_factory(FAKE_RESULTS))
        self.assertIn("error", brief)


class TestPrepInterview(unittest.TestCase):
    def _prep(self, profile, **kwargs):
        with mock.patch.object(
            briefs, "_get_job_details", return_value=dict(FAKE_JOB)
        ), mock.patch.object(
            briefs, "_load_saved_profile", return_value=profile
        ):
            return briefs.prep_interview(
                "lever:abc123", fetch=fake_fetch_factory([]), **kwargs
            )

    def test_prep_sections_present(self):
        prep = self._prep(FAKE_PROFILE)
        self.assertEqual(prep["title"], "Senior Backend Engineer")
        self.assertEqual(prep["company"], "Acme Corp")
        self.assertIn("role_summary", prep)
        self.assertTrue(prep["key_requirements"])
        questions = prep["likely_questions"]
        self.assertGreaterEqual(len(questions), 5)
        self.assertLessEqual(len(questions), 8)
        # Tailored to the posting: leadership + scale keywords present.
        joined = " ".join(questions).lower()
        self.assertTrue("led" in joined or "lead" in joined)
        self.assertGreaterEqual(len(prep["questions_to_ask_them"]), 4)
        self.assertIn("tos", prep)
        self.assertIn("tos_risk_tier", prep["tos"])
        self.assertIn("# Interview prep", prep["markdown"])

    def test_star_stories_come_only_from_profile(self):
        prep = self._prep(FAKE_PROFILE)
        stories = prep["star_stories"]
        # 1 experience entry + 1 achievement entry.
        self.assertEqual(len(stories), 2)
        self.assertEqual(stories[0]["company"], "Initech")
        # Metrics surfaced are literally in the source text — not invented.
        self.assertIn("40%", stories[0]["metrics"])
        self.assertIn("$2M", stories[0]["metrics"])
        self.assertIn("10x", stories[1]["metrics"])

    def test_no_profile_experience_means_no_stories_not_fabricated(self):
        prep = self._prep({"full_name": "Nobody", "experience": [], "achievements": []})
        self.assertEqual(prep["star_stories"], [])
        # And the markdown says so honestly instead of inventing.
        self.assertIn("None on file", prep["markdown"])

    def test_salary_points_use_profile_numbers(self):
        prep = self._prep(FAKE_PROFILE)
        joined = " ".join(prep["salary_talking_points"])
        self.assertIn("$200k", joined)
        self.assertIn("$160k", joined)

    def test_salary_points_generic_without_profile_numbers(self):
        prep = self._prep({"full_name": "Nobody"})
        self.assertTrue(prep["salary_talking_points"])
        joined = " ".join(prep["salary_talking_points"])
        self.assertNotIn("$", joined.replace("never", ""))  # no invented numbers

    def test_job_lookup_error_propagates(self):
        with mock.patch.object(
            briefs, "_get_job_details",
            return_value={"job_id": "x", "error": "Unknown board 'nope'"},
        ):
            prep = briefs.prep_interview("nope:abc")
        self.assertIn("error", prep)
        self.assertEqual(prep["job_id"], "nope:abc")

    def test_explicit_profile_arg_used(self):
        with mock.patch.object(
            briefs, "_get_job_details", return_value=dict(FAKE_JOB)
        ), mock.patch.object(
            briefs, "_load_saved_profile",
            side_effect=AssertionError("should not be called"),
        ):
            prep = briefs.prep_interview(
                "lever:abc123", profile=FAKE_PROFILE,
                fetch=fake_fetch_factory([]),
            )
        self.assertEqual(len(prep["star_stories"]), 2)


class TestWiring(unittest.TestCase):
    def test_register_tools(self):
        registered = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    registered[fn.__name__] = fn
                    return fn
                return deco

        briefs.register_tools(FakeMCP())
        self.assertIn("company_brief", registered)
        self.assertIn("prep_interview", registered)

        with mock.patch.object(
            briefs, "_get_job_details", return_value=dict(FAKE_JOB)
        ), mock.patch.object(
            briefs, "_load_saved_profile", return_value=FAKE_PROFILE
        ), mock.patch.object(
            briefs, "default_fetch", fake_fetch_factory(FAKE_RESULTS)
        ):
            out = registered["prep_interview"]("lever:abc123")
        self.assertEqual(out["title"], "Senior Backend Engineer")
        with mock.patch.object(
            briefs, "default_fetch", fake_fetch_factory(FAKE_RESULTS)
        ):
            out = registered["company_brief"]("Acme Corp")
        self.assertEqual(out["company"], "Acme Corp")

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        handlers = briefs.register_cli(sub)
        self.assertIn("company-brief", handlers)
        self.assertIn("prep-interview", handlers)
        args = parser.parse_args(["company-brief", "Acme Corp"])
        self.assertEqual(args.command, "company-brief")
        self.assertEqual(args.company, "Acme Corp")
        args = parser.parse_args(["prep-interview", "lever:abc123", "--json"])
        self.assertEqual(args.command, "prep-interview")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
