#!/usr/bin/env python3
"""Job-description decoder: deterministic JD analyzer.

Job seekers can't tell which postings are gold and which are traps, so
this module reads a job description the way a skeptical friend would:
it extracts structured, evidence-backed signals and renders an overall
read. Everything is deterministic (regex + counting, stdlib only) and
every signal carries the quote it came from — no vibes, no invention.

``decode_jd(text)``
    Structured signals: transparency (salary range? concrete equity?),
    overwork language (each phrase quoted with a plain-English read),
    vagueness (buzzword density vs concrete responsibilities), growth
    signals, and green flags.

``jd_verdict(text)``
    Overall read — "strong", "mixed", or "caution" — with the top 3
    reasons, each grounded in a quote from the posting. The decoder
    presents evidence; it never moralizes.

Honesty rules:
  - Only the text in front of it is analyzed. Nothing is inferred about
    the company beyond what the posting literally says.
  - Absence is reported as absence ("no salary range found"), never
    filled in with market data or assumptions.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

#: Salary range in many formats: $120k-$150k, $120,000 - $150,000,
#: USD 100k to 130k, £60,000–£75,000, €90K-€110K.
_SALARY_RANGE_RE = re.compile(
    r"(?:[$£€]|USD|EUR|GBP|CAD|AUD)\s?"
    r"(\d[\d,]*(?:\.\d+)?)\s?[kK]?"
    r"\s*(?:-|–|—|\bto\b)\s*"
    r"(?:[$£€])?\s?(\d[\d,]*(?:\.\d+)?)\s?[kK]?"
)

#: A bare salary figure or salary-adjacent wording, without a range.
_SALARY_SINGLE_RE = re.compile(r"[$£€]\s?\d[\d,]*(?:\.\d+)?\s?[kK]?\b")
_SALARY_WORDS_RE = re.compile(
    r"\bcompetitive\s+(salary|compensation|pay)\b"
    r"|\bmarket[ -]rate\s+(salary|compensation|pay)\b"
    r"|\bsalary\s*:?\s*(competitive|negotiable|tbd)\b",
    re.IGNORECASE,
)

#: Equity wording. Concrete = a vesting/grant/amount detail nearby;
#: anything else is a vague mention.
_EQUITY_TERMS_RE = re.compile(
    r"\bstock options?\b|\bRSUs?\b|\bESOP\b|\bequity\b|\bownership stake\b",
    re.IGNORECASE,
)
_EQUITY_DETAIL_RE = re.compile(
    r"\bvest(?:ing|ed|s)?\b|\bgrant\b|\brefresh(?:er)?s?\b|[$£€]\s?\d|%",
    re.IGNORECASE,
)

#: Overwork language: phrase -> plain-English read. Reads describe what
#: the phrase *may* signal and suggest a question to ask; they never
#: moralize about the company.
_OVERWORK_PHRASES: dict[str, str] = {
    "fast-paced": (
        "Signals high urgency and shifting priorities. Ask about sprint "
        "load, on-call rotations, and how deadlines get set."
    ),
    "fast paced": (
        "Signals high urgency and shifting priorities. Ask about sprint "
        "load, on-call rotations, and how deadlines get set."
    ),
    "wear many hats": (
        "The role may span several jobs' worth of work. Ask what a "
        "typical week looks like and which responsibilities are core."
    ),
    "always on": (
        "Suggests after-hours expectations. Ask how the team protects "
        "off-hours time."
    ),
    "24/7": (
        "Round-the-clock language. Ask whether on-call or weekend "
        "coverage is part of the role."
    ),
    "unlimited PTO": (
        "Teams with unlimited PTO often take less time off than teams "
        "with a fixed allotment. Ask how many days the team actually "
        "took last year."
    ),
    "unlimited vacation": (
        "Teams with unlimited vacation often take less time off than "
        "teams with a fixed allotment. Ask how many days the team "
        "actually took last year."
    ),
    "work hard play hard": (
        "Culture framing often paired with long hours. Ask what a "
        "normal week looks like, not the highlight reel."
    ),
    "do whatever it takes": (
        "Open-ended commitment language. Ask for concrete examples of "
        "what this has meant recently."
    ),
    "nights and weekends": (
        "Explicitly names off-hours work. Ask how often it happens and "
        "whether it is compensated or comped."
    ),
    "hustle": (
        "Ambition language that can mask overwork. Ask how success is "
        "measured — output, or hours."
    ),
}

#: Vagueness markers: buzzwords that substitute for concrete detail.
_BUZZWORDS = [
    "synergy",
    "synergies",
    "rockstar",
    "rock star",
    "ninja",
    "guru",
    "thought leader",
    "game changer",
    "game-changer",
    "paradigm",
]

#: Bullet / numbered lines count as concrete responsibilities.
_BULLET_RE = re.compile(r"^\s*(?:[-•*▪–>]|\d+[.)])\s+\S", re.MULTILINE)

#: Growth signals: pattern -> signal label.
_GROWTH_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bmentorship\b", re.I), "mentorship"),
    (re.compile(r"\bmentor(?:ing|ed|s)?\b", re.I), "mentoring"),
    (re.compile(r"\blearning\s+(budget|stipend)\b", re.I), "learning budget"),
    (re.compile(r"\bprofessional development\b", re.I), "professional development"),
    (re.compile(r"\bcareer\s+(path|growth|development)\b", re.I), "career path"),
    (re.compile(r"\bpromotion\s+(path|track|opportunit\w*)\b", re.I), "promotion path"),
    (re.compile(r"\btraining\s+budget\b", re.I), "training budget"),
    (re.compile(r"\bconference\s+(budget|stipend)\b", re.I), "conference budget"),
]

#: Green flags: pattern -> flag label.
_GREEN_FLAGS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bremote(?:-first|-friendly)?\b|\bwork from home\b|\bdistributed team\b", re.I),
     "remote flexibility"),
    (re.compile(r"\bflexible\s+(hours|schedule|working)\b", re.I), "flexible schedule"),
    (re.compile(r"\bmust[- ]haves?\b|\brequired qualifications\b", re.I), "must-haves listed"),
    (re.compile(r"\bnice[- ]to[- ]haves?\b|\bbonus points\b", re.I), "nice-to-haves listed"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _quote(text: str, start: int, end: int, window: int = 70) -> str:
    """Return ~``window`` chars of context around a match, with ellipses."""
    s = max(0, start - window)
    e = min(len(text), end + window)
    # Snap to word boundaries so the snippet reads cleanly.
    while s > 0 and not text[s].isspace():
        s -= 1
    while e < len(text) and not text[e - 1].isspace():
        e += 1
    snippet = " ".join(text[s:e].split())
    prefix = "… " if s > 0 else ""
    suffix = " …" if e < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _find_all(text: str, pattern: re.Pattern) -> list[dict[str, Any]]:
    """All matches of ``pattern`` with evidence quotes."""
    hits = []
    for m in pattern.finditer(text):
        hits.append(
            {"match": m.group(0), "quote": _quote(text, m.start(), m.end())}
        )
    return hits


def _first_quote(text: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(text)
    return _quote(text, m.start(), m.end()) if m else None


# ---------------------------------------------------------------------------
# Signal extractors
# ---------------------------------------------------------------------------

def _transparency(text: str) -> dict[str, Any]:
    """Salary-range / equity-concreteness scoring, 0–100."""
    score = 0
    salary_range = None
    salary_mention = None

    m = _SALARY_RANGE_RE.search(text)
    if m:
        salary_range = {
            "low": m.group(1),
            "high": m.group(2),
            "raw": m.group(0).strip(),
            "quote": _quote(text, m.start(), m.end()),
        }
        score += 60
    else:
        m2 = _SALARY_SINGLE_RE.search(text) or _SALARY_WORDS_RE.search(text)
        if m2:
            salary_mention = {
                "raw": m2.group(0).strip(),
                "quote": _quote(text, m2.start(), m2.end()),
            }
            score += 25

    equity = None
    em = _EQUITY_TERMS_RE.search(text)
    if em:
        window = text[max(0, em.start() - 80): em.end() + 80]
        concrete = bool(_EQUITY_DETAIL_RE.search(window))
        equity = {
            "term": em.group(0),
            "concrete": concrete,
            "quote": _quote(text, em.start(), em.end()),
        }
        score += 40 if concrete else 15

    return {
        "score": min(100, score),
        "salary_range": salary_range,
        "salary_mention": salary_mention,
        "equity": equity,
    }


def _phrase_re(phrase: str) -> re.Pattern:
    """Regex for a phrase that tolerates any whitespace (newlines in
    pasted JDs) between words."""
    return re.compile(
        r"\s+".join(re.escape(w) for w in phrase.split()), re.IGNORECASE
    )


def _overwork(text: str) -> list[dict[str, str]]:
    """Overwork phrases, each with its quote and a plain-English read."""
    hits = []
    seen_spans: list[tuple[int, int]] = []
    # Longest phrases first so "unlimited PTO" wins over any substring.
    for phrase in sorted(_OVERWORK_PHRASES, key=len, reverse=True):
        for m in _phrase_re(phrase).finditer(text):
            span = (m.start(), m.end())
            if any(s <= span[0] < e or s < span[1] <= e for s, e in seen_spans):
                continue
            seen_spans.append(span)
            hits.append(
                {
                    "phrase": phrase,
                    "quote": _quote(text, m.start(), m.end()),
                    "read": _OVERWORK_PHRASES[phrase],
                    "_pos": m.start(),
                }
            )
    # Deterministic order: appearance in the text.
    hits.sort(key=lambda h: h.pop("_pos"))
    return hits


def _vagueness(text: str) -> dict[str, Any]:
    """Buzzword density vs concrete-responsibility count. Score 0–100
    where higher means more concrete."""
    words = len(text.split())
    buzz_hits = []
    seen: list[tuple[int, int]] = []
    for word in sorted(_BUZZWORDS, key=len, reverse=True):
        pat = re.compile(
            r"\b" + r"\s+".join(re.escape(w) for w in word.split()) + r"s?\b",
            re.IGNORECASE,
        )
        for m in pat.finditer(text):
            span = (m.start(), m.end())
            if any(s <= span[0] < e or s < span[1] <= e for s, e in seen):
                continue
            seen.append(span)
            buzz_hits.append(
                {"buzzword": word, "quote": _quote(text, m.start(), m.end())}
            )
    responsibilities = len(_BULLET_RE.findall(text))
    density = round(len(buzz_hits) / max(words, 1) * 100, 2)  # per 100 words
    score = max(0, min(100, round(50 + responsibilities * 6 - density * 8)))
    return {
        "score": score,
        "buzzwords": buzz_hits,
        "buzzword_count": len(buzz_hits),
        "buzzword_density_per_100_words": density,
        "responsibilities": responsibilities,
        "word_count": words,
    }


def _growth(text: str) -> list[dict[str, str]]:
    """Growth signals with evidence quotes (deduped by signal label)."""
    hits = []
    seen_labels: set[str] = set()
    for pattern, label in _GROWTH_SIGNALS:
        for hit in _find_all(text, pattern):
            if label in seen_labels:
                break
            seen_labels.add(label)
            hits.append({"signal": label, "quote": hit["quote"]})
            break
    return hits


def _green_flags(text: str, salary_range: dict | None) -> list[dict[str, str]]:
    """Green flags with evidence quotes."""
    flags = []
    seen_labels: set[str] = set()
    for pattern, label in _GREEN_FLAGS:
        quote = _first_quote(text, pattern)
        if quote and label not in seen_labels:
            seen_labels.add(label)
            flags.append({"flag": label, "quote": quote})
    if salary_range:
        flags.append(
            {"flag": "salary range disclosed", "quote": salary_range["quote"]}
        )
    return flags


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decode_jd(text: str) -> dict[str, Any]:
    """Decode a job description into structured, evidence-backed signals.

    Returns transparency / overwork / vagueness / growth / green_flags,
    each grounded in quotes from the posting. Never invents, never
    moralizes.
    """
    text = text or ""
    transparency = _transparency(text)
    overwork = _overwork(text)
    vagueness = _vagueness(text)
    growth = _growth(text)
    green_flags = _green_flags(text, transparency["salary_range"])

    lines = ["# JD decode", ""]
    t = transparency
    lines.append(f"## Transparency: {t['score']}/100")
    if t["salary_range"]:
        lines.append(f"- Salary range: {t['salary_range']['raw']}")
    elif t["salary_mention"]:
        lines.append(f"- Salary mentioned, no range: {t['salary_mention']['raw']}")
    else:
        lines.append("- No compensation information listed")
    if t["equity"]:
        detail = "concrete" if t["equity"]["concrete"] else "vague"
        lines.append(f"- Equity ({detail}): {t['equity']['term']}")
    lines.append("")
    lines.append(f"## Overwork language: {len(overwork)} flag(s)")
    for hit in overwork:
        lines.append(f"- {hit['phrase']!r}: {hit['read']}")
    lines.append("")
    v = vagueness
    lines.append(
        f"## Vagueness: {v['score']}/100 concrete "
        f"({v['buzzword_count']} buzzwords, {v['responsibilities']} responsibilities)"
    )
    lines.append("")
    lines.append(f"## Growth signals: {len(growth)}")
    for g in growth:
        lines.append(f"- {g['signal']}")
    lines.append("")
    lines.append(f"## Green flags: {len(green_flags)}")
    for f in green_flags:
        lines.append(f"- {f['flag']}")
    markdown = "\n".join(lines)

    return {
        "transparency": transparency,
        "overwork": overwork,
        "vagueness": vagueness,
        "growth": growth,
        "green_flags": green_flags,
        "markdown": markdown,
    }


def jd_verdict(text: str) -> dict[str, Any]:
    """Overall read of a job description: strong / mixed / caution.

    Scores from evidence-backed factors; the top 3 reasons are each
    grounded in a quote from the posting (absence notes carry
    ``quote: None`` because there is nothing to quote). Presents
    evidence; never moralizes.
    """
    text = text or ""
    if not text.strip():
        return {
            "verdict": "mixed",
            "score": 50,
            "reasons": [
                {
                    "impact": 0,
                    "reason": "No job description text provided",
                    "quote": None,
                }
            ],
            "markdown": "# JD verdict: mixed (50/100)\n\nNo job description text provided.",
        }

    decoded = decode_jd(text)
    t = decoded["transparency"]
    v = decoded["vagueness"]

    score = 50
    factors: list[tuple[int, str, str | None]] = []  # (impact, reason, quote)

    if t["salary_range"]:
        score += 25
        factors.append(
            (25, "Salary range disclosed", t["salary_range"]["quote"])
        )
    elif t["salary_mention"]:
        score += 10
        factors.append(
            (10, "Salary mentioned, but no range given", t["salary_mention"]["quote"])
        )
    else:
        score -= 10
        factors.append((-10, "No compensation information listed", None))

    if t["equity"]:
        if t["equity"]["concrete"]:
            score += 10
            factors.append((10, "Equity described concretely", t["equity"]["quote"]))
        else:
            score += 5
            factors.append((5, "Equity mentioned without details", t["equity"]["quote"]))

    overwork_penalty = min(30, 8 * len(decoded["overwork"]))
    score -= overwork_penalty
    for hit in decoded["overwork"]:
        factors.append((-8, f"Overwork-language flag: {hit['phrase']!r}", hit["quote"]))

    if v["buzzword_density_per_100_words"] >= 3:
        score -= 10
        first = v["buzzwords"][0]["quote"] if v["buzzwords"] else None
        factors.append((-10, "High buzzword density, few concrete details", first))
    elif v["buzzword_count"] == 0 and v["responsibilities"] >= 3:
        score += 5
        factors.append((5, "Concrete responsibilities spelled out", None))

    growth_bonus = min(15, 5 * len(decoded["growth"]))
    score += growth_bonus
    for g in decoded["growth"]:
        factors.append((5, f"Growth signal: {g['signal']}", g["quote"]))

    green_bonus = min(15, 5 * len(decoded["green_flags"]))
    score += green_bonus
    for f in decoded["green_flags"]:
        factors.append((5, f"Green flag: {f['flag']}", f["quote"]))

    score = max(0, min(100, score))
    verdict = "strong" if score >= 70 else ("mixed" if score >= 40 else "caution")

    # Top 3 reasons: quoted evidence first, then by absolute impact.
    factors.sort(key=lambda f: (f[2] is not None, abs(f[0])), reverse=True)
    reasons = [
        {"impact": impact, "reason": reason, "quote": quote}
        for impact, reason, quote in factors[:3]
    ]

    lines = [f"# JD verdict: {verdict} ({score}/100)", ""]
    for r in reasons:
        sign = "+" if r["impact"] > 0 else ("-" if r["impact"] < 0 else "")
        lines.append(f"- [{sign}{abs(r['impact'])}] {r['reason']}")
        if r["quote"]:
            lines.append(f"  > {r['quote']}")
    markdown = "\n".join(lines)

    return {
        "verdict": verdict,
        "score": score,
        "reasons": reasons,
        "signals": decoded,
        "markdown": markdown,
    }


# ---------------------------------------------------------------------------
# Plugin wiring (server.py / cli.py pick these up)
# ---------------------------------------------------------------------------

def register_tools(mcp: Any) -> None:
    """Register the JD-decoder MCP tools on an MCP server instance."""
    # Bind the module-level implementations explicitly: the @mcp.tool()
    # wrappers below reuse the public names, which would otherwise shadow
    # the globals inside this scope.
    _impl_decode = globals()["decode_jd"]
    _impl_verdict = globals()["jd_verdict"]

    @mcp.tool()
    def decode_jd(text: str) -> dict:
        """Decode a job description into structured signals.

        Args:
            text: The full job description text.

        Returns:
            Dict with transparency, overwork, vagueness, growth, and
            green_flags signals — every signal grounded in a quote from
            the posting. Deterministic; nothing is invented.
        """
        return _impl_decode(text)

    @mcp.tool()
    def jd_verdict(text: str) -> dict:
        """Overall read of a job description: strong / mixed / caution.

        Args:
            text: The full job description text.

        Returns:
            Dict with verdict, score, and the top 3 reasons, each
            grounded in a quote. Presents evidence; never moralizes.
        """
        return _impl_verdict(text)


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("markdown") or json.dumps(result, indent=2))


def cmd_decode_jd(args: Any) -> int:
    """CLI handler for `decode-jd`."""
    text = args.text or ""
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text.strip():
        print("error: provide JD text as an argument or via --file", file=sys.stderr)
        return 2
    result = jd_verdict(text) if args.verdict else decode_jd(text)
    _print_result(result, getattr(args, "json", False))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `decode-jd` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser(
        "decode-jd", help="Decode a job description: traps vs green flags."
    )
    p.add_argument(
        "text",
        nargs="?",
        default="",
        help="Job description text (or use --file).",
    )
    p.add_argument("--file", help="Read the job description from a file.")
    p.add_argument(
        "--verdict",
        action="store_true",
        help="Show the overall strong/mixed/caution verdict instead of raw signals.",
    )
    p.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    return {"decode-jd": cmd_decode_jd}
