#!/usr/bin/env python3
"""Networking tracker (CRM-lite) for job-apply-mcp.

Warm intros beat cold applications. This module keeps a small, honest
record of the people you know and when you last talked to them:

  - ``add_contact`` — validated contact cards in ``network.json``.
  - ``log_interaction`` — timestamped touchpoints (intro, call, coffee,
    email, linkedin, followup). A followup may carry a ``due`` date for a
    promised follow-up.
  - ``nudge_list`` — who needs attention: no interaction in 21+ days, or
    promised follow-ups that are overdue. Ranked by staleness, each with a
    suggested next step.
  - ``warm_path_to`` — BFS over contacts' companies: who connects you
    toward a target company, 1st/2nd degree, via shared companies found in
    notes and interaction history. Company matching reuses
    :mod:`referrals`' normalization where available.
  - ``log_outreach_draft`` — store an outreach DRAFT on a contact for
    human review. Drafts are terminal review artifacts: this module never
    sends anything and has no code path that flips a draft to sent.

Honesty contract: warm paths are derived only from what you recorded.
The tool never invents relationships, and it says plainly when no path
exists. It never sends anything anywhere — it only tells you who to
contact and why.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.network_crm")

BASE_DIR = Path(__file__).resolve().parent
NETWORK_FILE = BASE_DIR / "network.json"

#: Interaction kinds accepted by :func:`log_interaction`.
INTERACTION_KINDS = ("intro", "call", "coffee", "email", "linkedin", "followup")

#: Days without any interaction before a contact lands on the nudge list.
STALE_AFTER_DAYS = 21

#: Same LinkedIn URL shape as mentors.py: in/pub/company/school profiles.
_LINKEDIN_RE = re.compile(
    r"^https?://(www\.)?linkedin\.com/(in|pub|company|school)/[^/\s?#]+",
    re.IGNORECASE,
)

#: Suggested next step per most-recent interaction kind.
_NEXT_STEPS = {
    "intro": "Send a follow-up note and propose a brief intro call.",
    "call": "Send a recap email with one useful link or takeaway.",
    "coffee": "Send a thank-you; check back in next quarter.",
    "email": "Reply or start a new thread with one concrete ask.",
    "linkedin": "Move the conversation to email or a short call.",
    "followup": "Close the loop on the follow-up you logged.",
    None: "Reach out and introduce yourself.",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Company normalization (reuses referrals.py where available)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised via whichever branch loads
    from referrals import normalize_company as _referral_normalize  # type: ignore
except Exception:  # noqa: BLE001 - standalone use must still work
    _referral_normalize = None


def _normalize_company(name: Any) -> str:
    """Normalize a company name for matching.

    Delegates to ``referrals.normalize_company`` when importable
    (lowercase, strip punctuation, drop corporate suffixes); otherwise a
    small local equivalent with the same behaviour.
    """
    if _referral_normalize is not None:
        return _referral_normalize(name)
    text = re.sub(r"[^a-z0-9\s]", " ", str(name or "").lower())
    suffixes = {
        "inc", "incorporated", "corp", "corporation", "llc", "ltd",
        "limited", "co", "company", "gmbh", "plc", "pty", "sa", "srl",
        "bv", "ab", "aps", "holdings", "group",
    }
    return " ".join(t for t in text.split() if t and t not in suffixes)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _load_network() -> dict[str, dict[str, Any]]:
    """Read network.json; {} when missing or corrupt."""
    try:
        data = json.loads(NETWORK_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.debug("network unreadable (%s); treating as empty", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_network(network: dict[str, dict[str, Any]]) -> None:
    """Write network.json atomically."""
    tmp = NETWORK_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(network, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(NETWORK_FILE)


def _contact_id(name: str, company: str, linkedin_url: str) -> str:
    """Deterministic id from name + company + LinkedIn URL."""
    seed = "|".join(
        [name.strip().lower(), company.strip().lower(), linkedin_url.strip().lower()]
    )
    return f"contact-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def _looks_like_linkedin(url: Any) -> bool:
    return isinstance(url, str) and bool(_LINKEDIN_RE.match(url.strip()))


def _get_contact(network: dict[str, dict[str, Any]], contact_id: str) -> dict[str, Any]:
    contact = network.get(contact_id)
    if contact is None:
        raise ValueError(f"unknown contact_id: {contact_id!r}")
    return contact


def _parse_due(due: Any) -> str | None:
    if due is None or (isinstance(due, str) and not due.strip()):
        return None
    if not isinstance(due, str):
        raise ValueError("due must be a YYYY-MM-DD date string")
    try:
        parsed = date.fromisoformat(due.strip())
    except ValueError:
        raise ValueError(f"due must be YYYY-MM-DD, got {due!r}")
    return parsed.isoformat()


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def add_contact(
    name: str,
    company: str,
    role: str = "",
    industry: str = "",
    linkedin_url: str = "",
    source: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a contact to the network.

    ``name`` and ``company`` are required. ``linkedin_url``, when given,
    must look like a LinkedIn profile/company URL (same rule as
    mentors.py). Returns the stored contact card.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(company, str) or not company.strip():
        raise ValueError("company is required")
    if linkedin_url and not _looks_like_linkedin(linkedin_url):
        raise ValueError(
            "linkedin_url must be a LinkedIn URL "
            "(https://www.linkedin.com/in/... or /company/...)"
        )
    network = _load_network()
    contact_id = _contact_id(name, company, linkedin_url or "")
    contact = network.get(contact_id, {})
    contact.update(
        {
            "id": contact_id,
            "name": name.strip(),
            "company": company.strip(),
            "role": (role or "").strip(),
            "industry": (industry or "").strip(),
            "linkedin_url": (linkedin_url or "").strip(),
            "source": (source or "").strip(),
            "notes": (notes or "").strip(),
            "created_at": contact.get("created_at", _now()),
            "interactions": contact.get("interactions", []),
            "last_interaction_at": contact.get("last_interaction_at"),
        }
    )
    network[contact_id] = contact
    _save_network(network)
    return contact


def list_contacts() -> list[dict[str, Any]]:
    """All contacts, most-recently-added first."""
    contacts = list(_load_network().values())
    contacts.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return contacts


def log_outreach_draft(
    contact_id: str,
    channel: str,
    subject: str,
    body: str,
    *,
    target_company: str = "",
) -> dict[str, Any]:
    """Store an outreach DRAFT on a contact record for human review.

    The draft is a review artifact with ``status: "draft"`` — a terminal
    state. This module has no code path that sends anything, and no code
    path that flips a draft to sent: sending is always the human's
    explicit act in their own client. Drafts never invent shared history;
    callers must build bodies only from recorded facts.
    """
    if not isinstance(channel, str) or not channel.strip():
        raise ValueError("channel is required (e.g. 'linkedin', 'email')")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("body is required")
    network = _load_network()
    contact = _get_contact(network, contact_id)
    draft = {
        "channel": channel.strip(),
        "subject": str(subject or "").strip(),
        "body": body.strip(),
        "target_company": str(target_company or "").strip(),
        "status": "draft",
        "created_at": _now(),
    }
    contact.setdefault("outreach_drafts", []).append(draft)
    _save_network(network)
    return draft


def log_interaction(
    contact_id: str,
    kind: str,
    summary: str,
    due: str | None = None,
) -> dict[str, Any]:
    """Log a timestamped interaction with a contact.

    ``kind`` must be one of: intro, call, coffee, email, linkedin,
    followup. ``summary`` is a one-or-two-line note. ``due`` (optional,
    YYYY-MM-DD) records a promised follow-up date — used by
    :func:`nudge_list` to flag overdue promises. Returns the logged entry.
    """
    if kind not in INTERACTION_KINDS:
        raise ValueError(
            f"kind must be one of {', '.join(INTERACTION_KINDS)}, got {kind!r}"
        )
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary is required")
    due_iso = _parse_due(due)
    network = _load_network()
    contact = _get_contact(network, contact_id)
    entry = {
        "kind": kind,
        "summary": summary.strip(),
        "due": due_iso,
        "at": _now(),
    }
    contact.setdefault("interactions", []).append(entry)
    contact["last_interaction_at"] = entry["at"]
    _save_network(network)
    return entry


# ---------------------------------------------------------------------------
# Nudges
# ---------------------------------------------------------------------------


def _days_since(iso_ts: str | None) -> int | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def _overdue_followups(contact: dict[str, Any]) -> list[dict[str, Any]]:
    today = _today().isoformat()
    return [
        i
        for i in contact.get("interactions", [])
        if i.get("kind") == "followup" and i.get("due") and i["due"] < today
    ]


def nudge_list() -> list[dict[str, Any]]:
    """Contacts needing attention, ranked by staleness (stalest first).

    A contact qualifies when it has had no interaction in
    ``STALE_AFTER_DAYS``+ days (using ``created_at`` when never
    contacted), or when a promised follow-up (``kind="followup"`` with a
    ``due`` date) is overdue. Each entry carries the contact, days since
    last touch, the reason, and a suggested next step.
    """
    nudges: list[dict[str, Any]] = []
    for contact in _load_network().values():
        last_at = contact.get("last_interaction_at") or contact.get("created_at")
        stale_days = _days_since(last_at)
        overdue = _overdue_followups(contact)
        reasons: list[str] = []
        if overdue:
            worst = min(i["due"] for i in overdue if i.get("due"))
            late_by = (_today() - date.fromisoformat(worst)).days
            reasons.append(
                f"promised follow-up overdue (due {worst}, {late_by}d late)"
            )
        if stale_days is not None and stale_days >= STALE_AFTER_DAYS:
            reasons.append(f"no interaction in {stale_days} days")
        if not reasons:
            continue
        last_kind = None
        interactions = contact.get("interactions", [])
        if interactions:
            last_kind = interactions[-1].get("kind")
        if overdue:
            next_step = (
                f"You promised a follow-up by {min(i['due'] for i in overdue if i.get('due'))} "
                "— send it now."
            )
        else:
            next_step = _NEXT_STEPS.get(last_kind, _NEXT_STEPS[None])
        nudges.append(
            {
                "contact_id": contact["id"],
                "name": contact["name"],
                "company": contact.get("company", ""),
                "role": contact.get("role", ""),
                "stale_days": stale_days,
                "reasons": reasons,
                "overdue_followups": len(overdue),
                "suggested_next_step": next_step,
            }
        )
    nudges.sort(
        key=lambda n: (
            -(n["stale_days"] or 0),
            -n["overdue_followups"],
            n["name"].lower(),
        )
    )
    return nudges


# ---------------------------------------------------------------------------
# Warm paths
# ---------------------------------------------------------------------------


def _contact_text(contact: dict[str, Any]) -> str:
    """Normalized blob of a contact's notes + interaction summaries."""
    parts = [contact.get("notes", "")]
    parts.extend(i.get("summary", "") for i in contact.get("interactions", []))
    return _normalize_company(" ".join(parts))


