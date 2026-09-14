#!/usr/bin/env python3
"""Referral radar for job-apply-mcp.

Finds the user's LinkedIn connections who work at target companies and
ranks them as referral prospects. Target companies come from (in order)
``preferences.json`` (``preferred_companies``), ``watches.json`` (watch
filters that name companies), and ``applications.json`` (companies the
user already applied to).

Network data comes from a LinkedIn ``Connections.csv`` export
(``profiles/connections.csv``) with a manual ``profiles/connections.json``
fallback (a list of ``{"name", "company", "position"}`` objects) for users
who do not want to export the CSV.

Honesty rules (hard):
  * Relationship strength is never inferred or fabricated. Every ranked
    contact carries ``relationship: "connection"`` unless the caller
    overrides it, plus an ``evidence`` list containing only labels backed
    by data: ``"same company"`` and/or ``"title overlap"``.
  * Outreach drafts never invent shared history, shared employers, or
    mutual acquaintances. Warm drafts say "I see you're at X".

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("job-apply-mcp.referrals")

BASE_DIR = Path(__file__).resolve().parent
CONNECTIONS_CSV = BASE_DIR / "profiles" / "connections.csv"
CONNECTIONS_JSON = BASE_DIR / "profiles" / "connections.json"
WATCHES_FILE = BASE_DIR / "watches.json"
APPLICATIONS_FILE = BASE_DIR / "applications.json"
PREFS_FILE = BASE_DIR / "preferences.json"

# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

#: Canonical field -> accepted header spellings (compared after lowercasing
#: and stripping spaces, underscores and hyphens).
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("firstname", "first_name", "first", "givenname", "given_name"),
    "last_name": (
        "lastname", "last_name", "last", "surname", "familyname", "family_name",
    ),
    "company": (
        "company", "employer", "organization", "organisation",
        "currentcompany", "current_company", "currentemployer",
        "current_employer",
    ),
    "position": (
        "position", "title", "jobtitle", "job_title", "role",
        "currentposition", "current_position", "headline",
    ),
    "connected_on": (
        "connectedon", "connected_on", "connected", "dateconnected",
        "date_connected",
    ),
    "url": ("url", "profileurl", "profile_url", "linkedinurl", "linkedin_url"),
    "email": ("emailaddress", "email_address", "email"),
}


def _norm_header(header: str) -> str:
    """Normalize a CSV header for alias matching."""
    return re.sub(r"[\s_\-]+", "", (header or "").lower())


def _column_map(fieldnames: list[str] | None) -> dict[str, str]:
    """Map canonical field names to the actual CSV column headers."""
    mapping: dict[str, str] = {}
    if not fieldnames:
        return mapping
    normalized = {_norm_header(h): h for h in fieldnames if h}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[canonical] = normalized[alias]
                break
    return mapping


def load_connections(path: str | Path = CONNECTIONS_CSV) -> list[dict[str, Any]]:
    """Parse a LinkedIn Connections.csv export into contact dicts.

    Tolerates missing or renamed columns (via alias matching) and skips
    blank rows (no usable name). Returns [] when the file is missing or
    unreadable.

    Args:
        path: Path to the Connections.csv file.

    Returns:
        List of dicts with keys: name, first_name, last_name, company,
        position, connected_on, url, email, source ("csv").
    """
    path = Path(path)
    contacts: list[dict[str, Any]] = []
    try:
        # utf-8-sig: LinkedIn exports carry a BOM.
        fh = path.open("r", encoding="utf-8-sig", errors="replace", newline="")
    except FileNotFoundError:
        log.debug("Connections CSV not found: %s", path)
        return []
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []
    with fh:
        reader = csv.DictReader(fh)
        colmap = _column_map(reader.fieldnames)
        for row in reader:
            if not isinstance(row, dict):
                continue

            def _get(canonical: str) -> str:
                header = colmap.get(canonical)
                if header is None:
                    return ""
                return str(row.get(header) or "").strip()

            first = _get("first_name")
            last = _get("last_name")
            name = f"{first} {last}".strip()
            if not name:
                continue  # blank row
            contacts.append(
                {
                    "name": name,
                    "first_name": first,
                    "last_name": last,
                    "company": _get("company"),
                    "position": _get("position"),
                    "connected_on": _get("connected_on"),
                    "url": _get("url"),
                    "email": _get("email"),
                    "source": "csv",
                }
            )
    log.info("Loaded %d connections from %s", len(contacts), path)
    return contacts


def load_connections_json(
    path: str | Path = CONNECTIONS_JSON,
) -> list[dict[str, Any]]:
    """Load the manual connections.json fallback.

    Expected shape: a JSON list of objects with ``name`` (or
    ``first_name``/``last_name``), ``company`` and ``position``. Blank
    entries are skipped; missing/corrupt files yield [].

    Args:
        path: Path to connections.json.

    Returns:
        Contact dicts in the same shape as :func:`load_connections`
        (with ``source`` set to ``"json"``).
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.debug("Connections JSON not found: %s", path)
        return []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []
    if not isinstance(data, list):
        log.warning("%s is not a JSON list; ignoring", path)
        return []
    contacts: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            first = str(entry.get("first_name") or "").strip()
            last = str(entry.get("last_name") or "").strip()
            name = f"{first} {last}".strip()
        if not name:
            continue
        contacts.append(
            {
                "name": name,
                "first_name": str(entry.get("first_name") or "").strip(),
                "last_name": str(entry.get("last_name") or "").strip(),
                "company": str(entry.get("company") or "").strip(),
                "position": str(entry.get("position") or "").strip(),
                "connected_on": str(entry.get("connected_on") or "").strip(),
                "url": str(entry.get("url") or "").strip(),
                "email": str(entry.get("email") or "").strip(),
                "source": "json",
            }
        )
    log.info("Loaded %d connections from %s", len(contacts), path)
    return contacts


