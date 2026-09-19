#!/usr/bin/env python3
"""Initiative 06 — longitudinal practice tracking and drill recommendation.

This module answers the proof-of-value question: *which behaviors
improved across sessions, and which gaps still affect readiness?*

Two sources feed the history:

1. Practice session results recorded by the lab modules (mock
   interviews, soft-skill scenarios, AI-proficiency lab exercises) via
   :func:`record_result`. Each result carries per-dimension rubric
   scores (see ``initiatives.i06.rubric``).
2. Outcome events from Initiative 01 (``outcomes.jsonl``,
   schema ``outcome-min-v0``) linked by ``job_id``/``application_id``
   when available. Reading them is defensive: if the outcomes module
   or its file is missing, history degrades gracefully to
   session-local data and says so in the output.

State lives in a user data dir (``~/.local/share/veto/i06``, or
``$XDG_DATA_HOME/veto/i06``) — never in the repo tree, so practice
labels naming real employers can't land in git history. Legacy
repo-root files (``i06_practice_history.json``,
``i06_practice_focus.json``) are migrated once, at import, into the
new location.

Load provenance (Rule 1): a corrupt or unreadable history file is
NEVER silently treated as "no history". Every public output dict
carries ``history_ok`` (and ``focus_ok`` where a focus file is read);
``record_result`` refuses to overwrite a corrupt file unless the
caller passes ``recover=True`` explicitly. Writes are atomic
(tmp-file + rename) and keep a ``.bak`` of the previous file.

The observed-gap rule
---------------------
The roadmap's Q1 gate defines an **observed gap** as: the same
disclosed rubric dimension scoring below the approved threshold in at
least 2 of 3 completed sessions — OR an explicit user selection.
:class:`ObservedGapRule` parameterizes exactly that definition
(``window``, ``repeat_count``, ``threshold``), so if the operator and the
independent framework reviewer approve different numbers by
2027-01-10 (roadmap Q1 day 10), the engine adopts them without a code
change. Until that approval is recorded, every rule payload carries
``"pending_approval": true`` (see ``RULE_PENDING_APPROVAL``).

The hard boundary: recommendations are built ONLY from observed gaps
or explicit user selections. When there are neither, the engine says
so plainly and suggests a maintenance rep — it never invents weakness
labels, never emits a generic course list.

Focus staleness: an explicit focus selection older than
``FOCUS_TTL_DAYS`` days is treated as expired (the recommendation
falls back to observed gaps and says so). While a fresh focus is
active, co-occurring observed gaps are still surfaced as "also
observed" so they are never silently suppressed.

Stdlib only, deterministic, no network.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # normal package use: pytest, `python -m`, skill_gaps
    from .rubric import DIMENSIONS
except ImportError:  # direct script execution
    from rubric import DIMENSIONS  # type: ignore[no-redef]

BASE_DIR = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# State location: user data dir, never the repo tree
# ---------------------------------------------------------------------------

def _default_state_dir() -> Path:
    """User data directory for i06 practice state (stdlib XDG-style).

    Practice labels can name real employers, so state must never live
    in the repo tree (risk: employer names in git history). Honors
    ``XDG_DATA_HOME`` when set, else ``~/.local/share``.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    state = base / "veto" / "i06"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _migrate_repo_root_state(state_dir: Path) -> None:
    """One-way migration of legacy repo-root state files.

    Moves ``i06_practice_history.json`` / ``i06_practice_focus.json``
    from the repo root into the user data dir when they exist there
    and no copy exists yet. Idempotent; never overwrites the new
    location.
    """
    for name in ("i06_practice_history.json", "i06_practice_focus.json"):
        legacy = BASE_DIR / name
        target = state_dir / name
        if legacy.exists() and not target.exists():
            target.write_bytes(legacy.read_bytes())
            legacy.unlink()


STATE_DIR = _default_state_dir()
try:
    _migrate_repo_root_state(STATE_DIR)
except OSError:
    pass  # migration is best-effort; new writes go to STATE_DIR regardless

HISTORY_PATH = STATE_DIR / "i06_practice_history.json"
FOCUS_PATH = STATE_DIR / "i06_practice_focus.json"

# Where outcome events live when Initiative 01 capture is active.
OUTCOMES_FILENAME = "outcomes.jsonl"


