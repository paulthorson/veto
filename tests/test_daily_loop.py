#!/usr/bin/env python3
"""Tests for the top-five brief (briefs.daily_top_five) and the action
inbox (action_inbox.inbox) — Initiative 03.

Stdlib unittest only. Network/profile/Gmail are not touched: jobs,
profile, and preferences are passed explicitly; follow-up/outcome stores
are redirected to temp dirs.
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import action_inbox
import briefs


def _job(i, score_hint="good"):
    return {
        "job_id": f"job-{i}",
        "title": f"Software Engineer {i}",
        "company": f"Acme {i}",
        "location": "Remote",
        "board": "greenhouse",
        "snippet": "Python backend engineer, 5 years experience",
        "posted_days_ago": i,
    }


_PROFILE = {
    "skills": ["Python", "backend"],
    "seniority": "senior",
    "location": "Remote",
}


class TestDailyTopFive(unittest.TestCase):
    def test_ranks_and_limits(self):
        result = briefs.daily_top_five(
            [_job(1), _job(2), _job(3)], profile=_PROFILE, limit=2
        )
        self.assertEqual(result["considered"], 3)
        self.assertEqual(len(result["top"]), 2)
        scores = [e["fit_score"] for e in result["top"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_vetoed_jobs_excluded(self):
        bad = {
            "job_id": "job-bad",
            "title": "Neurosurgeon",
            "company": "Hospital",
            "location": "Antarctica",
            "board": "x",
            "snippet": "MD required, surgery residency, on-site Antarctica",
            "posted_days_ago": 400,
        }
        result = briefs.daily_top_five([bad], profile=_PROFILE)
        self.assertEqual(result["qualified"], 0)
        self.assertEqual(result["vetoed"], 1)
        self.assertEqual(result["top"], [])

    def test_entries_carry_evidence_freshness_readiness(self):
        result = briefs.daily_top_five([_job(1)], profile=_PROFILE)
        entry = result["top"][0]
        self.assertEqual(entry["fit_band"], "excellent")
        self.assertTrue(entry["evidence"])
        self.assertEqual(entry["freshness_days"], 1.0)
        self.assertTrue(entry["readiness"])
        self.assertIn("job_id", entry)

    def test_provenance_static_while_gate_unmet(self):
        # calibration module does not exist yet -> static, pinned off.
        result = briefs.daily_top_five([_job(1)], profile=_PROFILE)
        prov = result["provenance"]
        self.assertEqual(prov["model"], "static")
        self.assertFalse(prov["personalized"])
        self.assertIn("pinned off", prov["reason"])

    def test_provenance_reads_calibration_when_present(self):
        fake = mock.MagicMock()
        fake.evidence_gate_status.return_value = {
            "gate_met": True,
            "weights_source": "personalized v3",
            "resolved_outcomes": 40,
            "qualified_replies": 8,
        }
        with mock.patch.dict(sys.modules, {"calibration": fake}):
            prov = briefs._score_provenance()
        self.assertEqual(prov["model"], "personalized")
        self.assertTrue(prov["personalized"])

    def test_no_jobs_says_so(self):
        result = briefs.daily_top_five([], profile=_PROFILE)
        self.assertIn("note", result)
        self.assertEqual(result["top"], [])

    def test_readiness_flags_incomplete_profile(self):
        notes = briefs._readiness_notes(_job(1), {})
        self.assertTrue(any("incomplete" in n for n in notes))


class TestActionInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmpdir = Path(self.tmp.name)
        self.apps = tmpdir / "applications.json"
        self.apps.write_text(
            json.dumps([
                {
                    "job_id": "job-1",
                    "company": "Acme",
                    "title": "Engineer",
                    "stage": "applied",
                    "submitted_at": "2026-08-01T00:00:00+00:00",
                    "follow_up_due": "2026-09-01",  # overdue
                    "stage_history": [],
                }
            ]),
            encoding="utf-8",
        )
        patch = mock.patch.object(action_inbox, "APPLICATIONS_FILE", self.apps)
        patch.start()
        self.addCleanup(patch.stop)
        # reply_radar reads its own store: redirect to a temp file.
        import reply_radar

        patch = mock.patch.object(
            reply_radar, "PROPOSALS_FILE", tmpdir / "reply_proposals.json"
        )
        patch.start()
        self.addCleanup(patch.stop)

    def test_inbox_orders_reply_proposals_first(self):
        with mock.patch.object(
            action_inbox, "_reply_proposal_items",
            return_value=[{
                "kind": "reply_proposal",
                "title": "Reply: Interview invitation",
                "detail": "d", "action": "a", "action_args": {},
                "source": "reply_radar",
            }],
        ):
            result = action_inbox.inbox()
        kinds = [i["kind"] for i in result["items"]]
        self.assertEqual(kinds[0], "reply_proposal")
        self.assertIn("followup_due", kinds)
        self.assertEqual(result["by_kind"]["reply_proposal"], 1)

    def test_followup_items_surface_drafts(self):
        result = action_inbox.inbox(today=date(2026, 9, 13))
        fu = [i for i in result["items"] if i["kind"] == "followup_due"]
        self.assertTrue(fu)
        self.assertTrue(fu[0]["overdue"])
        self.assertIn("Draft ready", fu[0]["detail"])

    def test_inbox_is_read_only(self):
        before = self.apps.read_text(encoding="utf-8")
        action_inbox.inbox()
        self.assertEqual(self.apps.read_text(encoding="utf-8"), before)

    def test_outcome_prompts_surfaced_when_stale(self):
        # applications.json has an applied entry from 2026-08-01 with no
        # outcome events -> stale prompt expected.
        result = action_inbox.inbox(today=date(2026, 9, 13))
        prompts = [i for i in result["items"]
                   if i["kind"] == "outcome_prompt"]
        self.assertTrue(prompts)
        self.assertIn("Outcome stale", prompts[0]["title"])


if __name__ == "__main__":
    unittest.main()
