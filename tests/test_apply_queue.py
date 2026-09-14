#!/usr/bin/env python3
"""Tests for the drip-apply scheduler (apply_queue.py).

Stdlib unittest only. All file I/O goes to temporary directories — the
real ``apply_queue.json`` / ``compliance.json`` / ``applications.json``
are never touched. No sleeping (sleep_fn/pace_fn injected), no network.

Run:  cd ~/workspace/job-apply-mcp && python3 -m unittest discover -s tests -v
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

import apply_queue
import compliance


def _ok_apply(job_id):
    return {"status": "confirmed", "application": {"job_id": job_id}}


class TestQueueStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "apply_queue.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_list_remove(self):
        item = apply_queue.add_to_queue("job-1", path=self.path)
        self.assertEqual(item["job_id"], "job-1")
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["attempts"], 0)
        items = apply_queue.list_queue(path=self.path)
        self.assertEqual([i["job_id"] for i in items], ["job-1"])
        self.assertTrue(apply_queue.remove_from_queue("job-1", path=self.path))
        self.assertEqual(apply_queue.list_queue(path=self.path), [])
        self.assertFalse(apply_queue.remove_from_queue("job-1", path=self.path))

    def test_add_is_idempotent_and_reschedules(self):
        apply_queue.add_to_queue("job-1", path=self.path)
        apply_queue.add_to_queue("job-1", path=self.path)
        items = apply_queue.list_queue(path=self.path)
        self.assertEqual(len(items), 1)
        future = "2999-01-01T00:00:00+00:00"
        item = apply_queue.add_to_queue("job-1", scheduled_for=future,
                                        path=self.path)
        self.assertEqual(item["scheduled_for"], future)

    def test_add_rejects_empty_job_id(self):
        with self.assertRaises(ValueError):
            apply_queue.add_to_queue("  ", path=self.path)

    def test_add_rejects_bad_scheduled_for(self):
        with self.assertRaises(ValueError):
            apply_queue.add_to_queue("job-1", scheduled_for="not-a-date",
                                     path=self.path)

    def test_load_queue_tolerates_corrupt_file(self):
        self.path.write_text("not json{{", encoding="utf-8")
        self.assertEqual(apply_queue.load_queue(path=self.path), {"items": []})


class TestRunQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "apply_queue.json"
        self.recorded = []
        self.sleeps = []
        self.compliance_applies = 0

        def fake_record(job_id, result):
            self.recorded.append(job_id)
            return {"job_id": job_id}

        self.patches = [
            mock.patch.object(
                compliance, "check_apply_allowed", return_value=(True, "")
            ),
            mock.patch.object(
                compliance, "record_application",
                side_effect=lambda *a, **k: self._count_apply(),
            ),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)
        self.fake_record = fake_record

    def _count_apply(self):
        self.compliance_applies += 1
        return {}

    def _run(self, **kwargs):
        kwargs.setdefault("path", self.path)
        kwargs.setdefault("record_fn", self.fake_record)
        kwargs.setdefault("sleep_fn", self.sleeps.append)
        kwargs.setdefault("pace_fn", lambda: 0)
        return apply_queue.run_queue(kwargs.pop("apply_fn"), **kwargs)

    def test_success_pops_item_and_records(self):
        apply_queue.add_to_queue("job-1", path=self.path)
        apply_queue.add_to_queue("job-2", path=self.path)
        report = self._run(apply_fn=_ok_apply)
        self.assertEqual(report["applied"], ["job-1", "job-2"])
        self.assertEqual(report["failed"], [])
        self.assertIsNone(report["stopped"])
        self.assertEqual(report["remaining"], 0)
        self.assertEqual(self.recorded, ["job-1", "job-2"])
        self.assertEqual(self.compliance_applies, 2)
        # One pace sleep between the two applications, none before the first.
        self.assertEqual(self.sleeps, [0])
        # Applied items leave the queue (they live in applications.json now).
        self.assertEqual(apply_queue.list_queue(path=self.path), [])
        json.dumps(report)  # report must be JSON-serializable

    def test_cap_hit_stops_cleanly(self):
        apply_queue.add_to_queue("job-1", path=self.path)
        apply_queue.add_to_queue("job-2", path=self.path)
        calls = []

        def gated_apply(job_id):
            calls.append(job_id)
            return _ok_apply(job_id)

        with mock.patch.object(
            compliance, "check_apply_allowed",
            side_effect=[(True, ""), (False, "cap reached")],
        ):
            report = self._run(apply_fn=gated_apply)
        self.assertEqual(report["stopped"], "cap_reached")
        self.assertEqual(report["cap_reason"], "cap reached")
        self.assertEqual(report["applied"], ["job-1"])
        self.assertEqual(calls, ["job-1"])  # second item never attempted
        remaining = apply_queue.list_queue(path=self.path)
        self.assertEqual([i["job_id"] for i in remaining], ["job-2"])

    def test_max_items_limits_run(self):
        for j in ("job-1", "job-2", "job-3"):
            apply_queue.add_to_queue(j, path=self.path)
        report = self._run(apply_fn=_ok_apply, max_items=2)
        self.assertEqual(len(report["applied"]), 2)
        self.assertEqual(report["remaining"], 1)

    def test_failed_apply_stays_queued_with_error_note(self):
        apply_queue.add_to_queue("job-1", path=self.path)

        def bad_apply(job_id):
            return {"error": "form changed"}

        report = self._run(apply_fn=bad_apply)
        self.assertEqual(report["applied"], [])
        self.assertEqual(len(report["failed"]), 1)
        self.assertEqual(report["failed"][0]["job_id"], "job-1")
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.compliance_applies, 0)
        items = apply_queue.list_queue(path=self.path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["attempts"], 1)
        self.assertIn("form changed", items[0]["last_error"])

    def test_apply_exception_stays_queued(self):
        apply_queue.add_to_queue("job-1", path=self.path)

        def boom(job_id):
            raise RuntimeError("network down")

        report = self._run(apply_fn=boom)
        self.assertEqual(len(report["failed"]), 1)
        items = apply_queue.list_queue(path=self.path)
        self.assertEqual(items[0]["attempts"], 1)
        self.assertIn("RuntimeError", items[0]["last_error"])

    def test_future_scheduled_item_not_processed(self):
        apply_queue.add_to_queue("job-1", path=self.path)
        apply_queue.add_to_queue(
            "job-2", scheduled_for="2999-01-01T00:00:00+00:00", path=self.path
        )
        calls = []
        report = self._run(apply_fn=lambda j: calls.append(j) or _ok_apply(j))
        self.assertEqual(calls, ["job-1"])
        self.assertEqual(report["remaining"], 1)

    def test_record_failure_keeps_item_queued(self):
        apply_queue.add_to_queue("job-1", path=self.path)

        def bad_record(job_id, result):
            raise OSError("disk full")

        report = self._run(apply_fn=_ok_apply, record_fn=bad_record)
        self.assertEqual(len(report["failed"]), 1)
        items = apply_queue.list_queue(path=self.path)
        self.assertEqual(len(items), 1)
        self.assertIn("record_failed", items[0]["last_error"])

    def test_notify_called_with_summary(self):
        apply_queue.add_to_queue("job-1", path=self.path)
        notes = []
        report = self._run(
            apply_fn=_ok_apply,
            notify_fn=lambda t, b: notes.append((t, b)),
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("applied=1", notes[0][1])
        json.dumps(report)


class TestDefaultRecordFn(unittest.TestCase):
    """_default_record_fn dedupes against the applications store."""

    def setUp(self):
        # The outcome-capture hook must never touch the real
        # outcomes.jsonl: point it at a temp store.
        self._tmp = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            "os.environ",
            {"VETO_OUTCOMES_FILE": str(Path(self._tmp.name) / "outcomes.jsonl")},
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._tmp.cleanup()

    def test_dedupe_path_still_emits_outcome_event(self):
        # Even when the application was already recorded (e.g. before
        # capture was enabled), the dedupe early-return must ensure the
        # outcome event exists. Duplicate-safe: a second call adds nothing.
        existing = [{
            "job_id": "job-1",
            "title": "Existing",
            "submitted_at": "2026-09-13T12:00:00+00:00",
        }]
        fake_server = mock.MagicMock()
        fake_server._load_applications.side_effect = lambda: list(existing)
        with mock.patch.dict(sys.modules, {"server": fake_server}):
            apply_queue._default_record_fn("job-1", {"status": "confirmed"})
            apply_queue._default_record_fn("job-1", {"status": "confirmed"})
        import outcomes as _outcomes

        events = _outcomes.load_events(
            Path(self._tmp.name) / "outcomes.jsonl"
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["application_id"], "job-1")
        self.assertEqual(events[0]["event_type"], "applied")

    def test_dedupe_on_job_id(self):
        existing = [{"job_id": "job-1", "title": "Existing"}]
        saved = []

        fake_server = mock.MagicMock()
        fake_server._load_applications.side_effect = lambda: list(existing)
        fake_server._save_applications.side_effect = lambda apps: saved.extend(apps)
        with mock.patch.dict(sys.modules, {"server": fake_server}):
            entry = apply_queue._default_record_fn(
                "job-1", {"status": "confirmed"}
            )
        self.assertEqual(entry["title"], "Existing")
        # No new entry written: the store already had it.
        self.assertEqual(saved, [])

    def test_appends_new_entry(self):
        saved = []

        fake_server = mock.MagicMock()
        fake_server._load_applications.return_value = []
        fake_server._save_applications.side_effect = (
            lambda apps: saved.extend(apps)
        )
        result = {
            "status": "confirmed",
            "preview": {"title": "Dev", "company": "Acme", "board": "lever"},
        }
        with mock.patch.dict(sys.modules, {"server": fake_server}):
            entry = apply_queue._default_record_fn("job-9", result)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["job_id"], "job-9")
        self.assertEqual(saved[0]["title"], "Dev")
        self.assertEqual(saved[0]["stage"], "applied")
        self.assertEqual(entry, saved[0])


class TestRegisterContracts(unittest.TestCase):
    def test_register_cli_adds_parsers(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        apply_queue.register_cli(sub)
        for cmd in ("queue-add", "queue-list", "queue-run"):
            args = parser.parse_args([cmd] + (["x"] if cmd == "queue-add" else []))
            self.assertTrue(callable(args.func), cmd)

    def test_register_tools_registers(self):
        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn

                return deco

        apply_queue.register_tools(FakeMCP())
        self.assertEqual(set(seen), {"queue_add", "queue_list"})


if __name__ == "__main__":
    unittest.main()
