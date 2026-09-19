"""Ashby board provider (public API, no auth).

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{board}
  (primary; returns {"jobs": [...]}). When GET fails (returns None) on
  some boards, a POST with an empty JSON body to the SAME public posting
  endpoint is used as a read-only fallback — resilience against vendor
  endpoint quirks, never a write; no apply/submit POST exists anywhere.
  An empty-but-successful GET ({"jobs": []}) is NOT retried: the vendor
  answered, it just has no jobs.

Each job has: id, title, department, team, employmentType, location,
isRemote, jobUrl, applyUrl, descriptionHtml, descriptionPlain.

Board tokens are read from providers/boards.json ("ashby" key). Each token
was verified live against the API; dead ones were dropped.

get_details is best-effort: the public job-board endpoint embeds the full
HTML description, so details re-fetch the board and find the posting by
id; if the board shape ever differs, it degrades gracefully (returns what
it has plus a note).
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

log = logging.getLogger("veto-mcp.providers.ashby")

_BOARDS_FILE = Path(__file__).resolve().parent / "boards.json"


def _load_tokens() -> list[str]:
    try:
        return list(json.loads(_BOARDS_FILE.read_text(encoding="utf-8"))["ashby"])
    except Exception as exc:  # missing/corrupt file: degrade, don't crash
        log.warning("Could not load board tokens from %s: %s", _BOARDS_FILE, exc)
        return []


def _coerce_location(value: Any) -> str:
    """Best-effort string location. The vendor's `location` is normally a
    string, but hostile shapes (dict, list, number) arrive — coerce to a
    string, never crash a .lower() chain. GUARANTEES str output: a
    non-string dict `name` (e.g. {"name": 123}, {"name": ["x"]}) is
    stringified, never returned raw."""
    name = value.get("name") if isinstance(value, dict) else value
    if name is None:
        text = ""
    elif isinstance(name, str):
        text = name
    else:
        text = str(name)
    return text or "Unknown"


def _coerce_text(value: Any) -> str:
    """Guaranteed-str coercion for description-family fields (description,
    descriptionPlain, descriptionHtml). Hostile truthy non-strings (dict,
    list, number) arrive — coerce to str so strip_html is only ever
    called on str and the detail dict never leaks a non-str. Falsy values
    (None, "", 0, [], {}) degrade to ""."""
    if not value:
        return ""
    return value if isinstance(value, str) else str(value)


class AshbyProvider:
    """Search jobs across curated Ashby-hosted company boards."""

    name = "ashby"
    BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"

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
            for i, board in enumerate(tokens):
                if limit and len(jobs) >= limit:
                    break
                if i:
                    polite_delay()
                postings = self._fetch_board(client, board)
                taken = 0
                for raw in postings:
                    if limit and (len(jobs) >= limit or taken >= per_token):
                        break
                    job = self._parse_job(raw, board)
                    if not job:
                        continue
                    if query_words and not words_match(job["_search_text"], query_words):
                        continue
                    if location and location not in job["location"].lower():
                        continue
                    if remote_only and not job["_remote"]:
                        continue
                    taken += 1
                    del job["_search_text"]
                    del job["_remote"]
                    jobs.append(job)
        log.info(
            "Ashby: %d jobs for %r in %r across %d boards",
            len(jobs),
            query,
            location,
            len(tokens),
        )
        return jobs

    def _fetch_board(
        self, client: Any, board: str
    ) -> list[dict[str, Any]]:
        """Return the board's job list; GET first, POST as fallback."""
        url = self.BOARD_URL.format(board=board)
        data = fetch_json(client, "GET", url)
        if data is None:
            # POST-{} read fallback on the same public posting endpoint:
            # the posting API intermittently rejects GET on some boards
            # while answering POST with the same JSON body — resilience
            # against vendor endpoint quirks, never a write; no
            # apply/submit POST exists anywhere (see module docstring).
            data = fetch_json(client, "POST", url, json={})
        if not data:
            return []
        if isinstance(data, dict):
            # Guard the container: a hostile `jobs` (None when the key
            # exists with a null value, a number, a string, ...) must
            # not crash the comprehension and kill the whole multi-board
            # search — degrade to no jobs instead.
            jobs = data.get("jobs")
            if not isinstance(jobs, list):
                jobs = []
        elif isinstance(data, list):
            jobs = data
        else:
            log.warning("Ashby: unexpected board shape for %r: %r", board, type(data))
            return []
        return [j for j in jobs if isinstance(j, dict)]

    def _parse_job(
        self, raw: dict[str, Any], board: str
    ) -> dict[str, Any] | None:
        job_id = raw.get("id")
        # isinstance-guarded: vendor `title`/`department`/`team` are
        # normally strings, but hostile non-string shapes (numbers, lists)
        # arrive — coerce to str so .strip()/.join never raise and the
        # detail dict never leaks non-strings.
        title = str(raw.get("title") or "").strip()
        if not job_id or not title:
            return None
        # isinstance-guarded via _coerce_location: vendor `location` is
        # normally a string, but hostile shapes must not crash
        # location.lower() or leak non-strings into the detail dict.
        location = _coerce_location(raw.get("location"))
        department = str(raw.get("department") or "")
        team = str(raw.get("team") or "")
        url = str(raw.get("jobUrl") or raw.get("applyUrl") or "")
        payload = f"{board}:{job_id}"
        remote = bool(raw.get("isRemote")) or "remote" in location.lower()
        snippet_bits = [t for t in (title, department, team, location) if t]
        return {
            "id": make_job_id(self.name, payload),
            "title": title,
            "company": prettify_token(board),
            "location": location,
            "url": url,
            "board": self.name,
            "snippet": " — ".join(snippet_bits)[:400],
            # internal: query matching + remote flag
            "_search_text": f"{title} {department} {team}",
            "_remote": remote,
        }

    # -- details --------------------------------------------------------
    def get_details(self, payload: str) -> dict[str, Any]:
        """Best-effort full details: re-fetch the job-board endpoint and
        find the posting by id. ``payload`` is the decoded id payload
        (``"board:job_id"``)."""
        board, sep, job_id = payload.partition(":")
        if not sep or not board or not job_id:
            return {"url": "", "board": self.name,
                    "error": f"Malformed Ashby payload: {payload!r}"}
        with make_client() as client:
            polite_delay()
            postings = self._fetch_board(client, board)
        raw = next((p for p in postings if str(p.get("id")) == job_id), None)
        if raw is None:
            return {
                "url": "",
                "board": self.name,
                "error": (
                    f"Ashby posting {job_id!r} not found on board {board!r}; "
                    "the posting may have closed. Details re-fetch the live "
                    "job board and match by id."
                ),
            }
        # isinstance-guarded via _coerce_text: a hostile truthy
        # non-string descriptionPlain (dict, list, number) is coerced to
        # str — never returned raw — and descriptionHtml is coerced
        # before strip_html, so strip_html only ever sees str (its
        # contract is pinned for None/""). GUARANTEES str output.
        description = _coerce_text(raw.get("descriptionPlain")) or strip_html(
            _coerce_text(raw.get("descriptionHtml"))
        )
        # Apply-readiness enrichment (Initiative 09, epic 2). Ashby's public
        # posting API does not expose the application question list, so
        # application_questions is 0 and the readiness note says so — the
        # honest declaration is part of the feature.
        # Coerce non-string vendor values: a numeric `title` or
        # `department`/`team` must not leak a non-str into the detail dict
        # or crash downstream consumers.
        departments = [str(d) for d in (raw.get("department"),) if d]
        team = str(raw.get("team") or "")
        if team and team not in departments:
            departments.append(team)
        return {
            "url": str(raw.get("jobUrl") or ""),
            "board": self.name,
            "title": str(raw.get("title") or "Unknown"),
            "company": prettify_token(board),
            "location": _coerce_location(raw.get("location")),
            "description": description,
            "requirements": "",  # embedded in the description HTML
            "apply_url": str(raw.get("applyUrl") or raw.get("jobUrl") or ""),
            "note": (
                "Details served from the public job-board endpoint; "
                "description is the posting's HTML stripped to text."
            ),
            # --- Initiative 09 enrichment (additive; official API only) ---
            "departments": departments,
            "employment_type": str(raw.get("employmentType") or ""),
            "remote": bool(raw.get("isRemote")),
            "posted_date": str(raw.get("publishedDate") or "")[:10],
            "application_questions": 0,
            "required_questions": 0,
            "custom_fields": [],
            "closing_date": "",
            "readiness_note": (
                "Details come from Ashby's official public posting API "
                "(read-only). The API does not expose the application "
                "question list — questions are visible only in the "
                "browser-assisted apply flow. Direct API submission is "
                "not usable: applicationForm.submit returned 401 "
                "Unauthorized on live probes (see "
                "ats_apply.endpoint_status('ashby'))."
            ),
        }
