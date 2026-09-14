#!/usr/bin/env python3
"""Reliability scorecard — Initiative 09, epic 6.

Consumes the provider-health contract (``providers/health_contract.py``,
defined by i09 pending Initiative 03's publication) and renders one row
per provider: freshness, error state, last success, and capability label,
plus budget, cooldown, CAPTCHA state, and recovery streak.

Local-first: the scorecard reads the local health store and emits text or
JSON. It performs no network calls and sends no data anywhere.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from providers import health_contract
from providers import _contract

#: Default capability labels when no health snapshot has been recorded yet.
#: Canonical mapping lives in health_contract.CAPABILITY_LABELS; kept here
#: as an alias for backwards compatibility.
DEFAULT_CAPABILITY_LABELS: dict[str, str] = health_contract.CAPABILITY_LABELS


def _age_str(ts: float) -> str:
    if not ts:
        return "never"
    delta = max(0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _iso(ts: float) -> str:
    if not ts:
        return "—"
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def scoreboard(
    store: health_contract.HealthStore | None = None,
) -> list[dict[str, Any]]:
    """Build one scorecard row per known provider.

    Providers with no recorded snapshot get an honest "no data yet" row
    rather than invented health — an unknown is shown as unknown, never as
    healthy. The tri-state snapshot status is rendered verbatim:
    ``"ok"`` / ``"error"`` / ``"unknown"`` — the "unknown" state is an
    explicit row status, never collapsed into "error".
    """
    store = store or health_contract.default_store()
    known = sorted(
        set(_contract.PROVIDER_MANIFESTS) | set(store.providers())
    )
    rows: list[dict[str, Any]] = []
    for name in known:
        snap = store.latest(name)
        manifest = _contract.manifest_for(name)
        terms, _, _ = _contract.terms_for(name)
        daily, _cooldown = _contract.budget_for(name)
        if snap is None:
            rows.append(
                {
                    "provider": name,
                    "status": "no data",
                    "freshness": "no data",
                    "error_state": "—",
                    "last_success": "never",
                    "capability": DEFAULT_CAPABILITY_LABELS.get(name, "unknown"),
                    "budget": f"0/{daily}",
                    "cooldown": "—",
                    "captcha": "—",
                    "recovery_streak": 0,
                    "terms": terms.value,
                    "manifest_complete": manifest.validate() == [] if manifest else False,
                }
            )
            continue
        # Render the tri-state verbatim: "ok" | "error" | "unknown".
        # Anything unexpected is an unknown, never green.
        status = snap.status if snap.status in ("ok", "error", "unknown") else "unknown"
        rows.append(
            {
                "provider": name,
                "status": status,
                "freshness": "fresh" if snap.is_fresh else _age_str(snap.fetched_at),
                "error_state": snap.error_state or "—",
                "last_success": _iso(snap.last_success_ts),
                "capability": snap.capability_label
                or DEFAULT_CAPABILITY_LABELS.get(name, "unknown"),
                # Unknown rows carry no observed outcome: render "—", not
                # an invented "0/N" budget.
                "budget": (
                    "—"
                    if status == "unknown"
                    else f"{snap.budget_used}/{snap.budget_total or daily}"
                ),
                "cooldown": "cooling down"
                if snap.is_cooling_down
                else "—",
                "captcha": snap.captcha_state,
                "recovery_streak": snap.recovery_streak,
                "terms": terms.value,
                "manifest_complete": manifest.validate() == [] if manifest else False,
            }
        )
    return rows


def render_terminal(rows: list[dict[str, Any]] | None = None) -> str:
    """Render the scorecard as a plain-text table (terminal dashboard)."""
    rows = rows if rows is not None else scoreboard()
    headers = ["provider", "status", "freshness", "error", "last success",
               "capability", "budget", "cooldown", "captcha", "streak"]
    keys = ["provider", "status", "freshness", "error_state", "last_success",
            "capability", "budget", "cooldown", "captcha", "recovery_streak"]
    widths = [len(h) for h in headers]
    str_rows = [[str(r[k]) for k in keys] for r in rows]
    for sr in str_rows:
        for i, cell in enumerate(sr):
            widths[i] = max(widths[i], len(cell))
    lines = [
        "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(sr)) for sr in str_rows]
    lines.append("")
    lines.append(
        "Health is local-first: this board reads the on-device health store "
        "only. Rows marked 'no data' mean no fetch has been recorded yet; "
        "rows marked 'unknown' had no recognizable status. Both are "
        "unknowns, not healthy."
    )
    return "\n".join(lines)


def to_json(rows: list[dict[str, Any]] | None = None) -> str:
    """JSON form for the local web UI / phone surface."""
    return json.dumps(rows if rows is not None else scoreboard(), indent=2)
