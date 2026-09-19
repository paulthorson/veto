#!/usr/bin/env python3
"""Initiative 12 / Epic 6 — consent analytics + tripwire tests.

Includes the Q3 exit-gate proof: smuggling resume-like content into a
telemetry event MUST shut analytics off, quarantine the data, and require
the named-specialist clearing protocol before re-enable.
"""

import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i12 import telemetry
from initiatives.i12.telemetry import (
    ConsentRequired,
    SchemaViolation,
    TelemetryError,
    TelemetryStore,
    TripwireTripped,
)


def _store():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "telemetry.json"
    st = TelemetryStore(path)
    st._tmpdir = tmp  # keep alive for the test's lifetime
    return st


class ConsentTest(unittest.TestCase):
    def test_default_off(self):
        st = _store()
        self.assertFalse(st.consented)
        self.assertFalse(st.enabled)

    def test_record_without_consent_rejected(self):
        st = _store()
        with self.assertRaises(ConsentRequired):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="abc")

    def test_opt_in_audited(self):
        st = _store()
        rec = st.set_consent(True, source="test")
        self.assertTrue(rec["opted_in"])
        self.assertTrue(st.consented)
        self.assertTrue(st.enabled)
        hist = json.loads(st.path.read_text())["consent"]["history"]
        self.assertEqual(hist[-1]["source"], "test")


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.st = _store()
        self.st.set_consent(True, source="test")

    def test_valid_event_recorded(self):
        e = self.st.record("tool_opened", tool="jd_decoder",
                           surface="terminal", session_id="sess1")
        self.assertEqual(e["event"], "tool_opened")
        self.assertIn("ts", e)

    def test_unknown_event_type_rejected(self):
        with self.assertRaises(SchemaViolation):
            self.st.record("resume_uploaded", foo="bar")

    def test_unknown_field_rejected(self):
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_opened", tool="jd_decoder",
                           surface="terminal", session_id="s",
                           resume_text="hello")

    def test_bad_token_rejected(self):
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_opened", tool="jd decoder!!",
                           surface="terminal", session_id="s")

    def test_missing_required_field_rejected(self):
        # Every schema field is required: zero kwargs is rejected...
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_opened")
        # ...and so is a partially-filled event.
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_opened", tool="jd_decoder",
                           surface="terminal")

    def test_unknown_tool_and_surface_rejected(self):
        # Pinned value domains are enforced, not just token shape.
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_opened", tool="resume_uploader",
                           surface="terminal", session_id="s1")
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_opened", tool="jd_decoder",
                           surface="dark_web", session_id="s1")
        with self.assertRaises(SchemaViolation):
            self.st.record("artifact_shared", kind="resume_pdf",
                           surface="terminal", session_id="s1")

    def test_wrong_type_rejected(self):
        with self.assertRaises(SchemaViolation):
            self.st.record("tool_completed", tool="jd_decoder",
                           surface="terminal", session_id="s",
                           duration_s="fast", completed=True)


