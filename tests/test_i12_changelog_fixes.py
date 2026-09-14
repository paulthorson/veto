#!/usr/bin/env python3
"""Initiative 12 / WS3 fixer tests — blind-review must-fix findings.

Proves: load_entries() never writes/seeds; seeding is explicit and
idempotent; add_entry validates (non-empty why/limitation/source,
YYYY-MM-DD date, duplicate title+date rejected); corrupt JSON raises a
clear error; writes are atomic (temp-then-rename); shipped entries with
not-launched language are flagged; seed entries carry verifiable sources;
git ref input is sanitized; the minimal CLI works.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from initiatives.i12 import changelog


def _entry(**kw):
    d = {"date": "2026-09-14", "category": "limited", "title": "T",
         "why": "W", "limitation": "L", "source": "S"}
    d.update(kw)
    return d


class ReadPathPurityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entries = Path(self.tmp.name) / "entries.json"
        self._patch = mock.patch.object(changelog, "ENTRIES_FILE", self.entries)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_load_never_seeds_or_writes(self):
        self.assertEqual(changelog.load_entries(), [])
        self.assertFalse(self.entries.exists())

    def test_seed_is_explicit_and_idempotent(self):
        self.assertTrue(changelog.seed_entries())
        self.assertTrue(self.entries.exists())
        first = self.entries.read_text(encoding="utf-8")
        self.assertFalse(changelog.seed_entries())  # no-op second time
        self.assertEqual(self.entries.read_text(encoding="utf-8"), first)
        self.assertTrue(changelog.seed_entries(force=True))  # force rewrites

    def test_seed_entries_carry_verifiable_sources(self):
        changelog.seed_entries()
        for e in changelog.load_entries():
            self.assertTrue(e["source"].strip(), e["title"])
            # No invented references: sources point at repo paths we can check.
            self.assertNotIn("http", e["source"])

    def test_no_shipped_entry_contradicts_its_category(self):
        changelog.seed_entries()
        self.assertEqual(changelog.consistency_warnings(), [])

    def test_render_cites_source(self):
        changelog.seed_entries()
        md = changelog.render_markdown()
        self.assertIn("**Source:**", md)


class AddEntryValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entries = Path(self.tmp.name) / "entries.json"
        self._patch = mock.patch.object(changelog, "ENTRIES_FILE", self.entries)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _add(self, **kw):
        e = _entry(**kw)
        return changelog.add_entry(e["date"], e["category"], e["title"],
                                   e["why"], e["limitation"], e["source"])

    def test_rejects_empty_why_limitation_source(self):
        for field in ("why", "limitation", "source"):
            with self.assertRaises(ValueError, msg=field):
                self._add(**{field: "   "})

    def test_rejects_bad_date_format(self):
        for bad in ("14-09-2026", "2026/09/14", "2026-13-01", "2026-02-30",
                    "not-a-date", ""):
            with self.assertRaises(ValueError, msg=bad):
                self._add(date=bad)

    def test_rejects_duplicate_title_and_date(self):
        self._add(title="Same title", date="2026-09-14")
        with self.assertRaises(ValueError):
            self._add(title="Same title", date="2026-09-14")
        # Same title on a different date is fine.
        self._add(title="Same title", date="2026-09-15")

    def test_add_seeds_first_on_write_path(self):
        # Write path may seed; read path may not.
        self._add()
        titles = {e["title"] for e in changelog.load_entries()}
        self.assertGreater(len(titles), 1)  # seeds + the new entry

    def test_corrupt_json_raises_clear_error(self):
        self.entries.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            changelog.load_entries()
        self.assertIn("not valid JSON", str(cm.exception))
        self.assertIn(str(self.entries), str(cm.exception))

    def test_missing_required_field_rejected(self):
        e = _entry()
        del e["source"]
        self.entries.write_text(json.dumps([e]), encoding="utf-8")
        with self.assertRaises(ValueError):
            changelog.load_entries()


class AtomicWriteTest(unittest.TestCase):
    def test_no_tmp_file_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = Path(tmp) / "entries.json"
            with mock.patch.object(changelog, "ENTRIES_FILE", entries):
                changelog.seed_entries()
                changelog.add_entry("2026-09-15", "shipped", "T2", "W", "L",
                                    "S")
            leftovers = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(leftovers, [])
            self.assertTrue(entries.exists())
            # File parses cleanly after the rename.
            json.loads(entries.read_text(encoding="utf-8"))


class ConsistencyCheckTest(unittest.TestCase):
    def test_shipped_with_not_launched_language_warns(self):
        entries = [_entry(category="shipped", title="Launched thing",
                          limitation="NOT launched yet, sorry.")]
        warnings = changelog.consistency_warnings(entries)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Launched thing", warnings[0])

    def test_shipped_without_not_launched_language_is_quiet(self):
        entries = [_entry(category="shipped", title="Real launch",
                          limitation="Only the CLI ships; web UI next.")]
        self.assertEqual(changelog.consistency_warnings(entries), [])

    def test_health_surfaces_consistency_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = Path(tmp) / "entries.json"
            entries.write_text(json.dumps(
                [_entry(category="shipped", title="X",
                        limitation="not public yet")]), encoding="utf-8")
            with mock.patch.object(changelog, "ENTRIES_FILE", entries):
                h = changelog.health()
        self.assertEqual(len(h["consistency_warnings"]), 1)


class GitRefSanitizationTest(unittest.TestCase):
    def test_unsafe_refs_rejected(self):
        for bad in ("-n", "--all", "a;b", "a b", "$(x)", "a|b", ""):
            if bad == "":
                self.assertEqual(changelog._sanitize_ref(bad), "")
            else:
                with self.assertRaises(ValueError, msg=bad):
                    changelog._sanitize_ref(bad)

    def test_safe_refs_pass(self):
        self.assertEqual(changelog._sanitize_ref("v1.0"), "v1.0")
        self.assertEqual(changelog._sanitize_ref("HEAD~3"), "HEAD~3")


class ChangelogCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entries = Path(self.tmp.name) / "entries.json"
        self._patch = mock.patch.object(changelog, "ENTRIES_FILE", self.entries)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = changelog.main(argv)
        return rc, buf.getvalue()

    def test_cli_seed_then_render(self):
        rc, out = self._run(["seed"])
        self.assertEqual(rc, 0)
        self.assertIn("seeded", out)
        rc, out = self._run(["render"])
        self.assertEqual(rc, 0)
        self.assertIn("# Veto — Transparent changelog", out)
        self.assertIn("**Source:**", out)

    def test_cli_add_entry(self):
        self._run(["seed"])
        rc, out = self._run(["add-entry", "--date", "2026-09-15",
                             "--category", "limited", "--title", "CLI test",
                             "--why", "testing", "--limitation", "none",
                             "--source", "tests/"])
        self.assertEqual(rc, 0)
        self.assertIn("entry added", out)
        self.assertIn("CLI test", self._run(["render"])[1])

    def test_cli_health(self):
        self._run(["seed"])
        rc, out = self._run(["health"])
        self.assertEqual(rc, 0)
        h = json.loads(out)
        self.assertIn("by_category", h)
        self.assertIn("consistency_warnings", h)

    def test_cli_help_clean(self):
        for cmd in ("render", "add-entry", "seed", "health"):
            with self.assertRaises(SystemExit) as cm:
                changelog.main([cmd, "--help"])
            self.assertEqual(cm.exception.code, 0, cmd)


if __name__ == "__main__":
    unittest.main()
