#!/usr/bin/env python3
"""Deduplicate job listings.

The same role is often posted on several boards (or reposted with a
slightly different title). ``dedupe_jobs`` collapses those into one entry
using three signals, in order:

1. Exact URL match (after stripping query strings / fragments).
2. Exact normalized (company, title) match.
3. Fuzzy title match (difflib.SequenceMatcher >= 0.85) for the same
   normalized company.

When duplicates collapse, the entry with the most information (longest
description/snippet, preferring one with a non-"Unknown" company) is kept.
"""

from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit, urlunsplit

FUZZY_TITLE_THRESHOLD = 0.85


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and extra whitespace."""
    text = (text or "").lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", text).strip()


def _normalize_url(url: str) -> str:
    """Strip query string and fragment so tracking params don't split dupes."""
    try:
        parts = urlsplit(url or "")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return url or ""


def _info_score(job: dict[str, Any]) -> int:
    """Heuristic: how much usable information does this entry carry?"""
    score = 0
    for key in ("description", "snippet", "requirements"):
        score += len(str(job.get(key) or ""))
    if (job.get("company") or "Unknown") != "Unknown":
        score += 50
    if (job.get("location") or "Unknown") != "Unknown":
        score += 10
    return score


def _better(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return a if _info_score(a) >= _info_score(b) else b


def dedupe_jobs(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate postings, preserving first-seen order.

    Args:
        jobs: Job dicts as returned by providers (id/title/company/
            location/url/board/snippet).

    Returns:
        Deduplicated list; survivors keep all their original keys.
    """
    seen_urls: dict[str, dict[str, Any]] = {}
    # normalized company -> list of (normalized title, job dict)
    by_company: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    survivors: list[dict[str, Any]] = []

    def register(job: dict[str, Any]) -> None:
        survivors.append(job)
        norm_url = _normalize_url(job.get("url", ""))
        if norm_url:
            seen_urls[norm_url] = job
        company = _normalize(job.get("company", ""))
        title = _normalize(job.get("title", ""))
        by_company.setdefault(company, []).append((title, job))

    for job in jobs:
        norm_url = _normalize_url(job.get("url", ""))
        if norm_url and norm_url in seen_urls:
            existing = seen_urls[norm_url]
            merged = _better(existing, job)
            _replace_survivor(survivors, seen_urls, by_company, existing, merged)
            continue

        company = _normalize(job.get("company", ""))
        title = _normalize(job.get("title", ""))
        duplicate_of: dict[str, Any] | None = None
        # Never fuzzy-merge on an unknown/empty company: different postings
        # with no company attribution can't be compared safely.
        if company and company != "unknown":
            for other_title, other_job in by_company.get(company, []):
                if title == other_title or (
                    title
                    and other_title
                    and SequenceMatcher(None, title, other_title).ratio()
                    >= FUZZY_TITLE_THRESHOLD
                ):
                    duplicate_of = other_job
                    break
        if duplicate_of is not None:
            merged = _better(duplicate_of, job)
            _replace_survivor(survivors, seen_urls, by_company, duplicate_of, merged)
        else:
            register(job)

    return survivors


def _replace_survivor(
    survivors: list[dict[str, Any]],
    seen_urls: dict[str, dict[str, Any]],
    by_company: dict[str, list[tuple[str, dict[str, Any]]]],
    old: dict[str, Any],
    new: dict[str, Any],
) -> None:
    """Swap a survivor for a more informative duplicate, fixing indexes."""
    if new is old:
        return
    for i, job in enumerate(survivors):
        if job is old:
            survivors[i] = new
            break
    for url, job in list(seen_urls.items()):
        if job is old:
            seen_urls[url] = new
    for entries in by_company.values():
        for i, (title, job) in enumerate(entries):
            if job is old:
                entries[i] = (title, new)
