#!/usr/bin/env python3
"""Initiative 12 / Epic 1 — Interactive public tools.

Four demos a visitor can use without an account and without uploading a
resume: JD decoder, fit explainer, application-risk check, and role
comparison. Everything runs locally (the decoder is the deterministic
regex+counting engine — no model, no API, no network) and every output
carries a methodology link and an honest-limitations note.

Design rules:

* Public tools must be *useful*, not lead magnets. Each demo does the real
  analysis and says plainly what it cannot do.
* No resume upload is ever required or requested by these demos. The fit
  explainer works from a compact, user-typed skill list — never a pasted
  resume — and states that a full analysis needs the local app.
* Nothing here submits, sends, or applies anywhere. The risk check is
  proposal-only by construction: there is no code path to an application.
* Outputs are JSON-serializable dicts plus a ``render_text`` view, so the
  terminal, local web, and (later) marketing site render the same result.

Contract-first: the decoder resolves through
:mod:`initiatives.i12.contracts` (Initiative 04 interface). The fit
explainer builds on :mod:`match`'s five-factor scorer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import contracts

#: Public methodology URL. Honest placeholder: there is no public URL until
#: the operator launches; the share module renders this verbatim into shared markdown,
#: so a relative repo path would be a dead link. Set at launch.
METHODOLOGY_URL = "TBD \u2014 public methodology URL set by the operator at launch"
LIMITATIONS_NOTE = (
    "Demo analysis runs entirely on your device. It is a screening aid, "
    "not career advice: it only sees the text you paste, knows nothing "
    "about the company beyond that text, and cannot predict hiring outcomes."
)

#: Hard cap from the roadmap: role comparison covers at most four jobs.
MAX_COMPARE_JOBS = 4


def _methodology_url() -> str:
    """Launch gate for the methodology URL placeholder.

    The TBD placeholder must never reach a real surface: if the package
    status has moved past ``built-pending-review`` while the URL is still
    TBD, fail loudly instead of rendering a dead link into every envelope
    and share artifact.
    """
    if METHODOLOGY_URL.startswith("TBD"):
        try:
            from . import __status__ as _status
        except ImportError:  # pragma: no cover - defensive
            _status = "built-pending-review"
        if _status != "built-pending-review":
            raise RuntimeError(
                f"Initiative 12 status is {_status!r} but METHODOLOGY_URL is "
                "still the TBD placeholder — set the public methodology URL "
                "before launch."
            )
    return METHODOLOGY_URL


def _demo_envelope(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.update(
        {
            "tool": tool,
            "methodology": _methodology_url(),
            "limitations": LIMITATIONS_NOTE,
            "demo": True,
            "submits_anything": False,
        }
    )
    return payload


# ---------------------------------------------------------------------------
# JD decoder demo
# ---------------------------------------------------------------------------

def _decode_section(decoded: Any, key: str) -> dict[str, Any]:
    """Defensive accessor for one top-level decoder section.

    Decoder return-shape drift must degrade honestly, never KeyError: a
    missing or mistyped section reads as an empty dict.
    """
    if isinstance(decoded, dict):
        section = decoded.get(key)
        if isinstance(section, dict):
            return section
    return {}


def _decode_list(decoded: Any, key: str) -> list[Any]:
    """Defensive accessor for one top-level decoder list section."""
    if isinstance(decoded, dict):
        items = decoded.get(key)
        if isinstance(items, list):
            return items
    return []


def jd_demo(text: str, max_chars: int = 20000) -> dict[str, Any]:
    """Public JD decoder demo: decode pasted JD text via the contracted
    Initiative 04 decoder interface (currently the deterministic engine).

    The decoder presents evidence with quotes; it never moralizes and never
    infers beyond the text. ``max_chars`` bounds pasted input.

    Return-shape drift in the decoder degrades honestly (missing sections
    read as empty) — this function never raises KeyError on the decoder's
    output. Runtime failures inside the decoder degrade to the unavailable
    envelope rather than a raw traceback. When ``text`` exceeds ``max_chars``
    the verdict is computed on the truncated text and the envelope says so.
    """
    raw_text = text or ""
    truncated = len(raw_text) > max_chars
    text = raw_text[:max_chars]
    caps = {c.name: c for c in contracts.decoder_capabilities()}
    if not (caps.get("decode_jd") and caps["decode_jd"].available):
        return _demo_envelope(
            "jd_decoder",
            {"available": False,
             "message": "The JD decoder is temporarily unavailable."},
        )
    import jd_decoder

    try:
        decoded = jd_decoder.decode_jd(text)
        verdict = jd_decoder.jd_verdict(text)
    except Exception as exc:
        return _demo_envelope(
            "jd_decoder",
            {"available": False,
             "message": "The JD decoder failed on this input: "
                        f"{type(exc).__name__}. Try shorter or simpler text."},
        )
    if not isinstance(decoded, dict) or not isinstance(verdict, dict):
        return _demo_envelope(
            "jd_decoder",
            {"available": False,
             "message": "The JD decoder returned an unexpected result shape."},
        )
    transparency = _decode_section(decoded, "transparency")
    vagueness = _decode_section(decoded, "vagueness")
    salary_range = transparency.get("salary_range")
    reasons = verdict.get("reasons")
    return _demo_envelope(
        "jd_decoder",
        {
            "available": True,
            "truncated": truncated,
            "truncation_note": (
                "Input exceeded max_chars; the verdict below was computed on "
                f"the first {max_chars} characters."
                if truncated else ""
            ),
            "verdict": verdict.get("verdict"),
            "top_reasons": list(reasons)[:3] if isinstance(reasons, list) else [],
            "transparency_score": transparency.get("score"),
            "salary_range": salary_range.get("raw") if isinstance(salary_range, dict) else None,
            "overwork_flags": [
                {"phrase": h.get("phrase"), "read": h.get("read")}
                for h in _decode_list(decoded, "overwork")
                if isinstance(h, dict)
            ],
            "vagueness": {
                "score": vagueness.get("score"),
                "buzzword_count": vagueness.get("buzzword_count"),
                "responsibilities": vagueness.get("responsibilities"),
            },
            "growth_signals": [
                g.get("signal") for g in _decode_list(decoded, "growth")
                if isinstance(g, dict)
            ],
            "green_flags": [
                g.get("flag") for g in _decode_list(decoded, "green_flags")
                if isinstance(g, dict)
            ],
            "markdown": decoded.get("markdown"),
        },
    )


# ---------------------------------------------------------------------------
# Fit explainer demo
# ---------------------------------------------------------------------------

@dataclass
class MiniProfile:
    """A compact, user-typed profile for the public fit explainer.

    Deliberately NOT a resume: just skills, seniority, locations, and
    preferences. The demo explains the five-factor model against this and
    points at the local app for real evidence-backed scoring.
    """

    skills: list[str] = field(default_factory=list)
    seniority: str = "mid"
    locations: list[str] = field(default_factory=list)
    remote_ok: bool = True
    min_salary: float | None = None


def _mini_profile_dict(p: MiniProfile) -> dict[str, Any]:
    """Canonical profile dict for :mod:`match`.

    Single source of truth: every demo builds this dict once and derives
    everything else from it. Key namespace: ``skills``, ``seniority``,
    ``locations``, ``remote_ok``, ``salary_min`` (``MiniProfile.min_salary``
    maps to ``salary_min``, the key :func:`match.score_job` honors).
    """
    return {
        "skills": list(p.skills),
        "seniority": p.seniority,
        "locations": list(p.locations),
        "remote_ok": p.remote_ok,
        "salary_min": p.min_salary,
    }


def _profile_prefs(prof: dict[str, Any]) -> dict[str, Any]:
    """Preference view derived from a canonical profile dict.

    Callers never build a second ``prefs`` literal — this is the only
    constructor. Semantic translation, explicit: :func:`match.score_job`
    honors ``remote_only`` (remote roles only), while the demo profile
    carries ``remote_ok`` (remote acceptable). The demo profile cannot
    express "remote only", so ``remote_only`` is always False here and the
    ``remote_ok=False`` case is surfaced separately by callers via
    :func:`_remote_mismatch_note` — the scorer would otherwise give a
    remote role 12/15 "location-flexible" points to a user who excluded
    remote work. ``salary_min`` maps 1:1; it is the key
    :func:`match.score_job` honors.
    """
    prefs: dict[str, Any] = {
        "locations": list(prof.get("locations") or []),
        "remote_only": False,
    }
    if prof.get("salary_min") is not None:
        prefs["salary_min"] = prof["salary_min"]
    return prefs


def _looks_remote(job: dict[str, Any]) -> bool:
    """Best-effort remote detection for the remote_ok=False warning."""
    hay = f"{job.get('location', '')}\n{job.get('description', '')}".lower()
    return "remote" in hay


def _remote_mismatch_note(job: dict[str, Any], profile: MiniProfile) -> str:
    """Honest note when the profile excludes remote but the job looks remote.

    The demo profile's ``remote_ok=False`` has no exact scorer key (the
    scorer only knows ``remote_only``), so this is surfaced as an explicit
    note rather than silently mis-scored.
    """
    if not profile.remote_ok and _looks_remote(job):
        return (
            "Your profile excludes remote work but this posting looks remote; "
            "the location score above does not reflect your remote preference "
            "— treat this role as a mismatch on location."
        )
    return ""


def _import_match() -> Any:
    """Resolve :mod:`match`, or None when the upstream is not importable.

    Public demos must degrade to an honest unavailable envelope — never a
    raw ImportError traceback — when the upstream scorer is missing. This
    mirrors the ``decoder_capabilities()`` -> unavailable-envelope pattern
    that :func:`jd_demo` already uses.
    """
    try:
        import match
    except ImportError:
        return None
    return match


def _match_unavailable(tool: str) -> dict[str, Any]:
    """Honest degradation envelope when :mod:`match` cannot be imported."""
    return _demo_envelope(
        tool,
        {"available": False,
         "message": "The fit scorer is temporarily unavailable."},
    )


#: Keyword map from flat scorer reasons to the five factors. The scorer's
#: `reasons` list is not per-factor, so explanations attribute each reason
#: to the factor it mentions; unattributed reasons stay in the top-level
#: `reasons` list, never dropped.
_FACTOR_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("skills", ("skill",)),
    ("seniority", ("seniority", "mid-level", "senior", "title", "level")),
    ("salary", ("salary", "floor", "$", "pay", "compensation")),
    ("location", ("location", "remote", "on-site", "hybrid")),
    ("recency", ("recen", "posting date", "stale", "fresh")),
)


def _reasons_for_factor(reasons: list[str], factor: str) -> list[str]:
    """Reasons mentioning a factor (substring match, case-insensitive)."""
    keywords = dict(_FACTOR_KEYWORDS)[factor]
    return [r for r in reasons
            if any(k in r.lower() for k in keywords)]


def _score_or_unavailable(tool: str, job: dict[str, Any],
                          profile: MiniProfile) -> dict[str, Any]:
    """Run the contracted scorer, degrading honestly on any failure.

    Returns the validated result dict, or an envelope dict (with
    ``available: False``) when the scorer is missing or its return shape
    drifted from the pinned contract. A shape drift is LOUD — never silent
    empty output presented as complete.
    """
    match_mod = _import_match()
    if match_mod is None:
        return _match_unavailable(tool)
    prof = _mini_profile_dict(profile)
    prefs = _profile_prefs(prof)
    try:
        result = match_mod.score_job(job, prof, prefs)
    except Exception as exc:
        return _demo_envelope(
            tool,
            {"available": False,
             "message": "The fit scorer failed on this input: "
                        f"{type(exc).__name__}."},
        )
    try:
        return contracts.check_match_result(result)
    except contracts.MatchContractViolation as exc:
        return _demo_envelope(
            tool,
            {"available": False,
             "message": f"The fit scorer's output changed shape: {exc}"},
        )


def fit_explainer(job: dict[str, Any], profile: MiniProfile) -> dict[str, Any]:
    """Explain *why* a role fits or doesn't, in plain English.

    Uses :mod:`match`'s five-factor scorer through the pinned contract in
    :mod:`initiatives.i12.contracts` (real keys: score, reasons, matched,
    missing, veto, veto_reason, components). Each factor gets its
    component points plus the scorer reasons that mention it; reasons that
    mention no factor stay in the top-level list. No raw resume text is
    involved — the profile is the compact typed list above. A scorer shape
    drift degrades to an honest unavailable envelope, never to silent
    empty explanations.
    """
    scored = _score_or_unavailable("fit_explainer", job, profile)
    if scored.get("available") is False:
        return scored
    components = scored.get("components", {})
    reasons = scored.get("reasons", [])
    explanations = []
    for name in ("skills", "seniority", "salary", "location", "recency"):
        explanations.append(
            {
                "factor": name,
                "score": components.get(name),
                "why": "; ".join(_reasons_for_factor(reasons, name)[:3]),
            }
        )
    remote_note = _remote_mismatch_note(job, profile)
    return _demo_envelope(
        "fit_explainer",
        {
            "job_title": job.get("title"),
            "company": job.get("company"),
            "total_score": scored.get("score"),
            "vetoed": bool(scored.get("veto")),
            "veto_reason": scored.get("veto_reason"),
            "factors": explanations,
            "reasons": reasons,
            "remote_preference_note": remote_note,
            "note": (
                "Demo uses a compact typed profile. The full app scores "
                "against your real evidence library with provenance per claim."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Application-risk check demo
# ---------------------------------------------------------------------------

def risk_check(job: dict[str, Any], profile: MiniProfile) -> dict[str, Any]:
    """Application-risk check: what could go wrong *before* you apply.

    Checks veto signals, JD transparency red flags, and readiness gaps.
    Proposal-only: returns findings and suggested next steps. There is no
    code path here that submits or sends anything, by design. A scorer
    shape drift degrades to an honest unavailable envelope.
    """
    scored = _score_or_unavailable("risk_check", job, profile)
    if scored.get("available") is False:
        return scored
    findings: list[dict[str, str]] = []

    if scored.get("veto"):
        findings.append(
            {"severity": "blocker", "area": "veto",
             "finding": str(scored.get("veto_reason") or "vetoed by scorer"),
             "next_step": "Do not apply; find a role without this blocker."}
        )

    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    decoded = jd_demo(text)
    if decoded.get("available"):
        if decoded.get("verdict") == "caution":
            findings.append(
                {"severity": "warning", "area": "transparency",
                 "finding": "JD decode verdict is 'caution': "
                 + "; ".join(decoded.get("top_reasons", [])[:2]),
                 "next_step": "Ask the recruiter for the missing specifics before investing time."}
            )
        if not decoded.get("salary_range"):
            findings.append(
                {"severity": "info", "area": "compensation",
                 "finding": "No salary range in the posting.",
                 "next_step": "Confirm a range before interviewing to avoid wasted loops."}
            )

    factors = scored.get("components", {})
    missing = scored.get("missing", [])
    if missing:
        findings.append(
            {"severity": "warning", "area": "readiness",
             "finding": f"Missing skills vs posting: {', '.join(str(m) for m in missing[:5])}.",
             "next_step": "Either gather evidence for these or deprioritize this role."}
        )
    remote_note = _remote_mismatch_note(job, profile)
    if remote_note:
        findings.append(
            {"severity": "warning", "area": "location",
             "finding": remote_note,
             "next_step": "Filter for on-site/hybrid roles in your target locations."}
        )

    return _demo_envelope(
        "risk_check",
        {
            "job_title": job.get("title"),
            "company": job.get("company"),
            "findings": findings,
            "blockers": sum(1 for f in findings if f["severity"] == "blocker"),
            "proposal_only": True,
            "cannot_submit": True,
        },
    )


# ---------------------------------------------------------------------------
# Role comparison demo
# ---------------------------------------------------------------------------

_COMPARE_DIMS = ("fit", "risk", "compensation", "location", "readiness")


def role_compare(jobs: list[dict[str, Any]], profile: MiniProfile) -> dict[str, Any]:
    """Compare up to four roles across fit, risk, compensation, location,
    and readiness. Compensation comes only from JD text (never invented);
    missing data is reported as missing. ``truncated`` is true only when
    the input actually exceeded MAX_COMPARE_JOBS. Degrades to an honest
    unavailable envelope when :mod:`match` is not importable.
    """
    all_jobs = list(jobs)
    truncated = len(all_jobs) > MAX_COMPARE_JOBS
    jobs = all_jobs[:MAX_COMPARE_JOBS]

    match_mod = _import_match()
    if match_mod is None:
        return _match_unavailable("role_compare")

    def _invalid_row(note: str) -> dict[str, Any]:
        return {
            "title": None,
            "company": None,
            "fit": None,
            "vetoed": False,
            "risk_findings": 0,
            "blockers": 0,
            "compensation": note,
            "location": "not listed",
            "readiness": "invalid input",
        }

    def _unavailable_row(job: dict[str, Any], note: str) -> dict[str, Any]:
        return {
            "title": job.get("title"),
            "company": job.get("company"),
            "fit": None,
            "vetoed": False,
            "risk_findings": 0,
            "blockers": 0,
            "compensation": note,
            "location": job.get("location") or "not listed",
            "readiness": "scorer unavailable",
        }

    prof = _mini_profile_dict(profile)
    prefs = _profile_prefs(prof)
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            rows.append(_invalid_row("invalid job entry (not an object)"))
            continue
        try:
            scored = contracts.check_match_result(
                match_mod.score_job(job, prof, prefs))
        except contracts.MatchContractViolation as exc:
            rows.append(_unavailable_row(
                job, f"scorer output changed shape: {exc}"))
            continue
        except Exception:
            rows.append(_unavailable_row(job, "scorer unavailable"))
            continue
        risk = risk_check(job, profile)
        text = f"{job.get('title', '')}\n{job.get('description', '')}"
        decoded = jd_demo(text)
        rows.append(
            {
                "title": job.get("title"),
                "company": job.get("company"),
                "fit": scored.get("score"),
                "vetoed": bool(scored.get("veto")),
                "risk_findings": len(risk["findings"]),
                "blockers": risk["blockers"],
                "compensation": decoded.get("salary_range")
                or "not listed in posting",
                "location": job.get("location") or "not listed",
                "readiness": (
                    "blocked" if risk["blockers"]
                    else ("gaps to close" if risk["findings"] else "ready to explore")
                ),
            }
        )
    return _demo_envelope(
        "role_compare",
        {
            "dimensions": list(_COMPARE_DIMS),
            "compared": len(rows),
            "truncated": truncated,
            "rows": rows,
            "note": "Compensation is quoted from the posting text only; "
                    "absent data is never filled in.",
        },
    )


# ---------------------------------------------------------------------------
# Plain-text rendering
# ---------------------------------------------------------------------------

def render_text(obj: dict[str, Any]) -> str:
    """Plain-text view of a demo envelope (the ``render_text`` view the
    module docstring promises): one line per key, lists on indented lines.
    """
    lines = [f"== {obj.get('tool')} (demo) =="]
    for key, value in obj.items():
        if key == "tool":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI (terminal surface for the public tools)
# ---------------------------------------------------------------------------

def _print(obj: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2))
    else:
        print(render_text(obj))


def register_cli(subparsers: Any) -> dict[str, Any]:
    """argparse wiring. The parent CLI owns ``cli.py`` — this module only
    exposes a factory so a future wiring can attach it without edits here."""
    p = subparsers.add_parser(
        "i12-demo",
        help="Initiative 12 public-tool demos (JD decoder, fit explainer, "
             "risk check, role compare). Local-only; nothing is submitted.",
    )
    p.add_argument("demo", choices=["jd", "fit", "risk", "compare", "install-status"])
    p.add_argument("--text", default="", help="JD text (for the jd demo)")
    p.add_argument("--job-json", default="", help="Job JSON file (fit/risk/compare)")
    p.add_argument("--jobs-json", default="", help="Jobs JSON array file (compare)")
    p.add_argument("--skills", default="", help="Comma-separated skills (fit/risk/compare)")
    p.add_argument("--json", action="store_true")

    def _load_json_file(path: str, flag: str) -> tuple[Any, int]:
        """Load a JSON file for a CLI flag.

        Returns (value, 0) on success. On any failure (missing file,
        invalid JSON) prints to stderr and returns (None, 2) — raw
        tracebacks and AttributeErrors on `null`/`[1]` payloads are
        never user-facing.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), 0
        except FileNotFoundError:
            print(f"{flag}: file not found: {path}", file=sys.stderr)
        except OSError as exc:
            print(f"{flag}: cannot read {path}: {exc}", file=sys.stderr)
        except json.JSONDecodeError as exc:
            print(f"{flag}: invalid JSON in {path}: {exc}", file=sys.stderr)
        return None, 2

    def _cmd(args: Any) -> int:
        profile = MiniProfile(
            skills=[s.strip() for s in args.skills.split(",") if s.strip()]
        )
        if args.demo == "jd":
            _print(jd_demo(args.text), args.json)
        elif args.demo == "install-status":
            _print(contracts.install_path_status(), args.json)
        else:
            job: Any = {}
            if args.job_json:
                job, code = _load_json_file(args.job_json, "--job-json")
                if code:
                    return code
            if not isinstance(job, dict):
                print("--job-json must contain a JSON object describing one job, "
                      f"got {type(job).__name__}", file=sys.stderr)
                return 2
            if args.demo == "fit":
                _print(fit_explainer(job, profile), args.json)
            elif args.demo == "risk":
                _print(risk_check(job, profile), args.json)
            elif args.demo == "compare":
                jobs: Any = [job]
                if args.jobs_json:
                    jobs, code = _load_json_file(args.jobs_json, "--jobs-json")
                    if code:
                        return code
                if not isinstance(jobs, list):
                    print("--jobs-json must contain a JSON array of job objects, "
                          f"got {type(jobs).__name__}", file=sys.stderr)
                    return 2
                _print(role_compare(jobs, profile), args.json)
        return 0

    return {"i12-demo": _cmd}
