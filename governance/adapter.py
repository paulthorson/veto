#!/usr/bin/env python3
"""Adapter: gate job applications through the user's agentic governance framework.

The framework (``adversarial-agents`` / ``agentic-governance``) exposes an MCP
server whose tools are plain Python functions (verified: ``@mcp.tool()`` in the
mcp v1 SDK returns the decorated function unchanged). This project, however,
runs on **mcp 2.x**, where ``mcp.server.fastmcp`` no longer exists, so the
framework's server module cannot be imported as-is. The framework's tool logic
(veto keyword scan, verdict JSONL log, constitution file reads) is pure
stdlib — only the ``@mcp.tool()`` decorator touches the mcp package. This
adapter therefore injects a minimal ``mcp.server.fastmcp`` stand-in whose
``tool()`` decorator is the identity function (exactly replicating the
verified v1 behavior), then imports the framework's real server module. All
veto/review/verdict logic executed here is the framework's own code.

Fail-open / fail-closed policy (also documented in README "Governance"):

+-------------------------------+-------------------------------------------+
| Check                         | On framework error                        |
+-------------------------------+-------------------------------------------+
| Veto screen (check_veto)      | FAIL CLOSED: block, reason                |
|                               | "governance_error". The veto is the hard  |
|                               | safety gate; an unevaluated gate must not |
|                               | be treated as passed.                     |
| Cover-letter honesty          | FAIL CLOSED on framework veto-check       |
| (framework part)              | errors; FAIL OPEN (warn) on local         |
|                               | claim-extraction heuristic errors.        |
| Qualification assessment      | FAIL OPEN: warn ("qualification_unknown" |
|                               | when no profile; advisory otherwise). A   |
|                               | crash in scoring must not block a human's |
|                               | application. Missing profile never       |
|                               | blocks — it warns to run the wizard.      |
| Adversarial review            | FAIL OPEN: warn, do not block             |
| (run_review)                  | (prompt assembly / LLM unavailability).   |
+-------------------------------+-------------------------------------------+

Only a human can clear a veto: edit the application (or profile/cover
letter), then re-run. The framework also ships ``overturn_verdict`` for
its own ledger; this adapter never overturns automatically.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import types
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.governance")

#: Governance domain used for job applications. "universal" is the catch-all
#: domain whose vetoes cover irrecoverable harm.
GOVERNANCE_DOMAIN = "universal"

#: Qualification score bands (see assess_qualification).
QUALIFIED_THRESHOLD = 0.65   # >= : proceed silently
BORDERLINE_THRESHOLD = 0.40  # >= : proceed with qualification_warning; < : block


class GovernanceError(Exception):
    """The governance framework itself failed (distinct from a veto)."""


class GovernanceUnavailable(GovernanceError):
    """No usable governance framework installation was found."""


# ---------------------------------------------------------------------------
# Framework discovery / loading
# ---------------------------------------------------------------------------


def find_governance_root() -> Path | None:
    """Locate the governance framework installation.

    Search order: ``GOVERNANCE_ROOT`` env var, ``~/agentic-governance``,
    ``~/adversarial-agents``. Returns None when nothing is found.
    """
    candidates: list[Path] = []
    env = os.environ.get("GOVERNANCE_ROOT")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "agentic-governance")
    candidates.append(Path.home() / "adversarial-agents")
    for candidate in candidates:
        if (candidate / "mcp" / "adversarial_mcp" / "server.py").is_file():
            return candidate
    return None


def _install_fastmcp_shim() -> None:
    """Inject a minimal stand-in for ``mcp.server.fastmcp``.

    The installed ``mcp`` package here is v2 (FastMCP was renamed to
    MCPServer), so the framework's ``from mcp.server.fastmcp import FastMCP``
    would fail. The shim's ``tool()`` decorator is the identity function,
    which exactly replicates the verified mcp v1 behavior (decorated tools
    remain plain, directly callable functions). Only the leaf module name is
    injected into ``sys.modules``; the real ``mcp`` / ``mcp.server``
    packages are untouched.
    """
    if "mcp.server.fastmcp" in sys.modules:
        return
    stub = types.ModuleType("mcp.server.fastmcp")

    class _ShimFastMCP:
        def __init__(self, name: str = "") -> None:
            self.name = name

        def tool(self, *dargs: Any, **dkwargs: Any):
            def _deco(fn):
                return fn  # identity: tools stay plain functions

            return _deco

    stub.FastMCP = _ShimFastMCP  # type: ignore[attr-defined]
    sys.modules["mcp.server.fastmcp"] = stub


_CACHE: dict[str, Any] = {"root": None, "module": None}


def load_framework() -> types.ModuleType:
    """Import the framework's MCP server module and return it.

    Raises:
        GovernanceUnavailable: if no framework installation is found or the
            expected tools are not callable.
    """
    if _CACHE["module"] is not None:
        return _CACHE["module"]
    root = find_governance_root()
    if root is None:
        raise GovernanceUnavailable(
            "No governance framework found. Set GOVERNANCE_ROOT or install "
            "the framework at ~/agentic-governance."
        )
    _install_fastmcp_shim()
    mcp_dir = str(root / "mcp")
    added = mcp_dir not in sys.path
    if added:
        sys.path.insert(0, mcp_dir)
    try:
        # Drop stale imports so a changed GOVERNANCE_ROOT is honored.
        for stale in (
            "adversarial_mcp",
            "adversarial_mcp.server",
            "adversarial_mcp.setup_wizard",
        ):
            sys.modules.pop(stale, None)
        os.environ["ADVERSARIAL_ROOT"] = str(root)
        module = importlib.import_module("adversarial_mcp.server")
    finally:
        if added:
            try:
                sys.path.remove(mcp_dir)
            except ValueError:
                pass
    for name in ("check_veto", "run_review", "record_verdict"):
        if not callable(getattr(module, name, None)):
            raise GovernanceUnavailable(
                f"Framework module has no callable {name!r}; "
                "is this a compatible framework version?"
            )
    _redirect_verdict_log_if_needed(module, root)
    _CACHE["root"] = root
    _CACHE["module"] = module
    return module


def _redirect_verdict_log_if_needed(module: types.ModuleType, root: Path) -> None:
    """Keep verdict writes away from read-only framework checkouts.

    Honors ``GOVERNANCE_VERDICT_LOG`` when set. Otherwise, if the
    framework's own ``runs/`` directory is not writable (e.g. a read-only
    reference clone), verdicts are redirected to this project's
    ``runs/governance-verdicts.jsonl`` instead of modifying the checkout.
    """
    override = os.environ.get("GOVERNANCE_VERDICT_LOG")
    if override:
        target = Path(override).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        module.VERDICT_LOG = target
        return
    runs_dir = root / "runs"
    try:
        writable = (
            os.access(runs_dir, os.W_OK)
            if runs_dir.exists()
            else os.access(root, os.W_OK)
        )
    except OSError:
        writable = False
    if not writable:
        alt = (
            Path(__file__).resolve().parent.parent
            / "runs"
            / "governance-verdicts.jsonl"
        )
        alt.parent.mkdir(parents=True, exist_ok=True)
        module.VERDICT_LOG = alt
        log.info("Framework runs/ not writable; verdicts -> %s", alt)


def governance_enabled() -> bool:
    """True when a usable governance framework is installed."""
    try:
        load_framework()
        return True
    except GovernanceUnavailable:
        return False


def project_dir() -> Path:
    """This project's root (parent of the governance/ package)."""
    return Path(__file__).resolve().parent.parent


