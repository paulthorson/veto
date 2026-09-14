"""Unit tests for the tailor diff + approval workflow (no network).

Covers base_resume_md byte-identity with tailor_resume on empty
postings, resume_diff header/counts, tailor_with_diff keys, the
honesty contract (no invented skills in diff output), and the
approved-variant store round-trip (temp dir, never the real one).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tailor


def _profile() -> dict:
    return {
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "555-0100",
        "location": "New York, NY",
        "linkedin_url": "https://linkedin.com/in/ada",
        "years_experience": 5,
        "target_titles": ["Backend Engineer"],
        "skills": ["Python", "PostgreSQL", "Docker"],
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "Analytica",
                "dates": "2021-2024",
                "bullets": [
                    "Maintained legacy reporting dashboards.",
                    "Built Python ETL pipelines processing 10M rows daily.",
                    "Containerized services with Docker for staging deploys.",
                ],
            },
            {
                "title": "Junior Developer",
                "company": "Webshop",
                "dates": "2019-2021",
                "bullets": ["Fixed bugs in the storefront checkout."],
            },
        ],
        "education": [
            {"degree": "B.S. Computer Science", "school": "NYU", "dates": "2015-2019"}
        ],
    }


def _job() -> dict:
    return {
        "title": "Senior Backend Engineer",
        "company": "Fintech Co",
        "description": (
            "We need a backend engineer with deep Python experience and "
            "Docker expertise to build ETL pipelines. PostgreSQL required."
        ),
    }


class BaseResumeTests(unittest.TestCase):
    def test_byte_identical_on_empty_posting(self):
        """No matched keywords -> tailor output equals base output."""
        profile = _profile()
        tailored = tailor.tailor_resume(profile, {"title": "", "description": ""})
        self.assertEqual(tailored["resume_md"], tailor.base_resume_md(profile))

    def test_base_keeps_profile_bullet_order(self):
        base = tailor.base_resume_md(_profile())
        dash = base.index("Maintained legacy reporting dashboards.")
        etl = base.index("Built Python ETL pipelines")
        self.assertLess(dash, etl)

    def test_tailored_front_loads_matched_bullets(self):
        tailored = tailor.tailor_resume(_profile(), _job())["resume_md"]
        etl = tailored.index("Built Python ETL pipelines")
        dash = tailored.index("Maintained legacy reporting dashboards.")
        self.assertLess(etl, dash)

    def test_base_has_no_skills_section_but_tailored_does(self):
        profile = _profile()
        self.assertNotIn("## Skills", tailor.base_resume_md(profile))
        self.assertIn("## Skills", tailor.tailor_resume(profile, _job())["resume_md"])


class ResumeDiffTests(unittest.TestCase):
    def test_identical_inputs_report_no_changes(self):
        out = tailor.resume_diff("a\nb\n", "a\nb\n")
        first = out.splitlines()[0]
        self.assertIn("+0", first)
        self.assertIn("-0", first)
        self.assertIn("no changes", first)

    def test_header_counts_match_unified_diff_body(self):
        base = "line1\nline2\nline3\n"
        new = "line1\nline2 changed\nline4\n"
        out = tailor.resume_diff(base, new)
        header = out.splitlines()[0]
        added = sum(
            1 for l in out.splitlines()[2:] if l.startswith("+") and not l.startswith("+++")
        )
        removed = sum(
            1 for l in out.splitlines()[2:] if l.startswith("-") and not l.startswith("---")
        )
        self.assertIn(f"+{added}", header)
        self.assertIn(f"-{removed}", header)
        self.assertIn("@@", out)  # real unified-diff hunks present

    def test_diff_only_reorders_and_adds_skills(self):
        """Honesty: the diff must not contain words from outside the profile."""
        profile = _profile()
        base = tailor.base_resume_md(profile)
        result = tailor.tailor_resume(profile, _job())
        diff = tailor.resume_diff(base, result["resume_md"])
        added_lines = [
            l[1:] for l in diff.splitlines()
            if l.startswith("+") and not l.startswith("+++")
        ]
        blob = "\n".join(added_lines).lower()
        self.assertNotIn("kubernetes", blob)  # missing skill, must not appear
        # missing_skills reported, not merged into the resume
        self.assertNotIn("kubernetes", result["resume_md"].lower())
        self.assertIn("Python", result["matched_skills"])


class TailorWithDiffTests(unittest.TestCase):
    def test_returns_all_keys(self):
        result = tailor.tailor_with_diff(_profile(), _job())
        for key in (
            "resume_md", "cover_letter", "matched_skills",
            "missing_skills", "diff", "diff_summary",
        ):
            self.assertIn(key, result)

    def test_diff_summary_matches_diff_header(self):
        result = tailor.tailor_with_diff(_profile(), _job())
        self.assertEqual(result["diff_summary"], result["diff"].splitlines()[0])
        self.assertIn("Python", result["matched_skills"])


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = tailor.TAILORED_DIR
        tailor.TAILORED_DIR = Path(self._tmp.name) / "tailored"

    def tearDown(self):
        tailor.TAILORED_DIR = self._old
        self._tmp.cleanup()

    def test_save_load_round_trip(self):
        path = tailor.save_approved_variant("linkedin:9f2c", "# Resume\n", "Dear Hiring Manager,")
        self.assertTrue(path.is_file())
        loaded = tailor.load_approved_variant("linkedin:9f2c")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded["job_id"], "linkedin:9f2c")
        self.assertEqual(loaded["resume_md"], "# Resume\n")
        self.assertIn("approved_at", loaded)

    def test_load_missing_returns_none(self):
        self.assertIsNone(tailor.load_approved_variant("nope:123"))

    def test_job_id_sanitized_for_filename(self):
        path = tailor.save_approved_variant("../../etc/evil?x=1", "r", "c")
        self.assertNotIn("..", path.name)
        self.assertNotIn("/", path.name)
        self.assertTrue(path.parent == tailor.TAILORED_DIR)

    def test_list_returns_sorted_job_ids(self):
        tailor.save_approved_variant("b-job", "r", "c")
        tailor.save_approved_variant("a-job", "r", "c")
        self.assertEqual(tailor.list_approved_variants(), ["a-job", "b-job"])

    def test_corrupt_file_is_treated_as_absent(self):
        tailor.TAILORED_DIR.mkdir(parents=True, exist_ok=True)
        (tailor.TAILORED_DIR / "bad.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(tailor.load_approved_variant("bad"))
        self.assertEqual(tailor.list_approved_variants(), [])


if __name__ == "__main__":
    unittest.main()
