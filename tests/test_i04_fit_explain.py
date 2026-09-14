#!/usr/bin/env python3
"""Tests for ``initiatives.i04.fit_explain`` (Epic 1 combined surface).

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

from initiatives.i04 import fit_explain  # noqa: E402
from initiatives.i04.schemas import validate_fit_result  # noqa: E402

#: SYNTHETIC FIXTURE — fake JD. Nothing here is real.
SYNTHETIC_JD = """Senior Widget Engineer — Synthetic Systems Inc.
$140k-$180k salary range. 4-day week, remote-first.

Requirements:
- 5+ years of Python experience
- Experience with Kubernetes in production

Nice to have:
- Familiarity with Rust
"""

#: SYNTHETIC FIXTURE — fake resume. Nothing here is real.
SYNTHETIC_RESUME = """Jane Synthetic

Experience
Senior Software Engineer, Fictional Corp
- 6 years Python building data pipelines; reduced ETL runtime 40%
- Ran Kubernetes in production across 12 services

Skills
Python, Kubernetes, Docker
"""


class FitExplainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.outcome = fit_explain.fit_explain(
            SYNTHETIC_RESUME, SYNTHETIC_JD, "synthetic-job-001",
            job_title="Senior Widget Engineer (synthetic)",
        )
        self.result = self.outcome["fit_result"]

    def test_result_validates(self) -> None:
        self.assertEqual(self.outcome["validation_errors"], [])
        self.assertEqual(validate_fit_result(self.result), [])

    def test_score_in_range_with_components(self) -> None:
        self.assertGreaterEqual(self.result["fit_score"], 0)
        self.assertLessEqual(self.result["fit_score"], 100)
        for key in ("skills", "seniority", "salary", "location", "recency"):
            self.assertIn(key, self.result["components"], key)

    def test_provenance_is_static_until_02_lands(self) -> None:
        self.assertEqual(self.result["provenance"], {"kind": "static", "n": 0})
        self.assertIn("Static score", self.result["provenance_note"])

    def test_evidence_map_embedded(self) -> None:
        ev_map = self.result["evidence_map"]
        self.assertEqual(ev_map["schema"], "veto/evidence-map/v1")
        self.assertTrue(ev_map["entries"])
        summary = self.result["evidence_summary"]
        self.assertEqual(
            summary["supported"] + summary["gaps"] + summary["grill_questions"],
            len(ev_map["entries"]),
        )

    def test_jd_verdict_included(self) -> None:
        jd_verdict = self.result["jd_verdict"]
        self.assertIn(jd_verdict["verdict"], ("strong", "mixed", "caution"))
        self.assertTrue(jd_verdict["reasons"])

    def test_limitations_always_present(self) -> None:
        self.assertTrue(self.result["limitations"])
        blob = " ".join(self.result["limitations"])
        self.assertIn("no language model", blob)

    def test_explain_lines_human_readable(self) -> None:
        self.assertTrue(self.result["explain"])
        self.assertIn("Fit score", self.result["explain"][0])

    def test_empty_inputs_degrade_gracefully(self) -> None:
        outcome = fit_explain.fit_explain("", "", "syn-empty")
        self.assertEqual(outcome["validation_errors"], [])
        result = outcome["fit_result"]
        self.assertTrue(
            any("No resume text" in note for note in result["limitations"])
        )
        self.assertTrue(
            any("No job description" in note for note in result["limitations"])
        )

    def test_raw_text_never_in_result(self) -> None:
        # The fit result carries quotes/fragments, never the raw inputs
        # wholesale. (Full privacy proof lives in the share-card scrub.)
        blob = str(self.result)
        self.assertNotIn(SYNTHETIC_RESUME, blob)
        self.assertNotIn(SYNTHETIC_JD, blob)


if __name__ == "__main__":
    unittest.main()
