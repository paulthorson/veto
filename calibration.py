#!/usr/bin/env python3
"""Offline calibration harness for job-apply-mcp (Initiative 02).

Evaluates the Initiative 02 evidence gate against the ``outcome-min-v0``
event store and produces weight reports — **without ever applying new
weights**. Personalized weights stay pinned OFF unless the gate passes:

* >= 20 resolved outcomes including >= 4 qualified replies, and
* no role-family slice below 10 resolved / 2 qualified replies.

Operational definitions (documented, heuristic):
* **resolved outcome**: an application whose latest canonical event is one
  of replied / screened / interviewed / offered / accepted / rejected /
  withdrawn (i.e. we learned something beyond applied/discovered/
  shortlisted/stale).
* **qualified reply**: a ``replied`` event for an application that was
  later followed by screened / interviewed / offered / accepted. This is
  a temporal-sequence definition only — no causal claim is made that the
  reply *caused* the progression.
* **role family**: normalized from the event's role/title string by
  keyword heuristics (engineering, design, data, product, other).

  Slice rule interpretation: only families that actually appear in the
  event store are evaluated — a family with no events has nothing to
  personalize and cannot trip the gate; a family with *some* events must
  meet the 10 resolved / 2 qualified-reply minimums before its slice may
  be personalized.

Public API:
* :func:`evidence_gate_status` — the gate verdict + counts (used by the
  daily brief's provenance display).
* :func:`run_harness` — full offline run: gate status, per-family slices,
  first weight report, confidence/uncertainty.
* :func:`weight_report` — what personalized weights WOULD look like given
  linked component-score evidence; pure report, never applied.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.calibration")

BASE_DIR = Path(__file__).resolve().parent

#: Gate thresholds (roadmap Initiative 02 exit gate).
GATE_MIN_RESOLVED = 20
GATE_MIN_QUALIFIED_REPLIES = 4
SLICE_MIN_RESOLVED = 10
SLICE_MIN_QUALIFIED_REPLIES = 2

#: Event types that count as "we learned something" for gate purposes.
RESOLVED_EVENT_TYPES = frozenset({
    "replied", "screened", "interviewed", "offered",
    "accepted", "rejected", "withdrawn",
})

#: A reply counts as qualified when the application was later followed by
#: one of these (temporal sequence only; no causal claim).
QUALIFYING_FOLLOWUPS = frozenset({
    "screened", "interviewed", "offered", "accepted",
})

_ROLE_FAMILY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("engineering", ("engineer", "developer", "software", "backend",
                     "frontend", "fullstack", "devops", "sre", "platform",
                     "mobile", "ios", "android")),
    ("data", ("data", "analyst", "analytics", "scientist", "machine learning",
              "ml ", "ai ")),
    ("design", ("design", "ux", "ui", "visual", "interaction")),
    ("product", ("product manager", "program manager", "project manager")),
)


def role_family(role: Any) -> str:
    """Heuristic role-family normalization (documented as heuristic)."""
    text = f" {str(role or '').lower()} "
    for family, keywords in _ROLE_FAMILY_KEYWORDS:
        if any(kw in text for kw in keywords):
            return family
    return "other"


def _latest_by_application(
    events: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_app: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        app_id = str(ev.get("application_id") or "")
        if app_id:
            by_app.setdefault(app_id, []).append(ev)
    return by_app


def _event_order_key(ev: dict[str, Any]) -> str:
    return str(ev.get("occurred_at") or ev.get("recorded_at") or "")


def analyze_events(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute resolved/qualified counts overall and per role family."""
    by_app = _latest_by_application(events)
    resolved = 0
    qualified = 0
    families: dict[str, dict[str, int]] = {}
    for app_id, evs in by_app.items():
        ordered = sorted(evs, key=_event_order_key)
        types = [str(e.get("event_type")) for e in ordered]
        latest = types[-1] if types else ""
        if latest not in RESOLVED_EVENT_TYPES:
            continue
        resolved += 1
        fam = role_family(ordered[-1].get("role") or ordered[0].get("role"))
        slot = families.setdefault(
            fam, {"resolved": 0, "qualified_replies": 0})
        slot["resolved"] += 1
        if "replied" in types and any(
            t in QUALIFYING_FOLLOWUPS for t in types[types.index("replied") + 1:]
        ):
            qualified += 1
            slot["qualified_replies"] += 1
    return {
        "resolved_outcomes": resolved,
        "qualified_replies": qualified,
        "families": families,
    }