# ---------------------------------------------------------------------------
# Human gate + tuning constants (all named, all documented)
# ---------------------------------------------------------------------------

#: Human gate. The roadmap requires the operator and the independent framework
#: reviewer to approve the observed-gap window/repeat_count/threshold by
#: 2027-01-10 (roadmap Q1 2027, day 10 — Q1 is JANUARY–MARCH 2027). Until
#: an explicit approval is recorded, every rule payload carries
#: ``"pending_approval": true``. Flip to False ONLY when an approval
#: record exists (none exists yet — this is hardcoded, not inferred).
RULE_PENDING_APPROVAL = True
RULE_APPROVAL_DEADLINE = "2027-01-10"

#: Focus TTL. An explicit user focus selection older than this many days
#: is treated as expired: recommendations fall back to observed gaps and
#: say the focus expired (instead of suppressing observed gaps forever).
FOCUS_TTL_DAYS = 30

#: Rotation cap. At most this many session records are kept in the live
#: history file; older records are appended to
#: ``i06_practice_history.json.archive.jsonl`` so history can't grow
#: unboundedly.
MAX_HISTORY_RECORDS = 200

#: Trend thresholds for :func:`progress_report`. A half-split average
#: delta >= +5 is "improving", <= -5 is "declining", otherwise "steady".
TREND_IMPROVING_DELTA = 5.0
TREND_DECLINING_DELTA = -5.0


# ---------------------------------------------------------------------------
# Observed-gap rule (parameterized per the Q1 gate)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservedGapRule:
    """The approvable definition of an observed practice gap.

    Defaults encode the roadmap's baseline: the same disclosed rubric
    dimension below ``threshold`` in at least ``repeat_count`` of the
    last ``window`` completed sessions, or an explicit user selection
    (handled separately by select_focus). Pending approval by the operator and
    the independent framework reviewer by 2027-01-10 (roadmap Q1 day
    10); until then the numbers below are the unapproved baseline (see
    ``pending_approval`` in every rule payload).
    """
    window: int = 3
    repeat_count: int = 2
    threshold: int = 70

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError("window must be >= 1")
        if not 1 <= self.repeat_count <= self.window:
            raise ValueError("repeat_count must be within [1, window]")
        if not 0 <= self.threshold <= 100:
            raise ValueError("threshold must be within [0, 100]")

    def describe(self) -> str:
        return (
            f"Observed gap = a disclosed rubric dimension scoring below "
            f"{self.threshold}/100 in at least {self.repeat_count} of the "
            f"last {self.window} completed sessions, or your explicit "
            f"selection."
        )


DEFAULT_RULE = ObservedGapRule()


def _rule_payload(rule: ObservedGapRule) -> dict[str, Any]:
    """Rule dict for public outputs, including the human-gate status."""
    return {
        "window": rule.window,
        "repeat_count": rule.repeat_count,
        "threshold": rule.threshold,
        "description": rule.describe(),
        "pending_approval": RULE_PENDING_APPROVAL,
        "approval_deadline": RULE_APPROVAL_DEADLINE,
        "approval_note": (
            "the operator and the independent framework reviewer must approve "
            "window/repeat_count/threshold by 2027-01-10 (roadmap Q1 "
            "2027, day 10). Until an approval is recorded, these numbers "
            "are the unapproved baseline."
        ),
    }


# ---------------------------------------------------------------------------
# History store — provenance-aware, atomic, never silently destructive
# ---------------------------------------------------------------------------

class CorruptHistoryError(RuntimeError):
    """The history file exists but is unreadable or malformed.

    Raised by :func:`record_result` instead of silently overwriting the
    file. Pass ``recover=True`` to explicitly discard the corrupt file
    (the previous bytes are preserved as ``.bak`` first).
    """


