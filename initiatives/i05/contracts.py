#!/usr/bin/env python3
"""Cross-initiative schema contracts for the Application Studio (i05).

The studio builds against these contracts and integrates live when the
owning teams land. Pure local validation — no network, no file access
to other teams' stores.

ASSUMPTION FLAGS (explicit, per the build brief):

* **A1 — Initiative 04 (decoder team):** requirement→evidence links land
  as :data:`LINK_SCHEMA`. Until they land, the studio accepts plain
  dicts and validates on read via :func:`validate_link`.
* **A2 — Initiatives 01–02 (capture + data-plumbing teams):** the
  evidence store lands as :data:`EVIDENCE_SCHEMA` and score provenance
  lands as :data:`PROVENANCE_SCHEMA`. Until they land, the studio's own
  :mod:`evidence_library` is the evidence source of record and
  :func:`validate_evidence` / :func:`validate_provenance` gate anything
  the other teams hand over.

Every record carries ``schema`` = :data:`CONTRACT_VERSION` so a future
contract bump fails loudly instead of misreading fields.
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "i05-contracts/1"

# ---------------------------------------------------------------------------
# Requirement -> evidence link (Initiative 04 contract)
# ---------------------------------------------------------------------------

#: A link asserts that one approved evidence item supports one stated
#: job requirement. ``quote`` is a short verbatim excerpt of the
#: evidence text (never the whole item); ``confidence`` is the
#: decoder's own 0..1 score, NOT a prediction about any hiring outcome.
LINK_SCHEMA: dict[str, Any] = {
    "schema": CONTRACT_VERSION,
    "required": ("link_id", "requirement_id", "evidence_id"),
    "optional": ("quote", "confidence", "created_at", "decoder_version"),
}

#: Evidence-store item (Initiatives 01–02 contract). ``kind`` is one of
#: ``achievement`` | ``metric`` | ``portfolio_link`` | ``approved_phrase``.
#: ``approved`` is a truthy gate: only truthy-``approved`` items may
#: back a tailored statement. ``source`` names the profile field the item was
#: derived from (e.g. ``experience[0].bullets[2]``) — the anti-invention
#: anchor.
EVIDENCE_SCHEMA: dict[str, Any] = {
    "schema": CONTRACT_VERSION,
    "required": ("evidence_id", "kind", "text", "source", "approved"),
    "optional": ("created_at", "approved_at", "used_in"),
}

#: Score provenance (Initiative 02 contract). ``factors`` maps factor
#: name -> {"weight", "value", "evidence_ids"}; ``inputs_hash`` pins the
#: exact inputs the score was computed from.
PROVENANCE_SCHEMA: dict[str, Any] = {
    "schema": CONTRACT_VERSION,
    "required": ("score_id", "factors", "inputs_hash", "created_at"),
    "optional": ("model_version", "notes"),
}


def _missing(record: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    return [f for f in schema["required"] if f not in record]


def _schema_problem(record: dict[str, Any]) -> list[str]:
    # The module contract promises a future contract bump "fails loudly
    # instead of misreading fields": a stamped record on a different
    # schema version is a violation. Unstamped records (the A1/A2 plain
    # dicts the studio accepts until the owning teams land) pass.
    schema_val = record.get("schema")
    if schema_val is None:
        return []
    if schema_val != CONTRACT_VERSION:
        return [f"schema mismatch: {schema_val!r} != {CONTRACT_VERSION!r}"]
    return []


def validate_link(link: dict[str, Any]) -> list[str]:
    """Return a list of schema violations for a requirement→evidence link.

    Empty means the link satisfies the Initiative 04 contract. Also
    rejects out-of-range ``confidence`` values when present, and a
    ``schema`` stamp that does not match :data:`CONTRACT_VERSION`.
    """
    if not isinstance(link, dict):
        return ["link must be a dict"]
    problems = _schema_problem(link)
    problems += [f"missing required field: {f}" for f in _missing(link, LINK_SCHEMA)]
    conf = link.get("confidence")
    if conf is not None:
        try:
            if not 0.0 <= float(conf) <= 1.0:
                problems.append("confidence must be within 0..1")
        except (TypeError, ValueError):
            problems.append("confidence must be numeric")
    return problems


def validate_evidence(item: dict[str, Any]) -> list[str]:
    """Return schema violations for an evidence-store item (01–02 contract).

    Also fails loudly on a ``schema`` stamp that does not match
    :data:`CONTRACT_VERSION`.
    """
    if not isinstance(item, dict):
        return ["evidence item must be a dict"]
    problems = _schema_problem(item)
    problems += [f"missing required field: {f}" for f in _missing(item, EVIDENCE_SCHEMA)]
    kind = item.get("kind")
    if kind is not None and kind not in {
        "achievement", "metric", "portfolio_link", "approved_phrase",
    }:
        problems.append(f"unknown evidence kind: {kind!r}")
    return problems


def validate_provenance(record: dict[str, Any]) -> list[str]:
    """Return schema violations for a score-provenance record (02 contract).

    Also fails loudly on a ``schema`` stamp that does not match
    :data:`CONTRACT_VERSION`.
    """
    if not isinstance(record, dict):
        return ["provenance record must be a dict"]
    problems = _schema_problem(record)
    problems += [
        f"missing required field: {f}"
        for f in _missing(record, PROVENANCE_SCHEMA)
    ]
    return problems


def normalize_link(link: dict[str, Any]) -> dict[str, Any]:
    """Stamp a link with the contract version and validate it.

    Raises :class:`ValueError` listing violations when the link does
    not satisfy the contract — the studio never silently consumes a
    malformed link.
    """
    problems = validate_link(link)
    if problems:
        raise ValueError("requirement→evidence link failed contract: "
                         + "; ".join(problems))
    out = dict(link)
    out.setdefault("schema", CONTRACT_VERSION)
    return out


def normalize_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Stamp an evidence item with the contract version and validate it."""
    problems = validate_evidence(item)
    if problems:
        raise ValueError("evidence item failed contract: " + "; ".join(problems))
    out = dict(item)
    out.setdefault("schema", CONTRACT_VERSION)
    return out


def links_for_requirement(
    links: list[dict[str, Any]], requirement_id: str
) -> list[dict[str, Any]]:
    """Return validated links for one requirement id, highest confidence first."""
    out = []
    for link in links or []:
        if validate_link(link):
            continue
        if str(link.get("requirement_id")) == str(requirement_id):
            out.append(link)
    out.sort(key=lambda l: float(l.get("confidence") or 0.0), reverse=True)
    return out


def approved_evidence(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return only schema-valid, approved evidence items.

    The approval gate is truthy (consistent with
    ``versions.trace_report``, which also classifies on a truthy
    ``approved`` flag) — an item with ``approved=1`` counts as
    approved. Unapproved or invalid items can never back a tailored
    statement.
    """
    out = []
    for item in items or []:
        if validate_evidence(item):
            continue
        if item.get("approved"):
            out.append(item)
    return out
