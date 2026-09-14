#!/usr/bin/env python3
"""Provider contract kit — Initiative 09, epic 1.

This module is the single place that defines what a Veto connector *is*:

* ``JobPosting`` / ``JobDetail`` — the normalized search/detail schema every
  provider must emit (and that conformance tests validate against).
* ``TermsClass`` — robots/ToS classification for every registered
  connector, aligned with the risk tiers in ``compliance.py``
  (``official`` / ``scraping``).
* ``budget_for`` — declarative daily budgets and cooldowns per provider,
  derived from ``compliance.py`` (the single source of truth for
  enforcement). Enforcement stays in ``compliance.py`` (daily budgets,
  circuit breakers, recovery streaks); this kit only declares the numbers
  so tests and the scorecard can reason about them.
* ``SubmissionStatus`` / ``SubmissionResult`` / ``SubmissionConnector`` —
  the submission interface contract. Headline property: **it must be
  impossible for a connector to silently drop applications or misreport
  submission status.** Every apply call yields a ``SubmissionResult``
  whose status is a member of the closed ``SubmissionStatus`` vocabulary;
  unknown statuses fail validation, ``submitted`` requires confirmation
  evidence, and ``failed`` requires an error. ``submission_result_from``
  adapts the real modules' native result dicts (``ats_apply``,
  ``browser_apply``) into this vocabulary.
* ``ConnectorManifest`` — the proof-of-value declaration every connector
  carries: what it can do, why it may be blocked, what data it sends, and
  which actions require confirmation. The Q2 gate requires every connector
  to pass one capability contract *and* one policy test suite; the manifest
  is the capability contract's machine-readable form.
* ``validate_job_posting`` / ``validate_job_detail`` /
  ``validate_submission_result`` / ``conformance_suite`` — the conformance
  tests.

No hard third-party dependencies: stdlib only at module level (``compliance``
is imported lazily inside functions). This module performs no network calls
and reads no credentials: every answer it gives is a pure declaration.
Providers must never import ``server.py``.
"""

from __future__ import annotations

import base64
import inspect
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol

log = logging.getLogger("job-apply-mcp.providers.contract")

# ---------------------------------------------------------------------------
# Terms / robots classification
# ---------------------------------------------------------------------------


class TermsClass(str, Enum):
    """ToS classification for a connector's data interface.

    Mirrors the risk tiers in compliance.py and names the *basis* for the
    classification so a reviewer can see why, not just which tier.
    """

    OFFICIAL_API = "official_api"  # vendor-published API; ToS-friendly
    KEY_GATED_API = "key_gated_api"  # official API, but needs a key the user
    #                               may not have; honest stub until then
    HTML_SCRAPING = "html_scraping"  # guest-endpoint/HTML scraping; ToS risk
    HONEST_STUB = "honest_stub"  # no working interface today; declares it


# Registry: connector name -> (TermsClass, basis, robots_url).
#
# Basis strings name the evidence: an official doc URL, a live probe, or a
# robots.txt policy. They are human-readable so the declaration stays honest.
#
# This registry and PROVIDER_MANIFESTS are the ONE authoritative connector
# registry: conformance_suite's ``registry.agreement`` check requires their
# key sets to be identical, so a connector can never be classified without
# a manifest or manifested without a classification.
#
# Note on removed entries: ``linkedin`` / ``indeed`` used to be classified
# here as HTML_SCRAPING, but Veto has no search interface for either — no
# provider module, no manifest, no evidence. The LinkedIn Easy Apply
# fill-only submission path was deleted (legal-hardening commit 3), so
# keeping scraping-tier search classifications for them was aspirational,
# not evidential. They were removed; their risk posture is still enforced
# in compliance.py.
TERMS_CLASSIFICATION: dict[str, tuple[TermsClass, str, str]] = {
    "greenhouse": (
        TermsClass.OFFICIAL_API,
        "Public Job Board API documented at "
        "https://developers.greenhouse.io/; live-probed on 2026-09-10",
        "https://boards-api.greenhouse.io/robots.txt",
    ),
    "lever": (
        TermsClass.OFFICIAL_API,
        "Public postings API at https://api.lever.co/v0/postings; "
        "live-probed per-company tokens on 2026-09-10",
        "https://api.lever.co/robots.txt",
    ),
    "ashby": (
        TermsClass.OFFICIAL_API,
        "Public posting API at https://api.ashbyhq.com/posting-api; "
        "live-probed on 2026-09-10",
        "https://api.ashbyhq.com/robots.txt",
    ),
    "adzuna": (
        TermsClass.KEY_GATED_API,
        "Official API at https://developer.adzuna.com/ requires "
        "ADZUNA_APP_ID/ADZUNA_APP_KEY; honest stub without them",
        "https://www.adzuna.com/robots.txt",
    ),
    "glassdoor": (
        TermsClass.HONEST_STUB,
        "Declared stub: no working public interface; search raises "
        "NotImplementedError rather than pretending",
        "https://www.glassdoor.com/robots.txt",
    ),
    "ats_direct": (
        TermsClass.OFFICIAL_API,
        "Vendor submission endpoints are official APIs (Ashby "
        "applicationForm.submit, Lever POST apply, Greenhouse POST "
        "applications), but NO endpoint is verified usable today: Ashby "
        "401s, Lever needs a Lever-issued key, Greenhouse needs an "
        "employer-issued key (probed/documented 2026-09-10). The registry "
        "holds only unverified entries, so no submit ever fires.",
        "https://developers.ashbyhq.com/",
    ),
}


def terms_for(provider_name: str) -> tuple[TermsClass, str, str]:
    """Return (TermsClass, basis, robots_url) for a connector.

    Unknown connectors default to HONEST_STUB — the safe declaration — so
    a new connector can never silently claim a friendly interface. Only a
    registered entry (with recorded evidence) can be certified by the
    conformance suite.
    """
    return TERMS_CLASSIFICATION.get(
        provider_name,
        (
            TermsClass.HONEST_STUB,
            "No classification registered; treated as an honest stub "
            "until evidence is recorded",
            "",
        ),
    )