def _read_history_store(path: Path
                        ) -> tuple[list[dict[str, Any]], bool, str]:
    """Read the history file WITH provenance.

    Returns ``(history, ok, note)``. A missing file is a fresh start
    (``ok=True``); a corrupt/unreadable/wrong-schema file returns
    ``ok=False`` and an empty list — never silently treated as absent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], True, "no history file yet (fresh start)"
    except OSError as exc:
        return [], False, f"history file unreadable: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], False, f"history file is corrupt JSON: {exc}"
    if not isinstance(data, list):
        return [], False, (
            "history file has wrong schema: expected a list of session "
            f"records, got {type(data).__name__}")
    return data, True, "history loaded"


def _read_focus_store(path: Path
                      ) -> tuple[dict[str, Any] | None, bool, str]:
    """Read the focus file WITH provenance: ``(focus, ok, note)``."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, True, "no focus selected"
    except OSError as exc:
        return None, False, f"focus file unreadable: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, False, f"focus file is corrupt JSON: {exc}"
    if not isinstance(data, dict) or data.get("dimension") not in DIMENSIONS:
        return None, False, (
            "focus file has wrong schema: expected an object with a "
            "valid 'dimension'")
    return data, True, "focus loaded"


def _save_json_atomic(path: Path, data: Any) -> Path:
    """Write JSON atomically (tmp file + rename) with a ``.bak`` backup.

    The previous file content is preserved at ``<name>.bak`` before the
    rename, so a torn write or a bad payload is recoverable. Returns the
    backup path (or None when there was no previous file).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    backup = path.with_name(path.name + ".bak")
    if path.exists():
        shutil.copy2(path, backup)
        existed = True
    else:
        existed = False
    tmp.replace(path)
    return backup if existed else None


def _resolve_history(history: list[dict[str, Any]] | None
                     ) -> tuple[list[dict[str, Any]], bool, str]:
    """History list plus load provenance.

    Explicitly supplied history is caller-owned (``ok=True``);
    otherwise the history file is read with corruption surfaced, never
    swallowed.
    """
    if history is not None:
        return list(history), True, "history supplied by caller"
    return _read_history_store(HISTORY_PATH)


def _rotate_history(history: list[dict[str, Any]],
                    path: Path) -> tuple[list[dict[str, Any]], int]:
    """Enforce MAX_HISTORY_RECORDS; archive overflow to ``.archive.jsonl``.

    Returns ``(kept_history, n_archived)``. Archived records are appended
    (never deleted) so nothing is lost by rotation.
    """
    if len(history) <= MAX_HISTORY_RECORDS:
        return history, 0
    archived = history[:-MAX_HISTORY_RECORDS]
    kept = history[-MAX_HISTORY_RECORDS:]
    archive_path = path.with_name(path.name + ".archive.jsonl")
    with archive_path.open("a", encoding="utf-8") as fh:
        for rec in archived:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return kept, len(archived)


def _save_history(history: list[dict[str, Any]],
                  path: Path | None = None) -> None:
    _save_json_atomic(path or HISTORY_PATH, history)


def _validate_score(dim: str, score: Any) -> int | None:
    """Validate one dimension score: int 0-100, or None (unscored)."""
    if score is None:
        return None
    if isinstance(score, bool) or not isinstance(score, int):
        raise ValueError(
            f"Score for {dim!r} must be an int 0-100 or None, "
            f"got {score!r}.")
    if not 0 <= score <= 100:
        raise ValueError(
            f"Score for {dim!r} must be 0-100, got {score}.")
    return score


def _validate_dimension_scores(
        dimension_scores: dict[str, Any]) -> dict[str, int]:
    """Validate dimension keys against the disclosed rubric.

    Unknown keys raise ValueError immediately — a mistyped dimension
    must never silently become a zero-drill target.
    """
    if not isinstance(dimension_scores, dict):
        raise ValueError("dimension_scores must be a dict mapping "
                         "dimension name -> 0-100 int or None")
    validated: dict[str, int] = {}
    for dim, score in dimension_scores.items():
        if dim not in DIMENSIONS:
            raise ValueError(
                f"Unknown dimension {dim!r}. Choose one of: "
                f"{', '.join(DIMENSIONS)}.")
        checked = _validate_score(dim, score)
        if checked is not None:
            validated[dim] = checked
    return validated


def record_result(
    *,
    source: str,
    label: str,
    dimension_scores: dict[str, int | None],
    overall: int | None = None,
    mode: str = "",
    session_id: str = "",
    job_id: str = "",
    completed_at: str | None = None,
    history_path: Path | None = None,
    recover: bool = False,
) -> dict[str, Any]:
    """Append one completed practice result to the longitudinal history.

    Args:
        source: which lab produced it, e.g. "mock_interview",
            "soft_skill_scenario", "ai_lab".
        label: human label, e.g. "Mock interview panel @ Hooli Labs"
            (fictional example — use a label that can't be mistaken for
            a real employer unless the session really targeted one).
        dimension_scores: {dimension: 0-100 int, or None for unscored}.
            Keys must be members of ``rubric.DIMENSIONS`` (unknown keys
            raise ValueError); values must be ints 0-100 (out-of-range
            or non-int values raise ValueError).
        overall: overall session score (int 0-100, optional).
        mode: interview mode / scenario kind / exercise track.
        session_id: originating session id.
        job_id: linked application id when the practice targets a real
            application (used to join outcome events).
        completed_at: ISO timestamp (defaults to now, UTC).
        history_path: override for tests.
        recover: when the history file is corrupt, refuse by default
            (raises :class:`CorruptHistoryError` WITHOUT touching the
            file). Pass ``recover=True`` to explicitly discard the
            corrupt file — its previous bytes are preserved as
            ``.bak`` first.

    History is capped at ``MAX_HISTORY_RECORDS`` sessions; older
    records are appended to ``<history>.archive.jsonl``.

    Returns a copy of the stored record plus ``history_ok`` (load
    provenance), ``recovered`` (whether recovery was used), and
    ``archived_records`` (how many sessions rotated out on this write).
    """
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label must be a non-empty string")
    validated_scores = _validate_dimension_scores(dimension_scores)
    if overall is not None:
        overall = _validate_score("overall", overall)
    record = {
        "source": source,
        "label": label,
        "mode": mode,
        "session_id": session_id,
        "job_id": job_id,
        "dimension_scores": validated_scores,
        "unscored_dimensions": sorted(
            k for k, v in dimension_scores.items() if v is None),
        "overall": overall,
        "completed_at": (completed_at
                         or datetime.now(timezone.utc).isoformat()),
    }
    path = history_path or HISTORY_PATH
    history, ok, note = _read_history_store(path)
    recovered = False
    if not ok and not recover:
        raise CorruptHistoryError(
            f"Refusing to overwrite corrupt history file {path}: {note}. "
            f"Recover the file from {path.with_name(path.name + '.bak')} "
            f"or pass recover=True to explicitly discard it.")
    if not ok and recover:
        history = []
        recovered = True
    history.append(record)
    history, n_archived = _rotate_history(history, path)
    _save_history(history, path)
    return {**record, "history_ok": ok or recovered,
            "recovered": recovered,
            "archived_records": n_archived}


# ---------------------------------------------------------------------------
# Outcome-event join (Initiative 01 contract; degrades gracefully)
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO timestamp to an aware datetime, or None.

    Unparseable values return None so callers can fall back — never
    raise on dirty data.
    """
    try:
        text = str(value or "").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_outcome_events() -> tuple[list[dict[str, Any]], str]:
    """Load outcome events; returns (events, provenance_note)."""
    path = BASE_DIR / OUTCOMES_FILENAME
    if not path.exists():
        return [], ("No outcome events found (Initiative 01 capture not "
                    "active) — readiness below is practice-only.")
    try:
        from outcomes import load_events  # defensive, like skill_gaps
        events = load_events(path)
    except Exception:
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    return events, (f"{len(events)} outcome events available for context."
                    if events else
                    "Outcome events file is empty — readiness below is "
                    "practice-only.")


