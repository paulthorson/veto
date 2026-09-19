#!/usr/bin/env python3
"""Outcome nudges for Veto (Initiative 01).

Delivers the capture contract's missing-outcome prompts through the
notification system:

* :func:`pending_prompts` — stale applications needing an outcome
  (read-only; wraps ``outcomes.missing_outcome_prompts``).
* :func:`send_prompts` — one ``outcome_stale`` notification per prompt,
  honoring the user's notification preferences, quiet hours, and digest
  cadence via :mod:`notify`. Notifications propose; they never record
  outcomes themselves — the user confirms via the guided update.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.outcome_nudges")

BASE_DIR = Path(__file__).resolve().parent
APPLICATIONS_FILE = BASE_DIR / "applications.json"


def pending_prompts(
    applications_path: str | Path | None = None,
    stale_days: int = 14,
) -> list[dict[str, Any]]:
    """Stale applications needing an outcome prompt (read-only)."""
    import lifecycle

    try:
        import outcomes
    except ImportError as exc:
        log.debug("outcomes contract unavailable: %s", exc)
        return []
    apps_path = Path(applications_path or APPLICATIONS_FILE)
    try:
        applications = lifecycle.load_entries(apps_path)
        from datetime import datetime, timezone

        return outcomes.missing_outcome_prompts(
            applications,
            outcomes.default_events_path(apps_path),
            stale_days=stale_days,
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        log.debug("prompt scan failed: %s", exc)
        return []


def send_prompts(
    applications_path: str | Path | None = None,
    stale_days: int = 14,
    limit: int | None = None,
) -> dict[str, Any]:
    """Send one ``outcome_stale`` notification per stale prompt.

    Delivery honors notification preferences (opt-in, quiet hours,
    digest) via :mod:`notify`. Returns per-prompt delivery results.
    """
    import notify

    prompts = pending_prompts(applications_path, stale_days)
    if limit is not None:
        prompts = prompts[: max(0, limit)]
    results = []
    for prompt in prompts:
        suggested = ", ".join(prompt.get("suggested_types", []))
        result = notify.send(
            event="outcome_stale",
            title=f"Outcome stale: {prompt.get('company')}",
            body=(
                f"{prompt.get('role')} at {prompt.get('company')}: last "
                f"event {prompt.get('last_event_type')} "
                f"{prompt.get('days_since_last_event')}d ago. "
                f"Record an outcome ({suggested}) via the guided update."
            ),
        )
        results.append({
            "application_id": prompt.get("application_id"),
            "delivery": result,
        })
    return {
        "prompts": len(prompts),
        "notifications": results,
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register outcome-nudge tools on the MCP server."""

    @mcp.tool()
    def outcome_prompts(stale_days: int = 14) -> dict:
        """Stale applications needing an outcome prompt (read-only)."""
        return {"prompts": pending_prompts(stale_days=stale_days)}

    @mcp.tool()
    def outcome_prompts_send(stale_days: int = 14,
                             limit: int | None = None) -> dict:
        """Send outcome-stale notifications (honors notification
        prefs/quiet hours/digest). Notifications propose; they never
        record outcomes."""
        return send_prompts(stale_days=stale_days, limit=limit)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_list(args: Any) -> int:
    prompts = pending_prompts(stale_days=args.stale_days)
    if args.json:
        print(json.dumps({"prompts": prompts}, indent=2, ensure_ascii=False,
                         default=str))
        return 0
    if not prompts:
        print("No stale outcome prompts.")
        return 0
    for p in prompts:
        print(f"- {p.get('company')} ({p.get('role')}): "
              f"{p.get('last_event_type')} "
              f"{p.get('days_since_last_event')}d ago "
              f"-> {', '.join(p.get('suggested_types', []))}")
    return 0


def _cli_send(args: Any) -> int:
    result = send_prompts(stale_days=args.stale_days, limit=args.limit)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "outcome-nudges",
        help="Initiative 01: deliver missing-outcome prompts via notify.",
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="on_cmd", required=True)

    pl = sub.add_parser("list", help="List stale prompts (read-only).")
    pl.add_argument("--stale-days", type=int, default=14)
    pl.set_defaults(func=_cli_list)

    ps = sub.add_parser("send", help="Send outcome-stale notifications.")
    ps.add_argument("--stale-days", type=int, default=14)
    ps.add_argument("--limit", type=int, default=None)
    ps.set_defaults(func=_cli_send)

    return {"outcome-nudges": lambda args: args.func(args)}
