#!/usr/bin/env python3
"""ToS-risk mitigation for the Veto MCP server.

Boards are classified into risk tiers:

  official  - public or official APIs (Greenhouse, Lever, Ashby, Adzuna).
              ToS-friendly; always preferred.
  scraping   - HTML or guest-endpoint scraping (Glassdoor). Works until it
              doesn't: it can violate the site's Terms of Service and
              trigger HTTP 403/429, CAPTCHAs, IP rate-limiting, or account
              restrictions.

Compliance modes (persisted in ``compliance.json``, gitignored)::

  strict    Only "official"-tier boards are used. No scraping at all.
            This is the only mode that is ToS-clean by construction.
  standard  All boards may be used, but scraping-tier boards are
            daily-budgeted, circuit-broken when the site pushes back,
            and every result is tagged with its risk tier. Requires the
            user to explicitly acknowledge the ToS risk first.

Enforcement points (wired by server.py, cli.py, apply_queue.py):

  search  ``check_search_allowed(board)`` gates every provider call;
          ``record_search(board)`` spends daily budget; ``record_block``
          trips the circuit breaker with exponential cooldown, and
          ``record_search_success(board)`` counts consecutive genuine
          successful fetches toward recovery: a full window of them
          clears the block via ``record_block_cleared``.
  apply   ``check_apply_allowed()`` enforces a small daily cap so
          applications always move at a human pace; apply flows must
          surface ``apply_disclosure(board)`` in the preview, and for
          scraping-tier boards the automation must never perform the
          final submit click itself — the human does it (fill-only).

Nothing here stores credentials, and nothing here makes scraping
ToS-compliant. It makes the risk explicit, bounded, and user-consented.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
COMPLIANCE_FILE = BASE_DIR / "compliance.json"

# ---------------------------------------------------------------------------
# Tiers, modes, budgets
# ---------------------------------------------------------------------------

RISK_TIER: dict[str, str] = {
    "greenhouse": "official",
    "lever": "official",
    "ashby": "official",
    "adzuna": "official",
    "glassdoor": "scraping",
    # Bring-your-own-listing (legal-hardening commit 7, spec section 5.2):
    # the user pasted the text, supplied the URL, or saved the file
    # themselves. Nothing is scraped and no board is queried, so this is
    # neither "official" nor "scraping" — it gets its own tier with its
    # own notices.
    "user": "user",
}

OFFICIAL_BOARDS = {b for b, t in RISK_TIER.items() if t == "official"}
SCRAPING_BOARDS = {b for b, t in RISK_TIER.items() if t == "scraping"}

MODES = ("strict", "standard")

#: Max searches per board per day. Scraping-tier budgets are deliberately
#: small: politeness delays reduce load but do not make scraping compliant.
#: The "user" budget is a formality: searching saved listings is local
#: only and puts no load on any third party.
#:
#: LEGAL POSTURE (spec §6.5, legal-hardening commit 8): this is a hard
#: code constant. It is read from this module directly by
#: check_search_allowed(); no config file, environment variable, or CLI
#: flag can raise it. Raising it requires editing this source.
DEFAULT_SEARCH_BUDGET = {"official": 1000, "scraping": 40, "user": 1000}

#: Max recorded/submitted applications per day across all boards.
#:
#: LEGAL POSTURE (spec §6.5, legal-hardening commit 8): this is a hard
#: code constant. It is read from this module directly by
#: check_apply_allowed(); no config file, environment variable, or CLI
#: flag can raise it. Raising it requires editing this source.
DEFAULT_DAILY_APPLY_CAP = 5

#: Seconds to wait between automated applications (scheduler enforces).
APPLY_PACE_SECONDS = (120, 420)

#: Circuit-breaker cooldown after a block: 1h, doubling, capped at 24h.
BLOCK_COOLDOWN_BASE_SECONDS = 3600
BLOCK_COOLDOWN_MAX_SECONDS = 86400

#: Consecutive genuine successful fetches that clear a tripped circuit
#: breaker. After this many successes with no intervening block, the
#: block is cleared and the cooldown resets, so the next block starts
#: again at the 1h base instead of escalating toward the 24h cap.
RECOVERY_SUCCESS_WINDOW = 3

TIER_NOTICES = {
    "official": (
        "Sourced via the board's public/official API."
    ),
    "scraping": (
        "Sourced by scraping. This may violate the site's Terms of "
        "Service; the site can rate-limit (HTTP 429), block (HTTP 403), "
        "or show CAPTCHAs at any time, and logged-in automation risks "
        "account restrictions. Verify details on the live posting."
    ),
    "user": (
        "Supplied by you (pasted text, a URL you gave, or a file you "
        "saved). Nothing was scraped; no job board was queried."
    ),
}

APPLY_DISCLOSURES = {
    "official": (
        "This board offers an official application API. Submission is "
        "ToS-friendly."
    ),
    "scraping": (
        "Automated applying on this board carries ToS and anti-bot risk "
        "(CAPTCHAs, IP throttles, account limits or bans — especially on "
        "logged-in flows). Mitigations in force: no credential storage, "
        "explicit confirmation required, one application per action, "
        "human-paced daily cap, and fill-only mode — the final submit "
        "click is always yours."
    ),
    "user": (
        "This listing was supplied by you. Veto never submits: the final "
        "submit click is always yours."
    ),
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _default_state() -> dict:
    return {
        "mode": "standard",
        "risk_acknowledged": False,
        "acknowledged_at": None,
        "counters": {},   # date -> {"search": {board: n}, "applies": n}
        "blocks": {},     # board -> {"consecutive": n, "blocked_until": iso}
    }


def load_state(path: Path | None = None) -> dict:
    """Load compliance state; return defaults when missing/corrupt."""
    path = path or COMPLIANCE_FILE
    state = _default_state()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return state
    if isinstance(data, dict):
        for key in state:
            if key in data:
                state[key] = data[key]
    if state.get("mode") not in MODES:
        state["mode"] = "standard"
    return state


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or COMPLIANCE_FILE
    Path(path).write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _counters_for(state: dict) -> dict:
    day = _today()
    per_day = state.setdefault("counters", {}).setdefault(day, {})
    per_day.setdefault("search", {})
    per_day.setdefault("applies", 0)
    # Opportunistic prune: keep only the last 14 days of counters.
    for old in [d for d in state["counters"] if d < day][-30:]:
        if old < (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d"):
            del state["counters"][old]
    return per_day


# ---------------------------------------------------------------------------
# Mode + consent
# ---------------------------------------------------------------------------


def get_mode(state: dict | None = None) -> str:
    return (state or load_state()).get("mode", "standard")


def set_mode(mode: str, state: dict | None = None,
             path: Path | None = None) -> dict:
    """Set the compliance mode; returns the updated state."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    state = state if state is not None else load_state(path)
    state["mode"] = mode
    save_state(state, path)
    return state


