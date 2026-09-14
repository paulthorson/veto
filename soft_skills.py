#!/usr/bin/env python3
"""Soft-skills training for the Veto job-search tool.

Job seekers lose offers on soft skills: unclear communication, weak STAR
stories, poor negotiation, low executive presence. This module provides:

- ``SKILL_AREAS``: the skill catalog (description + what "good" looks like).
- ``assess(area, answers)``: a short deterministic diagnostic per area.
- ``drill(area, profile, mode, difficulty)`` / ``review_drill(...)``:
  practice scenarios. ``mode="coach"`` is a single friendly rep;
  ``mode="adversarial"`` is a 3-round tough interviewer that stays
  in character during the round and breaks character for labeled COACH
  feedback afterwards.
- ``negotiation_sim(...)`` / ``negotiation_round(...)``: a 3-round
  salary negotiation against an adversarial hiring manager
  (difficulty: firm / hardball / brutal). The adversary attacks the
  ARGUMENT, never the person — tough but professional. Between rounds
  the sim breaks character with labeled COACH feedback.
- ``log_practice`` / ``progress``: practice reps (logged as practice,
  never as credentials) with per-area trends.
- Initiative 06 lab: ``scenario_kinds()`` / ``start_scenario(kind)`` /
  ``review_scenario(...)`` / ``scenario_summary(...)`` — four disclosed
  3-round scenarios (conflict, influence, negotiation,
  ambiguous_stakeholder), each scored against the disclosed rubric plus
  scenario behavior signals, and recorded to the longitudinal practice
  history on completion.

Honesty contract: feedback describes the qualities of the ANSWER the
user gave. Drill prompts are built ONLY from the profile's real
experience — achievements are never invented, and practice reps are
never presented as certifications.

Stdlib only, no network, deterministic (no LLM).
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Initiative 06 soft-skill lab (same repo).
from initiatives.i06 import longitudinal as _i06_longitudinal
from initiatives.i06 import rubric as _i06_rubric
from initiatives.i06 import scenarios as _i06_scenarios

log = logging.getLogger("job-apply-mcp.soft_skills")

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_PATH = BASE_DIR / "soft_skill_sessions.json"
PROGRESS_PATH = BASE_DIR / "soft_skills.json"
PROFILE_PATH = BASE_DIR / "profiles" / "profile.json"

DIFFICULTIES = ("firm", "hardball", "brutal")
DRILL_MODES = ("coach", "adversarial")
LEVELS = ("developing", "practicing", "strong")

# Words the adversary must never use: it attacks the argument, not the
# person. (Enforced by construction in the line banks below, and asserted
# in tests.)
_BANNED_TOKENS = (
    "stupid", "idiot", "incompetent", "loser", "pathetic", "worthless",
    "clueless", "dumb",
)

# ---------------------------------------------------------------------------
# Skill catalog
# ---------------------------------------------------------------------------

SKILL_AREAS: dict[str, dict[str, str]] = {
    "communication_clarity": {
        "description": (
            "Saying the important thing first, in plain language, without "
            "jargon or rambling. The skill behind every update, email, and "
            "interview answer."
        ),
        "good_looks_like": (
            "Bottom line up front, then supporting detail. A non-technical "
            "stakeholder can repeat your point back. Written updates fit in "
            "a glance; spoken answers land in under two minutes."
        ),
    },
    "star_storytelling": {
        "description": (
            "Turning your experience into Situation-Task-Action-Result "
            "stories with specific outcomes. The core unit of behavioral "
            "interviews."
        ),
        "good_looks_like": (
            "Every story has a 30-second setup, YOUR specific actions "
            "(not 'we'), and a measurable result. Numbers appear naturally; "
            "nothing is inflated."
        ),
    },
    "leadership_influence": {
        "description": (
            "Moving people and decisions without relying on authority: "
            "framing, listening, and navigating disagreement."
        ),
        "good_looks_like": (
            "You understand what the other party wants before you ask. "
            "Disagreement is surfaced early and resolved 1:1. Decisions get "
            "made, committed to, and owned."
        ),
    },
    "negotiation": {
        "description": (
            "Anchoring, justifying, and holding your number under pressure: "
            "offers, scope, timelines. Negotiation is a conversation, not a "
            "confrontation."
        ),
        "good_looks_like": (
            "You anchor with a researched range, justify with market data "
            "and specific value, stay warm under pressure tactics, and never "
            "bid against yourself."
        ),
    },
    "active_listening": {
        "description": (
            "Hearing what is actually being said — including the concern "
            "underneath the words — before responding."
        ),
        "good_looks_like": (
            "You reflect the concern back before answering it. You ask the "
            "question behind the question. Comfortable with silence."
        ),
    },
    "executive_presence": {
        "description": (
            "Calm, concise authority in high-stakes rooms: disagreeing well, "
            "handling surprises, and making your value obvious fast."
        ),
        "good_looks_like": (
            "Thirty-second versions of everything. Disagreement is brief, "
            "respectful, and framed around risk. Surprises are narrated "
            "calmly, not apologized into."
        ),
    },
}

# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

# Each question: scenario prompt + 3 choices scored 1/2/3 (developing /
# practicing / strong) with specific feedback per choice.
_ASSESS_QUESTIONS: dict[str, list[dict[str, Any]]] = {
    "communication_clarity": [
        {
            "prompt": (
                "You're explaining a 3-week project delay to a "
                "non-technical stakeholder. You open with…"
            ),
            "choices": [
                {
                    "text": (
                        "The technical cause: consumer lag, partitioning, "
                        "the new event schema…"
                    ),
                    "score": 1,
                    "feedback": (
                        "Jargon-first loses the room. Lead with the bottom "
                        "line (the new date), then the plain-language why."
                    ),
                },
                {
                    "text": (
                        "The business impact, then offer to explain the "
                        "technical cause if they want it."
                    ),
                    "score": 2,
                    "feedback": (
                        "Good instinct — impact before mechanism. Stronger "
                        "still: state the new date in the first sentence."
                    ),
                },
                {
                    "text": (
                        "\"Launch moves from June 1 to June 21. Here's why "
                        "in plain terms, and the plan to hold the new date.\""
                    ),
                    "score": 3,
                    "feedback": (
                        "Exactly right: bottom line up front, plain "
                        "language, forward-looking plan."
                    ),
                },
            ],
        },
        {
            "prompt": (
                "In a status meeting you're asked something you don't know. "
                "You…"
            ),
            "choices": [
                {
                    "text": "Answer vaguely — a confident tone covers it.",
                    "score": 1,
                    "feedback": (
                        "Bluffing destroys trust faster than ignorance. "
                        "Say you don't know, then commit to a follow-up "
                        "time."
                    ),
                },
                {
                    "text": (
                        "\"Good question — let me think…\" then think out "
                        "loud for a while."
                    ),
                    "score": 2,
                    "feedback": (
                        "Honest, but rambling burns the room's patience. "
                        "Defer cleanly instead of thinking aloud."
                    ),
                },
                {
                    "text": (
                        "\"I don't know — I'll find out by EOD and post it "
                        "in the channel.\""
                    ),
                    "score": 3,
                    "feedback": (
                        "Perfect: honest, specific, and it closes the loop "
                        "without derailing the meeting."
                    ),
                },
            ],
        },
        {
            "prompt": "Your written status update is 800 words. Best edit?",
            "choices": [
                {
                    "text": "Send it — detail shows diligence.",
                    "score": 1,
                    "feedback": (
                        "Nobody reads 800-word updates. Respect the "
                        "reader's time: TL;DR first, detail on demand."
                    ),
                },
                {
                    "text": "Trim it to ~400 words.",
                    "score": 2,
                    "feedback": (
                        "Better, but still a wall of text. Structure beats "
                        "length: bullets, headlines, appendix."
                    ),
                },
                {
                    "text": "Cut to a 5-bullet TL;DR with the rest as an appendix.",
                    "score": 3,
                    "feedback": (
                        "Right: scannable headline version, full detail "
                        "available for whoever wants it."
                    ),
                },
            ],
        },
    ],
    "star_storytelling": [
        {
            "prompt": (
                "\"Tell me about a challenging project.\" You start with…"
            ),
            "choices": [
                {
                    "text": "What you personally did, right away.",
                    "score": 2,
                    "feedback": (
                        "Action-first skips the setup — the interviewer "
                        "can't judge the achievement without the stakes. "
                        "Give 30 seconds of context first."
                    ),
                },
                {
                    "text": "\"Long story, basically we had issues…\"",
                    "score": 1,
                    "feedback": (
                        "Apologizing for the story tells them it isn't "
                        "worth hearing. Open with context, your role, and "
                        "the stakes — confidently."
                    ),
                },
                {
                    "text": (
                        "A 30-second setup: the context, your role, and "
                        "what was at stake."
                    ),
                    "score": 3,
                    "feedback": (
                        "Exactly: Situation and Task in 30 seconds, then "
                        "your Actions, then the Result."
                    ),
                },
            ],
        },
        {
            "prompt": (
                "Your story has no measurable outcome. How do you fix it?"
            ),
            "choices": [
                {
                    "text": "Leave it — the impact is implied.",
                    "score": 1,
                    "feedback": (
                        "Implied impact reads as no impact. Find the "
                        "closest honest number, even a directional one."
                    ),
                },
                {
                    "text": "Estimate generously — close enough.",
                    "score": 2,
                    "feedback": (
                        "Danger zone: inflated numbers unravel under one "
                        "follow-up question. Directional and honest beats "
                        "big and shaky."
                    ),
                },
                {
                    "text": (
                        "Add the closest honest number, even if it's "
                        "directional (\"roughly 30% faster\")."
                    ),
                    "score": 3,
                    "feedback": (
                        "Right — specific, honest, and defensible. Numbers "
                        "make stories memorable."
                    ),
                },
            ],
        },
        {
            "prompt": "You keep saying \"we\" in your stories. The fix:",
            "choices": [
                {
                    "text": "Keep it — teamwork matters.",
                    "score": 2,
                    "feedback": (
                        "Teamwork does matter, but the interviewer is "
                        "hiring YOU. Credit the team, then name your "
                        "specific actions with \"I\"."
                    ),
                },
                {
                    "text": "Name your specific actions with \"I\", crediting the team.",
                    "score": 3,
                    "feedback": (
                        "The balance: \"the team shipped X; my part was Y.\" "
                        "Ownership plus generosity."
                    ),
                },
                {
                    "text": "Rewrite everything as solo heroics.",
                    "score": 1,
                    "feedback": (
                        "Overcorrection — solo-hero stories sound inflated "
                        "and raise collaboration concerns."
                    ),
                },
            ],
        },
    ],
    "leadership_influence": [
        {
            "prompt": (
                "You need another team's engineering time but have no "
                "authority over them. First move?"
            ),
            "choices": [
                {
                    "text": "Escalate to your manager to apply pressure.",
                    "score": 1,
                    "feedback": (
                        "Escalation first burns goodwill you'll need later. "
                        "Influence starts with understanding their goals."
                    ),
                },
                {
                    "text": "Send a detailed spec and hope they pick it up.",
                    "score": 2,
                    "feedback": (
                        "A spec is a monologue. Start with a conversation: "
                        "what are they optimizing for this quarter?"
                    ),
                },
                {
                    "text": (
                        "Learn their goals, then frame your ask as helping "
                        "their roadmap."
                    ),
                    "score": 3,
                    "feedback": (
                        "Influence without authority: make your ask their "
                        "win. People support what helps their goals."
                    ),
                },
            ],
        },
        {
            "prompt": (
                "A senior engineer publicly dismisses your proposal. You…"
            ),
            "choices": [
                {
                    "text": "Argue harder in the meeting.",
                    "score": 1,
                    "feedback": (
                        "Public debate becomes status combat. Nobody's mind "
                        "changes when there's an audience."
                    ),
                },
                {
                    "text": "Drop it — not worth the conflict.",
                    "score": 2,
                    "feedback": (
                        "Avoidance feels safe but the idea dies quietly. "
                        "There's a middle path: get curious 1:1."
                    ),
                },
                {
                    "text": (
                        "Ask what concern is underneath, then address it 1:1."
                    ),
                    "score": 3,
                    "feedback": (
                        "Right: separate the person from the position, find "
                        "the real objection, handle it privately."
                    ),
                },
            ],
        },
        {
            "prompt": "Your team is split on technical approach. You…",
            "choices": [
                {
                    "text": "Decide unilaterally to save time.",
                    "score": 1,
                    "feedback": (
                        "Fast, but the losers disengage. A decision people "
                        "don't own is a decision that rots."
                    ),
                },
                {
                    "text": "Let the debate continue until consensus emerges.",
                    "score": 2,
                    "feedback": (
                        "Consensus is slow and often fake. Timebox it, then "
                        "decide."
                    ),
                },
                {
                    "text": "Timebox the debate, decide, and commit publicly to the outcome.",
                    "score": 3,
                    "feedback": (
                        "Disagree-and-commit done right: everyone heard, a "
                        "call was made, the team moves as one."
                    ),
                },
            ],
        },
    ],
    "negotiation": [
        {
            "prompt": (
                "The first offer is 15% below your target. You…"
            ),
            "choices": [
                {
                    "text": "Accept — you're grateful for the offer.",
                    "score": 1,
                    "feedback": (
                        "Gratitude is fine; leaving 15% on the table "
                        "unexamined is expensive. The first offer is an "
                        "opening, not a verdict."
                    ),
                },
                {
                    "text": "Counter at exactly your target number.",
                    "score": 2,
                    "feedback": (
                        "Better than accepting — but anchoring AT your "
                        "target leaves no room to land on it. Anchor above."
                    ),
                },
                {
                    "text": (
                        "Counter above target, backed by market data and "
                        "the specific value you'll bring."
                    ),
                    "score": 3,
                    "feedback": (
                        "Textbook: anchor high with justification. You can "
                        "always come down to your target — you can't go up."
                    ),
                },
            ],
        },
        {
            "prompt": "They say \"this is the top of the band.\" You…",
            "choices": [
                {
                    "text": "Accept the frame — bands are bands.",
                    "score": 1,
                    "feedback": (
                        "Bands have flex: level, signing bonus, equity, "
                        "review timeline. \"Top of the band\" is often a "
                        "negotiating position, not a law of physics."
                    ),
                },
                {
                    "text": "Repeat your number more firmly.",
                    "score": 2,
                    "feedback": (
                        "Firmness without new information just adds "
                        "friction. Probe instead: which band, for what "
                        "level, and what else can move?"
                    ),
                },
                {
                    "text": (
                        "Probe: the band for this level vs. the role, plus "
                        "signing, equity, and level-up path."
                    ),
                    "score": 3,
                    "feedback": (
                        "Right — expand the pie before splitting it. Total "
                        "comp has more levers than base salary."
                    ),
                },
            ],
        },
        {
            "prompt": "\"We need an answer by Friday.\" You…",
            "choices": [
                {
                    "text": "Decide under pressure — deadlines are deadlines.",
                    "score": 1,
                    "feedback": (
                        "Exploding offers are a pressure tactic. A real "
                        "deadline can be discussed; a fake one evaporates "
                        "when you ask what's driving it."
                    ),
                },
                {
                    "text": "Bluff that you have a competing offer.",
                    "score": 2,
                    "feedback": (
                        "Never bluff — it unravels instantly (\"great, take "
                        "it\") and torches trust. Honesty is the stronger "
                        "play."
                    ),
                },
                {
                    "text": (
                        "Acknowledge the timeline, ask what's flexible, and "
                        "refuse to decide scared."
                    ),
                    "score": 3,
                    "feedback": (
                        "Calm and direct: respect their process, protect "
                        "yours. Pressure is information — treat it that way."
                    ),
                },
            ],
        },
    ],
    "active_listening": [
        {
            "prompt": (
                "Interviewer: \"We're worried you'd get bored here.\" You…"
            ),
            "choices": [
                {
                    "text": "\"I won't get bored, I promise!\"",
                    "score": 1,
                    "feedback": (
                        "Dismissing the concern confirms it. The worry is "
                        "retention — address THAT, not the word \"bored\"."
                    ),
                },
                {
                    "text": "List all the exciting projects you'd work on.",
                    "score": 2,
                    "feedback": (
                        "Selling past the concern. First show you heard it: "
                        "reflect it back, then ask what's behind it."
                    ),
                },
                {
                    "text": (
                        "\"Sounds like retention is the real concern — can "
                        "I ask what happened with the last person in this "
                        "role?\""
                    ),
                    "score": 3,
                    "feedback": (
                        "The question behind the question. You heard the "
                        "fear, named it, and invited the real story."
                    ),
                },
            ],
        },
        {
            "prompt": "A teammate vents about the on-call rotation. You…",
            "choices": [
                {
                    "text": "\"Yeah, on-call sucks.\"",
                    "score": 1,
                    "feedback": (
                        "Commiseration without curiosity. They vented — "
                        "reflect it back and find out what \"better\" "
                        "looks like."
                    ),
                },
                {
                    "text": "Immediately propose a fix.",
                    "score": 2,
                    "feedback": (
                        "Solution-first skips understanding. People accept "
                        "fixes to problems they feel heard about."
                    ),
                },
                {
                    "text": (
                        "Reflect what you heard, then ask what \"good\" "
                        "would look like."
                    ),
                    "score": 3,
                    "feedback": (
                        "Listen, reflect, then co-design. That's how venting "
                        "turns into an actionable plan."
                    ),
                },
            ],
        },
        {
            "prompt": (
                "In a negotiation they go quiet after your counter. You…"
            ),
            "choices": [
                {
                    "text": "Fill the silence — soften your number a bit.",
                    "score": 1,
                    "feedback": (
                        "Classic self-negotiation. Silence is thinking, not "
                        "rejection — the first one to talk loses."
                    ),
                },
                {
                    "text": "\"Is that too high?\"",
                    "score": 2,
                    "feedback": (
                        "Better than softening unprompted, but you're still "
                        "negotiating against yourself. Ask an open question "
                        "or wait."
                    ),
                },
                {
                    "text": "Wait. Let them break the silence.",
                    "score": 3,
                    "feedback": (
                        "Discipline. Silence after a justified counter is "
                        "processing time — hold your anchor."
                    ),
                },
            ],
        },
    ],
    "executive_presence": [
        {
            "prompt": (
                "The CTO asks \"why should we hire you?\" — you have 30 "
                "seconds. You…"
            ),
            "choices": [
                {
                    "text": "Give the 5-minute version of your career.",
                    "score": 1,
                    "feedback": (
                        "Thirty seconds means thirty seconds. Rambling "
                        "signals you can't prioritize — fatal at the exec "
                        "level."
                    ),
                },
                {
                    "text": "\"I'm a hard worker and a fast learner.\"",
                    "score": 2,
                    "feedback": (
                        "True of everyone. Executives buy specific value: "
                        "the problem you solve, proof, and what you'll do "
                        "here."
                    ),
                },
                {
                    "text": (
                        "Headline: the problem you solve, one proof point, "
                        "what you'll do in the first 90 days."
                    ),
                    "score": 3,
                    "feedback": (
                        "Executive-grade: problem, proof, plan — in under a "
                        "minute. Leave them wanting more."
                    ),
                },
            ],
        },
        {
            "prompt": "You disagree with the VP's plan in a big meeting. You…",
            "choices": [
                {
                    "text": "Stay silent — not your place.",
                    "score": 1,
                    "feedback": (
                        "Silence when you see risk isn't loyalty, it's "
                        "abdication. There's a respectful way to dissent."
                    ),
                },
                {
                    "text": "Debate them publicly until someone wins.",
                    "score": 2,
                    "feedback": (
                        "Public combat helps no one. Brief, respectful, "
                        "risk-framed — then commit to the call."
                    ),
                },
                {
                    "text": (
                        "\"I see it differently — 60 seconds on the risk I "
                        "see, then I'll commit to your call.\""
                    ),
                    "score": 3,
                    "feedback": (
                        "The disagree-and-commit script: bounded, "
                        "risk-focused, and it ends with loyalty."
                    ),
                },
            ],
        },
        {
            "prompt": "Your live demo crashes in front of everyone. You…",
            "choices": [
                {
                    "text": "Apologize repeatedly while you debug.",
                    "score": 1,
                    "feedback": (
                        "Repeated apologies spotlight the failure. Narrate "
                        "calmly, switch to the backup, debrief later."
                    ),
                },
                {
                    "text": "Blame the wifi / the environment.",
                    "score": 2,
                    "feedback": (
                        "Blame reads as deflection even when it's true. Own "
                        "the room instead: stay calm, keep moving."
                    ),
                },
                {
                    "text": (
                        "Narrate calmly, switch to the backup plan, debrief "
                        "after."
                    ),
                    "score": 3,
                    "feedback": (
                        "Presence under fire: the audience remembers your "
                        "composure longer than the glitch."
                    ),
                },
            ],
        },
    ],
}

_LEVEL_CUT = {"developing": (0, 44), "practicing": (45, 77), "strong": (78, 100)}

_NEXT_STEP = {
    "developing": (
        "Start with one drill rep in this area (coach mode), then read the "
        "feedback carefully before your next rep."
    ),
    "practicing": (
        "Solid foundation. Try adversarial mode — a tough interviewer will "
        "find the gaps polite practice misses."
    ),
    "strong": (
        "Strong. Keep it sharp with an occasional brutal-mode rep, and use "
        "this strength deliberately in real interviews."
    ),
}


def _validate_area(area: str) -> str:
    area = (area or "").strip().lower()
    if area not in SKILL_AREAS:
        raise ValueError(
            f"Unknown skill area {area!r}; choose from: "
            + ", ".join(sorted(SKILL_AREAS))
        )
    return area


def _validate_difficulty(difficulty: str) -> str:
    difficulty = (difficulty or "").strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError(
            f"Unknown difficulty {difficulty!r}; choose from: "
            + ", ".join(DIFFICULTIES)
        )
    return difficulty


def assess(area: str, answers: list[Any]) -> dict[str, Any]:
    """Run the diagnostic for a skill area.

    Args:
        area: one of the SKILL_AREAS keys.
        answers: one choice per question, as "A"/"B"/"C" (case-insensitive)
            or 0/1/2.

    Returns:
        {"area", "level" (developing/practicing/strong), "score_pct",
         "per_question": [{question, choice, score, feedback}], "next_step"}.
    """
    area = _validate_area(area)
    questions = _ASSESS_QUESTIONS[area]
    if len(answers) != len(questions):
        raise ValueError(
            f"Expected {len(questions)} answers for {area}, got {len(answers)}"
        )
    per_question: list[dict[str, Any]] = []
    total = 0
    for i, (q, raw) in enumerate(zip(questions, answers)):
        idx = _choice_index(raw, len(q["choices"]), i)
        choice = q["choices"][idx]
        total += choice["score"]
        per_question.append(
            {
                "question": q["prompt"],
                "choice": choice["text"],
                "score": choice["score"],
                "max": 3,
                "feedback": choice["feedback"],
            }
        )
    pct = round(total / (len(questions) * 3) * 100)
    level = next(
        lvl for lvl, (lo, hi) in _LEVEL_CUT.items() if lo <= pct <= hi
    )
    return {
        "area": area,
        "level": level,
        "score_pct": pct,
        "per_question": per_question,
        "next_step": _NEXT_STEP[level],
    }


def _choice_index(raw: Any, n: int, i: int) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        idx = raw
    elif isinstance(raw, str):
        s = raw.strip().upper()
        mapping = {"A": 0, "B": 1, "C": 2}
        if s not in mapping:
            raise ValueError(f"Answer {i + 1}: use A/B/C or 0/1/2, got {raw!r}")
        idx = mapping[s]
    else:
        raise ValueError(f"Answer {i + 1}: use A/B/C or 0/1/2, got {raw!r}")
    if not 0 <= idx < n:
        raise ValueError(f"Answer {i + 1}: out of range, got {raw!r}")
    return idx


# ---------------------------------------------------------------------------
# Answer analysis (shared by drill review + negotiation)
# ---------------------------------------------------------------------------

_FILLERS = (
    "um", "uh", "like", "you know", "basically", "actually", "sort of",
    "kind of", "literally", "stuff", "things", "very", "really",
)
_APOLOGETIC = ("sorry", "just", "maybe", "i think", "hopefully", "if possible",
               "i guess", "kind of", "sort of")
_AGGRESSIVE = ("demand", "must have", "non-negotiable", "ultimatum",
               "or i walk", "take it or leave it")
_MARKET_SIGNALS = ("market", "levels.fyi", "payscale", "glassdoor", "benchmark",
                   "range", "percentile", "data")
_EVIDENCE_SIGNALS = ("led", "built", "shipped", "launched", "saved", "grew",
                     "reduced", "increased", "drove", "delivered")

_STAR_KEYWORDS = {
    "situation": ("when", "while", "background", "context", "situation",
                  "at the time"),
    "task": ("responsible", "tasked", "goal", "needed to", "had to",
             "my role", "objective"),
    "action": ("i led", "i built", "i decided", "i implemented", "i drove",
               "i shipped", "i proposed", "i convinced", "i designed"),
    "result": ("result", "outcome", "increased", "decreased", "reduced",
               "grew", "saved", "shipped", "launched", "as a result"),
}
_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?%?")
_I_RE = re.compile(r"\bi(?:'m|'ve|'d|'ll)?\b|\bmy\b", re.I)
_WE_RE = re.compile(r"\bwe(?:'ve|'d|'ll|'re)?\b|\bour\b", re.I)


def _analyze_answer(answer: str) -> dict[str, Any]:
    """Deterministic answer analysis. Describes the answer, never the person."""
    text = (answer or "").strip()
    words = re.findall(r"[A-Za-z']+", text.lower())
    word_count = len(words)
    filler_hits = [f for f in _FILLERS if f in text.lower()]
    numbers = _NUMBER_RE.findall(text)
    star: dict[str, dict[str, Any]] = {}
    for part, kws in _STAR_KEYWORDS.items():
        hit = next((k for k in kws if k in text.lower()), None)
        star[part] = {"present": hit is not None, "evidence": hit}
    i_count = len(_I_RE.findall(text))
    we_count = len(_WE_RE.findall(text))
    tone = "neutral"
    low = text.lower()
    if any(t in low for t in _APOLOGETIC):
        tone = "apologetic"
    if any(t in low for t in _AGGRESSIVE):
        tone = "aggressive"
    market = any(s in low for s in _MARKET_SIGNALS)
    evidence = any(s in low for s in _EVIDENCE_SIGNALS) or bool(numbers)
    return {
        "word_count": word_count,
        "filler_hits": filler_hits,
        "filler_count": len(filler_hits),
        "has_numbers": bool(numbers),
        "numbers": numbers[:5],
        "star": star,
        "star_parts_present": sum(1 for v in star.values() if v["present"]),
        "i_statements": i_count,
        "we_statements": we_count,
        "tone": tone,
        "market_signals": market,
        "evidence_signals": evidence,
    }


def _score_drill_answer(a: dict[str, Any]) -> tuple[int, list[str]]:
    """Score 0-100 + feedback lines. Feedback describes the answer only."""
    notes: list[str] = []
    score = 0

    # STAR completeness: 40 pts (10 per part).
    star_pts = a["star_parts_present"] * 10
    score += star_pts
    missing = [p for p, v in a["star"].items() if not v["present"]]
    if missing:
        notes.append(
            "STAR check: missing " + ", ".join(missing) + ". "
            "Name each part explicitly — interviewers listen for it."
        )
    else:
        notes.append("STAR check: all four parts present. Well structured.")

    # Specificity: 20 pts.
    if a["has_numbers"]:
        score += 20
        notes.append(
            f"Specificity: numbers present ({', '.join(a['numbers'])}). "
            "Concrete beats abstract."
        )
    else:
        notes.append(
            "Specificity: no numbers or measurable outcomes. Add the "
            "closest honest figure — even directional."
        )

    # Clarity: 20 pts (filler penalty + length sanity).
    clarity = 20
    if a["filler_count"]:
        clarity -= min(12, a["filler_count"] * 4)
        notes.append(
            f"Filler words: {a['filler_count']} "
            f"({', '.join(a['filler_hits'])}). Cut them; they dilute "
            "authority."
        )
    wc = a["word_count"]
    if wc < 40:
        clarity -= 8
        notes.append(
            f"Length: {wc} words is thin for a behavioral answer — you're "
            "likely skipping the setup or the result."
        )
    elif wc > 350:
        clarity -= 8
        notes.append(
            f"Length: {wc} words risks rambling. Aim for 90–250: setup, "
            "your actions, outcome."
        )
    else:
        notes.append(f"Length: {wc} words — in a good range.")
    score += max(0, clarity)

    # Ownership: 20 pts.
    if a["i_statements"] >= 3 and a["i_statements"] >= a["we_statements"]:
        score += 20
        notes.append("Ownership: clear 'I' statements — your contribution is visible.")
    elif a["we_statements"] > a["i_statements"]:
        score += 8
        notes.append(
            "Ownership: more 'we' than 'I'. Credit the team, then name YOUR "
            "specific actions."
        )
    else:
        score += 12
        notes.append("Ownership: okay, but make your personal actions unmistakable.")

    if a["tone"] == "apologetic":
        score = max(0, score - 5)
        notes.append("Tone: reads apologetic — state the facts without hedging.")
    elif a["tone"] == "aggressive":
        score = max(0, score - 5)
        notes.append("Tone: reads combative — firm is good, sharp edges aren't.")

    return max(0, min(100, score)), notes


# ---------------------------------------------------------------------------
# Drills
# ---------------------------------------------------------------------------

def _profile_anchor(profile: dict[str, Any]) -> dict[str, str]:
    """Most recent experience entry (or target title) for tailored prompts.

    Only real profile data is used — nothing is invented.
    """
    exp = (profile or {}).get("experience") or []
    if exp and isinstance(exp[0], dict):
        e = exp[0]
        return {
            "title": str(e.get("title") or "your role"),
            "company": str(e.get("company") or "your company"),
        }
    targets = (profile or {}).get("target_titles") or []
    title = str(targets[0]) if targets else "the role you're targeting"
    return {"title": title, "company": "your target company"}


def _coach_prompt(area: str, anchor: dict[str, str]) -> str:
    t, c = anchor["title"], anchor["company"]
    prompts = {
        "communication_clarity": (
            f"Explain what you do as {t} to a smart non-technical "
            "stakeholder in 60 seconds. No jargon — bottom line first."
        ),
        "star_storytelling": (
            f"Tell me about a project at {c} you're proud of. Structure "
            "it as Situation → Task → Action → Result, with numbers."
        ),
        "leadership_influence": (
            f"Tell me about a time you changed someone's mind at {c} "
            "without having authority over them."
        ),
        "negotiation": (
            "You've been offered a number 10% below your target. Make "
            "your counter-case in three sentences: anchor, justification, "
            "warm close."
        ),
        "active_listening": (
            "Your interviewer says: 'We're worried you'd get bored here.' "
            "Respond — but first show you heard the concern underneath."
        ),
        "executive_presence": (
            f"You have two minutes with the CTO in an elevator. Pitch why "
            f"they should hire you as {t}: problem you solve, proof, plan."
        ),
    }
    return prompts[area]


_ADVERSARY_OPENERS: dict[str, dict[str, str]] = {
    "firm": {
        "communication_clarity": (
            "Explain your current role to me like I'm a smart outsider. "
            "I'll stop you if you hide behind jargon — go."
        ),
        "star_storytelling": (
            "Give me your best STAR story. And I want specifics, not the "
            "polished version — go."
        ),
        "leadership_influence": (
            "Tell me about a time you influenced a decision without "
            "authority. I'll be asking what YOU actually did — go."
        ),
        "negotiation": (
            "Make your case for the salary you want. I'll push back like "
            "a real hiring manager would — go."
        ),
        "active_listening": (
            "Here's the scenario: I just told you I'm worried you'd get "
            "bored here. Respond. I'm listening for whether you actually "
            "heard me — go."
        ),
        "executive_presence": (
            "Thirty seconds: why should I hire you? The clock is running "
            "— go."
        ),
    },
    "hardball": {
        "communication_clarity": (
            "Explain what you do. No jargon, no rambling — if I can't "
            "repeat it back, you fail the round. Go."
        ),
        "star_storytelling": (
            "Your best STAR story. I've heard a thousand of these, so "
            "skip the gloss and give me the real mechanics. Go."
        ),
        "leadership_influence": (
            "Influence without authority — everyone claims it. Prove it "
            "with one story, and I'll be checking whose actions those "
            "really were. Go."
        ),
        "negotiation": (
            "Pitch me your number. I will find the weak spots in it. Go."
        ),
        "active_listening": (
            "'I'm worried you'd get bored here.' Most candidates blow "
            "this in ten seconds. Don't. Go."
        ),
        "executive_presence": (
            "Thirty seconds, why you. If you waste the first ten on "
            "pleasantries, we're done. Go."
        ),
    },
    "brutal": {
        "communication_clarity": (
            "Explain your role so a smart teenager gets it. Jargon, and "
            "I cut you off. Go."
        ),
        "star_storytelling": (
            "One STAR story. I will interrogate every claim in it. If any "
            "part is soft, I'll find it. Go."
        ),
        "leadership_influence": (
            "Influence without authority. I'm going to assume you just "
            "got lucky until you prove otherwise. Go."
        ),
        "negotiation": (
            "Give me your number and your best justification. Then I'll "
            "dismantle it, professionally. Go."
        ),
        "active_listening": (
            "'You'd get bored here.' Answer wrong and I mentally check "
            "out — just like a real interviewer would. Go."
        ),
        "executive_presence": (
            "Thirty seconds. No filler, no throat-clearing, no life story. "
            "Go."
        ),
    },
}


def _adversary_followup(
    a: dict[str, Any], difficulty: str, round_no: int
) -> str:
    """Pick the adversary's next in-character challenge from the analysis.

    Attacks the argument (vagueness, missing ownership, no outcome) —
    never the person.
    """
    prefix = {
        "firm": "Let me push on that a bit: ",
        "hardball": "I'm going to press you here: ",
        "brutal": "I'm going to be blunt: ",
    }[difficulty]

    if not a["has_numbers"]:
        body = (
            "that sounds vague — what exactly did YOU do versus the team? "
            "Give me numbers."
        )
    elif a["we_statements"] > a["i_statements"]:
        body = (
            "I'm hearing a lot of 'we'. What was YOUR contribution, "
            "specifically?"
        )
    elif a["word_count"] < 40:
        body = (
            "hold on — you're glossing over the hard part. Slow down and "
            "walk me through the actual moment, step by step."
        )
    elif not a["star"]["result"]["present"]:
        body = (
            "and the outcome was…? You've given me setup without a "
            "punchline. What changed because of what you did?"
        )
    elif a["filler_count"] >= 3:
        body = (
            "strip the filler and give me the headline first. What "
            "happened, in one sentence?"
        )
    elif round_no >= 2 and difficulty in ("hardball", "brutal"):
        body = (
            "devil's advocate: that sounds like table stakes for the role. "
            "What made YOUR approach different from anyone else's?"
        )
    else:
        body = (
            "okay — now steelman the opposite view. What's the strongest "
            "argument AGAINST how you handled it?"
        )
    return prefix + body


def _new_session_id() -> str:
    return "ss_" + uuid.uuid4().hex[:10]


def _load_sessions() -> dict[str, Any]:
    try:
        data = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_sessions(sessions: dict[str, Any]) -> None:
    tmp = SESSIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    tmp.replace(SESSIONS_PATH)


def _load_profile() -> dict[str, Any]:
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def drill(
    area: str,
    profile: dict[str, Any] | None = None,
    mode: str = "coach",
    difficulty: str = "firm",
) -> dict[str, Any]:
    """Start a practice drill.

    Args:
        area: one of the SKILL_AREAS keys.
        profile: candidate profile (defaults to the saved onboarding
            profile). Only real experience is used in prompts.
        mode: "coach" (single friendly rep) or "adversarial" (3-round
            tough interviewer).
        difficulty: "firm" | "hardball" | "brutal" (adversarial mode).

    Returns:
        {"session_id", "area", "mode", "difficulty", "round", "prompt",
         "rounds_total"} — answer with review_drill().
    """
    area = _validate_area(area)
    mode = (mode or "coach").strip().lower()
    if mode not in DRILL_MODES:
        raise ValueError(f"Unknown drill mode {mode!r}; use coach|adversarial")
    difficulty = _validate_difficulty(difficulty)
    prof = dict(profile) if profile is not None else _load_profile()
    anchor = _profile_anchor(prof)

    session_id = _new_session_id()
    if mode == "coach":
        prompt = _coach_prompt(area, anchor)
        rounds_total = 1
    else:
        prompt = _ADVERSARY_OPENERS[difficulty][area]
        rounds_total = 3

    sessions = _load_sessions()
    sessions[session_id] = {
        "type": "drill",
        "area": area,
        "mode": mode,
        "difficulty": difficulty,
        "round": 1,
        "rounds_total": rounds_total,
        "prompt": prompt,
        "history": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "done": False,
    }
    _save_sessions(sessions)
    log.info("drill started: %s area=%s mode=%s", session_id, area, mode)
    return {
        "session_id": session_id,
        "area": area,
        "mode": mode,
        "difficulty": difficulty,
        "round": 1,
        "rounds_total": rounds_total,
        "prompt": prompt,
    }


def review_drill(session_id: str, answer: str) -> dict[str, Any]:
    """Review a drill answer with structured feedback.

    In adversarial mode, returns labeled COACH feedback plus the next
    in-character adversary challenge (until 3 rounds are done, then a
    final debrief). In coach mode, one review closes the session.
    """
    sessions = _load_sessions()
    session = sessions.get(session_id)
    if not session or session.get("type") != "drill":
        raise ValueError(f"Unknown drill session {session_id!r}")
    if session.get("done"):
        raise ValueError(f"Session {session_id!r} is already complete")

    a = _analyze_answer(answer)
    score, notes = _score_drill_answer(a)
    session["history"].append(
        {"round": session["round"], "answer": answer, "score": score}
    )

    coach = {
        "score": score,
        "analysis": {
            "word_count": a["word_count"],
            "filler_count": a["filler_count"],
            "has_numbers": a["has_numbers"],
            "star_parts_present": a["star_parts_present"],
            "tone": a["tone"],
        },
        "feedback": notes,
    }

    if session["mode"] == "coach" or session["round"] >= session["rounds_total"]:
        session["done"] = True
        _save_sessions(sessions)
        log_practice(session["area"], score, kind="drill-" + session["mode"])
        debrief = _drill_debrief(session)
        return {
            "session_id": session_id,
            "done": True,
            "coach": coach,
            "debrief": debrief,
        }

    session["round"] += 1
    next_prompt = _adversary_followup(a, session["difficulty"], session["round"])
    session["prompt"] = next_prompt
    _save_sessions(sessions)
    return {
        "session_id": session_id,
        "done": False,
        "round": session["round"],
        "rounds_total": session["rounds_total"],
        "coach": coach,
        "adversary": next_prompt,
    }


def _drill_debrief(session: dict[str, Any]) -> dict[str, Any]:
    scores = [h["score"] for h in session["history"]]
    avg = round(sum(scores) / len(scores)) if scores else 0
    trend = "steady"
    if len(scores) >= 2:
        if scores[-1] - scores[0] >= 10:
            trend = "warming up — later rounds scored higher"
        elif scores[0] - scores[-1] >= 10:
            trend = "fading — earlier rounds were stronger; watch fatigue"
    return {
        "rounds": len(scores),
        "scores": scores,
        "average": avg,
        "trend": trend,
        "note": (
            "Reps logged as practice. Take the single weakest feedback "
            "line above into your next real interview."
        ),
    }


# ---------------------------------------------------------------------------
# Negotiation sim (adversarial hiring manager)
# ---------------------------------------------------------------------------

_TACTIC_EXPLAINERS = {
    "low_anchor": (
        "Low anchor: they opened below your target to drag the midpoint "
        "down. The first number sets the gravity — never accept its frame."
    ),
    "budget_pushback": (
        "Budget-constraint pushback: 'the budget is locked' is usually a "
        "negotiating position, not a law of physics. Bands flex via level, "
        "signing bonus, equity, and review timelines."
    ),
    "competing_candidate": (
        "Competing-candidate pressure: invoking a rival creates urgency and "
        "makes you negotiate against a phantom. Respond to the substance, "
        "not the ghost."
    ),
    "exploding_offer": (
        "Exploding offer / time pressure: deadlines force emotional "
        "decisions. A real deadline survives the question 'what's driving "
        "it?' — a fake one evaporates."
    ),
    "justification_teardown": (
        "Justification teardown: they attacked your reasons, not your "
        "number. That means the number is defensible — rebuild the case "
        "with specifics they can't wave away."
    ),
    "trial_close": (
        "Trial close: a small concession paired with 'can we wrap this "
        "up?' They're testing whether you'll stop pushing. If the gap "
        "still matters, keep going — politely."
    ),
    "take_it_or_leave_it": (
        "Take-it-or-leave-it: the final pressure move. It's only real if "
        "you believe they'd rather lose you than pay. Call it calmly or "
        "take the win — never decide scared."
    ),
}


def _money(n: float) -> str:
    return f"${n:,.0f}"


def _negotiation_opener(difficulty: str, offer: float) -> tuple[str, str]:
    if difficulty == "firm":
        return (
            "low_anchor",
            f"Thanks for your time today — we're excited about you. The "
            f"offer is {_money(offer)} base. That's the top of the band for "
            f"this level, and frankly the budget for this headcount is "
            f"locked. What do you think?",
        )
    if difficulty == "hardball":
        return (
            "low_anchor",
            f"Good news: we want to move forward at {_money(offer)} base. "
            f"I'll be straight with you — we have another strong candidate "
            f"who would sign at this number tomorrow. Convince me why we "
            f"should stretch for you.",
        )
    return (
        "exploding_offer",
        f"We're prepared to offer {_money(offer)}. Take it or leave it — "
        f"this explodes Friday at 5pm, and after that we move to our backup "
        f"candidate. What's it going to be?",
    )


def _adversary_negotiation_reply(
    session: dict[str, Any],
    counter: float,
    message: str,
    q: float,
    a: dict[str, Any],
) -> tuple[str, str, str]:
    """Return (tactic, in-character reply, coach_try_next)."""
    difficulty = session["difficulty"]
    rnd = session["round"]
    pos = session["pos"]
    no_case = not (a["market_signals"] or a["evidence_signals"]) and len(message.split()) < 12

    if rnd == 1:
        if no_case:
            tactic = "budget_pushback"
            if difficulty == "firm":
                reply = (
                    f"I hear the number, but I can't take a number to comp "
                    f"without a reason. What's driving it?"
                )
            elif difficulty == "hardball":
                reply = (
                    f"A number without a reason is just a wish. Give me "
                    f"something I can defend upstairs."
                )
            else:
                reply = "No rationale, no movement. Try again."
            tip = ("Bring one market data point and one specific result. "
                   "'I want more' is not a case.")
        else:
            tactic = "justification_teardown"
            if difficulty == "firm":
                reply = (
                    f"I appreciate the specifics — genuinely. Here's my "
                    f"problem: the band exists for a reason. Let me see "
                    f"what I can do… I can move to {_money(pos)}."
                )
            elif difficulty == "hardball":
                reply = (
                    f"Market data cuts both ways — our band IS the market "
                    f"for this scope, and strong results are expected at "
                    f"this level, not a premium. Best I can do right now "
                    f"is {_money(pos)}."
                )
            else:
                reply = (
                    f"Everyone has numbers. That doesn't change our "
                    f"budget. {_money(pos)}, and Friday still stands."
                )
            tip = ("They attacked the justification, not the number — your "
                   "anchor held. Reload with scope: what will you own in "
                   "the first 90 days?")
    elif rnd == 2:
        if difficulty == "firm":
            tactic = "trial_close"
            reply = (
                f"I went back to comp — {_money(pos)} plus a "
                f"$5k signing bonus. That's genuinely my best. Can we wrap "
                f"this up and get you started?"
            )
            tip = ("Trial close: small sweetener + 'can we wrap up?' If the "
                   "gap still matters to you, say so warmly and hold.")
        elif difficulty == "hardball":
            tactic = "competing_candidate"
            reply = (
                f"I need to be honest: the other candidate just told us "
                f"they're ready to sign. {_money(pos)} is where I am, and "
                f"I can't hold this headcount past this week."
            )
            tip = ("Phantom rival + deadline. Don't negotiate against the "
                   "ghost — restate your value and ask what closes the gap.")
        else:
            tactic = "take_it_or_leave_it"
            reply = (
                f"We're {_money(counter - pos)} apart and I'm not sure this "
                f"is going to work. {_money(pos)}, Friday, final answer — "
                f"or we move on. Your call."
            )
            tip = ("Walk-away framing. Stay calm, don't flinch: 'I want "
                   "this to work — help me understand what's flexible "
                   "besides base.'")
    else:
        tactic = "take_it_or_leave_it"
        if difficulty == "firm":
            reply = (
                f"Final offer: {_money(pos)} base, $5k signing, and I'll "
                f"put a 6-month compensation review in writing. I can't go "
                f"further — but I want you here."
            )
        elif difficulty == "hardball":
            reply = (
                f"Final: {_money(pos)}. The other candidate signs Monday if "
                f"you pass. I hope you take it."
            )
        else:
            reply = (
                f"{_money(pos)}. Yes or no, right now. Clock's ticking."
            )
        tip = ("Decide on the merits, not the pressure. If the number "
               "works, take it warmly. If not, walk away kindly.")
    return tactic, reply, tip


def negotiation_sim(
    current_offer: float,
    target: float,
    profile: dict[str, Any] | None = None,
    difficulty: str = "firm",
) -> dict[str, Any]:
    """Start a 3-round salary negotiation against an adversarial manager.

    Args:
        current_offer: the company's offer (annual base).
        target: the number you want.
        profile: unused for adversary lines, kept for API symmetry.
        difficulty: "firm" | "hardball" | "brutal".

    Returns:
        {"session_id", "round", "rounds_total", "adversary", "tactic",
         "coach_note"} — counter with negotiation_round().
    """
    difficulty = _validate_difficulty(difficulty)
    for name, val in (("current_offer", current_offer), ("target", target)):
        if not isinstance(val, (int, float)) or val <= 0:
            raise ValueError(f"{name} must be a positive number, got {val!r}")

    tactic, opener = _negotiation_opener(difficulty, float(current_offer))
    session_id = _new_session_id()
    sessions = _load_sessions()
    sessions[session_id] = {
        "type": "negotiation",
        "difficulty": difficulty,
        "offer": float(current_offer),
        "target": float(target),
        "pos": float(current_offer),
        "round": 1,
        "rounds_total": 3,
        "history": [{"role": "adversary", "tactic": tactic, "text": opener}],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "done": False,
    }
    _save_sessions(sessions)
    log.info("negotiation started: %s difficulty=%s", session_id, difficulty)
    return {
        "session_id": session_id,
        "round": 1,
        "rounds_total": 3,
        "difficulty": difficulty,
        "adversary": opener,
        "tactic": tactic,
        "coach_note": (
            "[COACH] Their move: " + _TACTIC_EXPLAINERS[tactic] + " Your "
            "opening counter should anchor ABOVE your target with one "
            "market data point and one specific result — and stay warm."
        ),
    }


def _concession(session: dict[str, Any], q: float) -> float:
    gap = max(0.0, session["target"] - session["pos"])
    rate = {"firm": 0.25 + 0.35 * q, "hardball": 0.10 + 0.25 * q,
            "brutal": 0.05 + 0.10 * q}[session["difficulty"]]
    return gap * rate


def negotiation_round(
    session_id: str, counter: float, message: str
) -> dict[str, Any]:
    """Play one negotiation round: your counter vs. the adversary.

    Between rounds the sim breaks character with labeled COACH feedback:
    what the adversary's move was, how your counter landed (anchoring,
    justification, tone), and what to try next.
    """
    sessions = _load_sessions()
    session = sessions.get(session_id)
    if not session or session.get("type") != "negotiation":
        raise ValueError(f"Unknown negotiation session {session_id!r}")
    if session.get("done"):
        raise ValueError(f"Session {session_id!r} is already complete")
    if not isinstance(counter, (int, float)) or counter <= 0:
        raise ValueError(f"counter must be a positive number, got {counter!r}")

    counter = float(counter)
    a = _analyze_answer(message or "")
    target = session["target"]

    # --- assess the user's counter ---
    blunders: list[str] = []
    if counter <= session["pos"]:
        blunders.append(
            "You bid against yourself: your counter is at or below their "
            "current position. Never move their direction for them."
        )
    if counter > target * 1.25:
        blunders.append(
            "Your anchor is more than 25% above your own target — "
            "unrealistic anchors hurt credibility."
        )
    anchor_note = (
        f"You anchored at {_money(counter)} vs. your {_money(target)} target."
        + (
            " Good — room to land on your target."
            if target < counter <= target * 1.15
            else " Consider anchoring 5–15% above target: room to concede."
        )
    )
    q = 0.0
    just_parts: list[str] = []
    if a["market_signals"]:
        q += 0.35
        just_parts.append("market data cited")
    if a["evidence_signals"]:
        q += 0.35
        just_parts.append("specific results/numbers cited")
    if len((message or "").split()) >= 25:
        q += 0.15
        just_parts.append("substantive case made")
    if a["tone"] == "neutral":
        q += 0.15
    just_note = (
        "Justification: " + (", ".join(just_parts) if just_parts else "none detected")
        + "."
    )
    tone_note = {
        "neutral": "Tone: professional and steady.",
        "apologetic": (
            "Tone: apologetic — hedging ('just', 'maybe', 'sorry') invites "
            "them to discount your number. State it plainly."
        ),
        "aggressive": (
            "Tone: combative — pressure invites counter-pressure. Stay "
            "warm and immovable instead."
        ),
    }[a["tone"]]
    q = min(1.0, q)

    session["history"].append(
        {"role": "user", "counter": counter, "text": message,
         "justification_q": round(q, 2)}
    )

    # --- adversary moves ---
    move = _concession(session, q)
    session["pos"] = min(target, session["pos"] + move)
    tactic, reply, tip = _adversary_negotiation_reply(
        session, counter, message, q, a
    )
    session["history"].append({"role": "adversary", "tactic": tactic, "text": reply})

    coach = {
        "adversary_move": tactic,
        "adversary_move_explained": _TACTIC_EXPLAINERS[tactic],
        "your_counter": [anchor_note, just_note, tone_note] + blunders,
        "try_next": tip,
    }

    done = session["round"] >= session["rounds_total"]
    result: dict[str, Any] = {
        "session_id": session_id,
        "done": done,
        "round": session["round"],
        "rounds_total": session["rounds_total"],
        "their_position": round(session["pos"]),
        "adversary": reply,
        "coach": coach,
    }

    if done:
        session["done"] = True
        summary = _negotiation_summary(session)
        result["final_offer"] = round(session["pos"])
        result["summary"] = summary
        log_practice("negotiation", summary["score"], kind="negotiation-" + session["difficulty"])
    else:
        session["round"] += 1

    _save_sessions(sessions)
    return result


def _negotiation_summary(session: dict[str, Any]) -> dict[str, Any]:
    user_moves = [h for h in session["history"] if h["role"] == "user"]
    qs = [h.get("justification_q", 0) for h in user_moves]
    avg_q = sum(qs) / len(qs) if qs else 0
    gained = session["pos"] - session["offer"]
    gap = max(1.0, session["target"] - session["offer"])
    # Score: justification quality 50 + value captured 50.
    score = round(min(1.0, avg_q) * 50 + min(1.0, gained / gap) * 50)
    lessons = []
    if avg_q < 0.5:
        lessons.append(
            "Lead every counter with evidence: one market data point, one "
            "specific result. Numbers move numbers."
        )
    if gained / gap < 0.3:
        lessons.append(
            "You captured little of the gap — hold your anchor longer and "
            "make them bid against themselves."
        )
    if not lessons:
        lessons.append(
            "Strong round: justified counters, steady tone, real movement. "
            "Take this discipline into the real thing."
        )
    return {
        "rounds": len(user_moves),
        "difficulty": session["difficulty"],
        "opened_at": round(session["offer"]),
        "closed_at": round(session["pos"]),
        "target": round(session["target"]),
        "gained": round(gained),
        "score": max(0, min(100, score)),
        "lessons": lessons,
        "note": "Practice rep only — not a credential. The real table is easier when you've sat at this one.",
    }


# ---------------------------------------------------------------------------
# Practice log + progress
# ---------------------------------------------------------------------------

def _load_progress() -> dict[str, Any]:
    try:
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reps": []}
    if not isinstance(data, dict) or not isinstance(data.get("reps"), list):
        return {"reps": []}
    return data


def _save_progress(data: dict[str, Any]) -> None:
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def log_practice(
    area: str, score: float, kind: str = "drill"
) -> dict[str, Any]:
    """Log a practice rep. Reps are practice — never credentials.

    Args:
        area: one of the SKILL_AREAS keys.
        score: 0-100 for the rep.
        kind: free label, e.g. "drill-coach", "negotiation-hardball".
    """
    area = _validate_area(area)
    if not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError(f"score must be 0-100, got {score!r}")
    data = _load_progress()
    rep = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "area": area,
        "score": round(float(score)),
        "kind": str(kind),
    }
    data["reps"].append(rep)
    _save_progress(data)
    log.info("practice logged: %s %s", area, rep["score"])
    return {"logged": True, "rep": rep, "total_reps": len(data["reps"])}


def progress() -> dict[str, Any]:
    """Per-area practice reps, averages, and trends."""
    reps = _load_progress()["reps"]
    by_area: dict[str, list[dict[str, Any]]] = {}
    for r in reps:
        by_area.setdefault(r["area"], []).append(r)
    areas: dict[str, Any] = {}
    for area in sorted(SKILL_AREAS):
        rs = by_area.get(area, [])
        scores = [r["score"] for r in rs]
        if not scores:
            areas[area] = {"reps": 0, "average": None, "best": None,
                           "trend": "not started"}
            continue
        trend = "warming up"
        if len(scores) >= 4:
            early = sum(scores[:2]) / 2
            late = sum(scores[-2:]) / 2
            if late - early >= 5:
                trend = "improving"
            elif early - late >= 5:
                trend = "declining"
            else:
                trend = "steady"
        areas[area] = {
            "reps": len(scores),
            "average": round(sum(scores) / len(scores)),
            "best": max(scores),
            "trend": trend,
            "last_kind": rs[-1].get("kind"),
        }
    return {
        "areas": areas,
        "total_reps": len(reps),
        "note": "Practice reps only — not certifications or credentials.",
    }


# ---------------------------------------------------------------------------
# Initiative 06 — soft-skill lab: conflict, influence, negotiation,
# ambiguous-stakeholder scenarios
# ---------------------------------------------------------------------------
#
# ``scenario_kinds()`` lists the four disclosed scenarios.
# ``start_scenario(kind)`` opens a 3-round session (briefing card first).
# ``review_scenario(session_id, answer)`` scores each round against the
# disclosed rubric plus scenario-specific behavior signals.
# ``scenario_summary(session_id)`` reports per-round results.
# Completed scenarios are recorded to the longitudinal practice history
# (best-effort; answering never breaks if the store is unavailable).

#: Lab scenario sessions (separate from drill sessions).
LAB_SESSIONS_PATH = BASE_DIR / "soft_skill_lab_sessions.json"

#: Tests redirect the lab session store here; None = default path.
_LAB_SESSIONS_PATH: Path | None = None

#: Tests redirect the longitudinal history here; None = default path.
_LAB_LONGITUDINAL_HISTORY_PATH: Path | None = None


def _load_lab_sessions() -> dict[str, Any]:
    path = _LAB_SESSIONS_PATH or LAB_SESSIONS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_lab_sessions(sessions: dict[str, Any]) -> None:
    path = _LAB_SESSIONS_PATH or LAB_SESSIONS_PATH
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
    tmp.replace(path)


def scenario_kinds() -> dict[str, Any]:
    """List the four lab scenarios with their disclosed briefing cards."""
    return {
        "kinds": [
            {"kind": kind,
             "title": _i06_scenarios.SCENARIOS[kind]["title"],
             "card": _i06_scenarios.scenario_card(kind)}
            for kind in _i06_scenarios.SCENARIO_KINDS
        ]
    }


def start_scenario(kind: str) -> dict[str, Any]:
    """Start a 3-round soft-skill lab scenario.

    Args:
        kind: "conflict" | "influence" | "negotiation" |
            "ambiguous_stakeholder".

    Returns {"session_id", "kind", "title", "card", "round",
    "rounds_total", "prompt"} — answer each round with review_scenario.
    """
    kind = _i06_scenarios.validate_kind(kind)
    session_id = "lab_" + uuid.uuid4().hex[:10]
    first = _i06_scenarios.SCENARIOS[kind]["rounds"][0]["prompt"]
    sessions = _load_lab_sessions()
    sessions[session_id] = {
        "type": "lab_scenario",
        "session_id": session_id,
        "kind": kind,
        "round": 0,
        "rounds_total": len(_i06_scenarios.SCENARIOS[kind]["rounds"]),
        "results": [],
        "done": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_lab_sessions(sessions)
    return {
        "session_id": session_id,
        "kind": kind,
        "title": _i06_scenarios.SCENARIOS[kind]["title"],
        "card": _i06_scenarios.scenario_card(kind),
        "round": 1,
        "rounds_total": len(_i06_scenarios.SCENARIOS[kind]["rounds"]),
        "prompt": first,
    }


def review_scenario(
    session_id: str,
    answer: str,
    input_modality: str = "text",
) -> dict[str, Any]:
    """Score one scenario round.

    Args:
        session_id: from start_scenario.
        answer: the candidate's response text.
        input_modality: "text" (default) or "voice" — recorded per
            answer for the privacy ledger.

    Returns the round evaluation (rubric scores + behavior signals);
    when the final round is done, includes a debrief and records the
    scenario to the longitudinal practice history.
    """
    sessions = _load_lab_sessions()
    session = sessions.get(session_id)
    if session is None or session.get("type") != "lab_scenario":
        raise ValueError(f"No lab scenario session {session_id!r}")
    if session.get("done"):
        raise ValueError(f"Scenario session {session_id!r} is complete")

    kind = session["kind"]
    round_index = session["round"]
    evaluation = _i06_scenarios.evaluate_round(kind, round_index, answer)
    session["results"].append({
        "round": round_index + 1,
        "answer": str(answer or ""),
        "input_modality": input_modality or "text",
        **evaluation,
    })
    session["round"] = round_index + 1
    session["done"] = session["round"] >= session["rounds_total"]
    _save_lab_sessions(sessions)

    out: dict[str, Any] = {
        "session_id": session_id,
        "kind": kind,
        "round": round_index + 1,
        "rounds_total": session["rounds_total"],
        "passed": evaluation["passed"],
        "rubric": evaluation["rubric"],
        "signals": evaluation["signals"],
        "feedback": evaluation["feedback"],
        "input_modality": input_modality or "text",
        "done": session["done"],
    }
    if session["done"]:
        out["debrief"] = _lab_debrief(session)
        _record_lab_result(session)
    else:
        nxt = _i06_scenarios.SCENARIOS[kind]["rounds"][session["round"]]
        out["next_prompt"] = nxt["prompt"]
        out["next_round"] = session["round"] + 1
    return out


def _lab_debrief(session: dict[str, Any]) -> dict[str, Any]:
    """Final debrief: per-round pass/fail, rubric averages, next rep."""
    results = session["results"]
    dims: dict[str, list[int]] = {}
    for r in results:
        for d, s in r["rubric"]["dimension_scores"].items():
            if s is not None:
                dims.setdefault(d, []).append(s)
    averages = {d: round(sum(v) / len(v)) for d, v in dims.items() if v}
    missed = sorted({name for r in results for name, s
                     in r["signals"].items() if not s["passed"]})
    kind = session["kind"]
    title = _i06_scenarios.SCENARIOS[kind]["title"]
    if not missed:
        next_rep = (f"Clean run on '{title}'. Re-run it in a harder "
                    "frame: halve your words each round.")
    else:
        readable = ", ".join(n.replace("_", " ") for n in missed)
        next_rep = (f"Re-run '{title}' focusing on: {readable}. One "
                    "round at a time, out loud.")
    return {
        "rounds_passed": sum(1 for r in results if r["passed"]),
        "rounds_total": len(results),
        "rubric_averages": averages,
        "signals_missed": missed,
        "next_rep": next_rep,
    }


def _record_lab_result(session: dict[str, Any]) -> None:
    """Best-effort longitudinal record; never raises."""
    try:
        dims: dict[str, list[int]] = {}
        overalls: list[int] = []
        for r in session["results"]:
            for d, s in r["rubric"]["dimension_scores"].items():
                if s is not None:
                    dims.setdefault(d, []).append(s)
            if r["rubric"].get("overall") is not None:
                overalls.append(r["rubric"]["overall"])
        dimension_scores: dict[str, int | None] = {
            d: round(sum(v) / len(v)) for d, v in dims.items() if v
        }
        for d in _i06_rubric.DIMENSIONS:
            dimension_scores.setdefault(d, None)
        _i06_longitudinal.record_result(
            source="soft_skill_scenario",
            label=_i06_scenarios.SCENARIOS[session["kind"]]["title"],
            dimension_scores=dimension_scores,
            overall=(round(sum(overalls) / len(overalls)) if overalls
                     else None),
            mode=session["kind"],
            session_id=session.get("session_id", ""),
            history_path=_LAB_LONGITUDINAL_HISTORY_PATH,
        )
    except Exception as exc:  # noqa: BLE001 - history is best-effort
        log.warning("lab longitudinal record failed: %s", exc)


def scenario_summary(session_id: str) -> dict[str, Any]:
    """Summarize a lab scenario session: per-round results + debrief."""
    session = _load_lab_sessions().get(session_id)
    if session is None or session.get("type") != "lab_scenario":
        raise ValueError(f"No lab scenario session {session_id!r}")
    return {
        "session_id": session_id,
        "kind": session["kind"],
        "title": _i06_scenarios.SCENARIOS[session["kind"]]["title"],
        "done": session["done"],
        "rounds": [
            {"round": r["round"], "passed": r["passed"],
             "rubric": r["rubric"], "signals": r["signals"],
             "input_modality": r.get("input_modality", "text")}
            for r in session["results"]
        ],
        "debrief": _lab_debrief(session) if session["done"] else None,
    }


# ---------------------------------------------------------------------------
# Plugin wiring: MCP tools + CLI
# ---------------------------------------------------------------------------

def register_tools(mcp: Any) -> None:
    """Register the soft-skills MCP tools on a server instance."""
    _impl = globals()

    @mcp.tool()
    def soft_skill_areas() -> dict:
        """List the soft-skill training areas with descriptions.

        Returns:
            {"areas": {name: {"description", "good_looks_like"}}}.
        """
        return {"areas": SKILL_AREAS}

    @mcp.tool()
    def assess_soft_skill(area: str, answers: list) -> dict:
        """Run a soft-skill diagnostic: 3 scenario questions per area.

        Args:
            area: e.g. "negotiation", "star_storytelling".
            answers: one per question, as "A"/"B"/"C" or 0/1/2.

        Returns:
            {"area", "level" (developing/practicing/strong), "score_pct",
             "per_question" feedback, "next_step"}.
        """
        return _impl["assess"](area, answers)

    @mcp.tool()
    def start_drill(
        area: str, mode: str = "coach", difficulty: str = "firm"
    ) -> dict:
        """Start a soft-skill practice drill.

        Args:
            area: skill area to practice.
            mode: "coach" (single friendly rep) or "adversarial"
                (3-round tough interviewer).
            difficulty: "firm" | "hardball" | "brutal".

        Returns:
            {"session_id", "prompt", ...} — answer via review_drill_answer.
        """
        return _impl["drill"](area, None, mode, difficulty)

    @mcp.tool()
    def review_drill_answer(session_id: str, answer: str) -> dict:
        """Review a drill answer: STAR completeness, specificity, filler,
        tone. In adversarial mode, includes the next in-character
        challenge plus labeled COACH feedback.

        Args:
            session_id: from start_drill.
            answer: the user's spoken/written answer as text.
        """
        return _impl["review_drill"](session_id, answer)

    @mcp.tool()
    def start_negotiation(
        current_offer: float, target: float, difficulty: str = "firm"
    ) -> dict:
        """Start a 3-round salary negotiation vs. an adversarial hiring
        manager (firm/hardball/brutal). The adversary uses real tactics —
        low anchors, budget pushback, competing candidates, exploding
        offers. Between rounds you get labeled COACH feedback.

        Args:
            current_offer: the company's offer (annual base).
            target: the number you want.
            difficulty: "firm" | "hardball" | "brutal".
        """
        return _impl["negotiation_sim"](current_offer, target, None, difficulty)

    @mcp.tool()
    def negotiation_counter(
        session_id: str, counter: float, message: str
    ) -> dict:
        """Play one negotiation round: your counter-offer and message.

        Args:
            session_id: from start_negotiation.
            counter: your counter number.
            message: what you say to justify it.
        """
        return _impl["negotiation_round"](session_id, counter, message)

    @mcp.tool()
    def log_soft_skill_practice(
        area: str, score: float, kind: str = "drill"
    ) -> dict:
        """Log a practice rep (practice only — never a credential)."""
        return _impl["log_practice"](area, score, kind)

    @mcp.tool()
    def soft_skill_progress() -> dict:
        """Per-area practice reps, averages, and trends."""
        return _impl["progress"]()

    @mcp.tool()
    def lab_scenario_kinds() -> dict:
        """List the four soft-skill lab scenarios with briefing cards.

        conflict, influence, negotiation, ambiguous_stakeholder —
        each card discloses the setup, what good looks like, and how
        it is scored, before the scenario starts.
        """
        return _impl["scenario_kinds"]()

    @mcp.tool()
    def start_lab_scenario(kind: str) -> dict:
        """Start a 3-round soft-skill lab scenario.

        Args:
            kind: "conflict" | "influence" | "negotiation" |
                "ambiguous_stakeholder".
        """
        try:
            return _impl["start_scenario"](kind)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def review_lab_scenario(session_id: str, answer: str,
                            input_modality: str = "text") -> dict:
        """Score one lab scenario round against the disclosed rubric
        plus scenario behavior signals.

        Args:
            session_id: from start_lab_scenario.
            answer: the candidate's response text.
            input_modality: "text" (default) or "voice".
        """
        try:
            return _impl["review_scenario"](
                session_id, answer, input_modality=input_modality)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def lab_scenario_summary(session_id: str) -> dict:
        """Summarize a lab scenario: per-round results + debrief.

        Args:
            session_id: from start_lab_scenario.
        """
        try:
            return _impl["scenario_summary"](session_id)
        except ValueError as exc:
            return {"error": str(exc)}


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_soft_skills(args: Any) -> int:
    """CLI handler for `soft-skills` (dispatches on sub-action)."""
    as_json = getattr(args, "json", False)
    action = args.action
    try:
        if action == "areas":
            _print({"areas": SKILL_AREAS}, as_json)
        elif action == "assess":
            result = assess(args.area, args.answer)
            _print(result, as_json)
        elif action == "drill":
            result = drill(args.area, _load_profile(), args.mode, args.difficulty)
            _print(result, as_json)
        elif action == "review":
            result = review_drill(args.session_id, args.answer)
            _print(result, as_json)
        elif action == "negotiate":
            result = negotiation_sim(args.offer, args.target, None, args.difficulty)
            _print(result, as_json)
        elif action == "counter":
            result = negotiation_round(args.session_id, args.counter, args.message)
            _print(result, as_json)
        elif action == "log":
            result = log_practice(args.area, args.score, args.kind)
            _print(result, as_json)
        elif action == "progress":
            _print(progress(), as_json)
        elif action == "lab-kinds":
            _print(scenario_kinds(), as_json)
        elif action == "lab-start":
            result = start_scenario(args.kind)
            _print(result, as_json)
        elif action == "lab-review":
            result = review_scenario(args.session_id, args.answer)
            _print(result, as_json)
        elif action == "lab-summary":
            result = scenario_summary(args.session_id)
            _print(result, as_json)
        else:
            raise ValueError(f"Unknown action {action!r}")
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `soft-skills` to an argparse subparsers.

    Returns {"soft-skills": handler} for the caller's dispatch table
    (cli.py-style).
    """
    p = subparsers.add_parser(
        "soft-skills",
        help="Soft-skills training: diagnostics, drills, negotiation sim.",
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("areas", help="List the skill areas.")

    a = sub.add_parser("assess", help="Run a diagnostic for an area.")
    a.add_argument("area", help="Skill area, e.g. negotiation.")
    a.add_argument("answer", nargs=3,
                   help="One choice per question: A/B/C or 0/1/2.")

    d = sub.add_parser("drill", help="Start a practice drill.")
    d.add_argument("area", help="Skill area, e.g. star_storytelling.")
    d.add_argument("--mode", default="coach",
                   help="coach or adversarial (default: coach).")
    d.add_argument("--difficulty", default="firm",
                   help="firm|hardball|brutal (default: firm).")

    r = sub.add_parser("review", help="Review a drill answer.")
    r.add_argument("session_id", help="Session id from drill.")
    r.add_argument("answer", help="Your answer text (quote it).")

    n = sub.add_parser("negotiate", help="Start a negotiation sim.")
    n.add_argument("--offer", type=float, required=True,
                   help="The company's offer (annual base).")
    n.add_argument("--target", type=float, required=True,
                   help="The number you want.")
    n.add_argument("--difficulty", default="firm",
                   help="firm|hardball|brutal (default: firm).")

    c = sub.add_parser("counter", help="Play one negotiation round.")
    c.add_argument("session_id", help="Session id from negotiate.")
    c.add_argument("--counter", type=float, required=True,
                   help="Your counter number.")
    c.add_argument("--message", default="",
                   help="What you say to justify it (quote it).")

    lg = sub.add_parser("log", help="Log a practice rep.")
    lg.add_argument("area", help="Skill area.")
    lg.add_argument("score", type=float, help="0-100.")
    lg.add_argument("--kind", default="drill", help="Rep label.")

    sub.add_parser("progress", help="Show practice reps and trends.")

    sub.add_parser("lab-kinds", help="List the lab scenarios.")

    ls = sub.add_parser("lab-start", help="Start a lab scenario.")
    ls.add_argument("kind", help="conflict|influence|negotiation|"
                                "ambiguous_stakeholder.")

    lr = sub.add_parser("lab-review", help="Score one lab scenario round.")
    lr.add_argument("session_id", help="Session id from lab-start.")
    lr.add_argument("answer", help="Your response text (quote it).")

    lsm = sub.add_parser("lab-summary", help="Summarize a lab scenario.")
    lsm.add_argument("session_id", help="Session id from lab-start.")

    return {"soft-skills": cmd_soft_skills}
