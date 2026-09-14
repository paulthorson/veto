#!/usr/bin/env python3
"""Tests for reply_radar.py (Initiative 03: recruiter reply radar).

The radar is proposal-only by construction: scan() can never change a
stage; confirm() requires explicit confirmed=True.

Stdlib unittest only. Gmail is mocked; applications + proposals redirected
to temp dirs.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import reply_radar


def _scan_result():
    return {
        "scanned": 1,
        "matched": 1,
        "proposed_updates": [
            {
                "message_id": "m1",
                "subject": "Interview invitation",
                "classification": "interview_invite",
                "confidence": 0.84,
                "application_id": "job-1",
                "company": "Acme Corp",
                "title": "Senior Engineer",
                "current_stage": "applied",
                "proposed_stage": "interviewing",
                "action": "proposed",
            }
        ],
        "applied_updates": [],
    }


def _app(job_id="job-1", company="Acme Corp", title="Senior Engineer"):
    return {
        "job_id": job_id,
        "company": company,
        "title": title,
        "stage": "applied",
        "stage_history": [],
    }


class _IsoMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmpdir = Path(self.tmp.name)
        self.props = tmpdir / "reply_proposals.json"
        self.apps = tmpdir / "applications.json"
        self.apps.write_text(json.dumps([_app()]), encoding="utf-8")
        for attr, path in (
            ("PROPOSALS_FILE", self.props),
            ("APPLICATIONS_FILE", self.apps),
        ):
            patch = mock.patch.object(reply_radar, attr, path)
            patch.start()
            self.addCleanup(patch.stop)
        scan_patch = mock.patch(
            "email_sync.scan_recruiter_emails", return_value=_scan_result()
        )
        scan_patch.start()
        self.addCleanup(scan_patch.stop)


class TestScan(_IsoMixin, unittest.TestCase):
    def test_scan_creates_proposal_never_applies(self):
        result = reply_radar.scan(days=14)
        self.assertEqual(result["new_proposals"], 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["classification"], "interview_invite")
        self.assertEqual(proposal["reply_type"], "Interview invitation")
        self.assertEqual(proposal["proposed_stage"], "interviewing")
        self.assertEqual(proposal["suggested_action"], "confirm_stage")
        self.assertEqual(proposal["status"], "proposed")
        self.assertIn("proposal_id", proposal)
        # Application stage untouched by construction.
        apps = json.loads(self.apps.read_text(encoding="utf-8"))
        self.assertEqual(apps[0]["stage"], "applied")

    def test_rescan_never_resurfaces_same_message(self):
        first = reply_radar.scan()
        pid = first["proposals"][0]["proposal_id"]
        reply_radar.dismiss(pid, reason="nope")
        second = reply_radar.scan()
        self.assertEqual(first["new_proposals"], 1)
        self.assertEqual(second["new_proposals"], 0)
        self.assertEqual(second["open_proposals"], 0)

    def test_informational_items_are_not_proposals(self):
        # already_at_stage / review items belong to the action inbox,
        # not the confirmation queue.
        with mock.patch(
            "email_sync.scan_recruiter_emails",
            return_value={
                "scanned": 2, "matched": 2, "applied_updates": [],
                "proposed_updates": [
                    dict(_scan_result()["proposed_updates"][0],
                         action="already_at_stage"),
                    dict(_scan_result()["proposed_updates"][0],
                         message_id="m2", action="review",
                         proposed_stage=None),
                ],
            },
        ):
            result = reply_radar.scan()
        self.assertEqual(result["new_proposals"], 0)

    def test_scan_gmail_error_surfaced(self):
        with mock.patch(
            "email_sync.scan_recruiter_emails",
            return_value={"scanned": 0, "matched": 0, "proposed_updates": [],
                         "applied_updates": [],
                         "error": "gmail_not_connected",
                         "message": "Connect it."},
        ):
            result = reply_radar.scan()
        self.assertEqual(result["error"], "gmail_not_connected")
        self.assertEqual(result["new_proposals"], 0)


class TestConfirm(_IsoMixin, unittest.TestCase):
    def _proposal_id(self):
        return reply_radar.scan()["proposals"][0]["proposal_id"]

    def test_confirm_without_flag_refuses(self):
        pid = self._proposal_id()
        result = reply_radar.confirm(pid)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "confirmation_required")
        apps = json.loads(self.apps.read_text(encoding="utf-8"))
        self.assertEqual(apps[0]["stage"], "applied")
        # Still proposed: the user can decide later.
        self.assertEqual(reply_radar.list_proposals()[0]["status"], "proposed")

    def test_confirm_with_flag_applies_and_records_provenance(self):
        pid = self._proposal_id()
        result = reply_radar.confirm(pid, confirmed=True, note="looks legit")
        self.assertTrue(result["applied"])
        self.assertEqual(result["to_stage"], "interviewing")
        apps = json.loads(self.apps.read_text(encoding="utf-8"))
        self.assertEqual(apps[0]["stage"], "interviewing")
        notes = [h.get("note", "") for h in apps[0]["stage_history"]]
        self.assertTrue(any("reply-radar confirmed by user" in n for n in notes))
        self.assertTrue(any(pid in n for n in notes))
        self.assertEqual(reply_radar.list_proposals()[0]["status"], "confirmed")

    def test_confirm_stale_proposal_refuses(self):
        # Application moved since the scan: confirming would mis-record.
        pid = self._proposal_id()
        apps = json.loads(self.apps.read_text(encoding="utf-8"))
        apps[0]["stage"] = "interviewing"
        self.apps.write_text(json.dumps(apps), encoding="utf-8")
        result = reply_radar.confirm(pid, confirmed=True)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "stale_proposal")
        self.assertEqual(reply_radar.list_proposals()[0]["status"], "proposed")

    def test_confirm_twice_rejected(self):
        pid = self._proposal_id()
        reply_radar.confirm(pid, confirmed=True)
        with self.assertRaises(ValueError):
            reply_radar.confirm(pid, confirmed=True)

    def test_confirm_unknown_proposal(self):
        with self.assertRaises(KeyError):
            reply_radar.confirm("nope", confirmed=True)

    def test_confirm_proposal_without_stage_change(self):
        reply_radar.scan()
        proposals = reply_radar.load_proposals()
        proposals[0]["proposed_stage"] = None
        reply_radar.save_proposals(proposals)
        with self.assertRaises(ValueError):
            reply_radar.confirm(proposals[0]["proposal_id"], confirmed=True)


class TestDismiss(_IsoMixin, unittest.TestCase):
    def test_dismiss_records_reason_and_keeps_log(self):
        pid = reply_radar.scan()["proposals"][0]["proposal_id"]
        result = reply_radar.dismiss(pid, reason="already handled by phone")
        self.assertTrue(result["dismissed"])
        stored = reply_radar.list_proposals()[0]
        self.assertEqual(stored["status"], "dismissed")
        self.assertIn("already handled", stored["history"][-1]["note"])
        # Dismissed proposals are filterable; nothing was applied.
        self.assertEqual(reply_radar.list_proposals(status="proposed"), [])
        apps = json.loads(self.apps.read_text(encoding="utf-8"))
        self.assertEqual(apps[0]["stage"], "applied")

    def test_list_filter_rejects_bad_status(self):
        with self.assertRaises(ValueError):
            reply_radar.list_proposals(status="bogus")


class TestCli(unittest.TestCase):
    def test_register_cli(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = reply_radar.register_cli(sub)
        self.assertIn("reply-radar", handlers)
        args = parser.parse_args(["reply-radar", "scan", "--days", "7"])
        self.assertEqual(args.func.__name__, "_cli_scan")
        # confirm requires the explicit flag to exist
        args = parser.parse_args(["reply-radar", "confirm", "abc123"])
        self.assertFalse(args.confirm)

    def test_cli_confirm_without_flag_refuses(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        props = Path(tmp.name) / "p.json"
        with mock.patch.object(reply_radar, "PROPOSALS_FILE", props):
            reply_radar.save_proposals([{
                "proposal_id": "abc123", "status": "proposed",
                "proposed_stage": "interviewing", "classification": "x",
                "confidence": 0.9, "application_id": "job-1",
                "current_stage": "applied", "history": [],
            }])
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers()
            handlers = reply_radar.register_cli(sub)
            args = parser.parse_args(["reply-radar", "confirm", "abc123"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = handlers["reply-radar"](args)
            self.assertEqual(code, 1)  # refused, not applied


if __name__ == "__main__":
    unittest.main()
