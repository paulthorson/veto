#!/usr/bin/env python3
"""Q2 gate policy test suite — Initiative 09.

The roadmap's Q2 exit gate: *every connector passes one capability
contract and one policy test suite before release.* The capability half
lives in test_provider_contracts.py (conformance_suite); this module is
the POLICY half, applied uniformly across every connector:

  P1  Every connector declares a complete manifest (the four
      proof-of-value sections).
  P2  CAPTCHA is never bypassed (pause-and-hand-off is the only path).
      Tested at the contract level: the submission contract's closed
      vocabulary has PAUSED/CAPTCHA_BLOCKED as terminal non-submit
      outcomes, no bypass status exists, rescue-paused flows map to
      non-SUBMITTED statuses, and forged bypass statuses are rejected.
  P3  No submit/send/write happens without explicit confirmation at the
      moment of action.
  P4  The contract kit is local-first: stdlib-only, no network calls, no
      credential reads. Tested behaviorally (socket-blocked calls,
      fresh-interpreter import check, env-secret independence).
  P5  Communication channels never auto-enable.
"""

import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import ats_apply
from initiatives.i09 import calendar_handoff
from initiatives.i09.channels import registry as channel_registry
from providers import _contract
from providers._contract import (
    SubmissionResult,
    SubmissionStatus,
    submission_result_from,
    validate_submission_result,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _all_manifests():
    manifests = list(_contract.all_manifests())
    manifests += [spec.manifest for spec in channel_registry.CHANNELS.values()]
    manifests.append(calendar_handoff.CALENDAR_MANIFEST)
    return manifests


class P1ManifestCompleteness(unittest.TestCase):
    def test_every_connector_manifest_complete(self):
        for manifest in _all_manifests():
            self.assertEqual(
                manifest.validate(), [],
                f"{manifest.name}: incomplete proof-of-value declaration",
            )

    def test_every_manifest_names_what_requires_confirmation(self):
        for manifest in _all_manifests():
            self.assertTrue(
                manifest.what_requires_confirmation,
                f"{manifest.name}: no confirmation requirements declared",
            )

    def test_every_manifest_names_what_data_it_sends(self):
        for manifest in _all_manifests():
            self.assertTrue(
                manifest.what_data_it_sends,
                f"{manifest.name}: data egress not declared",
            )


class P2CaptchaNeverBypassed(unittest.TestCase):
    """Contract-level: pause-and-hand-off is the only path through a block.

    The contract guarantees this structurally: the closed submission
    vocabulary contains PAUSED and CAPTCHA_BLOCKED as terminal non-submit
    outcomes and contains no bypass status, so a connector literally has
    no honest way to report "captcha bypassed".
    """

    def test_vocabulary_has_no_bypass_status(self):
        values = [s.value for s in SubmissionStatus]
        self.assertFalse(
            any("bypass" in v for v in values),
            f"bypass status exists in vocabulary: {values}",
        )

    def test_rescue_paused_browser_flow_maps_to_captcha_blocked(self):
        # Shape mirrors browser_apply.apply_via_browser's rescue return:
        # paused, handed to the user, never submitted.
        raw = {
            "ok": False,
            "submitted": False,
            "paused": True,
            "rescue": {"reason": "captcha"},
            "final_url": "https://example.com/apply",
            "error": "Paused: captcha detected — control handed to the user.",
        }
        result = submission_result_from(raw, connector="browser")
        self.assertEqual(result.status, SubmissionStatus.CAPTCHA_BLOCKED)
        self.assertNotEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertEqual(validate_submission_result(result), [])

    def test_forged_bypass_status_is_rejected(self):
        forged = SubmissionResult(status="captcha_bypassed", connector="x")
        problems = validate_submission_result(forged)
        self.assertTrue(
            any("unknown submission status" in p for p in problems),
            "a bypass status must fail validation",
        )

    def test_handoff_outcome_cannot_claim_submission(self):
        # Even a well-formed pause outcome carries no confirmation, so it
        # can never validate as SUBMITTED.
        result = SubmissionResult(
            status=SubmissionStatus.CAPTCHA_BLOCKED,
            connector="x",
            error="Paused: captcha detected",
        )
        self.assertNotEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertEqual(validate_submission_result(result), [])


class P3ConfirmationGates(unittest.TestCase):
    def test_ats_apply_preview_without_confirm(self):
        from providers._common import make_job_id
        job_id = make_job_id("ashby", "board:123")
        out = ats_apply.apply_direct(
            "ashby", {"id": job_id}, {"full_name": "Test"},
            confirm=False, dry_run=True,
        )
        self.assertEqual(out["mode"], "preview")
        self.assertEqual(out["network_calls"], 0)
        self.assertFalse(out["direct_apply_available"])

    def test_ats_apply_confirmed_is_still_preview_only(self):
        # Legal-hardening commit 6 deleted the submit path: even a
        # confirmed call returns the dry-run preview, never a refusal
        # dict and never a network submit.
        from providers._common import make_job_id
        job_id = make_job_id("greenhouse", "tok:1")
        out = ats_apply.apply_direct(
            "greenhouse", {"id": job_id}, {"full_name": "Test"},
            confirm=True, dry_run=False,
        )
        self.assertEqual(out["mode"], "preview")
        self.assertEqual(out["network_calls"], 0)
        self.assertFalse(out["direct_apply_available"])

    def test_calendar_handoff_needs_confirm(self):
        draft = calendar_handoff.propose_followup_reminder(
            {"company": "Acme", "title": "Eng"},
            from_date="2026-09-13T00:00:00+00:00",
        )
        out = calendar_handoff.confirm_and_handoff(draft, confirm=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["handed_off"], 0)

    def test_gmail_send_needs_two_gates(self):
        # Gate 1: channel enablement. Gate 2: per-send confirm.
        channel = channel_registry.GmailChannel()
        out = channel.send_followup({"to": "r@example.com"}, confirm=True)
        self.assertFalse(out.get("sent", False))
        self.assertEqual(out["error"], "channel_not_enabled")


class P4LocalFirst(unittest.TestCase):
    """Contract-level: the contract kit is local-first.

    The contract guarantees its answers are pure declarations: stdlib-only
    imports, no network calls, no credential reads. These tests verify that
    behaviorally instead of grepping other modules' source text.
    """

    def test_contract_calls_make_no_network_calls(self):
        class StubProvider:
            name = "greenhouse"

            def search(self, *a, **k):
                return []

            def get_details(self, *a, **k):
                return {}

        def _no_network(*a, **k):
            raise AssertionError("contract attempted a network call")

        with mock.patch.object(socket, "socket", _no_network), mock.patch.object(
            socket, "create_connection", _no_network
        ):
            terms, _, _ = _contract.terms_for("greenhouse")
            daily, cooldown = _contract.budget_for("greenhouse")
            results = _contract.conformance_suite(StubProvider())
        self.assertEqual(terms, _contract.TermsClass.OFFICIAL_API)
        self.assertGreater(daily, 0)
        self.assertGreater(cooldown, 0)
        self.assertTrue(results)

    def test_contract_module_imports_no_network_libraries(self):
        # Behavioral: load providers/_contract.py in a fresh interpreter
        # WITHOUT executing providers/__init__ (which legitimately loads
        # httpx for the real provider modules), exercise it, and assert no
        # network library was pulled in transitively.
        code = (
            "import sys, importlib.util; "
            "spec = importlib.util.spec_from_file_location("
            "'cmod', 'providers/_contract.py'); "
            "mod = importlib.util.module_from_spec(spec); "
            "sys.modules['cmod'] = mod; "
            "spec.loader.exec_module(mod); "
            "mod.budget_for('greenhouse'); mod.terms_for('greenhouse'); "
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('httpx','requests','urllib3','playwright','bs4','selenium','aiohttp')]; "
            "print('BAD:' + ','.join(sorted(bad)))"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "BAD:")

    def test_contract_reads_no_credentials(self):
        # The contract never sniffs the environment for secrets: junk
        # credentials change nothing about adzuna's honest-stub declaration.
        with mock.patch.dict(
            os.environ, {"ADZUNA_APP_ID": "junk-id", "ADZUNA_APP_KEY": "junk-key"}
        ):
            terms, _, _ = _contract.terms_for("adzuna")
            daily, _ = _contract.budget_for("adzuna")
        self.assertEqual(terms, _contract.TermsClass.KEY_GATED_API)
        self.assertEqual(daily, 0)


class P5NoAutoEnable(unittest.TestCase):
    def test_channels_never_auto_enable(self):
        for name, spec in channel_registry.CHANNELS.items():
            self.assertFalse(spec.enabled_by_default, name)
        # Fresh registry state (no consent log) => nothing enabled.
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(channel_registry, "CONSENT_LOG",
                                   Path(tmp) / "c.jsonl"):
                self.assertEqual(channel_registry.enabled_channels(), [])


if __name__ == "__main__":
    unittest.main()
