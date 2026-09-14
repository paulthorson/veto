#!/usr/bin/env python3
"""Cover-letter drafting with an honesty gate and an approval workflow.

Pure local string logic — no LLM calls, no network.

Honesty contract (hard rule, shared with :mod:`tailor`): this module
NEVER invents experience, titles, companies, dates, metrics, or skills.
A draft is built exclusively from facts already present in the
applicant's profile. Job requirements the profile cannot evidence are
addressed with "eager to develop" language — never claimed.

``honesty_scan`` independently verifies any letter (drafted here or
written by hand) against the profile and flags sentences containing
claims that are not traceable to profile text: numbers/metrics,
company names, titles, and skills.

Drafts are drafts until explicitly approved: :func:`approve_letter`
persists an approved letter under ``cover_letters/<safe_job_id>.txt``,
mirroring :mod:`tailor`'s approved-variant flow.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tailor

log = logging.getLogger("job-apply-mcp.cover_letters")

BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "profiles" / "profile.json"
COVER_LETTERS_DIR = BASE_DIR / "cover_letters"


# ---------------------------------------------------------------------------
# Profile helpers (local mirrors of tailor's approach: claims must be
# traceable to profile text)
# ---------------------------------------------------------------------------


def _profile_skills(profile: dict[str, Any]) -> list[str]:
    raw = profile.get("skills", []) or []
    skills: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("name") or item.get("skill")
        else:
            name = item
        if name:
            skills.append(str(name).strip())
    seen: set[str] = set()
    out: list[str] = []
    for s in skills:
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _evidence_text(profile: dict[str, Any]) -> str:
    """All profile text a claim can be traced to (lowercased).

    Name, summary, skills, years of experience, and every experience /
    education field. Aspirational fields (target titles) are deliberately
    excluded: wanting a title is not evidence of holding it.
    """
    parts: list[str] = []
    name = str(profile.get("full_name") or profile.get("name") or "").strip()
    if name:
        parts.append(name)
    for key in ("summary", "headline"):
        if profile.get(key):
            parts.append(str(profile[key]))
    parts.extend(_profile_skills(profile))
    years = profile.get("years_experience") or profile.get("years_of_experience")
    if years is not None:
        parts.append(str(years))
    for exp in profile.get("experience", []) or []:
        if isinstance(exp, dict):
            parts.append(
                " ".join(
                    str(exp.get(k, "") or "") for k in ("title", "company", "dates")
                )
            )
            parts.extend(str(b) for b in exp.get("bullets", []) or [])
    for edu in profile.get("education", []) or []:
        if isinstance(edu, dict):
            parts.append(
                " ".join(
                    str(edu.get(k, "") or "") for k in ("degree", "school", "dates")
                )
            )
        else:
            parts.append(str(edu))
    return "\n".join(parts).lower()


def _bullet_score(bullet: str, skill: str) -> int:
    low = bullet.lower()
    term = skill.lower()
    return len(re.findall(r"(?<![a-z0-9+#])" + re.escape(term) + r"(?![a-z0-9+#])", low))


def _best_evidence(
    profile: dict[str, Any], skill: str, used: set[str]
) -> tuple[str, str] | None:
    """Return (company, bullet) best evidencing ``skill``, else None.

    Scores every experience bullet against the skill and takes the
    highest-scoring unused one. Facts are never rewritten downstream —
    the bullet is quoted/rephrased in first person only.
    """
    best: tuple[str, str] | None = None
    best_score = 0
    for exp in profile.get("experience", []) or []:
        if not isinstance(exp, dict):
            continue
        company = str(exp.get("company", "") or "").strip()
        for bullet in exp.get("bullets", []) or []:
            bullet = str(bullet).strip()
            if not bullet or bullet in used:
                continue
            score = _bullet_score(bullet, skill)
            if score > best_score:
                best_score = score
                best = (company, bullet)
    return best


def _as_first_person(bullet: str) -> str:
    bullet = bullet.strip().rstrip(".")
    if not bullet:
        return bullet
    if re.match(r"^I\s", bullet):
        return bullet
    return "I " + bullet[0].lower() + bullet[1:]


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


def draft_cover_letter(
    profile: dict[str, Any], job_details: dict[str, Any]
) -> dict[str, Any]:
    """Draft a cover letter built ONLY from profile facts.

    Structure: hook line, two proof paragraphs (each mapped to a stated
    job requirement and grounded in a real profile bullet), an
    "eager to develop" paragraph for requirements the profile cannot
    evidence, and a close. Returns::

        {"letter", "matched_requirements", "missing_requirements",
         "proofs", "honesty_self_check"}

    ``proofs`` maps each requirement to the evidence bullet used.
    ``honesty_self_check`` is :func:`honesty_scan` run on the draft —
    it must be empty, and is included so callers can verify that.
    """
    keywords = tailor.extract_job_keywords(job_details, profile)
    matched = keywords["matched"]
    missing = keywords["missing"]

    name = str(profile.get("full_name") or profile.get("name") or "").strip()
    company = str(job_details.get("company") or "").strip()
    job_title = str(job_details.get("title") or "").strip()

    exps = [e for e in profile.get("experience", []) or [] if isinstance(e, dict)]
    top_title = str((exps[0].get("title") if exps else "") or "").strip()
    if not top_title:
        targets = profile.get("target_titles") or profile.get("target_roles") or []
        top_title = str(targets[0] if targets else "professional").strip()
    years = profile.get("years_experience") or profile.get("years_of_experience")

    # --- hook: identity + why this role, profile facts only ---
    hook_bits = [f"I'm a {top_title}"]
    if years:
        hook_bits.append(f"with {years} years of experience")
    if matched:
        hook_bits.append(f"skilled in {', '.join(matched[:3])}")
    hook = (
        ", ".join(hook_bits)
        + ", and I'm excited to apply for the "
        + (f"{job_title} role" if job_title else "role")
        + (f" at {company}" if company else "")
        + "."
    )

    # --- proof paragraphs: one per top matched requirement, each with
    # --- a real evidence bullet. Reuse bullets across paragraphs never.
    proofs: list[dict[str, str]] = []
    used: set[str] = set()
    paragraphs: list[str] = [hook]
    for req in matched[:2]:
        evidence = _best_evidence(profile, req, used)
        if evidence is None:
            # The skill is genuinely in the profile's skill list but no
            # experience bullet mentions it: say exactly that, nothing more.
            proofs.append(
                {"requirement": req, "company": "", "evidence_bullet": ""}
            )
            paragraphs.append(
                f"Your posting emphasizes {req}. {req} is among my core "
                f"skills, and I'd welcome the chance to put it to work "
                f"on your team."
            )
            continue
        ev_company, ev_bullet = evidence
        used.add(ev_bullet)
        proofs.append(
            {"requirement": req, "company": ev_company, "evidence_bullet": ev_bullet}
        )
        at = f"At {ev_company}, " if ev_company else ""
        paragraphs.append(
            f"Your posting emphasizes {req}. {at}{_as_first_person(ev_bullet)}."
        )

    # --- growth paragraph: missing requirements are NEVER claimed ---
    if missing:
        gap = missing[:2]
        plural = len(gap) > 1
        paragraphs.append(
            f"{' and '.join(gap)} {'are' if plural else 'is'} an area "
            f"I'm still developing \u2014 I'm eager to deepen my expertise "
            f"there, and I ramp up on new stacks quickly."
        )

    # --- close ---
    close = (
        "I'd welcome the chance to discuss how I can contribute"
        + (f" to {company}" if company else "")
        + ". Thank you for your consideration."
    )
    paragraphs.append(close)

    letter = "Dear Hiring Manager,\n\n" + "\n\n".join(paragraphs)
    if name:
        letter += f"\n\nSincerely,\n{name}"

    return {
        "letter": letter,
        "matched_requirements": matched,
        "missing_requirements": missing,
        "proofs": proofs,
        "honesty_self_check": honesty_scan(letter, profile, job_details),
    }


# ---------------------------------------------------------------------------
# Honesty scan
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_PHRASE_RE = re.compile(r"\b([A-Z][A-Za-z0-9&'.-]*(?:\s+[A-Z][A-Za-z0-9&'.-]*){1,2})")
_TOKEN_RE = re.compile(r"\b([A-Z][A-Za-z0-9&'.-]*)\b")

#: Greeting/closing boilerplate that is not a factual claim.
_BOILERPLATE = {"dear hiring manager", "hiring manager", "thank you"}

#: Hedge phrases that make a skill mention aspirational, not a claim.
_HEDGES = (
    "eager to",
    "still developing",
    "new to",
    "learning to",
    "ramp up",
    "excited to learn",
    "deepen my expertise",
)

#: Leading prepositions/articles stripped before a proper-noun phrase is
#: checked, so "At Analytica" is verified as "Analytica".
_PHRASE_LEAD_STOPS = {
    "At", "In", "On", "To", "For", "From", "With", "And", "The", "A",
    "An", "As", "Of", "By", "Dear",
}

#: Capitalized words that carry no factual content.
_CAPITAL_STOPS = {
    "I", "I'm", "I've", "I'll", "I'd", "It", "It's", "Its", "This", "That",
    "These", "Those", "Your", "My", "Our", "We", "While", "With", "For",
    "From", "And", "Thank", "Thanks", "Sincerely", "Dear", "Hiring",
    "Manager", "At", "In", "On", "To", "As", "Of", "The", "A", "An",
}


def _split_sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", text.strip())
        if s.strip()
    ]


def honesty_scan(
    letter: str,
    profile: dict[str, Any],
    job_details: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Flag sentences with claims not traceable to the profile.

    Checks three claim categories, reusing :mod:`tailor`'s philosophy
    that every fact must exist in profile text:

    - **numbers/metrics** (``300%``, ``$120,000``, ``10M``): flagged
      unless the exact figure appears in the profile;
    - **proper nouns** (companies, titles): multiword capitalized
      phrases — and mid-sentence capitalized tokens — flagged unless
      found in profile text or whitelisted from ``job_details``
      (the hiring company's own name/title are expected in a letter);
    - **skills**: vocabulary terms absent from both the profile's skill
      list and its text flagged unless the sentence hedges
      ("eager to", "still developing", ...).

    Returns a list of ``{"sentence", "claim", "reason"}``. Empty means
    the letter is clean.
    """
    evidence = _evidence_text(profile)
    profile_skill_set = {s.lower() for s in _profile_skills(profile)}

    # Words from the job's own company/title are expected in the letter.
    job_words: set[str] = set()
    if job_details:
        for key in ("company", "title"):
            job_words.update(
                re.findall(
                    r"[a-z0-9+#]+", str(job_details.get(key, "") or "").lower()
                )
            )

    # Skill terms the letter is not entitled to claim: vocabulary minus
    # the profile's own skills (terms already in profile text, e.g. a
    # bullet mentioning "ETL", are legitimate mentions, checked below).
    vocab = set(tailor._SKILL_VOCABULARY) | profile_skill_set
    unowned_skills = [t for t in vocab if t not in profile_skill_set]

    flags: list[dict[str, str]] = []
    for sent in _split_sentences(letter):
        slow = sent.lower()
        hedged = any(h in slow for h in _HEDGES)

        # --- numbers / metrics ---
        for match in _NUMBER_RE.finditer(sent):
            raw = match.group(0)
            token = raw.lower().replace("$", "").replace(",", "")
            if not token:
                continue
            if not re.search(
                r"(?<![\d.])" + re.escape(token) + r"(?![\d.])", evidence
            ):
                flags.append(
                    {
                        "sentence": sent,
                        "claim": raw,
                        "reason": (
                            "number/metric not found anywhere in the profile — "
                            "remove it or ground it in real experience"
                        ),
                    }
                )

        # --- multiword proper nouns (companies, titles) ---
        for match in _PHRASE_RE.finditer(sent):
            phrase = match.group(1).rstrip(".-'")
            if not phrase:
                continue
            if phrase.lower() in _BOILERPLATE:
                continue
            words = phrase.split()
            while len(words) > 1 and words[0] in _PHRASE_LEAD_STOPS:
                words.pop(0)
            phrase = " ".join(words)
            plow = phrase.lower()
            if plow in evidence:
                continue
            if all(w in job_words for w in plow.split()):
                continue
            flags.append(
                {
                    "sentence": sent,
                    "claim": phrase,
                    "reason": (
                        "proper noun not traceable to the profile — verify the "
                        "company/title is real experience, not invented"
                    ),
                }
            )

        # --- single capitalized tokens (mid-sentence only) ---
        if not hedged:
            for match in _TOKEN_RE.finditer(sent):
                if match.start() == 0:
                    continue  # sentence-initial capitalization is grammatical
                token = match.group(1)
                if token in _CAPITAL_STOPS:
                    continue
                tlow = token.lower()
                if tlow in profile_skill_set or tlow in job_words:
                    continue
                if re.search(
                    r"(?<![a-z])" + re.escape(tlow) + r"(?![a-z])", evidence
                ):
                    continue
                flags.append(
                    {
                        "sentence": sent,
                        "claim": token,
                        "reason": (
                            "capitalized term not traceable to the profile — "
                            "verify it is not an invented name or title"
                        ),
                    }
                )

        # --- unowned skill claims (unhedged only) ---
        if not hedged:
            for term in unowned_skills:
                if term in evidence:
                    continue  # mentioned in the profile's own text: fine
                if re.search(
                    r"(?<![a-z0-9+#])" + re.escape(term) + r"(?![a-z0-9+#])",
                    slow,
                ):
                    flags.append(
                        {
                            "sentence": sent,
                            "claim": term,
                            "reason": (
                                "claims a skill that is neither in the profile's "
                                "skill list nor its text — rephrase as "
                                '"eager to develop" instead of claiming it'
                            ),
                        }
                    )
                    break  # one skill flag per sentence is enough

    return flags


