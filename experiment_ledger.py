#!/usr/bin/env python3
"""Experiment ledger for Veto (Initiative 01).

Records the ranking / tailoring version behind every application so
later analysis can attribute outcomes to the exact configuration that
produced them:

* ``ranking_version`` — which fit-score weights ranked the job
  (e.g. ``static-v0`` today; a model-card version later);
* ``tailoring_version`` — which resume/tailoring pipeline variant was
  used (the "resume variant" funnel dimension);
* ``fit_score`` / ``fit_components`` — the score and its component
  breakdown at decision time, feeding the Initiative 02 calibration
  harness's linked evidence.

Append-only JSONL (``experiment_ledger.jsonl``); :func:`assignment`
returns the latest record per application. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.experiment_ledger")

BASE_DIR = Path(__file__).resolve().parent
LEDGER_FILE = BASE_DIR / "experiment_ledger.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    application_id: str,
    *,
    ranking_version: str,
    tailoring_version: str | None = None,
    fit_score: float | None = None,
    fit_components: dict[str, float] | None = None,
    note: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Append an experiment assignment for an application.

    Raises ValueError when ``application_id`` or ``ranking_version`` is
    empty — an unattributed application is a hole in every later
    analysis, so the ledger refuses to create one.
    """
    application_id = str(application_id or "").strip()
    ranking_version = str(ranking_version or "").strip()
    if not application_id:
        raise ValueError("application_id is required")
    if not ranking_version:
        raise ValueError("ranking_version is required")
    entry = {
        "application_id": application_id,
        "ranking_version": ranking_version,
        "tailoring_version": tailoring_version,
        "fit_score": fit_score,
        "fit_components": dict(fit_components) if fit_components else None,
        "note": str(note)[:280],
        "recorded_at": _utcnow_iso(),
    }
    with open(Path(path or LEDGER_FILE), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def _read_all(path: str | Path | None) -> list[dict[str, Any]]:
    try:
        text = Path(path or LEDGER_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.warning("Skipping corrupt ledger line %d", lineno)
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def assignment(
    application_id: str, path: str | Path | None = None
) -> dict[str, Any] | None:
    """Latest ledger record for an application (None when absent)."""
    matches = [
        e for e in _read_all(path)
        if str(e.get("application_id")) == str(application_id)
    ]
    return matches[-1] if matches else None


def all_assignments(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Every ledger record, oldest first."""
    return _read_all(path)


def linked_evidence(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Records with component scores, shaped for
    ``calibration.weight_report(scored=...)``."""
    return [
        {
            "application_id": e["application_id"],
            "components": e["fit_components"],
        }
        for e in _read_all(path)
        if e.get("fit_components")
    ]


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register experiment-ledger tools on the MCP server."""

    @mcp.tool()
    def experiment_record(application_id: str, ranking_version: str,
                          tailoring_version: str | None = None,
                          fit_score: float | None = None,
                          fit_components: dict | None = None,
                          note: str = "") -> dict:
        """Record the ranking/tailoring version behind an application."""
        return record(application_id, ranking_version=ranking_version,
                      tailoring_version=tailoring_version,
                      fit_score=fit_score, fit_components=fit_components,
                      note=note)

    @mcp.tool()
    def experiment_assignment(application_id: str) -> dict:
        """Latest experiment assignment for an application."""
        return {"assignment": assignment(application_id)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_record(args: Any) -> int:
    try:
        components = (json.loads(args.fit_components)
                      if args.fit_components else None)
    except json.JSONDecodeError as exc:
        print(f"error: --fit-components must be JSON: {exc}")
        return 2
    try:
        entry = record(
            args.application_id,
            ranking_version=args.ranking_version,
            tailoring_version=args.tailoring_version,
            fit_score=args.fit_score,
            fit_components=components,
            note=args.note or "",
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


def _cli_get(args: Any) -> int:
    print(json.dumps({"assignment": assignment(args.application_id)},
                     indent=2, ensure_ascii=False))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "experiment",
        help="Initiative 01: ranking/tailoring version ledger.",
    )
    sub = p.add_subparsers(dest="exp_cmd", required=True)

    pr = sub.add_parser("record", help="Record an experiment assignment.")
    pr.add_argument("application_id")
    pr.add_argument("--ranking-version", required=True)
    pr.add_argument("--tailoring-version", default=None)
    pr.add_argument("--fit-score", type=float, default=None)
    pr.add_argument("--fit-components", default=None,
                    help='JSON, e.g. \'{"skills":40,...}\'')
    pr.add_argument("--note", default="")
    pr.set_defaults(func=_cli_record)

    pg = sub.add_parser("get", help="Latest assignment for an application.")
    pg.add_argument("application_id")
    pg.set_defaults(func=_cli_get)

    return {"experiment": lambda args: args.func(args)}
