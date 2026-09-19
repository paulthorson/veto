#!/usr/bin/env python3
"""Multi-profile support for veto-mcp.

The onboarding wizard writes ``profiles/profile.json`` (legacy single
profile). This module adds *named* profiles — e.g. one per target role —
without breaking the existing behavior:

Resolution order for ``load_profile(name="")``:

1. explicit ``name`` argument -> ``profiles/<name>.json``
2. ``PROFILE_NAME`` environment variable -> ``profiles/<env>.json``
3. ``profiles/default.json`` if it exists
4. legacy ``profiles/profile.json`` (whatever the wizard wrote)

``server._load_saved_profile`` / ``get_profile`` keep using the legacy
path, so existing flows are untouched; new code (and the CLI) can opt
into named profiles via this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.profiles")

BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "profiles"
LEGACY_PROFILE = PROFILES_DIR / "profile.json"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _sanitize_name(name: str) -> str:
    """Validate a profile name (no path traversal)."""
    name = str(name).strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name {name!r}: use letters, digits, "
            "dots, dashes, underscores"
        )
    return name


def resolve_profile_path(name: str = "", profiles_dir: Path = PROFILES_DIR) -> Path:
    """Resolve which profile file to use, per the order documented above."""
    profiles_dir = Path(profiles_dir)
    if name:
        return profiles_dir / f"{_sanitize_name(name)}.json"
    env_name = os.environ.get("PROFILE_NAME", "").strip()
    if env_name:
        return profiles_dir / f"{_sanitize_name(env_name)}.json"
    default = profiles_dir / "default.json"
    if default.exists():
        return default
    return profiles_dir / "profile.json"  # legacy fallback (may not exist)


def load_profile(name: str = "", profiles_dir: Path = PROFILES_DIR) -> dict[str, Any]:
    """Load a profile; returns {} when no file exists or it is invalid."""
    path = resolve_profile_path(name, profiles_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("No profile at %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def save_profile(
    profile: dict[str, Any], name: str = "", profiles_dir: Path = PROFILES_DIR
) -> Path:
    """Write a profile. ``name=""`` writes the legacy ``profile.json``.

    Note: ``profiles/*.json`` is gitignored — profiles never get committed.
    """
    path = resolve_profile_path(name, profiles_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    log.info("Saved profile to %s", path)
    return path


def list_profiles(profiles_dir: Path = PROFILES_DIR) -> list[str]:
    """Names of all saved profiles (stem of each ``*.json`` file)."""
    profiles_dir = Path(profiles_dir)
    if not profiles_dir.is_dir():
        return []
    return sorted(p.stem for p in profiles_dir.glob("*.json"))
