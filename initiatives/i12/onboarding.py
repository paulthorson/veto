#!/usr/bin/env python3
"""Initiative 12 / Epic 5 — Onboarding experiments.

Compares onboarding paths across a role-maturity x technical-comfort grid so
Veto learns which guidance actually activates people — with a hard rule:
**no dark patterns, ever.** The prohibition is encoded as a checked list,
not a policy paragraph: every onboarding path definition and every piece of
onboarding copy passes through :func:`check_dark_patterns`.

Experiment design:

* Two axes: role maturity (``new`` | ``switching`` | ``senior``) and
  technical comfort (``low`` | ``medium`` | ``high``) -> up to 9 cells.
* Each cell maps to a path: an ordered list of steps with honest framing.
* Assignment is deterministic by grid cell (see :func:`assign_path` for the
  session-token lifecycle requirement behind the "never re-randomized"
  guarantee).
* Measured outcome: qualified activation — a completed core workflow in the
  session, detected as a pure function by
  :func:`detect_qualified_activation` — never vanity counts.

Dark patterns banned (checked by code in :func:`check_dark_patterns`):

* false urgency / fake scarcity ("only 3 left!", resetting countdown
  timers like 04:59 next to "expires"/"claim"),
* confirm-shaming, including decline-label variants
  ("No, I hate getting interviews", "No thanks, I'd rather ..."),
* nagging loops that punish declining ("Wait! Are you sure? Users who skip
  this step almost never get hired."),
* roach motel (easy in, hard to cancel/delete — including delete-via-email
  or delete-via-support hurdles),
* hidden costs or surprise steps (including paywalls and trial-to-charge
  copy that never uses the word "free"),
* disguised ads or trick CTAs.

Pre-checked consent is banned too, but it is **not** checked by
:func:`check_dark_patterns`: copy strings cannot express checkbox state, so
the copy checker cannot honestly promise to ban it. Instead it is enforced
at the UI-definition layer — every consent toggle must be registered with
:func:`register_consent_toggle`, and registration raises
:exc:`PreCheckedConsent` when a toggle defaults on (see
:func:`check_consent_defaults`). The one consent surface in onboarding
(analytics opt-in) is registered default-off at import time.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ROLE_MATURITY = ("new", "switching", "senior")
TECH_COMFORT = ("low", "medium", "high")

#: The four core workflows an onboarding path can activate toward. Every
#: OnboardingPath.goal_workflow must be a key here; this removes the
#: previously dangling ``role_compare`` reference by defining it.
GOAL_WORKFLOWS: dict[str, str] = {
    "jd_decode": "Decode a job description into a transparency verdict "
                 "with quoted evidence.",
    "risk_check": "Run a pre-apply risk check: blockers, warnings, next "
                  "steps. Never applies for the user.",
    "fit_explainer": "Walk the five fit factors with visible evidence and "
                     "weights.",
    "role_compare": "Compare up to 4 roles side by side (fit, risk count, "
                    "compensation as quoted or 'not listed', location, "
                    "readiness).",
}

#: (pattern, dark-pattern name, why it's banned). Checked against all
#: onboarding copy. Case-insensitive. Note: pre-checked consent is NOT here
#: on purpose — see the module docstring and register_consent_toggle.
DARK_PATTERN_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), name, why)
    for p, name, why in (
        (r"only \d+ (left|remaining|spots?)", "false scarcity",
         "invented scarcity pressures decisions without evidence"),
        (r"\bhurry\b|\bact now\b|\bdon'?t miss out\b|\blast chance\b",
         "false urgency",
         "urgency must reflect a real deadline, never be manufactured"),
        # Resetting countdown timers: a clock next to pressure words.
        (r"\b\d{1,2}:\d{2}\b.{0,40}(expir|left|claim|hurry|remaining)",
         "false urgency",
         "countdown timers next to pressure words manufacture urgency; "
         "a timer that resets on reload is not a real deadline"),
        (r"(expir|left|claim|hurry).{0,40}\b\d{1,2}:\d{2}\b",
         "false urgency",
         "countdown timers next to pressure words manufacture urgency; "
         "a timer that resets on reload is not a real deadline"),
        (r"\bno,?\s*i\s+(don'?t|do not|hate)\b", "confirm-shaming",
         "shaming the decline option manipulates the choice"),
        # Decline-label variants of confirm-shaming.
        (r"\bno thanks,?\s*i'?d rather\b", "confirm-shaming",
         "guilt-laden decline labels punish saying no"),
        # Nagging loops that punish declining.
        (r"\bwait!?\s*are you sure\b", "nagging loop",
         "re-confirmation nagging wears down a declined choice"),
        (r"\bare you sure you want to (skip|leave|cancel|decline|close|go)\b",
         "nagging loop",
         "re-confirmation nagging wears down a declined choice"),
        (r"\busers? who (skip|decline|leave).{0,60}(never|rarely)\b",
         "nagging loop",
         "threatening outcomes to punish skipping manipulates the choice"),
        (r"cancel.{0,30}call us|to cancel.{0,30}contact support",
         "roach motel",
         "leaving must be as easy as joining; self-serve cancellation"),
        # Delete-via-email / delete-via-support hurdles.
        (r"\bto delete.{0,40}(email|contact|write to|support|call us)\b",
         "roach motel",
         "account deletion must be self-serve, not a support ticket"),
        (r"\bdelet.{0,20}(your )?(account|data).{0,40}"
         r"(email us|contact us|support ticket)\b",
         "roach motel",
         "account deletion must be self-serve, not a support ticket"),
        (r"\bfree\b.{0,40}(credit card|payment).{0,40}required",
         "hidden cost",
         "costs are stated before commitment, never after"),
        # Paywalls and trial-to-charge copy that never says "free".
        (r"\b(unlock|upgrade to|go (pro|premium)|premium).{0,50}\$\d",
         "hidden cost",
         "paywalls are stated before commitment, never sprung mid-flow"),
        (r"\b(trial|membership|subscription).{0,60}"
         r"(auto-?renew|you'?ll be charged|will be charged|kicks in)",
         "hidden cost",
         "trial-to-charge transitions are stated before the trial starts"),
        (r"\byou'?ve (won|been selected)\b", "fake reward",
         "no invented prizes or selections"),
        (r"continue.{0,20}»|click here to continue", "trick CTA",
         "CTAs say what they do; no misdirection"),
    )
)


# ---------------------------------------------------------------------------
# Fail-closed escape hatch: explicit, auditable copy-check overrides.
# ---------------------------------------------------------------------------
#: pattern name -> matched text -> override record. Empty by default: no
#: copy is exempt from the rules unless explicitly registered here.
DARK_PATTERN_OVERRIDES: dict[str, dict[str, dict[str, str]]] = {}

#: Append-only audit trail of every registered override.
OVERRIDE_AUDIT: list[dict[str, str]] = []


def register_dark_pattern_override(pattern_name: str, matched_text: str, *,
                                    reason: str, approved_by: str) -> None:
    """Register an explicit, auditable override for the copy checker.

    This is the fail-closed escape hatch: a single regex false positive in
    future copy must not brick all 9 paths at deploy time, but the escape
    cannot be silent. Every override names the exact matched text it covers,
    states a reason, and names the approver; all registrations land in
    :data:`OVERRIDE_AUDIT`. Overrides match on (pattern name, exact matched
    text) so they cannot drift onto new copy.
    """
    record = {
        "pattern": pattern_name,
        "matched_text": matched_text,
        "reason": reason,
        "approved_by": approved_by,
        "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    DARK_PATTERN_OVERRIDES.setdefault(pattern_name, {})[matched_text] = record
    OVERRIDE_AUDIT.append(record)


def _is_overridden(pattern_name: str, matched_text: str) -> dict[str, str] | None:
    return DARK_PATTERN_OVERRIDES.get(pattern_name, {}).get(matched_text)


def check_dark_patterns(copy: str, *,
                        apply_overrides: bool = True) -> list[dict[str, str]]:
    """Scan onboarding copy for banned dark patterns.

    Returns a list of {pattern, matched_text, why_banned}. Empty = clean.
    Findings covered by an explicit override (see
    :func:`register_dark_pattern_override`) are reported with
    ``"overridden": "true"`` and the override record, and are excluded by
    default when ``apply_overrides`` is True.
    """
    findings = []
    for rx, name, why in DARK_PATTERN_RULES:
        m = rx.search(copy or "")
        if not m:
            continue
        matched = m.group(0)[:80]
        override = _is_overridden(name, matched)
        if override and apply_overrides:
            findings.append({
                "pattern": name,
                "matched_text": matched,
                "why_banned": why,
                "overridden": "true",
                "override_reason": override["reason"],
                "override_approved_by": override["approved_by"],
            })
        else:
            findings.append({
                "pattern": name,
                "matched_text": matched,
                "why_banned": why,
            })
    return findings


def assert_no_dark_patterns(copy: str, where: str, *,
                            apply_overrides: bool = True) -> None:
    """Raise :exc:`DarkPatternDetected` unless copy is clean or explicitly
    overridden. Fail-closed: overrides only suppress when the exact matched
    text was registered via :func:`register_dark_pattern_override`."""
    findings = check_dark_patterns(copy, apply_overrides=apply_overrides)
    live = [f for f in findings if f.get("overridden") != "true"]
    if live:
        raise DarkPatternDetected(where, live)


class DarkPatternDetected(Exception):
    def __init__(self, where: str, findings: list[dict[str, str]]):
        self.where = where
        self.findings = findings
        super().__init__(
            f"Dark pattern detected in {where}: "
            + "; ".join(f["pattern"] for f in findings)
        )


# ---------------------------------------------------------------------------
# UI-definition layer enforcement for pre-checked consent.
# ---------------------------------------------------------------------------
class PreCheckedConsent(ValueError):
    """A consent toggle was registered with default-on state. Consent is
    opt-in: nothing consequential is pre-enabled."""


#: Consent-toggle registry: toggle_id -> record. Copy strings cannot express
#: checkbox state, so pre-checked consent is enforced here, not in the copy
#: checker. UI layers (terminal wizard, web UI) must register their consent
#: toggles; unregistered toggles are a code-review finding, registered
#: default-on toggles raise immediately.
CONSENT_TOGGLES: dict[str, dict[str, Any]] = {}

#: Append-only audit trail of toggle registrations.
CONSENT_AUDIT: list[dict[str, str]] = []


def register_consent_toggle(toggle_id: str, *, default_on: bool,
                            description: str, surface: str) -> None:
    """Register a UI consent toggle. Raises :exc:`PreCheckedConsent` if it
    defaults on — registration itself is the enforcement point, so a
    pre-checked default cannot be added silently."""
    record = {
        "toggle_id": toggle_id,
        "default_on": default_on,
        "description": description,
        "surface": surface,
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    CONSENT_TOGGLES[toggle_id] = record
    CONSENT_AUDIT.append({**record, "default_on": str(default_on)})
    if default_on:
        raise PreCheckedConsent(
            f"consent toggle {toggle_id!r} ({description}) on {surface} "
            "defaults ON: consent must be opt-in"
        )


def check_consent_defaults() -> list[str]:
    """Fail-closed audit: raise :exc:`PreCheckedConsent` if any registered
    toggle defaults on. Returns the checked toggle ids when clean."""
    bad = [tid for tid, t in CONSENT_TOGGLES.items() if t["default_on"]]
    if bad:
        raise PreCheckedConsent(
            "consent toggles defaulting ON (must be opt-in): "
            + ", ".join(bad)
        )
    return sorted(CONSENT_TOGGLES)


# Onboarding's one consent surface: product analytics. Off until explicit
# opt-in; the copy says so and the toggle registry proves it.
register_consent_toggle(
    "analytics-opt-in",
    default_on=False,
    description="Product analytics; off until explicit opt-in. "
                "Never includes resume text.",
    surface="onboarding privacy step",
)


@dataclass(frozen=True)
class OnboardingStep:
    id: str
    title: str
    body: str
    cta: str
    skippable: bool = True

    def __post_init__(self) -> None:
        assert_no_dark_patterns(f"{self.title}\n{self.body}\n{self.cta}",
                                f"onboarding step {self.id!r}")


@dataclass(frozen=True)
class OnboardingPath:
    id: str
    role_maturity: str
    tech_comfort: str
    steps: tuple[OnboardingStep, ...]
    goal_workflow: str  # the core workflow this path activates toward

    def __post_init__(self) -> None:
        if self.role_maturity not in ROLE_MATURITY:
            raise ValueError(f"bad role_maturity {self.role_maturity!r}")
        if self.tech_comfort not in TECH_COMFORT:
            raise ValueError(f"bad tech_comfort {self.tech_comfort!r}")
        if self.goal_workflow not in GOAL_WORKFLOWS:
            raise ValueError(f"bad goal_workflow {self.goal_workflow!r}; "
                             f"must be one of {sorted(GOAL_WORKFLOWS)}")


def _s(id: str, title: str, body: str, cta: str,
        skippable: bool = True) -> OnboardingStep:
    return OnboardingStep(id=id, title=title, body=body, cta=cta,
                          skippable=skippable)


def build_paths() -> dict[str, OnboardingPath]:
    """Define the experiment's onboarding paths (one per grid cell)."""
    paths: dict[str, OnboardingPath] = {}

    def add(rm: str, tc: str, goal: str, steps: list[OnboardingStep]) -> None:
        pid = f"onboard-{rm}-{tc}"
        paths[pid] = OnboardingPath(id=pid, role_maturity=rm,
                                    tech_comfort=tc,
                                    steps=tuple(steps),
                                    goal_workflow=goal)

    welcome = _s("welcome", "See what a job posting is really saying",
                 "Paste any job description. Veto decodes it on your device — "
                 "nothing is uploaded, and you don't need an account.",
                 "Try the JD decoder")
    privacy = _s("privacy", "Your data stays on your device",
                 "Profiles, resumes, and outcomes live on your computer. "
                 "Analytics are off unless you explicitly opt in, and they "
                 "never include resume text.",
                 "Understood — continue")
    first_decode = _s("first_decode", "Decode your first posting",
                      "Paste a job description you are considering. Look for "
                      "the transparency score and the quoted evidence.",
                      "Decode a posting", skippable=False)
    fit_tour = _s("fit_tour", "How Veto scores fit",
                  "Five factors, each with visible evidence and weights. "
                  "A better number without a reason is not a better product.",
                  "See the five factors")
    evidence_intro = _s("evidence_intro", "Evidence, not keywords",
                        "Veto links each requirement to a fact from your "
                        "profile — or marks it as a gap. Nothing is invented.",
                        "How evidence works")
    risk_intro = _s("risk_intro", "Check the risk before you apply",
                    "The risk check lists blockers, warnings, and next steps. "
                    "It never applies for you — sending always needs your "
                    "explicit review.",
                    "Run a risk check")
    install = _s("install", "Make it yours",
                 "Install the local app to score against your real profile. "
                 "Current path: clone the repo and follow the README. "
                 "The one-command installer is on the roadmap.",
                 "Show me the install steps")
    skip_note = _s("skip_note", "Skip anything",
                   "Every step is skippable except the one hands-on demo. "
                   "You can also delete everything later from settings — "
                   "leaving is as easy as joining.",
                   "Got it")

    add("new", "low", "jd_decode", [welcome, privacy, first_decode, skip_note])
    add("new", "medium", "jd_decode", [welcome, first_decode, fit_tour, skip_note])
    add("new", "high", "jd_decode", [first_decode, fit_tour, install, skip_note])
    add("switching", "low", "risk_check",
        [welcome, privacy, risk_intro, first_decode, skip_note])
    add("switching", "medium", "risk_check",
        [welcome, risk_intro, evidence_intro, skip_note])
    add("switching", "high", "risk_check",
        [risk_intro, evidence_intro, install, skip_note])
    add("senior", "low", "fit_explainer",
        [welcome, privacy, fit_tour, first_decode, skip_note])
    add("senior", "medium", "fit_explainer",
        [fit_tour, evidence_intro, risk_intro, skip_note])
    add("senior", "high", "role_compare",
        [evidence_intro, risk_intro, install, skip_note])
    return paths


