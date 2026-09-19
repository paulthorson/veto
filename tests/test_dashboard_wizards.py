#!/usr/bin/env python3
"""Tests for the dashboard's second-wave guided workflows (Workstream 4).

Covers the seven wizards added for the engine/plugin modules that had no
guided path: watches (20), apply dry-run (21), grill (22), company
brief / interview prep (23), recruiter email scan (24), application
analytics (25), and free-form job search (26).

Convention mirrors tests/test_dashboard.py: stdlib unittest only, every
module boundary mocked so the tests never touch the real state files
(``watches.json``, ``apply_queue.json``, ``grill_sessions.json``), never
hit the network, and never send anything. The confirm-gate contract is
asserted directly: the apply wizard may only ever call the ATS path with
``confirm=False, dry_run=True``, and the email wizard must never call
``send_followup``.

Run:  cd ~/workspace/veto && .venv/bin/python -m pytest tests/test_dashboard_wizards.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import dashboard  # noqa: E402


def _run(fn, *args):
    """Run a workflow, capturing stdout; return the printed text."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class MenuRegistrationTests(unittest.TestCase):
    """The seven new wizards are registered in MENU and rendered by print_menu."""

    EXPECTED = {
        "20": "wf_watch",
        "21": "wf_apply",
        "22": "wf_grill",
        "23": "wf_briefs",
        "24": "wf_email",
        "25": "wf_analytics",
        "26": "wf_search",
    }

    def test_new_wizards_registered(self):
        for key, fn_name in self.EXPECTED.items():
            self.assertIn(key, dashboard.MENU, f"MENU missing key {key!r}")
            label, fn = dashboard.MENU[key]
            self.assertEqual(fn.__name__, fn_name)
            self.assertTrue(label, f"empty label for {key!r}")

    def test_menu_groups_render_new_items(self):
        out = _run(dashboard.print_menu)
        for token in (
            "APPLY",
            "Job watches",
            "Apply to a job (guided dry-run)",
            "Answer the grill",
            "Company brief / interview prep",
            "Scan recruiter email",
            "Application analytics",
            "Search jobs (free-form search)",
        ):
            self.assertIn(token, out, f"print_menu missing {token!r}")

    def test_every_menu_entry_still_callable(self):
        for key, (label, fn) in dashboard.MENU.items():
            self.assertTrue(callable(fn), f"MENU[{key!r}] not callable")


# ---------------------------------------------------------------------------
# wf_watch
# ---------------------------------------------------------------------------