# NOTE on robots.txt: the canonical helper is
# ``providers._common.robots_allows(board, url)`` (cached, fail-closed, and
# already honored by the scraping-tier providers). This kit does not ship
# a second implementation; the per-provider robots.txt URLs live in
# TERMS_CLASSIFICATION above so reviewers can audit the policy directly.


# ---------------------------------------------------------------------------
# Budgets and cooldowns — derived from compliance.py (single source of truth)
# ---------------------------------------------------------------------------
#
# Enforcement lives in compliance.py (tier daily budgets, exponential
# circuit-breaker cooldowns). This kit exposes the *same* numbers so the
# scorecard and tests can reason about them without maintaining a second
# table that could drift from enforcement. Connectors that perform no
# searches — HONEST_STUB, KEY_GATED_API (credentials unpresumed), and
# non-search connector types such as "ats" — get a zero budget: displaying
# any budget for them would be dishonest.


def _declares_search(provider_name: str) -> bool:
    """True when the connector is expected to perform searches.

    False for honest stubs, for key-gated connectors (the contract never
    presumes credentials exist), and for non-search connector types
    (``ats`` / ``channel`` / ``calendar``): none of them search, so a
    declared search budget for them would be dishonest.
    """
    terms, _, _ = terms_for(provider_name)
    if terms in (TermsClass.HONEST_STUB, TermsClass.KEY_GATED_API):
        return False
    manifest = PROVIDER_MANIFESTS.get(provider_name)
    if manifest is not None and manifest.connector_type != "job_provider":
        return False
    return True


def budget_for(provider_name: str) -> tuple[int, int]:
    """Return (daily_budget, base_cooldown_s) for a connector.

    Derived from ``compliance.py``: the tier budget for the connector's
    risk tier and the circuit-breaker's base cooldown (the actual cooldown
    doubles per consecutive block, capped at 24h — see
    ``compliance.record_block``). Connectors that perform no searches get
    ``(0, compliance.BLOCK_COOLDOWN_BASE_SECONDS)``: no budget, long
    cooldown — the safe, honest default until evidence exists.

    Note on ``adzuna``: it is KEY_GATED_API and has no credentials in this
    environment, so it declares a zero budget even though enforcement's
    tier budget is 1000/day. The 1000/day cap still applies *if* the user
    configures credentials and the connector starts searching; until then
    the contract refuses to display budget for searches that cannot happen.
    """
    base_cooldown = 3600
    try:
        import compliance

        base_cooldown = int(compliance.BLOCK_COOLDOWN_BASE_SECONDS)
    except Exception as exc:  # compliance unavailable: fail safe
        log.warning("budget_for: compliance unavailable, safe default: %s", exc)
        return 0, base_cooldown
    if not _declares_search(provider_name):
        return 0, base_cooldown
    try:
        import compliance

        tier = compliance.board_tier(provider_name)
        daily = int(compliance.DEFAULT_SEARCH_BUDGET.get(tier, 0))
    except Exception as exc:
        log.warning("budget_for: tier lookup failed, safe default: %s", exc)
        return 0, base_cooldown
    return daily, base_cooldown


# ---------------------------------------------------------------------------
# Submission interface contract — the headline property
# ---------------------------------------------------------------------------
#
# HEADLINE PROPERTY: it must be impossible for a connector to silently drop
# applications or misreport submission status.
#
# The contract has three parts:
# 1. ``SubmissionStatus`` — the CLOSED vocabulary every submission outcome
#    is reported in. There is no other status: ``validate_submission_result``
#    rejects anything outside the vocabulary.
# 2. ``SubmissionResult`` — the typed result every apply call yields, with
#    honesty rules: ``submitted`` requires confirmation evidence,
#    ``failed`` requires an error, ``draft`` must not claim submission.
# 3. ``SubmissionConnector`` — the ``apply()`` interface signature for
#    connectors that submit. Connectors keep their native entry points
#    (``ats_apply.apply_direct``, ``browser_apply.apply_via_browser``);
#    ``submission_result_from`` is the
#    adapter that maps their native result dicts into the vocabulary.
#
# The Q2 gate (``conformance_suite``) only ever dry-runs apply paths
# (``confirm=False, dry_run=True``) and requires: an apply call returns a
# ``SubmissionResult`` (never None, never a swallowed exception), its
# status is in the vocabulary, a dry run reports ``draft`` with no
# confirmation, and a forged unknown status is rejected.


class SubmissionStatus(str, Enum):
    """Closed vocabulary for submission outcomes. The contract owns this
    list; every connector reports in it, and nothing else is a status.

    Grounded in the real modules' reporting (see ``submission_result_from``
    for the mapping):
    """

    DRAFT = "draft"  # dry-run preview; no submission attempted
    #   (ats_apply ``mode: preview``; browser_apply confirm=False fill-only)
    SUBMITTED = "submitted"  # the endpoint confirmed receipt
    #   (ats_apply ``ok: True``; browser_apply ``submitted: True``)
    FAILED = "failed"  # attempted; the attempt failed — error is required
    #   (ats_apply ``ok: False``; browser_apply ``ok: False``)
    PAUSED = "paused"  # paused by session rescue; control handed to the user
    #   (browser_apply ``paused: True`` with a non-captcha reason).
    #    Pause-and-hand-off is the only path — Veto
    #    never bypasses the thing that paused it.
    CAPTCHA_BLOCKED = "captcha_blocked"  # CAPTCHA/bot-check stopped the flow
    #   (browser_apply rescue ``reason: captcha``). Never bypassed, never
    #    auto-retried around: the user completes it or the run stays blocked.
    RATE_LIMITED = "rate_limited"  # compliance cap / HTTP 429 / block
    #   (ats_apply daily-cap refusal)
    NEEDS_CONFIRMATION = "needs_confirmation"  # staged/filled; the human's
    #   explicit submit click is still required
    #   (fill-only result — the final Submit click is ALWAYS the user's)
    UNAVAILABLE = "unavailable"  # no usable submit interface for this board
    #   (ats_apply ``direct_apply_available: False``)


