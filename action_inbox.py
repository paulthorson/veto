#!/usr/bin/env python3
"""Action inbox for job-apply-mcp (Initiative 03 daily operating loop).

One queue for the day's decisions, assembled read-only from the modules
that own each signal:

* open reply-radar proposals (``reply_radar``) — confirm or dismiss, each
  an explicit human decision;
* follow-ups due with their drafts (``followup``);
* stale applications missing an outcome prompt (``outcomes`` capture
  contract, when present).

The inbox never applies anything itself. Every item names the action and
the module that owns it.

Stdlib only. Nothing is persisted: the inbox is a view.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.action_inbox")

BASE_DIR = Path(__file__).resolve().parent
APPLICATIONS_FILE = BASE_DIR / "applications.json"

#: Lower number = surfaces first.
_KIND_PRIORITY = {
    "reply_proposal": 10,
    "followup_due": 20,
    "outcome_prompt": 30,
}


def _reply_proposal_items() -> list[dict[str, Any]]:
    try:
        import reply_radar
    except ImportError as exc:
        log.debug("reply_radar unavailable: %s", exc)
        return []
    items = []
    for p in reply_radar.list_proposals(status="proposed"):
        items.append(
            {
                "kind": "reply_proposal",
                "title": (
                    f"Reply: {p.get('reply_type')} — {p.get('company')} "
                    f"({p.get('title')})"
                ),
                "detail": (
                    f"Proposes {p.get('current_stage')} -> "
                    f"{p.get('proposed_stage')} "
                    f"(confidence {p.get('confidence')}). "
                    f"{p.get('rationale') or ''}"
                ),
                "action": "reply-radar confirm|dismiss",
                "action_args": {"proposal_id": p.get("proposal_id")},
                "source": "reply_radar",
            }
        )
    return items


def _followup_items(
    applications_path: str | Path | None,
    today: date | None,
) -> list[dict[str, Any]]:
    try:
        import followup
        import profiles
        import lifecycle
    except ImportError as exc:
        log.debug("followup stack unavailable: %s", exc)
        return []
    try:
        profile = profiles.load_profile()
    except Exception:
        profile = {}
    apps_path = Path(applications_path or APPLICATIONS_FILE)
    try:
        paired = followup.followups_with_drafts(
            profile, path=apps_path, today=today
        )
    except Exception as exc:
        log.debug("followup scan failed: %s", exc)
        return []
    today = today or date.today()
    items = []
    for pair in paired:
        entry = pair.get("entry", {})
        follow_up_due = str(entry.get("follow_up_due") or "")
        overdue = bool(follow_up_due) and follow_up_due < today.isoformat()
        items.append(
            {
                "kind": "followup_due",
                "title": (
                    f"{'Overdue follow-up' if overdue else 'Follow-up due'}: "
                    f"{entry.get('company')} ({entry.get('title')})"
                ),
                "detail": (
                    f"Due {follow_up_due or 'unscheduled'}; "
                    f"stage {entry.get('stage')}. "
                    f"Draft ready: "
                    f"{bool((pair.get('draft') or {}).get('body'))}."
                ),
                "action": "followup draft",
                "action_args": {"job_id": entry.get("job_id")},
                "source": "followup",
                "overdue": overdue,
            }
        )
    return items


def _outcome_prompt_items(
    applications_path: str | Path | None,
    today: date | None,
) -> list[dict[str, Any]]:
    try:
        import lifecycle
        import outcomes
    except ImportError as exc:
        log.debug("outcomes contract unavailable: %s", exc)
        return []
    apps_path = Path(applications_path or APPLICATIONS_FILE)
    try:
        applications = lifecycle.load_entries(apps_path)
        prompts = outcomes.missing_outcome_prompts(
            applications,
            outcomes.default_events_path(apps_path),
            now=datetime.now(timezone.utc),
        )
    except Exception as exc:
        log.debug("outcome prompt scan failed: %s", exc)
        return []
    items = []
    for prompt in prompts:
        items.append(
            {
                "kind": "outcome_prompt",
                "title": (
                    f"Outcome stale: {prompt.get('company')} "
                    f"({prompt.get('role')})"
                ),
                "detail": (
                    f"Last event {prompt.get('last_event_type')} "
                    f"{prompt.get('days_since_last_event')}d ago; "
                    f"suggested event types: "
                    f"{', '.join(prompt.get('suggested_types', []))}."
                ),
                "action": "record outcome (guided update)",
                "action_args": {
                    "application_id": prompt.get("application_id")
                },
                "source": "outcomes",
            }
        )
    return items


def inbox(
    applications_path: str | Path | None = None,
    today: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Assemble today's action inbox (read-only view).

    Items sort by kind priority (reply proposals first), then overdue
    before upcoming within follow-ups.
    """
    items = (
        _reply_proposal_items()
        + _followup_items(applications_path, today)
        + _outcome_prompt_items(applications_path, today)
    )
    for item in items:
        item["priority"] = _KIND_PRIORITY.get(item["kind"], 99)
    items.sort(
        key=lambda i: (i["priority"], not bool(i.get("overdue", False)))
    )
    if limit is not None:
        items = items[: max(0, limit)]
    by_kind: dict[str, int] = {}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {
        "date": (today or date.today()).isoformat(),
        "count": len(items),
        "by_kind": by_kind,
        "items": items,
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the action-inbox tool on the MCP server."""

    @mcp.tool()
    def action_inbox(limit: int | None = None) -> dict:
        """Today's action inbox: reply proposals, due follow-ups, stale
        outcome prompts. Read-only view; nothing is applied."""
        return inbox(limit=limit)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_inbox(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    print(f"Action inbox — {result['date']} ({result['count']} items)")
    if not result["items"]:
        print("All clear.")
        return
    for item in result["items"]:
        print(f"\n[{item['kind']}] {item['title']}")
        print(f"  {item['detail']}")
        print(f"  -> {item['action']}")


def _cli_inbox(args: Any) -> int:
    _print_inbox(inbox(limit=args.limit), args.json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "inbox", help="Today's action inbox (read-only view)."
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    p.set_defaults(func=_cli_inbox)
    return {"inbox": lambda args: args.func(args)}