class WfWatchTests(unittest.TestCase):
    def _patch_store(self, watches):
        p_load = mock.patch.object(
            dashboard.watch, "load_watches", return_value=watches
        )
        p_save = mock.patch.object(dashboard.watch, "save_watches")
        self.addCleanup(p_load.stop)
        self.addCleanup(p_save.stop)
        return p_load.start(), p_save.start()

    def test_list_shows_saved_watches(self):
        _, save = self._patch_store(
            {"w1": {"query": "engineer", "location": "NYC", "board": "all",
                    "last_checked_at": None}}
        )
        with mock.patch.object(
            dashboard, "ask_choice", return_value="list watches"
        ):
            out = _run(dashboard.wf_watch)
        self.assertIn("w1", out)
        self.assertIn("engineer", out)
        save.assert_not_called()

    def test_list_empty(self):
        self._patch_store({})
        with mock.patch.object(
            dashboard, "ask_choice", return_value="list watches"
        ):
            out = _run(dashboard.wf_watch)
        self.assertIn("No watches saved yet", out)

    def test_add_saves_and_explains_baseline(self):
        _, save = self._patch_store({})
        with mock.patch.object(
            dashboard, "ask_choice", side_effect=["add a watch", "all"]
        ), mock.patch.object(
            dashboard, "ask", side_effect=["mywatch", "engineer", "NYC"]
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ), mock.patch.object(
            dashboard.watch, "add_watch",
            return_value={"name": "mywatch"},
        ) as add:
            out = _run(dashboard.wf_watch)
        add.assert_called_once()
        args = add.call_args.args
        self.assertEqual(args[1:5], ("mywatch", "engineer", "NYC", "all"))
        save.assert_called_once()
        self.assertIn("baseline", out)

    def test_check_baseline_reports_nothing_new(self):
        watches = {
            "w1": {"name": "w1", "query": "eng", "location": "",
                   "board": "all", "last_seen_ids": [], "initialized": False,
                   "filters": {}}
        }
        _, save = self._patch_store(watches)
        fake_server = types.ModuleType("server")
        fake_server.search_jobs = lambda **kw: [
            {"id": "g:1", "title": "Eng", "company": "Acme",
             "board": "greenhouse"}
        ]
        sys.modules["server"] = fake_server
        self.addCleanup(sys.modules.pop, "server", None)
        with mock.patch.object(
            dashboard, "ask_choice", return_value="check for new postings"
        ):
            out = _run(dashboard.wf_watch)
        save.assert_called_once()
        self.assertIn("Nothing new", out)
        self.assertTrue(watches["w1"]["initialized"])

    def test_check_reports_new_postings(self):
        watches = {
            "w1": {"name": "w1", "query": "eng", "location": "",
                   "board": "all", "last_seen_ids": ["g:0"],
                   "initialized": True, "filters": {}}
        }
        self._patch_store(watches)
        fake_server = types.ModuleType("server")
        fake_server.search_jobs = lambda **kw: [
            {"id": "g:0", "title": "Old", "company": "Acme",
             "board": "greenhouse"},
            {"id": "g:1", "title": "New Eng", "company": "Beta",
             "board": "lever"},
        ]
        sys.modules["server"] = fake_server
        self.addCleanup(sys.modules.pop, "server", None)
        with mock.patch.object(
            dashboard, "ask_choice", return_value="check for new postings"
        ):
            out = _run(dashboard.wf_watch)
        self.assertIn("1 new", out)
        self.assertIn("New Eng", out)

    def test_remove_asks_for_confirmation(self):
        _, save = self._patch_store({"w1": {"query": "x"}})
        with mock.patch.object(
            dashboard, "ask_choice", side_effect=["remove a watch", "w1"]
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=True
        ):
            out = _run(dashboard.wf_watch)
        save.assert_called_once()
        self.assertIn("Removed 'w1'", out)


# ---------------------------------------------------------------------------
# wf_apply
# ---------------------------------------------------------------------------


