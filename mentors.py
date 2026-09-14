#!/usr/bin/env python3
"""Mentor matchmaking for the job-apply MCP server.

Connects mentors with people seeking mentorship, opt-in only on both sides:

* Mentors opt in with ``mentor_opt_in(profile)`` — they publish a public
  mentor card: a display name **or just initials** (their privacy choice),
  industry, current role, seniority band, topics, a LinkedIn profile URL,
  availability, a ``max_mentees`` cap, and a short bio. Only the LinkedIn
  URL is ever shared as contact info — no emails, no phone numbers.
* Mentees answer a short questionnaire (``matchmake(answers)``) and get
  ranked mentors with human-readable reasons per match.
* ``connection_draft(mentor_id, mentee_name, mentee_goal)`` writes a
  LinkedIn connection-request note draft (under LinkedIn's 300-character
  limit). The mentee sends it themselves on LinkedIn — this tool never
  sends anything, anywhere, on anyone's behalf.
* ``suggest_mentors_from_training()`` reads your soft-skills and
  AI-proficiency practice history (both imports are defensive — the tool
  degrades gracefully if they are unavailable) and points you at mentors
  for your weakest areas, with evidence drawn from real scores.
* ``session_agenda(mentor_id, mentee_goal)`` builds a 30-minute first-
  session plan as markdown — a starting template, not a script.
* ``matchmake(answers, consent_preview=False)``: the questionnaire now
  takes context, urgency, expected outcome, and (voluntarily supplied)
  identity preferences — the last are echoed, never ranked on. With
  ``consent_preview=True``, matches go through the Initiative 07
  redacted preview (see initiatives/i07/CONTRACTS.md): contact fields
  are sealed until both parties approve the introduction via the consent
  handshake. The default full mode remains for the self-serve path.
* Mentor cards may carry ``availability_windows``, ``boundaries``
  (offers/wont/note), and ``preferred_contact`` ("hidden" default —
  contact sealed until mutual consent; "linkedin_public" opts the URL
  into the public discovery card). **Hidden is sealed everywhere**:
  ``matchmake`` (either mode), ``connection_draft``, and
  ``publish_mentor_card`` never emit the LinkedIn URL of a hidden mentor —
  the URL is revealed only after mutual consent (see
  ``initiatives.i07.consent.reveal_contact``).
* ``matchmake`` is the single safety choke point: it consults
  ``initiatives.i07.safety.discovery_exclusions()`` internally and hides
  blocked/quarantined mentors from every surface (web UI, dashboard,
  CLI, MCP) without each caller having to remember. Pass
  ``apply_safety_exclusions=False`` only to deliberately opt out.
* ``rate_mentorship(mentor_id, stars, note="", by="mentee")`` records
  two-sided ratings ("mentee" or "mentor" perspective); per-side averages
  show on cards and match results.
* ``prep_for_topic(topic)`` names the concrete drill or lesson to do
  before reaching out. Prep is a recommendation, never a hard block —
  it also ships in ``matchmake`` results and connection drafts.

Boundaries (enforced, not just documented):
  - Only mentors with remaining capacity (``max_mentees`` minus recorded
    mentees) are ever surfaced by ``matchmake``.
  - No contact info beyond what the mentor published: LinkedIn URL only.
  - The mentee always initiates. ``record_outreach`` lets a mentee log
    that they reached out, which decrements the mentor's capacity so the
    directory stays honest.
  - ``publish_mentor_card`` writes to the community directory outbox
    (``contributions/outbox/mentor-<id>.json``) ONLY with explicit
    ``confirmed=True``. It never auto-publishes.
  - An empty directory returns an empty match list plus guidance — mentors
    are never invented.

Mentor cards live in ``mentors.json`` next to this module. Reassign
``mentors.MENTORS_FILE`` to a temp path in tests.

Stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.mentors")

BASE_DIR = Path(__file__).resolve().parent

#: Local mentor directory. Reassign in tests to redirect storage.
MENTORS_FILE = BASE_DIR / "mentors.json"

#: Where a confirmed card is staged for contribution to the community
#: mentor directory. Reassignable for tests.
OUTBOX_DIR = BASE_DIR / "contributions" / "outbox"

#: Fixed matchmaking topic list. Both mentors and mentees choose from this.
TOPICS: tuple[str, ...] = (
    "interviewing",
    "salary_negotiation",
    "career_switch",
    "leadership",
    "executive_presence",
    "ai_skills",
    "networking",
    "resume_craft",
)

_LINKEDIN_RE = re.compile(
    r"^https?://(www\.)?linkedin\.com/(in|pub|company|school)/[^/\s?#]+",
    re.IGNORECASE,
)

_VALID_BANDS = (
    "ic1-3",
    "ic4",
    "ic5+",
    "manager",
    "director+",
    "executive",
)

_MAX_NOTE_LEN = 300  # LinkedIn connection-request note limit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _load_directory() -> dict[str, dict[str, Any]]:
    """Read mentors.json; {} when missing or corrupt."""
    try:
        data = json.loads(MENTORS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        log.debug("mentors directory unreadable (%s); treating as empty", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save_directory(directory: dict[str, dict[str, Any]]) -> None:
    """Write mentors.json atomically."""
    tmp = MENTORS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(directory, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(MENTORS_FILE)


def _card_id(linkedin_url: str) -> str:
    """Deterministic id from the LinkedIn URL (one card per profile)."""
    digest = hashlib.sha1(linkedin_url.strip().lower().encode("utf-8")).hexdigest()
    return f"mentor-{digest[:12]}"


# ---------------------------------------------------------------------------
# Safety choke point (Initiative 07, epic 6)
# ---------------------------------------------------------------------------


def _safety_exclusions() -> set[str]:
    """Mentor ids the local user ("me") must never see in discovery.

    The union of actors the user blocked and every quarantined actor —
    see ``initiatives.i07.safety.discovery_exclusions``. The import is
    lazy so mentors.py stays importable without the initiatives package
    on the path. Fails open to an empty set only when the safety module
    itself cannot be reached; exclusions passed explicitly via
    ``exclude_mentor_ids`` are always honored. A corrupt safety state
    raises ``SafetyStateError`` (fail closed: discovery is refused rather
    than silently surfacing blocked or quarantined actors).
    """
    try:
        from initiatives.i07 import safety as _safety
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("initiatives.i07.safety unavailable (%s); no safety exclusions", exc)
        return set()
    try:
        return set(_safety.discovery_exclusions("me"))
    except _safety.SafetyStateError:
        raise  # corrupt state: fail closed, discovery refused
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("safety.discovery_exclusions failed (%s); no safety exclusions", exc)
        return set()


def _exclusion_rejection(mentor_id: str) -> dict[str, Any] | None:
    """Loud refusal when a direct-id tool targets an excluded mentor.

    Returns an ``{"ok": False, ...}`` error dict when ``mentor_id`` is in
    the local user's safety exclusions (blocked or quarantined), else
    ``None``. Direct-id tools (connection_draft, session_agenda,
    record_outreach) must call this before touching the directory.
    """
    mid = str(mentor_id or "").strip()
    if not mid:
        return None
    try:
        from initiatives.i07 import safety as _safety
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        excluded = _safety.discovery_exclusions("me")
    except _safety.SafetyStateError:
        # Corrupt safety state: fail closed — refuse the action rather
        # than risk reaching out to a blocked/quarantined mentor.
        return {
            "ok": False,
            "error": (
                "safety state is unreadable; refusing to act rather than "
                "risk contacting a blocked or quarantined mentor. Only a "
                "human can clear or repair the underlying safety store."
            ),
        }
    except Exception:  # pragma: no cover - defensive
        return None
    if mid not in excluded:
        return None
    try:
        reason = (
            "under safety review (quarantined)"
            if _safety.is_quarantined(mid)
            else "blocked by you"
        )
    except Exception:  # pragma: no cover - defensive
        reason = "excluded by your safety settings"
    return {
        "ok": False,
        "error": (
            f"mentor {mid!r} is {reason}: this tool refuses to draft, "
            "plan, or log outreach to excluded mentors."
        ),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _looks_like_linkedin(url: Any) -> bool:
    return isinstance(url, str) and bool(_LINKEDIN_RE.match(url.strip()))


def _validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Check an opt-in profile; returns the normalized card fields or raises."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be a dict")

    def need(key: str) -> str:
        val = profile.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"profile[{key!r}] is required and must be a non-empty string")
        return val.strip()

    name = need("name")  # display name or initials — the mentor's privacy choice
    industry = need("industry")
    role = need("role")
    seniority = str(profile.get("seniority") or "").strip()
    if seniority not in _VALID_BANDS:
        raise ValueError(
            f"profile['seniority'] must be one of {_VALID_BANDS}, got {seniority!r}"
        )
    linkedin_url = need("linkedin_url")
    if not _looks_like_linkedin(linkedin_url):
        raise ValueError(
            "profile['linkedin_url'] must be a LinkedIn profile URL "
            "(https://www.linkedin.com/in/...)"
        )
    topics = profile.get("topics") or []
    if not isinstance(topics, list) or not topics:
        raise ValueError("profile['topics'] must be a non-empty list from TOPICS")
    bad = [t for t in topics if t not in TOPICS]
    if bad:
        raise ValueError(f"unknown topics {bad!r}; choose from {list(TOPICS)}")
    max_mentees = profile.get("max_mentees", 3)
    if not isinstance(max_mentees, int) or max_mentees < 1:
        raise ValueError("profile['max_mentees'] must be a positive integer")
    availability = profile.get("availability", "")
    if availability is not None and not isinstance(availability, str):
        raise ValueError("profile['availability'] must be a string")
    bio = profile.get("bio", "")
    if bio is not None and not isinstance(bio, str):
        raise ValueError("profile['bio'] must be a string")
    if len(bio) > 280:
        raise ValueError("profile['bio'] must be 280 characters or fewer")

    # Preferred contact path: "hidden" (default — revealed only after
    # mutual two-sided consent) or "linkedin_public" (the mentor explicitly
    # opts their LinkedIn URL into the public discovery card).
    preferred_contact = profile.get("preferred_contact", "hidden")
    if preferred_contact not in ("hidden", "linkedin_public"):
        raise ValueError(
            "profile['preferred_contact'] must be 'hidden' or "
            f"'linkedin_public', got {preferred_contact!r}"
        )

    # Structured availability windows: list of {"day", "start", "end"} or
    # plain strings like "Tue/Thu afternoons".
    availability_windows = profile.get("availability_windows") or []
    if not isinstance(availability_windows, list):
        raise ValueError("profile['availability_windows'] must be a list")
    for w in availability_windows:
        if isinstance(w, dict):
            if not w.get("day"):
                raise ValueError(
                    "availability_windows entries need at least a 'day'"
                )
        elif not isinstance(w, str) or not w.strip():
            raise ValueError(
                "availability_windows entries must be dicts or non-empty strings"
            )

    # Boundaries: what the mentor offers and what they will not do.
    boundaries = profile.get("boundaries") or {}
    if not isinstance(boundaries, dict):
        raise ValueError("profile['boundaries'] must be a dict")
    for key in ("offers", "wont"):
        vals = boundaries.get(key) or []
        if not isinstance(vals, list) or any(
            not isinstance(v, str) or not v.strip() for v in vals
        ):
            raise ValueError(
                f"profile['boundaries'][{key!r}] must be a list of non-empty strings"
            )
    note = boundaries.get("note", "")
    if note is not None and not isinstance(note, str):
        raise ValueError("profile['boundaries']['note'] must be a string")
    if len(note) > 500:
        raise ValueError("profile['boundaries']['note'] must be 500 characters or fewer")

    return {
        "id": _card_id(linkedin_url),
        "name": name,
        "industry": industry,
        "role": role,
        "seniority": seniority,
        "topics": list(dict.fromkeys(topics)),  # dedupe, keep order
        "linkedin_url": linkedin_url.strip(),
        "preferred_contact": preferred_contact,
        "availability": (availability or "").strip(),
        "availability_windows": availability_windows,
        "boundaries": {
            "offers": [str(v).strip() for v in (boundaries.get("offers") or [])],
            "wont": [str(v).strip() for v in (boundaries.get("wont") or [])],
            "note": note.strip(),
        },
        "max_mentees": max_mentees,
        "mentee_count": 0,
        "bio": bio.strip(),
        "own": True,
        "created_at": _now(),
    }


# ---------------------------------------------------------------------------
# Mentor side
# ---------------------------------------------------------------------------


def mentor_opt_in(profile: dict[str, Any], own: bool = True) -> dict[str, Any]:
    """Publish a mentor card to the local directory.

    The mentor controls their own privacy: ``name`` may be a full display
    name or just initials. Contact info is sealed by default
    (``preferred_contact="hidden"``): the LinkedIn URL is revealed only
    after mutual two-sided consent via the Initiative 07 handshake; a
    mentor may instead pass ``preferred_contact="linkedin_public"`` to opt
    the URL into the public discovery card. Optional profile fields:
    ``availability_windows`` (structured availability), ``boundaries``
    (``offers`` / ``wont`` lists plus a ``note`` — what the mentor will
    and will not discuss). Cards contributed by others (e.g. synced from
    the community directory) are added with ``own=False``. Raises
    ``ValueError`` on invalid profiles (bad LinkedIn URL, unknown topic,
    non-positive max_mentees, missing fields).
    """
    card = _validate_profile(profile)
    card["own"] = own
    directory = _load_directory()
    if own:
        # Retire any previous own card before adding the new one.
        for key in [k for k, v in directory.items() if v.get("own")]:
            directory.pop(key, None)
    directory[card["id"]] = card
    _save_directory(directory)
    return {"ok": True, "card": card}


def mentor_opt_out(mentor_id: str | None = None) -> dict[str, Any]:
    """Remove a mentor card. Defaults to your own card."""
    directory = _load_directory()
    if mentor_id is None:
        own = [k for k, v in directory.items() if v.get("own")]
        if not own:
            return {"ok": False, "error": "no own mentor card to remove"}
        mentor_id = own[0]
    if mentor_id not in directory:
        return {"ok": False, "error": f"unknown mentor id {mentor_id!r}"}
    card = directory.pop(mentor_id)
    _save_directory(directory)
    return {"ok": True, "removed": card}


def my_mentor_card(updates: dict[str, Any] | None = None) -> dict[str, Any]:
    """Show (and optionally edit) your own mentor card.

    ``updates`` may change name, industry, role, seniority, topics,
    availability, availability_windows, boundaries, preferred_contact,
    max_mentees, bio, or linkedin_url. Invalid updates raise
    ``ValueError``; contact-info rules still apply.
    """
    directory = _load_directory()
    own = [k for k, v in directory.items() if v.get("own")]
    if not own:
        return {"ok": False, "error": "you have no mentor card yet"}
    mentor_id = own[0]
    card = directory[mentor_id]
    if updates:
        merged = dict(card)
        merged.pop("id", None)
        merged.pop("own", None)
        merged.pop("created_at", None)
        merged["mentee_count"] = card.get("mentee_count", 0)
        for key, value in updates.items():
            if key in ("name", "industry", "role", "seniority", "topics",
                       "linkedin_url", "preferred_contact", "availability",
                       "availability_windows", "boundaries", "max_mentees",
                       "bio"):
                merged[key] = value
        # Re-validate everything through the same gate.
        rechecked = _validate_profile(merged)
        rechecked["mentee_count"] = card.get("mentee_count", 0)
        rechecked["ratings"] = card.get("ratings", {"mentee": [], "mentor": []})
        rechecked["created_at"] = card.get("created_at", _now())
        rechecked["updated_at"] = _now()
        # A LinkedIn URL change means a new card id.
        directory.pop(mentor_id, None)
        mentor_id = rechecked["id"]
        card = rechecked
        directory[mentor_id] = card
        _save_directory(directory)
    shown = dict(card)
    shown["rating_summary"] = _rating_summary(card)
    return {"ok": True, "card": shown}


def remaining_capacity(card: dict[str, Any]) -> int:
    """Spots left for this mentor (max_mentees minus recorded mentees)."""
    try:
        return max(0, int(card.get("max_mentees", 0)) - int(card.get("mentee_count", 0)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Mentee side: matchmaking
# ---------------------------------------------------------------------------


def _role_similarity(target_role: str, mentor_role: str) -> float:
    """0..1 token-overlap between a mentee's target role and a mentor's role."""
    target = {t for t in re.findall(r"[a-z]+", target_role.lower()) if len(t) > 2}
    mentor = {t for t in re.findall(r"[a-z]+", mentor_role.lower()) if len(t) > 2}
    if not target or not mentor:
        return 0.0
    return len(target & mentor) / len(target | mentor)


_VALID_URGENCY = ("this_week", "this_month", "exploring")


def matchmake(
    answers: dict[str, Any],
    consent_preview: bool = False,
    exclude_mentor_ids: tuple[str, ...] | list[str] = (),
    apply_safety_exclusions: bool = True,
) -> dict[str, Any]:
    """Rank mentors against a mentee's questionnaire.

    ``answers`` keys: industry (str), target_role (str),
    topics_ranked (list, most important first), goal (one sentence, str),
    context (str, optional background), urgency (one of "this_week",
    "this_month", "exploring"), expected_outcome (str — what a useful
    session would produce), identity_preferences (optional dict, accepted
    ONLY when voluntarily supplied; stored verbatim and NEVER used for
    ranking).

    Score out of 100 (deterministic):
      industry match 40, role proximity 25, topic overlap 25 (rank-weighted),
      availability/capacity 10.

    Only mentors with remaining capacity are considered. An empty directory
    returns an empty match list plus guidance — mentors are never invented.

    ``consent_preview=True`` redacts contact fields from every match
    (Initiative 07 revelation rule): no LinkedIn URL unless the mentor
    explicitly opted it into the public discovery card. Use this mode for
    the two-sided-consent introduction flow; the default full mode remains
    for the self-serve cold-contact path (the mentee sends their own
    LinkedIn note via ``connection_draft``).

    ``exclude_mentor_ids`` hides mentors from results entirely (blocked or
    quarantined actors — see ``initiatives.i07.safety.discovery_exclusions``).

    ``apply_safety_exclusions`` (default True) makes ``matchmake`` itself
    consult ``initiatives.i07.safety.discovery_exclusions()`` and hide
    blocked/quarantined mentors from every surface (web UI, dashboard,
    CLI, MCP) without each caller having to remember — this function is
    the single safety choke point. Pass ``False`` only to deliberately
    opt out of the safety consult (explicit ``exclude_mentor_ids`` are
    still honored).

    Contact sealing: matches never carry a ``linkedin_url`` for mentors
    whose ``preferred_contact`` is "hidden" (the default) — the key is
    absent until mutual consent reveals it. Mentors who opted
    ``preferred_contact="linkedin_public"`` keep their URL in results.
    """
    answers = answers or {}
    industry = str(answers.get("industry", "")).strip().lower()
    target_role = str(answers.get("target_role", "")).strip()
    topics_ranked = [t for t in (answers.get("topics_ranked") or []) if t in TOPICS]
    goal = str(answers.get("goal", "")).strip()
    context = str(answers.get("context", "")).strip()
    expected_outcome = str(
        answers.get("expected_outcome") or answers.get("useful_session_outcome") or ""
    ).strip()
    urgency = str(answers.get("urgency", "") or "exploring").strip().lower()
    if urgency not in _VALID_URGENCY:
        urgency = "exploring"
    # Voluntary only: echoed back for transparency, never ranked on.
    identity_preferences = answers.get("identity_preferences")
    if not isinstance(identity_preferences, dict):
        identity_preferences = {}

    directory = _load_directory()
    excluded = set(exclude_mentor_ids or ())
    if apply_safety_exclusions:
        # THE safety choke point: blocked/quarantined mentors are hidden
        # from every discovery surface (web UI, dashboard, CLI, MCP)
        # without each caller having to remember. A corrupt safety state
        # raises SafetyStateError here: discovery is refused, fail closed.
        excluded |= _safety_exclusions()
    with_capacity = [c for c in directory.values() if remaining_capacity(c) > 0]
    available = [c for c in with_capacity if c.get("id") not in excluded]
    if not available:
        if with_capacity and excluded:
            guidance = (
                "No mentors available after applying your safety exclusions "
                "(blocked or under-review mentors are hidden). Unblock or "
                "wait for review to clear before they can reappear."
            )
        else:
            guidance = (
                "No mentors with remaining capacity in the local directory yet. "
                "Opt in as a mentor with mentor_opt_in, or contribute a card to "
                "the community directory with publish_mentor_card(confirmed=True)."
            )
        return {
            "ok": True,
            "matches": [],
            "guidance": guidance,
            "mentee_goal": goal,
            "mentee_context": context,
            "mentee_urgency": urgency,
            "mentee_expected_outcome": expected_outcome,
            "identity_preferences": identity_preferences,
            "consent_preview": consent_preview,
        }

    ranked: list[dict[str, Any]] = []
    for card in available:
        reasons: list[str] = []
        score = 0.0

        # Industry match — 40.
        card_industry = str(card.get("industry", "")).lower()
        if industry and (industry in card_industry or card_industry in industry):
            score += 40
            reasons.append(f"same industry ({card.get('industry')})")

        # Role proximity — 25.
        role_sim = _role_similarity(target_role, str(card.get("role", "")))
        score += round(role_sim * 25, 1)
        if role_sim >= 0.5:
            reasons.append(f"close role match ({card.get('role')})")

        # Topic overlap, rank-weighted — 25.
        prep_topic: str | None = None
        if topics_ranked:
            card_topics = set(card.get("topics", []))
            n = len(topics_ranked)
            weights = [(n - i) for i in range(n)]
            total_w = sum(weights)
            shared = [
                (t, topics_ranked.index(t) + 1)
                for t in card_topics
                if t in topics_ranked
            ]
            earned = sum(weights[topics_ranked.index(t)] for t, _ in shared)
            score += round(25 * earned / total_w, 1)
            ordered_shared = sorted(shared, key=lambda x: x[1])
            for topic, rank in ordered_shared:
                reasons.append(f"covers '{topic}' (your priority #{rank})")
            if ordered_shared:
                prep_topic = ordered_shared[0][0]
        if prep_topic is None:
            card_topics_list = card.get("topics") or []
            prep_topic = card_topics_list[0] if card_topics_list else None

        # Availability — 10.
        cap = remaining_capacity(card)
        score += 10 if cap > 0 else 0
        if cap > 0:
            reasons.append(f"{cap} mentoring spot(s) open")
        availability = str(card.get("availability", "")).strip()
        if availability:
            reasons.append(f"availability: {availability}")

        # Contact sealing (Initiative 07 revelation rule): a hidden mentor's
        # LinkedIn URL never leaves the module until mutual consent reveals
        # it. Only an explicit preferred_contact="linkedin_public" opt-in
        # keeps the URL in discovery results.
        match: dict[str, Any] = {
            "mentor_id": card.get("id"),
            "name": card.get("name"),
            "industry": card.get("industry"),
            "role": card.get("role"),
            "seniority": card.get("seniority"),
            "topics": card.get("topics"),
            "preferred_contact": card.get("preferred_contact", "hidden"),
            "bio": card.get("bio"),
            "availability": card.get("availability"),
            "availability_windows": card.get("availability_windows", []),
            "boundaries": card.get("boundaries", {}),
            "remaining_capacity": cap,
            "score": min(100, round(score, 1)),
            "reasons": reasons,
            "prep": _prep_text(prep_topic) if prep_topic else _prep_text(""),
            "ratings": _rating_summary(card),
        }
        if card.get("preferred_contact", "hidden") == "linkedin_public":
            match["linkedin_url"] = card.get("linkedin_url")
        ranked.append(match)

    ranked.sort(key=lambda m: (-m["score"], m["name"]))
    if consent_preview:
        ranked = [_consent_preview_view(m) for m in ranked]
    guidance = (
        f"{len(ranked)} mentor(s) with open capacity. The mentee always "
        "initiates: use connection_draft to write a LinkedIn note and send "
        "it yourself — the tool never sends on your behalf."
    )
    if urgency == "this_week":
        guidance += (
            " You marked this urgent: prefer mentors whose availability "
            "mentions this week, and keep your first message short."
        )
    if identity_preferences:
        guidance += (
            " Your identity preferences were recorded as you supplied them; "
            "they are never used for ranking."
        )
    return {
        "ok": True,
        "matches": ranked,
        "guidance": guidance,
        "mentee_goal": goal,
        "mentee_context": context,
        "mentee_urgency": urgency,
        "mentee_expected_outcome": expected_outcome,
        "identity_preferences": identity_preferences,
        "consent_preview": consent_preview,
    }


def _consent_preview_view(match: dict[str, Any]) -> dict[str, Any]:
    """Redact a match dict for pre-consent display (Initiative 07).

    Discovery fields stay; contact fields go unless the mentor explicitly
    opted their LinkedIn URL into the public discovery card. Lazy import
    keeps mentors.py free of import cycles with initiatives.i07.
    """
    from initiatives.i07 import consent as _consent

    cardish = {
        "id": match.get("mentor_id"),
        "name": match.get("name"),
        "industry": match.get("industry"),
        "role": match.get("role"),
        "seniority": match.get("seniority"),
        "topics": match.get("topics"),
        "bio": match.get("bio"),
        "availability": match.get("availability"),
        "remaining_capacity": match.get("remaining_capacity"),
        "rating_summary": match.get("ratings"),
        "reasons": match.get("reasons"),
        "score": match.get("score"),
        "preferred_contact": match.get("preferred_contact"),
        "linkedin_url": match.get("linkedin_url"),
    }
    preview = _consent.redacted_preview(cardish)
    preview["mentor_id"] = preview.pop("id", match.get("mentor_id"))
    preview["ratings"] = preview.pop("rating_summary", match.get("ratings"))
    return preview


def record_outreach(mentor_id: str) -> dict[str, Any]:
    """Log that you (the mentee) reached out to a mentor.

    Increments the mentor's recorded mentee count so capacity stays honest.
    Reversible only by the directory owner editing mentors.json.
    Refuses loudly when ``mentor_id`` is blocked or quarantined.
    """
    rejected = _exclusion_rejection(mentor_id)
    if rejected is not None:
        return rejected
    directory = _load_directory()
    card = directory.get(mentor_id)
    if card is None:
        return {"ok": False, "error": f"unknown mentor id {mentor_id!r}"}
    card["mentee_count"] = int(card.get("mentee_count", 0)) + 1
    _save_directory(directory)
    return {
        "ok": True,
        "mentor_id": mentor_id,
        "remaining_capacity": remaining_capacity(card),
    }


# ---------------------------------------------------------------------------
# Connection drafts
# ---------------------------------------------------------------------------


def _connection_note(mentee_name: str, mentee_goal: str, card: dict[str, Any]) -> str:
    """Build a LinkedIn connection-request note, always < 300 chars."""
    first_name = str(card.get("name", "there")).split()[0]
    industry = card.get("industry", "your field")
    topic = (card.get("topics") or ["mentorship"])[0].replace("_", " ")

    goal = mentee_goal.strip()
    note = (
        f"Hi {first_name}, I admire your work in {industry}. I'm {mentee_name} "
        f"and I'm {goal}. I'd value your perspective on {topic} — happy to "
        f"work around your schedule. Thanks for considering!"
    )
    # Shrink the mentee's own goal first if the note overflows the limit.
    if len(note) > _MAX_NOTE_LEN:
        overflow = len(note) - _MAX_NOTE_LEN + 1  # +1 for the ellipsis
        goal = goal[:-overflow].rstrip() + "…" if len(goal) > overflow else goal
        note = (
            f"Hi {first_name}, I admire your work in {industry}. I'm {mentee_name} "
            f"and I'm {goal}. I'd value your perspective on {topic} — happy to "
            f"work around your schedule. Thanks for considering!"
        )
    if len(note) > _MAX_NOTE_LEN:  # belt and suspenders
        note = note[: _MAX_NOTE_LEN - 1] + "…"
    return note


def connection_draft(
    mentor_id: str, mentee_name: str, mentee_goal: str
) -> dict[str, Any]:
    """Draft a LinkedIn connection-request note to a mentor.

    Returned labeled as a DRAFT with the mentor's LinkedIn URL (when the
    mentor opted ``preferred_contact="linkedin_public"``) and explicit
    instructions: the mentee sends this themselves on LinkedIn. The tool
    never sends anything.

    Contact sealing: for a "hidden" mentor (the default) the LinkedIn URL
    is NOT returned — ``mentor_linkedin_url`` is absent and the
    instructions route you to the two-sided consent introduction flow,
    which reveals contact only after mutual consent. Refuses loudly when
    ``mentor_id`` is blocked or quarantined.
    """
    rejected = _exclusion_rejection(mentor_id)
    if rejected is not None:
        return rejected
    if not mentee_name or not mentee_name.strip():
        return {"ok": False, "error": "mentee_name is required"}
    if not mentee_goal or not mentee_goal.strip():
        return {"ok": False, "error": "mentee_goal is required"}
    directory = _load_directory()
    card = directory.get(mentor_id)
    if card is None:
        return {"ok": False, "error": f"unknown mentor id {mentor_id!r}"}
    if remaining_capacity(card) <= 0:
        return {
            "ok": False,
            "error": "this mentor has no remaining capacity right now",
        }
    note = _connection_note(mentee_name.strip(), mentee_goal.strip(), card)
    prep_checklist = [_prep_text(t) for t in (card.get("topics") or [])]
    result: dict[str, Any] = {
        "ok": True,
        "status": "DRAFT — not sent",
        "note": note,
        "note_length": len(note),
        "mentor_name": card.get("name"),
        "prep_checklist": prep_checklist,
        "prep_note": (
            "Completing the prep is recommended, not required — it makes the "
            "first session more useful, but no mentor will check."
        ),
    }
    if card.get("preferred_contact", "hidden") == "linkedin_public":
        result["mentor_linkedin_url"] = card.get("linkedin_url")
        result["instructions"] = (
            "YOU send this yourself on LinkedIn: open the mentor's profile URL, "
            "click Connect, add a note, and paste the text above. This tool "
            "never sends messages on your behalf."
        )
    else:
        # Sealed: the mentor's contact is private until mutual consent.
        result["contact_sealed"] = True
        result["instructions"] = (
            "This mentor keeps their contact details private until mutual "
            "consent — no profile URL is returned here. Request an "
            "introduction instead (the intro-request consent flow): once both "
            "sides approve, the LinkedIn URL is revealed via "
            "initiatives.i07.consent.reveal_contact. This tool never sends "
            "messages on your behalf."
        )
    return result


# ---------------------------------------------------------------------------
# Community directory publishing
# ---------------------------------------------------------------------------


def publish_mentor_card(confirmed: bool = False) -> dict[str, Any]:
    """Stage your mentor card for the community mentor directory.

    Writes ``contributions/outbox/mentor-<id>.json`` ONLY with explicit
    ``confirmed=True``. Without confirmation it returns a preview and the
    next steps instead of publishing anything.

    Contact sealing: when ``preferred_contact`` is "hidden" (the default),
    the published card does NOT carry ``linkedin_url`` — the URL is sealed
    until mutual consent reveals it, so the outbox file never declares a
    hidden contact while leaking it.
    """
    directory = _load_directory()
    own = [c for c in directory.values() if c.get("own")]
    if not own:
        return {"ok": False, "error": "you have no mentor card to publish"}
    card = dict(own[0])
    card.pop("own", None)  # the published card doesn't carry local flags

    sealed = card.get("preferred_contact", "hidden") != "linkedin_public"
    public_card = dict(card)
    if sealed:
        # A "hidden" card must not declare the preference while leaking the
        # URL in the same file: the URL stays out until mutual consent.
        public_card.pop("linkedin_url", None)

    if not confirmed:
        preview: dict[str, Any] = dict(public_card)
        if sealed:
            preview["contact_note"] = (
                "linkedin_url sealed (preferred_contact='hidden'): your contact "
                "details stay private until mutual consent. The published card "
                "carries no contact URL."
            )
        return {
            "ok": True,
            "published": False,
            "preview": preview,
            "next_steps": (
                "Review the preview above. If it looks right, re-run with "
                "confirmed=True and this exact card will be written to "
                "contributions/outbox/mentor-<id>.json for submission to the "
                "project's community mentor directory. Nothing is published "
                "automatically — you review it first."
            ),
        }

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTBOX_DIR / f"mentor-{card['id']}.json"
    path.write_text(
        json.dumps(public_card, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {
        "ok": True,
        "published": True,
        "path": str(path),
        "next_steps": (
            f"Card written to {path}. To complete the submission, commit it to "
            "the project's git repository (e.g. `git add` + `git commit`) and "
            "open a pull request so maintainers can review it before it joins "
            "the community directory."
        ),
    }


# ---------------------------------------------------------------------------
# Training-triggered mentor matching
# ---------------------------------------------------------------------------

# soft_skills / ai_proficiency may not be installed next to this module;
# everything below degrades gracefully when they are missing.
try:  # pragma: no cover - depends on the deployment environment
    import soft_skills  # type: ignore
except ImportError:  # pragma: no cover
    soft_skills = None  # type: ignore

try:  # pragma: no cover - depends on the deployment environment
    import ai_proficiency  # type: ignore
except ImportError:  # pragma: no cover
    ai_proficiency = None  # type: ignore

#: Soft-skill training areas mapped to the mentor topics they point at.
WEAK_AREA_TO_TOPICS: dict[str, list[str]] = {
    "negotiation": ["salary_negotiation"],
    "star_storytelling": ["interviewing"],
    "communication_clarity": ["interviewing", "executive_presence"],
    "leadership_influence": ["leadership"],
    "executive_presence": ["executive_presence"],
    "active_listening": ["leadership"],
}

#: Concrete practice to do before reaching out, per mentor topic.
#: A recommendation, never a hard block.
TOPIC_TO_PREP: dict[str, str] = {
    "interviewing": "soft-skills drill: star_storytelling (adversarial, firm)",
    "salary_negotiation": "soft-skills: negotiation_sim (firm) + a negotiation drill",
    "career_switch": "soft-skills drill: star_storytelling (coach)",
    "leadership": "soft-skills drill: leadership_influence (adversarial, firm)",
    "executive_presence": "soft-skills drill: executive_presence (coach)",
    "ai_skills": "ai-skills: run your track diagnostic, then complete lesson 1",
    "networking": "soft-skills drill: communication_clarity (coach)",
    "resume_craft": "draft your resume tailoring, then a communication_clarity drill",
}

_WEAK_AVERAGE_BELOW = 70  # average score below this counts as a weak area


def prep_for_topic(topic: str) -> dict[str, Any]:
    """Name the concrete practice to do before reaching out on a topic.

    Returns the prep for one of TOPICS. Prep is a recommendation, never a
    requirement — mentors welcome motivated mentees at any stage.
    """
    if topic not in TOPIC_TO_PREP:
        return {
            "ok": False,
            "error": f"unknown topic {topic!r}; choose from {list(TOPIC_TO_PREP)}",
        }
    return {
        "ok": True,
        "topic": topic,
        "prep": TOPIC_TO_PREP[topic],
        "note": (
            "Recommended prep, not a requirement — completing it makes the "
            "first session more useful, but no mentor will check."
        ),
    }


def _prep_text(topic: str) -> str:
    """One-line prep recommendation for a topic (always safe to show)."""
    if topic in TOPIC_TO_PREP:
        return f"Recommended prep before reaching out: {TOPIC_TO_PREP[topic]}."
    return "Recommended prep before reaching out: a soft-skills drill."


def _training_weak_areas() -> tuple[list[dict[str, Any]], list[str], bool]:
    """Find weak areas across the training modules.

    Returns (weak_areas, unavailable_modules, has_any_history). Weakness is
    derived only from real recorded scores — nothing is fabricated. An area
    counts as weak when its average is below 70 or its trend is declining.
    """
    weak: list[dict[str, Any]] = []
    unavailable: list[str] = []
    has_history = False

    if soft_skills is None:
        unavailable.append("soft_skills")
    else:
        try:
            prog = soft_skills.progress()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("soft_skills.progress() failed: %s", exc)
            unavailable.append("soft_skills")
        else:
            areas = prog.get("areas") or {}
            if prog.get("total_reps"):
                has_history = True
            for area, stats in areas.items():
                reps = stats.get("reps", 0) or 0
                avg = stats.get("average")
                trend = stats.get("trend")
                if not reps or avg is None:
                    continue
                if avg < _WEAK_AVERAGE_BELOW or trend == "declining":
                    label = area.replace("_", " ")
                    if trend == "declining":
                        evidence = (
                            f"{label} drills averaging {avg}/100 "
                            f"over {reps} reps, trending down"
                        )
                    else:
                        evidence = (
                            f"{label} drills averaging {avg}/100 "
                            f"over {reps} reps"
                        )
                    weak.append({
                        "source": "soft_skills",
                        "area": area,
                        "average": avg,
                        "reps": reps,
                        "trend": trend,
                        "evidence": evidence,
                        "topics": WEAK_AREA_TO_TOPICS.get(area, []),
                    })

    if ai_proficiency is None:
        unavailable.append("ai_proficiency")
    else:
        try:
            prog = ai_proficiency.progress()
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("ai_proficiency.progress() failed: %s", exc)
            unavailable.append("ai_proficiency")
        else:
            for tid, track in (prog.get("tracks") or {}).items():
                if track.get("diagnosed"):
                    has_history = True
                if track.get("level") == "foundations":
                    label = track.get("label") or tid
                    done = track.get("completed_count", 0)
                    total = track.get("total_lessons", 0)
                    weak.append({
                        "source": "ai_proficiency",
                        "area": label,
                        "track": tid,
                        "level": "foundations",
                        "evidence": (
                            f"{label} AI track at foundations level "
                            f"({done}/{total} lessons done)"
                        ),
                        "topics": ["ai_skills"],
                    })

    return weak, unavailable, has_history


def suggest_mentors_from_training(
    exclude_mentor_ids: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Suggest mentors based on your real training history.

    Reads ``soft_skills.progress()`` and ``ai_proficiency.progress()``,
    finds weak areas (average below 70 or declining trend), maps them to
    mentor topics via ``WEAK_AREA_TO_TOPICS``, and reuses ``matchmake`` for
    the top mentors per topic. With no training history it says so and
    suggests running a drill first — training data is never fabricated.
    ``exclude_mentor_ids`` is forwarded to ``matchmake`` (blocked or
    quarantined actors never surface; the safety choke point also applies
    internally).
    """
    weak, unavailable, has_history = _training_weak_areas()

    topics: list[str] = []
    for entry in weak:
        for topic in entry.get("topics", []):
            if topic not in topics:
                topics.append(topic)

    matches_by_topic: dict[str, list[dict[str, Any]]] = {}
    for topic in topics:
        labels = ", ".join(
            w["area"].replace("_", " ") for w in weak if topic in w.get("topics", [])
        )
        res = matchmake(
            {
                "industry": "",
                "target_role": "",
                "topics_ranked": [topic],
                "goal": f"Get better at {labels} (flagged by my practice history).",
            },
            exclude_mentor_ids=exclude_mentor_ids,
        )
        # The suggestion is topic-driven: prep should speak to the suggested
        # topic, not to whatever topic the mentor happens to list first.
        matches = res.get("matches", [])[:3]
        for match in matches:
            match["prep"] = _prep_text(topic)
        matches_by_topic[topic] = matches

    if not weak:
        if has_history:
            guidance = (
                "Your practice history looks strong everywhere — no weak "
                "areas to target. Use mentor_match with a full questionnaire "
                "to find mentors for your next goal instead."
            )
        else:
            guidance = (
                "No training history yet — run a soft-skills drill "
                "(e.g. `soft-skills drill negotiation`) or an ai-skills "
                "diagnostic first, then come back and I'll point you at "
                "mentors for your weakest areas."
            )
    else:
        guidance = (
            f"{len(weak)} weak area(s) found in your practice history. "
            "Matches below are the top mentors per suggested topic. Tip: "
            "use mentor_match with your industry and target role for "
            "sharper, personalized matches."
        )

    result: dict[str, Any] = {
        "ok": True,
        "weak_areas": weak,
        "suggested_topics": topics,
        "matches_by_topic": matches_by_topic,
        "guidance": guidance,
    }
    if unavailable:
        result["unavailable_modules"] = unavailable
        result["guidance"] += (
            f" (Note: {', '.join(unavailable)} not available in this "
            "environment, so only the other module's history was checked.)"
        )
    return result