def load_network(
    csv_path: str | Path | None = None,
    json_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Load the user's professional network, CSV first, JSON fallback.

    Tries the LinkedIn CSV export; when it is missing or yields no
    contacts, falls back to the manual ``connections.json``. Returns []
    when neither source has data.
    """
    contacts = load_connections(csv_path or CONNECTIONS_CSV)
    if contacts:
        return contacts
    return load_connections_json(json_path or CONNECTIONS_JSON)


# ---------------------------------------------------------------------------
# Target companies
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "llc", "ltd",
        "limited", "co", "company", "gmbh", "plc", "pty", "sa", "srl",
        "bv", "ab", "aps", "holdings", "group",
    }
)

#: Watch-filter keys that may name companies.
_WATCH_COMPANY_KEYS = (
    "company", "companies", "preferred_companies", "employers",
    "target_companies",
)


def normalize_company(name: str) -> str:
    """Normalize a company name for matching.

    Lowercases, strips punctuation and drops corporate suffixes
    ("Inc", "LLC", ...), so "Acme Inc." matches "acme".
    """
    text = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    tokens = [t for t in text.split() if t and t not in _COMPANY_SUFFIXES]
    return " ".join(tokens)


def _read_json_list(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _watch_target_companies(watches: dict[str, Any]) -> list[str]:
    """Extract company names from watch definitions' filters."""
    found: list[str] = []
    for watch in watches.values():
        if not isinstance(watch, dict):
            continue
        filters = watch.get("filters")
        if not isinstance(filters, dict):
            continue
        for key in _WATCH_COMPANY_KEYS:
            value = filters.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
            elif isinstance(value, list):
                found.extend(
                    str(v).strip() for v in value if str(v).strip()
                )
    return found


def target_companies(
    watches_path: str | Path = WATCHES_FILE,
    applications_path: str | Path = APPLICATIONS_FILE,
    prefs_path: str | Path = PREFS_FILE,
) -> list[str]:
    """Gather target companies from preferences, watches and applications.

    Sources, in priority order:
      1. ``preferences.json`` -> ``preferred_companies`` (explicit targets).
      2. ``watches.json`` -> watch filters that name companies.
      3. ``applications.json`` -> companies already applied to.

    Missing or corrupt files are tolerated (that source is skipped).
    Names are de-duplicated case-insensitively, preserving first-seen
    order; blank and "Unknown" entries are dropped.

    Returns:
        Ordered list of company display names.
    """
    candidates: list[str] = []

    prefs = _read_json_dict(Path(prefs_path))
    preferred = prefs.get("preferred_companies")
    if isinstance(preferred, list):
        candidates.extend(str(c).strip() for c in preferred)

    watches = _read_json_dict(Path(watches_path))
    candidates.extend(_watch_target_companies(watches))

    for entry in _read_json_list(Path(applications_path)):
        if isinstance(entry, dict):
            company = str(entry.get("company") or "").strip()
            if company and company.lower() != "unknown":
                candidates.append(company)

    seen: set[str] = set()
    targets: list[str] = []
    for name in candidates:
        name = str(name).strip()
        if not name or name.lower() == "unknown":
            continue
        key = normalize_company(name)
        if not key or key in seen:
            continue
        seen.add(key)
        targets.append(name)
    return targets


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

#: Seniority ladder used for proximity scoring (higher = more senior).
_SENIORITY_PATTERNS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (7, ("chief", "cfo", "cto", "ceo", "coo", "founder", "partner",
          "president", "vp", "vice president")),
    (6, ("director", "head of")),
    (5, ("staff", "principal")),
    (4, ("senior", "sr.", "sr ", "lead")),
    (1, ("junior", "jr.", "jr ", "associate", "intern", "trainee",
          "apprentice")),
)
_MID_LEVEL = 2