class WfApplyTests(unittest.TestCase):
    """The apply wizard previews only: confirm=False, dry_run=True, and the
    drip queue is the only local write it performs."""

    def _base_patches(self, grill_state):
        patches = [
            mock.patch.object(dashboard, "load_profile", return_value={
                "full_name": "Jane Doe", "email": "jane@example.com"}),
            mock.patch.object(dashboard.grill, "grill_status",
                              return_value=grill_state),
            mock.patch.object(dashboard.compliance, "check_apply_allowed",
                              return_value=(True, "")),
            mock.patch.object(dashboard.compliance, "apply_disclosure",
                              return_value="disclosure text"),
        ]
        for p in patches:
            self.addCleanup(p.stop)
            p.start()

    def test_ats_path_is_preview_only(self):
        self._base_patches({"total": 0})
        fake_ats = types.ModuleType("ats_apply")
        fake_ats.apply_direct = mock.Mock(return_value={
            "mode": "preview", "dry_run": True, "network_calls": 0,
            "board": "greenhouse", "endpoint": "https://x",
            "endpoint_status": "verified",
            "direct_apply_available": True,
            "fields_that_would_be_sent": {"full_name": "Jane Doe"},
        })
        sys.modules["ats_apply"] = fake_ats
        self.addCleanup(sys.modules.pop, "ats_apply", None)
        with mock.patch.object(
            dashboard, "ask_choice",
            return_value="official ATS (direct API)"
        ), mock.patch.object(
            dashboard, "ask",
            side_effect=["greenhouse:abc123", "", ""],
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ):
            out = _run(dashboard.wf_apply)
        fake_ats.apply_direct.assert_called_once()
        _, kwargs = fake_ats.apply_direct.call_args
        self.assertFalse(kwargs["confirm"], "wizard must not confirm submits")
        self.assertTrue(kwargs["dry_run"], "wizard must stay in dry-run")
        self.assertIn("zero network calls", out)
        self.assertIn("never submits", out)

    def test_ats_path_without_httpx_degrades(self):
        self._base_patches({"total": 0})
        real_import = __import__

        def no_ats(name, *args, **kwargs):
            if name == "ats_apply":
                raise ImportError("No module named 'ats_apply'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(
            dashboard, "ask_choice",
            return_value="official ATS (direct API)"
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch("builtins.__import__", side_effect=no_ats):
            out = _run(dashboard.wf_apply)
        self.assertIn("needs httpx", out)

    def test_browser_path_shows_session_state(self):
        self._base_patches({"total": 0})
        with mock.patch.object(
            dashboard, "ask_choice",
            return_value="browser fill (no saved login)"
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ):
            out = _run(dashboard.wf_apply)
        self.assertIn("Saved login sessions are not supported", out)
        self.assertIn("submit click", out)

    def test_grill_gate_offers_grill_first(self):
        self._base_patches({"total": 2, "answered": 1, "complete": False})
        with mock.patch.object(
            dashboard, "ask_choice",
            return_value="browser fill (no saved login)"
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", side_effect=[False, False]
        ), mock.patch("dashboard.wf_grill") as grill_wf:
            out = _run(dashboard.wf_apply)
        self.assertIn("1/2 answered", out)
        grill_wf.assert_not_called()  # user declined; no recursion

    def test_queue_parks_job_locally(self):
        # Regression guard: with httpx installed the real ats_apply used to
        # raise ValueError on the fake job id before the queue prompt, so
        # add.called was False and a guard skipped every assertion. The
        # fake module below forces the preview path, and the assertions
        # are unconditional: if the queue path is never reached this
        # test fails loudly instead of passing silently.
        self._base_patches({"total": 0})
        fake_ats = types.ModuleType("ats_apply")
        fake_ats.apply_direct = mock.Mock(return_value={
            "mode": "preview", "dry_run": True, "network_calls": 0,
            "board": "greenhouse", "endpoint": "https://x",
            "endpoint_status": "verified",
            "direct_apply_available": True,
            "fields_that_would_be_sent": {"full_name": "Jane Doe"},
        })
        sys.modules["ats_apply"] = fake_ats
        self.addCleanup(sys.modules.pop, "ats_apply", None)
        with mock.patch.object(
            dashboard, "ask_choice",
            side_effect=["official ATS (direct API)", "done"]
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=True
        ), mock.patch.object(
            dashboard.apply_queue, "add_to_queue",
            return_value={"job_id": "greenhouse:abc123",
                          "status": "queued"},
        ) as add, mock.patch.object(
            dashboard.apply_queue, "list_queue",
            return_value=[{"job_id": "greenhouse:abc123",
                           "status": "queued"}],
        ):
            out = _run(dashboard.wf_apply)
        add.assert_called_once_with("greenhouse:abc123")
        self.assertIn("Queued greenhouse:abc123", out)
        self.assertIn("never submits", out)

    def _queue_base(self, queued):
        """Preview-only ATS fake + mocked queue store for the queue section."""
        self._base_patches({"total": 0})
        fake_ats = types.ModuleType("ats_apply")
        fake_ats.apply_direct = mock.Mock(return_value={
            "mode": "preview", "dry_run": True, "network_calls": 0,
            "board": "greenhouse", "endpoint": "https://x",
            "endpoint_status": "verified",
            "direct_apply_available": True,
            "fields_that_would_be_sent": {"full_name": "Jane Doe"},
        })
        sys.modules["ats_apply"] = fake_ats
        self.addCleanup(sys.modules.pop, "ats_apply", None)
        p_list = mock.patch.object(
            dashboard.apply_queue, "list_queue", return_value=queued)
        self.addCleanup(p_list.stop)
        p_list.start()

    def test_queue_view_lists_queued_jobs(self):
        queued = [
            {"job_id": "greenhouse:one", "status": "queued", "attempts": 0},
            {"job_id": "lever:two", "status": "queued", "attempts": 1},
        ]
        self._queue_base(queued)
        with mock.patch.object(
            dashboard, "ask_choice",
            side_effect=["official ATS (direct API)", "view the queue"]
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ):
            out = _run(dashboard.wf_apply)
        self.assertIn("2 job(s) queued", out)
        self.assertIn("greenhouse:one", out)
        self.assertIn("lever:two", out)

    def test_queue_remove_asks_which_and_removes(self):
        queued = [
            {"job_id": "greenhouse:one", "status": "queued", "attempts": 0},
            {"job_id": "lever:two", "status": "queued", "attempts": 0},
        ]
        self._queue_base(queued)
        with mock.patch.object(
            dashboard, "ask_choice",
            side_effect=["official ATS (direct API)",
                         "remove a queued job", "greenhouse:one"]
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ), mock.patch.object(
            dashboard.apply_queue, "remove_from_queue", return_value=True
        ) as remove:
            out = _run(dashboard.wf_apply)
        remove.assert_called_once_with("greenhouse:one")
        self.assertIn("Removed greenhouse:one from the queue", out)

    def test_queue_run_needs_explicit_consent(self):
        queued = [{"job_id": "greenhouse:one", "status": "queued",
                   "attempts": 0}]
        self._queue_base(queued)
        with mock.patch.object(
            dashboard, "ask_choice",
            side_effect=["official ATS (direct API)",
                         "run the queue (submit real applications)"]
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", side_effect=[False, False]
        ), mock.patch.object(
            dashboard.apply_queue, "run_queue"
        ) as run:
            out = _run(dashboard.wf_apply)
        run.assert_not_called()
        self.assertIn("Queue run cancelled. Nothing was submitted.", out)

    def test_queue_run_with_consent_applies_via_confirmed_engine(self):
        queued = [{"job_id": "greenhouse:one", "status": "queued",
                   "attempts": 0}]
        self._queue_base(queued)
        fake_server = types.ModuleType("server")
        fake_server.apply_to_job = mock.Mock(
            return_value={"status": "applied"})
        sys.modules["server"] = fake_server
        self.addCleanup(sys.modules.pop, "server", None)
        with mock.patch.object(
            dashboard, "ask_choice",
            side_effect=["official ATS (direct API)",
                         "run the queue (submit real applications)"]
        ), mock.patch.object(
            dashboard, "ask", side_effect=["greenhouse:abc123", "", ""]
        ), mock.patch.object(
            dashboard, "ask_yes_no", side_effect=[False, True]
        ), mock.patch.object(
            dashboard.apply_queue, "run_queue",
            return_value={"processed": 1, "applied": ["greenhouse:one"],
                          "failed": [], "stopped": None,
                          "cap_reason": None, "remaining": 0},
        ) as run:
            out = _run(dashboard.wf_apply)
        run.assert_called_once()
        # The runner's apply_fn must go through the engine's own confirm
        # flow: confirm=True, never an unconfirmed submit.
        apply_fn = run.call_args.args[0]
        apply_fn("greenhouse:one")
        _, kwargs = fake_server.apply_to_job.call_args
        self.assertTrue(kwargs["confirm"])
        self.assertIn("1 applied", out)


# ---------------------------------------------------------------------------
# wf_grill
# ---------------------------------------------------------------------------


class WfGrillTests(unittest.TestCase):
    QUESTIONS = [
        {"id": "q0", "kind": "gap", "question": "Do you know Kubernetes?"},
        {"id": "q1", "kind": "quantify",
         "question": "How many engineers did you lead?"},
    ]

    def test_new_session_asks_each_question(self):
        with mock.patch.object(
            dashboard.grill, "get_grill_session", return_value=None
        ), mock.patch.object(
            dashboard, "load_profile", return_value={}
        ), mock.patch.object(
            dashboard, "ask", side_effect=["Engineer", "Acme"]
        ), mock.patch.object(
            dashboard, "read_text_or_file", return_value="job text"
        ), mock.patch.object(
            dashboard.grill, "start_grill",
            return_value={"questions": self.QUESTIONS},
        ) as start, mock.patch.object(
            dashboard, "_read", side_effect=["Yes, 3 years", ""]
        ), mock.patch.object(
            dashboard.grill, "record_answer",
            return_value={"complete": False, "answered": 1, "total": 2},
        ) as record, mock.patch.object(
            dashboard.grill, "grill_status",
            return_value={"complete": False, "answered": 1, "total": 2},
        ):
            out = _run(dashboard.wf_grill, "test:job1")
        start.assert_called_once()
        record.assert_called_once_with("test:job1", "q0", "Yes, 3 years")
        self.assertIn("Do you know Kubernetes?", out)
        self.assertIn("1/2 answered", out)

    def test_complete_session_does_not_restart(self):
        session = {"questions": self.QUESTIONS,
                   "answers": {"q0": "yes", "q1": "five"}}
        with mock.patch.object(
            dashboard.grill, "get_grill_session", return_value=session
        ), mock.patch.object(
            dashboard.grill, "start_grill"
        ) as start, mock.patch.object(
            dashboard.grill, "grill_status",
            return_value={"complete": True, "answered": 2, "total": 2},
        ):
            out = _run(dashboard.wf_grill, "test:job2")
        start.assert_not_called()
        self.assertIn("Grill complete (2/2 answered)", out)


# ---------------------------------------------------------------------------
# wf_email
# ---------------------------------------------------------------------------


class WfEmailTests(unittest.TestCase):
    def test_gmail_not_connected_explains(self):
        with mock.patch.object(
            dashboard, "ask_int", return_value=14
        ), mock.patch.object(
            dashboard.email_sync, "scan_recruiter_emails",
            return_value={"error": "gmail_not_connected",
                          "scanned": 0, "matched": 0,
                          "proposed_updates": [],
                          "message": "Gmail is not connected."},
        ), mock.patch.object(
            dashboard, "ask_yes_no"
        ) as yes_no:
            out = _run(dashboard.wf_email)
        self.assertIn("Gmail is not connected", out)
        yes_no.assert_not_called()

    def test_proposals_are_preview_only(self):
        proposals = [{
            "subject": "Interview invite", "company": "Acme",
            "title": "Engineer", "current_stage": "applied",
            "proposed_stage": "interviewing", "confidence": 0.9,
            "action": "none",
        }]
        with mock.patch.object(
            dashboard, "ask_int", return_value=14
        ), mock.patch.object(
            dashboard.email_sync, "scan_recruiter_emails",
            return_value={"scanned": 5, "matched": 1,
                          "proposed_updates": proposals},
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ), mock.patch.object(
            dashboard.email_sync, "send_followup"
        ) as send:
            out = _run(dashboard.wf_email)
        send.assert_not_called()
        self.assertIn("Scanned 5 message(s), matched 1", out)
        self.assertIn("applied -> interviewing", out)
        self.assertIn("Nothing was applied", out)

    def test_draft_followup_never_sends(self):
        with mock.patch.object(
            dashboard, "ask_int", return_value=14
        ), mock.patch.object(
            dashboard.email_sync, "scan_recruiter_emails",
            return_value={"scanned": 0, "matched": 0,
                          "proposed_updates": []},
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=True
        ), mock.patch.object(
            dashboard, "ask", return_value="app1"
        ), mock.patch.object(
            dashboard, "ask_choice", return_value="check_in"
        ), mock.patch.object(
            dashboard.email_sync, "draft_followup",
            return_value={"to": "r@acme.com", "subject": "Following up",
                          "body": "Hello,\nChecking in."},
        ), mock.patch.object(
            dashboard.email_sync, "send_followup"
        ) as send:
            out = _run(dashboard.wf_email)
        send.assert_not_called()
        self.assertIn("Following up", out)
        self.assertIn("Draft only, not sent", out)

    def test_scan_is_proposals_only(self):
        # The wizard's apply_updates=False is a safety contract: flipping it
        # must fail loudly here (modelled on test_ats_path_is_preview_only).
        with mock.patch.object(
            dashboard, "ask_int", return_value=14
        ), mock.patch.object(
            dashboard.email_sync, "scan_recruiter_emails",
            return_value={"scanned": 2, "matched": 1,
                          "proposed_updates": []},
        ) as scan, mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ):
            _run(dashboard.wf_email)
        scan.assert_called_once()
        self.assertIs(scan.call_args.kwargs["apply_updates"], False,
                      "wizard must never let the email scan apply updates")

    def test_unknown_application_id(self):
        with mock.patch.object(
            dashboard, "ask_int", return_value=14
        ), mock.patch.object(
            dashboard.email_sync, "scan_recruiter_emails",
            return_value={"scanned": 0, "matched": 0,
                          "proposed_updates": []},
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=True
        ), mock.patch.object(
            dashboard, "ask", return_value="nope"
        ), mock.patch.object(
            dashboard, "ask_choice", return_value="check_in"
        ), mock.patch.object(
            dashboard.email_sync, "draft_followup",
            side_effect=KeyError("nope"),
        ):
            out = _run(dashboard.wf_email)
        self.assertIn("No application with that id", out)


