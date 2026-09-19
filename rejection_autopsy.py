#!/usr/bin/env python3
"""Rejection autopsy: turn job-search outcomes into pattern analysis.

Rejections are data. This module records what happened to each
application (rejected / ghosted / offer / withdrawn, and the stage it
reached), finds where the funnel leaks, and turns the patterns into
concrete fixes tied to the repo's own capabilities (grill, tailoring,
mock interviews, follow-ups, ...).

``record_outcome(job_id, outcome, stage, notes="")``
    Log what happened. One record per job id (re-recording the same
    job updates it — an outcome can change as the process moves).

``autopsy()``
    Deterministic pattern analysis over the store: loss rate by stage
    (where the funnel leaks), ghosting rate, common note keywords, and
    time-to-decision stats. Pure function of the records.

``diagnose()``
    Turns patterns into suggested fixes, each tied to a repo module.
    Sibling modules are imported defensively — a suggestion degrades
    gracefully (``available: False``) when its module is absent.
    Tone is frank but kind: patterns are diagnosed, the user is never
    blamed.

Stdlib only. The store is ``outcomes.json`` next to this file; tests
redirect ``OUTCOMES_FILE`` to a tmp dir.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("veto-mcp.rejection_autopsy")

BASE_DIR = Path(__file__).resolve().parent
OUTCOMES_FILE = BASE_DIR / "outcomes.json"

#: Valid outcomes for a job.
OUTCOMES = ("rejected", "ghosted", "offer", "withdrawn")

#: Funnel stages, in order. "applied" is the top of the funnel.
STAGES = ("applied", "screening", "interview", "final", "offer")
STAGE_ORDER = list(STAGES)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")

_STOPWORDS = frozenset(
    """
    the a an and or of to in for on with was were are is be been being
    at as by from that this it its they them their he she his her we our
    you your i me my not no but had has have do does did will would can
    could should very more most just about into over after before during
    than then there here when where which who whom what how all any both
    each few other some such only own same so too until while because
    though although however also per via within without really quite much
    many back out up down off through got get got told said say says
    """.split()
)

#: Keyword stems mapped to a repo capability that addresses them.
_KEYWORD_FIXES: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (("salary", "compensation", "pay", "offer-letter", "negotiat"),
     "Compensation came up repeatedly in your notes.",
     "Practice anchoring and counters in the negotiation sim, then "
     "compare competing offers side by side.",
     "soft_skills"),
    (("experience", "skills", "background", "qualified", "years"),
     "Notes keep pointing at experience or skill fit.",
     "Run a skill-gap analysis against your target jobs and close the "
     "top gaps with focused practice.",
     "skill_gaps"),
    (("culture", "fit", "team", "vibe"),
     "Fit/culture keeps coming up.",
     "Warm intros beat cold applications — work your referral radar "
     "and talk to people inside before you apply.",
     "referrals"),
)

_module_cache: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_outcomes() -> list[dict[str, Any]]:
    try:
        raw = OUTCOMES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("outcomes file unreadable, treating as empty: %s", exc)
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _save_outcomes(records: list[dict[str, Any]]) -> None:
    OUTCOMES_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(OUTCOMES_FILE.parent), prefix="outcomes-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(records, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp_name, OUTCOMES_FILE)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _coerce_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def record_outcome(
    job_id: str,
    outcome: str,
    stage: str,
    notes: str = "",
    applied_at: Any = None,
) -> dict[str, Any]:
    """Record (or update) what happened with one job.

    ``outcome`` must be one of ``OUTCOMES``; ``stage`` one of ``STAGES``.
    ``applied_at`` is optional (ISO string or datetime) and powers the
    time-to-decision stats.
    """
    job_id = (job_id or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    outcome = (outcome or "").strip().lower()
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    stage = (stage or "").strip().lower()
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}, got {stage!r}")

    records = _load_outcomes()
    record = {
        "job_id": job_id,
        "outcome": outcome,
        "stage": stage,
        "notes": str(notes or "").strip(),
        "applied_at": _coerce_iso(applied_at),
        "recorded_at": _now_iso(),
    }
    for i, existing in enumerate(records):
        if existing.get("job_id") == job_id:
            records[i] = record
            break
    else:
        records.append(record)
    _save_outcomes(records)
    return record


# ---------------------------------------------------------------------------
# Pattern analysis
# ---------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _top_keywords(records: list[dict[str, Any]], limit: int = 5) -> list[tuple[str, int]]:
    from collections import Counter

    counts: Counter[str] = Counter()
    for record in records:
        for token in _TOKEN_RE.findall(str(record.get("notes", "")).lower()):
            token = token.strip("-")
            if len(token) < 4 or token in _STOPWORDS or token.isdigit():
                continue
            counts[token] += 1
    return counts.most_common(limit)


def _days_to_decision(record: dict[str, Any]) -> float | None:
    applied = _coerce_iso(record.get("applied_at"))
    recorded = _coerce_iso(record.get("recorded_at"))
    if not applied or not recorded:
        return None
    delta = (
        datetime.fromisoformat(recorded) - datetime.fromisoformat(applied)
    ).total_seconds() / 86400.0
    return delta if delta >= 0 else None


def autopsy(outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Analyze outcome patterns. Pure function of the records.

    Returns loss/rejection rates by stage, ghosting and offer rates, the
    stage where the funnel leaks most, common note keywords, and
    time-to-decision medians.
    """
    records = list(outcomes) if outcomes is not None else _load_outcomes()
    total = len(records)

    by_outcome = {name: 0 for name in OUTCOMES}
    for record in records:
        if record.get("outcome") in by_outcome:
            by_outcome[record["outcome"]] += 1

    by_stage: dict[str, dict[str, Any]] = {}
    for stage in STAGE_ORDER:
        at_stage = [r for r in records if r.get("stage") == stage]
        n = len(at_stage)
        lost = sum(1 for r in at_stage if r.get("outcome") in ("rejected", "ghosted"))
        rej = sum(1 for r in at_stage if r.get("outcome") == "rejected")
        by_stage[stage] = {
            "total": n,
            "rejected": rej,
            "ghosted": sum(1 for r in at_stage if r.get("outcome") == "ghosted"),
            "loss_rate": (lost / n) if n else None,
            "rejection_rate": (rej / n) if n else None,
        }

    candidates = [
        (stage, info) for stage, info in by_stage.items() if info["total"] >= 2
    ]
    biggest_leak = None
    if candidates:
        candidates.sort(
            key=lambda kv: (kv[1]["loss_rate"] or 0.0, kv[1]["total"]),
            reverse=True,
        )
        if (candidates[0][1]["loss_rate"] or 0.0) > 0:
            biggest_leak = candidates[0][0]

    ghosted = by_outcome["ghosted"]
    offers = by_outcome["offer"]
    decision_days: dict[str, list[float]] = {
        "rejected": [], "ghosted": [], "offer": [], "withdrawn": [],
    }
    for record in records:
        days = _days_to_decision(record)
        if days is not None and record.get("outcome") in decision_days:
            decision_days[record["outcome"]].append(days)

    result = {
        "total": total,
        "by_outcome": by_outcome,
        "by_stage": by_stage,
        "offer_rate": (offers / total) if total else None,
        "ghosting_rate": (ghosted / total) if total else None,
        "biggest_leak": biggest_leak,
        "top_note_keywords": _top_keywords(records),
        "time_to_decision_days": {
            name: _median(days) for name, days in decision_days.items()
        },
    }
    result["markdown"] = _render_autopsy(result)
    return result