def is_risk_acknowledged(state: dict | None = None) -> bool:
    return bool((state or load_state()).get("risk_acknowledged"))


def acknowledge_risks(state: dict | None = None,
                      path: Path | None = None) -> dict:
    """Record the user's explicit acknowledgment of the scraping ToS risk."""
    state = state if state is not None else load_state(path)
    state["risk_acknowledged"] = True
    state["acknowledged_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state, path)
    return state


def board_tier(board: str) -> str:
    """Risk tier for a board; unknown boards are treated as scraping."""
    return RISK_TIER.get((board or "").lower(), "scraping")


# ---------------------------------------------------------------------------
# Search gating
# ---------------------------------------------------------------------------


def is_board_allowed(board: str, state: dict | None = None) -> tuple[bool, str]:
    """Mode gate: strict mode allows only official-tier boards."""
    state = state if state is not None else load_state()
    tier = board_tier(board)
    # Strict mode allows official APIs plus the bring-your-own-listing
    # board: "user" listings were supplied by the user (paste / URL /
    # saved file), so no third-party fetch is involved at all.
    if get_mode(state) == "strict" and tier not in ("official", "user"):
        return False, (
            f"Board '{board}' is scraping-tier and compliance mode is "
            f"'strict' (official APIs and user-supplied listings only). "
            f"Switch to 'standard' mode with explicit risk acknowledgment "
            f"to enable it."
        )
    return True, ""


def _block_info(state: dict, board: str) -> dict:
    return state.setdefault("blocks", {}).setdefault(
        board, {"consecutive": 0, "blocked_until": None}
    )


def check_search_allowed(board: str, state: dict | None = None,
                         path: Path | None = None) -> tuple[bool, str]:
    """Full search gate: mode + circuit breaker + daily budget."""
    state = state if state is not None else load_state(path)
    allowed, reason = is_board_allowed(board, state)
    if not allowed:
        return False, reason

    if get_mode(state) == "standard" and board_tier(board) == "scraping" \
            and not is_risk_acknowledged(state):
        return False, (
            "Scraping-tier boards require explicit ToS-risk acknowledgment. "
            "Run the wizard (or call acknowledge_risks) to confirm you "
            "understand the risk before searching scraping-tier boards."
        )

    info = _block_info(state, board)
    blocked_until = info.get("blocked_until")
    if blocked_until:
        try:
            resume_at = datetime.fromisoformat(blocked_until)
            if datetime.now(timezone.utc) < resume_at:
                return False, (
                    f"Board '{board}' is cooling down after the site pushed "
                    f"back (consecutive blocks: {info.get('consecutive', 0)}). "
                    f"Retries resume after {resume_at.isoformat()}."
                )
        except ValueError:
            pass

    per_day = _counters_for(state)
    used = per_day["search"].get(board, 0)
    budget = DEFAULT_SEARCH_BUDGET[board_tier(board)]
    if used >= budget:
        return False, (
            f"Daily search budget exhausted for '{board}' ({used}/{budget}). "
            f"Budgets reset at midnight UTC; scraping-tier budgets are "
            f"deliberately small."
        )
    return True, ""


def record_search(board: str, state: dict | None = None,
                  path: Path | None = None) -> dict:
    """Spend one unit of daily search budget for a board."""
    state = state if state is not None else load_state(path)
    per_day = _counters_for(state)
    per_day["search"][board] = per_day["search"].get(board, 0) + 1
    save_state(state, path)
    return state


def record_block(board: str, state: dict | None = None,
                 path: Path | None = None) -> int:
    """Record the site pushing back (403/429/CAPTCHA).

    Trips the circuit breaker with exponential cooldown. Any block
    resets the recovery success streak, so intermittent failures keep
    escalating. Returns the cooldown in seconds.
    """
    state = state if state is not None else load_state(path)
    info = _block_info(state, board)
    info["consecutive"] = int(info.get("consecutive", 0)) + 1
    info["successes"] = 0
    cooldown = min(
        BLOCK_COOLDOWN_BASE_SECONDS * (2 ** (info["consecutive"] - 1)),
        BLOCK_COOLDOWN_MAX_SECONDS,
    )
    info["blocked_until"] = (
        datetime.now(timezone.utc) + timedelta(seconds=cooldown)
    ).isoformat()
    save_state(state, path)
    return cooldown


def record_block_cleared(board: str, state: dict | None = None,
                         path: Path | None = None) -> dict:
    """Reset the circuit breaker after a successful request."""
    state = state if state is not None else load_state(path)
    info = _block_info(state, board)
    info["consecutive"] = 0
    info["blocked_until"] = None
    info["successes"] = 0
    save_state(state, path)
    return state


def record_search_success(board: str, state: dict | None = None,
                          path: Path | None = None) -> bool:
    """Record one genuine successful fetch for a board.

    Counts consecutive successful fetches toward circuit-breaker
    recovery. When ``RECOVERY_SUCCESS_WINDOW`` successes accumulate
    with no intervening block, the block is cleared (the cooldown
    resets to the base via ``record_block_cleared``) and True is
    returned.

    Any block (``record_block``) resets the streak, so intermittent
    failures still escalate instead of decaying. CAPTCHA detections
    record blocks, never successes, so recovery only ever follows
    genuine successful fetches. Boards with no block history are a
    no-op (returns False) so healthy boards cause no state churn.
    """
    state = state if state is not None else load_state(path)
    info = _block_info(state, board)
    if int(info.get("consecutive", 0)) <= 0:
        return False  # no block history; nothing to recover
    info["successes"] = int(info.get("successes", 0)) + 1
    if info["successes"] >= RECOVERY_SUCCESS_WINDOW:
        record_block_cleared(board, state, path)
        return True
    save_state(state, path)
    return False


# ---------------------------------------------------------------------------
# Apply gating
# ---------------------------------------------------------------------------


def check_apply_allowed(state: dict | None = None,
                        path: Path | None = None) -> tuple[bool, str]:
    """Enforce the human-paced daily application cap."""
    state = state if state is not None else load_state(path)
    per_day = _counters_for(state)
    used = per_day.get("applies", 0)
    if used >= DEFAULT_DAILY_APPLY_CAP:
        return False, (
            f"Daily application cap reached ({used}/{DEFAULT_DAILY_APPLY_CAP}). "
            f"Applications move at a human pace — a handful per day, not "
            f"bulk blasts. The cap resets at midnight UTC."
        )
    return True, ""


def record_application(state: dict | None = None,
                       path: Path | None = None) -> dict:
    state = state if state is not None else load_state(path)
    per_day = _counters_for(state)
    per_day["applies"] = per_day.get("applies", 0) + 1
    save_state(state, path)
    return state


# ---------------------------------------------------------------------------
# Disclosures
# ---------------------------------------------------------------------------


def risk_notice(board: str) -> str:
    """Short ToS notice for a board's risk tier."""
    return TIER_NOTICES[board_tier(board)]


def apply_disclosure(board: str) -> str:
    """Disclosure text that apply flows must show in their preview."""
    return APPLY_DISCLOSURES[board_tier(board)]


def result_risk_tag(board: str) -> dict:
    """Risk metadata stamped onto every search result."""
    tier = board_tier(board)
    return {"tos_risk_tier": tier, "tos_notice": TIER_NOTICES[tier]}


def status_summary(state: dict | None = None) -> dict:
    """Human-readable compliance snapshot for doctor/status tools."""
    state = state if state is not None else load_state()
    per_day = _counters_for(state)
    return {
        "mode": get_mode(state),
        "risk_acknowledged": is_risk_acknowledged(state),
        "acknowledged_at": state.get("acknowledged_at"),
        "boards": {
            board: {
                "tier": board_tier(board),
                "searches_today": per_day["search"].get(board, 0),
                "search_budget": DEFAULT_SEARCH_BUDGET[board_tier(board)],
                "consecutive_blocks": state.get("blocks", {}).get(board, {}).get(
                    "consecutive", 0
                ),
                "cooling_down_until": state.get("blocks", {}).get(board, {}).get(
                    "blocked_until"
                ),
                "recovery_successes": state.get("blocks", {}).get(board, {}).get(
                    "successes", 0
                ),
                "recovery_window": RECOVERY_SUCCESS_WINDOW,
            }
            for board in sorted(RISK_TIER)
        },
        "applications_today": per_day.get("applies", 0),
        "daily_apply_cap": DEFAULT_DAILY_APPLY_CAP,
    }