def evidence_gate_status(
    events_path: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate the Initiative 02 evidence gate (read-only).

    Returns {gate_met, resolved_outcomes, qualified_replies, families,
    slices_below_minimum, weights_source, reason}. ``gate_met`` is True
    only when the global thresholds AND every non-empty family slice meet
    their minimums.
    """
    try:
        import outcomes

        events = outcomes.load_events(events_path)
    except ImportError as exc:
        log.debug("outcomes module unavailable: %s", exc)
        events = []
    analysis = analyze_events(events)
    resolved = analysis["resolved_outcomes"]
    qualified = analysis["qualified_replies"]
    below = [
        fam for fam, counts in analysis["families"].items()
        if counts["resolved"] < SLICE_MIN_RESOLVED
        or counts["qualified_replies"] < SLICE_MIN_QUALIFIED_REPLIES
    ]
    gate_met = (
        resolved >= GATE_MIN_RESOLVED
        and qualified >= GATE_MIN_QUALIFIED_REPLIES
        and not below
    )
    reason = (
        "gate met: personalized weights eligible (still require explicit "
        "human approval per change)"
        if gate_met else
        "adaptive weights pinned off: Initiative 02 evidence gate not met "
        f"({resolved}/{GATE_MIN_RESOLVED} resolved, "
        f"{qualified}/{GATE_MIN_QUALIFIED_REPLIES} qualified replies"
        + (f"; slices below minimum: {', '.join(sorted(below))}" if below else "")
        + ")"
    )
    return {
        "gate_met": gate_met,
        "resolved_outcomes": resolved,
        "qualified_replies": qualified,
        "families": analysis["families"],
        "slices_below_minimum": sorted(below),
        "weights_source": "personalized (gate met)" if gate_met else "static",
        "reason": reason,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def weight_report(
    scored: list[dict[str, Any]] | None = None,
    events_path: str | Path | None = None,
) -> dict[str, Any]:
    """First weight report: what personalized weights WOULD look like.

    ``scored`` is optional linked evidence: [{application_id,
    components: {skills, seniority, salary, location, recency}, ...}].
    Without it, the report honestly states there is no linked evidence
    and proposes no deltas. **Nothing here is applied** — applying a
    weight change goes through ``model_card.record_change`` with explicit
    human approval, and only when the gate passes.
    """
    gate = evidence_gate_status(events_path)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate": gate,
        "applied": False,
        "note": "report only: weights are never applied by the harness",
    }
    if not scored:
        report["deltas"] = {}
        report["evidence"] = "none"
        report["explanation"] = (
            "No linked component-score evidence supplied (the experiment "
            "ledger records per-application ranking versions; until it "
            "stores component scores, no weight deltas can be estimated)."
        )
        return report
    # Positive outcome = reached interviewed/offered/accepted.
    try:
        import outcomes

        events = outcomes.load_events(events_path)
    except ImportError:
        events = []
    positive_apps = set()
    for app_id, evs in _latest_by_application(events).items():
        types = {str(e.get("event_type")) for e in evs}
        if types & {"interviewed", "offered", "accepted"}:
            positive_apps.add(app_id)
    components = ("skills", "seniority", "salary", "location", "recency")
    deltas: dict[str, dict[str, Any]] = {}
    for comp in components:
        pos = [s["components"][comp] for s in scored
               if s.get("application_id") in positive_apps
               and comp in (s.get("components") or {})]
        neg = [s["components"][comp] for s in scored
               if s.get("application_id") not in positive_apps
               and comp in (s.get("components") or {})]
        if len(pos) >= 3 and len(neg) >= 3:
            mean_pos = sum(pos) / len(pos)
            mean_neg = sum(neg) / len(neg)
            # Standard error of the difference (uncertainty display).
            var = (sum((x - mean_pos) ** 2 for x in pos)
                   + sum((x - mean_neg) ** 2 for x in neg)) / (len(pos) + len(neg) - 2)
            se = math.sqrt(var * (1 / len(pos) + 1 / len(neg)))
            deltas[comp] = {
                "mean_positive": round(mean_pos, 2),
                "mean_other": round(mean_neg, 2),
                "lift": round(mean_pos - mean_neg, 2),
                "std_error": round(se, 2),
                "n_positive": len(pos),
                "n_other": len(neg),
                # Observational only: describes the direction of the
                # association in this sample, NOT a recommendation and NOT
                # evidence that changing the weight would cause better
                # outcomes.
                "observed_association": (
                    "higher_in_positive" if mean_pos > mean_neg
                    else "lower_in_positive"
                ),
            }
        else:
            deltas[comp] = {
                "lift": None,
                "reason": (
                    f"insufficient linked samples "
                    f"(positive={len(pos)}, other={len(neg)}; need >=3 each)"
                ),
            }
    report["deltas"] = deltas
    report["evidence"] = "linked" if any(
        d.get("lift") is not None for d in deltas.values()
    ) else "insufficient"
    report["sample_size"] = len(scored)
    return report


def run_harness(
    events_path: str | Path | None = None,
    scored: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full offline calibration run: gate + slices + weight report."""
    return {
        "harness": "calibration-v1",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "gate": evidence_gate_status(events_path),
        "weight_report": weight_report(scored, events_path),
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register calibration tools on the MCP server."""

    @mcp.tool()
    def calibration_gate() -> dict:
        """Evaluate the Initiative 02 evidence gate for personalized
        weights. Read-only; never changes weights."""
        return evidence_gate_status()

    @mcp.tool()
    def calibration_report() -> dict:
        """Run the offline calibration harness: gate status, family
        slices, and the first (unapplied) weight report."""
        return run_harness()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_gate(args: Any) -> int:
    print(json.dumps(evidence_gate_status(), indent=2, ensure_ascii=False))
    return 0


def _cli_report(args: Any) -> int:
    scored = None
    if args.scored_file:
        scored = json.loads(
            Path(args.scored_file).read_text(encoding="utf-8"))
    print(json.dumps(run_harness(scored=scored),
                     indent=2, ensure_ascii=False))
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    p = subparsers.add_parser(
        "calibrate",
        help="Initiative 02: evidence gate + offline weight report (read-only).",
    )
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    sub = p.add_subparsers(dest="cal_cmd", required=True)

    pg = sub.add_parser("gate", help="Evaluate the evidence gate.")
    pg.set_defaults(func=_cli_gate)

    pr = sub.add_parser("report", help="Full harness run.")
    pr.add_argument("--scored-file", default=None,
                    help="JSON list of {application_id, components} evidence.")
    pr.set_defaults(func=_cli_report)

    return {"calibrate": lambda args: args.func(args)}
