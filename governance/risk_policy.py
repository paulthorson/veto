#!/usr/bin/env python3
"""Framework-governed risk policy for veto-mcp.

``compliance.py`` implements the ToS-risk mitigations as deterministic
gates (tiers, modes, budgets, circuit breaker, caps). This module promotes
those mitigations into *governed policy*: every risky action is
adjudicated through the user's agentic-governance framework —

  1. the deterministic compliance gate runs first (fast, no side effects);
  2. the framework's veto screen reviews a plain-language description of
     the action, so anything the user's constitution forbids is caught
     even if compliance.py would allow it;
  3. the verdict is written to the framework's verdict ledger, producing
     an audit trail of every allow / block decision.

Fail-open / fail-closed follows governance/adapter.py: a veto *hit* or a
*failed veto check* blocks (fail closed); an unavailable framework falls
back to the compliance.py decision with a warning (the mitigations still
hold — they just aren't framework-adjudicated); recording failures never
block.

The human-readable policy this enforces lives in
``governance/risk_constitution.md``.
"""

from __future__ import annotations

import logging
from typing import Any

import compliance
from governance import adapter

log = logging.getLogger("veto-mcp.risk_policy")

GOVERNANCE_DOMAIN = adapter.GOVERNANCE_DOMAIN


# ---------------------------------------------------------------------------
# Verdict recording (never raises)
# ---------------------------------------------------------------------------


def record_risk_verdict(
    action: str, passed: bool, summary: str
) -> dict[str, Any]:
    """Append a risk-policy verdict to the framework ledger.

    Never raises: a ledger outage must not break the action being governed.
    """
    try:
        mod = adapter.load_framework()
    except adapter.GovernanceUnavailable as exc:
        return {"recorded": False, "error": str(exc)}
    try:
        return mod.record_verdict(
            domain=GOVERNANCE_DOMAIN,
            verdict="pass" if passed else "veto",
            summary=summary,
            ticket=f"veto:{action}",
            case_tag="risk-allowed" if passed else "risk-blocked",
        )
    except Exception as exc:
        log.warning("record_verdict failed: %s", exc)
        return {"recorded": False, "error": str(exc)}


def _veto_screen(action_text: str) -> tuple[bool, list[str], str]:
    """Run the framework veto screen on an action description.

    Returns (vetoed, hits, warning). A framework *error* is treated as a
    veto (fail closed); an *unavailable* framework returns vetoed=False
    with a warning so the caller falls back to compliance-only.
    """
    try:
        mod = adapter.load_framework()
    except adapter.GovernanceUnavailable as exc:
        return False, [], f"governance unavailable ({exc}); compliance-only"
    try:
        result = mod.check_veto(domain=GOVERNANCE_DOMAIN, text=action_text)
    except Exception as exc:
        return True, [], f"governance veto check failed (fail-closed): {exc}"
    hits = (
        result.get("veto_hits", []) if isinstance(result, dict) else []
    )
    return bool(hits), list(hits), ""


# ---------------------------------------------------------------------------
# Adjudication
# ---------------------------------------------------------------------------


def adjudicate_search(
    board: str, query: str = "", context: str = ""
) -> dict[str, Any]:
    """Adjudicate one provider search through policy.

    Side-effect free except ledger writes: usage recording stays with the
    caller (compliance.record_search) so budgets are never double-spent.

    Returns {"allowed": bool, "reason": str, "tier": str,
              "governance": "adjudicated"|"compliance-only",
              "verdict": {...}}.
    """
    tier = compliance.board_tier(board)
    allowed, reason = compliance.check_search_allowed(board)
    action = f"risk:search:{board}"
    if not allowed:
        verdict = record_risk_verdict(
            action, False, f"search blocked by compliance gate: {reason}"
        )
        return {
            "allowed": False,
            "reason": reason,
            "tier": tier,
            "governance": "compliance-only",
            "verdict": verdict,
        }

    summary = (
        f"Automated job search: the agent will query the '{board}' job "
        f"board (risk tier: {tier}) for '{query}'. "
    )
    if tier == "scraping":
        summary += (
            "This uses scraping/guest endpoints, which may violate the "
            "site's Terms of Service and can trigger rate limits, blocks, "
            "or CAPTCHAs. The user has acknowledged this risk; searches "
            "are daily-budgeted and circuit-broken."
        )
    elif tier == "user":
        summary += (
            "This reads only the user's saved bring-your-own listings "
            "(pasted text, user-supplied URLs, saved files) — no job "
            "board is queried."
        )
    else:
        summary += "This uses the board's official/public API."
    if context:
        summary += f" Context: {context}"

    vetoed, hits, warning = _veto_screen(summary)
    governance = "adjudicated" if not warning.startswith("governance unavailable") else "compliance-only"
    if vetoed:
        reason = (
            f"Blocked by governance veto: {hits or [warning]}. "
            "Only a human can clear a veto."
        )
        verdict = record_risk_verdict(action, False, reason)
        return {
            "allowed": False,
            "reason": reason,
            "tier": tier,
            "governance": governance,
            "verdict": verdict,
        }
    verdict = record_risk_verdict(
        action, True, f"search allowed on '{board}' (tier {tier}) for '{query}'"
    )
    out: dict[str, Any] = {
        "allowed": True,
        "reason": "",
        "tier": tier,
        "governance": governance,
        "verdict": verdict,
    }
    if warning:
        out["warning"] = warning
    return out


