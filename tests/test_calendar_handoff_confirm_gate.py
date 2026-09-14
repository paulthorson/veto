#!/usr/bin/env python3
"""Tests for directive §6.1 (2026-09-14): no caller-asserted confirmation.

``confirm_and_handoff``'s ``confirm=True`` no longer authorizes a
handoff by itself — it only REQUESTS the human confirmation flow. The
authorization is the human typing a per-call random confirmation value
on an interactive terminal via
``circuit_breaker.require_action_confirmation``. These tests pin that
contract by stubbing the gate (never touching a real TTY):

- review path (``confirm=False`` / non-literal-True) never invokes the
  gate and never hands off;
- ``confirm=True`` + gate approval -> handoff payload, with a fresh
  token per call;
- ``confirm=True`` + gate decline/mismatch -> refusal payload, nothing
  handed off;
- ``confirm=True`` + non-TTY (SystemExit(2) from the gate) propagates;
- the typed value varies per call, so no caller can pre-assert it.
"""

import unittest

from initiatives.i09 import calendar_handoff as ch


def _draft():
    return ch.propose_followup_reminder(
        {"company": "Acme", "title": "Eng", "job_id": "gh:abc"},
        from_date="2026-10-01T00:00:00+00:00",
    )


class ConfirmGateTests(unittest.TestCase):
    def setUp(self):
        self._orig = ch.require_action_confirmation
        self.calls = []

    def tearDown(self):
        ch.require_action_confirmation = self._orig

    def _stub_gate(self, result=None, exc=None):
        def fake(**kwargs):
            self.calls.append(kwargs)
            if exc is not None:
                raise exc
            return result

        ch.require_action_confirmation = fake

    def test_review_path_never_prompts(self):
        self._stub_gate(result={"ok": True, "approved": True})
        out = ch.confirm_and_handoff(_draft())
        self.assertFalse(out["ok"])
        self.assertEqual(out["handed_off"], 0)
        self.assertEqual(self.calls, [])

    def test_non_literal_true_never_prompts(self):
        def boom(**kwargs):  # pragma: no cover - must never run
            raise AssertionError("gate must not be invoked")

        ch.require_action_confirmation = boom
        for bad in ("yes", 1, [True]):
            out = ch.confirm_and_handoff(_draft(), confirm=bad)
            self.assertFalse(out["ok"])
            self.assertEqual(out["handed_off"], 0)

    def test_confirm_true_without_gate_approval_never_hands_off(self):
        # Even the literal True authorizes nothing by itself: a
        # declined gate is a refusal, not a handoff.
        self._stub_gate(result={"ok": False, "error": "approval_declined"})
        out = ch.confirm_and_handoff(_draft(), confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["handed_off"], 0)
        self.assertEqual(out["error"], "approval_declined")
        self.assertIn("dropped_count", out)

    def test_confirm_true_with_approval_hands_off(self):
        self._stub_gate(result={"ok": True, "approved": True})
        out = ch.confirm_and_handoff(_draft(), confirm=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["handed_off"], 1)
        self.assertEqual(len(self.calls), 1)
        token = self.calls[0]["expected_value"]
        self.assertTrue(token)  # never empty: fails closed otherwise
        self.assertIn("Acme", self.calls[0]["summary"])
        self.assertIn(token, self.calls[0]["summary"])

    def test_confirmation_value_varies_per_call(self):
        self._stub_gate(result={"ok": True, "approved": True})
        ch.confirm_and_handoff(_draft(), confirm=True)
        ch.confirm_and_handoff(_draft(), confirm=True)
        tokens = [c["expected_value"] for c in self.calls]
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1])

    def test_non_tty_refusal_propagates(self):
        # circuit_breaker refuses non-interactive callers with
        # SystemExit(2); the module lets it propagate per the gate's
        # contract (server/MCP wiring converts it to a refusal).
        self._stub_gate(exc=SystemExit(2))
        with self.assertRaises(SystemExit) as ctx:
            ch.confirm_and_handoff(_draft(), confirm=True)
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