# ---------------------------------------------------------------------------
# First-session agenda
# ---------------------------------------------------------------------------

_AGENDA_QUESTIONS: dict[str, list[str]] = {
    "interviewing": [
        "How do you structure a behavioral answer so it lands in under two minutes?",
        "What's the most common mistake you see candidates make in interviews?",
        "How would you prepare stories differently for a startup vs. a big company?",
    ],
    "salary_negotiation": [
        "How do you research your range before you anchor a number?",
        "What do you say when they tell you the budget is fixed?",
        "Beyond base salary, what has actually been negotiable in your experience?",
    ],
    "career_switch": [
        "What made hiring managers take your switch seriously?",
        "How did you reframe your past experience for the new role?",
        "What would you do differently if you switched again?",
    ],
    "leadership": [
        "How do you influence a decision when you have no authority over the room?",
        "How do you handle disagreement with a peer you depend on?",
        "What does 'good' look like in your first 90 days leading something new?",
    ],
    "executive_presence": [
        "What separates people who get heard in a room from people who don't?",
        "How do you stay concise when the topic is complex?",
        "What small habits made the biggest difference in how you're perceived?",
    ],
    "ai_skills": [
        "How are you actually using AI in your day-to-day work right now?",
        "What AI skill do you think is most underrated in our field?",
        "Where do you draw the line on what AI should vs. shouldn't do?",
    ],
    "networking": [
        "How do you start conversations that don't feel transactional?",
        "What does a good follow-up after meeting someone look like?",
        "How do you stay in touch with people without it feeling forced?",
    ],
    "resume_craft": [
        "What makes you stop and read a resume instead of skimming past it?",
        "How do you decide what to cut when everything feels important?",
        "What's the biggest resume red flag in our industry?",
    ],
}


