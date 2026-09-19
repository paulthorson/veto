#!/usr/bin/env python3
"""Initiative 06 — disclosed interview rubric engine.

Scores practice answers against a PUBLIC rubric: the dimensions, their
weights, what each measures, and exactly how it is scored are all
visible to the user (see :func:`rubric_card`). No hidden factors, no
opaque model — every point on the score is traceable to an observable
signal in the answer text.

Dimensions (weights sum to 1.0):

* ``structure``    (0.25) — STAR shape: situation, task, action, result.
* ``evidence``     (0.25) — specifics: numbers, named outcomes, concrete
  decisions taken by the candidate.
* ``clarity``      (0.20) — bottom line up front, plain language,
  interview-length answers, low jargon.
* ``trade_offs``   (0.15) — alternatives considered and why this path
  was chosen; risks named.
* ``question_quality`` (0.15) — the candidate's own questions to the
  interviewer: specific, open-ended, not answerable from the website.

``MEETS_THRESHOLD`` (default 70) is the per-dimension bar used by the
longitudinal observed-gap rule. It is a *parameter*, not a fact: the
roadmap requires the operator + the independent framework reviewer to approve
the final threshold by Q1 day 10, and the engine must accept whatever
they approve. See ``initiatives.i06.longitudinal.ObservedGapRule``.

Honesty contract: every score is computed from the answer text alone.
Feedback describes answer craft and asks the candidate to supply their
own real facts; it never invents candidate experience.

Stdlib only, deterministic, no network.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# The disclosed rubric
# ---------------------------------------------------------------------------

DIMENSIONS = ("structure", "evidence", "clarity", "trade_offs",
              "question_quality")

#: Pending-approval default. The roadmap's Q1 gate requires the operator and the
#: independent framework reviewer to approve the final threshold by Q1 day
#: 10; the longitudinal engine takes whatever threshold they approve, so
#: this value is a default, not a locked fact.
MEETS_THRESHOLD = 70

DISCLOSED_RUBRIC: dict[str, dict[str, Any]] = {
    "structure": {
        "weight": 0.25,
        "measures": (
            "Whether the answer follows the Situation-Task-Action-Result "
            "arc so an interviewer can follow it."
        ),
        "how_scored": (
            "Counts explicit STAR markers: one point per part present "
            "(situation, task, action, result). Starts at 20; each part "
            "adds 20. No partial credit within a part."
        ),
        "strong_looks_like": (
            "You name the setup, your assignment, what you personally did, "
            "and the outcome — in that order."
        ),
        "weak_looks_like": (
            "A story that jumps straight to the ending, or describes what "
            "'we' did without your part."
        ),
    },
    "evidence": {
        "weight": 0.25,
        "measures": (
            "Whether the answer is anchored in specific, checkable facts: "
            "numbers, named systems or teams, and concrete decisions."
        ),
        "how_scored": (
            "Starts at 30. +20 for at least one number (metric, count, or "
            "date); +15 more for a second distinct number; +15 for naming "
            "a concrete artifact (system, tool, team, customer); +20 for a "
            "stated personal decision ('I chose', 'I decided'). 30 + 20 + "
            "15 + 15 + 20 = 100: a fully specific answer with a named "
            "decision scores exactly 100. Capped at 100. Artifact and "
            "decision matching is word-boundary: the word 'app' counts, "
            "but 'applied' and 'happy' do not; 'i decided' counts, but "
            "'i decidedly' does not."
        ),
        "strong_looks_like": (
            "Two real numbers and one concrete thing you changed, with "
            "your decision named."
        ),
        "weak_looks_like": (
            "Adjectives instead of facts: 'big impact', 'lots of users', "
            "'very successful'."
        ),
    },
    "clarity": {
        "weight": 0.20,
        "measures": (
            "Whether a busy interviewer can repeat your point back: "
            "bottom line up front, plain language, bounded length."
        ),
        "how_scored": (
            "Starts at 80. +15 if the answer leads with the conclusion: "
            "the first two sentences contain one of 'the result', 'in "
            "short', 'bottom line', 'we shipped', or a number — and the "
            "conclusion sentence carries content (it contains a number or "
            "is at least 6 words; a bare 'We shipped' or an empty marker "
            "earns nothing). -10 per jargon hit beyond the first, from a "
            "fixed list of 6: 'synergy', 'leverage' as a verb, 'holistic', "
            "'paradigm', 'bandwidth' for people, 'deep dive'. Matching is "
            "word-boundary on each term; 'leverage' counts only as a verb "
            "(inflected forms always count; the base form is skipped after "
            "a noun-phrase word such as 'the', 'financial', or 'debt'); "
            "'bandwidth' counts only for people (skipped after a network "
            "word such as 'network', 'internet', or 'data'). -15 if over "
            "250 words, +5 if 40-160 words. 80 + 15 + 5 = 100: a crisp, "
            "plain-language answer with the conclusion up front scores "
            "exactly 100. Capped 0-100."
        ),
        "strong_looks_like": (
            "The point lands in the first two sentences; a non-expert "
            "could summarize it."
        ),
        "weak_looks_like": (
            "Two minutes of context before the point; buzzwords doing the "
            "work of nouns."
        ),
    },
    "trade_offs": {
        "weight": 0.15,
        "measures": (
            "Whether the candidate shows judgment: alternatives "
            "considered, why this path, what was risked."
        ),
        "how_scored": (
            "Starts at 25. +25 for naming an alternative that was "
            "rejected ('instead of', 'rather than', 'considered', "
            "'evaluated', 'looked at', 'weighed'). +25 "
            "for a reason for the choice: 'because' followed by a real "
            "clause of 5+ words (a bare 'because I like it' does not "
            "count), or naming the choice directly ('chose X over Y', "
            "'decided on', 'picked', 'went with', 'prioritized'). "
            "+25 for naming a downside or risk ('downside', 'risk', "
            "'trade-off', 'tradeoff', 'cost was', 'the catch', "
            "'compromise'). Matching is word-boundary on each term: "
            "'considered' counts, but 'reconsidered' does not; 'the "
            "catch' counts as the phrase. Capped at 100. Scores "
            "are low by design on answers that never discuss a decision — "
            "a pure STAR story with no fork in the road scores 25."
        ),
        "strong_looks_like": (
            "Names the road not taken, says why, and admits what it cost."
        ),
        "weak_looks_like": (
            "One path described as if it were the only possible path."
        ),
    },
    "question_quality": {
        "weight": 0.15,
        "measures": (
            "The quality of the candidate's own questions for the "
            "interviewer: specific to this team and role, open-ended, not "
            "answerable from the careers page."
        ),
        "how_scored": (
            "Scored per question, averaged. Starts at 30. +25 for an "
            "open-ended opener ('how', 'what', 'why', 'tell me about', "
            "'walk me through'). +20 for naming something concrete "
            "about the role ('team', 'role', 'product', 'roadmap', "
            "'stack', 'on-call', 'manager', 'sprint', 'customer'). +25 "
            "for asking about a real tension ('hardest', 'challenge', "
            "'how do you decide', 'success look like', 'gets in the "
            "way', 'what would you change', 'proud of'). Then -30 for a "
            "question answerable from "
            "the website ('what does the company do', 'where are you "
            "located') — applied after the positive signals, so a "
            "website-answerable question with every positive signal "
            "scores 30 + 25 + 20 + 25 - 30 = 70, and a bare "
            "website-answerable question scores 0. Matching is "
            "substring-based by design: a longer question that merely "
            "begins with a banned phrase (e.g. 'What does the company "
            "do about on-call staffing?') still takes the -30 — "
            "word-boundary matching does not disambiguate this edge "
            "because the phrases are already multi-word, and leading "
            "with the careers-page phrasing reads as the website-"
            "answerable question even when more words follow. "
            "Rephrase to lead with the specific ask. Capped 0-100. A "
            "session round with no candidate questions leaves this "
            "dimension unscored (None), never 0."
        ),
        "strong_looks_like": (
            "'What does success look like for this team in the first six "
            "months, and what usually gets in the way?'"
        ),
        "weak_looks_like": (
            "'So... what does the company do?' (answerable from the "
            "careers page)."
        ),
    },
}


def _weights() -> dict[str, float]:
    return {d: DISCLOSED_RUBRIC[d]["weight"] for d in DIMENSIONS}


# ---------------------------------------------------------------------------
# Signal extraction (all from the answer text; nothing invented)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_SENT_RE = re.compile(r"[^.!?]+[.!?]")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

#: STAR markers per part. The loose markers ("had to", "at the time",
#: "was facing") were removed: they let content-free answers score
#: structure credit. Each remaining marker is matched word-boundary.
_STAR_MARKERS: dict[str, tuple[str, ...]] = {
    "situation": ("situation", "context", "background", "the problem",
                  "the challenge", "when i joined"),
    "task": ("task", "goal", "objective", "responsible for", "needed to",
             "my job was", "assigned to", "was asked to"),
    "action": ("action", "i did", "i built", "i led", "i implemented",
               "i decided", "i designed", "i wrote", "i drove",
               "steps i took", "my approach", "i chose", "i created",
               "i shipped"),
    "result": ("result", "outcome", "impact", "as a result", "led to",
               "improved", "reduced", "increased", "shipped", "launched",
               "saved", "grew", "cut "),
}

#: The disclosed jargon list: exactly 6 terms (see
#: ``DISCLOSED_RUBRIC["clarity"]["how_scored"]``). Matching is
#: word-boundary; "leverage" and "bandwidth" carry the disclosed
#: context qualifiers implemented by :func:`_jargon_hits`.
_JARGON = ("synergy", "leverage", "holistic", "paradigm", "bandwidth",
           "deep dive")

#: A word immediately before base-form "leverage" that marks noun use
#: ("financial leverage", "the leverage") — disclosed qualifier.
_LEVERAGE_NOUN_PRECEDERS = frozenset({
    "the", "a", "an", "of", "with", "by", "on", "as", "for",
    "financial", "debt", "operating", "operational", "brand",
    "market", "data", "high", "low", "more", "less", "greater",
    "much", "enough",
})

#: A word immediately before "bandwidth" that marks network-capacity
#: use ("network bandwidth") rather than people-capacity — disclosed
#: qualifier.
_BANDWIDTH_NETWORK_PRECEDERS = frozenset({
    "network", "internet", "broadband", "server", "data", "mobile",
    "wifi", "fiber", "fibre", "upload", "download",
})


def _jargon_hits(text: str) -> list[str]:
    """Distinct disclosed jargon terms found in the text.

    Word-boundary matching on each of the 6 terms. "leverage" counts
    only as a verb: inflected forms (leverages/leveraged/leveraging)
    always count; the base form is skipped when preceded by a
    noun-phrase word (see ``_LEVERAGE_NOUN_PRECEDERS``). "bandwidth"
    counts only for people: skipped after a network word (see
    ``_BANDWIDTH_NETWORK_PRECEDERS``). "deep dive" matches the spaced
    and hyphenated forms.

    This is a deliberately small heuristic, not NLP: it trades away
    true part-of-speech tagging to stay stdlib-only and deterministic
    (see the WS-A decision record). Edge cases it gets wrong are
    documented rather than hidden: e.g. "use leverage to grow" is
    counted as a verb use.
    """
    tokens = _WORD_RE.findall((text or "").lower())
    raw: list[str] = []
    for i, tok in enumerate(tokens):
        prev = tokens[i - 1] if i else ""
        if tok in ("synergy", "holistic", "paradigm"):
            raw.append(tok)
        elif tok in ("leverage", "leverages", "leveraged",
                      "leveraging"):
            if tok == "leverage" and prev in _LEVERAGE_NOUN_PRECEDERS:
                continue
            raw.append("leverage")
        elif tok == "bandwidth":
            if prev in _BANDWIDTH_NETWORK_PRECEDERS:
                continue
            raw.append("bandwidth")
        elif (tok == "deep" and i + 1 < len(tokens)
              and tokens[i + 1] == "dive"):
            raw.append("deep dive")
    seen: list[str] = []
    for hit in raw:
        if hit not in seen:
            seen.append(hit)
    return seen


#: The disclosed conclusion markers for the clarity +15: exactly these
#: four, plus a number in the first two sentences. (Undisclosed extras
#: — "i shipped", "the outcome", "ultimately" — were removed so the
#: list matches the card exactly.)
_CONCLUSION_EARLY = ("the result", "in short", "bottom line", "we shipped")
_CONCLUSION_RES = tuple(
    re.compile(rf"\b{re.escape(m)}\b") for m in _CONCLUSION_EARLY)

#: Minimum words for a marker-bearing conclusion sentence to count as
#: carrying content (disclosed).
_CONCLUSION_MIN_WORDS = 6

_ALTERNATIVE = ("instead of", "rather than", "considered", "evaluated",
                "looked at", "weighed")
_REASON = ("because", "chose", "decided on", "picked", "went with",
           "prioritized")
#: Words a "because" clause needs to count as a reason (disclosed):
#: a bare "because I like it" (3 words) is not a reason.
_REASON_CLAUSE_MIN_WORDS = 5


def _has_reason(lowered: str) -> bool:
    """Whether the text states a reason for the choice (disclosed rule).

    "because" counts only when its clause (up to the next sentence or
    clause break) has at least ``_REASON_CLAUSE_MIN_WORDS`` words —
    "because I like it" earns nothing. The choice verbs ("chose",
    "decided on", "picked", "went with", "prioritized") count on a
    plain word-boundary match, as disclosed.
    """
    for m in re.finditer(r"\bbecause\b", lowered):
        clause = re.split(r"[.!?;]", lowered[m.end():], maxsplit=1)[0]
        if len(_WORD_RE.findall(clause)) >= _REASON_CLAUSE_MIN_WORDS:
            return True
    rest = tuple(p for p in _REASON if p != "because")
    return any(re.search(rf"\b{re.escape(p)}\b", lowered) for p in rest)
_DOWNSIDE = ("downside", "risk", "trade-off", "tradeoff", "cost was",
             "the catch", "compromise")

_OPENERS = ("how ", "what ", "why ", "tell me about", "walk me through")
_SPECIFIC = ("team", "role", "product", "roadmap", "stack", "on-call",
             "manager", "sprint", "customer")
_TENSION = ("hardest", "challenge", "how do you decide", "success look like",
            "gets in the way", "what would you change", "proud of")
_WEBSITE_ANSWERABLE = ("what does the company do", "where are you located",
                       "how big is the company", "what industry",
                       "are you hiring")


def _contains_any(lowered: str, phrases: tuple[str, ...]) -> bool:
    return any(p in lowered for p in phrases)


def _contains_any_word(lowered: str, phrases: tuple[str, ...]) -> bool:
    """Word-boundary variant of :func:`_contains_any` for marker lists.

    Prevents e.g. "task" matching inside "multitasking".
    """
    return any(re.search(rf"\b{re.escape(p.strip())}\b", lowered)
               for p in phrases)


def score_structure(answer: str) -> dict[str, Any]:
    """Score the structure dimension (0-100) with named signals."""
    lowered = (answer or "").lower()
    hits = {part: _contains_any_word(lowered, markers)
            for part, markers in _STAR_MARKERS.items()}
    n = sum(1 for v in hits.values() if v)
    score = min(100, 20 + 20 * n)
    missing = [p for p, v in hits.items() if not v]
    return {
        "score": score,
        "signals": {"star_parts_present": [p for p, v in hits.items()
                                           if v],
                    "star_parts_missing": missing},
        "feedback": (
            [f"Structure: all four STAR parts present."]
            if not missing else
            [f"Structure: missing {', '.join(missing)} — name "
             f"{' and '.join(missing)} explicitly so the interviewer can "
             "follow the arc."]
        ),
    }


def score_evidence(answer: str) -> dict[str, Any]:
    """Score the evidence dimension (0-100) with named signals."""
    text = answer or ""
    lowered = text.lower()
    numbers = _NUMBER_RE.findall(text)
    distinct_numbers = len({n.strip(",") for n in numbers})
    # Word-boundary matching (shared with the structure/STAR markers):
    # "app" must be the word "app", not a substring of "applied" or
    # "happy"; "api" not "rapidly"; "i decided" not "i decidedly".
    # Substring matching here granted unearned artifact/decision credit
    # to ordinary interview-practice phrasing (critic RE-review,
    # 2026-09-13).
    has_named_artifact = _contains_any_word(
        lowered, ("system", "service", "tool", "team", "customer",
                  "client", "pipeline", "dashboard", "api", "app"))
    has_personal_decision = _contains_any_word(
        lowered, ("i chose", "i decided", "my decision", "i picked",
                  "i prioritized"))
    score = 30
    if distinct_numbers >= 1:
        score += 20
    if distinct_numbers >= 2:
        score += 15
    if has_named_artifact:
        score += 15
    if has_personal_decision:
        score += 20
    score = min(100, score)
    feedback = []
    if distinct_numbers == 0:
        feedback.append(
            "Evidence: no numbers in this answer — add one measurable "
            "outcome from your real experience (latency, users, revenue, "
            "time saved).")
    elif distinct_numbers == 1:
        feedback.append(
            "Evidence: one number present — a second distinct number "
            "(before/after, cost, scale) would anchor the story.")
    if not has_named_artifact:
        feedback.append(
            "Evidence: name the concrete thing — the system, team, or "
            "customer this happened with.")
    if not has_personal_decision:
        feedback.append(
            "Evidence: state your decision explicitly ('I chose…') so the "
            "interviewer sees your judgment, not just the outcome.")
    if not feedback:
        feedback.append("Evidence: specific and anchored — good.")
    return {
        "score": score,
        "signals": {"distinct_numbers": distinct_numbers,
                    "named_artifact": has_named_artifact,
                    "personal_decision": has_personal_decision},
        "feedback": feedback,
    }


def score_clarity(answer: str) -> dict[str, Any]:
    """Score the clarity dimension (0-100) with named signals."""
    text = answer or ""
    words = _WORD_RE.findall(text)
    n_words = len(words)
    sentences = [s.strip() for s in _SENT_RE.findall(text) if s.strip()]
    first_two = sentences[:2]
    first_two_text = " ".join(first_two)
    conclusion_sentence = next(
        (s for s in first_two
         if any(rx.search(s.lower()) for rx in _CONCLUSION_RES)),
        None)
    has_number = bool(_NUMBER_RE.search(conclusion_sentence or ""))
    long_enough = (
        conclusion_sentence is not None
        and len(_WORD_RE.findall(conclusion_sentence))
        >= _CONCLUSION_MIN_WORDS)
    conclusion_has_content = (
        conclusion_sentence is not None and (has_number or long_enough))
    bottom_line_up_front = (
        conclusion_has_content
        or bool(_NUMBER_RE.search(first_two_text)))
    jargon_hits = _jargon_hits(text)
    score = 80
    if bottom_line_up_front:
        score += 15
    extra_jargon = max(0, len(jargon_hits) - 1)
    score -= 10 * extra_jargon
    if n_words > 250:
        score -= 15
    elif 40 <= n_words <= 160:
        score += 5
    score = max(0, min(100, score))
    feedback = []
    if not bottom_line_up_front:
        feedback.append(
            "Clarity: put the conclusion in the first two sentences — "
            "state the outcome, then the story.")
    if len(jargon_hits) > 1:
        feedback.append(
            f"Clarity: jargon doing the work of nouns "
            f"({', '.join(jargon_hits[:3])}) — swap for plain words.")
    elif jargon_hits:
        feedback.append(
            f"Clarity: one jargon term ('{jargon_hits[0]}') — fine once, "
            "but keep the rest plain.")
    if n_words > 250:
        feedback.append(
            f"Clarity: {n_words} words is long for an interview answer — "
            "tighten to the key decisions and the outcome.")
    elif n_words < 40:
        feedback.append(
            f"Clarity: {n_words} words is thin — an interviewer needs "
            "enough detail to evaluate you.")
    if not feedback:
        feedback.append("Clarity: lands the point cleanly — good.")
    return {
        "score": score,
        "signals": {"words": n_words,
                    "bottom_line_up_front": bottom_line_up_front,
                    "jargon_hits": jargon_hits},
        "feedback": feedback,
    }


def score_trade_offs(answer: str) -> dict[str, Any]:
    """Score the trade-offs dimension (0-100) with named signals."""
    lowered = (answer or "").lower()
    # Word-boundary matching, consistent with the evidence dimension:
    # "reconsidered" must not fire "considered", "risks" must not fire
    # "risk". (RE-review minor, 2026-09-13.)
    has_alternative = _contains_any_word(lowered, _ALTERNATIVE)
    has_reason = _has_reason(lowered)
    has_downside = _contains_any_word(lowered, _DOWNSIDE)
    score = 25
    if has_alternative:
        score += 25
    if has_reason:
        score += 25
    if has_downside:
        score += 25
    score = min(100, score)
    feedback = []
    if not has_alternative:
        feedback.append(
            "Trade-offs: name the road not taken ('instead of…', 'we "
            "considered…') — judgment is shown by comparison.")
    if not has_reason:
        feedback.append(
            "Trade-offs: say why this path won ('because…', 'we chose X "
            "over Y since…').")
    if not has_downside:
        feedback.append(
            "Trade-offs: admit what it cost — a risk, a downside, or a "
            "compromise. Every real decision has one.")
    if not feedback:
        feedback.append("Trade-offs: alternatives, reasons, and costs — "
                        "good judgment on display.")
    return {
        "score": score,
        "signals": {"alternative_named": has_alternative,
                    "reason_given": has_reason,
                    "downside_named": has_downside},
        "feedback": feedback,
    }


def score_question_quality(questions: list[str]) -> dict[str, Any]:
    """Score candidate-asked questions (0-100, averaged), or None.

    Returns ``{"score": None, ...}`` when no questions were given — the
    dimension is *unscored*, never zero, so it cannot drag a round down
    for a question type that never happened.
    """
    cleaned = [q.strip() for q in (questions or []) if str(q).strip()]
    if not cleaned:
        return {
            "score": None,
            "signals": {"questions_asked": 0},
            "feedback": ["Question quality: no candidate questions in "
                         "this round — unscored, not penalized."],
        }
    per_q = []
    website_flags = []
    for q in cleaned:
        lowered = q.lower().strip()
        is_website_answerable = _contains_any(lowered, _WEBSITE_ANSWERABLE)
        website_flags.append(is_website_answerable)
        s = 30
        if lowered.startswith(_OPENERS):
            s += 25
        if _contains_any(lowered, _SPECIFIC):
            s += 20
        if _contains_any(lowered, _TENSION):
            s += 25
        if is_website_answerable:
            s -= 30
        per_q.append(max(0, min(100, s)))
    avg = round(sum(per_q) / len(per_q))
    return {
        "score": avg,
        "signals": {"questions_asked": len(cleaned),
                    "per_question": per_q,
                    "website_answerable": website_flags},
        "feedback": (
            ["Question quality: specific and probing — good."]
            if avg >= MEETS_THRESHOLD else
            ["Question quality: aim for open-ended, team-specific "
             "questions about real tensions ('what does success look "
             "like here in six months, and what gets in the way?') — "
             "avoid anything the careers page answers."]
        ),
    }


# ---------------------------------------------------------------------------
# Combined scoring
# ---------------------------------------------------------------------------

_SCORERS = {
    "structure": score_structure,
    "evidence": score_evidence,
    "clarity": score_clarity,
    "trade_offs": score_trade_offs,
}


def score_answer(
    answer: str,
    dimensions: tuple[str, ...] | list[str] | None = None,
    weights: dict[str, float] | None = None,
    candidate_questions: list[str] | None = None,
) -> dict[str, Any]:
    """Score an answer against the disclosed rubric.

    Args:
        answer: The candidate's answer text.
        dimensions: Subset of DIMENSIONS to score (default: all five;
            ``question_quality`` needs ``candidate_questions``).
        weights: Optional per-dimension weights (must be the disclosed
            weights unless a mode overrides them — see modes.py).
        candidate_questions: The candidate's own questions to the
            interviewer, for the ``question_quality`` dimension.

    Returns:
        {"dimensions": {dim: {"score", "signals", "feedback"}}, "overall":
        int 0-100, "weights": {...}, "unscored": [dims with None]}.
        Dimensions that could not be scored are listed in "unscored"
        and excluded from the overall (weights renormalized).
    """
    dims = list(dimensions) if dimensions else list(DIMENSIONS)
    unknown = [d for d in dims if d not in DIMENSIONS]
    if unknown:
        raise ValueError(f"Unknown rubric dimensions: {unknown}")
    w = dict(weights) if weights else _weights()
    results: dict[str, dict[str, Any]] = {}
    for dim in dims:
        if dim == "question_quality":
            results[dim] = score_question_quality(candidate_questions or [])
        else:
            results[dim] = _SCORERS[dim](answer)
    scored = {d: r for d, r in results.items()
              if r["score"] is not None}
    unscored = [d for d, r in results.items() if r["score"] is None]
    total_w = sum(w.get(d, 0.0) for d in scored)
    overall = (round(sum(r["score"] * w.get(d, 0.0) for d, r in scored.items())
                    / total_w)
               if scored and total_w > 0 else 0)
    return {
        "dimensions": results,
        "overall": overall,
        "weights": {d: w.get(d, 0.0) for d in dims},
        "unscored": unscored,
    }


def meets_threshold(score: int | None,
                    threshold: int | None = None) -> bool | None:
    """Whether a dimension score meets the bar.

    Returns None for unscored (None) dimensions — never coerced to
    False, so unscored dimensions can't manufacture a gap.

    The default is resolved from ``MEETS_THRESHOLD`` at call time,
    not bound at def-time: the roadmap's Q1 gate lets the operator and the
    independent framework reviewer approve a new threshold value, and
    that approval must take effect without re-importing this module.
    """
    if score is None:
        return None
    if threshold is None:
        threshold = MEETS_THRESHOLD
    return score >= threshold


def rubric_card() -> str:
    """The full disclosed rubric as markdown (for terminal/web/phone)."""
    lines = [
        "# Interview rubric (disclosed)",
        "",
        "Every practice answer is scored against these five dimensions. "
        "Weights, measures, and scoring rules are public — there are no "
        "hidden factors.",
        "",
        f"Default 'meets' threshold: **{MEETS_THRESHOLD}/100** per "
        "dimension. The final threshold is approved by the operator and the "
        "independent framework reviewer (roadmap Q1 gate); the engine "
        "accepts whatever they approve.",
        "",
    ]
    for dim in DIMENSIONS:
        spec = DISCLOSED_RUBRIC[dim]
        lines += [
            f"## {dim.replace('_', ' ').title()} — weight "
            f"{spec['weight']:.0%}",
            "",
            f"**Measures:** {spec['measures']}",
            "",
            f"**How scored:** {spec['how_scored']}",
            "",
            f"**Strong:** {spec['strong_looks_like']}",
            "",
            f"**Weak:** {spec['weak_looks_like']}",
            "",
        ]
    return "\n".join(lines).strip() + "\n"
