"""Greenhouse board provider (public API, no auth).

API docs: https://developers.greenhouse.io/harvest.html (job board variant
served from boards-api.greenhouse.io).

List:   GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs
Detail: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true

Board tokens are read from providers/boards.json ("greenhouse" key).
Each token was verified live against the API; dead ones were dropped
(see the module docstring / test notes in the repo README).
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from providers._common import (
    fetch_json,
    make_client,
    make_job_id,
    polite_delay,
    prettify_token,
    strip_html,
    words_match,
)

log = logging.getLogger("job-apply-mcp.providers.greenhouse")

_BOARDS_FILE = Path(__file__).resolve().parent / "boards.json"


def _load_tokens() -> list[str]:
    try:
        return list(json.loads(_BOARDS_FILE.read_text(encoding="utf-8"))["greenhouse"])
    except Exception as exc:  # missing/corrupt file: degrade, don't crash
        log.warning("Could not load board tokens from %s: %s", _BOARDS_FILE, exc)
        return []


def _coerce_list(value: Any) -> list[Any]:
    """Best-effort list container for nested vendor collections
    (`departments`, `offices`). Guarding the items (`isinstance(d, dict)`)
    is not enough: a hostile truthy non-iterable (e.g. ``42``, ``True``)
    makes the `raw.get(k) or []` idiom a no-op and iteration raises
    TypeError, killing the whole call — the N1 defect class one level
    down. Degrade to [] instead. GUARANTEES list output."""
    return value if isinstance(value, list) else []


def _coerce_location(value: Any) -> str:
    """Best-effort string location (mirrors ashby._coerce_location). The
    vendor's `location` normally arrives as {"name": ...}, but hostile
    shapes (string, list, number, dict with non-string name) arrive —
    coerce to a string, never crash a .lower() chain. GUARANTEES str
    output: a non-string dict `name` (e.g. {"name": 123}, {"name":
    ["x"]}) is stringified, never returned raw."""
    name = value.get("name") if isinstance(value, dict) else value
    if name is None:
        text = ""
    elif isinstance(name, str):
        text = name
    else:
        text = str(name)
    return text or "Unknown"


def _coerce_text(value: Any) -> str:
    """Guaranteed-str coercion for description-family fields (`content`).
    Hostile truthy non-strings (dict, list, number) arrive — coerce to
    str so strip_html is only ever called on str (its contract is pinned
    for None/"") and the detail dict never leaks a non-str. Falsy values
    degrade to ""."""
    if not value:
        return ""
    return value if isinstance(value, str) else str(value)


class GreenhouseProvider:
    """Search jobs across curated Greenhouse-hosted company boards."""

    name = "greenhouse"
    LIST_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    DETAIL_URL = (
        "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
        "?questions=true"
    )

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens if tokens is not None else _load_tokens()

    # -- search ---------------------------------------------------------
    def search(
        self, query: str, location: str, limit: int, remote_only: bool
    ) -> list[dict[str, Any]]:
        query_words = query.lower().split()
        location = (location or "").lower()
        jobs: list[dict[str, Any]] = []
        tokens = self.tokens
        # Cap per-token contributions so the total stays within ``limit``.
        per_token = max(1, math.ceil(limit / max(1, len(tokens)))) if limit else 0

        with make_client() as client:
            for i, token in enumerate(tokens):
                if limit and len(jobs) >= limit:
                    break
                if i:
                    polite_delay()
                data = fetch_json(client, "GET", self.LIST_URL.format(token=token))
                if not data or not isinstance(data, dict):
                    continue
                company = str(data.get("company_name") or prettify_token(token))
                # Guard both the container and its items: a hostile
                # `jobs` (non-list) or a non-dict item mid-list must not
                # kill the whole multi-board search — skip them instead.
                jobs_list = data.get("jobs")
                if not isinstance(jobs_list, list):
                    jobs_list = []
                taken = 0
                for raw in jobs_list:
                    if not isinstance(raw, dict):
                        continue
                    if limit and (len(jobs) >= limit or taken >= per_token):
                        break
                    job = self._parse_job(raw, token, company)
                    if not job:
                        continue
                    if query_words and not words_match(job["_search_text"], query_words):
                        continue
                    if location and location not in job["location"].lower():
                        continue
                    if remote_only and "remote" not in job["location"].lower():
                        continue
                    taken += 1
                    del job["_search_text"]
                    jobs.append(job)
        log.info(
            "Greenhouse: %d jobs for %r in %r across %d boards",
            len(jobs),
            query,
            location,
            len(tokens),
        )
        return jobs

    def _parse_job(
        self, raw: dict[str, Any], token: str, company: str
    ) -> dict[str, Any] | None:
        job_id = raw.get("id")
        # isinstance-guarded: vendor `title` and department `name`s are
        # normally strings, but hostile non-string shapes arrive — coerce
        # to str so .strip()/join never raise and the dict never leaks
        # non-strings.
        title = str(raw.get("title") or "").strip()
        if job_id is None or not title:
            return None
        location = _coerce_location(raw.get("location"))
        # Container-guarded via _coerce_list (MAJOR round-5 finding): a
        # hostile truthy non-iterable `departments` (e.g. 42, True) makes
        # `raw.get(k) or []` a no-op and iteration raises TypeError.
        departments = _coerce_list(raw.get("departments"))
        dept_text = " ".join(
            str(d.get("name") or "")
            for d in departments
            if isinstance(d, dict)
        )
        url = str(raw.get("absolute_url") or "")
        payload = f"{token}:{job_id}"
        snippet = f"{title} — {dept_text} — {location}".strip(" —")[:400]
        return {
            "id": make_job_id(self.name, payload),
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "board": self.name,
            "snippet": snippet,
            # internal: full text the query words are matched against
            "_search_text": f"{title} {dept_text}",
        }

    # -- details --------------------------------------------------------
    def get_details(self, payload: str) -> dict[str, Any]:
        """Full details for a job. ``payload`` is the decoded id payload
        (``"token:jobid"``)."""
        token, sep, job_id = payload.partition(":")
        if not sep or not token or not job_id:
            return {"url": "", "board": self.name,
                    "error": f"Malformed Greenhouse payload: {payload!r}"}
        with make_client() as client:
            polite_delay()
            data = fetch_json(
                client, "GET", self.DETAIL_URL.format(token=token, job_id=job_id)
            )
        if not data or not isinstance(data, dict):
            # Falsy (None, {}) or a truthy non-dict (list, str, ...) from a
            # degraded vendor response: return the documented fetch-failure
            # error shape instead of crashing on .get chains.
            return {
                "url": "",
                "board": self.name,
                "error": f"Greenhouse job {payload!r} not found or fetch failed",
            }
        url = str(data.get("absolute_url") or "")
        # Container-guarded via _coerce_list (MAJOR round-5 finding):
        # a hostile truthy non-iterable makes `data.get(k) or []` a no-op
        # and the comprehensions below raise TypeError.
        departments = _coerce_list(data.get("departments"))
        offices = _coerce_list(data.get("offices"))
        metadata = data.get("metadata")
        # Apply-readiness enrichment (Initiative 09, epic 2): the official
        # Job Board API already answers ?questions=true, so we surface the
        # application surface honestly without submitting anything.
        # `questions` must be a list: a truthy non-list (e.g. a string)
        # would make len(questions) count characters instead of questions.
        questions = data.get("questions")
        if not isinstance(questions, list):
            questions = []
        required_questions = sum(
            1 for q in questions if isinstance(q, dict) and q.get("required")
        )
        custom_fields = [
            str(m.get("name"))
            for m in (metadata if isinstance(metadata, list) else [])
            if isinstance(m, dict) and m.get("name")
        ]
        # Wire the Employment Type metadata value through instead of
        # hardcoding "". Case-insensitive name match; first hit wins.
        employment_type = ""
        for m in metadata if isinstance(metadata, list) else []:
            if (
                isinstance(m, dict)
                and str(m.get("name") or "").strip().lower() == "employment type"
            ):
                employment_type = str(m.get("value") or "").strip()
                break
        # isinstance-guarded via _coerce_location: vendor `location`
        # sometimes arrives as a string, a list, a number, or a dict with a
        # non-string name instead of {"name": ...} — always a str out.
        location_name = _coerce_location(data.get("location"))
        return {
            "url": url,
            "board": self.name,
            "title": str(data.get("title") or "Unknown"),
            "company": str(data.get("company_name") or prettify_token(token)),
            "location": location_name,
            "description": strip_html(_coerce_text(data.get("content"))),
            "requirements": self._format_metadata(metadata),
            "departments": [
                str(d.get("name"))
                for d in departments
                if isinstance(d, dict) and d.get("name")
            ],
            "apply_url": url,
            # --- Initiative 09 enrichment (additive; official API only) ---
            "offices": [
                str(o.get("name"))
                for o in offices
                if isinstance(o, dict) and o.get("name")
            ],
            "employment_type": employment_type,
            "remote": "remote" in location_name.lower(),
            "posted_date": str(data.get("updated_at") or "")[:10],
            "application_questions": len(questions),
            "required_questions": required_questions,
            "custom_fields": custom_fields,
            "closing_date": "",
            "readiness_note": (
                "Details and application questions come from Greenhouse's "
                "official public Job Board API (read-only). Direct API "
                "submission is not permitted to job seekers: POST "
                "applications requires an employer-issued Job Board API key "
                "(see ats_apply.endpoint_status('greenhouse'))."
            ),
        }

    @staticmethod
    def _format_metadata(metadata: Any) -> str:
        """Best-effort text extraction from the job's metadata block."""
        if not metadata:
            return ""
        try:
            if isinstance(metadata, list):
                parts = [
                    f"{m.get('name')}: {m.get('value')}"
                    for m in metadata
                    if isinstance(m, dict) and m.get("name")
                ]
                return "\n".join(parts)
            if isinstance(metadata, dict):
                return "\n".join(f"{k}: {v}" for k, v in metadata.items())
            return str(metadata)
        except Exception:
            return ""
