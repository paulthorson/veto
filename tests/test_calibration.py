#!/usr/bin/env python3
"""Tests for Initiative 02 scaffolding: calibration harness, versioned
model card, near-miss review queue.

The core invariant under test: personalized weights stay pinned OFF
until the evidence gate passes, and nothing in the harness can change
weights by itself.

Stdlib unittest only. The outcome event store is redirected via the
VETO_OUTCOMES_FILE env override (honored by outcomes.default_store_path);
model-card / near-miss stores are temp files.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import calibration
import model_card
import near_miss


def _ev(app_id, event_type, role="Software Engineer", at="2026-09-01T00:00:00+00:00"):
    return {
        "event_id": f"{app_id}-{event_type}",
        "application_id": app_id,
        "event_type": event_type,
        "occurred_at": at,
        "role": role,
        "company": "Acme",
    }


class _EventsMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = Path(self.tmp.name) / "outcomes.jsonl"
        self._env = mock.patch.dict(
            os.environ, {"VETO_OUTCOMES_FILE": str(self.events)}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def write_events(self, events):
        self.events.write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )


def _twenty_resolved_with_four_qualified():
    """20 resolved apps, 4 of them qualified replies (engineering)."""
    events = []
    for i in range(20):
        app = f"app-{i}"
        events.append(_ev(app, "applied", at="2026-08-01T00:00:00+00:00"))
        if i < 4:
            events.append(_ev(app, "replied", at="2026-08-05T00:00:00+00:00"))
            events.append(_ev(app, "interviewed", at="2026-08-10T00:00:00+00:00"))
        elif i < 10:
            events.append(_ev(app, "rejected", at="2026-08-06T00:00:00+00:00"))
        else:
            events.append(_ev(app, "replied", at="2026-08-05T00:00:00+00:00"))
            events.append(_ev(app, "withdrawn", at="2026-08-07T00:00:00+00:00"))
    return events


class TestEvidenceGate(_EventsMixin):
    def test_gate_not_met_on_empty_store(self):
        status = calibration.evidence_gate_status()
        self.assertFalse(status["gate_met"])
        self.assertEqual(status["resolved_outcomes"], 0)
        self.assertIn("pinned off", status["reason"])
        self.assertEqual(status["weights_source"], "static")

    def test_gate_counts_resolved_and_qualified(self):
        self.write_events(_twenty_resolved_with_four_qualified())
        status = calibration.evidence_gate_status()
        self.assertEqual(status["resolved_outcomes"], 20)
        self.assertEqual(status["qualified_replies"], 4)
        # All 20 are engineering: slice needs >=10 resolved and >=2 replies.
        self.assertTrue(status["gate_met"])
        self.assertEqual(status["weights_source"], "personalized (gate met)")

    def test_gate_fails_when_replies_too_few(self):
        events = []
        for i in range(20):
            app = f"app-{i}"
            events.append(_ev(app, "applied", at="2026-08-01T00:00:00+00:00"))
            events.append(_ev(app, "rejected", at="2026-08-06T00:00:00+00:00"))
        self.write_events(events)
        status = calibration.evidence_gate_status()
        self.assertEqual(status["resolved_outcomes"], 20)
        self.assertEqual(status["qualified_replies"], 0)
        self.assertFalse(status["gate_met"])

    def test_small_family_slice_blocks(self):
        events = _twenty_resolved_with_four_qualified()
        # Add a tiny design slice: 3 resolved, 0 qualified.
        for i in range(3):
            app = f"design-{i}"
            events.append(_ev(app, "applied", role="UX Designer",
                              at="2026-08-01T00:00:00+00:00"))
            events.append(_ev(app, "rejected", role="UX Designer",
                              at="2026-08-06T00:00:00+00:00"))
        self.write_events(events)
        status = calibration.evidence_gate_status()
        self.assertIn("design", status["slices_below_minimum"])
        self.assertFalse(status["gate_met"])

    def test_non_resolved_events_do_not_count(self):
        self.write_events([
            _ev("a1", "discovered"),
            _ev("a1", "shortlisted"),
            _ev("a2", "applied"),
        ])
        status = calibration.evidence_gate_status()
        self.assertEqual(status["resolved_outcomes"], 0)

    def test_role_family_heuristic(self):
        self.assertEqual(calibration.role_family("Senior Backend Engineer"),
                         "engineering")
        self.assertEqual(calibration.role_family("UX Designer"), "design")
        self.assertEqual(calibration.role_family("Data Analyst"), "data")
        self.assertEqual(calibration.role_family("Product Manager"), "product")
        self.assertEqual(calibration.role_family("Chief of Staff"), "other")


class TestWeightReport(_EventsMixin):
    def test_report_without_evidence_is_honest(self):
        report = calibration.weight_report()
        self.assertFalse(report["applied"])
        self.assertEqual(report["evidence"], "none")
        self.assertEqual(report["deltas"], {})
        self.assertFalse(report["gate"]["gate_met"])

    def test_report_never_applies(self):
        scored = [
            {"application_id": f"app-{i}",
             "components": {"skills": 40.0, "seniority": 12.0,
                            "salary": 10.0, "location": 12.0,
                            "recency": 4.0}}
            for i in range(20)
        ]
        self.write_events(_twenty_resolved_with_four_qualified())
        report = calibration.weight_report(scored)
        self.assertFalse(report["applied"])
        self.assertIn("skills", report["deltas"])
        self.assertIsNotNone(report["deltas"]["skills"].get("lift"))

    def test_harness_shape(self):
        result = calibration.run_harness()
        self.assertIn("gate", result)
        self.assertIn("weight_report", result)
        self.assertEqual(result["harness"], "calibration-v1")


class _CardMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.card = Path(self.tmp.name) / "model_card.jsonl"
        patch = mock.patch.object(model_card, "MODEL_CARD_FILE", self.card)
        patch.start()
        self.addCleanup(patch.stop)

    def change(self, **kw):
        kw.setdefault("weights", dict(model_card.STATIC_WEIGHTS))
        kw.setdefault("reason", "test")
        kw.setdefault("approved_by", "tester")
        kw["path"] = self.card
        return model_card.record_change(**kw)


class TestModelCard(_CardMixin):
    def test_current_is_v0_static_pinned(self):
        card = model_card.current(self.card)
        self.assertEqual(card["version"], 0)
        self.assertFalse(card["personalized"])
        self.assertTrue(card["pinned"])
        self.assertEqual(card["weights"], model_card.STATIC_WEIGHTS)

    def test_change_requires_approval(self):
        with self.assertRaises(ValueError):
            model_card.record_change(dict(model_card.STATIC_WEIGHTS),
                                     "x", "", path=self.card)

    def test_change_refused_while_pinned(self):
        with self.assertRaises(ValueError) as ctx:
            self.change()
        self.assertIn("pinned", str(ctx.exception))

    def test_personalized_change_refused_when_gate_fails(self):
        model_card.unpin("test", path=self.card)
        tweaked = dict(model_card.STATIC_WEIGHTS)
        tweaked["skills"] = 55.0
        with mock.patch("calibration.evidence_gate_status",
                        return_value={"gate_met": False,
                                      "reason": "no data",
                                      "resolved_outcomes": 0,
                                      "qualified_replies": 0}):
            with self.assertRaises(ValueError) as ctx:
                model_card.record_change(tweaked, "test", "tester",
                                         path=self.card)
        self.assertIn("evidence gate", str(ctx.exception))

    def test_personalized_change_allowed_when_gate_passes(self):
        model_card.unpin("test", path=self.card)
        tweaked = dict(model_card.STATIC_WEIGHTS)
        tweaked["skills"] = 55.0
        with mock.patch("calibration.evidence_gate_status",
                        return_value={"gate_met": True,
                                      "resolved_outcomes": 40,
                                      "qualified_replies": 8}):
            card = model_card.record_change(tweaked, "first tuning",
                                            "tester", path=self.card)
        self.assertEqual(card["version"], 2)  # v0 init + v1 unpin
        self.assertTrue(card["personalized"])
        self.assertEqual(card["approved_by"], "tester")
        self.assertTrue(card["gate_snapshot"]["gate_met"])

    def test_rollback_and_reset_are_append_only(self):
        model_card.unpin("test", path=self.card)
        self.change()
        rolled = model_card.rollback(0, "bad idea", path=self.card)
        self.assertEqual(rolled["weights"], model_card.STATIC_WEIGHTS)
        self.assertEqual(rolled["rollback_target"], 0)
        reset = model_card.reset("back to basics", path=self.card)
        self.assertTrue(reset["pinned"])
        self.assertFalse(reset["personalized"])
        # History kept: 4 appended versions (unpin, change, rollback, reset);
        # v0 init is virtual and always available as a rollback target.
        self.assertEqual(len(model_card.history(path=self.card)), 4)

    def test_rollback_unknown_version(self):
        with self.assertRaises(ValueError):
            model_card.rollback(99, path=self.card)


class TestNearMiss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "near_miss.json"
        patch = mock.patch.object(near_miss, "QUEUE_FILE", self.queue)
        patch.start()
        self.addCleanup(patch.stop)

    def _job(self, score_text):
        return {
            "job_id": "job-1",
            "title": "Software Engineer",
            "company": "Acme",
            "location": "Remote",
            "board": "greenhouse",
            "snippet": score_text,
            "posted_days_ago": 3,
        }

    def test_scan_queues_near_miss(self):
        # Veto-band job: weak skill match but not terrible.
        with mock.patch("match.score_job", return_value={
            "score": 55, "veto": True, "veto_reason": "VETOED: 55 < 60",
            "matched": [], "missing": ["Go"],
        }):
            result = near_miss.scan([self._job("x")])
        self.assertEqual(result["queued"], 1)
        item = result["items"][0]
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["score"], 55)

    def test_scan_skips_clear_veto_and_qualified(self):
        with mock.patch("match.score_job", return_value={
            "score": 20, "veto": True, "veto_reason": "VETOED",
            "matched": [], "missing": [],
        }):
            low = near_miss.scan([self._job("x")])
        with mock.patch("match.score_job", return_value={
            "score": 80, "veto": False, "matched": [], "missing": [],
        }):
            high = near_miss.scan([self._job("x")])
        self.assertEqual(low["queued"], 0)
        self.assertEqual(high["queued"], 0)

    def test_resolve_records_verdict(self):
        with mock.patch("match.score_job", return_value={
            "score": 55, "veto": True, "veto_reason": "VETOED",
            "matched": [], "missing": [],
        }):
            item_id = near_miss.scan([self._job("x")])["items"][0]["item_id"]
        result = near_miss.resolve(item_id, "correct_veto",
                                   note="stale posting", path=self.queue)
        self.assertEqual(result["verdict"], "correct_veto")
        self.assertEqual(near_miss.list_items(path=self.queue)[0]["status"],
                         "correct_veto")
        with self.assertRaises(ValueError):
            near_miss.resolve(item_id, "worth_a_look", path=self.queue)

    def test_resolve_rejects_bad_verdict(self):
        with self.assertRaises(ValueError):
            near_miss.resolve("nope", "maybe", path=self.queue)


if __name__ == "__main__":
    unittest.main()
