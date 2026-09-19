#!/usr/bin/env python3
"""Epic 4 — calendar handoff: confirmed interview-prep events and follow-up
reminders through connected calendars.

Design (roadmap constraint 02 — humans own consequential actions):

* ``propose_interview_prep`` / ``propose_followup_reminder`` build event
  DRAFTS only. They read application records, they change nothing, they
  touch no calendar.
* ``confirm_and_handoff`` turns drafts into a handoff payload ONLY after
  the human confirms on an interactive terminal
  (``circuit_breaker.require_action_confirmation``): ``confirm=True``
  REQUESTS that confirmation flow — it never asserts it. Without it,
  the drafts are returned with instructions.
* The actual calendar write happens through the user's connected calendar
  (the google-calendar skill) at the wiring layer — this module names the
  destination explicitly and lists exactly what data the event carries, so
  the confirmation the user gives is informed.

Every handoff declares its manifest: what it can do, why it may be
blocked, what data it sends, which actions require confirmation.

Connectivity is checked at the wiring layer, NOT here: this module never
touches a calendar, so it cannot verify one is connected. The edge that
executes the payload (via the google-calendar skill) owns that check.
The manifest therefore does not list "no calendar connected" as a block
reason — every manifest claim must be true of THIS module.

Validation contract (enforced by ``_validate_draft`` on EVERY path —
builder output, hand-constructed drafts, and ``from_dict`` input —
before any payload can be built): start_iso/end_iso are str +
timezone-aware ISO-8601 with start < end; title/description/company/
role/application_id are type-checked str (free-form — empty strings pass
the type check; the handoff gate then treats empty/blank or near-miss
placeholder text ("Unknown company", case-insensitive) in
company/role/title as missing and blocks it unless
allow_placeholders=True); event_type is a closed vocabulary
{"interview_prep", "followup_reminder"}; reminders_min is a list of
non-negative ints (bools and negatives rejected); attendees is a list of
str; and attendees_confirmed is a strict bool — a truthy string like
"yes" is REJECTED (TypeError raised from ``_validate_draft`` /
``from_dict``; on the ``confirm_and_handoff`` path it is dropped and
named, not raised to the caller) instead of silently keeping the
attendee list. The ``confirm`` and ``allow_placeholders`` switches are
strict-bool: only the literal ``True`` takes effect; any other truthy
value fails closed. And ``confirm=True`` is NOT a caller assertion —
it only requests the human confirmation flow; the authorization is the
human typing a per-call random confirmation value on an interactive
terminal (``circuit_breaker.require_action_confirmation``), which
refuses with SystemExit(2) when stdin/stdout are not both TTYs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any

from circuit_breaker import require_action_confirmation
from providers._contract import ConnectorManifest

log = logging.getLogger("veto-mcp.i09.calendar")

CALENDAR_MANIFEST = ConnectorManifest(
    name="calendar_handoff",
    connector_type="calendar",
    what_it_can_do=[
        "Draft interview-prep calendar events from confirmed interview "
        "details (role, company, time, prep checklist).",
        "Draft follow-up reminders tied to application records.",
        "Produce a handoff payload for the wiring layer to write to the "
        "connected calendar — only after the user confirms each handoff "
        "(this module never writes to the calendar itself).",
    ],
    why_it_may_be_blocked=[
        # NOTE: "no calendar is connected" is deliberately NOT listed
        # here. This module never touches a calendar, so it cannot check
        # connectivity — the wiring layer that executes the payload (via
        # the google-calendar skill) owns that check. Every claim in this
        # manifest must be true of THIS module.
        "The interview details are missing a time — a draft is still "
        "built, but handoff waits for the user to set the time.",
        "The user did not confirm the handoff.",
    ],
    what_data_it_sends=[
        "On confirmed handoff only, each event carries ALL of: title, "
        "start/end time, event_type, description, application_id, company, "
        "role, reminder offsets, attendees (explicitly confirmed drafts "
        "only), attendees_confirmed, the attendees_stripped marker when "
        "unconfirmed attendees were removed, and an idempotency_key — "
        "carried in the handoff payload for the wiring layer to write "
        "to the connected calendar service.",
        "Attendees are sent ONLY for drafts with explicit per-draft "
        "confirmation (attendees_confirmed=True); unconfirmed attendee "
        "lists are stripped before the payload is built.",
        "Drafting sends nothing anywhere.",
    ],
    what_requires_confirmation=[
        "Every calendar write: confirm_and_handoff hands off only after "
        "the human types a per-call confirmation value on an interactive "
        "terminal (circuit_breaker.require_action_confirmation). "
        "confirm=True REQUESTS that prompt — it never asserts approval — "
        "and non-interactive callers are refused outright.",
        "Attendee invites are never added without the user naming them "
        "explicitly.",
    ],
    official_interface="Connected calendar via the google-calendar skill",
    interface_basis="User's own calendar grant; Veto never invents events.",
)


# Fields that describe confirmation state, not event identity. Excluded
# from the idempotency hash so confirming a draft does not change its
# key: the key names the EVENT, and the wiring layer treats a re-confirm
# with the same key as an UPDATE when the event content differs (e.g.
# newly confirmed attendees) — dropping only byte-identical re-confirms.
# See the payload ``note`` for the exact dedupe-vs-update rule.
_CONFIRMATION_STATE_FIELDS = ("attendees_confirmed",)


# Event-type vocabulary. Closed on purpose: the wiring layer maps each
# member to a calendar event kind, and a made-up value would either be
# silently dropped there or written wrong. Reject early, here.
_EVENT_TYPE_VOCABULARY = ("interview_prep", "followup_reminder")

# String payload fields: type-checked (must be str) but otherwise
# free-form. Left loose deliberately: empty strings pass the type check —
# the handoff gate (not field validation) is the authority on missing
# content. It treats empty/blank or near-miss placeholder text
# ("unknown company", matched case-insensitively) in company/role/title
# as missing and blocks it unless allow_placeholders is True, so there
# is a named refusal instead of silent data loss from rejecting "".
_STRING_FIELDS = ("title", "description", "application_id", "company", "role")


def _validate_draft_fields(draft: "CalendarDraft") -> None:
    """Field type/value validation for a constructed CalendarDraft.

    Round-1 guarantees preserved on every path:
    - ``attendees_confirmed`` must be a strict bool — a truthy string
      like "yes" RAISES TypeError instead of silently keeping the
      attendee list.
    - ``attendees`` must be a list of str — a bare string like
      "recruiter@example.com" is rejected, not written.
    - ``event_type`` must be in the known vocabulary.
    Wrong types raise TypeError with a clear message; bad values raise
    ValueError.
    """
    for name in ("start_iso", "end_iso"):
        value = getattr(draft, name)
        if not isinstance(value, str):
            raise TypeError(
                f"calendar_handoff: {name} must be str, got "
                f"{type(value).__name__}"
            )
    for name in _STRING_FIELDS:
        value = getattr(draft, name)
        if not isinstance(value, str):
            raise TypeError(
                f"calendar_handoff: {name} must be str, got "
                f"{type(value).__name__}"
            )
    if draft.event_type not in _EVENT_TYPE_VOCABULARY:
        raise ValueError(
            "calendar_handoff: event_type must be one of "
            f"{list(_EVENT_TYPE_VOCABULARY)}, got {draft.event_type!r}"
        )
    reminders = draft.reminders_min
    if not isinstance(reminders, list) or any(
        not isinstance(r, int) or isinstance(r, bool) for r in reminders
    ):
        raise TypeError(
            "calendar_handoff: reminders_min must be a list of ints, got "
            f"{reminders!r}"
        )
    if any(r < 0 for r in reminders):
        raise ValueError(
            "calendar_handoff: reminders_min offsets must be >= 0, got "
            f"{reminders!r}"
        )
    attendees = draft.attendees
    if not isinstance(attendees, list) or any(
        not isinstance(a, str) for a in attendees
    ):
        raise TypeError(
            "calendar_handoff: attendees must be a list of str, got "
            f"{attendees!r}"
        )
    confirmed = draft.attendees_confirmed
    if not isinstance(confirmed, bool):
        raise TypeError(
            "calendar_handoff: attendees_confirmed must be bool, got "
            f"{type(confirmed).__name__} — a truthy string like 'yes' "
            "does NOT count as attendee confirmation"
        )


def _validate_draft(draft: "CalendarDraft") -> None:
    """THE shared gate: every draft, however constructed (builder output,
    hand-constructed object, or ``from_dict`` deserialization), passes the
    SAME field-type checks and the SAME datetime rules
    (``_validate_draft_times``) before any payload can be built. One rule,
    one implementation — there is no second path around it."""
    _validate_draft_fields(draft)
    _validate_draft_times(draft)


def _validate_draft_times(draft: "CalendarDraft") -> None:
    """The single validated gate: every draft, however constructed, passes
    the same datetime rules before a payload can be built. Naive,
    non-string, or unparseable datetimes are rejected and start < end is
    enforced."""
    start = _parse_when(draft.start_iso)
    end = _parse_when(draft.end_iso)
    if start is not None and end is not None and start >= end:
        raise ValueError(
            f"calendar_handoff: draft '{draft.title}': start must be strictly "
            f"before end (start={start.isoformat()}, end={end.isoformat()})"
        )


def _short_repr(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class CalendarDraft:
    """One proposed calendar event. A draft is not a write."""

    title: str
    start_iso: str  # ISO-8601; "" when the time is not known yet
    end_iso: str
    description: str
    event_type: str  # "interview_prep" | "followup_reminder"
    application_id: str = ""
    company: str = ""
    role: str = ""
    reminders_min: list[int] = field(default_factory=lambda: [60, 1440])
    attendees: list[str] = field(default_factory=list)
    # Explicit per-draft opt-in before any attendee may be written to the
    # calendar. The manifest guarantees attendees are never added without
    # the user naming them explicitly; the handoff gate enforces it by
    # stripping attendees from drafts that lack this flag.
    attendees_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalendarDraft":
        """Rebuild a draft from its serialized form (round-trip support).

        Unknown keys are ignored so older/newer serializations degrade
        gracefully. Missing keys fall back to dataclass defaults ONLY for
        fields that have them (application_id, company, role,
        reminders_min, attendees, attendees_confirmed). The five REQUIRED
        fields (title, start_iso, end_iso, description, event_type) have
        no defaults — omitting one raises TypeError at construction; on
        the ``confirm_and_handoff`` path that input is dropped and named,
        never written (fail-closed).

        Validation is strict and centralized: the rebuilt draft passes the
        SAME ``_validate_draft`` gate the handoff path uses for
        hand-constructed drafts — field type checks (start_iso/end_iso
        must be str, title/description/company/role/application_id str,
        event_type in its known vocabulary, reminders_min a list of ints,
        attendees a list of str, attendees_confirmed a strict bool so a
        truthy string like "yes" does NOT count as confirmation), then
        ``_parse_when`` on start/end (naive, non-string, or unparseable
        datetimes rejected) and start < end enforced. Wrong types raise
        TypeError; bad datetimes/values raise ValueError — both with a
        clear message. No dict input can reach a payload without passing
        the full gate.
        """
        if not isinstance(data, dict):
            raise TypeError(
                f"calendar_handoff: from_dict needs a dict, got {type(data).__name__}"
            )
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        draft = cls(**filtered)
        _validate_draft(draft)
        return draft

    @property
    def idempotency_key(self) -> str:
        """Stable content hash naming the draft's EVENT (not the confirm
        attempt), so the wiring layer can dedupe double confirms: the
        same draft always yields the same key.

        Confirmation-state fields (attendees_confirmed) are EXCLUDED from
        the hash: confirming attendees on a draft must not change its key,
        so confirm -> set attendees_confirmed -> re-confirm yields the
        SAME key. The wiring layer must NOT dedupe on the key alone —
        that would silently swallow the confirmed re-confirm (lost
        update: the payload shows attendees included while nothing new
        is written, and retrying keeps producing the same key). Exact
        rule, also stated in the payload ``note``: create when the key
        is unknown; apply as an UPDATE when a known key arrives with
        different event content; drop only when the content is
        byte-identical to what was written.
        """
        canonical = json.dumps(
            {
                k: v
                for k, v in self.to_dict().items()
                if k not in _CONFIRMATION_STATE_FIELDS
            },
            sort_keys=True,
            default=str,
        )
        return "cal-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    @property
    def has_time(self) -> bool:
        return bool(self.start_iso and self.end_iso)


def _parse_when(value: Any) -> datetime | None:
    """Parse an ISO-8601 datetime, fail-closed on anything ambiguous.

    Empty input returns None (the draft simply has no time yet). Naive
    datetimes are REJECTED, not silently stamped as UTC, and unparseable
    values raise instead of falling back to "now" — both raise ValueError
    with a clear message.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            raise ValueError(
                f"calendar_handoff: cannot parse datetime {value!r} — "
                "supply ISO-8601, e.g. '2026-10-01T14:00:00+00:00'"
            ) from None
    if dt.tzinfo is None:
        raise ValueError(
            "calendar_handoff: naive datetime "
            f"'{dt.isoformat()}' rejected — supply a timezone-aware "
            "datetime, e.g. '2026-10-01T14:00:00+00:00'"
        )
    return dt


