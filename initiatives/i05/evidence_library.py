#!/usr/bin/env python3
"""Evidence library — Epic 2 of Initiative 05.

Reusable achievements, portfolio links, metrics, and approved phrasing,
each tied to a source fact. Items satisfy the Initiatives 01–02
evidence-store contract (see :mod:`initiatives.i05.contracts`); until
those teams land, this library is the studio's evidence source of
record.

Hard rules:

* **Never invent.** :func:`build_from_profile` derives drafts ONLY from
  the user's real profile. :func:`add_item` records caller-supplied
  text verbatim with its source — it never generates content.
* **Approval gates use.** Items start as drafts (``approved=False``).
  Only ``approved=True`` items may back a tailored statement (see
  :func:`contracts.approved_evidence`). Editing an approved item's text
  resets approval — approval is of the exact wording.
* Storage is a single JSON file with atomic tmp+rename writes (fsync
  before rename), inside a gitignored directory (evidence text is the
  user's personal data).

Trust model (see DECISIONS.md D6b):

* Every item carries ``origin``: ``"derived"`` means the item was built
  by :func:`build_from_profile` — text copied verbatim from the
  profile, source reference generated from the same profile.
  ``"asserted"`` means a caller supplied the text and source through
  :func:`add_item`; the library does NOT vouch that it is true.
* When :func:`add_item` is given the ``profile``, the ``source``
  reference must resolve to a real value in that profile AND the text
  must match that value verbatim — otherwise :class:`ValueError`.
  Without a profile the source cannot be checked, so the item is
  stored as ``origin="asserted"``: labeled, never presented as a
  profile fact. A fabricated source therefore can never look identical
  to a profile-derived item.
* Retrieval (:func:`find_for_requirement`, :func:`links_for_job`)
  returns only approved items; every returned item carries its
  ``origin`` so consumers see asserted vs derived. Derived items rank
  ahead of asserted ones on equal relevance.

Corrupt-store policy: if ``library.json`` exists but is unreadable,
malformed, or carries a different schema version, :func:`_load` raises
:class:`CorruptStoreError` LOUDLY — reads and writes both refuse.
The corrupt bytes are left untouched for manual recovery. The library
never warns-and-proceeds and never overwrites user data it could not
read.

``build_from_profile`` is idempotent: items are deduplicated on the
stable key ``(kind, text, source)``.

Portfolio links come from the profile's own URL fields; metrics are
bullets containing numbers, quoted verbatim.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import CONTRACT_VERSION, normalize_evidence

log = logging.getLogger("job-apply-mcp.i05.evidence_library")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LIBRARY_DIR = BASE_DIR / "evidence_library"
_LIBRARY_FILE = "library.json"

KINDS = ("achievement", "metric", "portfolio_link", "approved_phrase")

ORIGIN_DERIVED = "derived"
ORIGIN_ASSERTED = "asserted"

_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_URL_RE = re.compile(r"https?://\S+")
_SOURCE_REF_RE = re.compile(
    r"^(experience|skills)\[(\d+)\](?:\.(bullets)\[(\d+)\])?$"
)
_TOP_LEVEL_SOURCE_KEYS = ("linkedin_url", "website")


class CorruptStoreError(RuntimeError):
    """The evidence store file exists but cannot be loaded safely.

    Raised for unreadable JSON, wrong payload shape, or a schema
    version mismatch. The raw bytes are left on disk untouched —
    recover or delete the file manually before the library will
    operate on that directory again.
    """


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return f"ev_{uuid.uuid4().hex[:12]}"


def _lib_path(library_dir: Path | None, create: bool) -> Path:
    d = Path(library_dir) if library_dir else DEFAULT_LIBRARY_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d / _LIBRARY_FILE


def _load(library_dir: Path | None) -> dict[str, Any]:
    """Load the store payload.

    Raises :class:`CorruptStoreError` when the store file exists but
    is unreadable, malformed, or stamped with a different schema
    version — never returns an empty library over corrupt bytes.
    """
    path = _lib_path(library_dir, create=False)
    if not path.is_file():
        return {"schema": CONTRACT_VERSION, "items": []}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise CorruptStoreError(
            f"evidence store {path} is unreadable ({exc}); refusing to "
            f"operate — the raw bytes were left untouched for manual "
            f"recovery"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise CorruptStoreError(
            f"evidence store {path} is malformed (expected a dict with "
            f"an 'items' list); refusing to operate — the raw bytes were "
            f"left untouched for manual recovery"
        )
    if data.get("schema") != CONTRACT_VERSION:
        raise CorruptStoreError(
            f"evidence store {path} has schema {data.get('schema')!r}, "
            f"expected {CONTRACT_VERSION!r}; refusing to operate rather "
            f"than misread fields"
        )
    return data


def _save(library_dir: Path | None, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["schema"] = CONTRACT_VERSION
    path = _lib_path(library_dir, create=True)
    tmp = path.with_name(
        path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Source-reference resolution (honesty anchor)
# ---------------------------------------------------------------------------


def _resolve_source(profile: dict[str, Any], source: str) -> str | None:
    """Resolve a source reference against a profile.

    Returns the referenced value as a stripped string, or ``None``
    when the reference does not resolve to a real value. Accepted
    forms: ``experience[i].bullets[j]``, ``skills[i]``,
    ``linkedin_url``, ``website``.
    """
    if not isinstance(profile, dict):
        return None
    m = _SOURCE_REF_RE.match(source)
    if m:
        section, i, _, j = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        seq = profile.get(section)
        if not isinstance(seq, list) or not 0 <= i < len(seq):
            return None
        entry = seq[i]
        if j is not None:  # experience[i].bullets[j]
            if not isinstance(entry, dict):
                return None
            bullets = entry.get("bullets")
            jj = int(j)
            if not isinstance(bullets, list) or not 0 <= jj < len(bullets):
                return None
            value = bullets[jj]
        else:  # skills[i]
            if isinstance(entry, dict):
                value = entry.get("name")
            else:
                value = entry
        value = str(value or "").strip()
        return value or None
    if source in _TOP_LEVEL_SOURCE_KEYS:
        value = str(profile.get(source) or "").strip()
        return value or None
    return None


# ---------------------------------------------------------------------------
# CRUD + approval
# ---------------------------------------------------------------------------


def _store_item(
    kind: str,
    text: str,
    source: str,
    origin: str,
    library_dir: Path | None,
) -> dict[str, Any]:
    text = str(text or "").strip()
    source = str(source or "").strip()
    if not text:
        raise ValueError("evidence text must not be empty")
    if not source:
        raise ValueError("evidence source must name the profile fact")
    if origin not in (ORIGIN_DERIVED, ORIGIN_ASSERTED):
        raise ValueError(f"unknown origin: {origin!r}")
    item = normalize_evidence(
        {
            "evidence_id": _new_id(),
            "kind": kind,
            "text": text,
            "source": source,
            "origin": origin,
            "approved": False,
            "created_at": _utcnow(),
            "used_in": [],
        }
    )
    lib = _load(library_dir)
    lib["items"].append(item)
    _save(library_dir, lib)
    return item


def add_item(
    kind: str,
    text: str,
    source: str,
    library_dir: Path | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a new evidence item as a draft (``approved=False``).

    ``text`` is stored verbatim — this function never generates or
    embellishes content. The item is tagged ``origin="asserted"``: the
    caller asserts the text and source; the library does not vouch for
    them.

    When ``profile`` is supplied, the ``source`` reference is resolved
    against it and must both resolve to a real value AND match ``text``
    verbatim, otherwise :class:`ValueError`. This is what stops a
    fabricated claim from borrowing fake provenance. Without a
    profile the source cannot be checked, so the item is stored
    labeled as asserted — it can never look identical to a
    profile-derived item.
    Raises :class:`ValueError` on contract violations.
    """
    if profile is not None:
        source = str(source or "").strip()
        resolved = _resolve_source(profile, source)
        if resolved is None:
            raise ValueError(
                f"source {source!r} does not resolve against the "
                f"provided profile"
            )
        if str(text or "").strip() != resolved:
            raise ValueError(
                f"text does not match the profile value at {source!r} "
                f"verbatim"
            )
    return _store_item(kind, text, source, ORIGIN_ASSERTED, library_dir)