@dataclass
class SubmissionResult:
    """Typed outcome of one apply call. Every connector that submits must
    yield one of these per call — a missing result is a silent drop and
    fails conformance."""

    status: SubmissionStatus
    connector: str = ""  # connector name, e.g. "ats_direct"
    job_id: str = ""  # self-describing job id the call concerned
    confirmation: str = ""  # evidence of receipt (confirmation id, http-2xx)
    error: str = ""  # human-readable failure; required when failed
    detail: dict[str, Any] = field(default_factory=dict)  # extra evidence

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = (
            self.status.value
            if isinstance(self.status, SubmissionStatus)
            else self.status
        )
        return d


class SubmissionConnector(Protocol):
    """Interface signature for connectors that submit applications.

    ``name`` identifies the connector (matches its manifest/terms entry).
    ``apply`` performs (or dry-runs) one application and MUST return a
    ``SubmissionResult`` — never None, never a bare dict, and never by
    raising on the dry-run path. ``confirm=False`` (the default) means no
    submission is attempted; ``dry_run=True`` (the default) means zero
    side effects.
    """

    name: str

    def apply(
        self,
        job: dict[str, Any],
        applicant: dict[str, Any],
        *,
        confirm: bool = False,
        dry_run: bool = True,
    ) -> SubmissionResult: ...


def _normalize_status(value: Any) -> SubmissionStatus | None:
    """Map a status value to the vocabulary; None when it is unknown."""
    if isinstance(value, SubmissionStatus):
        return value
    if isinstance(value, str) and value:
        try:
            return SubmissionStatus(value)
        except ValueError:
            return None
    return None


def _result_field(result: Any, key: str) -> Any:
    if isinstance(result, SubmissionResult):
        return getattr(result, key, None)
    if isinstance(result, dict):
        return result.get(key)
    return None


def validate_submission_result(result: Any) -> list[str]:
    """Return conformance problems for a submission result (empty = honest).

    Rules — the honesty core of the headline property:
    * the status must be a member of ``SubmissionStatus``; anything else
      (including invented strings like ``"captcha_bypassed"``) fails;
    * ``submitted`` requires non-empty confirmation evidence — a bare
      success claim is misreporting;
    * ``failed`` requires a non-empty error — silent failures are drops;
    * ``draft`` must not carry confirmation evidence — a preview must not
      claim a submission happened.
    """
    problems: list[str] = []
    raw_status = _result_field(result, "status")
    status = _normalize_status(raw_status)
    if status is None:
        problems.append(
            f"unknown submission status: {raw_status!r} — not in the "
            "SubmissionStatus vocabulary; connectors must report in the "
            "closed vocabulary so outcomes cannot be misreported"
        )
        return problems
    confirmation = str(_result_field(result, "confirmation") or "").strip()
    error = str(_result_field(result, "error") or "").strip()
    if status is SubmissionStatus.SUBMITTED and not confirmation:
        problems.append(
            "status is 'submitted' but no confirmation evidence recorded — "
            "a success claim without evidence is misreporting"
        )
    if status is SubmissionStatus.FAILED and not error:
        problems.append(
            "status is 'failed' but no error recorded — failures must be "
            "loud, never silent"
        )
    if status is SubmissionStatus.DRAFT and confirmation:
        problems.append(
            "status is 'draft' but carries confirmation evidence — a preview "
            "must not claim a submission happened"
        )
    return problems


def _detect_source(raw: dict[str, Any]) -> str:
    """Guess which module produced a native result dict ('ats' | 'browser'
    | ''). Callers may pass ``source=`` explicitly instead."""
    if "submitted" in raw or "paused" in raw:
        return "browser"
    if (
        "browser_open" in raw
        or "fields_filled" in raw
        or "fields_detected" in raw
    ):
        return "browser"
    if "mode" in raw or "direct_apply_available" in raw or "ok" in raw:
        return "ats"
    return ""


def _rescue_status(raw: dict[str, Any]) -> SubmissionStatus:
    """A rescue pause is CAPTCHA_BLOCKED when the rescue reason is captcha,
    PAUSED otherwise. There is no bypass outcome."""
    rescue = raw.get("rescue") or {}
    reason = str(rescue.get("reason") or "") if isinstance(rescue, dict) else ""
    return (
        SubmissionStatus.CAPTCHA_BLOCKED
        if reason == "captcha"
        else SubmissionStatus.PAUSED
    )


def _from_ats(raw: dict[str, Any], connector: str) -> SubmissionResult:
    """Adapt an ``ats_apply.apply_direct`` result dict."""
    error = str(raw.get("error") or "")
    confirmation = str(raw.get("confirmation") or "")
    detail: dict[str, Any] = {
        k: raw[k]
        for k in ("status_code", "endpoint", "endpoint_status", "mode")
        if k in raw
    }
    if raw.get("mode") == "preview" or raw.get("dry_run"):
        status = SubmissionStatus.DRAFT
    elif raw.get("ok"):
        # Truthiness, consistent with _from_browser: the modules report
        # booleans, but a truthy non-bool must not silently become FAILED.
        status = SubmissionStatus.SUBMITTED
        if not confirmation:
            # A 2xx without a confirmation id is still evidence of receipt.
            sc = raw.get("status_code")
            if isinstance(sc, int) and 200 <= sc < 300:
                confirmation = f"http-{sc}"
    elif raw.get("direct_apply_available") is False:
        status = SubmissionStatus.UNAVAILABLE
    else:
        status = SubmissionStatus.FAILED
        # Refine: compliance daily-cap refusals and HTTP 429 are rate
        # limits, not generic failures.
        sc = raw.get("status_code")
        if (
            sc == 429
            or "cap reached" in error.lower()
            or "rate limit" in error.lower()
        ):
            status = SubmissionStatus.RATE_LIMITED
    return SubmissionResult(
        status=status,
        connector=connector or str(raw.get("board") or ""),
        job_id=str(raw.get("job_id") or ""),
        confirmation=confirmation,
        error=error,
        detail=detail,
    )