class TripwireTest(unittest.TestCase):
    """Q3 exit-gate proof: content in telemetry => shut off + quarantine."""

    def setUp(self):
        self.st = _store()
        self.st.set_consent(True, source="test")
        self.st.record("tool_opened", tool="jd_decoder", surface="terminal",
                       session_id="sess1")

    def _arm_roles(self):
        # The clearing protocol requires a human-populated roles registry.
        self.st.set_roles(specialist="Jordan Ellis",
                          reviewer="Independent Reviewer",
                          populated_by="operator (test)")

    def _inject_content(self):
        # Phone-shaped session id: passes the strict session_id grammar
        # (compact alphanumerics) but is caught by the privacy content
        # scan — the tripwire backstop proof.
        return self.st.record("tool_opened", tool="jd_decoder",
                              surface="terminal", session_id="5551234567")

    def test_content_triggers_tripwire(self):
        with self.assertRaises(TripwireTripped):
            self._inject_content()

    def test_tripwire_shuts_off_analytics(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        self.assertFalse(self.st.enabled)
        status = self.st.status()
        self.assertEqual(status["open_incidents"], 1)

    def test_tripwire_quarantines_and_deletes_live_copy(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        st2 = TelemetryStore(self.st.path)
        self.assertEqual(st2._state["events"], [])
        qdir = self.st.path.parent / "quarantine"
        qfiles = list(qdir.glob("INC-*.json"))
        self.assertEqual(len(qfiles), 1)
        stored = json.loads(qfiles[0].read_text())
        self.assertIn("incident_id", stored)
        # The offending payload IS persisted for the specialist's forensic
        # investigation (that is the point of quarantine)...
        self.assertEqual(stored["offending_event"]["session_id"], "5551234567")
        self.assertIn("prior_live_events", stored)
        self.assertEqual(len(stored["prior_live_events"]), 1)
        # ...the manifest says exactly that, and never claims "sealed"
        # (there is no encryption — access control only).
        self.assertNotIn("sealed", stored["note"].lower())
        self.assertIn("0600", stored["note"])
        # Access-controlled: directory 0700, payload file 0600.
        self.assertEqual(oct(qdir.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(qfiles[0].stat().st_mode & 0o777), "0o600")
        # The incident record points at the quarantine file.
        inc = self.st._state["incidents"][0]
        self.assertEqual(inc["quarantine_file"], str(qfiles[0]))
        self.assertIsNone(inc["quarantine_error"])

    def test_recording_blocked_while_tripped(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        with self.assertRaises(TripwireTripped):
            self.st.record("tool_opened", tool="jd_decoder",
                           surface="terminal", session_id="s2")

    def test_reenable_requires_full_protocol(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        self._arm_roles()
        incident_id = self.st._state["incidents"][0]["incident_id"]
        with self.assertRaises(TelemetryError):
            self.st.reenable()  # open incident

    def test_clearing_requires_named_specialist_and_reason(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        self._arm_roles()
        incident_id = self.st._state["incidents"][0]["incident_id"]
        with self.assertRaises(TelemetryError):
            self.st.clear_incident(incident_id, "", "a" * 30)
        with self.assertRaises(TelemetryError):
            self.st.clear_incident(incident_id, "Jane Doe", "too short")

    def test_full_clearing_protocol_reenables(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        self._arm_roles()
        incident_id = self.st._state["incidents"][0]["incident_id"]
        self.st.clear_incident(
            incident_id, "Jordan Ellis",
            "Schema-allowlisted token field carried a phone-shaped test "
            "value; no real user content was involved. Session-id grammar "
            "confirmed tight and scanner confirmed armed.")
        with self.assertRaises(TelemetryError):
            self.st.reenable()  # cleared but not verified
        # Reviewer must be the registry-named independent reviewer (and a
        # different person from the specialist).
        with self.assertRaises(TelemetryError):
            self.st.verify_clearance(incident_id, "Jordan Ellis", "ref-1")
        self.st.verify_clearance(incident_id, "Independent Reviewer",
                                 "evidence-ref-123")
        self.st.reenable()
        self.assertTrue(self.st.enabled)

    def test_paul_veto_blocks_reenable(self):
        try:
            self._inject_content()
        except TripwireTripped:
            pass
        self._arm_roles()
        incident_id = self.st._state["incidents"][0]["incident_id"]
        self.st.clear_incident(incident_id, "Jordan Ellis",
                               "synthetic test content; scanner verified armed "
                               "and token patterns tightened.")
        self.st.verify_clearance(incident_id, "Independent Reviewer", "ref-9")
        self.st.paul_veto("holding for deeper investigation")
        with self.assertRaises(TelemetryError):
            self.st.reenable()
        # Lifting a veto is not a bare call: operator types a confirmation.
        with self.assertRaises(TelemetryError):
            self.st.lift_paul_veto("yes")
        self.st.lift_paul_veto(
            "Operator: veto lifted after reviewing the specialist's report")
        self.st.reenable()
        self.assertTrue(self.st.enabled)


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.st = _store()
        self.st.set_consent(True, source="test")

    def test_banned_vanity_metrics_refused(self):
        for m in ("applications_per_day", "application_volume",
                  "submissions_per_day", "apply_count"):
            with self.assertRaises(TelemetryError):
                self.st.report(m)

    def test_qualified_activation_not_vanity(self):
        self.st.record("tool_opened", tool="jd_decoder", surface="terminal",
                       session_id="s1")
        self.st.record("workflow_completed", workflow="jd_decode",
                       surface="terminal", session_id="s1", duration_s=12.0)
        self.st.record("artifact_shared", kind="score_card",
                       surface="terminal", session_id="s1")
        self.st.record("tool_opened", tool="jd_decoder", surface="terminal",
                       session_id="s2")
        r = self.st.report("qualified_activation")
        self.assertEqual(r["sessions"], 2)
        self.assertEqual(r["qualified_sessions"], 1)

    def test_public_payload_has_no_content_fields(self):
        self.st.record("tool_opened", tool="jd_decoder", surface="web",
                       session_id="s1")
        p = self.st.public_payload()
        self.assertEqual(p["schema"], "i12-public-analytics-v1")
        # Only counts/tokens — the privacy scan passes by construction.
        from initiatives.i12.privacy import scan_payload
        self.assertEqual(scan_payload(p), [])

    def test_unknown_metric_rejected(self):
        with self.assertRaises(TelemetryError):
            self.st.report("vibes")


if __name__ == "__main__":
    unittest.main()
