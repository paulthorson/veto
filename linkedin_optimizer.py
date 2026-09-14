#!/usr/bin/env python3
"""LinkedIn profile optimizer for the job-apply MCP server.

Deterministic, stdlib-only analysis and rewrite suggestions:

``audit_profile(profile, target_role=None)``
    0–100 sub-scores for headline, about section, experience bullets,
    and skills coverage, each with specific fix suggestions.

``rewrite_headline(profile, target_role)`` / ``rewrite_about(profile)``
    Suggested rewrites assembled ONLY from facts already present in the
    profile. Quantified claims appear only where the profile already
    contains numbers. Titles and employers are never invented: when the
    profile has no experience, the headline falls back to the user's own
    ``target_titles`` (their stated aspiration) or the explicitly passed
    ``target_role`` — never a fabricated job.

``keyword_gap(profile, target_role)``
    Keywords typical for the target role's postings that are missing
    from the profile, ranked by importance.

Honesty contract (hard rule): every suggestion reuses the candidate's
own facts and words. The module never invents achievements, titles,
employers, or numbers.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.linkedin_optimizer")

BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "profiles" / "profile.json"

#: LinkedIn headline character limit.
HEADLINE_LIMIT = 220

# ---------------------------------------------------------------------------
# Role keyword sets: (keyword, importance 1-3) — terms that typically appear
# in postings for the role and that recruiters search for.
# ---------------------------------------------------------------------------

ROLE_KEYWORDS: dict[str, list[tuple[str, int]]] = {
    "engineer": [
        ("python", 3), ("system design", 3), ("aws", 3), ("kubernetes", 3),
        ("microservices", 2), ("distributed systems", 2), ("ci/cd", 2),
        ("testing", 2), ("docker", 2), ("sql", 2), ("rest apis", 2),
        ("typescript", 2), ("monitoring", 1), ("git", 1), ("linux", 1),
    ],
    "product": [
        ("roadmap", 3), ("product strategy", 3), ("stakeholder management", 3),
        ("a/b testing", 2), ("metrics", 2), ("user research", 2),
        ("prioritization", 2), ("agile", 2), ("go-to-market", 2),
        ("analytics", 2), ("sql", 1), ("wireframing", 1),
    ],
    "data": [
        ("sql", 3), ("python", 3), ("machine learning", 2), ("statistics", 2),
        ("a/b testing", 2), ("experimentation", 2), ("data modeling", 2),
        ("pandas", 2), ("dashboards", 2), ("dbt", 2), ("airflow", 1),
        ("tableau", 1),
    ],
    "design": [
        ("figma", 3), ("user research", 2), ("prototyping", 2),
        ("design systems", 2), ("wireframing", 2), ("usability testing", 2),
        ("interaction design", 2), ("visual design", 1), ("accessibility", 1),
    ],
    "sales": [
        ("prospecting", 3), ("pipeline management", 3), ("negotiation", 3),
        ("quota", 2), ("outbound", 2), ("discovery calls", 2),
        ("closing", 2), ("salesforce", 2), ("crm", 2),
    ],
    "marketing": [
        ("seo", 3), ("content marketing", 2), ("email marketing", 2),
        ("analytics", 2), ("campaign management", 2), ("paid media", 2),
        ("growth", 2), ("copywriting", 2), ("hubspot", 1), ("social media", 1),
    ],
}

_ROLE_ALIASES: dict[str, str] = {
    "software engineer": "engineer", "backend": "engineer",
    "frontend": "engineer", "fullstack": "engineer", "full-stack": "engineer",
    "devops": "engineer", "swe": "engineer", "developer": "engineer",
    "engineering": "engineer",
    "pm": "product", "product manager": "product",
    "product owner": "product",
    "data scientist": "data", "data analyst": "data", "data engineer": "data",
    "ml engineer": "data", "analytics": "data",
    "ux": "design", "ui": "design", "ux designer": "design",
    "product designer": "design", "ui designer": "design",
    "ae": "sales", "account executive": "sales", "sdr": "sales",
    "account manager": "sales", "bdr": "sales",
    "growth": "marketing", "content": "marketing",
    "demand gen": "marketing", "brand": "marketing",
}

_ACTION_VERBS = {
    "led", "built", "designed", "launched", "shipped", "drove", "delivered",
    "grew", "reduced", "increased", "improved", "managed", "created",
    "developed", "owned", "scaled", "architected", "spearheaded",
    "streamlined", "automated", "mentored", "negotiated", "partnered",
    "pioneered", "revamped", "transformed", "founded", "established",
    "doubled", "tripled", "cut", "saved", "generated", "closed",
    "implemented", "migrated", "optimized", "redesigned", "rewrote",
}

_NUMBER_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:%|percent|x|X|\+|k|K|m|M|million|billion)?"
)
_FIRST_PERSON_RE = re.compile(r"\b(I|I'm|I've|I'll|my|me|we|our)\b", re.IGNORECASE)
_CTA_RE = re.compile(
    r"\b(connect|reach out|open to|dm me|message me|let's talk|happy to chat)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Profile helpers
# ---------------------------------------------------------------------------


def _load_profile() -> dict[str, Any]:
    """Load the saved profile; {} when absent (never raises)."""
    try:
        return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - absent profile is normal
        log.warning("linkedin_optimizer: no saved profile: %s", exc)
        return {}


def _profile_skills(profile: dict[str, Any]) -> list[str]:
    skills = profile.get("skills") or []
    return [str(s) for s in skills if str(s).strip()]


def _profile_blob(profile: dict[str, Any]) -> str:
    """All profile text, used for keyword presence checks."""
    parts: list[str] = []
    for key in ("headline", "summary"):
        if profile.get(key):
            parts.append(str(profile[key]))
    parts.extend(_profile_skills(profile))
    for exp in profile.get("experience", []) or []:
        if not isinstance(exp, dict):
            continue
        for key in ("title", "company", "dates"):
            if exp.get(key):
                parts.append(str(exp[key]))
        parts.extend(str(b) for b in exp.get("bullets", []) or [])
    for edu in profile.get("education", []) or []:
        if not isinstance(edu, dict):
            continue
        for key in ("degree", "school", "dates"):
            if edu.get(key):
                parts.append(str(edu[key]))
    return "\n".join(parts)


def _bullets(profile: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for exp in profile.get("experience", []) or []:
        if isinstance(exp, dict):
            out.extend(
                str(b).strip()
                for b in exp.get("bullets", []) or []
                if str(b).strip()
            )
    return out


def _latest_experience(profile: dict[str, Any]) -> dict[str, Any]:
    for exp in profile.get("experience", []) or []:
        if isinstance(exp, dict):
            return exp
    return {}


def resolve_role(target_role: str | None) -> str | None:
    """Map free-text role input to a canonical role key (or None)."""
    if not target_role:
        return None
    text = target_role.strip().lower()
    if text in ROLE_KEYWORDS:
        return text
    for alias, canonical in _ROLE_ALIASES.items():
        if alias in text:
            return canonical
    return None


def _contains(haystack: str, needle: str) -> bool:
    return (
        re.search(
            r"(?<![a-z0-9+#])" + re.escape(needle) + r"(?![a-z0-9+#])",
            haystack,
            re.IGNORECASE,
        )
        is not None
    )


# ---------------------------------------------------------------------------
# 1. Audit
# ---------------------------------------------------------------------------


def _score_headline(
    profile: dict[str, Any], role: str | None
) -> dict[str, Any]:
    headline = str(profile.get("headline") or "").strip()
    findings: list[str] = []
    fixes: list[str] = []
    if not headline:
        return {
            "score": 0,
            "findings": ["No headline set."],
            "fixes": [
                "Add a headline — it is the most-searched field on LinkedIn."
            ],
        }
    score = 25  # has a headline
    findings.append(f"Headline present ({len(headline)} chars).")
    if len(headline) <= HEADLINE_LIMIT:
        score += 10
    else:
        fixes.append(
            f"Headline is {len(headline)} chars; LinkedIn truncates at "
            f"{HEADLINE_LIMIT} — trim it."
        )
    titles = [
        str(t).lower()
        for t in (profile.get("target_titles") or [])
        if str(t).strip()
    ]
    titles += [
        str(e.get("title", "")).lower()
        for e in profile.get("experience", []) or []
        if isinstance(e, dict) and e.get("title")
    ]
    if any(t and t in headline.lower() for t in titles):
        score += 25
        findings.append("Role title is stated explicitly.")
    else:
        fixes.append(
            "Name your role explicitly "
            f"({' / '.join(profile.get('target_titles') or ['your target title'])}) "
            "so recruiters find you."
        )
    vocab = (
        [kw for kw, _ in ROLE_KEYWORDS[role]]
        if role
        else [s.lower() for s in _profile_skills(profile)]
    )
    hits = [kw for kw in vocab if _contains(headline, kw)]
    score += min(30, len(hits) * 10)
    findings.append(f"{len(hits)} searchable keyword(s) in headline.")
    if len(hits) < 2:
        fixes.append(
            "Add 2–3 searchable skills to the headline "
            "(e.g. separated by | or ·)."
        )
    if "|" in headline or "·" in headline:
        score += 10
    return {
        "score": min(100, score),
        "findings": findings,
        "fixes": fixes,
    }


def _score_about(profile: dict[str, Any]) -> dict[str, Any]:
    about = str(profile.get("summary") or "").strip()
    findings: list[str] = []
    fixes: list[str] = []
    if not about:
        return {
            "score": 0,
            "findings": ["No about section."],
            "fixes": [
                "Write an about section (300–1500 chars): who you are, "
                "proof of impact, what you are looking for."
            ],
        }
    score = 0
    n = len(about)
    findings.append(f"About section present ({n} chars).")
    if 300 <= n <= 2000:
        score += 35
    elif 150 <= n < 300:
        score += 20
        fixes.append(
            "About section is thin — expand toward 300+ chars with "
            "a concrete result or two."
        )
    elif n < 150:
        score += 10
        fixes.append("About section is very short — add specifics.")
    else:
        score += 25
        fixes.append("About section is long — tighten the opening 3 lines.")
    if _FIRST_PERSON_RE.search(about):
        score += 25
        findings.append("First-person voice detected.")
    else:
        fixes.append("Write in first person (I / my) — it reads as human.")
    if _NUMBER_RE.search(about):
        score += 25
        findings.append("Quantified proof present.")
    else:
        fixes.append(
            "Add at least one quantified result "
            "(users, revenue, latency, team size…)."
        )
    if _CTA_RE.search(about):
        score += 15
        findings.append("Call to action present.")
    else:
        fixes.append("End with a call to action ('open to … roles').")
    return {"score": min(100, score), "findings": findings, "fixes": fixes}


def _score_bullets(profile: dict[str, Any]) -> dict[str, Any]:
    bullets = _bullets(profile)
    findings: list[str] = []
    fixes: list[str] = []
    if not bullets:
        return {
            "score": 15,
            "findings": ["No experience bullets found."],
            "fixes": [
                "Add experience with bullets — profiles with quantified "
                "bullets get far more recruiter outreach."
            ],
        }
    score = 0
    action_starts = sum(
        1
        for b in bullets
        if (b.split() or [""])[0].strip(",.").lower() in _ACTION_VERBS
    )
    quantified = sum(1 for b in bullets if _NUMBER_RE.search(b))
    action_ratio = action_starts / len(bullets)
    quant_ratio = quantified / len(bullets)
    score += round(action_ratio * 40)
    score += round(quant_ratio * 40)
    findings.append(
        f"{action_starts}/{len(bullets)} bullets start with an action verb; "
        f"{quantified}/{len(bullets)} are quantified."
    )
    if action_ratio < 0.6:
        fixes.append(
            "Start more bullets with strong action verbs "
            "(Led, Built, Shipped, Drove…)."
        )
    if quant_ratio < 0.5:
        fixes.append(
            "Quantify more bullets — add numbers (%, $, users, time saved) "
            "wherever they already exist in your records."
        )
    avg_len = sum(len(b) for b in bullets) / len(bullets)
    if 60 <= avg_len <= 220:
        score += 20
    else:
        score += 10
        fixes.append(
            "Aim for one-line bullets (~60–220 chars); "
            "split or tighten outliers."
        )
    return {"score": min(100, score), "findings": findings, "fixes": fixes}


def _score_skills(
    profile: dict[str, Any], role: str | None
) -> dict[str, Any]:
    skills = _profile_skills(profile)
    findings: list[str] = []
    fixes: list[str] = []
    blob = _profile_blob(profile).lower()
    if role:
        keywords = ROLE_KEYWORDS[role]
        matched = [kw for kw, _ in keywords if _contains(blob, kw)]
        missing = [kw for kw, _ in keywords if kw not in matched]
        score = round(len(matched) / len(keywords) * 100)
        findings.append(
            f"{len(matched)}/{len(keywords)} '{role}' keywords present."
        )
        if missing:
            top = [kw for kw, _ in keywords if kw in missing][:5]
            fixes.append(
                "Missing high-value keywords: " + ", ".join(top) + ". "
                "Add the ones you genuinely have to Skills and use them "
                "in experience bullets."
            )
        return {"score": score, "findings": findings, "fixes": fixes}
    n = len(skills)
    findings.append(f"{n} skills listed.")
    if n >= 15:
        score = 100
    elif n >= 10:
        score = 80
    elif n >= 5:
        score = 60
    elif n >= 1:
        score = 40
        fixes.append("Add more skills — aim for 10–15 core ones.")
    else:
        score = 0
        fixes.append("Add a Skills section — recruiters filter on it.")
    return {"score": score, "findings": findings, "fixes": fixes}


def audit_profile(
    profile: dict[str, Any], target_role: str | None = None
) -> dict[str, Any]:
    """Score a LinkedIn profile 0–100 across four sections.

    Returns {"overall", "target_role", "sections": {name: {score,
    findings, fixes}}}. Pure function of the profile dict — never
    raises on missing keys.
    """
    profile = profile or {}
    role = resolve_role(target_role)
    sections = {
        "headline": _score_headline(profile, role),
        "about": _score_about(profile),
        "experience_bullets": _score_bullets(profile),
        "skills_coverage": _score_skills(profile, role),
    }
    overall = round(
        sum(s["score"] for s in sections.values()) / len(sections)
    )
    all_fixes = [
        fix
        for s in sections.values()
        for fix in s["fixes"]
    ]
    return {
        "overall": overall,
        "target_role": role,
        "sections": sections,
        "top_fixes": all_fixes[:5],
    }


# ---------------------------------------------------------------------------
# 2. Rewrites (profile facts only — never invented)
# ---------------------------------------------------------------------------


def _role_keyword_hits(profile: dict[str, Any], role: str | None) -> list[str]:
    """Profile skills that are also role keywords (or top skills)."""
    skills = _profile_skills(profile)
    if role:
        wanted = {kw for kw, _ in ROLE_KEYWORDS[role]}
        hits = [s for s in skills if s.lower() in wanted]
        if hits:
            return hits[:4]
    return skills[:4]


def rewrite_headline(
    profile: dict[str, Any], target_role: str | None = None
) -> dict[str, Any]:
    """Suggest a LinkedIn headline built only from profile facts.

    Uses the latest experience title, years of experience, matching
    skills, and latest employer — all from the profile. When the profile
    has no experience title, it falls back to the user's own
    ``target_titles`` (their stated aspiration), never an invented job.
    """
    profile = profile or {}
    role = resolve_role(target_role)
    latest = _latest_experience(profile)
    built_from: list[str] = []
    parts: list[str] = []

    title = str(latest.get("title") or "").strip()
    if not title:
        targets = [
            str(t) for t in (profile.get("target_titles") or []) if str(t).strip()
        ]
        if targets:
            title = targets[0]
            built_from.append(f"target_titles[0]={title!r} (your stated goal)")
        elif target_role:
            title = f"Aspiring {target_role.strip()}"
            built_from.append(
                f"target_role={target_role.strip()!r} (your stated goal)"
            )
    if title and not title.startswith("Aspiring"):
        parts.append(title)
        built_from.append(f"experience title={title!r}")
    elif title:
        parts.append(title)

    years = profile.get("years_experience") or profile.get("years_of_experience")
    if isinstance(years, (int, float)) and years > 0:
        yrs = int(years)
        parts.append(f"{yrs} yrs")
        built_from.append(f"years_experience={yrs}")

    skill_hits = _role_keyword_hits(profile, role)
    if skill_hits:
        parts.append(", ".join(skill_hits))
        built_from.append(f"skills={skill_hits}")

    company = str(latest.get("company") or "").strip()
    if company:
        parts.append(f"ex-{company}")
        built_from.append(f"experience company={company!r}")

    headline = " | ".join(p for p in parts if p)
    note = ""
    if len(headline) > HEADLINE_LIMIT:
        # Trim skill list first — never drop the title.
        while len(headline) > HEADLINE_LIMIT and len(parts) > 2:
            parts.pop(1)
            headline = " | ".join(p for p in parts if p)
        note = "Trimmed to fit LinkedIn's 220-char limit."
    if not headline:
        headline = "Add your profile details to generate a headline."
        note = "Profile has no title, skills, or target titles to work with."
    return {
        "headline": headline,
        "built_from": built_from,
        "note": note,
        "char_count": len(headline),
    }


def _quantified_bullets(profile: dict[str, Any]) -> list[str]:
    """The user's own bullets that already contain numbers (verbatim)."""
    return [b for b in _bullets(profile) if _NUMBER_RE.search(b)][:3]


