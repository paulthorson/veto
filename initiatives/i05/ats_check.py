#!/usr/bin/env python3
"""ATS readiness check — Epic 3 of Initiative 05.

Checks a resume for parser-friendly structure, keyword coverage,
field completeness, and export validity. Pure local logic — no
network, no vendor calls.

QA CONTRACT (release blocker, verified by adversarial review): UI copy
from this module must NEVER imply or predict how any vendor system
orders candidates. Every user-facing message is generated from the
templates below and validated by :func:`check_copy` against
``COPY_CONTRACT``. Any violation blocks release.

DECISION — what the "ready" verdict means (2026-09-13 blind-review
rework of a dishonest-verdict finding):
  Option: (a) gate "ready" on parser-critical warnings, not just
    fails. ``ready`` is True only when nothing failed AND no finding
    carries positive evidence of parser-hostility or incompleteness
    (see ``_READINESS_BLOCKING_KEYS``). Advisory warnings — keyword
    coverage, PDF-backend availability, a skipped date check —
    describe workflow quality or inconclusive results, not the
    document, and do not gate the verdict.
  Trades away: "ready" is stricter than "no red". A resume can clear
    every fail and still render "needs attention" until
    parser-critical warnings (tables, missing contact, extreme
    length, no bullets, bad date formats, missing contact fields)
    are addressed. Field completeness also gates the headline, even
    when the resume body itself parses fine.
  To get: the headline property — the rendered report can never
    print "ready" for a resume the module's own evidence calls
    parser-hostile. Rejected (b) demoting the label to "no blocking
    issues": the epic's purpose is a readiness verdict, and a softer
    label would keep honest copy while abandoning the feature.
    Rejected (c) promoting the checks to fails: a missing bullet
    list is actionable guidance, not a document-breaking failure —
    fails stay reserved for the title/sections/export problems.
    "Ready" still describes format and completeness only; it is NOT
    a prediction about any hiring system (see the disclaimer).
"""

from __future__ import annotations

import re
from typing import Any

# COPY_CONTRACT_BEGIN
#: Phrases that must never appear in user-facing copy. "Rank" language
#: (rank/ranking/ranked), ATS-gaming language, guarantees, predictions
#: about passing, and vendor names used in a predictive claim are all
#: banned. Copy may say a resume is parseable/readable/complete; it may
#: never say what a vendor system will do with it.
COPY_CONTRACT_BANNED = (
    "rank",
    "ranking",
    "ranked",
    "ranks",
    "beat the ats",
    "game the ats",
    "trick the ats",
    "ats score",
    "guarantee",
    "will pass",
    "top of the pile",
    "shortlist",
    "workday",
    "greenhouse",
    "lever",
    "ashby",
    "taleo",
    "icims",
)
# COPY_CONTRACT_END


def check_copy(text: str) -> list[str]:
    """Return banned phrases found in ``text`` (word-boundary matched).

    Empty means the copy satisfies the QA contract. Used by the module
    itself (self-check in tests) and by reviewers.
    """
    low = str(text or "").lower()
    return [
        phrase
        for phrase in COPY_CONTRACT_BANNED
        if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", low)
    ]


# ---------------------------------------------------------------------------
# Message templates — the ONLY user-facing strings this module emits.
# ---------------------------------------------------------------------------

