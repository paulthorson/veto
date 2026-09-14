#!/usr/bin/env python3
"""Frozen schema contracts for Initiative 04, plus validators.

Three schemas, all ``veto/<name>/v1``:

* ``veto/evidence-map/v1`` — the requirement→evidence link contract.
  **Published for Initiative 05** (application studio): 05's evidence
  library consumes these link entries. Freeze date: 2026-09-13.
* ``veto/fit-result/v1`` — the fit decoder's result envelope: score,
  the five-axis component breakdown, score provenance (per Initiative
  02's contract), limitations, the JD verdict, and the **embedded**
  requirement→evidence map — the whole ``veto/evidence-map/v1``
  document, validated in place by :func:`validate_fit_result` when
  present. Embedding (not a pointer) keeps a single source of truth:
  consumers such as the share-card builder read the map's entries
  straight from the envelope.
* ``veto/share-card/v1`` — the private share-card payload. Enforces the
  Q1 exit-gate privacy property: **no raw resume or job-description
  text may appear in the payload.** :func:`validate_share_card` is the
  scrub-style test harness.

All validators are stdlib-only and never raise on malformed input —
they return a list of violation strings (empty = valid). Producers
should call :func:`validate_evidence_map` / :func:`validate_fit_result` /
:func:`validate_share_card` before persisting or exporting.

``components`` (fit result): the score's five-axis decomposition,
mirroring ``match.score_job`` — exactly the keys
``skills``, ``seniority``, ``salary``, ``location``, ``recency``.
Each axis is a *point contribution* in [0, 100], not a weight; the
frozen invariant is ``fit_score == clamp(round(sum(axes)))`` — the
parts must explain the total. (Decision record: ``DECISIONS.md``.)

Extension keys: any key not in a schema's documented contract MUST use
the ``x_`` prefix (e.g. ``x_my_tool_note``). Anything else is a
contract violation. ``x_`` keys are uninterpreted producer extensions —
never system assertions. The policy applies at every object level
(``PROVENANCE_KEYS``, ``FIT_JD_VERDICT_KEYS``,
``EVIDENCE_SUMMARY_KEYS``, ``SHARE_CARD_JD_VERDICT_KEYS``,
``SHARE_CARD_EVIDENCE_ITEM_KEYS`` pin the nested shapes), not just the
document top level. This is what makes the tri-state rule
contractual rather than conventional: a ``grill_question`` entry's
allowlist (``EVIDENCE_ENTRY_KEYS``) contains no free-text assertion
field, so an assertion smuggled as ``entry["note"]`` fails validation.

``evidence_source_hash`` binding: the producer computes it as
``"sha256:" + sha256(resume_text)[:16]`` over the *exact* resume text
analyzed (see :func:`evidence_source_hash_for`, mirroring
``resume_decoder`` → ``evidence_map``). :func:`validate_evidence_map`
enforces the format; :func:`verify_evidence_binding` recomputes the
binding against candidate source text *and* requires every evidence
quote to be a verbatim substring of that text, so consumers invalidate
stale maps instead of silently reusing them. Consumers holding cached
maps should call :func:`verify_evidence_binding` before rendering —
the binding is verified where it is consumed, not assumed from the
recorded hash.

Assumption flags (per program brief):
* The ``provenance`` shape on fit results follows Initiative 02's
  *contracted* (not yet implemented) score-provenance contract:
  ``{"kind": "static"|"personalized"|"cohort-informed", "n": int}``.
  Until 02's engine lands, producers emit ``kind="static"`` and
  ``n=0``, and surfaces must display that plainly.
* 04 is a **read-only** consumer of Initiative 01's outcome events
  (``outcome-min-v0``); it never writes events. The career graph
  refuses to render (rather than mis-rendering) when it sees an
  unknown ``schema_version``.
"""

from __future__ import annotations

import re
from typing import Any

#: The three frozen schemas this package owns.
EVIDENCE_MAP_SCHEMA = "veto/evidence-map/v1"
FIT_RESULT_SCHEMA = "veto/fit-result/v1"
SHARE_CARD_SCHEMA = "veto/share-card/v1"

