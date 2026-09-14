#!/usr/bin/env python3
"""Session kit for mentor introductions (Initiative 07, epic 4).

* **Agenda** — a first-session plan (extends ``mentors.session_agenda``).
* **Context packet** — the mentor profile + the mentee's questionnaire
  answers + rating *summaries*, assembled ONLY after mutual two-sided
  consent. Assembling it earlier is refused. The packet NEVER carries
  the mentor card's raw ``ratings`` dict (individual notes, raters, and
  timestamps stay private); only the aggregated ``rating_summary`` ships.
* **Question builder** — concrete questions per topic and goal.
* **Notes** — session notes, per handshake. Notes are append-only within
  the session lifecycle: there is no edit API, and the only way a note
  disappears is ``scrub_mentor_notes``, the consent-gated erasure path
  (audited, used for deletion requests).
* **Follow-up** — a next-steps plan after the session.
* **Feedback** — lightweight usefulness rating that feeds the mentor's
  two-sided rating history (consent-gated: only for handshakes that
  reached ``mutual``).

Corruption contract (fail loud, never silent): ``_load`` raises
``SessionStoreCorruptError`` if sessions.json exists but does not parse
as a JSON object. Every read/write path catches it and returns
``{"ok": False, "error": ...}`` instead of proceeding — a corrupt store
can NEVER be silently replaced by a write (that used to destroy every
session on disk). ``_save`` is atomic (write to tmp + fsync + rename),
so a crash mid-write leaves either the old or the new store, never a
truncated file.

Recovery procedure (operator): (1) stop anything writing sessions.json;
(2) restore the file from the most recent good backup over it;
(3) verify it parses: ``python -c "import json;
json.load(open('initiatives/i07/sessions.json'))"``. This module keeps no
automatic backups itself — keep filesystem backups of the i07 data dir.

Notes and packets never leave the device; there is no send path anywhere
in this module. Synthetic fixtures are labeled ``SYNTHETIC-``. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.i07.session_kit")

BASE_DIR = Path(__file__).resolve().parent

#: Session artifacts (notes, packets, feedback) per handshake. Reassignable.
SESSIONS_FILE = BASE_DIR / "sessions.json"

#: Question packs per topic: concrete, open-ended, session-ready.
QUESTION_PACKS: dict[str, list[str]] = {
    "interviewing": [
        "What is the single most common reason strong candidates fail your interview loop?",
        "How should I structure answers for behavioral rounds at your level?",
        "What does 'hiring bar' concretely mean on your team?",
        "Which of my stories is weakest, and how would you rebuild it?",
    ],
    "salary_negotiation": [
        "What information do you wish candidates had before the compensation call?",
        "How do you frame a counter without anchoring too low or alienating the recruiter?",
        "What non-salary levers have you seen work at your company?",
    ],
    "career_switch": [
        "What convinced you (or your team) to bet on a career switcher before?",
        "Which of my transferable skills would you lead with, and which would you downplay?",
        "What is the credible 90-day ramp story for someone from my background?",
    ],
    "leadership": [
        "What distinguishes a senior IC's influence from a manager's at your org?",
        "Tell me about a time you had to deliver hard feedback upward.",
        "How do you build trust with a new team in the first month?",
    ],
    "executive_presence": [
        "What reads as 'executive presence' in your culture, concretely?",
        "How do you prepare differently for an exec-level conversation?",
        "What is one AI failure mode I should be able to discuss fluently?",
    ],
    "ai_skills": [
        "Which AI workflows actually save your team time versus just demoing well?",
        "How do you evaluate whether someone uses AI well in an interview?",
        "What is one AI failure mode I should be able to discuss fluently?",
    ],
    "networking": [
        "How do you ask for introductions without burning social capital?",
        "What follow-up cadence has worked for you with senior contacts?",
        "How do you turn a cold conversation warm in one message?",
    ],
    "resume_craft": [
        "What makes you stop reading a resume in the first ten seconds?",
        "How would you reframe my most recent role for the roles I want?",
        "Which metrics on my resume are noise, and which are signal?",
    ],
}


class SessionStoreCorruptError(Exception):
    """sessions.json exists but does not parse as a JSON object."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _corrupt_error(exc: Exception) -> str:
    return (
        "session store is corrupt and reads/writes are refused to prevent "
        f"data loss ({exc}). Recovery: stop writers, restore sessions.json "
        "from your most recent good backup over it, and verify it parses."
    )