def _from_browser(raw: dict[str, Any], connector: str) -> SubmissionResult:
    """Adapt a ``browser_apply.apply_via_browser`` result dict."""
    error = str(raw.get("error") or "")
    if raw.get("paused"):
        status = _rescue_status(raw)
    elif raw.get("submitted"):
        status = SubmissionStatus.SUBMITTED
    elif raw.get("ok"):
        # Filled (and screenshotted) with confirm=False: nothing submitted.
        status = SubmissionStatus.DRAFT
    else:
        status = SubmissionStatus.FAILED
    confirmation = ""
    if status is SubmissionStatus.SUBMITTED:
        confirmation = str(
            raw.get("screenshot_after_submit") or raw.get("final_url") or ""
        )
    detail: dict[str, Any] = {
        k: raw[k]
        for k in ("final_url", "fields_filled", "fields_detected", "screenshot")
        if k in raw
    }
    if isinstance(raw.get("rescue"), dict):
        detail["rescue"] = raw["rescue"]
    return SubmissionResult(
        status=status,
        connector=connector or str(raw.get("board") or ""),
        job_id="",
        confirmation=confirmation,
        error=error,
        detail=detail,
    )


#: The documented ``source`` vocabulary for ``submission_result_from``.
_SUBMISSION_SOURCES = ("ats", "browser")


def submission_result_from(
    raw: dict[str, Any], *, connector: str = "", source: str = ""
) -> SubmissionResult:
    """Adapt a module-native submission result dict into a SubmissionResult.

    ``source`` is one of ``"ats"`` (``ats_apply.apply_direct``) or
    ``"browser"`` (``browser_apply.apply_via_browser``). When omitted,
    the source is detected from the dict's shape. An EXPLICIT ``source`` is validated against the vocabulary: an
    invalid explicit source raises ``ValueError`` rather than being
    silently re-detected — the caller's stated contract wins, so a wrong
    explicit source can never be quietly reinterpreted into a different
    module's semantics.
    Raises ``ValueError`` on an unrecognized shape or an unknown native
    status — loud, never a silent guess — so a connector cannot misreport
    by emitting shapes the contract does not understand.
    """
    if not isinstance(raw, dict) or not raw:
        raise ValueError(
            "submission_result_from: expected a non-empty result dict"
        )
    if source:
        src = str(source).lower()
        if src not in _SUBMISSION_SOURCES:
            raise ValueError(
                "submission_result_from: unknown explicit source "
                f"{source!r} (expected one of "
                f"{list(_SUBMISSION_SOURCES)}); refusing to guess"
            )
    else:
        src = _detect_source(raw)
    if src == "ats":
        return _from_ats(raw, connector)
    if src == "browser":
        return _from_browser(raw, connector)
    raise ValueError(
        f"unrecognized submission result shape (keys: {sorted(raw)}); "
        "refusing to guess — report in a known module shape"
    )


