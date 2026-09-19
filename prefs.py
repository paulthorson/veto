#!/usr/bin/env python3
"""Company preferences: blocklist, preferred companies, blocked keywords.

``preferences.json`` (gitignored, lives next to this project) holds::

    {
        "blocked_companies": ["Acme Corp"],
        "preferred_companies": ["Initech"],
        "blocked_keywords": ["crypto casino", "unpaid internship"]
    }

Matching is case-insensitive substring matching on normalized text.
``preferences.example.json`` (committed) documents the shape.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.prefs")

BASE_DIR = Path(__file__).resolve().parent
PREFS_FILE = BASE_DIR / "preferences.json"

DEFAULT_PREFS: dict[str, list[str]] = {
    "blocked_companies": [],
    "preferred_companies": [],
    "blocked_keywords": [],
}

#: Scalar (non-list) preferences with their defaults. These are persisted
#: alongside DEFAULT_PREFS; the onboarding wizard writes them.
SCALAR_DEFAULTS: dict[str, Any] = {
    "grill_on_apply": True,
    "grill_channel": "chat",  # chat | whatsapp | gmail | off
}

GRILL_CHANNELS = {"chat", "whatsapp", "gmail", "off"}


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def load_preferences() -> dict[str, Any]:
    """Read preferences.json; return defaults when missing/corrupt."""
    prefs: dict[str, Any] = {k: list(v) for k, v in DEFAULT_PREFS.items()}
    prefs.update(SCALAR_DEFAULTS)
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return prefs
    except json.JSONDecodeError as exc:
        log.warning("Could not parse %s: %s", PREFS_FILE, exc)
        return prefs
    if isinstance(data, dict):
        for key in DEFAULT_PREFS:
            val = data.get(key)
            if isinstance(val, list):
                prefs[key] = [str(v) for v in val]
        prefs["grill_on_apply"] = bool(data.get("grill_on_apply", True))
        channel = str(data.get("grill_channel", "chat")).strip().lower()
        prefs["grill_channel"] = (
            channel if channel in GRILL_CHANNELS else "chat"
        )
    return prefs


def save_preferences(prefs: dict[str, Any]) -> dict[str, Any]:
    """Persist preferences: known list keys plus scalar grill settings."""
    clean: dict[str, Any] = {
        k: [str(v) for v in prefs.get(k, [])] for k in DEFAULT_PREFS
    }
    clean["grill_on_apply"] = bool(prefs.get("grill_on_apply", True))
    channel = str(prefs.get("grill_channel", "chat")).strip().lower()
    clean["grill_channel"] = channel if channel in GRILL_CHANNELS else "chat"
    PREFS_FILE.write_text(
        json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return clean


def apply_preferences(
    jobs: list[dict[str, Any]], prefs: dict[str, list[str]] | None = None
) -> list[dict[str, Any]]:
    """Filter blocked jobs; boost preferred companies to the top.

    Blocked companies/keywords are removed. Preferred-company matches are
    flagged with ``"preferred": True`` and moved to the front, preserving
    their relative order. Returns a new list; input dicts are annotated
    in place with the ``preferred`` flag.
    """
    prefs = prefs or load_preferences()
    blocked_companies = [_norm(c) for c in prefs.get("blocked_companies", [])]
    preferred_companies = [_norm(c) for c in prefs.get("preferred_companies", [])]
    blocked_keywords = [_norm(k) for k in prefs.get("blocked_keywords", [])]

    kept: list[dict[str, Any]] = []
    preferred: list[dict[str, Any]] = []
    for job in jobs:
        company = _norm(job.get("company", ""))
        haystack = _norm(
            " ".join(
                str(job.get(k, "") or "")
                for k in ("title", "company", "snippet", "description")
            )
        )
        if any(bc and bc in company for bc in blocked_companies):
            continue
        if any(bk and bk in haystack for bk in blocked_keywords):
            continue
        if any(pc and pc in company for pc in preferred_companies):
            job["preferred"] = True
            preferred.append(job)
        else:
            job.pop("preferred", None)
            kept.append(job)
    return preferred + kept