# ---------------------------------------------------------------------------
# Assignment: deterministic per cell + module-level assignment log.
# ---------------------------------------------------------------------------
#: Append-only assignment log (module-level, no external deps). Each entry is
#: a plain dict: the sha256 prefix of the session token (never the raw
#: token), the assigned path id, cell axes, a diagnostic hash bucket, and a
#: UTC timestamp. Exists so the "18 paths assigned but only 9 defined" class
#: of misjoin is diagnosable from this module alone.
ASSIGNMENT_LOG: list[dict[str, Any]] = []


def _diagnostic_bucket(session_token: str) -> int:
    """Hash bucket kept only as a diagnostic field in ASSIGNMENT_LOG.

    It does NOT route assignment: the phantom -v{bucket} variant arms were
    removed because both arms shipped identical copies. If real variant arms
    are implemented later, they must be real variant copies, not id suffixes.
    """
    return int(hashlib.sha256(session_token.encode()).hexdigest(), 16) % 2


def assign_path(session_token: str, role_maturity: str,
                tech_comfort: str) -> OnboardingPath:
    """Deterministically assign the onboarding path for a grid cell.

    Returns the canonical path for the cell — ``onboard-{role}-{comfort}`` —
    with no variant id suffix. (The old ``-v{bucket}`` suffix created phantom
    experiment arms: the "variants" were byte-identical copies, so they were
    removed rather than shipped as fake arms.)

    The "never re-randomized" guarantee holds only under an explicit
    assumption: **callers must supply a session token that is stable for a
    given user across the experiment window.** Today the token does not
    influence which path is returned (assignment is by grid cell only), so
    token rotation cannot change assignment; but if real variant arms are
    ever reintroduced, a rotating token would silently re-randomize users.
    Name the assumption when reintroducing variants; do not assume it.

    Every call appends to :data:`ASSIGNMENT_LOG` (sha256 prefix of the
    token, never the raw token) for misjoin diagnosis.
    """
    paths = build_paths()
    pid = f"onboard-{role_maturity}-{tech_comfort}"
    if pid not in paths:
        raise ValueError(f"no onboarding path for {role_maturity}/{tech_comfort}")
    path = paths[pid]
    ASSIGNMENT_LOG.append({
        "session_token_sha256": hashlib.sha256(
            session_token.encode()).hexdigest()[:16],
        "path_id": path.id,
        "role_maturity": role_maturity,
        "tech_comfort": tech_comfort,
        "diagnostic_bucket": _diagnostic_bucket(session_token),
        "assigned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return path


# ---------------------------------------------------------------------------
# Observable outcome: qualified activation, detected as a pure function.
# ---------------------------------------------------------------------------
#: Fixed schema for the qualified-activation event. Field kinds follow the
#: telemetry.EVENT_SCHEMAS conventions (token/flag/count/seconds); there are
#: no free-text fields on purpose.
QUALIFIED_ACTIVATION_SCHEMA: dict[str, str] = {
    "workflow": "token",
    "path_id": "token",
    "session_id": "token",
    "duration_s": "seconds",
    "completed": "flag",
    "steps_completed": "count",
}


def detect_qualified_activation(
        session_events: list[dict[str, Any]],
        path: OnboardingPath) -> dict[str, Any]:
    """Pure function: (ordered session events, assigned path) -> verdict.

    A session counts as a qualified activation when it contains a completed
    ``workflow_completed`` event for the path's own ``goal_workflow``.
    Events for other workflows do not count (a risk_check completion does
    not activate a jd_decode path), which keeps per-cell outcomes honest.

    Returns ``{"activated": bool, "workflow": str | None, "reason": str,
    "event": dict | None}``. ``event`` is shaped to
    :data:`QUALIFIED_ACTIVATION_SCHEMA` and is ready to record as telemetry
    ``workflow_completed`` once analytics consent is on — see
    ``initiatives/i12/integration_notes.md`` for the wiring follow-up
    (telemetry.py itself is untouched).
    """
    events = session_events or []
    session_id = next(
        (e.get("session_id") for e in events if e.get("session_id")), None)
    for e in events:
        if (e.get("type") == "workflow_completed"
                and e.get("workflow") == path.goal_workflow
                and e.get("completed")):
            steps_completed = sum(
                1 for ev in events
                if ev.get("type") == "onboarding_step_completed")
            return {
                "activated": True,
                "workflow": path.goal_workflow,
                "reason": f"completed goal workflow {path.goal_workflow!r}",
                "event": {
                    "workflow": path.goal_workflow,
                    "path_id": path.id,
                    "session_id": session_id,
                    "duration_s": e.get("duration_s", 0),
                    "completed": True,
                    "steps_completed": steps_completed,
                },
            }
    other = [e.get("workflow") for e in events
             if e.get("type") == "workflow_completed" and e.get("completed")]
    reason = ("no completed goal workflow "
              f"({path.goal_workflow!r}); completed instead: {other or 'none'}")
    return {"activated": False, "workflow": None, "reason": reason,
            "event": None}


def experiment_summary() -> dict[str, Any]:
    """Machine-readable experiment definition for dashboards/docs."""
    paths = build_paths()
    return {
        "axes": {"role_maturity": list(ROLE_MATURITY),
                 "tech_comfort": list(TECH_COMFORT)},
        "paths": [
            {"id": p.id, "goal_workflow": p.goal_workflow,
             "goal_workflow_definition": GOAL_WORKFLOWS[p.goal_workflow],
             "steps": [s.id for s in p.steps]}
            for p in paths.values()
        ],
        "assignment": ("deterministic by grid cell; the session token is "
                       "logged (sha256 prefix) for misjoin diagnosis and "
                       "does not route assignment; never re-randomized only "
                       "if callers keep tokens stable per user across the "
                       "experiment window"),
        "primary_outcome": ("qualified_activation: a completed goal workflow "
                            "in the session, detected as a pure function by "
                            "detect_qualified_activation; emitted as a "
                            "workflow_completed event per "
                            "QUALIFIED_ACTIVATION_SCHEMA; never application "
                            "volume"),
        "analysis_plan": (
            "Each cell has a different goal_workflow, so cross-cell "
            "comparisons of raw activation rates are INVALID (confounded: "
            "jd_decode completion is not risk_check completion). Valid: "
            "(a) within-cell comparisons (e.g. real variant arms, once "
            "implemented as real variant copies); (b) cross-cell comparisons "
            "using the common outcome definition — qualified_activation as "
            "detected by detect_qualified_activation over "
            "QUALIFIED_ACTIVATION_SCHEMA — reported per goal_workflow, never "
            "pooled across different goal workflows."
        ),
        "dark_pattern_policy": ("banned by rule; copy checked in code via "
                                "check_dark_patterns; violations raise at "
                                "path-definition time; pre-checked consent "
                                "enforced at the UI-definition layer via "
                                "register_consent_toggle/check_consent_defaults "
                                "(copy strings cannot express checkbox "
                                "state); explicit auditable overrides via "
                                "register_dark_pattern_override"),
        "dark_pattern_overrides_registered": sum(
            len(v) for v in DARK_PATTERN_OVERRIDES.values()),
        "qualified_activation_schema": dict(QUALIFIED_ACTIVATION_SCHEMA),
    }