def load_saved_profile() -> dict[str, Any]:
    """Load the wizard-generated profile, or {} when absent/unreadable."""
    path = project_dir() / "profiles" / "profile.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("No saved profile at %s: %s", path, exc)
        return {}


# ---------------------------------------------------------------------------
# Veto + adversarial review
# ---------------------------------------------------------------------------


def _job_id_of(job: dict[str, Any] | str) -> str:
    if isinstance(job, dict):
        return str(job.get("job_id", ""))
    return str(job)


def review_application(
    job: dict[str, Any],
    profile: dict[str, Any] | None,
    cover_letter: str = "",
) -> dict[str, Any]:
    """Veto screen + adversarial review for one application.

    Args:
        job: dict with title/company/location/board/url/job_id.
        profile: applicant profile dict (may be empty).
        cover_letter: cover letter text (may be empty).

    Returns:
        ``{"passed": bool, "veto_hits": [...], "review": dict|None,
        "warnings": [...], "reason": str}``. ``reason`` is "" when passed,
        "veto" on a veto hit, "review_veto" when the adversarial review
        itself returns KICK_BACK.

    Raises:
        GovernanceError: if the veto check itself fails (fail closed).
        GovernanceUnavailable: if the framework is not installed.
    """
    mod = load_framework()
    applicant = (profile or {}).get("full_name") or "the applicant"
    summary = (
        f"Automated job application: {applicant} intends to submit an "
        f"application for the role '{job.get('title', 'Unknown')}' at "
        f"{job.get('company', 'Unknown')} ({job.get('location', 'Unknown')}) "
        f"via the {job.get('board', 'unknown')} board. The agent will fill "
        f"the employer's application form using the applicant's resume and "
        f"cover letter and submit it."
    )
    try:
        veto = mod.check_veto(domain=GOVERNANCE_DOMAIN, text=summary)
    except Exception as exc:
        raise GovernanceError(f"governance veto check failed: {exc}") from exc
    hits = veto.get("veto_hits", []) if isinstance(veto, dict) else []
    if hits:
        return {
            "passed": False,
            "veto_hits": hits,
            "review": None,
            "warnings": [],
            "reason": "veto",
        }

    warnings: list[str] = []
    review: dict[str, Any] | None = None
    try:
        work = (
            f"Role: {job.get('title', 'Unknown')} at "
            f"{job.get('company', 'Unknown')} "
            f"({job.get('location', 'Unknown')})\n"
            f"Board: {job.get('board', 'unknown')}\n"
            f"Apply URL: {job.get('url', '')}\n"
            f"Applicant: {applicant}\n"
            "Action: fill the employer's application form with the "
            "applicant's resume"
            + (" and cover letter, then submit it." if cover_letter else ".")
        )
        review = mod.run_review(
            domain=GOVERNANCE_DOMAIN,
            work=work,
            context="automated job application (Veto)",
            source="veto-mcp",
        )
    except Exception as exc:  # fail open: review is advisory
        warnings.append(f"adversarial review failed (non-blocking): {exc}")
        log.warning("run_review failed: %s", exc)

    if isinstance(review, dict) and review.get("verdict") == "KICK_BACK":
        return {
            "passed": False,
            "veto_hits": review.get("veto_hits", []),
            "review": review,
            "warnings": warnings,
            "reason": "review_veto",
        }
    return {
        "passed": True,
        "veto_hits": [],
        "review": review,
        "warnings": warnings,
        "reason": "",
    }


