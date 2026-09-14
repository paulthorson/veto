#!/usr/bin/env python3
"""Response analytics for the job-apply MCP server (stdlib only).

Reads ``applications.json`` **read-only** via :mod:`lifecycle` (which
backfills legacy entries in memory) and computes pipeline statistics:

* :func:`funnel` — counts per stage + applied→interviewing→offer
  conversion rates.
* :func:`response_rate_by_board` — per-board applied/responded/rate.
* :func:`time_to_first_response` — median days from ``applied`` to the
  first stage change, per board.
* :func:`stale_applications` — applications stuck in ``applied`` with no
  update for N days (feeds follow-up nudges).
* :func:`generate_report` — the full report dict.
* :func:`outcome_coverage` — outcome-event coverage vs the Q4 exit gate
  (>= 95% of observed state changes captured as auditable events).
* :func:`outcome_timeline` — recent ``outcome-min-v0`` events, newest
  first (optionally for one application).

Wiring (additive; the parent wires these in, this module never imports
``server.py`` or ``cli.py``):

* ``register_tools(mcp)`` — registers the ``application_analytics``
  MCP tool.
* ``register_cli(subparsers)`` — adds the ``analytics`` CLI command and
  returns a ``{command: handler}`` dict for the parent to merge into its
  dispatch table.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import lifecycle
import outcomes

BASE_DIR = Path(__file__).resolve().parent
APPLICATIONS_FILE = BASE_DIR / "applications.json"

#: Current stages that count as "the employer responded".
RESPONSE_STAGES = ("interviewing", "offer", "rejected")

#: Stages that mean the pipeline moved forward from "applied".
PROGRESS_STAGES = ("interviewing", "offer")


# ---------------------------------------------------------------------------
# Loading / timestamp helpers
# ---------------------------------------------------------------------------


def load_applications(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read the applications store (read-only); skip non-dict rows."""
    entries = lifecycle.load_entries(Path(path) if path else APPLICATIONS_FILE)
    return [e for e in entries if isinstance(e, dict)]


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (or datetime) to an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _entry_board(entry: dict[str, Any]) -> str:
    """Board for an entry, falling back to the job-id prefix."""
    board = (entry.get("board") or "").strip().lower()
    if board:
        return board
    job_id = str(entry.get("job_id") or "")
    prefix = job_id.partition(":")[0].strip().lower()
    return prefix or "unknown"


def _history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    hist = entry.get("stage_history") or []
    return [ev for ev in hist if isinstance(ev, dict)]


def _applied_at(entry: dict[str, Any]) -> datetime | None:
    """When the application entered the pipeline."""
    for ev in _history(entry):
        if str(ev.get("stage", "")).lower() == "applied":
            at = _parse_ts(ev.get("at"))
            if at:
                return at
    return _parse_ts(entry.get("submitted_at"))


def _first_stage_change_at(entry: dict[str, Any]) -> datetime | None:
    """Timestamp of the first stage event after the initial 'applied'."""
    seen_applied = False
    for ev in _history(entry):
        stage = str(ev.get("stage", "")).lower()
        if not seen_applied and stage == "applied":
            seen_applied = True
            continue
        at = _parse_ts(ev.get("at"))
        if at:
            return at
    return None


def _last_update_at(entry: dict[str, Any]) -> datetime | None:
    """Most recent stage-history timestamp (fallback: submitted_at)."""
    latest: datetime | None = None
    for ev in _history(entry):
        at = _parse_ts(ev.get("at"))
        if at and (latest is None or at > latest):
            latest = at
    return latest or _parse_ts(entry.get("submitted_at"))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def funnel(applications: list[dict[str, Any]]) -> dict[str, Any]:
    """Pipeline funnel: current-stage counts + conversion rates.

    ``reached_interviewing`` counts entries whose history ever hit
    ``interviewing`` or ``offer``; ``reached_offer`` counts entries that
    ever hit ``offer``.
    """
    counts: dict[str, int] = {stage: 0 for stage in lifecycle.STAGES}
    reached_interviewing = 0
    reached_offer = 0
    total = 0
    for entry in applications:
        if not isinstance(entry, dict):
            continue
        total += 1
        stage = str(entry.get("stage", "applied")).lower()
        counts[stage] = counts.get(stage, 0) + 1
        stages_seen = {
            str(ev.get("stage", "")).lower() for ev in _history(entry)
        }
        if stages_seen & set(PROGRESS_STAGES):
            reached_interviewing += 1
        if "offer" in stages_seen:
            reached_offer += 1
    return {
        "total": total,
        "current_stage_counts": counts,
        "reached_interviewing": reached_interviewing,
        "reached_offer": reached_offer,
        "applied_to_interview_rate": _rate(reached_interviewing, total),
        "interview_to_offer_rate": _rate(reached_offer, reached_interviewing),
        "applied_to_offer_rate": _rate(reached_offer, total),
    }


