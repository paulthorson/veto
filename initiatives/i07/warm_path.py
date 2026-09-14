#!/usr/bin/env python3
"""Warm-path planner (Initiative 07, epic 5).

Identifies user-provided connections toward a target company — via the
network CRM's warm paths and the referral radar — and DRAFTS outreach for
each path.

**The planner never sends anything.** Drafts are written to the draft
outbox (``~/.local/share/veto/warm_path_drafts.jsonl`` by default —
outside the repo tree, so real contact names are never one ``git add``
away from a commit) for the user to review, edit, and send
themselves in their own client. There is deliberately no send path in
this module: no function named ``send_*`` exists, and the test suite
asserts that none ever appears.

Honesty rules (hard, inherited from referrals/network_crm):
  * Relationship strength is never inferred or fabricated.
  * Drafts never invent shared history, shared employers, or mutual
    acquaintances — only facts present in the user's own records.
  * Drafts name ONLY the contact's recorded company as their employer.
    The target company appears only as the stated ask (the intro-ask
    template for bridge contacts who merely mentioned the target), never
    as the contact's workplace. "I see you're at {X}" is emitted only
    for the contact's actual recorded company — never for the target.
  * An empty network produces an honest empty plan, never invented people.

Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.i07.warm_path")

BASE_DIR = Path(__file__).resolve().parent


def _default_outbox() -> Path:
    """Default draft outbox, OUTSIDE the repo tree.

    Drafts contain real contact names, so the default lives in the user's
    XDG data dir (``~/.local/share/veto/``), never in the repository —
    drafts must never be one ``git add`` away from a commit.
    """
    data_home = os.environ.get("XDG_DATA_HOME") or str(
        Path.home() / ".local" / "share"
    )
    return Path(data_home) / "veto" / "warm_path_drafts.jsonl"


#: Draft outbox: every draft the planner produces, for human review.
#: Reassign in tests.
DRAFT_OUTBOX = _default_outbox()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Append a draft to the outbox. Review artifact only — never sent."""
    DRAFT_OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    draft = dict(draft)
    draft["draft_id"] = "draft-" + uuid.uuid4().hex[:12]
    draft["created_at"] = _now()
    draft["status"] = "draft"  # terminal: no code path ever changes this
    with DRAFT_OUTBOX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(draft, ensure_ascii=False) + "\n")
    return draft


def list_drafts() -> list[dict[str, Any]]:
    """Read the draft outbox for human review."""
    drafts: list[dict[str, Any]] = []
    try:
        with DRAFT_OUTBOX.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        drafts.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        pass
    return drafts


def _draft_key(
    kind: str, target_company: str, contact_name: str, contact_company: str
) -> tuple[str, str, str, str]:
    """Identity of a draft: reruns reuse, never duplicate."""
    return (
        str(kind or "").strip().lower(),
        str(target_company or "").strip().lower(),
        str(contact_name or "").strip().lower(),
        str(contact_company or "").strip().lower(),
    )