def link_outcomes(history: list[dict[str, Any]] | None = None
                  ) -> dict[str, Any]:
    """Annotate practice history with real outcome context.

    For records carrying a ``job_id``, looks up the latest outcome
    event for that application id (interviewed / offered / rejected /
    ...). "Latest" is by parsed ISO ``recorded_at`` datetime — NOT
    lexicographic string order, which misorders non-zero-padded dates.
    Events with unparseable timestamps are ignored for ordering. Never
    invents outcomes: records without a link are labeled
    "no linked outcome". The return also carries ``history_ok`` /
    ``history_note`` provenance for the history read (a corrupt or
    unreadable default history file surfaces here, never as an
    innocent empty record list).
    """
    history, history_ok, history_note = _resolve_history(history)
    events, note = _load_outcome_events()
    latest: dict[str, dict[str, Any]] = {}
    for ev in events:
        app_id = str(ev.get("application_id") or "")
        if not app_id:
            continue
        ev_dt = _parse_dt(ev.get("recorded_at"))
        prev = latest.get(app_id)
        prev_dt = _parse_dt(prev.get("recorded_at")) if prev else None
        if prev is None or (ev_dt is not None and
                            (prev_dt is None or ev_dt >= prev_dt)):
            latest[app_id] = ev
    annotated = []
    for rec in history:
        entry = dict(rec)
        job_id = str(rec.get("job_id") or "")
        ev = latest.get(job_id) if job_id else None
        entry["linked_outcome"] = (
            {"event_type": ev.get("event_type"),
             "occurred_at": ev.get("occurred_at"),
             "role": ev.get("role", "")}
            if ev else None)
        annotated.append(entry)
    return {"records": annotated, "outcome_note": note,
            "history_ok": history_ok, "history_note": history_note}


