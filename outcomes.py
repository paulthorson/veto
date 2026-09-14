#!/usr/bin/env python3
"""Outcome event capture for job-apply-mcp — schema ``outcome-min-v0``.

This module is the Week-0 minimum event (roadmap Decision 02): an
append-only, auditable record of what happened after an application.
Every confirmed submission is wired into an ``applied`` event
automatically; every other canonical lifecycle event type is recorded
through guided manual updates, CSV import, or the MCP/CLI surface.

Schema contract (also published in ``docs/outcome-event-contract.md``):

* ``event_id``        — uuid4 hex, the identity anchor (never reused).
* ``application_id``  — the application's ``job_id``.
* ``event_type``      — one of the 11 canonical types (see
  :data:`CANONICAL_EVENT_TYPES`). Week-0 auto-capture emits ``applied``
  only; the other types are valid from day one and arrive via manual
  updates.
* ``occurred_at``     — ISO-8601 UTC timestamp of when the thing
  happened (not when it was recorded).
* ``source``          — capture channel, e.g. ``server:apply_to_job``,
  ``apply_queue``, ``lifecycle:update_stage``, ``manual``, ``csv-import``.
* ``role``            — job title at capture time (``""`` if unknown).
* ``provenance``      — object describing who/what produced the event,
  e.g. ``{"actor": "paul", "method": "ats_apply_auto"}``.
* ``schema_version``  — always ``"outcome-min-v0"`` for this module.
* ``recorded_at``     — when the event was appended (writer-set).
* ``corrects``        — optional ``event_id`` this event supersedes
  (reversible corrections; the original line is never edited).

Storage is ``outcomes.jsonl`` (one JSON object per line), **append-only**:
the writer only ever appends. Duplicates are detected by a content
identity key and rejected, never appended twice.

Corrections are themselves events: :func:`correct_event` appends a new
event with ``corrects`` pointing at the superseded ``event_id``.
Readers exclude superseded events unless asked otherwise, so every
correction is reversible and fully provenanced.

Coverage instrumentation (:func:`coverage_report`) measures the Q4 exit
gate: the fraction of observed post-enablement state changes captured
as auditable events, plus cohort and semantic coverage. The gate needs
>= 95% of observed state changes captured.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("job-apply-mcp.outcomes")

BASE_DIR = Path(__file__).resolve().parent

#: Schema version stamped on every event this module writes.
SCHEMA_VERSION = "outcome-min-v0"

#: The 11 canonical lifecycle event types. Week-0 auto-capture emits
#: ``applied`` only; manual updates may emit any of them.
CANONICAL_EVENT_TYPES = (
    "discovered",
    "shortlisted",
    "applied",
    "replied",
    "screened",
    "interviewed",
    "offered",
    "accepted",
    "rejected",
    "withdrawn",
    "stale",
)

#: Legacy lifecycle stage -> canonical event type (used when mirroring
#: ``lifecycle.update_stage`` calls as outcome events).
STAGE_TO_EVENT = {
    "applied": "applied",
    "interviewing": "interviewed",
    "offer": "offered",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    "ghosted": "stale",
}

DEFAULT_EVENTS_FILE = BASE_DIR / "outcomes.jsonl"
META_FILE_NAME = "outcomes_meta.json"

#: Q4 exit-gate threshold: fraction of observed state changes captured.
COVERAGE_GATE = 0.95


class OutcomeValidationError(ValueError):
    """An event failed schema validation and was not recorded."""


class DuplicateEventError(ValueError):
    """An identical event was already recorded (identity-key match)."""


# ---------------------------------------------------------------------------
# Paths / timestamps
# ---------------------------------------------------------------------------


def default_store_path() -> Path:
    """Default event-store location.

    Honors the ``VETO_OUTCOMES_FILE`` environment override (used by the
    test suite so fake events never pollute the real store); otherwise
    ``outcomes.jsonl`` next to this module.
    """
    override = os.environ.get("VETO_OUTCOMES_FILE", "").strip()
    if override:
        return Path(override)
    return BASE_DIR / "outcomes.jsonl"


def default_events_path(applications_path: str | Path | None = None) -> Path:
    """Event store location: sibling of the applications store by default.

    ``VETO_OUTCOMES_FILE`` overrides every default (same rule as
    :func:`default_store_path`).
    """
    override = os.environ.get("VETO_OUTCOMES_FILE", "").strip()
    if override:
        return Path(override)
    if applications_path:
        return Path(applications_path).parent / "outcomes.jsonl"
    return BASE_DIR / "outcomes.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_ts(value: Any) -> str:
    """Normalize an ISO-8601 timestamp to UTC ISO format.

    Raises:
        OutcomeValidationError: If the value is missing or unparseable.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise OutcomeValidationError("occurred_at is required")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise OutcomeValidationError(
                f"occurred_at {value!r} is not ISO-8601 parseable"
            )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Event construction / validation