def _pct(value: float | None) -> str:
    return f"{value * 100:.0f}%" if value is not None else "n/a"


def _render_autopsy(a: dict[str, Any]) -> str:
    lines = ["# Rejection autopsy", ""]
    if not a["total"]:
        lines.append(
            "No outcomes recorded yet. Log a few with "
            "`autopsy record` and the patterns will show up here."
        )
        return "\n".join(lines)
    lines.append(
        f"**{a['total']}** outcomes: "
        + ", ".join(f"{n} {name}" for name, n in a["by_outcome"].items())
        + "."
    )
    lines.append(
        f"Offer rate **{_pct(a['offer_rate'])}** · "
        f"ghosting rate **{_pct(a['ghosting_rate'])}**."
    )
    lines.append("")
    lines.append("## Where the funnel leaks")
    for stage in STAGE_ORDER:
        info = a["by_stage"][stage]
        if not info["total"]:
            continue
        marker = " ← biggest leak" if stage == a["biggest_leak"] else ""
        lines.append(
            f"- **{stage}**: {info['total']} reached, "
            f"loss rate {_pct(info['loss_rate'])} "
            f"({info['rejected']} rejected, {info['ghosted']} ghosted){marker}"
        )
    keywords = a["top_note_keywords"]
    if keywords:
        lines.append("")
        lines.append("## Recurring themes in your notes")
        lines.append(
            ", ".join(f"**{word}** ({count})" for word, count in keywords)
        )
    timing = {
        name: v for name, v in a["time_to_decision_days"].items() if v is not None
    }
    if timing:
        lines.append("")
        lines.append("## Time to decision (median days, applied → recorded)")
        for name, days in timing.items():
            lines.append(f"- {name}: **{days:.0f} days**")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diagnosis → suggested fixes
