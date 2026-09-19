#!/usr/bin/env python3
"""Provider health board for Veto (Initiative 03).

One local JSON store (``provider_health.json``) tracks, per board/provider:

* **budget** — daily fetch quota (``used_today`` / ``daily_limit``);
* **cooldown** — ``cooldown_until`` timestamp after failures/CAPTCHAs;
* **CAPTCHA state** — ``clear`` / ``challenged`` / ``blocked``
  (observed only — this module never attempts to bypass a CAPTCHA);
* **recovery streak** — cumulative count of calendar days on which at least
  one fetch succeeded following a failure episode (at most one per day, so
  volume can never inflate it);
* **last successful fetch** — ISO timestamp of the most recent ``ok``.

The module is side-effect free apart from its own store: recording is done
by explicit calls (the connector/watch layer calls :func:`record_fetch`;
see :mod:`watch`'s ``health`` hook). Alerting is left to the caller via
:func:`degraded_providers` + :mod:`notify` so health tracking can never
spam on its own.

Stdlib only. All functions take explicit paths so tests run against temp
dirs.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.provider_health")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_STORE = BASE_DIR / "provider_health.json"

#: CAPTCHA states. ``challenged`` means a CAPTCHA was encountered (fetch
#: paused for that provider); ``blocked`` means repeated challenges.
#: This module only *records* the state — bypass is never attempted.
CAPTCHA_STATES = ("clear", "challenged", "blocked")

#: Consecutive failures before a provider is reported as degraded.
DEGRADED_AFTER_FAILURES = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> date:
    return _utcnow().date()


def _blank_provider() -> dict[str, Any]:
    return {
        "daily_limit": None,  # None = no budget configured
        "used_today": 0,
        "budget_day": _today().isoformat(),
        "cooldown_until": None,
        "cooldown_reason": "",
        "captcha_state": "clear",
        "captcha_at": None,
        "last_successful_fetch": None,
        "consecutive_failures": 0,
        "recovery_streak_days": 0,
        "last_recovery_day": None,
        "total_fetches": 0,
        "total_failures": 0,
    }


def load_store(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Read the health store (empty dict if missing/corrupt)."""
    try:
        raw = json.loads(Path(path or DEFAULT_STORE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        log.debug("Could not read health store: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    store: dict[str, dict[str, Any]] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        merged = _blank_provider()
        merged.update(entry)
        if merged["captcha_state"] not in CAPTCHA_STATES:
            merged["captcha_state"] = "clear"
        store[str(name)] = merged
    return store


def save_store(
    store: dict[str, dict[str, Any]], path: str | Path | None = None
) -> None:
    Path(path or DEFAULT_STORE).write_text(
        json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _get(store: dict[str, dict[str, Any]], provider: str) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    if not provider:
        raise ValueError("provider name must not be empty")
    return store.setdefault(provider, _blank_provider())


def _roll_budget(entry: dict[str, Any]) -> None:
    """Reset the daily counter when the calendar day has rolled over."""
    if entry.get("budget_day") != _today().isoformat():
        entry["budget_day"] = _today().isoformat()
        entry["used_today"] = 0


def record_fetch(
    provider: str,
    ok: bool,
    *,
    note: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Record one fetch attempt against ``provider``.

    Success clears ``consecutive_failures`` and stamps
    ``last_successful_fetch``; if the provider was previously failing, the
    recovery streak advances by one day (at most once per calendar day).
    Failure increments ``consecutive_failures``. Budget usage counts every
    attempt.
    """
    store = load_store(path)
    entry = _get(store, provider)
    _roll_budget(entry)
    entry["total_fetches"] = int(entry.get("total_fetches") or 0) + 1
    entry["used_today"] = int(entry.get("used_today") or 0) + 1
    now_iso = _utcnow().isoformat()
    if ok:
        was_failing = int(entry.get("consecutive_failures") or 0) > 0
        entry["consecutive_failures"] = 0
        entry["last_successful_fetch"] = now_iso
        if was_failing:
            today_s = _today().isoformat()
            if entry.get("last_recovery_day") != today_s:
                entry["recovery_streak_days"] = (
                    int(entry.get("recovery_streak_days") or 0) + 1
                )
                entry["last_recovery_day"] = today_s
        if entry.get("captcha_state") != "blocked":
            # A clean fetch clears a transient challenge, not a block.
            if entry.get("captcha_state") == "challenged":
                entry["captcha_state"] = "clear"
                entry["captcha_at"] = None
    else:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        entry["total_failures"] = int(entry.get("total_failures") or 0) + 1
    if note:
        entry["last_note"] = str(note)[:280]
    save_store(store, path)
    return {"provider": provider, "ok": ok, "entry": entry}


def record_captcha(
    provider: str,
    state: str = "challenged",
    *,
    cooldown_seconds: int = 3600,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Record an observed CAPTCHA for ``provider``.

    Sets the CAPTCHA state and a cooldown. This is observation only: Veto
    never attempts to bypass a CAPTCHA; the provider simply rests until
    the cooldown expires or the user resolves it manually.
    """
    if state not in CAPTCHA_STATES:
        raise ValueError(f"captcha state must be one of {CAPTCHA_STATES}")
    store = load_store(path)
    entry = _get(store, provider)
    entry["captcha_state"] = state
    entry["captcha_at"] = _utcnow().isoformat()
    if state != "clear":
        entry["cooldown_until"] = (
            _utcnow() + timedelta(seconds=max(0, cooldown_seconds))
        ).isoformat()
        entry["cooldown_reason"] = f"captcha_{state}"
    else:
        entry["cooldown_until"] = None
        entry["cooldown_reason"] = ""
    save_store(store, path)
    return {"provider": provider, "captcha_state": state}


def set_budget(
    provider: str, daily_limit: int | None, path: str | Path | None = None
) -> dict[str, Any]:
    """Set (or clear, with ``None``) the daily fetch budget for a provider."""
    if daily_limit is not None and int(daily_limit) < 0:
        raise ValueError("daily_limit must be >= 0 or None")
    store = load_store(path)
    entry = _get(store, provider)
    entry["daily_limit"] = None if daily_limit is None else int(daily_limit)
    save_store(store, path)
    return {"provider": provider, "daily_limit": entry["daily_limit"]}


def cooldown(
    provider: str,
    seconds: int,
    reason: str = "",
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Put ``provider`` on cooldown for ``seconds`` (manual or policy)."""
    store = load_store(path)
    entry = _get(store, provider)
    entry["cooldown_until"] = (
        _utcnow() + timedelta(seconds=max(0, int(seconds)))
    ).isoformat()
    entry["cooldown_reason"] = str(reason)[:140]
    save_store(store, path)
    return {"provider": provider, "cooldown_until": entry["cooldown_until"]}


def _cooldown_remaining_s(entry: dict[str, Any]) -> float:
    raw = entry.get("cooldown_until")
    if not raw:
        return 0.0
    try:
        until = datetime.fromisoformat(str(raw))
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return max(0.0, (until - _utcnow()).total_seconds())
    except ValueError:
        return 0.0


def provider_status(
    provider: str, store: dict[str, dict[str, Any]] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """One provider's board row: ok / cooling / degraded / blocked."""
    store = store if store is not None else load_store(path)
    entry = _get(store, provider)
    _roll_budget(entry)
    cooldown_s = _cooldown_remaining_s(entry)
    failures = int(entry.get("consecutive_failures") or 0)
    captcha = entry.get("captcha_state") or "clear"
    limit = entry.get("daily_limit")
    used = int(entry.get("used_today") or 0)
    if captcha == "blocked":
        status = "blocked"
    elif cooldown_s > 0:
        status = "cooling"
    elif failures >= DEGRADED_AFTER_FAILURES:
        status = "degraded"
    else:
        status = "ok"
    return {
        "provider": provider,
        "status": status,
        "budget": {
            "daily_limit": limit,
            "used_today": used,
            "remaining": (limit - used) if limit is not None else None,
            "exhausted": limit is not None and used >= limit,
        },
        "cooldown_remaining_s": round(cooldown_s),
        "cooldown_reason": entry.get("cooldown_reason") or "",
        "captcha_state": captcha,
        "consecutive_failures": failures,
        "recovery_streak_days": int(entry.get("recovery_streak_days") or 0),
        "last_successful_fetch": entry.get("last_successful_fetch"),
        "total_fetches": int(entry.get("total_fetches") or 0),
        "total_failures": int(entry.get("total_failures") or 0),
    }


def board(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """The full provider health board, worst status first."""
    store = load_store(path)
    rows = [provider_status(name, store) for name in sorted(store)]
    order = {"blocked": 0, "degraded": 1, "cooling": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["provider"]))
    return {
        "generated_at": _utcnow().isoformat(),
        "providers": rows,
        "counts": {
            s: sum(1 for r in rows if r["status"] == s)
            for s in ("ok", "cooling", "degraded", "blocked")
        },
    }


def degraded_providers(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Providers needing attention (degraded or blocked). For alerting."""
    return [r for r in board(path)["providers"] if r["status"] in ("degraded", "blocked")]


def fetch_allowed(
    provider: str, path: str | Path | None = None
) -> tuple[bool, str]:
    """Whether a fetch should be attempted right now: no active cooldown,
    no block, and budget remaining. Returns (allowed, reason)."""
    row = provider_status(provider, path=path)
    if row["status"] == "blocked":
        return False, "captcha_blocked"
    if row["cooldown_remaining_s"] > 0:
        return False, f"cooling_down:{row['cooldown_reason']}"
    if row["budget"]["exhausted"]:
        return False, "budget_exhausted"
    return True, ""


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register provider-health tools on the MCP server."""

    @mcp.tool()
    def provider_health_board() -> dict:
        """Provider health board: budget, cooldown, CAPTCHA state, recovery
        streak, and last successful fetch per provider."""
        return board()

    @mcp.tool()
    def provider_health_record(
        provider: str, ok: bool, note: str = ""
    ) -> dict:
        """Record one fetch attempt for a provider (ok true/false)."""
        return record_fetch(provider, bool(ok), note=note)

    @mcp.tool()
    def provider_health_captcha(
        provider: str, state: str = "challenged", cooldown_seconds: int = 3600
    ) -> dict:
        """Record an observed CAPTCHA state for a provider (observation
        only — never a bypass)."""
        return record_captcha(provider, state,
                              cooldown_seconds=cooldown_seconds)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_result(result: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        _print_board_text(result) if isinstance(result, dict) and "providers" in result \
            else print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _print_board_text(result: dict[str, Any]) -> None:
    rows = result.get("providers", [])
    if not rows:
        print("No provider observations yet. Record fetch attempts with:")
        print("  health record --provider <name> --ok|--fail")
        return
    print(f"{'provider':<18}{'status':<10}{'budget':<14}{'cooldown':<12}"
          f"{'captcha':<12}{'fail':<6}{'streak':<7}last fetch")
    for r in rows:
        b = r["budget"]
        budget = (
            f"{b['used_today']}/{b['daily_limit']}"
            if b["daily_limit"] is not None else "-"
        )
        cd = r["cooldown_remaining_s"]
        cooldown_s = f"{int(cd // 60)}m{int(cd % 60)}s" if cd > 0 else "-"
        last = (r["last_successful_fetch"] or "-")[:16].replace("T", " ")
        print(f"{r['provider']:<18}{r['status']:<10}{budget:<14}{cooldown_s:<12}"
              f"{r['captcha_state']:<12}{r['consecutive_failures']:<6}"
              f"{r['recovery_streak_days']:<7}{last}")
    counts = result.get("counts", {})
    print("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


def _cli_board(args: Any) -> int:
    _print_result(board(), bool(getattr(args, "json", False)))
    return 0


def _cli_record(args: Any) -> int:
    if args.ok == args.fail:
        print("error: pass exactly one of --ok or --fail")
        return 2
    result = record_fetch(args.provider, args.ok, note=args.note or "")
    _print_result(result, args.json)
    return 0


def _cli_captcha(args: Any) -> int:
    try:
        result = record_captcha(
            args.provider, args.state, cooldown_seconds=args.cooldown_seconds
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    _print_result(result, args.json)
    return 0


def _cli_budget(args: Any) -> int:
    try:
        result = set_budget(args.provider, args.daily_limit)
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    _print_result(result, args.json)
    return 0


def _cli_cooldown(args: Any) -> int:
    result = cooldown(args.provider, args.seconds, reason=args.reason or "")
    _print_result(result, args.json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "health", help="Provider health board: budget, cooldown, CAPTCHA, streaks."
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="health_cmd")

    pb = sub.add_parser("board", help="Show the provider health board.")
    pb.add_argument("--json", action="store_true")
    pb.set_defaults(func=_cli_board)

    pr = sub.add_parser("record", help="Record one fetch attempt.")
    pr.add_argument("--provider", required=True)
    pr.add_argument("--ok", action="store_true", help="The fetch succeeded.")
    pr.add_argument("--fail", action="store_true", help="The fetch failed.")
    pr.add_argument("--note", default="")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=_cli_record)

    pc = sub.add_parser("captcha", help="Record an observed CAPTCHA state.")
    pc.add_argument("--provider", required=True)
    pc.add_argument("--state", default="challenged",
                    choices=list(CAPTCHA_STATES))
    pc.add_argument("--cooldown-seconds", type=int, default=3600)
    pc.set_defaults(func=_cli_captcha)

    pbu = sub.add_parser("budget", help="Set a provider's daily fetch budget.")
    pbu.add_argument("--provider", required=True)
    pbu.add_argument("--daily-limit", type=int, default=None,
                     help="Omit to clear the budget.")
    pbu.set_defaults(func=_cli_budget)

    pco = sub.add_parser("cooldown", help="Put a provider on cooldown.")
    pco.add_argument("--provider", required=True)
    pco.add_argument("--seconds", type=int, required=True)
    pco.add_argument("--reason", default="")
    pco.set_defaults(func=_cli_cooldown)

    # Bare `health` shows the board.
    p.set_defaults(func=_cli_board)
    return {"health": lambda args: args.func(args)}
