"""Initiative 08 — aggregation tests.

k>=25 floor (upward-tunable only), sparse slices fully suppressed,
research/diagnostics/provider-health never emit record-level rows and
reject content fields by schema.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from initiatives.i08 import aggregation  # noqa: E402


def _rows(n, **extra):
    rows = []
    for i in range(n):
        row = {"contributor_id": f"c-{i:03d}", "persona_id": "p1",
               "stars": 4, "turn_count": 12,
               "outcome_bucket": "applied" if i % 2 else "no_response"}
        row.update(extra)
        rows.append(row)
    return rows


class AggregationTestCase(unittest.TestCase):
    def test_floor_is_25_and_default_is_25(self):
        self.assertEqual(aggregation.K_FLOOR, 25)
        self.assertEqual(aggregation.DEFAULT_MIN_COHORT_K, 25)
        self.assertEqual(aggregation.min_cohort_k(), 25)

    def test_minimum_never_lowered(self):
        with self.assertRaises(ValueError):
            aggregation.min_cohort_k({"min_cohort_k": 5})
        with self.assertRaises(ValueError):
            aggregation.min_cohort_k({"min_cohort_k": 24})
        # upward tuning is allowed
        self.assertEqual(aggregation.min_cohort_k({"min_cohort_k": 50}), 50)

    def test_cohort_check(self):
        check = aggregation.cohort_check(10)
        self.assertFalse(check["meets_minimum"])
        self.assertTrue(check["suppressed"])
        check = aggregation.cohort_check(25)
        self.assertTrue(check["meets_minimum"])

    def test_sparse_slices_fully_suppressed(self):
        result = aggregation.suppress_sparse_slices(
            {"persona-a": 30, "persona-b": 10})
        self.assertEqual(result["published"], {"persona-a": 30})
        self.assertEqual(result["suppressed"], ["persona-b"])
        # suppressed slices are dropped, never published as "low N"
        self.assertNotIn("persona-b", result["published"])

    def test_research_aggregate_shape(self):
        result = aggregation.build_research_aggregate(
            _rows(30), group_key="persona_id", metric="stars")
        self.assertEqual(result["k"], 25)
        self.assertFalse(result["record_level"])
        self.assertEqual(len(result["slices"]), 1)
        self.assertEqual(result["slices"]["p1"]["n"], 30)
        # no record-level rows anywhere
        self.assertNotIn("rows", result)
        self.assertNotIn("records", result)
        self.assertNotIn("sessions", result)

    def test_sparse_research_slice_suppressed(self):
        result = aggregation.build_research_aggregate(
            _rows(10), group_key="persona_id", metric="stars")
        self.assertEqual(result["slices"], {})
        self.assertEqual(result["suppressed"], ["p1"])

    def test_content_fields_rejected(self):
        for builder in (aggregation.build_research_aggregate,
                        aggregation.build_diagnostics_aggregate,
                        aggregation.build_provider_health_aggregate):
            with self.assertRaises(ValueError, msg=builder.__name__):
                builder(_rows(30, transcript_excerpt="hello"),
                        group_key="persona_id", metric="stars")
            with self.assertRaises(ValueError, msg=builder.__name__):
                builder(_rows(30, note="private"),
                        group_key="persona_id", metric="stars")

    def test_purpose_labels(self):
        diag = aggregation.build_diagnostics_aggregate(
            _rows(30), group_key="persona_id", metric="stars")
        self.assertEqual(diag["purpose"], "product_diagnostics")
        self.assertFalse(diag["record_level"])
        health = aggregation.build_provider_health_aggregate(
            _rows(30), group_key="persona_id", metric="stars")
        self.assertEqual(health["purpose"], "provider_health")

    def test_generalize_ladder(self):
        ladder = {"Initech": "tech"}
        self.assertEqual(aggregation.generalize("Initech", ladder),
                         {"level": "category", "label": "tech"})
        self.assertEqual(aggregation.generalize("Unknown Co", ladder),
                         {"level": "all", "label": "all"})

    def test_generalize_buckets(self):
        self.assertEqual(aggregation.generalize_stars(5), "4-5")
        self.assertEqual(aggregation.generalize_stars(3), "3")
        self.assertEqual(aggregation.generalize_stars(1), "1-2")
        self.assertEqual(aggregation.generalize_turns(3), "1-5")
        self.assertEqual(aggregation.generalize_turns(12), "6-20")
        self.assertEqual(aggregation.generalize_turns(40), "21+")


if __name__ == "__main__":
    unittest.main()