def session_agenda(mentor_id: str, mentee_goal: str) -> dict[str, Any]:
    """Build a 30-minute first-session plan as markdown.

    Structure: 5 min intros + goal, 15 min deep-dive (3 questions drawn
    from the mentor's topics), 5 min mentor-story prompt, 5 min next steps.
    Labeled as a starting template the pair can adapt — not a script.
    Refuses loudly when ``mentor_id`` is blocked or quarantined.
    """
    rejected = _exclusion_rejection(mentor_id)
    if rejected is not None:
        return rejected
    if not mentee_goal or not mentee_goal.strip():
        return {"ok": False, "error": "mentee_goal is required"}
    directory = _load_directory()
    card = directory.get(mentor_id)
    if card is None:
        return {"ok": False, "error": f"unknown mentor id {mentor_id!r}"}

    goal = mentee_goal.strip()
    name = card.get("name", "your mentor")
    role = card.get("role", "")
    industry = card.get("industry", "")
    mentor_topics = [t for t in (card.get("topics") or []) if t in _AGENDA_QUESTIONS][:3]

    questions: list[str] = []
    for topic in mentor_topics:
        bank = _AGENDA_QUESTIONS[topic]
        questions.append(bank[0])
    while len(questions) < 3 and mentor_topics:
        # Fill remaining slots from the banks of the mentor's topics.
        for topic in mentor_topics:
            bank = _AGENDA_QUESTIONS[topic]
            idx = len([q for q in questions if q in bank])
            if idx < len(bank):
                questions.append(bank[idx])
            if len(questions) >= 3:
                break

    deep_dive = "\n".join(
        f"{i + 1}. {q}" for i, q in enumerate(questions[:3])
    )
    story_topic = (mentor_topics[0].replace("_", " ")
                   if mentor_topics else "your field")

    markdown = f"""# First mentorship session — 30 minutes
**Mentor:** {name} ({role}, {industry})
**Your goal:** {goal}

> Starting template — read it together at the top of the call and adapt it.
> The best sessions throw half of this away once the conversation gets going.

## 0:00–0:05 — Intros + goal
- 60-second intros each: who you are, what you're working on.
- Say your goal out loud: "{goal}".
- Agree on what a good outcome for these 30 minutes looks like.

## 0:05–0:20 — Deep dive
Questions tailored to {name}'s background — pick the ones that fit your goal:
{deep_dive}

## 0:20–0:25 — Mentor's story
- "Can you tell me about a time you faced a tough {story_topic} situation —
  what did you try, and what would you do differently now?"
- Listen for the decision, not just the outcome.

## 0:25–0:30 — Next steps
- One concrete commitment from you (e.g. "{_prep_text(mentor_topics[0] if mentor_topics else 'interviewing')}" or a draft to share).
- One ask of the mentor (an intro, a resource, feedback on something specific).
- Decide together: is there a second session, and when?
"""

    return {
        "ok": True,
        "mentor_id": mentor_id,
        "mentor_name": name,
        "markdown": markdown,
        "template_note": (
            "This is a starting template, not a script — adapt it with your "
            "mentor at the start of the call."
        ),
    }