# ---------------------------------------------------------------------------
# wf_briefs
# ---------------------------------------------------------------------------


class WfBriefsTests(unittest.TestCase):
    def test_company_brief_prints_markdown(self):
        with mock.patch.object(
            dashboard, "ask_choice", return_value="company brief"
        ), mock.patch.object(
            dashboard, "ask", return_value="Acme"
        ), mock.patch.object(
            dashboard.briefs, "company_brief",
            return_value={"markdown": "# Acme\n\nDoes things.",
                          "verified": False},
        ):
            out = _run(dashboard.wf_briefs)
        self.assertIn("# Acme", out)
        self.assertIn("Unverified", out)

    def test_interview_prep_uses_profile(self):
        with mock.patch.object(
            dashboard, "ask_choice", return_value="interview prep"
        ), mock.patch.object(
            dashboard, "ask", return_value="lever:abc"
        ), mock.patch.object(
            dashboard, "load_profile",
            return_value={"full_name": "Jane"},
        ), mock.patch.object(
            dashboard.briefs, "prep_interview",
            return_value={"markdown": "# Prep\n\nBe yourself."},
        ) as prep:
            out = _run(dashboard.wf_briefs)
        prep.assert_called_once_with("lever:abc",
                                     profile={"full_name": "Jane"})
        self.assertIn("# Prep", out)

    def test_interview_prep_error(self):
        with mock.patch.object(
            dashboard, "ask_choice", return_value="interview prep"
        ), mock.patch.object(
            dashboard, "ask", return_value="lever:bad"
        ), mock.patch.object(
            dashboard, "load_profile", return_value={}
        ), mock.patch.object(
            dashboard.briefs, "prep_interview",
            return_value={"job_id": "lever:bad",
                          "error": "could not fetch job"},
        ):
            out = _run(dashboard.wf_briefs)
        self.assertIn("could not fetch job", out)


