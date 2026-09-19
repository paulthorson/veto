#!/usr/bin/env python3
"""Mock interview practice for the Veto MCP server.

A five-question practice loop for an upcoming interview:

``start_mock_interview(company, role, profile, channel="chat")``
    Build a session: 5 questions mixing behavioral (STAR), role-specific
    technical, and company-specific prompts. When a ``job_id`` is given
    the questions are reused from ``briefs.prep_interview``'s
    ``likely_questions`` (already tailored to the posting); otherwise a
    deterministic role-based question bank is used. For
    ``channel="chat"`` the session comes back with the first question;
    for ``channel="whatsapp"`` it returns a ready-to-send outbound
    message -- the caller (running in the WhatsApp side chat) delivers it.

``answer_mock_question(session_id, answer)``
    Score the next unanswered question with deterministic heuristics:
    STAR structure coverage, specificity (numbers present?), length
    sanity, and "I vs we" ownership. No LLM, no invented facts -- every
    feedback line refers only to the answer text itself.

``mock_summary(session_id)``
    Per-question scores, overall score, and the top 2 improvement tips.

Channel reality (same as grill.py): WhatsApp is only sendable from the
WhatsApp side chat, so this module never claims to have sent anything --
it hands the caller a ``deliverable_in`` routing hint instead.

Session state lives in ``mock_sessions.json`` next to this module,
keyed by session_id. NOTE: that file can contain practice answers; add
``mock_sessions.json`` to ``.gitignore``. This module deliberately does
not edit .gitignore itself.

Honesty rules (fail closed on facts):
  - Feedback never invents facts about the candidate. It comments on
    answer structure/wording and asks the candidate to supply their own
    real numbers and stories -- it never supplies them.
  - STAR story hints shown with questions come only from
    ``briefs._star_stories(profile)`` (profile data); with no experience
    on file the hints are empty, never fabricated.

Initiative 06 (interview lab):
  - ``mode`` selects a disclosed interviewer persona: "recruiter",
    "hiring_manager", "peer", "executive", or "adversarial" (see
    ``initiatives.i06.modes``). Each mode publishes its persona, round
    shape, and rubric weights before the round starts. ``mode=None``
    keeps the classic question bank for backward compatibility.
  - Every answer is also scored against the disclosed rubric
    (``initiatives.i06.rubric``: structure, evidence, clarity,
    trade-offs, question quality). The legacy 0-100 craft score is
    unchanged; rubric scores are added, never substituted.
  - Completed sessions are recorded to the longitudinal practice
    history (``initiatives.i06.longitudinal``) so repeated gaps can be
    observed across sessions. Recording is best-effort and never
    breaks answering.
  - ``input_modality`` ("text" default, "voice" via the optional voice
    mode) is recorded per answer for the privacy ledger.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import briefs

# Initiative 06 lab engines (same repo; hard import is fine — the
# initiatives package ships with the project).
from initiatives.i06 import longitudinal as _i06_longitudinal
from initiatives.i06 import modes as _i06_modes
from initiatives.i06 import rubric as _i06_rubric

log = logging.getLogger("veto-mcp.mock_interview")

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "mock_sessions.json"

# Connect link for the WhatsApp side chat (user must link once).
WHATSAPP_CONNECT_URL = "https://agent.meta.ai/connect/channel?service=whatsapp"

NUM_QUESTIONS = 5


# ---------------------------------------------------------------------------
# Question bank (deterministic; used when no job_id prep output exists)
# ---------------------------------------------------------------------------

#: (role keyword group, [two technical questions])
_TECHNICAL_BANK: list[tuple[tuple[str, ...], list[str]]] = [
    (("backend", "server", "api", "platform", "infra", "devops", "sre",
      "systems", "software engineer"), [
        "Design a rate limiter for an API handling 10k requests per second. "
        "Walk me through your approach and the trade-offs.",
        "A production service's p99 latency just doubled. Walk me through "
        "how you'd debug it end to end.",
    ]),
    (("frontend", "front-end", "web", "ui", "ux", "mobile", "ios",
      "android"), [
        "A page takes 8 seconds to become interactive. How do you diagnose "
        "it and what do you fix first?",
        "Walk me through how you'd build an accessible, "
        "keyboard-navigable data table from scratch.",
    ]),
    (("data", "analy", "bi ", "warehouse", "etl"), [
        "A key dashboard metric dropped 20% overnight. How do you figure "
        "out whether it's real or a data bug?",
        "Describe a data pipeline you owned. How did you catch and handle "
        "bad data?",
    ]),
    (("ml", "machine learning", " ai", "nlp", "llm", "deep learning"), [
        "How would you evaluate whether a new model version is safe to ship "
        "to production?",
        "Your model's offline metrics look great but online performance is "
        "flat. What do you check first?",
    ]),
    (("manager", "lead", "director", "head of", "em ",
      "engineering manager"), [
        "Tell me about a time you had to deliver bad news about a slipping "
        "deadline to stakeholders.",
        "How do you decide what tech debt to pay down versus what to live "
        "with?",
    ]),
    (("product", "pm "), [
        "Your top feature request conflicts with the company strategy. How "
        "do you decide what to build?",
        "How do you measure whether a launch actually worked?",
    ]),
    (("design",), [
        "Walk me through a design decision you made that you later "
        "reversed. What changed your mind?",
        "How do you handle feedback that contradicts your design instincts?",
    ]),
    (("sales", "account", "sdr", "customer success", "support"), [
        "Walk me through how you'd run discovery on a first call with a "
        "skeptical prospect.",
        "Tell me about a deal you lost. What did you learn from it?",
    ]),
]

_FALLBACK_TECHNICAL = [
    "Tell me about the hardest technical problem you solved recently -- "
    "what was your specific contribution?",
    "Describe a project you owned end to end. What would you do "
    "differently next time?",
]

_BEHAVIORAL_STAR = [
    "Tell me about the hardest problem you solved in a previous role. "
    "Set the scene, walk me through what you personally did, and share "
    "the outcome.",
    "Tell me about a time you disagreed with a teammate or manager about "
    "a decision. How did you handle it, and what happened?",
]


def _role_technical_questions(role: str) -> list[str]:
    """Pick two technical questions matched to the role title.

    Deterministic: the first bank whose keyword group matches wins;
    falls back to generic ownership questions when nothing matches.
    """
    lowered = (role or "").lower()
    for keywords, questions in _TECHNICAL_BANK:
        if any(kw in lowered for kw in keywords):
            return list(questions)
    return list(_FALLBACK_TECHNICAL)


def _company_question(company: str) -> str:
    """One company-specific question (never asserts facts about them)."""
    company = (company or "this company").strip() or "this company"
    return (
        f"Why {company}? What do you know about what they're building, "
        f"and why does that excite you specifically?"
    )


def _bank_questions(company: str, role: str) -> list[dict[str, str]]:
    """Build the 5-question set from the deterministic question bank."""
    technical = _role_technical_questions(role)
    kinds = ["behavioral", "technical", "company", "behavioral", "technical"]
    texts = [
        _BEHAVIORAL_STAR[0],
        technical[0],
        _company_question(company),
        _BEHAVIORAL_STAR[1],
        technical[1],
    ]
    return [
        {"kind": kind, "question": text}
        for kind, text in zip(kinds, texts)
    ]


def _classify_question(question: str, company: str) -> str:
    """Best-effort kind label for a prep_interview likely_question."""
    lowered = question.lower()
    company_l = (company or "").lower()
    if company_l and company_l in lowered:
        return "company"
    if any(k in lowered for k in ("why ", "why this", "why are you",
                                  "why do you want")):
        return "company"
    if any(k in lowered for k in ("tell me about a time", "disagreement",
                                  "proud of", "hardest", "conflict",
                                  "mistake", "failure")):
        return "behavioral"
    return "technical"


def _questions_from_prep(company: str, role: str,
                         prep: dict[str, Any]) -> list[dict[str, str]] | None:
    """Reuse briefs.prep_interview likely_questions when usable.

    Returns None when the prep doc is missing or too thin, so the caller
    falls back to the question bank. Pads to NUM_QUESTIONS with bank
    questions (deduplicated) so the session always has a full set.
    """
    likely = prep.get("likely_questions") if isinstance(prep, dict) else None
    if not likely or not isinstance(likely, list):
        return None
    questions = [
        {"kind": _classify_question(str(q), company), "question": str(q)}
        for q in likely[:NUM_QUESTIONS]
        if str(q).strip()
    ]
    if len(questions) < 3:
        return None
    seen = {q["question"] for q in questions}
    for bank_q in _bank_questions(company, role):
        if len(questions) >= NUM_QUESTIONS:
            break
        if bank_q["question"] not in seen:
            seen.add(bank_q["question"])
            questions.append(bank_q)
    return questions[:NUM_QUESTIONS]


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


def _load_sessions() -> dict[str, Any]:
    """Read mock_sessions.json; {} when missing or corrupt."""
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_sessions(sessions: dict[str, Any]) -> None:
    """Write mock_sessions.json (pretty, UTF-8)."""
    SESSIONS_FILE.write_text(
        json.dumps(sessions, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _get_session(session_id: str) -> dict[str, Any]:
    """Return the session dict or raise ValueError for an unknown id."""
    session = _load_sessions().get(session_id)
    if session is None:
        raise ValueError(f"No mock interview session {session_id!r}")
    return session


# ---------------------------------------------------------------------------
# Outbound message (whatsapp channel)
# ---------------------------------------------------------------------------


def _build_outbound_message(session: dict[str, Any]) -> str:
    """Format the 5 questions as a ready-to-send chat message.

    Returns text only -- the caller delivers it. Never claims delivery.
    """
    role = session.get("role") or "this role"
    company = session.get("company") or "this company"
    lines = [
        f"Mock interview: {role} at {company} -- {NUM_QUESTIONS} questions.",
        "",
        "Answer one at a time and I'll score each on STAR structure, "
        "specifics, and clarity. This is practice -- nothing is shared "
        "anywhere.",
        "",
    ]
    for i, q in enumerate(session["questions"], start=1):
        lines.append(f"{i}. {q['question']}")
    lines += [
        "",
        'Reply with the question number and your answer, e.g. "1: ...".',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# start_mock_interview
# ---------------------------------------------------------------------------


def start_mock_interview(
    company: str,
    role: str,
    profile: dict[str, Any] | None = None,
    channel: str = "chat",
    job_id: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Start a 5-question mock interview session.

    Args:
        company: Company name, e.g. "Stripe".
        role: Role title, e.g. "Senior Backend Engineer".
        profile: Applicant profile dict (for honest STAR story hints).
            Defaults to {} -- hints are then empty, never invented.
        channel: "chat" (default) or "whatsapp".
        job_id: Optional search_jobs job id. When given, questions are
            reused from ``briefs.prep_interview``'s likely_questions
            (already tailored to the posting); otherwise the
            deterministic role-based question bank is used.
        mode: Optional interviewer persona — one of "recruiter",
            "hiring_manager", "peer", "executive", "adversarial" (see
            ``initiatives.i06.modes``). Selects mode-flavored questions
            and mode-specific rubric weights, and returns a disclosed
            ``mode_card`` describing the persona before the round
            starts. ``None`` (default) keeps the classic question bank
            for backward compatibility.

    Returns:
        For channel="chat": {"session_id", "session", "channel",
        "first_question", "mode", "mode_card", "note"}. For
        channel="whatsapp": {"session_id", "channel", "deliverable_in",
        "outbound_message", "mode", "note"} -- the message is handed to
        the caller for delivery; this function never sends anything.

    Raises:
        ValueError: for an unknown channel or an unknown mode.
    """
    channel = (channel or "chat").strip().lower()
    if channel not in ("chat", "whatsapp"):
        raise ValueError(
            f"Unknown channel {channel!r}; expected 'chat' or 'whatsapp'")
    mode_name = _i06_modes.validate_mode(mode) if mode else None
    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        profile = {}

    company = (company or "").strip() or "this company"
    role = (role or "").strip() or "this role"

    questions: list[dict[str, str]] | None = None
    source = "question_bank"
    if job_id:
        try:
            prep = briefs.prep_interview(job_id, profile=profile)
        except Exception as exc:  # noqa: BLE001 - prep is best-effort
            log.warning("prep_interview failed for %r: %s", job_id, exc)
            prep = {}
        if isinstance(prep, dict) and not prep.get("error"):
            questions = _questions_from_prep(company, role, prep)
            if questions:
                source = "prep_interview"
    if questions is None:
        if mode_name:
            questions = _i06_modes.build_mode_questions(
                mode_name, company, role,
                technical=_role_technical_questions(role))
            source = f"mode_bank:{mode_name}"
        else:
            questions = _bank_questions(company, role)

    # Honest STAR story hints: profile data only, [] when nothing on file.
    try:
        star_hints = briefs._star_stories(profile)  # noqa: SLF001 - same project
    except Exception:  # noqa: BLE001 - defensive
        star_hints = []

    numbered = [
        {"id": f"q{i}", "kind": q["kind"], "question": q["question"],
         "mode": q.get("mode") or mode_name or "classic"}
        for i, q in enumerate(questions, start=1)
    ]
    session = {
        "session_id": uuid.uuid4().hex[:12],
        "company": company,
        "role": role,
        "job_id": job_id or "",
        "question_source": source,
        "channel": channel,
        "mode": mode_name or "classic",
        "questions": numbered,
        "answers": {},
        "star_story_hints": star_hints,
        "complete": False,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    sessions = _load_sessions()
    sessions[session["session_id"]] = session
    _save_sessions(sessions)

    first = {"id": numbered[0]["id"], "kind": numbered[0]["kind"],
             "question": numbered[0]["question"]}
    mode_card = (_i06_modes.mode_card(mode_name) if mode_name else None)
    if channel == "whatsapp":
        return {
            "session_id": session["session_id"],
            "channel": "whatsapp",
            "deliverable_in": "whatsapp_side_chat",
            "outbound_message": _build_outbound_message(session),
            "mode": session["mode"],
            "note": (
                "WhatsApp is the only proactive channel and this message "
                "can only be sent from the WhatsApp side chat (link once: "
                f"{WHATSAPP_CONNECT_URL}). Record each reply with "
                "mock_answer. This function did not send anything."
            ),
        }
    return {
        "session_id": session["session_id"],
        "session": session,
        "channel": "chat",
        "first_question": first,
        "mode": session["mode"],
        "mode_card": mode_card,
        "note": (
            "Ask the first question in the current chat; record answers "
            "with mock_answer (answer_mock_question)."
        ),
    }


# ---------------------------------------------------------------------------
# Deterministic answer feedback (heuristics only -- never invents facts)
# ---------------------------------------------------------------------------

#: STAR structure keyword groups (case-insensitive substring match).
_STAR_GROUPS: dict[str, list[str]] = {
    "situation": ["situation", "context", "background", "the problem",
                  "the challenge", "was facing", "when i joined",
                  "at the time", "the team was"],
    "task": ["task", "goal", "objective", "responsible for", "needed to",
             "my job was", "assigned to", "had to", "was asked to"],
    "action": ["action", "i did", "i built", "i led", "i implemented",
               "i decided", "i designed", "i wrote", "i drove",
               "steps i took", "my approach", "i chose", "i created"],
    "result": ["result", "outcome", "impact", "as a result", "led to",
               "improved", "reduced", "increased", "shipped", "launched",
               "saved", "grew", "cut "],
}

_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_NUMBER_RE = re.compile(r"\d")
_I_RE = re.compile(r"\bi(?:'m|'ve|'ll|'d)?\b", re.IGNORECASE)
_WE_RE = re.compile(r"\bwe(?:'re|'ve|'ll|'d)?\b", re.IGNORECASE)

_TOO_SHORT_WORDS = 20
_MIN_GOOD_WORDS = 40
_MAX_GOOD_WORDS = 250
_TOO_LONG_WORDS = 400


def _star_coverage(answer: str) -> dict[str, bool]:
    """Which STAR groups the answer text mentions (substring match)."""
    lowered = (answer or "").lower()
    return {
        group: any(kw in lowered for kw in keywords)
        for group, keywords in _STAR_GROUPS.items()
    }


def _score_feedback(answer: str,
                    coverage: dict[str, bool]) -> tuple[int, list[str]]:
    """Deterministic 0-100 score + feedback lines for one answer.

    Feedback only ever comments on the answer's own structure and
    wording; it never asserts facts about the candidate.
    """
    words = len(_WORD_RE.findall(answer or ""))
    hits = sum(1 for v in coverage.values() if v)
    has_numbers = bool(_NUMBER_RE.search(answer or ""))
    i_count = len(_I_RE.findall(answer or ""))
    we_count = len(_WE_RE.findall(answer or ""))

    score = 35 + 12 * hits
    feedback: list[str] = []

    missing = [g for g, v in coverage.items() if not v]
    if missing:
        feedback.append(
            "STAR structure: "
            + ("all four parts present (Situation, Task, Action, Result)."
               if not missing else
               f"missing {', '.join(missing)} -- add "
               f"{' and '.join(missing)} explicitly so the interviewer "
               "can follow the arc.")
        )
    else:
        feedback.append(
            "STAR structure: all four parts present (Situation, Task, "
            "Action, Result).")

    if has_numbers:
        score += 10
    else:
        feedback.append(
            "Specificity: no numbers in this answer -- add one measurable "
            "outcome from your real experience (latency, users, revenue, "
            "time saved).")

    if words < _TOO_SHORT_WORDS:
        score -= 15
        feedback.append(
            f"Length: {words} words is too short to evaluate -- aim for "
            f"{_MIN_GOOD_WORDS}-{_MAX_GOOD_WORDS} words with specifics.")
    elif words < _MIN_GOOD_WORDS:
        feedback.append(
            f"Length: {words} words -- a little thin; expand the Action "
            "with one concrete decision you made.")
    elif words <= _MAX_GOOD_WORDS:
        score += 5
    else:
        score -= 10
        feedback.append(
            f"Length: {words} words is long -- tighten to the key "
            "decisions and the outcome.")

    if we_count > i_count and i_count < 2 and words >= _TOO_SHORT_WORDS:
        score -= 5
        feedback.append(
            "Ownership: mostly 'we' with little 'I' -- name your personal "
            "contribution so the interviewer knows what *you* did.")

    if not feedback:
        # Unreachable in practice (STAR branch always appends), but keeps
        # the contract "feedback is never empty" airtight.
        feedback.append("Solid answer -- clear and complete.")

    score = max(0, min(100, score))
    return score, feedback


def answer_mock_question(
    session_id: str,
    answer: str,
    input_modality: str = "text",
    candidate_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Score the session's next unanswered question.

    Args:
        session_id: The session id from start_mock_interview.
        answer: The candidate's answer text.
        input_modality: How the answer was provided — "text" (default)
            or "voice" (via the optional voice mode; see
            ``initiatives.i06.voice``). Recorded per answer for the
            privacy ledger.
        candidate_questions: The candidate's own questions to the
            interviewer (for the rubric's ``question_quality``
            dimension). When omitted the dimension is unscored, never
            zero.

    Returns:
        {"session_id", "question_id", "question_index" (1-based),
        "question", "length_words", "star" ({group: bool, ...,
        "coverage": "3/4"}), "has_numbers", "overall" (0-100, legacy
        craft score), "feedback" ([str]), "rubric" ({"dimensions":
        {dim: {"score", "signals", "feedback"}}, "overall", "weights",
        "unscored"} — the disclosed rubric), "input_modality",
        "answered", "total", "complete", "next_question" ({"id","kind",
        "question"} or None)}.

    Raises:
        ValueError: unknown session, or every question already answered.
    """
    sessions = _load_sessions()
    session = sessions.get(session_id)
    if session is None:
        raise ValueError(f"No mock interview session {session_id!r}")

    questions = session["questions"]
    answers = session["answers"]
    pending = [q for q in questions if q["id"] not in answers]
    if not pending:
        raise ValueError(f"Session {session_id!r} is already complete")

    current = pending[0]
    coverage = _star_coverage(answer)
    overall, feedback = _score_feedback(answer, coverage)
    words = len(_WORD_RE.findall(answer or ""))

    # Initiative 06 rubric: disclosed weights, mode-specific when the
    # session runs in a mode; classic sessions use the rubric defaults.
    session_mode = session.get("mode") or "classic"
    rubric_weights = (_i06_modes.mode_weights(session_mode)
                      if session_mode != "classic" else None)
    rubric = _i06_rubric.score_answer(
        answer, weights=rubric_weights,
        candidate_questions=candidate_questions)
    rubric_flat = {d: r["score"] for d, r in rubric["dimensions"].items()}

    answers[current["id"]] = {
        "answer": str(answer or ""),
        "overall": overall,
        "star": dict(coverage),
        "has_numbers": bool(_NUMBER_RE.search(answer or "")),
        "length_words": words,
        "feedback": feedback,
        "rubric": {
            "dimension_scores": rubric_flat,
            "overall": rubric["overall"],
            "weights": rubric["weights"],
            "unscored": rubric["unscored"],
        },
        "input_modality": input_modality or "text",
    }
    answered = len(answers)
    session["complete"] = answered == len(questions)
    _save_sessions(sessions)

    if session["complete"]:
        _record_longitudinal_result(session)

    remaining = [q for q in questions if q["id"] not in answers]
    nxt = remaining[0] if remaining else None
    return {
        "session_id": session_id,
        "question_id": current["id"],
        "question_index": questions.index(current) + 1,
        "question": current["question"],
        "length_words": words,
        "star": {**coverage,
                 "coverage": f"{sum(1 for v in coverage.values() if v)}/4"},
        "has_numbers": bool(_NUMBER_RE.search(answer or "")),
        "overall": overall,
        "feedback": feedback,
        "rubric": {
            "dimension_scores": rubric_flat,
            "overall": rubric["overall"],
            "weights": rubric["weights"],
            "unscored": rubric["unscored"],
        },
        "input_modality": input_modality or "text",
        "answered": answered,
        "total": len(questions),
        "complete": session["complete"],
        "next_question": (
            {"id": nxt["id"], "kind": nxt["kind"],
             "question": nxt["question"]} if nxt else None
        ),
    }


def _record_longitudinal_result(session: dict[str, Any]) -> None:
    """Best-effort: log a completed session to the practice history.

    Never raises — answering must work even if the history store is
    unavailable. Tests can redirect via ``_LONGITUDINAL_HISTORY_PATH``.
    """
    try:
        dim_totals: dict[str, list[int]] = {}
        rubric_overalls: list[int] = []
        for entry in session.get("answers", {}).values():
            rub = entry.get("rubric") or {}
            for dim, score in (rub.get("dimension_scores") or {}).items():
                if score is not None:
                    dim_totals.setdefault(dim, []).append(score)
            if rub.get("overall") is not None:
                rubric_overalls.append(rub["overall"])
        dimension_scores = {
            d: round(sum(v) / len(v)) for d, v in dim_totals.items() if v
        }
        for dim in _i06_rubric.DIMENSIONS:
            dimension_scores.setdefault(dim, None)
        _i06_longitudinal.record_result(
            source="mock_interview",
            label=f"{session.get('mode', 'classic')} @ "
                  f"{session.get('company', '')}",
            dimension_scores=dimension_scores,
            overall=(round(sum(rubric_overalls) / len(rubric_overalls))
                     if rubric_overalls else None),
            mode=session.get("mode", "classic"),
            session_id=session.get("session_id", ""),
            job_id=session.get("job_id", ""),
            history_path=_LONGITUDINAL_HISTORY_PATH,
        )
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        log.warning("longitudinal record failed: %s", exc)


#: Tests redirect the longitudinal history here; None = default path.
_LONGITUDINAL_HISTORY_PATH: Path | None = None


# ---------------------------------------------------------------------------
# mock_summary
# ---------------------------------------------------------------------------


def _top_tips(per_question: list[dict[str, Any]]) -> list[str]:
    """Derive the top 2 improvement tips from answered questions.

    Deterministic priority order; every tip refers to answer craft, never
    to invented candidate facts.
    """
    answered = [p for p in per_question if p["answered"]]
    if not answered:
        return [
            "Answer at least one question to get scored feedback -- start "
            "with the behavioral ones, they carry the most weight.",
            "Use the STAR shape (Situation, Task, Action, Result) and one "
            "real number per answer.",
        ]
    tips: list[str] = []
    avg_star = sum(p["star_hits"] for p in answered) / len(answered)
    frac_numbers = sum(1 for p in answered if p["has_numbers"]) / len(answered)
    avg_len = sum(p["length_words"] for p in answered) / len(answered)
    we_heavy = any(p["we_heavy"] for p in answered)

    if avg_star < 3:
        tips.append(
            "Structure: name the STAR parts explicitly -- most answers are "
            "missing Situation, Task, Action, or Result. Say the words.")
    if frac_numbers < 0.5:
        tips.append(
            "Specificity: fewer than half your answers include a number. "
            "Add one measurable outcome per answer from your real "
            "experience.")
    if avg_len < _MIN_GOOD_WORDS:
        tips.append(
            "Depth: answers average under "
            f"{_MIN_GOOD_WORDS} words -- expand the Action with one "
            "concrete decision you made and why.")
    if we_heavy:
        tips.append(
            "Ownership: at least one answer leans on 'we' -- restate your "
            "personal contribution with 'I'.")
    if not tips:
        tips.append(
            "Strong round -- polish by tailoring one answer to the "
            "company's actual product or recent news.")
        tips.append(
            "Rehearse your two best STAR stories out loud until each fits "
            "in about two minutes.")
    return tips[:2]


def mock_summary(session_id: str) -> dict[str, Any]:
    """Summarize a mock interview session.

    Returns {"session_id", "company", "role", "answered", "total",
    "complete", "per_question" ([{question_id, kind, question,
    answered, overall, star_coverage, star_hits, has_numbers,
    length_words, we_heavy, top_tip}]), "overall_score" (mean of
    answered, 0 when none), "top_tips" ([2 str]), "unanswered" ([ids]),
    "markdown"}.

    Raises:
        ValueError: unknown session.
    """
    session = _get_session(session_id)
    questions = session["questions"]
    answers = session.get("answers", {})

    per_question = []
    for q in questions:
        entry = answers.get(q["id"])
        if entry is None:
            per_question.append({
                "question_id": q["id"],
                "kind": q["kind"],
                "question": q["question"],
                "answered": False,
                "overall": None,
                "star_coverage": "0/4",
                "star_hits": 0,
                "has_numbers": False,
                "length_words": 0,
                "we_heavy": False,
                "top_tip": "Unanswered -- take a swing at it.",
            })
            continue
        star = entry.get("star", {})
        hits = sum(1 for v in star.values() if v)
        fb = entry.get("feedback", [])
        per_question.append({
            "question_id": q["id"],
            "kind": q["kind"],
            "question": q["question"],
            "answered": True,
            "overall": entry.get("overall"),
            "star_coverage": f"{hits}/4",
            "star_hits": hits,
            "has_numbers": bool(entry.get("has_numbers")),
            "length_words": entry.get("length_words", 0),
            "we_heavy": any("Ownership" in line for line in fb),
            "top_tip": fb[0] if fb else "",
        })

    answered_n = sum(1 for p in per_question if p["answered"])
    scored = [p["overall"] for p in per_question
              if p["answered"] and p["overall"] is not None]
    overall_score = round(sum(scored) / len(scored)) if scored else 0
    tips = _top_tips(per_question)

    # Initiative 06: per-dimension rubric averages across answered
    # questions (unscored dimensions stay None, never 0).
    rubric_averages: dict[str, int | None] = {}
    for dim in _i06_rubric.DIMENSIONS:
        dim_scores = [
            answers[q["id"]]["rubric"]["dimension_scores"][dim]
            for q in questions
            if q["id"] in answers
            and dim in (answers[q["id"]].get("rubric") or {}).get(
                "dimension_scores", {})
            and answers[q["id"]]["rubric"]["dimension_scores"][dim]
            is not None
        ]
        rubric_averages[dim] = (round(sum(dim_scores) / len(dim_scores))
                                if dim_scores else None)

    summary: dict[str, Any] = {
        "session_id": session_id,
        "company": session.get("company", ""),
        "role": session.get("role", ""),
        "mode": session.get("mode", "classic"),
        "answered": answered_n,
        "total": len(questions),
        "complete": bool(session.get("complete")),
        "per_question": per_question,
        "overall_score": overall_score,
        "rubric_averages": rubric_averages,
        "top_tips": tips,
        "unanswered": [p["question_id"] for p in per_question
                       if not p["answered"]],
        "markdown": "",
    }
    summary["markdown"] = _summary_markdown(summary)
    return summary


def show_rubric() -> dict[str, Any]:
    """Return the disclosed interview rubric and interview modes.

    Everything the lab scores against is public: dimension weights,
    what each measures, how it is scored, and each mode's persona and
    scoring weights. No hidden factors.
    """
    return {
        "rubric": _i06_rubric.DISCLOSED_RUBRIC,
        "meets_threshold_default": _i06_rubric.MEETS_THRESHOLD,
        "threshold_note": (
            "Default threshold; the final threshold is approved by Paul "
            "and the independent framework reviewer (roadmap Q1 gate)."),
        "modes": {
            name: {
                "label": spec["label"],
                "persona": spec["persona"],
                "round_shape": spec["round_shape"],
                "weights": spec["weights"],
            }
            for name, spec in _i06_modes.MODES.items()
        },
        "markdown": _i06_rubric.rubric_card(),
    }


def _summary_markdown(summary: dict[str, Any]) -> str:
    """Human-readable summary for chat/WhatsApp display."""
    lines = [
        f"# Mock interview summary: {summary['role']} @ {summary['company']}",
        "",
        f"**Mode:** {summary.get('mode', 'classic')}",
        f"**Score:** {summary['overall_score']}/100 "
        f"({summary['answered']}/{summary['total']} answered)",
        "",
        "## Rubric averages (disclosed rubric)",
    ]
    for dim, avg in (summary.get("rubric_averages") or {}).items():
        mark = f"{avg}/100" if avg is not None else "unscored"
        lines.append(f"- {dim.replace('_', ' ')}: {mark}")
    lines += ["", "## Per-question scores"]
    for p in summary["per_question"]:
        mark = p["overall"] if p["answered"] else "--"
        lines.append(
            f"- {p['question_id']} [{p['kind']}] {mark}/100 "
            f"(STAR {p['star_coverage']})")
    lines += ["", "## Top tips"]
    lines.extend(f"{i}. {t}" for i, t in enumerate(summary["top_tips"], 1))
    if summary["unanswered"]:
        lines += ["",
                  f"Unanswered: {', '.join(summary['unanswered'])}"]
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# MCP / CLI registration (server.py / cli.py call these; this module is
# never imported by them at module load, so there is no import cycle)
# ---------------------------------------------------------------------------


def _print_result(result: Any, as_json: bool) -> None:
    """Print a handler result (JSON always -- machine-first, like grill)."""
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def cmd_mock_start(args: Any) -> int:
    """CLI handler for `mock-start`."""
    profile: dict[str, Any] = {}
    try:
        from server import _load_saved_profile as _load  # lazy, no cycle

        loaded = _load()
        if isinstance(loaded, dict):
            profile = loaded
    except Exception:  # noqa: BLE001 - server may be unavailable
        pass
    try:
        result = start_mock_interview(
            args.company, args.role, profile,
            channel=getattr(args, "channel", "chat") or "chat",
            job_id=getattr(args, "job_id", None),
            mode=getattr(args, "mode", None),
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    _print_result(result, getattr(args, "json", False))
    return 0


def cmd_mock_answer(args: Any) -> int:
    """CLI handler for `mock-answer`."""
    try:
        result = answer_mock_question(args.session_id, args.answer)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    _print_result(result, getattr(args, "json", False))
    return 0


def cmd_mock_summary(args: Any) -> int:
    """CLI handler for `mock-summary`."""
    try:
        result = mock_summary(args.session_id)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    if getattr(args, "json", False):
        _print_result(result, True)
    else:
        print(result["markdown"])
    return 0


def cmd_mock_rubric(args: Any) -> int:
    """CLI handler for `mock-rubric` (disclosed rubric + modes)."""
    result = show_rubric()
    if getattr(args, "json", False):
        _print_result(result, True)
    else:
        print(result["markdown"])
        for name, spec in result["modes"].items():
            print(f"--- {name}: {spec['label']} ---")
            print(spec["persona"])
    return 0


def register_tools(mcp: Any) -> None:
    """Register the mock-interview MCP tools on an MCP server instance."""
    _impl_start = globals()["start_mock_interview"]
    _impl_answer = globals()["answer_mock_question"]
    _impl_summary = globals()["mock_summary"]
    _impl_rubric = globals()["show_rubric"]

    @mcp.tool()
    def start_mock_interview(company: str, role: str,
                             channel: str = "chat",
                             mode: str = "hiring_manager") -> dict:
        """Start a 5-question mock interview for a role at a company.

        Mixes behavioral (STAR), role-specific technical, and
        company-specific questions. With channel="whatsapp" returns a
        ready-to-send message for the WhatsApp side chat to deliver;
        nothing is ever sent by this tool. Every answer is scored
        against the disclosed interview rubric (structure, evidence,
        clarity, trade-offs, question quality).

        Args:
            company: Company name, e.g. "Stripe".
            role: Role title, e.g. "Senior Backend Engineer".
            channel: "chat" (default) or "whatsapp".
            mode: Interviewer persona — "recruiter",
                "hiring_manager" (default), "peer", "executive", or
                "adversarial". The persona, round shape, and scoring
                weights are disclosed in the returned mode_card.
        """
        try:
            from server import _load_saved_profile as _load  # lazy, no cycle

            profile = _load()
        except Exception:  # noqa: BLE001 - server may be unavailable
            profile = {}
        try:
            return _impl_start(company, role, profile, channel=channel,
                               mode=mode)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def answer_mock_question(session_id: str, answer: str,
                             input_modality: str = "text",
                             candidate_questions: list | None = None
                             ) -> dict:
        """Score the next unanswered mock-interview question.

        Deterministic heuristics: STAR structure coverage, specificity
        (numbers present?), length sanity, and "I vs we" ownership —
        plus the disclosed rubric (structure, evidence, clarity,
        trade-offs, question quality). No LLM; feedback never invents
        facts about the candidate.

        Args:
            session_id: The session id from start_mock_interview.
            answer: The candidate's answer text.
            input_modality: "text" (default) or "voice" — recorded per
                answer for the privacy ledger.
            candidate_questions: The candidate's own questions to the
                interviewer, for the question-quality dimension.
        """
        try:
            return _impl_answer(session_id, answer,
                                input_modality=input_modality,
                                candidate_questions=candidate_questions)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def mock_summary(session_id: str) -> dict:
        """Summarize a mock interview: per-question scores + top 2 tips.

        Args:
            session_id: The session id from start_mock_interview.
        """
        try:
            return _impl_summary(session_id)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def mock_rubric() -> dict:
        """Show the disclosed interview rubric and interview modes.

        Everything the lab scores against is public: dimension
        weights, scoring rules, and each mode's persona and weights.
        """
        return _impl_rubric()


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `mock-start` / `mock-answer` / `mock-summary` subcommands.

    Returns a {command: handler} mapping the caller merges into its own
    dispatch table (cli.py-style).
    """
    p_start = subparsers.add_parser(
        "mock-start", help="Start a 5-question mock interview session.")
    p_start.add_argument("company", help='Company name, e.g. "Stripe".')
    p_start.add_argument("role", help='Role title, e.g. "Backend Engineer".')
    p_start.add_argument(
        "--channel", choices=["chat", "whatsapp"], default="chat",
        help="Where the questions go (default: chat).")
    p_start.add_argument(
        "--job-id", default=None,
        help="Optional search_jobs job id: reuse its tailored questions.")
    p_start.add_argument(
        "--mode", default=None,
        choices=["recruiter", "hiring_manager", "peer", "executive",
                 "adversarial"],
        help=("Interviewer persona: mode-flavored questions and disclosed "
              "mode-specific rubric weights (default: classic bank)."))
    p_start.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    p_answer = subparsers.add_parser(
        "mock-answer", help="Score the next unanswered mock question.")
    p_answer.add_argument("session_id",
                          help="Session id from mock-start.")
    p_answer.add_argument("answer", help="The candidate's answer text.")
    p_answer.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    p_summary = subparsers.add_parser(
        "mock-summary", help="Per-question scores + top 2 improvement tips.")
    p_summary.add_argument("session_id",
                           help="Session id from mock-start.")
    p_summary.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    p_rubric = subparsers.add_parser(
        "mock-rubric",
        help="Show the disclosed interview rubric and modes.")
    p_rubric.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output.")

    return {
        "mock-start": cmd_mock_start,
        "mock-answer": cmd_mock_answer,
        "mock-summary": cmd_mock_summary,
        "mock-rubric": cmd_mock_rubric,
    }
