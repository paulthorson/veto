#!/usr/bin/env python3
"""Bring-your-own-listing: the primary listing-ingestion flow.

Legal-hardening commit 7 (spec section 5.2). This is the replacement for
the board scrapers deleted in commits 3-5 (LinkedIn, Indeed,
ZipRecruiter): instead of Veto fetching job boards, the user pastes a job
description, pastes a posting URL, or drops a saved HTML file, and Veto
parses it locally. The user's browser did the fetching; Veto does the
thinking.

Three inputs, one parsed listing, one downstream path:

  1. ``add_listing(text=...)`` — pasted job-description text. Local only.
  2. ``add_listing(url=...)`` — a posting URL, fetched exactly once with
     the honest Veto User-Agent (``providers._common.VETO_USER_AGENT``)
     plus the existing request hygiene (robots.txt check, polite delay),
     then parsed locally. No crawling, no board search.
  3. ``add_listing(html_file=...)`` — a saved HTML file, parsed locally
     with zero network traffic.

The parsed listing is a standard job dict — the same keys the providers
produce (``id``, ``board``, ``title``, ``company``, ``location``,
``url``, ``apply_url``, ``description``, ``requirements``) — saved to
``listings/<key>.json``. Its id is ``"user:<base64url(listing-key)>"``,
and the ``UserListingsBoard`` pseudo-provider (``name = "user"``) is
registered in ``server.PROVIDERS``, so ``get_job_details``,
``briefs.prep_interview``, ``tailor``, the apply queue, and the
fill-only apply path all consume it exactly like a provider-sourced
listing. One code path, no duplication.

Compliance posture of the ``"user"`` board (see ``compliance.RISK_TIER``):
the listing was supplied by the user, so it is neither "official"
(documented public API) nor "scraping". The single URL fetch in (2) uses
the existing ``robots_allows`` hygiene; robots.txt is fail-closed (spec §7.2),
so when robots.txt cannot be fetched or parsed the URL is never requested.

HTML parsing prefers ``<script type="application/ld+json">`` JobPosting
blocks (title, hiringOrganization, jobLocation, description) — the shape
browsers save — then falls back to og:/twitter: meta tags, ``<title>``,
and the first ``<h1>``. Title/company/location overrides always win.

"""

from __future__ import annotations

import hashlib
import html as _html_module
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from providers._common import (
    VETO_USER_AGENT,
    make_client,
    make_job_id,
    polite_delay,
    robots_allows,
    strip_html,
)

log = logging.getLogger("job-apply-mcp.byol")

BASE_DIR = Path(__file__).resolve().parent

#: Local listing store. Each listing is one JSON file; the filename stem
#: is the listing key that ``get_details`` resolves. (Add ``listings/`` to
#: .gitignore alongside applications.json — user data, never committed.)
LISTINGS_DIR = BASE_DIR / "listings"

#: Board name used in job ids (``user:<base64url(listing-key)>``).
BOARD = "user"

#: Largest HTML payload accepted from a URL fetch or file (bytes).
MAX_HTML_BYTES = 2 * 1024 * 1024

