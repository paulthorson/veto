#!/usr/bin/env python3
"""Smart follow-ups for job-apply-mcp.

Turns the stale-application signal (``lifecycle.due_followups``) into
actionable drafts:

* :func:`draft_followup` — pure function: given one application entry and
  the user's profile, pick the right template and render a short, human
  follow-up email. Two templates:

  - ``applied_nudge`` (stage ``applied``, 7+ days, no response): a polite
    nudge that reaffirms interest and adds a one-line value proposition
    drawn **only** from the profile (skills/headline) — never invented.
  - ``interview_checkin`` (stage ``interviewing``, 4+ days since last
    touch): a thank-you plus a light timeline check-in that references
    the interview (and the interviewer by name when the record has one).

* :func:`followups_with_drafts` — load the store via ``lifecycle`` and
  pair every follow-up candidate with its draft. Candidates are the
  lifecycle-due entries **plus** applied-stage applications that have
  been waiting 7+ days with no response (these usually carry no
  ``follow_up_due`` date, so the lifecycle query alone would miss them).

* :func:`send_followup` — the approval gate for actually sending.
  Refuses unless ``confirmed=True`` **and** ``to``/``subject``/``body``
  are all explicitly provided non-empty — and even then, nothing is
  sent without a genuine interactive user approval of the exact draft
  (see :mod:`email_sync` / ``initiatives.i09.channels.registry``).
  A caller-asserted ``confirmed=True`` authorizes nothing on its own.
  On a passing gate it delegates to :mod:`email_sync`'s Gmail send path
  (``hatch_gws_cli gmail +send`` via a connected account); nothing is
  ever sent on the unconfirmed path. The project's Gmail rule stands:
  the exact recipient, subject, and body must be approved by the user
  first — interactively, at the send boundary.

Stdlib only. All functions take explicit arguments (plus small optional
``path``/``today`` overrides) so they are trivially testable offline;
``server.py`` / ``cli.py`` wire them up (see WIRING below and the module
docstring of :mod:`email_sync` for the established pattern).

Wiring contract (the parent adds these; this module edits nothing):

* ``register_tools(mcp)`` — registers the ``draft_followup``,
  ``followups_with_drafts`` and ``send_followup`` MCP tools.
* ``register_cli(sub)`` — adds the ``followups`` CLI command and returns
  the ``{command: handler}`` mapping ``cli.py`` merges into its dispatch
  table. Add ``"followup"`` to ``_PLUGIN_CLI_MODULES`` in ``cli.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import lifecycle

import email_sync

log = logging.getLogger("job-apply-mcp.followup")

BASE_DIR = Path(__file__).resolve().parent
APPLICATIONS_FILE = BASE_DIR / "applications.json"

#: Days with no response before an "applied" application earns a nudge.
APPLIED_NUDGE_DAYS = 7

#: Days since the last interview touch before a check-in is appropriate.
INTERVIEW_CHECKIN_DAYS = 4

#: Contact-email fields we accept from an application entry, in order.
_CONTACT_EMAIL_KEYS = (
    "recruiter_email",
    "contact_email",
    "hiring_manager_email",
)

#: Name fields we accept for the interviewer/contact, in order.
_CONTACT_NAME_KEYS = (
    "interviewer",
    "interviewer_name",
    "contact_name",
    "recruiter_name",
    "hiring_manager",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date | None:
    """Parse an ISO date/datetime string (or date/datetime) to a date.

    Returns None when the value is missing or unparseable — callers treat
    that as "unknown", never as zero.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _stage_events(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Stage-history events, tolerantly normalized."""
    events = entry.get("stage_history") or []
    return [e for e in events if isinstance(e, dict)]


def _days_since_applied(entry: dict[str, Any], today: date) -> int | None:
    """Days since the application was submitted; None when unknown."""
    submitted = _parse_date(entry.get("submitted_at"))
    if submitted is None:
        for event in _stage_events(entry):
            if str(event.get("stage")) == "applied":
                submitted = _parse_date(event.get("at"))
                if submitted is not None:
                    break
    if submitted is None:
        return None
    return max(0, (today - submitted).days)


def _days_since_last_touch(entry: dict[str, Any], today: date) -> int | None:
    """Days since the most recent stage-history event; None when unknown."""
    stamps = [
        _parse_date(e.get("at")) for e in _stage_events(entry)
    ]
    stamps = [s for s in stamps if s is not None]
    if not stamps:
        return _days_since_applied(entry, today)
    return max(0, (today - max(stamps)).days)


def _contact_name(entry: dict[str, Any]) -> str:
    """Best-effort interviewer/contact name from the entry (may be "")."""
    for key in _CONTACT_NAME_KEYS:
        name = str(entry.get(key) or "").strip()
        if name:
            return name
    return ""


def resolve_recipient(entry: dict[str, Any]) -> str:
    """Recipient address for a follow-up, from the entry (may be "").

    Checks ``recruiter_email``, ``contact_email`` and
    ``hiring_manager_email`` in order. Empty means the record carries no
    address — the sender must supply ``--to`` / ``to`` explicitly.
    """
    for key in _CONTACT_EMAIL_KEYS:
        addr = str(entry.get(key) or "").strip()
        if addr:
            return addr
    return ""


def find_entry(
    entries: list[dict[str, Any]], entry_id: str
) -> dict[str, Any]:
    """Locate an entry by ``job_id`` or numeric index.

    Raises:
        KeyError: If no entry matches.
    """
    key = str(entry_id)
    for entry in entries:
        if str(entry.get("job_id")) == key:
            return entry
    try:
        idx = int(key)
    except ValueError:
        idx = None
    if idx is not None and 0 <= idx < len(entries):
        return entries[idx]
    raise KeyError(f"No application found for {entry_id!r}")


def _first_name(full: str) -> str:
    """First token of a name, for greetings."""
    return str(full).split()[0] if str(full).split() else ""


def _signoff(profile: dict[str, Any]) -> str:
    """Signature block: name plus phone when the profile has one."""
    name = str(
        profile.get("full_name") or profile.get("first_name") or "Applicant"
    ).strip()
    phone = str(profile.get("phone") or "").strip()
    return f"{name}\n{phone}" if phone else name


def _value_suffix(profile: dict[str, Any]) -> str:
    """One-line value proposition drawn ONLY from the profile.

    Returns a sentence fragment like
    ``" — my background in Python, Postgres, and distributed systems
    feels like a strong fit"``, or ``""`` when the profile offers
    nothing usable. Never invents skills or experience.
    """
    skills = [
        str(s).strip()
        for s in (profile.get("skills") or [])
        if str(s).strip()
    ][:3]
    if skills:
        return (
            " — my background in " + ", ".join(skills)
            + " feels like a strong fit"
        )
    headline = str(profile.get("headline") or "").strip()
    if headline:
        return f" — my experience ({headline}) feels like a strong fit"
    return ""


def _greeting(entry: dict[str, Any]) -> str:
    """'Hi <FirstName>,' when we know a contact, else 'Hi there,'."""
    first = _first_name(_contact_name(entry))
    return f"Hi {first}," if first else "Hi there,"


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def draft_followup(
    entry: dict[str, Any],
    profile: dict[str, Any],
    today: date | None = None,
) -> dict[str, Any]:
    """Draft a follow-up email for one application. Pure: does not send.

    Template selection is driven by the application stage:

    * ``applied`` (or anything non-terminal without an interview):
      ``applied_nudge`` — a polite nudge reaffirming interest, with a
      one-line value proposition drawn only from ``profile``.
    * ``interviewing``: ``interview_checkin`` — a thank-you plus a light
      timeline check-in referencing the interview (and the interviewer
      by name when the record has one).

    Args:
        entry: A (backfilled) application entry: company, title,
            stage, dates, optional contact fields.
        profile: The user's profile dict (full_name, skills, headline,
            phone, ...). Only these fields feed the value proposition.
        today: Override for "now" (tests).

    Returns:
        ``{"entry_id", "kind", "to", "subject", "body", "tone_notes",
        "days_waiting"}``. ``to`` may be "" when the record carries no
        contact address — the sender must supply it explicitly.

    Raises:
        ValueError: If the stage is terminal (offer/rejected/withdrawn)
            — no follow-up is appropriate there.
    """
    today = today or date.today()
    entry = lifecycle.backfill_entry(dict(entry))
    profile = dict(profile or {})

    stage = str(entry.get("stage") or "applied").lower()
    if stage in lifecycle.TERMINAL_STAGES:
        raise ValueError(
            f"Stage {stage!r} is terminal — no follow-up is appropriate."
        )

    company = str(entry.get("company") or "your team").strip()
    title = str(entry.get("title") or "the role").strip()
    greeting = _greeting(entry)
    signoff = _signoff(profile)
    entry_id = str(entry.get("job_id") or "")

    if stage == "interviewing":
        kind = "interview_checkin"
        days_waiting = _days_since_last_touch(entry, today)
        interviewer = _contact_name(entry)
        ref = (
            f" — I really enjoyed speaking with {_first_name(interviewer)}"
            if interviewer
            else " — I really enjoyed our conversation"
        )
        subject = f"Thank you — {title} interview at {company}"
        body = (
            f"{greeting}\n\n"
            f"Thank you for speaking with me about the {title} role at "
            f"{company}{ref}. I'm very enthusiastic about the opportunity"
            f"{_value_suffix(profile)}, and I wanted to check in on the "
            f"timeline for next steps.\n\n"
            f"Best regards,\n{signoff}"
        )
        tone_notes = (
            "Warm thank-you plus a light timeline check-in; references the "
            "interview (and the interviewer by first name when known). "
            "One ask, no pressure, no repeated follow-ups."
        )
    else:
        kind = "applied_nudge"
        days_waiting = _days_since_applied(entry, today)
        subject = f"Following up — {title} at {company}"
        body = (
            f"{greeting}\n\n"
            f"I wanted to follow up on my application for the {title} role "
            f"at {company}. I'm very enthusiastic about the opportunity"
            f"{_value_suffix(profile)}, and I'd welcome the chance to "
            f"discuss next steps whenever you have a moment.\n\n"
            f"Best regards,\n{signoff}"
        )
        tone_notes = (
            "Polite nudge: reaffirms interest, adds one value line drawn "
            "only from the saved profile (skills/headline — nothing "
            "invented), and leaves the timing to them. Short enough to "
            "read on a phone."
        )

    return {
        "entry_id": entry_id,
        "kind": kind,
        "to": resolve_recipient(entry),
        "subject": subject,
        "body": body,
        "tone_notes": tone_notes,
        "days_waiting": days_waiting,
        "company": entry.get("company"),
        "title": entry.get("title"),
        "stage": stage,
    }


# ---------------------------------------------------------------------------
# Pairing due entries with drafts
# ---------------------------------------------------------------------------


def _needs_followup(entry: dict[str, Any], today: date) -> bool:
    """True when a follow-up is currently appropriate for ``entry``.

    Covers the lifecycle-due condition (``follow_up_due`` reached, stage
    still nudgeable) plus the stale-applied heuristic: stage ``applied``
    with 7+ days and no response — these entries usually carry no
    ``follow_up_due`` date, so the lifecycle query alone would miss them.
    Terminal stages never qualify.
    """
    entry = lifecycle.backfill_entry(entry)
    stage = str(entry.get("stage") or "applied").lower()
    if stage in lifecycle.TERMINAL_STAGES:
        return False
    due = entry.get("follow_up_due")
    if (
        due
        and str(due) <= today.isoformat()
        and stage in lifecycle.NUDGE_STAGES
    ):
        return True
    if stage == "applied":
        days = _days_since_applied(entry, today)
        if days is not None and days >= APPLIED_NUDGE_DAYS:
            return True
    return False


def followups_with_drafts(
    profile: dict[str, Any],
    path: Path | str | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Load applications and pair each follow-up candidate with its draft.

    Args:
        profile: The user's profile dict (feeds each draft's value line).
        path: Applications store (default: the real applications.json).
        today: Override for "now" (tests).

    Returns:
        A list of ``{"entry": entry, "draft": draft}`` dicts, one per
        candidate. Entries whose stage is terminal are never included.
    """
    today = today or date.today()
    store = Path(path) if path else APPLICATIONS_FILE
    entries = lifecycle.load_entries(store)
    # lifecycle.due_followups stays the authority for the due-date rule;
    # dict == compares by value, and backfill_entry is idempotent, so
    # membership testing against its results is exact.
    due = lifecycle.due_followups(entries, today)
    profile = dict(profile or {})
    paired = []
    for entry in entries:
        if not (entry in due or _needs_followup(entry, today)):
            continue
        try:
            draft = draft_followup(entry, profile, today)
        except ValueError:
            continue  # terminal stage slipped through; skip defensively
        paired.append({"entry": entry, "draft": draft})
    return paired