def propose_interview_prep(
    application: dict[str, Any],
    interview_at: Any = None,
    prep_minutes: int = 45,
) -> CalendarDraft:
    """Draft an interview-prep event. No side effects, no calendar touched.

    ``application`` is a record with company/title/job_id keys.
    ``interview_at`` is the confirmed interview time (ISO string or
    datetime); when unknown the draft is still returned with empty times
    so the user can set them at handoff.
    """
    application = application or {}
    company = str(application.get("company") or "Unknown company")
    role = str(application.get("title") or "Unknown role")
    if prep_minutes < 0:
        raise ValueError(
            f"calendar_handoff: prep_minutes must be >= 0, got {prep_minutes}"
        )
    when = _parse_when(interview_at or application.get("interview_at"))
    if when:
        start = when - timedelta(minutes=prep_minutes)
        if start >= when:
            raise ValueError(
                "calendar_handoff: prep event must start before the interview "
                f"(start={start.isoformat()}, interview={when.isoformat()})"
            )
        start_iso, end_iso = start.isoformat(), when.isoformat()
    else:
        start_iso, end_iso = "", ""
    checklist = "\n".join(
        [
            "Prep checklist:",
            f"• Re-read the tailored resume + cover letter for {role}",
            "• 3 STAR stories mapped to the job requirements",
            "• 2 questions for the interviewer (role + team)",
            "• Logistics: link/location, contact, materials ready",
        ]
    )
    return CalendarDraft(
        title=f"Interview prep — {role} @ {company}",
        start_iso=start_iso,
        end_iso=end_iso,
        description=checklist,
        event_type="interview_prep",
        application_id=str(application.get("job_id") or application.get("id") or ""),
        company=company,
        role=role,
        reminders_min=[60, 1440],
    )


