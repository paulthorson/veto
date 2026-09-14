#!/usr/bin/env python3
"""Tests for ``initiatives.i04.career_graph`` (Epic 4).

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

from initiatives.i04 import career_graph  # noqa: E402


def _event(app_id: str, event_type: str, occurred_at: str,
           role: str = "Senior Widget Engineer (synthetic)",
           schema_version: str = "outcome-min-v0") -> dict:
    return {
        "event_id": f"ev-{app_id}-{event_type}",
        "application_id": app_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "source": "manual",
        "role": role,
        "provenance": {"actor": "user"},
        "schema_version": schema_version,
    }


def _fit_result(score: int, entries: list[tuple[str, str]]) -> dict:
    return {
        "schema": "veto/fit-result/v1",
        "fit_score": score,
        "evidence_map": {
            "schema": "veto/evidence-map/v1",
            "entries": [
                {"requirement": {"text": label, "source_quote": label,
                                 "kind": "must_have"},
                 "status": status, "evidence": [], "grill_question_id": None}
                for label, status in entries
            ],
        },
    }


class CareerGraphTest(unittest.TestCase):
    def test_unknown_schema_version_refuses_to_render(self) -> None:
        events = [_event("a1", "applied", "2026-09-01T10:00:00+00:00",
                         schema_version="outcome-v9")]
        result = career_graph.build_career_graph(events)
        self.assertFalse(result["rendered"])
        self.assertIn("Refusing to render", result["reason"])

    def test_timeline_groups_and_orders(self) -> None:
        events = [
            _event("a1", "interviewed", "2026-09-05T10:00:00+00:00"),
            _event("a1", "applied", "2026-09-01T10:00:00+00:00"),
            _event("a1", "replied", "2026-09-03T10:00:00+00:00"),
        ]
        result = career_graph.build_career_graph(events)
        self.assertTrue(result["rendered"])
        tl = result["timeline"]["a1"]
        self.assertEqual(
            [e["event_type"] for e in tl["events"]],
            ["applied", "replied", "interviewed"],
        )
        self.assertEqual(tl["current_state"], "interviewed")
        self.assertTrue(tl["progressed"])
        self.assertFalse(tl["terminal"])

    def test_terminal_state_detected(self) -> None:
        events = [
            _event("a2", "applied", "2026-09-01T10:00:00+00:00"),
            _event("a2", "rejected", "2026-09-10T10:00:00+00:00"),
        ]
        result = career_graph.build_career_graph(events)
        self.assertTrue(result["timeline"]["a2"]["terminal"])

    def test_strengths_and_gaps_trended(self) -> None:
        events = [
            _event("a1", "applied", "2026-09-01T10:00:00+00:00"),
            _event("a2", "applied", "2026-09-02T10:00:00+00:00"),
        ]
        fits = {
            "a1": _fit_result(80, [("python", "supported"), ("rust", "gap")]),
            "a2": _fit_result(70, [("python", "supported"), ("rust", "gap")]),
        }
        result = career_graph.build_career_graph(events, fits)
        strengths = {s["requirement"]: s["times_supported"]
                     for s in result["strengths"]}
        self.assertEqual(strengths.get("python"), 2)
        gaps = {g["requirement"]: g for g in result["gaps"]}
        self.assertEqual(gaps["rust"]["times_seen"], 2)
        self.assertEqual(gaps["rust"]["first_seen"], "2026-09-01T10:00:00+00:00")
        self.assertEqual(gaps["rust"]["last_seen"], "2026-09-02T10:00:00+00:00")

    def test_seniority_trend_uses_controlled_vocabulary(self) -> None:
        events = [
            _event("a1", "applied", "2026-09-01T10:00:00+00:00",
                   role="Junior Widget Engineer"),
            _event("a2", "applied", "2026-09-02T10:00:00+00:00",
                   role="Senior Widget Engineer"),
        ]
        result = career_graph.build_career_graph(events)
        trend = result["seniority_trend"]
        self.assertEqual([p["seniority"] for p in trend], ["junior", "senior"])
        for point in trend:
            self.assertIn(point["seniority"], career_graph.SENIORITY_LEVELS)

    def test_gap_dates_use_min_max_not_input_order(self) -> None:
        # Regression: backfilled applications arrive newest-first; the
        # old code set first_seen from the first *processed* app, which
        # inverted the range. first_seen/last_seen must be the min/max
        # of occurred across applications by parsed time.
        events = [
            _event("a2", "applied", "2026-09-02T10:00:00+00:00"),
            _event("a1", "applied", "2026-09-01T10:00:00+00:00"),
        ]
        fits = {
            "a1": _fit_result(70, [("rust", "gap")]),
            "a2": _fit_result(70, [("rust", "gap")]),
        }
        result = career_graph.build_career_graph(events, fits)
        gap = result["gaps"][0]
        self.assertEqual(gap["requirement"], "rust")
        self.assertEqual(gap["first_seen"], "2026-09-01T10:00:00+00:00")
        self.assertEqual(gap["last_seen"], "2026-09-02T10:00:00+00:00")

    def test_gap_dates_stable_under_mixed_timestamp_formats(self) -> None:
        # Mixed ISO-8601 spellings of the same instant must not perturb
        # the range.
        events = [
            _event("a2", "applied", "2026-09-02T10:00:00Z"),
            _event("a1", "applied", "2026-09-01T10:00:00+00:00"),
        ]
        fits = {
            "a1": _fit_result(70, [("rust", "gap")]),
            "a2": _fit_result(70, [("rust", "gap")]),
        }
        result = career_graph.build_career_graph(events, fits)
        gap = result["gaps"][0]
        self.assertEqual(gap["first_seen"], "2026-09-01T10:00:00+00:00")
        self.assertEqual(gap["last_seen"], "2026-09-02T10:00:00Z")

    def test_label_seniority_most_specific_keyword_wins(self) -> None:
        # Real titles the old first-match-wins logic mislabeled:
        # "SVP of Engineering"->mid, "Chief of Staff"->staff,
        # "Senior VP of Sales"->senior.
        self.assertEqual(
            career_graph.label_seniority("SVP of Engineering"), "executive")
        self.assertEqual(
            career_graph.label_seniority("Chief of Staff"), "executive")
        self.assertEqual(
            career_graph.label_seniority("Senior VP of Sales"), "senior")
        # "svp" is not a prefix match for "vp": the word boundary in
        # "svp" keeps the more specific executive label.
        self.assertEqual(
            career_graph.label_seniority("svp, Widget Co"), "executive")

    def test_corrections_exclude_superseded_originals_by_default(self) -> None:
        # Mirrors 01's reader default: a correction's superseded original
        # is invisible; include_superseded=True keeps the audit trail.
        original = _event("a1", "applied", "2026-09-01T10:00:00+00:00",
                          role="Junior Widget Engineer")
        original["event_id"] = "ev-original-1"
        correction = _event("a1", "applied", "2026-09-01T10:00:00+00:00",
                           role="Senior Widget Engineer")
        correction["event_id"] = "ev-correction-1"
        correction["corrects"] = "ev-original-1"

        result = career_graph.build_career_graph([original, correction])
        tl = result["timeline"]["a1"]
        self.assertEqual(len(tl["events"]), 1)
        self.assertEqual(tl["role"], "Senior Widget Engineer")

        audit = career_graph.build_career_graph(
            [original, correction], include_superseded=True)
        self.assertEqual(len(audit["timeline"]["a1"]["events"]), 2)

    def test_timeline_sorts_by_parsed_timestamp_not_raw_string(self) -> None:
        # Lexicographic sorting would put "06:00:01-04:00" before
        # "10:00:00Z" even though it happened a second later.
        events = [
            _event("a1", "interviewed", "2026-09-01T10:00:02+00:00"),
            _event("a1", "replied", "2026-09-01T06:00:01-04:00"),
            _event("a1", "applied", "2026-09-01T10:00:00Z"),
        ]
        result = career_graph.build_career_graph(events)
        self.assertEqual(
            [e["event_type"] for e in result["timeline"]["a1"]["events"]],
            ["applied", "replied", "interviewed"],
        )

    def test_label_seniority_defaults_mid(self) -> None:
        self.assertEqual(career_graph.label_seniority("Widget Engineer"), "mid")
        self.assertEqual(career_graph.label_seniority("Staff Engineer"), "staff")
        self.assertEqual(career_graph.label_seniority("VP Engineering"), "executive")

    def test_small_bands_labeled_insufficient(self) -> None:
        events = [_event(f"a{i}", "applied", f"2026-09-0{i+1}T10:00:00+00:00")
                  for i in range(3)]
        fits = {f"a{i}": _fit_result(80, []) for i in range(3)}
        result = career_graph.build_career_graph(events, fits)
        band = result["conversion_by_fit_band"]["high (>=75)"]
        self.assertEqual(band["label"], "insufficient_data")
        self.assertEqual(band["n"], 3)

    def test_conversion_rate_reported_with_sample(self) -> None:
        events = []
        fits = {}
        for i in range(6):
            app = f"b{i}"
            events.append(_event(app, "applied", f"2026-08-0{i+1}T10:00:00+00:00"))
            if i < 2:
                events.append(
                    _event(app, "replied", f"2026-08-1{i+1}T10:00:00+00:00"))
            fits[app] = _fit_result(80, [])
        result = career_graph.build_career_graph(events, fits)
        band = result["conversion_by_fit_band"]["high (>=75)"]
        self.assertEqual(band["label"], "reported")
        self.assertEqual(band["n"], 6)
        self.assertAlmostEqual(band["reply_rate"], 2 / 6, places=3)

    def test_apps_without_fit_results_timeline_only(self) -> None:
        events = [_event("a1", "applied", "2026-09-01T10:00:00+00:00")]
        result = career_graph.build_career_graph(events, {})
        self.assertTrue(result["rendered"])
        self.assertEqual(result["strengths"], [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(len(result["seniority_trend"]), 1)

    def test_limitations_always_shipped(self) -> None:
        result = career_graph.build_career_graph([])
        self.assertTrue(result["rendered"])
        self.assertTrue(result["limitations"])


if __name__ == "__main__":
    unittest.main()
