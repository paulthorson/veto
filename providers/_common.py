"""Shared helpers for the job-board provider modules.

Duplicates the tiny pieces providers need from ``server.py`` so the
providers package stays independent (server.py imports providers, never
the reverse).
"""

from __future__ import annotations

import base64
import logging
import os
import random
import re
import time
import urllib.parse
import urllib.robotparser
from typing import Any

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("veto-mcp.providers")

#: Project version, matching the ``vX.Y.Z`` git tags and the ``__version__``
#: declarations in the initiatives packages. The repo has no pyproject.toml
#: or setup.py, so this is the canonical version used by the User-Agent.
_VETO_VERSION = "0.1.0"

#: The single honest User-Agent sent on every outbound HTTP request.
#: Defined ONCE here and imported by ``server.py``, ``briefs.py``, and
#: ``browser_apply.py`` so it cannot drift. It identifies the tool and
#: never imitates a browser.
VETO_USER_AGENT = (
    f"veto/{_VETO_VERSION} (+https://github.com/paulthorson/veto)"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_proxy_env() -> None:
    """Drop no_proxy entries that httpx cannot parse.

    This function never sets or selects a proxy; it only prunes malformed
    ``no_proxy`` entries.

    Some environments ship entries like ``*[::1]`` or bracketed IPv6
    (``[::1]``). httpx turns any entry it doesn't recognize as IPv4/IPv6
    into an ``all://*<host>`` pattern, and ``URLPattern`` then crashes at
    client init ("Invalid port"). The unbracketed equivalents (``::1``)
    are kept and parsed correctly, so dropping the problematic forms is
    safe.
    """
    for var in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(var)
        if not val:
            continue
        kept = [
            entry
            for entry in val.split(",")
            if "*" not in entry and "[" not in entry and "]" not in entry
        ]
        os.environ[var] = ",".join(kept)


sanitize_proxy_env()


def make_client(timeout: float = 20.0) -> httpx.Client:
    """Return an httpx client that identifies with the single honest Veto User-Agent."""
    return httpx.Client(
        headers={
            "User-Agent": VETO_USER_AGENT,
            "Accept": "application/json,text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=timeout,
        follow_redirects=True,
    )


def polite_delay(min_s: float = 1.0, max_s: float = 2.0) -> None:
    """Sleep 1-2s between requests to respect rate limits."""
    delay = random.uniform(min_s, max_s)
    log.debug("Politeness delay: %.2fs", delay)
    time.sleep(delay)


#: How long a fetched robots.txt is trusted before re-fetching.
_ROBOTS_TTL_SECONDS = 3600.0

#: base URL ("https://host") -> (fetched_at_epoch, robots.txt body or None).
_robots_cache: dict[str, tuple[float, str | None]] = {}


def robots_allows(board: str, url: str, timeout: float = 5.0) -> bool:
    """True when the site's robots.txt permits fetching ``url``.

    Fetches ``/robots.txt`` once per host (cached for an hour) and honors
    it. Fail-CLOSED (spec §7.2): when robots.txt cannot be fetched or
    parsed, the URL is treated as disallowed and is NOT fetched. An
    unreadable robots file is not permission.
    Disallowed URLs must be skipped by the caller; they are never fetched.
    """
    base = urllib.parse.urlsplit(url)
    host = f"{base.scheme}://{base.netloc}"
    now = time.time()
    cached = _robots_cache.get(host)
    if cached is not None and now - cached[0] < _ROBOTS_TTL_SECONDS:
        body = cached[1]
    else:
        body = None
        try:
            with make_client(timeout=timeout) as client:
                resp = client.get(host + "/robots.txt")
                if resp.status_code == 200 and resp.text:
                    body = resp.text
        except Exception as exc:
            log.debug("robots.txt fetch failed for %s: %s", host, exc)
        _robots_cache[host] = (now, body)
    if body is None:
        log.debug("robots.txt unavailable for %s: treating %s as disallowed", host, url)
        return False
    try:
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.splitlines())
        return parser.can_fetch("*", url)
    except Exception as exc:
        log.debug("robots.txt parse failed for %s: %s", host, exc)
        return False


def clear_robots_cache() -> None:
    """Drop the in-memory robots.txt cache (tests)."""
    _robots_cache.clear()


def encode_payload(payload: str) -> str:
    """base64url-encode ``payload`` with padding stripped (matches server.py)."""
    return (
        base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    )


def decode_payload(token: str) -> str:
    """Decode a stripped-padding base64url token back to the payload string."""
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def make_job_id(board: str, payload: str) -> str:
    """Build a self-describing job id: ``<board>:<base64url(payload)>``."""
    return f"{board}:{encode_payload(payload)}"


def prettify_token(token: str) -> str:
    """Turn a board token like ``"robinhood"`` into ``"Robinhood"``."""
    return token.replace("-", " ").replace("_", " ").strip().title()


def fetch_json(
    client: httpx.Client, method: str, url: str, **kwargs: Any
) -> Any | None:
    """Safe JSON fetch: returns the parsed body, or None on any failure.

    Logs the failure but never raises, so one dead board can't crash a
    multi-board search.
    """
    try:
        resp = client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("fetch_json failed for %s %s: %s", method.upper(), url, exc)
        return None


def strip_html(html: str | None) -> str:
    """Convert an HTML fragment to plain text ("" for empty input)."""
    if not html:
        return ""
    text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
    # Collapse runs of blank lines left over from block elements.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def words_match(text: str, words: list[str]) -> bool:
    """True when every query word appears (case-insensitively) in ``text``."""
    lowered = text.lower()
    return all(word in lowered for word in words)