#: Score-provenance kinds, per Initiative 02's contracted provenance
#: contract. ``static`` = rule-based score with no outcome data;
#: ``personalized`` = informed by the user's own outcomes;
#: ``cohort-informed`` = informed by an anonymized cohort (requires
#: minimum cohort size ``n``; see 02's contract).
PROVENANCE_KINDS = ("static", "personalized", "cohort-informed")

#: Evidence-map link statuses. Tri-state by construction: there is no
#: "maybe" status that could become an unsupported-claim path.
LINK_STATUSES = ("supported", "gap", "grill_question")

#: Requirement kinds.
REQUIREMENT_KINDS = ("must_have", "nice_to_have")

#: Fit-result component axes, mirroring ``match.score_job``'s five
#: factors. Frozen: exactly these keys, each a point contribution in
#: [0, 100] that sums (rounded) to ``fit_score``.
FIT_RESULT_COMPONENTS = ("skills", "seniority", "salary", "location", "recency")

#: Extension-key namespace. Any key outside a schema's documented
#: contract must start with this prefix; anything else is a violation.
EXTENSION_KEY_PREFIX = "x_"

#: Documented (non-extension) keys per document type. The tri-state
#: no-free-text-assertion rule is contractual because
#: ``EVIDENCE_ENTRY_KEYS`` has no assertion field: a ``grill_question``
#: entry can only carry the verbatim requirement, its status, an (empty)
#: evidence list, and the linked ``grill_question_id``.
EVIDENCE_MAP_KEYS = frozenset(
    {"schema", "job_id", "job_title", "evidence_source_hash", "created_at", "entries"}
)
EVIDENCE_ENTRY_KEYS = frozenset(
    {"requirement", "status", "evidence", "grill_question_id"}
)
REQUIREMENT_KEYS = frozenset({"text", "source_quote", "kind"})
EVIDENCE_ITEM_KEYS = frozenset(
    {"evidence_id", "quote", "profile_field", "quantified"}
)
FIT_RESULT_KEYS = frozenset(
    {
        "schema",
        "job_id",
        "job_title",
        "fit_score",
        "components",
        "reasons",
        "veto",
        "veto_reason",
        "provenance",
        "provenance_note",
        "jd_verdict",
        "evidence_map",
        "evidence_summary",
        "limitations",
        "explain",
    }
)
SHARE_CARD_KEYS = frozenset(
    {
        "schema",
        "job_id",
        "job_title",
        "fit_score",
        "components",
        "provenance",
        "jd_verdict",
        "evidence",
        "limitations",
        "privacy_note",
        "h",
    }
)

#: Documented keys of the fit result's nested objects. The
#: additionalProperties policy applies at *every* object level, not
#: just the document top level: an undeclared key on a nested object
#: (e.g. a free-text ``analyst_note`` smuggled onto ``provenance``)
#: is a violation unless it uses the ``x_`` prefix.
PROVENANCE_KEYS = frozenset({"kind", "n"})
FIT_JD_VERDICT_KEYS = frozenset({"verdict", "score", "reasons"})
EVIDENCE_SUMMARY_KEYS = frozenset({"supported", "gaps", "grill_questions"})

#: Share-card nested objects. The share card's ``jd_verdict`` is the
#: reduced shape (no ``score`` — scores stay on the private fit
#: result); each ``evidence`` item is one user-selected fragment.
SHARE_CARD_JD_VERDICT_KEYS = frozenset({"verdict", "reasons"})
SHARE_CARD_EVIDENCE_ITEM_KEYS = frozenset({"requirement", "status", "quote"})

#: ``evidence_source_hash`` must look like ``sha256:`` + 16 hex chars.
_EVIDENCE_HASH_RE = re.compile(r"sha256:[0-9a-f]{16}\Z")

#: Traversal guards: validators walk arbitrary caller-supplied payloads
#: iteratively (no recursion), and bail out of pathological inputs
#: instead of raising ``RecursionError`` or hanging.
_MAX_TRAVERSAL_NODES = 100_000
_MAX_TRAVERSAL_DEPTH = 256