# ---------------------------------------------------------------------------
# Approval store (mirrors tailor's approved-variant flow)
# ---------------------------------------------------------------------------


def _safe_job_id(job_id: str) -> str:
    """Sanitize a job id for use as a filename stem."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(job_id))
    return safe[:120] or "job"


def _letter_path(job_id: str) -> Path:
    return COVER_LETTERS_DIR / f"{_safe_job_id(job_id)}.txt"


def approve_letter(job_id: str, letter: str) -> dict[str, Any]:
    """Persist a user-approved cover letter for ``job_id``.

    Drafts are drafts until this is called with the exact approved text.
    Writes the letter verbatim to ``cover_letters/<safe_job_id>.txt``
    (atomic via a temp file + rename). Returns
    ``{"job_id", "path", "approved_at"}``.
    """
    if not str(letter or "").strip():
        raise ValueError("cannot approve an empty letter")
    COVER_LETTERS_DIR.mkdir(parents=True, exist_ok=True)
    path = _letter_path(job_id)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(letter if letter.endswith("\n") else letter + "\n")
    os.replace(tmp, path)
    approved_at = datetime.now(timezone.utc).isoformat()
    log.info("approved cover letter saved for job %s -> %s", job_id, path)
    return {"job_id": str(job_id), "path": str(path), "approved_at": approved_at}


def load_letter(job_id: str) -> str | None:
    """Load the approved letter for ``job_id``, or ``None`` if absent.

    An unreadable file is treated as absent (logged, not raised) so a
    bad file can never break the apply flow.
    """
    path = _letter_path(job_id)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("ignoring unreadable approved letter %s: %s", path, exc)
        return None


def list_letters() -> list[str]:
    """Return the sorted job ids that have an approved cover letter."""
    if not COVER_LETTERS_DIR.is_dir():
        return []
    ids: list[str] = []
    for path in COVER_LETTERS_DIR.glob("*.txt"):
        if path.is_file():
            ids.append(path.stem)
    return sorted(ids)


# ---------------------------------------------------------------------------
# Plugin wiring (briefs.py pattern)
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register the cover-letter MCP tools on an MCP server instance."""

    @mcp.tool()
    def draft_cover_letter_tool(profile: dict, job_details: dict) -> dict:
        """Draft a cover letter from profile facts only.

        Args:
            profile: Applicant profile dict (skills, experience, ...).
            job_details: Job dict with title/company/description.

        Returns:
            Dict with the letter, matched/missing requirements, the
            requirement-to-evidence mapping, and an honesty self-check
            (empty when clean). Missing requirements are phrased as
            "eager to develop" — never claimed.
        """
        return draft_cover_letter(profile, job_details)

    @mcp.tool()
    def scan_cover_letter(
        letter: str, profile: dict, job_details: dict | None = None
    ) -> list:
        """Scan a cover letter for claims not traceable to the profile.

        Args:
            letter: The letter text to verify.
            profile: Applicant profile dict.
            job_details: Optional job dict; its company/title words are
                expected in a letter and are not flagged.

        Returns:
            List of {sentence, claim, reason} flags; empty means clean.
        """
        return honesty_scan(letter, profile, job_details)

    @mcp.tool()
    def approve_cover_letter(job_id: str, letter: str) -> dict:
        """Approve a cover-letter draft, persisting it for the job.

        Args:
            job_id: The job id the letter is for.
            letter: The exact approved letter text.

        Returns:
            Dict with job_id, path, and approved_at timestamp.
        """
        return approve_letter(job_id, letter)

    @mcp.tool()
    def list_cover_letters() -> list:
        """List job ids with an approved cover letter."""
        return list_letters()

    @mcp.tool()
    def load_cover_letter(job_id: str) -> dict:
        """Load the approved cover letter for a job id.

        Args:
            job_id: The job id to load.

        Returns:
            Dict with job_id and letter (None when absent).
        """
        return {"job_id": job_id, "letter": load_letter(job_id)}


