#!/usr/bin/env python3
"""Canonical migration contract for outcome events.

``outcome-min-v0`` (see :mod:`outcomes`) is the Week-0 minimum schema.
When the full canonical outcome model lands, min-v0 events migrate to
the canonical schema with this contract:

* **Identity preserved** — the canonical event keeps the min-v0
  ``event_id`` and carries ``migrated_from`` pointing at it.
* **Time preserved** — ``occurred_at`` is copied verbatim (never
  re-stamped).
* **Lossless** — :func:`verify_migration` proves every min-v0 event has
  exactly one canonical counterpart with identical id and time.
* **Rollback recorded** — :func:`rollback_migration` appends a rollback
  record to the migration log; readers exclude rolled-back migrations
  unless asked. The min-v0 store is never modified or deleted, so a
  rollback is always re-derivable.
* **Reversible corrections** — corrections stay on the min-v0 store via
  :func:`outcomes.correct_event`; migration carries ``corrects``
  pointers through unchanged.

Storage:

* ``outcomes_canonical.jsonl`` — canonical events (append-only).
* ``outcomes_migration.jsonl`` — migration + rollback records
  (append-only). A migration record looks like::

      {"record_type": "migration", "migration_id": "...", "at": "...",
       "actor": "...", "schema_from": "outcome-min-v0",
       "schema_to": "outcome-v1", "event_count": N, "rolled_back": false}

  and a rollback like::

      {"record_type": "migration_rollback", "migration_id": "...",
       "at": "...", "actor": "...", "reason": "..."}
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import outcomes

log = logging.getLogger("job-apply-mcp.outcome_migration")

BASE_DIR = Path(__file__).resolve().parent

#: Schema version stamped on migrated canonical events.
CANONICAL_SCHEMA_VERSION = "outcome-v1"

DEFAULT_CANONICAL_PATH = BASE_DIR / "outcomes_canonical.jsonl"
DEFAULT_MIGRATION_LOG = BASE_DIR / "outcomes_migration.jsonl"


class MigrationError(ValueError):
    """A migration or rollback precondition failed."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    records = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping corrupt line %d in %s", lineno, path)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def migration_records(
    migration_log: str | Path | None = None,
) -> list[dict[str, Any]]:
    """All migration + rollback records, oldest first."""
    log_path = Path(migration_log) if migration_log else DEFAULT_MIGRATION_LOG
    return _read_jsonl(log_path)


def rolled_back_migration_ids(
    migration_log: str | Path | None = None,
) -> set[str]:
    """Migration ids that have a rollback record."""
    return {
        str(rec.get("migration_id"))
        for rec in migration_records(migration_log)
        if rec.get("record_type") == "migration_rollback"
        and rec.get("migration_id")
    }


def load_canonical_events(
    canonical_path: str | Path | None = None,
    migration_log: str | Path | None = None,
    *,
    include_rolled_back: bool = False,
) -> list[dict[str, Any]]:
    """Read the canonical event store.

    Events from rolled-back migrations are excluded unless
    ``include_rolled_back`` is set. Superseded events (ones a correction
    ``corrects``) are left in place here — use
    :func:`outcomes.load_events`-style filtering when you need the
    non-superseded view; the ``corrects`` pointers migrate through
    unchanged.
    """
    canonical_path = (
        Path(canonical_path) if canonical_path else DEFAULT_CANONICAL_PATH
    )
    events = _read_jsonl(canonical_path)
    if not include_rolled_back:
        rolled_back = rolled_back_migration_ids(migration_log)
        events = [
            ev for ev in events if str(ev.get("migration_id")) not in rolled_back
        ]
    return events


