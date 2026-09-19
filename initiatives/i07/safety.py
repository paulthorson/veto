#!/usr/bin/env python3
"""Safety controls for mentor matching & warm paths (Initiative 07, epic 6).

* **Block** — a blocked actor never appears in matches/previews and cannot
  start or answer handshakes with the blocker. Pending handshakes between
  the pair are auto-withdrawn.
* **Report** — abuse reports with a fixed category; 2+ distinct reporters
  mark an actor ``pending_human_review`` (hidden from discovery, pending
  handshakes withdrawn, audit entry written). There is no auto-takedown:
  the quarantine holds until the real-party pilot human gate — the operator +
  an independent reviewer — clears it via ``clear_quarantine``.
  Reporter ids are caller-asserted in the local-first deployment (see
  CONTRACTS §2 trust boundary), so two reports are a tripwire for human
  review, not a verdict.
* **Rate limits** — token-bucket limiter per (actor, action), enforced on
  ``report``, ``block``, and ``clear_quarantine``. Buckets are in-memory
  per process and do NOT survive CLI re-invocation (documented honestly
  in :func:`check_rate_limit`); per-action limits for other actions live
  in their owning modules.
* **Deletion** — full erase of a mentor card, handshake payloads, session
  notes, and reports on request. Notes are scrubbed BEFORE handshakes are
  tombstoned (tombstones carry no mentor_id, so scrubbing after would find
  nothing). Free-text PII in the consent audit trail is scrubbed on
  deletion. Block entries keep the id only.
* **Corrupt state fails closed** — a corrupt ``safety.json`` raises
  :class:`SafetyStateError` with a loud log line naming the file and the
  parse error, instead of silently returning an empty state. All safety
  reads treat unreadable state as DENY (never permissive); writes against
  unreadable state refuse entirely so an empty state never overwrites the
  real one. ``_load_safety`` is structurally fail-closed: its contract is
  return-a-validated-state or raise ``SafetyStateError`` — a catch-all
  backstop converts EVERY other failure class (OSError subclasses,
  ``RecursionError`` from nested JSON, ...) into ``SafetyStateError``, so
  no corruption class can escape as a native exception the discovery
  choke point would swallow (fail open).
* **Segregation** — :func:`segregation_check` fails loudly if mentorship
  data ever cross-references job-application data. Mentorship must never
  influence application decisions.

All state lives under this package directory (``safety.json``,
``reports.jsonl``); paths are reassignable for tests. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.i07.safety")

BASE_DIR = Path(__file__).resolve().parent

#: Block list + quarantine state. Reassign in tests.
SAFETY_FILE = BASE_DIR / "safety.json"

#: Abuse reports, append-only. Reassign in tests.
REPORTS_FILE = BASE_DIR / "reports.jsonl"

#: Fixed report categories — free text is not a category.
REPORT_CATEGORIES = (
    "spam",
    "harassment",
    "inappropriate_contact",
    "misrepresentation",
    "other",
)

#: Distinct reporters required before an actor is quarantined.
QUARANTINE_THRESHOLD = 2

#: Token-bucket settings per safety-owned action: (capacity, refill_per_second).
#:
#: Only actions enforced by safety.py appear here. Per-action limits for
#: consent (request_introduction), consent (mentor_respond), session_kit
#: (reveal_contact), and warm_path (draft_outreach) are owned by those
#: modules — listing them here was dead config (zero enforcement points)
#: and has been removed rather than left as false advertising.
DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
    "report": (10, 10 / 86400),  # 10 reports per rolling 24h
    "block": (30, 30 / 86400),  # 30 blocks per rolling 24h
    "clear_quarantine": (10, 10 / 86400),  # 10 quarantine clears per rolling 24h
}

#: In-memory token buckets: (actor, action) -> {"tokens": float, "at": float}.
_BUCKETS: dict[tuple[str, str], dict[str, float]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SafetyStateError(RuntimeError):
    """safety.json (or reports.jsonl) is present but corrupt and could not
    be read.

    Raising (instead of silently returning an empty state) is the
    fail-closed path: all safety reads treat unreadable state as deny,
    and no write may proceed against it. Only a human — the operator — clears
    or repairs the underlying file.
    """


# ---------------------------------------------------------------------------
# Block / quarantine state
# ---------------------------------------------------------------------------


def _load_safety() -> dict[str, Any]:
    """Load the block/quarantine state.

    A missing file is a fresh install and yields an empty state. A corrupt
    file raises :class:`SafetyStateError` LOUDLY (log names the file and
    the parse error) — it is never silently replaced with an empty state,
    which would delete every block and quarantine in the system.

    Corruption classes covered (fail closed, never fail open):
    invalid UTF-8 bytes, unparseable JSON, a non-object top level, and
    wrong-typed ``blocked``/``quarantined`` values or entries (e.g.
    ``{"blocked": null}``) — the last one is not a parse failure, so
    without explicit validation it would surface later as an
    ``AttributeError`` in a caller that swallows non-``SafetyStateError``
    exceptions and returns an empty exclusion set.

    STRUCTURAL GUARANTEE (fail closed, never fail open): this function's
    contract is binary — it returns a validated state, or raises
    :class:`SafetyStateError`. There is no third outcome. The entire read
    + parse sequence below runs under a catch-all backstop (the final
    ``except Exception`` arm), so EVERY failure class — invalid UTF-8
    bytes, unparseable JSON, a path that is a directory, permission
    errors, ``RecursionError`` from pathologically nested JSON, anything
    else the OS or the parser can throw — becomes ``SafetyStateError``
    with a loud log line naming the file and the error. Enumerating
    corruption classes is not enough: the discovery choke point in
    mentors.py swallows any non-``SafetyStateError`` via
    ``except Exception -> return set()`` (fail OPEN), so a single residual
    class escaping as its native exception would silently surface
    blocked/quarantined mentors in discovery — reproducing the original
    Rule 1 veto's harm (CONTRACTS §9: "Unreadable state is deny").
    """
    # The entire read + parse sequence is under this one try. The final
    # `except Exception` arm is the structural backstop; the specific
    # arms above it keep their loud, case-specific log lines.
    try:
        try:
            raw = SAFETY_FILE.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"blocked": {}, "quarantined": {}}
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error(
            "SAFETY STATE CORRUPT: file=%s parse_error=%s — failing closed; "
            "all safety reads are deny until a human repairs the file",
            SAFETY_FILE,
            exc,
        )
        raise SafetyStateError(
            f"safety state file {SAFETY_FILE} is corrupt ({exc}); "
            "failing closed — no safety reads or writes will proceed"
        ) from exc
    except UnicodeDecodeError as exc:
        # Invalid UTF-8 bytes: read_text raises UnicodeDecodeError, NOT
        # json.JSONDecodeError, so this needs its own except arm.
        log.error(
            "SAFETY STATE CORRUPT: file=%s not valid UTF-8 (%s) — failing closed; "
            "all safety reads are deny until a human repairs the file",
            SAFETY_FILE,
            exc,
        )
        raise SafetyStateError(
            f"safety state file {SAFETY_FILE} is not valid UTF-8 ({exc}); "
            "failing closed — no safety reads or writes will proceed"
        ) from exc
    except SafetyStateError:
        raise  # already fail-closed; never wrap it in another one
    except Exception as exc:
        # THE structural backstop: any failure class the OS or the parser
        # can throw — IsADirectoryError, PermissionError, RecursionError
        # from pathologically nested JSON, anything future — becomes
        # SafetyStateError with a loud log line. Enumerating cases is what
        # let residual classes escape as native exceptions that the
        # discovery choke point swallows (fail open); this arm closes the
        # whole class of holes at once.
        log.error(
            "SAFETY STATE UNREADABLE: file=%s error_type=%s error=%s — "
            "failing closed; all safety reads are deny until a human "
            "repairs the file",
            SAFETY_FILE,
            type(exc).__name__,
            exc,
        )
        raise SafetyStateError(
            f"safety state file {SAFETY_FILE} is unreadable "
            f"({type(exc).__name__}: {exc}); failing closed — no safety "
            "reads or writes will proceed"
        ) from exc
    if not isinstance(data, dict):
        log.error(
            "SAFETY STATE CORRUPT: file=%s top-level is %s, not an object — failing closed",
            SAFETY_FILE,
            type(data).__name__,
        )
        raise SafetyStateError(
            f"safety state file {SAFETY_FILE} is corrupt "
            "(top-level is not an object); failing closed"
        )
    _validate_safety_state(data)
    data.setdefault("blocked", {})
    data.setdefault("quarantined", {})
    return data


def _validate_safety_state(data: dict[str, Any]) -> None:
    """Reject wrong-typed block/quarantine values and entries.

    Cheap shape validation: ``blocked`` must be
    ``{blocker_id: {blocked_id: {...}}}`` and ``quarantined`` must be
    ``{actor_id: {...record...}}``. JSON with valid syntax but a null or
    wrong-typed value (``{"blocked": null}``) would otherwise sail past
    ``setdefault`` — which never replaces an EXISTING key — and blow up
    downstream as ``AttributeError``, an exception class the discovery
    choke point treats as "no exclusions" (fail OPEN). Raising
    :class:`SafetyStateError` here keeps every corruption class on the
    fail-closed path.
    """
    blocked = data.get("blocked", {})
    quarantined = data.get("quarantined", {})
    if not isinstance(blocked, dict):
        _corrupt_shape("blocked", blocked)
    if not isinstance(quarantined, dict):
        _corrupt_shape("quarantined", quarantined)
    for blocker, entries in blocked.items():
        if not isinstance(entries, dict):
            _corrupt_shape(f"blocked[{blocker!r}]", entries)
        for blocked_id, entry in entries.items():
            if not isinstance(entry, dict):
                _corrupt_shape(f"blocked[{blocker!r}][{blocked_id!r}]", entry)
    for actor, record in quarantined.items():
        if not isinstance(record, dict):
            _corrupt_shape(f"quarantined[{actor!r}]", record)


def _corrupt_shape(where: str, value: Any) -> None:
    log.error(
        "SAFETY STATE CORRUPT: file=%s %s has wrong type %s — failing closed",
        SAFETY_FILE,
        where,
        type(value).__name__,
    )
    raise SafetyStateError(
        f"safety state file {SAFETY_FILE} is corrupt "
        f"({where} is {type(value).__name__}, not an object); failing closed"
    )


def _save_safety(state: dict[str, Any]) -> None:
    SAFETY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SAFETY_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def block_actor(blocker_id: str, blocked_id: str, *, reason: str = "") -> dict[str, Any]:
    """Block ``blocked_id`` on behalf of ``blocker_id``.

    The blocked actor disappears from the blocker's discovery results and
    can neither start nor answer handshakes with the blocker. Pending
    handshakes between the pair are withdrawn immediately (recorded as
    system withdrawals in the consent audit trail).
    """
    if not str(blocker_id or "").strip() or not str(blocked_id or "").strip():
        return {"ok": False, "error": "blocker_id and blocked_id are required"}
    if blocker_id == blocked_id:
        return {"ok": False, "error": "cannot block yourself"}
    if not check_rate_limit(blocker_id, "block"):
        return {"ok": False, "error": "rate limit: too many blocks; slow down"}
    state = _load_safety()  # raises SafetyStateError on corruption: fail closed
    state["blocked"].setdefault(str(blocker_id), {})
    state["blocked"][str(blocker_id)][str(blocked_id)] = {
        "at": _now(),
        "reason": str(reason or "").strip(),
    }
    _save_safety(state)
    withdrawn = _withdraw_pair_handshakes(
        blocker_id, blocked_id, reason=f"block by {blocker_id}"
    )
    return {"ok": True, "blocked": blocked_id, "withdrawn_handshakes": withdrawn}


def unblock_actor(blocker_id: str, blocked_id: str) -> dict[str, Any]:
    state = _load_safety()
    mine = state["blocked"].get(str(blocker_id), {})
    if str(blocked_id) not in mine:
        return {"ok": False, "error": f"{blocked_id!r} is not blocked by {blocker_id!r}"}
    del mine[str(blocked_id)]
    _save_safety(state)
    return {"ok": True, "unblocked": blocked_id}


def is_blocked(blocker_id: str, blocked_id: str) -> bool:
    """True if ``blocker_id`` blocked ``blocked_id``.

    Fail closed: if the safety state is unreadable (corrupt), this returns
    True (deny) and logs loudly — never permissive.
    """
    try:
        state = _load_safety()
    except SafetyStateError:
        log.error(
            "is_blocked: safety state unreadable — failing CLOSED (deny)"
        )
        return True
    return str(blocked_id) in state["blocked"].get(str(blocker_id), {})


def is_quarantined(actor_id: str) -> bool:
    """True if ``actor_id`` is quarantined (including pending_human_review).

    Fail closed: if the safety state is unreadable (corrupt), this returns
    True (deny) and logs loudly — never permissive.
    """
    try:
        state = _load_safety()
    except SafetyStateError:
        log.error(
            "is_quarantined: safety state unreadable — failing CLOSED (deny)"
        )
        return True
    return str(actor_id) in state.get("quarantined", {})


def quarantine_status(actor_id: str) -> dict[str, Any]:
    """Return the quarantine record for ``actor_id``, if any.

    Surfaces the ``pending_human_review`` state explicitly so callers and
    operators can see the hold, its reporters, and its review gate.
    Returns ``{"quarantined": False}`` when the actor is not quarantined.
    """
    try:
        state = _load_safety()
    except SafetyStateError:
        return {"quarantined": True, "status": "state_unreadable"}
    record = state.get("quarantined", {}).get(str(actor_id))
    if record is None:
        return {"quarantined": False}
    return {"quarantined": True, **record}


def consent_block_check(mentee_id: str, mentor_id: str) -> dict[str, Any]:
    """Refuse a consent interaction if either party blocked the other or is quarantined.

    Fail closed: if the safety state is unreadable (corrupt), this refuses
    the interaction and says so explicitly.
    """
    try:
        _load_safety()
    except SafetyStateError:
        log.error(
            "consent_block_check: safety state unreadable — failing CLOSED (refuse)"
        )
        return {
            "blocked": True,
            "error": (
                "introduction unavailable: safety state is unreadable "
                "(fail-closed); a human must repair safety.json"
            ),
        }
    if is_blocked(mentee_id, mentor_id) or is_blocked(mentor_id, mentee_id):
        return {
            "blocked": True,
            "error": "introduction unavailable: a block exists between these parties",
        }
    if is_quarantined(mentor_id):
        return {
            "blocked": True,
            "error": "introduction unavailable: this mentor is under safety review",
        }
    if is_quarantined(mentee_id):
        return {
            "blocked": True,
            "error": "introduction unavailable: this account is under safety review",
        }
    return {"blocked": False}


def discovery_exclusions(blocker_id: str) -> set[str]:
    """Mentor ids that must never surface in ``blocker_id``'s discovery.

    The union of actors the user blocked and every quarantined actor.
    Pass to ``mentors.matchmake(..., exclude_mentor_ids=...)``.

    Fail closed: if the safety state is unreadable (corrupt), this RAISES
    :class:`SafetyStateError` instead of returning a possibly-empty set —
    discovery must refuse entirely rather than silently surface blocked or
    quarantined actors. Callers treat the raise as deny-all.
    """
    state = _load_safety()  # raises SafetyStateError on corruption: fail closed
    excluded = set(state["blocked"].get(str(blocker_id), {}))
    excluded.update(state.get("quarantined", {}))
    return excluded


def _withdraw_pair_handshakes(a: str, b: str, *, reason: str) -> list[str]:
    """Withdraw all non-terminal handshakes between a and b. Returns ids."""
    from . import consent

    withdrawn: list[str] = []
    for hs in consent.list_handshakes():
        parties = {hs.get("mentee_id"), hs.get("mentor_id")}
        if {a, b} == parties and hs.get("state") not in consent.TERMINAL_STATES:
            res = consent.withdraw(hs["id"], "system", reason=reason)
            if res.get("ok"):
                withdrawn.append(hs["id"])
    return withdrawn


# ---------------------------------------------------------------------------
# Reports + quarantine
# ---------------------------------------------------------------------------


def report(
    reporter_id: str,
    reported_id: str,
    category: str,
    *,
    detail: str = "",
    handshake_id: str = "",
) -> dict[str, Any]:
    """File an abuse report. Appends to the report log; may trigger quarantine."""
    if category not in REPORT_CATEGORIES:
        return {
            "ok": False,
            "error": f"category must be one of {list(REPORT_CATEGORIES)}, got {category!r}",
        }
    if not str(reporter_id or "").strip() or not str(reported_id or "").strip():
        return {"ok": False, "error": "reporter_id and reported_id are required"}
    if reporter_id == reported_id:
        return {"ok": False, "error": "cannot report yourself"}
    if not check_rate_limit(reporter_id, "report"):
        return {"ok": False, "error": "rate limit: too many reports; slow down"}

    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": _now(),
        "reporter_id": reporter_id,
        "reported_id": reported_id,
        "category": category,
        "detail": str(detail or "").strip(),
        "handshake_id": handshake_id or None,
    }
    with REPORTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    quarantined = _maybe_quarantine(reported_id)
    return {"ok": True, "quarantined": quarantined}


def list_reports(reported_id: str | None = None) -> list[dict[str, Any]]:
    """Return abuse-report entries, optionally filtered by reported actor.

    DECISION (fail closed, not silent-skip): a corrupt line in
    reports.jsonl raises :class:`SafetyStateError` LOUDLY instead of being
    skipped. Skipping is the dangerous choice here: ``_distinct_reporters``
    would undercount and could silently MISS the 2-reporter quarantine
    tripwire, leaving a reported actor visible in discovery. A corrupt
    reports file is unreadable state, and unreadable safety state is deny —
    only a human repairs the file. Non-dict entries (valid JSON, wrong
    shape) and invalid UTF-8 bytes count as corrupt for the same reason.
    """
    entries: list[dict[str, Any]] = []
    try:
        fh = REPORTS_FILE.open(encoding="utf-8")
    except FileNotFoundError:
        return entries
    try:
        with fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    log.error(
                        "REPORTS CORRUPT: file=%s line=%d parse_error=%s — "
                        "failing closed; reporter counts cannot be trusted "
                        "until a human repairs the file",
                        REPORTS_FILE,
                        lineno,
                        exc,
                    )
                    raise SafetyStateError(
                        f"reports file {REPORTS_FILE} is corrupt "
                        f"(line {lineno}: {exc}); failing closed"
                    ) from exc
                if not isinstance(entry, dict):
                    log.error(
                        "REPORTS CORRUPT: file=%s line=%d is %s, not an "
                        "object — failing closed",
                        REPORTS_FILE,
                        lineno,
                        type(entry).__name__,
                    )
                    raise SafetyStateError(
                        f"reports file {REPORTS_FILE} is corrupt "
                        f"(line {lineno} is not an object); failing closed"
                    )
                if reported_id and entry.get("reported_id") != reported_id:
                    continue
                entries.append(entry)
    except UnicodeDecodeError as exc:
        log.error(
            "REPORTS CORRUPT: file=%s not valid UTF-8 (%s) — failing closed",
            REPORTS_FILE,
            exc,
        )
        raise SafetyStateError(
            f"reports file {REPORTS_FILE} is not valid UTF-8 ({exc}); "
            "failing closed"
        ) from exc
    return entries


def _distinct_reporters(reported_id: str) -> set[str]:
    return {
        e["reporter_id"]
        for e in list_reports(reported_id)
        if e.get("reporter_id")
    }


def _maybe_quarantine(reported_id: str) -> bool:
    """Quarantine after QUARANTINE_THRESHOLD distinct reporters.

    Anti-griefing: two distinct caller-asserted reporter ids are a tripwire
    for human review, NOT a verdict — reporter identity cannot be verified
    in the local-first deployment (see CONTRACTS §2 trust boundary), so a
    single report never triggers anything and a two-report quarantine is
    marked ``pending_human_review``. The actor stays hidden from discovery
    and pending handshakes are withdrawn as a conservative hold; only
    :func:`clear_quarantine` with the operator + independent-reviewer gate
    lifts it. There is no auto-takedown path.
    """
    if is_quarantined(reported_id):
        return True
    if len(_distinct_reporters(reported_id)) < QUARANTINE_THRESHOLD:
        return False
    state = _load_safety()
    state["quarantined"][reported_id] = {
        "at": _now(),
        "reporters": sorted(_distinct_reporters(reported_id)),
        "status": "pending_human_review",
        "review_gate": "paul_and_independent_reviewer",
        "reviewed_by": None,
    }
    _save_safety(state)
    # Withdraw that actor's pending handshakes on both sides.
    from . import consent

    for hs in consent.list_handshakes():
        parties = {hs.get("mentee_id"), hs.get("mentor_id")}
        if reported_id in parties and hs.get("state") not in consent.TERMINAL_STATES:
            other = (parties - {reported_id}).pop() if len(parties) == 2 else reported_id
            consent.withdraw(hs["id"], other, reason="actor quarantined")
    from . import consent as _c

    _c._audit("quarantine", "", "system", f"actor={reported_id}")  # noqa: SLF001
    return True


def clear_quarantine(actor_id: str, *, reviewed_by: str) -> dict[str, Any]:
    """Lift a quarantine after human review. ``reviewed_by`` is required and recorded.

    The quarantine is ``pending_human_review`` from the moment it is set;
    the real-party pilot human gate is the operator + an independent reviewer.
    ``reviewed_by`` must name the reviewer(s) who cleared it (e.g.
    ``"paul+reviewer"``); a blank value is rejected. There is no automatic
    or single-report takedown path in either direction.
    """
    if not str(reviewed_by or "").strip():
        return {"ok": False, "error": "reviewed_by is required: quarantines lift only on human review"}
    if not check_rate_limit(reviewed_by, "clear_quarantine"):
        return {"ok": False, "error": "rate limit: too many quarantine clears; slow down"}
    state = _load_safety()  # raises SafetyStateError on corruption: fail closed
    if actor_id not in state.get("quarantined", {}):
        return {"ok": False, "error": f"{actor_id!r} is not quarantined"}
    del state["quarantined"][actor_id]
    _save_safety(state)
    from . import consent as _c

    _c._audit(  # noqa: SLF001
        "quarantine_cleared", "", reviewed_by, f"actor={actor_id}"
    )
    return {"ok": True, "cleared": actor_id}


# ---------------------------------------------------------------------------
# Rate limits (token bucket)
# ---------------------------------------------------------------------------


def check_rate_limit(actor: str, action: str, *, limits: dict | None = None) -> bool:
    """Consume one token for (actor, action). False = over the limit.

    Honesty note: buckets live in PROCESS MEMORY ONLY. They do not survive
    a CLI re-invocation, so these limits are a soft in-session guard, not a
    hard enforcement boundary against a caller who re-runs the CLI. Any
    safety-critical abuse case (quarantine decisions) goes through the
    pending-human-review gate regardless.
    """
    table = limits or DEFAULT_LIMITS
    if action not in table:
        return True  # unknown actions are not limited here
    capacity, refill = table[action]
    key = (str(actor), action)
    bucket = _BUCKETS.get(key, {"tokens": float(capacity), "at": time.monotonic()})
    now = time.monotonic()
    bucket["tokens"] = min(float(capacity), bucket["tokens"] + (now - bucket["at"]) * refill)
    bucket["at"] = now
    if bucket["tokens"] < 1.0:
        _BUCKETS[key] = bucket
        return False
    bucket["tokens"] -= 1.0
    _BUCKETS[key] = bucket
    return True


def reset_rate_limits() -> None:
    """Clear in-memory buckets. Tests only."""
    _BUCKETS.clear()


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def delete_mentor_data(mentor_id: str, *, requested_by: str) -> dict[str, Any]:
    """Erase a mentor's data on request: card, handshake payloads, session notes, reports.

    AUTHORIZATION (fail closed): erasure is a data-subject right, so
    ``requested_by`` must be the mentor themselves (``requested_by ==
    mentor_id``). Empty ``requested_by`` and any other identity —
    strangers, mentees, third parties — are rejected with
    ``{"ok": False, "error": ...}`` and NO data is touched. There is no
    admin-equivalent role defined for this project, so the gate is
    mentor-only. This is the validation that makes
    ``session_kit.scrub_mentor_notes``'s ``actor=None`` internal path safe:
    by the time the scrub is reached, ``requested_by`` has been proven to
    be the data subject, and it is passed through as ``actor=mentor_id``.

    ORDER MATTERS: session notes are scrubbed FIRST (by mentor id, while
    handshakes still carry it) and handshakes are tombstoned AFTER —
    tombstones carry no mentor_id, so scrubbing after tombstoning finds
    nothing and leaves note text verbatim. Then free-text PII in the
    consent audit trail is scrubbed (see :func:`_scrub_audit_pii` and
    CONTRACTS §6).

    Block entries keep the id (safety needs to remember who is blocked)
    but the reason is scrubbed.
    """
    if not str(requested_by or "").strip():
        return {"ok": False, "error": "requested_by is required"}
    if str(requested_by).strip() != str(mentor_id or "").strip():
        return {
            "ok": False,
            "error": "requested_by must be the mentor themselves (data-subject erasure)",
        }

    # FAIL FAST, ZERO SIDE EFFECTS (CONTRACTS §9 "refuse entirely"):
    # validate the safety state AND the reports log BEFORE any mutation.
    # _load_safety() raises SafetyStateError on corruption (invalid UTF-8,
    # bad JSON, wrong-typed values — see _validate_safety_state), and
    # list_reports() raises on corrupt report lines. If either is
    # unreadable, nothing below has run yet: the card, notes, handshakes,
    # reports, and audit trail are all untouched.
    state = _load_safety()
    all_reports = list_reports()

    import mentors as _mentors
    from . import consent as _c

    removed: dict[str, Any] = {
        "card": False,
        "handshakes": 0,
        "notes": 0,
        "reports": 0,
        "audit_entries_scrubbed": 0,
    }

    directory = _mentors._load_directory()  # noqa: SLF001
    if mentor_id in directory:
        directory.pop(mentor_id)
        _mentors._save_directory(directory)  # noqa: SLF001
        removed["card"] = True

    # 1. Scrub session notes BEFORE tombstoning handshakes. The session
    #    kit resolves notes through handshakes by mentor_id; tombstones
    #    have no mentor_id, so doing this second scrubs 0 notes.
    try:
        from . import session_kit as _sk

        removed["notes"] = _sk.scrub_mentor_notes(mentor_id)
    except (ImportError, AttributeError):
        pass

    # 2. Tombstone handshake payloads, collecting ids for the audit scrub.
    store = _c._load()  # noqa: SLF001
    deleted_ids: list[str] = []
    for hs in list(store.values()):
        if hs.get("mentor_id") == mentor_id:
            deleted_ids.append(hs["id"])
            tombstone = {
                "id": hs["id"],
                "deleted": True,
                "redacted_at": _now(),
                "state": "withdrawn",
            }
            store[hs["id"]] = tombstone
            removed["handshakes"] += 1
    _c._save(store)  # noqa: SLF001

    # Scrub report details naming this actor (keep category + timestamp).
    # all_reports was validated up front (fail fast, zero side effects);
    # reusing it here avoids a second read of the reports log.
    kept: list[dict[str, Any]] = []
    for entry in all_reports:
        if entry.get("reported_id") == mentor_id or entry.get("reporter_id") == mentor_id:
            entry["detail"] = ""
            entry["handshake_id"] = None
            removed["reports"] += 1
        kept.append(entry)
    try:
        with REPORTS_FILE.open("w", encoding="utf-8") as fh:
            for entry in kept:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except FileNotFoundError:
        pass

    # Scrub block reasons involving this actor (keep the block itself).
    # state was validated up front (fail fast, zero side effects).
    for blocker, blocked in state.get("blocked", {}).items():
        if mentor_id in blocked:
            blocked[mentor_id]["reason"] = ""
    _save_safety(state)

    # 3. Scrub free-text PII from the audit trail (goal, labels, note
    #    excerpts); structural fields and erasure receipts stay.
    removed["audit_entries_scrubbed"] = _scrub_audit_pii(
        mentor_id=mentor_id, handshake_ids=deleted_ids
    )

    _c._audit("deletion", "", requested_by, f"mentor_id={mentor_id} erased on request")  # noqa: SLF001
    return {"ok": True, "removed": removed}


def delete_mentee_data(mentee_id: str, *, requested_by: str) -> dict[str, Any]:
    """Erase a mentee's handshake payloads and questionnaire answers on request.

    Free-text PII in the consent audit trail (goals, labels) is scrubbed;
    audit lines and structural fields are kept (see CONTRACTS §6).
    """
    if not str(requested_by or "").strip():
        return {"ok": False, "error": "requested_by is required"}
    from . import consent as _c

    store = _c._load()  # noqa: SLF001
    count = 0
    deleted_ids: list[str] = []
    for hs in list(store.values()):
        if hs.get("mentee_id") == mentee_id and not hs.get("deleted"):
            deleted_ids.append(hs["id"])
            store[hs["id"]] = {
                "id": hs["id"],
                "deleted": True,
                "redacted_at": _now(),
                "state": "withdrawn",
            }
            count += 1
    _c._save(store)  # noqa: SLF001
    scrubbed = _scrub_audit_pii(mentee_id=mentee_id, handshake_ids=deleted_ids)
    _c._audit("deletion", "", requested_by, f"mentee_id={mentee_id} erased on request")  # noqa: SLF001
    return {"ok": True, "removed": {"handshakes": count, "audit_entries_scrubbed": scrubbed}}


def _audit_canonical(entry: dict[str, Any]) -> str:
    """Canonical JSON for audit entries, mirroring consent._audit's format.

    Deliberately duplicated (not imported) so the safety module does not
    depend on consent.py internals; the regression test asserts
    ``consent.verify_audit()["ok"]`` after every deletion, which locks the
    two formats together.
    """
    import hashlib

    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    entry["entry_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scrub_audit_pii(
    *,
    mentor_id: str | None = None,
    mentee_id: str | None = None,
    handshake_ids: list[str] | tuple[str, ...] = (),
) -> int:
    """Scrub free-text PII from consent-audit entries tied to a deleted party.

    DECISION (documented in CONTRACTS §6): deletion erases a party's data
    EVERYWHERE, and the audit trail is not a recoverable PII loophole.
    Audit LINES are never removed — the chain of timestamps, events,
    handshake ids, and actors stays intact — but the free-text ``detail``
    field (which carries goals, labels, note excerpts) is replaced with a
    redaction marker on every entry whose handshake was deleted or whose
    actor is the deleted party. Erasure receipts (``event == "deletion"``)
    are left untouched as evidence the erasure happened.

    The consent fixer made the audit trail a SHA-256 hash chain, so a
    scrub RE-CHAINS the file: every chained entry gets its ``prev_hash``
    and ``entry_hash`` recomputed in order, keeping ``verify_audit()``
    green. Pre-chain entries (no hash fields) and unparseable lines are
    never re-chained — they are written back with detail scrubbed only
    (pre-chain) or verbatim (unparseable), and ``verify_audit`` continues
    to flag them exactly as before.

    Reads/writes ``consent.AUDIT_FILE`` directly; consent.py is not touched.
    Returns the number of entries scrubbed.
    """
    from . import consent as _c

    path = _c.AUDIT_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    ids = set(handshake_ids)
    scrubbed = 0
    out: list[str] = []
    prev = "GENESIS"
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            out.append(line)  # verbatim: verify_audit still flags it
            continue
        if not isinstance(entry, dict):
            out.append(line)
            continue
        actor = entry.get("actor")
        tied_to_deleted = entry.get("handshake_id") in ids
        actor_is_deleted = (mentor_id is not None and actor == mentor_id) or (
            mentee_id is not None and actor == mentee_id
        )
        if entry.get("event") != "deletion" and (tied_to_deleted or actor_is_deleted):
            entry["detail"] = "[redacted: personal data deleted on request]"
            scrubbed += 1
        if "entry_hash" in entry:
            # Chained entry: re-chain so verify_audit() stays green.
            entry["prev_hash"] = prev
            out.append(_audit_canonical(entry))
            prev = entry["entry_hash"]
        else:
            # Pre-chain entry: scrub only, never fabricate a hash for it.
            out.append(
                json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            )
    path.write_text(
        "\n".join(out) + ("\n" if out else ""), encoding="utf-8"
    )
    return scrubbed


# ---------------------------------------------------------------------------
# Segregation: mentorship must never feed job-application decisions
# ---------------------------------------------------------------------------


def segregation_check() -> dict[str, Any]:
    """Fail loudly if mentorship data cross-references application data.

    Scans the mentor directory, handshake store, session notes, abuse
    reports, safety state, and the consent audit trail for shared markers —
    mentor ids, handshake ids, AND card names — and scans every one of
    those mentorship stores for application ids from the application stores
    (applications.json, apply_queue.json, watches.json). Any hit is a hard
    boundary violation. Markers shorter than 4 characters are skipped to
    avoid substring noise; unreadable stores are reported as violations
    rather than silently skipped.
    """
    violations: list[str] = []
    import mentors as _mentors
    from . import consent as _c
    from . import session_kit as _sk

    base = Path(_mentors.BASE_DIR)

    def _marker_ok(marker: object) -> bool:
        return isinstance(marker, str) and len(marker) >= 4

    mentor_ids: set[str] = set()
    card_names: set[str] = set()
    try:
        directory = _mentors._load_directory()  # noqa: SLF001
        mentor_ids = {str(k) for k in directory.keys()}
        card_names = {
            str(v.get("name", ""))
            for v in directory.values()
            if isinstance(v, dict) and v.get("name")
        }
    except Exception as exc:  # noqa: BLE001 - report, don't crash the check
        violations.append(f"could not read mentor directory: {exc}")

    handshake_ids: set[str] = set()
    try:
        handshake_ids = {
            str(h.get("id", ""))
            for h in _c._load().values()  # noqa: SLF001
            if h.get("id")
        }
    except Exception as exc:  # noqa: BLE001
        violations.append(f"could not read handshake store: {exc}")

    # Forward direction: application stores must never carry mentorship markers.
    markers = {
        m for m in (mentor_ids | handshake_ids | card_names) if _marker_ok(m)
    }
    app_files = [
        base / "applications.json",
        base / "apply_queue.json",
        base / "watches.json",
    ]
    for path in app_files:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            violations.append(f"could not read {path.name}: {exc}")
            continue
        for marker in markers:
            if marker in text:
                violations.append(
                    f"{path.name} references mentorship marker {marker!r}"
                )

    # Reverse direction: mentorship stores must not carry application ids.
    app_ids: set[str] = set()
    for path in (base / "applications.json", base / "apply_queue.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        except OSError as exc:
            violations.append(f"could not read {path.name}: {exc}")
            continue
        items = data if isinstance(data, list) else data.get("applications", [])
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("id"):
                app_ids.add(str(item["id"]))
    app_ids = {a for a in app_ids if _marker_ok(a)}

    mentorship_files = [
        _c.HANDSHAKES_FILE,
        _mentors.MENTORS_FILE,
        _sk.SESSIONS_FILE,
        REPORTS_FILE,
        SAFETY_FILE,
        _c.AUDIT_FILE,
    ]
    for path in mentorship_files:
        try:
            blob = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            violations.append(f"could not read mentorship store {path.name}: {exc}")
            continue
        for app_id in app_ids:
            if app_id in blob:
                violations.append(
                    f"mentorship store {path.name} references application id {app_id!r}"
                )

    return {"ok": not violations, "violations": violations}
