#!/usr/bin/env python3
"""Legal-hardening commit 8 (spec §6): non-programmatic per-action circuit
breakers.

Pins plan §10.3 end to end:

* §10.3 step 1 — the exact action summary is rendered and shown: company
  name, job title, every field value as filled, destination URL.
* §6.2 — the prompt requires a per-action VARYING value typed exactly as
  shown (the company name for an application; the recipient address for a
  send; the update count for an email-scan write). A fixed "yes"/"send"
  FAILS. The prompt never echoes the expected value.
* §6.3 — without an interactive terminal (stdin AND stdout both TTYs)
  the action REFUSES and the process exits nonzero. No environment
  variable, CI escape hatch, --yes/--confirm flag, batch approval,
  confirm-all, remembered approval, or session-spanning approval exists.
* §6.5 — the daily apply cap and per-provider search ceilings are hard
  source constants; no config/env/CLI route can raise them.

The single governing approval authority is
``initiatives.i09.channels.registry`` (nonce + content-hash + TTL +
single-use, HMAC-signed log); circuit_breaker is a facade over it, and
these tests exercise the real boundary — never a mocked approval edge.
"""

import argparse
import contextlib
import io
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import circuit_breaker
import cli
import compliance
import provider_health
import server
from initiatives.i09.channels import registry


def _isolated_registry(testcase):
    """Point the consent log at a temp file for the duration of a test."""
    tmp = TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    patch = mock.patch.object(
        registry, "CONSENT_LOG", Path(tmp.name) / "consent.jsonl"
    )
    patch.start()
    testcase.addCleanup(patch.stop)
    registry._CONSUMED_APPROVALS.clear()
    testcase.addCleanup(registry._CONSUMED_APPROVALS.clear)


class SummaryRenderingTests(unittest.TestCase):
    def test_summary_shows_company_title_fields_and_destination(self):
        summary = circuit_breaker.render_application_summary(
            company="Acme Corp",
            title="Senior Engineer",
            fields_filled={"full_name": "Ada Lovelace", "email": "ada@example.com"},
            destination_url="https://example.com/apply/123",
            board="greenhouse",
        )
        self.assertIn("Acme Corp", summary)
        self.assertIn("Senior Engineer", summary)
        self.assertIn("full_name: Ada Lovelace", summary)
        self.assertIn("email: ada@example.com", summary)
        self.assertIn("https://example.com/apply/123", summary)

    def test_company_rendered_exactly_as_passed(self):
        company = "Société Générale"
        summary = circuit_breaker.render_application_summary(
            company=company, title="Analyst", destination_url="https://x.example/"
        )
        line = next(
            l for l in summary.splitlines() if l.startswith("Company:")
        )
        self.assertTrue(line.endswith(company), line)