_MSG = {
    "title_ok": "Name header present.",
    "title_missing": (
        "Add a top-level name header ('# Your Name') so parsers can "
        "identify the document."
    ),
    "contact_ok": "Contact line present.",
    "contact_missing": (
        "No email or phone found near the top — include at least one "
        "way to reach you."
    ),
    "sections_ok": "Section headers found ({n}).",
    "sections_missing": (
        "Add '## ' section headers (Experience, Education, Skills) so "
        "each part of the resume is labeled."
    ),
    "tables_ok": "No tables detected.",
    "tables_found": (
        "Tables were detected. Many parsers read tables poorly — "
        "prefer plain headings and bullet lists."
    ),
    "length_ok": "Length looks reasonable ({n} words).",
    "length_warn": (
        "Resume is {n} words. Very short or very long resumes can be "
        "harder for parsers to handle cleanly."
    ),
    "bullets_ok": "Bullet lists found.",
    "bullets_missing": (
        "No bullet lists found. Bullets ('- ') help parsers separate "
        "individual achievements."
    ),
    "dates_ok": "Date lines look consistent.",
    "dates_none": (
        "No date lines found in the expected '*...*' format — date "
        "check skipped."
    ),
    "dates_warn": (
        "Some date lines lack a 4-digit year or 'Present'. Use formats "
        "like '2020 - 2024' or '2020 - Present'."
    ),
    "keyword_ok": "All {n} matched keywords appear in the resume.",
    "keyword_none": "No job details supplied — keyword coverage skipped.",
    "keyword_unavailable": (
        "Keyword extraction failed — keyword coverage skipped."
    ),
    "keyword_partial": (
        "{covered} of {total} matched keywords appear in the resume. "
        "Not yet visible: {missing}."
    ),
    "field_ok": "All contact fields present.",
    "field_partial": "Missing fields: {missing}.",
    "export_ok": "Profile markdown and HTML exports render cleanly.",
    "export_skipped": (
        "No profile supplied — profile export check skipped."
    ),
    "export_fail": "Export rendering failed: {error}",
    "pdf_info": "PDF backend available: {backend}.",
    "pdf_none": (
        "No PDF backend installed (weasyprint/reportlab). Markdown and "
        "HTML exports still work."
    ),
    "disclaimer": (
        "Readiness describes format and completeness only — it does not "
        "predict how any hiring system treats the resume."
    ),
}


def _finding(area: str, severity: str, key: str, **kw: Any) -> dict[str, Any]:
    message = _MSG[key].format(**kw)
    violations = check_copy(message)
    if violations:  # pragma: no cover — contract: templates are clean
        raise AssertionError(
            f"template {key!r} violates the QA contract: {violations}")
    return {
        "area": area,
        "severity": severity,  # pass | warn | fail
        "key": key,  # template key — drives the readiness gate below
        # Blocking: fails always; warns only when they are positive
        # evidence of parser-hostility/incompleteness. Advisory warns
        # (inconclusive or workflow-scoped) never block readiness.
        "blocking": severity == "fail" or key in _READINESS_BLOCKING_KEYS,
        "message": message,
    }