# ---------------------------------------------------------------------------
# wf_analytics
# ---------------------------------------------------------------------------


REPORT = {
    "total_applications": 3,
    "funnel": {
        "total": 3,
        "current_stage_counts": {"applied": 2, "interviewing": 1,
                                 "offer": 0, "rejected": 0},
        "applied_to_interview_rate": 0.3333,
        "interview_to_offer_rate": 0.0,
        "applied_to_offer_rate": 0.0,
    },
    "response_rate_by_board": {
        "greenhouse": {"applied": 2, "responded": 1, "rate": 0.5},
    },
    "time_to_first_response_days": {
        "greenhouse": {"median_days": 4.0, "samples": 1},
    },
    "stale_applications": [
        {"job_id": "g:1", "title": "Engineer", "company": "Acme",
         "board": "greenhouse", "days_stale": 21,
         "last_update": "2026-08-01T00:00:00+00:00"},
    ],
}


class WfAnalyticsTests(unittest.TestCase):
    def test_report_prints_funnel_and_boards(self):
        with mock.patch.object(
            dashboard.analytics, "generate_report", return_value=REPORT
        ):
            out = _run(dashboard.wf_analytics)
        self.assertIn("Total applications: 3", out)
        self.assertIn("applied 2", out)
        self.assertIn("interviewing 1", out)
        self.assertIn("applied -> interview: 33%", out)
        self.assertIn("greenhouse: 1/2 (50%)", out)
        self.assertIn("greenhouse: 4.0", out)
        self.assertIn("Acme: Engineer", out)
        self.assertIn("21 days quiet", out)

    def test_empty_report(self):
        empty = {"total_applications": 0, "funnel": {},
                 "response_rate_by_board": {},
                 "time_to_first_response_days": {},
                 "stale_applications": []}
        with mock.patch.object(
            dashboard.analytics, "generate_report", return_value=empty
        ):
            out = _run(dashboard.wf_analytics)
        self.assertIn("Total applications: 0", out)
        self.assertIn("Nothing stale.", out)


