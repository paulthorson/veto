#!/usr/bin/env python3
"""Earned-success feedback — Epic 6 of Initiative 05.

Delight is EARNED, never decorative. A success moment may surface only
when ALL of these hold:

1. Initiative 00's context gate is open (``context["gate_open"]``).
2. The success was user-initiated (``context["user_initiated"]``) — the
   user approved a packet, finished a review, earned the moment.
3. The current state is a genuine success — never rejection, veto,
   error, blocked, or empty.

Every moment is dismissible (dismissal is persisted; a dismissed
moment never re-surfaces) and fully inert under reduced motion (the
payload carries no animation directives at all when
``context["reduced_motion"]`` is true — there is nothing to honor or
ignore, motion simply isn't part of the payload).

Initiative 00 contract adapter: until Initiative 00 lands its live
contract, the gate is supplied by the caller as ``gate_open``. See
DECISIONS.md D10 and integration_notes.md.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.i05.feedback")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = BASE_DIR / "feedback_state"

#: States in which delight is prohibited, full stop.
PROHIBITED_STATES = ("rejection", "veto", "error", "blocked", "empty")

_MESSAGES = {
    "packet-approved": (
        "Packet approved. Every claim in it traces to evidence you "
        "approved — that's the hard part, done right."
    ),
    "version-approved": (
        "Version approved and pinned. You can restore this exact resume "
        "any time."
    ),
    "review-complete": (
        "All changes reviewed. Nothing in this packet is unexamined."
    ),
    "evidence-approved": (
        "Evidence approved. Your library is stronger for the next role."
    ),
}


def _state_dir(state_dir: Path | None, create: bool) -> Path:
    d = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _dismissed_path(state_dir: Path) -> Path:
    return state_dir / "dismissed.json"


def _load_dismissed(state_dir: Path) -> set[str]:
    path = _dismissed_path(state_dir)
    if not path.is_file():
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("ignoring unreadable dismissed store: %s", exc)
        return set()
    return set(data) if isinstance(data, list) else set()


def _save_dismissed(state_dir: Path, dismissed: set[str]) -> None:
    path = _dismissed_path(state_dir)
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(dismissed), fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def gate_allows(context: dict[str, Any] | None) -> bool:
    """True only when Initiative 00's context gate is open for delight."""
    return bool((context or {}).get("gate_open"))


def success_moment(
    kind: str,
    context: dict[str, Any] | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a success moment, or decline it.

    Returns ``{"allowed", "message", "dismiss_key", "animation"}``.
    ``allowed`` is False (and ``message`` None) in every prohibited
    state, when the gate is closed, when the success wasn't
    user-initiated, or when the moment was already dismissed.
    ``animation`` is always ``None`` under reduced motion — and, by
    design, this module never emits animation directives otherwise
    either; delight here is words, not motion.
    """
    context = context or {}
    # Fail closed: an unknown state is never treated as a success.
    state = str(context.get("state") or "unknown")
    dismiss_key = f"i05:{kind}"

    allowed = (
        kind in _MESSAGES
        and state not in PROHIBITED_STATES
        and state == "success"
        and gate_allows(context)
        and bool(context.get("user_initiated"))
    )
    if allowed:
        d = _state_dir(state_dir, create=False)
        if dismiss_key in _load_dismissed(d):
            allowed = False

    return {
        "kind": kind,
        "allowed": allowed,
        "message": _MESSAGES[kind] if allowed else None,
        "dismiss_key": dismiss_key,
        "dismissed": False,
        # Motion contract: payloads carry no animation, ever. Under
        # reduced motion this is a hard guarantee, not a preference.
        "animation": None,
        "reduced_motion": bool(context.get("reduced_motion")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def dismiss(
    dismiss_key: str, state_dir: Path | None = None
) -> bool:
    """Persist a dismissal. A dismissed moment never re-surfaces."""
    d = _state_dir(state_dir, create=True)
    dismissed = _load_dismissed(d)
    if dismiss_key in dismissed:
        return False
    dismissed.add(dismiss_key)
    _save_dismissed(d, dismissed)
    return True


def is_dismissed(
    dismiss_key: str, state_dir: Path | None = None
) -> bool:
    """Check whether a moment was dismissed."""
    return dismiss_key in _load_dismissed(_state_dir(state_dir, create=False))


def state_matrix(
    kinds: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The full delight gate matrix: every kind × every relevant state.

    Used by tests and by reviewers to verify the gate. ``allowed`` is
    True only for the single cell: gate open + user-initiated +
    state == success + not dismissed.
    """
    kinds = kinds or sorted(_MESSAGES)
    rows: list[dict[str, Any]] = []
    for kind in kinds:
        for state in list(PROHIBITED_STATES) + ["success"]:
            for gate_open in (False, True):
                for user_initiated in (False, True):
                    moment = success_moment(
                        kind,
                        {"state": state, "gate_open": gate_open,
                         "user_initiated": user_initiated},
                    )
                    rows.append({
                        "kind": kind,
                        "state": state,
                        "gate_open": gate_open,
                        "user_initiated": user_initiated,
                        "allowed": moment["allowed"],
                    })
    return rows