#: Minimum verbatim run (chars) that counts as a raw-input leak in the
#: share-card scrub. Any run this long, from *any* alignment, is a
#: violation — the window check below steps by 1, so it is sound. A
#: second pass over whitespace-collapsed text catches leaks that
#: re-flow a raw line break into a space.
_SCRUB_WINDOW = 24


def _collapse_ws(text: str) -> str:
    """Collapse every whitespace run to a single space (for the scrub's
    re-flow pass)."""
    return re.sub(r"\s+", " ", text).strip()


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_keys(
    doc: dict[str, Any],
    allowed: frozenset[str],
    where: str,
    errors: list[str],
) -> None:
    """Enforce the extension-key policy: documented keys plus ``x_*``."""
    for key in doc:
        if (
            not isinstance(key, str)
            or (key not in allowed and not key.startswith(EXTENSION_KEY_PREFIX))
        ):
            errors.append(
                f"{where}: unexpected key {key!r}; extension keys must "
                f"use the {EXTENSION_KEY_PREFIX!r} prefix"
            )


def validate_evidence_map(doc: Any) -> list[str]:
    """Validate a ``veto/evidence-map/v1`` document.

    Rules (frozen with the schema):
    * ``supported`` requires >= 1 evidence item, each with a verbatim
      ``quote`` from the evidence source.
    * ``gap`` must carry no evidence items.
    * ``grill_question`` must carry a ``grill_question_id`` (a link to an
      existing grill session question) — never a free-text assertion.
      The entry key allowlist (``EVIDENCE_ENTRY_KEYS``) has no assertion
      field, so the tri-state rule is contractual, not conventional.
    * ``evidence_source_hash`` must be set *and* well-formed
      (``sha256:`` + 16 hex chars); see :func:`verify_evidence_binding`
      for the full binding check against the source text.
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["document must be an object"]
    _check_keys(doc, EVIDENCE_MAP_KEYS, "$", errors)
    if doc.get("schema") != EVIDENCE_MAP_SCHEMA:
        errors.append(
            f"schema must be {EVIDENCE_MAP_SCHEMA!r}, got "
            f"{doc.get('schema')!r}"
        )
    if not _is_str(doc.get("job_id")) or not doc["job_id"]:
        errors.append("job_id must be a non-empty string")
    source_hash = doc.get("evidence_source_hash")
    if not _is_str(source_hash) or not source_hash:
        errors.append(
            "evidence_source_hash must be set: it anchors the map to the "
            "exact profile/resume text analyzed, so later edits invalidate "
            "stale maps instead of silently reusing them"
        )
    elif not _EVIDENCE_HASH_RE.match(source_hash):
        errors.append(
            "evidence_source_hash must be 'sha256:' followed by 16 hex "
            f"chars (the binding computed by evidence_source_hash_for), "
            f"got {source_hash!r}"
        )
    entries = doc.get("entries")
    if not isinstance(entries, list):
        return errors + ["entries must be a list"]
    for idx, entry in enumerate(entries):
        where = f"entries[{idx}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        _check_keys(entry, EVIDENCE_ENTRY_KEYS, where, errors)
        req = entry.get("requirement")
        if not isinstance(req, dict):
            errors.append(f"{where}.requirement must be an object")
        else:
            _check_keys(req, REQUIREMENT_KEYS, f"{where}.requirement", errors)
            if not _is_str(req.get("text")) or not req["text"]:
                errors.append(f"{where}.requirement.text must be non-empty")
            if not _is_str(req.get("source_quote")) or not req.get(
                "source_quote"
            ):
                errors.append(
                    f"{where}.requirement.source_quote must be the verbatim "
                    "JD quote the requirement was extracted from"
                )
            if req.get("kind") not in REQUIREMENT_KINDS:
                errors.append(
                    f"{where}.requirement.kind must be one of "
                    f"{REQUIREMENT_KINDS}"
                )
        status = entry.get("status")
        if status not in LINK_STATUSES:
            errors.append(
                f"{where}.status must be one of {LINK_STATUSES}"
            )
        evidence = entry.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{where}.evidence must be a list")
            continue
        for e_idx, item in enumerate(evidence):
            e_where = f"{where}.evidence[{e_idx}]"
            if not isinstance(item, dict):
                errors.append(f"{e_where} must be an object")
                continue
            _check_keys(item, EVIDENCE_ITEM_KEYS, e_where, errors)
            if not _is_str(item.get("quote")) or not item["quote"]:
                errors.append(
                    f"{e_where}.quote must be the verbatim evidence quote"
                )
        if status == "supported" and not evidence:
            errors.append(
                f"{where}: status 'supported' requires >= 1 evidence item"
            )
        if status == "gap" and evidence:
            errors.append(
                f"{where}: status 'gap' must not carry evidence items; "
                "move ambiguous matches to 'grill_question'"
            )
        if status == "grill_question":
            if not _is_str(entry.get("grill_question_id")) or not entry.get(
                "grill_question_id"
            ):
                errors.append(
                    f"{where}: status 'grill_question' must link "
                    "'grill_question_id' to an existing grill session "
                    "question (never a free-text system assertion)"
                )
            if evidence:
                errors.append(
                    f"{where}: status 'grill_question' must not carry "
                    "evidence items (ambiguity is resolved by the grill, "
                    "not asserted by the decoder)"
                )
    return errors


def _validate_components(
    components: Any, score: Any, errors: list[str]
) -> None:
    """Pin down ``components``: exactly the five frozen axes, each a
    point contribution in [0, 100], summing (rounded) to ``fit_score``."""
    if not isinstance(components, dict):
        errors.append("components must be an object")
        return
    axis_values: list[float] = []
    for axis in FIT_RESULT_COMPONENTS:
        if axis not in components:
            errors.append(
                f"components.{axis} is required: the frozen contract is "
                f"exactly the axes {FIT_RESULT_COMPONENTS}"
            )
            continue
        value = components[axis]
        if not _is_number(value) or not 0 <= value <= 100:
            errors.append(
                f"components.{axis} must be a point contribution in "
                f"[0, 100], got {value!r}"
            )
        else:
            axis_values.append(value)
    for key in components:
        if key not in FIT_RESULT_COMPONENTS and not (
            isinstance(key, str) and key.startswith(EXTENSION_KEY_PREFIX)
        ):
            errors.append(
                f"components: unexpected axis {key!r}; the frozen axes are "
                f"{FIT_RESULT_COMPONENTS} (extensions use "
                f"{EXTENSION_KEY_PREFIX!r})"
            )
    if _is_number(score) and len(axis_values) == len(FIT_RESULT_COMPONENTS):
        # The parts must explain the total: match.score_job defines
        # fit_score as int(round(sum of the rounded axes)), clamped.
        expected = max(0, min(100, int(round(sum(axis_values)))))
        if abs(expected - score) > 1:
            errors.append(
                "components do not explain fit_score: the five axes sum "
                f"to {expected}, but fit_score is {score}"
            )


def validate_fit_result(doc: Any) -> list[str]:
    """Validate a ``veto/fit-result/v1`` document.

    Enforces the frozen ``components`` contract (exactly the five axes
    in ``FIT_RESULT_COMPONENTS``, each a point contribution in
    [0, 100], summing — rounded — to ``fit_score``), the provenance
    contract, and the extension-key policy. When the envelope carries
    the embedded evidence map (``evidence_map``), it is validated in
    place as a ``veto/evidence-map/v1`` document.
    """
    errors: list[str] = []
    if not isinstance(doc, dict):
        return ["document must be an object"]
    _check_keys(doc, FIT_RESULT_KEYS, "$", errors)
    if doc.get("schema") != FIT_RESULT_SCHEMA:
        errors.append(f"schema must be {FIT_RESULT_SCHEMA!r}")
    score = doc.get("fit_score")
    if not _is_number(score):
        errors.append("fit_score must be a number")
    elif not 0 <= score <= 100:
        errors.append("fit_score must be in [0, 100]")
    _validate_components(doc.get("components"), score, errors)
    ev_map = doc.get("evidence_map")
    if ev_map is not None:
        if not isinstance(ev_map, dict):
            errors.append("evidence_map must be an object when present")
        else:
            for sub in validate_evidence_map(ev_map):
                errors.append(f"evidence_map: {sub}")
    prov = doc.get("provenance")
    if not isinstance(prov, dict):
        errors.append("provenance must be an object")
    else:
        _check_keys(prov, PROVENANCE_KEYS, "provenance", errors)
        if prov.get("kind") not in PROVENANCE_KINDS:
            errors.append(
                f"provenance.kind must be one of {PROVENANCE_KINDS}"
            )
        n = prov.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            errors.append("provenance.n must be a non-negative integer")
        if prov.get("kind") == "static" and n != 0:
            errors.append(
                "provenance.n must be 0 when kind is 'static' "
                "(static scores have no outcome sample)"
            )
    limitations = doc.get("limitations")
    if not isinstance(limitations, list) or not all(
        _is_str(x) for x in limitations
    ):
        errors.append("limitations must be a list of strings")
    jd_verdict = doc.get("jd_verdict")
    if isinstance(jd_verdict, dict):
        _check_keys(jd_verdict, FIT_JD_VERDICT_KEYS, "jd_verdict", errors)
    ev_summary = doc.get("evidence_summary")
    if isinstance(ev_summary, dict):
        _check_keys(
            ev_summary, EVIDENCE_SUMMARY_KEYS, "evidence_summary", errors
        )
    return errors


def _collect_strings(value: Any) -> list[str]:
    """Collect every string in a nested payload, iteratively.

    Dict keys are collected as well as values: a raw-input run
    smuggled into a key is still a verbatim leak. Bounded:
    pathological inputs (absurd breadth/depth) stop the walk instead
    of raising ``RecursionError`` or hanging.
    """
    found: list[str] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_TRAVERSAL_NODES or depth > _MAX_TRAVERSAL_DEPTH:
            continue
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str):
                    found.append(key)
                stack.append((item, depth + 1))
        elif isinstance(node, (list, tuple)):
            stack.extend((v, depth + 1) for v in node)
    return found


def _check_quote_lengths(
    payload: Any, max_quote_chars: int, errors: list[str]
) -> None:
    """Fail evidence quotes longer than ``max_quote_chars``.

    Iterative twin of the old recursive walk: same ``path`` semantics
    (any string-valued field whose path ends in ``quote``), bounded
    against pathological nesting.
    """
    stack: list[tuple[Any, str, int]] = [(payload, "$", 0)]
    nodes = 0
    while stack:
        node, path, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_TRAVERSAL_NODES or depth > _MAX_TRAVERSAL_DEPTH:
            continue
        if isinstance(node, dict):
            for key, item in node.items():
                stack.append((item, f"{path}.{key}", depth + 1))
        elif isinstance(node, (list, tuple)):
            for i, item in enumerate(node):
                stack.append((item, f"{path}[{i}]", depth + 1))
        elif isinstance(node, str) and path.endswith("quote"):
            if len(node) > max_quote_chars:
                errors.append(
                    f"{path}: evidence quote exceeds {max_quote_chars} "
                    f"chars ({len(node)}); share cards carry short "
                    "fragments only"
                )


def validate_share_card(
    payload: Any,
    *,
    raw_resume_text: str = "",
    raw_job_text: str = "",
    max_quote_chars: int = 200,
    allowed_quotes: list[str] | None = None,
) -> list[str]:
    """Validate a ``veto/share-card/v1`` payload.

    The Q1 exit-gate privacy property: **no raw resume or job-description
    text in the payload** beyond the fragments the user explicitly
    selected. The caller supplies the raw inputs; the validator fails if
    any distinctive substring (>= 24 chars) of the raw inputs appears
    verbatim in any payload string *outside* ``allowed_quotes`` — the
    exact fragments the user opted in to share.

    The fragment check is sound: *every* 24-char window of every
    payload string is tested (step 1), so any verbatim run of >= 24
    chars is caught regardless of alignment — dict keys included, so a
    run smuggled into a key is caught too; a second pass over
    whitespace-collapsed text additionally catches leaks that re-flow
    a raw line break into a space. Payloads are tiny, so the O(n)
    windows cost nothing.

    Refuses to certify (returns a violation) when called without any raw
    input — or with non-string raw inputs: a scrub with nothing (or
    nonsense) to check against would be a silent pass, and the privacy
    gate must never silently pass.

    Also enforced: evidence quotes are capped at ``max_quote_chars``
    (short fragments only), and the extension-key policy
    (``x_``-prefixed extensions only) at every object level —
    including dict keys, which are scrubbed like any other string.
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    if not isinstance(raw_resume_text, str) or not isinstance(
        raw_job_text, str
    ):
        return [
            "privacy gate cannot be certified: raw_resume_text and "
            "raw_job_text must be strings; refusing to scrub against "
            "non-text input"
        ]
    if not isinstance(max_quote_chars, int) or isinstance(
        max_quote_chars, bool
    ):
        return ["max_quote_chars must be an integer"]
    if allowed_quotes is not None and (
        not isinstance(allowed_quotes, list)
        or not all(_is_str(q) for q in allowed_quotes)
    ):
        return ["allowed_quotes must be a list of strings or None"]
    _check_keys(payload, SHARE_CARD_KEYS, "$", errors)
    if payload.get("schema") != SHARE_CARD_SCHEMA:
        errors.append(f"schema must be {SHARE_CARD_SCHEMA!r}")
    prov = payload.get("provenance")
    if isinstance(prov, dict):
        _check_keys(prov, PROVENANCE_KEYS, "provenance", errors)
    jd_verdict = payload.get("jd_verdict")
    if isinstance(jd_verdict, dict):
        _check_keys(
            jd_verdict, SHARE_CARD_JD_VERDICT_KEYS, "jd_verdict", errors
        )
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for idx, item in enumerate(evidence):
            if isinstance(item, dict):
                _check_keys(
                    item,
                    SHARE_CARD_EVIDENCE_ITEM_KEYS,
                    f"evidence[{idx}]",
                    errors,
                )
    if not raw_resume_text.strip() and not raw_job_text.strip():
        errors.append(
            "privacy gate cannot be certified: validate_share_card was "
            "called without raw resume/job text to scrub against; pass "
            "the actual analyzed inputs"
        )
        return errors

    # Distinctive substrings of the raw inputs that must not leak:
    # every full line of 24+ chars.
    secret_lines: list[str] = []
    for raw in (raw_resume_text, raw_job_text):
        for line in raw.splitlines():
            line = line.strip()
            if len(line) >= 24:
                secret_lines.append(line)

    payload_strings = _collect_strings(payload)
    # Remove the user-selected fragments before scrubbing: they are
    # the explicitly opted-in shares, not leaks. Anything else from
    # the raw inputs that survives in the payload is a violation.
    scrubbed_strings = []
    for value in payload_strings:
        for allowed in allowed_quotes or []:
            if allowed:
                value = value.replace(allowed, "")
        scrubbed_strings.append(value)

    def _leak_found() -> str | None:
        for secret in secret_lines:
            for value in scrubbed_strings:
                if secret and secret in value:
                    return secret
        # Sound fragment check: slide a 24-char window over each
        # payload string with step 1, and ask whether that window
        # occurs verbatim in either raw input. Any verbatim run of
        # >= 24 chars contains such a window, whatever its alignment.
        for value in scrubbed_strings:
            for i in range(0, len(value) - (_SCRUB_WINDOW - 1)):
                window = value[i : i + _SCRUB_WINDOW]
                if window in raw_resume_text or window in raw_job_text:
                    return window
        # Re-flow pass: collapse whitespace on both sides so that
        # turning a raw line break into a space (or vice versa) does
        # not hide a leak. User-selected fragments are removed after
        # collapsing too.
        collapsed_raws = [
            _collapse_ws(raw) for raw in (raw_resume_text, raw_job_text)
        ]
        collapsed_allowed = [
            _collapse_ws(a) for a in (allowed_quotes or []) if a
        ]
        for value in scrubbed_strings:
            cval = _collapse_ws(value)
            for allowed in collapsed_allowed:
                cval = cval.replace(allowed, "")
            for i in range(0, len(cval) - (_SCRUB_WINDOW - 1)):
                window = cval[i : i + _SCRUB_WINDOW]
                if any(window in craw for craw in collapsed_raws):
                    return window
        return None

    leaked = _leak_found()
    if leaked:
        errors.append(
            "privacy violation: payload contains a verbatim "
            f"substring of raw input ({leaked[:60]!r}...) outside "
            "the user-selected evidence fragments"
        )

    _check_quote_lengths(payload, max_quote_chars, errors)
    return errors