def adjudicate_apply(
    board: str,
    job_title: str = "",
    company: str = "",
    via: str = "browser",
) -> dict[str, Any]:
    """Adjudicate one application submission through policy.

    ``via`` is "browser" or "ats" (official API).
    Scraping-tier boards may only proceed fill-only: the automation must
    never perform the final submit click.
    """
    tier = compliance.board_tier(board)
    allowed, reason = compliance.check_apply_allowed()
    action = f"risk:apply:{board}:{via}"
    if not allowed:
        verdict = record_risk_verdict(
            action, False, f"application blocked by compliance gate: {reason}"
        )
        return {
            "allowed": False,
            "reason": reason,
            "tier": tier,
            "governance": "compliance-only",
            "verdict": verdict,
        }

    if tier == "scraping" and via == "browser":
        # Extra mitigation for the riskiest path: full browser submission
        # on a scraping-tier board is disallowed by policy; use the
        # fill-only browser flow (the human clicks submit) or an official
        # ATS endpoint instead.
        reason = (
            f"Policy blocks full browser submission on scraping-tier board "
            f"'{board}'. Use the fill-only flow (the human clicks submit) "
            f"or an official ATS application endpoint."
        )
        verdict = record_risk_verdict(action, False, reason)
        return {
            "allowed": False,
            "reason": reason,
            "tier": tier,
            "governance": "compliance-only",
            "verdict": verdict,
        }

    summary = (
        f"Automated job application: the agent will apply for "
        f"'{job_title}' at {company} via the '{board}' board "
        f"(risk tier: {tier}, method: {via}). "
    )
    if tier == "scraping":
        summary += (
            "Scraping-tier: automation fills the form but NEVER clicks the "
            "final submit; the human reviews and submits. One application "
            "per action, human-paced daily cap enforced."
        )
    elif tier == "user":
        summary += (
            "User-supplied listing: the posting came from the user's own "
            "paste, URL, or saved file — no board was queried. Fill-only: "
            "automation never clicks the final submit; the human reviews "
            "and submits. One application per action, human-paced daily "
            "cap enforced."
        )
    else:
        summary += "Official-tier: submission uses the board's official API."

    vetoed, hits, warning = _veto_screen(summary)
    governance = "adjudicated" if not warning.startswith("governance unavailable") else "compliance-only"
    if vetoed:
        reason = (
            f"Blocked by governance veto: {hits or [warning]}. "
            "Only a human can clear a veto."
        )
        verdict = record_risk_verdict(action, False, reason)
        return {
            "allowed": False,
            "reason": reason,
            "tier": tier,
            "governance": governance,
            "verdict": verdict,
        }
    verdict = record_risk_verdict(
        action, True,
        f"application allowed on '{board}' via {via} for '{job_title}' at {company}",
    )
    out: dict[str, Any] = {
        "allowed": True,
        "reason": "",
        "tier": tier,
        "governance": governance,
        "verdict": verdict,
    }
    if warning:
        out["warning"] = warning
    if tier == "scraping":
        out["fill_only"] = True
    return out


def adjudicate_automation(action: str, detail: str = "") -> dict[str, Any]:
    """Veto-screen any new automation (scheduler runs, bulk flows, etc.).

    Generic gate for actions without a dedicated adjudicator. Fail-closed
    on veto hits or veto-check errors; falls back gracefully when the
    framework is unavailable.
    """
    text = f"Automated job-search action '{action}'. {detail}".strip()
    vetoed, hits, warning = _veto_screen(text)
    governance = "adjudicated" if not warning.startswith("governance unavailable") else "unavailable"
    if vetoed:
        reason = f"Blocked by governance veto: {hits or [warning]}."
        verdict = record_risk_verdict(f"risk:automation:{action}", False, reason)
        return {"allowed": False, "reason": reason,
                "governance": governance, "verdict": verdict}
    verdict = record_risk_verdict(
        f"risk:automation:{action}", True, f"automation '{action}' allowed"
    )
    out: dict[str, Any] = {"allowed": True, "reason": "",
                           "governance": governance, "verdict": verdict}
    if warning:
        out["warning"] = warning
    return out


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def audit_risk(state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Review compliance state for risk anomalies.

    Returns {"findings": [{"severity": "info"|"warning"|"critical",
    "message": str}], "summary": {...}}. Pure read-only.
    """
    summary = compliance.status_summary(state)
    findings: list[dict[str, str]] = []

    if summary["mode"] == "standard" and not summary["risk_acknowledged"]:
        findings.append({
            "severity": "warning",
            "message": "Standard mode without recorded ToS-risk "
                       "acknowledgment — scraping-tier searches will be "
                       "gated until the user acknowledges.",
        })

    for board, info in summary["boards"].items():
        blocks = info.get("consecutive_blocks", 0)
        if blocks >= 3:
            findings.append({
                "severity": "critical",
                "message": f"Board '{board}' has been blocked "
                           f"{blocks} times consecutively — the site is "
                           f"actively pushing back. Consider strict mode or "
                           f"pausing this board.",
            })
        elif blocks >= 1:
            findings.append({
                "severity": "warning",
                "message": f"Board '{board}' hit a block recently "
                           f"(cooldown until {info.get('cooling_down_until')}).",
            })
        if info["tier"] == "scraping":
            used, budget = info["searches_today"], info["search_budget"]
            if used >= budget:
                findings.append({
                    "severity": "info",
                    "message": f"Scraping-tier board '{board}' exhausted its "
                               f"daily search budget ({used}/{budget}).",
                })

    if summary["applications_today"] >= summary["daily_apply_cap"]:
        findings.append({
            "severity": "info",
            "message": "Daily application cap reached — scheduler will "
                       "pause until midnight UTC.",
        })

    if not findings:
        findings.append({
            "severity": "info",
            "message": "No risk anomalies. All boards within budget; no "
                       "active cooldowns.",
        })
    return {"findings": findings, "summary": summary}