#: Template keys whose warnings block the ``ready`` verdict. Each is a
#: warning whose own message describes a parser-hostile or incomplete
#: document: tables (read poorly by parsers), no contact info, extreme
#: length, no bullet lists, non-standard date formats, missing
#: contact/identity fields. Advisory warnings — keyword coverage,
#: keyword-source failure, PDF-backend availability, a skipped date
#: check — are absent here on purpose: they describe workflow quality
#: or an inconclusive result, not evidence the document parses badly.
_READINESS_BLOCKING_KEYS = frozenset({
    "contact_missing",
    "tables_found",
    "length_warn",
    "bullets_missing",
    "dates_warn",
    "field_partial",
})


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_structure(resume_md: str) -> list[dict[str, Any]]:
    """Parser-friendly structure checks on resume markdown."""
    findings: list[dict[str, Any]] = []
    text = str(resume_md or "")
    lines = [l for l in text.splitlines() if l.strip()]

    if lines and lines[0].lstrip().startswith("# "):
        findings.append(_finding("structure", "pass", "title_ok"))
    else:
        findings.append(_finding("structure", "fail", "title_missing"))

    head = "\n".join(lines[:4])
    # Heuristic: recognizes common 10-digit phone layouts; it confirms
    # a phone-like string is present near the top, not that the number
    # is real or dialable.
    if "@" in head or re.search(r"\d{3}[\s.-]?\d{3}[\s.-]?\d{4}", head):
        findings.append(_finding("structure", "pass", "contact_ok"))
    else:
        findings.append(_finding("structure", "warn", "contact_missing"))

    # Heuristic: a labeled resume normally has at least two sections;
    # fewer suggests a missing part (e.g. no Experience block).
    sections = [l for l in lines if l.lstrip().startswith("## ")]
    if len(sections) >= 2:
        findings.append(
            _finding("structure", "pass", "sections_ok", n=len(sections)))
    else:
        findings.append(_finding("structure", "fail", "sections_missing"))

    # Markdown table = a pipe-delimited block with a --- separator row.
    tabley = any(
        re.match(r"^\s*\|.*\|\s*$", l) for l in lines
    ) and any(re.match(r"^\s*\|?[\s:|-]+\|?\s*$", l) and "---" in l
              for l in lines)
    if tabley or "<table" in text.lower():
        findings.append(_finding("structure", "warn", "tables_found"))
    else:
        findings.append(_finding("structure", "pass", "tables_ok"))

    # Heuristic guardrails with no authoritative basis — no parser
    # vendor publishes word-count limits. 80 words is below a plausible
    # one-page resume; 1600 words is roughly three dense pages, where
    # long documents get harder to parse and to read. Deliberately
    # lenient: only extremes are flagged.
    words = len(text.split())
    if 80 <= words <= 1600:
        findings.append(_finding("structure", "pass", "length_ok", n=words))
    else:
        findings.append(_finding("structure", "warn", "length_warn", n=words))

    if any(l.lstrip().startswith("- ") for l in lines):
        findings.append(_finding("structure", "pass", "bullets_ok"))
    else:
        findings.append(_finding("structure", "warn", "bullets_missing"))

    # Date-line heuristic: only '*...*' italic lines are inspected —
    # the format this initiative's own generators emit. Lines in other
    # formats are invisible to this check, so zero matches means "not
    # examined", never "consistent".
    date_lines = [l for l in lines
                  if l.strip().startswith("*") and l.strip().endswith("*")]
    if not date_lines:
        findings.append(_finding("structure", "warn", "dates_none"))
    else:
        bad_dates = [
            l for l in date_lines
            if not (re.search(r"\b(19|20)\d{2}\b", l)
                    or "present" in l.lower())
        ]
        if not bad_dates:
            findings.append(_finding("structure", "pass", "dates_ok"))
        else:
            findings.append(_finding("structure", "warn", "dates_warn"))
    return findings


