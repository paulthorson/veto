#!/usr/bin/env python3
"""Setup health check for the Veto MCP server.

``run_doctor() -> dict`` returns ``{"ok": bool, "checks": [...]}`` where
each check is ``{"name", "ok", "detail", "severity"}``. Severity is
``"error"`` (setup is broken without this) or ``"warning"`` (optional /
degraded). The top-level ``ok`` is True only when every error-severity
check passes — a missing optional component (e.g. Playwright browsers,
only needed for Phase 2 browser automation) does not fail the doctor.

Checks:

* profile — wizard-saved profile exists and has the browser-hook keys
  (full_name, first_name, last_name, email, phone, location,
  linkedin_url, website, cover_letter).
* compliance — ``compliance.status_summary()`` loads: mode, risk
  acknowledgment, applications used vs. daily cap.
* playwright — ``python -m playwright --version`` works and browser
  binaries are present (warning only; Phase 2 feature).
* sessions — saved board login sessions exist (warning only).
* preferences — ``preferences.json`` parses (missing file = defaults,
  which is fine).
* boards — the provider/boards listing is importable and non-empty.

``if __name__ == "__main__"`` usage::

    python3 doctor.py [--json]

Wiring into the main CLI (cli.py) without editing it — in the wiring
code, after ``sub = parser.add_subparsers(...)``::

    import doctor
    handlers = doctor.register_cli(sub)  # {command: handler} mapping
    dispatch.update(handlers)

The returned mapping merges into the caller's dispatch table; each
parser also carries ``func`` so a caller can dispatch
``args.func(args)`` when the parsed args carry it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PROFILE_FILE = BASE_DIR / "profiles" / "profile.json"
PROFILE_DEFAULT_FILE = BASE_DIR / "profiles" / "default.json"
SESSIONS_DIR = BASE_DIR / "sessions"

import compliance  # noqa: E402  (stdlib-style local module)

#: Keys the browser-apply hook needs from the saved profile.
BROWSER_HOOK_KEYS = (
    "full_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "location",
    "linkedin_url",
    "website",
    "cover_letter",
)


def _check(
    name: str, ok: bool, detail: str, severity: str = "error"
) -> dict:
    return {"name": name, "ok": ok, "detail": detail, "severity": severity}


def check_profile(path: Path | None = None) -> dict:
    """Profile file exists and carries the browser-hook keys."""
    candidates = [Path(path)] if path else [PROFILE_FILE, PROFILE_DEFAULT_FILE]
    found: Path | None = None
    profile: dict = {}
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            found, profile = candidate, data
            break
    if found is None:
        return _check(
            "profile",
            False,
            "No profile found. Run `python3 wizard.py` to create "
            f"{PROFILE_FILE.name} (also checked {PROFILE_DEFAULT_FILE.name}).",
        )
    missing = [
        k
        for k in BROWSER_HOOK_KEYS
        # website/cover_letter may legitimately be empty; the hook only
        # needs the keys to exist.
        if k not in profile
        or (k not in {"website", "cover_letter"} and not profile.get(k))
    ]
    if missing:
        return _check(
            "profile",
            False,
            f"{found.name} is missing keys: {', '.join(missing)}. "
            "Re-run the wizard to fill them in.",
        )
    return _check(
        "profile",
        True,
        f"{found.name} present for {profile.get('full_name') or '(unnamed)'} "
        f"<{profile.get('email') or 'no email'}>; browser-hook keys OK.",
    )


def check_compliance() -> dict:
    """Compliance state loads and the daily apply budget is readable."""
    try:
        summary = compliance.status_summary()
    except Exception as exc:
        return _check("compliance", False, f"status_summary() failed: {exc}")
    return _check(
        "compliance",
        True,
        f"mode={summary.get('mode')} risk_acknowledged="
        f"{summary.get('risk_acknowledged')} "
        f"applications_today={summary.get('applications_today')}/"
        f"{summary.get('daily_apply_cap')}",
    )


def check_playwright() -> dict:
    """Playwright CLI + browser binaries (warning: Phase 2 only)."""
    detail_parts: list[str] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:
        return _check(
            "playwright",
            False,
            f"Could not run `python -m playwright --version`: {exc}. "
            "Phase 2 browser automation will be unavailable.",
            severity="warning",
        )
    if proc.returncode != 0:
        return _check(
            "playwright",
            False,
            "Playwright is not installed (`python -m playwright --version` "
            "failed). Phase 2 browser automation will be unavailable; "
            "Phase 1 recording still works.",
            severity="warning",
        )
    detail_parts.append(proc.stdout.strip() or "playwright installed")
    browsers_dir = Path.home() / ".cache" / "ms-playwright"
    if browsers_dir.is_dir() and any(browsers_dir.iterdir()):
        detail_parts.append(f"browsers present in {browsers_dir}")
    else:
        return _check(
            "playwright",
            False,
            "; ".join(detail_parts)
            + " — but no browser binaries found "
            f"({browsers_dir} is empty/missing). Run "
            "`python -m playwright install chromium` for Phase 2.",
            severity="warning",
        )
    return _check("playwright", True, "; ".join(detail_parts), severity="warning")


def check_sessions(path: Path | None = None) -> dict:
    """Saved board login sessions (warning: optional convenience)."""
    sessions_dir = Path(path) if path else SESSIONS_DIR
    if not sessions_dir.is_dir():
        return _check(
            "sessions",
            False,
            f"{sessions_dir.name}/ not found — no saved board logins. "
            "Phase 2 will run without stored sessions.",
            severity="warning",
        )
    files = [p for p in sessions_dir.iterdir() if p.is_file()]
    return _check(
        "sessions",
        True,
        f"{len(files)} saved session(s) in {sessions_dir.name}/"
        + (f": {', '.join(p.name for p in files[:5])}" if files else ""),
        severity="warning",
    )


def check_preferences() -> dict:
    """preferences.json parses (missing file = defaults, which is fine)."""
    from prefs import PREFS_FILE, load_preferences  # noqa: E402  (lazy, cheap)

    if not PREFS_FILE.is_file():
        prefs = load_preferences()
        return _check(
            "preferences",
            True,
            "preferences.json not found — using defaults "
            f"(blocked_companies={len(prefs['blocked_companies'])}, "
            f"blocked_keywords={len(prefs['blocked_keywords'])}).",
        )
    try:
        data = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _check(
            "preferences", False, f"preferences.json is unreadable: {exc}"
        )
    if not isinstance(data, dict):
        return _check(
            "preferences", False, "preferences.json is not a JSON object."
        )
    return _check(
        "preferences",
        True,
        f"preferences.json OK "
        f"(blocked_companies={len(data.get('blocked_companies', []) or [])}, "
        f"preferred_companies={len(data.get('preferred_companies', []) or [])}, "
        f"blocked_keywords={len(data.get('blocked_keywords', []) or [])}).",
    )


def check_boards() -> dict:
    """Board/provider listing is importable and non-empty."""
    try:
        from server import list_boards  # noqa: E402  (lazy: heavy MCP import)

        boards = list_boards()
    except Exception as exc:
        return _check("boards", False, f"Could not load board list: {exc}")
    if not boards:
        return _check("boards", False, "Board list came back empty.")
    names = [
        b.get("board") if isinstance(b, dict) else str(b) for b in boards
    ]
    return _check(
        "boards", True, f"{len(boards)} board(s): {', '.join(names)}"
    )


def run_doctor() -> dict:
    """Run every check. Never raises; each check degrades to a failure entry."""
    checks: list[dict] = []
    for fn in (
        check_profile,
        check_compliance,
        check_playwright,
        check_sessions,
        check_preferences,
        check_boards,
    ):
        try:
            checks.append(fn())
        except Exception as exc:  # a check must never kill the doctor
            name = getattr(fn, "__name__", "check")
            checks.append(_check(name, False, f"check crashed: {exc}"))
    ok = all(c["ok"] for c in checks if c.get("severity") != "warning")
    return {"ok": ok, "checks": checks}


# ---------------------------------------------------------------------------
# CLI (plugin: register_cli; do NOT edit cli.py — wire from outside)
# ---------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for check in report["checks"]:
            if check["ok"]:
                mark = "OK  "
            elif check.get("severity") == "warning":
                mark = "WARN"
            else:
                mark = "FAIL"
            print(f"[{mark}] {check['name']}: {check['detail']}")
        print(f"\nDoctor: {'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``doctor`` command to a CLI.

    Returns a {command: handler} mapping the caller merges into its
    own dispatch table (cli.py-style). The parser also gets ``func``
    set so the owner can dispatch ``args.func(args)``.
    """
    p = subparsers.add_parser(
        "doctor", help="Check the Veto setup health (profile, "
        "compliance, browsers, sessions, preferences, boards)."
    )
    p.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    p.set_defaults(func=_cmd_doctor)
    return {"doctor": _cmd_doctor}


def register_tools(mcp: Any) -> None:
    """Register doctor tools on an MCP server instance."""

    @mcp.tool()
    def compliance_status() -> dict:
        """Compliance snapshot: mode, ToS-risk acknowledgment, today's
        search budgets and block cooldowns per board, and the daily
        application cap usage."""
        return compliance.status_summary()


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(
        prog="doctor.py", description="Health check for the Veto setup."
    )
    _sub = _parser.add_subparsers(dest="command", required=True)
    register_cli(_sub)
    _args = _parser.parse_args()
    sys.exit(_args.func(_args))