def get_item(
    evidence_id: str, library_dir: Path | None = None
) -> dict[str, Any] | None:
    """Fetch one item by id, or ``None``."""
    for item in _load(library_dir)["items"]:
        if item.get("evidence_id") == evidence_id:
            return item
    return None


def list_items(
    kind: str | None = None,
    approved_only: bool = False,
    library_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """List items, optionally filtered by kind and approval."""
    out = []
    for item in _load(library_dir)["items"]:
        if kind is not None and item.get("kind") != kind:
            continue
        if approved_only and item.get("approved") is not True:
            continue
        out.append(item)
    return out


def approve_item(
    evidence_id: str, library_dir: Path | None = None
) -> dict[str, Any]:
    """Approve an item's exact current text for use in tailoring.

    Approval is the human gate: the approver vouches for the item,
    including its ``origin`` label. Raises :class:`KeyError` when
    unknown.
    """
    lib = _load(library_dir)
    for item in lib["items"]:
        if item.get("evidence_id") == evidence_id:
            item["approved"] = True
            item["approved_at"] = _utcnow()
            _save(library_dir, lib)
            return item
    raise KeyError(f"unknown evidence item: {evidence_id}")


def update_item(
    evidence_id: str,
    text: str,
    library_dir: Path | None = None,
) -> dict[str, Any]:
    """Replace an item's text. Approval resets to ``False``.

    Approval is of the exact wording — an edited item must be
    re-approved before it can back a statement again. Editing
    downgrades the item's ``origin`` to ``"asserted"``: the Trust model
    defines ``"derived"`` as text copied verbatim from the profile, and
    caller-supplied text no longer satisfies that definition.
    """
    text = str(text or "").strip()
    if not text:
        raise ValueError("evidence text must not be empty")
    lib = _load(library_dir)
    for item in lib["items"]:
        if item.get("evidence_id") == evidence_id:
            item["text"] = text
            item["approved"] = False
            item["origin"] = ORIGIN_ASSERTED
            item.pop("approved_at", None)
            _save(library_dir, lib)
            return item
    raise KeyError(f"unknown evidence item: {evidence_id}")


def delete_item(evidence_id: str, library_dir: Path | None = None) -> bool:
    """Delete a draft item. Approved items are protected: unapprove via
    :func:`update_item` first. Returns True when deleted."""
    lib = _load(library_dir)
    for i, item in enumerate(lib["items"]):
        if item.get("evidence_id") == evidence_id:
            if item.get("approved") is True:
                raise ValueError(
                    "cannot delete an approved item; edit it (which "
                    "resets approval) or keep it as history"
                )
            del lib["items"][i]
            _save(library_dir, lib)
            return True
    return False


def record_use(
    evidence_id: str,
    variant_id: str,
    library_dir: Path | None = None,
) -> None:
    """Note that a resume variant used an evidence item (provenance)."""
    lib = _load(library_dir)
    for item in lib["items"]:
        if item.get("evidence_id") == evidence_id:
            used = item.setdefault("used_in", [])
            if variant_id not in used:
                used.append(variant_id)
            _save(library_dir, lib)
            return
    raise KeyError(f"unknown evidence item: {evidence_id}")


# ---------------------------------------------------------------------------
# Derivation from the real profile (never invents)
# ---------------------------------------------------------------------------


def build_from_profile(
    profile: dict[str, Any],
    library_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Derive draft evidence items from the user's real profile.

    * every experience bullet -> ``achievement``;
    * bullets containing numbers -> also ``metric`` (quoted verbatim);
    * every skill -> ``approved_phrase``;
    * linkedin/website URLs -> ``portfolio_link``.

    All items start unapproved and are tagged ``origin="derived"``.
    Nothing is generated: text is copied verbatim from the profile
    with its source reference.

    Idempotent: items are deduplicated on the stable key
    ``(kind, text, source)`` — re-running against the same profile
    creates nothing new. Returns only the newly created items.
    """
    created: list[dict[str, Any]] = []
    seen = {
        (i.get("kind"), i.get("text"), i.get("source"))
        for i in _load(library_dir)["items"]
    }

    def _add(kind: str, text: str, source: str) -> None:
        key = (kind, text, source)
        if key in seen:
            return
        try:
            item = _store_item(kind, text, source, ORIGIN_DERIVED,
                               library_dir)
        except ValueError as exc:
            log.warning("skipping evidence item: %s", exc)
            return
        seen.add(key)
        created.append(item)

    for i, exp in enumerate(profile.get("experience", []) or []):
        if not isinstance(exp, dict):
            continue
        for j, bullet in enumerate(exp.get("bullets", []) or []):
            bullet = str(bullet or "").strip()
            if not bullet:
                continue
            source = f"experience[{i}].bullets[{j}]"
            _add("achievement", bullet, source)
            if _NUMBER_RE.search(bullet):
                _add("metric", bullet, source)
    for i, skill in enumerate(profile.get("skills", []) or []):
        name = skill.get("name") if isinstance(skill, dict) else skill
        name = str(name or "").strip()
        if name:
            _add("approved_phrase", name, f"skills[{i}]")
    for key, label in (("linkedin_url", "LinkedIn"), ("website", "website")):
        url = str(profile.get(key) or "").strip()
        if url:
            _add("portfolio_link", f"{label}: {url}", key)
    return created


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#]+", str(text or "").lower()))


def find_for_requirement(
    requirement_text: str,
    library_dir: Path | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank approved items against a requirement by token overlap.

    Only ``approved=True`` items are ever returned — drafts can never
    back a tailored statement. Every returned item carries its
    ``origin`` label: ``asserted`` items are caller-supplied claims the
    human approver vouched for, never profile facts; ``derived`` items
    win ties over ``asserted`` ones on equal relevance. Returns at
    most ``limit`` items, best first; empty when nothing relevant is
    approved yet.
    """
    need = _tokens(requirement_text)
    if not need:
        return []
    scored = []
    for item in list_items(approved_only=True, library_dir=library_dir):
        have = _tokens(item.get("text", ""))
        overlap = need & have
        if overlap:
            derived_first = 0 if item.get("origin") == ORIGIN_DERIVED else 1
            scored.append((derived_first, -len(overlap), item))
    scored.sort(key=lambda pair: (pair[0], pair[1]))
    return [item for _, _, item in scored[:limit]]


def links_for_job(
    job_id: str,
    library_dir: Path | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return approved portfolio links for a job's tailor run.

    Called by the studio's ``variant-tailor`` path with the job id; the
    returned links are handed to the tailor as candidate portfolio
    URLs. Only ``approved=True`` items of kind ``portfolio_link`` are
    returned, each as::

        {"evidence_id", "url", "label", "text", "source", "origin",
         "job_id"}

    ``url`` is the raw URL extracted from the item text; ``label`` is
    the profile field label (e.g. ``"LinkedIn"``). ``origin`` is always
    present so the tailor path can distinguish profile-derived links
    from caller-asserted ones. Read-only: no state is mutated.
    """
    job_id = str(job_id or "").strip()
    out = []
    for item in list_items(kind="portfolio_link", approved_only=True,
                           library_dir=library_dir):
        text = str(item.get("text") or "")
        match = _URL_RE.search(text)
        url = match.group(0).rstrip(").,;") if match else text.strip()
        label = text[: match.start()].strip().rstrip(":") if match else ""
        out.append(
            {
                "evidence_id": item.get("evidence_id"),
                "url": url,
                "label": label,
                "text": item.get("text"),
                "source": item.get("source"),
                "origin": item.get("origin", ORIGIN_ASSERTED),
                "job_id": job_id,
            }
        )
        if len(out) >= limit:
            break
    return out
