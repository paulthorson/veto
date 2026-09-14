#!/usr/bin/env python3
"""Evidence map builder (Initiative 04, Epic 2).

Connects each job requirement to a supported resume fact, a gap, or a
question for the grill — one requirement → one tri-state link entry,
per the frozen ``veto/evidence-map/v1`` contract (see
``schema_evidence_map_v1.md`` and ``schemas.validate_evidence_map``).

Pipeline (all on-device, deterministic, stdlib-only):

1. ``grill.extract_jd_keywords`` extracts ``{keyword, must_have}`` from
   the JD (requirement sections drive must-have classification).
2. Each keyword is matched against the decoded resume profile with the
   local ``_keyword_in_text`` (the same word-boundary/substring rule as
   ``grill._keyword_in_text``), alias-aware (``k8s`` ↔ ``Kubernetes``)
   via ``initiatives.i04.aliases``.
3. Tri-state classification:
   * **supported** — ≥1 verbatim evidence quote from the resume text.
     Every quote is traced back to its exact source line and asserted
     to be a substring of the resume text before a ``supported`` status
     is emitted; a match that cannot be quoted verbatim (a PII-redacted
     or decoder-joined line) degrades to ``grill_question`` — never to
     a fabricated quote.
     "N+ years X" requirements additionally check the resume's
     ``years_claims``: a claim of ≥N years in X is supporting evidence;
     a smaller claim is a **gap**. Gaps are never upgraded, even when
     other evidence for the keyword exists — the years detail is left
     to the human/grill.
   * **gap** — no evidence found. Never upgraded by the system.
   * **grill_question** — keyword present but the evidence is
     unquantified (no number on the keyword's line), or the match came
     only via an alias, or the match cannot be quoted verbatim.
     Ambiguity is resolved by the grill, not asserted by the decoder.
     The entry links ``grill_question_id`` (a stable derived id);
     :func:`build_evidence_map` also returns a companion
     ``suggested_grill_questions`` list keyed by those ids for the grill
     surface to materialize (integration_notes.md).

Honesty rules: every evidence quote is a verbatim substring of the
resume text; every requirement ``source_quote`` is a verbatim substring
of the JD text; absence is a gap, never filled in; the map is anchored
to the exact resume text via ``evidence_source_hash`` so later profile
edits invalidate stale maps.

Decision records (genuine options considered; each states its trade):

* **Tri-state classification, gaps never upgraded.** Options: (a)
  upgrade a gap to ``supported`` when partial evidence exists (e.g. a
  years shortfall but a quantified bullet mentions the skill); (b) a
  years shortfall is a gap, full stop — the system never upgrades gaps.
  Chosen (b): trades recall for honesty — (a) would mark more entries
  supported, but "supported" would then sometimes mean "supported
  except for the headline number", the exact soft claim the frozen
  contract forbids; (b) keeps every gap an honest absence at the cost of
  entries a human might have judged supportable, which the grill or a
  human re-examines.
* **Grill-question id namespacing.** Options: (a) keyword-only ids,
  stable across jobs so the grill surface can dedupe identical
  questions; (b) job-namespaced ids
  (``gq_<sha256(job_id, keyword)[:8]>``). Chosen (b): trades cross-job
  dedup for per-job correctness — the same keyword carries different
  source quotes and must-have kinds in different JDs, so a shared id
  would join one question to the wrong requirement; namespacing keeps
  the join exact at the cost of asking "the same" question twice across
  two applications.
* **Verbatim-quote enforcement strategy.** Options: (a) quote decoded
  profile lines directly — simple, and the decoder's lines are "close
  enough"; (b) trace every candidate quote back to its exact source
  line, skip lines that cannot be traced (PII-redacted, joined), and
  assert ``quote in resume_text`` at emission, degrading to
  ``grill_question``. Chosen (b): trades implementation complexity and
  some lost evidence (a redacted contact line holding the only keyword
  mention cannot be quoted) for the invariant the frozen schema
  promises — every ``supported`` quote is a verbatim substring, never a
  decoder-constructed string.
* **via_alias attribution.** Options: (a) infer from substring presence
  (``keyword not in line``); (b) derive from match provenance — the
  winning candidate versus the keyword under canonicalization. Chosen
  (b): trades a helper function for honest framing — (a) misfires when
  the decoder normalizes wording (resume said "k8s", profile says
  "Kubernetes"), mislabeling a genuine alias match as literal and
  misframing the grill question; (b) reports the resume's actual wording.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .aliases import CANONICAL_TO_ALIASES, canonicalize, known_aliases
from .resume_decoder import decode_resume
from .schemas import (
    EVIDENCE_MAP_SCHEMA,
    validate_evidence_map,
)

# grill.py is upstream-owned (read-only for this initiative); we reuse
# its public requirement-extraction helper. Keyword matching uses the
# local _keyword_in_text (same rule as grill._keyword_in_text), not
# grill.profile_has_keyword, because the map needs per-line quotes and
# match provenance rather than a single profile-level hit.
import grill  # noqa: E402

_YEARS_REQ_RE = re.compile(
    r"\b(\d{1,2})\+?\s*(?:years?|yrs?)(?:\s+of)?(?:\s+experience)?"
    r"(?:\s+(?:with|in|using))?\s+([A-Za-z][\w+#./-]*)",
    re.IGNORECASE,
)

_HAS_NUMBER_RE = re.compile(r"\d")


def _source_quote(jd_text: str, keyword: str) -> str | None:
    """First JD line mentioning the keyword (verbatim, capped).

    Returns None when no JD line mentions the keyword. The caller must
    not fall back to the bare keyword: the frozen schema requires
    ``source_quote`` to be the verbatim JD quote the requirement was
    extracted from, and the bare keyword is not one. (Unreachable with
    the current extractor, which only yields keywords it found in the
    JD — defensive.)
    """
    lowered = keyword.lower()
    for line in jd_text.splitlines():
        if lowered in line.lower():
            return line.strip()[:300]
    return None


def _keyword_in_text(keyword: str, text: str) -> bool:
    """Word-boundary match for short tokens, substring for longer ones
    (same rule as ``grill._keyword_in_text``)."""
    if len(keyword) <= 3:
        return bool(
            re.search(
                r"(?<![\w+#.])" + re.escape(keyword) + r"(?![\w+#.])",
                text,
                re.IGNORECASE,
            )
        )
    return keyword.lower() in text.lower()


def _verbatim_source_line(resume_text: str, decoded_line: str) -> str | None:
    """Trace a decoded profile line back to its verbatim source line.

    Decoded profile lines are PII-redacted (email/phone values replaced
    with labels) and some profile text is joined from several source
    lines — neither is a verbatim substring of the resume text, so
    quoting them would violate the honesty invariant. Returns the exact
    source line (stripped), or None when no single source line matches
    the decoded line verbatim.
    """
    if not decoded_line:
        return None
    for line in resume_text.splitlines():
        if line.strip() == decoded_line:
            return line.strip()
    return None


def _skill_evidence_lines(decoded: dict[str, Any], resume_text: str) -> Any:
    """Yield (verbatim_line, matched_as) per decoded skill.

    The profile's skills list holds canonical names ("k8s" ->
    "Kubernetes") and the joined list is a decoder construction, not
    resume text — so each skill resolves to its own verbatim source
    line instead. The decoder's ``skills_found`` record carries the
    actual wording the resume used (``matched_as``) and the line it was
    found in; only a recorded line that is verbatim in the resume text
    is yielded. A recorded line that is not verbatim (PII-redacted by
    the decoder, or truncated) is skipped — it cannot be quoted
    honestly, and the raw line must not be substituted in because it
    may carry the very contact values the decoder redacted. Presence
    without a quotable line is detected separately (see
    ``_find_evidence``) and degrades to a grill question, never to a
    fabricated quote.
    """
    for rec in decoded.get("skills_found", []) or []:
        if not isinstance(rec, dict):
            continue
        matched_as = str(rec.get("matched_as") or rec.get("skill") or "").strip()
        quote = str(rec.get("quote", "") or "")
        src = _verbatim_source_line(resume_text, quote)
        if src is not None:
            yield src, matched_as


def _iter_decoded_lines(profile: dict[str, Any]) -> Any:
    """Yield decoded profile lines, verbatim or not.

    Used ONLY for presence detection (see ``_find_evidence``): these
    lines may be PII-redacted or joined by the decoder, so they must
    never be quoted. Quoting goes through ``_iter_profile_lines``
    (verbatim only).
    """
    for idx, entry in enumerate(profile.get("experience", []) or []):
        if not isinstance(entry, dict):
            continue
        summary = str(entry.get("summary", "") or "").strip()
        if summary:
            yield summary
        for bullet in entry.get("bullets", []) or []:
            text = str(bullet or "").strip()
            if text:
                yield text
    summary = str(profile.get("summary", "") or "").strip()
    if summary:
        yield summary
    skills = [str(s) for s in (profile.get("skills", []) or []) if str(s).strip()]
    if skills:
        yield ", ".join(skills)


def _iter_profile_lines(
    profile: dict[str, Any], resume_text: str, decoded: dict[str, Any]
) -> Any:
    """Yield (verbatim_line, profile_field) pairs, richest evidence first.

    Experience bullets/summaries come before the bare skills list: a
    quantified achievement line is better evidence than a keyword in a
    list, and the map must prefer it.

    Every yielded line is a verbatim substring of the resume text.
    Decoded lines that cannot be traced back to a single source line
    are skipped, never quoted: PII-redacted lines (contact values
    replaced with labels) and joined text (the multi-line summary) are
    not verbatim, so quoting them would break the honesty invariant.
    Skills resolve to their own verbatim source lines (see
    ``_skill_evidence_lines``) instead of the joined canonical list.
    """
    for idx, entry in enumerate(profile.get("experience", []) or []):
        if not isinstance(entry, dict):
            continue
        summary = str(entry.get("summary", "") or "").strip()
        src = _verbatim_source_line(resume_text, summary)
        if src is not None:
            yield src, f"experience[{idx}].summary"
        for bullet in entry.get("bullets", []) or []:
            text = str(bullet or "").strip()
            src = _verbatim_source_line(resume_text, text)
            if src is not None:
                yield src, f"experience[{idx}].bullets"
    summary = str(profile.get("summary", "") or "").strip()
    src = _verbatim_source_line(resume_text, summary)
    if src is not None:
        yield src, "summary"
    for src, _matched_as in _skill_evidence_lines(decoded, resume_text):
        yield src, "skills"


def _canonical_skill_name(keyword: str) -> str:
    """Case-insensitive canonical skill resolution.

    ``canonicalize`` resolves aliases case-insensitively ("k8s" ->
    "Kubernetes") but returns unknown inputs unchanged, so a lowercased
    canonical name ("kubernetes") never reaches the canonical table and
    its alias set is silently missed. This resolves against the table
    case-insensitively so "kubernetes", "Kubernetes", and "k8s" all find
    the same alias set.
    """
    canon = canonicalize(keyword)
    if canon in CANONICAL_TO_ALIASES:
        return canon
    lowered = canon.lower()
    for known in CANONICAL_TO_ALIASES:
        if known.lower() == lowered:
            return known
    return canon


def _lookup_candidates(keyword: str) -> list[str]:
    """Keyword plus canonical form plus known aliases, deduped.

    Canonical resolution is case-insensitive (see
    ``_canonical_skill_name``): a lowercased canonical keyword still
    finds its alias set. Aliases are sorted for deterministic order.
    """
    canon = _canonical_skill_name(keyword)
    candidates: list[str] = []
    for cand in [keyword, canon, *sorted(known_aliases(canon))]:
        if cand.lower() not in {c.lower() for c in candidates}:
            candidates.append(cand)
    return candidates


def _matched_via_alias(keyword: str, matched_candidate: str) -> bool:
    """True when the resume's own wording differs from the JD keyword.

    The match came through alias normalization: the resume said "k8s"
    where the JD said "Kubernetes", or the decoder normalized the
    resume's wording to a canonical skill name. A literal match — the
    winning candidate is the keyword itself — is never via-alias, even
    when other aliases exist for the skill. This is computed from the
    match provenance, not from substring presence in a (possibly
    normalized) profile line, so decoder normalization cannot misframe
    the attribution.
    """
    if matched_candidate.lower() == keyword.lower():
        return False
    return (
        _canonical_skill_name(matched_candidate).lower()
        == _canonical_skill_name(keyword).lower()
    )


def _find_evidence(
    decoded: dict[str, Any], keyword: str, resume_text: str
) -> tuple[dict[str, Any] | None, bool]:
    """Find the best evidence for the keyword.

    Returns (evidence_item_dict | None, via_alias). Two passes:

    1. Verbatim lines only (see ``_iter_profile_lines``): prefers a
       quantified line (numbers = concrete evidence) over a bare
       mention. Every candidate quote is a verbatim substring of the
       resume text — lines the decoder redacted or joined are never
       quoted.
    2. Presence check on decoded lines (see ``_iter_decoded_lines``):
       the keyword may be present only on a line that cannot be quoted
       verbatim (PII-redacted, or truncated by the decoder). That is
       presence without quotable evidence — the returned item's quote
       is the decoded line, which the caller's honesty guard
       (``quote in resume_text``) degrades to a grill question, never
       to a fabricated ``supported`` quote.

    ``via_alias`` is True when the winning match came through alias
    normalization rather than the JD keyword's own wording (see
    ``_matched_via_alias``) — ambiguity for the grill.
    """
    profile = decoded.get("profile", {}) or {}
    best_unquantified: dict[str, Any] | None = None
    for candidate in _lookup_candidates(keyword):
        for line, field in _iter_profile_lines(profile, resume_text, decoded):
            if not _keyword_in_text(candidate, line):
                continue
            quantified = _is_quantified(line)
            item = {
                "quote": line[:300],
                "profile_field": field,
                "quantified": quantified,
                "matched_as": candidate,
            }
            if quantified:
                return item, _matched_via_alias(keyword, candidate)
            if best_unquantified is None:
                best_unquantified = item
    if best_unquantified is not None:
        return best_unquantified, _matched_via_alias(
            keyword, best_unquantified["matched_as"]
        )
    for candidate in _lookup_candidates(keyword):
        for line in _iter_decoded_lines(profile):
            if not _keyword_in_text(candidate, line):
                continue
            return (
                {
                    "quote": line[:300],
                    "profile_field": None,
                    "quantified": _is_quantified(line),
                    "matched_as": candidate,
                },
                _matched_via_alias(keyword, candidate),
            )
    return None, False


def _is_quantified(quote: str) -> bool:
    """True if the evidence line contains any number."""
    return bool(_HAS_NUMBER_RE.search(quote))


def _years_requirement_met(
    requirement_text: str, years_claims: list[dict[str, Any]]
) -> bool | None:
    """Check "N+ years X" against the resume's years claims.

    Returns True/False when the requirement parses as a years
    requirement and a matching skill claim exists; None when the
    requirement isn't a years requirement or no skill-specific claim
    exists (then the keyword match alone decides).
    """
    match = _YEARS_REQ_RE.search(requirement_text)
    if not match:
        return None
    needed = int(match.group(1))
    skill = canonicalize(match.group(2))
    for claim in years_claims:
        if claim.get("skill") and claim["skill"].lower() == skill.lower():
            return claim["years"] >= needed
    return None


def _grill_question_id(job_id: str, requirement_text: str) -> str:
    digest = hashlib.sha256(
        f"{job_id}\x00{requirement_text}".encode("utf-8")
    ).hexdigest()[:8]
    return f"gq_{digest}"


def build_evidence_map(
    jd_text: str,
    resume_text: str,
    job_id: str,
    job_title: str = "",
) -> dict[str, Any]:
    """Build a ``veto/evidence-map/v1`` document plus grill companions.

    Returns ``{"evidence_map": <validated doc>,
    "suggested_grill_questions": [...], "validation_errors": [...]}``.
    ``validation_errors`` is empty on success; the doc is still returned
    (invalid docs are a producer bug — surfaces must refuse to render
    them and the errors say why).
    """
    decoded = decode_resume(resume_text)
    job = {"description": jd_text or "", "title": job_title}
    keywords = grill.extract_jd_keywords(job)

    entries: list[dict[str, Any]] = []
    suggested_questions: list[dict[str, Any]] = []

    for seq, item in enumerate(keywords):
        keyword = item["keyword"]
        must_have = item["must_have"]
        source_quote = _source_quote(jd_text or "", keyword)
        if source_quote is None:
            # Defensive: the extractor only yields keywords it found in
            # the JD, so every keyword should have a verbatim JD line.
            # The frozen schema requires source_quote to be the verbatim
            # JD quote — without one the requirement cannot be emitted
            # honestly, so it is dropped rather than fabricated.
            continue
        found, via_alias = _find_evidence(decoded, keyword, resume_text)

        requirement = {
            "text": keyword,
            "source_quote": source_quote,
            "kind": "must_have" if must_have else "nice_to_have",
        }

        if found is None:
            entries.append(
                {
                    "requirement": requirement,
                    "status": "gap",
                    "evidence": [],
                    "grill_question_id": None,
                }
            )
            continue

        # Years requirements are checked against the JD line, not the
        # bare keyword: "10+ years of Python experience" must see the
        # resume's own "N years Python" claim meet or beat it. A
        # shortfall is a gap — gaps are never upgraded.
        years_ok = _years_requirement_met(source_quote, decoded["years_claims"])
        if years_ok is False:
            entries.append(
                {
                    "requirement": requirement,
                    "status": "gap",
                    "evidence": [],
                    "grill_question_id": None,
                }
            )
            continue

        matched_as = found["matched_as"]
        quote = found["quote"]
        # Emission-time honesty guard (defense in depth:
        # _find_evidence only yields verbatim lines): a non-verbatim
        # quote must never be emitted as supported. The keyword was
        # found, so this is not a gap — it is ambiguity for the grill.
        quotable = quote in resume_text
        if via_alias or not found["quantified"] or not quotable:
            qid = _grill_question_id(job_id, keyword)
            entries.append(
                {
                    "requirement": requirement,
                    "status": "grill_question",
                    "evidence": [],
                    "grill_question_id": qid,
                }
            )
            if via_alias:
                reason = "the resume mentions this only via an alias"
            elif not quotable:
                reason = (
                    "the resume appears to mention this, but the exact "
                    "wording could not be quoted verbatim"
                )
            else:
                reason = "the resume mentions this but without numbers or detail"
            suggested_questions.append(
                {
                    "grill_question_id": qid,
                    "requirement": keyword,
                    "kind": "must_have" if must_have else "nice_to_have",
                    "question": (
                        f"Your resume mentions '{matched_as}' ({reason}). "
                        f"Can you describe your experience with '{keyword}' "
                        "concretely — what you did and what changed?"
                    ),
                }
            )
            continue

        entries.append(
            {
                "requirement": requirement,
                "status": "supported",
                "evidence": [
                    {
                        "evidence_id": f"ev_{seq + 1:03d}",
                        "quote": quote,
                        "profile_field": found["profile_field"],
                        "quantified": True,
                    }
                ],
                "grill_question_id": None,
            }
        )

    doc = {
        "schema": EVIDENCE_MAP_SCHEMA,
        "job_id": job_id,
        "job_title": job_title,
        "evidence_source_hash": "sha256:" + decoded["resume_text_hash"],
        "entries": entries,
    }
    errors = validate_evidence_map(doc)
    return {
        "evidence_map": doc,
        "suggested_grill_questions": suggested_questions,
        "validation_errors": errors,
    }