_SENIORITY_LABELS = {
    7: "executive", 6: "director", 5: "staff/principal", 4: "senior",
    3: "mid", 2: "mid", 1: "junior/associate",
}

_TOKEN_STOPWORDS = frozenset(
    {
        "and", "the", "of", "a", "an", "at", "in", "for", "to", "with",
        "on", "&", "|", "-", "i", "ii", "iii",
    }
)


def seniority_level(title: str) -> int:
    """Map a job title to a seniority rung (1=junior .. 7=executive).

    Unrecognized titles default to mid-level (2).
    """
    text = f" {(title or '').lower()} "
    for level, patterns in _SENIORITY_PATTERNS:
        for pattern in patterns:
            if pattern in text:
                return level
    return _MID_LEVEL


def _tokens(text: str) -> set[str]:
    """Tokenize free text into a lowercase keyword set."""
    words = re.findall(r"[a-z0-9][a-z0-9+#.\-]*", (text or "").lower())
    return {w for w in words if w not in _TOKEN_STOPWORDS and len(w) > 1}


def _profile_keywords(profile: dict[str, Any]) -> set[str]:
    """Keyword set from the profile's skills and target titles."""
    keywords: set[str] = set()
    for key in ("skills", "target_titles", "target_roles"):
        values = profile.get(key) if isinstance(profile, dict) else None
        if isinstance(values, list):
            for value in values:
                keywords |= _tokens(str(value))
    return keywords


