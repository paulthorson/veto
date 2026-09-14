#!/usr/bin/env python3
"""Application packet — Epic 5 of Initiative 05.

Bundles resume, cover letter, grill answers, fit rationale, and a
review checklist for one role. Build-only: this module assembles and
reviews the packet. It NEVER submits applications — there is
deliberately no submit path here or anywhere in the studio, and a test
asserts that. Submissions need Paul's explicit per-application
confirmation through the governed apply flow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import diff_explain
from .contracts import CONTRACT_VERSION

log = logging.getLogger("job-apply-mcp.i05.packet")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PACKETS_DIR = BASE_DIR / "packets"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_job_id(job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(job_id or "")).strip("_")
    return safe[:120] or "job"


def _packets_dir(packets_dir: Path | None, create: bool) -> Path:
    d = Path(packets_dir) if packets_dir else DEFAULT_PACKETS_DIR
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _packet_path(packets_dir: Path, packet_id: str) -> Path:
    return packets_dir / f"{_safe_job_id(packet_id)}.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(
        path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("ignoring unreadable packet %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _import_repo(name: str) -> Any:
    import sys

    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    return __import__(name)


# ---------------------------------------------------------------------------
# Packet assembly
# ---------------------------------------------------------------------------


def _grill_answers(job_id: str) -> list[dict[str, str]]:
    """Q&A pairs from the grill session for this job, if any."""
    try:
        grill = _import_repo("grill")
        pairs = grill.get_qa_pairs(job_id) or []
        return [{"question": q, "answer": a} for q, a in pairs]
    except Exception as exc:  # grill is optional input, never fatal
        log.debug("grill answers unavailable for %s: %s", job_id, exc)
        return []


def build_fit_rationale(
    profile: dict[str, Any],
    job_details: dict[str, Any],
    provenance: dict[str, Any] | None = None,
    library_dir: Path | None = None,
) -> str:
    """Explain the fit using only profile facts + approved evidence.

    Each matched skill is paired with its backing evidence; missing
    skills are listed as not claimed. Nothing is invented.
    """
    from . import evidence_library

    tailor_mod = _import_repo("tailor")
    job_details = job_details or {}
    profile = profile or {}
    matched = (provenance or {}).get("matched_skills")
    if matched is None:
        matched = tailor_mod.extract_job_keywords(job_details, profile)["matched"]
    missing = (provenance or {}).get("missing_skills")
    if missing is None:
        missing = tailor_mod.extract_job_keywords(job_details, profile)["missing"]

    title = str(job_details.get("title") or "the role").strip()
    company = str(job_details.get("company") or "").strip()
    where = f" for {title}" + (f" at {company}" if company else "")

    lines = [f"Fit rationale{where}:", ""]
    if matched:
        lines.append("Emphasized strengths (each backed by evidence):")
        for skill in matched:
            hits = evidence_library.find_for_requirement(
                str(skill), library_dir=library_dir, limit=1)
            if hits:
                item = hits[0]
                lines.append(
                    f"- {skill}: {item['text']} "
                    f"(evidence: {item['evidence_id']}, "
                    f"source: {item['source']})")
            else:
                lines.append(
                    f"- {skill}: listed in the profile's skills; no "
                    f"approved evidence item yet.")
        lines.append("")
    else:
        lines.append("No profile skills matched this posting's keywords.")
        lines.append("")
    if missing:
        lines.append(
            "Not claimed (absent from the profile — the cover letter "
            "phrases these as eagerness to develop, never as experience):")
        for skill in missing[:5]:
            lines.append(f"- {skill}")
    return "\n".join(lines).rstrip() + "\n"


_CHECKLIST = (
    ("diff-reviewed",
     "Every proposed change is explained and marked reviewed."),
    ("honesty-clean",
     "Cover letter passes the honesty scan (no untraceable claims)."),
    ("ats-ready",
     "ATS readiness check passes (no failures)."),
    ("version-approved",
     "Resume variant version is explicitly approved."),
    ("trace-clean",
     "Every tailored statement traces to approved/profile evidence."),
)


def _compute_checklist(
    packet: dict[str, Any],
    profile: dict[str, Any],
    job_details: dict[str, Any],
    variant_info: dict[str, Any] | None,
    evidence_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cover_mod = _import_repo("cover_letters")
    from . import ats_check, versions

    resume_md = packet.get("resume_md", "")
    letter = packet.get("cover_letter", "")
    items: list[dict[str, Any]] = []

    pending = diff_explain.pending_changes(packet.get("changes", []))
    items.append(_check_item(
        "diff-reviewed",
        "done" if not pending else "pending",
        f"{len(pending)} change(s) still need review." if pending
        else "All changes reviewed."))

    flags = cover_mod.honesty_scan(letter, profile, job_details)
    items.append(_check_item(
        "honesty-clean",
        "done" if not flags else "pending",
        f"{len(flags)} untraceable claim(s) flagged." if flags
        else "No untraceable claims."))

    ats = ats_check.ats_readiness_check(resume_md, job_details, profile)
    items.append(_check_item(
        "ats-ready",
        "done" if ats["ready"] else "pending",
        f"{ats['summary']['fail']} failing check(s)." if not ats["ready"]
        else "No failing checks."))

    if variant_info is None:
        items.append(_check_item("version-approved", "skipped",
                                 "No variant linked to this packet."))
    else:
        approved = bool((variant_info.get("provenance", {}) or {}).get("approved"))
        items.append(_check_item(
            "version-approved",
            "done" if approved else "pending",
            "Version approved." if approved else "Version not approved."))

    gaps = versions.untraced_statements(
        packet["variant_id"], packet["version_id"],
        resume_md=resume_md,
        variants_dir=packet.get("_variants_dir")) if packet.get("variant_id") else []
    report = versions.trace_report(
        packet["variant_id"], packet["version_id"], evidence_items,
        variants_dir=packet.get("_variants_dir")) if packet.get("variant_id") else None
    problems = list(gaps)
    if report:
        problems += [f"unapproved: {e}" for e in report["unapproved"]]
        problems += [f"unknown: {e}" for e in report["unknown"]]
    items.append(_check_item(
        "trace-clean",
        "done" if not problems else "pending",
        "; ".join(problems[:5]) if problems else "All statements trace."))
    return items


def _check_item(check_id: str, status: str, detail: str) -> dict[str, Any]:
    label = dict(_CHECKLIST).get(check_id, check_id)
    return {"id": check_id, "label": label, "status": status,
            "detail": detail}


def build_packet(
    job_id: str,
    profile: dict[str, Any],
    job_details: dict[str, Any],
    variant_id: str | None = None,
    version_id: str | None = None,
    evidence_links: list[dict[str, Any]] | None = None,
    library_dir: Path | None = None,
    variants_dir: Path | None = None,
    packets_dir: Path | None = None,
) -> dict[str, Any]:
    """Assemble an application packet for one role (build-only).

    Contents: resume + cover letter (from the linked variant version,
    or freshly tailored), grill Q&A answers, fit rationale, change
    explanations, and a review checklist. The checklist starts pending;
    use :func:`mark_change_reviewed` and :func:`refresh_checklist`,
    then :func:`approve_packet`.
    """
    from . import evidence_library, versions

    tailor_mod = _import_repo("tailor")

    variant_info: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    resume_md = ""
    cover_letter = ""
    if variant_id:
        restored = versions.restore_version(
            variant_id, version_id, variants_dir=variants_dir)
        resume_md = restored["resume_md"]
        cover_letter = restored["cover_letter"]
        provenance = restored["provenance"]
        version_id = restored["version_id"]
        variant_info = {"provenance": provenance}
    else:
        result = tailor_mod.tailor_with_provenance(
            profile, job_details, evidence_links)
        resume_md = result["resume_md"]
        cover_letter = result["cover_letter"]
        provenance = result["provenance"]

    base_md = tailor_mod.base_resume_md(profile)
    changes = diff_explain.explain_resume_diff(
        base_md, resume_md,
        matched_skills=(provenance or {}).get("matched_skills", []),
        statement_map=(provenance or {}).get("statement_map", []))

    evidence_items = evidence_library.list_items(library_dir=library_dir)
    packet_id = f"pkt_{uuid.uuid4().hex[:12]}"
    packet = {
        "packet_id": packet_id,
        "job_id": str(job_id),
        "created_at": _utcnow(),
        "schema": CONTRACT_VERSION,
        "variant_id": variant_id,
        "version_id": version_id,
        "resume_md": resume_md,
        "cover_letter": cover_letter,
        "answers": _grill_answers(str(job_id)),
        "fit_rationale": build_fit_rationale(
            profile, job_details, provenance, library_dir),
        "changes": changes,
        "status": "draft",
        "_variants_dir": str(variants_dir) if variants_dir else None,
    }
    packet["review_checklist"] = _compute_checklist(
        packet, profile, job_details, variant_info, evidence_items)
    packet.pop("_variants_dir", None)

    d = _packets_dir(packets_dir, create=True)
    _write_json(_packet_path(d, packet_id), packet)
    log.info("packet %s built for job %s", packet_id, job_id)
    return packet


def get_packet(
    packet_id: str, packets_dir: Path | None = None
) -> dict[str, Any] | None:
    """Load a packet, or ``None``."""
    d = _packets_dir(packets_dir, create=False)
    return _read_json(_packet_path(d, packet_id))


def _reload_for_update(
    packet_id: str, packets_dir: Path | None
) -> tuple[dict[str, Any], Path]:
    d = _packets_dir(packets_dir, create=True)
    path = _packet_path(d, packet_id)
    packet = _read_json(path)
    if packet is None:
        raise KeyError(f"unknown packet: {packet_id}")
    return packet, path


def _clear_approval(packet: dict[str, Any]) -> None:
    """Revert a packet to draft, dropping approval stamps."""
    packet["status"] = "draft"
    packet.pop("approved_at", None)
    packet.pop("content_sha256", None)


def mark_change_reviewed(
    packet_id: str,
    change_id: str,
    packets_dir: Path | None = None,
) -> bool:
    """Mark one proposed change reviewed. Returns True when found.

    Any edit to the review state reverts an approved packet to draft:
    approval is of the exact reviewed content.
    """
    packet, path = _reload_for_update(packet_id, packets_dir)
    found = diff_explain.mark_reviewed(packet.get("changes", []), change_id)
    if found:
        _clear_approval(packet)
        _write_json(path, packet)
    return found


def refresh_checklist(
    packet_id: str,
    profile: dict[str, Any],
    job_details: dict[str, Any],
    library_dir: Path | None = None,
    variants_dir: Path | None = None,
    packets_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Recompute the review checklist against current content."""
    from . import evidence_library, versions

    packet, path = _reload_for_update(packet_id, packets_dir)
    variant_info = None
    evidence_items: list[dict[str, Any]] = []
    try:
        if packet.get("variant_id"):
            version = versions.get_version(
                packet["variant_id"], packet["version_id"],
                variants_dir=variants_dir)
            if version is not None:
                variant_info = {"provenance": version.get("provenance", {})}
            packet["_variants_dir"] = (
                str(variants_dir) if variants_dir else None)
        evidence_items = evidence_library.list_items(library_dir=library_dir)
        packet["review_checklist"] = _compute_checklist(
            packet, profile, job_details, variant_info, evidence_items)
    finally:
        packet.pop("_variants_dir", None)
    # A re-check that surfaces new blockers invalidates a prior approval.
    if (packet.get("status") == "approved"
            and any(i.get("status") == "pending"
                    for i in packet["review_checklist"])):
        _clear_approval(packet)
    _write_json(path, packet)
    return packet["review_checklist"]