# ---------------------------------------------------------------------------


def build_event(
    *,
    application_id: str,
    event_type: str,
    occurred_at: Any = None,
    source: str,
    role: str = "",
    provenance: dict[str, Any] | None = None,
    corrects: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build and validate an ``outcome-min-v0`` event dict (not yet stored).

    Raises:
        OutcomeValidationError: On any schema violation.
    """
    application_id = str(application_id or "").strip()
    if not application_id:
        raise OutcomeValidationError("application_id is required")
    event_type = str(event_type or "").strip().lower()
    if event_type not in CANONICAL_EVENT_TYPES:
        raise OutcomeValidationError(
            f"Unknown event_type {event_type!r}. "
            f"Allowed: {', '.join(CANONICAL_EVENT_TYPES)}"
        )
    source = str(source or "").strip()
    if not source:
        raise OutcomeValidationError("source is required")
    provenance = dict(provenance or {})
    event: dict[str, Any] = {
        "event_id": event_id or uuid.uuid4().hex,
        "application_id": application_id,
        "event_type": event_type,
        "occurred_at": _normalize_ts(occurred_at or _utcnow_iso()),
        "source": source,
        "role": str(role or ""),
        "provenance": provenance,
        "schema_version": SCHEMA_VERSION,
    }
    if corrects:
        event["corrects"] = str(corrects)
    return event


def identity_key(event: dict[str, Any]) -> str:
    """Stable duplicate-detection key for an event.

    Two events with the same schema version, application, type,
    occurrence instant, and source are the same observation — the second
    append is rejected as a duplicate. ``corrects`` is part of the key
    so a correction (which supersedes an earlier event) is a distinct
    observation, not a duplicate of the original.
    """
    canonical = json.dumps(
        [
            event.get("schema_version"),
            event.get("application_id"),
            event.get("event_type"),
            event.get("occurred_at"),
            event.get("source"),
            event.get("corrects"),
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Append-only store
# ---------------------------------------------------------------------------


def load_events(
    path: str | Path | None = None,
    *,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Read the event store. Never raises on corrupt lines (warns, skips).

    By default superseded events (ones a later correction ``corrects``)
    are excluded; pass ``include_superseded=True`` for the full audit
    trail.
    """
    events_path = Path(path) if path else default_store_path()
    events: list[dict[str, Any]] = []
    try:
        text = events_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning(
                "Skipping corrupt line %d in %s", lineno, events_path
            )
            continue
        if isinstance(obj, dict):
            events.append(obj)
    if not include_superseded:
        superseded = {
            str(ev.get("corrects"))
            for ev in events
            if ev.get("corrects")
        }
        events = [
            ev for ev in events if str(ev.get("event_id")) not in superseded
        ]
    return events


def _append_line(events_path: Path, event: dict[str, Any]) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    stored = dict(event)
    stored.setdefault("recorded_at", _utcnow_iso())
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(stored, ensure_ascii=False) + "\n")


