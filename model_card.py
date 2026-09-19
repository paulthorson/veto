#!/usr/bin/env python3
"""Versioned model card for Veto (Initiative 02).

Every weight change — reason, date, weights, rollback/pin/reset — is an
append-only entry in ``model_card.jsonl``. The live card starts at v0
(static defaults, pinned off) and stays there until a human explicitly
records a change through :func:`record_change`, and only when
:func:`calibration.evidence_gate_status` reports the gate met.

* :func:`current` — latest card (v0 static when the log is empty).
* :func:`record_change` — new version; requires the evidence gate to pass
  for any personalized weights, plus an explicit human ``approved_by``.
* :func:`pin` — freeze the current weights (no further changes without
  unpin).
* :func:`rollback` — revert to a prior version (recorded as a new version
  with reason "rollback").
* :func:`reset` — return to v0 static defaults (recorded, never deletes
  history).

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.model_card")

BASE_DIR = Path(__file__).resolve().parent
MODEL_CARD_FILE = BASE_DIR / "model_card.jsonl"

STATIC_WEIGHTS = {
    "skills": 50.0,
    "seniority": 15.0,
    "salary": 15.0,
    "location": 15.0,
    "recency": 5.0,
}

_ACTIONS = ("record", "pin", "unpin", "rollback", "reset")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cards(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read the card log (empty list when missing/corrupt lines skipped)."""
    cards: list[dict[str, Any]] = []
    try:
        text = Path(path or MODEL_CARD_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping corrupt model-card line %d", lineno)
            continue
        if isinstance(obj, dict):
            cards.append(obj)
    return cards


def _append(card: dict[str, Any], path: str | Path | None) -> dict[str, Any]:
    with open(Path(path or MODEL_CARD_FILE), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(card, ensure_ascii=False) + "\n")
    return card


def _v0() -> dict[str, Any]:
    return {
        "version": 0,
        "action": "init",
        "at": _utcnow_iso(),
        "weights": dict(STATIC_WEIGHTS),
        "personalized": False,
        "pinned": True,
        "reason": "initial static defaults; adaptive weights pinned off "
                  "until the Initiative 02 evidence gate passes",
    }


def current(path: str | Path | None = None) -> dict[str, Any]:
    """Latest model card; v0 static when the log is empty."""
    cards = load_cards(path)
    return cards[-1] if cards else _v0()


def record_change(
    weights: dict[str, float],
    reason: str,
    approved_by: str,
    path: str | Path | None = None,
    events_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record a new weight version (explicit human approval required).

    Raises:
        ValueError: if ``approved_by`` is empty, the weights are not a
            complete component set, or personalized weights are requested
            while the evidence gate is not met.
    """
    if not str(approved_by or "").strip():
        raise ValueError("approved_by is required: a human must approve "
                         "every weight change")
    if set(weights or {}) != set(STATIC_WEIGHTS):
        raise ValueError(
            f"weights must cover exactly {sorted(STATIC_WEIGHTS)}"
        )
    personalized = any(
        abs(float(weights[k]) - STATIC_WEIGHTS[k]) > 1e-9
        for k in STATIC_WEIGHTS
    )
    gate: dict[str, Any] = {"gate_met": False}
    if personalized:
        import calibration

        gate = calibration.evidence_gate_status(events_path)
        if not gate.get("gate_met"):
            raise ValueError(
                "refused: personalized weights require the Initiative 02 "
                f"evidence gate to pass ({gate.get('reason')})"
            )
    prev = current(path)
    if prev.get("pinned"):
        raise ValueError(
            "refused: weights are pinned; unpin first (explicit human "
            "action) before recording a change"
        )
    card = {
        "version": int(prev.get("version", 0)) + 1,
        "action": "record",
        "at": _utcnow_iso(),
        "weights": {k: float(weights[k]) for k in STATIC_WEIGHTS},
        "personalized": personalized,
        "pinned": False,
        "reason": str(reason)[:500],
        "approved_by": str(approved_by),
        "gate_snapshot": {
            "gate_met": gate.get("gate_met"),
            "resolved_outcomes": gate.get("resolved_outcomes"),
            "qualified_replies": gate.get("qualified_replies"),
        },
        "previous_version": prev.get("version", 0),
    }
    return _append(card, path)


def pin(reason: str = "", path: str | Path | None = None) -> dict[str, Any]:
    """Freeze the current weights (explicit human action)."""
    prev = current(path)
    card = {
        "version": int(prev.get("version", 0)) + 1,
        "action": "pin",
        "at": _utcnow_iso(),
        "weights": dict(prev.get("weights", STATIC_WEIGHTS)),
        "personalized": bool(prev.get("personalized")),
        "pinned": True,
        "reason": str(reason)[:500] or "weights pinned by human",
        "previous_version": prev.get("version", 0),
    }
    return _append(card, path)


def unpin(reason: str = "", path: str | Path | None = None) -> dict[str, Any]:
    """Unfreeze (explicit human action; does not change weights)."""
    prev = current(path)
    card = {
        "version": int(prev.get("version", 0)) + 1,
        "action": "unpin",
        "at": _utcnow_iso(),
        "weights": dict(prev.get("weights", STATIC_WEIGHTS)),
        "personalized": bool(prev.get("personalized")),
        "pinned": False,
        "reason": str(reason)[:500] or "weights unpinned by human",
        "previous_version": prev.get("version", 0),
    }
    return _append(card, path)


def rollback(
    to_version: int, reason: str = "", path: str | Path | None = None
) -> dict[str, Any]:
    """Revert to a prior version's weights (recorded as a new version)."""
    cards = load_cards(path)
    # v0 is virtual (synthesized by current() when the log is empty) but
    # always a valid rollback target: it is the initial static state.
    candidates = ([_v0()] if not any(
        int(c.get("version", -1)) == 0 for c in cards) else []) + cards
    target = next(
        (c for c in candidates if int(c.get("version", -1)) == int(to_version)),
        None,
    )
    if target is None:
        raise ValueError(f"no model-card version {to_version}")
    prev = cards[-1] if cards else _v0()
    card = {
        "version": int(prev.get("version", 0)) + 1,
        "action": "rollback",
        "at": _utcnow_iso(),
        "weights": dict(target.get("weights", STATIC_WEIGHTS)),
        "personalized": bool(target.get("personalized")),
        "pinned": bool(target.get("pinned")),
        "reason": str(reason)[:500] or f"rollback to v{to_version}",
        "previous_version": prev.get("version", 0),
        "rollback_target": int(to_version),
    }
    return _append(card, path)


def reset(reason: str = "", path: str | Path | None = None) -> dict[str, Any]:
    """Return to v0 static defaults (append-only; history is kept)."""
    prev = current(path)
    card = {
        "version": int(prev.get("version", 0)) + 1,
        "action": "reset",
        "at": _utcnow_iso(),
        "weights": dict(STATIC_WEIGHTS),
        "personalized": False,
        "pinned": True,
        "reason": str(reason)[:500] or "reset to static defaults by human",
        "previous_version": prev.get("version", 0),
    }
    return _append(card, path)


def history(
    path: str | Path | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Card versions, newest first."""
    cards = sorted(load_cards(path),
                   key=lambda c: int(c.get("version", 0)), reverse=True)
    return cards[:limit] if limit is not None else cards


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register model-card tools on the MCP server."""

    @mcp.tool()
    def model_card_current() -> dict:
        """Current model card: weights version, static vs personalized,
        pinned state."""
        return current()

    @mcp.tool()
    def model_card_history(limit: int | None = None) -> dict:
        """Version history of the model card (newest first)."""
        return {"versions": history(limit=limit)}

    @mcp.tool()
    def model_card_change(weights: dict, reason: str,
                          approved_by: str) -> dict:
        """Record a weight change. REQUIRES explicit human approval
        (approved_by) and, for personalized weights, a passing evidence
        gate. Refuses otherwise."""
        return record_change(weights, reason, approved_by)

    @mcp.tool()
    def model_card_pin(reason: str = "") -> dict:
        """Pin (freeze) the current weights."""
        return pin(reason)

    @mcp.tool()
    def model_card_rollback(to_version: int, reason: str = "") -> dict:
        """Roll back to a prior version's weights (recorded as new)."""
        return rollback(to_version, reason)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_show(args: Any) -> int:
    print(json.dumps(current(), indent=2, ensure_ascii=False))
    return 0


def _cli_history(args: Any) -> int:
    print(json.dumps({"versions": history(limit=args.limit)},
                     indent=2, ensure_ascii=False))
    return 0


def _cli_change(args: Any) -> int:
    try:
        weights = json.loads(args.weights)
    except json.JSONDecodeError as exc:
        print(f"error: --weights must be JSON: {exc}")
        return 2
    try:
        card = record_change(weights, args.reason, args.approved_by)
    except ValueError as exc:
        print(f"refused: {exc}")
        return 1
    print(json.dumps(card, indent=2, ensure_ascii=False))
    return 0


def _cli_pin(args: Any) -> int:
    print(json.dumps(pin(args.reason), indent=2, ensure_ascii=False))
    return 0


def _cli_rollback(args: Any) -> int:
    try:
        card = rollback(args.to_version, args.reason)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(card, indent=2, ensure_ascii=False))
    return 0


def _cli_reset(args: Any) -> int:
    print(json.dumps(reset(args.reason), indent=2, ensure_ascii=False))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "model-card",
        help="Initiative 02: versioned fit-score model card (pin/rollback/reset).",
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="mc_cmd", required=True)

    ps = sub.add_parser("show", help="Current model card.")
    ps.set_defaults(func=_cli_show)

    ph = sub.add_parser("history", help="Version history.")
    ph.add_argument("--limit", type=int, default=None)
    ph.set_defaults(func=_cli_history)

    pc = sub.add_parser("change",
                        help="Record a weight change (needs --approved-by).")
    pc.add_argument("--weights", required=True,
                    help='JSON, e.g. \'{"skills":52,...}\'')
    pc.add_argument("--reason", required=True)
    pc.add_argument("--approved-by", required=True,
                    help="Human approver name/handle.")
    pc.set_defaults(func=_cli_change)

    pp = sub.add_parser("pin", help="Pin (freeze) current weights.")
    pp.add_argument("--reason", default="")
    pp.set_defaults(func=_cli_pin)

    prb = sub.add_parser("rollback", help="Roll back to a prior version.")
    prb.add_argument("to_version", type=int)
    prb.add_argument("--reason", default="")
    prb.set_defaults(func=_cli_rollback)

    prs = sub.add_parser("reset", help="Reset to static defaults.")
    prs.add_argument("--reason", default="")
    prs.set_defaults(func=_cli_reset)

    return {"model-card": lambda args: args.func(args)}