def _load() -> dict[str, dict[str, Any]]:
    """Load the session store. Fails LOUDLY on corruption — never silent {}."""
    try:
        text = SESSIONS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error(
            "sessions.json is corrupt (%s); refusing to proceed", exc, exc_info=True
        )
        raise SessionStoreCorruptError(
            f"sessions.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        log.error(
            "sessions.json parsed to %s, not an object; refusing to proceed",
            type(data).__name__,
        )
        raise SessionStoreCorruptError(
            f"sessions.json parsed to {type(data).__name__}, expected an object"
        )
    return data


def _save(store: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the session store (tmp + fsync + rename)."""
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSIONS_FILE.with_suffix(".json.tmp")
    payload = json.dumps(store, indent=2, ensure_ascii=False, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, SESSIONS_FILE)
    # fsync the directory so the rename itself survives a crash.
    dir_fd = os.open(SESSIONS_FILE.parent, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _load_read() -> dict[str, Any]:
    """Load for a read path: convert store corruption into an error dict."""
    try:
        return {"ok": True, "store": _load()}
    except SessionStoreCorruptError as exc:
        log.error("refusing session read: corrupt store", exc_info=True)
        return {"ok": False, "error": _corrupt_error(exc)}


def _load_write() -> dict[str, Any]:
    """Load for a write path: convert store corruption into an error dict."""
    try:
        return {"ok": True, "store": _load()}
    except SessionStoreCorruptError as exc:
        log.error("refusing session write: corrupt store", exc_info=True)
        return {"ok": False, "error": _corrupt_error(exc)}


def _get_session(
    store: dict[str, dict[str, Any]],
    handshake_id: str,
    *,
    mentor_id: str = "",
    mentee_id: str = "",
) -> dict[str, Any]:
    return store.setdefault(
        handshake_id,
        {
            "handshake_id": handshake_id,
            "mentor_id": mentor_id,
            "mentee_id": mentee_id,
            "notes": [],
            "feedback": [],
            "followups": [],
            "created_at": _now(),
        },
    )


def _mutual_handshake(handshake_id: str, actor: str) -> dict[str, Any]:
    """Return the handshake iff it is mutual and actor is a party.

    ALSO refuses when a block exists between the two parties or either
    party is quarantined (safety check): a blocked/quarantined party
    cannot read the context packet, notes, agenda, follow-up plan, or
    submit feedback, even with a historically mutual handshake.
    """
    from . import consent, safety

    got = consent.get_handshake(handshake_id, actor)
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error")}
    hs = got["handshake"]
    if hs.get("state") != "mutual":
        return {
            "ok": False,
            "error": (
                "the session kit unlocks only after mutual consent; this "
                f"handshake is {hs.get('state')!r}"
            ),
        }
    block = safety.consent_block_check(hs.get("mentee_id"), hs.get("mentor_id"))
    if block.get("blocked"):
        log.warning(
            "session kit access refused for handshake %s: %s",
            handshake_id,
            block.get("error"),
        )
        return {"ok": False, "error": block.get("error")}
    return {"ok": True, "handshake": hs}


# ---------------------------------------------------------------------------
# Agenda
# ---------------------------------------------------------------------------


def build_agenda(handshake_id: str, actor: str) -> dict[str, Any]:
    """Build a first-session agenda. Requires mutual consent."""
    import mentors as _mentors

    chk = _mutual_handshake(handshake_id, actor)
    if not chk.get("ok"):
        return chk
    hs = chk["handshake"]
    base = _mentors.session_agenda(hs["mentor_id"], hs.get("goal", ""))
    if not base.get("ok"):
        return base
    agenda = base.get("agenda", "")
    expected = hs.get("expected_outcome", "").strip()
    if expected:
        agenda += f"\n\n## What a useful session produces\n\n{expected}\n"
    urgency = hs.get("urgency", "exploring")
    if urgency == "this_week":
        agenda += (
            "\n> Urgency: this week. Open with the time constraint and agree "
            "on one concrete next step before the call ends.\n"
        )
    return {"ok": True, "agenda": agenda, "handshake_id": handshake_id}


# ---------------------------------------------------------------------------
# Context packet (consent-gated)
# ---------------------------------------------------------------------------


def context_packet(handshake_id: str, actor: str) -> dict[str, Any]:
    """Assemble the context packet for a session.

    Includes the mentor's public profile (contact included — both parties
    consented), the mentee's questionnaire answers, and aggregated rating
    summaries. REFUSED unless the handshake is mutual.

    Privacy: the raw ``ratings`` dict is STRIPPED from the copied mentor
    card — individual rating notes, raters (``by``), and timestamps
    (``at``) never leave the device. Only the per-side ``rating_summary``
    (counts + averages) ships in the packet.
    """
    import mentors as _mentors

    chk = _mutual_handshake(handshake_id, actor)
    if not chk.get("ok"):
        return chk
    hs = chk["handshake"]
    card = dict(_mentors._load_directory().get(hs["mentor_id"], {}))  # noqa: SLF001
    card["rating_summary"] = _mentors._rating_summary(card)  # noqa: SLF001
    card.pop("ratings", None)  # privacy: never expose per-rating notes/by/at
    packet = {
        "handshake_id": handshake_id,
        "generated_at": _now(),
        "mentor": card,
        "mentee": {
            "label": hs.get("mentee_label"),
            "goal": hs.get("goal"),
            "context": hs.get("context"),
            "urgency": hs.get("urgency"),
            "expected_outcome": hs.get("expected_outcome"),
        },
        "consent_receipt": {
            "mentee_consented_at": hs.get("mentee_consented_at"),
            "mentor_consented_at": hs.get("mentor_consented_at"),
            "mentor_decision_channel": hs.get("mentor_decision_channel"),
        },
    }
    return {"ok": True, "packet": packet}


# ---------------------------------------------------------------------------
# Question builder
# ---------------------------------------------------------------------------


def question_builder(
    topics: list[str] | None, goal: str = "", n: int = 4
) -> dict[str, Any]:
    """Suggest concrete session questions from topic packs.

    Pure content helper — no consent needed (no personal data involved).
    """
    topics = [t for t in (topics or []) if t in QUESTION_PACKS]
    if not topics:
        return {
            "ok": False,
            "error": f"choose topics from {sorted(QUESTION_PACKS)}",
        }
    questions: list[dict[str, str]] = []
    per = max(1, n // max(1, len(topics)))
    for topic in topics:
        for q in QUESTION_PACKS[topic][:per]:
            questions.append({"topic": topic, "question": q})
    if goal.strip():
        questions.insert(
            0,
            {
                "topic": "goal",
                "question": (
                    f"My goal is: {goal.strip()} — where should I start, "
                    "and what am I likely underestimating?"
                ),
            },
        )
    return {"ok": True, "questions": questions[: max(n, 1)]}


# ---------------------------------------------------------------------------
# Notes (append-only within the session lifecycle; see scrub_mentor_notes)
# ---------------------------------------------------------------------------


def add_note(
    handshake_id: str, actor: str, text: str, *, kind: str = "note"
) -> dict[str, Any]:
    """Append a session note. Requires mutual consent.

    Notes are append-only within the session lifecycle (no edit API);
    the only removal path is ``scrub_mentor_notes`` (audited erasure).
    """
    if kind not in ("note", "takeaway", "action"):
        return {"ok": False, "error": "kind must be 'note', 'takeaway', or 'action'"}
    if not str(text or "").strip():
        return {"ok": False, "error": "note text is required"}
    chk = _mutual_handshake(handshake_id, actor)
    if not chk.get("ok"):
        return chk
    hs = chk["handshake"]
    loaded = _load_write()
    if not loaded.get("ok"):
        return loaded
    store = loaded["store"]
    session = _get_session(store, handshake_id,
                      mentor_id=hs.get("mentor_id", ""),
                      mentee_id=hs.get("mentee_id", ""))
    session["notes"].append(
        {"at": _now(), "by": actor, "kind": kind, "text": str(text).strip()}
    )
    _save(store)
    return {"ok": True, "note_count": len(session["notes"])}


def get_notes(handshake_id: str, actor: str) -> dict[str, Any]:
    chk = _mutual_handshake(handshake_id, actor)
    if not chk.get("ok"):
        return chk
    loaded = _load_read()
    if not loaded.get("ok"):
        return loaded
    session = loaded["store"].get(handshake_id, {})
    return {"ok": True, "notes": list(session.get("notes", []))}


def scrub_mentor_notes(mentor_id: str, *, actor: str | None = None) -> int:
    """Erasure path: remove session records for this mentor's handshakes.

    ``actor`` (keyword-only) is REQUIRED for consent gating: it must be
    the mentor themselves or the mentee party of one of the mentor's
    handshakes — anything else raises ``PermissionError`` (fail closed).
    ``actor=None`` is the internal system path used by
    ``safety.delete_mentor_data`` after it validates ``requested_by``;
    it is treated as the data subject (the mentor) exercising erasure.

    One audit entry is written per deleted session record
    (``session_notes_scrubbed``) via the consent audit trail. The whole
    session record (notes, feedback, follow-ups) for each affected
    handshake is deleted — the other party's notes included, because
    erasure removes the mentor's sessions entirely.

    Rating-store behavior (explicit contract): the mentor card's raw
    ``ratings`` dict in mentors.json is a separate governed store and is
    NOT touched here — neither deleted nor altered. The session record's
    own feedback copies inside sessions.json ARE deleted with the record.
    Full erasure (card + ratings + sessions) goes through
    ``safety.delete_mentor_data``, which removes the entire mentor card
    (ratings included). Contract and code agree on this split.
    """
    from . import consent

    mentor_id = str(mentor_id or "").strip()
    if not mentor_id:
        raise ValueError("mentor_id is required")
    if actor is None:
        # Internal system deletion path (safety.delete_mentor_data already
        # validated requested_by): act as the data subject.
        actor = mentor_id
    actor = str(actor).strip()
    loaded = _load_write()
    if not loaded.get("ok"):
        raise SessionStoreCorruptError(loaded["error"])
    store = loaded["store"]

    consent_records = {
        hs.get("id"): hs
        for hs in consent.list_handshakes()
        if hs.get("id") in store
    }
    target_ids: set[str] = set()
    for hs_id, hs in consent_records.items():
        if hs.get("mentor_id") == mentor_id:
            target_ids.add(hs_id)
    # Fallback for handshakes whose consent record is already gone or
    # tombstoned (e.g. safety.delete_mentor_data tombstones first): the
    # session record itself carries the mentor_id it was created with.
    for hs_id, rec in store.items():
        if isinstance(rec, dict) and rec.get("mentor_id") == mentor_id:
            target_ids.add(hs_id)

    for hs_id in sorted(target_ids):
        hs = consent_records.get(hs_id)
        if hs is not None:
            parties = {hs.get("mentee_id"), hs.get("mentor_id")}
        else:
            # Handshake record is gone/tombstoned: fail closed — only the
            # mentor (data subject, and the internal deletion path) may
            # erase it now, since no party membership can be verified.
            parties = {mentor_id}
        if actor not in parties:
            log.warning(
                "scrub_mentor_notes refused: actor %r is not a party of handshake %s",
                actor,
                hs_id,
            )
            raise PermissionError(
                f"actor {actor!r} is not a party to handshake {hs_id!r}; "
                "scrub refused (fail closed)"
            )

    count = 0
    for hs_id in sorted(target_ids):
        count += len(store[hs_id].get("notes", []))
        consent._audit(  # noqa: SLF001
            "session_notes_scrubbed",
            hs_id,
            actor,
            f"mentor_id={mentor_id} session record erased "
            f"(notes={len(store[hs_id].get('notes', []))}, "
            f"feedback={len(store[hs_id].get('feedback', []))}, "
            f"followups={len(store[hs_id].get('followups', []))})",
        )
        del store[hs_id]
    if target_ids:
        _save(store)
    return count


# ---------------------------------------------------------------------------
# Follow-up
# ---------------------------------------------------------------------------


def followup_plan(handshake_id: str, actor: str) -> dict[str, Any]:
    """Draft a follow-up plan from takeaways and actions. Mutual consent required."""
    chk = _mutual_handshake(handshake_id, actor)
    if not chk.get("ok"):
        return chk
    hs = chk["handshake"]
    loaded = _load_read()
    if not loaded.get("ok"):
        return loaded
    session = loaded["store"].get(handshake_id, {})
    actions = [n for n in session.get("notes", []) if n.get("kind") == "action"]
    takeaways = [n for n in session.get("notes", []) if n.get("kind") == "takeaway"]
    lines = ["# Follow-up plan", ""]
    lines.append("## Thank the mentor")
    lines.append(
        "- Send one short thank-you within 24h naming the single most useful point."
    )
    if takeaways:
        lines.append("")
        lines.append("## Your takeaways")
        lines.extend(f"- {t['text']}" for t in takeaways)
    if actions:
        lines.append("")
        lines.append("## Committed actions")
        lines.extend(f"- [ ] {a['text']}" for a in actions)
    else:
        lines.append("")
        lines.append("## Committed actions")
        lines.append("- [ ] Write down the one action this session produced.")
    lines += [
        "",
        "## Keep the door open",
        "- Share the outcome once you act on the advice — mentors invest "
        "where they see momentum.",
    ]
    loaded2 = _load_write()
    if not loaded2.get("ok"):
        return loaded2
    store = loaded2["store"]
    sess = _get_session(store, handshake_id,
                      mentor_id=hs.get("mentor_id", ""),
                      mentee_id=hs.get("mentee_id", ""))
    plan = "\n".join(lines)
    sess["followups"].append({"at": _now(), "by": actor, "plan": plan})
    _save(store)
    return {"ok": True, "plan": plan}


# ---------------------------------------------------------------------------
# Lightweight feedback (consent-gated)
# ---------------------------------------------------------------------------


def session_feedback(
    handshake_id: str,
    actor: str,
    useful_1_5: int,
    *,
    note: str = "",
) -> dict[str, Any]:
    """Rate session usefulness 1–5. Only for mutual handshakes.

    Feeds the mentor card's two-sided rating history via
    ``mentors.rate_mentorship`` (``by="mentee"`` or ``by="mentor"``),
    keeping one consistent rating store. Fail closed: ``actor`` must be
    one of the handshake parties — non-party actors (including the
    "owner" superuser) are rejected loudly instead of being mislabeled
    as mentor-side ratings.
    """
    import mentors as _mentors

    if (
        not isinstance(useful_1_5, int)
        or isinstance(useful_1_5, bool)
        or not 1 <= useful_1_5 <= 5
    ):
        return {"ok": False, "error": "useful_1_5 must be an integer from 1 to 5"}
    chk = _mutual_handshake(handshake_id, actor)
    if not chk.get("ok"):
        return chk
    hs = chk["handshake"]
    if actor not in (hs.get("mentee_id"), hs.get("mentor_id")):
        log.warning(
            "session_feedback rejected: actor %r is not a party of handshake %s",
            actor,
            handshake_id,
        )
        return {
            "ok": False,
            "error": (
                f"actor {actor!r} is not a party to this handshake; "
                "feedback rejected (fail closed)"
            ),
        }
    side = "mentee" if actor == hs.get("mentee_id") else "mentor"

    # Persist the session record FIRST: a rating must never exist in
    # mentors.json without a matching session record here (no orphans).
    loaded = _load_write()
    if not loaded.get("ok"):
        return loaded
    store = loaded["store"]
    session = _get_session(store, handshake_id,
                      mentor_id=hs.get("mentor_id", ""),
                      mentee_id=hs.get("mentee_id", ""))
    _save(store)

    rated = _mentors.rate_mentorship(
        hs["mentor_id"], useful_1_5, note=note, by=side
    )
    if not rated.get("ok"):
        return rated
    session["feedback"].append(
        {
            "at": _now(),
            "by": actor,
            "side": side,
            "useful_1_5": useful_1_5,
            "note": str(note or "").strip(),
        }
    )
    _save(store)
    return {
        "ok": True,
        "side": side,
        "useful_1_5": useful_1_5,
        "rating_summary": rated.get("rating_summary"),
    }
