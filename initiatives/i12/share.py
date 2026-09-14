#!/usr/bin/env python3
"""Initiative 12 / Epic 2 — Shareable artifacts.

Privacy-safe artifacts a user can share: score cards, interview plans, and
progress snapshots. Every artifact:

* carries a methodology link and a "what's included / what's not" block,
* is scrubbed by :mod:`initiatives.i12.privacy` — any content field
  (resume text, JD text, names, contact details, or other private
  job-search content) raises :class:`ContentDetected` and the artifact is
  never built,
* is deep-frozen at build time: the returned artifact raises TypeError on
  any mutation attempt, so a post-scan edit cannot smuggle content in,
* is re-scanned at render time: :func:`render_markdown` fails closed on
  any dict it is handed, even one constructed outside the builders,
* never embeds raw resume or job-description text — only scores, factor
  names, counts, and user-approved short labels.

Artifacts are data dicts + a markdown rendering; the web UI and terminal
render the same dict.

Schema evolution (C7): ``SHARE_VERSION`` is bumped on ANY artifact-schema
change (new/renamed/removed payload field). Consumers treat an unknown
``share_version`` as malformed. ``SAFE_FIELD_NAMES`` (in privacy.py) is the
cross-module name allowlist — adding a name there exempts it from the
forbidden-name check for share AND telemetry, so additions require
reviewer sign-off and a matching SHARE_VERSION bump.
"""

from __future__ import annotations

from typing import Any

from .privacy import assert_clean

#: Public URL for the Initiative 12 methodology page, stamped on every
#: shared artifact. PLACEHOLDER — Paul sets the real public URL at launch.
#: It is deliberately not a relative repo path: "docs/i12/methodology.md"
#: is meaningless (and misleading) once the markdown leaves this machine.
METHODOLOGY_URL = "TBD — public methodology URL set by Paul at launch"
PRIVACY_NOTE = (
    "This card contains scores and factor names only. It never includes "
    "your resume text, job-description text, or personal details."
)

SHARE_VERSION = "1"