def propose_followup_reminder(
    application: dict[str, Any],
    days_after: int = 7,
    from_date: Any = None,
) -> CalendarDraft:
    """Draft a follow-up reminder. No side effects."""
    application = application or {}
    company = str(application.get("company") or "Unknown company")
    role = str(application.get("title") or "Unknown role")
    if days_after < 0:
        raise ValueError(
            f"calendar_handoff: days_after must be >= 0, got {days_after}"
        )
    base = _parse_when(from_date) or datetime.now(timezone.utc)
    if from_date is not None and base < datetime.now(timezone.utc):
        log.warning(
            "calendar_handoff: from_date %s is in the past — the reminder is "
            "anchored to it anyway; pass an explicit future from_date if "
            "that was not intended",
            base.isoformat(),
        )
    when = base + timedelta(days=days_after)
    return CalendarDraft(
        title=f"Follow up — {role} @ {company}",
        start_iso=when.isoformat(),
        end_iso=(when + timedelta(minutes=15)).isoformat(),
        description=(
            f"Check on the {role} application at {company}. "
            "Draft the follow-up with the gmail channel (draft first, "
            "send only on confirm)."
        ),
        event_type="followup_reminder",
        application_id=str(application.get("job_id") or application.get("id") or ""),
        company=company,
        role=role,
        reminders_min=[60],
    )