class VaryingValueTests(unittest.TestCase):
    """§6.2: the typed value varies per action; fixed strings fail."""

    COMPANY = "Acme Corp"

    def setUp(self):
        _isolated_registry(self)
        self._tty = mock.patch.object(
            registry, "_stdin_is_tty", return_value=True
        )
        self._tty.start()
        self.addCleanup(self._tty.stop)

    def _confirm(self, typed, **kwargs):
        prompts = []
        with mock.patch.object(
            registry, "_approval_input", side_effect=lambda p: prompts.append(p) or typed
        ):
            res = circuit_breaker.require_action_confirmation(
                summary=circuit_breaker.render_application_summary(
                    company=self.COMPANY,
                    title="Senior Engineer",
                    destination_url="https://example.com/apply",
                ),
                expected_value=self.COMPANY,
                **kwargs,
            )
        return res, prompts

    def test_exact_company_name_passes(self):
        res, _ = self._confirm("Acme Corp")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["approved"])

    def test_yes_fails(self):
        res, _ = self._confirm("yes")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "approval_declined")

    def test_send_fails(self):
        res, _ = self._confirm("send")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "approval_declined")

    def test_mismatched_company_fails(self):
        res, _ = self._confirm("Acme Corp.")  # trailing period
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "approval_declined")

    def test_different_company_fails(self):
        res, _ = self._confirm("Globex")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "approval_declined")

    def test_surrounding_whitespace_is_tolerated(self):
        res, _ = self._confirm("  Acme Corp\n")
        self.assertTrue(res["ok"], res)

    def test_empty_expected_value_fails_closed_without_prompting(self):
        with mock.patch.object(
            registry, "_approval_input", side_effect=AssertionError("must not prompt")
        ):
            res = circuit_breaker.require_action_confirmation(
                summary="irrelevant", expected_value=""
            )
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "missing_expected_value")

    def test_prompt_shape_is_exact(self):
        _, prompts = self._confirm("Acme Corp")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(
            prompts[0],
            "Type the company name exactly as shown above to submit this "
            "application, anything else to abort: ",
        )

    def test_prompt_never_echoes_the_expected_value(self):
        _, prompts = self._confirm("Acme Corp")
        self.assertNotIn(self.COMPANY, prompts[0])

    def test_custom_value_and_action_labels(self):
        prompts = []
        with mock.patch.object(
            registry,
            "_approval_input",
            side_effect=lambda p: prompts.append(p) or "hr@example.com",
        ):
            res = circuit_breaker.require_action_confirmation(
                summary="To: hr@example.com",
                expected_value="hr@example.com",
                value_label="recipient address",
                action_label="email send",
            )
        self.assertTrue(res["ok"], res)
        self.assertEqual(
            prompts[0],
            "Type the recipient address exactly as shown above to submit "
            "this email send, anything else to abort: ",
        )

    def test_approval_is_single_use(self):
        res, _ = self._confirm("Acme Corp")
        self.assertTrue(res["ok"])
        # The returned approval id authorizes nothing further: it was
        # consumed the moment it authorized this one action. Validating
        # the EXACT content it was minted for now reports single-use
        # exhaustion (the content check passes; the consumed check fires).
        err = registry._validate_approval(
            res["approval_id"],
            res["content_hash"],
            channel="application",
        )
        self.assertEqual(err, "approval_already_used")

    def test_approval_bound_to_exact_summary(self):
        # Mint (not via the facade, so it stays unconsumed) and validate
        # against DIFFERENT content: content binding fails closed even
        # though the approval is fresh and unused.
        with mock.patch.object(
            registry, "_approval_input", return_value=self.COMPANY
        ):
            res = registry.request_action_approval(
                summary="Company: Acme Corp",
                expected_value=self.COMPANY,
                channel="application",
            )
        self.assertTrue(res["ok"], res)
        other_token = registry.draft_approval_token({"body": "different action"})
        err = registry._validate_approval(
            res["approval_id"], other_token, channel="application"
        )
        self.assertEqual(err, "approval_content_mismatch")


class NonTTYRefusalTests(unittest.TestCase):
    """§6.3: no usable terminal → refuse AND exit nonzero. No overrides."""

    def setUp(self):
        _isolated_registry(self)

    def _refuse(self):
        with mock.patch.object(registry, "_stdin_is_tty", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                circuit_breaker.require_action_confirmation(
                    summary="Company: Acme Corp",
                    expected_value="Acme Corp",
                )
        return ctx.exception

    def test_non_tty_refuses_and_exits_nonzero(self):
        exc = self._refuse()
        self.assertEqual(exc.code, 2)

    def test_refusal_names_the_terminal_requirement(self):
        buf = io.StringIO()
        with mock.patch.object(registry, "_stdin_is_tty", return_value=False):
            with contextlib.redirect_stderr(buf):
                with self.assertRaises(SystemExit):
                    circuit_breaker.require_action_confirmation(
                        summary="x", expected_value="x"
                    )
        out = buf.getvalue()
        self.assertIn("interactive terminal", out)
        self.assertIn("no environment", out.lower())

    def test_stdout_piped_away_refuses(self):
        # stdin a TTY but stdout piped: the user would type blind — not
        # interactive. Fail closed.
        with (
            mock.patch.object(registry.sys.stdin, "isatty", return_value=True),
            mock.patch.object(registry.sys.stdout, "isatty", return_value=False),
            self.assertRaises(SystemExit) as ctx,
        ):
            circuit_breaker.require_action_confirmation(
                summary="x", expected_value="x"
            )
        self.assertEqual(ctx.exception.code, 2)

    def test_no_env_override(self):
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "VETO_AUTO_CONFIRM": "1",
            "VETO_YES": "1",
            "VETO_NON_INTERACTIVE": "1",
            "VETO_CONFIRM_ALL": "1",
            "JOB_MCP_AUTO_APPROVE": "1",
            "DEBIAN_FRONTEND": "noninteractive",
        }
        with mock.patch.dict(os.environ, env):
            exc = self._refuse()
        self.assertEqual(exc.code, 2)

    def test_never_prompts_when_not_interactive(self):
        with (
            mock.patch.object(registry, "_stdin_is_tty", return_value=False),
            mock.patch.object(
                registry, "_approval_input", side_effect=AssertionError("must not prompt")
            ),
            self.assertRaises(SystemExit),
        ):
            circuit_breaker.require_action_confirmation(
                summary="x", expected_value="x"
            )

    def test_cli_apply_maps_refusal_to_exit_2(self):
        refused = {
            "status": "refused",
            "error": "not_interactive",
            "instructions": "Refused: confirming this application requires a human at an interactive terminal.",
        }
        args = argparse.Namespace(
            job_id="job-1", resume="", cover_letter="", profile="", confirm=True
        )
        with mock.patch.object(server, "apply_to_job", return_value=refused):
            with contextlib.redirect_stderr(io.StringIO()):
                code = cli.cmd_apply(args)
        self.assertEqual(code, 2)


