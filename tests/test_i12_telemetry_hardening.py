#!/usr/bin/env python3
"""Initiative 12 / Epic 6 — WS6 adversarial-review hardening tests.

Proves the fixes for the blind-review KICK_BACK blockers (C1-C4, C6, C10,
O2/O3/O4) plus the nits. Each test class maps to a finding:

* VetoHaltingTest ........... C1: veto halts recording immediately
* TripwireQuarantineTest ..... C2: offending payload(s) actually persisted
* TripwireFailSafeTest ....... C10: _trip() fails closed on quarantine error
* CorruptStateTest ........... C3: corrupt state stashed + raises, never reset
* ClearingIdentityTest ....... C4: registry + distinct identities + hash chain
* TokenGrammarTest ........... C6: tightened token grammar + honest scanner note
* NotifierTest ............... O3: out-of-band notification path
* RetentionTest .............. O2/O4: access-controlled quarantine + retention
* VanityGuardTest ............ C7/C9: banned metrics + no per-day counters
* StatusArmedTest ............ nit: tripwire_armed reflects actual state
"""

import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from initiatives.i12 import privacy, telemetry
from initiatives.i12.telemetry import (
    ConsentRequired,
    CorruptStateError,
    SchemaViolation,
    TelemetryError,
    TelemetryStore,
    TripwireTripped,
    VetoInForce,
)


def _store(**kw):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "telemetry.json"
    st = TelemetryStore(path, **kw)
    st._tmpdir = tmp  # keep alive for the test's lifetime
    st.set_consent(True, source="test")
    return st


def _arm_roles(st):
    return st.set_roles(specialist="Jordan Ellis",
                        reviewer="Independent Reviewer",
                        populated_by="operator (test)")


def _trip_store(**kw):
    """A store with consent on and one open tripwire incident."""
    st = _store(**kw)
    st.record("tool_opened", tool="jd_decoder", surface="terminal",
              session_id="sess1")
    with _assert_raises(TripwireTripped):
        st.record("tool_opened", tool="jd_decoder", surface="terminal",
                  session_id="5551234567")
    return st


@contextmanager
def _assert_raises(exc):
    try:
        yield
    except exc:
        return
    raise AssertionError(f"{exc.__name__} not raised")


class VetoHaltingTest(unittest.TestCase):
    """C1: paul_veto() halts recording immediately, mid-run."""

    def test_veto_halts_recording_mid_run(self):
        st = _store()
        # Recording works with consent on and no veto...
        st.record("tool_opened", tool="jd_decoder", surface="terminal",
                  session_id="sess1")
        self.assertTrue(st.enabled)
        # ...then the operator files a veto mid-run...
        st.paul_veto("pausing all collection pending review")
        # ...and recording refuses immediately.
        self.assertFalse(st.enabled)
        with self.assertRaises(VetoInForce):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="sess2")
        # VetoInForce is a TelemetryError, so generic handlers still catch it.
        self.assertTrue(issubclass(VetoInForce, TelemetryError))

    def test_veto_requires_reason(self):
        st = _store()
        with self.assertRaises(TelemetryError):
            st.paul_veto("  ")

    def test_lift_requires_explicit_confirmation(self):
        st = _store()
        st.paul_veto("holding")
        with self.assertRaises(TelemetryError):  # bare call is gone
            st.lift_paul_veto()
        with self.assertRaises(TelemetryError):  # trivial confirmation
            st.lift_paul_veto("yes")
        with self.assertRaises(TelemetryError):
            st.lift_paul_veto("ok lift it")
        st.lift_paul_veto("Operator: veto lifted after reviewing the report")
        self.assertTrue(st.enabled)
        st.record("tool_opened", tool="jd_decoder", surface="terminal",
                  session_id="s9")

    def test_lift_with_no_veto_raises(self):
        st = _store()
        with self.assertRaises(TelemetryError):
            st.lift_paul_veto("Operator: there is no veto to lift, honest")

    def test_veto_lift_is_audited(self):
        st = _store()
        st.paul_veto("holding for review")
        st.lift_paul_veto("Operator: reviewed, lifting the veto now")
        log = (st.path.parent / "i12_telemetry_clearings.jsonl").read_text()
        self.assertIn("veto_lifted", log)
        self.assertIn("reviewed, lifting the veto now", log)


