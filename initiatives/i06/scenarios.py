"""Initiative 06 — soft-skill lab scenarios.

Four deterministic, role-played practice scenarios. Each scenario is
fully disclosed before it starts: :func:`scenario_card` shows the setup,
what good looks like, how each round is scored — including every round's
signal *names* and the plain-language behavior that earns each signal
(see ``_SIGNAL_GUIDANCE``), so the signal triggers are visible to the
candidate, not hidden heuristics.

Each scenario runs 3 rounds and is evaluated against the disclosed
rubric (``initiatives.i06.rubric``) plus scenario-specific signals.

Scenarios:

* ``conflict`` — a peer wants to ship a shortcut you think is risky.
* ``influence`` — win a skeptical stakeholder's support without
  authority.
* ``negotiation`` — negotiate scope/timeline with a stakeholder
  (complements the salary ``negotiation_sim`` in soft_skills.py).
* ``ambiguous_stakeholder`` — a senior asks for something vague;
  clarify before you build.

Signal convention: most signals are phrase lists matched against the
answer with word-boundary matching (``"blameless"`` does not match
``"blame"``; a stray ``"which"`` inside prose does not count as a
question). Two signals are "magic" — they declare ``None`` phrases and
get dedicated handlers in :func:`_check_signal`:

* ``no_blame`` — passes unless blame language is found.
* ``no_self_bid`` — passes unless the candidate bids against themself.

A new signal declared with ``None`` phrases and no dedicated handler is
a configuration bug: :func:`_check_signal` raises a descriptive
``ValueError`` for it rather than failing opaquely.

Honesty contract (same as soft_skills.py): feedback describes the
ANSWER's qualities. Prompts use only the candidate's real profile
experience when a profile is supplied; nothing is invented.

The scenario "counterpart" stays professional: it challenges the
argument, never the person (banned-token invariant asserted in
tests).

Package-only: this module is part of the ``initiatives.i06`` package
and imports its sibling ``rubric`` with a relative import, so it must
be imported (``from initiatives.i06 import scenarios``), not executed
directly.
"""

from __future__ import annotations

import re
from typing import Any

from .rubric import score_answer

BANNED_TOKENS = (
    "stupid", "idiot", "incompetent", "loser", "pathetic", "worthless",
    "clueless", "dumb",
)


def _has_any(lowered: str, phrases: tuple[str, ...]) -> bool:
    """Word-boundary match of any phrase in the lowered answer text."""
    return any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in phrases)


_WH_OPENERS = ("what", "how", "why", "when", "who", "which")


def _looks_like_question(text: str) -> bool:
    """Heuristic: does the text contain an actual question?

    A literal ``?`` counts; otherwise a sentence/chunk must open with a
    question word ("what", "how", "why", "when", "who", "which"). This
    keeps a declarative use of "which" ("built with React which is
    fine") from passing as a clarifying question.
    """
    if "?" in text:
        return True
    for chunk in re.split(r"[.!?\n]+", text):
        first = chunk.strip().lower()
        words = first.split()
        if words and (words[0] in _WH_OPENERS):
            return True
    return False


_PERSPECTIVE = ("from their perspective", "their concern", "they worry",
                "they need", "their side", "see it as", "their priority",
                "understand their")
_BLAME = ("your fault", "you always", "you never", "blame")
_PROPOSAL = ("i propose", "my proposal", "how about", "what if we",
             "i suggest", "let's try", "compromise", "middle ground")

_THEIR_GOALS = ("your goal", "what you need", "matters to you",
                "your team needs", "your priority", "success for you")
_CLEAR_ASK = ("i'm asking", "my ask", "will you", "can we agree",
              "i need from you", "the ask is")

_ANCHOR = ("i'm looking for", "my range", "i need", "the number",
           "targeting")
_NO_SELF_BID = ("against myself", "i'll lower", "i can go lower")
_TRADE = ("in exchange", "if you", "trade", "give and take", "in return",
          "flexible on")

# NOTE: no bare "which" here — question detection for clarifying
# questions lives in _looks_like_question, so declarative prose like
# "built with React which is fine" cannot pass the signal.
_CLARIFY = ("what do you mean", "help me understand",
            "specifically", "when you say", "can you clarify",
            "what does success look like", "who is this for")
_ASSUMPTION = ("i'm assuming", "my assumption", "assuming that",
               "based on what you've said")
_SCOPE_BOUND = ("out of scope", "not in scope", "phase two", "later",
                "v1", "minimum")


