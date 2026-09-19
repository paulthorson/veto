#!/usr/bin/env python3
"""Per-job application tailoring: keyword-matched resume + cover letter.

Pure local string logic — no LLM calls, no network.

Honesty contract (hard rule): this module NEVER invents experience,
titles, companies, dates, or skills. It only rephrases, reorders, and
selects from what already exists in the applicant's profile. Skills the
job asks for but the profile lacks are reported in ``missing_skills``
and are NOT added to the resume.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.tailor")

# Built-in vocabulary for keyword extraction. The profile's own skills are
# always added to this set, so niche skills still get matched.
_SKILL_VOCABULARY = {
    # languages / runtimes
    "python", "java", "javascript", "typescript", "go", "golang", "rust",
    "c++", "c#", "ruby", "php", "swift", "kotlin", "scala", "sql",
    # web / frontend
    "react", "angular", "vue", "next.js", "node.js", "nodejs", "html",
    "css", "django", "flask", "fastapi", "spring", "rails",
    # data / ml
    "machine learning", "deep learning", "pandas", "numpy", "spark",
    "tensorflow", "pytorch", "nlp", "etl", "airflow", "dbt", "tableau",
    "power bi", "excel",
    # infra / devops
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
    "jenkins", "github actions", "ci/cd", "linux", "nginx",
    # practices
    "agile", "scrum", "rest", "graphql", "microservices", "tdd",
    "a/b testing", "system design", "distributed systems",
    # general professional
    "leadership", "mentoring", "communication", "stakeholder",
    "project management", "product management",
}


def _profile_skills(profile: dict[str, Any]) -> list[str]:
    raw = profile.get("skills", []) or []
    skills: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name") or item.get("skill")
        else:
            name = item
        if name:
            skills.append(str(name).strip())
    # De-dupe case-insensitively, keep first-seen casing.
    seen: set[str] = set()
    out: list[str] = []
    for s in skills:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _profile_text_blob(profile: dict[str, Any]) -> str:
    """All profile text, used to verify a claim really exists in it."""
    parts: list[str] = []
    for key in ("summary", "headline"):
        if profile.get(key):
            parts.append(str(profile[key]))
    for exp in profile.get("experience", []) or []:
        if isinstance(exp, dict):
            parts.append(" ".join(str(exp.get(k, "") or "") for k in ("title", "company", "dates")))
            parts.extend(str(b) for b in exp.get("bullets", []) or [])
    for edu in profile.get("education", []) or []:
        if isinstance(edu, dict):
            parts.append(" ".join(str(edu.get(k, "") or "") for k in ("degree", "school", "dates")))
    return "\n".join(parts)


def extract_job_keywords(
    job_details: dict[str, Any], profile: dict[str, Any]
) -> dict[str, list[str]]:
    """Split job-description keywords into matched vs missing skills.

    Returns {"matched": [...], "missing": [...]} ordered by frequency in
    the posting. A skill counts as matched only if it appears in the
    profile's own skill list (case-insensitive).
    """
    text = " ".join(
        str(job_details.get(k, "") or "")
        for k in ("title", "description", "requirements", "snippet")
    ).lower()
    vocab = set(_SKILL_VOCABULARY) | {s.lower() for s in _profile_skills(profile)}
    profile_skill_set = {s.lower() for s in _profile_skills(profile)}

    counts: dict[str, int] = {}
    for term in vocab:
        # Word-boundary match so "go" doesn't match "going".
        hits = len(re.findall(r"(?<![a-z0-9+#])" + re.escape(term) + r"(?![a-z0-9+#])", text))
        if hits:
            counts[term] = hits

    # Restore the profile's original casing for matched skills.
    casing = {s.lower(): s for s in _profile_skills(profile)}
    matched = sorted(
        (casing.get(t, t) for t in counts if t in profile_skill_set),
        key=lambda t: -counts[t.lower()],
    )
    missing = sorted(
        (t for t in counts if t not in profile_skill_set),
        key=lambda t: -counts[t],
    )
    return {"matched": matched, "missing": missing}


def _bullet_score(bullet: str, keywords: list[str]) -> int:
    low = bullet.lower()
    return sum(1 for kw in keywords if kw.lower() in low)


def _render_resume_md(profile: dict[str, Any], matched: list[str]) -> str:
    """Render the full resume markdown.

    Shared by ``tailor_resume`` and ``base_resume_md`` so the untailored
    base resume is byte-identical in format to the tailored one (it is
    simply the rendering with no matched keywords). Facts are never
    rewritten — only bullet ordering and the keyword-driven summary
    fallback / Skills section depend on ``matched``.
    """
    name = str(profile.get("full_name") or profile.get("name") or "").strip()
    contact_bits = [
        str(profile.get(k, "") or "").strip()
        for k in ("email", "phone", "location")
    ]
    contact_bits = [b for b in contact_bits if b]
    linkedin = str(profile.get("linkedin_url") or "").strip()

    lines: list[str] = [f"# {name}" if name else "# Resume"]
    if contact_bits:
        lines.append(" | ".join(contact_bits))
    if linkedin:
        lines.append(linkedin)
    lines.append("")

    summary = str(profile.get("summary") or "").strip()
    if not summary:
        titles = profile.get("target_titles") or profile.get("target_roles") or []
        years = profile.get("years_experience") or profile.get("years_of_experience")
        bits = []
        if years:
            bits.append(f"{years} years of experience")
        if titles:
            t = titles[0] if isinstance(titles, list) else titles
            bits.append(f"targeting {t} roles")
        if matched:
            bits.append(f"skilled in {', '.join(matched[:5])}")
        summary = ("Professional with " + ", ".join(bits) + ".") if bits else ""
    if summary:
        lines += ["## Summary", summary, ""]

    lines.append("## Experience")
    for exp in profile.get("experience", []) or []:
        if not isinstance(exp, dict):
            continue
        header = " — ".join(
            p for p in (exp.get("title"), exp.get("company")) if p
        )
        dates = str(exp.get("dates") or "").strip()
        lines.append(f"### {header}" + (f"  \n*{dates}*" if dates else ""))
        bullets = [str(b) for b in exp.get("bullets", []) or [] if b]
        # Front-load bullets that mention the job's matched keywords.
        # Facts are untouched; only ordering changes.
        bullets.sort(key=lambda b: -_bullet_score(b, matched))
        for b in bullets:
            lines.append(f"- {b}")
        lines.append("")

    education = profile.get("education", []) or []
    if education:
        lines.append("## Education")
        for edu in education:
            if isinstance(edu, dict):
                deg = " — ".join(p for p in (edu.get("degree"), edu.get("school")) if p)
                dates = str(edu.get("dates") or "").strip()
                lines.append(f"- {deg}" + (f" ({dates})" if dates else ""))
            else:
                lines.append(f"- {edu}")
        lines.append("")

    if matched:
        lines += ["## Skills", ", ".join(matched), ""]

    return "\n".join(lines).strip() + "\n"


def tailor_resume(
    profile: dict[str, Any], job_details: dict[str, Any]
) -> dict[str, Any]:
    """Build a keyword-tuned resume + cover letter from the profile.

    Reorders (never rewrites the facts of) experience bullets so bullets
    mentioning the job's matched keywords come first. Returns
    {"resume_md", "cover_letter", "matched_skills", "missing_skills"}.
    """
    keywords = extract_job_keywords(job_details, profile)
    matched = keywords["matched"]

    name = str(profile.get("full_name") or profile.get("name") or "").strip()
    resume_md = _render_resume_md(profile, matched)

    # --- cover letter: 3 paragraphs, profile facts only ---
    company = str(job_details.get("company") or "").strip()
    title = str(job_details.get("title") or "").strip()
    first_name = name.split()[0] if name else "there"
    top_exp: dict[str, Any] = {}
    for exp in profile.get("experience", []) or []:
        if isinstance(exp, dict) and exp.get("title"):
            top_exp = exp
            break
    top_title = str(top_exp.get("title", "") or "")
    top_company = str(top_exp.get("company", "") or "")

    p1_bits = []
    if top_title:
        p1_bits.append(f"as a {top_title}" + (f" at {top_company}" if top_company else ""))
    if matched:
        p1_bits.append(f"with hands-on experience in {', '.join(matched[:4])}")
    p1 = (
        f"I'm excited to apply for the {title} role"
        + (f" at {company}" if company else "")
        + (", bringing " + " ".join(p1_bits) if p1_bits else "")
        + "."
    )
    p2 = (
        f"What draws me to this opportunity is the focus on "
        f"{', '.join(matched[:3]) if matched else 'the core responsibilities described'}"
        + (f" at {company}" if company else "")
        + ", which aligns closely with the work I've been doing."
    )
    p3 = (
        f"I'd welcome the chance to discuss how I can contribute. "
        f"Thank you for your consideration — {name}."
        if name
        else "I'd welcome the chance to discuss how I can contribute. Thank you for your consideration."
    )
    cover_letter = "\n\n".join(
        [f"Dear Hiring Manager,", p1, p2, p3]
    )

    return {
        "resume_md": resume_md,
        "cover_letter": cover_letter,
        "matched_skills": matched,
        "missing_skills": keywords["missing"],
    }


# ----------------------------------------------------------------------
# Provenance (Initiative 05 — application studio)
# ----------------------------------------------------------------------

def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def tailor_with_provenance(
    profile: dict[str, Any],
    job_details: dict[str, Any],
    evidence_links: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Tailor a resume and record per-statement provenance.

    Returns the same keys as :func:`tailor_with_diff` plus
    ``provenance``::

        {"job_id", "matched_skills", "missing_skills", "statement_map",
         "tailor_version"}

    ``statement_map`` holds one entry per rendered resume line:
    ``{"statement", "evidence_ids", "link_ids"}``. Every line is mapped
    back to a profile source (``profile:experience[i].bullets[j]``,
    ``profile:skills``, ``profile:summary``, ...) and, when
    ``evidence_links`` (Initiative 04 requirement→evidence links) are
    supplied, to the link/evidence ids whose requirement text mentions
    the same skill. Malformed links are ignored, never trusted.

    The honesty contract applies unchanged: provenance records *which*
    evidence justified each statement; it never invents new facts.
    """
    result = tailor_with_diff(profile, job_details)
    matched = result["matched_skills"]
    resume_md = result["resume_md"]

    # Index profile bullets -> source references for exact trace-back.
    bullet_sources: dict[str, str] = {}
    for i, exp in enumerate(profile.get("experience", []) or []):
        if not isinstance(exp, dict):
            continue
        for j, bullet in enumerate(exp.get("bullets", []) or []):
            key = _norm_text(bullet)
            if key:
                bullet_sources.setdefault(
                    key, f"profile:experience[{i}].bullets[{j}]")

    links = evidence_links or []
    statement_map: list[dict[str, Any]] = []
    for line in resume_md.splitlines():
        stripped = line.strip()
        if (not stripped or stripped.startswith("#") or "|" in stripped
                or (stripped.startswith("*") and stripped.endswith("*"))):
            continue
        norm = _norm_text(stripped)
        evidence_ids: list[str] = []
        link_ids: list[str] = []
        if norm in bullet_sources:
            evidence_ids.append(bullet_sources[norm])
        for skill in matched:
            # Word-boundary match (same discipline as keyword
            # extraction) so "go" never matches "good".
            if not re.search(
                r"(?<![a-z0-9+#])" + re.escape(skill.lower())
                + r"(?![a-z0-9+#])",
                norm,
            ):
                continue
            if "profile:skills" not in evidence_ids:
                evidence_ids.append("profile:skills")
            for link in links:
                if not isinstance(link, dict):
                    continue
                lid = str(link.get("link_id") or "")
                blob = (
                    str(link.get("requirement_id") or "") + " "
                    + str(link.get("quote") or "")
                ).lower()
                if lid and skill.lower() in blob:
                    if lid not in link_ids:
                        link_ids.append(lid)
                    eid = link.get("evidence_id")
                    if eid and str(eid) not in evidence_ids:
                        evidence_ids.append(str(eid))
        if not evidence_ids:
            # Summary / education / contact-adjacent lines: profile-sourced.
            evidence_ids.append("profile:base")
        statement_map.append(
            {
                "statement": stripped,
                "evidence_ids": evidence_ids,
                "link_ids": link_ids,
            }
        )

    result["provenance"] = {
        "job_id": str(
            job_details.get("id") or job_details.get("job_id") or ""),
        "matched_skills": matched,
        "missing_skills": result["missing_skills"],
        "statement_map": statement_map,
        "tailor_version": "tailor_with_provenance/1",
    }
    return result


