"""Lever board provider (public API, no auth).

List:   GET https://api.lever.co/v0/postings/{company}?mode=json
Detail: GET https://api.lever.co/v0/postings/{company}/{posting_id}?mode=json

Board tokens are read from providers/boards.json ("lever" key). Each token
was verified live against the API; dead ones were dropped.

Caveat: Lever's public API is per-company and tokens churn as employers
join/leave Lever. A token that 404s is skipped gracefully (logged).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
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

log = logging.getLogger("job-apply-mcp.providers.lever")

_BOARDS_FILE = Path(__file__).resolve().parent / "boards.json"


def _load_tokens() -> list[str]:
    try:
        return list(json.loads(_BOARDS_FILE.read_text(encoding="utf-8"))["lever"])
    except Exception as exc:  # missing/corrupt file: degrade, don't crash
        log.warning("Could not load board tokens from %s: %s", _BOARDS_FILE, exc)
        return []


def _coerce_list(value: Any) -> list[Any]:
    """Best-effort list container for nested vendor collections
    (`lists`). Guarding the items (`isinstance(item, dict)`) is not
    enough: a hostile truthy non-iterable (e.g. ``42``, ``True``) makes
    the `(raw.get(k) or [])` idiom a no-op and iteration raises
    TypeError, killing the detail call. Degrade to [] instead.
    GUARANTEES list output."""
    return value if isinstance(value, list) else []


def _coerce_location(value: Any) -> str:
    """Best-effort string location (mirrors ashby._coerce_location). The
    vendor's `location` normally arrives as a plain string or inside
    `categories`, but hostile shapes (dict with non-string name, list,
    number) arrive — coerce to a string, never crash a .lower() chain.
    GUARANTEES str output: never returns the raw non-string value."""
    name = value.get("name") if isinstance(value, dict) else value
    if name is None:
        text = ""
    elif isinstance(name, str):
        text = name
    else:
        text = str(name)
    return text or "Unknown"


def _coerce_categories(value: Any) -> dict[str, Any]:
    """Best-effort categories dict: a truthy non-dict `categories` (list,
    string, number) would crash `cats.get` — degrade to {} instead."""
    return value if isinstance(value, dict) else {}


def _coerce_text(value: Any) -> str:
    """Guaranteed-str coercion for description-family fields
    (descriptionPlain, description). Hostile truthy non-strings (dict,
    list, number) arrive — coerce to str so strip_html is only ever
    called on str (its contract is pinned for None/"") and the detail
    dict never leaks a non-str. Falsy values degrade to ""."""
    if not value:
        return ""
    return value if isinstance(value, str) else str(value)


class LeverProvider:
    """Search jobs across curated Lever-hosted company boards."""

    name = "lever"
    LIST_URL = "https://api.lever.co/v0/postings/{company}?mode=json"
    DETAIL_URL = "https://api.lever.co/v0/postings/{company}/{posting_id}?mode=json"

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
        per_token = max(1, math.ceil(limit / max(1, len(tokens)))) if limit else 0

        with make_client() as client:
            for i, company in enumerate(tokens):
                if limit and len(jobs) >= limit:
                    break
                if i:
                    polite_delay()
                postings = fetch_json(
                    client, "GET", self.LIST_URL.format(company=company)
                )
                if not postings or not isinstance(postings, list):
                    continue
                taken = 0
                for raw in postings:
                    # A hostile non-dict item mid-list must not kill the
                    # whole multi-board search — skip it instead.
                    if not isinstance(raw, dict):
                        continue
                    if limit and (len(jobs) >= limit or taken >= per_token):
                        break
                    job = self._parse_posting(raw, company)
                    if not job:
                        continue
                    if query_words and not words_match(job["_search_text"], query_words):
                        continue
                    if location and location not in job["location"].lower():
                        continue
                    if remote_only and "remote" not in (
                        job["location"] + " " + job["_workplace"]
                    ).lower():
                        continue
                    taken += 1
                    del job["_search_text"]
                    del job["_workplace"]
                    jobs.append(job)
        log.info(
            "Lever: %d jobs for %r in %r across %d boards",
            len(jobs),
            query,
            location,
            len(tokens),
        )
        return jobs

    def _parse_posting(
        self, raw: dict[str, Any], company: str
    ) -> dict[str, Any] | None:
        posting_id = raw.get("id")
        # isinstance-guarded: vendor `text`/`categories` values are
        # normally strings, but hostile non-string shapes arrive — coerce
        # to str so .strip()/join never raise and the dict never leaks
        # non-strings.
        title = str(raw.get("text") or "").strip()
        if not posting_id or not title:
            return None
        # isinstance-guarded via _coerce_categories: a truthy non-dict
        # `categories` (list, string, number) would crash `cats.get`.
        cats = _coerce_categories(raw.get("categories"))
        department = str(cats.get("department") or "")
        commitment = str(cats.get("commitment") or "")
        loc = _coerce_location(cats.get("location") or raw.get("country"))
        workplace = str(raw.get("workplaceType") or "").lower()
        url = str(raw.get("hostedUrl") or raw.get("applyUrl") or "")
        payload = f"{company}:{posting_id}"
        snippet_bits = [t for t in (title, department, commitment, loc) if t]
        return {
            "id": make_job_id(self.name, payload),
            "title": title,
            "company": prettify_token(company),
            "location": loc,
            "url": url,
            "board": self.name,
            "snippet": " — ".join(snippet_bits)[:400],
            # internal: query matching + remote matching text
            "_search_text": f"{title} {department} {commitment}",
            "_workplace": workplace,
        }

    # -- details --------------------------------------------------------
    def get_details(self, payload: str) -> dict[str, Any]:
        """Full details for a posting. ``payload`` is the decoded id payload
        (``"company:posting_id"``)."""
        company, sep, posting_id = payload.partition(":")
        if not sep or not company or not posting_id:
            return {"url": "", "board": self.name,
                    "error": f"Malformed Lever payload: {payload!r}"}
        with make_client() as client:
            polite_delay()
            raw = fetch_json(
                client,
                "GET",
                self.DETAIL_URL.format(company=company, posting_id=posting_id),
            )
        if not raw or not isinstance(raw, dict):
            # Falsy (None, {}) or a truthy non-dict (list, str, ...) from
            # a degraded vendor response: return the documented
            # fetch-failure error shape instead of crashing on .get
            # chains (same guard as greenhouse.get_details).
            return {
                "url": "",
                "board": self.name,
                "error": f"Lever posting {payload!r} not found or fetch failed",
            }
        # isinstance-guarded: a truthy non-dict `categories` would crash
        # `cats.get`.
        cats = _coerce_categories(raw.get("categories"))
        # isinstance-guarded via _coerce_text: a hostile truthy
        # non-string descriptionPlain/description is coerced to str —
        # never returned raw — so strip_html only ever sees str.
        description = _coerce_text(raw.get("descriptionPlain")) or strip_html(
            _coerce_text(raw.get("description"))
        )
        # Container-guarded via _coerce_list (MAJOR round-5 finding): a
        # hostile truthy non-iterable `lists` (e.g. 42, True) makes
        # `(raw.get(k) or [])` a no-op and iteration raises TypeError.
        req_bits = [
            f"{item.get('text', '')}: {item.get('content', '')}".strip(": ")
            for item in _coerce_list(raw.get("lists"))
            if isinstance(item, dict)
        ]
        workplace = str(raw.get("workplaceType") or "").lower()
        # Apply-readiness enrichment (Initiative 09, epic 2). Lever's public
        # postings API does not expose the application question list, so
        # application_questions is 0 and the readiness note says so — the
        # honest declaration is part of the feature.
        posted_ms = raw.get("createdAt")
        posted_date = ""
        try:
            if posted_ms:
                posted_date = datetime.fromtimestamp(
                    int(posted_ms) / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            # OverflowError: hostile createdAt (e.g. 10**22 ms) is out of
            # range for platform time_t — never raise on vendor data.
            posted_date = ""
        departments = [
            str(c) for c in (cats.get("department"), cats.get("team")) if c
        ]
        return {
            "url": str(raw.get("hostedUrl") or ""),
            "board": self.name,
            "title": str(raw.get("text") or "Unknown"),
            "company": prettify_token(company),
            "location": _coerce_location(cats.get("location") or raw.get("country")),
            "description": description,
            "requirements": "\n".join(b for b in req_bits if b),
            "apply_url": str(raw.get("applyUrl") or raw.get("hostedUrl") or ""),
            # --- Initiative 09 enrichment (additive; official API only) ---
            "departments": departments,
            "employment_type": str(cats.get("commitment") or ""),
            "remote": workplace == "remote",
            "posted_date": posted_date,
            "application_questions": 0,
            "required_questions": 0,
            "custom_fields": [],
            "closing_date": "",
            "readiness_note": (
                "Details come from Lever's official public postings API "
                "(read-only). The API does not expose the application "
                "question list — questions are visible only in the "
                "browser-assisted apply flow. Direct API submission is not "
                "permitted to job seekers: POST apply requires a "
                "Lever-issued API key (see "
                "ats_apply.endpoint_status('lever'))."
            ),
        }