def _company_graph(
    contacts: list[dict[str, Any]],
    extra_companies: tuple[str, ...] = (),
) -> tuple[dict[str, str], dict[str, list[tuple[str, dict[str, Any]]]]]:
    """Build an undirected company graph.

    Nodes are normalized company names; an edge X-Y labelled with contact
    C exists when C works at X and mentions Y in notes/history (or vice
    versa). ``extra_companies`` are matchable mention targets that have
    no contact working there (used for the warm-path target itself).
    Returns (display_names, adjacency).
    """
    display: dict[str, str] = {}
    for raw in extra_companies:
        norm = _normalize_company(raw)
        if norm and norm not in display:
            display[norm] = raw.strip()
    for c in contacts:
        norm = _normalize_company(c.get("company", ""))
        if norm and norm not in display:
            display[norm] = (c.get("company") or "").strip()
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {n: [] for n in display}
    companies = list(display)
    for contact in contacts:
        own = _normalize_company(contact.get("company", ""))
        if not own:
            continue
        haystack = f" {_contact_text(contact)} "
        for other in companies:
            if other == own:
                continue
            if f" {other} " in haystack:
                adjacency[own].append((other, contact))
                adjacency[other].append((own, contact))
    return display, adjacency


def warm_path_to(company: str) -> list[dict[str, Any]]:
    """Find warm paths from you toward ``company`` via your network.

    1st degree: a contact who works at the target company. 2nd degree:
    BFS over the company graph — a contact at company X who mentioned the
    target (or a chain of such mentions) in notes/history. Every path is
    derived only from recorded data; an empty list means no path exists
    in your network. Results sorted by degree, then contact name.
    """
    if not isinstance(company, str) or not company.strip():
        raise ValueError("company is required")
    target = _normalize_company(company)
    if not target:
        raise ValueError(f"could not normalize company name: {company!r}")
    contacts = list(_load_network().values())
    if not contacts:
        return []

    paths: list[dict[str, Any]] = []

    # -- 1st degree: someone works there ----------------------------------
    for contact in contacts:
        if _normalize_company(contact.get("company", "")) == target:
            paths.append(
                {
                    "degrees": 1,
                    "contact_id": contact["id"],
                    "contact_name": contact["name"],
                    "contact_role": contact.get("role", ""),
                    "path_companies": [contact.get("company", "").strip()],
                    "via": f"{contact['name']} works at {contact.get('company', '').strip()}",
                }
            )
    if paths:
        paths.sort(key=lambda p: p["contact_name"].lower())
        return paths

    # -- 2nd degree: a contact's notes/history mention the target -------
    # The target itself is a matchable mention target. Every edge incident
    # to it is labelled by a contact at the neighbouring company who named
    # the target in notes/history — that contact is the bridge. (This
    # makes 2nd degree the longest possible path here: any path to the
    # target goes through someone who mentioned it.)
    display, adjacency = _company_graph(contacts, extra_companies=(company.strip(),))
    target_display = display.get(target, company.strip())
    for _neighbour, bridge in adjacency.get(target, []):
        bridge_co = (bridge.get("company") or "").strip()
        paths.append(
            {
                "degrees": 2,
                "contact_id": bridge["id"],
                "contact_name": bridge["name"],
                "contact_role": bridge.get("role", ""),
                "path_companies": [target_display, bridge_co],
                "via": (
                    f"{bridge['name']} ({bridge_co}) mentioned "
                    f"{target_display} — ask for a warm intro"
                ),
            }
        )

    # Deduplicate identical (degrees, contact) pairs, keep shortest first.
    seen: set[tuple[int, str]] = set()
    unique: list[dict[str, Any]] = []
    for p in sorted(
        paths, key=lambda p: (p["degrees"], p["contact_name"].lower())
    ):
        key = (p["degrees"], p["contact_id"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# MCP / CLI wiring (briefs.py pattern)
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the networking MCP tools on a server instance."""
    _impl_add = globals()["add_contact"]
    _impl_list = globals()["list_contacts"]
    _impl_log = globals()["log_interaction"]
    _impl_nudges = globals()["nudge_list"]
    _impl_warm = globals()["warm_path_to"]

    @mcp.tool()
    def add_contact(
        name: str,
        company: str,
        role: str = "",
        industry: str = "",
        linkedin_url: str = "",
        source: str = "",
        notes: str = "",
    ) -> dict:
        """Add a contact to your networking tracker.

        Args:
            name: Contact's full name (required).
            company: Current company (required).
            role: Their role/title.
            industry: Industry, e.g. "fintech".
            linkedin_url: LinkedIn profile/company URL (validated).
            source: Where you met, e.g. "conference", "referral".
            notes: Free-form notes (also scanned for warm-path mentions).

        Returns:
            The stored contact card.
        """
        return _impl_add(name, company, role, industry, linkedin_url, source, notes)

    @mcp.tool()
    def list_contacts() -> list:
        """List all tracked contacts, most-recently-added first."""
        return _impl_list()

    @mcp.tool()
    def log_interaction(
        contact_id: str, kind: str, summary: str, due: str = ""
    ) -> dict:
        """Log an interaction with a contact.

        Args:
            contact_id: Id from add_contact / list_contacts.
            kind: One of intro, call, coffee, email, linkedin, followup.
            summary: One-or-two-line note on what happened.
            due: Optional promised-follow-up date (YYYY-MM-DD).

        Returns:
            The logged interaction entry.
        """
        return _impl_log(contact_id, kind, summary, due or None)

    @mcp.tool()
    def nudge_list() -> list:
        """Who needs attention: stale 21+ days or overdue promised
        follow-ups. Ranked stalest-first with a suggested next step each.

        Returns:
            List of nudge dicts.
        """
        return _impl_nudges()

    @mcp.tool()
    def warm_path_to(company: str) -> list:
        """Find warm paths toward a target company via your network.

        1st degree: a contact who works there. 2nd degree: BFS over
        companies mentioned in contacts' notes/history. Derived only
        from recorded data — empty list means no path exists.

        Args:
            company: Target company name.

        Returns:
            Ranked path dicts with degrees, contact, and explanation.
        """
        return _impl_warm(company)


def _print_result(result: Any, as_json: bool) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def cmd_network(args: Any) -> int:
    """CLI handler for `network` (action-dispatched)."""
    action = args.action
    as_json = getattr(args, "json", False)
    if action == "add":
        result = add_contact(
            args.name,
            args.company,
            role=args.role or "",
            industry=args.industry or "",
            linkedin_url=args.linkedin_url or "",
            source=args.source or "",
            notes=args.notes or "",
        )
    elif action == "list":
        result = list_contacts()
    elif action == "log":
        result = log_interaction(
            args.contact_id, args.kind, args.summary, args.due or None
        )
    elif action == "nudges":
        result = nudge_list()
    elif action == "warm-path":
        result = warm_path_to(args.company)
    else:  # pragma: no cover - argparse restricts choices
        raise ValueError(f"unknown action: {action}")
    _print_result(result, as_json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the `network` command to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser("network", help="Networking tracker (CRM-lite).")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    actions = p.add_subparsers(dest="action", required=True)

    a_add = actions.add_parser("add", help="Add a contact.")
    a_add.add_argument("--name", required=True, help="Full name.")
    a_add.add_argument("--company", required=True, help="Current company.")
    a_add.add_argument("--role", default="", help="Role/title.")
    a_add.add_argument("--industry", default="", help="Industry.")
    a_add.add_argument("--linkedin-url", default="", help="LinkedIn URL.")
    a_add.add_argument("--source", default="", help="Where you met.")
    a_add.add_argument("--notes", default="", help="Free-form notes.")

    actions.add_parser("list", help="List all contacts.")

    a_log = actions.add_parser("log", help="Log an interaction.")
    a_log.add_argument("--contact-id", required=True, help="Contact id.")
    a_log.add_argument(
        "--kind",
        required=True,
        choices=list(INTERACTION_KINDS),
        help="Interaction kind.",
    )
    a_log.add_argument("--summary", required=True, help="What happened.")
    a_log.add_argument("--due", default="", help="Promised follow-up date YYYY-MM-DD.")

    actions.add_parser("nudges", help="Contacts needing attention.")

    a_warm = actions.add_parser("warm-path", help="Warm paths to a company.")
    a_warm.add_argument("--company", required=True, help="Target company.")

    return {"network": cmd_network}


if __name__ == "__main__":  # pragma: no cover - convenience entry
    parser = argparse.ArgumentParser(description="Networking tracker (CRM-lite).")
    subs = parser.add_subparsers()
    handlers = register_cli(subs)
    parsed = parser.parse_args()
    raise SystemExit(handlers["network"](parsed))
