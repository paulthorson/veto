#!/usr/bin/env python3
"""Fit scoring for job listings: 0-100 score with human-readable reasons.

This is the engine behind "the job bot that says no": every job gets a
fit score, and anything under 60 is vetoed with a concrete reason.

Scoring weights (documented here so they can be tuned deliberately):

    skills     50 pts  skill overlap between posting and profile
    seniority  15 pts  detected job level vs profile seniority
    salary     15 pts  salary hint vs preferences salary floor
    location   15 pts  remote/location fit vs preferences
    recency     5 pts  how fresh the posting is
                       ─────────
                       100 pts

Honesty contract (hard rule): reasons only ever cite skills that are
actually in the profile or actually in the posting. Missing skills are
reported, never silently added. This module reuses
``tailor.extract_job_keywords`` and ``filters.detect_seniority`` /
``filters._annual_from_text`` rather than reimplementing them.

Stdlib only. No network.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from filters import _annual_from_text, detect_seniority
from tailor import extract_job_keywords

log = logging.getLogger("veto-mcp.match")

#: Below this score a job is vetoed ("the job bot that says no").
#:
#: With a 5/day application cap, each slot is precious: the bar is
#: "clearly worth one of your five slots" (60+), not "not terrible".
VETO_THRESHOLD = 60

_WEIGHTS = {
    "skills": 50,
    "seniority": 15,
    "salary": 15,
    "location": 15,
    "recency": 5,
}

_LEVEL_RANK = {"entry": 0, "mid": 1, "senior": 2, "staff": 3, "principal": 4}

#: Rough years-of-experience implied by a seniority level, used only to
#: add a human-readable note — never to veto.
_LEVEL_YEARS = {
    "entry": "0-2 yrs",
    "mid": "2-5 yrs",
    "senior": "5-8 yrs",
    "staff": "8+ yrs",
    "principal": "10+ yrs",
}

_REMOTE_RE = re.compile(r"\bremote\b", re.I)


def _job_text(job: dict[str, Any]) -> str:
    """All searchable text of a posting, in one blob."""
    return " ".join(
        str(job.get(k, "") or "")
        for k in ("title", "snippet", "description", "requirements", "company")
    )


def _profile_seniority(profile: dict[str, Any]) -> str:
    """Profile seniority level; defaults to 'mid' when unset/unknown."""
    level = str(profile.get("seniority") or "").strip().lower()
    return level if level in _LEVEL_RANK else "mid"


def _is_remote(job: dict[str, Any]) -> bool:
    """True when the posting is explicitly marked remote."""
    blob = " ".join(
        str(job.get(k, "") or "") for k in ("title", "location", "snippet", "description")
    )
    return bool(_REMOTE_RE.search(blob))


def _location_matches(job_location: str, wanted: list[str]) -> str | None:
    """Return the matching wanted location string, or None."""
    jl = (job_location or "").lower()
    for w in wanted:
        w = str(w or "").strip().lower()
        if w and (w in jl or jl in w):
            return w
    return None


def _score_skills(job: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str], list[str], list[str]]:
    """Skill overlap: 50 pts scaled by matched/(matched+missing).

    Missing required skills hurt — the denominator includes them. A
    posting with no detectable skill keywords scores neutrally (half),
    since absence of signal is not evidence of mismatch.
    """
    keywords = extract_job_keywords(job, profile)
    matched, missing = keywords["matched"], keywords["missing"]
    total = len(matched) + len(missing)
    reasons: list[str] = []
    if total == 0:
        reasons.append("no skill keywords detected in posting — scored neutrally")
        return _WEIGHTS["skills"] / 2, reasons, matched, missing
    score = _WEIGHTS["skills"] * len(matched) / total
    reasons.append(
        f"{len(matched)} of {total} key skills match"
        + (f": {', '.join(matched[:6])}" if matched else "")
    )
    if missing:
        reasons.append(f"missing required skills: {', '.join(missing[:6])}")
    return score, reasons, matched, missing


def _score_seniority(job: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    """Seniority fit: 15 pts from the gap between job level and profile level.

    One level above is a stretch (partial credit); two or more above is a
    near-zero. Being overqualified keeps most points — it's a fit
    question, not a hard filter.
    """
    want = _profile_seniority(profile)
    detected = detect_seniority(str(job.get("title", "") or ""))
    reasons: list[str] = []
    if detected is None:
        reasons.append("no seniority marker in title — treated as mid-level")
        detected = "mid"
    gap = _LEVEL_RANK[detected] - _LEVEL_RANK[want]
    table = {0: 15, 1: 9, 2: 4, -1: 11, -2: 7}
    if gap >= 3:
        pts, note = 0, f"{detected}-level role, you are {want}-level — likely out of reach"
    elif gap <= -3:
        pts, note = 5, f"well below your {want}-level — overqualified"
    else:
        pts = table[gap]
        note = (
            f"seniority match: {detected}-level role, you are {want}-level"
            if gap == 0
            else f"{detected}-level role vs your {want}-level"
            + (f" ({_LEVEL_YEARS[detected]} typical)" if gap > 0 else "")
        )
    reasons.append(note)
    return float(pts), reasons


def _score_salary(job: dict[str, Any], preferences: dict[str, Any]) -> tuple[float, list[str]]:
    """Salary fit: 15 pts vs the preferences salary floor.

    Recall-friendly: no floor set, or no salary hint in the posting,
    scores neutrally instead of zero.
    """
    reasons: list[str] = []
    try:
        floor = int(preferences.get("salary_min") or 0)
    except (TypeError, ValueError):
        floor = 0
    if floor <= 0:
        reasons.append("no salary floor set — scored neutrally")
        return 12.0, reasons
    annual = _annual_from_text(_job_text(job))
    if annual is None:
        reasons.append("no salary listed — not penalized")
        return 10.0, reasons
    if annual >= floor:
        reasons.append(f"lists ~${annual:,.0f}, meets your ${floor:,} floor")
        return 15.0, reasons
    if annual >= 0.9 * floor:
        reasons.append(f"lists ~${annual:,.0f}, just under your ${floor:,} floor")
        return 8.0, reasons
    reasons.append(f"lists ~${annual:,.0f}, below your ${floor:,} floor")
    return 0.0, reasons


def _score_location(
    job: dict[str, Any], profile: dict[str, Any], preferences: dict[str, Any]
) -> tuple[float, list[str]]:
    """Location/remote fit: 15 pts vs remote_only + locations preferences."""
    reasons: list[str] = []
    remote_only = bool(preferences.get("remote_only"))
    wanted = [str(x) for x in (preferences.get("locations") or []) if str(x).strip()]
    remote = _is_remote(job)
    job_loc = str(job.get("location") or "").strip()

    if remote_only:
        if remote:
            reasons.append("remote role, matches your remote-only preference")
            return 15.0, reasons
        reasons.append(f"on-site/hybrid in {job_loc or 'unknown location'} — you want remote-only")
        return 0.0, reasons

    if wanted:
        hit = _location_matches(job_loc, wanted)
        if hit:
            reasons.append(f"in {job_loc or hit} — matches your preferred locations")
            return 15.0, reasons
        if remote:
            reasons.append("remote role — location-flexible")
            return 12.0, reasons
        reasons.append(
            f"in {job_loc or 'unknown location'} — outside your preferred {', '.join(wanted[:3])}"
        )
        return 3.0, reasons

    if remote:
        reasons.append("remote role — location-flexible")
        return 13.0, reasons
    reasons.append(f"location fit unknown ({job_loc or 'no location listed'}) — scored neutrally")
    return 10.0, reasons


def _posting_age_days(job: dict[str, Any]) -> float | None:
    """Age of the posting in days, or None when undetectable."""
    if job.get("posted_days_ago") is not None:
        try:
            return float(job["posted_days_ago"])
        except (TypeError, ValueError):
            pass
    raw = str(job.get("posted_at") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except ValueError:
            pass
    return None


def _score_recency(job: dict[str, Any]) -> tuple[float, list[str]]:
    """Recency bonus: 5 pts, fresher postings score higher."""
    reasons: list[str] = []
    age = _posting_age_days(job)
    if age is None:
        reasons.append("posting date unknown — small neutral bonus")
        return 2.0, reasons
    if age <= 7:
        pts, note = 5.0, "posted within the last week"
    elif age <= 14:
        pts, note = 4.0, "posted within the last two weeks"
    elif age <= 30:
        pts, note = 3.0, "posted within the last month"
    elif age <= 60:
        pts, note = 2.0, "posted over a month ago"
    else:
        pts, note = 1.0, "stale posting (60+ days)"
    reasons.append(note)
    return pts, reasons


def _build_veto_reason(
    score: int,
    skills_pts: float,
    seniority_pts: float,
    salary_pts: float,
    location_pts: float,
    missing: list[str],
    job: dict[str, Any],
    profile: dict[str, Any],
    preferences: dict[str, Any],
) -> str:
    """Pick the concrete, human-readable reason this job was vetoed."""
    if missing and skills_pts < _WEIGHTS["skills"] * 0.35:
        return f"skill gap: missing {', '.join(missing[:4])} required by this posting"
    if salary_pts == 0:
        try:
            floor = int(preferences.get("salary_min") or 0)
        except (TypeError, ValueError):
            floor = 0
        annual = _annual_from_text(_job_text(job))
        if floor and annual:
            return f"pay (~${annual:,.0f}) is below your ${floor:,} floor"
    want = _profile_seniority(profile)
    detected = detect_seniority(str(job.get("title", "") or "")) or "mid"
    if seniority_pts <= 4 and _LEVEL_RANK[detected] - _LEVEL_RANK[want] >= 2:
        return f"seniority mismatch: {detected}-level role, you are {want}-level"
    if location_pts == 0:
        return "location mismatch: not remote and outside your preferred locations"
    return f"overall fit {score}/100 is below the {VETO_THRESHOLD} veto bar"


def score_job(
    job: dict[str, Any],
    profile: dict[str, Any],
    preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score a single job posting 0-100 with reasons.

    Args:
        job: posting with title/snippet/description/requirements/company/
            location/board (any subset; missing fields score neutrally).
        profile: candidate profile with skills, seniority (entry|mid|
            senior|staff|principal, default 'mid'), location, etc.
        preferences: user preferences; honors ``salary_min``,
            ``remote_only``, ``locations``. ``None`` means defaults.

    Returns:
        {"score", "reasons", "matched", "missing", "veto",
         "veto_reason", "components"} where ``components`` breaks the
        score down by skills/seniority/salary/location/recency.
        ``veto`` is True (with ``veto_reason`` set) when the score is
        under 60.
    """
    preferences = preferences or {}
    reasons: list[str] = []

    skills_pts, skill_reasons, matched, missing = _score_skills(job, profile)
    seniority_pts, seniority_reasons = _score_seniority(job, profile)
    salary_pts, salary_reasons = _score_salary(job, preferences)
    location_pts, location_reasons = _score_location(job, profile, preferences)
    recency_pts, recency_reasons = _score_recency(job)

    reasons.extend(skill_reasons + seniority_reasons + salary_reasons + location_reasons + recency_reasons)

    score = int(
        round(skills_pts + seniority_pts + salary_pts + location_pts + recency_pts)
    )
    score = max(0, min(100, score))

    components = {
        "skills": round(skills_pts, 1),
        "seniority": round(seniority_pts, 1),
        "salary": round(salary_pts, 1),
        "location": round(location_pts, 1),
        "recency": round(recency_pts, 1),
    }

    veto = score < VETO_THRESHOLD
    veto_reason = (
        _build_veto_reason(
            score, skills_pts, seniority_pts, salary_pts, location_pts,
            missing, job, profile, preferences,
        )
        if veto
        else None
    )
    if veto:
        reasons.insert(0, f"VETOED: {veto_reason}")

    log.info(
        "scored %r @ %r: %d/100 veto=%s",
        job.get("title"), job.get("company"), score, veto,
    )
    return {
        "score": score,
        "reasons": reasons,
        "matched": matched,
        "missing": missing,
        "veto": veto,
        "veto_reason": veto_reason,
        "components": components,
    }