#: Schemes we will fetch. Anything else (file:, javascript:, data:, ...)
#: is rejected outright.
_ALLOWED_SCHEMES = ("http", "https")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _listing_key(text: str) -> str:
    """Deterministic, filesystem-safe key for a listing's text.

    Content-addressed: re-adding the same text is idempotent and returns
    the same job id.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"listing-{digest}"


def save_listing(listing: dict[str, Any]) -> str:
    """Persist a listing dict; returns its key."""
    LISTINGS_DIR.mkdir(parents=True, exist_ok=True)
    key = str(listing.get("_key") or _listing_key(listing.get("description", "")))
    listing = {k: v for k, v in listing.items() if not k.startswith("_")}
    (LISTINGS_DIR / f"{key}.json").write_text(
        json.dumps(listing, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return key


def load_listing(key: str) -> dict[str, Any] | None:
    """Load a saved listing by key (None when missing/corrupt)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", key or "")
    if not safe or safe != key:
        return None
    path = LISTINGS_DIR / f"{safe}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def list_listings() -> list[dict[str, Any]]:
    """Metadata for every saved listing, newest first."""
    if not LISTINGS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(LISTINGS_DIR.glob("listing-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        out.append(
            {
                "id": make_job_id(BOARD, path.stem),
                "title": data.get("title", "Unknown"),
                "company": data.get("company", "Unknown"),
                "location": data.get("location", ""),
                "source": data.get("source", ""),
                "added_at": data.get("added_at", ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Text heuristics
# ---------------------------------------------------------------------------

#: Separators tried (in order) to split a "Title @ Company" first line.
_TITLE_SPLIT_RES = [
    re.compile(r"\s+@\s+"),
    re.compile(r"\s+[|｜]\s+"),
    re.compile(r"\s+at\s+", re.IGNORECASE),
]

_BULLET_RE = re.compile(r"^\s*(?:[-•*▪–>]|\d+[.)])\s+\S.*$", re.MULTILINE)


def _split_title_line(line: str) -> tuple[str, str]:
    """Split a first line like ``"Senior SWE @ Acme"`` into (title, company).

    Conservative: only @, |, and " at " count as separators (a bare "-"
    is too ambiguous — salary ranges and title suffixes use it).
    """
    for pattern in _TITLE_SPLIT_RES:
        parts = pattern.split(line.strip(), maxsplit=1)
        if len(parts) == 2 and all(p.strip() for p in parts):
            return parts[0].strip(), parts[1].strip()
    return line.strip(), ""


def _requirements_from_text(text: str) -> str:
    """Join the bullet/numbered lines of a posting into a requirements string."""
    bullets = [m.group(0).strip() for m in _BULLET_RE.finditer(text)]
    return "\n".join(bullets)


def _requirements_from_soup(soup: Any, description: str) -> str:
    """Requirements from bullet lines plus the page's <li> items.

    JSON-LD/HTML descriptions lose their list markers when stripped to
    text, so <li> texts are harvested directly and merged with any
    bullet lines, deduped in order.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for line in _requirements_from_text(description).splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            merged.append(line)
    for li in soup.find_all("li"):
        text = li.get_text(" ", strip=True)
        if text and text not in seen:
            seen.add(text)
            merged.append(text)
    return "\n".join(merged)


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def _ld_jobposting(soup: Any) -> dict[str, Any]:
    """First JSON-LD JobPosting block found, or {}.

    Saved job pages (and most ATS pages) embed schema.org JobPosting
    JSON-LD — the most reliable signal when it exists.
    """
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if "@graph" in item and isinstance(item["@graph"], list):
                candidates.extend(
                    g for g in item["@graph"] if isinstance(g, dict)
                )
                continue
            types = item.get("@type")
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and "JobPosting" in types:
                return item
    return {}


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _org_name(org: Any) -> str:
    if isinstance(org, dict):
        return _str(org.get("name")).strip()
    return _str(org).strip()


def _location_text(loc: Any) -> str:
    """Best-effort location string from a JobPosting jobLocation value."""
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [
                _str(addr.get(k))
                for k in ("addressLocality", "addressRegion", "addressCountry")
            ]
            return ", ".join(p for p in parts if p).strip()
        if isinstance(addr, str):
            return addr.strip()
        name = _str(loc.get("name")).strip()
        if name:
            return name
    if isinstance(loc, list) and loc:
        return _location_text(loc[0])
    return _str(loc).strip()


def _meta(soup: Any, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def extract_from_html(html: str, source_url: str = "") -> dict[str, str]:
    """Extract title/company/location/description from saved/fetched HTML.

    All parsing is local. JSON-LD JobPosting wins when present; otherwise
    og:/twitter: meta, ``<title>``, and the first ``<h1>`` are tried in
    order. Never invents: anything not found comes back "".
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    ld = _ld_jobposting(soup)

    title = _str(ld.get("title")).strip()
    company = _org_name(ld.get("hiringOrganization"))
    location = _location_text(ld.get("jobLocation"))
    description_html = _str(ld.get("description"))

    if not title:
        title = _meta(soup, "og:title", "twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
    if not company:
        company = _meta(soup, "og:site_name")
    if not title:
        title = "Unknown"

    # If the first line of the title tag is "Title @ Company", split it.
    if not company and title:
        t, c = _split_title_line(title)
        if c:
            title, company = t, c

    if description_html:
        description = strip_html(description_html)
    else:
        # Prefer the main content element; fall back to the whole body.
        main = soup.find("article") or soup.find("main")
        description = strip_html(str(main) if main else html)

    return {
        "title": _html_module.unescape(title),
        "company": _html_module.unescape(company),
        "location": _html_module.unescape(location),
        "description": _html_module.unescape(description),
        "requirements": _requirements_from_soup(soup, description),
        "source_url": source_url,
    }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def parse_text(
    text: str,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    """Build a standard job dict from pasted/plain job-description text.

    Explicit title/company/location win; otherwise the first line is
    tried as ``"Title @ Company"``. The full text is kept as the
    description (lossless), with bullet lines surfaced as requirements.
    """
    text = (text or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    auto_title, auto_company = _split_title_line(lines[0]) if lines else ("", "")
    final_title = title.strip() or auto_title or "Unknown"
    final_company = company.strip() or auto_company or "Unknown"
    return {
        "board": BOARD,
        "title": final_title,
        "company": final_company,
        "location": location.strip(),
        "url": source_url,
        "apply_url": source_url,
        "description": text,
        "requirements": _requirements_from_text(text),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_html(html: str, *, source_url: str = "") -> dict[str, Any]:
    """Build a standard job dict from an HTML document string."""
    extracted = extract_from_html(html, source_url=source_url)
    return {
        "board": BOARD,
        "title": extracted["title"] or "Unknown",
        "company": extracted["company"] or "Unknown",
        "location": extracted["location"],
        "url": source_url,
        "apply_url": source_url,
        "description": extracted["description"],
        "requirements": extracted.get("requirements", ""),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_url(url: str, client: Any = None) -> dict[str, Any]:
    """Fetch a posting URL exactly once and return its HTML.

    Uses the single honest Veto User-Agent plus the existing request
    hygiene: a robots.txt check and a polite delay before the request.
    Returns ``{"html": ...}`` or ``{"error": ...}`` — never raises for
    network trouble. ``client`` is injectable for tests.
    """
    url = (url or "").strip()
    try:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError:
        scheme = ""
    if scheme not in _ALLOWED_SCHEMES:
        return {"error": f"Refusing to fetch non-http(s) URL: {url!r}"}
    if not robots_allows(BOARD, url):
        return {"error": f"robots.txt disallows fetching {url}"}
    try:
        polite_delay()
        own_client = client is None
        client = client or make_client()
        try:
            resp = client.get(url)
        finally:
            if own_client:
                client.close()
    except Exception as exc:
        return {"error": f"Fetch failed for {url}: {exc}"}
    if resp.status_code != 200:
        return {"error": f"Fetch failed for {url}: HTTP {resp.status_code}"}
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower() and "text" not in content_type.lower():
        # Be lenient: some servers omit content-type; sniff the body.
        body_start = resp.text[:200].lower()
        if "<html" not in body_start and "<!doctype" not in body_start:
            return {
                "error": f"URL did not return an HTML page (content-type: {content_type})"
            }
    text = resp.text
    if len(text.encode("utf-8")) > MAX_HTML_BYTES:
        return {"error": f"Page too large (> {MAX_HTML_BYTES} bytes): {url}"}
    return {"html": text}


def parse_html_file(path: str) -> dict[str, Any]:
    """Build a standard job dict from a saved HTML file. Zero network."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        return {"error": f"No file at {file_path}"}
    try:
        if file_path.stat().st_size > MAX_HTML_BYTES:
            return {"error": f"File too large (> {MAX_HTML_BYTES} bytes): {file_path}"}
        html = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"error": f"Could not read {file_path}: {exc}"}
    return parse_html(html)


def add_listing(
    text: str = "",
    url: str = "",
    html_file: str = "",
    title: str = "",
    company: str = "",
    location: str = "",
) -> dict[str, Any]:
    """Add a listing from exactly one of pasted text / URL / saved HTML file.

    Parses locally, saves to the listing store, and returns the standard
    job dict with its ``id`` (``user:<base64url>``) — the same shape the
    providers produce, so the whole downstream pipeline (briefs, tailor,
    apply-plan, fill-only apply) consumes it unchanged. Returns
    ``{"error": ...}`` when the input is missing or a fetch/read fails.
    """
    sources = [bool(text and text.strip()), bool(url and url.strip()),
               bool(html_file and html_file.strip())]
    if sum(sources) != 1:
        return {
            "error": "Provide exactly one of: pasted text, a URL, or an HTML file path."
        }
    if url:
        fetched = fetch_url(url)
        if fetched.get("error"):
            return {"error": fetched["error"]}
        listing = parse_html(fetched["html"], source_url=url.strip())
        listing["source"] = "url"
        listing["source_detail"] = url.strip()
    elif html_file:
        parsed = parse_html_file(html_file)
        if parsed.get("error"):
            return parsed
        listing = parsed
        listing["source"] = "html_file"
        listing["source_detail"] = str(Path(html_file).expanduser())
    else:
        listing = parse_text(
            text,
            title=title,
            company=company,
            location=location,
        )
        listing["source"] = "paste"
        listing["source_detail"] = "pasted text"
    if title.strip():
        listing["title"] = title.strip()
    if company.strip():
        listing["company"] = company.strip()
    if location.strip():
        listing["location"] = location.strip()
    if not listing.get("description", "").strip():
        return {"error": "No job-description text could be extracted from the input."}
    listing["_key"] = _listing_key(listing["description"])
    key = save_listing(listing)
    listing.pop("_key", None)
    listing["id"] = make_job_id(BOARD, key)
    listing["job_id"] = listing["id"]
    return listing


# ---------------------------------------------------------------------------
# Pseudo-provider: plugs the store into server.PROVIDERS / briefs
# ---------------------------------------------------------------------------

def get_details(payload: str) -> dict[str, Any]:
    """Provider-contract details lookup: ``payload`` is the listing key.

    Same error shape as the API providers (``{"url": "", "board", "error"}``)
    so ``server.get_job_details`` needs no special case.
    """
    listing = load_listing(payload)
    if listing is None:
        return {
            "url": "",
            "board": BOARD,
            "error": f"Saved listing {payload!r} not found",
        }
    details = dict(listing)
    details.setdefault("url", "")
    details.setdefault("apply_url", details.get("url", ""))
    details.setdefault("requirements", "")
    return details


class UserListingsBoard:
    """Pseudo-provider for bring-your-own listings (board name ``"user"``).

    ``search`` queries the user's saved listings locally — no network.
    ``get_details`` resolves a listing key from the store. Registered in
    ``server.PROVIDERS`` so BYOL ids flow through the standard pipeline.
    """

    name = BOARD

    def search(
        self,
        query: str,
        location: str,
        limit: int = 10,
        remote_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search saved listings locally (no network, no board query)."""
        words = [w for w in (query or "").lower().split() if w]
        results: list[dict[str, Any]] = []
        for path in sorted(
            LISTINGS_DIR.glob("listing-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            listing = load_listing(path.stem)
            if not listing:
                continue
            haystack = " ".join(
                str(listing.get(k, ""))
                for k in ("title", "company", "location", "description")
            ).lower()
            if words and not all(w in haystack for w in words):
                continue
            if location and location.lower() not in str(
                listing.get("location", "")
            ).lower():
                continue
            if remote_only and "remote" not in haystack:
                continue
            results.append(
                {
                    "id": make_job_id(BOARD, path.stem),
                    "title": listing.get("title", "Unknown"),
                    "company": listing.get("company", "Unknown"),
                    "location": listing.get("location", ""),
                    "url": listing.get("url", ""),
                    "board": BOARD,
                    "snippet": (
                        f"{listing.get('title', '')} @ "
                        f"{listing.get('company', '')}"
                    )[:400],
                    "tos_risk_tier": "user",
                    "tos_notice": (
                        "Supplied by you (pasted text, a URL you gave, or a "
                        "file you saved). Nothing was scraped; no job board "
                        "was queried."
                    ),
                }
            )
            if len(results) >= max(limit, 1):
                break
        return results

    def get_details(self, payload: str) -> dict[str, Any]:
        return get_details(payload)


# ---------------------------------------------------------------------------
# Plugin wiring (server.py / cli.py pick these up)
# ---------------------------------------------------------------------------

def register_tools(mcp: Any) -> None:
    """Register the BYOL MCP tools on an MCP server instance."""
    _impl_add = globals()["add_listing"]
    _impl_list = globals()["list_listings"]

    @mcp.tool()
    def add_listing(
        text: str = "",
        url: str = "",
        html_file: str = "",
        title: str = "",
        company: str = "",
        location: str = "",
    ) -> dict:
        """Add a job listing from pasted text, a posting URL, or a saved HTML file.

        This is the primary way to get a posting into Veto. Exactly one
        of ``text``, ``url``, ``html_file`` is required. A URL is fetched
        exactly once (honest Veto User-Agent, robots.txt honored) and
        parsed locally; an HTML file is parsed with zero network.

        Args:
            text: Pasted job-description text.
            url: Job posting URL to fetch once and parse locally.
            html_file: Path to a saved HTML file (parsed locally).
            title: Override the auto-detected job title.
            company: Override the auto-detected company.
            location: Override the auto-detected location.

        Returns:
            The standard job dict with its ``id`` (``user:<...>``), ready
            for get_job_details / briefs / tailor / apply — or an
            ``"error"`` key.
        """
        return _impl_add(
            text=text, url=url, html_file=html_file,
            title=title, company=company, location=location,
        )

    @mcp.tool()
    def list_listings() -> dict:
        """List your saved bring-your-own listings (newest first).

        Returns:
            Dict with ``listings``: [{id, title, company, location,
            source, added_at}]. The ids work with get_job_details and the
            rest of the pipeline.
        """
        return {"listings": _impl_list()}


def cmd_add_listing(args: Any) -> int:
    """CLI handler for ``add-listing``."""
    import sys as _sys

    result = add_listing(
        text=args.text or "",
        url=args.url or "",
        html_file=args.html_file or "",
        title=args.title or "",
        company=args.company or "",
        location=args.location or "",
    )
    if result.get("error"):
        print(f"error: {result['error']}", file=_sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    print(f"Saved: {result['title']} @ {result['company']}")
    if result.get("location"):
        print(f"  Location: {result['location']}")
    print(f"  Job id:   {result['id']}")
    print(f"  Source:   {result['source']} ({result['source_detail']})")
    print()
    print("Next steps (same pipeline as any listing):")
    print(f"  show it:      cli.py show {result['id']}")
    print(f"  score it:     cli.py match ...        (match.score_job)")
    print(f"  brief/prep:   cli.py brief ...        (prep_interview)")
    print(f"  apply plan:   cli.py apply {result['id']} --resume <path>")
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add ``add-listing`` to an argparse subparsers.

    Returns a {command: handler} mapping the caller merges into its own
    dispatch table (cli.py-style).
    """
    p = subparsers.add_parser(
        "add-listing",
        help="Add a job listing: paste text, give a URL, or point at a saved HTML file.",
    )
    p.add_argument(
        "text",
        nargs="?",
        default="",
        help="Job description text (or use --url / --html-file).",
    )
    p.add_argument(
        "--url",
        default="",
        help="Posting URL: fetched exactly once (honest User-Agent) and parsed locally.",
    )
    p.add_argument(
        "--html-file",
        default="",
        help="Saved HTML file path: parsed locally, zero network.",
    )
    p.add_argument("--title", default="", help="Override the auto-detected title.")
    p.add_argument("--company", default="", help="Override the auto-detected company.")
    p.add_argument("--location", default="", help="Override the auto-detected location.")
    p.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    return {"add-listing": cmd_add_listing}
