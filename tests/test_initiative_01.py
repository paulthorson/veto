#!/usr/bin/env python3
"""Tests for Initiative 01 data-plumbing: funnel views, experiment
ledger, outcome nudges, and verification of the capture contract
(outcome-min-v0) supplied by the capture team.

Stdlib unittest only. The event store is redirected via VETO_OUTCOMES_FILE;
applications/ledger/nudge stores are temp files.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import experiment_ledger
import funnels
import outcome_nudges
import outcomes


def _ev(app_id, event_type, role="Software Engineer", company="Acme",
        at="2026-09-01T00:00:00+00:00", **kw):
    ev = {
        "event_id": f"{app_id}-{event_type}-{at[:10]}",
        "application_id": app_id,
        "event_type": event_type,
        "occurred_at": at,
        "role": role,
        "company": company,
    }
    ev.update(kw)
    return ev


class _StoreMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmpdir = Path(self.tmp.name)
        self.events = tmpdir / "outcomes.jsonl"
        self.apps = tmpdir / "applications.json"
        self.ledger = tmpdir / "experiment_ledger.jsonl"
        env = mock.patch.dict(
            os.environ, {"VETO_OUTCOMES_FILE": str(self.events)})
        env.start()
        self.addCleanup(env.stop)
        for mod, attr, path in (
            (funnels, "APPLICATIONS_FILE", self.apps),
            (experiment_ledger, "LEDGER_FILE", self.ledger),
            (outcome_nudges, "APPLICATIONS_FILE", self.apps),
        ):
            patch = mock.patch.object(mod, attr, path)
            patch.start()
            self.addCleanup(patch.stop)

    def write_events(self, events):
        self.events.write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    def write_apps(self, apps):
        self.apps.write_text(json.dumps(apps), encoding="utf-8")

    def app(self, job_id, **kw):
        base = {
            "job_id": job_id,
            "company": "Acme",
            "title": "Software Engineer",
            "board": "greenhouse",
            "stage": "applied",
            "submitted_at": "2026-09-01T00:00:00+00:00",
            "stage_history": [],
        }
        base.update(kw)
        return base


class TestFunnelViews(_StoreMixin):
    def test_funnel_by_source(self):
        self.write_apps([self.app("a1"), self.app("a2", board="lever")])
        self.write_events([
            _ev("a1", "applied", at="2026-09-01T00:00:00+00:00"),
            _ev("a1", "replied", at="2026-09-05T00:00:00+00:00"),
            _ev("a2", "applied", at="2026-09-02T00:00:00+00:00"),
        ])
        result = funnels.funnel(by="source")
        self.assertEqual(result["total"], 2)
        gh = result["slices"]["greenhouse"]
        self.assertEqual(gh["stages"]["applied"], 0)
        self.assertEqual(gh["stages"]["replied"], 1)
        self.assertEqual(gh["conversion"]["replied"], 1.0)
        self.assertEqual(result["slices"]["lever"]["stages"]["applied"], 1)

    def test_funnel_by_role_family(self):
        self.write_apps([
            self.app("a1", title="Backend Engineer"),
            self.app("a2", title="UX Designer"),
        ])
        result = funnels.funnel(by="role_family")
        self.assertIn("engineering", result["slices"])
        self.assertIn("design", result["slices"])

    def test_funnel_by_cohort(self):
        self.write_apps([
            self.app("a1", submitted_at="2026-09-01T00:00:00+00:00"),
            self.app("a2", submitted_at="2026-09-10T00:00:00+00:00"),
        ])
        result = funnels.funnel(by="cohort")
        self.assertEqual(len(result["slices"]), 2)

    def test_funnel_terminal_exits(self):
        self.write_apps([self.app("a1")])
        self.write_events([_ev("a1", "rejected",
                               at="2026-09-06T00:00:00+00:00")])
        result = funnels.funnel(by="source")
        self.assertEqual(
            result["slices"]["greenhouse"]["exits"]["rejected"], 1)

    def test_funnel_rejects_bad_dimension(self):
        with self.assertRaises(ValueError):
            funnels.funnel(by="bogus")

    def test_fit_band_from_ledger(self):
        self.write_apps([self.app("a1")])
        experiment_ledger.record("a1", ranking_version="static-v0",
                                 fit_score=88.0)
        result = funnels.funnel(by="fit_band")
        self.assertIn("excellent", result["slices"])

    def test_resume_variant_from_ledger(self):
        self.write_apps([self.app("a1")])
        experiment_ledger.record("a1", ranking_version="static-v0",
                                 tailoring_version="tailor-v2")
        result = funnels.funnel(by="resume_variant")
        self.assertIn("tailor-v2", result["slices"])

    def test_funnel_is_read_only(self):
        self.write_apps([self.app("a1")])
        before = (self.apps.read_text(encoding="utf-8"),
                  self.events.read_text(encoding="utf-8")
                  if self.events.exists() else None)
        funnels.funnel(by="source")
        after = (self.apps.read_text(encoding="utf-8"),
                 self.events.read_text(encoding="utf-8")
                 if self.events.exists() else None)
        self.assertEqual(before, after)


class TestRenderTimeline(_StoreMixin):
    def test_readable_timeline(self):
        self.write_events([
            _ev("a1", "applied", at="2026-09-01T10:00:00+00:00",
                provenance={"actor": "user", "method": "cli"}),
            _ev("a1", "replied", at="2026-09-05T10:00:00+00:00",
                provenance={"actor": "system",
                            "method": "reply-radar",
                            "note": "classified interview_invite"}),
        ])
        text = funnels.render_timeline("a1")
        self.assertIn("applied", text)
        self.assertIn("replied", text)
        self.assertIn("2026-09-05", text)
        self.assertNotIn("{", text)  # no raw JSON

    def test_empty_timeline_message(self):
        self.assertIn("No outcome events",
                      funnels.render_timeline("missing"))


class TestExperimentLedger(_StoreMixin):
    def test_record_and_assignment(self):
        entry = experiment_ledger.record(
            "a1", ranking_version="static-v0",
            tailoring_version="tailor-v2", fit_score=82.5,
            fit_components={"skills": 40.0, "seniority": 12.0,
                            "salary": 10.0, "location": 15.0,
                            "recency": 5.0},
            note="daily brief pick",
        )
        self.assertEqual(entry["ranking_version"], "static-v0")
        got = experiment_ledger.assignment("a1")
        self.assertEqual(got["tailoring_version"], "tailor-v2")
        self.assertEqual(got["fit_score"], 82.5)

    def test_latest_wins(self):
        experiment_ledger.record("a1", ranking_version="static-v0")
        experiment_ledger.record("a1", ranking_version="static-v1")
        self.assertEqual(
            experiment_ledger.assignment("a1")["ranking_version"],
            "static-v1",
        )

    def test_requires_ids(self):
        with self.assertRaises(ValueError):
            experiment_ledger.record("", ranking_version="static-v0")
        with self.assertRaises(ValueError):
            experiment_ledger.record("a1", ranking_version="")

    def test_linked_evidence_shape(self):
        experiment_ledger.record(
            "a1", ranking_version="static-v0",
            fit_components={"skills": 40.0, "seniority": 12.0,
                            "salary": 10.0, "location": 15.0,
                            "recency": 5.0},
        )
        experiment_ledger.record("a2", ranking_version="static-v0")
        linked = experiment_ledger.linked_evidence()
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["application_id"], "a1")
        self.assertIn("skills", linked[0]["components"])

    def test_assignment_missing_returns_none(self):
        self.assertIsNone(experiment_ledger.assignment("nope"))


class TestOutcomeNudges(_StoreMixin):
    def test_pending_prompts_read_only(self):
        self.write_apps([self.app(
            "a1", submitted_at="2026-08-01T00:00:00+00:00")])
        prompts = outcome_nudges.pending_prompts()
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["application_id"], "a1")

    def test_send_prompts_honors_notify(self):
        self.write_apps([self.app(
            "a1", submitted_at="2026-08-01T00:00:00+00:00")])
        with mock.patch("notify.send",
                        return_value={"channels": ["log"]}) as send:
            result = outcome_nudges.send_prompts()
        self.assertEqual(result["prompts"], 1)
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs["event"], "outcome_stale")
        self.assertEqual(result["notifications"][0]["application_id"], "a1")
        self.assertIn("Acme", kwargs["title"])

    def test_send_prompts_empty_when_fresh(self):
        self.write_apps([self.app(
            "a1", submitted_at="2026-09-12T00:00:00+00:00")])
        with mock.patch("notify.send") as send:
            result = outcome_nudges.send_prompts()
        self.assertEqual(result["prompts"], 0)
        send.assert_not_called()


class TestCaptureContract(_StoreMixin):
    """Verification of the capture team's outcome-min-v0 contract."""

    def test_duplicate_detection(self):
        first = outcomes.record_event(
            application_id="a1", event_type="applied",
            occurred_at="2026-09-01T00:00:00+00:00",
            role="Software Engineer", source="test",
        )
        self.assertTrue(first["appended"])
        # Same identity key: rejected, never double-appended.
        second = outcomes.record_event(
            application_id="a1", event_type="applied",
            occurred_at="2026-09-01T00:00:00+00:00",
            role="Software Engineer", source="test",
        )
        self.assertFalse(second["appended"])
        self.assertEqual(
            second["duplicate_of"], first["event"]["event_id"])
        self.assertEqual(len(outcomes.load_events(
            include_superseded=True)), 1)

    def test_correction_provenance_and_reversibility(self):
        first = outcomes.record_event(
            application_id="a1", event_type="replied",
            occurred_at="2026-09-05T00:00:00+00:00", source="test",
        )
        event_id = first["event"]["event_id"]
        corrected = outcomes.correct_event(
            event_id=event_id,
            corrected_fields={"role": "Senior Software Engineer"},
            actor="tester",
            reason="title was understated",
        )
        corr_event = corrected["event"]
        self.assertEqual(corr_event["role"], "Senior Software Engineer")
        self.assertEqual(corr_event["corrects"], event_id)
        self.assertEqual(
            corr_event["provenance"]["correction_actor"], "tester")
        self.assertEqual(
            corr_event["provenance"]["correction_reason"],
            "title was understated",
        )
        # Identity is immutable: correcting event_type is refused.
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.correct_event(
                event_id=event_id, corrected_fields={"event_type": "screened"},
                actor="tester")
        # Default reads exclude the superseded original; the audit trail
        # keeps both.
        default = outcomes.load_events()
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0]["role"], "Senior Software Engineer")
        full = outcomes.load_events(include_superseded=True)
        self.assertEqual(len(full), 2)

    def test_csv_import_idempotent(self):
        csv_path = Path(self.tmp.name) / "apps.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["job_id", "title", "company", "submitted_at"])
            writer.writerow(["a1", "Engineer", "Acme",
                             "2026-09-01T00:00:00+00:00"])
        first = outcomes.import_csv(csv_path)
        second = outcomes.import_csv(csv_path)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(second["imported"], 0)

    def test_guided_update_requires_confirmation(self):
        self.write_apps([self.app("a1")])
        answers = iter(["1", "interviewed", "n"])
        result = outcomes.guided_update(
            [self.app("a1")],
            input_fn=lambda _: next(answers),
            print_fn=lambda *a: None,
        )
        self.assertIsNone(result)
        self.assertEqual(outcomes.load_events(), [])

    def test_coverage_report_shape(self):
        self.write_apps([self.app("a1")])
        report = outcomes.coverage_report([self.app("a1")])
        self.assertIn("cohort_coverage", report)
        self.assertIn("semantic_coverage", report)


if __name__ == "__main__":
    unittest.main()