def rank_jobs(
    jobs: list[dict[str, Any]],
    profile: dict[str, Any],
    preferences: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Score every job and return them sorted by score, highest first.

    Each returned dict is a copy of the input job annotated with
    ``score``, ``reasons``, ``matched``, ``missing``, ``veto``,
    ``veto_reason``, and ``components``. Ties keep input order (stable).
    """
    scored: list[dict[str, Any]] = []
    for job in jobs:
        result = score_job(job, profile, preferences)
        annotated = dict(job)
        annotated.update(result)
        scored.append(annotated)
    scored.sort(key=lambda j: j["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Plugin wiring (MCP + CLI)
# ---------------------------------------------------------------------------

def _load_json_file(path: str) -> Any:
    import json

    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def register_tools(mcp):  # noqa: ANN001, ANN202 - duck-typed FastMCP
    """Register the fit-score tools on a FastMCP instance."""
    # Capture module-level cores first: the wrappers below reuse the names.
    _core_score = globals()["score_job"]
    _core_rank = globals()["rank_jobs"]

    @mcp.tool()
    def score_job_posting(  # noqa: F811 - intentional tool wrapper
        job: dict, profile: dict, preferences: dict | None = None
    ) -> dict:
        """Score one job posting 0-100 with human-readable reasons.

        Weights: skills 50, seniority 15, salary 15, location 15,
        recency 5. Anything under 60 is vetoed with a concrete reason.

        Args:
            job: posting with title/snippet/description/requirements/
                company/location/board (any subset).
            profile: candidate profile (skills, seniority, location...).
            preferences: optional user preferences (salary_min,
                remote_only, locations).
        """
        return _core_score(job, profile, preferences)

    @mcp.tool()
    def rank_job_postings(  # noqa: F811 - intentional tool wrapper
        jobs: list, profile: dict, preferences: dict | None = None
    ) -> list:
        """Score every posting and return them sorted, best first.

        Each posting is annotated with score, reasons, matched/missing
        skills, veto flag, veto_reason, and per-axis components.
        """
        return _core_rank(jobs, profile, preferences)


def _cli_match(args: Any) -> int:
    """``veto match`` implementation."""
    import json

    if not args.job_file and not args.jobs_file:
        print("error: one of --job-file or --jobs-file is required", flush=True)
        return 2
    profile = _load_json_file(args.profile_file)
    preferences = _load_json_file(args.preferences_file) if args.preferences_file else None
    if args.jobs_file:
        jobs = _load_json_file(args.jobs_file)
        ranked = rank_jobs(jobs, profile, preferences)
        if args.top:
            ranked = ranked[: args.top]
        payload: Any = ranked
    else:
        job = _load_json_file(args.job_file)
        payload = score_job(job, profile, preferences)
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            title = item.get("title", "?")
            company = item.get("company", "?")
            score = item.get("score", 0)
            veto = " [VETOED]" if item.get("veto") else ""
            print(f"{score:3.0f}  {title} @ {company}{veto}")
            for reason in item.get("reasons", [])[:4]:
                print(f"      - {reason}")
            if item.get("veto_reason"):
                print(f"      ! {item['veto_reason']}")
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``match`` subcommand; return {command: handler}."""
    parser = subparsers.add_parser(
        "match", help="Score and rank job postings by fit (0-100, veto under 60)."
    )
    parser.add_argument(
        "--job-file", default="", help="JSON file with one posting (score mode)."
    )
    parser.add_argument(
        "--jobs-file", default="", help="JSON file with a list of postings (rank mode)."
    )
    parser.add_argument(
        "--profile-file", default="profiles/profile.json",
        help="Candidate profile JSON.",
    )
    parser.add_argument(
        "--preferences-file", default="",
        help="Optional preferences JSON (salary_min, remote_only, locations).",
    )
    parser.add_argument(
        "--top", type=int, default=0, help="Show only the top N postings (rank mode)."
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return {"match": _cli_match}