def migrate_to_canonical(
    events_path: str | Path | None = None,
    canonical_path: str | Path | None = None,
    migration_log: str | Path | None = None,
    *,
    actor: str = "user",
) -> dict[str, Any]:
    """Migrate all ``outcome-min-v0`` events to the canonical schema.

    Identity (``event_id``) and time (``occurred_at``) are preserved
    verbatim; each canonical event carries ``migrated_from`` and
    ``migration_id``. The min-v0 store is never touched. A migration
    record is appended to the migration log.

    Raises:
        MigrationError: If there are no min-v0 events to migrate.
    """
    events_path = Path(events_path) if events_path else outcomes.default_store_path()
    canonical_path = Path(canonical_path) if canonical_path else DEFAULT_CANONICAL_PATH
    log_path = Path(migration_log) if migration_log else DEFAULT_MIGRATION_LOG

    min_events = outcomes.load_events(events_path, include_superseded=True)
    min_v0 = [
        ev
        for ev in min_events
        if ev.get("schema_version") == outcomes.SCHEMA_VERSION
    ]
    if not min_v0:
        raise MigrationError(f"No {outcomes.SCHEMA_VERSION} events in {events_path}")

    already_migrated = {
        str(ev.get("migrated_from"))
        for ev in _read_jsonl(canonical_path)
        if ev.get("migrated_from")
        and str(ev.get("migration_id"))
        not in rolled_back_migration_ids(log_path)
    }
    pending = [ev for ev in min_v0 if str(ev.get("event_id")) not in already_migrated]
    if not pending:
        raise MigrationError("All min-v0 events are already migrated")

    migration_id = uuid.uuid4().hex
    migrated = 0
    for ev in pending:
        canonical = {
            "event_id": ev["event_id"],
            "application_id": ev.get("application_id"),
            "event_type": ev.get("event_type"),
            "occurred_at": ev.get("occurred_at"),
            "recorded_at": ev.get("recorded_at"),
            "source": ev.get("source"),
            "role": ev.get("role", ""),
            "provenance": ev.get("provenance") or {},
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "migrated_from": ev["event_id"],
            "migration_id": migration_id,
        }
        if ev.get("corrects"):
            canonical["corrects"] = ev["corrects"]
        _append_jsonl(canonical_path, canonical)
        migrated += 1

    record = {
        "record_type": "migration",
        "migration_id": migration_id,
        "at": _utcnow_iso(),
        "actor": actor,
        "schema_from": outcomes.SCHEMA_VERSION,
        "schema_to": CANONICAL_SCHEMA_VERSION,
        "event_count": migrated,
        "events_path": str(events_path),
        "canonical_path": str(canonical_path),
    }
    _append_jsonl(log_path, record)
    log.info(
        "Migrated %d events (%s -> %s) as %s",
        migrated,
        outcomes.SCHEMA_VERSION,
        CANONICAL_SCHEMA_VERSION,
        migration_id[:8],
    )
    return record


def rollback_migration(
    migration_id: str,
    migration_log: str | Path | None = None,
    *,
    actor: str = "user",
    reason: str = "",
) -> dict[str, Any]:
    """Record a rollback of a migration.

    Appends a rollback record; the canonical events stay on disk (the
    log is append-only) but readers exclude them by default. Because the
    min-v0 store is untouched, the migration can be re-run after a
    rollback.

    Raises:
        MigrationError: If the migration id is unknown or already
            rolled back.
    """
    log_path = Path(migration_log) if migration_log else DEFAULT_MIGRATION_LOG
    records = migration_records(log_path)
    migrations = {
        str(rec.get("migration_id"))
        for rec in records
        if rec.get("record_type") == "migration"
    }
    if str(migration_id) not in migrations:
        raise MigrationError(f"Unknown migration_id {migration_id!r}")
    if str(migration_id) in rolled_back_migration_ids(log_path):
        raise MigrationError(f"Migration {migration_id!r} already rolled back")
    record = {
        "record_type": "migration_rollback",
        "migration_id": str(migration_id),
        "at": _utcnow_iso(),
        "actor": actor,
        "reason": reason,
    }
    _append_jsonl(log_path, record)
    log.info("Rolled back migration %s", str(migration_id)[:8])
    return record


def verify_migration(
    migration_id: str,
    events_path: str | Path | None = None,
    canonical_path: str | Path | None = None,
    migration_log: str | Path | None = None,
) -> dict[str, Any]:
    """Prove a migration was lossless.

    Every min-v0 event present at migration time must have exactly one
    canonical counterpart with identical ``event_id`` and
    ``occurred_at``. Returns ``{"ok": bool, ...}`` with mismatch detail.
    """
    events_path = Path(events_path) if events_path else outcomes.default_store_path()
    canonical_path = Path(canonical_path) if canonical_path else DEFAULT_CANONICAL_PATH
    min_events = [
        ev
        for ev in outcomes.load_events(events_path, include_superseded=True)
        if ev.get("schema_version") == outcomes.SCHEMA_VERSION
    ]
    canonical = [
        ev
        for ev in load_canonical_events(
            canonical_path, migration_log, include_rolled_back=True
        )
        if str(ev.get("migration_id")) == str(migration_id)
    ]
    by_id: dict[str, list[dict[str, Any]]] = {}
    for ev in canonical:
        by_id.setdefault(str(ev.get("event_id")), []).append(ev)
    missing = []
    mismatched_time = []
    duplicated = []
    for ev in min_events:
        eid = str(ev.get("event_id"))
        matches = by_id.get(eid, [])
        if not matches:
            missing.append(eid)
        elif len(matches) > 1:
            duplicated.append(eid)
        elif matches[0].get("occurred_at") != ev.get("occurred_at"):
            mismatched_time.append(eid)
    ok = not (missing or mismatched_time or duplicated)
    return {
        "ok": ok,
        "migration_id": str(migration_id),
        "min_events": len(min_events),
        "canonical_events": len(canonical),
        "missing": missing,
        "mismatched_time": mismatched_time,
        "duplicated": duplicated,
    }
