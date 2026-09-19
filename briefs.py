#!/usr/bin/env python3
"""Company briefs + interview prep for the Veto MCP server.

Two building blocks for the "before you apply / before you interview"
part of the loop:

``company_brief(company)``
    Research a company: what they do, funding stage, headcount hints,
    recent news, and interview-process hints gathered from web search.

``prep_interview(job_id)``
    Decode a job id (``<board>:<base64url(payload)>``), reload the full
    job details through the board provider (same lookup logic as
    ``server.get_job_details``, without importing ``server``), then
    produce a prep doc: role summary, key requirements, likely interview
    questions tailored to the posting, STAR stories built **only** from
    the saved profile's experience/achievements (never invented), sharp
    questions to ask the interviewer, and salary talking points.

Honesty rules (fail closed on facts):
  - Every brief field is marked ``"unverified"`` when the web fetch
    returns nothing. Funding/headcount are only ever reported when the
    search snippets literally contain them — they are never invented.
  - STAR stories are built exclusively from profile data. With no
    experience/achievements on file, ``star_stories`` is ``[]`` — not
    fabricated stories.

Stdlib only (``urllib`` for the default web fetch); everything network
or profile related is injectable so tests run offline:

    briefs.company_brief("Acme", fetch=fake_fetch)
    briefs.prep_interview("lever:abc...", profile={...}, fetch=fake_fetch)

Compliance: interview-prep output is stamped with
``compliance.result_risk_tag(board)`` whenever the job details came from
a scraping-tier board, so ToS risk stays visible.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import compliance
from providers._common import VETO_USER_AGENT, decode_payload

log = logging.getLogger("veto-mcp.briefs")

BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "profiles" / "profile.json"

#: Marker used for any fact that could not be verified from a source.
UNVERIFIED = "unverified"

#: A search result: {"title": ..., "url": ..., "snippet": ...}.
FetchFn = Callable[[str], list[dict[str, str]]]


# ---------------------------------------------------------------------------
# Web fetch (injectable)
# ---------------------------------------------------------------------------


def _safe_fetch(fetch: FetchFn, query: str) -> list[dict[str, str]]:
    """Run ``fetch``; never raise — failures mean "no data", not a crash."""
    try:
        results = fetch(query)
    except Exception as exc:  # noqa: BLE001 - best-effort research
        log.warning("brief fetch failed for %r: %s", query, exc)
        return []
    if not isinstance(results, list):
        return []
    cleaned = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        cleaned.append(
            {
                "title": str(item.get("title", "")).strip(),
                "url": url,
                "snippet": str(item.get("snippet", "")).strip(),
            }
        )
    return cleaned


_DDG_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_DDG_SNIPPET_RE = re.compile(
    r'class="result__snippet"[^>]*>(.*?)</a>', re.S
)
_TAG_RE = re.compile(r"<[^>]+>")


def _ddg_url(href: str) -> str:
    """Resolve a DuckDuckGo result href to the real target URL."""
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if uddg:
            return uddg
    return href


def default_fetch(query: str) -> list[dict[str, str]]:
    """Best-effort web search via DuckDuckGo's HTML endpoint (no API key).

    Returns a list of {"title", "url", "snippet"}. Any failure (blocked,
    rate-limited, parse miss) yields [] — callers treat that as
    "unverified", never as an error to work around by guessing.
    """
    endpoint = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode(
        {"q": query}
    )
    request = urllib.request.Request(
        endpoint, headers={"User-Agent": VETO_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310
            html = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - network is best-effort
        log.warning("default_fetch failed for %r: %s", query, exc)
        return []

    anchors = _DDG_RESULT_RE.findall(html)
    snippets = _DDG_SNIPPET_RE.findall(html)
    results = []
    for i, (href, title_html) in enumerate(anchors[:10]):
        url = _ddg_url(href)
        if not url.startswith("http"):
            continue
        title = _TAG_RE.sub("", title_html).strip()
        snippet = _TAG_RE.sub("", snippets[i]).strip() if i < len(snippets) else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


# ---------------------------------------------------------------------------
# Signal extraction (only from fetched text — never invented)
# ---------------------------------------------------------------------------


def _extract_funding(texts: list[str]) -> str:
    """Return a funding-stage hint found in the texts, else UNVERIFIED."""
    joined = " ".join(texts)
    amount = re.search(r"raised\s+(\$[\d.,]+\s*(?:billion|million|[KMB])?)", joined, re.I)
    amount_str = f" ({amount.group(1).strip()})" if amount else ""
    for pattern, label in [
        (r"\bseries\s+[A-E]\b", "Series funding"),
        (r"\bpre-seed\b|\bseed\s+round\b|\bseed\s+funding\b", "Seed stage"),
        (r"\bwent\s+public\b|\bIPO\b|\bNASDAQ\b|\bNYSE\b", "Public company"),
        (r"\bacquired\s+by\b", "Acquired"),
        (r"\bbootstrapped\b", "Bootstrapped"),
    ]:
        match = re.search(pattern, joined, re.I)
        if match:
            return f"{match.group(0).strip()}{amount_str} (from search snippets)"
    if amount:
        return f"Raised {amount_str.strip(' ()')} (from search snippets)"
    return UNVERIFIED


def _extract_headcount(texts: list[str]) -> str:
    """Return a headcount hint found in the texts, else UNVERIFIED."""
    joined = " ".join(texts)
    match = re.search(
        r"(\d[\d,]*)\s*\+\s*(?:employees|people|staff)"
        r"|(\d[\d,]*)\s+(?:employees|people|staff)"
        r"|headcount\s+(?:of\s+)?(\d[\d,]*)",
        joined,
        re.I,
    )
    if match:
        number = next(g for g in match.groups() if g)
        return f"~{number} employees (from search snippets)"
    return UNVERIFIED


def _extract_numbers(text: str) -> list[str]:
    """Pull metric-looking tokens out of a text (for STAR "Result" lines).

    Only ever surfaces numbers that are literally present in the source
    text — it never invents them.
    """
    if not text:
        return []
    found = re.findall(
        r"\$\d[\d.,]*\s*(?:billion|million|[KMB])?"
        r"|\b\d[\d,]*(?:\.\d+)?\s*(?:%|percent|x|X|ms|s\b|users|requests|\+)",
        text,
    )
    seen: set[str] = set()
    out = []
    for token in found:
        key = token.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(token.strip())
    return out[:8]


# ---------------------------------------------------------------------------
# company_brief
# ---------------------------------------------------------------------------


def company_brief(company: str, fetch: FetchFn | None = None) -> dict[str, Any]:
    """Research a company for interview prep.

    Returns {company, what_they_do, funding_stage, headcount_hint,
    recent_news, interview_process_hints, sources, verified, markdown}.
    Every field is "unverified" (lists empty) when the fetch returns
    nothing; funding/headcount are only reported when the snippets
    literally contain them.
    """
    fetch = fetch or default_fetch
    company = (company or "").strip()

    brief: dict[str, Any] = {
        "company": company,
        "what_they_do": UNVERIFIED,
        "funding_stage": UNVERIFIED,
        "headcount_hint": UNVERIFIED,
        "recent_news": [],
        "interview_process_hints": UNVERIFIED,
        "sources": [],
        "verified": False,
        "markdown": "",
    }
    if not company:
        brief["error"] = "company name is required"
        brief["markdown"] = "# Company brief\n\nNo company name given."
        return brief

    overview = _safe_fetch(fetch, f"{company} company what they do")
    funding = _safe_fetch(fetch, f"{company} funding raised series")
    news = _safe_fetch(fetch, f"{company} company news")
    interviews = _safe_fetch(fetch, f"{company} interview process")

    all_results = overview + funding + news + interviews
    if not all_results:
        brief["markdown"] = (
            f"# Company brief: {company}\n\n"
            "Web research returned nothing — every field is unverified. "
            "Check the company's site and recent news manually."
        )
        return brief

    texts = [f"{r['title']} {r['snippet']}" for r in all_results]
    brief["verified"] = True

    if overview:
        top = overview[0]
        what = top["snippet"] or top["title"]
        brief["what_they_do"] = (what[:400] + "…") if len(what) > 400 else what

    brief["funding_stage"] = _extract_funding(texts)
    brief["headcount_hint"] = _extract_headcount(texts)

    seen_urls: set[str] = set()
    for item in news[:3]:
        if item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            brief["recent_news"].append(
                {"title": item["title"] or item["url"], "url": item["url"]}
            )

    hints = []
    for item in interviews:
        snippet = item["snippet"]
        if "interview" in snippet.lower() and snippet not in hints:
            hints.append(snippet[:220])
        if len(hints) == 3:
            break
    if hints:
        brief["interview_process_hints"] = hints

    brief["sources"] = sorted(
        {r["url"] for r in all_results if r["url"].startswith("http")}
    )
    brief["markdown"] = _brief_markdown(brief)
    return brief


def _brief_markdown(brief: dict[str, Any]) -> str:
    lines = [f"# Company brief: {brief['company']}", ""]
    lines.append(f"**What they do:** {brief['what_they_do']}")
    lines.append(f"**Funding stage:** {brief['funding_stage']}")
    lines.append(f"**Headcount hint:** {brief['headcount_hint']}")
    lines.append("")
    news = brief["recent_news"]
    lines.append("## Recent news")
    if news:
        lines.extend(f"- [{n['title']}]({n['url']})" for n in news)
    else:
        lines.append("- unverified")
    lines.append("")
    lines.append("## Interview process hints")
    hints = brief["interview_process_hints"]
    if isinstance(hints, list):
        lines.extend(f"- {h}" for h in hints)
    else:
        lines.append(f"- {hints}")
    lines.append("")
    if brief["sources"]:
        lines.append("## Sources")
        lines.extend(f"- {url}" for url in brief["sources"][:10])
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Job lookup (mirrors server.get_job_details; does NOT import server)
# ---------------------------------------------------------------------------


def _provider_for(board: str) -> Any | None:
    """Return a provider instance for ``board``.

    Official-API providers come from the ``providers`` package. Boards
    whose provider classes live in ``server.py`` (glassdoor, …)
    are resolved with a lazy server import — safe because server never
    imports this module.
    """
    try:
        from providers import (
            AdzunaProvider,
            AshbyProvider,
            GreenhouseProvider,
            LeverProvider,
        )

        official = {
            "greenhouse": GreenhouseProvider(),
            "lever": LeverProvider(),
            "ashby": AshbyProvider(),
            "adzuna": AdzunaProvider(),
        }
    except Exception as exc:  # noqa: BLE001 - defensive
        log.warning("Could not load official providers: %s", exc)
        official = {}
    if board in official:
        return official[board]
    try:
        import server  # lazy: avoids the circular import at module load

        return server.PROVIDERS.get(board)
    except Exception as exc:  # noqa: BLE001 - server may be unavailable
        log.warning("Could not resolve provider for board %r: %s", board, exc)
        return None


def _get_job_details(job_id: str) -> dict[str, Any]:
    """Fetch full job details for a job id (mirrors server.get_job_details).

    Separated out so tests can monkeypatch ``briefs._get_job_details``
    instead of hitting the network.
    """
    board, sep, token = job_id.partition(":")
    if not sep or not board or not token:
        return {"job_id": job_id, "error": f"Malformed job_id: {job_id!r}"}
    try:
        payload = decode_payload(token)
    except Exception as exc:  # noqa: BLE001 - malformed base64
        return {"job_id": job_id, "error": f"Malformed job_id payload: {exc}"}
    provider = _provider_for(board)
    if provider is None:
        return {"job_id": job_id, "error": f"Unknown board {board!r}"}
    try:
        details = provider.get_details(payload)
    except NotImplementedError as exc:
        return {"job_id": job_id, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - provider fetch is best-effort
        return {"job_id": job_id, "error": f"get_details failed: {exc}"}
    if not isinstance(details, dict):
        return {"job_id": job_id, "error": "provider returned no details"}
    details.setdefault("title", "Unknown")
    details.setdefault("company", "Unknown")
    details["job_id"] = job_id
    return details


def _load_saved_profile() -> dict[str, Any]:
    """Return the wizard-saved applicant profile, or {}.

    Prefers server's loader (lazy import, no circular import); falls
    back to reading profiles/profile.json directly.
    """
    try:
        from server import _load_saved_profile as _server_load

        profile = _server_load()
        return profile if isinstance(profile, dict) else {}
    except Exception:  # noqa: BLE001 - server may be unavailable
        pass
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# prep_interview
# ---------------------------------------------------------------------------


def _key_requirements(description: str, requirements: Any) -> list[str]:
    """Extract the posting's key requirements (best-effort, no invention)."""
    reqs: list[str] = []
    if isinstance(requirements, list):
        reqs.extend(str(r).strip() for r in requirements if str(r).strip())
    elif isinstance(requirements, str) and requirements.strip():
        reqs.append(requirements.strip())
    if reqs:
        return reqs[:8]
    # Fallback: sentences that read like requirements.
    keywords = ("experience", "years", "must", "should", "required",
                "proficiency", "familiar", "expertise", "degree")
    for sentence in re.split(r"(?<=[.!?])\s+", description or ""):
        lowered = sentence.lower()
        if any(k in lowered for k in keywords) and 20 < len(sentence) < 300:
            reqs.append(sentence.strip())
        if len(reqs) == 6:
            break
    return reqs


