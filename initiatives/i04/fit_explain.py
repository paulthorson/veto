#!/usr/bin/env python3
"""Resume + job fit decoder (Initiative 04, Epic 1).

``fit_explain(resume_text, jd_text, ...)`` analyzes pasted resume and job
text fully on-device — no model, no API, no network — and returns a
``veto/fit-result/v1`` document: the fit score, its component
breakdown, score provenance, limitations, the JD's skeptical read, and
the embedded requirement→evidence map.

Composition (no new NLP — the spike's validated approach):

* resume side: :mod:`initiatives.i04.resume_decoder`
* JD side: ``jd_decoder.decode_jd`` / ``jd_decoder.jd_verdict``
  (transparency, overwork, vagueness, growth, green flags — every
  signal quoted)
* fit score: ``match.score_job`` five-factor
  (skills/seniority/salary/location/recency) with the 60-point veto —
  the "explain the yes or no" is the components + reasons + veto_reason
* requirement→evidence links: :mod:`initiatives.i04.evidence_map`
* provenance: Initiative 02's *contracted* score-provenance shape.
  Until 02's engine lands, every result carries
  ``{"kind": "static", "n": 0}`` and surfaces must display
  "static score — no outcome data yet" plainly.

Pure function: text in, JSON-serializable dict out. Never raises on odd
input — empty inputs yield an explicit low-confidence result whose
limitations say exactly what was missing.
"""

from __future__ import annotations

from typing import Any

import jd_decoder  # noqa: E402
import match  # noqa: E402 (read-only upstream; never modified by 04)

from .evidence_map import build_evidence_map
from .resume_decoder import decode_resume
from .schemas import FIT_RESULT_SCHEMA, validate_fit_result

#: Resume-decoder seniority label -> match.py seniority level.
_SENIORITY_MAP = {
    "intern": "entry",
    "junior": "entry",
    "associate": "entry",
    "senior": "senior",
    "staff": "staff",
    "principal": "principal",
    "lead": "senior",
    "manager": "senior",
    "director": "senior",
    "executive": "principal",
}

_STATIC_PROVENANCE = {"kind": "static", "n": 0}

#: Plain-language limits of the on-device decoder. Always shipped with
#: the result (method card, not fine print).
_DECODER_LIMITATIONS = [
    "On-device deterministic analysis: regex and keyword lists, no "
    "language model. It matches what is literally stated — it does not "
    "infer skills, read between lines, or understand paraphrase beyond "
    "a curated alias list.",
    "Skill lists are English-centric and finite; unmatched skills are "
    "reported as absent, never guessed.",
    "Salary/compensation comparisons use only ranges stated in the "
    "posting; missing data is reported as missing, never filled in "
    "with market data.",
    "Score provenance is static until Initiative 02's outcome-informed "
    "scoring lands: the score does not learn from your outcomes yet.",
]


def _match_profile(decoded_resume: dict[str, Any]) -> dict[str, Any]:
    """Adapt the decoded resume to ``match.score_job``'s profile shape."""
    seniority = decoded_resume.get("seniority") or {}
    label = str(seniority.get("label", "") or "").lower()
    return {
        "skills": [s["skill"] for s in decoded_resume.get("skills_found", [])],
        "seniority": _SENIORITY_MAP.get(label, "mid"),
        # Location is not in the decoded resume's structured fields;
        # match.py scores it neutrally when absent. Reported as a
        # limitation rather than guessed.
    }


def fit_explain(
    resume_text: str,
    jd_text: str,
    job_id: str,
    job_title: str = "",
) -> dict[str, Any]:
    """Explain the fit between a resume and a job posting.

    Returns ``{"fit_result": <veto/fit-result/v1 doc>,
    "suggested_grill_questions": [...], "validation_errors": [...]}``.
    """
    resume_text = resume_text or ""
    jd_text = jd_text or ""

    decoded_resume = decode_resume(resume_text)
    jd_signals = jd_decoder.decode_jd(jd_text)
    jd_read = jd_decoder.jd_verdict(jd_text)
    evidence = build_evidence_map(jd_text, resume_text, job_id, job_title)

    job = {"title": job_title, "description": jd_text}
    scored = match.score_job(job, _match_profile(decoded_resume))

    supported = sum(
        1 for e in evidence["evidence_map"]["entries"]
        if e["status"] == "supported"
    )
    gaps = sum(
        1 for e in evidence["evidence_map"]["entries"]
        if e["status"] == "gap"
    )
    questions = sum(
        1 for e in evidence["evidence_map"]["entries"]
        if e["status"] == "grill_question"
    )

    limitations = list(_DECODER_LIMITATIONS)
    limitations.extend(decoded_resume.get("limitations", []))
    if not jd_text.strip():
        limitations.append("No job description text provided.")
    # Deduplicate while keeping order.
    limitations = list(dict.fromkeys(limitations))

    explain_lines = [
        f"Fit score {scored['score']}/100 "
        f"({'VETOED: ' + scored['veto_reason'] if scored['veto'] else 'above the 60-point bar'}).",
        f"Evidence: {supported} requirement(s) supported, {gaps} gap(s), "
        f"{questions} question(s) for the grill.",
        f"Posting read: {jd_read['verdict']} — "
        + "; ".join(r["reason"] for r in jd_read["reasons"][:2]
                     if r.get("reason")),
    ]

    fit_result = {
        "schema": FIT_RESULT_SCHEMA,
        "job_id": job_id,
        "job_title": job_title,
        "fit_score": scored["score"],
        "components": scored["components"],
        "reasons": scored["reasons"],
        "veto": scored["veto"],
        "veto_reason": scored["veto_reason"],
        "provenance": dict(_STATIC_PROVENANCE),
        "provenance_note": (
            "Static score — no outcome data yet. Initiative 02's "
            "outcome-informed scoring is not implemented; this score "
            "does not learn from your applications."
        ),
        "jd_verdict": {
            "verdict": jd_read["verdict"],
            "score": jd_read["score"],
            "reasons": jd_read["reasons"],
        },
        "evidence_map": evidence["evidence_map"],
        "evidence_summary": {
            "supported": supported,
            "gaps": gaps,
            "grill_questions": questions,
        },
        "limitations": limitations,
        "explain": explain_lines,
    }
    errors = validate_fit_result(fit_result)
    errors.extend(
        f"evidence_map: {e}" for e in evidence["validation_errors"]
    )
    return {
        "fit_result": fit_result,
        "suggested_grill_questions": evidence["suggested_grill_questions"],
        "validation_errors": errors,
    }
