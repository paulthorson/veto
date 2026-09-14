"""Streaks and gamification: make the job-hunt reps addictive.

Workflow:
1. ``log_rep(kind)`` — log one rep of a kind (application, drill,
   mock_interview, negotiation_round, ai_lesson, networking_touch,
   tailoring). Timestamped, stored in ``streaks.json`` (atomic writes).
2. ``streaks()`` — current streak per kind + overall (consecutive days
   with >=1 rep), longest streaks, today's rep counts vs daily goals.
3. ``set_goal(kind, daily_target)`` / ``goals()`` — user-set daily
   targets; ``today()`` shows text progress bars (e.g.
   ``drills [###--] 3/5``).
4. ``weekly_recap()`` — this week's totals per kind, streaks gained/lost,
   and one encouraging line derived from the actual numbers (never empty
   cheerleading).
5. ``share_card()`` — a plain-text/Markdown progress card for posting,
   built ONLY from real logged reps — never inflated. This is the viral
   loop.

Honesty contract: streaks count days you actually logged reps. The
share card only includes kinds with >=1 real rep, and every number on
it is traceable to the log. This module never estimates or pads.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
STORE_PATH = BASE_DIR / "streaks.json"

#: Injectable "today" for deterministic tests. Tests set
#: ``streaks._TODAY_OVERRIDE = date(...)``; production leaves it None.
_TODAY_OVERRIDE: date | None = None

REP_KINDS = (
    "application",
    "drill",
    "mock_interview",
    "negotiation_round",
    "ai_lesson",
    "networking_touch",
    "tailoring",
)

KIND_LABELS = {
    "application": "applications",
    "drill": "drills",
    "mock_interview": "mock interviews",
    "negotiation_round": "negotiation rounds",
    "ai_lesson": "AI lessons",
    "networking_touch": "networking touches",
    "tailoring": "tailored apps",
}

#: Sensible starting targets; the user can override any of them.
DEFAULT_GOALS = {
    "application": 3,
    "drill": 2,
    "mock_interview": 1,
    "negotiation_round": 1,
    "ai_lesson": 1,
    "networking_touch": 2,
    "tailoring": 1,
}

_BAR_WIDTH = 10


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _today() -> date:
    return _TODAY_OVERRIDE or date.today()


def _load_store() -> dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reps": [], "goals": {}}
    if not isinstance(data, dict):
        return {"reps": [], "goals": {}}
    reps = data.get("reps")
    goals = data.get("goals")
    return {
        "reps": reps if isinstance(reps, list) else [],
        "goals": goals if isinstance(goals, dict) else {},
    }


def _save_store(store: dict[str, Any]) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _day_of(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def _default_ts() -> float:
    """Local-noon timestamp of the effective today.

    Uses ``_today()`` so reps logged without an explicit ``ts`` honor the
    injectable test override, and noon keeps the stored value safely
    inside the day boundary under either local or UTC interpretation of
    the epoch value (see tests/test_dashboard.py's ``_streaks_store``).
    """
    today = _today()
    return datetime(today.year, today.month, today.day, 12, 0, 0).timestamp()


def _rep_days(store: dict[str, Any], kind: str | None = None) -> set[date]:
    days: set[date] = set()
    for rep in store["reps"]:
        if not isinstance(rep, dict):
            continue
        if kind is not None and rep.get("kind") != kind:
            continue
        ts = rep.get("ts")
        if isinstance(ts, (int, float)):
            days.add(_day_of(ts))
    return days


def _current_streak(days: set[date], today: date) -> int:
    """Consecutive rep-days ending today; forgives today if empty (the
    streak is only dead after a full missed day)."""
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _longest_streak(days: set[date]) -> int:
    if not days:
        return 0
    ordered = sorted(days)
    best = run = 1
    for prev, cur in zip(ordered, ordered[1:]):
        if cur - prev == timedelta(days=1):
            run += 1
            best = max(best, run)
        else:
            run = 1
    return best


def _count_on(store: dict[str, Any], day: date, kind: str | None = None) -> int:
    return sum(
        1
        for rep in store["reps"]
        if isinstance(rep, dict)
        and (kind is None or rep.get("kind") == kind)
        and isinstance(rep.get("ts"), (int, float))
        and _day_of(rep["ts"]) == day
    )


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def log_rep(kind: str, ts: float | None = None) -> dict[str, Any]:
    """Log one rep of ``kind``. Returns the stored entry.

    When ``ts`` is omitted the rep is stamped at local noon of the
    effective today (see ``_default_ts``), so it always lands in the day
    bucket that ``_today()`` reports.
    """
    if kind not in REP_KINDS:
        raise ValueError(f"unknown rep kind {kind!r}; expected one of {list(REP_KINDS)}")
    store = _load_store()
    entry = {"kind": kind, "ts": ts if ts is not None else _default_ts()}
    store["reps"].append(entry)
    _save_store(store)
    return {"ok": True, "rep": entry, "today_count": _count_on(store, _today(), kind)}


def streaks() -> dict[str, Any]:
    """Current streak per kind + overall, longest streaks, and today's
    counts vs goals."""
    store = _load_store()
    today = _today()
    goal_map = goals()["goals"]
    per_kind: dict[str, dict[str, Any]] = {}
    for kind in REP_KINDS:
        days = _rep_days(store, kind)
        per_kind[kind] = {
            "label": KIND_LABELS[kind],
            "current": _current_streak(days, today),
            "longest": _longest_streak(days),
            "today": _count_on(store, today, kind),
            "goal": goal_map[kind],
            "total_reps": sum(1 for r in store["reps"] if isinstance(r, dict) and r.get("kind") == kind),
        }
    all_days = _rep_days(store)
    return {
        "today": today.isoformat(),
        "kinds": per_kind,
        "overall": {
            "current": _current_streak(all_days, today),
            "longest": _longest_streak(all_days),
            "today": _count_on(store, today),
        },
    }


def set_goal(kind: str, daily_target: int) -> dict[str, Any]:
    """Set the daily target for a rep kind (positive integer)."""
    if kind not in REP_KINDS:
        raise ValueError(f"unknown rep kind {kind!r}; expected one of {list(REP_KINDS)}")
    if isinstance(daily_target, bool) or not isinstance(daily_target, int) or daily_target < 1:
        raise ValueError("daily_target must be a positive integer")
    store = _load_store()
    store["goals"][kind] = daily_target
    _save_store(store)
    return {"ok": True, "kind": kind, "daily_target": daily_target}


def goals() -> dict[str, Any]:
    """Effective daily goals: user overrides merged over defaults."""
    store = _load_store()
    merged = dict(DEFAULT_GOALS)
    for kind, target in store["goals"].items():
        if kind in REP_KINDS and isinstance(target, int) and target >= 1:
            merged[kind] = target
    return {"goals": merged}


def _progress_bar(count: int, target: int) -> str:
    filled = min(_BAR_WIDTH, round(_BAR_WIDTH * count / max(target, 1)))
    return "#" * filled + "-" * (_BAR_WIDTH - filled)


def today() -> dict[str, Any]:
    """Today's progress per kind as text progress bars."""
    status = streaks()
    lines = []
    for kind in REP_KINDS:
        info = status["kinds"][kind]
        bar = _progress_bar(info["today"], info["goal"])
        lines.append(f'{info["label"]:20s} [{bar}] {info["today"]}/{info["goal"]}')
    rendered = "\n".join(lines)
    return {"today": status["today"], "lines": lines, "rendered": rendered}


def weekly_recap() -> dict[str, Any]:
    """This week's totals per kind, streaks gained/lost, and one
    encouraging line derived from the actual numbers."""
    store = _load_store()
    today = _today()
    week_start = today - timedelta(days=6)

    totals: dict[str, int] = {}
    for kind in REP_KINDS:
        totals[kind] = sum(
            1
            for rep in store["reps"]
            if isinstance(rep, dict)
            and rep.get("kind") == kind
            and isinstance(rep.get("ts"), (int, float))
            and week_start <= _day_of(rep["ts"]) <= today
        )

    gained: list[str] = []
    lost: list[str] = []
    for kind in REP_KINDS:
        days = _rep_days(store, kind)
        current = _current_streak(days, today)
        longest = _longest_streak(days)
        old_days = {d for d in days if d <= today - timedelta(days=7)}
        old_longest = _longest_streak(old_days)
        if longest > old_longest and longest >= 2:
            gained.append(f"{KIND_LABELS[kind]}: new record {longest}-day streak")
        if current == 0 and days:
            last_active = max(days)
            ended = _current_streak(days, last_active)
            if ended >= 2:
                lost.append(f"{KIND_LABELS[kind]}: streak ended at {ended} days")

    total_reps = sum(totals.values())
    if total_reps == 0:
        line = "No reps logged this week yet. One drill today starts the streak — small reps count."
    else:
        leader = max(REP_KINDS, key=lambda k: totals[k])
        goal_map = goals()["goals"]
        ratio = totals[leader] / max(goal_map[leader] * 7, 1)
        line = (
            f"Biggest push: {totals[leader]} {KIND_LABELS[leader]} this week "
            f"({ratio:.1f}x your weekly goal pace)"
        )
        if gained:
            line += f". New record: {gained[0].split(': ', 1)[1]}."
        elif lost:
            line += f". One to rebuild: {lost[0].split(': ', 1)[1]}."

    return {
        "week_start": week_start.isoformat(),
        "week_end": today.isoformat(),
        "totals": totals,
        "total_reps": total_reps,
        "streaks_gained": gained,
        "streaks_lost": lost,
        "encouragement": line,
    }


def share_card() -> dict[str, Any]:
    """A plain-text/Markdown progress card built ONLY from real logged
    reps — kinds with zero reps are omitted, numbers are never inflated."""
    status = streaks()
    lines = ["MY VETO GRIND", ""]
    shown = 0
    for kind in REP_KINDS:
        info = status["kinds"][kind]
        if info["total_reps"] == 0:
            continue
        shown += 1
        bits = [f"{info['total_reps']} {info['label']}"]
        if info["current"] >= 2:
            bits.append(f"{info['current']}-day streak")
        if info["longest"] >= 2:
            bits.append(f"best {info['longest']} days")
        lines.append("- " + ", ".join(bits))
    if shown == 0:
        lines.append("No reps logged yet — the grind starts with one.")
    else:
        overall = status["overall"]
        if overall["current"] >= 2:
            lines.append("")
            lines.append(f"Overall: {overall['current']}-day streak, every rep earned.")
    lines.append("")
    lines.append("Logged with Veto — every number above is a real rep.")
    card = "\n".join(lines)
    return {"card": card, "kinds_shown": shown}


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the streaks MCP tools on an MCP server instance."""
    _impl_log = globals()["log_rep"]
    _impl_streaks = globals()["streaks"]
    _impl_set_goal = globals()["set_goal"]
    _impl_goals = globals()["goals"]
    _impl_today = globals()["today"]
    _impl_recap = globals()["weekly_recap"]
    _impl_card = globals()["share_card"]

    @mcp.tool()
    def log_streak_rep(kind: str) -> dict:
        """Log one practice rep.

        Args:
            kind: One of application, drill, mock_interview,
                negotiation_round, ai_lesson, networking_touch, tailoring.

        Returns:
            The stored rep plus today's count for that kind.
        """
        return _impl_log(kind)

    @mcp.tool()
    def streak_status() -> dict:
        """Current streak per kind + overall, longest streaks, and
        today's rep counts vs daily goals."""
        return _impl_streaks()

    @mcp.tool()
    def today_progress() -> dict:
        """Today's progress per kind as text progress bars."""
        return _impl_today()

    @mcp.tool()
    def set_streak_goal(kind: str, daily_target: int) -> dict:
        """Set the daily target for a rep kind.

        Args:
            kind: Rep kind (see log_streak_rep).
            daily_target: Positive integer target per day.
        """
        return _impl_set_goal(kind, daily_target)

    @mcp.tool()
    def streak_goals() -> dict:
        """Effective daily goals (user overrides over defaults)."""
        return _impl_goals()

    @mcp.tool()
    def weekly_recap_report() -> dict:
        """This week's totals per kind, streaks gained/lost, and one
        encouraging line derived from the actual numbers."""
        return _impl_recap()

    @mcp.tool()
    def share_progress_card() -> dict:
        """A Markdown progress card built only from real logged reps —
        safe to post, never inflated."""
        return _impl_card()


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
    elif isinstance(result, dict) and "rendered" in result:
        print(result["rendered"])
    elif isinstance(result, dict) and "card" in result:
        print(result["card"])
    elif isinstance(result, dict) and "lines" in result:
        print("\n".join(result["lines"]))
    elif isinstance(result, dict) and "encouragement" in result:
        r = result
        for kind in REP_KINDS:
            print(f"{KIND_LABELS[kind]:20s} {r['totals'][kind]} reps this week")
        print(f"\nGained: {', '.join(r['streaks_gained']) or 'none'}")
        print(f"Lost:   {', '.join(r['streaks_lost']) or 'none'}")
        print(f"\n{r['encouragement']}")
    else:
        print(json.dumps(result, indent=2))


def cmd_streaks(args: argparse.Namespace) -> int:
    """Handler for the `streaks` CLI command."""
    action = args.action
    try:
        if action in ("log", "set-goal") and not args.kind:
            raise ValueError(f"action {action!r} requires --kind")
        if action == "set-goal" and args.target is None:
            raise ValueError("action 'set-goal' requires --target")
        if action == "log":
            result = log_rep(args.kind)
        elif action == "status":
            result = streaks()
        elif action == "today":
            result = today()
        elif action == "set-goal":
            result = set_goal(args.kind, args.target)
        elif action == "goals":
            result = goals()
        elif action == "recap":
            result = weekly_recap()
        elif action == "card":
            result = share_card()
        else:
            raise ValueError(f"unknown action {action!r}")
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    _print_result(result, args.json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `streaks` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser(
        "streaks", help="Log reps, track streaks, and share progress cards."
    )
    p.add_argument(
        "action",
        choices=["log", "status", "today", "set-goal", "goals", "recap", "card"],
        help="What to do.",
    )
    p.add_argument(
        "--kind",
        choices=list(REP_KINDS),
        help="Rep kind (for log and set-goal).",
    )
    p.add_argument(
        "--target",
        type=int,
        help="Daily target (for set-goal).",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return {"streaks": cmd_streaks}
