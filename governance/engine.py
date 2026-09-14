#!/usr/bin/env python3
"""Limen engine bridge for job-apply-mcp.

Limen is the reference engine for agentic-governance: it spawns workers,
isolates worktrees, manages jobs, and merges — while the framework
supplies the harnesses (constitution, adversarial review, vetoes). The
framework's setup wizard records the engine choice (``limen``,
``claude-code``, ``cursor``, ``paperclip``, ``custom``) per governed repo
in ``config/wizard-state.json``.

This module:

  - detects the engine setup: is the ``limen`` CLI installed? What engine
    did the framework wizard record?
  - runs automation as *governed jobs* following the engine's lifecycle::

        spawn   -> risk adjudication (veto-screen the job before it starts)
        work    -> the actual callable
        review  -> adversarial review of what was done (fail-open/advisory)
        merge   -> verdict recorded to the ledger; the human remains the
                   merge authority (our confirm gates are the merge decision)

  When the limen CLI itself is installed, jobs are tagged with engine
  metadata so a limen coordinator can discover them; execution stays
  in-process so nothing here depends on the limen CLI being present.

Mapping to the framework docs (docs/engines/limen.md):

  CEO harness (routes work, escalates)  -> run_governed_job (this module)
  Worker (artifact format, stop conds)  -> the work_fn passed in
  Adversarial review                   -> framework run_review (review step)
  Acceptance record + veto gate        -> confirm gates + verdict ledger
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from governance import adapter
from governance import risk_policy

log = logging.getLogger("job-apply-mcp.engine")

WIZARD_STATE_FILE = "wizard-state.json"

#: Default limen checkout location (LIMEN_DIR env var overrides).
LIMEN_DIR = Path(
    os.environ.get("LIMEN_DIR", Path.home() / "limen")
).expanduser()


def limen_version(directory: Path | None = None) -> str | None:
    """Short git revision of the limen checkout, if present."""
    directory = directory or LIMEN_DIR
    if not (directory / ".git").is_dir():
        return None
    try:
        proc = __import__("subprocess").run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(directory), capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_limen_installed() -> bool:
    """True when the limen CLI is on PATH."""
    return shutil.which("limen") is not None


def wizard_engine(framework_root: Path | None = None) -> str | None:
    """Engine recorded by the framework setup wizard, if any."""
    root = framework_root or adapter.find_governance_root()
    if root is None:
        return None
    state_path = Path(root) / "config" / WIZARD_STATE_FILE
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    engine = (data.get("answers") or {}).get("engine") if isinstance(data, dict) else None
    return str(engine) if engine else None


def detect_engine() -> dict[str, Any]:
    """Describe the engine setup governing this project."""
    root = adapter.find_governance_root()
    recorded = wizard_engine(root)
    installed = is_limen_installed()
    if recorded == "limen" and installed:
        active = "limen"
    elif recorded:
        active = f"byoe:{recorded}"
    elif installed:
        active = "limen (installed, not selected by wizard)"
    else:
        active = "none (framework harnesses apply directly)"
    return {
        "limen_installed": installed,
        "limen_dir": str(LIMEN_DIR),
        "limen_version": limen_version(),
        "framework_root": str(root) if root else None,
        "framework_available": adapter.governance_enabled(),
        "wizard_engine": recorded,
        "active_engine": active,
        "lifecycle": ["spawn", "work", "review", "merge"],
        "note": (
            "Automation runs as governed jobs (spawn/review/merge). "
            "Install limen (github.com/overment/limen) and select it in "
            "the framework wizard to orchestrate jobs through the limen "
            "CLI; the governance harnesses apply either way."
            if not installed else
            "Limen CLI detected; jobs carry engine metadata for coordinator "
            "discovery."
        ),
    }


# ---------------------------------------------------------------------------
# Governed jobs: spawn / work / review / merge
# ---------------------------------------------------------------------------


def run_governed_job(
    action: str,
    work_fn: Callable[[], Any],
    detail: str = "",
) -> dict[str, Any]:
    """Run ``work_fn`` as a governed job. Never raises.

    Lifecycle:
      spawn:  risk_policy.adjudicate_automation — veto-screens the job.
              A veto (or failed veto check) stops the job before any work.
      work:   work_fn() executes; exceptions are captured, not raised.
      review: framework adversarial review of the outcome (advisory;
              failures warn, never block).
      merge:  verdict written to the ledger. The human merge decision is
              represented by the caller's own confirm gates — this runner
              never auto-approves anything; it only records.

    Returns a report dict with keys: action, engine, spawn, result,
    review, verdict.
    """
    engine = detect_engine()
    report: dict[str, Any] = {
        "action": action,
        "engine": engine["active_engine"],
        "spawn": None,
        "result": None,
        "review": None,
        "verdict": {"recorded": False},
    }

    # -- spawn: adjudicate before any work happens -------------------------
    spawn = risk_policy.adjudicate_automation(action, detail)
    report["spawn"] = {
        k: v for k, v in spawn.items() if k != "verdict"
    }
    if not spawn["allowed"]:
        report["verdict"] = spawn.get("verdict", {"recorded": False})
        return report

    # -- work ---------------------------------------------------------------
    try:
        report["result"] = {"ok": True, "value": work_fn()}
    except Exception as exc:  # noqa: BLE001 - captured into the report
        log.warning("governed job %r failed: %s", action, exc)
        report["result"] = {"ok": False, "error": str(exc)}

    # -- review: adversarial, advisory --------------------------------------
    try:
        mod = adapter.load_framework()
        outcome = (
            "succeeded" if report["result"]["ok"]
            else f"failed: {report['result'].get('error')}"
        )
        review = mod.run_review(
            domain=adapter.GOVERNANCE_DOMAIN,
            work=f"Automated job '{action}' {outcome}. Detail: {detail}",
            context="governed job review (job-apply-mcp limen bridge)",
            source="job-apply-mcp",
        )
        report["review"] = review if isinstance(review, dict) else {"raw": review}
    except Exception as exc:  # fail open: review is advisory
        report["review"] = {"warning": f"adversarial review failed: {exc}"}

    # -- merge: record the verdict -------------------------------------------
    verdict_ok = bool(report["result"]["ok"])
    report["verdict"] = risk_policy.record_risk_verdict(
        f"job:{action}",
        verdict_ok,
        f"governed job '{action}' {('completed' if verdict_ok else 'failed')}",
    )
    return report


def register_tools(mcp: Any) -> None:
    """Register engine tools on an MCP server instance."""

    @mcp.tool()
    def governance_engine_status() -> dict:
        """Report the engine setup: limen CLI presence, framework wizard's
        recorded engine, and the governed-job lifecycle."""
        return detect_engine()
