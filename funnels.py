#!/usr/bin/env python3
"""Initiative 01 analytics: funnel views + history reconstruction.

Funnel views slice the application pipeline by dimension:

* ``source`` — board / provenance board (greenhouse, lever, ...)
* ``role_family`` — normalized via :mod:`calibration` heuristics
* ``fit_band`` — from the experiment ledger (or ``unknown``)
* ``resume_variant`` — tailoring version from the experiment ledger
  (or ``unknown``)
* ``cohort`` — ISO week of first activity

Each slice reports per-stage counts and stage-to-stage conversion rates
over the canonical funnel
discovered -> shortlisted -> applied -> replied -> screened ->
interviewed -> offered -> accepted, with rejected / withdrawn / stale
counted as terminal exits.

:func:`render_timeline` reconstructs one application's history as
human-readable text — no raw JSON.

Read-only w.r.t. both stores. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.funnels")

BASE_DIR = Path(__file__).resolve().parent
APPLICATIONS_FILE = BASE_DIR / "applications.json"

FUNNEL_STAGES = (
    "discovered",
    "shortlisted",
    "applied",
    "replied",
    "screened",
    "interviewed",
    "offered",
    "accepted",
)
TERMINAL_EXITS = ("rejected", "withdrawn", "stale")

_DIMENSIONS = ("source", "role_family", "fit_band", "resume_variant",
               "cohort")


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _latest_event_per_app(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for ev in events:
        app_id = str(ev.get("application_id") or "")
        if not app_id:
            continue
        key = str(ev.get("occurred_at") or ev.get("recorded_at") or "")
        cur = latest.get(app_id)
        if cur is None or key >= str(
            cur.get("occurred_at") or cur.get("recorded_at") or ""
        ):
            latest[app_id] = ev
    return latest


def _dimension_value(
    dim: str,
    entry: dict[str, Any],
    latest_event: dict[str, Any] | None,
    ledger_lookup: Any,
) -> str:
    if dim == "source":
        prov = (latest_event or {}).get("provenance") or {}
        return str(prov.get("board") or entry.get("board") or "unknown")
    if dim == "role_family":
        try:
            import calibration

            return calibration.role_family(
                entry.get("title") or (latest_event or {}).get("role")
            )
        except ImportError:
            return "unknown"
    if dim == "fit_band":
        score = None
        if ledger_lookup is not None:
            rec = ledger_lookup(str(entry.get("job_id") or ""))
            score = (rec or {}).get("fit_score")
        score = entry.get("fit_score") if score is None else score
        if score is None:
            return "unknown"
        try:
            s = float(score)
        except (TypeError, ValueError):
            return "unknown"
        if s >= 85:
            return "excellent"
        if s >= 70:
            return "strong"
        if s >= 60:
            return "borderline"
        return "weak"
    if dim == "resume_variant":
        variant = entry.get("resume_variant")
        if variant is None and ledger_lookup is not None:
            rec = ledger_lookup(str(entry.get("job_id") or ""))
            variant = (rec or {}).get("tailoring_version")
        return str(variant or "unknown")
    if dim == "cohort":
        first = _parse_ts(entry.get("submitted_at")) or _parse_ts(
            (latest_event or {}).get("occurred_at")
        )
        if first is None:
            return "unknown"
        year, week, _ = first.isocalendar()
        return f"{year}-W{week:02d}"
    raise ValueError(f"unknown dimension {dim!r}")


def funnel(
    applications: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    by: str = "source",
    applications_path: str | Path | None = None,
    events_path: str | Path | None = None,
) -> dict[str, Any]:
    """Funnel view of the pipeline sliced by ``by``.

    Each slice: {count, stages: {stage: n}, exits: {exit: n},
    conversion: {stage: rate from previous stage}}. Read-only.
    """
    if by not in _DIMENSIONS:
        raise ValueError(f"by must be one of {_DIMENSIONS}")
    import lifecycle

    try:
        import outcomes
    except ImportError:
        outcomes = None  # type: ignore
    try:
        import experiment_ledger
    except ImportError:
        experiment_ledger = None  # type: ignore

    apps = (applications if applications is not None
            else lifecycle.load_entries(
                Path(applications_path) if applications_path
                else APPLICATIONS_FILE))
    evs = (events if events is not None
           else (outcomes.load_events(events_path) if outcomes else []))
    latest = _latest_event_per_app(evs)
    ledger_lookup = (experiment_ledger.assignment
                     if experiment_ledger else None)

    slices: dict[str, dict[str, Any]] = {}
    for entry in apps:
        if not isinstance(entry, dict):
            continue
        app_id = str(entry.get("job_id") or "")
        lev = latest.get(app_id)
        dim_value = _dimension_value(by, entry, lev, ledger_lookup)
        sl = slices.setdefault(dim_value, {
            "count": 0,
            "stages": {s: 0 for s in FUNNEL_STAGES},
            "exits": {e: 0 for e in TERMINAL_EXITS},
        })
        sl["count"] += 1
        ev_type = str((lev or {}).get("event_type") or "")
        if ev_type in FUNNEL_STAGES:
            sl["stages"][ev_type] += 1
        elif ev_type in TERMINAL_EXITS:
            sl["exits"][ev_type] += 1
        else:
            # No outcome event yet: fall back to the lifecycle stage.
            stage = str(entry.get("stage") or "applied")
            mapped = {"interviewing": "interviewed",
                      "offer": "offered"}.get(stage, stage)
            if mapped in FUNNEL_STAGES:
                sl["stages"][mapped] += 1
            elif mapped in ("rejected", "withdrawn", "ghosted"):
                sl["exits"]["stale" if mapped == "ghosted" else mapped] += 1
    # Conversion: share of the slice reaching each stage (auditable,
    # no imputed denominators).
    for sl in slices.values():
        sl["conversion"] = {
            stage: round(sl["stages"][stage] / sl["count"], 3)
            if sl["count"] else 0.0
            for stage in FUNNEL_STAGES
        }
    return {
        "dimension": by,
        "slices": slices,
        "total": sum(s["count"] for s in slices.values()),
        "note": "conversion = share of the slice reaching each stage",
    }


def render_timeline(
    application_id: str,
    events_path: str | Path | None = None,
) -> str:
    """Human-readable history for one application (no raw JSON).

    Returns a short text timeline, newest last, with provenance on each
    line. Returns a plain message when there is nothing to show.
    """
    try:
        import outcomes
    except ImportError:
        return "Outcome event store unavailable."
    events = [e for e in outcomes.load_events(events_path)
              if str(e.get("application_id")) == str(application_id)]
    if not events:
        return f"No outcome events recorded for {application_id}."
    events.sort(key=lambda e: str(e.get("occurred_at") or ""))
    company = str(events[0].get("company") or "").strip()
    role = str(events[0].get("role") or "").strip()
    header = f"History for {application_id}"
    if company or role:
        header += f" ({' — '.join(p for p in (company, role) if p)})"
    lines = [header]
    for ev in events:
        at = str(ev.get("occurred_at") or ev.get("recorded_at") or "?")[:16]
        etype = ev.get("event_type")
        prov = ev.get("provenance") or {}
        actor = prov.get("actor", "?")
        method = prov.get("method", "?")
        note = f" — {prov['note']}"[:80] if prov.get("note") else ""
        corrected = ""
        if ev.get("corrects"):
            corrected = f" (corrects {ev['corrects']})"
        lines.append(
            f"{at}  {etype}{corrected}  [by {actor} via {method}]{note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register funnel tools on the MCP server."""

    @mcp.tool()
    def funnel_view(by: str = "source") -> dict:
        """Pipeline funnel sliced by dimension: source, role_family,
        fit_band, resume_variant, cohort. Read-only."""
        return funnel(by=by)

    @mcp.tool()
    def application_timeline(application_id: str) -> dict:
        """Human-readable event history for one application."""
        return {"timeline": render_timeline(application_id)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_funnel(args: Any) -> int:
    print(json.dumps(funnel(by=args.by), indent=2, ensure_ascii=False,
                     default=str))
    return 0


def _cli_timeline(args: Any) -> int:
    print(render_timeline(args.application_id))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "funnel", help="Initiative 01: pipeline funnel views + timelines."
    )
    sub = p.add_subparsers(dest="f_cmd", required=True)

    pf = sub.add_parser("view", help="Funnel sliced by a dimension.")
    pf.add_argument("--by", default="source", choices=list(_DIMENSIONS))
    pf.set_defaults(func=_cli_funnel)

    pt = sub.add_parser("timeline", help="Readable history, no raw JSON.")
    pt.add_argument("application_id")
    pt.set_defaults(func=_cli_timeline)

    return {"funnel": lambda args: args.func(args)}
