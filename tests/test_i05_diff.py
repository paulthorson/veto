"""Tests for Initiative 05 side-by-side diff explanations (Epic 4).

All fixtures are synthetic and labeled as such.
"""

from __future__ import annotations

import unittest

import tailor
from initiatives.i05 import diff_explain


def _profile() -> dict:
    return {
        "full_name": "Synthetic Fixture",
        "email": "fixture@example.com",
        "summary": "Synthetic fixture profile for tests.",
        "skills": ["Python", "PostgreSQL", "Docker"],
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "FixtureCorp",
                "dates": "2020 - 2024",
                "bullets": [
                    "Mentored junior engineers.",
                    "Shipped Python services handling 1M requests a day.",
                ],
            }
        ],
    }


def _job() -> dict:
    return {
        "id": "job-synth-1",
        "title": "Backend Engineer",
        "description": "We need Python and PostgreSQL experience.",
    }


class ExplainDiffTest(unittest.TestCase):
    def setUp(self):
        self.profile = _profile()
        self.job = _job()
        self.base = tailor.base_resume_md(self.profile)
        self.result = tailor.tailor_with_provenance(self.profile, self.job)

    def test_every_change_gets_an_explanation(self):
        changes = diff_explain.explain_resume_diff(
            self.base, self.result["resume_md"],
            matched_skills=self.result["matched_skills"],
            statement_map=self.result["provenance"]["statement_map"])
        self.assertTrue(changes)
        for change in changes:
            self.assertTrue(change["explanation"])
            self.assertIn(change["type"],
                          {"reorder", "summary", "skills", "added", "removed"})
            self.assertFalse(change["reviewed"])

    def test_reorder_detected_with_skill_hint(self):
        changes = diff_explain.explain_resume_diff(
            self.base, self.result["resume_md"],
            matched_skills=self.result["matched_skills"],
            statement_map=self.result["provenance"]["statement_map"])
        reorders = [c for c in changes if c["type"] == "reorder"]
        # The Python bullet moves above the mentoring bullet.
        self.assertTrue(reorders)
        self.assertTrue(
            any("Python" in c["explanation"] for c in reorders))

    def test_reorder_carries_evidence(self):
        changes = diff_explain.explain_resume_diff(
            self.base, self.result["resume_md"],
            matched_skills=self.result["matched_skills"],
            statement_map=self.result["provenance"]["statement_map"])
        reorders = [c for c in changes if c["type"] == "reorder"]
        self.assertTrue(
            any(c["evidence_ids"] for c in reorders))

    def test_identical_resumes_have_no_changes(self):
        changes = diff_explain.explain_resume_diff(self.base, self.base)
        self.assertEqual(changes, [])
        text = diff_explain.render_explanations_text(changes)
        self.assertIn("No changes", text)

    def test_change_ids_are_stable(self):
        a = diff_explain.explain_resume_diff(self.base,
                                             self.result["resume_md"])
        b = diff_explain.explain_resume_diff(self.base,
                                             self.result["resume_md"])
        self.assertEqual([c["change_id"] for c in a],
                         [c["change_id"] for c in b])

    def test_review_gate(self):
        changes = diff_explain.explain_resume_diff(
            self.base, self.result["resume_md"])
        self.assertTrue(diff_explain.pending_changes(changes))
        for change in changes:
            self.assertTrue(
                diff_explain.mark_reviewed(changes, change["change_id"]))
        self.assertEqual(diff_explain.pending_changes(changes), [])
        self.assertFalse(
            diff_explain.mark_reviewed(changes, "chg_nope"))
        text = diff_explain.render_explanations_text(changes)
        self.assertIn("ready for approval", text)


class SideBySideTest(unittest.TestCase):
    def test_pairs_cover_both_texts(self):
        base = tailor.base_resume_md(_profile())
        tailored = tailor.tailor_with_provenance(_profile(), _job())["resume_md"]
        pairs = diff_explain.side_by_side(base, tailored)
        self.assertTrue(pairs)
        kinds = {p["kind"] for p in pairs}
        self.assertTrue(kinds <= {"same", "changed", "added", "removed"})
        # Every non-blank base line appears on the left exactly once.
        lefts = [p["left"] for p in pairs if p["left"]]
        for line in base.splitlines():
            if line.strip():
                self.assertIn(line, lefts)


class LetterDiffTest(unittest.TestCase):
    def test_letter_changes_explained(self):
        base = "Dear Hiring Manager,\n\nHello.\n\nThanks."
        new = "Dear Hiring Manager,\n\nHello there.\n\nThanks."
        changes = diff_explain.explain_letter_diff(base, new)
        self.assertEqual(len(changes), 2)  # one removed, one added
        types = {c["type"] for c in changes}
        self.assertEqual(types, {"removed", "added"})

    def test_identical_letters_no_changes(self):
        self.assertEqual(
            diff_explain.explain_letter_diff("a\n\nb", "a\n\nb"), [])


if __name__ == "__main__":
    unittest.main()
