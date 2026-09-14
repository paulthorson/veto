"""Initiative 10 — schema registry tests (synthetic fixtures)."""

import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i10 import schemas


def _event(**over):
    base = {
        "event_id": "abc123",
        "application_id": "job-1",
        "event_type": "applied",
        "occurred_at": "2026-09-01T10:00:00",
        "source": "test",
        "role": "Engineer",
        "provenance": {"actor": "user"},
        "schema_version": "outcome-min-v0",
    }
    base.update(over)
    return base


class TestOutcomeEventValidator(unittest.TestCase):
    def test_valid_event_passes(self):
        self.assertEqual(schemas.validate_outcome_event(_event()), [])

    def test_bad_event_type_rejected(self):
        problems = schemas.validate_outcome_event(_event(event_type="hired"))
        self.assertTrue(any("event_type" in p for p in problems))

    def test_wrong_schema_version_rejected(self):
        problems = schemas.validate_outcome_event(_event(schema_version="v9"))
        self.assertTrue(any("schema_version" in p for p in problems))

    def test_missing_field_rejected(self):
        e = _event()
        del e["source"]
        self.assertTrue(schemas.validate_outcome_event(e))

    def test_all_eleven_canonical_types_valid(self):
        for t in ("discovered shortlisted applied replied screened interviewed "
                  "offered accepted rejected withdrawn stale").split():
            self.assertEqual(schemas.validate_outcome_event(_event(event_type=t)), [], t)


class TestRegistry(unittest.TestCase):
    def test_only_named_stable_schemas(self):
        self.assertEqual(
            sorted(schemas.SCHEMA_REGISTRY), ["evidence-store", "outcome-event", "profile"]
        )

    def test_unknown_schema_raises(self):
        with self.assertRaises(KeyError):
            schemas.get_schema("resume-pdf")

    def test_outcome_event_is_published_contract(self):
        s = schemas.get_schema("outcome-event")
        self.assertEqual(s.status, "published")
        self.assertEqual(s.version, "outcome-min-v0")

    def test_profile_and_evidence_are_i10_defined(self):
        # Assumption flag: 01's contracts not yet published; i10 defines
        # the required interface and integrates when they land.
        self.assertEqual(schemas.get_schema("profile").status, "i10-defined")
        self.assertEqual(schemas.get_schema("evidence-store").status, "i10-defined")


class TestCheckDataDir(unittest.TestCase):
    def test_check_file_reports_invalid_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "outcomes.jsonl"
            p.write_text(
                json.dumps(_event()) + "\n"
                + json.dumps(_event(event_id="x2", event_type="bogus")) + "\n"
                + "not json\n",
                encoding="utf-8",
            )
            r = schemas.check_file("outcome-event", p)
            self.assertFalse(r.ok)
            self.assertEqual(r.checked, 3)
            self.assertEqual(r.invalid, 2)

    def test_check_data_dir_skips_absent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(schemas.check_data_dir(tmp), [])


if __name__ == "__main__":
    unittest.main()
