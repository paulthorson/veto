#!/usr/bin/env python3
"""Initiative 06 — role-specific interview simulation modes.

Five disclosed modes. Each mode publishes:

* ``label`` / ``persona`` — who this interviewer is and what they are
  listening for (shown to the user before the round starts).
* ``round_shape`` — what the session covers.
* ``weights`` — per-dimension rubric weights for this mode (they sum to
  1.0 and are disclosed alongside the rubric).
* ``questions`` — deterministic question banks: behavioral openers,
  mode-specific probes, and a closing "your questions for us" slot.

The adversarial mode stress-tests the argument, never the person: it
uses the same banned-token list as soft_skills.py (asserted in tests —
a tough follow-up may challenge a claim but may not insult the
candidate).

Question selection is deterministic (no LLM): banks are indexed by a
stable hash of (mode, company, role) so a session is reproducible, and
job-specific questions from ``briefs.prep_interview`` can be slotted in
by the caller.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Tokens an adversarial follow-up must never use: it attacks the
# argument, never the person. Mirrors soft_skills._BANNED_TOKENS.
BANNED_TOKENS = (
    "stupid", "idiot", "incompetent", "loser", "pathetic", "worthless",
    "clueless", "dumb",
)

MODES: dict[str, dict[str, Any]] = {
    "recruiter": {
        "label": "Recruiter screen",
        "persona": (
            "A friendly recruiter running a 30-minute screen. They care "
            "about motivation, background fit, and logistics — can you "
            "explain what you want and why this role, crisply?"
        ),
        "round_shape": (
            "Background walkthrough, motivation for this role, one STAR "
            "story, logistics, then your questions for them."
        ),
        "weights": {"structure": 0.20, "evidence": 0.20, "clarity": 0.35,
                    "trade_offs": 0.05, "question_quality": 0.20},
        "behavioral": [
            "Walk me through your background in two minutes — what led "
            "you to this role?",
            "Why this company and this role, specifically?",
            "What are you optimizing for in your next move?",
        ],
        "probes": [
            "Tell me about a recent project you're proud of — what was "
            "your part?",
            "What's your timeline, and are you talking to other companies?",
            "What compensation range are you targeting, and how did you "
            "land on it?",
        ],
    },
    "hiring_manager": {
        "label": "Hiring manager",
        "persona": (
            "The person you'd actually report to. They probe ownership, "
            "judgment, and how you handle the messy middle — trade-offs, "
            "disagreement, and delivery under constraints."
        ),
        "round_shape": (
            "Deep STAR stories, a trade-off walkthrough, a disagreement "
            "story, then your questions for the team."
        ),
        "weights": {"structure": 0.25, "evidence": 0.30, "clarity": 0.15,
                    "trade_offs": 0.25, "question_quality": 0.05},
        "behavioral": [
            "Tell me about the hardest problem you solved in the last "
            "two years. Set the scene and walk me through what you "
            "personally did.",
            "Tell me about a time you disagreed with a teammate or your "
            "manager about a decision. What happened?",
            "Describe a project you owned end to end. What would you do "
            "differently next time?",
        ],
        "probes": [
            "Walk me through a decision where you had to choose between "
            "two reasonable options. What did you pick, and what did it "
            "cost?",
            "Tell me about a deadline you knew you'd miss. When did you "
            "know, who did you tell, and what did you do?",
            "What's the sharpest feedback you've received, and what did "
            "you change because of it?",
        ],
    },
    "peer": {
        "label": "Peer interview",
        "persona": (
            "A future teammate. They want to know what it's like to work "
            "with you: how you collaborate, give and take feedback, and "
            "handle technical disagreement."
        ),
        "round_shape": (
            "Collaboration stories, a technical disagreement, code/design "
            "review habits, then your questions about the team."
        ),
        "weights": {"structure": 0.20, "evidence": 0.25, "clarity": 0.25,
                    "trade_offs": 0.20, "question_quality": 0.10},
        "behavioral": [
            "Tell me about a time a teammate's approach conflicted with "
            "yours. How did you resolve it?",
            "Describe the best code or design review feedback you ever "
            "gave — and received.",
            "Tell me about a time you helped someone else succeed at "
            "work.",
        ],
        "probes": [
            "Your teammate wants to ship a shortcut you think is risky. "
            "Walk me through the conversation.",
            "How do you bring a new teammate up to speed on a system you "
            "own?",
            "Tell me about a time you were wrong about something "
            "technical. How did you find out?",
        ],
    },
    "executive": {
        "label": "Executive interview",
        "persona": (
            "A director/VP with 20 minutes and a business lens. They "
            "listen for impact, brevity, and whether you connect your "
            "work to outcomes they care about."
        ),
        "round_shape": (
            "Impact stories with business outcomes, vision and ambition, "
            "then two sharp questions from you."
        ),
        "weights": {"structure": 0.15, "evidence": 0.35, "clarity": 0.35,
                    "trade_offs": 0.05, "question_quality": 0.10},
        "behavioral": [
            "What's the most business impact you've had in a role? Give "
            "me the numbers.",
            "Where do you want to be in three years, and why does this "
            "role get you there?",
            "Tell me about a time you influenced a decision above your "
            "level.",
        ],
        "probes": [
            "If you joined tomorrow, what's the first thing you'd want "
            "to understand about this business?",
            "Tell me about a bet you made that didn't pay off. What did "
            "you learn?",
            "How do you decide what NOT to work on?",
        ],
    },
    "adversarial": {
        "label": "Adversarial (stress test)",
        "persona": (
            "A skeptical interviewer who stress-tests your claims. "
            "Follow-ups poke at weak evidence and hand-waved trade-offs. "
            "The pressure is on the ARGUMENT — never on you as a person."
        ),
        "round_shape": (
            "Short answers challenged with pointed follow-ups; every "
            "claim gets a 'prove it' or 'what did it cost'. Then your "
            "questions, which are also stress-tested."
        ),
        "weights": {"structure": 0.20, "evidence": 0.35, "clarity": 0.15,
                    "trade_offs": 0.25, "question_quality": 0.05},
        "behavioral": [
            "Tell me about the hardest problem you solved recently — and "
            "be specific, I'll ask for numbers.",
            "You say that project was a success. Who disagreed, and were "
            "they right about anything?",
            "Walk me through a decision you made that you'd defend "
            "today. Convince me.",
        ],
        "probes": [
            "That sounds like the team's win. What did YOU do that no "
            "one else could have?",
            "What was the cost of that decision — and who paid it?",
            "Give me the number. If you don't have one, say so and tell "
            "me what you'd measure next time.",
        ],
    },
}

MODE_NAMES = tuple(MODES.keys())
DEFAULT_MODE = "hiring_manager"

#: Closing slot present in every mode: the candidate's own questions.
CLOSING_QUESTIONS = [
    "Now your turn: what are your top two questions for the interviewer?",
]

#: Deterministic follow-up openers per mode (the interviewer "presses"
#: after an answer). Never personal attacks — asserted in tests.
FOLLOWUPS: dict[str, list[str]] = {
    "recruiter": [
        "Got it — and what specifically excites you about this team?",
        "Thanks. One more: what would your last manager say you need "
        "to work on?",
    ],
    "hiring_manager": [
        "And what did that cost — time, money, goodwill?",
        "What would you do differently with what you know now?",
    ],
    "peer": [
        "How did the other person react to that?",
        "What did you learn about working with them?",
    ],
    "executive": [
        "In one sentence: what was the business outcome?",
        "What would you need from me to repeat that here?",
    ],
    "adversarial": [
        "Prove it — what number backs that up?",
        "What's the strongest argument AGAINST what you just said?",
        "Who disagreed with you, and what was their best point?",
    ],
}


def validate_mode(mode: str | None) -> str:
    """Return the canonical mode name; raise ValueError on unknown."""
    m = (mode or DEFAULT_MODE).strip().lower()
    if m not in MODES:
        raise ValueError(
            f"Unknown interview mode {mode!r}. "
            f"Choose one of: {', '.join(MODE_NAMES)}.")
    return m


def _pick(items: list[str], salt: str, n: int) -> list[str]:
    """Deterministically pick n items from a bank (stable order)."""
    if n >= len(items):
        return list(items)
    digest = hashlib.sha256(salt.encode()).digest()
    start = digest[0] % len(items)
    return [items[(start + i) % len(items)] for i in range(n)]


def build_mode_questions(
    mode: str,
    company: str,
    role: str,
    technical: list[str] | None = None,
    num_questions: int = 5,
) -> list[dict[str, str]]:
    """Build a deterministic question set for a mode.

    Layout: 2 behavioral (mode-flavored) + up to 2 technical (role bank,
    supplied by the caller) + 1 mode probe, then a closing
    candidate-questions slot appended by the caller as needed.
    """
    mode = validate_mode(mode)
    spec = MODES[mode]
    salt = f"{mode}|{company}|{role}"
    behavioral = _pick(spec["behavioral"], salt + "|b", 2)
    probes = _pick(spec["probes"], salt + "|p", 1)
    tech = list(technical or [])[:2]
    texts: list[tuple[str, str]] = []
    for i, q in enumerate(behavioral):
        texts.append(("behavioral", q))
        if i < len(tech):
            texts.append(("technical", tech[i]))
    texts.append(("probe", probes[0]))
    return [{"kind": kind, "question": text, "mode": mode}
            for kind, text in texts[:num_questions]]


def mode_card(mode: str) -> str:
    """Disclosed mode description (shown before the round starts)."""
    mode = validate_mode(mode)
    spec = MODES[mode]
    weights = ", ".join(
        f"{d.replace('_', ' ')} {w:.0%}"
        for d, w in spec["weights"].items())
    return (
        f"## {spec['label']}\n\n"
        f"{spec['persona']}\n\n"
        f"**Round:** {spec['round_shape']}\n\n"
        f"**Scoring weights this round:** {weights}\n"
    )


def assert_no_banned_tokens() -> None:
    """Raise if any adversarial follow-up contains a banned token.

    Called in tests; the invariant is "attack the argument, never the
    person".
    """
    for mode, followups in FOLLOWUPS.items():
        for text in followups:
            lowered = text.lower()
            for token in BANNED_TOKENS:
                # Word-boundary, same as the MODES banks below: a word
                # merely containing a banned token ("dumbo" contains
                # "dumb") must not trip the assert.
                if re.search(rf"\b{re.escape(token)}\b", lowered):
                    raise AssertionError(
                        f"Banned token {token!r} in {mode} follow-up: "
                        f"{text!r}")
    for mode, spec in MODES.items():
        for key in ("behavioral", "probes", "persona"):
            items = spec[key] if isinstance(spec[key], list) else [spec[key]]
            for text in items:
                lowered = str(text).lower()
                for token in BANNED_TOKENS:
                    if re.search(rf"\b{re.escape(token)}\b", lowered):
                        raise AssertionError(
                            f"Banned token {token!r} in {mode}.{key}: "
                            f"{text!r}")


def mode_weights(mode: str) -> dict[str, float]:
    """Disclosed per-dimension rubric weights for a mode (sums to 1.0)."""
    mode = validate_mode(mode)
    return dict(MODES[mode]["weights"])
