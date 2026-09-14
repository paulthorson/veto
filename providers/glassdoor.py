"""Glassdoor provider: honest stub (no reliable access without login).

Why this stays a stub instead of a fake provider (researched 2026-09):

* Glassdoor shut down its public API years ago; what remains is
  partner-only, so there is no official endpoint to call.
* Job search pages are a React app behind aggressive bot mitigation.
  The only documented working technique (theabhishekchandra/applyverse
  ``docs/TECHNIQUES.md``, rewritten 2026-07) is "DOM scrape +
  client-side 'Show more jobs' clicks": i.e. it needs a *rendered
  browser session* and is explicitly flagged "bot-sensitive".
* This provider layer is plain-HTTP (httpx). Fetching Glassdoor search
  HTML without a browser is instantly CAPTCHA'd / blocked (HTTP 403),
  so shipping an httpx "provider" would be dishonest: it would return
  ``[]`` and look exactly like "no jobs found".
* The correct future path is a Playwright *session* provider (logged-in
  browser session), not an httpx scraper.

Until then, ``search()`` / ``get_details()`` raise ``NotImplementedError``
with this explanation. ``search_jobs`` in server.py already converts
``NotImplementedError`` into ``ValueError("Board 'glassdoor' is not
usable: ...")``, so the failure surfaces honestly instead of as fake
empty results.
"""

from __future__ import annotations

from typing import Any

WHY_NOT_SUPPORTED = (
    "Glassdoor is not supported: it has no public API (partner-only), and "
    "its React search pages sit behind aggressive bot mitigation; reliable "
    "access requires a rendered, logged-in browser session, which plain-HTTP "
    "providers cannot do. An httpx scraper would be CAPTCHA'd/blocked "
    "instantly and return fake-empty results. The honest path is a future "
    "Playwright session provider; until then this board stays disabled."
)


class GlassdoorProvider:
    """Placeholder that fails loudly instead of faking results."""

    name = "glassdoor"

    def search(
        self, query: str, location: str, limit: int, remote_only: bool
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(WHY_NOT_SUPPORTED)

    def get_details(self, payload: str) -> dict[str, Any]:
        raise NotImplementedError(WHY_NOT_SUPPORTED)


def get_providers() -> dict[str, GlassdoorProvider]:
    """Provider registry entry (wired in by the parent; no server import)."""
    return {"glassdoor": GlassdoorProvider()}
