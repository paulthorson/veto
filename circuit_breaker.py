#!/usr/bin/env python3
"""Legal-hardening commit 8 (spec section 6): non-programmatic per-action
circuit breakers.

THE GOVERNING APPROVAL AUTHORITY is
``initiatives.i09.channels.registry`` — nonce + content-hash + TTL +
single-use approvals in the HMAC-signed consent log. This module mints
NO approvals of its own: it is a thin per-action facade ON TOP of the
registry, adding the spec §6.2 varying typed value and the spec §6.3
TTY-or-exit-nonzero CLI gate. There is exactly ONE approval path; the
registry governs it, this module documents and wires it.

Mechanism (docs/legal-hardening-plan.md §10.3), CLI, step by step:

1. The exact action summary is printed to stdout — for an application:
   company name, job title, every field value as filled, destination
   URL (see ``render_application_summary``).
2. ``sys.stdin.isatty()`` AND ``sys.stdout.isatty()`` must BOTH hold. If
   either is false, the action is refused and the process exits nonzero
   (``SystemExit(2)``). There is NO environment variable that overrides
   this — this module does not read the environment at all. There is NO
   CI escape hatch. There are no ``--yes`` / ``--confirm`` /
   ``--non-interactive`` flags on this path.
3. On a TTY, the prompt is::

       Type the company name exactly as shown above to submit this
       application, anything else to abort:

   (``value_label`` / ``action_label`` vary the nouns for other action
   kinds.) The expected value is the per-action varying string rendered
   in the summary — a DIFFERENT string for every application. The prompt
   NEVER echoes the expected value: the user must read it from the
   summary, not copy it from the prompt.
4. The typed answer is compared ONLY against the expected value, exact
   match (surrounding whitespace ignored). A hardcoded "yes", "y",
   "ok", or "send" FAILS. On mismatch the action is refused, nothing
   proceeds, and no retry state is remembered — each attempt re-renders
   the summary.
5. On exact match the registry mints a single-use, TTL-bound approval
   bound by content-hash to the exact summary bytes shown; this module
   consumes it immediately (it authorized exactly this one action, and
   is spent the moment it is used). The next application needs a fresh
   prompt and a fresh typed value.

There is no batch confirm. No confirm-all. No remembered confirmation.
No session-level approval spanning multiple applications.

Wired call sites (the breaker guards what REMAINS after commit 6
deleted the submit paths — it does not invent an application-submit
path):

* ``server._apply_via_browser_and_record`` — Phase-2 fill-only browser
  flow: after the form is filled, the gate runs with the application
  summary and the company name as the varying value; only on approval
  is the filled form handed back for the user's own submit click.
* Email/message sends — via the registry's ``authorize_send`` /
  ``request_send_approval``, now requiring the per-send varying value
  (``email_sync.send_followup`` derives it from the recipient address
  shown in the draft display; the email-scan stage-update approval uses
  the update count shown in its display).

Ceilings (spec §6.5): applications-per-day and per-provider request
ceilings are hard code constants — ``compliance.DEFAULT_DAILY_APPLY_CAP``
and ``compliance.DEFAULT_SEARCH_BUDGET``. No config file, environment
variable, or CLI flag can raise them; raising them requires editing
source. (Commit-12 tests assert this; ``tests/test_circuit_breaker.py``
pins the behavior here too.)

Web UI: plan §10.3 describes a modal whose typed value is checked in a
LOCAL UI event handler, with the server-side action endpoint requiring
the single-use approval record minted only by that human interaction.
That modal is NOT built in this commit: no web-UI action currently
reaches a third party without a registry approval minted on a real
terminal (the follow-up send path fails closed without one; the other
gated web-UI actions stage locally or record locally). When a web-UI
outbound action exists, its modal must satisfy §10.3 steps 2-5 above —
no API endpoint may accept a pre-computed confirmation.

Stdlib only.
"""

from __future__ import annotations

import sys
from typing import Any

from initiatives.i09.channels import registry as _registry

#: Channel name bound into approval records minted for the generic
#: per-action gate (as opposed to "gmail" / "email-scan" for sends).
ACTION_CHANNEL = "application"

#: Refusal text printed to stderr when there is no usable terminal.
#: Deliberately terse: the caller (CLI) already explains the mechanism.
_NON_TTY_REFUSAL = (
    "REFUSED: this action requires a human at an interactive terminal "
    "(stdin and stdout must both be TTYs). There is no environment "
    "variable, flag, or config that overrides this."
)


def render_application_summary(
    *,
    company: str,
    title: str,
    fields_filled: dict[str, Any] | None = None,
    destination_url: str = "",
    board: str = "",
    extra_lines: tuple[str, ...] | list[str] = (),
) -> str:
    """Build the exact application summary the gate shows the user.

    Plan §10.3 step 1: company name, job title, every field value as
    filled, destination URL. The company name is rendered EXACTLY as the
    caller passes it — the gate's ``expected_value`` must be the same
    string, since the user types what is shown here.
    """
    lines = [
        f"Company:     {company}",
        f"Job title:   {title}",
    ]
    if board:
        lines.append(f"Board:       {board}")
    if destination_url:
        lines.append(f"Destination: {destination_url}")
    lines.append("--- fields as filled ---")
    filled = fields_filled or {}
    if filled:
        for key in sorted(filled):
            lines.append(f"{key}: {filled[key]}")
    else:
        lines.append("(no field values captured)")
    for extra in extra_lines or ():
        lines.append(str(extra))
    return "\n".join(lines)


def require_action_confirmation(
    *,
    summary: str,
    expected_value: str,
    value_label: str = "company name",
    action_label: str = "application",
    channel: str = ACTION_CHANNEL,
) -> dict[str, Any]:
    """Run the per-action circuit breaker; return the approval result.

    Renders ``summary`` on the terminal and requires the user to type
    ``expected_value`` exactly as shown (see module docstring for the
    exact prompt shape). On match, returns
    ``{"ok": True, "approved": True, "approval_id": ...}`` — the
    single-use approval is already consumed, so the id authorizes
    nothing further.

    Non-TTY (spec §6.3): prints the refusal to stderr and raises
    ``SystemExit(2)`` — the action refuses AND the process exits
    nonzero. Server/MCP callers catch this and convert it to a refusal
    result (a refused action must not kill the server); CLI callers let
    it propagate. There is no environment override, no CI escape hatch,
    and no flag that bypasses this: this module does not read the
    environment at all.

    Decline / mismatch / timeout / abort return
    ``{"ok": False, "error": ...}`` without raising — the caller may
    re-render and retry; nothing is remembered between attempts.

    A missing or empty ``expected_value`` fails closed
    (``missing_expected_value``): the prompt never degrades to a fixed
    string.
    """
    res = _registry.request_action_approval(
        summary=summary,
        expected_value=expected_value,
        value_label=value_label,
        action_label=action_label,
        channel=channel,
    )
    if res.get("ok"):
        # Single-use: the approval authorized exactly this one action;
        # consume it the moment it is used so the returned id can never
        # authorize anything else. consume_approval is idempotent.
        _registry.consume_approval(res["approval_id"], channel=channel)
        return {
            "ok": True,
            "approved": True,
            "approval_id": res["approval_id"],
            "content_hash": res.get("content_hash"),
            "channel": res.get("channel"),
        }
    if res.get("error") == "not_interactive":
        # Spec §6.3: refuse AND exit nonzero. No env override, no CI
        # escape hatch, no --yes flag — the check above depends on
        # nothing but the TTYs themselves.
        print(_NON_TTY_REFUSAL, file=sys.stderr)
        raise SystemExit(2)
    return res