# ----------------------------------------------------------------------
# Diff + approval workflow
# ----------------------------------------------------------------------

def base_resume_md(profile: dict[str, Any]) -> str:
    """Render the untailored resume from the profile.

    Uses the exact same renderer as :func:`tailor_resume` with no
    matched keywords, so the output format is byte-identical to what
    ``tailor_resume`` produces when a posting matches nothing.
    """
    return _render_resume_md(profile, [])


def resume_diff(base_md: str, tailored_md: str) -> str:
    """Unified diff of ``tailored_md`` against ``base_md``.

    The diff is prefixed with a short human-readable header giving the
    added/removed line counts. Returns the header alone when the two
    resumes are identical. Pure stdlib :mod:`difflib`.
    """
    base_lines = base_md.splitlines()
    tailored_lines = tailored_md.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            base_lines,
            tailored_lines,
            fromfile="base_resume.md",
            tofile="tailored_resume.md",
            lineterm="",
        )
    )
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    header = f"Resume tailoring diff: +{added} -{removed} lines"
    if added or removed:
        header += f" (net {'+' if added - removed >= 0 else ''}{added - removed})"
    else:
        header += " — no changes"
    return "\n".join([header, ""] + diff_lines) + "\n"


def tailor_with_diff(
    profile: dict[str, Any], job_details: dict[str, Any]
) -> dict[str, Any]:
    """Tailor a resume and include a diff against the untailored base.

    Returns the same keys as :func:`tailor_resume` plus ``diff`` (the
    full unified diff with a summary header) and ``diff_summary`` (the
    header line alone). The honesty contract applies unchanged: the
    diff can only ever show reordering and the keyword-driven
    Summary/Skills sections — never invented facts.
    """
    result = tailor_resume(profile, job_details)
    base = base_resume_md(profile)
    diff = resume_diff(base, result["resume_md"])
    result["diff"] = diff
    result["diff_summary"] = diff.splitlines()[0]
    return result


