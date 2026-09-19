#!/usr/bin/env python3
"""Tests for outcome-min-v0 event capture (outcomes.py, outcome_migration.py).

Covers: schema validation, append-only storage, duplicate rejection,
reversible corrections, lifecycle stage mirroring, coverage
instrumentation, CSV import, guided updates, and the canonical
migration contract (identity/time preserved, rollback recorded).
"""

from __future__ import annotations

import csv
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lifecycle
import outcome_migration
import outcomes

TS = "2026-09-13T12:00:00+00:00"


def _now_ts() -> str:
    """Current UTC timestamp (for entries that must postdate enablement)."""
    return datetime.now(timezone.utc).isoformat()


def _entry(job_id="acme:1", **over):
    base = {
        "job_id": job_id,
        "board": "greenhouse",
        "title": "Engineer",
        "company": "Acme",
        "submitted_at": TS,
        "stage": "applied",
        "stage_history": [{"stage": "applied", "at": TS, "note": ""}],
    }
    base.update(over)
    return base


class SchemaTest(unittest.TestCase):
    def test_schema_version_constant(self):
        self.assertEqual(outcomes.SCHEMA_VERSION, "outcome-min-v0")

    def test_eleven_canonical_types(self):
        self.assertEqual(len(outcomes.CANONICAL_EVENT_TYPES), 11)
        for t in ("applied", "discovered", "shortlisted", "replied",
                  "screened", "interviewed", "offered", "accepted",
                  "rejected", "withdrawn", "stale"):
            self.assertIn(t, outcomes.CANONICAL_EVENT_TYPES)

    def test_build_event_fields(self):
        ev = outcomes.build_event(
            application_id="acme:1", event_type="applied",
            occurred_at=TS, source="manual", role="Engineer",
            provenance={"actor": "user"},
        )
        for field in ("event_id", "application_id", "event_type",
                      "occurred_at", "source", "role", "provenance",
                      "schema_version"):
            self.assertIn(field, ev)
        self.assertEqual(ev["schema_version"], "outcome-min-v0")
        self.assertEqual(ev["event_type"], "applied")

    def test_build_event_rejects_bad_type(self):
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.build_event(application_id="a", event_type="hired",
                                 source="manual")

    def test_build_event_rejects_missing_app_id(self):
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.build_event(application_id="", event_type="applied",
                                 source="manual")

    def test_build_event_rejects_missing_source(self):
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.build_event(application_id="a", event_type="applied",
                                 source="")

    def test_build_event_rejects_bad_timestamp(self):
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.build_event(application_id="a", event_type="applied",
                                 source="manual", occurred_at="not-a-time")


class StoreTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "outcomes.jsonl"

    def _record(self, **kw):
        kw.setdefault("application_id", "acme:1")
        kw.setdefault("event_type", "applied")
        kw.setdefault("occurred_at", TS)
        kw.setdefault("source", "manual")
        return outcomes.record_event(self.events, **kw)

    def test_record_appends_one_line(self):
        res = self._record()
        self.assertTrue(res["appended"])
        self.assertIsNone(res["duplicate_of"])
        lines = self.events.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        stored = json.loads(lines[0])
        self.assertEqual(stored["application_id"], "acme:1")
        self.assertIn("recorded_at", stored)

    def test_duplicate_rejected_not_appended(self):
        first = self._record()
        second = self._record()
        self.assertFalse(second["appended"])
        self.assertEqual(second["duplicate_of"], first["event"]["event_id"])
        lines = self.events.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_same_app_different_time_is_not_duplicate(self):
        self._record()
        res = self._record(occurred_at="2026-09-14T12:00:00+00:00")
        self.assertTrue(res["appended"])

    def test_identity_key_stable(self):
        ev = outcomes.build_event(
            application_id="acme:1", event_type="applied",
            occurred_at=TS, source="manual",
        )
        ev2 = outcomes.build_event(
            application_id="acme:1", event_type="applied",
            occurred_at=TS, source="manual",
        )
        self.assertEqual(outcomes.identity_key(ev), outcomes.identity_key(ev2))
        ev2["source"] = "other"
        self.assertNotEqual(outcomes.identity_key(ev), outcomes.identity_key(ev2))

    def test_load_tolerates_corrupt_lines(self):
        self._record()
        with open(self.events, "a") as fh:
            fh.write("this is not json\n\n")
        self._record(occurred_at="2026-09-14T12:00:00+00:00")
        self.assertEqual(len(outcomes.load_events(self.events)), 2)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(outcomes.load_events(self.tmp / "nope.jsonl"), [])

    def test_capture_enabled_marker_written(self):
        self.assertIsNone(outcomes.capture_enabled_at(self.events))
        self._record()
        enabled = outcomes.capture_enabled_at(self.events)
        self.assertIsNotNone(enabled)

    def test_record_application_event(self):
        res = outcomes.record_application_event(
            _entry(), "server:apply_to_job", self.events
        )
        self.assertTrue(res["appended"])
        ev = res["event"]
        self.assertEqual(ev["event_type"], "applied")
        self.assertEqual(ev["occurred_at"], TS)
        self.assertEqual(ev["role"], "Engineer")
        self.assertEqual(ev["provenance"]["board"], "greenhouse")

    def test_record_application_event_surfaces_submitted_flag(self):
        res = outcomes.record_application_event(
            _entry(submitted=False), "server:browser_apply", self.events
        )
        self.assertTrue(res["appended"])
        self.assertFalse(res["event"]["provenance"]["submitted"])
        res2 = outcomes.record_application_event(
            _entry(), "server:apply_to_job", self.events
        )
        self.assertNotIn("submitted", res2["event"]["provenance"])

    def test_timeline_order_and_filter(self):
        self._record(application_id="a", occurred_at="2026-09-13T12:00:00+00:00")
        self._record(application_id="b", occurred_at="2026-09-14T12:00:00+00:00")
        tl = outcomes.timeline(self.events)
        self.assertEqual([e["application_id"] for e in tl], ["b", "a"])
        self.assertEqual(len(outcomes.timeline(self.events, application_id="a")), 1)
        self.assertEqual(
            len(outcomes.events_for_application("a", self.events)), 1
        )


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "outcomes.jsonl"
        self.first = outcomes.record_event(
            self.events, application_id="acme:1", event_type="applied",
            occurred_at=TS, source="manual", role="Enginer",
        )["event"]

    def test_correction_appends_with_pointer(self):
        res = outcomes.correct_event(
            self.events, event_id=self.first["event_id"],
            corrected_fields={"role": "Engineer"},
            actor="operator", reason="typo",
        )
        self.assertTrue(res["appended"])
        ev = res["event"]
        self.assertEqual(ev["corrects"], self.first["event_id"])
        self.assertEqual(ev["role"], "Engineer")
        self.assertEqual(ev["provenance"]["correction_actor"], "operator")
        self.assertEqual(ev["provenance"]["correction_reason"], "typo")
        # original never edited: still on disk with the typo
        raw = outcomes.load_events(self.events, include_superseded=True)
        original = next(e for e in raw if e["event_id"] == self.first["event_id"])
        self.assertEqual(original["role"], "Enginer")

    def test_superseded_excluded_by_default(self):
        outcomes.correct_event(
            self.events, event_id=self.first["event_id"],
            corrected_fields={"role": "Engineer"},
        )
        visible = outcomes.load_events(self.events)
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["role"], "Engineer")
        full = outcomes.load_events(self.events, include_superseded=True)
        self.assertEqual(len(full), 2)

    def test_correction_rejects_identity_fields(self):
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.correct_event(
                self.events, event_id=self.first["event_id"],
                corrected_fields={"event_type": "rejected"},
            )

    def test_correction_unknown_id(self):
        with self.assertRaises(outcomes.OutcomeValidationError):
            outcomes.correct_event(
                self.events, event_id="nope", corrected_fields={"role": "x"}
            )


class LifecycleMirrorTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "applications.json"
        lifecycle.save_entries(self.store, [_entry()])

    def test_update_stage_emits_canonical_event(self):
        lifecycle.update_stage(self.store, "acme:1", "interviewing",
                               note="phone screen")
        events = outcomes.load_events(outcomes.default_events_path(self.store))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "interviewed")
        self.assertEqual(events[0]["application_id"], "acme:1")
        self.assertEqual(events[0]["source"], "lifecycle:update_stage")

    def test_stage_mapping(self):
        expected = {
            "applied": "applied", "interviewing": "interviewed",
            "offer": "offered", "rejected": "rejected",
            "withdrawn": "withdrawn", "ghosted": "stale",
        }
        for stage, event_type in expected.items():
            self.assertEqual(outcomes.STAGE_TO_EVENT[stage], event_type)

    def test_update_stage_still_works_when_capture_writes(self):
        updated = lifecycle.update_stage(self.store, "acme:1", "rejected")
        self.assertEqual(updated["stage"], "rejected")


class CoverageTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "applications.json"
        self.events = outcomes.default_events_path(self.store)
        # Write the capture-enablement marker first so entries stamped
        # with "now" below count as post-enablement.
        outcomes.record_event(
            self.events, application_id="dummy:0", event_type="applied",
            occurred_at="2026-09-13T12:00:00+00:00", source="test-setup",
        )

    def test_full_coverage_after_capture(self):
        now = _now_ts()
        apps = [_entry("acme:1", submitted_at=now,
                       stage_history=[{"stage": "applied", "at": now, "note": ""}]),
                _entry("acme:2", submitted_at=now,
                       stage_history=[{"stage": "applied", "at": now, "note": ""}])]
        lifecycle.save_entries(self.store, apps)
        for app in apps:
            outcomes.record_application_event(app, "server:apply_to_job",
                                              self.events)
        report = outcomes.coverage_report(
            lifecycle.load_entries(self.store), self.events
        )
        self.assertEqual(report["cohort_coverage"], 1.0)
        self.assertEqual(report["state_change_coverage"], 1.0)
        self.assertFalse(report["below_gate"])

    def test_missing_event_trips_gate(self):
        now = _now_ts()
        apps = [_entry("acme:1", submitted_at=now,
                       stage_history=[{"stage": "applied", "at": now, "note": ""}]),
                _entry("acme:2", submitted_at=now,
                       stage_history=[{"stage": "applied", "at": now, "note": ""}])]
        lifecycle.save_entries(self.store, apps)
        outcomes.record_application_event(apps[0], "server:apply_to_job",
                                          self.events)
        report = outcomes.coverage_report(
            lifecycle.load_entries(self.store), self.events
        )
        self.assertEqual(report["cohort_coverage"], 0.5)
        self.assertEqual(report["state_change_coverage"], 0.5)
        self.assertTrue(report["below_gate"])

    def test_manual_stage_update_keeps_state_change_coverage(self):
        now = _now_ts()
        app = _entry("acme:1", submitted_at=now,
                     stage_history=[{"stage": "applied", "at": now, "note": ""}])
        lifecycle.save_entries(self.store, [app])
        outcomes.record_application_event(app, "server:apply_to_job",
                                          self.events)
        lifecycle.update_stage(self.store, "acme:1", "interviewing")
        report = outcomes.coverage_report(
            lifecycle.load_entries(self.store), self.events
        )
        self.assertEqual(report["state_change_coverage"], 1.0)
        self.assertFalse(report["below_gate"])

    def test_semantic_coverage_counts_types(self):
        lifecycle.save_entries(self.store, [_entry("acme:1")])
        outcomes.record_event(self.events, application_id="acme:1",
                              event_type="applied", occurred_at=TS,
                              source="manual")
        outcomes.record_event(self.events, application_id="acme:1",
                              event_type="rejected",
                              occurred_at="2026-09-14T12:00:00+00:00",
                              source="manual")
        report = outcomes.coverage_report(
            lifecycle.load_entries(self.store), self.events
        )
        self.assertAlmostEqual(report["semantic_coverage"], 2 / 11, places=4)

    def test_insufficient_data_flagged(self):
        report = outcomes.coverage_report([], self.events)
        self.assertTrue(report["insufficient_data"])
        # ...but counts are still reported alongside the flag
        self.assertIn("state_changes_observed", report)

    def test_pre_enablement_history_excluded_from_gate(self):
        old_ts = "2026-01-01T12:00:00+00:00"
        now = _now_ts()
        old_app = _entry("acme:1", submitted_at=old_ts,
                         stage_history=[{"stage": "applied", "at": old_ts,
                                         "note": ""}])
        new_app = _entry("acme:2", submitted_at=now,
                         stage_history=[{"stage": "applied", "at": now,
                                         "note": ""}])
        lifecycle.save_entries(self.store, [old_app, new_app])
        # Enable capture now with the new app; the old app's history predates it.
        outcomes.record_application_event(new_app, "s", self.events)
        report = outcomes.coverage_report(
            lifecycle.load_entries(self.store), self.events
        )
        self.assertEqual(report["state_changes_observed"], 1)
        self.assertEqual(report["state_change_coverage"], 1.0)


class PromptsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "outcomes.jsonl"

    def _old_app(self, job_id, days_ago, stage="applied"):
        at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return _entry(job_id, submitted_at=at, stage=stage,
                      stage_history=[{"stage": "applied", "at": at, "note": ""}])

    def test_stale_app_prompted(self):
        prompts = outcomes.missing_outcome_prompts(
            [self._old_app("acme:1", 30)], self.events, stale_days=14
        )
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["application_id"], "acme:1")
        self.assertEqual(prompts[0]["days_since_last_event"], 30)
        self.assertIn("rejected", prompts[0]["suggested_types"])

    def test_fresh_app_not_prompted(self):
        prompts = outcomes.missing_outcome_prompts(
            [self._old_app("acme:1", 3)], self.events, stale_days=14
        )
        self.assertEqual(prompts, [])

    def test_terminal_outcome_not_prompted(self):
        app = self._old_app("acme:1", 60)
        outcomes.record_event(self.events, application_id="acme:1",
                              event_type="rejected",
                              occurred_at=app["submitted_at"], source="manual")
        prompts = outcomes.missing_outcome_prompts([app], self.events,
                                                   stale_days=14)
        self.assertEqual(prompts, [])

    def test_recent_event_resets_staleness(self):
        app = self._old_app("acme:1", 60)
        outcomes.record_event(
            self.events, application_id="acme:1", event_type="interviewed",
            occurred_at=datetime.now(timezone.utc).isoformat(), source="manual")
        prompts = outcomes.missing_outcome_prompts([app], self.events,
                                                   stale_days=14)
        self.assertEqual(prompts, [])


class CsvImportTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "outcomes.jsonl"
        self.csv_path = self.tmp / "apps.csv"
        with open(self.csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(outcomes.CSV_IMPORT_COLUMNS))
            writer.writeheader()
            writer.writerow({"job_id": "acme:1", "title": "Engineer",
                             "company": "Acme", "board": "greenhouse",
                             "submitted_at": TS, "stage": "applied"})
            writer.writerow({"job_id": "", "title": "Nope"})  # error row

    def test_import_and_idempotent_rerun(self):
        first = outcomes.import_csv(self.csv_path, self.events)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(len(first["errors"]), 1)
        second = outcomes.import_csv(self.csv_path, self.events)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(outcomes.load_events(self.events)), 1)


class GuidedUpdateTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "outcomes.jsonl"
        self.apps = [_entry("acme:1")]

    def _run(self, answers):
        answers = list(answers)
        printed = []
        result = outcomes.guided_update(
            self.apps, self.events,
            input_fn=lambda prompt: answers.pop(0),
            print_fn=printed.append,
        )
        return result, printed

    def test_guided_records_with_confirmation(self):
        idx = outcomes.CANONICAL_EVENT_TYPES.index("interviewed")
        result, _ = self._run(["0", str(idx), "now", "phone screen", "y"])
        self.assertIsNotNone(result)
        self.assertTrue(result["appended"])
        self.assertEqual(result["event"]["event_type"], "interviewed")
        self.assertEqual(result["event"]["source"], "manual")

    def test_guided_cancel_on_no_confirm(self):
        idx = outcomes.CANONICAL_EVENT_TYPES.index("rejected")
        result, _ = self._run(["0", str(idx), "now", "", "n"])
        self.assertIsNone(result)
        self.assertEqual(outcomes.load_events(self.events), [])

    def test_guided_invalid_selection_cancels(self):
        result, _ = self._run(["99"])
        self.assertIsNone(result)

    def test_guided_no_applications(self):
        printed = []
        result = outcomes.guided_update(
            [], self.events, input_fn=lambda p: "0", print_fn=printed.append
        )
        self.assertIsNone(result)


