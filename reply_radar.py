#!/usr/bin/env python3
"""Recruiter reply radar for Veto (Initiative 03).

Scans recruiter email (via :mod:`email_sync`), classifies the likely reply
type, and **proposes** application stage updates. Proposals are never
applied without explicit human confirmation:

* :func:`scan` — classify recent recruiter mail, match to applications,
  persist one proposal per confident signal. Read-only w.r.t. application
  stages: this function cannot change a stage, by construction.
* :func:`confirm` — apply a proposal's stage update. Requires
  ``confirmed=True`` (the CLI/MCP surface only passes it with an explicit
  ``--confirm`` / ``confirmed=true`` from the human). The stage change goes
  through :func:`lifecycle.update_stage`, which mirrors it as a canonical
  ``outcome-min-v0`` event with full provenance.
* :func:`dismiss` — drop a proposal with a reason (recorded, reversible
  in the log).

Q4 governance contract (roadmap exit gate): reply classification and
notifications are **proposal-only until the user confirms the action**.
``email_sync.scan_recruiter_emails(..., apply_updates=True)`` now requires
an additional explicit ``confirmed=True``; without it the scan degrades to
propose-only with an explanatory note.

Stdlib only. Proposals persist in ``reply_proposals.json`` (append-only
log; each proposal carries a status history).
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.reply_radar")

BASE_DIR = Path(__file__).resolve().parent
PROPOSALS_FILE = BASE_DIR / "reply_proposals.json"
APPLICATIONS_FILE = BASE_DIR / "applications.json"

#: Classification -> human-readable reply type.
REPLY_TYPES = {
    "interview_invite": "Interview invitation",
    "rejection": "Rejection",
    "offer": "Offer",
    "followup_needed": "Follow-up requested",
    "recruiter_outreach": "Recruiter outreach",
}

#: Suggested next action per classification.
_SUGGESTED_ACTION = {
    "interview_invite": "confirm_stage",
    "rejection": "confirm_stage",
    "offer": "confirm_stage",
    "followup_needed": "draft_followup",
    "recruiter_outreach": "review",
}

_PROPOSAL_STATUSES = ("proposed", "confirmed", "dismissed")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_proposals(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read the proposal log (empty list if missing/corrupt)."""
    try:
        raw = json.loads(Path(path or PROPOSALS_FILE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("Could not read proposals: %s", exc)
        return []
    return raw if isinstance(raw, list) else []


def save_proposals(
    proposals: list[dict[str, Any]], path: str | Path | None = None
) -> None:
    Path(path or PROPOSALS_FILE).write_text(
        json.dumps(proposals, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _rationale(classification: str, confidence: float, company: Any) -> str:
    label = REPLY_TYPES.get(classification, classification)
    where = f" from {company}" if company else ""
    return (
        f"Classified as {label}{where} "
        f"(heuristic confidence {confidence:.2f}). "
        "Verify the email before confirming — the classifier is heuristic."
    )


def scan(
    days: int = 14,
    proposals_path: str | Path | None = None,
) -> dict[str, Any]:
    """Scan recruiter mail and record stage-update proposals.

    **Never changes an application stage.** Each confident, unambiguous
    signal becomes a ``proposed`` proposal with a stable ``proposal_id``.
    Only confirmable stage changes become proposals (informational items
    belong to the action inbox); re-scanning never resurfaces a message
    that already has a proposal in any status.
    """
    import email_sync

    result = email_sync.scan_recruiter_emails(days=days, apply_updates=False)
    proposals = load_proposals(proposals_path)
    # Any status: a dismissed or confirmed proposal for the same message must
    # not resurface on re-scan.
    seen_keys = {
        (p.get("message_id"), p.get("application_id")) for p in proposals
    }
    created: list[dict[str, Any]] = []
    for item in result.get("proposed_updates", []):
        # The radar only carries confirmable stage changes. Informational
        # items (action "review", or "already_at_stage") belong to the
        # action inbox, not to the confirmation queue.
        if item.get("action") != "proposed" or not item.get("proposed_stage"):
            continue
        key = (item.get("message_id"), item.get("application_id"))
        if key in seen_keys:
            continue
        classification = str(item.get("classification") or "")
        proposal = {
            "proposal_id": uuid.uuid4().hex[:12],
            "message_id": item.get("message_id"),
            "classification": classification,
            "reply_type": REPLY_TYPES.get(classification, classification),
            "confidence": item.get("confidence"),
            "application_id": item.get("application_id"),
            "company": item.get("company"),
            "title": item.get("title"),
            "current_stage": item.get("current_stage"),
            "proposed_stage": item.get("proposed_stage"),
            "suggested_action": _SUGGESTED_ACTION.get(classification, "review"),
            "rationale": _rationale(
                classification, float(item.get("confidence") or 0),
                item.get("company"),
            ),
            "status": "proposed",
            "created_at": _utcnow_iso(),
            "history": [{"at": _utcnow_iso(), "status": "proposed"}],
        }
        proposals.append(proposal)
        created.append(proposal)
        seen_keys.add(key)
    save_proposals(proposals, proposals_path)
    return {
        "scanned": result.get("scanned", 0),
        "matched": result.get("matched", 0),
        "new_proposals": len(created),
        "open_proposals": sum(1 for p in proposals if p.get("status") == "proposed"),
        "proposals": created,
        **({"error": result["error"], "message": result.get("message")}
           if result.get("error") else {}),
    }


def list_proposals(
    status: str | None = None, path: str | Path | None = None
) -> list[dict[str, Any]]:
    """Proposals, newest first; optionally filtered by status."""
    proposals = load_proposals(path)
    if status is not None:
        if status not in _PROPOSAL_STATUSES:
            raise ValueError(f"status must be one of {_PROPOSAL_STATUSES}")
        proposals = [p for p in proposals if p.get("status") == status]
    return sorted(proposals, key=lambda p: str(p.get("created_at") or ""),
                  reverse=True)


def _find_proposal(
    proposals: list[dict[str, Any]], proposal_id: str
) -> dict[str, Any]:
    for p in proposals:
        if p.get("proposal_id") == proposal_id:
            return p
    raise KeyError(f"No proposal {proposal_id!r}")


def confirm(
    proposal_id: str,
    confirmed: bool = False,
    note: str = "",
    path: str | Path | None = None,
    applications_path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply a proposal's stage update — **only** with explicit confirmation.

    ``confirmed`` must be ``True``; anything else (including omission)
    refuses and leaves the proposal ``proposed``. This is the governance
    gate: no code path in this module applies a stage change without the
    human's explicit confirm.
    """
    proposals = load_proposals(path)
    proposal = _find_proposal(proposals, proposal_id)
    if proposal.get("status") != "proposed":
        raise ValueError(
            f"Proposal {proposal_id} is {proposal.get('status')}, not proposed"
        )
    if not confirmed:
        return {
            "proposal_id": proposal_id,
            "applied": False,
            "reason": "confirmation_required",
            "message": (
                "Not applied: confirming a stage update requires explicit "
                "human confirmation (confirmed=True)."
            ),
        }
    proposed_stage = proposal.get("proposed_stage")
    if not proposed_stage:
        raise ValueError(
            f"Proposal {proposal_id} proposes no stage change "
            f"(classification {proposal.get('classification')!r}); "
            "nothing to confirm."
        )
    import lifecycle

    apps_path = Path(applications_path or APPLICATIONS_FILE)
    # Stale-proposal guard: the application may have moved since the scan
    # (manual update, another proposal). Confirming against a moved
    # application would write a misleading history note — refuse instead.
    try:
        current = lifecycle.load_entries(apps_path)
        live = next(
            (e for e in current
             if str(e.get("job_id")) == str(proposal.get("application_id"))),
            None,
        )
    except Exception:
        live = None
    live_stage = str((live or {}).get("stage") or "")
    recorded_stage = str(proposal.get("current_stage") or "")
    if live is not None and live_stage and recorded_stage \
            and live_stage != recorded_stage:
        return {
            "proposal_id": proposal_id,
            "applied": False,
            "reason": "stale_proposal",
            "message": (
                f"Not applied: application moved from {recorded_stage!r} to "
                f"{live_stage!r} since this proposal was created. Dismiss it "
                "or re-scan."
            ),
        }
    entry = lifecycle.update_stage(
        apps_path,
        str(proposal.get("application_id") or ""),
        str(proposed_stage),
        note=(
            f"reply-radar confirmed by user: classified as "
            f"{proposal.get('classification')} "
            f"(confidence {proposal.get('confidence')}, "
            f"proposal {proposal_id})"
            + (f"; {note}" if note else "")
        ),
    )
    proposal["status"] = "confirmed"
    proposal["confirmed_at"] = _utcnow_iso()
    proposal["history"].append(
        {"at": _utcnow_iso(), "status": "confirmed",
         "note": str(note)[:280]}
    )
    save_proposals(proposals, path)
    log.info("Proposal %s confirmed: %s -> %s", proposal_id,
             proposal.get("current_stage"), proposed_stage)
    return {
        "proposal_id": proposal_id,
        "applied": True,
        "application_id": proposal.get("application_id"),
        "from_stage": proposal.get("current_stage"),
        "to_stage": proposed_stage,
        "stage": entry.get("stage"),
    }


def dismiss(
    proposal_id: str, reason: str = "", path: str | Path | None = None
) -> dict[str, Any]:
    """Dismiss a proposal with a recorded reason. The log entry is kept."""
    proposals = load_proposals(path)
    proposal = _find_proposal(proposals, proposal_id)
    if proposal.get("status") != "proposed":
        raise ValueError(
            f"Proposal {proposal_id} is {proposal.get('status')}, not proposed"
        )
    proposal["status"] = "dismissed"
    proposal["dismissed_at"] = _utcnow_iso()
    proposal["history"].append(
        {"at": _utcnow_iso(), "status": "dismissed",
         "note": str(reason)[:280]}
    )
    save_proposals(proposals, path)
    return {"proposal_id": proposal_id, "dismissed": True}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register reply-radar tools on the MCP server."""

    @mcp.tool()
    def reply_radar_scan(days: int = 14) -> dict:
        """Scan recruiter email and propose application stage updates.
        Proposal-only: never changes a stage."""
        return scan(days=days)

    @mcp.tool()
    def reply_radar_proposals(status: str | None = None) -> dict:
        """List reply-radar proposals (newest first), optionally filtered
        by status: proposed, confirmed, dismissed."""
        return {"proposals": list_proposals(status=status)}

    @mcp.tool()
    def reply_radar_confirm(proposal_id: str, confirmed: bool = False,
                            note: str = "") -> dict:
        """Apply a proposal's stage update. REQUIRES explicit human
        confirmation: pass confirmed=true only when the user has confirmed
        this specific proposal."""
        return confirm(proposal_id, confirmed=confirmed, note=note)

    @mcp.tool()
    def reply_radar_dismiss(proposal_id: str, reason: str = "") -> dict:
        """Dismiss a proposal with a recorded reason."""
        return dismiss(proposal_id, reason=reason)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(result: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return
    if isinstance(result, dict) and "proposals" in result:
        rows = result["proposals"]
        if not rows:
            print("No reply-radar proposals.")
            return
        for p in rows:
            print(f"[{p['proposal_id']}] {p.get('reply_type')} "
                  f"(conf {p.get('confidence')})")
            print(f"  {p.get('company')} — {p.get('title')}")
            print(f"  {p.get('current_stage')} -> {p.get('proposed_stage')} "
                  f"| {p.get('status')} | action: {p.get('suggested_action')}")
        return
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _cli_scan(args: Any) -> int:
    _print_result(scan(days=args.days), args.json)
    return 0


def _cli_list(args: Any) -> int:
    _print_result({"proposals": list_proposals(status=args.status)}, args.json)
    return 0


def _cli_confirm(args: Any) -> int:
    try:
        result = confirm(args.proposal_id, confirmed=args.confirm,
                         note=args.note or "")
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
    _print_result(result, args.json)
    return 0 if result.get("applied") else 1


def _cli_dismiss(args: Any) -> int:
    try:
        result = dismiss(args.proposal_id, reason=args.reason or "")
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}")
        return 2
    _print_result(result, args.json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "reply-radar",
        help="Classify recruiter replies; propose (never auto-apply) stage updates.",
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="radar_cmd", required=True)

    ps = sub.add_parser("scan", help="Scan recruiter mail; record proposals.")
    ps.add_argument("--days", type=int, default=14)
    ps.set_defaults(func=_cli_scan)

    pl = sub.add_parser("list", help="List proposals.")
    pl.add_argument("--status", default=None,
                    choices=list(_PROPOSAL_STATUSES))
    pl.set_defaults(func=_cli_list)

    pc = sub.add_parser("confirm",
                        help="Apply a proposal's stage update (needs --confirm).")
    pc.add_argument("proposal_id")
    pc.add_argument("--confirm", action="store_true",
                    help="Explicit human confirmation for THIS proposal.")
    pc.add_argument("--note", default="")
    pc.set_defaults(func=_cli_confirm)

    pd = sub.add_parser("dismiss", help="Dismiss a proposal with a reason.")
    pd.add_argument("proposal_id")
    pd.add_argument("--reason", default="")
    pd.set_defaults(func=_cli_dismiss)

    return {"reply-radar": lambda args: args.func(args)}