# --- approval store ----------------------------------------------------

TAILORED_DIR = Path(__file__).resolve().parent / "tailored"


def _safe_job_id(job_id: str) -> str:
    """Sanitize a job id for use as a filename stem.

    Keeps letters, digits, ``-`` and ``_``; everything else becomes
    ``_``. Truncated to 120 chars so paths stay well under OS limits.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(job_id))
    return safe[:120] or "job"


def _variant_path(job_id: str) -> Path:
    return TAILORED_DIR / f"{_safe_job_id(job_id)}.json"


def save_approved_variant(
    job_id: str, resume_md: str, cover_letter: str
) -> Path:
    """Persist a user-approved tailored variant for ``job_id``.

    Writes JSON ``{"job_id", "resume_md", "cover_letter",
    "approved_at"}`` to ``tailored/<safe_job_id>.json`` (atomic via a
    temp file + rename). Returns the path written.
    """
    import json

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": str(job_id),
        "resume_md": resume_md,
        "cover_letter": cover_letter,
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _variant_path(job_id)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    log.info("approved tailored variant saved for job %s -> %s", job_id, path)
    return path


def load_approved_variant(job_id: str) -> dict[str, Any] | None:
    """Load the approved variant for ``job_id``, or ``None`` if absent.

    A corrupt JSON file is treated as absent (logged, not raised) so a
    bad file can never break the apply flow.
    """
    import json

    path = _variant_path(job_id)
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("ignoring unreadable approved variant %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_approved_variants() -> list[str]:
    """Return the sorted job ids that have an approved tailored variant."""
    if not TAILORED_DIR.is_dir():
        return []
    ids: list[str] = []
    for path in TAILORED_DIR.glob("*.json"):
        variant = load_approved_variant(path.stem)
        if variant is not None:
            ids.append(str(variant.get("job_id", path.stem)))
    return sorted(ids)