def record_event(
    path: str | Path | None = None,
    *,
    application_id: str,
    event_type: str,
    occurred_at: Any = None,
    source: str,
    role: str = "",
    provenance: dict[str, Any] | None = None,
    corrects: str | None = None,
) -> dict[str, Any]:
    """Validate, deduplicate, and append an event.

    Returns ``{"event": ..., "appended": True, "duplicate_of": None}`` on
    success, or ``{"event": None, "appended": False,
    "duplicate_of": <event_id>}`` when the identity key (or event_id)
    already exists — duplicates are rejected, never double-appended.

    The first-ever append marks capture enablement (see
    :func:`capture_enabled_at`); this is what the coverage gate measures
    "after enablement" against.
    """
    events_path = Path(path) if path else default_store_path()
    event = build_event(
        application_id=application_id,
        event_type=event_type,
        occurred_at=occurred_at,
        source=source,
        role=role,
        provenance=provenance,
        corrects=corrects,
    )
    key = identity_key(event)
    for existing in load_events(events_path, include_superseded=True):
        if str(existing.get("event_id")) == event["event_id"]:
            raise DuplicateEventError(
                f"event_id {event['event_id']} already recorded"
            )
        if identity_key(existing) == key:
            return {
                "event": None,
                "appended": False,
                "duplicate_of": existing.get("event_id"),
            }
    _append_line(events_path, event)
    _mark_capture_enabled(events_path)
    log.info(
        "Recorded outcome event %s %s for %s",
        event["event_id"][:8],
        event_type,
        application_id,
    )
    return {"event": event, "appended": True, "duplicate_of": None}


def record_application_event(
    entry: dict[str, Any],
    source: str,
    events_path: str | Path | None = None,
) -> dict[str, Any]:
    """Capture a confirmed application submission as an ``applied`` event.

    ``entry`` is an applications.json-style dict; ``occurred_at`` comes
    from its ``submitted_at``. Never raises for validation problems the
    caller can't fix — instead raises only on genuinely bad input; the
    call sites guard with try/except so capture can never break a
    confirmed submission.
    """
    provenance = {
        "actor": "user",
        "method": source,
        "board": str(entry.get("board") or ""),
        "company": str(entry.get("company") or ""),
    }
    # Phase-2 browser records carry an explicit submitted flag; surface it
    # so a failed browser attempt is distinguishable from a confirmation.
    if "submitted" in entry:
        provenance["submitted"] = bool(entry.get("submitted"))
    return record_event(
        events_path,
        application_id=str(entry.get("job_id") or ""),
        event_type="applied",
        occurred_at=entry.get("submitted_at"),
        source=source,
        role=str(entry.get("title") or ""),
        provenance=provenance,
    )


def correct_event(
    path: str | Path | None = None,
    *,
    event_id: str,
    corrected_fields: dict[str, Any],
    actor: str = "user",
    reason: str = "",
) -> dict[str, Any]:
    """Append a reversible correction for a recorded event.

    The original event line is never edited; the correction carries
    ``corrects: <event_id>`` plus provenance naming the actor and reason.
    Only ``role``, ``occurred_at``, ``source``, and ``provenance`` may be
    corrected — identity (``event_id``, ``application_id``,
    ``event_type``) is immutable. Returns the ``record_event`` result.
    """
    events_path = Path(path) if path else default_store_path()
    originals = [
        ev
        for ev in load_events(events_path, include_superseded=True)
        if str(ev.get("event_id")) == str(event_id)
    ]
    if not originals:
        raise OutcomeValidationError(f"No event with event_id {event_id!r}")
    original = originals[0]
    allowed = {"role", "occurred_at", "source", "provenance"}
    unknown = set(corrected_fields) - allowed
    if unknown:
        raise OutcomeValidationError(
            f"Cannot correct fields {sorted(unknown)}; "
            f"correctable: {sorted(allowed)}"
        )
    merged = dict(original)
    merged.update({k: v for k, v in corrected_fields.items()})
    provenance = dict(merged.get("provenance") or {})
    provenance.update(
        {
            "correction_of": original["event_id"],
            "correction_actor": actor,
            "correction_reason": reason,
        }
    )
    return record_event(
        events_path,
        application_id=original["application_id"],
        event_type=original["event_type"],
        occurred_at=merged.get("occurred_at"),
        source=merged.get("source") or original["source"],
        role=merged.get("role", ""),
        provenance=provenance,
        corrects=original["event_id"],
    )