def rewrite_about(profile: dict[str, Any]) -> dict[str, Any]:
    """Draft an about section from profile facts only.

    First-person framing sentences reference only titles, companies,
    skills, and education present in the profile. Proof lines reuse the
    user's own quantified bullets verbatim — numbers are never created.
    """
    profile = profile or {}
    latest = _latest_experience(profile)
    built_from: list[str] = []
    paras: list[str] = []

    first = str(profile.get("first_name") or "").strip()
    title = str(latest.get("title") or "").strip()
    targets = [
        str(t) for t in (profile.get("target_titles") or []) if str(t).strip()
    ]
    years = profile.get("years_experience") or profile.get("years_of_experience")

    opener_bits: list[str] = []
    if first:
        opener_bits.append(f"I'm {first}")
    else:
        opener_bits.append("I'm")
    role_phrase = title or (targets[0] if targets else "a professional")
    opener_bits.append(role_phrase)
    if isinstance(years, (int, float)) and years > 0:
        opener_bits.append(f"with {int(years)} years of experience")
        built_from.append(f"years_experience={int(years)}")
    opener = " ".join(opener_bits) + "."
    if title:
        built_from.append(f"experience title={title!r}")
    elif targets:
        built_from.append(f"target_titles[0]={targets[0]!r} (your stated goal)")

    skills = _profile_skills(profile)[:8]
    if skills:
        opener += f" My core toolkit: {', '.join(skills)}."
        built_from.append(f"skills={skills}")
    paras.append(opener)

    company = str(latest.get("company") or "").strip()
    if company and title:
        paras.append(
            f"Most recently, I was {title} at {company}."
        )
        built_from.append(f"experience company={company!r}")
    elif company:
        paras.append(f"Most recently, I worked at {company}.")
        built_from.append(f"experience company={company!r}")

    proof = _quantified_bullets(profile)
    if proof:
        paras.append(
            "Selected results:\n" + "\n".join(f"- {b}" for b in proof)
        )
        built_from.append(f"{len(proof)} quantified bullet(s), reused verbatim")
    else:
        paras.append(
            "Selected results: (add a quantified bullet to your experience — "
            "e.g. users served, revenue influenced, or time saved — and it "
            "will appear here automatically.)"
        )

    edu = profile.get("education") or []
    if edu and isinstance(edu[0], dict):
        degree = str(edu[0].get("degree") or "").strip()
        school = str(edu[0].get("school") or "").strip()
        if degree or school:
            paras.append(f"Education: {' — '.join(x for x in (degree, school) if x)}.")
            built_from.append(f"education={degree!r} @ {school!r}")

    if targets:
        paras.append(
            f"Open to {', '.join(targets[:3])} roles — "
            "happy to connect if you're hiring or just want to trade notes."
        )
    else:
        paras.append("Happy to connect.")

    about = "\n\n".join(paras)
    return {
        "about": about,
        "built_from": built_from,
        "note": (
            "Draft only — every claim traces to a profile fact listed in "
            "'built_from'. Edit in your own voice before publishing."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Keyword gap
# ---------------------------------------------------------------------------


def keyword_gap(
    profile: dict[str, Any], target_role: str | None = None
) -> dict[str, Any]:
    """Keywords typical for the target role missing from the profile.

    Returns missing keywords ranked by importance with a placement tip.
    Never suggests inventing experience — tips say to add only keywords
    the candidate genuinely has.
    """
    profile = profile or {}
    role = resolve_role(target_role)
    if role is None:
        return {
            "target_role": None,
            "missing": [],
            "note": (
                "Pass a target role (engineer, product, data, design, "
                "sales, marketing) to get a keyword gap analysis."
            ),
        }
    blob = _profile_blob(profile)
    missing: list[dict[str, Any]] = []
    for keyword, importance in ROLE_KEYWORDS[role]:
        if not _contains(blob, keyword):
            missing.append(
                {
                    "keyword": keyword,
                    "importance": importance,
                    "tip": (
                        "Add to Skills and use in an experience bullet — "
                        "only if you genuinely have it."
                    ),
                }
            )
    missing.sort(key=lambda m: (-m["importance"], m["keyword"]))
    return {
        "target_role": role,
        "missing": missing,
        "matched_count": len(ROLE_KEYWORDS[role]) - len(missing),
        "total_keywords": len(ROLE_KEYWORDS[role]),
    }


# ---------------------------------------------------------------------------
# Plugin wiring (briefs.py pattern)
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the LinkedIn optimizer MCP tools on a server instance."""
    _impl_audit = globals()["audit_profile"]
    _impl_headline = globals()["rewrite_headline"]
    _impl_about = globals()["rewrite_about"]
    _impl_gap = globals()["keyword_gap"]

    @mcp.tool()
    def linkedin_audit(target_role: str = "") -> dict:
        """Audit a LinkedIn profile: 0-100 sub-scores + fix suggestions.

        Args:
            target_role: Role to score against, e.g. "engineer".

        Returns:
            Dict with overall score, per-section scores/findings/fixes,
            and top fixes. Reads the saved profile.
        """
        return _impl_audit(_load_profile(), target_role or None)

    @mcp.tool()
    def linkedin_headline(target_role: str = "") -> dict:
        """Suggest a LinkedIn headline built only from profile facts.

        Args:
            target_role: Target role, e.g. "Senior Backend Engineer".

        Returns:
            Dict with the suggested headline, the facts it was built
            from, and a note. Nothing is invented.
        """
        return _impl_headline(_load_profile(), target_role or None)

    @mcp.tool()
    def linkedin_about() -> dict:
        """Draft a LinkedIn about section from profile facts only.

        Returns:
            Dict with the draft, the facts it was built from, and a
            note. Quantified claims reuse the profile's own numbers.
        """
        return _impl_about(_load_profile())

    @mcp.tool()
    def linkedin_keyword_gap(target_role: str = "") -> dict:
        """Keywords typical for the target role missing from the profile.

        Args:
            target_role: Role key, e.g. "engineer", "product", "data".

        Returns:
            Dict with missing keywords ranked by importance. Reads the
            saved profile.
        """
        return _impl_gap(_load_profile(), target_role or None)


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        text = (
            result.get("headline")
            or result.get("about")
            or json.dumps(result, indent=2)
        )
        print(text)


def cmd_linkedin(args: Any) -> int:
    """CLI handler for `linkedin`."""
    profile = _load_profile()
    target_role = getattr(args, "target_role", "") or None
    action = args.action
    if action == "audit":
        result = audit_profile(profile, target_role)
    elif action == "headline":
        result = rewrite_headline(profile, target_role)
    elif action == "about":
        result = rewrite_about(profile)
    elif action == "keywords":
        result = keyword_gap(profile, target_role)
    else:  # pragma: no cover - argparse constrains choices
        raise SystemExit(f"unknown action: {action}")
    _print_result(result, getattr(args, "json", False))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `linkedin` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    parser = subparsers.add_parser(
        "linkedin", help="Audit and optimize your LinkedIn profile."
    )
    parser.add_argument(
        "action",
        choices=["audit", "headline", "about", "keywords"],
        help="audit: score the profile; headline/about: suggested rewrites; "
        "keywords: missing role keywords.",
    )
    parser.add_argument(
        "--target-role",
        default="",
        help='Target role, e.g. "engineer", "product", "data".',
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    return {"linkedin": cmd_linkedin}