def _likely_questions(title: str, company: str, description: str) -> list[str]:
    """5-8 interview questions tailored to the posting (templates, honest)."""
    lowered = (description or "").lower()
    questions = [
        "Walk me through your background — why are you a fit for this role?",
        f"Why {company}? Why this role, and why now?",
    ]
    rules = [
        (("lead", "led", "manage", "mentor", "manager"), (
            "Tell me about a time you led a project or mentored an engineer "
            "through a hard problem.")),
        (("distribut", "scale", "microservice", "high traffic"), (
            "How would you design a system that has to scale 10x? "
            "Walk me through the trade-offs.")),
        (("python",), "How deep is your Python experience — what have you built with it?"),
        (("data", "pipeline", "etl", "warehouse"), (
            "Describe a data pipeline you built or owned. How did you "
            "handle data quality?")),
        (("startup", "fast-paced", "ambigu"), (
            "This environment moves fast with ambiguity — give an example "
            "of shipping something with incomplete requirements.")),
        (("customer", "client", "stakeholder"), (
            "Tell me about working directly with customers or stakeholders "
            "on a technical deliverable.")),
        (("test", "quality", "reliability"), (
            "How do you think about testing and reliability in your work?")),
    ]
    for keywords, question in rules:
        if any(k in lowered for k in keywords):
            questions.append(question)
    # Pad to at least 5 with generic STAR prompts; cap at 8.
    padding = [
        "Tell me about the hardest technical problem you solved recently — "
        "what was your specific contribution?",
        "Describe a disagreement with a teammate and how you resolved it.",
        "What's something you're proud of that isn't on your resume?",
    ]
    for question in padding:
        if len(questions) >= 5:
            break
        questions.append(question)
    seen: set[str] = set()
    ordered = []
    for question in questions:
        if question not in seen:
            seen.add(question)
            ordered.append(question)
    return ordered[:8]


