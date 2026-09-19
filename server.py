#!/usr/bin/env python3
"""Veto MCP server.

Exposes job-board search and assisted-application tools over the Model
Context Protocol (MCP) so an AI agent can:

  * search jobs across official board APIs (Greenhouse, Lever, Ashby,
    Adzuna; stub for Glassdoor),
  * fetch full job descriptions,
  * prepare (and, only with explicit user confirmation, record)
    job applications without manual form filling.

Phase 1 application support returns an application *preview* plus the direct
``apply_url`` and extracted form fields, and logs the attempt to
``applications.json``. It never auto-submits a form. A commented Phase 2
hook sketches future Playwright-based browser automation.

Safety: scraping job boards may violate their Terms of Service and can
trigger rate limits / CAPTCHAs / account restrictions. Built-in politeness
(1-2s delays between requests) reduces but does not eliminate that risk.
Every outbound request carries a single honest, identifying User-Agent.
"""

from __future__ import annotations

import abc
import base64
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

# API-based providers (Greenhouse / Lever / Ashby / Adzuna) live in
# providers/. They intentionally do NOT import server.py (no circular
# imports); server.py imports them here. Job ids stay self-describing
# (<board>:<base64url(payload)>), so _decode_job_id needs no changes.
try:
    from providers import (
        AdzunaProvider,
        AshbyProvider,
        GreenhouseProvider,
        LeverProvider,
    )
    # The single honest User-Agent (defined in providers/_common.py).
    from providers._common import VETO_USER_AGENT

    _API_PROVIDERS: dict[str, Any] = {
        "greenhouse": GreenhouseProvider(),
        "lever": LeverProvider(),
        "ashby": AshbyProvider(),
        "adzuna": AdzunaProvider(),
    }
except ImportError as exc:  # defensive: the server must still start
    log.warning("providers/ package failed to import; API boards disabled: %s", exc)
    _API_PROVIDERS = {}
    # Fallback copy of the honest User-Agent, defined only when the providers
    # package itself is unimportable. The canonical definition is
    # providers._common.VETO_USER_AGENT; this literal must match it.
    VETO_USER_AGENT = "veto/0.1.0 (+https://github.com/paulthorson/veto)"  # noqa: F811

# Application-quality helpers (dedup.py, prefs.py, filters.py, tailor.py).
# Imported defensively: the server must start even if one is missing.
try:
    from dedup import dedupe_jobs
except ImportError as exc:
    log.warning("dedup.py unavailable; result dedup disabled: %s", exc)
    dedupe_jobs = None  # type: ignore[assignment]

try:
    from prefs import apply_preferences, load_preferences, save_preferences
except ImportError as exc:
    log.warning("prefs.py unavailable; company preferences disabled: %s", exc)
    apply_preferences = None  # type: ignore[assignment]
    load_preferences = None  # type: ignore[assignment]
    save_preferences = None  # type: ignore[assignment]

try:
    from filters import filter_by_salary, filter_by_seniority
except ImportError as exc:
    log.warning("filters.py unavailable; salary/seniority filters disabled: %s", exc)
    filter_by_salary = None  # type: ignore[assignment]
    filter_by_seniority = None  # type: ignore[assignment]


def _sanitize_proxy_env() -> None:
    """Drop no_proxy entries that httpx cannot parse.

    Some environments ship entries like ``*[::1]`` or bracketed IPv6
    (``[::1]``). httpx turns any entry it doesn't recognize as IPv4/IPv6
    into an ``all://*<host>`` pattern, and ``URLPattern`` then crashes at
    client init ("Invalid port"). The unbracketed equivalents (``::1``)
    are kept and parsed correctly, so dropping the problematic forms is
    safe.
    """
    for var in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(var)
        if not val:
            continue
        kept = [
            entry
            for entry in val.split(",")
            if "*" not in entry and "[" not in entry and "]" not in entry
        ]
        os.environ[var] = ",".join(kept)


_sanitize_proxy_env()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [veto-mcp] %(message)s",
)
log = logging.getLogger("veto-mcp")

# ---------------------------------------------------------------------------
# Enhancement plugins (grill, email sync, briefs, ATS apply,
# queue, analytics, doctor) and the risk/governance layer. Imported
# defensively: the server must start even when one is missing; missing
# pieces are skipped at registration with a warning.
# ---------------------------------------------------------------------------
try:
    import compliance as _compliance
except ImportError as exc:
    log.warning("compliance.py unavailable; risk budgets disabled: %s", exc)
    _compliance = None  # type: ignore[assignment]

try:
    from governance import risk_policy as _risk_policy
except ImportError as exc:
    log.warning(
        "governance.risk_policy unavailable; risk gates disabled: %s", exc)
    _risk_policy = None  # type: ignore[assignment]

try:
    from providers.glassdoor import WHY_NOT_SUPPORTED as _GLASSDOOR_REASON
except ImportError as exc:
    log.warning("providers.glassdoor unavailable: %s", exc)
    _GLASSDOOR_REASON = (
        "Glassdoor is not supported: it has no public API, and its "
        "search pages sit behind aggressive bot mitigation that "
        "plain-HTTP fetching cannot pass."
    )

_PLUGIN_MODULES: list = []
for _plugin_name in (
    "ats_apply",
    "apply_queue",
    "doctor",
    "analytics",
    "briefs",
    "byol",
    "email_sync",
    "grill",
    "match",
    "radar",
    "followup",
    "referrals",
    "mock_interview",
    "soft_skills",
    "ai_proficiency",
    "mentors",
    "offer_compare",
    "skill_gaps",
    "cover_letters",
    "linkedin_optimizer",
    "crew",
    "jd_decoder",
    "network_crm",
    "rejection_autopsy",
    "streaks",
    "notify",
    "provider_health",
    "reply_radar",
    "action_inbox",
    "calibration",
    "model_card",
    "near_miss",
    "funnels",
    "experiment_ledger",
    "outcome_nudges",
):
    try:
        _PLUGIN_MODULES.append((_plugin_name, __import__(_plugin_name)))
    except ImportError as exc:
        log.warning(
            "plugin %s unavailable; its tools are disabled: %s",
            _plugin_name,
            exc,
        )