#: Candidate-facing guidance for every scenario signal: what behavior
#: earns the signal, in plain words. Rendered in scenario_card() so the
#: triggers are disclosed before the scenario starts, and reused as the
#: "missing" feedback inside _check_signal().
_SIGNAL_GUIDANCE: dict[str, str] = {
    "perspective_taking": (
        "State their side first ('from their perspective…', 'their "
        "concern is…') before your position."),
    "proposal_made": (
        "Make a concrete proposal ('I propose…', 'how about…') — a "
        "position without a path forward stalls the conversation."),
    "commitment_named": (
        "Name the commitment explicitly: who does what, by when."),
    "their_goals_first": (
        "Open in their terms ('what matters to you…', 'your priority "
        "is…') before your proposal."),
    "history_acknowledged": (
        "Acknowledge the history ('fair point — last year…') before "
        "explaining what's different now."),
    "difference_named": (
        "Name what's different this time ('different because…', "
        "'what changed…')."),
    "clear_ask": (
        "Make the ask explicit ('my ask is…', 'will you… by…')."),
    "anchor_set": (
        "Anchor clearly ('I'm looking for…', 'the team can deliver… "
        "because…')."),
    "trade_offered": (
        "Trade, don't concede ('in exchange…', 'if you… then we "
        "can…')."),
    "clarifying_questions": (
        "Ask before you solve — use real questions ('help me "
        "understand…', 'when you say X, do you mean…?')."),
    "assumptions_stated": (
        "State assumptions out loud ('I'm assuming…') so they can "
        "be corrected."),
    "scope_bounded": (
        "Bound the scope ('in scope for v1…', 'phase two…')."),
    "no_blame": (
        "No blame language: attack the problem, not the person."),
    "no_self_bid": (
        "Don't bid against yourself: state your anchor and hold it."),
}

#: Signal names handled by dedicated logic in _check_signal() instead
#: of phrase-list matching. These are the only names allowed to carry
#: None phrases; anything else with None phrases is a config error.
_MAGIC_SIGNALS = ("no_blame", "no_self_bid")


