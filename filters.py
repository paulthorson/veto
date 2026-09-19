#!/usr/bin/env python3
"""Post-search filters: salary floor and seniority level.

Most job boards don't expose salary/seniority as search parameters (or
only behind login), so these filters run on the text providers return —
the ``snippet`` from search plus ``description``/``requirements`` when a
job was enriched by get_job_details.

Both filters are recall-friendly: a job with NO detectable signal is
kept, never dropped. Only clear mismatches are removed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("veto-mcp.filters")

# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

# $150k / $150K / 150k
_K_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*[kK]\b")
# $150,000 / $150000
_FULL_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d{5,6})\b")
# $75/hr, $75/hour, $75 per hour
_HOURLY_RE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(?:/|per\s+)(?:hr|hour)\b", re.I)
_HOURS_PER_YEAR = 2080


def _annual_from_text(text: str) -> float | None:
    """Best annual salary hint found in text, or None."""
    text = text or ""
    candidates: list[float] = []
    for m in _K_RE.finditer(text):
        candidates.append(float(m.group(1)) * 1000)
    for m in _FULL_RE.finditer(text):
        candidates.append(float(m.group(1).replace(",", "")))
    for m in _HOURLY_RE.finditer(text):
        candidates.append(float(m.group(1)) * _HOURS_PER_YEAR)
    # Ranges like "150k-180k" / "$120,000 - $150,000": take the top end.
    return max(candidates) if candidates else None


def filter_by_salary(
    jobs: list[dict[str, Any]], salary_min: int
) -> list[dict[str, Any]]:
    """Keep jobs whose best salary hint meets ``salary_min`` (annual USD).

    Jobs with no detectable salary are kept. ``salary_min <= 0`` disables.
    """
    if not salary_min or salary_min <= 0:
        return jobs
    kept: list[dict[str, Any]] = []
    for job in jobs:
        text = " ".join(
            str(job.get(k, "") or "")
            for k in ("title", "snippet", "description", "requirements")
        )
        annual = _annual_from_text(text)
        if annual is None or annual >= salary_min:
            kept.append(job)
    log.info(
        "salary_min=%d: %d/%d jobs kept", salary_min, len(kept), len(jobs)
    )
    return kept


# ---------------------------------------------------------------------------
# Seniority
# ---------------------------------------------------------------------------

# Ordered low -> high. A title matching several markers takes the highest.
_SENIORITY_PATTERNS: list[tuple[str, list[str]]] = [
    ("entry", [r"\bintern", r"\bjunior\b", r"\bjr\.?\b", r"entry[\s-]?level", r"new grad"]),
    ("senior", [r"\bsenior\b", r"\bsr\.?\b", r"\blead\b"]),
    ("staff", [r"\bstaff\b"]),
    ("principal", [r"\bprincipal\b"]),
]
_LEVEL_RANK = {"entry": 0, "mid": 1, "senior": 2, "staff": 3, "principal": 4}


def detect_seniority(title: str) -> str | None:
    """Seniority level from a job title, or None when undetectable."""
    title = (title or "").lower()
    found: str | None = None
    for level, patterns in _SENIORITY_PATTERNS:
        if any(re.search(p, title) for p in patterns):
            found = level  # later (higher) levels overwrite
    return found


def filter_by_seniority(
    jobs: list[dict[str, Any]], seniority: str
) -> list[dict[str, Any]]:
    """Keep jobs matching the requested seniority level.

    ``seniority`` is one of entry|mid|senior|staff|principal (case
    insensitive). A job whose title carries no seniority marker counts as
    "mid" for matching purposes; jobs that can't be classified at all are
    kept. Empty string disables the filter.
    """
    want = (seniority or "").strip().lower()
    if not want:
        return jobs
    if want not in _LEVEL_RANK:
        raise ValueError(
            f"Unknown seniority {seniority!r}; use one of "
            "entry|mid|senior|staff|principal."
        )
    kept: list[dict[str, Any]] = []
    for job in jobs:
        detected = detect_seniority(job.get("title", "")) or "mid"
        if detected == want:
            kept.append(job)
    log.info("seniority=%s: %d/%d jobs kept", want, len(kept), len(jobs))
    return kept