def load_profile(name: str = "") -> dict[str, Any]:
    """Load the saved user profile for follow-up personalization.

    Uses the wizard-saved profile the same way ``server.py`` does;
    falls back to :mod:`profiles`. Returns {} when nothing is saved —
    drafts still render, just without the value line.
    """
    if not name:
        try:
            from server import _load_saved_profile  # lazy: avoid import cycles

            profile = _load_saved_profile()
            if isinstance(profile, dict):
                return profile
        except Exception:  # pragma: no cover - defensive
            log.debug("server profile helper unavailable", exc_info=True)
    try:
        from profiles import load_profile as _profiles_load

        profile = _profiles_load(name)
        return profile if isinstance(profile, dict) else {}
    except Exception:  # pragma: no cover - defensive
        log.debug("profiles.load_profile unavailable", exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Sending (approval gate + delegation to email_sync)
# ---------------------------------------------------------------------------


def _refuse(error: str, instructions: str, **extra: Any) -> dict[str, Any]:
    """Build a refusal result. Nothing was sent."""
    result: dict[str, Any] = {
        "sent": False,
        "error": error,
        "instructions": instructions,
    }
    result.update(extra)
    return result


def send_followup(
    entry_id: str,
    to: str,
    subject: str,
    body: str,
    confirmed: bool = False,
    approval_id: str | None = None,
    expected_value: str | None = None,
    value_label: str = "recipient address",
) -> dict[str, Any]:
    """Send a follow-up email via Gmail — only with a genuine user approval.

    THE REAL GATE is the interactive send boundary in
    :func:`email_sync.send_followup` (via
    ``initiatives.i09.channels.registry``): the user must have approved
    the EXACT to/subject/body at the terminal by typing the per-send
    varying value shown in the display (by default the recipient
    address; a hardcoded "send" fails), or an unconsumed approval
    record for that exact content must be on file.
    A caller-asserted ``confirmed=True`` authorizes NOTHING on its own —
    it is deprecated (kept only so existing callers fail closed with
    instructions instead of a TypeError) and the send is still refused
    without a genuine interactive approval.

    Args:
        entry_id: job_id (or index) of the application, for audit.
        to: Recipient address (required, must contain "@").
        subject: Exact approved subject line (required, single line).
        body: Exact approved message body (required).
        confirmed: DEPRECATED as an authorization signal. Warns; the send
            still requires an interactive user approval.
        approval_id: Approval record from an interactive approval
            (``registry.request_send_approval``), threaded through to the
            send boundary.
        expected_value: Override for the per-send varying value the
            prompt requires. Defaults to the recipient address (shown in
            the display, varies per send).
        value_label: Names the varying value in the prompt without
            echoing it.

    Returns:
        ``{"sent": True, ...}`` on success; ``{"sent": False, "error":
        ..., "instructions": ...}`` on any refusal or send failure.
    """
    if confirmed is not True:
        return _refuse(
            "not_confirmed",
            "Not sent: user confirmation is required. Show the exact "
            "recipient, subject, and body to the user and obtain an "
            "interactive approval (the send prompt asks the user to type "
            "the recipient address exactly as shown for this exact text) "
            "— then call again with confirmed=True. Note: even "
            "confirmed=True cannot send without that interactive approval.",
            entry_id=str(entry_id),
            to=str(to or ""),
            subject=str(subject or ""),
            body=str(body or ""),
        )

    fields = (("to", to), ("subject", subject), ("body", body))
    for label, value in fields:
        if not isinstance(value, str) or not value.strip():
            return _refuse(
                "missing_field",
                f"Not sent: {label!r} must be explicitly provided and "
                "non-empty. Approval requires the exact recipient, "
                "subject, and body.",
                entry_id=str(entry_id),
                missing=label,
            )

    to_s, subject_s, body_s = to.strip(), subject.strip(), body.strip()
    if "@" not in to_s or "\n" in to_s or "\r" in to_s:
        return _refuse(
            "bad_recipient",
            "Not sent: 'to' must be a single valid email address.",
            entry_id=str(entry_id),
        )
    if "\n" in subject_s or "\r" in subject_s:
        return _refuse(
            "bad_subject",
            "Not sent: 'subject' must be a single line (no newlines).",
            entry_id=str(entry_id),
        )

    draft = {
        "application_id": str(entry_id),
        "to": to_s,
        "subject": subject_s,
        "body": body_s,
    }
    try:
        # Attribute access (not a from-import) so tests can patch
        # email_sync.send_followup cleanly.
        # B1: this call site does NOT implement single-use itself — it
        # threads approval_id to email_sync.send_followup, which relies
        # on the registry's guarantee (authorize_send atomically
        # validates+consumes under the cross-process lock;
        # consume_approval is idempotent; the on-disk approval_consumed
        # entry is the cross-process replay evidence). See the call-site
        # comments in email_sync.py; full B1 proof awaits the
        # integration-surfaces unit's registry rework.
        result = email_sync.send_followup(
            draft,
            confirm=confirmed,
            approval_id=approval_id,
            expected_value=expected_value,
            value_label=value_label,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Follow-up send failed: %s", exc)
        return _refuse(
            "send_failed",
            f"Not sent: the Gmail send failed ({exc}).",
            entry_id=str(entry_id),
        )
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("entry_id", str(entry_id))
        return result
    return {"sent": bool(result), "entry_id": str(entry_id)}


# ---------------------------------------------------------------------------
# Wiring: MCP tools + CLI (server.py / cli.py call these; new-file only)
# ---------------------------------------------------------------------------


def register_tools(mcp):  # noqa: ANN001, ANN202 - duck-typed FastMCP
    """Register the follow-up tools on a FastMCP instance."""
    # Capture the module-level cores first: the tool wrappers below reuse
    # the same names, which would otherwise shadow the globals.
    _core_draft = globals()["draft_followup"]
    _core_paired = globals()["followups_with_drafts"]
    _core_send = globals()["send_followup"]
    _core_profile = globals()["load_profile"]
    _core_find = globals()["find_entry"]

    @mcp.tool()
    def draft_followup(  # noqa: F811 - intentional tool wrapper
        entry_id: str, profile_name: str = ""
    ) -> dict:
        """Draft a smart follow-up email for one application. Does NOT send.

        Picks the template from the application stage: stage=applied
        (7+ days, no response) gets a polite nudge reaffirming interest
        with a one-line value proposition drawn ONLY from the saved
        profile (never invented); stage=interviewing (4+ days since last
        touch) gets a thank-you / timeline check-in referencing the
        interview. Uses company/role/interviewer name from the record
        when present.

        Args:
            entry_id: job_id (or numeric index) of the application.
            profile_name: Named profile for the value line (default:
                auto-resolved).
        """
        entries = lifecycle.load_entries(APPLICATIONS_FILE)
        entry = _core_find(entries, entry_id)
        profile = _core_profile(profile_name)
        return _core_draft(entry, profile)

    @mcp.tool()
    def followups_with_drafts(  # noqa: F811 - intentional tool wrapper
        profile_name: str = "",
    ) -> list:
        """Stale applications, each paired with its follow-up draft.

        Covers lifecycle-due entries plus applied-stage applications
        waiting 7+ days with no response. Review each draft, then send
        via send_followup() after the user approves the exact text.

        Args:
            profile_name: Named profile for the value lines (default:
                auto-resolved).
        """
        return _core_paired(_core_profile(profile_name))

    @mcp.tool()
    def send_followup(  # noqa: F811 - intentional tool wrapper
        entry_id: str,
        to: str,
        subject: str,
        body: str,
        confirmed: bool = False,
        approval_id: str | None = None,
    ) -> dict:
        """Send a follow-up email via the connected Gmail account.

        APPROVAL GATE: a caller-asserted ``confirmed=True`` authorizes
        NOTHING on its own. Every send must pass the interactive send
        boundary: the user approves the exact recipient, subject, and
        body at the terminal (typing the recipient address exactly as
        shown — a hardcoded "send" fails), which mints a single-use
        approval record; pass its ``approval_id`` here, or call with an
        approval already on file. Without it the send is refused (a
        non-interactive agent cannot self-approve). Nothing is sent on
        the unconfirmed path — the exact text is returned for review.

        Args:
            entry_id: job_id (or numeric index) of the application.
            to: Exact approved recipient address.
            subject: Exact approved subject line.
            body: Exact approved message body.
            confirmed: DEPRECATED as an authorization signal; the send
                still requires a genuine interactive approval.
            approval_id: Approval record from an interactive approval of
                this exact draft.
        """
        return _core_send(
            entry_id, to, subject, body, confirmed=confirmed,
            approval_id=approval_id,
        )

    return mcp


def _cli_followups(args) -> int:  # noqa: ANN001 - argparse namespace
    """Handler for the ``followups`` command."""
    profile = load_profile(getattr(args, "profile", "") or "")
    store = APPLICATIONS_FILE

    if args.send:
        missing = [
            flag
            for flag, val in (
                ("--to", args.to),
                ("--subject", args.subject),
                ("--body", args.body),
            )
            if not (val or "").strip()
        ]
        if missing:
            print(
                f"Error: --send requires {' '.join(missing)} "
                "(explicit, non-empty)."
            )
            return 2
        result = send_followup(
            args.send, args.to, args.subject, args.body,
            confirmed=args.confirm,
        )
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0 if result.get("sent") else 1
        if result.get("sent"):
            print(f"Sent to {result.get('to')}: {result.get('subject')}")
            return 0
        print(f"Not sent ({result.get('error')}): {result.get('instructions')}")
        if result.get("error") == "not_confirmed":
            print("\n--- Exact text awaiting approval ---")
            print(f"To: {result.get('to')}")
            print(f"Subject: {result.get('subject')}")
            print(f"\n{result.get('body')}")
            print(
                "\nRe-run from an interactive terminal to approve this exact "
                "text at the send prompt (type the recipient address "
                "exactly as shown). A --confirm flag alone cannot "
                "authorize a send."
            )
        return 1

    if args.draft:
        try:
            entry = find_entry(lifecycle.load_entries(store), args.draft)
        except KeyError as exc:
            print(f"Error: {exc}")
            return 1
        try:
            draft = draft_followup(entry, profile)
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1
        if getattr(args, "json", False):
            print(json.dumps(draft, indent=2, ensure_ascii=False))
            return 0
        print(f"To: {draft['to'] or '(no address on file — pass --to when sending)'}")
        print(f"Subject: {draft['subject']}")
        print(f"\n{draft['body']}")
        print(f"\n[tone] {draft['tone_notes']}")
        return 0

    # Default: list candidates with one-line draft previews.
    paired = followups_with_drafts(profile, path=store)
    if getattr(args, "json", False):
        print(json.dumps(paired, indent=2, ensure_ascii=False))
        return 0
    if not paired:
        print("No follow-ups due. The pipeline is quiet — for now.")
        return 0
    print(f"{len(paired)} follow-up(s) due:\n")
    for item in paired:
        entry, draft = item["entry"], item["draft"]
        ident = entry.get("job_id") or "?"
        waiting = draft.get("days_waiting")
        age = f"{waiting}d waiting" if waiting is not None else "age unknown"
        print(
            f"  [{ident}] {draft.get('company')} — {draft.get('title')} "
            f"({draft.get('stage')}, {age})"
        )
        print(f"      subject: {draft['subject']}")
        print(
            f"      draft:   cli.py followups --draft {ident} | "
            f"send: cli.py followups --send {ident} --to ADDR "
            f"--subject S --body B --confirm"
        )
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``followups`` subcommand; return {command: handler}.

    The parent merges the returned dict into its own dispatch table
    (cli.py-style). Usage::

        followups                      # list due follow-ups + previews
        followups --draft <id>         # show the full draft for one entry
        followups --send <id> --to <addr> --subject <s> --body <b>
            --confirm                  # send (all of --to/--subject/
                                       # --body/--confirm required)
    """
    parser = subparsers.add_parser(
        "followups",
        help="List stale applications and draft/send follow-up emails.",
    )
    parser.add_argument(
        "--draft", metavar="ENTRY_ID", default="",
        help="Show the full follow-up draft for one application.",
    )
    parser.add_argument(
        "--send", metavar="ENTRY_ID", default="",
        help="Send a follow-up for one application (needs --to, "
             "--subject, --body and --confirm).",
    )
    parser.add_argument(
        "--to", default="",
        help="Recipient address (required with --send).",
    )
    parser.add_argument(
        "--subject", default="",
        help="Exact subject line (required with --send).",
    )
    parser.add_argument(
        "--body", default="",
        help="Exact message body (required with --send).",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="DEPRECATED as an authorization signal: a caller-asserted "
        "flag is not proof of approval; sending still requires typing "
        "the recipient address exactly as shown at an interactive "
        "terminal (required with --send to attempt the send).",
    )
    parser.add_argument(
        "--profile", default="",
        help="Named profile for the value line (default: auto-resolved).",
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    return {"followups": _cli_followups}