def _intro_ask_draft(
    contact: dict[str, Any],
    target_company: str,
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Draft an introduction ASK for a bridge contact (2nd degree).

    The bridge contact does NOT work at the target company — they merely
    mentioned it in recorded notes/history. The target company is named
    ONLY as the ask; the ONLY employer named in the draft is the
    contact's actual recorded company. This is the honesty-critical
    template: it must never say "I see you're at {target}".
    """
    profile = profile or {}
    first = str(contact.get("name") or "").strip().split(" ")[0]
    actual = str(contact.get("company") or "").strip()
    target = str(target_company or "").strip()
    my_name = str(profile.get("full_name") or "").strip()
    my_titles = profile.get("target_titles") or []
    my_title = str(my_titles[0]).strip() if my_titles else "engineer"

    subject = f"Quick intro ask — {target}" if target else "Quick intro ask"
    lines = [f"Hi {first}," if first else "Hi,", ""]
    if actual:
        # The ONLY "I see you're at {X}" in this module: X is always the
        # contact's actual recorded company, never the target.
        lines += [f"I see you're at {actual}.", ""]
    lines.append(
        f"You mentioned {target} in passing, and I'm exploring {my_title} "
        f"roles there — it's high on my list."
        if target
        else f"I'm exploring {my_title} roles and would value your perspective."
    )
    lines += [
        "",
        (
            f"Would you be comfortable introducing me to anyone you know at "
            f"{target}? Happy to share a short blurb and my resume to make "
            f"it easy — and no worries at all if not."
            if target
            else "Would you be open to a brief chat about your experience?"
        ),
        "",
        "Thanks so much,",
        my_name or "—",
    ]
    return {
        "kind": "warm-intro-ask",
        "to": str(contact.get("name") or "").strip(),
        "subject": subject,
        "body": "\n".join(lines),
    }


def plan_warm_path(
    target_company: str,
    *,
    profile: dict[str, Any] | None = None,
    max_paths: int = 5,
    synthetic: bool = False,
) -> dict[str, Any]:
    """Build a warm-path outreach plan toward ``target_company``.

    Returns ranked paths (CRM warm paths first, then referral prospects),
    each with a DRAFT message, the evidence behind it, and an honesty
    checklist. Empty network -> honest empty plan with guidance.

    Honesty: drafts are built against each contact's ACTUAL recorded
    company. Second-degree bridge contacts (who merely mentioned the
    target) get an explicit intro-ask template that names the target only
    as the ask — never as the contact's employer.
    """
    import network_crm as _crm
    import referrals as _ref

    if not str(target_company or "").strip():
        return {"ok": False, "error": "target_company is required"}

    plans: list[dict[str, Any]] = []

    # Rerun guard: a draft is identified by (kind, target, contact name,
    # contact company). Re-running a plan reuses the existing draft
    # instead of appending a duplicate to the outbox.
    existing_by_key = {
        _draft_key(
            d.get("kind", ""),
            d.get("target_company", ""),
            d.get("contact_name", ""),
            d.get("contact_company", ""),
        ): d
        for d in list_drafts()
    }

    def _emit_draft(payload: dict[str, Any]) -> dict[str, Any]:
        key = _draft_key(
            payload.get("kind", ""),
            payload.get("target_company", ""),
            payload.get("contact_name", ""),
            payload.get("contact_company", ""),
        )
        prev = existing_by_key.get(key)
        if prev is not None:
            return prev
        record = _record_draft(payload)
        existing_by_key[key] = record
        return record

    # 1. CRM warm paths (user's own recorded network).
    try:
        crm_paths = _crm.warm_path_to(target_company)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    for path in crm_paths[:max_paths]:
        degrees = path.get("degrees") or 1
        # The contact's ACTUAL recorded company — the last company on the
        # path. For a 1st-degree path this IS the target company; for a
        # 2nd-degree bridge it is the bridge's own employer (NOT the target).
        actual_company = str((path.get("path_companies") or [""])[-1] or "").strip()
        contact = {
            "name": path.get("contact_name", ""),
            "company": actual_company,
            "position": path.get("contact_role", ""),
        }
        if degrees >= 2:
            # Bridge contact: they do NOT work at the target company. The
            # target is named only as the ask, never as their employer.
            draft_text = _intro_ask_draft(contact, target_company, profile=profile)
            kind = "crm_bridge_intro_ask"
            evidence = [
                "recorded contact",
                path.get("via", ""),
                "bridge: does not work at the target company",
            ]
        else:
            # 1st degree: the contact actually works at the target company,
            # so naming it as their employer is honest.
            draft_text = _ref.draft_outreach(
                contact,
                job={"company": actual_company},
                profile=profile,
                kind="warm",
            )
            kind = "crm_warm_path"
            evidence = ["recorded contact", path.get("via", ""), "same company"]
        record = _emit_draft(
            {
                "kind": kind,
                "target_company": target_company,
                "degrees": degrees,
                "contact_name": contact["name"],
                "contact_company": actual_company,
                "via": path.get("via"),
                "to": draft_text["to"],
                "subject": draft_text["subject"],
                "body": draft_text["body"],
                "evidence": evidence,
                "synthetic": synthetic,
            }
        )
        plans.append(
            {
                "source": "network_crm",
                "degrees": degrees,
                "contact_name": path.get("contact_name"),
                "contact_company": actual_company,
                "via": path.get("via"),
                "draft_id": record["draft_id"],
                "draft": {"subject": record["subject"], "body": record["body"]},
            }
        )

    # 2. Referral-radar prospects at the target company.
    try:
        connections = _ref.load_network()
    except Exception:  # noqa: BLE001 - radar is best-effort
        connections = []
    if connections:
        ranked = _ref.rank_referrals(connections, [target_company], profile)
        # Dedup on (contact name, company): the same name at different
        # companies is a different person and gets their own draft.
        seen = {
            (
                str(p.get("contact_name") or "").strip().lower(),
                str(p.get("contact_company") or "").strip().lower(),
            )
            for p in plans
        }
        for prospect in ranked[:max_paths]:
            actual_company = str(prospect.get("company") or "").strip()
            prospect_key = (
                str(prospect.get("name") or "").strip().lower(),
                actual_company.lower(),
            )
            if prospect_key in seen:
                continue
            seen.add(prospect_key)
            # Draft against the contact's ACTUAL recorded company — never
            # the target. "I see you're at {X}" is honest exactly when X is
            # the contact's own employer (evidence "same company"); for
            # everyone else the draft names their real employer and the
            # target appears nowhere as their workplace.
            draft_text = _ref.draft_outreach(
                {
                    "name": prospect.get("name", ""),
                    "company": actual_company,
                    "position": prospect.get("position", ""),
                },
                job={"company": actual_company},
                profile=profile,
                kind="warm",
            )
            record = _emit_draft(
                {
                    "kind": "referral_prospect",
                    "target_company": target_company,
                    "score": prospect.get("score"),
                    "reasons": prospect.get("reasons"),
                    "contact_name": prospect.get("name"),
                    "contact_company": actual_company,
                    "to": draft_text["to"],
                    "subject": draft_text["subject"],
                    "body": draft_text["body"],
                    "evidence": prospect.get("evidence", []),
                    "synthetic": synthetic,
                }
            )
            plans.append(
                {
                    "source": "referrals",
                    "contact_name": prospect.get("name"),
                    "contact_company": actual_company,
                    "score": prospect.get("score"),
                    "reasons": prospect.get("reasons"),
                    "evidence": prospect.get("evidence"),
                    "draft_id": record["draft_id"],
                    "draft": {"subject": record["subject"], "body": record["body"]},
                }
            )
            if len(plans) >= max_paths:
                break

    honesty_checklist = [
        "Every path is derived from your own recorded contacts — none invented.",
        "Every draft names only the contact's recorded employer as their "
        "workplace. The target company appears only as the stated ask (the "
        "intro-ask template for bridge contacts), never as the contact's "
        "employer — 'I see you're at {X}' is emitted only for the contact's "
        "actual recorded company.",
        "Relationship strength is not inferred; every contact is labeled 'connection'.",
        "Nothing has been sent. Review each draft, edit in your voice, send it yourself.",
    ]
    if not plans:
        return {
            "ok": True,
            "target_company": target_company,
            "plans": [],
            "honesty_checklist": honesty_checklist,
            "guidance": (
                f"No warm path to {target_company} in your recorded network yet. "
                "Add contacts you actually know with network_crm.add_contact, "
                "log real interactions, and re-run — the planner will never "
                "invent people to fill the gap."
            ),
        }
    return {
        "ok": True,
        "target_company": target_company,
        "plans": plans,
        "honesty_checklist": honesty_checklist,
        "guidance": (
            f"{len(plans)} draft(s) ready for review in the draft outbox. "
            "Nothing has been sent and nothing will be sent by this tool."
        ),
    }


def discard_draft(draft_id: str) -> dict[str, Any]:
    """Remove a draft from the outbox (human rejected it)."""
    kept: list[dict[str, Any]] = []
    found = False
    for draft in list_drafts():
        if draft.get("draft_id") == draft_id:
            found = True
            continue
        kept.append(draft)
    if not found:
        return {"ok": False, "error": f"unknown draft id {draft_id!r}"}
    with DRAFT_OUTBOX.open("w", encoding="utf-8") as fh:
        for draft in kept:
            fh.write(json.dumps(draft, ensure_ascii=False) + "\n")
    return {"ok": True, "discarded": draft_id}