SCENARIOS: dict[str, dict[str, Any]] = {
    "conflict": {
        "title": "The blocked launch",
        "setup": (
            "You and a peer, Sam, disagree about a launch. Sam wants to "
            "ship a shortcut you believe is risky; the deadline is "
            "Friday. You have a 1:1 with Sam in ten minutes."
        ),
        "good_looks_like": (
            "You state Sam's concern in your own words before your "
            "position, avoid blame, and propose a concrete middle path "
            "with a named owner and date."
        ),
        "rounds": [
            {
                "prompt": (
                    "Round 1 — Frame it: in two minutes, state the "
                    "disagreement as you see it, then state it as Sam "
                    "probably sees it."
                ),
                "focus": ("clarity", "structure"),
                "signals": {"perspective_taking": _PERSPECTIVE,
                            "no_blame": None},
            },
            {
                "prompt": (
                    "Round 2 — The 1:1: Sam says 'we've always shipped "
                    "like this and it's been fine.' Respond: hold your "
                    "position without attacking Sam, and propose a "
                    "concrete middle path."
                ),
                "focus": ("evidence", "clarity"),
                "signals": {"proposal_made": _PROPOSAL,
                            "no_blame": None},
            },
            {
                "prompt": (
                    "Round 3 — Commit: close the conversation. Name what "
                    "you both agreed, who owns what, and by when."
                ),
                "focus": ("structure", "clarity"),
                "signals": {"commitment_named": ("owner", "by friday",
                                                 "by monday", "i will",
                                                 "you will", "deadline"),
                            "no_blame": None},
            },
        ],
    },
    "influence": {
        "title": "The skeptical stakeholder",
        "setup": (
            "You need VP Dana's support for a proposal, but you have no "
            "authority over Dana's team. Dana's public priority this "
            "quarter is cutting operational cost."
        ),
        "good_looks_like": (
            "You frame the proposal in Dana's terms first, bring one "
            "piece of evidence tied to cost, and make a clear, small ask."
        ),
        "rounds": [
            {
                "prompt": (
                    "Round 1 — Frame it in their terms: open the "
                    "conversation by connecting your proposal to Dana's "
                    "cost-cutting priority."
                ),
                "focus": ("clarity", "evidence"),
                "signals": {"their_goals_first": _THEIR_GOALS},
            },
            {
                "prompt": (
                    "Round 2 — The objection: Dana says 'we tried "
                    "something like this last year and it went nowhere.' "
                    "Respond without dismissing the history."
                ),
                "focus": ("evidence", "trade_offs"),
                "signals": {"history_acknowledged": ("last year",
                                                     "last time",
                                                     "previously",
                                                     "fair point",
                                                     "you're right"),
                            "difference_named": ("different because",
                                                 "this time",
                                                 "what changed")},
            },
            {
                "prompt": (
                    "Round 3 — The ask: make your request concrete and "
                    "small. What exactly do you need from Dana, and by "
                    "when?"
                ),
                "focus": ("clarity", "structure"),
                "signals": {"clear_ask": _CLEAR_ASK},
            },
        ],
    },
    "negotiation": {
        "title": "The scope conversation",
        "setup": (
            "Stakeholder Priya wants three features in six weeks; your "
            "team can credibly deliver two. Priya opens: 'We really need "
            "all three — can't you stretch?'"
        ),
        "good_looks_like": (
            "You anchor on what's deliverable, never bid against "
            "yourself, and trade scope for something (time, resources, "
            "phasing) instead of conceding for free."
        ),
        "rounds": [
            {
                "prompt": (
                    "Round 1 — Anchor: respond to 'can't you stretch?' "
                    "with a clear statement of what the team can deliver "
                    "in six weeks, and why."
                ),
                "focus": ("clarity", "evidence"),
                "signals": {"anchor_set": _ANCHOR,
                            "no_self_bid": None},
            },
            {
                "prompt": (
                    "Round 2 — The push: Priya says 'what if we drop the "
                    "testing phase?' Hold the line on quality and offer "
                    "a trade, not a concession."
                ),
                "focus": ("trade_offs", "clarity"),
                "signals": {"trade_offered": _TRADE},
            },
            {
                "prompt": (
                    "Round 3 — Close: summarize the agreement — what "
                    "ships, what moves to phase two, and what you need "
                    "from Priya."
                ),
                "focus": ("structure", "clarity"),
                "signals": {"commitment_named": ("phase two", "ships",
                                                 "agreed", "i will",
                                                 "you will")},
            },
        ],
    },
    "ambiguous_stakeholder": {
        "title": "The vague request",
        "setup": (
            "Senior leader Jordan says: 'The dashboard needs to be "
            "better. Can you make it great by next month?' No metrics, "
            "no users named, no definition of better."
        ),
        "good_looks_like": (
            "You ask clarifying questions before proposing anything, "
            "state your assumptions out loud, and bound the scope so "
            "'great' becomes shippable."
        ),
        "rounds": [
            {
                "prompt": (
                    "Round 1 — Clarify first: respond with questions, "
                    "not solutions. What do you need to know before you "
                    "can scope this?"
                ),
                "focus": ("clarity", "question_quality"),
                "signals": {"clarifying_questions": _CLARIFY},
            },
            {
                "prompt": (
                    "Round 2 — Propose with assumptions: Jordan answers "
                    "'sales uses it daily and it's too slow.' Propose a "
                    "plan that states your assumptions explicitly."
                ),
                "focus": ("structure", "evidence"),
                "signals": {"assumptions_stated": _ASSUMPTION},
            },
            {
                "prompt": (
                    "Round 3 — Bound it: Jordan adds 'and can it also "
                    "email reports?' Keep v1 shippable: name what's in "
                    "and what's phase two."
                ),
                "focus": ("clarity", "trade_offs"),
                "signals": {"scope_bounded": _SCOPE_BOUND},
            },
        ],
    },
}

SCENARIO_KINDS = tuple(SCENARIOS.keys())


def validate_kind(kind: str | None) -> str:
    """Return the canonical scenario kind; raise ValueError on unknown."""
    k = (kind or "").strip().lower()
    if k not in SCENARIOS:
        raise ValueError(
            f"Unknown scenario {kind!r}. Choose one of: "
            f"{', '.join(SCENARIO_KINDS)}.")
    return k


def scenario_card(kind: str) -> str:
    """Disclosed scenario briefing (shown before the scenario starts).

    Discloses the setup, what good looks like, and — per round — every
    signal name with the plain-language behavior that earns it. The
    signal triggers are therefore visible to the candidate before the
    scenario starts; there are no hidden scoring factors.
    """
    kind = validate_kind(kind)
    spec = SCENARIOS[kind]
    lines = [
        f"## {spec['title']}",
        "",
        f"**Setup:** {spec['setup']}",
        "",
        f"**Good looks like:** {spec['good_looks_like']}",
        "",
        "**How each round is scored:** every round is scored against "
        "the disclosed rubric plus the scenario signals listed below. "
        "What earns each signal is spelled out in plain words — nothing "
        "is hidden.",
        "",
    ]
    for i, rnd in enumerate(spec["rounds"], start=1):
        bits = []
        for name in rnd["signals"]:
            label = name.replace("_", " ")
            bits.append(
                f"{label} — {_SIGNAL_GUIDANCE.get(name, name)}")
        lines.append(f"- Round {i}: {'; '.join(bits)}")
    lines += ["", f"**Rounds:** {len(spec['rounds'])} total."]
    return "\n".join(lines) + "\n"