def record_application_verdict(
    job: dict[str, Any] | str, passed: bool, summary: str
) -> dict[str, Any]:
    """Append an allow/block verdict for an application to the ledger.

    Never raises: recording failures are logged and reported in the return
    value so a ledger outage can never block or crash an application.
    """
    try:
        mod = load_framework()
    except GovernanceUnavailable as exc:
        return {"recorded": False, "error": str(exc)}
    try:
        return mod.record_verdict(
            domain=GOVERNANCE_DOMAIN,
            verdict="pass" if passed else "veto",
            summary=summary,
            ticket=f"veto:{_job_id_of(job)}",
            case_tag="application-allowed" if passed else "application-blocked",
        )
    except Exception as exc:
        log.warning("record_verdict failed: %s", exc)
        return {"recorded": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Skill / experience extraction
# ---------------------------------------------------------------------------

#: Skill vocabulary for requirement/claim extraction. Lowercase; matching is
#: case-insensitive with non-word lookarounds. Deliberately excludes
#: ultra-short tokens ("r", "go", "c") that false-positive on plain English.
SKILL_VOCABULARY: frozenset[str] = frozenset(
    {
        # languages
        "python", "java", "javascript", "typescript", "golang", "rust", "c++",
        "c#", "ruby", "php", "swift", "kotlin", "scala", "matlab", "bash",
        "powershell", "perl",
        # frontend
        "react", "angular", "vue.js", "next.js", "node.js", "html", "css",
        "sass", "webpack",
        # backend / data stores
        "sql", "postgresql", "postgres", "mysql", "mongodb", "redis",
        "elasticsearch", "graphql", "rest api", "grpc", "kafka", "rabbitmq",
        # data / ml
        "pandas", "numpy", "scikit-learn", "tensorflow", "pytorch",
        "machine learning", "deep learning", "nlp",
        "natural language processing", "data analysis", "data science",
        "etl", "spark", "airflow", "tableau", "power bi", "excel",
        "statistics",
        # cloud / devops
        "aws", "azure", "gcp", "google cloud", "docker", "kubernetes",
        "terraform", "jenkins", "ci/cd", "linux", "git", "github",
        "gitlab", "ansible",
        # mobile
        "ios", "android", "react native", "flutter",
        # security
        "cybersecurity", "penetration testing", "networking",
        # general professional
        "project management", "agile", "scrum", "jira", "communication",
        "leadership", "mentoring", "sales", "marketing", "seo",
        "content marketing", "copywriting", "customer service",
        "accounting", "bookkeeping", "quickbooks", "financial modeling",
        "product management", "roadmap", "a/b testing", "user research",
        "figma", "photoshop", "illustrator", "video editing",
        "salesforce", "hubspot", "recruiting", "sourcing",
        "distributed systems", "microservices", "system design",
    }
)


def _extract_skills(
    text: str, vocabulary: frozenset[str] = SKILL_VOCABULARY
) -> set[str]:
    """Return the vocabulary skills mentioned in text (case-insensitive)."""
    found: set[str] = set()
    low = text.lower()
    for skill in vocabulary:
        if re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", low):
            found.add(skill)
    return found


_YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)


