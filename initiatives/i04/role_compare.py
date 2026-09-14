#!/usr/bin/env python3
"""Role comparison (Initiative 04, Epic 3).

Compare up to four jobs across fit, risk, compensation, location, and
readiness — one table, apples-to-apples.

Inputs per job: ``job_id``, ``job_title``, ``jd_text``, and optionally
a ``score_snapshot`` captured at apply time. The snapshot shape follows
Initiative 01's *contracted* per-application snapshot::

    {"score": int, "components": {5}, "model_version": str,
     "weights_version": str,
     "confidence": {"kind": "static"|"personalized"|"cohort-informed",
                    "n": int}}

When no snapshot is present, the comparison decodes live with
:func:`initiatives.i04.fit_explain.fit_explain` against the supplied
resume text. Mixed snapshot/live rows are labeled, and rows scored
under different ``model_version``/``weights_version`` carry a
comparability warning instead of a silent ranking.

Compensation axis rule (spike open question 4, resolved here): the
comparison shows stated compensation ranges only when at least two of
the compared postings state a range. Otherwise the axis is hidden with
a stated reason — comparing "no data vs no data" is noise, and the
decoder never fills missing compensation with market data.

Readiness (from the evidence map): the share of *must-have*
requirements with ``supported`` status —
``ready`` (all), ``close`` (>= half), ``gaps to close`` (< half),
``unknown`` (no requirements extracted).

Pure function; deterministic; stdlib only.
"""

from __future__ import annotations

from typing import Any

import jd_decoder  # noqa: E402

from .fit_explain import fit_explain

MAX_JOBS = 4


def _compensation_of(jd_text: str) -> dict[str, Any]:
    """Stated compensation from the JD: range, mention, or absent."""
    signals = jd_decoder.decode_jd(jd_text or "")
    transparency = signals.get("transparency", {})
    salary_range = transparency.get("salary_range")
    if salary_range:
        return {
            "stated": True,
            "display": salary_range.get("raw") or "range stated",
            "kind": "range",
        }
    mention = transparency.get("salary_mention")
    if mention:
        raw = mention.get("raw") if isinstance(mention, dict) else str(mention)
        return {"stated": True, "display": raw or "salary mentioned", "kind": "mention"}
    return {"stated": False, "display": "not stated", "kind": "absent"}