def canonical_json(doc: Any) -> str:
    """Deterministic JSON encoding (sorted keys, UTF-8) for hashing.

    Never raises: non-serializable values are replaced by their
    ``repr``, reference cycles by ``"<cycle>"``, and nesting past the
    traversal depth cap by ``"<max-depth>"`` before encoding.
    """
    import json

    try:
        return json.dumps(
            doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return json.dumps(
            _json_safe(doc),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _json_safe(value: Any, _seen: set[int] | None = None, _depth: int = 0) -> Any:
    """Lossy-but-total JSON projection used by :func:`canonical_json`."""
    if _seen is None:
        _seen = set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _depth > _MAX_TRAVERSAL_DEPTH:
        return "<max-depth>"
    if isinstance(value, dict):
        if id(value) in _seen:
            return "<cycle>"
        _seen.add(id(value))
        try:
            return {
                str(k): _json_safe(v, _seen, _depth + 1)
                for k, v in value.items()
            }
        finally:
            _seen.discard(id(value))
    if isinstance(value, (list, tuple)):
        if id(value) in _seen:
            return "<cycle>"
        _seen.add(id(value))
        try:
            return [_json_safe(v, _seen, _depth + 1) for v in value]
        finally:
            _seen.discard(id(value))
    return repr(value)


def sha256_hex(text: str, length: int = 16) -> str:
    """First ``length`` hex chars of SHA-256 over ``text``."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def evidence_source_hash_for(resume_text: str) -> str:
    """Compute the ``evidence_source_hash`` binding for resume text.

    Mirrors the producer (``resume_decoder`` → ``evidence_map``):
    ``"sha256:"`` + the first 16 hex chars of SHA-256 over the *exact*
    text analyzed. Any edit to the source text changes the binding, so
    stale maps cannot be silently reused.
    """
    return "sha256:" + sha256_hex(resume_text, 16)


def verify_evidence_binding(doc: Any, source_text: Any) -> list[str]:
    """Verify an evidence map's ``evidence_source_hash`` binding.

    Two checks, both against the supplied source text (the exact text
    the map was built from):
    * the recorded ``evidence_source_hash`` equals
      :func:`evidence_source_hash_for` of that text — a stale map
      (source edited after analysis) fails instead of being silently
      reused;
    * every evidence ``quote`` in the map is a verbatim substring of
      that text — a map whose quotes drifted from the source fails.

    Returns violations (never raises). Consumers holding cached maps
    should call this before rendering.
    """
    if not isinstance(doc, dict):
        return ["document must be an object"]
    if not isinstance(source_text, str):
        return [
            "cannot verify binding: source_text must be a string; "
            "refusing to certify against non-text input"
        ]
    errors: list[str] = []
    expected = evidence_source_hash_for(source_text)
    actual = doc.get("evidence_source_hash")
    if actual != expected:
        errors.append(
            "evidence_source_hash mismatch: the map is anchored to "
            f"{actual!r} but the supplied source text hashes to "
            f"{expected!r}; the map is stale — rebuild it instead of "
            "reusing it"
        )
    entries = doc.get("entries")
    if isinstance(entries, list):
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"entries[{idx}] must be an object")
                continue
            evidence = entry.get("evidence")
            if not isinstance(evidence, list):
                continue
            for e_idx, item in enumerate(evidence):
                if not isinstance(item, dict):
                    continue
                quote = item.get("quote")
                if _is_str(quote) and quote and quote not in source_text:
                    errors.append(
                        f"entries[{idx}].evidence[{e_idx}].quote is not a "
                        "verbatim substring of the source text the map "
                        "is anchored to; the binding is broken"
                    )
    return errors