def _star_stories(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Build STAR story seeds ONLY from profile experience/achievements.

    Never invents experience: with nothing on file this returns [].
    Each story carries the raw source text plus any metrics literally
    present in it, so the user can flesh out Situation/Task/Action/Result
    without fabricating numbers.
    """
    stories: list[dict[str, Any]] = []

    def add_story(role: str, company: str, text: str, dates: str = "") -> None:
        if not (role or company or text):
            return
        stories.append(
            {
                "role": role or "Unknown role",
                "company": company or "Unknown company",
                "dates": dates,
                "story_seed": text.strip() if text.strip() else UNVERIFIED,
                "metrics": _extract_numbers(text),
            }
        )

    for exp in profile.get("experience", []) or []:
        if not isinstance(exp, dict):
            continue
        dates = " – ".join(
            part for part in (exp.get("start", ""), exp.get("end", "") or "present")
            if part
        )
        add_story(
            str(exp.get("title", "")),
            str(exp.get("company", "")),
            str(exp.get("description", "")),
            dates,
        )
    for achievement in profile.get("achievements", []) or []:
        if isinstance(achievement, dict):
            add_story(
                str(achievement.get("role", achievement.get("title", ""))),
                str(achievement.get("company", "")),
                str(achievement.get("achievement", achievement.get("text", ""))),
            )
        elif isinstance(achievement, str) and achievement.strip():
            add_story("", "", achievement)
    return stories


def _questions_to_ask(company: str) -> list[str]:
    return [
        "What does success look like in the first 90 days?",
        f"What is the biggest technical challenge the team is facing right now?",
        "How is the team structured, and who would I work with day to day?",
        f"Why is this role open — growth, backfill, or a new initiative at {company}?",
        "How does the team handle on-call, tech debt, and learning time?",
        "What would make you excited to hire for this role?",
    ]


def _salary_talking_points(profile: dict[str, Any]) -> list[str]:
    points = []
    target = profile.get("target_salary") or ""
    floor = (
        profile.get("salary_floor")
        or profile.get("minimum_salary")
        or ""
    )
    if target:
        points.append(
            f"Your stated target is {target} — anchor 10–15% above it and "
            "let them negotiate down to your number."
        )
    if floor:
        points.append(
            f"Your walk-away floor is {floor}. Share the target, never the floor."
        )
    points.extend(
        [
            "Never give a number first — ask for the approved range for the role.",
            "Negotiate total comp (base, bonus, equity, sign-on), not just base salary.",
        ]
    )
    return points


def prep_interview(
    job_id: str,
    profile: dict[str, Any] | None = None,
    fetch: FetchFn | None = None,
) -> dict[str, Any]:
    """Build an interview-prep doc for a job id.

    Returns {job_id, title, company, role_summary, key_requirements,
    likely_questions, star_stories, questions_to_ask_them,
    salary_talking_points, company_brief, tos, markdown}. STAR stories
    come only from the profile — with no experience on file the list is
    empty, never fabricated.
    """
    details = _get_job_details(job_id)
    if details.get("error"):
        return {"job_id": job_id, "error": details["error"]}

    board = job_id.partition(":")[0]
    title = str(details.get("title", "Unknown"))
    company = str(details.get("company", "Unknown"))
    location = str(details.get("location", ""))
    description = str(details.get("description", ""))
    requirements = details.get("requirements")

    if profile is None:
        profile = _load_saved_profile()
    if not isinstance(profile, dict):
        profile = {}

    role_summary = f"{title} at {company}"
    if location and location != "Unknown":
        role_summary += f" ({location})"
    first_sentences = " ".join(
        re.split(r"(?<=[.!?])\s+", description.strip())[:2]
    ).strip()
    if first_sentences:
        role_summary += f" — {first_sentences[:300]}"

    reqs = _key_requirements(description, requirements)
    stories = _star_stories(profile)
    brief = company_brief(company, fetch=fetch)

    prep: dict[str, Any] = {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": location,
        "role_summary": role_summary,
        "key_requirements": reqs,
        "likely_questions": _likely_questions(title, company, description),
        "star_stories": stories,
        "questions_to_ask_them": _questions_to_ask(company),
        "salary_talking_points": _salary_talking_points(profile),
        "company_brief": brief,
        "tos": compliance.result_risk_tag(board),
        "markdown": "",
    }
    prep["markdown"] = _prep_markdown(prep)
    return prep


def _prep_markdown(prep: dict[str, Any]) -> str:
    lines = [f"# Interview prep: {prep['title']} @ {prep['company']}", ""]
    if prep.get("location"):
        lines.append(f"**Location:** {prep['location']}")
    lines.append(f"**Role:** {prep['role_summary']}")
    lines.append("")
    lines.append("## Key requirements")
    reqs = prep["key_requirements"]
    lines.extend(f"- {r}" for r in reqs) if reqs else lines.append("- unverified")
    lines.append("")
    lines.append("## Likely questions")
    lines.extend(f"{i}. {q}" for i, q in enumerate(prep["likely_questions"], 1))
    lines.append("")
    lines.append("## STAR stories (from your profile — never invented)")
    stories = prep["star_stories"]
    if stories:
        for story in stories:
            lines.append(f"### {story['role']} — {story['company']}")
            if story.get("dates"):
                lines.append(f"*{story['dates']}*")
            lines.append(f"- Story: {story['story_seed']}")
            if story["metrics"]:
                lines.append(f"- Metrics on file: {', '.join(story['metrics'])}")
            lines.append("")
    else:
        lines.append(
            "- None on file. Run the onboarding wizard (grill mode) to add "
            "experience and achievements — stories are only ever built from "
            "your real history."
        )
    lines.append("")
    lines.append("## Questions to ask them")
    lines.extend(f"- {q}" for q in prep["questions_to_ask_them"])
    lines.append("")
    lines.append("## Salary talking points")
    lines.extend(f"- {p}" for p in prep["salary_talking_points"])
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(prep["company_brief"]["markdown"].rstrip())
    tos = prep.get("tos", {})
    if tos:
        lines.append("")
        lines.append(f"> ToS note ({tos.get('tos_risk_tier')}): {tos.get('tos_notice')}")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Wiring: MCP tools + CLI (server.py / cli.py call these; this module is
# never imported by them at module load, so there is no import cycle)
# ---------------------------------------------------------------------------


def _score_provenance() -> dict[str, Any]:
    """How the fit scores were produced (Initiative 02 evidence gate).

    Personalized weights stay pinned OFF until the Initiative 02 evidence
    gate passes (>=20 resolved outcomes incl. >=4 qualified replies, and no
    role-family slice below 10 resolved / 2 replies). When the calibration
    module exists and reports the gate met, provenance says so explicitly;
    otherwise it says ``static`` and why.
    """
    provenance: dict[str, Any] = {
        "model": "static",
        "personalized": False,
        "weights_source": "static defaults (match.score_job)",
    }
    try:
        import calibration  # Initiative 02 (may not exist yet)

        status = calibration.evidence_gate_status()
    except Exception as exc:  # fail closed: any calibration problem pins OFF
        log.warning("calibration.evidence_gate_status failed: %s", exc)
        provenance["reason"] = (
            "adaptive weights pinned off: calibration unavailable or "
            f"errored ({exc.__class__.__name__}); static scoring only"
        )
        return provenance
    gate = bool(status.get("gate_met"))
    provenance["personalized"] = gate
    provenance["model"] = "personalized" if gate else "static"
    provenance["weights_source"] = status.get("weights_source", "unknown")
    provenance["evidence"] = {
        "resolved_outcomes": status.get("resolved_outcomes"),
        "qualified_replies": status.get("qualified_replies"),
        "gate_met": gate,
    }
    if not gate:
        provenance["reason"] = (
            "adaptive weights pinned off: Initiative 02 evidence gate not "
            "met; static scoring only"
        )
    return provenance


def _readiness_notes(
    job: dict[str, Any], profile: dict[str, Any]
) -> list[str]:
    """Application readiness signals (never invents facts)."""
    notes: list[str] = []
    missing = [
        field for field in ("skills", "seniority", "location")
        if not profile.get(field)
    ]
    if missing:
        notes.append(f"profile incomplete: add {', '.join(missing)}")
    else:
        notes.append("profile complete (skills, seniority, location)")
    resume = list((BASE_DIR / "profiles").glob("resume.*"))
    if resume:
        notes.append("resume on file")
    else:
        notes.append("no resume on file — add one before applying")
    return notes


def daily_top_five(
    jobs: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Rank today's best qualified opportunities (Initiative 03 daily loop).

    Args:
        jobs: candidate postings (e.g. from a saved search). ``None`` means
            no input — the brief says so rather than inventing candidates.
        profile: candidate profile (defaults to the saved profile, {} when
            none exists).
        preferences: user preferences ({} when none exist).
        limit: how many to return (default 5).

    Returns:
        {date, considered, vetoed, top: [...], provenance}. Each entry:
        {job_id, title, company, fit_score, fit_band, evidence (top score
        reasons), freshness_days, readiness (notes), tos}. ``provenance``
        states explicitly whether scores are static or personalized
        (Initiative 02 gate).
    """
    from datetime import date as _date

    import match

    jobs = list(jobs or [])
    if profile is None:
        try:
            import profiles

            profile = profiles.load_profile()
        except (ImportError, AttributeError):
            profile = {}
    profile = profile or {}
    preferences = preferences or {}

    ranked: list[dict[str, Any]] = []
    vetoed = 0
    for job in jobs:
        scored = match.score_job(job, profile, preferences)
        if scored.get("veto"):
            vetoed += 1
            continue
        age = match._posting_age_days(job)  # same-package freshness helper
        ranked.append(
            {
                "job_id": job.get("job_id") or job.get("id"),
                "title": job.get("title"),
                "company": job.get("company"),
                "location": job.get("location"),
                "board": job.get("board"),
                "fit_score": scored.get("score"),
                "fit_band": _fit_band(scored.get("score", 0)),
                "evidence": list(scored.get("reasons", []))[:6],
                "matched_skills": list(scored.get("matched", [])),
                "missing_skills": list(scored.get("missing", [])),
                "freshness_days": round(age, 1) if age is not None else None,
                "readiness": _readiness_notes(job, profile),
            }
        )
    ranked.sort(key=lambda r: (r.get("fit_score") or 0), reverse=True)
    top = ranked[: max(0, limit)]
    return {
        "date": _date.today().isoformat(),
        "considered": len(jobs),
        "qualified": len(ranked),
        "vetoed": vetoed,
        "top": top,
        "provenance": _score_provenance(),
        **({} if jobs else {"note": "no jobs supplied — nothing ranked"}),
    }


def _fit_band(score: int) -> str:
    if score >= 85:
        return "excellent"
    if score >= 70:
        return "strong"
    if score >= 60:
        return "borderline"
    return "weak"


def register_tools(mcp: Any) -> None:
    """Register the brief/prep MCP tools on an MCP server instance."""
    # Bind the module-level implementations explicitly: the @mcp.tool()
    # wrappers below reuse the public names, which would otherwise shadow
    # the globals inside this scope.
    _impl_brief = globals()["company_brief"]
    _impl_prep = globals()["prep_interview"]
    _impl_top5 = globals()["daily_top_five"]

    @mcp.tool()
    def company_brief(company: str) -> dict:
        """Research a company for interview prep.

        Args:
            company: Company name, e.g. "Stripe".

        Returns:
            Dict with what_they_do, funding_stage, headcount_hint,
            recent_news, interview_process_hints, sources. Every field is
            "unverified" when web research returns nothing — facts are
            never invented.
        """
        return _impl_brief(company)

    @mcp.tool()
    def prep_interview(job_id: str) -> dict:
        """Build an interview-prep doc for a job from search_jobs.

        Args:
            job_id: The job id returned by search_jobs.

        Returns:
            Dict with role summary, key requirements, likely questions,
            STAR stories (built only from your saved profile — never
            invented), questions to ask them, salary talking points, a
            company brief, and markdown.
        """
        return _impl_prep(job_id)

    @mcp.tool()
    def brief_top_five(jobs: list) -> dict:
        """Rank today's top-five opportunities by fit, with evidence.

        Args:
            jobs: candidate postings (e.g. from search_jobs), each with
                title/company/snippet/description/requirements/location/board.

        Returns:
            Dict with date, considered/qualified/vetoed counts, top five
            entries (fit score, fit band, evidence, freshness, readiness),
            and scoring provenance (static vs personalized).
        """
        return _impl_top5(jobs)


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("markdown") or json.dumps(result, indent=2))