# ---------------------------------------------------------------------------
# Observed gaps
# ---------------------------------------------------------------------------

def observed_gaps(
    history: list[dict[str, Any]] | None = None,
    rule: ObservedGapRule | None = None,
) -> dict[str, Any]:
    """Find dimensions meeting the observed-gap rule.

    Returns {"gaps": [...], "rule": {...}, "sessions_considered": n,
    "sessions_total": n, "history_ok": bool, "history_note": str}.
    Each gap carries basis "observed" and the evidence behind it:
    which sessions scored below threshold and the average. Unscored
    dimensions are never counted as gaps. ``history_ok`` is False when
    the history file could not be loaded — an empty gap list then means
    "could not read your history", never a clean bill of health.
    The rule payload includes ``pending_approval`` (human gate:
    approval by the operator + independent reviewer due 2027-01-10).
    """
    rule = rule or DEFAULT_RULE
    history, history_ok, history_note = _resolve_history(history)
    window = history[-rule.window:]
    gaps: list[dict[str, Any]] = []
    dims: set[str] = set()
    for rec in window:
        dims.update((rec.get("dimension_scores") or {}).keys())
    for dim in sorted(dims):
        below = [(rec.get("session_id", ""), rec["dimension_scores"][dim])
                 for rec in window
                 if dim in (rec.get("dimension_scores") or {})
                 and rec["dimension_scores"][dim] is not None
                 and rec["dimension_scores"][dim] < rule.threshold]
        if len(below) >= rule.repeat_count:
            scores = [s for _, s in below]
            gaps.append({
                "dimension": dim,
                "basis": "observed",
                "sessions_below_threshold": len(below),
                "sessions_considered": len(window),
                "session_ids": [sid for sid, _ in below],
                "average_score": round(sum(scores) / len(scores), 1),
                "threshold": rule.threshold,
            })
    gaps.sort(key=lambda g: (g["average_score"], g["dimension"]))
    return {
        "gaps": gaps,
        "rule": _rule_payload(rule),
        "sessions_considered": len(window),
        "sessions_total": len(history),
        "history_ok": history_ok,
        "history_note": history_note,
    }


# ---------------------------------------------------------------------------
# Explicit user selection (with staleness)
# ---------------------------------------------------------------------------

def select_focus(dimension: str,
                 focus_path: Path | None = None) -> dict[str, Any]:
    """Record the user's explicit practice focus (a user choice, not a
    system label). The selection carries ``selected_at``; selections
    older than ``FOCUS_TTL_DAYS`` are treated as expired by
    :func:`recommend_next_drill`. Returns the stored focus."""
    dim = (dimension or "").strip().lower()
    if dim not in DIMENSIONS:
        raise ValueError(
            f"Unknown dimension {dimension!r}. Choose one of: "
            f"{', '.join(DIMENSIONS)}.")
    focus = {"dimension": dim,
             "basis": "user_selected",
             "selected_at": datetime.now(timezone.utc).isoformat()}
    _save_json_atomic(focus_path or FOCUS_PATH, focus)
    return focus


def clear_focus(focus_path: Path | None = None) -> None:
    path = focus_path or FOCUS_PATH
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def current_focus(focus_path: Path | None = None
                  ) -> dict[str, Any] | None:
    data, _ok, _note = _read_focus_store(focus_path or FOCUS_PATH)
    return data


def _focus_age_days(focus: dict[str, Any] | None) -> int | None:
    """Days since the focus was selected; None if not determinable."""
    dt = _parse_dt((focus or {}).get("selected_at"))
    if dt is None:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _focus_is_stale(focus: dict[str, Any] | None) -> bool:
    age = _focus_age_days(focus)
    return age is not None and age > FOCUS_TTL_DAYS


