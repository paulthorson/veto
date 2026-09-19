#!/usr/bin/env python3
"""Initiative 12 — privacy scanner shared by shareable artifacts and telemetry.

One definition of "content field": anything that could carry resume text,
job-description text, names, contact details, or other private job-search
content. Both the shareable-artifact builder and the analytics tripwire use
this scanner, so the rule is defined once.

A field is *content-bearing* when:

* its name matches a forbidden pattern (resume*, cover_letter, *_text,
  description, summary, email, phone, address, ...), or
* its value looks like content: an email/phone match, a person's name in
  a field whose name contains a `name` token (candidate_name, full_name,
  username, ... — plus the whitelisted `name` field itself, whose
  multi-word capitalized values are person names, never factor names),
  a long free-text blob, a verbatim-looking quotation (straight or curly
  quotes), or resume/JD marker phrases.

Public artifacts and telemetry events must contain zero content fields.

Observability: every :class:`ContentDetected` raised through
:func:`assert_clean` increments the module-level ``detection_count`` and
invokes the optional hook set via :func:`set_detection_hook`. Triage
expectation: a detection in the share path means caller code let user data
reach an artifact builder — fix the caller; a detection in telemetry means
the tripwire protocol owns it (see telemetry.py).

Schema evolution (C7): ``SAFE_FIELD_NAMES`` is a cross-module contract —
adding a name here exempts it from the forbidden-*name* check for both
share artifacts and telemetry events, and (except "name" itself) from the
person-name *value* check — so additions need reviewer sign-off.
``SHARE_VERSION`` (in share.py) is bumped on any artifact-schema change;
old consumers treat an unknown version as malformed.

Detection limits (C9 — these are estimates, not guarantees):
``LONG_TEXT_CHARS`` (280) and ``QUOTED_SPAN_CHARS`` (40) are heuristic
thresholds chosen to sit well below real resume/JD prose and well above
legit metadata labels; they are documented guesses, not measured optima.
There is deliberately NO depth cutoff: the scan walks containers to full
depth (an iterative walk, so no recursion limit to hit) with a visited set
guarding against cyclic structures — deeply nested content cannot slip
past a recursion bound, and a self-referential dict cannot hang the scan.
Real artifacts nest at most 4 levels deep, but the guarantee does not
depend on that. Short, unquoted, marker-free prose under the thresholds is
NOT reliably detectable and is an accepted residual risk (see the WS2
decision record).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

_log = logging.getLogger(__name__)

#: Field names that are never allowed in a public artifact or telemetry event.
FORBIDDEN_FIELD_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"resume",
        r"cover_?letter",
        r"cv\b",
        r"job_?description",
        r"\bjd_?text\b",
        r"posting_?text",
        r"description_full",
        r"full_?text",
        r"\btext\b",
        r"summary",
        r"objective",
        r"experience",
        r"work_?history",
        r"education",
        r"\bname\b",
        r"first_?name",
        r"last_?name",
        r"email",
        r"phone",
        r"address",
        r"location_detail",
        r"linkedin",
        r"github_?handle",
        r"portfolio_?url",
        r"ssn",
        r"dob",
        r"date_?of_?birth",
        r"salary_?history",
        r"references",
        r"\bnotes?\b",
        # NOTE (C8): bare "cover" and "letter" were removed — they
        # false-positived on innocent tokens like "newsletter_opt_in".
        # The compound "cover_?letter" pattern above is the precise one.
        r"statement",
        r"bio",
    )
)

#: Exact field names that are part of Initiative 12's pinned schemas
#: (share artifacts, telemetry events) and are known metadata carriers.
#: The forbidden-*name* check is skipped for these, and (F2) the
#: person-name *value* check is skipped for these too — EXCEPT the field
#: "name" itself, which keeps the person-name value check (C2: factor
#: entries legitimately use "name" for factor names like "skills", but a
#: multi-word capitalized value there is a person's name). Values in SAFE
#: fields are still scanned for emails, phones, markers, quotations, and
#: long text like everything else. Unknown fields get the full check.
SAFE_FIELD_NAMES: frozenset[str] = frozenset({
    # share.py artifact envelope + payloads
    "share_version", "kind", "methodology", "privacy_note", "payload",
    "role", "company", "fit_score", "verdict", "top_reasons", "factors",
    "name", "score", "evidence_summary", "sessions_planned", "focus_areas",
    "area", "why", "drills", "weeks_active", "workflows_completed",
    "outcomes_recorded", "note",
    # telemetry.py event schemas
    "event", "ts", "tool", "surface", "session_id", "duration_s",
    "completed", "path_id", "role_maturity", "tech_comfort", "guide_id",
    "workflow", "schema",
    # reporting aggregates
    "counts", "metric", "opened", "sessions", "qualified_sessions", "rate",
    "tool_opens", "workflow_completion", "qualified_activation", "guide_views",
    # person-name value-check exemption (F2/D2): "product_name" is a pinned
    # metadata label field, never a person's name. Adding it here exempts it
    # from the person-name *value* check only — its values are still scanned
    # for emails/phones/markers/long text like everything else. (It matches
    # no forbidden *name* pattern, so the forbidden-name check is unaffected.)
    "product_name",
})

#: Marker phrases that indicate resume/JD prose inside a value.
CONTENT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"work experience",
        r"professional experience",
        r"bachelor'?s|master'?s|ph\.?d\.?",
        r"gpa",
        r"references available",
        r"responsible for",
        r"spearheaded",
        r"\breports to\b",
        r"we are looking for",
        r"the ideal candidate",
        r"about the role",
        r"what you'?ll (do|bring)",
        r"minimum qualifications",
        r"preferred qualifications",
        r"\d+\+?\s*years? (of )?experience",
    )
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(\+?1[-.\s]?)?(\(?\d{3}\)?[-.\s]?){2}\d{4}")

#: Free-text values at/above this length are treated as content, not metadata.
#: Estimate (see module docstring): well below real resume/JD prose,
#: well above any legitimate metadata label.
LONG_TEXT_CHARS = 280

#: Quoted spans ("..."/'...', straight or curly) at/above this length look
#: like verbatim quotations of source prose, not paraphrases. Estimate:
#: legitimate evidence summaries quote at most a short phrase; a 40+ char
#: quoted span is almost always pasted content. Short unquoted prose under
#: LONG_TEXT_CHARS remains an accepted residual risk (see docstring).
QUOTED_SPAN_CHARS = 40

#: Matches a quoted span of at least QUOTED_SPAN_CHARS characters, in
#: straight ("..."/'...') or curly (“...”/‘...’) quotes. F4: curly quotes
#: used to bypass the heuristic — they are matched now. A long quoted span
#: reads as a verbatim quotation of source prose, not a paraphrase. Still
#: an estimate, not a guarantee (see module docstring).
_QUOTE_RE = re.compile(
    "\"([^\"]{%d,})\"|'([^']{%d,})'"
    "|\u201c([^\u201d]{%d,})\u201d"
    "|\u2018([^\u2019]{%d,})\u2019" % (
        QUOTED_SPAN_CHARS, QUOTED_SPAN_CHARS,
        QUOTED_SPAN_CHARS, QUOTED_SPAN_CHARS,
    )
)

#: Fields whose name contains a `name` token get a person-name *value*
#: check (F2). "name" is in SAFE_FIELD_NAMES because share factor entries
#: use it for factor names ("skills"), so the forbidden-*name* check is
#: skipped for it — but a multi-word capitalized value there
#: ("Alex Rivera") is a person's name, never a factor name, so "name"
#: keeps the value check (C2). Any other field whose name contains a
#: "name" token (candidate_name, full_name, display_name, contact_name,
#: username, ...) gets the check too — UNLESS it is in SAFE_FIELD_NAMES
#: (e.g. "product_name": a pinned metadata label field, never a person's
#: name — see _name_value_check_applies). Values in SAFE fields are still
#: scanned for emails/phones/markers/quotations/long text like everything
#: else; only the person-name value check is scoped this way.
_TOKEN_SPLIT_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _has_name_token(field_name: str) -> bool:
    """True if any token of the field name contains "name".

    The name is split on underscores and camelCase boundaries, so
    candidate_name, fullName, display_name, contact_name, userName, and
    username all match. A token merely *containing* "name" counts (so the
    compound "username" is covered). Documented trade-offs (D2): compounds
    like "filename" also match — a multi-word capitalized value there
    would fire, accepted as residual false-positive risk; and fields with
    no "name" anywhere ("hiring_manager") do NOT match — accepted residual
    false-negative risk. The check is deliberately token-based, not a raw
    substring search, so the rule stays auditable.
    """
    for part in field_name.split("_"):
        for tok in _TOKEN_SPLIT_RE.findall(part):
            if "name" in tok.lower():
                return True
    return False


def _name_value_check_applies(field_name: str) -> bool:
    """Whether the person-name value check applies to this field.

    "name" itself always applies (C2). Other name-token fields apply only
    when they are NOT pinned SAFE_FIELD_NAMES schema fields — e.g.
    "product_name" is exempt (its name is metadata, never a person), while
    "candidate_name" is not in SAFE_FIELD_NAMES and gets the check.
    """
    if not _has_name_token(field_name):
        return False
    if field_name == "name":
        return True
    return field_name not in SAFE_FIELD_NAMES

#: Two or more capitalized words, e.g. "Alex Rivera". Conservative on
#: purpose: single tokens ("skills") and lowercase labels never match.
_PERSON_NAME_RE = re.compile(r"^[A-Z][a-zA-Z'.-]+(?: [A-Z][a-zA-Z'.-]+)+$")

# ---------------------------------------------------------------------------
# Observability (O3): a minimal audit signal when ContentDetected fires.
# ---------------------------------------------------------------------------

#: Total ContentDetected raisings through assert_clean in this process.
#: Local-only (resets on restart); tests and ops dashboards read it to
#: confirm the control is live. Not a security log — finding *descriptions*
#: never carry offending values.
detection_count: int = 0

_detection_hook: Callable[[str, list[str]], None] | None = None


def set_detection_hook(fn: Callable[[str, list[str]], None] | None) -> None:
    """Register an optional hook called as fn(what, findings) each time
    assert_clean raises ContentDetected. Pass None to clear. The hook lets
    the embedding app forward detections to its own incident pipeline;
    exceptions raised by the hook are swallowed so reporting can never
    break the fail-closed raise."""
    global _detection_hook
    _detection_hook = fn


def _record_detection(what: str, findings: list[str]) -> None:
    global detection_count
    detection_count += 1
    _log.warning("ContentDetected in %s: %s", what, "; ".join(findings[:3]))
    hook = _detection_hook
    if hook is not None:
        try:
            hook(what, findings)
        except Exception:  # never let observability break fail-closed
            _log.exception("detection hook failed")


def _scan_string(value: str) -> list[str]:
    """Content findings for one string value (no field-name checks)."""
    findings: list[str] = []
    v = value.strip()
    if _EMAIL_RE.search(v):
        findings.append("value contains an email address")
    if _PHONE_RE.search(v):
        findings.append("value contains a phone-number-like string")
    if _QUOTE_RE.search(v):
        findings.append(
            "value contains a long quoted span — possible verbatim quotation"
        )
    for pat in CONTENT_MARKERS:
        if pat.search(v):
            findings.append(f"value matches resume/JD marker: {pat.pattern!r}")
            break
    if len(v) >= LONG_TEXT_CHARS and " " in v:
        findings.append(
            f"value is long free text ({len(v)} chars) — treated as content"
        )
    return findings


def _check_field_name(name: Any) -> tuple[str, list[str]]:
    """Coerce a dict key and run the field-*name* checks.

    Returns (coerced_name, findings). F3: keys are coerced with str()
    before the forbidden-pattern search — ``pat.search(123)`` used to
    raise TypeError instead of failing closed. A non-string key is itself
    a finding: pinned schemas use string keys, so a non-string key is
    anomalous and fails closed via assert_clean (with the audit signal
    recording it) instead of crashing the scanner.
    """
    findings: list[str] = []
    if not isinstance(name, str):
        findings.append(
            f"field name {name!r} is not a string — treated as content"
        )
    key = name if isinstance(name, str) else str(name)
    if key not in SAFE_FIELD_NAMES:
        for pat in FORBIDDEN_FIELD_PATTERNS:
            if pat.search(key):
                findings.append(
                    f"field name {key!r} matches forbidden pattern {pat.pattern!r}"
                )
                break
    return key, findings


def _check_name_like_value(key: str, value: Any) -> list[str]:
    """Person-name value check for name-token fields (F2, C2)."""
    if _name_value_check_applies(key) and isinstance(value, str):
        if _PERSON_NAME_RE.match(value.strip()):
            return [
                f"value in name-like field {key!r} looks like a person's name"
            ]
    return []


def scan_value(value: Any) -> list[str]:
    """Return a list of finding strings for content-like values.

    Fail-closed depth policy (F1): the scan walks containers to FULL
    depth — there is deliberately no depth cutoff that could silently pass
    over-deep content. The walk is iterative (no recursion limit to hit)
    and a visited set of container ids guards against cyclic structures: a
    self-referential dict/list cannot hang the scan, and a cycle back-edge
    is skipped only after its node was already scanned, so cycles cannot
    hide content either. Every reachable node is visited; nothing is
    silently passed. Finding formats are unchanged from the recursive
    version (``k: ...`` / ``[i]: ...`` prefixes accumulate with depth).
    """
    findings: list[str] = []
    seen: set[int] = set()
    # Stack of (node, prefix segments); children are pushed in reverse so
    # findings come out in insertion order, as before.
    stack: list[tuple[Any, tuple[str, ...]]] = [(value, ())]
    while stack:
        node, segs = stack.pop()
        if isinstance(node, str):
            prefix = "".join(segs)
            for f in _scan_string(node):
                findings.append(prefix + f if prefix else f)
        elif isinstance(node, dict):
            if id(node) in seen:
                continue  # cycle back-edge; the node was already scanned
            seen.add(id(node))
            for k, sub in reversed(list(node.items())):
                key, name_findings = _check_field_name(k)
                here = "".join(segs) + f"{k}: "
                for f in name_findings:
                    findings.append(here + f)
                for f in _check_name_like_value(key, sub):
                    findings.append(here + f)
                stack.append((sub, segs + (f"{k}: ",)))
        elif isinstance(node, (list, tuple)):
            if id(node) in seen:
                continue  # cycle back-edge; the node was already scanned
            seen.add(id(node))
            for i in range(len(node) - 1, -1, -1):
                stack.append((node[i], segs + (f"[{i}]: ",)))
    return findings


def scan_field(name: Any, value: Any) -> list[str]:
    """Scan one named field. Returns finding strings (empty = clean).

    The field name is coerced with str() before pattern checks (F3), and
    a non-string key is itself a finding — fail closed, never TypeError.
    """
    key, findings = _check_field_name(name)
    findings.extend(_check_name_like_value(key, value))
    findings.extend(scan_value(value))
    return findings


def scan_payload(payload: dict[str, Any]) -> list[str]:
    """Scan a whole artifact/telemetry payload. Empty list = clean."""
    findings: list[str] = []
    for name, value in payload.items():
        findings.extend(scan_field(name, value))
    return findings


def assert_clean(payload: dict[str, Any], what: str) -> None:
    """Raise ContentDetected if the payload carries any content field.

    Fail closed: on detection the module counter and optional hook fire
    (observability, O3) and the exception is raised — there is no
    clean-on-error path."""
    findings = scan_payload(payload)
    if findings:
        _record_detection(what, findings)
        raise ContentDetected(what, findings)


class ContentDetected(Exception):
    """A content field was found where only metadata is allowed."""

    def __init__(self, what: str, findings: list[str]):
        self.what = what
        self.findings = findings
        # Findings intentionally omit the offending values: the exception
        # itself must never become a content carrier.
        super().__init__(
            f"Content detected in {what}: "
            + "; ".join(findings[:5])
            + (f" (+{len(findings) - 5} more)" if len(findings) > 5 else "")
        )