def _extract_years_of_experience(text: str) -> float | None:
    """Best-effort required-years extraction ("5+ years of experience").

    Takes the maximum N where an N-years mention appears near an
    experience-related word, to avoid matching e.g. "2 years warranty".
    """
    best: float | None = None
    for m in _YEARS_RE.finditer(text):
        window = text[max(0, m.start() - 50) : m.end() + 30].lower()
        if "experien" in window or "exp." in window or "exp " in window:
            yrs = float(m.group(1))
            best = yrs if best is None else max(best, yrs)
    return best


def _coerce_years(value: Any) -> float | None:
    """Coerce a profile years_experience value ("5", 5, "5+ years") to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"(\d+(?:\.\d+)?)", str(value))
    return float(m.group(1)) if m else None


def _profile_skills(profile: dict[str, Any]) -> set[str]:
    skills = profile.get("skills") or []
    return {str(s).strip().lower() for s in skills if str(s).strip()}


def _profile_corpus(profile: dict[str, Any]) -> str:
    """Lowercased blob of everything the profile claims (for honesty checks)."""
    parts: list[str] = []
    for key in ("summary", "full_name"):
        val = profile.get(key)
        if val:
            parts.append(str(val))
    for title in profile.get("target_titles") or []:
        parts.append(str(title))
    for exp in profile.get("experience") or []:
        if isinstance(exp, dict):
            for key in ("title", "company", "description"):
                if exp.get(key):
                    parts.append(str(exp[key]))
    parts.extend(sorted(_profile_skills(profile)))
    return " ".join(parts).lower()


# ---------------------------------------------------------------------------
# Qualification assessment
# ---------------------------------------------------------------------------


def assess_qualification(
    job_details: dict[str, Any], profile: dict[str, Any] | None
) -> dict[str, Any]:
    """Assess whether the applicant is qualified for the job.

    Compares skills/keywords and years-of-experience extracted from the job
    description/requirements against the profile (skills, years_experience,
    past titles).

    Returns a dict with ``qualified`` (bool, or None when no profile),
    ``score`` 0..1, ``matched_skills``, ``missing_skills``,
    ``experience_years_required`` / ``experience_years_user``,
    ``severe_experience_gap`` (user has < 50% of required years),
    ``qualification_unknown`` (no profile: fail open), and ``guidance``.
    """
    result: dict[str, Any] = {
        "qualified": None,
        "score": None,
        "matched_skills": [],
        "missing_skills": [],
        "experience_years_required": None,
        "experience_years_user": None,
        "severe_experience_gap": False,
        "qualification_unknown": False,
        "guidance": "",
    }
    if not profile:
        result["qualification_unknown"] = True
        result["guidance"] = (
            "No applicant profile available — run the onboarding wizard "
            "(python3 wizard.py) so applications can be checked for fit. "
            "Proceeding without a qualification check."
        )
        return result

    text = "\n".join(
        str(job_details.get(k) or "") for k in ("title", "description", "requirements")
    )
    required = _extract_skills(text)
    user_skills = _profile_skills(profile)
    corpus = _profile_corpus(profile)

    def _supported(skill: str) -> bool:
        if skill in user_skills:
            return True
        return bool(
            re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", corpus)
        )

    matched = sorted(s for s in required if _supported(s))
    missing = sorted(s for s in required if s not in matched)
    req_years = _extract_years_of_experience(text)
    user_years = _coerce_years(profile.get("years_experience"))

    skill_ratio = (len(matched) / len(required)) if required else None
    if skill_ratio is None and req_years is None:
        score = 0.55  # nothing extractable: borderline -> warn, proceed
        basis = "no explicit skill or experience requirements extracted"
    else:
        skill_component = skill_ratio if skill_ratio is not None else 0.6
        if req_years is None:
            exp_component = 1.0
        elif user_years is None:
            exp_component = 0.7  # unknown user experience: neutral-ish
        elif req_years > 0:
            exp_component = min(1.0, user_years / req_years)
        else:
            exp_component = 1.0
        score = round(0.65 * skill_component + 0.35 * exp_component, 2)
        basis = (
            f"{len(matched)}/{len(required)} skills"
            if required
            else "experience only"
        )

    severe_gap = bool(
        req_years and user_years is not None and user_years < 0.5 * req_years
    )
    qualified = (score >= QUALIFIED_THRESHOLD) and not severe_gap

    bits: list[str] = [f"fit score {score:.2f} ({basis})"]
    if required:
        bits.append(f"matched skills: {', '.join(matched) or 'none'}")
        if missing:
            bits.append(f"missing: {', '.join(missing[:8])}")
    if req_years is not None:
        have = f"{user_years:g}" if user_years is not None else "unknown"
        bits.append(f"requires ~{req_years:g} yrs experience; profile shows {have}")
    if severe_gap:
        bits.append("experience gap is severe (<50% of required)")
    if qualified:
        bits.append("verdict: qualified — proceed")
    elif score >= BORDERLINE_THRESHOLD:
        bits.append("verdict: borderline fit — proceed with caution")
    else:
        bits.append("verdict: poor fit — recommend not applying")

    result.update(
        qualified=qualified,
        score=score,
        matched_skills=matched,
        missing_skills=missing,
        experience_years_required=req_years,
        experience_years_user=user_years,
        severe_experience_gap=severe_gap,
        guidance="; ".join(bits) + ".",
    )
    return result


# ---------------------------------------------------------------------------
# Cover-letter honesty
# ---------------------------------------------------------------------------


def check_cover_letter_honesty(
    cover_letter: str,
    profile: dict[str, Any] | None,
    mod: Any | None = None,
) -> dict[str, Any]:
    """Check the cover letter against the profile for unsupported claims.

    Two layers: (a) the framework's veto keyword scan on the letter itself
    (fail closed on framework errors); (b) a best-effort local comparison of
    skills/years claimed in the letter vs. what the profile supports
    (fail open on heuristic errors).

    Returns ``{"passed": bool, "hits": [...], "warnings": [...]}``.

    Raises:
        GovernanceError: if the framework veto check fails (fail closed).
    """
    mod = mod if mod is not None else load_framework()
    hits: list[str] = []
    warnings: list[str] = []
    try:
        veto = mod.check_veto(domain=GOVERNANCE_DOMAIN, text=cover_letter)
    except Exception as exc:
        raise GovernanceError(
            f"cover-letter veto check failed: {exc}"
        ) from exc
    for hit in veto.get("veto_hits", []) if isinstance(veto, dict) else []:
        hits.append(f"veto:{hit}")

    if not profile:
        warnings.append("no profile: cover-letter claims could not be verified")
        return {"passed": not hits, "hits": hits, "warnings": warnings}

    try:
        claimed_skills = _extract_skills(cover_letter)
        user_skills = _profile_skills(profile)
        corpus = _profile_corpus(profile)
        for skill in sorted(claimed_skills):
            if skill in user_skills:
                continue
            if re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", corpus):
                continue
            hits.append(f"unsupported-claim:skill:{skill}")
        claimed_years = _extract_years_of_experience(cover_letter)
        user_years = _coerce_years(profile.get("years_experience"))
        if (
            claimed_years is not None
            and user_years is not None
            and claimed_years > user_years + 0.5
        ):
            hits.append(
                f"unsupported-claim:experience:{claimed_years:g}yrs claimed "
                f"vs {user_years:g} in profile"
            )
    except Exception as exc:  # heuristic layer fails open
        warnings.append(f"claim extraction failed (non-blocking): {exc}")
        log.warning("cover-letter claim extraction failed: %s", exc)

    return {"passed": not hits, "hits": hits, "warnings": warnings}


# ---------------------------------------------------------------------------
# Full gate (used by server.py's apply_to_job confirm path)
# ---------------------------------------------------------------------------


def _blocked_response(
    reason: str,
    message: str,
    veto_hits: list[str],
    assessment: dict[str, Any] | None,
    review: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": "governance_blocked",
        "reason": reason,
        "message": message,
        "veto_hits": veto_hits,
        "assessment": assessment,
        "review": review,
    }


def governance_gate(
    *,
    job_id: str,
    job: dict[str, Any],
    job_details: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    cover_letter: str = "",
) -> dict[str, Any]:
    """Run the full governance gate for one confirmed application.

    Order: (1) veto screen + adversarial review, (2) cover-letter honesty,
    (3) qualification assessment. Returns::

        {"enabled": False}                                    # no framework: caller proceeds
        {"enabled": True, "blocked": True, "reason": ...,
         "response": {...}, "recorded": {...}}                # blocked; veto verdict recorded
        {"enabled": True, "blocked": False, "warnings": [...],
         "assessment": {...}, "review": {...}}                # proceed; warnings are advisory

    A blocked gate means NOTHING was submitted or logged to
    applications.json — the caller must return ``response`` immediately.
    """
    try:
        load_framework()
    except GovernanceUnavailable:
        return {"enabled": False}

    # 1. Veto screen + adversarial review (veto-check errors fail closed).
    try:
        ra = review_application(job, profile, cover_letter)
    except GovernanceError as exc:
        recorded = record_application_verdict(
            job_id, False, f"governance error, fail-closed: {exc}"
        )
        return {
            "enabled": True,
            "blocked": True,
            "reason": "governance_error",
            "response": _blocked_response(
                "governance_error",
                f"Blocked: the governance veto check itself failed ({exc}). "
                "Fail-closed: resolve the framework error and re-run. Only a "
                "human can clear this.",
                [],
                None,
                None,
            ),
            "recorded": recorded,
        }
    if not ra["passed"]:
        recorded = record_application_verdict(
            job_id,
            False,
            f"governance {ra['reason']}: veto_hits={ra['veto_hits']}",
        )
        return {
            "enabled": True,
            "blocked": True,
            "reason": ra["reason"],
            "response": _blocked_response(
                ra["reason"],
                "Blocked by governance veto. Only a human can clear a veto: "
                "edit the application details and re-run, or use the "
                "framework's overturn_verdict with a reason.",
                ra["veto_hits"],
                None,
                ra["review"],
            ),
            "recorded": recorded,
        }
    warnings: list[str] = list(ra.get("warnings", []))

    # 2. Cover-letter honesty (framework errors fail closed).
    if cover_letter:
        try:
            honesty = check_cover_letter_honesty(cover_letter, profile)
        except GovernanceError as exc:
            recorded = record_application_verdict(
                job_id, False, f"governance error, fail-closed: {exc}"
            )
            return {
                "enabled": True,
                "blocked": True,
                "reason": "governance_error",
                "response": _blocked_response(
                    "governance_error",
                    f"Blocked: cover-letter governance check failed ({exc}). "
                    "Fail-closed: resolve the framework error and re-run.",
                    [],
                    None,
                    ra["review"],
                ),
                "recorded": recorded,
            }
        warnings.extend(honesty.get("warnings", []))
        if not honesty["passed"]:
            recorded = record_application_verdict(
                job_id,
                False,
                f"cover-letter honesty hits: {honesty['hits']}",
            )
            return {
                "enabled": True,
                "blocked": True,
                "reason": "cover_letter_honesty",
                "response": _blocked_response(
                    "cover_letter_honesty",
                    "Blocked: the cover letter claims qualifications the "
                    "profile does not support. Do not submit claims you "
                    "cannot back up — fix the letter or the profile and "
                    "re-run.",
                    honesty["hits"],
                    None,
                    ra["review"],
                ),
                "recorded": recorded,
            }

    # 3. Qualification assessment (fail open on missing data / errors).
    assessment: dict[str, Any] | None = None
    try:
        assessment = assess_qualification(job_details or {}, profile)
    except Exception as exc:  # fail open: scoring must never block by crashing
        warnings.append(f"qualification assessment failed (non-blocking): {exc}")
        log.warning("assess_qualification failed: %s", exc)
    if assessment is not None:
        if assessment.get("qualification_unknown"):
            warnings.append("qualification_unknown: " + assessment["guidance"])
        elif assessment["score"] is not None and (
            assessment["score"] < BORDERLINE_THRESHOLD
            or assessment.get("severe_experience_gap")
        ):
            recorded = record_application_verdict(
                job_id,
                False,
                f"qualification block: score={assessment['score']}, "
                f"missing={assessment['missing_skills'][:5]}",
            )
            return {
                "enabled": True,
                "blocked": True,
                "reason": "qualification",
                "response": _blocked_response(
                    "qualification",
                    "Blocked: the applicant does not appear qualified for "
                    "this role. " + assessment["guidance"] + " Only a human "
                    "can override this — confirm the fit manually and re-run "
                    "if you disagree.",
                    [],
                    assessment,
                    ra["review"],
                ),
                "recorded": recorded,
            }
        elif (
            assessment["score"] is not None
            and assessment["score"] < QUALIFIED_THRESHOLD
        ):
            warnings.append("qualification_warning: " + assessment["guidance"])

    return {
        "enabled": True,
        "blocked": False,
        "reason": "",
        "warnings": warnings,
        "assessment": assessment,
        "review": ra["review"],
    }
