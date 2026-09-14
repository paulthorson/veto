#!/usr/bin/env python3
"""Initiative 12 / Epic 3 — transparent changelog tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i12 import changelog


class ChangelogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entries = Path(self.tmp.name) / "entries.json"
        self._patch = mock.patch.object(changelog, "ENTRIES_FILE", self.entries)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_load_missing_file_is_pure_read(self):
        # Reads never write: no seeding side effect.
        self.assertEqual(changelog.load_entries(), [])
        self.assertFalse(self.entries.exists())

    def test_seed_writes_defaults_explicitly(self):
        self.assertTrue(changelog.seed_entries())
        entries = changelog.load_entries()
        cats = {e["category"] for e in entries}
        self.assertIn("shipped", cats)
        self.assertIn("rejected", cats)
        self.assertIn("limited", cats)
        # Second seed is a no-op without --force.
        self.assertFalse(changelog.seed_entries())

    def test_add_entry(self):
        e = changelog.add_entry("2026-09-14", "rejected", "X", "because " * 5,
                                "none", "initiatives/i12/tools.py")
        self.assertEqual(e["category"], "rejected")
        self.assertIn(e, changelog.load_entries())

    def test_bad_category_rejected(self):
        with self.assertRaises(ValueError):
            changelog.add_entry("2026-09-14", "maybe", "X", "why", "lim",
                                "src")

    def test_missing_keys_rejected(self):
        self.entries.write_text(json.dumps([{"category": "shipped"}]))
        with self.assertRaises(ValueError):
            changelog.load_entries()

    def test_render_has_all_sections(self):
        changelog.seed_entries()  # explicit init; reads never seed
        md = changelog.render_markdown()
        self.assertIn("## Shipped", md)
        self.assertIn("## Rejected", md)
        self.assertIn("## Limited", md)
        self.assertIn("**Why:**", md)
        self.assertIn("**Limitation:**", md)

    def test_health_flags_missing_sections(self):
        self.entries.write_text(json.dumps([
            {"date": "2026-09-13", "category": "shipped", "title": "T",
             "why": "W", "limitation": "L", "source": "S"},
        ]))
        h = changelog.health()
        self.assertTrue(h["missing_rejections"])
        self.assertTrue(h["missing_limitations"])
        md = changelog.render_markdown()
        self.assertIn("transparency defect", md)

    def test_health_clean_with_all_sections(self):
        changelog.seed_entries()  # seeds defaults which cover all categories
        h = changelog.health()
        self.assertFalse(h["missing_rejections"])
        self.assertFalse(h["missing_limitations"])


if __name__ == "__main__":
    unittest.main()
