#!/usr/bin/env python3
"""Tests for followup.py — smart follow-up drafts and the send gate.

Stdlib unittest only. All file I/O goes to temporary directories — the
real ``applications.json`` is never touched. No network: the Gmail send
path is mocked at ``followup.email_sync.send_followup``.

Run:  cd ~/workspace/job-apply-mcp && .venv/bin/python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import followup
import lifecycle

TODAY = date(2026, 9, 10)


def _entry(**overrides):
    entry = {
        "job_id": "linkedin:abc123",
        "board": "linkedin",
        "title": "Senior Backend Engineer",
        "company": "Acme Corp",
        "location": "New York, NY",
        "stage": "applied",
        "submitted_at": "2026-08-28T12:00:00+00:00",
        "stage_history": [
            {"stage": "applied", "at": "2026-08-28T12:00:00+00:00", "note": ""}
        ],
        "follow_up_due": None,
    }
    entry.update(overrides)
    return entry


def _profile(**overrides):
    profile = {
        "full_name": "Jane Doe",
        "first_name": "Jane",
        "email": "jane@example.com",
        "phone": "555-0100",
        "headline": "Senior Backend Engineer",
        "skills": ["Python", "Postgres", "distributed systems"],
    }
    profile.update(overrides)
    return profile


def _write_store(entries):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "applications.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return tmp, path  # keep tmp alive via the returned handle


class DraftAppliedNudgeTests(unittest.TestCase):
    def test_applied_nudge_uses_company_role_and_profile_skills(self):
        draft = followup.draft_followup(_entry(), _profile(), TODAY)
        self.assertEqual(draft["kind"], "applied_nudge")
        self.assertIn("Acme Corp", draft["subject"])
        self.assertIn("Senior Backend Engineer", draft["subject"])
        self.assertIn("Acme Corp", draft["body"])
        # Value proposition comes from the profile's skills...
        self.assertIn("Python", draft["body"])
        self.assertIn("Postgres", draft["body"])
        # ...and the signature uses the profile name.
        self.assertIn("Jane Doe", draft["body"])
        self.assertIn("555-0100", draft["body"])
        self.assertTrue(draft["tone_notes"])

    def test_applied_nudge_invents_nothing_without_profile_data(self):
        draft = followup.draft_followup(_entry(), {}, TODAY)
        self.assertEqual(draft["kind"], "applied_nudge")
        self.assertNotIn("Python", draft["body"])
        # No dangling fragments where the value line would go: every
        # non-empty line must be clean single-spaced prose.
        for line in draft["body"].splitlines():
            if line.strip():
                self.assertNotIn("  ", line)
        self.assertIn("enthusiastic", draft["body"])

    def test_applied_nudge_falls_back_to_headline(self):
        profile = _profile(skills=[], headline="Staff SRE")
        draft = followup.draft_followup(_entry(), profile, TODAY)
        self.assertIn("Staff SRE", draft["body"])

    def test_days_waiting_reported(self):
        draft = followup.draft_followup(_entry(), _profile(), TODAY)
        self.assertEqual(draft["days_waiting"], 13)  # Aug 28 -> Sep 10


class DraftInterviewCheckinTests(unittest.TestCase):
    def _interview_entry(self, **overrides):
        base = _entry(
            stage="interviewing",
            interviewer="Sam Rivera",
            recruiter_email="sam@acme.example",
            stage_history=[
                {"stage": "applied", "at": "2026-08-20T12:00:00+00:00", "note": ""},
                {"stage": "interviewing", "at": "2026-09-05T12:00:00+00:00", "note": ""},
            ],
            follow_up_due="2026-09-12",
        )
        base.update(overrides)
        return base

    def test_interview_checkin_references_interviewer(self):
        draft = followup.draft_followup(self._interview_entry(), _profile(), TODAY)
        self.assertEqual(draft["kind"], "interview_checkin")
        self.assertIn("Sam", draft["body"])  # first name greeting
        self.assertIn("interview", draft["subject"].lower())
        self.assertIn("timeline", draft["body"].lower())
        self.assertEqual(draft["to"], "sam@acme.example")
        self.assertTrue(draft["tone_notes"])

    def test_interview_checkin_without_interviewer_name(self):
        entry = self._interview_entry(interviewer="")
        draft = followup.draft_followup(entry, _profile(), TODAY)
        self.assertIn("Hi there,", draft["body"])
        self.assertIn("our conversation", draft["body"])

    def test_terminal_stage_raises(self):
        for stage in ("offer", "rejected", "withdrawn"):
            with self.subTest(stage=stage):
                with self.assertRaises(ValueError):
                    followup.draft_followup(
                        _entry(stage=stage), _profile(), TODAY
                    )


class PairingTests(unittest.TestCase):
    def test_stale_applied_paired_with_draft(self):
        tmp, path = _write_store([_entry()])  # applied 13 days ago
        try:
            paired = followup.followups_with_drafts(_profile(), path=path, today=TODAY)
        finally:
            tmp.cleanup()
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["draft"]["kind"], "applied_nudge")
        self.assertEqual(paired[0]["entry"]["job_id"], "linkedin:abc123")

    def test_fresh_applied_not_included(self):
        fresh = _entry(
            job_id="x:1",
            submitted_at="2026-09-08T12:00:00+00:00",
            stage_history=[
                {"stage": "applied", "at": "2026-09-08T12:00:00+00:00", "note": ""}
            ],
        )
        tmp, path = _write_store([fresh])
        try:
            paired = followup.followups_with_drafts(_profile(), path=path, today=TODAY)
        finally:
            tmp.cleanup()
        self.assertEqual(paired, [])

    def test_due_interviewing_included_via_lifecycle_rule(self):
        entry = _entry(
            job_id="x:2",
            stage="interviewing",
            follow_up_due="2026-09-10",  # due today
            stage_history=[
                {"stage": "applied", "at": "2026-08-20T12:00:00+00:00", "note": ""},
                {"stage": "interviewing", "at": "2026-09-03T12:00:00+00:00", "note": ""},
            ],
        )
        tmp, path = _write_store([entry])
        try:
            paired = followup.followups_with_drafts(_profile(), path=path, today=TODAY)
        finally:
            tmp.cleanup()
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["draft"]["kind"], "interview_checkin")

    def test_terminal_and_ghosted_handling(self):
        rejected = _entry(job_id="x:3", stage="rejected")
        tmp, path = _write_store([rejected])
        try:
            paired = followup.followups_with_drafts(_profile(), path=path, today=TODAY)
        finally:
            tmp.cleanup()
        self.assertEqual(paired, [])

    def test_find_entry_by_index(self):
        found = followup.find_entry([_entry(job_id="a"), _entry(job_id="b")], "1")
        self.assertEqual(found["job_id"], "b")
        with self.assertRaises(KeyError):
            followup.find_entry([_entry(job_id="a")], "nope")

    def test_resolve_recipient_prefers_recruiter_email(self):
        entry = _entry(recruiter_email="r@x.example", contact_email="c@x.example")
        self.assertEqual(followup.resolve_recipient(entry), "r@x.example")
        self.assertEqual(followup.resolve_recipient(_entry()), "")


class SendGateTests(unittest.TestCase):
    def test_refuses_without_confirmation_and_makes_no_gmail_call(self):
        with mock.patch(
            "followup.email_sync.send_followup"
        ) as mock_send:
            result = followup.send_followup(
                "linkedin:abc123", "sam@acme.example",
                "Subject", "Body", confirmed=False,
            )
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "not_confirmed")
        mock_send.assert_not_called()
        # Exact text is returned for review.
        self.assertEqual(result["to"], "sam@acme.example")
        self.assertEqual(result["body"], "Body")

    def test_refuses_missing_fields_even_when_confirmed(self):
        with mock.patch(
            "followup.email_sync.send_followup"
        ) as mock_send:
            for kwargs in (
                dict(to="", subject="S", body="B"),
                dict(to="a@b.c", subject="  ", body="B"),
                dict(to="a@b.c", subject="S", body=""),
            ):
                result = followup.send_followup(
                    "x", confirmed=True, **kwargs
                )
                self.assertFalse(result["sent"])
                self.assertEqual(result["error"], "missing_field")
            mock_send.assert_not_called()

    def test_refuses_bad_recipient_and_multiline_subject(self):
        with mock.patch(
            "followup.email_sync.send_followup"
        ) as mock_send:
            bad_to = followup.send_followup(
                "x", "not-an-email", "S", "B", confirmed=True)
            self.assertFalse(bad_to["sent"])
            self.assertEqual(bad_to["error"], "bad_recipient")
            bad_subj = followup.send_followup(
                "x", "a@b.c", "Line1\nLine2", "B", confirmed=True)
            self.assertFalse(bad_subj["sent"])
            self.assertEqual(bad_subj["error"], "bad_subject")
            mock_send.assert_not_called()

    def test_confirmed_send_delegates_to_email_sync(self):
        with mock.patch(
            "followup.email_sync.send_followup",
            return_value={"sent": True, "to": "sam@acme.example",
                          "subject": "S"},
        ) as mock_send:
            result = followup.send_followup(
                "linkedin:abc123", "sam@acme.example",
                "Following up", "Hi Sam,", confirmed=True,
            )
        self.assertTrue(result["sent"])
        self.assertEqual(result["entry_id"], "linkedin:abc123")
        mock_send.assert_called_once()
        sent_draft, = mock_send.call_args.args
        self.assertEqual(sent_draft["to"], "sam@acme.example")
        self.assertTrue(mock_send.call_args.kwargs.get("confirm"))

    def test_send_failure_is_reported_not_raised(self):
        with mock.patch(
            "followup.email_sync.send_followup",
            side_effect=RuntimeError("boom"),
        ):
            result = followup.send_followup(
                "x", "a@b.c", "S", "B", confirmed=True)
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "send_failed")


class CliRegistrationTests(unittest.TestCase):
    def test_register_cli_returns_command_mapping(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        mapping = followup.register_cli(sub)
        self.assertIn("followups", mapping)
        self.assertTrue(callable(mapping["followups"]))

    def test_cli_list_runs_offline(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        tmp, path = _write_store([_entry()])
        real_store = followup.APPLICATIONS_FILE
        followup.APPLICATIONS_FILE = path
        try:
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest="command")
            mapping = followup.register_cli(sub)
            args = parser.parse_args(["followups"])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mapping["followups"](args)
            self.assertEqual(rc, 0)
            self.assertIn("follow-up(s) due", buf.getvalue())
        finally:
            followup.APPLICATIONS_FILE = real_store
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