del _plugin_name

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
APPLICATIONS_FILE = BASE_DIR / "applications.json"
WATCHES_FILE = BASE_DIR / "watches.json"

# Lifecycle (application stages / stats / follow-ups), job watches, and
# multi-profile helpers live in their own modules so the tools below stay
# thin. They use stdlib + local code only (no heavy deps).
import lifecycle
import watch as watch_store

# In-memory cache of jobs returned by recent search_jobs calls, keyed by
# job_id. Enriches previews/details; job_ids are also self-describing
# (board + base64url of the job URL) so they work without a prior search.
_JOB_REGISTRY: dict[str, dict[str, Any]] = {}


def _headers() -> dict[str, str]:
    """Return request headers that identify with the single honest Veto User-Agent."""
    return {
        "User-Agent": VETO_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _polite_delay() -> None:
    """Sleep 1-2s between requests to respect rate limits."""
    delay = random.uniform(1.0, 2.0)
    log.debug("Politeness delay: %.2fs", delay)
    time.sleep(delay)


def _encode_job_id(board: str, url: str) -> str:
    """Build a self-describing job id: ``<board>:<base64url(url)>``."""
    token = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{board}:{token}"


def _decode_job_id(job_id: str) -> tuple[str, str]:
    """Split a job id back into ``(board, url)``.

    Raises:
        ValueError: If the job id is malformed.
    """
    board, sep, token = job_id.partition(":")
    if not sep or not board or not token:
        raise ValueError(f"Malformed job_id: {job_id!r}")
    padded = token + "=" * (-len(token) % 4)
    url = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    return board, url


def _load_applications() -> list[dict[str, Any]]:
    """Read the local applications store (empty list if missing/corrupt)."""
    try:
        return json.loads(APPLICATIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", APPLICATIONS_FILE, exc)
        return []


def _save_applications(applications: list[dict[str, Any]]) -> None:
    """Persist the local applications store."""
    APPLICATIONS_FILE.write_text(
        json.dumps(applications, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


PROFILE_FILE = BASE_DIR / "profiles" / "profile.json"


def _load_saved_profile() -> dict[str, Any]:
    """Return the wizard-saved applicant profile, or {} if none exists."""
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("No saved profile at %s: %s", PROFILE_FILE, exc)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class JobProvider(abc.ABC):
    """Pluggable job-board provider."""

    name: str = "base"

    @abc.abstractmethod
    def search(
        self, query: str, location: str, limit: int, remote_only: bool
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` job dicts for the query/location."""

    @abc.abstractmethod
    def get_details(self, url: str) -> dict[str, Any]:
        """Return full details for a job posting URL."""


class GlassdoorProvider(JobProvider):
    """Documented stub: Glassdoor cannot be implemented within policy.

    The full rationale lives in providers/glassdoor.py (no public API,
    partner-only access; React search pages behind aggressive bot
    mitigation that instantly CAPTCHAs/blocks plain-HTTP fetches; the
    policy forbids CAPTCHA bypass, and an httpx scraper would return
    fake-empty results). The compliant path is a future logged-in
    Playwright session provider, which this plain-HTTP layer cannot do.
    """

    name = "glassdoor"

    def search(self, query: str, location: str, limit: int, remote_only: bool):
        raise NotImplementedError(_GLASSDOOR_REASON)

    def get_details(self, url: str):
        raise NotImplementedError(_GLASSDOOR_REASON)


PROVIDERS: dict[str, JobProvider] = {
    "glassdoor": GlassdoorProvider(),
    **_API_PROVIDERS,
}

# Bring-your-own-listing pseudo-provider (legal-hardening commit 7, spec
# section 5.2): the user's pasted/URL/file listings, board name "user".
# Imported defensively like the API providers above: the server must
# still start if byol is missing. Deliberately NOT in ACTIVE_BOARDS —
# "user" is a personal listing store, not a board to query on "all".
try:
    import byol as _byol

    PROVIDERS["user"] = _byol.UserListingsBoard()
except ImportError as exc:  # defensive: the server must still start
    log.warning("byol provider unavailable; user listings disabled: %s", exc)

ACTIVE_BOARDS = {
    "greenhouse",
    "lever",
    "ashby",
    "adzuna",
}


# ---------------------------------------------------------------------------
# Phase 2: browser-automation apply hook (browser_apply.py)
# ---------------------------------------------------------------------------
#
# Implemented in browser_apply.py (Playwright, sync API). Enable with the
# environment variable JOB_MCP_BROWSER_APPLY=1. When enabled and
# apply_to_job(..., confirm=True) is called, the server opens the job's
# apply_url in a browser, fills the form from `profile`, uploads
# the resume, screenshots the completed form, and stops - fill-only.
# The user reviews and clicks submit themselves. Without the env var,
# Phase 1 (preview + local log) behavior is preserved.
#
# Anti-bot note: automated applications can lead to CAPTCHAs / IP throttles
# / account restrictions on LinkedIn, Indeed, etc. Keep human review in the
# loop and apply at a human pace.


def _browser_apply_enabled() -> bool:
    """True when the operator opted into Phase 2 browser automation."""
    return os.environ.get("JOB_MCP_BROWSER_APPLY") == "1"


# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "veto",
    instructions=(
        "Search jobs across major boards and prepare assisted job "
        "applications. apply_to_job NEVER submits. "
        "Without the JOB_MCP_BROWSER_APPLY env var, confirm=True only "
        "records the application locally (Phase 1) and the user completes "
        "the submission in their browser; with JOB_MCP_BROWSER_APPLY=1, "
        "confirm=True auto-fills the form in a browser via a profile dict "
        "(Phase 2) and waits for the user's own submit click."
    ),
)


@mcp.tool()
def list_boards() -> list[dict[str, str]]:
    """List supported job boards and whether each provider is active or a stub."""
    notes = {
        "adzuna": "needs ADZUNA_APP_ID + ADZUNA_APP_KEY env vars (developer.adzuna.com)",
        "glassdoor": "deliberately unsupported: no public API; plain-HTTP "
        "blocked by bot mitigation (see providers/glassdoor.py)",
        "user": "your saved listings: add-listing (paste text / URL / HTML "
        "file). Searched explicitly with board='user'; never queried on 'all'.",
    }
    return [
        {
            "board": name,
            "status": "active" if name in ACTIVE_BOARDS else "stub",
            "notes": (
                "search + details supported"
                if name in ACTIVE_BOARDS
                else notes.get(name, "not yet implemented")
            ),
        }
        for name in PROVIDERS
    ]


@mcp.tool()
def search_jobs(
    query: str,
    location: str,
    board: str = "all",
    limit: int = 10,
    remote_only: bool = False,
    salary_min: int = 0,
    seniority: str = "",
) -> list[dict[str, Any]]:
    """Search job postings across major boards.

    Args:
        query: Job title / keywords, e.g. "software engineer".
        location: e.g. "New York, NY" or "Remote".
        board: One of "all", "greenhouse", "lever",
            "ashby", "adzuna", "glassdoor", "user". "user" searches only
            your saved bring-your-own listings (add-listing) locally —
            no board is queried.
        limit: Max jobs to return per board (default 10).
        remote_only: Only include remote-friendly postings.
        salary_min: Keep jobs whose best salary hint meets this annual USD
            floor (post-filter on listing text; jobs with no salary hint
            are kept). 0 disables.
        seniority: One of entry|mid|senior|staff|principal (post-filter on
            title keywords; untitled/unknown seniority is kept). ""
            disables.

    Returns:
        Deduplicated, preference-filtered list of jobs with id, title,
        company, location, url, board, snippet. Preferred-company matches
        carry "preferred": True and sort first. Every result carries a
        risk tag (board tier + disclosure).

    Every board search is adjudicated by the risk policy first: blocked
    boards (strict mode, exhausted budget, tripped circuit breaker,
    governance veto) raise for an explicit board choice and are skipped
    for "all". See the risk_status tool for the audit surface.
    """
    board = board.lower().strip()
    if board != "all" and board not in PROVIDERS:
        raise ValueError(
            f"Unknown board {board!r}. Use list_boards() to see supported boards."
        )
    targets = (
        [b for b in PROVIDERS if b in ACTIVE_BOARDS]
        if board == "all"
        else [board]
    )
    results: list[dict[str, Any]] = []
    for name in targets:
        # Risk gate (additive): every board search is adjudicated through
        # the risk policy (tiers, search budgets, circuit breakers, the
        # governance veto screen). A blocked board raises for an explicit
        # board choice and is skipped for "all", mirroring stub-board
        # behavior. Every adjudication is written to the audit ledger.
        if _risk_policy is not None:
            _search_verdict = _risk_policy.adjudicate_search(name, query)
            if not _search_verdict.get("allowed", False):
                _reason = _search_verdict.get("reason", "blocked by risk policy")
                log.warning("search skipped for board %s: %s", name, _reason)
                if board != "all":
                    raise ValueError(f"Board {name!r} is not usable: {_reason}")
                continue
        provider = PROVIDERS[name]
        try:
            _found = provider.search(
                query, location, limit=limit, remote_only=remote_only
            )
        except (NotImplementedError, RuntimeError) as exc:
            # NotImplementedError: documented stub boards (glassdoor).
            # RuntimeError: providers needing user credentials (adzuna
            # without ADZUNA_APP_ID/ADZUNA_APP_KEY). Surface as ValueError
            # so the tool contract stays consistent.
            log.warning("%s: %s", name, exc)
            raise ValueError(f"Board {name!r} is not usable: {exc}")
        # Budget accounting + risk tagging.
        # Recovery streaks are recorded by the providers themselves at
        # the exact points where a genuine fetch completed (post
        # block/CAPTCHA checks). Recording here would count blocked or
        # CAPTCHA'd fetches that returned without raising.
        if _compliance is not None:
            _compliance.record_search(name)
            _tag = _compliance.result_risk_tag(name)
            for _job in _found:
                if isinstance(_job, dict):
                    _job.update(_tag)
        results.extend(_found)
    results = results[: limit * len(targets)]

    # Application-quality pipeline (all recall-friendly; each degrades to
    # a no-op when its module failed to import).
    if apply_preferences is not None:
        results = apply_preferences(results)
    if dedupe_jobs is not None:
        before = len(results)
        results = dedupe_jobs(results)
        log.info("dedup: %d -> %d jobs", before, len(results))
    if filter_by_salary is not None:
        results = filter_by_salary(results, salary_min)
    if filter_by_seniority is not None:
        results = filter_by_seniority(results, seniority)
    return results


@mcp.tool()
def get_job_details(job_id: str) -> dict[str, Any]:
    """Fetch the full description for a job from search_jobs.

    Args:
        job_id: The job id returned by search_jobs.

    Returns:
        Dict with title, company, description, requirements, apply_url
        (or an "error" key if the fetch failed).
    """
    try:
        board, url = _decode_job_id(job_id)
    except ValueError as exc:
        return {"job_id": job_id, "error": str(exc)}
    provider = PROVIDERS.get(board)
    if provider is None:
        return {"job_id": job_id, "error": f"Unknown board {board!r}"}
    try:
        details = provider.get_details(url)
    except NotImplementedError as exc:
        return {"job_id": job_id, "error": str(exc)}
    cached = _JOB_REGISTRY.get(job_id, {})
    details.setdefault("title", cached.get("title", "Unknown"))
    details.setdefault("company", cached.get("company", "Unknown"))
    details["job_id"] = job_id
    return details


# ---------------------------------------------------------------------------
# Governance wiring (additive)
#
# Gates confirm=True applications through the user's agentic governance
# framework (governance/adapter.py): constitutional veto screen,
# cover-letter honesty check, qualification assessment, and an advisory
# adversarial review. Every helper below is a no-op when the framework is
# not installed, so behavior is unchanged without it.
# ---------------------------------------------------------------------------


def _governance_precheck(
    *,
    job_id: str,
    board: str,
    url: str,
    preview: dict[str, Any],
    profile: dict[str, str],
    cover_letter: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Run the governance gate before any submission or logging.

    Returns ``(blocked_response, warnings)``. ``blocked_response`` is None
    when the application may proceed; otherwise the caller must return it
    immediately (nothing was submitted or recorded). Never raises:
    unexpected errors degrade to non-blocking warnings, except veto-check
    failures which the adapter fails closed on.
    """
    try:
        from governance import adapter as _gov
    except Exception as exc:
        log.warning("Governance adapter unavailable: %s", exc)
        return None, []
    if not _gov.governance_enabled():
        return None, []
    details: dict[str, Any] = {}
    try:
        fetched = get_job_details(job_id)
        if isinstance(fetched, dict) and not fetched.get("error"):
            details = fetched
    except Exception as exc:
        log.warning("Governance: job details fetch failed: %s", exc)
    effective_profile = dict(profile or {}) or _gov.load_saved_profile()
    try:
        gate = _gov.governance_gate(
            job_id=job_id,
            job={
                "job_id": job_id,
                "title": preview.get("title"),
                "company": preview.get("company"),
                "location": preview.get("location"),
                "board": board,
                "url": url,
            },
            job_details=details,
            profile=effective_profile,
            cover_letter=cover_letter,
        )
    except Exception as exc:  # fail-open: a crashed gate warns, never blocks
        log.warning("Governance gate crashed (non-blocking): %s", exc)
        return None, [f"governance gate error (non-blocking): {exc}"]
    if gate.get("blocked"):
        return gate["response"], []
    return None, gate.get("warnings", [])


def _governance_record_outcome(
    *, job_id: str, preview: dict[str, Any], passed: bool, note: str
) -> None:
    """Record an allow-verdict after a confirmed application.

    No-op when the governance framework is not installed. Never raises.
    """
    try:
        from governance import adapter as _gov
    except Exception:
        return
    if not _gov.governance_enabled():
        return
    try:
        _gov.record_application_verdict(
            {
                "job_id": job_id,
                "title": preview.get("title"),
                "company": preview.get("company"),
            },
            passed,
            f"veto: application {note} for "
            f"{preview.get('title')} at {preview.get('company')}",
        )
    except Exception as exc:
        log.warning("Governance verdict recording failed: %s", exc)


@mcp.tool()
def apply_to_job(
    job_id: str,
    resume_path: str,
    cover_letter: str = "",
    answers: dict[str, str] | None = None,
    confirm: bool = False,
    profile: dict[str, str] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    """Prepare (and optionally record) a job application. Never submits.

    Phase 1 (default): does NOT auto-submit any form. Without confirm=True
    this returns a dry-run preview only. With confirm=True the application
    is logged to the local applications.json store and the agent returns
    the direct apply_url so the user can complete submission in their
    browser.

    Phase 2 (opt-in browser automation): set the environment variable
    JOB_MCP_BROWSER_APPLY=1 and pass a `profile` dict. Then confirm=True
    opens the apply_url in a browser, fills the form from the profile,
    uploads the resume, screenshots the completed form, and stops —
    fill-only. With headless=False the call waits until the user closes
    the browser; with headless=True the screenshot is returned so the
    user reviews and clicks submit themselves. Nothing is ever
    submitted by the tool.

    Args:
        job_id: The job id returned by search_jobs.
        resume_path: Local path to the resume file to attach.
        cover_letter: Optional cover letter text.
        answers: Optional common application answers, e.g.
            {"work_authorization": "yes", "salary_expectation": "150000"}.
        confirm: MUST be True to record the application (Phase 1) or to
            fill the form via the browser (Phase 2). Defaults to False
            (dry-run preview only).
        profile: Applicant details for Phase 2 browser fill, e.g.
            {"full_name": "Ada Lovelace", "email": "ada@example.com",
             "phone": "+1 555-0100", "location": "New York, NY",
             "linkedin_url": "...", "website": "...",
             "cover_letter": "..."}. full_name is split into first/last
            when the form asks for them separately. Defaults to the saved
            profiles/profile.json when omitted (see get_profile() and
            wizard.py); pass an explicit dict to override it.
        headless: Run the Phase 2 browser headless (default True). Set
            False to watch / manually log in during the session.

    Returns:
        Dry-run preview, or the recorded application entry when confirmed.
    """
    answers = answers or {}
    # Default to the wizard-saved profile when the caller passes none.
    # An explicitly passed dict (even empty) is respected as-is.
    if profile is None:
        profile = _load_saved_profile()
    else:
        profile = dict(profile)
    try:
        board, url = _decode_job_id(job_id)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}

    if board == "user":
        # Bring-your-own listing (spec section 5.2): the id payload is a
        # local store key, not a URL. Resolve the real posting URL from
        # the saved listing so the dry-run preview, the recorded entry,
        # and the Phase 2 fill-only browser all open the actual posting.
        # Listings added from pasted text have no URL — the empty string
        # survives, exactly like a provider listing with no apply_url.
        try:
            _byol_details = get_job_details(job_id)
        except Exception as exc:  # noqa: BLE001 - best-effort resolution
            _byol_details = {"error": str(exc)}
        if isinstance(_byol_details, dict) and not _byol_details.get("error"):
            _byol_url = _byol_details.get("url") or _byol_details.get("apply_url")
            if _byol_url:
                url = _byol_url

    cached = _JOB_REGISTRY.get(job_id, {})
    resume = Path(resume_path).expanduser()
    preview: dict[str, Any] = {
        "job_id": job_id,
        "board": board,
        "title": cached.get("title", "Unknown"),
        "company": cached.get("company", "Unknown"),
        "location": cached.get("location", "Unknown"),
        "apply_url": url,
        "resume_path": str(resume),
        "resume_found": resume.is_file(),
        "cover_letter_chars": len(cover_letter),
        "answers_provided": sorted(answers.keys()),
        "profile_keys_provided": sorted(profile.keys()),
        # Common fields most application forms ask for; extracted so the
        # agent can pre-fill them for the user in Phase 2 / manual apply.
        "typical_form_fields": [
            "full_name",
            "email",
            "phone",
            "location",
            "linkedin_url",
            "work_authorization",
            "sponsorship_required",
            "salary_expectation",
            "start_date",
            "resume_upload",
            "cover_letter_upload",
        ],
    }
    if not preview["resume_found"]:
        preview["warning"] = (
            f"Resume not found at {resume}; attach a valid file before applying."
        )

    if not confirm:
        message = (
            "Dry run only: nothing was submitted or recorded. "
            "Review the preview and re-call with confirm=True to log "
            "this application."
        )
        if _browser_apply_enabled() and profile:
            message += (
                " Phase 2 browser automation is enabled: on confirm, the "
                "form will be auto-filled from your profile in a browser "
                "and left open for your own submit click."
            )
        elif profile:
            message += (
                " Tip: pass profile + set JOB_MCP_BROWSER_APPLY=1 to enable "
                "Phase 2 browser auto-fill on confirm."
            )
        return {
            "status": "dry_run",
            "message": message,
            "preview": preview,
        }

    # confirm=True ---------------------------------------------------------
    # Governance gate (additive): veto + cover-letter-honesty +
    # qualification checks run BEFORE any browser submission or local
    # logging. No-op when the governance framework is not installed.
    # A blocked gate returns immediately: nothing is submitted or recorded.
    _gov_block, _gov_warnings = _governance_precheck(
        job_id=job_id,
        board=board,
        url=url,
        preview=preview,
        profile=profile,
        cover_letter=cover_letter,
    )
    if _gov_block is not None:
        return _gov_block
    if _gov_warnings:
        preview.setdefault("governance_warnings", []).extend(_gov_warnings)

    # Apply-time grilling gate (additive): when enabled in preferences,
    # the application pauses here until the candidate answers the grill
    # questions (or skips them). Nothing is submitted or recorded first;
    # the caller re-invokes apply_to_job with confirm=True afterwards.
    _grill_mod = dict(_PLUGIN_MODULES).get("grill")
    if _grill_mod is not None:
        try:
            _grill_prefs = _grill_mod.get_grill_prefs()
        except Exception:
            _grill_prefs = {}
        if _grill_prefs.get("grill_on_apply") and _grill_prefs.get(
            "grill_channel"
        ) != "off":
            _grill_state = _grill_mod.grill_status(job_id)
            if not _grill_state.get("complete"):
                if _grill_state.get("total", 0) == 0:
                    try:
                        _grill_mod.start_grill(
                            job_id,
                            {
                                "title": preview.get("title", ""),
                                "company": preview.get("company", ""),
                                "location": preview.get("location", ""),
                                "description": "",
                            },
                            profile,
                        )
                    except Exception as exc:
                        log.warning(
                            "grill start failed for %s: %s", job_id, exc
                        )
                return {
                    "status": "grill_pending",
                    "message": (
                        "Grilling is enabled: answer these questions before "
                        "the application proceeds. Nothing has been submitted "
                        "or recorded. Answer via the grill_answer tool (chat), "
                        "or reply to the grill email, then call apply_to_job "
                        "again with confirm=True."
                    ),
                    "outbound": _grill_mod.outbound_for_channel(job_id),
                    "grill_status": _grill_mod.grill_status(job_id),
                }

    # Risk-policy gate (additive): daily application cap, board tiers,
    # scraping-tier fill-only enforcement, and the governance veto screen.
    # Fail-closed: a blocked gate returns immediately; nothing is submitted
    # or recorded, and the verdict lands in the audit ledger.
    if _risk_policy is not None:
        _via = "browser" if _browser_apply_enabled() else "manual"
        _apply_verdict = _risk_policy.adjudicate_apply(
            board,
            job_title=str(preview.get("title", "")),
            company=str(preview.get("company", "")),
            via=_via,
        )
        if not _apply_verdict.get("allowed", False):
            _governance_record_outcome(
                job_id=job_id,
                preview=preview,
                passed=False,
                note=f"risk-policy blocked: {_apply_verdict.get('reason', '')}",
            )
            return {
                "status": "blocked",
                "reason": _apply_verdict.get("reason", "blocked by risk policy"),
                "tier": _apply_verdict.get("tier"),
                "governance": _apply_verdict.get("governance"),
            }
        if _apply_verdict.get("fill_only"):
            preview["fill_only"] = True
            preview.setdefault("governance_warnings", []).append(
                "Scraping-tier board: this flow fills the form only. "
                "Review it yourself and click submit."
            )

    if _browser_apply_enabled():
        _gov_browser_result = _apply_via_browser_and_record(
            job_id=job_id,
            board=board,
            url=url,
            preview=preview,
            answers=answers,
            profile=profile,
            cover_letter=cover_letter,
            resume=resume,
            headless=headless,
        )
        _governance_record_outcome(
            job_id=job_id,
            preview=preview,
            passed=True,
            note=(
                "phase-2 browser apply finished with status "
                f"{_gov_browser_result.get('status')}"
            ),
        )
        if _compliance is not None and not _gov_browser_result.get("error"):
            _compliance.record_application()
        return _gov_browser_result

    entry = {
        "job_id": job_id,
        "board": board,
        "title": preview["title"],
        "company": preview["company"],
        "location": preview["location"],
        "apply_url": url,
        "resume_path": str(resume),
        "cover_letter_chars": len(cover_letter),
        "answers": answers,
        "status": "confirmed",
        "submitted_at": (submitted_at := datetime.now(timezone.utc).isoformat()),
        # Lifecycle fields (see lifecycle.py): every recorded application
        # starts at stage "applied" with a seeded history event.
        "stage": "applied",
        "stage_history": [{"stage": "applied", "at": submitted_at, "note": ""}],
        "follow_up_due": None,
        "note": (
            "Phase 1: recorded locally. Complete the actual submission at "
            "apply_url in your browser. To enable Phase 2 browser "
            "auto-fill, set JOB_MCP_BROWSER_APPLY=1 and pass a profile "
            "dict (see browser_apply.py for the schema)."
        ),
    }
    applications = _load_applications()
    applications.append(entry)
    _save_applications(applications)
    if _compliance is not None:
        _compliance.record_application()
    _governance_record_outcome(
        job_id=job_id, preview=preview, passed=True, note="phase-1 recorded"
    )
    # outcome-min-v0 capture: append-only `applied` event. Guarded so
    # capture can never break a confirmed submission.
    try:
        import outcomes as _outcomes

        _outcomes.record_application_event(entry, source="server:apply_to_job")
    except Exception:
        log.exception("outcome-min-v0 capture failed for %s", job_id)
    log.info("Application confirmed for %s (%s)", entry["title"], job_id)
    return {
        "status": "confirmed",
        "message": (
            "Application recorded locally. Finish submitting it at the "
            f"apply_url: {url}"
        ),
        "application": entry,
        "preview": preview,
    }


def _apply_via_browser_and_record(
    *,
    job_id: str,
    board: str,
    url: str,
    preview: dict[str, Any],
    answers: dict[str, str],
    profile: dict[str, str],
    cover_letter: str,
    resume: Path,
    headless: bool,
) -> dict[str, Any]:
    """Phase 2: fill the form in a real browser (fill-only), then log the fill.

    Any failure (missing Playwright, navigation error) is recorded with
    ok=False - the tool never raises. Nothing is submitted; the user
    reviews and submits by hand. In headed mode the call waits until
    the user closes the browser window.
    """
    if cover_letter and not profile.get("cover_letter"):
        profile = {**profile, "cover_letter": cover_letter}
    try:
        from browser_apply import apply_via_browser

        browser_result = apply_via_browser(
            apply_url=url,
            profile=profile,
            resume_path=str(resume),
            headless=headless,
            board=board,
        )
    except Exception as exc:  # defensive: the tool must never crash
        log.warning("Phase 2 browser apply failed for %s: %s", job_id, exc)
        browser_result = {
            "ok": False,
            "fields_filled": {},
            "fields_detected": 0,
            "screenshot": None,
            "final_url": url,
            "browser_open": False,
            "handoff": None,
            "error": f"browser hook crashed: {exc}",
        }

    # Legal-hardening commit 8 (spec §6, plan §10.3): the per-action
    # circuit breaker. The form is filled (fill-only — Veto never
    # submits); before the filled form is handed back for the user's own
    # submit click, the user must type the company name exactly as shown
    # in the summary. Non-TTY refuses with a nonzero exit (SystemExit(2)
    # from the gate); the server/MCP context catches it and converts it
    # to a refusal result — a refused action must not kill the server.
    # A declined/mismatched answer also refuses: nothing is recorded and
    # the compliance budget is not spent.
    import circuit_breaker as _circuit_breaker

    _gate_summary = _circuit_breaker.render_application_summary(
        company=str(preview.get("company") or ""),
        title=str(preview.get("title") or ""),
        fields_filled=browser_result.get("fields_filled") or {},
        destination_url=str(browser_result.get("final_url") or url),
        board=board,
    )
    try:
        _gate = _circuit_breaker.require_action_confirmation(
            summary=_gate_summary,
            expected_value=str(preview.get("company") or ""),
            value_label="company name",
            action_label="application",
        )
    except SystemExit as exc:
        # Non-interactive refuse (spec §6.3). Convert to a result dict:
        # the MCP server and the apply queue must survive a refusal.
        # The CLI maps this status to a nonzero exit (see cmd_apply).
        log.info("Phase 2 apply refused (non-interactive) for %s.", job_id)
        return {
            "ok": False,
            "status": "refused",
            "error": "not_interactive",
            "exit_code": int(exc.code or 2),
            "instructions": (
                "Refused: confirming this application requires a human at "
                "an interactive terminal (stdin and stdout must both be "
                "TTYs). There is no environment variable, flag, or config "
                "that overrides this. Run the apply command from a real "
                "terminal."
            ),
        }
    if not _gate.get("ok"):
        log.info(
            "Phase 2 apply refused (%s) for %s.",
            _gate.get("error"), job_id,
        )
        return {
            "ok": False,
            "status": "refused",
            "error": _gate.get("error", "approval_declined"),
            "instructions": _gate.get(
                "instructions",
                "Refused: the typed value did not match. Nothing was "
                "recorded; re-run to try again.",
            ),
        }

    entry = {
        "job_id": job_id,
        "board": board,
        "title": preview["title"],
        "company": preview["company"],
        "location": preview["location"],
        "apply_url": url,
        "resume_path": str(resume),
        "cover_letter_chars": len(cover_letter),
        "answers": answers,
        "profile_keys": sorted(profile.keys()),
        "status": "browser_filled" if browser_result.get("ok") else "browser_failed",
        "submitted": False,
        "fields_detected": browser_result.get("fields_detected", 0),
        "fields_filled": browser_result.get("fields_filled", {}),
        "screenshot": browser_result.get("screenshot"),
        "final_url": browser_result.get("final_url"),
        "browser_open": browser_result.get("browser_open", False),
        "browser_error": browser_result.get("error"),
        "submitted_at": (submitted_at := datetime.now(timezone.utc).isoformat()),
        # Lifecycle fields (see lifecycle.py).
        "stage": "applied",
        "stage_history": [{"stage": "applied", "at": submitted_at, "note": ""}],
        "follow_up_due": None,
        "note": (
            "Phase 2 browser fill-only: the form was filled from the "
            "profile and screenshotted. Nothing was submitted — review "
            "the form and click submit yourself in your browser."
        ),
    }
    applications = _load_applications()
    applications.append(entry)
    _save_applications(applications)
    # outcome-min-v0 capture is skipped here: nothing was submitted, so
    # no applied lifecycle event is recorded for a fill. The
    # applications.json entry above (submitted: False) is the audit
    # trail.
    log.info(
        "Phase 2 fill for %s (%s): ok=%s",
        entry["title"],
        job_id,
        browser_result.get("ok"),
    )
    if browser_result.get("ok"):
        message = (
            "Application form filled via browser automation and recorded. "
            "Nothing was submitted — review the form and click submit "
            "yourself."
        )
    else:
        message = (
            "Browser automation did not complete the fill "
            f"({browser_result.get('error') or 'unknown error'}). "
            "The attempt was recorded; review the screenshot and fill "
            "manually if needed."
        )
    return {
        "status": entry["status"],
        "message": message,
        "application": entry,
        "preview": preview,
    }


@mcp.tool()
def track_applications() -> list[dict[str, Any]]:
    """Return all locally recorded job applications (from applications.json).

    Legacy entries (recorded before stages existed) are backfilled in
    memory with ``stage="applied"``; the file is not rewritten.
    """
    return lifecycle.load_entries(APPLICATIONS_FILE)


@mcp.tool()
def update_application(
    job_id_or_index: str, stage: str, note: str = ""
) -> dict[str, Any]:
    """Move a recorded application to a new pipeline stage.

    Args:
        job_id_or_index: The ``job_id`` from track_applications, or its
            numeric index in the list.
        stage: One of ``applied``, ``interviewing``, ``offer``,
            ``rejected``, ``withdrawn``, ``ghosted``.
        note: Optional note recorded with the stage change.

    Moving to ``interviewing`` sets a follow-up reminder 7 days out;
    terminal stages (``offer``/``rejected``/``withdrawn``) clear it.
    """
    try:
        return lifecycle.update_stage(APPLICATIONS_FILE, job_id_or_index, stage, note=note)
    except (ValueError, KeyError) as exc:
        return {"error": str(exc)}


@mcp.tool()
def application_stats() -> dict[str, Any]:
    """Aggregate stats over recorded applications.

    Returns ``{total, by_stage, by_board, response_rate}`` where
    response_rate = (interviewing + offer) / total.
    """
    return lifecycle.stats(lifecycle.load_entries(APPLICATIONS_FILE))


@mcp.tool()
def nudge_followups() -> list[dict[str, Any]]:
    """Applications with a follow-up due today or earlier.

    Covers stages ``applied`` and ``interviewing``. Pair with
    update_application() to advance or clear them.
    """
    return lifecycle.due_followups(lifecycle.load_entries(APPLICATIONS_FILE))


@mcp.tool()
def add_watch(
    name: str,
    query: str,
    location: str = "",
    board: str = "all",
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save a named job watch (a recurring search).

    The first check of a watch only records a baseline and reports no new
    jobs, so you are not spammed with everything that already existed.
    Run ``check_watches()`` (or ``python watch.py`` on a cron) to poll.
    """
    watches = watch_store.load_watches(WATCHES_FILE)
    try:
        saved = watch_store.add_watch(watches, name, query, location, board, filters)
    except ValueError as exc:
        return {"error": str(exc)}
    watch_store.save_watches(WATCHES_FILE, watches)
    return {
        "watch": saved,
        "message": (
            f"Watch {name!r} saved. The first check records a baseline "
            "(no alerts); later checks report only new postings."
        ),
    }


@mcp.tool()
def list_watches() -> list[dict[str, Any]]:
    """List all saved job watches and their last-check state."""
    return list(watch_store.load_watches(WATCHES_FILE).values())


@mcp.tool()
def remove_watch(name: str) -> dict[str, Any]:
    """Delete a saved job watch."""
    watches = watch_store.load_watches(WATCHES_FILE)
    removed = watch_store.remove_watch(watches, name)
    if removed:
        watch_store.save_watches(WATCHES_FILE, watches)
    return {"removed": removed, "name": name}


@mcp.tool()
def check_watches() -> dict[str, Any]:
    """Run every saved watch and return only the new postings per watch.

    Cron-friendly: output is plain JSON, one entry per watch with
    ``new_jobs`` and ``total_seen``. ``python watch.py`` does the same
    from a shell (prints JSON, exits 0).
    """
    watches = watch_store.load_watches(WATCHES_FILE)
    results = watch_store.check_all(watches, search_jobs)
    watch_store.save_watches(WATCHES_FILE, watches)
    return {
        name: {
            "new_jobs": r.get("new_jobs", []),
            "total_seen": r.get("total_seen", 0),
            **({"error": r["error"]} if r.get("error") else {}),
        }
        for name, r in results.items()
    }


@mcp.tool()
def export_applications_csv(path: str = "applications.csv") -> dict[str, Any]:
    """Export recorded applications to CSV (portability / spreadsheets).

    Args:
        path: Destination file; relative paths resolve under the project
            directory. Defaults to ``applications.csv``.
    """
    out = Path(path)
    if not out.is_absolute():
        out = BASE_DIR / out
    rows = lifecycle.export_csv(lifecycle.load_entries(APPLICATIONS_FILE), out)
    return {"path": str(out), "rows": rows}


@mcp.tool()
def get_profile() -> dict[str, Any]:
    """Return the applicant profile saved by the onboarding wizard.

    Run `python3 wizard.py` in the project directory to create or update
    profiles/profile.json. The wizard asks basic questions (name, contact,
    target titles, locations, remote preference, salary, skills, work
    authorization) and can ingest a LinkedIn data-export ZIP to pre-fill
    experience, education, and skills.

    The saved profile is used as the default `profile` for apply_to_job
    (Phase 2 browser auto-fill) and as the input for resume_builder.py.

    Returns:
        {"profile": {...}} when the wizard has been run, or
        {"profile": None, "message": ...} with setup instructions.
    """
    profile = _load_saved_profile()
    if not profile:
        return {
            "profile": None,
            "message": (
                "No saved profile found. Run the onboarding wizard to "
                "create one: `python3 wizard.py` in "
                "~/workspace/veto/ (it asks basic questions and "
                "can ingest a LinkedIn data-export ZIP to pre-fill "
                "experience/education/skills)."
            ),
        }
    return {"profile": profile}


@mcp.tool()
def tailor_application(
    job_id: str, profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a per-job tailored resume + cover letter.

    Uses tailor.py: extracts the posting's keywords, front-loads matching
    experience bullets in the resume, and drafts a 3-paragraph cover
    letter. Honesty rule: only rephrases/reorders facts already in the
    profile - skills the job wants but the profile lacks are reported in
    ``missing_skills`` and are NOT added to the resume.

    Args:
        job_id: The job id returned by search_jobs.
        profile: Optional profile dict; defaults to the wizard-saved
            profiles/profile.json.

    Returns:
        {"resume_md", "cover_letter", "matched_skills", "missing_skills"}
        or {"error": ...}.
    """
    try:
        from tailor import tailor_resume
    except ImportError as exc:
        return {"error": f"tailor.py unavailable: {exc}"}
    prof = dict(profile) if profile is not None else _load_saved_profile()
    if not prof:
        return {
            "error": (
                "No profile available. Run `python3 wizard.py` to create "
                "one, or pass a profile dict."
            )
        }
    details = get_job_details(job_id)
    if details.get("error"):
        return {"job_id": job_id, "error": details["error"]}
    result = tailor_resume(prof, details)
    result["job_id"] = job_id
    return result


@mcp.tool()
def update_preferences(
    blocked_companies: list[str] | None = None,
    preferred_companies: list[str] | None = None,
    blocked_keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Update company preferences used to filter search_jobs results.

    Each provided list REPLACES that preference list; omitted lists are
    left unchanged. Blocked companies/keywords are removed from results;
    preferred companies are flagged "preferred": True and sorted first.

    Args:
        blocked_companies: e.g. ["Acme Corp"].
        preferred_companies: e.g. ["Initech"].
        blocked_keywords: e.g. ["unpaid internship"].

    Returns:
        The full updated preferences dict.
    """
    if save_preferences is None or load_preferences is None:
        return {"error": "prefs.py unavailable."}
    prefs = load_preferences()
    if blocked_companies is not None:
        prefs["blocked_companies"] = [str(c) for c in blocked_companies]
    if preferred_companies is not None:
        prefs["preferred_companies"] = [str(c) for c in preferred_companies]
    if blocked_keywords is not None:
        prefs["blocked_keywords"] = [str(k) for k in blocked_keywords]
    return {"preferences": save_preferences(prefs)}


@mcp.tool()
def governance_status() -> dict[str, Any]:
    """Report whether the agentic governance framework is active.

    Returns:
        Dict with enabled, root, domains, and the gating policy
        (what fails closed vs. what warns and proceeds).
    """
    try:
        from governance import adapter as _gov
    except Exception as exc:
        return {
            "enabled": False,
            "root": None,
            "domains": [],
            "error": f"governance adapter import failed: {exc}",
        }
    root = _gov.find_governance_root()
    enabled = _gov.governance_enabled()
    domains: list[str] = []
    if enabled:
        try:
            domains = _gov.load_framework().list_domains()
        except Exception:
            domains = []
    return {
        "enabled": enabled,
        "root": str(root) if root else None,
        "domains": domains,
        "gating": {
            "veto_check": "fail-closed: blocks when a veto hits, and also "
            "blocks when the check itself errors",
            "cover_letter_honesty": "fail-closed: blocks on unsupported "
            "claims or framework errors",
            "qualification": "blocks below score 0.4 or on severe "
            "experience gap; warns (proceeds) on 0.4-0.65; fail-open "
            "with a warning when no profile is available",
            "adversarial_review": "advisory: warns on error, never blocks",
            "veto_clearing": "human only: fix the application/profile and "
            "re-run (framework overturn_verdict also exists)",
        },
    }


# ---------------------------------------------------------------------------
# Enhancement plugin tools
# ---------------------------------------------------------------------------


def _register_plugin_tools() -> list[str]:
    """Register each available plugin module's MCP tools.

    Returns the names of the modules that registered successfully.
    """
    registered: list[str] = []
    for _name, _mod in _PLUGIN_MODULES:
        _register = getattr(_mod, "register_tools", None)
        if _register is None:
            continue
        try:
            _register(mcp)
            registered.append(_name)
        except Exception as exc:
            log.warning(
                "plugin %s tool registration failed: %s", _name, exc
            )
    return registered


_REGISTERED_PLUGINS: list[str] = _register_plugin_tools()


@mcp.tool()
def risk_status() -> dict[str, Any]:
    """Risk-policy status: board tiers, search/apply budgets, and plugins.

    Returns:
        Dict with per-board tier + whether search is currently allowed
        (budget/circuit-breaker/strict-mode state), whether a new
        application is allowed under the daily cap, which enhancement
        plugins registered, and whether the risk-policy and compliance
        modules are loaded. This is the audit surface for every
        risk-gated search and application.
    """
    boards: dict[str, dict[str, Any]] = {}
    for _board in PROVIDERS:
        _tier = (
            _compliance.board_tier(_board) if _compliance is not None
            else "unknown"
        )
        if _compliance is not None:
            _search_allowed, _search_reason = _compliance.check_search_allowed(
                _board
            )
        else:
            _search_allowed, _search_reason = True, ""
        boards[_board] = {
            "tier": _tier,
            "search_allowed": _search_allowed,
            "reason": _search_reason,
        }
    if _compliance is not None:
        _apply_allowed, _apply_reason = _compliance.check_apply_allowed()
    else:
        _apply_allowed, _apply_reason = True, ""
    return {
        "boards": boards,
        "apply_allowed": _apply_allowed,
        "apply_reason": _apply_reason,
        "plugins": _REGISTERED_PLUGINS,
        "risk_policy_loaded": _risk_policy is not None,
        "compliance_loaded": _compliance is not None,
    }


if __name__ == "__main__":
    # stdio transport (default): hook up to Claude Desktop / Muse via
    # `mcpServers` config pointing at this file.
    mcp.run()
