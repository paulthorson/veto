"""Initiative 10 — migration framework tests (synthetic fixtures)."""

import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i10 import schema_migrate


def _data_dir_with_legacy_profile(tmp: str) -> Path:
    d = Path(tmp)
    (d / "profiles").mkdir()
    # Legacy wizard-written profile: no schema_version marker.
    (d / "profiles" / "profile.json").write_text(
        json.dumps({"full_name": "Test User"}), encoding="utf-8"
    )
    return d


class TestMigrationLifecycle(unittest.TestCase):
    def test_discover_finds_migrations_in_order(self):
        migs = schema_migrate.discover_migrations()
        self.assertTrue(migs)
        ids = [m.migration_id for m in migs]
        self.assertEqual(ids, sorted(ids))

    def test_migrate_applies_and_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir_with_legacy_profile(tmp)
            runner = schema_migrate.MigrationRunner(d)
            self.assertEqual(runner.status()["pending"], ["0001_stamp_schema_versions"])
            results = runner.migrate()
            self.assertTrue(all(r["applied"] for r in results))
            # Journal recorded.
            journal = schema_migrate.journal_records(d)
            self.assertEqual(journal[0]["action"], "applied")
            # Profile now stamped.
            profile = json.loads((d / "profiles" / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(profile["schema_version"], "profile-v1")
            # check_all (compatibility suite) passes.
            for r in runner.check_all():
                self.assertTrue(r["ok"], r)

    def test_migrate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir_with_legacy_profile(tmp)
            runner = schema_migrate.MigrationRunner(d)
            runner.migrate()
            self.assertEqual(runner.migrate(), [])

    def test_rollback_appends_record_and_allows_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir_with_legacy_profile(tmp)
            runner = schema_migrate.MigrationRunner(d)
            runner.migrate()
            result = runner.rollback("0001_stamp_schema_versions")
            self.assertTrue(result["rolled_back"])
            journal = schema_migrate.journal_records(d)
            actions = [r["action"] for r in journal]
            self.assertEqual(actions, ["applied", "rolled_back"])  # history preserved
            self.assertEqual(runner.status()["applied"], [])
            # Re-run works after rollback.
            runner.migrate()
            self.assertEqual(runner.status()["applied"], ["0001_stamp_schema_versions"])

    def test_rollback_unknown_migration_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = schema_migrate.MigrationRunner(Path(tmp))
            with self.assertRaises(schema_migrate.MigrationError):
                runner.rollback("9999_nope")

    def test_rollback_unapplied_migration_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = schema_migrate.MigrationRunner(Path(tmp))
            with self.assertRaises(schema_migrate.MigrationError):
                runner.rollback("0001_stamp_schema_versions")

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir_with_legacy_profile(tmp)
            runner = schema_migrate.MigrationRunner(d)
            runner.migrate(dry_run=True)
            self.assertFalse((d / "_migrations.jsonl").exists())
            self.assertEqual(runner.status()["applied"], [])


if __name__ == "__main__":
    unittest.main()