# ---------------------------------------------------------------------------


def _module_available(name: str) -> bool:
    if name not in _module_cache:
        _module_cache[name] = importlib.util.find_spec(name) is not None
    return _module_cache[name]


def _suggestion(
    signal: str,
    finding: str,
    fix: str,
    modules: list[tuple[str, str]],
    severity: str = "medium",
) -> dict[str, Any]:
    return {
        "signal": signal,
        "finding": finding,
        "fix": fix,
        "severity": severity,
        "modules": [
            {"name": name, "cli": cli, "available": _module_available(name)}
            for name, cli in modules
        ],
    }


def diagnose(outcomes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Turn autopsy patterns into suggested fixes.

    Each suggestion names the repo module that addresses it. Findings are
    frank but kind — the pattern is the problem, never the person.
    """
    records = list(outcomes) if outcomes is not None else _load_outcomes()
    a = autopsy(records)
    suggestions: list[dict[str, Any]] = []

    if a["total"] < 5:
        suggestions.append(
            _suggestion(
                "low_volume",
                "Fewer than 5 outcomes logged — there isn't enough data "
                "for real patterns yet, and that's completely normal "
                "early in a search.",
                "Keep applying and log every outcome (yes, even the "
                "ghosting). The autopsy gets sharper with each one.",
                [],
                severity="low",
            )
        )
    else:
        screen = a["by_stage"]["screening"]
        if screen["total"] >= 3 and (screen["loss_rate"] or 0) >= 0.5:
            suggestions.append(
                _suggestion(
                    "screening_leak",
                    "Most applications are dying at the screening stage. "
                    "That's usually a resume-vs-posting mismatch, not a "
                    "you problem — recruiters spend seconds here.",
                    "Put your resume through the grill to find what a "
                    "screener would flag, then tighten the tailoring so "
                    "each application mirrors the posting's language.",
                    [("grill", "grill"), ("tailor", "tailor")],
                    severity="high",
                )
            )
        interview = a["by_stage"]["interview"]
        if interview["total"] >= 3 and (interview["loss_rate"] or 0) >= 0.5:
            suggestions.append(
                _suggestion(
                    "interview_leak",
                    "You're getting conversations but they're stalling in "
                    "interviews. Getting this far means the resume works — "
                    "the gap is reps, not credentials.",
                    "Run mock interviews for your target roles and drill "
                    "the weak spots (STAR stories, specificity) until the "
                    "answers feel boring to you.",
                    [("mock_interview", "mock-start"), ("soft_skills", "soft-skills")],
                    severity="high",
                )
            )
        final = a["by_stage"]["final"]
        if final["total"] >= 2 and (final["loss_rate"] or 0) >= 0.5:
            suggestions.append(
                _suggestion(
                    "final_leak",
                    "Finals are close — you're a finalist, which means "
                    "they already want you. Late-stage losses are usually "
                    "about presence and closing, not competence.",
                    "Practice executive presence drills and rehearse your "
                    "close: the questions you'll ask them and how you'll "
                    "talk compensation.",
                    [("soft_skills", "soft-skills")],
                    severity="high",
                )
            )
        if (a["ghosting_rate"] or 0) >= 0.3:
            suggestions.append(
                _suggestion(
                    "ghosting",
                    f"{_pct(a['ghosting_rate'])} of your applications went "
                    "quiet. Ghosting is rude and common — it's a process "
                    "problem on their end, and a follow-up problem you "
                    "can fix on yours.",
                    "Check which applications have gone stale and send "
                    "polite, stage-aware follow-ups. One nudge revives "
                    "more pipelines than you'd expect.",
                    [("followup", "followup")],
                    severity="medium",
                )
            )
        late_ghosts = sum(
            1 for r in records
            if r.get("outcome") == "ghosted" and r.get("stage") in ("final", "offer")
        )
        if late_ghosts:
            suggestions.append(
                _suggestion(
                    "late_ghosting",
                    "You were ghosted after a final round or offer stage. "
                    "That stings — and it's the exact moment to practice "
                    "closing so the next one doesn't slip.",
                    "Rehearse the final-round close with the negotiation "
                    "sim: how you'd handle the offer call, the pause, "
                    "and the ask.",
                    [("soft_skills", "soft-skills")],
                    severity="medium",
                )
            )

    keyword_hits = {word for word, _ in a["top_note_keywords"]}
    for stems, finding, fix, module in _KEYWORD_FIXES:
        if any(
            any(word.startswith(stem) for stem in stems) for word in keyword_hits
        ):
            cli = {
                "soft_skills": "soft-skills",
                "skill_gaps": "skill-gaps",
                "referrals": "referrals",
            }.get(module, module)
            suggestions.append(
                _suggestion("note_theme", finding, fix, [(module, cli)],
                            severity="medium")
            )

    if a["total"] >= 5 and (a["offer_rate"] or 0) >= 0.25 and not suggestions:
        suggestions.append(
            _suggestion(
                "momentum",
                f"Offer rate is {_pct(a['offer_rate'])} across "
                f"{a['total']} outcomes — that's a healthy pipeline.",
                "Keep the volume up and keep logging outcomes. When an "
                "offer lands, compare it properly before signing.",
                [("offer_compare", "offers")],
                severity="low",
            )
        )

    summary = (
        "Not enough data yet — log more outcomes."
        if a["total"] < 5
        else (
            f"Biggest leak: **{a['biggest_leak']}**."
            if a["biggest_leak"]
            else "No single stage dominates the losses."
        )
    )
    result = {"summary": summary, "suggestions": suggestions}
    result["markdown"] = _render_diagnosis(result)
    return result


def _render_diagnosis(d: dict[str, Any]) -> str:
    lines = ["# Autopsy diagnosis", "", d["summary"], ""]
    if not d["suggestions"]:
        lines.append("Nothing actionable yet — the patterns need more data.")
        return "\n".join(lines)
    for s in d["suggestions"]:
        lines.append(f"## {s['signal']} ({s['severity']})")
        lines.append("")
        lines.append(s["finding"])
        lines.append("")
        lines.append(f"**Fix:** {s['fix']}")
        for mod in s["modules"]:
            status = "available" if mod["available"] else "not installed"
            lines.append(f"- `{mod['cli']}` ({status})")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the autopsy MCP tools on an MCP server instance."""
    _impl_record = globals()["record_outcome"]
    _impl_autopsy = globals()["autopsy"]
    _impl_diagnose = globals()["diagnose"]

    @mcp.tool()
    def record_outcome(
        job_id: str, outcome: str, stage: str, notes: str = ""
    ) -> dict:
        """Log what happened with a job application.

        Args:
            job_id: The job id from search output.
            outcome: One of: rejected, ghosted, offer, withdrawn.
            stage: Furthest stage reached: applied, screening, interview,
                final, offer.
            notes: Optional free-text note (feeds keyword analysis).

        Returns:
            The stored outcome record.
        """
        return _impl_record(job_id, outcome, stage, notes)

    @mcp.tool()
    def rejection_autopsy() -> dict:
        """Analyze outcome patterns: loss rate by stage, ghosting rate,
        recurring note themes, and time-to-decision stats.

        Returns:
            Dict with per-stage stats, rates, biggest leak, keywords,
            timing medians, and markdown.
        """
        return _impl_autopsy()

    @mcp.tool()
    def diagnose_rejection_patterns() -> dict:
        """Turn autopsy patterns into suggested fixes, each tied to a
        repo capability (grill, tailoring, mock interviews, ...).

        Returns:
            Dict with summary, suggestions, and markdown. Frank but kind;
            the pattern is diagnosed, never the person.
        """
        return _impl_diagnose()


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(result.get("markdown") or json.dumps(result, indent=2, default=str))


def cmd_autopsy(args: Any) -> int:
    """CLI handler for `autopsy`."""
    as_json = getattr(args, "json", False)
    action = getattr(args, "action", "report") or "report"
    if action == "record":
        _print_result(
            record_outcome(
                getattr(args, "job_id", ""),
                getattr(args, "outcome", ""),
                getattr(args, "stage", ""),
                getattr(args, "notes", "") or "",
                getattr(args, "applied_at", None),
            ),
            as_json,
        )
    elif action == "diagnose":
        _print_result(diagnose(), as_json)
    else:
        _print_result(autopsy(), as_json)
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `autopsy` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    parser = subparsers.add_parser(
        "autopsy", help="Analyze rejection patterns and get fixes."
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="report",
        choices=("record", "report", "diagnose"),
        help="record: log an outcome; report: pattern analysis (default); "
        "diagnose: suggested fixes.",
    )
    parser.add_argument("--job-id", help="Job id (for record).")
    parser.add_argument(
        "--outcome",
        choices=OUTCOMES,
        help="Outcome (for record): " + ", ".join(OUTCOMES) + ".",
    )
    parser.add_argument(
        "--stage",
        choices=STAGES,
        help="Furthest stage reached (for record): " + ", ".join(STAGES) + ".",
    )
    parser.add_argument("--notes", default="", help="Free-text note (for record).")
    parser.add_argument(
        "--applied-at", default=None, help="When you applied, ISO date (for record)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    return {"autopsy": cmd_autopsy}