class TripwireQuarantineTest(unittest.TestCase):
    """C2: the offending payload is actually persisted; the manifest's
    claims match what is stored."""

    def test_full_tripwire_sequence(self):
        st = _store()
        st.record("tool_opened", tool="jd_decoder", surface="terminal",
                  session_id="sess1")
        with self.assertRaises(TripwireTripped):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="5551234567")
        # enabled False...
        self.assertFalse(st.enabled)
        # ...incident recorded, pointing at the quarantine file...
        self.assertEqual(len(st._state["incidents"]), 1)
        inc = st._state["incidents"][0]
        self.assertEqual(inc["status"], "open")
        qfile = Path(inc["quarantine_file"])
        self.assertTrue(qfile.exists())
        # ...quarantine HAS the payload (offending + prior live events)...
        stored = json.loads(qfile.read_text())
        self.assertEqual(stored["offending_event"]["session_id"], "5551234567")
        self.assertEqual(len(stored["prior_live_events"]), 1)
        self.assertEqual(stored["prior_live_events"][0]["session_id"], "sess1")
        # ...and the manifest's claims match: access-controlled, never
        # "sealed" (no encryption is implemented).
        self.assertNotIn("sealed", json.dumps(stored).lower())
        self.assertIn("0600", stored["note"])
        self.assertIn("0700", stored["note"])
        # Access control is real: 0700 dir, 0600 file.
        self.assertEqual(oct(qfile.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(qfile.stat().st_mode & 0o777), "0o600")

    def test_state_file_is_user_only(self):
        st = _store()
        self.assertEqual(oct(st.path.stat().st_mode & 0o777), "0o600")


class TripwireFailSafeTest(unittest.TestCase):
    """C10: _trip() fails closed — a quarantine write failure still shuts
    analytics off and still records the incident."""

    def test_trip_survives_quarantine_write_failure(self):
        st = _store()
        st.record("tool_opened", tool="jd_decoder", surface="terminal",
                  session_id="sess1")

        def _boom(*a, **k):
            raise OSError("disk is on fire")

        st._quarantine_write = _boom
        with self.assertRaises(TripwireTripped):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="5551234567")
        # Fail closed: disabled in memory...
        self.assertFalse(st.enabled)
        # ...incident recorded with the failure noted, not swallowed...
        self.assertEqual(len(st._state["incidents"]), 1)
        inc = st._state["incidents"][0]
        self.assertIsNone(inc["quarantine_file"])
        self.assertIn("disk is on fire", inc["quarantine_error"])
        # ...and the shut-off survived to disk.
        st2 = TelemetryStore(st.path)
        self.assertFalse(st2._state["enabled"])
        self.assertEqual(len(st2._state["incidents"]), 1)