# ---------------------------------------------------------------------------
# wf_search
# ---------------------------------------------------------------------------


class WfSearchTests(unittest.TestCase):
    """Free-form search: the engine is fully mocked (no network), results
    render with risk tags plus the ToS disclosure, and parking a result
    is a local queue write only."""

    RESULTS = [
        {"id": "greenhouse:abc123", "title": "Backend Engineer",
         "company": "Acme", "board": "greenhouse", "location": "NYC",
         "tos_risk_tier": "official_api"},
        {"id": "lever:def456", "title": "Platform Engineer",
         "company": "Beta", "board": "lever", "location": "Remote",
         "tos_risk_tier": "official_api"},
    ]

    def _fake_server(self):
        fake = types.ModuleType("server")
        fake.search_jobs = mock.Mock(return_value=list(self.RESULTS))
        sys.modules["server"] = fake
        self.addCleanup(sys.modules.pop, "server", None)
        return fake

    def test_search_renders_results_with_risk_tags_and_disclosure(self):
        fake = self._fake_server()
        with mock.patch.object(
            dashboard, "ask", side_effect=["engineer", "NYC"]
        ), mock.patch.object(
            dashboard, "ask_choice", return_value="all"
        ), mock.patch.object(
            dashboard, "ask_int", return_value=10
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=False
        ), mock.patch.object(
            dashboard.apply_queue, "add_to_queue"
        ) as add:
            out = _run(dashboard.wf_search)
        fake.search_jobs.assert_called_once_with(
            query="engineer", location="NYC", board="all", limit=10)
        self.assertIn("Backend Engineer", out)
        self.assertIn("Acme", out)
        self.assertIn("official_api", out)
        self.assertIn("ToS disclosure", out)
        add.assert_not_called()
        self.assertIn("never applies from search", out)

    def test_search_park_path_queues_locally(self):
        self._fake_server()
        with mock.patch.object(
            dashboard, "ask", side_effect=["engineer", ""]
        ), mock.patch.object(
            dashboard, "ask_choice",
            side_effect=["greenhouse",
                         "Backend Engineer @ Acme [greenhouse]"]
        ), mock.patch.object(
            dashboard, "ask_int", return_value=10
        ), mock.patch.object(
            dashboard, "ask_yes_no", return_value=True
        ), mock.patch.object(
            dashboard.apply_queue, "add_to_queue",
            return_value={"job_id": "greenhouse:abc123",
                          "status": "queued"},
        ) as add:
            out = _run(dashboard.wf_search)
        add.assert_called_once_with("greenhouse:abc123")
        self.assertIn("Queued greenhouse:abc123", out)
        self.assertIn("Local write only", out)

    def test_search_without_engine_degrades(self):
        real_import = __import__

        def no_server(name, *args, **kwargs):
            if name == "server":
                raise ImportError("No module named 'server'")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(
            dashboard, "ask", side_effect=["engineer", ""]
        ), mock.patch.object(
            dashboard, "ask_choice", return_value="all"
        ), mock.patch.object(
            dashboard, "ask_int", return_value=10
        ), mock.patch("builtins.__import__", side_effect=no_server):
            out = _run(dashboard.wf_search)
        self.assertIn("without the search engine", out)

    def test_search_no_results(self):
        fake = types.ModuleType("server")
        fake.search_jobs = mock.Mock(return_value=[])
        sys.modules["server"] = fake
        self.addCleanup(sys.modules.pop, "server", None)
        with mock.patch.object(
            dashboard, "ask", side_effect=["zzzznonexistent", ""]
        ), mock.patch.object(
            dashboard, "ask_choice", return_value="all"
        ), mock.patch.object(
            dashboard, "ask_int", return_value=10
        ):
            out = _run(dashboard.wf_search)
        fake.search_jobs.assert_called_once()
        self.assertIn("No postings found", out)


if __name__ == "__main__":
    unittest.main()
