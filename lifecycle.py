#!/usr/bin/env python3
"""Application lifecycle management for veto-mcp.

Additive extension of the ``applications.json`` store:

* every entry carries ``stage`` (default ``"applied"``), a
  ``stage_history`` list of ``{stage, at, note}`` events, and an optional
  ``follow_up_due`` date (``YYYY-MM-DD``).
* legacy entries (written before stages existed) are backfilled **in
  memory** on read: ``stage="applied"``. The file itself is only rewritten
  when an update actually happens.

All functions take explicit paths so they are trivially testable against
temporary directories; ``server.py`` wires them to the real store.

Outcome capture (``outcome-min-v0``): :func:`update_stage` mirrors every
stage change as a canonical outcome event in the append-only
``outcomes.jsonl`` store (see :mod:`outcomes`). Capture is best-effort
and exception-guarded — it can never break a stage update.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.lifecycle")

#: Allowed application stages, in rough pipeline order.
STAGES = ("applied", "interviewing", "offer", "rejected", "withdrawn", "ghosted")

#: Stages after which no follow-up is needed anymore.
TERMINAL_STAGES = ("offer", "rejected", "withdrawn")

#: Stages that count as a "response" for response-rate stats.
RESPONSE_STAGES = ("interviewing", "offer")

#: Stages for which follow-up nudges are relevant.
NUDGE_STAGES = ("applied", "interviewing")

FOLLOW_UP_DAYS_AFTER_INTERVIEW = 7


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def backfill_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``entry`` with lifecycle fields guaranteed present.

    Legacy entries (no ``stage`` key) get ``stage="applied"`` with a
    ``stage_history`` seed pointing at their ``submitted_at`` timestamp.
    The input dict is never mutated.
    """
    entry = dict(entry)
    if "stage" not in entry:
        seed_at = entry.get("submitted_at") or _utcnow_iso()
        entry["stage"] = "applied"
        entry.setdefault(
            "stage_history",
            [{"stage": "applied", "at": seed_at, "note": "backfilled"}],
        )
    entry.setdefault("stage_history", [])
    entry.setdefault("follow_up_due", None)
    return entry


def new_entry_defaults() -> dict[str, Any]:
    """Lifecycle fields for a freshly recorded application."""
    now = _utcnow_iso()
    return {
        "stage": "applied",
        "stage_history": [{"stage": "applied", "at": now, "note": ""}],
        "follow_up_due": None,
    }


