#!/usr/bin/env python3
"""Tests for Initiative 09 epic 4: calendar handoff.

Asserts: drafting is side-effect free, handoff requires confirm=True,
drafts without times are refused, and the manifest is complete.
"""

import unittest

from initiatives.i09 import calendar_handoff
from initiatives.i09.calendar_handoff import (
    CALENDAR_MANIFEST,
    CalendarDraft,
    confirm_and_handoff,
    propose_followup_reminder,
    propose_interview_prep,
)


_APP = {"company": "Acme", "title": "Backend Engineer", "job_id": "gh:abc"}


class DraftTests(unittest.TestCase):
    def test_interview_prep_draft_with_time(self):
        draft = propose_interview_prep(_APP, interview_at="2026-10-01T14:00:00+00:00")
        self.assertTrue(draft.has_time)
        self.assertEqual(draft.event_type, "interview_prep")
        # Prep starts 45 minutes before the interview.
        self.assertTrue(draft.start_iso < draft.end_iso)
        self.assertIn("Backend Engineer", draft.title)
        self.assertIn("Acme", draft.title)
        self.assertIn("checklist", draft.description.lower())

    def test_interview_prep_draft_without_time(self):
        draft = propose_interview_prep(_APP)
        self.assertFalse(draft.has_time)
        self.assertEqual(draft.start_iso, "")

    def test_followup_reminder_draft(self):
        draft = propose_followup_reminder(_APP, days_after=7,
                                          from_date="2026-09-13T00:00:00+00:00")
        self.assertTrue(draft.has_time)
        self.assertEqual(draft.event_type, "followup_reminder")
        self.assertIn("2026-09-20", draft.start_iso)

    def test_draft_has_no_attendees_by_default(self):
        draft = propose_interview_prep(_APP, interview_at="2026-10-01T14:00:00+00:00")
        self.assertEqual(draft.attendees, [])

    def test_manifest_complete(self):
        self.assertEqual(CALENDAR_MANIFEST.validate(), [])


class HandoffGateTests(unittest.TestCase):
    def _draft(self):
        return propose_interview_prep(_APP, interview_at="2026-10-01T14:00:00+00:00")

    def test_handoff_refused_without_confirm(self):
        out = confirm_and_handoff(self._draft())
        self.assertFalse(out["ok"])
        self.assertEqual(out["handed_off"], 0)
        self.assertTrue(out["drafts"])  # drafts returned for review

    def test_handoff_confirmed(self):
        out = confirm_and_handoff(self._draft(), confirm=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["handed_off"], 1)
        self.assertIn("google-calendar", out["destination"])
        self.assertIn("title", out["data_sent_per_event"])
        self.assertIn("Wiring layer", out["note"])

    def test_missing_time_refused_even_with_confirm(self):
        draft = propose_interview_prep(_APP)  # no time
        out = confirm_and_handoff(draft, confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "missing_time")
        self.assertEqual(out["handed_off"], 0)

    def test_multiple_drafts(self):
        drafts = [
            self._draft(),
            propose_followup_reminder(_APP, from_date="2026-09-13T00:00:00+00:00"),
        ]
        out = confirm_and_handoff(drafts, confirm=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["handed_off"], 2)


if __name__ == "__main__":
    unittest.main()
