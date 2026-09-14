#!/usr/bin/env python3
"""Career graph (Initiative 04, Epic 4).

A **read-model** over Initiative 01's outcome events: it trends
recurring strengths, gaps, seniority, and target-role movement over
time. 04 never writes outcome events — it reads them.

Contract posture (per the program brief):
* Built against 01's *contracted* ``outcome-min-v0`` schema
  (``docs/outcome-event-contract.md``): ``event_id``,
  ``application_id``, ``event_type`` (11 canonical types),
  ``occurred_at``, ``source``, ``role``, ``provenance``,
  ``schema_version``.
* Unknown ``schema_version`` → the graph **refuses to render** rather
  than mis-rendering (returns ``{"rendered": False, "reason": ...}``).
* All aggregates are derivable without new PII: requirement labels,
  counts, and score bands only.

Inputs:
* ``events`` — outcome events (list of dicts). ``occurred_at`` must be
  ISO-8601 (per the contract it is a UTC timestamp, but mixed offsets
  such as ``Z`` / ``+00:00`` / ``-04:00`` are parsed and compared as
  instants — sorting is never lexicographic on the raw string).
  Unparseable ``occurred_at`` values sort first as visible data gaps.
* ``fit_results`` — optional mapping of ``application_id``/``job_id``
  → ``veto/fit-result/v1`` docs (from :mod:`initiatives.i04.fit_explain`).
  Joined on ``application_id`` ↔ ``job_id`` per 01's contract demand.
* ``include_superseded`` — pass ``True`` to keep the full correction
  audit trail (mirrors 01's reader flag). Default ``False``: superseded
  originals are excluded, matching 01's readers.

Outputs (``build_career_graph``):
* ``timeline`` — per application: events in time order, current state.
* ``strengths`` — requirement labels most often ``supported``.
* ``gaps`` — requirement labels most often ``gap``, with first/last
  seen dates (recurring gaps are the coaching signal for 06).
* ``seniority_trend`` — seniority label of applied roles over time
  (controlled vocabulary; see spike open question 3, resolved here).
* ``conversion_by_fit_band`` — qualified-reply (``replied`` events)
  rate per fit-score band, with sample-size honesty: bands with
  fewer than 5 applications are labeled ``insufficient_data``.
* ``limitations`` — always shipped; includes the Q4-evidence-threshold
  assumption flag, the corrections posture, and the timestamp-sorting
  guarantee.

Decision record (resolved in this module; spike §1d unless noted):

* Corrections. Genuine options: (a) show every event, including
  superseded originals, as an "honest raw view" of everything 01
  stored; (b) exclude superseded events by default, matching 01's
  reader behavior. Chose (b): it trades audit-trail visibility for
  reader consistency — one correction would otherwise render as two
  conflicting outcomes (e.g. a double-counted "replied" rate), and
  every other 01 consumer already treats superseded events as
  invisible; what we save: a divergence between 04's timeline and
  every 01 reader. ``include_superseded=True`` keeps the full trail.
* Refuse-to-render posture. Genuine options: (a) best-effort render on
  unknown schema versions (parse what we can, mark the rest);
  (b) refuse to render outright with a ``reason``. Chose (b) per
  spike §1d: it trades partial signal on schema drift for a guarantee
  of never mis-rendering — a half-built trend read as truth is worse
  for a career decision than an explicit refusal; what we save:
  silent wrongness the day 01's contract evolves. Revisit only if 04
  ever learns to version-migrate events itself.

Pure function; deterministic; stdlib only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

#: Outcome schema versions this read-model understands. Anything else
#: refuses to render (spike §1d: never mis-render on schema drift).
SUPPORTED_SCHEMA_VERSIONS = ("outcome-min-v0",)

#: Controlled seniority vocabulary (spike open question 3).
SENIORITY_LEVELS = (
    "intern",
    "junior",
    "mid",
    "senior",
    "staff",
    "principal",
    "executive",
)

_TITLE_SENIORITY: tuple[tuple[str, str], ...] = (
    # Most-specific-match-wins: the longest keyword present in a title
    # decides; order below is the tie-break (earlier wins) between
    # equal-length keywords, e.g. "Chief of Staff" → executive.
    ("vice president", "executive"),
    ("chief", "executive"),
    ("svp", "executive"),
    ("head of", "executive"),
    ("founder", "executive"),
    ("vp", "executive"),
    ("principal", "principal"),
    ("manager", "senior"),
    ("director", "senior"),
    ("senior", "senior"),
    ("staff", "staff"),
    ("lead", "senior"),
    ("junior", "junior"),
    ("intern", "intern"),
)

#: Event types that count as forward progress toward an offer.
_PROGRESS_TYPES = ("replied", "screened", "interviewed", "offered", "accepted")
#: Terminal states (no further progress expected).
_TERMINAL_TYPES = ("accepted", "rejected", "withdrawn", "stale")

#: Minimum applications in a fit band before a conversion rate is
#: reported; below it the band is labeled insufficient_data.
MIN_BAND_SAMPLE = 5


def label_seniority(role_title: str) -> str:
    """Map a free-text role title to the controlled seniority vocabulary.

    The most specific (longest) keyword present in the title wins — so
    "Senior VP of Sales" is senior (``senior`` beats ``vp``), "SVP of
    Engineering" is executive, and "Chief of Staff" is executive
    (``chief`` wins the length tie with ``staff``). Returns ``"mid"``
    when no seniority keyword is present — labeled as the default,
    never inferred beyond the keyword list.
    """
    lowered = (role_title or "").lower()
    best: tuple[int, int, str] | None = None
    for index, (keyword, label) in enumerate(_TITLE_SENIORITY):
        if re.search(r"\b" + re.escape(keyword) + r"\b", lowered):
            # Longest keyword first; tuple position breaks ties.
            candidate = (len(keyword), -index, label)
            if best is None or candidate > best:
                best = candidate
    return best[2] if best is not None else "mid"


def _parse_ts(raw: Any) -> "datetime | None":
    """Parse an ISO-8601 timestamp to an aware UTC datetime.

    Naive values are assumed UTC (the contract emits UTC timestamps).
    Returns ``None`` for missing or unparseable values.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ts_sort_key(raw: Any) -> tuple[int, float, str]:
    """Total, deterministic order key for ``occurred_at`` values.

    Compares by instant (mixed ``Z``/``+00:00``/non-UTC-offset formats
    sort by when they happened, not by raw string). Unparseable values
    sort first — a loud data gap — and tie-break on the raw string.
    """
    text = raw if isinstance(raw, str) else ""
    parsed = _parse_ts(text)
    if parsed is None:
        return (0, 0.0, text)
    return (1, parsed.timestamp(), text)