# ---------------------------------------------------------------------------
# Two-sided ratings
# ---------------------------------------------------------------------------


def _rating_summary(card: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-side rating averages for a mentor card (mentee|mentor)."""
    ratings = card.get("ratings") or {}
    summary: dict[str, dict[str, Any]] = {}
    for side in ("mentee", "mentor"):
        entries = [e for e in (ratings.get(side) or []) if isinstance(e, dict)]
        stars = [e["stars"] for e in entries
                 if isinstance(e.get("stars"), int) and not isinstance(e.get("stars"), bool)]
        summary[side] = {
            "count": len(entries),
            "average": round(sum(stars) / len(stars), 1) if stars else None,
        }
    return summary


def rate_mentorship(
    mentor_id: str, stars: int, note: str = "", by: str = "mentee"
) -> dict[str, Any]:
    """Rate a mentorship from either side.

    ``by`` is "mentee" (default) or "mentor"; ratings are stored per side
    on the mentor card and per-side averages appear on cards and match
    results. ``stars`` must be an integer 1–5.
    """
    if by not in ("mentee", "mentor"):
        return {"ok": False, "error": f"by must be 'mentee' or 'mentor', got {by!r}"}
    if not isinstance(stars, int) or isinstance(stars, bool) or not 1 <= stars <= 5:
        return {"ok": False, "error": "stars must be an integer from 1 to 5"}
    directory = _load_directory()
    card = directory.get(mentor_id)
    if card is None:
        return {"ok": False, "error": f"unknown mentor id {mentor_id!r}"}
    ratings = card.setdefault("ratings", {"mentee": [], "mentor": []})
    ratings.setdefault(by, []).append({
        "stars": stars,
        "note": str(note or "").strip(),
        "by": by,
        "at": _now(),
    })
    _save_directory(directory)
    return {
        "ok": True,
        "mentor_id": mentor_id,
        "by": by,
        "stars": stars,
        "rating_summary": _rating_summary(card),
    }


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------


def _print_result(result: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def register_tools(mcp: Any) -> None:
    """Register mentor-matchmaking tools on an MCP server instance."""
    _impl_opt_in = globals()["mentor_opt_in"]
    _impl_opt_out = globals()["mentor_opt_out"]
    _impl_card = globals()["my_mentor_card"]
    _impl_match = globals()["matchmake"]
    _impl_draft = globals()["connection_draft"]
    _impl_record = globals()["record_outreach"]
    _impl_publish = globals()["publish_mentor_card"]
    _impl_suggest = globals()["suggest_mentors_from_training"]
    _impl_agenda = globals()["session_agenda"]
    _impl_rate = globals()["rate_mentorship"]
    _impl_prep = globals()["prep_for_topic"]

    @mcp.tool()
    def mentor_opt_in(profile: dict) -> dict:
        """Opt in as a mentor: publish a mentor card to the local directory.

        Args:
            profile: Dict with name (display name OR initials — your privacy
                choice), industry, role, seniority (ic1-3|ic4|ic5+|manager|
                director+|executive), topics (subset of TOPICS), linkedin_url
                (must be a LinkedIn profile URL), availability (e.g.
                "2 calls/month"), max_mentees, bio (<=280 chars).
        """
        return _impl_opt_in(profile)

    @mcp.tool()
    def mentor_opt_out() -> dict:
        """Remove your mentor card from the local directory."""
        return _impl_opt_out()

    @mcp.tool()
    def my_mentor_card() -> dict:
        """Show your own mentor card."""
        return _impl_card()

    @mcp.tool()
    def mentor_match(industry: str, target_role: str, topics: list,
                     goal: str, consent_preview: bool = True,
                     exclude_mentor_ids: list | None = None) -> dict:
        """Matchmake: rank available mentors for a mentee's questionnaire.

        Defaults to the SAFE mode (consent_preview=True): contact fields
        are sealed until both parties approve the introduction via the
        consent handshake. Pass consent_preview=False only for the
        self-serve cold-contact path. Blocked/quarantined mentors are
        always excluded (the matchmake safety choke point applies them
        internally); exclude_mentor_ids hides additional ids.

        Args:
            industry: The mentee's industry.
            target_role: The mentee's target role.
            topics: Topics ranked most-important first.
            goal: The mentee's goal in one sentence.
            consent_preview: Seal contact fields until mutual consent
                (default True — the safe mode).
            exclude_mentor_ids: Extra mentor ids to hide from results.
        """
        return _impl_match({
            "industry": industry,
            "target_role": target_role,
            "topics_ranked": topics,
            "goal": goal,
        },
            consent_preview=consent_preview,
            exclude_mentor_ids=exclude_mentor_ids or (),
        )

    @mcp.tool()
    def mentor_connection_draft(mentor_id: str, mentee_name: str,
                                mentee_goal: str) -> dict:
        """Draft a LinkedIn connection-request note to a mentor.

        Returns a DRAFT (<300 chars) plus the mentor's LinkedIn URL and
        instructions. The mentee sends it themselves; this tool never sends.
        """
        return _impl_draft(mentor_id, mentee_name, mentee_goal)

    @mcp.tool()
    def mentor_record_outreach(mentor_id: str) -> dict:
        """Log that you reached out to a mentor (keeps capacity honest)."""
        return _impl_record(mentor_id)

    @mcp.tool()
    def mentor_publish(confirm: bool = False) -> dict:
        """Stage your mentor card for the community directory outbox.

        Publishes ONLY with confirm=True; otherwise returns a preview.
        """
        return _impl_publish(confirmed=confirm)

    @mcp.tool()
    def suggest_mentors_from_training(
        exclude_mentor_ids: list | None = None,
    ) -> dict:
        """Suggest mentors for your weakest training areas.

        Reads your soft-skills and AI-proficiency practice history and
        matches you with mentors for the areas where your scores are
        weakest. No history yet? It will say so and suggest a drill first.
        Blocked/quarantined mentors never surface.

        Args:
            exclude_mentor_ids: Extra mentor ids to hide from suggestions.
        """
        return _impl_suggest(exclude_mentor_ids=exclude_mentor_ids or ())

    @mcp.tool()
    def session_agenda(mentor_id: str, mentee_goal: str) -> dict:
        """Build a 30-minute first-session plan (markdown template).

        Args:
            mentor_id: The mentor's card id.
            mentee_goal: Your goal in one sentence.
        """
        return _impl_agenda(mentor_id, mentee_goal)

    @mcp.tool()
    def rate_mentorship(mentor_id: str, stars: int, note: str = "",
                        by: str = "mentee") -> dict:
        """Rate a mentorship from either side ("mentee" or "mentor").

        Args:
            mentor_id: The mentor's card id.
            stars: Integer 1-5.
            note: Optional note.
            by: "mentee" (default) or "mentor" — whose perspective this is.
        """
        return _impl_rate(mentor_id, stars, note=note, by=by)

    @mcp.tool()
    def prep_for_topic(topic: str) -> dict:
        """Name the concrete drill or lesson to do before reaching out.

        Args:
            topic: One of the mentor TOPICS.
        """
        return _impl_prep(topic)


def cmd_mentors(args: Any) -> int:
    """CLI handler for `mentors`."""
    action = args.action
    as_json = getattr(args, "json", False)
    if action == "opt-in":
        profile = json.loads(args.profile) if getattr(args, "profile", None) else {}
        _print_result(mentor_opt_in(profile), as_json)
    elif action == "opt-out":
        _print_result(mentor_opt_out(getattr(args, "mentor_id", None)), as_json)
    elif action == "card":
        updates = json.loads(args.updates) if getattr(args, "updates", None) else None
        _print_result(my_mentor_card(updates), as_json)
    elif action == "match":
        topics = [t.strip() for t in (args.topics or "").split(",") if t.strip()]
        excluded = [
            e.strip()
            for e in (args.exclude_mentor_ids or "").split(",")
            if e.strip()
        ]
        _print_result(
            matchmake(
                {
                    "industry": args.industry or "",
                    "target_role": args.target_role or "",
                    "topics_ranked": topics,
                    "goal": args.goal or "",
                },
                # SAFE mode by default: seal contact until mutual consent.
                consent_preview=args.consent_preview,
                exclude_mentor_ids=excluded,
            ),
            as_json,
        )
    elif action == "draft":
        _print_result(
            connection_draft(args.mentor_id, args.mentee_name, args.mentee_goal),
            as_json,
        )
    elif action == "record-outreach":
        _print_result(record_outreach(args.mentor_id), as_json)
    elif action == "publish":
        _print_result(publish_mentor_card(confirmed=args.confirm), as_json)
    elif action == "suggest-from-training":
        _print_result(suggest_mentors_from_training(), as_json)
    elif action == "agenda":
        _print_result(
            session_agenda(args.mentor_id, args.mentee_goal or ""),
            as_json,
        )
    elif action == "rate":
        _print_result(
            rate_mentorship(
                args.mentor_id,
                args.stars,
                note=args.note or "",
                by=args.by or "mentee",
            ),
            as_json,
        )
    elif action == "prep":
        _print_result(prep_for_topic(args.topic or ""), as_json)
    else:
        raise SystemExit(f"unknown mentors action: {action}")
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `mentors` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser("mentors", help="Mentor matchmaking.")
    p.add_argument(
        "action",
        choices=[
            "opt-in", "opt-out", "card", "match", "draft",
            "record-outreach", "publish",
            "suggest-from-training", "agenda", "rate", "prep",
        ],
        help=("opt-in|opt-out|card|match|draft|record-outreach|publish|"
              "suggest-from-training|agenda|rate|prep"),
    )
    p.add_argument("--profile", help="JSON profile for opt-in.")
    p.add_argument("--updates", help="JSON updates for the card action.")
    p.add_argument("--mentor-id", help="Mentor id for draft/record-outreach.")
    p.add_argument("--industry", help="Your industry (match).")
    p.add_argument("--target-role", help="Your target role (match).")
    p.add_argument("--topics", help="Comma-separated topics, most important first.")
    p.add_argument("--goal", help="Your goal in one sentence (match).")
    p.add_argument(
        "--consent-preview",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Seal mentor contact fields until mutual consent (default: on — "
            "the safe mode). Use --no-consent-preview for the self-serve "
            "full-contact path."
        ),
    )
    p.add_argument(
        "--exclude-mentor-ids",
        help="Comma-separated mentor ids to hide from match results (match).",
    )
    p.add_argument("--mentee-name", help="Your name (draft).")
    p.add_argument("--mentee-goal", help="Your goal in one sentence (draft, agenda).")
    p.add_argument("--stars", type=int, help="Rating 1-5 (rate).")
    p.add_argument("--note", help="Optional rating note (rate).")
    p.add_argument(
        "--by", choices=["mentee", "mentor"], default="mentee",
        help="Whose perspective the rating is (rate).",
    )
    p.add_argument("--topic", help="A mentor topic (prep).")
    p.add_argument(
        "--confirm", action="store_true",
        help="Required to actually publish your card to the outbox.",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return {"mentors": cmd_mentors}