def load_entries(path: Path) -> list[dict[str, Any]]:
    """Read the applications store, backfilling lifecycle fields in memory.

    The file is never rewritten here.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        log.warning("Applications store %s is not a list; ignoring", path)
        return []
    return [backfill_entry(e) if isinstance(e, dict) else e for e in raw]


def save_entries(path: Path, entries: list[dict[str, Any]]) -> None:
    """Persist the applications store."""
    Path(path).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _find_entry(
    entries: list[dict[str, Any]], job_id_or_index: str
) -> dict[str, Any]:
    """Locate an entry by ``job_id`` or by numeric index.

    Raises:
        KeyError: If no entry matches.
    """
    key = str(job_id_or_index)
    for entry in entries:
        if str(entry.get("job_id")) == key:
            return entry
    try:
        idx = int(key)
    except ValueError:
        idx = None
    if idx is not None and 0 <= idx < len(entries):
        return entries[idx]
    raise KeyError(f"No application found for {job_id_or_index!r}")


def update_stage(
    path: Path,
    job_id_or_index: str,
    stage: str,
    note: str = "",
    today: date | None = None,
) -> dict[str, Any]:
    """Move an application to a new stage and persist the store.

    Appends a ``{stage, at, note}`` event to ``stage_history``. Moving to
    ``"interviewing"`` sets ``follow_up_due`` to ``today + 7 days``;
    moving to a terminal stage clears it.

    Raises:
        ValueError: If ``stage`` is not a known stage.
        KeyError: If no application matches ``job_id_or_index``.
    """
    stage = str(stage).strip().lower()
    if stage not in STAGES:
        raise ValueError(
            f"Unknown stage {stage!r}. Allowed stages: {', '.join(STAGES)}"
        )
    today = today or date.today()
    entries = load_entries(path)
    entry = _find_entry(entries, job_id_or_index)
    entry["stage"] = stage
    entry.setdefault("stage_history", []).append(
        {"stage": stage, "at": _utcnow_iso(), "note": note}
    )
    if stage == "interviewing":
        entry["follow_up_due"] = (today + timedelta(days=FOLLOW_UP_DAYS_AFTER_INTERVIEW)).isoformat()
    elif stage in TERMINAL_STAGES:
        entry["follow_up_due"] = None
    save_entries(path, entries)
    log.info("Application %s -> stage %s", entry.get("job_id"), stage)
    _capture_stage_event(path, entry, stage, note)
    return entry


def _capture_stage_event(
    applications_path: Path,
    entry: dict[str, Any],
    stage: str,
    note: str,
) -> None:
    """Mirror a stage change as an outcome event (best-effort, never raises).

    Maps the legacy lifecycle stage to its canonical outcome event type
    (see ``outcomes.STAGE_TO_EVENT``) and appends to the ``outcomes.jsonl``
    store next to the applications file. Any failure is logged, never
    propagated — outcome capture must not break stage updates.
    """
    try:
        import outcomes as _outcomes

        _outcomes.record_event(
            _outcomes.default_events_path(applications_path),
            application_id=str(entry.get("job_id") or ""),
            event_type=_outcomes.STAGE_TO_EVENT.get(stage, "applied"),
            occurred_at=_utcnow_iso(),
            source="lifecycle:update_stage",
            role=str(entry.get("title") or ""),
            provenance={
                "actor": "user",
                "method": "manual-stage-update",
                "note": note,
            },
        )
    except Exception:
        log.exception(
            "Outcome event capture failed for %s", entry.get("job_id")
        )


def stats(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate statistics over (backfilled) application entries."""
    entries = [backfill_entry(e) for e in entries]
    total = len(entries)
    by_stage: dict[str, int] = {s: 0 for s in STAGES}
    by_board: dict[str, int] = {}
    for entry in entries:
        by_stage[entry.get("stage", "applied")] = (
            by_stage.get(entry.get("stage", "applied"), 0) + 1
        )
        board = str(entry.get("board", "unknown"))
        by_board[board] = by_board.get(board, 0) + 1
    responded = sum(by_stage.get(s, 0) for s in RESPONSE_STAGES)
    return {
        "total": total,
        "by_stage": {s: n for s, n in by_stage.items() if n},
        "by_board": by_board,
        "response_rate": (responded / total) if total else 0.0,
    }


def due_followups(
    entries: list[dict[str, Any]], today: date | None = None
) -> list[dict[str, Any]]:
    """Applications whose ``follow_up_due`` is today or earlier.

    Only stages where a follow-up still makes sense are included.
    """
    today = today or date.today()
    today_iso = today.isoformat()
    result = []
    for entry in (backfill_entry(e) for e in entries):
        due = entry.get("follow_up_due")
        if (
            due
            and str(due) <= today_iso
            and entry.get("stage") in NUDGE_STAGES
        ):
            result.append(entry)
    return result


CSV_COLUMNS = [
    "job_id",
    "board",
    "title",
    "company",
    "location",
    "stage",
    "submitted_at",
    "follow_up_due",
    "apply_url",
    "resume_path",
]


def export_csv(entries: list[dict[str, Any]], path: Path) -> int:
    """Write applications to CSV. Returns the number of data rows written."""
    rows = [backfill_entry(e) for e in entries if isinstance(e, dict)]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for entry in rows:
            writer.writerow({col: entry.get(col, "") for col in CSV_COLUMNS})
    log.info("Exported %d applications to %s", len(rows), path)
    return len(rows)
