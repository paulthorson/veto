#!/usr/bin/env python3
"""Tests for Initiative 09 epic 3: explicit opt-in communication channels.

Core invariants:
* No channel is ever enabled by default, by upgrade, or by implication.
* Every enable needs explicit confirm=True from an enumerated opt-in
  source and is audit-logged; a consent write that does not land is a
  FAILED enable, never reported as success.
* Every send requires a genuine interactive user approval bound to the
  exact draft content (nonce + content hash + timestamp, written only by
  the terminal prompt). A caller-asserted confirm=True is a deprecated
  no-op; a self-minted content digest (draft_approval_token) authorizes
  nothing; direct calls to email_sync.send_followup enforce the same
  boundary — draft substitution and replay fail closed.
* The consent store is integrity-protected (0o600, fsync, HMAC with a
  machine-local key); malformed/tampered entries are loudly logged and
  never trusted (fail closed). Key loss fails closed with a recovery
  runbook (RECOVERY.md).
"""

import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from initiatives.i09 import channels
from initiatives.i09.channels import registry


class ChannelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch = mock.patch.object(
            registry, "CONSENT_LOG", Path(self.tmp.name) / "consent.jsonl"
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_no_channel_enabled_by_default(self):
        for name in registry.CHANNELS:
            self.assertFalse(registry.is_enabled(name), name)
        self.assertEqual(registry.enabled_channels(), [])

    def test_enable_requires_explicit_confirm(self):
        result = registry.enable_channel("gmail", via="cli-wizard")
        self.assertFalse(result["ok"])
        self.assertFalse(registry.is_enabled("gmail"))
        self.assertIn("manifest", result)  # declaration shown first

    def test_enable_with_confirm_and_audit(self):
        result = registry.enable_channel(
            "gmail", confirm=True, via="cli-wizard"
        )
        self.assertTrue(result["ok"])
        self.assertTrue(registry.is_enabled("gmail"))
        history = registry.consent_history("gmail")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["action"], "enable")
        self.assertEqual(history[0]["via"], "cli-wizard")
        self.assertIn("mac", history[0])  # integrity-signed

    def test_consent_files_are_private(self):
        registry.enable_channel("gmail", confirm=True, via="web-ui")
        log_mode = stat.S_IMODE(registry.CONSENT_LOG.stat().st_mode)
        self.assertEqual(log_mode, 0o600)
        key_mode = stat.S_IMODE(
            registry._consent_key_path().stat().st_mode
        )
        self.assertEqual(key_mode, 0o600)

    def test_via_must_be_enumerated(self):
        result = registry.enable_channel(
            "gmail", confirm=True, via="test-wizard"
        )
        self.assertFalse(result["ok"])
        self.assertIn("invalid_via", result["error"])
        self.assertFalse(registry.is_enabled("gmail"))

    def test_disable_reverts(self):
        registry.enable_channel("discord", confirm=True, via="api")
        self.assertTrue(registry.is_enabled("discord"))
        out = registry.disable_channel("discord", via="api")
        self.assertTrue(out["ok"])
        self.assertFalse(registry.is_enabled("discord"))
        self.assertNotIn("discord", registry.enabled_channels())

    def test_unknown_channel_rejected(self):
        result = registry.enable_channel(
            "smoke-signals", confirm=True, via="cli-wizard"
        )
        self.assertFalse(result["ok"])
        self.assertIn("known_channels", result)

    def test_channel_spec_invariant_raises_not_asserts(self):
        bad = registry.ChannelSpec(
            name="evil",
            skill="",
            manifest=registry.channel_manifest("gmail"),
            enabled_by_default=True,
        )
        with self.assertRaises(ValueError):
            registry._register(bad)
        self.assertNotIn("evil", registry.CHANNELS)

    def test_consent_write_failure_is_enable_failure(self):
        with mock.patch("os.open", side_effect=OSError("EIO: disk gone")):
            result = registry.enable_channel(
                "gmail", confirm=True, via="cli-wizard"
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertIn("consent_write_failed", result["error"])

    def test_consent_write_failure_is_disable_failure(self):
        registry.enable_channel("gmail", confirm=True, via="cli-wizard")
        with mock.patch("os.open", side_effect=OSError("EIO: disk gone")):
            result = registry.disable_channel("gmail", via="cli-wizard")
        self.assertFalse(result["ok"])
        self.assertIn("consent_write_failed", result["error"])

    def test_malformed_lines_are_loud_and_fail_closed(self):
        registry.enable_channel("gmail", confirm=True, via="cli-wizard")
        with registry.CONSENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
            fh.write('{"channel": "gmail", "action": "enable"}\n')  # no mac
        with self.assertLogs("veto-mcp.i09.channels", level="ERROR") as cm:
            history = registry.consent_history("gmail")
        loud = "\n".join(cm.output)
        self.assertIn("malformed JSON", loud)
        self.assertIn("rejected", loud)
        # Only the one genuinely signed entry survives; store untrusted.
        self.assertEqual(len(history), 1)
        with self.assertLogs("veto-mcp.i09.channels", level="ERROR"):
            self.assertFalse(registry.is_enabled("gmail"))

    def test_tampered_entry_is_rejected_loudly(self):
        registry.enable_channel("gmail", confirm=True, via="cli-wizard")
        raw = registry.CONSENT_LOG.read_text(encoding="utf-8")
        tampered = raw.replace('"channel": "gmail"', '"channel": "discord"')
        self.assertNotEqual(raw, tampered)
        registry.CONSENT_LOG.write_text(tampered, encoding="utf-8")
        with self.assertLogs("veto-mcp.i09.channels", level="ERROR") as cm:
            history = registry.consent_history()
        self.assertIn("signature mismatch", "\n".join(cm.output))
        self.assertEqual(history, [])
        self.assertFalse(registry.is_enabled("gmail"))  # fail closed

    def test_channel_spec_invariant(self):
        for name, spec in registry.CHANNELS.items():
            self.assertFalse(spec.enabled_by_default, name)
            self.assertEqual(
                spec.manifest.validate(), [], f"manifest incomplete: {name}"
            )

    def test_skill_channels_declare_explicit_skill_basis(self):
        for name in ("whatsapp", "discord", "messenger"):
            manifest = registry.channel_manifest(name)
            self.assertIsNotNone(manifest)
            blocked = " ".join(manifest.why_it_may_be_blocked).lower()
            self.assertIn("skill", blocked)
            sent = " ".join(manifest.what_data_it_sends).lower()
            self.assertIn("nothing today", sent)

    def test_package_exports(self):
        self.assertIn("draft_approval_token", channels.__all__)
        self.assertIn("VALID_VIA_SOURCES", channels.__all__)


class GmailChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch = mock.patch.object(
            registry, "CONSENT_LOG", Path(self.tmp.name) / "consent.jsonl"
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.channel = registry.GmailChannel()
        self.draft_a = {
            "to": "recruiter@example.com",
            "subject": "Following up",
            "body": "Hi — checking in.",
        }

    def _enable(self):
        result = registry.enable_channel(
            "gmail", confirm=True, via="cli-wizard"
        )
        self.assertTrue(result["ok"])

    def _token_for(self, draft):
        return registry.draft_approval_token(draft)

    def _interactive_approval(self, draft, answer="send"):
        """Simulate a genuine interactive user approval of ``draft``.

        Patches the TTY check and the prompt input so request_send_approval
        runs exactly as it would with a real user at the terminal typing
        ``answer``. Returns the approval_id on success.
        """
        with (
            mock.patch.object(registry, "_stdin_is_tty", return_value=True),
            mock.patch.object(registry, "_approval_input", return_value=answer),
        ):
            res = registry.request_send_approval(draft, channel="gmail")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["approved"])
        return res["approval_id"]

    def test_blocked_when_channel_not_enabled(self):
        out = self.channel.draft_followup("app-1")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "channel_not_enabled")

    def test_send_refused_when_channel_disabled(self):
        out = self.channel.send_followup(self.draft_a, confirm=True)
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "channel_not_enabled")

    def test_self_mint_attack_fails(self):
        """VETO ATTACK 1 repro: self-minted token + confirm=True sends nothing.

        A caller with zero user involvement computes its own
        draft_approval_token for an arbitrary draft and asserts
        confirm=True. The send must be refused by the REAL boundary
        inside email_sync (only the transport is mocked) — never sent.
        """
        self._enable()
        evil = dict(self.draft_a, to="attacker@evil.example")
        self_minted = self._token_for(evil)  # public digest — proves nothing
        import email_sync

        with mock.patch.object(
            email_sync, "_gmail_send"
        ) as m:
            out = self.channel.send_followup(
                evil, confirm=True, approval_token=self_minted
            )
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "not_interactive")  # no TTY here
        m.assert_not_called()  # transport never attempted

    def test_confirm_true_alone_is_not_authorization(self):
        """confirm=True is a deprecated no-op: without a genuine interactive
        approval the send is refused, not sent."""
        self._enable()
        import email_sync

        with mock.patch.object(email_sync, "_gmail_send") as m:
            out = self.channel.send_followup(self.draft_a, confirm=True)
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "not_interactive")
        m.assert_not_called()

    def test_approval_token_is_ignored_for_authorization(self):
        """The content digest is never accepted as proof of approval."""
        self._enable()
        token = self._token_for(self.draft_a)
        problem = registry._validate_approval(token, registry._content_hash(self.draft_a))
        self.assertEqual(problem, "no_such_approval")

    def test_draft_substitution_attack_fails_closed(self):
        """Approving draft A must not authorize sending draft B.

        Simulates a genuine interactive approval of draft A, then tries
        to spend that approval on a substituted draft B. Refused, and the
        transport is never attempted.
        """
        self._enable()
        approval_id = self._interactive_approval(self.draft_a)
        draft_b = dict(self.draft_a, to="attacker@evil.example")
        with mock.patch("email_sync._gmail_send") as m:
            out = self.channel.send_followup(draft_b, approval_id=approval_id)
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "approval_content_mismatch")
        m.assert_not_called()

    def test_token_binds_exact_content(self):
        self._enable()
        token = self._token_for(self.draft_a)
        # Whitespace-only drift still matches (canonicalized); a real edit
        # does not.
        drifted = dict(self.draft_a, body="  Hi — checking in.  ")
        self.assertEqual(self._token_for(drifted), token)
        edited = dict(self.draft_a, subject="Following up!!!")
        self.assertNotEqual(self._token_for(edited), token)

    def test_draft_returns_approval_token(self):
        self._enable()
        with mock.patch(
            "email_sync.draft_followup", return_value=dict(self.draft_a)
        ):
            out = self.channel.draft_followup("app-1", kind="nudge")
        self.assertTrue(out["ok"])
        self.assertEqual(out["approval_token"], self._token_for(self.draft_a))

    def test_draft_delegates_to_email_sync_when_enabled(self):
        self._enable()
        with mock.patch(
            "email_sync.draft_followup", return_value={"to": "", "subject": "s"}
        ) as m:
            out = self.channel.draft_followup("app-1", kind="nudge")
        self.assertTrue(out["ok"])
        m.assert_called_once_with("app-1", kind="nudge")

    def test_send_does_not_mutate_delegated_result(self):
        self._enable()
        approval_id = self._interactive_approval(self.draft_a)
        delegated = {"sent": True}
        with mock.patch(
            "email_sync.send_followup", return_value=delegated
        ):
            out = self.channel.send_followup(
                self.draft_a, approval_id=approval_id
            )
        self.assertTrue(out["sent"])
        self.assertEqual(out["channel"], "gmail")
        self.assertNotIn("channel", delegated)  # original untouched

    def test_send_against_real_email_sync_gate(self):
        """Two-layer test with NO mocks on the send path.

        A genuine (simulated) interactive approval exists; the REAL
        email_sync.send_followup then runs its own identical boundary.
        In this environment Gmail is not connected, so the real module
        must refuse with its own 'gmail_not_connected' error — a value
        only the real email_sync can produce, proving the approval was
        honored and delegation actually happened. Nothing is sent.
        """
        self._enable()
        approval_id = self._interactive_approval(self.draft_a)
        import email_sync

        self.assertTrue(hasattr(email_sync, "send_followup"))
        out = self.channel.send_followup(
            self.draft_a, approval_id=approval_id
        )
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "gmail_not_connected")
        self.assertEqual(out["channel"], "gmail")

    def test_genuine_interactive_approval_sends(self):
        """The interactive path, end to end: user types 'send', Gmail
        (mocked transport) goes out exactly once."""
        self._enable()
        approval_id = self._interactive_approval(self.draft_a)
        import email_sync

        with (
            mock.patch.object(email_sync, "_gmail_connected", return_value=True),
            mock.patch.object(
                email_sync, "_gmail_send", return_value={"ok": True}
            ) as m,
        ):
            out = self.channel.send_followup(
                self.draft_a, approval_id=approval_id
            )
        self.assertTrue(out["sent"])
        self.assertEqual(out["to"], "recruiter@example.com")
        m.assert_called_once()

    def test_channel_stays_enabled_after_approval_record(self):
        """Regression: send_approval / approval_consumed log entries must
        not flip is_enabled — only enable/disable entries decide state."""
        self._enable()
        approval_id = self._interactive_approval(self.draft_a)
        self.assertTrue(registry.is_enabled("gmail"))
        registry.consume_approval(approval_id, channel="gmail")
        self.assertTrue(registry.is_enabled("gmail"))
        self.assertIn("gmail", registry.enabled_channels())
        out = registry.disable_channel("gmail", via="cli-wizard")
        self.assertTrue(out["ok"])
        self.assertFalse(registry.is_enabled("gmail"))

    def test_approval_is_single_use(self):
        """Spending an approval twice: the replay is refused."""
        self._enable()
        approval_id = self._interactive_approval(self.draft_a)
        import email_sync

        with (
            mock.patch.object(email_sync, "_gmail_connected", return_value=True),
            mock.patch.object(email_sync, "_gmail_send", return_value={"ok": True}),
        ):
            first = self.channel.send_followup(
                self.draft_a, approval_id=approval_id
            )
            second = self.channel.send_followup(
                self.draft_a, approval_id=approval_id
            )
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(second["error"], "approval_already_used")