def _check_signal(name: str, phrases: tuple[str, ...] | None,
                  answer: str) -> tuple[bool, str]:
    """Evaluate one scenario signal; returns (passed, feedback_line)."""
    lowered = (answer or "").lower()
    if name == "no_blame":
        bad = [b for b in _BLAME if _has_any(lowered, (b,))]
        if bad:
            return (False,
                    f"Blame language detected ({', '.join(bad)}) — "
                    "attack the problem, not the person.")
        return (True, "No blame language — the disagreement stays about "
                      "the work.")
    if name == "no_self_bid":
        bad = [b for b in _NO_SELF_BID if _has_any(lowered, (b,))]
        if bad:
            return (False,
                    "You bid against yourself — never lower your number "
                    "before they ask. Hold the anchor; let them move first.")
        return (True, "Anchor held — no bidding against yourself.")
    if name == "clarifying_questions":
        if phrases is None:
            raise ValueError(
                "Signal 'clarifying_questions' must declare a phrase "
                "list; only magic signals "
                f"{_MAGIC_SIGNALS} may use None.")
        if _looks_like_question(answer) or _has_any(lowered, phrases):
            return (True, "Signal 'clarifying questions' present — good.")
        return (False,
                f"Missing: {_SIGNAL_GUIDANCE['clarifying_questions']}")
    if phrases is None:
        raise ValueError(
            f"Signal {name!r} declares None phrases but has no dedicated "
            f"handler. Either add a handler in _check_signal() or give it "
            f"a phrase tuple. Magic signals (None allowed): "
            f"{_MAGIC_SIGNALS}.")
    if _has_any(lowered, phrases):
        return (True, f"Signal '{name.replace('_', ' ')}' present — good.")
    return (False, f"Missing: {_SIGNAL_GUIDANCE.get(name, name)}")


def evaluate_round(kind: str, round_index: int,
                   answer: str,
                   candidate_questions: list[str] | None = None
                   ) -> dict[str, Any]:
    """Score one scenario round.

    Args:
        kind: Scenario kind (see SCENARIO_KINDS).
        round_index: 0-based round within the scenario.
        answer: The candidate's answer text.
        candidate_questions: The candidate's own questions (for rounds
            whose focus includes the ``question_quality`` rubric
            dimension). Without it, that dimension is unscored (None),
            never 0.

    Returns {"rubric": <score_answer on the round's focus dimensions>,
    "signals": {name: {"passed": bool, "feedback": str}},
    "passed": bool (all signals), "feedback": [str]}.
    """
    kind = validate_kind(kind)
    spec = SCENARIOS[kind]
    rounds = spec["rounds"]
    if not 0 <= round_index < len(rounds):
        raise ValueError(
            f"Round {round_index} out of range for scenario {kind!r} "
            f"(0-{len(rounds) - 1}).")
    rnd = rounds[round_index]
    rubric = score_answer(answer, dimensions=rnd["focus"],
                          candidate_questions=candidate_questions)
    signals: dict[str, dict[str, Any]] = {}
    feedback: list[str] = []
    for name, phrases in rnd["signals"].items():
        passed, line = _check_signal(name, phrases, answer)
        signals[name] = {"passed": passed, "feedback": line}
        feedback.append(("✓ " if passed else "✗ ") + line)
    # Scenario-signal feedback first (behavior), then rubric feedback
    # (craft).
    for dim in rnd["focus"]:
        feedback.extend(rubric["dimensions"][dim]["feedback"])
    return {
        "kind": kind,
        "round_index": round_index,
        "rubric": {
            "dimension_scores": {d: r["score"]
                                 for d, r in rubric["dimensions"].items()},
            "overall": rubric["overall"],
        },
        "signals": signals,
        "passed": all(s["passed"] for s in signals.values()),
        "feedback": feedback,
    }


def assert_no_banned_tokens() -> None:
    """Raise if any scenario text contains a banned token."""
    for kind, spec in SCENARIOS.items():
        texts = [spec["title"], spec["setup"], spec["good_looks_like"]]
        texts += [r["prompt"] for r in spec["rounds"]]
        for text in texts:
            lowered = str(text).lower()
            for token in BANNED_TOKENS:
                if re.search(rf"\b{re.escape(token)}\b", lowered):
                    raise AssertionError(
                        f"Banned token {token!r} in scenario {kind!r}: "
                        f"{text!r}")
