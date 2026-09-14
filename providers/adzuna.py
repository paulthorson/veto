"""Adzuna provider (STUB).

Adzuna's jobs API requires an app id + key from developer.adzuna.com.
Without ``ADZUNA_APP_ID`` and ``ADZUNA_APP_KEY`` in the environment this
provider raises RuntimeError on search; get_details is NotImplementedError.

When credentials ARE present the search path uses the real API shape:

    GET https://api.adzuna.com/v1/api/jobs/{country}/search/1
        ?app_id=...&app_key=...&what={query}&where={location}
        &results_per_page={limit}&content-type=application/json

which returns {"results": [{id, title, company: {display_name},
location: {display_name, area: [...]}, redirect_url, description,
salary_min, salary_max, contract_time, created, category: {label}, ...}]}.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from providers._common import (
    fetch_json,
    make_client,
    make_job_id,
    polite_delay,
    prettify_token,
)

log = logging.getLogger("job-apply-mcp.providers.adzuna")

APP_ID = os.environ.get("ADZUNA_APP_ID")
APP_KEY = os.environ.get("ADZUNA_APP_KEY")
BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"


def _credentials() -> tuple[str, str]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError(
            "Adzuna needs ADZUNA_APP_ID and ADZUNA_APP_KEY env vars — "
            "get them at developer.adzuna.com"
        )
    return app_id, app_key


class AdzunaProvider:
    """Adzuna jobs API provider (stub until credentials are configured)."""

    name = "adzuna"

    def search(
        self, query: str, location: str, limit: int, remote_only: bool,
        country: str = "us",
    ) -> list[dict[str, Any]]:
        app_id, app_key = _credentials()
        params: dict[str, Any] = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": max(1, limit or 10),
            "what": query,
            "content-type": "application/json",
        }
        if location:
            params["where"] = location
        with make_client() as client:
            polite_delay()
            data = fetch_json(
                client, "GET", BASE_URL.format(country=country), params=params
            )
        if not data:
            return []
        jobs: list[dict[str, Any]] = []
        for raw in data.get("results", []):
            job = self._parse_result(raw, country)
            if not job:
                continue
            if remote_only and "remote" not in (
                job["location"] + " " + job["snippet"]
            ).lower():
                continue
            jobs.append(job)
            if limit and len(jobs) >= limit:
                break
        log.info("Adzuna: %d jobs for %r in %r", len(jobs), query, location)
        return jobs

    def _parse_result(
        self, raw: dict[str, Any], country: str
    ) -> dict[str, Any] | None:
        job_id = raw.get("id")
        title = (raw.get("title") or "").strip()
        if job_id is None or not title:
            return None
        company = (raw.get("company") or {}).get("display_name") or "Unknown"
        location = (raw.get("location") or {}).get("display_name") or "Unknown"
        url = raw.get("redirect_url") or ""
        payload = f"{country}:{job_id}"
        snippet = (raw.get("description") or "")[:400]
        return {
            "id": make_job_id(self.name, payload),
            "title": title,
            "company": company,
            "location": location,
            "url": url,
            "board": self.name,
            "snippet": snippet,
        }

    def get_details(self, payload: str) -> dict[str, Any]:
        raise NotImplementedError(
            "Adzuna get_details is not implemented: the search API only "
            "returns short descriptions; fetch the redirect_url directly "
            "for full details."
        )
