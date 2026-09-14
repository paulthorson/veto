"""KPI snapshot for the job-apply terminal dashboard (stdlib only).

``compute_kpis(root)`` reads the local JSON state stores under ``root``
(default: this repo directory) and returns a fixed-shape dict. Missing or
corrupt stores degrade gracefully to zeros/empties, so a fresh install
shows a clean zero state instead of crashing.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent

#: Stage values (lowercased) that count as terminal — never need follow-up.
_TERMINAL_STAGES = {"offer", "rejected", "withdrawn", "accepted"}

#: Stages that are still nudgeable once a due date arrives.
_NUDGE_STAGES = {"applied", "interviewing", "phone", "screen"}

#: Numeric keys that may carry a recorded fit score on an application entry.
_FIT_KEYS = ("fit_score", "fit", "match_score", "score")

#: Entry date fields consulted (in order) for activity/follow-up heuristics.
_DATE_KEYS = ("submitted_at", "applied_at", "applied_on", "created_at",
              "date_applied", "timestamp")

_STAGE_TO_FUNNEL = {
    "applied": "applied",
    "application": "applied",
    "phone": "phone",
    "phone_screen": "phone",
    "screen": "phone",
    "screening": "phone",
    "interview": "interview",
    "interviewing": "interview",
    "onsite": "interview",
    "final": "interview",
    "final_round": "interview",
    "offer": "offer",
    "offer_received": "offer",
    "accepted": "offer",
    "rejected": "rejected",
}

#: A stale "applied" application gets a follow-up nudge after this many days.
APPLIED_NUDGE_DAYS = 7


def _load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _fit_score(entry: dict) -> float | None:
    """First numeric value found under the known fit-score keys."""
    for key in _FIT_KEYS:
        value = entry.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):  # e.g. {"fit": {"score": 82}}
            nested = value.get("score")
            if isinstance(nested, (int, float)) and not isinstance(nested, bool):
                return float(nested)
    return None


def _entry_date(entry: dict) -> date | None:
    """Best-effort application date from known date fields or stage history."""
    history = entry.get("stage_history")
    if isinstance(history, list) and history:
        first = history[0]
        if isinstance(first, dict):
            parsed = _parse_date(first.get("at"))
            if parsed:
                return parsed
    for key in _DATE_KEYS:
        parsed = _parse_date(entry.get(key))
        if parsed:
            return parsed
    return None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _submitted_date(entry: dict) -> date | None:
    """When the application was submitted.

    Mirrors ``followup._days_since_applied``: ``submitted_at`` first, then
    the earliest ``stage_history`` event with stage "applied". Entries the
    user wrote by hand usually lack both, so ``applied_at``-style fields
    are a last-resort fallback.
    """
    parsed = _parse_date(entry.get("submitted_at"))
    if parsed:
        return parsed
    history = entry.get("stage_history")
    if isinstance(history, list):
        for event in history:
            if isinstance(event, dict) and str(event.get("stage")) == "applied":
                parsed = _parse_date(event.get("at"))
                if parsed:
                    return parsed
    for key in _DATE_KEYS:
        parsed = _parse_date(entry.get(key))
        if parsed:
            return parsed
    return None


def _followup_due(entry: dict, today: date) -> bool:
    """Follow-up heuristic for one application entry.

    Mirrors ``followup._needs_followup`` so the KPI header and the
    follow-up workflow agree. Counts when the entry is non-terminal AND
    either:
    - ``follow_up_due`` is set and reached (today >= due date) while the
      stage is still nudgeable (applied / interviewing / phone / screen), or
    - the stage is ``applied`` with 7+ days since submission
      (``submitted_at``, then the stage-history "applied" event, then any
      ``applied_at``-style date field) and no recorded response.
    """
    stage = str(entry.get("stage") or "applied").lower()
    if stage in _TERMINAL_STAGES:
        return False
    due = _parse_date(entry.get("follow_up_due"))
    if due and due <= today and stage in _NUDGE_STAGES:
        return True
    if stage == "applied":
        submitted = _submitted_date(entry)
        if submitted and (today - submitted).days >= APPLIED_NUDGE_DAYS:
            if not entry.get("responded") and not entry.get("response_at"):
                return True
    return False


def _streak_days(streaks_store: object, today: date) -> int:
    """Consecutive days with >=1 logged rep ending today (today may be empty)."""
    if not isinstance(streaks_store, dict):
        return 0
    days: set[date] = set()
    for rep in streaks_store.get("reps") or []:
        if isinstance(rep, dict):
            parsed = _parse_date(rep.get("ts") or rep.get("at") or rep.get("date"))
            if parsed:
                days.add(parsed)
    if not days:
        return 0
    cursor = today if today in days else today - timedelta(days=1)
    streak = 0
    while cursor in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def compute_kpis(root: Path | None = None) -> dict:
    """Compute dashboard KPIs from the JSON state stores under ``root``.

    Keys: applications, avg_fit, streak_days, interviews, offers,
    followups_due, activity_30d, funnel.
    """
    root = Path(root) if root else REPO_DIR
    today = date.today()

    applications_raw = _load_json(root / "applications.json", [])
    entries = [
        e for e in applications_raw if isinstance(e, dict)
    ] if isinstance(applications_raw, list) else []

    fit_scores = [s for s in (_fit_score(e) for e in entries) if s is not None]
    avg_fit = round(sum(fit_scores) / len(fit_scores), 1) if fit_scores else None

    streaks_store = _load_json(root / "streaks.json", {})

    offers_raw = _load_json(root / "offers.json", [])
    offers_list = offers_raw if isinstance(offers_raw, list) else []

    funnel = {"applied": 0, "phone": 0, "interview": 0, "offer": 0, "rejected": 0}
    for entry in entries:
        bucket = _STAGE_TO_FUNNEL.get(str(entry.get("stage") or "applied").lower())
        if bucket:
            funnel[bucket] += 1

    activity: list[dict] = []
    counts: dict[str, int] = {}
    for entry in entries:
        day = _entry_date(entry)
        if day:
            key = day.isoformat()
            counts[key] = counts.get(key, 0) + 1
    for back in range(29, -1, -1):
        day = today - timedelta(days=back)
        activity.append({"date": day.isoformat(), "count": counts.get(day.isoformat(), 0)})

    return {
        "applications": len(entries),
        "avg_fit": avg_fit,
        "streak_days": _streak_days(streaks_store, today),
        "interviews": funnel["phone"] + funnel["interview"],
        "offers": len(offers_list) + funnel["offer"],
        "followups_due": sum(1 for e in entries if _followup_due(e, today)),
        "activity_30d": activity,
        "funnel": funnel,
    }


if __name__ == "__main__":
    print(json.dumps(compute_kpis(), indent=2))