def cmd_company_brief(args: Any) -> int:
    """CLI handler for `company-brief`."""
    _print_result(company_brief(args.company), getattr(args, "json", False))
    return 0


def cmd_prep_interview(args: Any) -> int:
    """CLI handler for `prep-interview`."""
    _print_result(
        prep_interview(args.job_id), getattr(args, "json", False)
    )
    return 0


def cmd_top_five(args: Any) -> int:
    """CLI handler for `top-five`."""
    try:
        jobs = json.loads(Path(args.jobs_file).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: cannot read jobs file: {exc}")
        return 2
    if not isinstance(jobs, list):
        print("error: jobs file must contain a JSON list of postings")
        return 2
    result = daily_top_five(jobs, limit=args.limit)
    _print_top_five(result, getattr(args, "json", False))
    return 0


def _print_top_five(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    prov = result.get("provenance", {})
    print(f"Top opportunities — {result.get('date')} "
          f"({result.get('qualified')}/{result.get('considered')} qualified, "
          f"{result.get('vetoed')} vetoed)")
    print(f"Scoring: {prov.get('model')} ({prov.get('weights_source')})")
    for i, entry in enumerate(result.get("top", []), 1):
        fresh = entry.get("freshness_days")
        print(f"\n{i}. {entry.get('title')} @ {entry.get('company')} "
              f"— fit {entry.get('fit_score')} ({entry.get('fit_band')})"
              + (f", posted {fresh}d ago" if fresh is not None else ""))
        for reason in entry.get("evidence", [])[:3]:
            print(f"   - {reason}")


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `company-brief` / `prep-interview` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p_brief = subparsers.add_parser(
        "company-brief", help="Research a company for interview prep."
    )
    p_brief.add_argument("company", help='Company name, e.g. "Stripe".')
    p_brief.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )

    p_prep = subparsers.add_parser(
        "prep-interview", help="Build an interview-prep doc for a job id."
    )
    p_prep.add_argument("job_id", help="Job id from the search output.")
    p_prep.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )

    p_top = subparsers.add_parser(
        "top-five", help="Rank today's top opportunities by fit."
    )
    p_top.add_argument(
        "jobs_file", help="JSON file with a list of candidate postings."
    )
    p_top.add_argument("--limit", type=int, default=5)
    p_top.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )

    return {
        "company-brief": cmd_company_brief,
        "prep-interview": cmd_prep_interview,
        "top-five": cmd_top_five,
    }