class FrozenDict(dict):
    """A dict that refuses all mutation.

    Returned by the artifact builders so a post-build edit cannot bypass
    the build-time content scan. Any ``d[k] = v``, ``del``, ``pop``,
    ``update``, ``setdefault`` or ``clear`` raises TypeError. Nested dicts
    are frozen too (see :func:`_freeze`); nested lists become tuples.
    Still a real dict, so ``json.dumps`` and ``.get``/iteration keep working.
    """

    _MSG = "share artifact is immutable after build — post-scan mutation is blocked"

    def _reject(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(self._MSG)

    __setitem__ = _reject  # type: ignore[assignment]
    __delitem__ = _reject  # type: ignore[assignment]
    pop = _reject  # type: ignore[assignment]
    popitem = _reject  # type: ignore[assignment]
    clear = _reject  # type: ignore[assignment]
    update = _reject  # type: ignore[assignment]
    setdefault = _reject  # type: ignore[assignment]


def _freeze(value: Any) -> Any:
    """Deep-freeze an artifact tree: dicts -> FrozenDict, lists -> tuples."""
    if isinstance(value, dict):
        return FrozenDict({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _base(kind: str, payload: dict[str, Any]) -> FrozenDict:
    artifact = {
        "share_version": SHARE_VERSION,
        "kind": kind,
        "methodology": METHODOLOGY_URL,
        "privacy_note": PRIVACY_NOTE,
        "payload": payload,
    }
    assert_clean(artifact, f"shareable artifact ({kind})")
    return _freeze(artifact)


def score_card(
    role_label: str,
    company_label: str,
    fit_score: float,
    factors: list[dict[str, Any]],
    verdict: str,
    top_reasons: list[str],
) -> dict[str, Any]:
    """Privacy-safe fit score card.

    ``role_label``/``company_label`` are short user-approved labels
    ("Senior Backend Engineer", "Acme Corp") — never pasted JD text.
    ``factors`` entries: {name, score, evidence_summary} where
    evidence_summary is a short paraphrase, not a quote of resume text.
    """
    for f in factors:
        if len(str(f.get("evidence_summary", ""))) > 160:
            raise ValueError(
                "evidence_summary too long — paraphrase, do not quote resume text"
            )
    return _base(
        "score_card",
        {
            "role": role_label[:80],
            "company": company_label[:80],
            "fit_score": round(float(fit_score), 1),
            "verdict": verdict,
            "top_reasons": [str(r)[:160] for r in top_reasons[:3]],
            "factors": [
                {
                    "name": str(f.get("name", ""))[:40],
                    "score": f.get("score"),
                    "evidence_summary": str(f.get("evidence_summary", ""))[:160],
                }
                for f in factors
            ],
        },
    )


def interview_plan(
    role_label: str,
    focus_areas: list[dict[str, Any]],
    session_count: int,
) -> dict[str, Any]:
    """Shareable interview-prep plan: focus areas and drill counts only.

    ``focus_areas`` entries: {area, why_short, drills}. ``why_short`` is a
    one-line paraphrase (<=140 chars), never rubric internals or user data.
    """
    return _base(
        "interview_plan",
        {
            "role": role_label[:80],
            "sessions_planned": int(session_count),
            "focus_areas": [
                {
                    "area": str(a.get("area", ""))[:60],
                    "why": str(a.get("why_short", ""))[:140],
                    "drills": int(a.get("drills", 0)),
                }
                for a in focus_areas[:8]
            ],
        },
    )


def progress_snapshot(
    weeks_active: int,
    workflows_completed: dict[str, int],
    outcomes_recorded: int,
) -> dict[str, Any]:
    """Progress snapshot: counts and streaks, never application contents.

    ``workflows_completed`` maps workflow names (e.g. "jd_decode",
    "risk_check") to completion counts. No per-role or per-company detail.
    """
    if any(not str(k).replace("_", "").isalnum() for k in workflows_completed):
        raise ValueError("workflow names must be simple tokens")
    return _base(
        "progress_snapshot",
        {
            "weeks_active": int(weeks_active),
            "workflows_completed": {
                str(k)[:40]: int(v) for k, v in workflows_completed.items()
            },
            "outcomes_recorded": int(outcomes_recorded),
            "note": "Counts only. No roles, companies, or content included.",
        },
    )


def _require_dict(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            f"malformed share artifact: {where} must be a dict, "
            f"got {type(value).__name__}"
        )
    return value


def _require_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(
            f"malformed share artifact: {where} must be a list, "
            f"got {type(value).__name__}"
        )
    return list(value)


def render_markdown(artifact: dict[str, Any]) -> str:
    """Render an artifact as shareable markdown (methodology link included).

    Fail closed (C1, F1): the artifact is re-scanned with
    :func:`privacy.assert_clean` at render time, so a dict built or edited
    outside the builders — or mutated after a build — raises
    :class:`ContentDetected` instead of publishing. The scan walks the dict
    to full depth with no cutoff (an iterative, cycle-guarded walk), so no
    nesting depth can smuggle content past the gate. Malformed input raises
    :class:`ValueError` with a clear message, never a raw KeyError (C10).
    """
    artifact = _require_dict(artifact, "artifact")
    # Re-scan BEFORE trusting anything in the dict: this is the render-time
    # gate. A hand-constructed or post-build-mutated dict with content
    # fails here, not downstream.
    assert_clean(artifact, "shareable artifact (render)")
    kind = artifact.get("kind", "artifact")
    if not isinstance(kind, str):
        raise ValueError(
            f"malformed share artifact: 'kind' must be a string, "
            f"got {type(kind).__name__}"
        )
    payload = _require_dict(artifact.get("payload"), "payload")
    lines = [f"# Veto {kind.replace('_', ' ').title()}",
             "",
             f"_Methodology: {artifact.get('methodology')}_",
             "",
             str(artifact.get("privacy_note", "")),
             ""]
    if kind == "score_card":
        lines += [
            f"**Role:** {payload.get('role')}  ",
            f"**Company:** {payload.get('company')}  ",
            f"**Fit score:** {payload.get('fit_score')} — {payload.get('verdict')}",
            "",
            "## Top reasons",
        ]
        lines += [f"- {r}" for r in _require_list(payload.get("top_reasons", []), "top_reasons")]
        lines += ["", "## Factors"]
        for f in _require_list(payload.get("factors", []), "factors"):
            f = _require_dict(f, "factor entry")
            lines.append(
                f"- **{f.get('name')}** ({f.get('score')}): {f.get('evidence_summary')}"
            )
    elif kind == "interview_plan":
        lines += [f"**Role:** {payload.get('role')}  ",
                  f"**Sessions planned:** {payload.get('sessions_planned')}",
                  "", "## Focus areas"]
        for a in _require_list(payload.get("focus_areas", []), "focus_areas"):
            a = _require_dict(a, "focus-area entry")
            lines.append(
                f"- **{a.get('area')}** — {a.get('why')} ({a.get('drills')} drills)"
            )
    elif kind == "progress_snapshot":
        lines += [f"**Weeks active:** {payload.get('weeks_active')}  ",
                  f"**Outcomes recorded:** {payload.get('outcomes_recorded')}",
                  "", "## Workflows completed"]
        for k, v in _require_dict(
            payload.get("workflows_completed"), "workflows_completed"
        ).items():
            lines.append(f"- {k}: {v}")
        lines += ["", str(payload.get("note", ""))]
    else:
        raise ValueError(
            f"malformed share artifact: unknown kind {kind!r} "
            "(expected score_card, interview_plan, or progress_snapshot)"
        )
    lines += ["", f"_Shared from Veto (share v{artifact.get('share_version')})._"]
    return "\n".join(lines)