def _print_result(result: Any, as_json: bool) -> None:
    if as_json or not isinstance(result, str):
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print(result)


def _load_profile(path_arg: str | None) -> dict[str, Any] | None:
    path = Path(path_arg) if path_arg else PROFILE_PATH
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _load_job(path_arg: str | None) -> dict[str, Any] | None:
    if not path_arg:
        return None
    try:
        with open(path_arg, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def cmd_cover_letter(args: Any) -> int:
    """CLI handler for `cover-letter`."""
    as_json = getattr(args, "json", False)
    action = args.action

    if action == "list":
        _print_result(list_letters(), as_json)
        return 0

    if action == "load":
        if not args.job_id:
            print("error: --job-id is required for load")
            return 1
        _print_result({"job_id": args.job_id, "letter": load_letter(args.job_id)}, as_json)
        return 0

    if action == "approve":
        if not args.job_id or not args.letter_file:
            print("error: --job-id and --letter-file are required for approve")
            return 1
        try:
            letter = Path(args.letter_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read letter file: {exc}")
            return 1
        try:
            _print_result(approve_letter(args.job_id, letter), as_json)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        return 0

    if action == "scan":
        if not args.letter_file:
            print("error: --letter-file is required for scan")
            return 1
        profile = _load_profile(args.profile_file)
        if profile is None:
            print("error: no profile found (use --profile-file)")
            return 1
        try:
            letter = Path(args.letter_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read letter file: {exc}")
            return 1
        job = _load_job(args.job_file)
        flags = honesty_scan(letter, profile, job)
        if as_json:
            _print_result(flags, True)
        elif not flags:
            print("Letter is clean: no untraceable claims found.")
        else:
            for flag in flags:
                print(f"- {flag['claim']!r}: {flag['reason']}")
                print(f"  in: {flag['sentence']}")
        return 0

    if action == "draft":
        profile = _load_profile(args.profile_file)
        if profile is None:
            print("error: no profile found (use --profile-file)")
            return 1
        job = _load_job(args.job_file)
        if job is None:
            print("error: --job-file with job details JSON is required for draft")
            return 1
        result = draft_cover_letter(profile, job)
        if as_json:
            _print_result(result, True)
        else:
            _print_result(result["letter"], False)
        return 0

    print(f"error: unknown action {action!r}")
    return 1


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `cover-letter` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    parser = subparsers.add_parser(
        "cover-letter", help="Draft, scan, approve, and manage cover letters."
    )
    parser.add_argument(
        "action",
        choices=["draft", "scan", "approve", "list", "load"],
        help="Action to perform.",
    )
    parser.add_argument("--job-id", help="Job id (approve/load).")
    parser.add_argument("--job-file", help="JSON file with job details (draft/scan).")
    parser.add_argument(
        "--profile-file",
        help="JSON profile file (default: profiles/profile.json).",
    )
    parser.add_argument(
        "--letter-file", help="Letter text file (scan/approve input)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output."
    )

    return {"cover-letter": cmd_cover_letter}


# Allow `python cover_letters.py ...` for quick local use.
if __name__ == "__main__":
    _cli = argparse.ArgumentParser(prog="cover-letter")
    _sub = _cli.add_subparsers()
    _handlers = register_cli(_sub)
    _parsed = _cli.parse_args()
    if not hasattr(_parsed, "action"):
        _cli.print_help()
        raise SystemExit(2)
    raise SystemExit(_handlers["cover-letter"](_parsed))