PLACEHOLDER_COMPANY = "Unknown company"
PLACEHOLDER_ROLE = "Unknown role"

# Near-miss placeholder tokens, matched case-insensitively after
# stripping. Empty/blank text counts as missing too — the gate treats it
# exactly like the canonical placeholder strings.
_PLACEHOLDER_TOKENS = ("unknown company", "unknown role")


def _is_placeholder_text(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or normalized in _PLACEHOLDER_TOKENS


def _is_placeholder(draft: CalendarDraft) -> bool:
    """True when the draft carries placeholder or missing content in any
    of the fields a real calendar event must have: company, role, or
    title. Blank strings and case-insensitive near-misses ("unknown
    company") block exactly like the canonical placeholders — the
    builders fill PLACEHOLDER_COMPANY/PLACEHOLDER_ROLE, but
    hand-constructed and from_dict inputs can arrive with "" or
    near-miss text, and those must not reach a real event."""
    return (
        _is_placeholder_text(draft.company)
        or _is_placeholder_text(draft.role)
        or _is_placeholder_text(draft.title)
    )


def _render_handoff_summary(events: list[dict[str, Any]], token: str) -> str:
    """Render the human confirmation prompt for a batch of events.

    Lists exactly what is about to be handed off (already
    attendee-stripped) so the typed confirmation is informed, then
    names the per-call random ``token`` the human must type.
    """
    lines = [
        "Calendar handoff — review before confirming",
        f"Events to hand off: {len(events)}",
        "---",
    ]
    for e in events:
        lines.append(
            f"- {e.get('title')} @ {e.get('company')} "
            f"({e.get('event_type')}) "
            f"{e.get('start_iso')} -> {e.get('end_iso')}"
        )
    lines += [
        "---",
        "These events will be handed to your connected calendar.",
        f"To confirm, type: {token}",
    ]
    return "\n".join(lines)


def confirm_and_handoff(
    drafts: list[CalendarDraft | dict[str, Any]] | CalendarDraft | dict[str, Any],
    *,
    confirm: bool = False,
    destination: str = "connected calendar (google-calendar skill)",
    allow_placeholders: bool = False,
) -> dict[str, Any]:
    """Hand confirmed drafts to the calendar — ONLY after the human
    confirms on an interactive terminal.

    ``confirm=True`` REQUESTS the human confirmation flow; it never
    asserts it. After every draft passes the single validated gate
    below, the handoff is authorized exclusively by
    ``circuit_breaker.require_action_confirmation``: the events are
    rendered on the terminal and the human must type a per-call random
    confirmation value. A declined/mismatched/timed-out answer returns
    a refusal payload; a non-interactive caller (stdin/stdout not both
    TTYs) fails closed with SystemExit(2), which the wiring layer
    (server/MCP) catches and converts to a refusal result — the gate's
    own contract, not swallowed here.

    Accepts CalendarDraft objects or their serialized dicts (see
    ``CalendarDraft.from_dict``). EVERY input is normalized through a
    single validated gate before handoff: both CalendarDraft objects and
    dicts pass the same ``_validate_draft`` check (field types —
    attendees_confirmed strict bool, attendees a list of str,
    event_type in its known vocabulary — plus ``_parse_when`` on
    start/end, with naive, non-string, or unparseable datetimes rejected,
    and start < end enforced).

    Anything else — non-draft garbage, wrong field types, invalid
    datetimes, negative reminder offsets, and even a non-iterable
    top-level input (e.g. ``confirm_and_handoff(42)`` or
    ``confirm_and_handoff(None)``, dropped as index 0) — is dropped and
    NAMED in the ``dropped`` list (``{index, reason, input}``) of EVERY
    response: the confirm=False review path, the no-valid-drafts error,
    the other refusal branches, and the success payload. The count is
    ``dropped_count``. The promise "dropped and counted" holds on all
    paths, not just the empty-valid branch.

    ``confirm`` and ``allow_placeholders`` are strict-bool switches: ONLY
    the literal ``True`` takes effect. ``confirm="yes"``, ``confirm=1``,
    and ``confirm=[True]`` all fail closed to the review/refusal path —
    the same doctrine that rejects ``attendees_confirmed="yes"`` — and
    ``allow_placeholders="yes"`` does NOT bypass the placeholder gate.
    Note ``confirm=True`` does not authorize anything by itself: it
    routes to the circuit-breaker prompt, and the typed per-call value
    is the only authorization. No caller can pre-assert it.

    Returns a handoff payload the wiring layer (server.py / cli.py /
    webui.py) executes against the connected calendar. This module never
    performs the write itself: the confirmation gate lives here, the
    transport lives at the edge with the user's own grant. The wiring
    layer owns the connectivity check ("is a calendar connected?") —
    this module cannot make it.
    """
    if isinstance(drafts, (CalendarDraft, dict)):
        drafts = [drafts]
    valid: list[CalendarDraft] = []
    dropped: list[dict[str, Any]] = []

    def _with_dropped(payload: dict[str, Any]) -> dict[str, Any]:
        payload["dropped"] = dropped
        payload["dropped_count"] = len(dropped)
        return payload

    try:
        inputs: list[Any] = list(drafts)
    except TypeError:
        # Non-iterable top-level input (e.g. confirm_and_handoff(42) or
        # confirm_and_handoff(None)): dropped and NAMED as index 0, like
        # every other bad input — the "EVERY input is dropped and named"
        # promise holds here too.
        dropped.append(
            {
                "index": 0,
                "reason": (
                    "calendar_handoff: input is not a CalendarDraft, "
                    "dict, or iterable of drafts (got "
                    f"{type(drafts).__name__})"
                ),
                "input": _short_repr(drafts),
            }
        )
        inputs = []
    for i, d in enumerate(inputs):
        try:
            if isinstance(d, CalendarDraft):
                _validate_draft(d)
                valid.append(d)
            elif isinstance(d, dict):
                valid.append(CalendarDraft.from_dict(d))
            else:
                raise TypeError(
                    "calendar_handoff: input is not a CalendarDraft or dict "
                    f"(got {type(d).__name__})"
                )
        except (TypeError, ValueError) as exc:
            dropped.append(
                {"index": i, "reason": str(exc), "input": _short_repr(d)}
            )

    # Strict-bool gate: ONLY the literal True requests the human
    # confirmation flow. confirm="yes", confirm=1, confirm=[True] all
    # fail closed to the review path — the same doctrine that rejects
    # attendees_confirmed="yes" one function away. Even the literal True
    # asserts nothing: it routes to circuit_breaker below.
    if confirm is not True:
        return _with_dropped(
            {
                "ok": False,
                "handed_off": 0,
                "drafts": [d.to_dict() for d in valid],
                "instructions": (
                    "These are DRAFTS — nothing was written to any calendar. "
                    "Review each draft (title, time, description), set any "
                    "missing times, then call confirm_and_handoff with "
                    "confirm=True: you will be asked to type a confirmation "
                    "value on your terminal before anything is handed to "
                    "your connected calendar."
                    + (
                        f" {len(dropped)} input(s) were dropped and are "
                        "named in 'dropped'."
                        if dropped
                        else ""
                    )
                ),
            }
        )
    if not valid:
        return _with_dropped(
            {
                "ok": False,
                "handed_off": 0,
                "drafts": [],
                "error": "no_valid_drafts",
                "instructions": (
                    "No valid drafts were handed off"
                    + (
                        f" ({len(dropped)} input(s) were dropped: "
                        + "; ".join(
                            f"#{r['index']}: {r['reason']}" for r in dropped
                        )
                        + ")."
                        if dropped
                        else "."
                    )
                    + " Pass CalendarDraft objects or their dict form, then "
                    "confirm again."
                ),
            }
        )
    missing_time = [d.title for d in valid if not d.has_time]
    if missing_time:
        return _with_dropped(
            {
                "ok": False,
                "handed_off": 0,
                "drafts": [d.to_dict() for d in valid],
                "error": "missing_time",
                "instructions": (
                    "These drafts have no time set and were NOT handed off: "
                    + "; ".join(missing_time)
                    + ". Set start_iso/end_iso, then confirm again."
                ),
            }
        )
    # Strict-bool: only the literal True bypasses the placeholder gate —
    # allow_placeholders="yes" fails closed and the gate still applies.
    if allow_placeholders is not True:
        placeholders = [d.title for d in valid if _is_placeholder(d)]
        if placeholders:
            return _with_dropped(
                {
                    "ok": False,
                    "handed_off": 0,
                    "drafts": [d.to_dict() for d in valid],
                    "error": "placeholder_content",
                    "instructions": (
                        "These drafts contain placeholder, blank, or "
                        f"near-miss placeholder content "
                        f"('{PLACEHOLDER_COMPANY}' / '{PLACEHOLDER_ROLE}', "
                        "blank, or case-insensitive variants) in company, "
                        "role, or title and were NOT handed off: "
                        + "; ".join(placeholders) + ". Fill in the real "
                        "company/role/title, or pass allow_placeholders=True "
                        "(the literal True) to override explicitly."
                    ),
                }
            )
    events: list[dict[str, Any]] = []
    stripped_attendees: list[str] = []
    for d in valid:
        event = d.to_dict()
        # Manifest guarantee: attendees are never added without explicit
        # per-draft confirmation — strip unconfirmed attendee lists rather
        # than writing invitations the user did not approve.
        if d.attendees and not d.attendees_confirmed:
            stripped_attendees.append(d.title)
            event["attendees"] = []
            event["attendees_stripped"] = True
        event["idempotency_key"] = d.idempotency_key
        events.append(event)
    # Directive §6.1 (2026-09-14): no caller-asserted confirmation. The
    # human confirmation flow runs HERE, in this module — not deferred
    # to wiring time. confirm=True only got us this far; the handoff is
    # authorized exclusively by the human typing a per-call random value
    # on an interactive terminal. The value is generated fresh per call
    # and never leaves the TTY prompt, so no caller — and no code path
    # wiring this payload to the google-calendar skill — can pre-assert
    # it. Non-TTY fails closed with SystemExit(2) (propagates: the
    # circuit-breaker contract assigns refusal conversion to the
    # server/MCP wiring layer); decline/mismatch/timeout returns a
    # refusal payload below.
    _token = secrets.token_hex(3)
    _gate = require_action_confirmation(
        summary=_render_handoff_summary(events, _token),
        expected_value=_token,
        value_label="confirmation code",
        action_label="calendar handoff",
    )
    if not _gate.get("ok"):
        return _with_dropped(
            {
                "ok": False,
                "handed_off": 0,
                "drafts": [d.to_dict() for d in valid],
                "error": _gate.get("error", "approval_declined"),
                "instructions": (
                    "Calendar handoff refused "
                    f"({_gate.get('error', 'approval_declined')}): nothing "
                    "was written to any calendar. Re-run with confirm=True "
                    "to be prompted again."
                ),
            }
        )
    note = (
        "Wiring layer: create these events through the user's connected "
        "calendar (google-calendar skill) now. Each event carries an "
        "idempotency_key that names the EVENT, not the confirm attempt — "
        "do NOT dedupe on the key alone. Exact dedupe-vs-update rule: "
        "create when the key is unknown; when a known key arrives with "
        "different event content (e.g. attendees newly confirmed and "
        "included), apply it as an UPDATE, not a duplicate to drop "
        "(key-only dedupe would silently swallow the confirmed invite — "
        "a lost update); drop a re-confirm only when its event content "
        "is byte-identical to what was already written. Attendees are "
        "included only when explicitly confirmed per draft "
        "(attendees_confirmed=True); unconfirmed attendee lists were "
        "stripped, never written."
    )
    if stripped_attendees:
        note += " Stripped attendees from: " + "; ".join(stripped_attendees) + "."
    payload = _with_dropped(
        {
            "ok": True,
            "handed_off": len(valid),
            "destination": destination,
            "data_sent_per_event": [
                "title",
                "start/end time",
                "event_type",
                "description",
                "application_id",
                "company",
                "role",
                "reminder offsets",
                "attendees (explicitly confirmed only)",
                "attendees_confirmed",
                "attendees_stripped marker (when stripped)",
                "idempotency_key",
            ],
            "events": events,
            "note": note,
        }
    )
    log.info("Calendar handoff confirmed: %d event(s) to %s", len(valid), destination)
    return payload