class CliHandlerTest(unittest.TestCase):
    """_cmd_outcomes subcommands against temp stores."""

    def setUp(self):
        import argparse
        import contextlib
        import io
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "applications.json"
        self.events = self.tmp / "outcomes.jsonl"
        lifecycle.save_entries(self.store, [_entry("acme:1")])
        self._contextlib = contextlib
        self._io = io
        parser = argparse.ArgumentParser()
        outcomes._add_outcomes_arguments(parser)
        self._parser = parser

    def _run(self, argv):
        args = self._parser.parse_args(argv)
        args.applications = str(self.store)
        args.events = str(self.events)
        buf = self._io.StringIO()
        with self._contextlib.redirect_stdout(buf):
            code = outcomes._cmd_outcomes(args)
        return code, buf.getvalue()

    def test_record_then_duplicate(self):
        code, out = self._run(["record", "--application-id", "acme:1",
                               "--type", "applied", "--source", "manual"])
        self.assertEqual(code, 0)
        self.assertIn("Recorded event", out)
        code2, out2 = self._run(["record", "--application-id", "acme:1",
                                 "--type", "applied", "--source", "manual",
                                 "--occurred-at",
                                 outcomes.load_events(self.events)[0]["occurred_at"]])
        self.assertEqual(code2, 0)
        self.assertIn("Duplicate", out2)
        self.assertEqual(len(outcomes.load_events(self.events)), 1)

    def test_record_rejects_bad_type_at_parse(self):
        with self.assertRaises(SystemExit):
            self._parser.parse_args(["record", "--application-id", "a",
                                     "--type", "hired"])

    def test_record_missing_application_id_fails(self):
        code, _ = self._run(["record", "--application-id", "",
                             "--type", "applied"])
        self.assertEqual(code, 1)

    def test_timeline_lists_events(self):
        self._run(["record", "--application-id", "acme:1",
                   "--type", "interviewed"])
        code, out = self._run(["timeline"])
        self.assertEqual(code, 0)
        self.assertIn("interviewed", out)
        self.assertIn("acme:1", out)

    def test_coverage_reports_gate(self):
        code, out = self._run(["coverage"])
        self.assertEqual(code, 0)
        self.assertIn("Q4 gate", out)
        self.assertIn("insufficient data", out)  # no applied event yet

    def test_prompts_lists_stale(self):
        code, out = self._run(["prompts", "--stale-days", "0"])
        self.assertEqual(code, 0)
        self.assertIn("acme:1", out)

    def test_import_csv(self):
        csv_path = self.tmp / "apps.csv"
        with open(csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=list(outcomes.CSV_IMPORT_COLUMNS))
            writer.writeheader()
            writer.writerow({"job_id": "csv:1", "title": "T",
                             "submitted_at": TS})
        code, out = self._run(["import-csv", str(csv_path)])
        self.assertEqual(code, 0)
        self.assertIn("Imported 1 events", out)
        self.assertEqual(len(outcomes.load_events(self.events)), 1)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.events = self.tmp / "outcomes.jsonl"
        self.canonical = self.tmp / "outcomes_canonical.jsonl"
        self.mlog = self.tmp / "outcomes_migration.jsonl"
        self.ev1 = outcomes.record_event(
            self.events, application_id="acme:1", event_type="applied",
            occurred_at=TS, source="server:apply_to_job", role="Engineer",
        )["event"]
        self.ev2 = outcomes.record_event(
            self.events, application_id="acme:2", event_type="interviewed",
            occurred_at="2026-09-14T12:00:00+00:00", source="manual",
        )["event"]

    def _migrate(self, **kw):
        return outcome_migration.migrate_to_canonical(
            self.events, self.canonical, self.mlog, **kw
        )

    def test_migration_preserves_identity_and_time(self):
        rec = self._migrate(actor="tester")
        self.assertEqual(rec["event_count"], 2)
        self.assertEqual(rec["schema_from"], "outcome-min-v0")
        self.assertEqual(rec["schema_to"], "outcome-v1")
        canonical = outcome_migration.load_canonical_events(
            self.canonical, self.mlog
        )
        self.assertEqual(len(canonical), 2)
        by_id = {e["event_id"]: e for e in canonical}
        for original in (self.ev1, self.ev2):
            migrated = by_id[original["event_id"]]
            self.assertEqual(migrated["occurred_at"], original["occurred_at"])
            self.assertEqual(migrated["migrated_from"], original["event_id"])
            self.assertEqual(migrated["migration_id"], rec["migration_id"])
            self.assertEqual(migrated["schema_version"], "outcome-v1")

    def test_min_store_untouched(self):
        before = self.events.read_text()
        self._migrate()
        self.assertEqual(self.events.read_text(), before)

    def test_verify_migration_ok(self):
        rec = self._migrate()
        verdict = outcome_migration.verify_migration(
            rec["migration_id"], self.events, self.canonical, self.mlog
        )
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["min_events"], 2)

    def test_migrate_twice_raises(self):
        self._migrate()
        with self.assertRaises(outcome_migration.MigrationError):
            self._migrate()

    def test_rollback_excludes_and_allows_rerun(self):
        rec = self._migrate()
        rb = outcome_migration.rollback_migration(
            rec["migration_id"], self.mlog, actor="tester", reason="bad batch"
        )
        self.assertEqual(rb["record_type"], "migration_rollback")
        self.assertEqual(
            outcome_migration.load_canonical_events(self.canonical, self.mlog),
            [],
        )
        # Full audit trail still shows them:
        self.assertEqual(
            len(outcome_migration.load_canonical_events(
                self.canonical, self.mlog, include_rolled_back=True)),
            2,
        )
        # Re-running after rollback works (min store untouched):
        rec2 = self._migrate()
        self.assertNotEqual(rec2["migration_id"], rec["migration_id"])
        self.assertEqual(
            len(outcome_migration.load_canonical_events(
                self.canonical, self.mlog)),
            2,
        )

    def test_rollback_unknown_or_double_raises(self):
        with self.assertRaises(outcome_migration.MigrationError):
            outcome_migration.rollback_migration("nope", self.mlog)
        rec = self._migrate()
        outcome_migration.rollback_migration(rec["migration_id"], self.mlog)
        with self.assertRaises(outcome_migration.MigrationError):
            outcome_migration.rollback_migration(rec["migration_id"], self.mlog)

    def test_migration_no_events_raises(self):
        empty = self.tmp / "empty.jsonl"
        with self.assertRaises(outcome_migration.MigrationError):
            outcome_migration.migrate_to_canonical(
                empty, self.canonical, self.mlog
            )


class AnalyticsQueryTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.store = self.tmp / "applications.json"
        self.events = outcomes.default_events_path(self.store)
        # Enablement marker first, so the entry below is post-enablement.
        outcomes.record_event(
            self.events, application_id="dummy:0", event_type="applied",
            occurred_at="2026-09-13T12:00:00+00:00", source="test-setup",
        )
        now = _now_ts()
        app = _entry("acme:1", submitted_at=now,
                     stage_history=[{"stage": "applied", "at": now, "note": ""}])
        lifecycle.save_entries(self.store, [app])
        outcomes.record_application_event(app, "server:apply_to_job",
                                          self.events)

    def test_outcome_coverage_query(self):
        import analytics
        report = analytics.outcome_coverage(
            analytics.load_applications(self.store), self.events
        )
        self.assertEqual(report["cohort_coverage"], 1.0)

    def test_outcome_timeline_query(self):
        import analytics
        tl = analytics.outcome_timeline(self.events, limit=5)
        app_events = [e for e in tl if e["application_id"] == "acme:1"]
        self.assertEqual(len(app_events), 1)
        self.assertEqual(app_events[0]["event_type"], "applied")

    def test_outcome_prompts_query(self):
        import analytics
        prompts = analytics.outcome_prompts(
            analytics.load_applications(self.store), self.events, stale_days=0
        )
        self.assertEqual(len(prompts), 1)

    def test_generate_report_includes_event_sections(self):
        import analytics
        report = analytics.generate_report(path=self.store)
        self.assertIn("event_coverage", report)
        self.assertIn("recent_outcome_events", report)
        self.assertEqual(report["event_coverage"]["cohort_coverage"], 1.0)


class McpToolsTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self._env_patch = None
        # Point the MCP tools' default store at a temp file.
        import unittest.mock as mock

        self._env_patch = mock.patch.dict(
            os.environ,
            {"VETO_OUTCOMES_FILE": str(self.tmp / "outcomes.jsonl")},
        )
        self._env_patch.start()
        collected = {}

        class FakeMcp:
            def tool(self):
                def deco(fn):
                    collected[fn.__name__] = fn
                    return fn

                return deco

        self.tools = outcomes.register_tools(FakeMcp())
        self.assertEqual(
            set(self.tools),
            {"outcome_record_event", "outcome_coverage",
             "outcome_prompts", "outcome_timeline"},
        )

    def tearDown(self):
        if self._env_patch:
            self._env_patch.stop()

    def test_record_event_tool_success_and_error(self):
        ok = self.tools["outcome_record_event"](
            application_id="acme:1", event_type="applied", source="manual"
        )
        self.assertTrue(ok["appended"])
        self.assertIsNone(ok["error"])
        bad = self.tools["outcome_record_event"](
            application_id="acme:1", event_type="hired", source="manual"
        )
        self.assertFalse(bad["appended"])
        self.assertIsNotNone(bad["error"])


if __name__ == "__main__":
    unittest.main()