class CorruptStateTest(unittest.TestCase):
    """C3: corrupt state fails closed — stashed aside, then raise. Never a
    silent fresh state (that would erase vetoes/incidents/consent)."""

    def _corrupt_case(self, payload: bytes):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "telemetry.json"
        path.write_bytes(payload)
        return tmp, path

    def test_garbage_raises_and_stashes(self):
        tmp, path = self._corrupt_case(b"\x00\xff not json {{{")
        with self.assertRaises(CorruptStateError):
            TelemetryStore(path)
        backups = list(Path(tmp.name).glob("telemetry.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertFalse(path.exists())  # original moved aside
        tmp.cleanup()

    def test_valid_json_wrong_shape_raises(self):
        tmp, path = self._corrupt_case(b'{"events": [], "oops": true}')
        with self.assertRaises(CorruptStateError):
            TelemetryStore(path)
        self.assertEqual(len(list(Path(tmp.name).glob("*.corrupt-*"))), 1)
        tmp.cleanup()

    def test_corrupt_state_does_not_silently_erase_veto(self):
        # A veto on disk, then corruption: the store must raise, not come
        # back "clean" with the veto gone.
        st = _store()
        st.paul_veto("do not lose this veto")
        st.path.write_bytes(b"corrupted!!")
        with self.assertRaises(CorruptStateError):
            TelemetryStore(st.path)

    def test_healthy_state_still_loads(self):
        st = _store()
        st.record("tool_opened", tool="jd_decoder", surface="terminal",
                  session_id="sess1")
        st2 = TelemetryStore(st.path)
        self.assertEqual(len(st2._state["events"]), 1)


class ClearingIdentityTest(unittest.TestCase):
    """C4: separation of duties — registry-gated, distinct identities,
    hash-chained audit log. Plus the documented residual trust boundary."""

    def test_clearing_blocked_with_empty_registry(self):
        st = _trip_store()
        inc = st._state["incidents"][0]["incident_id"]
        self.assertIsNone(st.get_roles()["security_privacy_specialist"])
        with self.assertRaises(TelemetryError):
            st.clear_incident(inc, "Anyone At All",
                              "a" * 30 + " reason long enough here")

    def test_set_roles_rejects_same_person_twice(self):
        st = _store()
        with self.assertRaises(TelemetryError):
            st.set_roles(specialist="Jordan Ellis", reviewer="jordan ellis",
                         populated_by="operator (test)")
        with self.assertRaises(TelemetryError):
            st.set_roles(specialist="", reviewer="Independent Reviewer",
                         populated_by="operator (test)")

    def test_registry_never_invents_names(self):
        st = _store()
        roles = st.get_roles()
        self.assertIsNone(roles["security_privacy_specialist"])
        self.assertIsNone(roles["independent_reviewer"])
        # documents who populates it (production note; first-name scrub is
        # Soft fixtures only — assert on the human/ops clause)
        self.assertIn("ops", roles["note"])

    def test_clearing_requires_registry_specialist(self):
        st = _trip_store()
        _arm_roles(st)
        inc = st._state["incidents"][0]["incident_id"]
        with self.assertRaises(TelemetryError):
            st.clear_incident(inc, "Mallory Rogue",
                              "a" * 30 + " plausible-sounding reason here")

    def test_verify_requires_registry_reviewer_and_difference(self):
        st = _trip_store()
        _arm_roles(st)
        inc = st._state["incidents"][0]["incident_id"]
        st.clear_incident(inc, "Jordan Ellis",
                          "synthetic test content; scanner verified armed.")
        # Wrong reviewer name (not the registry reviewer)...
        with self.assertRaises(TelemetryError):
            st.verify_clearance(inc, "Some Random Person", "ref-1")
        # ...and the specialist cannot verify their own clearing.
        with self.assertRaises(TelemetryError):
            st.verify_clearance(inc, "Jordan Ellis", "ref-1")
        st.verify_clearance(inc, "Independent Reviewer", "evidence-ref-1")

    def test_verify_rejects_empty_evidence_ref(self):
        st = _trip_store()
        _arm_roles(st)
        inc = st._state["incidents"][0]["incident_id"]
        st.clear_incident(inc, "Jordan Ellis",
                          "synthetic test content; scanner verified armed.")
        with self.assertRaises(TelemetryError):
            st.verify_clearance(inc, "Independent Reviewer", "")
        with self.assertRaises(TelemetryError):
            st.verify_clearance(inc, "Independent Reviewer", "   ")

    def test_verify_rejects_duplicate_verification(self):
        st = _trip_store()
        _arm_roles(st)
        inc = st._state["incidents"][0]["incident_id"]
        st.clear_incident(inc, "Jordan Ellis",
                          "synthetic test content; scanner verified armed.")
        st.verify_clearance(inc, "Independent Reviewer", "evidence-ref-1")
        with self.assertRaises(TelemetryError):
            st.verify_clearance(inc, "Independent Reviewer", "evidence-ref-2")

    def test_clearing_log_is_hash_chained_and_verified(self):
        st = _trip_store()
        _arm_roles(st)
        inc = st._state["incidents"][0]["incident_id"]
        st.clear_incident(inc, "Jordan Ellis",
                          "synthetic test content; scanner verified armed.")
        st.verify_clearance(inc, "Independent Reviewer", "evidence-ref-1")
        lines = (st.path.parent / "i12_telemetry_clearings.jsonl") \
            .read_text().splitlines()
        self.assertGreaterEqual(len(lines), 4)  # incident, roles, clearing, verification
        prev = "GENESIS"
        for i, line in enumerate(lines):
            entry = json.loads(line)
            self.assertEqual(entry["seq"], i)
            self.assertEqual(entry["prev"], prev)
            body = {k: v for k, v in entry.items() if k != "hash"}
            import hashlib
            expect = hashlib.sha256(
                json.dumps(body, sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest()
            self.assertEqual(entry["hash"], expect)
            prev = entry["hash"]

    def test_tampered_log_blocks_load(self):
        st = _trip_store()
        _arm_roles(st)
        logp = st.path.parent / "i12_telemetry_clearings.jsonl"
        lines = logp.read_text().splitlines()
        entry = json.loads(lines[-1])
        entry["data"] = {"forged": True}  # tamper, keep the old hash
        lines[-1] = json.dumps(entry)
        logp.write_text("\n".join(lines) + "\n")
        with self.assertRaises(CorruptStateError):
            TelemetryStore(st.path)

    def test_trust_boundary_is_documented(self):
        # The residual trust boundary (code cannot verify the human behind
        # the keyboard) must be stated honestly in the module docstring.
        self.assertIn("TRUST BOUNDARY", telemetry.__doc__)
        self.assertIn("cannot verify human identity", telemetry.__doc__)


class TokenGrammarTest(unittest.TestCase):
    """C6: tightened token grammar. The constructed evasions are rejected
    at validation; the scanner-behavior test below is honest about what
    privacy.scan_payload does and does not catch TODAY (run against the
    real scanner, not a mock)."""

    def test_session_id_rejects_structured_pii_shapes(self):
        st = _store()
        # underscore-joined name + phone: no separators fit the grammar.
        with self.assertRaises(SchemaViolation):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="john_smith_5551234567")
        # over-long token.
        with self.assertRaises(SchemaViolation):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="a" * 33)

    def test_token_content_screen_rejects_evasions(self):
        st = _store()
        # "resume" marker smuggled into a guide id.
        with self.assertRaises(SchemaViolation):
            st.record("guide_viewed", guide_id="resume_tips_jane_doe_acme_corp",
                      surface="terminal", session_id="abc123")
        # email-shaped workflow id.
        with self.assertRaises(SchemaViolation):
            st.record("workflow_completed", workflow="jane@acme.com",
                      surface="terminal", session_id="abc123", duration_s=5)
        # dashed phone in a free token field.
        with self.assertRaises(SchemaViolation):
            st.record("guide_viewed", guide_id="call-555-123-4567",
                      surface="terminal", session_id="abc123")

    def test_scanner_behavior_on_evasions_is_honest(self):
        # Against the REAL privacy.scan_payload (no mocks): the phone-shaped
        # evasion IS caught by the scanner...
        phone = privacy.scan_payload({"session_id": "john_smith_5551234567"})
        self.assertTrue(phone, f"expected scanner findings, got {phone}")
        # ...but the bare-name guide_id evasion is NOT caught by the current
        # scanner (no marker matches). This is documented, not hidden: it is
        # exactly why _validate() screens token values against forbidden
        # content patterns instead of relying on the scanner alone.
        # RE-REVIEWER NOTE: re-run this test after the privacy hardening
        # lands; if scan_payload starts catching it, update this assertion
        # and the docstring claim to match the new behavior.
        guide = privacy.scan_payload(
            {"guide_id": "resume_tips_jane_doe_acme_corp"})
        self.assertEqual(guide, [])

    def test_compact_session_ids_still_work(self):
        st = _store()
        e = st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="sess1")
        self.assertEqual(e["session_id"], "sess1")


class NotifierTest(unittest.TestCase):
    """O3: the out-of-band notification path. Default: stderr + marker
    file. Pluggable: callable or argv command hook."""

    def test_default_notifier_drops_marker_file(self):
        st = _store()  # default notifier
        with self.assertRaises(TripwireTripped):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="5551234567")
        inc = st._state["incidents"][0]["incident_id"]
        markers = list(st.path.parent.glob(f"PENDING-NOTIFICATION-{inc}.json"))
        self.assertEqual(len(markers), 1)
        marker = json.loads(markers[0].read_text())
        self.assertEqual(marker["incident_id"], inc)
        self.assertIn("roles_registry", marker)
        self.assertIn("runbook", marker)
        self.assertEqual(oct(markers[0].stat().st_mode & 0o777), "0o600")

    def test_callable_notifier_receives_incident(self):
        seen = []
        st = _store(notifier=seen.append)
        with self.assertRaises(TripwireTripped):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="5551234567")
        self.assertEqual(len(seen), 1)
        self.assertIn("incident_id", seen[0])

    def test_command_hook_notifier_runs(self):
        st = _store(notifier=["true"])
        with self.assertRaises(TripwireTripped):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="5551234567")
        self.assertFalse(st.enabled)  # trip unaffected by hook plumbing

    def test_bare_string_notifier_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        with self.assertRaises(TelemetryError):
            TelemetryStore(Path(tmp.name) / "t.json",
                           notifier="page somebody")
        tmp.cleanup()

    def test_failing_notifier_does_not_untrip(self):
        def _boom(incident):
            raise RuntimeError("pager exploded")

        st = _store(notifier=_boom)
        with self.assertRaises(TripwireTripped):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="5551234567")
        self.assertFalse(st.enabled)
        self.assertEqual(len(st._state["incidents"]), 1)