def response_rate_by_board(
    applications: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per-board ``{applied, responded, rate}``.

    "Responded" = current stage is interviewing, offer, or rejected —
    i.e. the employer (or the process) moved the application forward.
    """
    per_board: dict[str, dict[str, Any]] = {}
    for entry in applications:
        if not isinstance(entry, dict):
            continue
        board = _entry_board(entry)
        stats = per_board.setdefault(board, {"applied": 0, "responded": 0})
        stats["applied"] += 1
        if str(entry.get("stage", "")).lower() in RESPONSE_STAGES:
            stats["responded"] += 1
    for stats in per_board.values():
        stats["rate"] = _rate(stats["responded"], stats["applied"])
    return per_board


def time_to_first_response(
    applications: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Median days from ``applied`` to the first stage change, per board.

    Boards with applications but no stage changes yet report
    ``median_days: None`` with ``samples: 0``.
    """
    del now  # reserved for future "still waiting" aging; unused today
    per_board_days: dict[str, list[float]] = {}
    boards_seen: set[str] = set()
    for entry in applications:
        if not isinstance(entry, dict):
            continue
        board = _entry_board(entry)
        boards_seen.add(board)
        applied_at = _applied_at(entry)
        first_change = _first_stage_change_at(entry)
        if not applied_at or not first_change:
            continue
        days = (first_change - applied_at).total_seconds() / 86400
        if days < 0:
            continue
        per_board_days.setdefault(board, []).append(days)
    report: dict[str, dict[str, Any]] = {}
    for board in sorted(boards_seen):
        days = per_board_days.get(board, [])
        report[board] = {
            "median_days": round(statistics.median(days), 2) if days else None,
            "samples": len(days),
        }
    return report


def stale_applications(
    applications: list[dict[str, Any]],
    days: int = 14,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Applications stuck in ``applied`` with no update for ``days``+ days.

    Sorted most-stale first. This is the feed for follow-up nudges:
    anything here is a candidate for a check-in.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    stale: list[dict[str, Any]] = []
    for entry in applications:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("stage", "")).lower() != "applied":
            continue
        last = _last_update_at(entry)
        if last is None or last >= cutoff:
            continue
        stale.append(
            {
                "job_id": entry.get("job_id"),
                "title": entry.get("title", "Unknown"),
                "company": entry.get("company", "Unknown"),
                "board": _entry_board(entry),
                "days_stale": (now - last).days,
                "last_update": last.isoformat(),
            }
        )
    stale.sort(key=lambda item: item["days_stale"], reverse=True)
    return stale


def generate_report(
    path: str | Path | None = None,
    stale_days: int = 14,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the full analytics report from the applications store."""
    now = now or datetime.now(timezone.utc)
    applications = load_applications(path)
    events_path = outcomes.default_events_path(path or APPLICATIONS_FILE)
    return {
        "generated_at": now.isoformat(),
        "total_applications": len(applications),
        "funnel": funnel(applications),
        "response_rate_by_board": response_rate_by_board(applications),
        "time_to_first_response_days": time_to_first_response(applications),
        "stale_applications": stale_applications(
            applications, days=stale_days, now=now
        ),
        "stale_days": stale_days,
        "event_coverage": outcome_coverage(
            applications=applications, events_path=events_path, now=now
        ),
        "recent_outcome_events": outcome_timeline(
            events_path=events_path, limit=10
        ),
    }


# ---------------------------------------------------------------------------
# Outcome-event queries (outcome-min-v0 event-query side)
# ---------------------------------------------------------------------------


def outcome_coverage(
    applications: list[dict[str, Any]] | None = None,
    events_path: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Outcome-event coverage vs the Q4 exit gate.

    Wraps :func:`outcomes.coverage_report`: cohort coverage (applied
    events for post-enablement submissions), semantic coverage
    (canonical types observed ÷ 11), and state-change coverage (the
    >= 95% gate metric).
    """
    apps = (
        applications
        if applications is not None
        else load_applications()
    )
    return outcomes.coverage_report(apps, events_path, now=now)


def outcome_timeline(
    events_path: str | Path | None = None,
    *,
    application_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Recent outcome events, newest first (optionally one application)."""
    return outcomes.timeline(
        events_path, application_id=application_id, limit=limit
    )


def outcome_prompts(
    applications: list[dict[str, Any]] | None = None,
    events_path: str | Path | None = None,
    *,
    stale_days: int = 14,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Applications needing an outcome prompt (stale/missing outcomes)."""
    apps = (
        applications
        if applications is not None
        else load_applications()
    )
    return outcomes.missing_outcome_prompts(
        apps, events_path, stale_days=stale_days, now=now
    )


# ---------------------------------------------------------------------------
# MCP + CLI wiring (called by the parent; never imported by server.py here)
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> dict[str, Any]:
    """Register the ``application_analytics`` tool on an MCP server."""

    @mcp.tool()
    def application_analytics() -> dict[str, Any]:
        """Pipeline analytics over your recorded applications.

        Returns funnel counts + applied→interviewing→offer conversion
        rates, per-board response rates, median days to first response,
        and applications stuck in "applied" with no update (follow-up
        candidates).

        Returns:
            The full analytics report dict (see generate_report).
        """
        return generate_report()

    return {"application_analytics": application_analytics}


def cmd_analytics(args: argparse.Namespace) -> int:
    """CLI handler for the ``analytics`` command."""
    report = generate_report(
        path=args.applications, stale_days=args.stale_days
    )
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    funnel_data = report["funnel"]
    print(f"Applications: {report['total_applications']}")
    print("Funnel (current stage):")
    for stage, count in funnel_data["current_stage_counts"].items():
        if count:
            print(f"  {stage:14s} {count}")
    print(
        "Conversion: applied→interview "
        f"{funnel_data['applied_to_interview_rate']:.0%} | "
        f"interview→offer {funnel_data['interview_to_offer_rate']:.0%} | "
        f"applied→offer {funnel_data['applied_to_offer_rate']:.0%}"
    )
    print("Response rate by board:")
    for board, stats in sorted(report["response_rate_by_board"].items()):
        print(
            f"  {board:14s} {stats['responded']}/{stats['applied']} "
            f"({stats['rate']:.0%})"
        )
    print("Median days to first response:")
    for board, stats in sorted(
        report["time_to_first_response_days"].items()
    ):
        median = stats["median_days"]
        print(
            f"  {board:14s} "
            f"{median if median is not None else '—'} "
            f"(n={stats['samples']})"
        )
    stale = report["stale_applications"]
    print(f"Stale (no update in {report['stale_days']}+ days): {len(stale)}")
    for item in stale[:10]:
        print(
            f"  {item['days_stale']:3d}d  {item['title']} @ {item['company']} "
            f"[{item['board']}]"
        )
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``analytics`` subcommand; return handlers for the parent.

    The parent merges the returned dict into its own dispatch table, e.g.::

        handlers.update(analytics.register_cli(sub))
    """
    parser = subparsers.add_parser(
        "analytics", help="Show application pipeline analytics."
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=14,
        help="Days without an update before an application counts as stale "
        "(default: 14).",
    )
    parser.add_argument(
        "--applications",
        default=str(APPLICATIONS_FILE),
        help="Path to applications.json.",
    )
    return {"analytics": cmd_analytics}


if __name__ == "__main__":  # `python3 analytics.py [--json]`
    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument("--json", action="store_true")
    _parser.add_argument("--stale-days", type=int, default=14)
    _parser.add_argument("--applications", default=str(APPLICATIONS_FILE))
    _cli_args = _parser.parse_args()
    raise SystemExit(cmd_analytics(_cli_args))
