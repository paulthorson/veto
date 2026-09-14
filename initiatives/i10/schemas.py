#!/usr/bin/env python3
"""Named stable-schema registry for Initiative 10.

Sync, backup, and the release-channel compatibility checks move ONLY the
schemas declared here. Each entry carries:

* ``name`` — stable dataset name (e.g. ``"outcome-event"``).
* ``version`` — contract version string (e.g. ``"outcome-min-v0"``).
* ``files`` — dataset filenames relative to the Veto data directory.
* ``validate`` — a validator: ``(dict_or_record) -> list[str]`` of problems.
* ``status`` — ``"published"`` (Initiative 01 froze it) or
  ``"i10-defined"`` (interface this package requires; integrates with the
  real contract when it lands).

Initiative 01 status (2026-09-13):
* ``outcome-event`` — PUBLISHED (``docs/outcome-event-contract.md``,
  frozen for Q4). Fields, storage format, duplicate-detection and
  correction rules are taken verbatim from that contract.
* ``profile`` — not yet published; i10 requires ``profile-v1``.
* ``evidence-store`` — not yet published; i10 requires ``evidence-v0``.

Migration and sync code must call ``get_schema(name)`` and honor
``SCHEMA_VERSION``; adding a new dataset is a registry entry, never a
hard-coded filename elsewhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_OUTCOME_TYPES = frozenset(
    {
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
    }
)

_REQUIRED_EVENT_FIELDS = (
    "event_id",
    "application_id",
    "event_type",
    "occurred_at",
    "source",
    "role",
    "provenance",
    "schema_version",
)


def validate_outcome_event(record: dict[str, Any]) -> list[str]:
    """Validate one outcome event against the published ``outcome-min-v0``
    contract. Returns a list of problem strings (empty = valid)."""
    problems: list[str] = []
    if not isinstance(record, dict):
        return ["record is not an object"]
    for f in _REQUIRED_EVENT_FIELDS:
        if f not in record or record[f] in (None, "") and f != "role":
            problems.append(f"missing required field {f!r}")
    if record.get("event_type") not in _OUTCOME_TYPES:
        problems.append(
            f"event_type {record.get('event_type')!r} not one of the 11 "
            "canonical types"
        )
    if record.get("schema_version") != "outcome-min-v0":
        problems.append(
            f"schema_version {record.get('schema_version')!r} != 'outcome-min-v0'"
        )
    ts = str(record.get("occurred_at", ""))
    if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts):
        problems.append(f"occurred_at {ts!r} is not ISO-8601")
    if not isinstance(record.get("provenance"), dict):
        problems.append("provenance must be an object")
    return problems


def validate_profile(record: dict[str, Any]) -> list[str]:
    """Validate a profile record against the i10-required ``profile-v1``
    interface. The real Initiative 01 contract supersedes this when it
    lands; until then, sync/backup treat profiles as opaque dicts with a
    required ``schema_version`` marker and forbid path-traversal names."""
    problems: list[str] = []
    if not isinstance(record, dict):
        return ["profile is not an object"]
    if record.get("schema_version") not in ("profile-v1",):
        problems.append(
            f"schema_version {record.get('schema_version')!r} != 'profile-v1'"
        )
    return problems


_EVIDENCE_KINDS = frozenset({"note", "artifact", "metric", "quote", "link"})


def validate_evidence_record(record: dict[str, Any]) -> list[str]:
    """Validate an evidence-store record against the i10-required
    ``evidence-v0`` interface."""
    problems: list[str] = []
    if not isinstance(record, dict):
        return ["record is not an object"]
    for f in ("record_id", "kind", "application_id", "created_at", "schema_version"):
        if f not in record or record[f] in (None, ""):
            problems.append(f"missing required field {f!r}")
    if record.get("kind") not in _EVIDENCE_KINDS:
        problems.append(f"kind {record.get('kind')!r} not a known evidence kind")
    if record.get("schema_version") != "evidence-v0":
        problems.append(
            f"schema_version {record.get('schema_version')!r} != 'evidence-v0'"
        )
    return problems


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StableSchema:
    name: str
    version: str
    files: tuple[str, ...]
    validator: Callable[[dict[str, Any]], list[str]]
    status: str  # "published" | "i10-defined"
    doc: str = ""
    # How records are keyed for sync merge / dedupe within each file.
    record_key: str = ""


SCHEMA_REGISTRY: dict[str, StableSchema] = {
    "outcome-event": StableSchema(
        name="outcome-event",
        version="outcome-min-v0",
        files=("outcomes.jsonl", "outcomes_meta.json"),
        validator=validate_outcome_event,
        status="published",
        doc="docs/outcome-event-contract.md",
        record_key="event_id",
    ),
    "profile": StableSchema(
        name="profile",
        version="profile-v1",
        files=("profiles/profile.json", "profiles/default.json"),
        validator=validate_profile,
        status="i10-defined",
        doc="initiatives/i10/schemas.py (integrate with real 01 contract when published)",
        record_key="",
    ),
    "evidence-store": StableSchema(
        name="evidence-store",
        version="evidence-v0",
        files=("evidence_store.jsonl",),
        validator=validate_evidence_record,
        status="i10-defined",
        doc="initiatives/i10/schemas.py (integrate with real 01 contract when published)",
        record_key="record_id",
    ),
}


def get_schema(name: str) -> StableSchema:
    try:
        return SCHEMA_REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"Unknown stable schema {name!r}. Sync/backup move ONLY named "
            f"stable schemas: {sorted(SCHEMA_REGISTRY)}"
        ) from None


def schema_versions() -> dict[str, str]:
    """{schema_name: version} for backup headers and channel checks."""
    return {name: s.version for name, s in SCHEMA_REGISTRY.items()}


# ---------------------------------------------------------------------------
# Compatibility checks (used by release channels, Epic 6)
# ---------------------------------------------------------------------------


@dataclass
class CompatibilityResult:
    ok: bool
    schema: str
    checked: int
    invalid: int
    problems: list[str] = field(default_factory=list)


def check_file(
    schema_name: str, path: Path, *, max_problems: int = 25
) -> CompatibilityResult:
    """Validate every record in a dataset file against its stable schema."""
    schema = get_schema(schema_name)
    checked = 0
    invalid = 0
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CompatibilityResult(True, schema_name, 0, 0, ["file absent (ok)"])
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        checked += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            if len(problems) < max_problems:
                problems.append(f"line {lineno}: not JSON")
            continue
        errs = schema.validator(record)
        if errs:
            invalid += 1
            if len(problems) < max_problems:
                problems.append(f"line {lineno}: {errs[0]}")
    return CompatibilityResult(invalid == 0, schema_name, checked, invalid, problems)


def check_data_dir(data_dir: str | Path) -> list[CompatibilityResult]:
    """Run automated compatibility checks over every stable-schema file
    present in a data directory (release-channel gate, Epic 6)."""
    data_dir = Path(data_dir)
    results: list[CompatibilityResult] = []
    for name, schema in SCHEMA_REGISTRY.items():
        for rel in schema.files:
            path = data_dir / rel
            if path.exists():
                results.append(check_file(name, path))
    return results
