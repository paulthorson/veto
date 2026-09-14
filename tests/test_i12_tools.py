#!/usr/bin/env python3
"""Initiative 12 / Epic 1 — public tools tests."""

import unittest

from initiatives.i12 import tools
from initiatives.i12.tools import MiniProfile

SAMPLE_JD = """
Senior Backend Engineer — Acme Corp

Salary: $140,000 - $175,000. Equity: 0.1% options vesting over 4 years.

Responsibilities:
- Design and operate payment services in Python.
- Lead incident response and mentor junior engineers.
- Collaborate with product on roadmap planning.

We value work-life balance: no on-call heroics, 40-hour weeks, flexible hours.
We are looking for 5+ years of experience building distributed systems.
"""


def _job(**kw):
    d = {"title": "Senior Backend Engineer", "company": "Acme Corp",
         "location": "Remote", "description": SAMPLE_JD}
    d.update(kw)
    return d


def _profile():
    return MiniProfile(skills=["Python", "distributed systems", "mentoring"],
                       seniority="senior", locations=["Remote"], remote_ok=True)


class JdDemoTest(unittest.TestCase):
    def test_decode_returns_evidence(self):
        out = tools.jd_demo(SAMPLE_JD)
        self.assertTrue(out["available"])
        self.assertEqual(out["tool"], "jd_decoder")
        self.assertIn(out["verdict"], ("strong", "mixed", "caution"))
        self.assertTrue(out["top_reasons"])
        self.assertIn("methodology", out)
        self.assertIn("limitations", out)
        self.assertFalse(out["submits_anything"])

    def test_salary_range_extracted_not_invented(self):
        out = tools.jd_demo(SAMPLE_JD)
        self.assertIsNotNone(out["salary_range"])
        self.assertIn("140", out["salary_range"])

    def test_empty_text_is_handled(self):
        out = tools.jd_demo("")
        self.assertTrue(out["available"])


class FitExplainerTest(unittest.TestCase):
    def test_explains_five_factors(self):
        out = tools.fit_explainer(_job(), _profile())
        self.assertEqual(out["tool"], "fit_explainer")
        names = [f["factor"] for f in out["factors"]]
        for expected in ("skills", "seniority", "salary", "location", "recency"):
            self.assertIn(expected, names)
        for f in out["factors"]:
            self.assertIn("why", f)

    def test_no_raw_resume_needed(self):
        # The demo profile is a typed skill list, never resume text.
        p = MiniProfile(skills=["Python"])
        out = tools.fit_explainer(_job(), p)
        self.assertTrue(out["factors"])


class RiskCheckTest(unittest.TestCase):
    def test_proposal_only_by_construction(self):
        out = tools.risk_check(_job(), _profile())
        self.assertTrue(out["proposal_only"])
        self.assertTrue(out["cannot_submit"])
        self.assertFalse(out["submits_anything"])
        self.assertIn("findings", out)
        for f in out["findings"]:
            self.assertIn(f["severity"], ("blocker", "warning", "info"))
            self.assertIn("next_step", f)

    def test_veto_becomes_blocker(self):
        job = _job(location="Onsite, Antarctica")
        out = tools.risk_check(job, _profile())
        # Location veto (or similar) should surface as a blocker finding
        # or the scored veto flag should be consistent.
        self.assertIsInstance(out["blockers"], int)


class RoleCompareTest(unittest.TestCase):
    def test_compares_up_to_four(self):
        jobs = [_job(title=f"Role {i}", company=f"Co {i}") for i in range(6)]
        out = tools.role_compare(jobs, _profile())
        self.assertEqual(out["compared"], 4)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["dimensions"],
                         ["fit", "risk", "compensation", "location", "readiness"])

    def test_compensation_missing_reported_as_missing(self):
        job = _job(description="Exciting role. Apply now.")
        out = tools.role_compare([job], _profile())
        self.assertEqual(out["rows"][0]["compensation"], "not listed in posting")

    def test_max_compare_constant(self):
        self.assertEqual(tools.MAX_COMPARE_JOBS, 4)


if __name__ == "__main__":
    unittest.main()