def apply_signature_problems(fn: Any) -> list[str]:
    """Check an apply callable against the ``SubmissionConnector`` signature.

    Requires ``apply(job, applicant, *, confirm=False, dry_run=True)`` —
    the two keyword parameters must exist and carry the safe defaults so
    the Q2 gate can always dry-run without side effects.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return ["apply() has no inspectable signature"]
    params = sig.parameters
    problems: list[str] = []
    for pname in ("job", "applicant"):
        if pname not in params:
            problems.append(f"apply() missing parameter: {pname}")
    for pname, default in (("confirm", False), ("dry_run", True)):
        p = params.get(pname)
        if p is None:
            problems.append(f"apply() missing keyword parameter: {pname}")
        elif p.kind not in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            problems.append(
                f"apply() parameter {pname} must be passable by keyword"
            )
        elif p.default is inspect.Parameter.empty:
            problems.append(
                f"apply() parameter {pname} must default "
                "(confirm=False, dry_run=True)"
            )
        elif p.default != default:
            problems.append(
                f"apply() parameter {pname} must default to {default!r} "
                f"(got {p.default!r}) — the safe default is no submission"
            )
    return problems


# ---------------------------------------------------------------------------
# Normalized search/detail schema
# ---------------------------------------------------------------------------


@dataclass
class JobPosting:
    """Normalized search result. Every provider's ``search()`` must emit
    dicts that validate against this schema."""

    id: str  # self-describing job id: "<board>:<base64url(payload)>"
    title: str
    company: str
    location: str
    url: str
    board: str  # provider token, e.g. "greenhouse"
    snippet: str = ""
    remote: bool = False
    posted_date: str = ""  # ISO-8601 date or "" when unknown
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobDetail:
    """Normalized detail record. Every provider's ``get_details()`` must
    emit dicts that validate against this schema, or an error dict with
    exactly the keys ``{"board", "payload", "error"}`` or
    ``{"url", "board", "error"}``."""

    url: str
    board: str
    title: str
    company: str
    location: str
    description: str
    apply_url: str = ""
    departments: list[str] = field(default_factory=list)
    employment_type: str = ""
    remote: bool = False
    posted_date: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    # Apply-readiness enrichment (epic 2): what the ATS API reveals about
    # the application surface, without submitting anything.
    application_questions: int = 0
    custom_fields: list[str] = field(default_factory=list)
    closing_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_POSTING_REQUIRED = ("id", "title", "company", "location", "url", "board")
_DETAIL_REQUIRED = ("url", "board", "title", "company", "location", "description")
# The only legal error-dict shapes for get_details().
_ERROR_SHAPES = (
    frozenset({"board", "payload", "error"}),
    frozenset({"url", "board", "error"}),
)


def _missing(value: Any) -> bool:
    """A required field is missing when it is None or the empty string.

    Both schema validators use this rule (previously they disagreed:
    ``not raw.get(key)`` vs ``in (None, "")``).
    """
    return value is None or value == ""


def validate_job_posting(raw: dict[str, Any]) -> list[str]:
    """Return a list of conformance problems (empty = conforms)."""
    problems: list[str] = []
    for key in _POSTING_REQUIRED:
        if _missing(raw.get(key)):
            problems.append(f"missing required field: {key}")
    job_id = str(raw.get("id") or "")
    board = str(raw.get("board") or "")
    if job_id:
        if ":" not in job_id:
            problems.append("id is not self-describing (<board>:<payload>)")
        else:
            if board and not job_id.startswith(board + ":"):
                problems.append("id prefix does not match board")
            payload = job_id.partition(":")[2]
            if payload:
                # The payload must be base64url (see
                # providers._common.make_job_id); padding was stripped at
                # encode time, so re-add it before decoding.
                padded = payload + "=" * (-len(payload) % 4)
                try:
                    base64.b64decode(
                        padded.encode("ascii"), altchars=b"-_", validate=True
                    )
                except Exception:
                    problems.append(
                        "id payload is not base64url "
                        "(<board>:<base64url(payload)>)"
                    )
    return problems


def validate_job_detail(raw: dict[str, Any]) -> list[str]:
    """Return conformance problems (empty = conforms).

    Error dicts (``{"error": ...}``) conform only when they carry exactly
    one of the documented error shapes — ``{"board", "payload", "error"}``
    or ``{"url", "board", "error"}`` — with a truthy ``error`` and board
    context. Extra keys fail: the shape is exact so consumers can rely on
    it.
    """
    if raw.get("error"):
        problems: list[str] = []
        keys = frozenset(raw.keys())
        if keys not in _ERROR_SHAPES:
            problems.append(
                f"error dict has unexpected keys {sorted(keys)}; must be "
                'exactly {"board", "payload", "error"} or '
                '{"url", "board", "error"}'
            )
        if not raw.get("board"):
            problems.append("error dict missing board context")
        return problems
    problems = []
    for key in _DETAIL_REQUIRED:
        if _missing(raw.get(key)):
            problems.append(f"missing required field: {key}")
    if not isinstance(raw.get("departments", []), list):
        problems.append("departments must be a list")
    return problems


# ---------------------------------------------------------------------------
# Connector manifest — the proof-of-value declaration
# ---------------------------------------------------------------------------

#: The documented connector_type vocabulary. ``ConnectorManifest.validate``
#: constrains ``connector_type`` to exactly this list: a typo (e.g.
#: ``"job_providers"``) fails validation rather than silently certifying an
#: unexamined interface.
CONNECTOR_TYPES = frozenset({"job_provider", "ats", "channel", "calendar"})


@dataclass
class ConnectorManifest:
    """What every connector declares before release (roadmap proof of value).

    * ``what_it_can_do`` — capability contract in plain language.
    * ``why_it_may_be_blocked`` — honest limitations: ToS, credentials,
      CAPTCHA, rate limits, probe results.
    * ``what_data_it_sends`` — every byte that leaves the device, named.
    * ``what_requires_confirmation`` — actions that need the human's
      explicit go-ahead at the moment of action.
    """

    name: str
    connector_type: str  # "job_provider" | "ats" | "channel" | "calendar"
    what_it_can_do: list[str]
    why_it_may_be_blocked: list[str]
    what_data_it_sends: list[str]
    what_requires_confirmation: list[str]
    official_interface: str = ""
    interface_basis: str = ""

    def validate(self) -> list[str]:
        """Return conformance problems (empty = conforms).

        A manifest is complete only when all four proof-of-value sections
        are non-empty — an empty section is a missing declaration, which
        blocks release under the Q2 gate. ``connector_type`` must be a
        member of the documented vocabulary (``CONNECTOR_TYPES``): a typo
        must fail validation, not silently certify an unexamined
        interface.
        """
        problems: list[str] = []
        if not self.name:
            problems.append("manifest missing name")
        if self.connector_type not in CONNECTOR_TYPES:
            problems.append(
                f"manifest connector_type {self.connector_type!r} not in "
                "documented vocabulary ('job_provider'|'ats'|'channel'|"
                "'calendar') — a misspelled type must not certify an "
                "unexamined interface"
            )
        for section in (
            "what_it_can_do",
            "why_it_may_be_blocked",
            "what_data_it_sends",
            "what_requires_confirmation",
        ):
            if not getattr(self, section):
                problems.append(f"manifest section empty: {section}")
        return problems

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Registered manifests for the built-in job providers and the ATS submit
# registry. Communication channels and the calendar handoff register their
# own manifests in initiatives/i09 (channels/registry.py,
# calendar_handoff.py). The key set here must equal the key set of
# TERMS_CLASSIFICATION (see ``registry.agreement`` in conformance_suite).
PROVIDER_MANIFESTS: dict[str, ConnectorManifest] = {}


def _register_manifest(manifest: ConnectorManifest) -> ConnectorManifest:
    PROVIDER_MANIFESTS[manifest.name] = manifest
    return manifest


_register_manifest(
    ConnectorManifest(
        name="greenhouse",
        connector_type="job_provider",
        what_it_can_do=[
            "Search jobs across curated Greenhouse-hosted company boards "
            "via the public Job Board API (no auth).",
            "Fetch full job details including departments, offices, "
            "metadata, and application questions.",
        ],
        why_it_may_be_blocked=[
            "Direct application submission is NOT possible: the Job Board "
            "API's POST applications endpoint requires an employer-issued "
            "API key a job seeker cannot obtain (verified against official "
            "docs 2026-09-10). Greenhouse applies stay browser-assisted.",
            "Boards are per-company tokens; tokens churn as employers join "
            "or leave Greenhouse.",
        ],
        what_data_it_sends=[
            "Search query, location filter, and limit to "
            "boards-api.greenhouse.io over HTTPS.",
            "Job-id payloads (board token + job id) to fetch details.",
        ],
        what_requires_confirmation=[
            "Browser-assisted application fill (browser_apply is fill-only; "
            "the user clicks submit themselves).",
        ],
        official_interface="https://developers.greenhouse.io/",
        interface_basis="Official public API; live-probed 2026-09-10.",
    )
)

_register_manifest(
    ConnectorManifest(
        name="lever",
        connector_type="job_provider",
        what_it_can_do=[
            "Search jobs across curated Lever-hosted company boards via the "
            "public postings API (no auth).",
            "Fetch full posting details including categories, workplace "
            "type, lists (departments/teams), and apply URLs.",
        ],
        why_it_may_be_blocked=[
            "Direct application submission is NOT possible: POST apply "
            "requires a Lever-issued POST API key (probe returned 403 "
            "'You need an API key' on 2026-09-10). Lever applies stay "
            "browser-assisted.",
            "Public API is per-company; tokens 404 when employers leave "
            "Lever and are skipped gracefully.",
        ],
        what_data_it_sends=[
            "Search query, location filter, and limit to api.lever.co "
            "over HTTPS.",
            "Company token + posting id to fetch details.",
        ],
        what_requires_confirmation=[
            "Browser-assisted application fill (browser_apply is fill-only; "
            "the user clicks submit themselves).",
        ],
        official_interface="https://github.com/lever/postings-api",
        interface_basis="Official public API; live-probed 2026-09-10.",
    )
)

_register_manifest(
    ConnectorManifest(
        name="ashby",
        connector_type="job_provider",
        what_it_can_do=[
            "Search jobs across curated Ashby-hosted company boards via the "
            "public posting API (no auth).",
            "Fetch posting details including department, team, employment "
            "type, location, remote flag, and apply URL.",
        ],
        why_it_may_be_blocked=[
            "Direct application submission is NOT possible: "
            "applicationForm.submit returned 401 Unauthorized on live "
            "non-mutating probes (2026-09-10). Ashby applies stay "
            "browser-assisted.",
            "The detail endpoint re-fetches the whole board and finds the "
            "posting by id; if Ashby changes the board shape, details "
            "degrade gracefully.",
        ],
        what_data_it_sends=[
            "Search query, location filter, and limit to "
            "api.ashbyhq.com/posting-api over HTTPS.",
            "Board token + posting id to fetch details.",
        ],
        what_requires_confirmation=[
            "Browser-assisted application fill (browser_apply is fill-only; "
            "the user clicks submit themselves).",
        ],
        official_interface="https://developers.ashbyhq.com/docs/creating-a-custom-careers-page",
        interface_basis="Official public API; live-probed 2026-09-10.",
    )
)

_register_manifest(
    ConnectorManifest(
        name="adzuna",
        connector_type="job_provider",
        what_it_can_do=[
            "Search jobs via the official Adzuna API when ADZUNA_APP_ID and "
            "ADZUNA_APP_KEY are configured.",
        ],
        why_it_may_be_blocked=[
            "Without API credentials the provider raises RuntimeError on "
            "search — it is an honest stub, not a working connector.",
            "get_details is not implemented even with credentials.",
        ],
        what_data_it_sends=[
            "Search query, location, and the user's Adzuna app credentials "
            "to api.adzuna.com over HTTPS (only when configured).",
        ],
        what_requires_confirmation=[
            "Credential configuration (user provides their own Adzuna app "
            "key; Veto never invents one).",
        ],
        official_interface="https://developer.adzuna.com/",
        interface_basis="Official API; key-gated.",
    )
)

_register_manifest(
    ConnectorManifest(
        name="glassdoor",
        connector_type="job_provider",
        what_it_can_do=[
            "Nothing yet: this is a declared stub so the connector list "
            "stays honest about coverage.",
        ],
        why_it_may_be_blocked=[
            "No working public interface exists in Veto today; search "
            "raises NotImplementedError rather than pretending.",
        ],
        what_data_it_sends=[
            "Nothing: the stub performs no network calls.",
        ],
        what_requires_confirmation=[
            "Enabling a future real implementation (would need a new "
            "manifest and review).",
        ],
        official_interface="",
        interface_basis="Declared stub; no interface probed successfully.",
    )
)

_register_manifest(
    ConnectorManifest(
        name="ats_direct",
        connector_type="ats",
        what_it_can_do=[
            "Registry of direct-submit ATS endpoints. Today the registry "
            "holds no usable anonymous submit endpoint and ats_apply "
            "contains no submit path: apply_direct() always returns a "
            "dry-run preview and never performs a network submit.",
        ],
        why_it_may_be_blocked=[
            "Ashby applicationForm.submit: 401 Unauthorized on live probes "
            "(2026-09-10).",
            "Lever POST apply: requires a Lever-issued POST API key "
            "(403 'You need an API key', 2026-09-10).",
            "Greenhouse POST applications: requires an employer-issued Job "
            "Board API key a job seeker cannot obtain (official docs).",
            "An endpoint is only submittable after flipping its registry "
            "entry to 'verified' with a field spec.",
        ],
        what_data_it_sends=[
            "Nothing today: no verified endpoint exists, so no application "
            "payload ever leaves the device through this path.",
        ],
        what_requires_confirmation=[
            "Any future direct submit would require explicit confirmation "
            "at the moment of action (roadmap constraint 02).",
        ],
        official_interface="https://developers.ashbyhq.com/ + Lever postings API + Greenhouse Job Board API docs",
        interface_basis="Live non-mutating probes + official docs, 2026-09-10.",
    )
)


def manifest_for(name: str) -> ConnectorManifest | None:
    """Return the registered manifest for a connector, or None."""
    return PROVIDER_MANIFESTS.get(name)


def all_manifests() -> list[ConnectorManifest]:
    """All registered job-provider/ATS manifests, sorted by name."""
    return [PROVIDER_MANIFESTS[k] for k in sorted(PROVIDER_MANIFESTS)]


# ---------------------------------------------------------------------------
# Conformance suite — the Q2 gate's "capability contract" half
# ---------------------------------------------------------------------------

_SUBMIT_CHECKS = (
    "submit.interface",
    "submit.no_silent_drops",
    "submit.preview_honest",
    "submit.unknown_status_rejected",
)


def conformance_suite(
    provider: Any,
    search_fixture: list[dict[str, Any]] | None = None,
    detail_fixture: dict[str, Any] | None = None,
    apply: Any | None = None,
    apply_job: dict[str, Any] | None = None,
    apply_applicant: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run the capability-contract checks for one connector.

    ``apply`` is the connector's apply callable with the
    ``SubmissionConnector`` signature (or the provider's own ``apply``
    method); ``apply_job`` / ``apply_applicant`` are the fixtures it is
    dry-run against. The suite only ever dry-runs
    (``confirm=False, dry_run=True``) — no submission is attempted.

    Checks (each returns a ``{"check", "passed", "detail"}`` record):
    1. manifest exists and is complete (the four proof-of-value sections).
    2. terms classification is registered with evidence — for any
       connector type, not just job providers (ats_direct must be
       certifiable by its own gate).
    3. the terms registry and the manifest registry agree (one
       authoritative registry story).
    4. budget/cooldown follow the declared policy: connectors that cannot
       search declare zero; the rest declare enforcement's tier budget
       AND base cooldown (drift detection against compliance.py on both
       numbers, not just the daily budget).
    5. search/detail interface callables (job providers only).
    6. search fixture rows validate against the JobPosting schema.
    7. detail fixture validates against the JobDetail schema.
    8. submit interface: the apply callable matches the
       ``SubmissionConnector`` signature (submit-capable connectors only).
    9. no silent drops: a dry-run apply returns a ``SubmissionResult``
       whose status is in the ``SubmissionStatus`` vocabulary.
    10. preview honesty: a dry run reports ``draft`` with no confirmation.
    11. unknown statuses are rejected (headline negative control).
    """
    results: list[dict[str, Any]] = []

    def record(check: str, passed: bool, detail: str = "") -> None:
        results.append({"check": check, "passed": passed, "detail": detail})

    name = getattr(provider, "name", None) or type(provider).__name__
    manifest = PROVIDER_MANIFESTS.get(name)
    if manifest is None:
        record("manifest.registered", False, f"no manifest for {name!r}")
    else:
        problems = manifest.validate()
        record(
            "manifest.complete",
            not problems,
            "; ".join(problems) if problems else "all four proof-of-value sections present",
        )
    ctype = manifest.connector_type if manifest else ""

    # 2. Terms classification: registered with evidence, whatever the
    # connector type.
    terms, basis, _robots_url = terms_for(name)
    record(
        "terms.classified",
        name in TERMS_CLASSIFICATION,
        f"{terms.value}; basis: {basis[:80]}"
        if name in TERMS_CLASSIFICATION
        else "no terms entry registered — unregistered connectors cannot "
        "be certified (they default to honest stub)",
    )

    # 3. One authoritative registry story: terms and manifests agree.
    terms_names = set(TERMS_CLASSIFICATION)
    manifest_names = set(PROVIDER_MANIFESTS)
    agreement_ok = terms_names == manifest_names
    record(
        "registry.agreement",
        agreement_ok,
        "terms and manifest registries agree"
        if agreement_ok
        else "terms-only: "
        f"{sorted(terms_names - manifest_names)}; manifest-only: "
        f"{sorted(manifest_names - terms_names)}",
    )

    # 4. Budget policy (encoded independently of budget_for's
    # implementation): connectors that cannot search declare zero; the
    # rest declare the tier budget enforcement uses — a drift detector, not
    # a tautology. Both daily budget AND base cooldown are compared
    # against compliance.py.
    daily, cooldown = budget_for(name)
    budget_problems: list[str] = []
    if not isinstance(daily, int) or daily < 0:
        budget_problems.append(f"daily budget not a non-negative int: {daily!r}")
    if not isinstance(cooldown, int) or cooldown <= 0:
        budget_problems.append(f"cooldown not a positive int: {cooldown!r}")
    try:
        import compliance

        expected_daily = (
            int(
                compliance.DEFAULT_SEARCH_BUDGET.get(
                    compliance.board_tier(name), 0
                )
            )
            if _declares_search(name)
            else 0
        )
        if daily != expected_daily:
            budget_problems.append(
                f"declared daily={daily} != policy expected={expected_daily} "
                "(stubs and non-search connectors declare zero; the rest "
                "declare enforcement's tier budget)"
            )
        expected_cooldown = int(compliance.BLOCK_COOLDOWN_BASE_SECONDS)
        if cooldown != expected_cooldown:
            budget_problems.append(
                f"declared cooldown_s={cooldown} != policy expected="
                f"{expected_cooldown} (enforcement's base cooldown; "
                "contract-kit/compliance.py drift)"
            )
        budget_detail = (
            "; ".join(budget_problems)
            if budget_problems
            else f"daily={daily} cooldown_s={cooldown} matches policy"
        )
    except Exception:
        budget_detail = (
            "; ".join(budget_problems)
            if budget_problems
            else f"daily={daily} cooldown_s={cooldown} "
            "(compliance unavailable; sanity checks only)"
        )
    record("budget.declared", not budget_problems, budget_detail)

    # 5-7. Search/detail interface — job providers (and unregistered
    # connectors, which must prove themselves) only.
    search_required = ctype in ("", "job_provider")
    has_search = callable(getattr(provider, "search", None))
    has_details = callable(getattr(provider, "get_details", None))
    if search_required:
        record(
            "interface.search",
            has_search,
            "provider.search callable" if has_search else "missing search()",
        )
        record(
            "interface.get_details",
            has_details,
            "provider.get_details callable"
            if has_details
            else "missing get_details()",
        )
    else:
        record(
            "interface.search",
            True,
            f"n/a — {ctype} connector has no search interface",
        )
        record(
            "interface.get_details",
            True,
            f"n/a — {ctype} connector has no search interface",
        )
    if search_fixture:
        # Malformed fixtures (e.g. [None], or a non-list) fail THIS
        # check with a record — they must never abort the whole suite
        # with zero records.
        if not isinstance(search_fixture, (list, tuple)):
            record(
                "schema.search_rows",
                False,
                f"search_fixture is not a list of rows: "
                f"{type(search_fixture).__name__}",
            )
        else:
            posting_problems: list[str] = []
            for i, row in enumerate(search_fixture):
                try:
                    row_problems = validate_job_posting(row)
                except Exception as exc:
                    row_problems = [
                        f"row {i}: fixture malformed "
                        f"({type(row).__name__}: {exc})"
                    ]
                posting_problems.extend(f"row {i}: {p}" for p in row_problems)
            record(
                "schema.search_rows",
                not posting_problems,
                "; ".join(posting_problems[:5])
                if posting_problems
                else f"{len(search_fixture)} fixture rows conform",
            )
    if detail_fixture:
        try:
            detail_problems = validate_job_detail(detail_fixture)
        except Exception as exc:
            detail_problems = [
                f"detail fixture malformed "
                f"({type(detail_fixture).__name__}: {exc})"
            ]
        record(
            "schema.detail",
            not detail_problems,
            "; ".join(detail_problems) if detail_problems else "detail fixture conforms",
        )

    # 8-11. Submission contract — connectors that submit (ats type, or an
    # explicitly supplied apply callable, or a provider apply method).
    apply_fn = apply if callable(apply) else getattr(provider, "apply", None)
    submit_capable = ctype == "ats" or callable(apply_fn)
    if not submit_capable:
        for chk in _SUBMIT_CHECKS:
            record(chk, True, "n/a — connector declares no submit path")
        return results
    if not callable(apply_fn):
        for chk in (
            "submit.interface",
            "submit.no_silent_drops",
            "submit.preview_honest",
        ):
            record(
                chk,
                False,
                "declares a submit path but exposes no apply() callable",
            )
    else:
        sig_problems = apply_signature_problems(apply_fn)
        record(
            "submit.interface",
            not sig_problems,
            "; ".join(sig_problems)
            if sig_problems
            else "apply(job, applicant, *, confirm=False, dry_run=True)",
        )
        job_fx = (
            apply_job
            if isinstance(apply_job, dict)
            else {"id": "ashby:Zm9vYmFy", "board": "ashby"}
        )
        applicant_fx = (
            apply_applicant
            if isinstance(apply_applicant, dict)
            else {
                "full_name": "Contract Fixture",
                "email": "fixture@example.com",
            }
        )
        dry_result = None
        dry_error: str | None = None
        try:
            dry_result = apply_fn(
                job_fx, applicant_fx, confirm=False, dry_run=True
            )
        except Exception as exc:
            dry_error = f"{type(exc).__name__}: {exc}"
        if dry_error is not None:
            # The dry run raised instead of returning a result: every
            # check that needs a result fails — each recorded exactly
            # once (an exception must not double-record the checks).
            record(
                "submit.no_silent_drops",
                False,
                f"dry-run apply() raised instead of returning a result: {dry_error}",
            )
            record("submit.preview_honest", False, "no result to inspect")
        elif dry_result is None:
            # A None return is the textbook silent drop: the call
            # produced no outcome at all.
            record(
                "submit.no_silent_drops",
                False,
                "dry-run apply() returned None — a silent drop; every apply "
                "call must yield a SubmissionResult",
            )
            record("submit.preview_honest", False, "no result to inspect")
        elif not isinstance(dry_result, SubmissionResult):
            record(
                "submit.no_silent_drops",
                False,
                f"dry-run apply() returned {type(dry_result).__name__}, "
                "not SubmissionResult — the outcome has no vocabulary "
                "status (adapt via submission_result_from)",
            )
            record(
                "submit.preview_honest",
                False,
                "no SubmissionResult to inspect",
            )
        else:
            vproblems = validate_submission_result(dry_result)
            norm = _normalize_status(dry_result.status)
            record(
                "submit.no_silent_drops",
                not vproblems,
                "; ".join(vproblems)
                if vproblems
                else "dry-run yielded vocabulary status "
                f"{(norm.value if norm is not None else dry_result.status)!r}",
            )
            honest = (
                norm is SubmissionStatus.DRAFT
                and not str(dry_result.confirmation or "").strip()
            )
            status_label = norm.value if norm is not None else dry_result.status
            record(
                "submit.preview_honest",
                honest,
                f"dry-run status={status_label!r}"
                + (
                    ""
                    if honest
                    else " — a dry run must be 'draft' with no "
                    "confirmation evidence"
                ),
            )
    # 11. Headline negative control: an unknown status must fail
    # validation, proving the vocabulary is closed.
    forged = SubmissionResult(status="captcha_bypassed", connector=name)  # type: ignore[arg-type]
    fproblems = validate_submission_result(forged)
    record(
        "submit.unknown_status_rejected",
        bool(fproblems),
        "; ".join(fproblems)
        if fproblems
        else "forged status 'captcha_bypassed' was ACCEPTED — the vocabulary "
        "is not closed",
    )
    return results


def suite_passed(results: list[dict[str, Any]]) -> bool:
    """True when every conformance check passed."""
    return all(r["passed"] for r in results)