# ---------------------------------------------------------------------------
# Capture enablement marker
# ---------------------------------------------------------------------------


def _meta_path(events_path: Path) -> Path:
    return events_path.parent / META_FILE_NAME


def _mark_capture_enabled(events_path: Path) -> None:
    meta_path = _meta_path(events_path)
    if meta_path.exists():
        return
    try:
        meta_path.write_text(
            json.dumps(
                {
                    "capture_enabled_at": _utcnow_iso(),
                    "schema_version": SCHEMA_VERSION,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        log.warning("Could not write capture-enable marker at %s", meta_path)


def capture_enabled_at(
    events_path: str | Path | None = None,
) -> datetime | None:
    """When automatic capture was first enabled (None if never)."""
    meta_path = _meta_path(
        Path(events_path) if events_path else default_store_path()
    )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    raw = meta.get("capture_enabled_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def events_for_application(
    application_id: str,
    path: str | Path | None = None,
    *,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """Outcome events for one application, oldest first."""
    events = [
        ev
        for ev in load_events(path, include_superseded=include_superseded)
        if str(ev.get("application_id")) == str(application_id)
    ]
    events.sort(key=lambda ev: str(ev.get("occurred_at") or ""))
    return events


def timeline(
    path: str | Path | None = None,
    *,
    application_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Most recent outcome events first (optionally for one application)."""
    events = load_events(path)
    if application_id:
        events = [
            ev
            for ev in events
            if str(ev.get("application_id")) == str(application_id)
        ]
    events.sort(key=lambda ev: str(ev.get("occurred_at") or ""), reverse=True)
    return events[:limit]


# ---------------------------------------------------------------------------
# Coverage instrumentation (Q4 exit gate)
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def coverage_report(
    applications: list[dict[str, Any]],
    events_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Measure how much observed state is captured as auditable events.

    * ``cohort_coverage`` — confirmed submissions since capture
      enablement that have an ``applied`` event (the Week-0 promise:
      100% of confirmed submissions after enablement).
    * ``semantic_coverage`` — distinct canonical event types observed ÷
      11 (the roadmap notes Week-0 auto-capture alone is 1/11 = 9.1%).
    * ``state_change_coverage`` — observed post-enablement
      ``stage_history`` transitions with a matching outcome event ÷ all
      such transitions. **This is the Q4 exit-gate metric (>= 95%).**

    ``below_gate`` is True when the gate metric misses 95% — the
    roadmap calls that a material bias.
    """
    now = now or datetime.now(timezone.utc)
    events_path = Path(events_path) if events_path else default_store_path()
    events = load_events(events_path)
    enabled_at = capture_enabled_at(events_path)

    apps = [a for a in applications if isinstance(a, dict)]

    def _submitted_at(app: dict[str, Any]) -> datetime | None:
        return _parse_ts(app.get("submitted_at"))

    post_enablement_apps = [
        a
        for a in apps
        if _submitted_at(a) and (enabled_at is None or _submitted_at(a) >= enabled_at)
    ]
    applied_ids = {
        str(ev.get("application_id"))
        for ev in events
        if str(ev.get("event_type")) == "applied"
    }
    cohort_hits = sum(
        1 for a in post_enablement_apps if str(a.get("job_id")) in applied_ids
    )
    cohort_coverage = (
        cohort_hits / len(post_enablement_apps) if post_enablement_apps else 1.0
    )

    semantic_types = {str(ev.get("event_type")) for ev in events}
    semantic_coverage = len(semantic_types) / len(CANONICAL_EVENT_TYPES)

    # Observed state changes: (application_id, canonical event type) pairs
    # from stage_history entries at/after enablement.
    observed: set[tuple[str, str]] = set()
    for app in apps:
        app_id = str(app.get("job_id") or "")
        history = app.get("stage_history") or []
        for item in history:
            if not isinstance(item, dict):
                continue
            at = _parse_ts(item.get("at"))
            if enabled_at is not None and (at is None or at < enabled_at):
                continue
            mapped = STAGE_TO_EVENT.get(str(item.get("stage", "")).lower())
            if mapped:
                observed.add((app_id, mapped))
    captured = {
        (str(ev.get("application_id")), str(ev.get("event_type")))
        for ev in events
    }
    matched = len(observed & captured)
    state_change_coverage = matched / len(observed) if observed else 1.0

    # Honest-metrics flag: with no events or no applications the 1.0
    # coverage values are vacuous. Consumers must check this before
    # claiming the gate is met.
    insufficient_data = not events or not apps

    return {
        "generated_at": now.isoformat(),
        "schema_version": SCHEMA_VERSION,
        "capture_enabled_at": enabled_at.isoformat() if enabled_at else None,
        "total_events": len(events),
        "total_applications": len(apps),
        "insufficient_data": insufficient_data,
        "cohort_coverage": round(cohort_coverage, 4),
        "cohort_applications": len(post_enablement_apps),
        "cohort_captured": cohort_hits,
        "semantic_coverage": round(semantic_coverage, 4),
        "semantic_types_observed": sorted(semantic_types),
        "state_change_coverage": round(state_change_coverage, 4),
        "state_changes_observed": len(observed),
        "state_changes_captured": matched,
        "gate_threshold": COVERAGE_GATE,
        "below_gate": state_change_coverage < COVERAGE_GATE,
    }


def missing_outcome_prompts(
    applications: list[dict[str, Any]],
    events_path: str | Path | None = None,
    *,
    stale_days: int = 14,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Applications that need an outcome prompt, most-stale first.

    An application qualifies when its latest outcome event (or, with no
    events yet, its submission) is ``stale_days``+ old and its latest
    known stage is not terminal. Each prompt names the suggested next
    canonical event types so the guided update can record them.
    """
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    events_path = Path(events_path) if events_path else default_store_path()
    events = load_events(events_path)
    latest_event: dict[str, tuple[str, datetime]] = {}
    for ev in events:
        at = _parse_ts(ev.get("occurred_at"))
        if not at:
            continue
        app_id = str(ev.get("application_id"))
        prev = latest_event.get(app_id)
        if prev is None or at > prev[1]:
            latest_event[app_id] = (str(ev.get("event_type")), at)

    terminal = {"offered", "accepted", "rejected", "withdrawn"}
    prompts: list[dict[str, Any]] = []
    for app in applications:
        if not isinstance(app, dict):
            continue
        app_id = str(app.get("job_id") or "")
        if not app_id:
            continue
        last_type, last_at = latest_event.get(app_id, (None, None))
        if last_at is None:
            last_at = _parse_ts(app.get("submitted_at"))
            last_type = "applied" if last_at else None
        if last_at is None or last_type in terminal:
            continue
        days = (now - last_at).days
        if days < stale_days:
            continue
        prompts.append(
            {
                "application_id": app_id,
                "role": app.get("title", ""),
                "company": app.get("company", ""),
                "last_event_type": last_type,
                "days_since_last_event": days,
                "suggested_types": [
                    "replied",
                    "screened",
                    "interviewed",
                    "rejected",
                    "stale",
                ],
            }
        )
    prompts.sort(key=lambda p: p["days_since_last_event"], reverse=True)
    return prompts


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

CSV_IMPORT_COLUMNS = (
    "job_id",
    "title",
    "company",
    "board",
    "submitted_at",
    "stage",
)


def import_csv(
    csv_path: str | Path,
    events_path: str | Path | None = None,
    *,
    source: str = "csv-import",
    actor: str = "user",
) -> dict[str, Any]:
    """Import applications from CSV as ``applied`` outcome events.

    Expected columns: ``job_id`` (required), ``title``, ``company``,
    ``board``, ``submitted_at`` (used as ``occurred_at``; defaults to
    now), ``stage`` (informational only). Idempotent: rows whose event
    identity key already exists are counted as duplicates and skipped.
    """
    events_path = Path(events_path) if events_path else default_store_path()
    imported = 0
    duplicates = 0
    errors: list[dict[str, Any]] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for lineno, row in enumerate(reader, start=2):
            job_id = str(row.get("job_id") or "").strip()
            if not job_id:
                errors.append({"line": lineno, "error": "missing job_id"})
                continue
            try:
                result = record_event(
                    events_path,
                    application_id=job_id,
                    event_type="applied",
                    occurred_at=row.get("submitted_at") or None,
                    source=source,
                    role=str(row.get("title") or ""),
                    provenance={
                        "actor": actor,
                        "method": source,
                        "board": str(row.get("board") or ""),
                        "company": str(row.get("company") or ""),
                    },
                )
            except OutcomeValidationError as exc:
                errors.append({"line": lineno, "error": str(exc)})
                continue
            if result["appended"]:
                imported += 1
            else:
                duplicates += 1
    return {
        "imported": imported,
        "duplicates": duplicates,
        "errors": errors,
        "events_path": str(events_path),
    }


# ---------------------------------------------------------------------------
# Guided manual stage updates
# ---------------------------------------------------------------------------


def guided_update(
    applications: list[dict[str, Any]],
    events_path: str | Path | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    actor: str = "user",
) -> dict[str, Any] | None:
    """Interactive flow to record a canonical outcome event.

    Lists applications, lets the operator pick one and a canonical
    event type, shows a summary, and requires explicit confirmation
    before appending. Returns the ``record_event`` result, or ``None``
    when the operator cancels.
    """
    events_path = Path(events_path) if events_path else default_store_path()
    apps = [a for a in applications if isinstance(a, dict) and a.get("job_id")]
    if not apps:
        print_fn("No applications to update.")
        return None

    print_fn("Applications:")
    for idx, app in enumerate(apps):
        print_fn(
            f"  [{idx}] {app.get('title', '?')} @ {app.get('company', '?')} "
            f"(stage: {app.get('stage', '?')}, id: {app.get('job_id')})"
        )
    choice = input_fn("Pick an application [0]: ").strip() or "0"
    try:
        app = apps[int(choice)]
    except (ValueError, IndexError):
        print_fn("Cancelled: invalid selection.")
        return None

    print_fn("Canonical event types:")
    for idx, etype in enumerate(CANONICAL_EVENT_TYPES):
        print_fn(f"  [{idx}] {etype}")
    type_choice = input_fn("Pick an event type: ").strip()
    try:
        event_type = CANONICAL_EVENT_TYPES[int(type_choice)]
    except (ValueError, IndexError):
        print_fn("Cancelled: invalid event type.")
        return None

    occurred = input_fn(
        "When did it happen? [now, or YYYY-MM-DD]: "
    ).strip() or "now"
    if occurred.lower() != "now":
        try:
            occurred_dt = datetime.fromisoformat(occurred)
            if occurred_dt.tzinfo is None:
                occurred_dt = occurred_dt.replace(tzinfo=timezone.utc)
            occurred_at: Any = occurred_dt.isoformat()
        except ValueError:
            print_fn("Cancelled: unparseable date.")
            return None
    else:
        occurred_at = _utcnow_iso()
    note = input_fn("Note (optional): ").strip()

    print_fn(
        f"\nAbout to record: {event_type} for "
        f"{app.get('title', '?')} @ {app.get('company', '?')} "
        f"at {occurred_at}"
    )
    confirm = input_fn("Confirm? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print_fn("Cancelled.")
        return None

    try:
        result = record_event(
            events_path,
            application_id=str(app.get("job_id")),
            event_type=event_type,
            occurred_at=occurred_at,
            source="manual",
            role=str(app.get("title") or ""),
            provenance={
                "actor": actor,
                "method": "guided-update",
                "note": note,
            },
        )
    except (OutcomeValidationError, DuplicateEventError) as exc:
        print_fn(f"Not recorded: {exc}")
        return None
    if result["appended"]:
        print_fn(f"Recorded event {result['event']['event_id']}.")
    else:
        print_fn(
            f"Already recorded (duplicate of {result['duplicate_of']})."
        )
    return result


# ---------------------------------------------------------------------------
# MCP + CLI wiring
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> dict[str, Any]:
    """Register outcome-event tools on an MCP server."""

    @mcp.tool()
    def outcome_record_event(
        application_id: str,
        event_type: str,
        source: str = "manual",
        role: str = "",
        occurred_at: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Record a canonical outcome event for an application.

        event_type must be one of: discovered, shortlisted, applied,
        replied, screened, interviewed, offered, accepted, rejected,
        withdrawn, stale. Duplicates are rejected, never double-appended.
        """
        try:
            result = record_event(
                None,
                application_id=application_id,
                event_type=event_type,
                occurred_at=occurred_at or None,
                source=source,
                role=role,
                provenance={"actor": "user", "method": source, "note": note},
            )
        except (OutcomeValidationError, DuplicateEventError) as exc:
            return {"appended": False, "duplicate_of": None,
                    "event_id": None, "error": str(exc)}
        return {
            "appended": result["appended"],
            "duplicate_of": result["duplicate_of"],
            "event_id": (result["event"] or {}).get("event_id"),
            "error": None,
        }

    @mcp.tool()
    def outcome_coverage() -> dict[str, Any]:
        """Outcome-event coverage vs the Q4 exit gate (>=95% captured)."""
        import lifecycle

        return coverage_report(lifecycle.load_entries(BASE_DIR / "applications.json"))

    @mcp.tool()
    def outcome_prompts(stale_days: int = 14) -> list[dict[str, Any]]:
        """Applications needing an outcome prompt (stale/missing)."""
        import lifecycle

        return missing_outcome_prompts(
            lifecycle.load_entries(BASE_DIR / "applications.json"),
            stale_days=stale_days,
        )

    @mcp.tool()
    def outcome_timeline(
        application_id: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Recent outcome events, optionally for one application."""
        return timeline(application_id=application_id or None, limit=limit)

    return {
        "outcome_record_event": outcome_record_event,
        "outcome_coverage": outcome_coverage,
        "outcome_prompts": outcome_prompts,
        "outcome_timeline": outcome_timeline,
    }


def _cmd_outcomes(args: argparse.Namespace) -> int:
    import lifecycle

    applications_path = Path(args.applications)
    events_path = (
        Path(args.events)
        if args.events
        else default_events_path(applications_path)
    )
    applications = lifecycle.load_entries(applications_path)

    if args.outcomes_cmd == "record":
        try:
            result = record_event(
                events_path,
                application_id=args.application_id,
                event_type=args.type,
                occurred_at=args.occurred_at or None,
                source=args.source,
                role=args.role,
                provenance={
                    "actor": "user",
                    "method": args.source,
                    "note": args.note,
                },
            )
        except (OutcomeValidationError, DuplicateEventError) as exc:
            print(f"Not recorded: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result["event"] or {"duplicate_of": result["duplicate_of"]}, indent=2))
        elif result["appended"]:
            print(f"Recorded event {result['event']['event_id']}")
        else:
            print(f"Duplicate of {result['duplicate_of']}; not appended.")
        return 0

    if args.outcomes_cmd == "update":
        result = guided_update(applications, events_path)
        return 0 if result else 1

    if args.outcomes_cmd == "timeline":
        for ev in timeline(
            events_path,
            application_id=args.application_id or None,
            limit=args.limit,
        ):
            print(
                f"{ev.get('occurred_at', '')[:19]}  {ev.get('event_type', ''):12s}  "
                f"{ev.get('role', '')} [{ev.get('application_id', '')}]"
            )
        return 0

    if args.outcomes_cmd == "coverage":
        report = coverage_report(applications, events_path)
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        print(f"Schema: {report['schema_version']}  Events: {report['total_events']}")
        if report["insufficient_data"]:
            print("NOTE: insufficient data — no events or no applications yet; "
                  "coverage figures are vacuous.")
        print(
            f"Cohort coverage (applied events for post-enablement submissions): "
            f"{report['cohort_coverage']:.1%} "
            f"({report['cohort_captured']}/{report['cohort_applications']})"
        )
        print(
            f"Semantic coverage (canonical types observed): "
            f"{report['semantic_coverage']:.1%} "
            f"({len(report['semantic_types_observed'])}/11)"
        )
        print(
            f"State-change coverage [Q4 gate >= 95%]: "
            f"{report['state_change_coverage']:.1%} "
            f"({report['state_changes_captured']}/{report['state_changes_observed']})"
            + ("  <-- BELOW GATE" if report["below_gate"] else "  OK")
        )
        return 0

    if args.outcomes_cmd == "prompts":
        prompts = missing_outcome_prompts(
            applications, events_path, stale_days=args.stale_days
        )
        if args.json:
            print(json.dumps(prompts, indent=2))
            return 0
        print(f"Outcome prompts ({len(prompts)}):")
        for item in prompts:
            print(
                f"  {item['days_since_last_event']:3d}d since {item['last_event_type']}: "
                f"{item['role']} @ {item['company']} "
                f"[{item['application_id']}]"
            )
        return 0

    if args.outcomes_cmd == "import-csv":
        result = import_csv(args.csv, events_path, actor="user")
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                f"Imported {result['imported']} events, "
                f"{result['duplicates']} duplicates skipped, "
                f"{len(result['errors'])} errors."
            )
            for err in result["errors"][:10]:
                print(f"  line {err['line']}: {err['error']}")
        return 0 if not result["errors"] else 1

    print(f"Unknown outcomes subcommand {args.outcomes_cmd!r}", file=sys.stderr)
    return 2


def _add_outcomes_arguments(parser: argparse.ArgumentParser) -> Any:
    """Add common flags + subcommands to an ``outcomes`` parser."""
    parser.add_argument(
        "--applications",
        default=str(BASE_DIR / "applications.json"),
        help="Path to applications.json.",
    )
    parser.add_argument(
        "--events",
        default="",
        help="Path to outcomes.jsonl (default: next to applications.json).",
    )
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="outcomes_cmd", required=True)

    p_record = sub.add_parser("record", help="Record one outcome event.")
    p_record.add_argument("--application-id", required=True)
    p_record.add_argument(
        "--type",
        required=True,
        choices=list(CANONICAL_EVENT_TYPES),
        help="Canonical event type.",
    )
    p_record.add_argument("--occurred-at", default="")
    p_record.add_argument("--source", default="manual")
    p_record.add_argument("--role", default="")
    p_record.add_argument("--note", default="")

    sub.add_parser("update", help="Guided manual stage update (interactive).")
    p_timeline = sub.add_parser("timeline", help="Show recent outcome events.")
    p_timeline.add_argument("--application-id", default="")
    p_timeline.add_argument("--limit", type=int, default=50)

    sub.add_parser("coverage", help="Coverage vs the Q4 exit gate.")
    p_prompts = sub.add_parser(
        "prompts", help="Applications needing an outcome prompt."
    )
    p_prompts.add_argument("--stale-days", type=int, default=14)
    p_import = sub.add_parser(
        "import-csv", help="Import applications from CSV as applied events."
    )
    p_import.add_argument("csv", help="Path to the CSV file.")
    return sub


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``outcomes`` command group; return handlers for the parent."""
    parser = subparsers.add_parser(
        "outcomes", help="Record and query outcome events (outcome-min-v0)."
    )
    _add_outcomes_arguments(parser)
    return {"outcomes": _cmd_outcomes}


if __name__ == "__main__":  # `python3 outcomes.py <subcommand> ...`
    _parser = argparse.ArgumentParser(description=__doc__)
    _add_outcomes_arguments(_parser)
    _cli_args = _parser.parse_args()
    raise SystemExit(_cmd_outcomes(_cli_args))