def check_keyword_coverage(
    resume_md: str,
    job_details: dict[str, Any] | None,
    profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Share of job-matched keywords actually visible in the resume.

    A broken keyword source (bad import, API change) must never kill
    the whole report — it degrades to a skipped-coverage warning.
    """
    try:
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import tailor as tailor_mod

        job_details = job_details or {}
        profile = profile or {}
        matched = tailor_mod.extract_job_keywords(
            job_details, profile)["matched"]
    except Exception:
        return [_finding("keywords", "warn", "keyword_unavailable")]

    low = str(resume_md or "").lower()
    if not matched and not job_details:
        return [_finding("keywords", "pass", "keyword_none")]
    covered = [
        s for s in matched
        if re.search(r"(?<![a-z0-9+#])" + re.escape(s.lower())
                     + r"(?![a-z0-9+#])", low)
    ]
    missing = [s for s in matched if s not in covered]
    if not matched:
        return [_finding("keywords", "pass", "keyword_ok", n=0)]
    if not missing:
        return [_finding("keywords", "pass", "keyword_ok", n=len(matched))]
    return [
        _finding(
            "keywords", "warn", "keyword_partial",
            covered=len(covered), total=len(matched),
            missing=", ".join(missing),
        )
    ]


_FIELD_LABELS = (
    ("email", "email"),
    ("phone", "phone"),
    ("location", "location"),
    ("linkedin_url", "LinkedIn URL"),
    ("website", "website"),
)


def check_field_completeness(
    profile: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Required contact/identity fields present in the profile."""
    profile = profile or {}
    missing = [
        label for key, label in _FIELD_LABELS
        if not str(profile.get(key) or "").strip()
    ]
    if not missing:
        return [_finding("fields", "pass", "field_ok")]
    return [
        _finding("fields", "warn", "field_partial",
                 missing=", ".join(missing))
    ]


def check_exports(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Profile renders to markdown/HTML; report PDF backend availability.

    Scope note (2026-09-13 blind-review rework): the renderers here
    build from the *profile* — ``resume_builder.build_markdown`` /
    ``build_html`` take a profile dict, and ``cmd_ats`` accepts
    ``--resume`` and ``--profile`` independently. The report therefore
    claims nothing about the resume markdown itself; every message is
    scoped to the profile. The resume document is checked separately
    by :func:`check_structure`.
    """
    findings: list[dict[str, Any]] = []
    profile = profile or {}
    if not profile:
        # Honest skip: rendering an empty profile says nothing about
        # the user's data, so don't claim a clean render.
        findings.append(_finding("exports", "pass", "export_skipped"))
    else:
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import resume_builder as rb

        try:
            rb.build_markdown(profile)
            rb.build_html(profile)
            findings.append(_finding("exports", "pass", "export_ok"))
        except Exception as exc:  # never break the check on render errors
            findings.append(_finding("exports", "fail", "export_fail",
                                     error=str(exc)[:120]))
    backend = "none"
    try:
        import weasyprint  # type: ignore  # noqa: F401
        backend = "weasyprint"
    except ImportError:
        try:
            import reportlab  # type: ignore  # noqa: F401
            backend = "reportlab"
        except ImportError:
            backend = "none"
    if backend == "none":
        findings.append(_finding("exports", "warn", "pdf_none"))
    else:
        findings.append(_finding("exports", "pass", "pdf_info",
                                 backend=backend))
    return findings


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def ats_readiness_check(
    resume_md: str,
    job_details: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run all readiness checks; return the report.

    ``{"findings", "summary": {"pass", "warn", "fail"},
    "ready"}``. ``ready`` is True only when no finding failed AND no
    finding is parser-critical: each finding carries a ``blocking``
    flag, True for fails and for the parser-hostile/incomplete
    warnings listed in ``_READINESS_BLOCKING_KEYS`` (tables, missing
    contact, extreme length, no bullets, bad date formats, missing
    contact fields). Advisory warnings (keyword coverage, PDF-backend
    availability, a skipped date check) never gate the verdict.
    "Ready" means the document looks parser-friendly and complete —
    NOT a prediction about any vendor system (see the disclaimer).
    """
    findings = (
        check_structure(resume_md)
        + check_keyword_coverage(resume_md, job_details, profile)
        + check_field_completeness(profile)
        + check_exports(profile)
    )
    # Final contract sweep: every emitted message must be clean.
    for finding in findings:
        violations = check_copy(finding["message"])
        if violations:  # pragma: no cover — defense in depth
            raise AssertionError(
                "ATS copy contract violated: " + "; ".join(violations))
    summary = {"pass": 0, "warn": 0, "fail": 0}
    for finding in findings:
        summary[finding["severity"]] += 1
    return {
        "findings": findings,
        "summary": summary,
        # Headline property: never claim readiness while the module's
        # own evidence calls the resume parser-hostile/incomplete.
        "ready": not any(f["blocking"] for f in findings),
    }


def render_report_text(report: dict[str, Any]) -> str:
    """Plain-text rendering of a readiness report (terminal/phone)."""
    lines = ["ATS readiness check"]
    s = report["summary"]
    lines.append(
        f"{s['pass']} passed, {s['warn']} warnings, {s['fail']} failed — "
        f"{'ready' if report['ready'] else 'needs attention'}"
    )
    lines.append("")
    for finding in report["findings"]:
        mark = {"pass": "✓", "warn": "!", "fail": "✗"}[finding["severity"]]
        lines.append(f"[{mark}] {finding['area']}: {finding['message']}")
    lines.append("")
    lines.append(_MSG["disclaimer"])
    return "\n".join(lines) + "\n"