def rank_referrals(
    connections: list[dict[str, Any]],
    targets: list[str],
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank connections as referral prospects for the target companies.

    Scoring (max 90):
      * +50 — contact works at a target company (strongest signal).
      * +0..20 — seniority proximity between the contact's title and the
        profile's target titles (closer = higher; contacts at or above
        the target level score best).
      * +0..20 — function/field overlap: keyword overlap between the
        contact's position and the profile's skills + target titles
        (5 points per shared keyword, capped at 20).

    Every result carries human-readable ``reasons`` and an ``evidence``
    list containing only data-backed labels (``"same company"``,
    ``"title overlap"``). ``relationship`` is always ``"connection"`` —
    strength is never inferred.

    Args:
        connections: Contact dicts from :func:`load_network`.
        targets: Target company names from :func:`target_companies`.
        profile: User profile dict (uses ``skills`` and ``target_titles``).

    Returns:
        Ranked list (best first) of dicts with keys: name, company,
        position, connected_on, score, reasons, evidence, relationship,
        target_company.
    """
    profile = profile or {}
    target_norm = {normalize_company(t): t for t in targets if t}
    profile_keywords = _profile_keywords(profile)

    target_titles = profile.get("target_titles") or []
    if not isinstance(target_titles, list):
        target_titles = []
    target_seniority = max(
        [seniority_level(str(t)) for t in target_titles] or [_MID_LEVEL]
    )

    ranked: list[dict[str, Any]] = []
    for contact in connections:
        if not isinstance(contact, dict):
            continue
        company = str(contact.get("company") or "").strip()
        position = str(contact.get("position") or "").strip()
        name = str(contact.get("name") or "").strip()
        if not name:
            continue

        score = 0
        reasons: list[str] = []
        evidence: list[str] = []

        company_key = normalize_company(company)
        is_target = bool(company_key and company_key in target_norm)
        if is_target:
            score += 50
            display = target_norm[company_key]
            reasons.append(f"works at {display} — a target company")
            evidence.append("same company")

        contact_seniority = seniority_level(position)
        proximity = max(0.0, 1.0 - abs(contact_seniority - target_seniority) / 4.0)
        seniority_points = round(20 * proximity)
        score += seniority_points
        if position:
            contact_label = _SENIORITY_LABELS.get(contact_seniority, "mid")
            target_label = _SENIORITY_LABELS.get(target_seniority, "mid")
            reasons.append(
                f"seniority: {contact_label} (target: {target_label})"
            )

        overlap = _tokens(position) & profile_keywords if position else set()
        overlap_points = min(20, 5 * len(overlap))
        score += overlap_points
        if overlap:
            words = ", ".join(sorted(overlap))
            reasons.append(f"title keyword overlap: {words}")
            evidence.append("title overlap")

        ranked.append(
            {
                "name": name,
                "company": company,
                "position": position,
                "connected_on": str(contact.get("connected_on") or ""),
                "score": score,
                "reasons": reasons,
                "evidence": evidence,
                # Never inferred: the only fact we have is the connection.
                "relationship": "connection",
                "target_company": is_target,
            }
        )

    ranked.sort(key=lambda r: (-r["score"], r["name"].lower()))
    return ranked


# ---------------------------------------------------------------------------
# Outreach drafts
# ---------------------------------------------------------------------------

_VALID_KINDS = ("warm",)


def draft_outreach(
    contact: dict[str, Any],
    job: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    kind: str = "warm",
) -> dict[str, str]:
    """Draft a short, human outreach message to a contact.

    * ``warm`` — asks for a referral or a brief intro call. Appropriate
      for existing connections. (The ``cold`` LinkedIn connection-note
      variant was removed; only ``warm`` is supported.)

    The drafts never claim shared history, shared employers, or mutual
    acquaintances — only facts present in ``contact``/``job``/``profile``.

    Args:
        contact: Contact dict (name, company, position).
        job: Optional job dict (title, company).
        profile: Optional user profile (full_name, target_titles).
        kind: "warm" (the only supported kind).

    Returns:
        Dict with keys: kind, to, subject, body.

    Raises:
        ValueError: If ``kind`` is not "warm".
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")
    contact = contact or {}
    job = job or {}
    profile = profile or {}

    first = str(contact.get("first_name") or "").strip()
    if not first:
        first = str(contact.get("name") or "").strip().split(" ")[0]
    company = str(contact.get("company") or "").strip()
    job_title = str(job.get("title") or "").strip()
    job_company = str(job.get("company") or company).strip()
    my_name = str(profile.get("full_name") or "").strip()
    my_titles = profile.get("target_titles") or []
    my_title = str(my_titles[0]).strip() if my_titles else "engineer"

    if not job_title:
        job_title = my_title

    subject = f"Quick question about {job_company}" if job_company else "Quick question"
    lines = [f"Hi {first}," if first else "Hi,", ""]
    if job_company:
        lines.append(
            f"I see you're at {job_company} — I'm exploring {job_title} "
            f"roles and {job_company} is high on my list."
        )
    else:
        lines.append(
            f"I'm exploring {job_title} roles and would value your "
            "perspective."
        )
    lines += [
        "",
        "Would you be open to a brief chat about your experience there? "
        "If it seems like a fit, I'd be grateful for a referral — happy "
        "to share my resume and a short blurb to make it easy.",
        "",
        "Thanks so much,",
        my_name or "—",
    ]
    body = "\n".join(lines)

    return {
        "kind": kind,
        "to": str(contact.get("name") or "").strip(),
        "subject": subject,
        "body": body,
    }


