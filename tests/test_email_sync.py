#!/usr/bin/env python3
"""Unit tests for email_sync.py — the Gmail skill interface is fully mocked.

No network access happens here: ``email_sync._run_gmail_cli`` is replaced
with fakes, and the applications store is pointed at a temp file.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import email_sync


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROFILE = {
    "full_name": "Ada Lovelace",
    "first_name": "Ada",
    "email": "ada@example.com",
    "phone": "+1 555-0100",
    "skills": ["Python", "Algorithms"],
}


def _app(job_id: str, company: str, title: str, stage: str = "applied") -> dict:
    return {
        "job_id": job_id,
        "board": "greenhouse",
        "title": title,
        "company": company,
        "location": "Remote",
        "apply_url": "https://example.com/apply",
        "stage": stage,
        "stage_history": [{"stage": stage, "at": "2026-09-01T00:00:00+00:00", "note": ""}],
        "follow_up_due": None,
    }


class EmailSyncTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.apps_file = Path(self.tmp.name) / "applications.json"
        self.patch_apps = mock.patch.object(
            email_sync, "APPLICATIONS_FILE", self.apps_file
        )
        self.patch_apps.start()
        self.addCleanup(self.patch_apps.stop)
        self.patch_profile = mock.patch.object(
            email_sync, "_load_profile", return_value=dict(PROFILE)
        )
        self.patch_profile.start()
        self.addCleanup(self.patch_profile.stop)

    def write_apps(self, apps: list[dict]) -> None:
        self.apps_file.write_text(json.dumps(apps), encoding="utf-8")

    def read_apps(self) -> list[dict]:
        return json.loads(self.apps_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class TestClassifier(unittest.TestCase):
    def test_interview_invite(self) -> None:
        self.assertEqual(
            email_sync.classify_recruiter_email(
                "Interview invitation: Senior Engineer",
                "We would like to schedule a phone screen next week.",
                "Jane Recruiter <jane@acme.com>",
            ),
            "interview_invite",
        )

    def test_rejection(self) -> None:
        self.assertEqual(
            email_sync.classify_recruiter_email(
                "Update on your application",
                "After careful consideration, we've decided to move forward "
                "with other candidates. We wish you the best.",
                "talent@acme.com",
            ),
            "rejection",
        )

    def test_offer(self) -> None:
        self.assertEqual(
            email_sync.classify_recruiter_email(
                "Offer letter — Senior Engineer",
                "We are pleased to offer you the position of Senior Engineer. "
                "Please find your offer letter attached.",
                "hr@acme.com",
            ),
            "offer",
        )

    def test_recruiter_outreach(self) -> None:
        self.assertEqual(
            email_sync.classify_recruiter_email(
                "Exciting opportunity at Beta",
                "I came across your profile and was impressed by your "
                "background. Would you be open to a conversation?",
                "sam@beta.io",
            ),
            "recruiter_outreach",
        )

    def test_followup_needed(self) -> None:
        self.assertEqual(
            email_sync.classify_recruiter_email(
                "Re: Senior Engineer role",
                "Just following up on my previous note — are you still "
                "interested?",
                "sam@beta.io",
            ),
            "followup_needed",
        )

    def test_none_for_job_alert_newsletter(self) -> None:
        self.assertIsNone(
            email_sync.classify_recruiter_email(
                "Job alert: 12 new jobs for you",
                "New Python jobs in New York. Unsubscribe here.",
                "noreply@jobboard.example",
            )
        )

    def test_none_for_application_confirmation(self) -> None:
        self.assertIsNone(
            email_sync.classify_recruiter_email(
                "Application received",
                "Thank you for applying! We received your application and "
                "will review it shortly.",
                "noreply@greenhouse.io",
            )
        )

    def test_none_for_ambiguous_tie(self) -> None:
        # Both interview and rejection signals: never guess.
        self.assertIsNone(
            email_sync.classify_recruiter_email(
                "Update",
                "We will not schedule an interview; we are not moving "
                "forward at this time.",
                "talent@acme.com",
            )
        )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


class TestMatchToApplication(unittest.TestCase):
    APPS = [
        _app("job-1", "Acme Corp", "Senior Engineer"),
        _app("job-2", "Beta LLC", "Data Scientist"),
    ]

    def test_unambiguous_sender_domain_match(self) -> None:
        self.assertEqual(
            email_sync.match_to_application(
                {
                    "sender": "Jane <jane@acme.com>",
                    "subject": "Interview invitation",
                    "body": "Let's schedule a phone screen.",
                },
                self.APPS,
            ),
            "job-1",
        )

    def test_company_mention_in_body(self) -> None:
        self.assertEqual(
            email_sync.match_to_application(
                {
                    "sender": "recruiter@unknown-mail.com",
                    "subject": "Next steps",
                    "body": "Thanks for applying to Beta for the data role.",
                },
                self.APPS,
            ),
            "job-2",
        )

    def test_ambiguous_returns_none(self) -> None:
        apps = [
            _app("job-1", "Acme Corp", "Senior Engineer"),
            _app("job-2", "Acme Labs", "Backend Engineer"),
        ]
        self.assertIsNone(
            email_sync.match_to_application(
                {
                    "sender": "jane@acme.com",
                    "subject": "Interview invitation",
                    "body": "Phone screen next week?",
                },
                apps,
            )
        )

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(
            email_sync.match_to_application(
                {
                    "sender": "sam@gamma.dev",
                    "subject": "Opportunity",
                    "body": "I came across your profile.",
                },
                self.APPS,
            )
        )


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

TRIAGE_MSG = {
    "id": "m1",
    "from": "Jane Recruiter <jane@acme.com>",
    "subject": "Interview invitation — Senior Engineer",
    "date": "2026-09-08",
    "snippet": (
        "We would like to schedule a phone screen and a technical "
        "assessment as the next round."
    ),
}


def _connected_gmail_fake(*args):
    if args[0] == "status":
        return {"ok": True, "status": "connected"}
    if "+triage" in args:
        return [dict(TRIAGE_MSG)]
    if "+read" in args:
        return dict(TRIAGE_MSG, body=TRIAGE_MSG["snippet"] + " Please reply.")
    raise AssertionError(f"unexpected gmail call: {args}")


class TestScan(EmailSyncTestBase):
    def test_scan_proposes_but_does_not_apply(self) -> None:
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        with mock.patch.object(
            email_sync, "_run_gmail_cli", side_effect=_connected_gmail_fake
        ):
            result = email_sync.scan_recruiter_emails(
                days=14, apply_updates=False
            )
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(len(result["proposed_updates"]), 1)
        proposal = result["proposed_updates"][0]
        self.assertEqual(proposal["classification"], "interview_invite")
        self.assertEqual(proposal["proposed_stage"], "interviewing")
        self.assertEqual(proposal["action"], "proposed")
        self.assertEqual(result["applied_updates"], [])
        # Store untouched.
        self.assertEqual(self.read_apps()[0]["stage"], "applied")

    def test_scan_apply_updates_requires_explicit_confirmation(self) -> None:
        # Q4 governance contract: proposal-only until the user confirms.
        # apply_updates=True WITHOUT confirmed=True degrades to propose-only.
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        with mock.patch.object(
            email_sync, "_run_gmail_cli", side_effect=_connected_gmail_fake
        ):
            result = email_sync.scan_recruiter_emails(
                days=14, apply_updates=True
            )
        self.assertEqual(result["applied_updates"], [])
        proposal = result["proposed_updates"][0]
        self.assertEqual(proposal["action"], "proposed")
        self.assertIn("confirmation", proposal["note"])
        self.assertEqual(self.read_apps()[0]["stage"], "applied")

    def test_scan_applies_confident_unambiguous_update(self) -> None:
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        with mock.patch.object(
            email_sync, "_run_gmail_cli", side_effect=_connected_gmail_fake
        ):
            result = email_sync.scan_recruiter_emails(
                days=14, apply_updates=True, confirmed=True
            )
        self.assertEqual(len(result["applied_updates"]), 1)
        applied = result["applied_updates"][0]
        self.assertEqual(applied["proposed_stage"], "interviewing")
        entry = self.read_apps()[0]
        self.assertEqual(entry["stage"], "interviewing")
        self.assertTrue(
            any(
                h["stage"] == "interviewing" and "email-sync" in h["note"]
                for h in entry["stage_history"]
            )
        )

    def test_scan_skips_ambiguous_match(self) -> None:
        self.write_apps(
            [
                _app("job-1", "Acme Corp", "Senior Engineer"),
                _app("job-2", "Acme Labs", "Backend Engineer"),
            ]
        )
        with mock.patch.object(
            email_sync, "_run_gmail_cli", side_effect=_connected_gmail_fake
        ):
            result = email_sync.scan_recruiter_emails()
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["proposed_updates"], [])
        self.assertEqual(result["applied_updates"], [])

    def test_scan_reports_not_connected(self) -> None:
        with mock.patch.object(
            email_sync,
            "_run_gmail_cli",
            return_value={"ok": True, "status": "not_connected"},
        ):
            result = email_sync.scan_recruiter_emails()
        self.assertEqual(result["error"], "gmail_not_connected")
        self.assertEqual(result["scanned"], 0)


# ---------------------------------------------------------------------------
# Drafts + sending
# ---------------------------------------------------------------------------


class TestDraftFollowup(EmailSyncTestBase):
    def test_draft_check_in(self) -> None:
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        draft = email_sync.draft_followup("job-1", kind="check_in")
        self.assertEqual(draft["application_id"], "job-1")
        self.assertIn("Senior Engineer", draft["subject"])
        self.assertIn("Acme Corp", draft["body"])
        self.assertIn("Ada Lovelace", draft["body"])
        self.assertEqual(draft["to"], "")  # no contact on file

    def test_draft_thank_you(self) -> None:
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        draft = email_sync.draft_followup("job-1", kind="thank_you")
        self.assertIn("Thank you", draft["subject"])
        self.assertIn("Ada", draft["body"])

    def test_draft_unknown_application(self) -> None:
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        with self.assertRaises(KeyError):
            email_sync.draft_followup("no-such-job")

    def test_draft_bad_kind(self) -> None:
        self.write_apps([_app("job-1", "Acme Corp", "Senior Engineer")])
        with self.assertRaises(ValueError):
            email_sync.draft_followup("job-1", kind="birthday_card")


class TestSendFollowup(EmailSyncTestBase):
    DRAFT = {
        "application_id": "job-1",
        "kind": "check_in",
        "to": "jane@acme.com",
        "subject": "Following up — Senior Engineer application",
        "body": "Hi, just following up.",
    }

    def setUp(self) -> None:
        super().setUp()
        # Isolate the consent/approval store: interactive-approval tests
        # must never write to the real repo consent log.
        from initiatives.i09.channels import registry

        self.consent_log = Path(self.tmp.name) / "consent.jsonl"
        patch = mock.patch.object(registry, "CONSENT_LOG", self.consent_log)
        patch.start()
        self.addCleanup(patch.stop)

    def test_send_without_confirm_does_not_send(self) -> None:
        with mock.patch.object(email_sync, "_run_gmail_cli") as fake:
            result = email_sync.send_followup(dict(self.DRAFT), confirm=False)
        self.assertFalse(result["sent"])
        self.assertIn("draft", result)
        self.assertIn("instructions", result)
        fake.assert_not_called()

    def _simulated_approval(self, draft):
        """A genuine interactive approval, simulated for tests: TTY
        present, user types 'send' at the prompt."""
        from initiatives.i09.channels import registry

        with (
            mock.patch.object(registry, "CONSENT_LOG", self.consent_log),
            mock.patch.object(registry, "_stdin_is_tty", return_value=True),
            mock.patch.object(registry, "_approval_input", return_value="send"),
        ):
            res = registry.request_send_approval(draft, channel="gmail")
        self.assertTrue(res["ok"], res)
        return res["approval_id"]

    def _no_tty(self):
        from initiatives.i09.channels import registry

        return mock.patch.object(registry, "_stdin_is_tty", return_value=False)

    def test_confirm_true_alone_does_not_send(self) -> None:
        """VETO regression: a caller-asserted confirm=True is not proof of
        approval. Without a genuine interactive approval (no TTY here),
        the send is refused and Gmail is never touched."""
        with (
            self._no_tty(),
            mock.patch.object(email_sync, "_run_gmail_cli") as fake,
        ):
            result = email_sync.send_followup(dict(self.DRAFT), confirm=True)
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "not_interactive")
        fake.assert_not_called()

    def test_send_with_genuine_interactive_approval_sends(self) -> None:
        calls = []

        def fake(*args):
            calls.append(args)
            return {"ok": True, "status": "connected"} if args[0] == "status" else {"ok": True}

        approval_id = self._simulated_approval(self.DRAFT)
        with mock.patch.object(email_sync, "_run_gmail_cli", side_effect=fake):
            result = email_sync.send_followup(
                dict(self.DRAFT), approval_id=approval_id
            )
        self.assertTrue(result["sent"])
        self.assertEqual(result["to"], "jane@acme.com")
        send_calls = [c for c in calls if "+send" in c]
        self.assertEqual(len(send_calls), 1)
        self.assertIn("jane@acme.com", send_calls[0])

    def test_send_with_confirm_but_no_recipient(self) -> None:
        draft = dict(self.DRAFT, to="")
        with mock.patch.object(email_sync, "_run_gmail_cli") as fake:
            result = email_sync.send_followup(draft, confirm=True)
        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "no_recipient")
        fake.assert_not_called()


# ---------------------------------------------------------------------------
# Wiring contract
# ---------------------------------------------------------------------------


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class TestWiring(unittest.TestCase):
    def test_register_tools(self) -> None:
        mcp = FakeMCP()
        email_sync.register_tools(mcp)
        self.assertIn("scan_recruiter_emails", mcp.tools)
        self.assertIn("draft_followup_email", mcp.tools)

    def test_register_cli(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        email_sync.register_cli(sub)
        args = parser.parse_args(["email-scan", "--days", "7"])
        self.assertEqual(args.func, email_sync._cli_email_scan)
        self.assertEqual(args.days, 7)
        args = parser.parse_args(["email-followup", "job-1", "--kind", "nudge"])
        self.assertEqual(args.func, email_sync._cli_email_followup)
        self.assertEqual(args.kind, "nudge")


if __name__ == "__main__":
    unittest.main()