class NoBypassSurfaceTests(unittest.TestCase):
    """No flag, batch, remembered, or session approval may exist."""

    def setUp(self):
        _isolated_registry(self)

    def test_apply_has_no_yes_or_non_interactive_flag(self):
        parser = cli.build_parser()
        for flag in ("--yes", "--non-interactive", "--batch", "--confirm-all", "--all"):
            with self.assertRaises(SystemExit, msg=flag):
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(["apply", "job-1", flag])

    def test_confirm_flag_does_not_bypass_the_gate(self):
        # Even with --confirm, the Phase-2 fill-only flow still runs the
        # per-action gate; without a TTY it refuses.
        with mock.patch.object(registry, "_stdin_is_tty", return_value=False):
            result = server._apply_via_browser_and_record(
                job_id="job-1",
                board="greenhouse",
                url="https://example.com/apply",
                preview={
                    "title": "Senior Engineer",
                    "company": "Acme Corp",
                    "location": "Remote",
                },
                answers={},
                profile={},
                cover_letter="",
                resume=Path("/tmp/resume.pdf"),
                headless=True,
            )
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["error"], "not_interactive")

    def test_every_action_reprompts_nothing_remembered(self):
        fake_input = mock.Mock(side_effect=["Acme Corp", "Acme Corp"])
        with (
            mock.patch.object(registry, "_stdin_is_tty", return_value=True),
            mock.patch.object(registry, "_approval_input", fake_input),
        ):
            first = circuit_breaker.require_action_confirmation(
                summary="Company: Acme Corp", expected_value="Acme Corp"
            )
            second = circuit_breaker.require_action_confirmation(
                summary="Company: Acme Corp", expected_value="Acme Corp"
            )
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        # One action per approval: the second action re-prompted even
        # though the first had just succeeded — nothing is remembered.
        self.assertEqual(fake_input.call_count, 2)
        self.assertNotEqual(first["approval_id"], second["approval_id"])


class CeilingTests(unittest.TestCase):
    """§6.5: daily/per-provider ceilings are un-raisable source constants."""

    def test_daily_apply_cap_pinned(self):
        self.assertEqual(compliance.DEFAULT_DAILY_APPLY_CAP, 5)

    def test_search_budgets_pinned(self):
        self.assertEqual(
            compliance.DEFAULT_SEARCH_BUDGET,
            {"official": 1000, "scraping": 40, "user": 1000},
        )

    def _search_state_at_budget(self, board, tier):
        state = {"mode": "standard", "risk_acknowledged": True}
        per_day = compliance._counters_for(state)
        per_day["search"][board] = compliance.DEFAULT_SEARCH_BUDGET[tier]
        return state

    def test_env_cannot_raise_search_budget(self):
        board = "greenhouse.example"
        tier = compliance.board_tier(board)
        env = {
            "VETO_SEARCH_BUDGET": "999999",
            "VETO_DAILY_SEARCH_LIMIT": "999999",
            "JOB_MCP_SEARCH_BUDGET": "999999",
        }
        with mock.patch.dict(os.environ, env):
            allowed, reason = compliance.check_search_allowed(
                board, state=self._search_state_at_budget(board, tier)
            )
        self.assertFalse(allowed)
        self.assertIn("budget", reason.lower())

    def test_env_cannot_raise_apply_cap(self):
        state = {}
        per_day = compliance._counters_for(state)
        per_day["applies"] = compliance.DEFAULT_DAILY_APPLY_CAP
        env = {
            "VETO_DAILY_APPLY_CAP": "999",
            "VETO_APPLY_CAP": "999",
            "JOB_MCP_APPLY_CAP": "999",
        }
        with mock.patch.dict(os.environ, env):
            allowed, reason = compliance.check_apply_allowed(state=state)
        self.assertFalse(allowed)

    def test_provider_health_budget_cannot_raise_legal_ceiling(self):
        # provider_health.set_budget is an operational dashboard knob; it
        # must not move the legal per-provider search ceiling enforced by
        # the compliance gate.
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "health.json"
        provider_health.set_budget("greenhouse", 10**9, path=store)
        board = "greenhouse.example"
        tier = compliance.board_tier(board)
        allowed, _ = compliance.check_search_allowed(
            board, state=self._search_state_at_budget(board, tier)
        )
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