def approve_packet(
    packet_id: str,
    packets_dir: Path | None = None,
) -> dict[str, Any]:
    """Approve a packet whose checklist has no pending items.

    Raises :class:`ValueError` listing blockers when items are pending.
    Approval pins a SHA-256 of the packet content; the packet is still
    build-only — approval never submits anything.
    """
    packet, path = _reload_for_update(packet_id, packets_dir)
    blockers = [
        item["label"] for item in packet.get("review_checklist", [])
        if item.get("status") == "pending"
    ]
    if blockers:
        raise ValueError(
            "packet has pending checklist items: " + "; ".join(blockers))
    content = json.dumps(
        {k: packet.get(k) for k in
         ("resume_md", "cover_letter", "answers", "fit_rationale")},
        sort_keys=True,
    ).encode("utf-8")
    packet["status"] = "approved"
    packet["approved_at"] = _utcnow()
    packet["content_sha256"] = hashlib.sha256(content).hexdigest()
    _write_json(path, packet)
    return packet


def render_packet_text(packet: dict[str, Any]) -> str:
    """Plain-text rendering of the packet (terminal/phone)."""
    lines = [
        f"Application packet for job {packet.get('job_id')}",
        f"Status: {packet.get('status')}",
        "",
        "## Review checklist",
    ]
    for item in packet.get("review_checklist", []):
        mark = {"done": "✓", "pending": "○", "skipped": "–"}.get(
            item.get("status"), "?")
        lines.append(f"[{mark}] {item['label']} — {item['detail']}")
    lines += ["", "## Fit rationale", packet.get("fit_rationale", "").rstrip(),
              "", "## Grill answers"]
    answers = packet.get("answers", [])
    if not answers:
        lines.append("(no grill session for this job)")
    for qa in answers:
        lines.append(f"Q: {qa['question']}")
        lines.append(f"A: {qa['answer']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