def _extreme_time(times: list[str], extreme: Any) -> str | None:
    """Return the min (first) or max (last) of raw occurred strings by
    parsed instant — immune to input order and mixed timestamp formats.
    """
    if not times:
        return None
    return extreme(times, key=_ts_sort_key)


def build_career_graph(
    events: list[dict[str, Any]],
    fit_results: dict[str, dict[str, Any]] | None = None,
    include_superseded: bool = False,
) -> dict[str, Any]:
    """Build the career-graph read-model from outcome events.

    ``include_superseded=True`` keeps superseded correction originals
    in the timeline (01's ``include_superseded=True``); the default
    excludes them, matching 01's readers.
    """
    fit_results = fit_results or {}

    unknown = {
        str(e.get("schema_version")) for e in events if isinstance(e, dict)
    } - set(SUPPORTED_SCHEMA_VERSIONS)
    if unknown:
        return {
            "rendered": False,
            "reason": (
                "Refusing to render: outcome events use unsupported "
                f"schema version(s) {sorted(unknown)}; this read-model "
                f"understands {list(SUPPORTED_SCHEMA_VERSIONS)}. Not "
                "mis-rendering on schema drift."
            ),
        }

    # Exclude superseded correction originals, matching 01's reader
    # default (contract §Corrections): an event whose event_id appears
    # in another event's ``corrects`` is invisible unless the caller
    # opts into the audit trail.
    superseded_ids = {
        str(e.get("corrects")) for e in events
        if isinstance(e, dict) and e.get("corrects")
    }
    if not include_superseded:
        events = [
            e for e in events
            if str(e.get("event_id") or "") not in superseded_ids
        ]

    # Group events per application; sort by parsed timestamp (never
    # lexicographically on the raw string) so mixed ISO-8601 formats
    # and non-UTC offsets order by instant.
    by_app: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        app_id = str(event.get("application_id") or "")
        if not app_id:
            continue
        by_app.setdefault(app_id, []).append(event)
    for app_events in by_app.values():
        app_events.sort(key=lambda e: _ts_sort_key(e.get("occurred_at")))

    timeline: dict[str, Any] = {}
    for app_id, app_events in by_app.items():
        states = [e.get("event_type") for e in app_events]
        current = states[-1] if states else None
        timeline[app_id] = {
            "role": app_events[-1].get("role", ""),
            "events": [
                {
                    "event_type": e.get("event_type"),
                    "occurred_at": e.get("occurred_at"),
                    "source": e.get("source"),
                }
                for e in app_events
            ],
            "current_state": current,
            "terminal": current in _TERMINAL_TYPES,
            "progressed": any(s in _PROGRESS_TYPES for s in states),
        }

    # Strengths / gaps from the joined fit results.
    strength_counts: dict[str, int] = {}
    gap_times_seen: dict[str, list[str]] = {}
    gap_counts: dict[str, int] = {}
    seniority_points: list[dict[str, Any]] = []
    band_stats: dict[str, dict[str, int]] = {
        "high (>=75)": {"n": 0, "replied": 0},
        "mid (60-74)": {"n": 0, "replied": 0},
        "low (<60)": {"n": 0, "replied": 0},
    }

    for app_id, info in timeline.items():
        first_at = info["events"][0]["occurred_at"] if info["events"] else None
        seniority_points.append(
            {
                "application_id": app_id,
                "role": info["role"],
                "seniority": label_seniority(info["role"]),
                "occurred_at": first_at,
            }
        )
        result = fit_results.get(app_id)
        if not isinstance(result, dict):
            continue
        ev_map = result.get("evidence_map") or {}
        for entry in ev_map.get("entries", []):
            if not isinstance(entry, dict):
                continue
            requirement = entry.get("requirement") or {}
            label = requirement.get("text")
            if not label:
                continue
            occurred = info["events"][0]["occurred_at"] if info["events"] else ""
            if entry["status"] == "supported":
                strength_counts[label] = strength_counts.get(label, 0) + 1
            elif entry["status"] == "gap":
                gap_counts[label] = gap_counts.get(label, 0) + 1
                if occurred:
                    # Collect every sighting; first/last are min/max by
                    # parsed time below — never input order, so backfilled
                    # or out-of-order applications can't invert the range.
                    gap_times_seen.setdefault(label, []).append(occurred)
        score = result.get("fit_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            band = (
                "high (>=75)" if score >= 75
                else "mid (60-74)" if score >= 60
                else "low (<60)"
            )
            band_stats[band]["n"] += 1
            if any(
                e["event_type"] == "replied" for e in info["events"]
            ):
                band_stats[band]["replied"] += 1

    seniority_points.sort(key=lambda p: _ts_sort_key(p["occurred_at"]))

    strengths = sorted(
        strength_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    gaps = sorted(
        (
            {
                "requirement": label,
                "times_seen": count,
                "first_seen": _extreme_time(gap_times_seen.get(label, []), min),
                "last_seen": _extreme_time(gap_times_seen.get(label, []), max),
            }
            for label, count in gap_counts.items()
        ),
        key=lambda g: (-g["times_seen"], g["requirement"]),
    )

    conversion_by_fit_band = {}
    for band, stats in band_stats.items():
        if stats["n"] < MIN_BAND_SAMPLE:
            conversion_by_fit_band[band] = {
                "label": "insufficient_data",
                "n": stats["n"],
                "detail": (
                    f"only {stats['n']} application(s) in this band; "
                    f"need >= {MIN_BAND_SAMPLE} for a rate"
                ),
            }
        else:
            conversion_by_fit_band[band] = {
                "label": "reported",
                "n": stats["n"],
                "reply_rate": round(stats["replied"] / stats["n"], 3),
            }

    return {
        "rendered": True,
        "schema_versions_seen": sorted(
            {str(e.get("schema_version")) for e in events if isinstance(e, dict)}
        ),
        "applications": len(timeline),
        "timeline": timeline,
        "strengths": [
            {"requirement": label, "times_supported": count}
            for label, count in strengths
        ],
        "gaps": gaps,
        "seniority_trend": seniority_points,
        "conversion_by_fit_band": conversion_by_fit_band,
        "limitations": [
            "Read-only over Initiative 01 outcome events; 04 never "
            "writes events.",
            "Strength/gap trends need joined fit results; applications "
            "without a fit result contribute to the timeline only.",
            "Conversion rates need >= 5 applications per fit band; "
            "smaller bands are labeled insufficient_data, never given "
            "a rate.",
            "Below the Q4 evidence threshold (>= 20 resolved outcomes "
            "incl. >= 4 qualified replies), all scores are static and "
            "conversion bands will mostly read insufficient_data — "
            "the graph degrades gracefully instead of inventing signal.",
            "Corrected events supersede their originals: the timeline "
            "excludes superseded events by default, matching 01's "
            "readers (include_superseded=True keeps the audit trail).",
            "Event ordering parses occurred_at as ISO-8601 instants "
            "(mixed Z/+00:00/non-UTC offsets compare by when they "
            "happened, never by raw string); unparseable timestamps "
            "sort first as visible data gaps, never silently dropped.",
        ],
    }
