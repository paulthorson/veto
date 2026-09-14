#!/usr/bin/env python3
"""On-device resume decoder (Initiative 04, Epic 1).

Analyzes pasted resume text fully on-device: no model, no API, no
network. Deterministic (regex + counting, stdlib only) and every
derived fact carries the quote it came from — the same honesty
invariant as ``jd_decoder``.

``decode_resume(text)`` returns a structured profile dict shaped to be
usable wherever the existing ``grill.py`` profile helpers are used
(``skills``, ``experience``, ``summary``, ...), plus decoder-native
fields:

* ``sections`` — segmented sections with their raw lines
  (experience, education, skills, summary, projects, certifications,
  other).
* ``skills_found`` — curated skills + alias-resolved skills, each with
  the quote it was found in.
* ``years_claims`` — explicit "N years" claims found in the text (the
  decoder reports what the resume *claims*; it never computes tenure
  from date arithmetic the text doesn't state).
* ``seniority`` — inferred seniority label from title keywords
  (``intern`` … ``staff``/``principal``/``director``/executive), always
  with the quote it came from. Title lines are preferred over bullets;
  the highest seniority rank seen wins.
* ``pii_present`` — which PII shapes were seen (email, US-format phone,
  URL). The decoder records *that* PII is present, never the values —
  email/phone values are masked everywhere they would be emitted, and
  ``pii_redacted`` labels the masking. Downstream surfaces (share
  cards) must never see raw contact values.

Honesty rules:
* Only the text in front of it is analyzed. The decoder never invents
  employers, titles, or metrics.
* Absence is reported as absence ("no skills section found"), never
  filled in.
* Ambiguity is labeled: ``confidence`` on each derived fact is
  ``"explicit"`` (stated verbatim) or ``"inferred"`` (pattern-derived,
  e.g. seniority from title keywords).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .aliases import canonicalize, known_aliases

# ---------------------------------------------------------------------------
# Section segmentation
# ---------------------------------------------------------------------------

_SECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("summary", re.compile(r"^\s*(summary|profile|objective|about)\b", re.I)),
    ("experience", re.compile(r"^\s*(experience|work experience|employment|work history)\b", re.I)),
    ("education", re.compile(r"^\s*(education|academic background|qualifications)\b", re.I)),
    ("skills", re.compile(r"^\s*(skills|technical skills|core competencies|competencies|technologies|tech stack)\b", re.I)),
    ("projects", re.compile(r"^\s*(projects|selected projects|personal projects)\b", re.I)),
    ("certifications", re.compile(r"^\s*(certifications?|licenses?|credentials)\b", re.I)),
    ("achievements", re.compile(r"^\s*(achievements|accomplishments|awards|honors)\b", re.I)),
]


def _segment_sections(text: str) -> dict[str, list[str]]:
    """Split resume text into labeled sections by header lines.

    A header must be the *whole* line (modulo a trailing colon) —
    ``fullmatch``, not ``search`` — so a body line like "Experience
    building APIs for clients" never becomes a section header.
    """
    sections: dict[str, list[str]] = {name: [] for name, _ in _SECTION_PATTERNS}
    sections["other"] = []
    current = "other"
    for line in text.splitlines():
        matched = False
        for name, pattern in _SECTION_PATTERNS:
            if pattern.fullmatch(line.strip().rstrip(":")):
                current = name
                matched = True
                break
        if not matched:
            sections[current].append(line)
    return sections


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

#: Curated skill vocabulary for resume-side matching. Alias-resolved via
#: ``aliases.canonicalize`` before matching, so "k8s" counts as Kubernetes.
SKILL_VOCABULARY: tuple[str, ...] = (
    "Python", "JavaScript", "TypeScript", "Go", "Java", "Ruby", "Rust",
    "C++", "SQL", "React", "Node.js", "Next.js", "Django", "Flask",
    "FastAPI", "Kubernetes", "Docker", "Terraform", "AWS", "Google Cloud",
    "Azure", "PostgreSQL", "MySQL", "MongoDB", "Elasticsearch", "Redis",
    "Kafka", "Airflow", "Spark", "Machine Learning", "Deep Learning",
    "Natural Language Processing", "Artificial Intelligence", "CI/CD",
    "GraphQL", "REST", "gRPC", "Linux", "Git", "Figma", "Agile", "Scrum",
    "Product Management", "Data Analysis", "A/B Testing", "SEO",
)

_YEARS_RE = re.compile(
    r"\b(\d{1,2})(?:\+)?\s*(?:years?|yrs?)\b"
    r"(?:\s+(?:of\s+)?(?:experience\s+)?(?:(?:with|in)\s+)?"
    # Multi-word capture: the skill phrase is attributed by
    # _known_skill_name, which keeps the longest leading phrase that
    # is a known skill ("4 years Machine Learning experience" ->
    # "Machine Learning"; "6 years Python building data pipelines" ->
    # "Python").
    r"((?:[A-Za-z][\w+#./-]*)(?:\s+[A-Za-z][\w+#./-]*)*))?",
    re.IGNORECASE,
)


def _known_skill_name(candidate: str) -> str | None:
    """Return the canonical skill name for ``candidate`` if it — or its
    longest leading phrase — is a skill ``skills_found`` can emit, else
    None.

    Keeps prose ("8 years of experience building") from becoming a
    bogus skill claim, and keeps multi-word skills ("Machine Learning")
    attributable where a single-token capture never could.
    """
    # Trailing sentence punctuation glues onto the last token in the
    # regex capture ("5 years of Python." captures "Python.") and would
    # defeat the lookup below; strip it. Interior punctuation ("Node.js",
    # "C++", "A/B") is part of the name and stays.
    words = candidate.strip().rstrip(".,;:!?").split()
    for end in range(len(words), 0, -1):
        canon = canonicalize(" ".join(words[:end]))
        if canon in _KNOWN_SKILL_NAMES:
            return canon
    return None


def _keyword_in_text(keyword: str, text: str) -> bool:
    """True when ``keyword`` appears as a standalone token in ``text``.

    Word-boundary style matching only — never bare substring. A skill
    must never be hallucinated with the highest confidence label, so
    "Java" does not match inside "JavaScript", "Rust" inside "trust",
    and "Agile" inside "fragile". The boundary class also covers
    ``+#.`` so tokens like "C++" and "Node.js" still match as whole
    tokens.
    """
    return bool(
        re.search(
            r"(?<![\w+#.])" + re.escape(keyword) + r"(?![\w+#.])",
            text,
            re.IGNORECASE,
        )
    )


def _extract_skills(text: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for skill in SKILL_VOCABULARY:
            canon = canonicalize(skill)
            if canon.lower() in seen:
                continue
            matched_alias: str | None = None
            if _keyword_in_text(skill, stripped):
                matched_alias = skill
            else:
                # Try each known alias of the canonical skill.
                for alias in known_aliases(skill):
                    if _keyword_in_text(alias, stripped):
                        matched_alias = alias
                        break
            if matched_alias:
                seen.add(canon.lower())
                found.append(
                    {
                        "skill": canon,
                        "matched_as": matched_alias,
                        "quote": _redact_pii(stripped)[:200],
                        "confidence": "explicit",
                    }
                )
    return sorted(found, key=lambda item: item["skill"].lower())


#: The names a years-claim may be attributed to: exactly the canonical
#: names ``skills_found`` can emit (vocabulary, canonicalized). Alias
#: canonical names that never appear in ``skills_found`` (e.g.
#: "Infrastructure as Code", "Test-Driven Development") are excluded —
#: attribution and skill emission stay consistent.
_KNOWN_SKILL_NAMES: frozenset[str] = frozenset(
    canonicalize(skill) for skill in SKILL_VOCABULARY
)


def _extract_years(text: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for line in text.splitlines():
        for match in _YEARS_RE.finditer(line):
            years = int(match.group(1))
            raw_skill = (match.group(2) or "").strip()
            skill = _known_skill_name(raw_skill) if raw_skill else None
            claims.append(
                {
                    "years": years,
                    "skill": skill,
                    "quote": _redact_pii(line.strip())[:200],
                    "confidence": "explicit",
                }
            )
    return claims


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------

#: Title keyword -> (seniority label, rank). Higher rank = more senior;
#: the HIGHEST rank seen wins, so "Senior Software Engineer (ex-intern)"
#: resolves to senior, never intern.
_SENIORITY_KEYWORDS: tuple[tuple[str, str, int], ...] = (
    ("intern", "intern", 0),
    ("junior", "junior", 1),
    ("associate", "associate", 2),
    ("senior", "senior", 3),
    ("lead", "lead", 4),
    ("staff", "staff", 5),
    ("principal", "principal", 6),
    ("manager", "manager", 7),
    ("director", "director", 8),
    ("vp", "executive", 9),
    ("vice president", "executive", 9),
    ("cto", "executive", 9),
    ("ceo", "executive", 9),
    ("head of", "executive", 9),
    ("founder", "executive", 9),
)


def _is_bullet_line(line: str) -> bool:
    """True for experience bullets (marker or indented continuation)."""
    return bool(re.match(r"^\s*[-•*]\s+", line)) or (
        bool(line.strip()) and line[0] in (" ", "\t")
    )


def _seniority_hits(line: str) -> list[tuple[int, str]]:
    """All (rank, label) seniority keywords found in ``line``."""
    lowered = line.lower()
    hits: list[tuple[int, str]] = []
    for keyword, label, rank in _SENIORITY_KEYWORDS:
        if re.search(r"\b" + re.escape(keyword) + r"\b", lowered):
            hits.append((rank, label))
    return hits


def _infer_seniority(
    experience_lines: list[str], other_lines: list[str]
) -> dict[str, Any] | None:
    """Infer seniority from the highest-ranked title keyword.

    Title lines (non-bullet experience lines) are preferred: only when
    no title line carries a seniority keyword do bullets and "other"
    lines get a say. Within a pass the HIGHEST rank wins — a senior
    title that mentions a past internship still reads as senior.
    """
    title_lines = [
        line for line in experience_lines
        if line.strip() and not _is_bullet_line(line)
    ]
    bullet_lines = [
        line for line in experience_lines
        if line.strip() and _is_bullet_line(line)
    ]
    for pool in (title_lines, bullet_lines, other_lines):
        best: tuple[int, str] | None = None
        best_line = ""
        for line in pool:
            if not line.strip():
                continue
            for rank, label in _seniority_hits(line):
                if best is None or rank > best[0]:
                    best = (rank, label)
                    best_line = line
        if best is not None:
            return {
                "label": best[1],
                "quote": _redact_pii(best_line.strip())[:200],
                "confidence": "inferred",
            }
    return None


# ---------------------------------------------------------------------------
# PII presence (shapes only, never values)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+|linkedin\.com/\S+|github\.com/\S+",
                     re.IGNORECASE)


def _pii_presence(text: str) -> dict[str, bool]:
    return {
        "email": bool(_EMAIL_RE.search(text)),
        "phone": bool(_PHONE_RE.search(text)),
        "url": bool(_URL_RE.search(text)),
    }


def _redact_pii(line: str) -> str:
    """Mask email/phone values in an emitted line.

    The decoder records *that* PII is present, never the values. URLs
    (LinkedIn/GitHub) are left intact: they are public identifiers the
    candidate put on the resume, while email/phone are contact vectors.
    Redaction is labeled via ``pii_redacted`` on the result so a
    consumer never mistakes a masked line for a verbatim quote.

    Callers redact BEFORE truncating quotes to 200 chars: truncating
    first could cut an email mid-value, leaving a raw PII fragment the
    regex no longer recognizes.
    """
    line = _EMAIL_RE.sub("[email redacted]", line)
    line = _PHONE_RE.sub("[phone redacted]", line)
    return line


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def decode_resume(text: str) -> dict[str, Any]:
    """Decode pasted resume text into a structured, quote-grounded profile.

    Pure function: text in, JSON-serializable dict out. Never raises on
    odd input — empty text yields an explicit empty profile with a
    ``limitations`` note.
    """
    text = text or ""
    sections = _segment_sections(text)
    non_empty = {k: v for k, v in sections.items() if any(l.strip() for l in v)}

    skills_found = _extract_skills(text)
    years_claims = _extract_years(text)
    seniority = _infer_seniority(sections["experience"], sections["other"])
    pii = _pii_presence(text)

    # Build the grill-compatible profile shape (skills list, experience
    # entries as dicts with summary text, misc summary lines). All
    # emitted text is PII-masked like the sections above.
    experience_entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in sections["experience"]:
        stripped = line.strip()
        if not stripped:
            continue
        stripped = _redact_pii(stripped)
        if re.match(r"^[-•*]\s+", stripped) or stripped.startswith("  "):
            # Bullet under the current role.
            if current is not None:
                current.setdefault("bullets", []).append(stripped)
            else:
                experience_entries.append({"summary": stripped})
        else:
            current = {"summary": stripped}
            experience_entries.append(current)

    limitations: list[str] = []
    if not text.strip():
        limitations.append("No resume text provided — nothing was analyzed.")
    if not skills_found:
        limitations.append(
            "No curated skills matched. The skill vocabulary is "
            "English-centric and finite; unmatched skills are reported "
            "as absent, not guessed."
        )
    if not any(l.strip() for l in sections["experience"]):
        limitations.append("No experience section detected.")
    limitations.append(
        "Phone detection covers US-format numbers only; non-US phone "
        "formats are reported as absent, not guessed."
    )

    return {
        # Section lines are emitted with PII values masked (see
        # _redact_pii); the raw text itself is never stored here.
        "sections": {
            k: [_redact_pii(ln) for ln in v] for k, v in non_empty.items()
        },
        "sections_detected": sorted(non_empty.keys()),
        "skills_found": skills_found,
        "years_claims": years_claims,
        "seniority": seniority,
        "pii_present": pii,
        "pii_redacted": True,
        # Grill-compatible profile shape (evidence_map reuses
        # grill.profile_has_keyword against this).
        "profile": {
            "skills": [s["skill"] for s in skills_found],
            "experience": experience_entries,
            # The summary is the one free-text profile field: mask PII
            # exactly like every other emitted string (sections,
            # quotes, experience entries). Contact info placed inside
            # a Summary section must never reach the grill-compatible
            # profile raw.
            "summary": _redact_pii(" ".join(sections["summary"]).strip()),
        },
        "resume_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "limitations": limitations,
    }