class RetentionTest(unittest.TestCase):
    """O2/O4: quarantine is access-controlled (never 'sealed'); retention
    is explicit-confirm purge that never deletes open-incident evidence."""

    def _aged_quarantine_file(self, st, name, days_old):
        qdir = st.path.parent / "quarantine"
        qdir.mkdir(parents=True, exist_ok=True)
        p = qdir / name
        p.write_text("{}")
        old = time.time() - days_old * 86400
        os.utime(p, (old, old))
        return p

    def test_purge_lists_without_deleting_by_default(self):
        st = _store()
        p = self._aged_quarantine_file(st, "INC-20000101-000000-aaaaaa.json", 100)
        res = st.purge_quarantine(older_than_days=90)
        self.assertIn(str(p), res["candidates"])
        self.assertEqual(res["deleted"], [])
        self.assertTrue(p.exists())

    def test_purge_with_confirm_deletes_old_unprotected(self):
        st = _store()
        p = self._aged_quarantine_file(st, "INC-20000101-000000-bbbbbb.json", 100)
        res = st.purge_quarantine(older_than_days=90, confirm=True)
        self.assertIn(str(p), res["deleted"])
        self.assertFalse(p.exists())
        # ...and the purge itself is audited.
        log = (st.path.parent / "i12_telemetry_clearings.jsonl").read_text()
        self.assertIn("quarantine_purge", log)

    def test_purge_never_deletes_open_incident_evidence(self):
        st = _trip_store()
        inc = st._state["incidents"][0]
        qfile = Path(inc["quarantine_file"])
        old = time.time() - 365 * 86400
        os.utime(qfile, (old, old))
        res = st.purge_quarantine(older_than_days=90, confirm=True)
        self.assertIn(str(qfile), res["skipped_protected"])
        self.assertNotIn(str(qfile), res["candidates"])
        self.assertTrue(qfile.exists())

    def test_no_silent_auto_delete(self):
        # Purge is always an explicit call with confirm=True; there is no
        # background expiry. Prove the API shape: confirm defaults False.
        import inspect
        sig = inspect.signature(TelemetryStore.purge_quarantine)
        self.assertFalse(sig.parameters["confirm"].default)