class InteractiveBoundaryRegressionTests(unittest.TestCase):
    """Regression tests for the Rule 1 veto rework (case
    init-09-comms-rereview): interactive confirmation at the send
    boundary, closing the self-mint + direct-call bypasses."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._patch = mock.patch.object(
            registry, "CONSENT_LOG", Path(self.tmp.name) / "consent.jsonl"
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.draft = {
            "to": "recruiter@example.com",
            "subject": "Following up",
            "body": "Hi — checking in.",
        }
        import email_sync

        self.email_sync = email_sync

    def _enable(self):
        self.assertTrue(
            registry.enable_channel("gmail", confirm=True, via="cli-wizard")["ok"]
        )

    def _interactive_approval(self, draft):
        with (
            mock.patch.object(registry, "_stdin_is_tty", return_value=True),
            mock.patch.object(registry, "_approval_input", return_value="send"),
        ):
            res = registry.request_send_approval(draft, channel="gmail")
        self.assertTrue(res["ok"], res)
        return res["approval_id"]

    def _no_tty(self):
        return mock.patch.object(registry, "_stdin_is_tty", return_value=False)

    # -- VETO ATTACK 1b repro: direct email_sync call, confirm=True, no user --
    def test_direct_email_sync_call_with_confirm_true_refuses(self):
        """email_sync.send_followup(draft, confirm=True) with no user
        approval and no TTY must refuse and never touch Gmail."""
        with (
            self._no_tty(),
            mock.patch.object(self.email_sync, "_run_gmail_cli") as cli,
        ):
            out = self.email_sync.send_followup(dict(self.draft), confirm=True)
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "not_interactive")
        cli.assert_not_called()

    # -- Interactive approval happy path through the direct call --
    def test_direct_call_with_genuine_approval_sends(self):
        approval_id = self._interactive_approval(self.draft)
        with (
            mock.patch.object(
                self.email_sync, "_gmail_connected", return_value=True
            ),
            mock.patch.object(
                self.email_sync, "_gmail_send", return_value={"ok": True}
            ) as m,
        ):
            out = self.email_sync.send_followup(
                dict(self.draft), approval_id=approval_id
            )
        self.assertTrue(out["sent"])
        m.assert_called_once_with(
            "recruiter@example.com", "Following up", "Hi — checking in."
        )

    # -- Approval already on file (wizard preview → approve → send flow) --
    def test_unconsumed_approval_on_file_authorizes_without_threading_id(self):
        approval_id = self._interactive_approval(self.draft)
        with (
            mock.patch.object(
                self.email_sync, "_gmail_connected", return_value=True
            ),
            mock.patch.object(
                self.email_sync, "_gmail_send", return_value={"ok": True}
            ) as m,
        ):
            out = self.email_sync.send_followup(dict(self.draft))
        self.assertTrue(out["sent"])
        m.assert_called_once()
        # The approval was spent: a second send must refuse, not reuse.
        with self._no_tty():
            again = self.email_sync.send_followup(dict(self.draft))
        self.assertFalse(again["sent"])

    # -- Declined / aborted approvals write nothing and refuse --
    def test_declined_approval_writes_no_record(self):
        with (
            mock.patch.object(registry, "_stdin_is_tty", return_value=True),
            mock.patch.object(registry, "_approval_input", return_value="no"),
        ):
            res = registry.request_send_approval(self.draft)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "approval_declined")
        self.assertIsNone(registry._find_unconsumed_approval(
            registry._content_hash(self.draft)))

    def test_non_tty_approval_request_refuses(self):
        with self._no_tty():
            res = registry.request_send_approval(self.draft)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "not_interactive")

    # -- Expired approvals are refused --
    def test_expired_approval_refused(self):
        approval_id = self._interactive_approval(self.draft)
        with mock.patch.object(registry, "APPROVAL_TTL_SECONDS", -1):
            problem = registry._validate_approval(
                approval_id, registry._content_hash(self.draft)
            )
        self.assertEqual(problem, "approval_expired")
        with (
            self._no_tty(),
            mock.patch.object(registry, "APPROVAL_TTL_SECONDS", -1),
        ):
            out = self.email_sync.send_followup(
                dict(self.draft), approval_id=approval_id
            )
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "approval_expired")

    # -- Malformed approval id is not trusted --
    def test_unknown_approval_id_refused(self):
        with self._no_tty():
            out = self.email_sync.send_followup(
                dict(self.draft), approval_id="deadbeef" * 8
            )
        self.assertFalse(out["sent"])
        self.assertEqual(out["error"], "no_such_approval")

    # -- Key loss fails closed and points at the runbook --
    def test_key_loss_fails_closed_with_recovery_pointer(self):
        self._enable()
        self.assertTrue(registry.is_enabled("gmail"))
        registry._consent_key_path().unlink()  # lose the key
        # A fresh key is generated on next use, so every old entry fails
        # verification: the store must read as untrusted and the channel
        # as disabled, with loud logs pointing at the recovery runbook.
        with self.assertLogs(
            "veto-mcp.i09.channels", level="ERROR"
        ) as cm:
            enabled = registry.is_enabled("gmail")
        self.assertFalse(enabled)  # fail closed
        loud = "\n".join(cm.output)
        self.assertIn("RECOVERY.md", loud)
        # The runbook the log points at must actually exist.
        recovery = (
            Path(registry.__file__).parent / "RECOVERY.md"
        )
        self.assertTrue(recovery.is_file())
        self.assertIn("re-key", recovery.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