def plan_outreach_drafts(
    prospects: list[dict[str, Any]],
    job: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    *,
    kind: str = "warm",
    max_drafts: int = 5,
) -> list[dict[str, Any]]:
    """Draft outreach for ranked referral prospects — drafts only, never sent.

    Takes prospects as returned by :func:`rank_referrals` and produces one
    draft per prospect (up to ``max_drafts``), each carrying the prospect's
    ``evidence`` labels so the human reviewer can see exactly what the
    draft is based on. Returns draft dicts; nothing is transmitted.
    """
    drafts: list[dict[str, Any]] = []
    for prospect in (prospects or [])[:max(0, max_drafts)]:
        if not isinstance(prospect, dict) or not prospect.get("name"):
            continue
        text = draft_outreach(prospect, job=job, profile=profile, kind=kind)
        text["evidence"] = list(prospect.get("evidence", []))
        text["reasons"] = list(prospect.get("reasons", []))
        text["status"] = "draft"
        drafts.append(text)
    return drafts


# ---------------------------------------------------------------------------
# MCP + CLI wiring (called by the parent; never imported by server.py here)
# ---------------------------------------------------------------------------


def _default_profile() -> dict[str, Any]:
    """Load the user's profile; {} when unavailable (never raises)."""
    try:
        import profiles

        loaded = profiles.load_profile()
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Could not load profile: %s", exc)
        return {}


def find_referrals_data(
    company: str = "",
    limit: int = 10,
    csv_path: str | Path | None = None,
    json_path: str | Path | None = None,
) -> dict[str, Any]:
    """Core logic shared by the MCP tool and the CLI.

    Args:
        company: Optional company filter (substring, case-insensitive).
        limit: Max referrals to return.
        csv_path: Override for the Connections.csv path.
        json_path: Override for the connections.json path.

    Returns:
        Dict with targets, total_contacts and the ranked referrals list.
    """
    contacts = load_network(csv_path, json_path)
    targets = target_companies()
    profile = _default_profile()
    ranked = rank_referrals(contacts, targets, profile)
    if company:
        needle = company.lower()
        ranked = [r for r in ranked if needle in r["company"].lower()]
    return {
        "targets": targets,
        "total_contacts": len(contacts),
        "referrals": ranked[: max(0, limit)],
    }


