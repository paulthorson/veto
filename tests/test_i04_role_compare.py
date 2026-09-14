#!/usr/bin/env python3
"""Tests for ``initiatives.i04.role_compare`` (Epic 3).

All fixtures are synthetic and clearly labeled; no real employers,
people, or achievements appear anywhere in this file.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from initiatives.i04 import role_compare  # noqa: E402

#: SYNTHETIC FIXTURES — fake postings/resume. Nothing here is real.
JD_A = """Senior Widget Engineer — Synthetic Systems Inc.
$140k-$180k. Remote-first.

Requirements:
- 5+ years of Python experience
- Experience with Kubernetes in production
"""

JD_B = """Junior Gadget Engineer — Fictional Corp
$80k-$95k. On-site in Springfield.

Requirements:
- 1+ years of Python experience
"""

JD_C = """Mystery Role — Placeholder LLC
Competitive salary.

Requirements:
- 5+ years of Python experience
"""

RESUME = """Jane Synthetic

Experience
Senior Software Engineer, Fictional Corp
- 6 years Python building data pipelines; reduced ETL runtime 40%
- Ran Kubernetes in production across 12 services

Skills
Python, Kubernetes, Docker
"""


def _job(job_id: str, jd: str, title: str = "") -> dict:
    return {"job_id": job_id, "job_title": title or job_id, "jd_text": jd}


class CompareRolesTest(unittest.TestCase):
    def test_four_job_cap_enforced(self) -> None:
        jobs = [_job(f"j{i}", JD_A) for i in range(5)]
        with self.assertRaises(ValueError):
            role_compare.compare_roles(jobs, RESUME)

    def test_missing_job_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            role_compare.compare_roles([{"jd_text": JD_A}], RESUME)

    def test_live_rows_ranked_with_all_axes(self) -> None:
        result = role_compare.compare_roles(
            [_job("a", JD_A, "Senior Widget"), _job("b", JD_B, "Junior Gadget")],
            RESUME,
        )
        self.assertEqual(len(result["jobs"]), 2)
        for row in result["jobs"]:
            for axis in ("fit_score", "components", "provenance", "risk",
                         "compensation", "location", "readiness"):
                self.assertIn(axis, row, axis)
            self.assertEqual(row["score_source"], "live")
        # Senior role should outscore the junior one for this resume.
        self.assertEqual(result["jobs"][0]["job_id"], "a")

    def test_compensation_axis_shown_with_two_ranges(self) -> None:
        result = role_compare.compare_roles(
            [_job("a", JD_A), _job("b", JD_B)], RESUME
        )
        self.assertTrue(result["compensation_axis"]["shown"])

    def test_compensation_axis_hidden_with_reason(self) -> None:
        result = role_compare.compare_roles(
            [_job("a", JD_A), _job("c", JD_C)], RESUME
        )
        axis = result["compensation_axis"]
        self.assertFalse(axis["shown"])
        self.assertIn("only 1 of 2", axis["reason"])

    def test_snapshot_rows_labeled_unknown_readiness(self) -> None:
        snapshot = {
            "score": 70,
            "components": {"skills": 30, "seniority": 15, "salary": 10,
                           "location": 10, "recency": 5},
            "model_version": "m1",
            "weights_version": "w1",
            "confidence": {"kind": "static", "n": 0},
        }
        job = _job("s", JD_A)
        job["score_snapshot"] = snapshot
        result = role_compare.compare_roles([job], RESUME)
        row = result["jobs"][0]
        self.assertEqual(row["score_source"], "snapshot")
        self.assertEqual(row["fit_score"], 70)
        self.assertEqual(row["readiness"]["label"], "unknown")
        self.assertEqual(row["provenance"], {"kind": "static", "n": 0})

    def test_mixed_versions_warn(self) -> None:
        def snap(mv: str) -> dict:
            return {"score": 70, "components": {}, "model_version": mv,
                    "weights_version": "w1",
                    "confidence": {"kind": "static", "n": 0}}
        jobs = [_job("a", JD_A), _job("b", JD_B)]
        jobs[0]["score_snapshot"] = snap("m1")
        jobs[1]["score_snapshot"] = snap("m2")
        result = role_compare.compare_roles(jobs, RESUME)
        self.assertIsNotNone(result["comparability_warning"])
        self.assertIn("m1", result["comparability_warning"])

    def test_same_versions_no_warning(self) -> None:
        result = role_compare.compare_roles([_job("a", JD_A)], RESUME)
        self.assertIsNone(result["comparability_warning"])

    def test_versionless_snapshot_does_not_crash(self) -> None:
        snapshot = {"score": 70, "components": {},
                    "confidence": {"kind": "static", "n": 0}}
        job = _job("s", JD_A)
        job["score_snapshot"] = snapshot
        result = role_compare.compare_roles([job, _job("b", JD_B)], RESUME)
        self.assertIsNotNone(result["comparability_warning"])

    def test_salary_mention_display_is_string(self) -> None:
        result = role_compare.compare_roles([_job("c", JD_C)], RESUME)
        comp = result["jobs"][0]["compensation"]
        self.assertEqual(comp["kind"], "mention")
        self.assertIsInstance(comp["display"], str)
        self.assertIn("Competitive salary", comp["display"])

    def test_readiness_labels(self) -> None:
        result = role_compare.compare_roles(
            [_job("a", JD_A, "Senior"), _job("b", JD_B, "Junior")], RESUME
        )
        by_id = {r["job_id"]: r for r in result["jobs"]}
        # Senior role: both must-haves supported -> ready.
        self.assertEqual(by_id["a"]["readiness"]["label"], "ready")
        # Junior role: 1+ years python, 3... resume claims 6 -> ready too,
        # but the label must be one of the controlled set regardless.
        self.assertIn(by_id["b"]["readiness"]["label"],
                      ("ready", "close", "gaps to close", "unknown"))


if __name__ == "__main__":
    unittest.main()
