#!/usr/bin/env python3
"""Tests for lifecycle, watches, multi-profile, and CSV export.

Stdlib unittest only. All file I/O goes to temporary directories — the
real ``applications.json`` / ``watches.json`` / ``profiles/`` are never
touched.

Run:  cd ~/workspace/job-apply-mcp && .venv/bin/python -m unittest discover -s tests -v
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import lifecycle
import profiles
import watch


def _sample_entry(**overrides):
    entry = {
        "job_id": "linkedin:abc123",
        "board": "linkedin",
        "title": "Software Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "apply_url": "https://example.com/job/1",
        "resume_path": "/tmp/resume.pdf",
        "status": "confirmed",
        "submitted_at": "2026-09-01T12:00:00+00:00",
    }
    entry.update(overrides)
    return entry


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "applications.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, entries):
        self.store.write_text(json.dumps(entries), encoding="utf-8")

    def test_backfill_legacy_entry_on_read(self):
        """Entries without stage get stage='applied' in memory only."""
        self._write([_sample_entry()])
        loaded = lifecycle.load_entries(self.store)
        self.assertEqual(loaded[0]["stage"], "applied")
        self.assertEqual(loaded[0]["stage_history"][0]["stage"], "applied")
        self.assertIsNone(loaded[0]["follow_up_due"])
        # file itself untouched (no stage key written back)
        raw = json.loads(self.store.read_text(encoding="utf-8"))
        self.assertNotIn("stage", raw[0])

    def test_update_stage_appends_history(self):
        self._write([_sample_entry()])
        updated = lifecycle.update_stage(self.store, "linkedin:abc123", "interviewing", note="phone screen")
        self.assertEqual(updated["stage"], "interviewing")
        stages = [h["stage"] for h in updated["stage_history"]]
        self.assertEqual(stages, ["applied", "interviewing"])
        self.assertEqual(updated["stage_history"][-1]["note"], "phone screen")
        # persisted
        reloaded = lifecycle.load_entries(self.store)
        self.assertEqual(reloaded[0]["stage"], "interviewing")

    def test_update_stage_by_index(self):
        self._write([_sample_entry(), _sample_entry(job_id="linkedin:def456")])
        updated = lifecycle.update_stage(self.store, "1", "rejected")
        self.assertEqual(updated["job_id"], "linkedin:def456")
        self.assertEqual(updated["stage"], "rejected")

    def test_update_stage_invalid_rejected(self):
        self._write([_sample_entry()])
        with self.assertRaises(ValueError):
            lifecycle.update_stage(self.store, "linkedin:abc123", "hired")
        with self.assertRaises(KeyError):
            lifecycle.update_stage(self.store, "nope", "offer")

    def test_interviewing_sets_follow_up_due(self):
        self._write([_sample_entry()])
        today = date(2026, 9, 9)
        updated = lifecycle.update_stage(self.store, "linkedin:abc123", "interviewing", today=today)
        self.assertEqual(updated["follow_up_due"], (today + timedelta(days=7)).isoformat())

    def test_terminal_stage_clears_follow_up_due(self):
        entry = _sample_entry()
        entry["follow_up_due"] = "2026-09-16"
        self._write([entry])
        updated = lifecycle.update_stage(self.store, "linkedin:abc123", "rejected")
        self.assertIsNone(updated["follow_up_due"])

    def test_due_followups(self):
        entries = [
            _sample_entry(job_id="a", follow_up_due="2026-09-09"),  # due today
            _sample_entry(job_id="b", follow_up_due="2026-09-20"),  # future
            _sample_entry(job_id="c"),  # none set
            _sample_entry(job_id="d", follow_up_due="2026-09-01", stage="rejected"),  # terminal
        ]
        due = lifecycle.due_followups(entries, today=date(2026, 9, 9))
        self.assertEqual([e["job_id"] for e in due], ["a"])

    def test_stats_math(self):
        entries = [
            _sample_entry(job_id="a", board="linkedin"),
            _sample_entry(job_id="b", board="linkedin", stage="interviewing"),
            _sample_entry(job_id="c", board="greenhouse", stage="offer"),
            _sample_entry(job_id="d", board="greenhouse", stage="rejected"),
        ]
        s = lifecycle.stats(entries)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_stage"], {"applied": 1, "interviewing": 1, "offer": 1, "rejected": 1})
        self.assertEqual(s["by_board"], {"linkedin": 2, "greenhouse": 2})
        self.assertAlmostEqual(s["response_rate"], 0.5)

    def test_stats_empty(self):
        s = lifecycle.stats([])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["response_rate"], 0.0)

    def test_export_csv(self):
        entries = [_sample_entry(), _sample_entry(job_id="b", stage="offer")]
        out = Path(self.tmp.name) / "applications.csv"
        n = lifecycle.export_csv(entries, out)
        self.assertEqual(n, 2)
        with open(out, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stage"], "applied")
        self.assertEqual(rows[1]["stage"], "offer")
        self.assertEqual(rows[0]["job_id"], "linkedin:abc123")


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / "watches.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _search_fn(self, jobs):
        def fake(**kwargs):
            return jobs
        return fake

    def _jobs(self, *ids):
        return [
            {"id": i, "title": f"Job {i}", "company": "Acme", "board": "linkedin"}
            for i in ids
        ]

    def test_first_run_is_baseline_no_alerts(self):
        watches = {}
        w = watch.add_watch(watches, "swe-nyc", "software engineer", "New York, NY")
        result = watch.check_watch(w, self._search_fn(self._jobs("j1", "j2")))
        self.assertEqual(result["new_jobs"], [])
        self.assertEqual(result["total_seen"], 2)
        self.assertTrue(w["initialized"])
        # persisted state survives a reload
        watch.save_watches(self.store, watches)
        reloaded = watch.load_watches(self.store)
        result2 = watch.check_watch(reloaded["swe-nyc"], self._search_fn(self._jobs("j1", "j2", "j3")))
        self.assertEqual([j["id"] for j in result2["new_jobs"]], ["j3"])

    def test_second_run_detects_only_new(self):
        watches = {}
        w = watch.add_watch(watches, "x", "q")
        watch.check_watch(w, self._search_fn(self._jobs("j1")))
        result = watch.check_watch(w, self._search_fn(self._jobs("j1", "j2", "j3")))
        self.assertEqual([j["id"] for j in result["new_jobs"]], ["j2", "j3"])

    def test_search_failure_does_not_crash(self):
        def boom(**kwargs):
            raise RuntimeError("blocked")
        watches = {}
        w = watch.add_watch(watches, "x", "q")
        result = watch.check_watch(w, boom)
        self.assertEqual(result["new_jobs"], [])
        self.assertIn("error", result)

    def test_add_remove_watch(self):
        watches = {}
        watch.add_watch(watches, "a", "query", filters={"remote_only": True})
        self.assertIn("a", watches)
        self.assertTrue(watch.remove_watch(watches, "a"))
        self.assertFalse(watch.remove_watch(watches, "a"))
        with self.assertRaises(ValueError):
            watch.add_watch(watches, "", "query")
        with self.assertRaises(ValueError):
            watch.add_watch(watches, "b", "")

    def test_check_all(self):
        watches = {}
        watch.add_watch(watches, "one", "q1")
        watch.add_watch(watches, "two", "q2")
        results = watch.check_all(watches, self._search_fn(self._jobs("j1")))
        self.assertEqual(set(results), {"one", "two"})
        # both baselined
        self.assertEqual(results["one"]["new_jobs"], [])


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, data):
        p = self.pdir / name
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    def test_explicit_name_wins(self):
        self._write("backend.json", {"full_name": "Backend Bob"})
        self._write("profile.json", {"full_name": "Legacy"})
        with mock.patch.dict(os.environ, {"PROFILE_NAME": "frontend"}):
            self._write("frontend.json", {"full_name": "Frontend Fran"})
            got = profiles.load_profile("backend", self.pdir)
        self.assertEqual(got["full_name"], "Backend Bob")

    def test_env_var_resolution(self):
        self._write("frontend.json", {"full_name": "Frontend Fran"})
        with mock.patch.dict(os.environ, {"PROFILE_NAME": "frontend"}):
            got = profiles.load_profile("", self.pdir)
        self.assertEqual(got["full_name"], "Frontend Fran")

    def test_default_json_before_legacy(self):
        self._write("default.json", {"full_name": "Default Dan"})
        self._write("profile.json", {"full_name": "Legacy"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROFILE_NAME", None)
            got = profiles.load_profile("", self.pdir)
        self.assertEqual(got["full_name"], "Default Dan")

    def test_legacy_fallback(self):
        self._write("profile.json", {"full_name": "Legacy"})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROFILE_NAME", None)
            got = profiles.load_profile("", self.pdir)
        self.assertEqual(got["full_name"], "Legacy")

    def test_missing_returns_empty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PROFILE_NAME", None)
            self.assertEqual(profiles.load_profile("", self.pdir), {})

    def test_name_sanitization(self):
        with self.assertRaises(ValueError):
            profiles.resolve_profile_path("../evil", self.pdir)
        with self.assertRaises(ValueError):
            profiles.resolve_profile_path("a/b", self.pdir)

    def test_save_and_list(self):
        profiles.save_profile({"full_name": "X"}, "backend", self.pdir)
        profiles.save_profile({"full_name": "Y"}, "", self.pdir)  # legacy
        self.assertEqual(sorted(profiles.list_profiles(self.pdir)), ["backend", "profile"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