def register_tools(mcp: Any) -> dict[str, Any]:
    """Register the ``find_referrals`` / ``draft_referral_outreach`` tools."""

    @mcp.tool()
    def find_referrals(company: str = "", limit: int = 10) -> dict[str, Any]:
        """Rank your LinkedIn connections as referral prospects.

        Scores each contact on: works at one of your target companies
        (from preferences, watches, applications), seniority proximity
        to your target role, and title/skill keyword overlap. Every
        result lists human-readable reasons and data-backed evidence
        labels — relationship strength is never guessed.

        Args:
            company: Optional filter, e.g. "Stripe" (substring match).
            limit: Max referrals to return (default 10).

        Returns:
            Dict with your target companies, total contacts scanned,
            and the ranked referral list.
        """
        return find_referrals_data(company=company, limit=limit)

    @mcp.tool()
    def draft_referral_outreach(
        name: str,
        company: str = "",
        job_title: str = "",
        kind: str = "warm",
    ) -> dict[str, str]:
        """Draft a short outreach message to a referral prospect.

        ``warm`` drafts ask an existing connection for a referral or a
        brief intro call. Drafts never invent shared history or shared
        employers.

        Args:
            name: Contact's full name (matched against your network;
                falls back to the name/company given).
            company: Contact's company (used when not found in network).
            job_title: Role you're pursuing (defaults to your target title).
            kind: "warm" (the only supported kind).

        Returns:
            Dict with kind, to, subject and body.
        """
        contacts = load_network()
        contact = next(
            (c for c in contacts if c.get("name", "").lower() == name.lower()),
            {"name": name, "company": company},
        )
        return draft_outreach(
            contact,
            {"title": job_title, "company": company or contact.get("company")},
            _default_profile(),
            kind=kind,
        )

    return {
        "find_referrals": find_referrals,
        "draft_referral_outreach": draft_referral_outreach,
    }


def cmd_referrals(args: argparse.Namespace) -> int:
    """CLI handler for the ``referrals`` command."""
    if args.draft:
        contacts = load_network(args.csv, args.connections_json)
        contact = next(
            (
                c
                for c in contacts
                if args.draft.lower() in c.get("name", "").lower()
            ),
            {"name": args.draft},
        )
        draft = draft_outreach(
            contact,
            {"title": args.job_title or "", "company": args.company or ""},
            _default_profile(),
            kind=args.kind,
        )
        if args.json:
            print(json.dumps(draft, indent=2, ensure_ascii=False))
        else:
            print(f"To:      {draft['to']}")
            print(f"Subject: {draft['subject']}")
            print()
            print(draft["body"])
        return 0

    data = find_referrals_data(
        company=args.company or "",
        limit=args.limit,
        csv_path=args.csv,
        json_path=args.connections_json,
    )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(f"Target companies: {', '.join(data['targets']) or '—'}")
    print(f"Contacts scanned: {data['total_contacts']}")
    print()
    if not data["referrals"]:
        print("No referral prospects found. Import profiles/connections.csv")
        print("(LinkedIn export) or create profiles/connections.json.")
        return 0
    for ref in data["referrals"]:
        marker = "*" if ref["target_company"] else " "
        print(
            f"{marker} [{ref['score']:2d}] {ref['name']} — "
            f"{ref['position'] or 'unknown title'} @ {ref['company'] or '?'}"
        )
        for reason in ref["reasons"]:
            print(f"      · {reason}")
    return 0


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add the ``referrals`` command to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p_ref = subparsers.add_parser(
        "referrals",
        help="Rank LinkedIn connections as referral prospects.",
    )
    p_ref.add_argument(
        "--company", default="", help="Filter prospects by company."
    )
    p_ref.add_argument(
        "--limit", type=int, default=10, help="Max prospects to show."
    )
    p_ref.add_argument(
        "--draft",
        default="",
        metavar="NAME",
        help="Draft an outreach message to NAME instead of listing.",
    )
    p_ref.add_argument(
        "--kind",
        default="warm",
        choices=["warm"],
        help="Outreach tone for --draft (warm only).",
    )
    p_ref.add_argument(
        "--job-title", default="", help="Role you are pursuing (for --draft)."
    )
    p_ref.add_argument("--csv", default=None, help="Connections.csv path.")
    p_ref.add_argument(
        "--connections-json", default=None, help="connections.json path."
    )
    p_ref.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )
    return {"referrals": cmd_referrals}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="referrals.py",
        description="Rank LinkedIn connections as referral prospects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    register_cli(sub)
    namespace = parser.parse_args()
    raise SystemExit(cmd_referrals(namespace))