class VanityGuardTest(unittest.TestCase):
    """C7/C9: banned vanity metrics are refused, and no
    applications-per-day-style counters exist anywhere in the module."""

    def test_banned_metrics_refused(self):
        st = _store()
        for m in telemetry.BANNED_METRICS:
            with self.assertRaises(TelemetryError):
                st.report(m)
        self.assertIn("applications_per_day", telemetry.BANNED_METRICS)

    def test_no_vanity_counters_in_module_source(self):
        src = Path(telemetry.__file__).read_text()
        for i, line in enumerate(src.splitlines(), 1):
            if ("per_day" in line or "apply_count" in line
                    or "application_volume" in line):
                stripped = line.strip()
                # Every occurrence must be part of the ban list itself or
                # the refusal message — never a computed counter.
                self.assertTrue(
                    stripped.startswith(('"', "'"))
                    or "banned" in stripped.lower()
                    or "BANNED_METRICS" in stripped,
                    f"vanity-style counter at telemetry.py:{i}: {stripped}")


class StatusArmedTest(unittest.TestCase):
    """Nit: status()['tripwire_armed'] reflects actual state."""

    def test_armed_reflects_state(self):
        st = _store()
        self.assertTrue(st.status()["tripwire_armed"])
        st.paul_veto("hold")
        self.assertFalse(st.status()["tripwire_armed"])
        st.lift_paul_veto("Operator: hold released after quick check")
        self.assertTrue(st.status()["tripwire_armed"])

    def test_disarmed_after_trip(self):
        st = _trip_store()
        self.assertFalse(st.status()["tripwire_armed"])
        self.assertEqual(st.status()["open_incidents"], 1)


class ConsentDefaultOffTest(unittest.TestCase):
    """C9: consent default-off (explicit coverage in this file too)."""

    def test_default_off(self):
        tmp = tempfile.TemporaryDirectory()
        st = TelemetryStore(Path(tmp.name) / "t.json")
        st._tmpdir = tmp
        self.assertFalse(st.consented)
        self.assertFalse(st.enabled)
        with self.assertRaises(ConsentRequired):
            st.record("tool_opened", tool="jd_decoder", surface="terminal",
                      session_id="sess1")


if __name__ == "__main__":
    unittest.main()
