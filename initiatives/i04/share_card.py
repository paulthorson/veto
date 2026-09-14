#!/usr/bin/env python3
"""Private share card (Initiative 04, Epic 5).

Export scores + *user-selected* evidence WITHOUT embedding raw resume
or job-description text. The Q1 exit gate's privacy property — raw
resume text NEVER in a share link — is enforced by construction and
proven by scrub-style tests.

Product rules (frozen with ``veto/share-card/v1``):

* The card carries: fit score, component breakdown, score provenance,
  the JD's risk verdict (verdict + reasons, no raw JD), and only the
  evidence fragments the user explicitly selected.
* Evidence quotes are capped at 200 chars. Selection is opt-in per
  fragment with an explicit preview step in the UI (a
  safety-sensitive action: previewed, confirmed, audited).
* There is NO excerpt field on fit share cards (spike open question 2,
  resolved): the excerpt option from ``jdShare.ts`` is removed
  entirely on this surface — the leak risk for resume fragments is
  higher than for JD excerpts.
* ``build_share_card`` fails closed: it validates the fit-result
  envelope (``schemas.validate_fit_result``) AND the privacy scrub
  (``schemas.validate_share_card``); any violation raises
  ``ShareCardPrivacyError`` instead of returning a payload.

Integrity: the payload carries ``"h"``, the first 12 hex chars of
SHA-256 over the canonical JSON (fixed key order, no ``"h"`` field),
mirroring ``site/src/lib/jdShare.ts``. Decoders recompute and reject
mismatches, so tampered links are detectable.
``encode_share_payload`` / ``decode_share_payload`` produce and verify
the base64url fragment for ``#/decoder/r/<payload>``-style links.
Decoding additionally rejects any key outside the fixed schema
(which the hash alone cannot see), so key-injection tampering with
the original hash is rejected.

The scrub distinguishes *selected* fragments (user opted in) from
*unselected* raw content: ``allowed_quotes`` are the exact fragments
the user selected, plus the requirement labels of selected entries
(structural labels the user already saw in the evidence-map preview);
any other verbatim raw-input substring in the payload is a violation.

Threat model (stated plainly): the scrub defends against *accidental*
inclusion of raw text in payload fields — the realistic bug class when
new fields are added. It does not defend against in-process tampering
of the fit result object itself (an attacker with that access already
holds the raw text).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .schemas import (
    SHARE_CARD_SCHEMA,
    sha256_hex,
    validate_fit_result,
    validate_share_card,
)

#: Canonical key order for the integrity hash (mirrors jdShare.ts's
#: fixed-order canonicalization).
_KEY_ORDER = (
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
)

#: All keys a decoded payload may carry. Decoding rejects any key
#: outside this set (fail closed): the integrity hash covers only the
#: canonical keys, so without this gate an attacker could decode a
#: valid fragment, inject an unknown key, and re-encode with the
#: original hash — the recomputed hash would still match.
_KNOWN_KEYS = frozenset(_KEY_ORDER) | frozenset({"h"})

MAX_QUOTE_CHARS = 200


def _canonical_for_hash(payload: dict[str, Any]) -> str:
    ordered = {key: payload[key] for key in _KEY_ORDER if key in payload}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def _encode_form(payload: dict[str, Any]) -> str:
    """Wire form: canonical key order plus the integrity hash."""
    ordered = {key: payload[key] for key in _KEY_ORDER if key in payload}
    ordered["h"] = payload["h"]
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


class ShareCardPrivacyError(ValueError):
    """The share card failed its privacy scrub and was not built."""


def build_share_card(
    fit_result: dict[str, Any],
    selected_evidence_ids: list[str],
    raw_resume_text: str,
    raw_job_text: str,
) -> dict[str, Any]:
    """Build a ``veto/share-card/v1`` payload from a fit result.

    ``selected_evidence_ids`` are ``evidence_id`` values from the fit
    result's embedded evidence map — the fragments the user explicitly
    chose to share. Raises :class:`ShareCardPrivacyError` if the
    envelope validation or the scrub fails (fail closed).
    """
    envelope_errors = validate_fit_result(fit_result)
    if envelope_errors:
        raise ShareCardPrivacyError(
            "share card envelope failed validation and was not built: "
            + "; ".join(envelope_errors)
        )
    ev_map = (fit_result.get("evidence_map") or {}).get("entries", [])
    selected = []
    allowed: list[str] = []
    wanted_ids = set(selected_evidence_ids)
    for entry in ev_map:
        if not isinstance(entry, dict):
            continue
        requirement_text = str((entry.get("requirement") or {}).get("text", ""))
        if not requirement_text:
            continue
        for item in entry.get("evidence", []) or []:
            if not isinstance(item, dict):
                continue
            # Fail closed on malformed ids: a non-string evidence_id
            # cannot match a legitimate selection, and an unhashable
            # id (e.g. a dict) must never surface as a raw TypeError.
            evidence_id = item.get("evidence_id")
            if not isinstance(evidence_id, str):
                raise ShareCardPrivacyError(
                    "malformed evidence entry: evidence_id must be a "
                    f"string, got {type(evidence_id).__name__}; not built"
                )
            if evidence_id in wanted_ids:
                quote = str(item.get("quote", ""))[:MAX_QUOTE_CHARS]
                selected.append(
                    {
                        "requirement": requirement_text,
                        "status": entry.get("status"),
                        "quote": quote,
                    }
                )
                # Requirement labels are structural (shown in the
                # evidence-map preview the user already saw); the scrub
                # allows them alongside the selected quotes.
                allowed.append(quote)
                allowed.append(requirement_text)
    jd_verdict = fit_result.get("jd_verdict", {}) or {}
    payload: dict[str, Any] = {
        "schema": SHARE_CARD_SCHEMA,
        "job_id": fit_result.get("job_id", ""),
        "job_title": fit_result.get("job_title", ""),
        "fit_score": fit_result.get("fit_score"),
        "components": fit_result.get("components", {}),
        "provenance": fit_result.get("provenance", {"kind": "static", "n": 0}),
        "jd_verdict": {
            "verdict": jd_verdict.get("verdict"),
            "reasons": [
                r.get("reason")
                for r in (jd_verdict.get("reasons", []) or [])
                if r.get("reason")
            ][:3],
        },
        "evidence": selected,
        "limitations": fit_result.get("limitations", []),
        "privacy_note": (
            "Scores and user-selected evidence fragments only. No raw "
            "resume or job-description text is embedded in this card."
        ),
    }
    errors = validate_share_card(        payload,
        raw_resume_text=raw_resume_text,
        raw_job_text=raw_job_text,
        max_quote_chars=MAX_QUOTE_CHARS,
        allowed_quotes=allowed,
    )
    if errors:
        raise ShareCardPrivacyError(
            "share card failed its privacy scrub and was not built: "
            + "; ".join(errors)
        )
    payload["h"] = sha256_hex(_canonical_for_hash(payload), 12)
    return payload


def encode_share_payload(payload: dict[str, Any]) -> str:
    """base64url-encode the payload for a share-link fragment."""
    if "h" not in payload or payload["h"] != sha256_hex(
        _canonical_for_hash(payload), 12
    ):
        raise ValueError("payload integrity hash missing or stale; not encoding")
    raw = _encode_form(payload)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def decode_share_payload(fragment: str) -> dict[str, Any]:
    """Decode and integrity-check a share-link fragment.

    Raises ``ValueError`` on tampering, unknown keys, or malformed
    input.
    """
    padded = fragment + "=" * (-len(fragment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"malformed share payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("malformed share payload: not an object")
    unknown = [key for key in payload if key not in _KNOWN_KEYS]
    if unknown:
        raise ValueError(
            f"share payload carries unknown key(s) {unknown!r}; rejected"
        )
    expected = sha256_hex(_canonical_for_hash(payload), 12)
    if payload.get("h") != expected:
        raise ValueError("share payload failed integrity check (tampered?)")
    return payload
