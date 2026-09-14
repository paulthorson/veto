#!/usr/bin/env python3
"""Unit tests for application-quality features (no browser, no network).

Run from the project directory:
    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dedup import dedupe_jobs  # noqa: E402
from filters import detect_seniority, filter_by_salary, filter_by_seniority  # noqa: E402
from prefs import apply_preferences  # noqa: E402
from tailor import extract_job_keywords, tailor_resume  # noqa: E402


def _job(**kw):
    base = {
        "id": "x",
        "title": "Software Engineer",
        "company": "Initech",
        "location": "New York, NY",
        "board": "test",
        "snippet": "",
    }
    base.update(kw)
    # Unique default URL per job id so tests don't collapse on URL dedup
    # unless they explicitly share a URL.
    base.setdefault("url", f"https://example.com/jobs/{base['id']}")
    return base


class DedupTests(unittest.TestCase):
    def test_exact_url_dupes_collapse(self):
        a = _job(id="a", url="https://example.com/jobs/1?utm=x")
        b = _job(id="b", url="https://example.com/jobs/1")
        out = dedupe_jobs([a, b])
        self.assertEqual(len(out), 1)

    def test_fuzzy_title_dupe_collapses(self):
        a = _job(id="a", title="Senior Software Engineer", company="Initech")
        b = _job(id="b", title="Sr. Software Engineer", company="Initech")
        out = dedupe_jobs([a, b])
        self.assertEqual(len(out), 1)

    def test_distinct_jobs_kept(self):
        a = _job(id="a", title="Software Engineer", company="Initech")
        b = _job(id="b", title="Data Analyst", company="Initech")
        c = _job(id="c", title="Software Engineer", company="Globex")
        self.assertEqual(len(dedupe_jobs([a, b, c])), 3)

    def test_best_info_survivor_kept(self):
        thin = _job(id="a", company="Unknown", snippet="",
                    url="https://example.com/jobs/1")
        rich = _job(
            id="b",
            company="Initech",
            snippet="Great role with Python and Django.",
            url="https://example.com/jobs/1",
        )
        out = dedupe_jobs([thin, rich])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["company"], "Initech")
        self.assertIn("Django", out[0]["snippet"])

    def test_unknown_company_not_fuzzy_merged(self):
        a = _job(id="a", title="Engineer", company="Unknown")
        b = _job(id="b", title="Engineer", company="Unknown")
        # Different URLs, unknown company: cannot safely merge.
        a["url"] = "https://example.com/1"
        b["url"] = "https://example.com/2"
        self.assertEqual(len(dedupe_jobs([a, b])), 2)


class TailorHonestyTests(unittest.TestCase):
    PROFILE = {
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "skills": ["Python", "Django"],
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "Initech",
                "dates": "2020-2024",
                "bullets": [
                    "Maintained legacy reporting scripts.",
                    "Built Python APIs with Django serving 1M requests/day.",
                ],
            }
        ],
        "education": [{"degree": "B.S. CS", "school": "MIT"}],
    }
    JOB = {
        "title": "Senior Python Engineer",
        "company": "Globex",
        "description": "We need Python, Django, and Rust. Kubernetes a plus.",
        "requirements": "5+ years Python. Rust experience required.",
    }

    def test_matched_vs_missing(self):
        kw = extract_job_keywords(self.JOB, self.PROFILE)
        self.assertIn("Python", kw["matched"])
        self.assertIn("Django", kw["matched"])
        missing = [m.lower() for m in kw["missing"]]
        self.assertIn("rust", missing)

    def test_no_invented_skills_in_resume(self):
        out = tailor_resume(self.PROFILE, self.JOB)
        resume = out["resume_md"].lower()
        # Rust is required by the job but absent from the profile: it must
        # not appear as a claimed skill anywhere in the tailored resume.
        self.assertNotIn("rust", resume)
        self.assertNotIn("kubernetes", resume)
        # Real skills survive.
        self.assertIn("python", resume)

    def test_bullets_reordered_not_rewritten(self):
        out = tailor_resume(self.PROFILE, self.JOB)
        resume = out["resume_md"]
        self.assertIn("Built Python APIs with Django", resume)
        self.assertIn("Maintained legacy reporting scripts.", resume)
        # Keyword-matching bullet comes first.
        self.assertLess(
            resume.index("Built Python APIs"), resume.index("Maintained legacy")
        )

    def test_cover_letter_uses_only_profile_facts(self):
        out = tailor_resume(self.PROFILE, self.JOB)
        cl = out["cover_letter"]
        self.assertIn("Globex", cl)
        self.assertIn("Backend Engineer", cl)
        self.assertNotIn("Rust", cl)


class FilterTests(unittest.TestCase):
    def test_salary_floor(self):
        rich = _job(id="a", snippet="Compensation: $150k-$180k per year")
        poor = _job(id="b", snippet="Pay: $50k per year")
        silent = _job(id="c", snippet="Great team, apply now")
        out = filter_by_salary([rich, poor, silent], 100000)
        ids = {j["id"] for j in out}
        self.assertEqual(ids, {"a", "c"})  # no-salary jobs are kept

    def test_salary_disabled(self):
        jobs = [_job(id="a")]
        self.assertEqual(filter_by_salary(jobs, 0), jobs)

    def test_hourly_converted(self):
        hourly = _job(id="a", snippet="$75/hr contract")
        out = filter_by_salary([hourly], 100000)  # 75*2080 = 156k
        self.assertEqual(len(out), 1)

    def test_seniority_detection(self):
        self.assertEqual(detect_seniority("Senior Software Engineer"), "senior")
        self.assertEqual(detect_seniority("Sr. Backend Dev"), "senior")
        self.assertEqual(detect_seniority("Junior Data Analyst"), "entry")
        self.assertEqual(detect_seniority("Staff Engineer"), "staff")
        self.assertEqual(detect_seniority("Principal Architect"), "principal")
        self.assertIsNone(detect_seniority("Software Engineer"))

    def test_seniority_filter(self):
        jobs = [
            _job(id="a", title="Senior Software Engineer"),
            _job(id="b", title="Junior Software Engineer"),
            _job(id="c", title="Software Engineer"),
        ]
        out = filter_by_seniority(jobs, "senior")
        self.assertEqual([j["id"] for j in out], ["a"])
        out = filter_by_seniority(jobs, "mid")
        self.assertEqual([j["id"] for j in out], ["c"])
        with self.assertRaises(ValueError):
            filter_by_seniority(jobs, "wizard")


class PrefsTests(unittest.TestCase):
    PREFS = {
        "blocked_companies": ["Acme Corp"],
        "preferred_companies": ["Initech"],
        "blocked_keywords": ["unpaid internship"],
    }

    def test_blocked_company_removed(self):
        jobs = [_job(id="a", company="Acme Corp"), _job(id="b", company="Globex")]
        out = apply_preferences(jobs, self.PREFS)
        self.assertEqual([j["id"] for j in out], ["b"])

    def test_blocked_keyword_removed(self):
        jobs = [
            _job(id="a", snippet="unpaid internship, great exposure!"),
            _job(id="b", snippet="salaried role"),
        ]
        out = apply_preferences(jobs, self.PREFS)
        self.assertEqual([j["id"] for j in out], ["b"])

    def test_preferred_boosted_first(self):
        jobs = [_job(id="a", company="Globex"), _job(id="b", company="Initech")]
        out = apply_preferences(jobs, self.PREFS)
        self.assertEqual([j["id"] for j in out], ["b", "a"])
        self.assertTrue(out[0].get("preferred"))
        self.assertNotIn("preferred", out[1])

    def test_case_insensitive(self):
        jobs = [_job(id="a", company="ACME CORP")]
        self.assertEqual(apply_preferences(jobs, self.PREFS), [])


if __name__ == "__main__":
    unittest.main()