# ---------------------------------------------------------------------------
# Progress: which behaviors improved, which still affect readiness
# ---------------------------------------------------------------------------

def progress_report(history: list[dict[str, Any]] | None = None,
                    rule: ObservedGapRule | None = None
                    ) -> dict[str, Any]:
    """Per-dimension trend across the history.

    Trend math (documented, parameterized by ``TREND_IMPROVING_DELTA``
    / ``TREND_DECLINING_DELTA``):
    - For each dimension with >= 2 scored sessions, sessions are
      ordered by ``completed_at`` parsed as an ISO datetime
      (unparseable values sort last by raw string).
    - ``first_avg`` = mean of the first half of scores,
      ``last_avg`` = mean of the second half, with
      ``half = max(1, n // 2)``; ``delta = last_avg - first_avg``.
    - ``delta >= +5`` -> "improving"; ``delta <= -5`` -> "declining";
      otherwise -> "steady". Fewer than 2 sessions ->
      "insufficient_data".

    Dimensions are labeled from the numbers only — never with invented
    weakness labels. ``history_ok`` is False when the history file
    could not be loaded (the empty report then means "could not read
    your history", not "no data").
    """
    rule = rule or DEFAULT_RULE
    history, history_ok, history_note = _resolve_history(history)
    dims: dict[str, list[tuple[str, int]]] = {}
    for rec in history:
        for dim, score in (rec.get("dimension_scores") or {}).items():
            dims.setdefault(dim, []).append(
                (rec.get("completed_at", ""), score))

    def _sort_key(item: tuple[str, int]) -> tuple[int, Any]:
        dt = _parse_dt(item[0])
        return (0, dt) if dt is not None else (1, item[0])

    report: list[dict[str, Any]] = []
    for dim in sorted(dims):
        series = sorted(dims[dim], key=_sort_key)
        scores = [s for _, s in series]
        if len(scores) < 2:
            trend = "insufficient_data"
            delta = None
        else:
            half = max(1, len(scores) // 2)
            first_avg = sum(scores[:half]) / half
            last_avg = sum(scores[-half:]) / half
            delta = round(last_avg - first_avg, 1)
            trend = ("improving" if delta >= TREND_IMPROVING_DELTA else
                     "declining" if delta <= TREND_DECLINING_DELTA else
                     "steady")
        report.append({
            "dimension": dim,
            "sessions": len(scores),
            "first_score": scores[0],
            "latest_score": scores[-1],
            "delta": delta,
            "trend": trend,
            "meets_threshold": (scores[-1] >= rule.threshold
                                if scores else None),
        })
    linked = link_outcomes(history)
    return {
        "dimensions": report,
        "outcome_note": linked["outcome_note"],
        "sessions_total": len(history),
        "history_ok": history_ok,
        "history_note": history_note,
    }


# ---------------------------------------------------------------------------
# Drill recommendation — observed gaps or user choice, never invented
# ---------------------------------------------------------------------------

#: Concrete drills mapped from a gap dimension to a specific rep in a
#: specific lab. These are *drills*, not a course list: each names the
#: module, the mode/scenario, and the single behavior to practice.
DRILLS: dict[str, list[dict[str, str]]] = {
    "structure": [
        {"lab": "mock_interview", "mode": "hiring_manager",
         "drill": "One STAR story, told twice: second telling must name "
                  "Situation, Task, Action, Result out loud in order.",
         "reps": "3 stories"},
        {"lab": "soft_skills", "mode": "coach",
         "drill": "STAR storytelling drill: answer one behavioral prompt, "
                  "then re-answer adding whichever STAR part was missing.",
         "reps": "2 prompts"},
    ],
    "evidence": [
        {"lab": "mock_interview", "mode": "adversarial",
         "drill": "'Prove it' round: every claim gets challenged — "
                  "answer with two real numbers and one named system.",
         "reps": "3 questions"},
        {"lab": "soft_skills", "mode": "coach",
         "drill": "Evidence pass: take your last answer and add one "
                  "measurable outcome from your real experience.",
         "reps": "2 answers"},
    ],
    "clarity": [
        {"lab": "mock_interview", "mode": "executive",
         "drill": "20-second version: answer each question with the "
                  "conclusion in the first two sentences.",
         "reps": "3 questions"},
        {"lab": "soft_skills", "mode": "coach",
         "drill": "Communication clarity drill: bottom line up front, "
                  "then detail; cut every jargon word.",
         "reps": "2 prompts"},
    ],
    "trade_offs": [
        {"lab": "mock_interview", "mode": "hiring_manager",
         "drill": "Decision autopsy: pick a past decision, name the "
                  "alternative you rejected, why it lost, and what it "
                  "cost.",
         "reps": "2 decisions"},
        {"lab": "soft_skills", "mode": "adversarial",
         "drill": "Tough-interviewer round on one decision: defend it "
                  "against 'what did it cost, and who paid?'",
         "reps": "1 decision"},
    ],
    "question_quality": [
        {"lab": "mock_interview", "mode": "recruiter",
         "drill": "Question workshop: write three questions for your "
                  "next interviewer about real team tensions — nothing "
                  "the careers page answers.",
         "reps": "3 questions"},
    ],
}


def recommend_next_drill(
    history: list[dict[str, Any]] | None = None,
    rule: ObservedGapRule | None = None,
    focus_path: Path | None = None,
) -> dict[str, Any]:
    """Recommend the next drill from observed gaps or user choice.

    Priority: explicit user selection first (unless it is older than
    ``FOCUS_TTL_DAYS`` — an expired focus falls back to observed gaps
    and the output says so), then observed gaps (worst average first).
    With neither, returns basis "none" and a maintenance rep —
    explicitly labeled as "no observed gaps", never as a weakness.

    A fresh focus does NOT suppress observed gaps silently: gaps
    co-occurring with the focus are surfaced as ``also_observed`` in
    the output and markdown.

    If the history file could not be loaded (``history_ok`` False), the
    output says so loudly and never reports a clean "no gaps".
    The rule payload includes ``pending_approval`` (human gate:
    approval by the operator + independent reviewer due 2027-01-10).
    """
    rule = rule or DEFAULT_RULE
    history, history_ok, history_note = _resolve_history(history)
    focus, focus_ok, focus_note = _read_focus_store(focus_path or FOCUS_PATH)
    gaps = observed_gaps(history, rule)["gaps"] if history_ok else []

    focus_age = _focus_age_days(focus)
    stale = _focus_is_stale(focus)
    expired_focus: dict[str, Any] | None = None
    also_observed: list[dict[str, Any]] = []
    drill_warnings: list[str] = []
    recommendations: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []

    if focus and not stale:
        basis = "user_selected"
        targets = [{"dimension": focus["dimension"],
                    "basis": "user_selected",
                    "selected_at": focus.get("selected_at"),
                    "focus_age_days": focus_age,
                    "stale": False}]
        # Surface co-occurring observed gaps alongside the focus —
        # never silently suppressed by a sticky selection.
        also_observed = [
            {"dimension": g["dimension"],
             "average_score": g["average_score"],
             "sessions_below_threshold": g["sessions_below_threshold"],
             "sessions_considered": g["sessions_considered"]}
            for g in gaps if g["dimension"] != focus["dimension"]]
    elif focus and stale:
        expired_focus = {"dimension": focus["dimension"],
                         "selected_at": focus.get("selected_at"),
                         "focus_age_days": focus_age}
        if gaps:
            basis = "observed"
            targets = gaps
        else:
            basis = "none"
    elif gaps:
        basis = "observed"
        targets = gaps
    else:
        basis = "none"

    for target in targets:
        dim = target["dimension"]
        drills = DRILLS.get(dim)
        if not drills:
            # Loud, not silent: a target dimension with no DRILLS entry
            # must never render as an empty section.
            drill_warnings.append(
                f"No DRILLS entry for dimension {dim!r}: cannot render a "
                f"drill for this target. Add a DRILLS entry for {dim!r} — "
                f"the target is NOT being silently dropped.")
            continue
        for drill in drills:
            recommendations.append({
                "dimension": dim,
                "basis": target["basis"],
                "lab": drill["lab"],
                "mode": drill["mode"],
                "drill": drill["drill"],
                "reps": drill["reps"],
                **({"evidence": {k: target[k] for k in
                                  ("average_score",
                                   "sessions_below_threshold",
                                   "sessions_considered", "session_ids")
                                  if k in target}}
                   if target["basis"] == "observed" else {}),
            })

    if basis == "none":
        if history_ok:
            maintenance_drill = (
                "Maintenance rep: no observed gaps in your recent "
                "sessions. Run one mixed round to keep the streak, "
                "or pick a focus dimension to target deliberately.")
        else:
            # Never claim "no observed gaps" when the history could not
            # be read — the record may be destroyed, not empty.
            maintenance_drill = (
                "Holding pattern: your practice history could not be "
                "loaded, so no recommendation can be made from your "
                "recorded sessions. Recover the history file first — "
                "do not treat this as a clean bill of health.")
        recommendations.append({
            "dimension": None,
            "basis": "none",
            "lab": "mock_interview",
            "mode": "hiring_manager",
            "drill": maintenance_drill,
            "reps": "1 round",
        })

    lines = ["# Next drill", ""]
    if not history_ok:
        lines.append(
            f"⚠️ Practice history could not be loaded ({history_note}). "
            f"The recommendation below does NOT reflect your recorded "
            f"sessions — recover the history file before trusting it.")
        lines.append("")
    if not focus_ok:
        lines.append(f"⚠️ Focus selection unreadable ({focus_note}) — "
                     f"proceeding without it.")
        lines.append("")
    if basis == "user_selected":
        target = targets[0]
        age = target.get("focus_age_days")
        age_txt = (f" (selected {age} day(s) ago; focus expires after "
                   f"{FOCUS_TTL_DAYS} days)"
                   if age is not None else "")
        lines.append(f"Your selected focus: **{target['dimension']}**"
                     f"{age_txt}.")
    elif basis == "observed":
        lines.append("Based on observed gaps "
                     f"({rule.describe()}):")
        if expired_focus:
            lines.append(
                f"Note: your focus selection "
                f"(**{expired_focus['dimension']}**, selected "
                f"{expired_focus['focus_age_days']} day(s) ago) expired "
                f"after {FOCUS_TTL_DAYS} days — showing observed gaps "
                f"instead. Re-select it to keep targeting it deliberately.")
    else:
        if expired_focus:
            if history_ok:
                lines.append(
                    f"Your focus selection (**{expired_focus['dimension']}**, "
                    f"selected {expired_focus['focus_age_days']} day(s) ago) "
                    f"expired after {FOCUS_TTL_DAYS} days. No observed gaps "
                    f"either — nothing is labeled a weakness. Suggested "
                    f"maintenance:")
            else:
                # Never claim "no observed gaps" when the history could
                # not be read — we could not assess observed gaps.
                lines.append(
                    f"Your focus selection (**{expired_focus['dimension']}**, "
                    f"selected {expired_focus['focus_age_days']} day(s) ago) "
                    f"expired after {FOCUS_TTL_DAYS} days — could not assess "
                    f"observed gaps (history unreadable). Nothing is labeled "
                    f"a weakness. Suggested maintenance:")
        elif history_ok:
            lines.append("No observed gaps and no selected focus — nothing "
                         "is labeled a weakness. Suggested maintenance:")
    lines.append("")
    for i, rec in enumerate(recommendations, 1):
        dim = rec["dimension"] or "maintenance"
        lines.append(f"{i}. **{dim}** ({rec['lab']}, {rec['mode']}): "
                     f"{rec['drill']} [{rec['reps']}]")
    if also_observed:
        lines += ["", "## Also observed (not the current target)", ""]
        for gap in also_observed:
            lines.append(
                f"- **{gap['dimension']}**: average "
                f"{gap['average_score']}, below {rule.threshold} in "
                f"{gap['sessions_below_threshold']} of "
                f"{gap['sessions_considered']} sessions — your focus "
                f"selection takes priority. Clear your focus to target "
                f"observed gaps.")
    for warning in drill_warnings:
        lines += ["", f"⚠️ {warning}"]
    return {
        "basis": basis,
        "rule": _rule_payload(rule),
        "targets": targets,
        "also_observed": also_observed,
        "expired_focus": expired_focus,
        "recommendations": recommendations,
        "drill_warnings": drill_warnings,
        "sessions_total": len(history),
        "history_ok": history_ok,
        "history_note": history_note,
        "focus_ok": focus_ok,
        "focus_note": focus_note,
        "markdown": "\n".join(lines),
    }
