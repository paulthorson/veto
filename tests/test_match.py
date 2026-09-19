#!/usr/bin/env python3
"""Tests for match.py — fit scoring, vetoing, and ranking.

Stdlib unittest only. Everything is offline: jobs, profiles, and
preferences are plain dicts built in the fixtures below.

Run:  cd ~/workspace/veto && .venv/bin/python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import match


def _profile(**overrides):
    profile = {
        "full_name": "Test Candidate",
        "seniority": "senior",
        "location": "New York, NY",
        "years_experience": 7,
        "skills": ["python", "django", "react", "aws", "docker", "sql", "system design"],
    }
    profile.update(overrides)
    return profile


def _job(**overrides):
    job = {
        "title": "Senior Software Engineer, Backend",
        "company": "Acme",
        "location": "New York, NY",
        "board": "greenhouse",
        "snippet": "Build payment infrastructure with Python and Django.",
        "description": (
            "We are hiring a Senior Software Engineer to build payment "
            "infrastructure. Requirements: 5+ years Python, Django, AWS, "
            "Docker, SQL, system design. Nice to have: Rust, Kubernetes. "
            "Salary: $170,000 - $200,000. Hybrid in New York."
        ),
        "requirements": "Python, Django, AWS, Docker, SQL, system design",
    }
    job.update(overrides)
    return job


def _prefs(**overrides):
    prefs = {"salary_min": 150000, "locations": ["New York, NY"]}
    prefs.update(overrides)
    return prefs


class ScoreJobTest(unittest.TestCase):
    def test_strong_match_scores_high_without_veto(self):
        result = match.score_job(_job(), _profile(), _prefs())
        self.assertGreaterEqual(result["score"], 70)
        self.assertFalse(result["veto"])
        self.assertIsNone(result["veto_reason"])
        self.assertIn("python", [s.lower() for s in result["matched"]])
        self.assertIn("rust", [s.lower() for s in result["missing"]])
        self.assertTrue(any("7" in r or "key skills" in r for r in result["reasons"]))

    def test_skill_gap_triggers_veto_with_concrete_reason(self):
        job = _job(
            title="Rust Systems Engineer",
            description="Deep Rust, Tokio, and low-level systems programming required.",
            requirements="Rust, Tokio, C++, embedded",
            snippet="Rust systems role.",
        )
        result = match.score_job(job, _profile(), _prefs())
        self.assertLess(result["score"], 60)
        self.assertTrue(result["veto"])
        self.assertIsNotNone(result["veto_reason"])
        self.assertIn("rust", result["veto_reason"].lower())
        self.assertIn("rust", [s.lower() for s in result["missing"]])
        # Honesty: veto must cite missing skills, not invent profile skills.
        for invented in ("rust", "tokio", "c++"):
            self.assertNotIn(invented, [s.lower() for s in result["matched"]])

    def test_salary_below_floor_reduces_score(self):
        low = _job(description="Salary: $90,000. Python and Django required.")
        high = _job(description="Salary: $190,000. Python and Django required.")
        low_score = match.score_job(low, _profile(), _prefs())["score"]
        high_score = match.score_job(high, _profile(), _prefs())["score"]
        self.assertLess(low_score, high_score)
        low_result = match.score_job(low, _profile(), _prefs())
        self.assertTrue(any("below your" in r for r in low_result["reasons"]))

    def test_no_salary_signal_is_not_penalized(self):
        job = _job(description="Python and Django required. Great team.")
        result = match.score_job(job, _profile(), _prefs())
        self.assertGreaterEqual(result["components"]["salary"], 10)

    def test_seniority_mismatch_reduces_score(self):
        junior_job = _job(title="Junior Software Engineer")
        junior_score = match.score_job(junior_job, _profile(), _prefs())["score"]
        senior_score = match.score_job(_job(), _profile(), _prefs())["score"]
        self.assertLess(junior_score, senior_score)

    def test_staff_role_for_senior_profile_gets_stretch_credit(self):
        staff_job = _job(title="Staff Software Engineer, Backend")
        mid_profile = _profile(seniority="mid")
        staff_score = match.score_job(staff_job, mid_profile, _prefs())["score"]
        self.assertGreater(staff_score, 0)
        principal_job = _job(title="Principal Engineer")
        entry_profile = _profile(seniority="entry")
        low = match.score_job(principal_job, entry_profile, _prefs())
        self.assertLess(low["components"]["seniority"], 5)

    def test_profile_seniority_defaults_to_mid(self):
        job = _job(title="Software Engineer")
        result = match.score_job(job, _profile(seniority=None), _prefs())
        self.assertTrue(any("mid" in r for r in result["reasons"]))

    def test_remote_only_rejects_onsite(self):
        prefs = _prefs(remote_only=True)
        onsite = _job(location="New York, NY", snippet="On-site role in our NYC office.")
        result = match.score_job(onsite, _profile(), prefs)
        self.assertEqual(result["components"]["location"], 0)
        remote = _job(
            location="Remote",
            title="Senior Software Engineer, Remote",
            snippet="Fully remote role.",
        )
        remote_result = match.score_job(remote, _profile(), prefs)
        self.assertEqual(remote_result["components"]["location"], 15)

    def test_location_mismatch_reduces_score(self):
        far = _job(location="San Francisco, CA")
        result = match.score_job(far, _profile(), _prefs())
        self.assertLess(result["components"]["location"], 10)
        self.assertTrue(any("San Francisco" in r for r in result["reasons"]))

    def test_recency_bonus_for_fresh_posting(self):
        fresh = _job(posted_days_ago=2)
        stale = _job(posted_days_ago=90)
        self.assertGreater(
            match.score_job(fresh, _profile(), _prefs())["components"]["recency"],
            match.score_job(stale, _profile(), _prefs())["components"]["recency"],
        )

    def test_recency_unknown_is_neutral(self):
        result = match.score_job(_job(), _profile(), _prefs())
        self.assertGreater(result["components"]["recency"], 0)

    def test_no_skill_keywords_scores_neutrally(self):
        job = _job(
            title="Software Engineer",
            description="Come join our amazing team! Great culture.",
            requirements="",
            snippet="Exciting opportunity.",
        )
        result = match.score_job(job, _profile(), _prefs())
        self.assertEqual(result["components"]["skills"], 25.0)
        self.assertTrue(any("neutrally" in r for r in result["reasons"]))

    def test_components_sum_to_score(self):
        result = match.score_job(_job(), _profile(), _prefs())
        total = sum(result["components"].values())
        self.assertAlmostEqual(total, result["score"], delta=1)
        for name, weight in (
            ("skills", 50), ("seniority", 15), ("salary", 15),
            ("location", 15), ("recency", 5),
        ):
            self.assertLessEqual(result["components"][name], weight)

    def test_missing_preferences_use_defaults(self):
        result = match.score_job(_job(), _profile(), None)
        self.assertIn("score", result)
        self.assertIn(result["score"], range(0, 101))

    def test_empty_job_dict_does_not_crash(self):
        result = match.score_job({}, _profile(), _prefs())
        self.assertIn(result["score"], range(0, 101))
        self.assertFalse(result["veto"])  # no signal -> neutral, kept


class RankJobsTest(unittest.TestCase):
    def test_sorted_descending_by_score(self):
        jobs = [
            _job(title="Rust Systems Engineer",
                 description="Rust, Tokio, C++ required.",
                 requirements="Rust, Tokio"),
            _job(title="Senior Software Engineer, Backend"),
            _job(title="Junior QA Analyst",
                 description="Manual testing, entry level.",
                 requirements="manual testing"),
        ]
        ranked = match.rank_jobs(jobs, _profile(), _prefs())
        scores = [j["score"] for j in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(ranked[0]["title"], "Senior Software Engineer, Backend")

    def test_annotations_present_on_every_job(self):
        ranked = match.rank_jobs([_job()], _profile(), _prefs())
        for key in ("score", "reasons", "matched", "missing", "veto",
                    "veto_reason", "components"):
            self.assertIn(key, ranked[0])
        self.assertIsInstance(ranked[0]["reasons"], list)

    def test_input_jobs_not_mutated(self):
        jobs = [_job()]
        match.rank_jobs(jobs, _profile(), _prefs())
        self.assertNotIn("score", jobs[0])

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(match.rank_jobs([], _profile(), _prefs()), [])

    def test_ties_keep_input_order(self):
        jobs = [_job(title="Software Engineer"), _job(title="Software Engineer")]
        ranked = match.rank_jobs(jobs, _profile(), _prefs())
        self.assertEqual(ranked[0]["score"], ranked[1]["score"])


class VetoReasonTest(unittest.TestCase):
    def test_salary_veto_reason_names_floor(self):
        job = _job(description="Salary: $80,000. Python, Django, AWS, Docker, SQL required.")
        result = match.score_job(job, _profile(), {"salary_min": 150000})
        if result["veto"]:
            self.assertIn("150,000", result["veto_reason"])
        self.assertLess(result["components"]["salary"], 5)

    def test_veto_reason_prefixes_reasons_list(self):
        job = _job(
            title="Rust Systems Engineer",
            snippet="Rust systems role.",
            description="Rust, Tokio, C++ required.",
            requirements="Rust, Tokio",
        )
        result = match.score_job(job, _profile(), _prefs())
        self.assertTrue(result["veto"])
        self.assertTrue(result["reasons"][0].startswith("VETOED:"))


if __name__ == "__main__":
    unittest.main()


class TestMatchPluginWiring(unittest.TestCase):
    def test_register_tools_exposes_two_tools(self):
        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn
                return deco

        match.register_tools(FakeMCP())
        self.assertEqual(set(seen), {"score_job_posting", "rank_job_postings"})
        job = {"title": "Backend Engineer", "description": "python api", "company": "Acme"}
        profile = {"skills": ["python"], "seniority": "mid"}
        out = seen["score_job_posting"](job, profile)
        self.assertIn("score", out)
        ranked = seen["rank_job_postings"]([job], profile)
        self.assertEqual(len(ranked), 1)

    def test_register_cli_returns_match_command(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        cmds = match.register_cli(sub)
        self.assertIn("match", cmds)
        self.assertTrue(callable(cmds["match"]))

    def test_cli_requires_input_file(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        cmds = match.register_cli(sub)
        args = argparse.Namespace(
            job_file="", jobs_file="", profile_file="", preferences_file="",
            top=0, json=True,
        )
        self.assertEqual(cmds["match"](args), 2)