def _location_of(jd_text: str) -> str:
    # The decoder reports what the posting states; location parsing
    # beyond the JD text is out of domain.
    for line in (jd_text or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("location:"):
            return stripped[len("location:"):].strip() or "not stated"
    lowered = (jd_text or "").lower()
    if "remote" in lowered:
        return "remote (mentioned)"
    return "not stated"


def _readiness(evidence_map: dict[str, Any]) -> dict[str, Any]:
    entries = evidence_map.get("entries", [])
    must_haves = [e for e in entries if e["requirement"]["kind"] == "must_have"]
    if not entries:
        return {"label": "unknown", "detail": "no requirements extracted"}
    if not must_haves:
        supported = sum(1 for e in entries if e["status"] == "supported")
        label = "ready" if supported == len(entries) else "close"
        return {
            "label": label,
            "detail": f"{supported}/{len(entries)} nice-to-haves supported",
        }
    supported = sum(1 for e in must_haves if e["status"] == "supported")
    ratio = supported / len(must_haves)
    if ratio == 1.0:
        label = "ready"
    elif ratio >= 0.5:
        label = "close"
    else:
        label = "gaps to close"
    return {
        "label": label,
        "detail": f"{supported}/{len(must_haves)} must-haves supported",
    }


def _row_from_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    snap = job["score_snapshot"]
    return {
        "score_source": "snapshot",
        "fit_score": snap.get("score"),
        "components": snap.get("components", {}),
        "provenance": snap.get("confidence", {"kind": "static", "n": 0}),
        "model_version": snap.get("model_version"),
        "weights_version": snap.get("weights_version"),
    }


def _row_from_live(
    job: dict[str, Any], resume_text: str
) -> dict[str, Any]:
    outcome = fit_explain(
        resume_text, job.get("jd_text", ""), job["job_id"],
        job.get("job_title", ""),
    )
    result = outcome["fit_result"]
    return {
        "score_source": "live",
        "fit_score": result["fit_score"],
        "components": result["components"],
        "provenance": result["provenance"],
        "model_version": "i04-live",
        "weights_version": "i04-live",
        "evidence_map": result["evidence_map"],
        "jd_verdict": result["jd_verdict"],
        "limitations": result["limitations"],
    }


def compare_roles(
    jobs: list[dict[str, Any]],
    resume_text: str = "",
) -> dict[str, Any]:
    """Compare up to four jobs. Returns a comparison document.

    Each job: ``{"job_id", "job_title", "jd_text", "score_snapshot"?}``.
    Raises ``ValueError`` on > 4 jobs or missing ``job_id`` — the caller
    (CLI/wizard) must enforce the four-job cap before calling.
    """
    if len(jobs) > MAX_JOBS:
        raise ValueError(
            f"compare_roles supports at most {MAX_JOBS} jobs, "
            f"got {len(jobs)}"
        )
    rows: list[dict[str, Any]] = []
    for job in jobs:
        job_id = job.get("job_id")
        if not job_id:
            raise ValueError("every compared job needs a job_id")
        jd_text = job.get("jd_text", "") or ""
        if job.get("score_snapshot"):
            row = _row_from_snapshot(job)
        else:
            row = _row_from_live(job, resume_text)
        verdict = jd_decoder.jd_verdict(jd_text)
        compensation = _compensation_of(jd_text)
        row.update(
            {
                "job_id": job_id,
                "job_title": job.get("job_title", ""),
                "risk": {
                    "verdict": verdict["verdict"],
                    "score": verdict["score"],
                    "top_reasons": [
                        r["reason"] for r in verdict["reasons"][:2]
                    ],
                },
                "compensation": compensation,
                "location": _location_of(jd_text),
            }
        )
        if row.get("evidence_map") is not None:
            row["readiness"] = _readiness(row["evidence_map"])
            # The map is large; the comparison keeps the readiness
            # rollup, not the full entry list.
            del row["evidence_map"]
        else:
            # Snapshot rows predate the evidence map: readiness unknown.
            row["readiness"] = {
                "label": "unknown",
                "detail": "score snapshot predates the evidence map",
            }
        rows.append(row)

    # Compensation axis: shown only when >= 2 postings state a range.
    stated_ranges = sum(
        1 for r in rows if r["compensation"]["kind"] == "range"
    )
    if stated_ranges >= 2:
        compensation_axis = {"shown": True}
    else:
        compensation_axis = {
            "shown": False,
            "reason": (
                f"only {stated_ranges} of {len(rows)} posting(s) state a "
                "compensation range — comparing missing data would be "
                "noise, and the decoder never fills it in"
            ),
        }

    # Comparability: snapshots scored under different model/weights
    # versions are not silently ranked against each other.
    versions = {
        (r.get("model_version"), r.get("weights_version")) for r in rows
    }
    comparability_warning = None
    if len(versions) > 1:
        version_names = sorted(str(v[0]) for v in versions)
        comparability_warning = (
            "Rows were scored under different model/weights versions "
            f"({version_names}); treat the ranking as "
            "approximate, not exact."
        )

    ranked = sorted(
        rows,
        key=lambda r: (r["fit_score"] is not None, r["fit_score"] or 0),
        reverse=True,
    )
    return {
        "schema": "veto/role-comparison/v1",
        "jobs": ranked,
        "compensation_axis": compensation_axis,
        "comparability_warning": comparability_warning,
        "limitations": [
            "Compensation uses only ranges stated in the postings.",
            "Location uses only what the posting states ('location:' "
            "line or a remote mention); anything else is 'not stated'.",
            "Snapshot rows predate the evidence map: their readiness "
            "is unknown and their scores are not re-derived.",
            "At most four jobs; the caller enforces the cap.",
        ],
    }
