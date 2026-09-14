#!/usr/bin/env python3
"""Veto Application Studio — terminal interface (Initiative 05).

Every studio workflow is reachable here: versioned variants, evidence
library, ATS readiness, diff explanations, application packets, and
earned-success feedback.

Web UI / phone wiring is specified in ``integration_notes.md`` — those
files (``cli.py``, ``webui.py``, ``dashboard.py``, ``server.py``) are
owned by other teams and are not edited from this initiative.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from initiatives.i05 import (  # noqa: E402
    ats_check,
    diff_explain,
    evidence_library,
    packet,
    success_feedback,
    versions,
)


def _load_json(path: str | None) -> dict:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _p(path: str | None) -> Path | None:
    return Path(path) if path else None


# ---------------------------------------------------------------- variants

def cmd_variant_create(a: argparse.Namespace) -> int:
    # NOTE: the variant-create parser defines no --profile (and
    # create_variant takes no profile): branches start empty and are
    # filled by variant-tailor. Use create_base via the Python API when
    # a base rendered from a profile is wanted.
    variant = versions.create_variant(
        a.name, job_id=a.job_id, variants_dir=_p(a.variants_dir))
    print(f"variant {variant['variant_id']} created")
    return 0


def cmd_variant_tailor(a: argparse.Namespace) -> int:
    import tailor as tailor_mod

    profile = _load_json(a.profile)
    job = _load_json(a.job)
    links = evidence_library.links_for_job(
        a.job_id, library_dir=_p(a.library_dir)) if a.job_id else None
    result = tailor_mod.tailor_with_provenance(profile, job, links)
    commit = versions.commit_version(
        a.variant, result["resume_md"], result["cover_letter"],
        provenance=result["provenance"],
        variants_dir=_p(a.variants_dir))
    print(f"committed {commit['version_id']}")
    print(f"matched: {', '.join(result['matched_skills']) or '(none)'}")
    return 0


def cmd_variant_approve(a: argparse.Namespace) -> int:
    versions.approve_version(a.variant, a.version,
                             variants_dir=_p(a.variants_dir))
    print(f"approved {a.version}")
    return 0


def cmd_variant_restore(a: argparse.Namespace) -> int:
    restored = versions.restore_version(
        a.variant, a.version, variants_dir=_p(a.variants_dir))
    out = Path(a.out or f"{a.variant}-{restored['version_id']}.md")
    out.write_text(restored["resume_md"], encoding="utf-8")
    print(f"restored {restored['version_id']} -> {out}")
    return 0


def cmd_variant_trace(a: argparse.Namespace) -> int:
    # trace_report returns {"statements", "unapproved", "unknown"} —
    # per-statement evidence_status is joined here for the summary
    # counts (there are no top-level "profile"/"approved" keys).
    version_id = a.version
    if version_id is None:
        variant = versions.get_variant(
            a.variant, variants_dir=_p(a.variants_dir))
        if variant is None:
            raise KeyError(f"unknown variant: {a.variant}")
        version_id = variant.get("current_version")
    items = evidence_library.list_items(library_dir=_p(a.library_dir))
    report = versions.trace_report(
        a.variant, version_id, items, variants_dir=_p(a.variants_dir))
    profile_backed = sum(
        1 for st in report["statements"]
        if any(s == "profile" for s in st["evidence_status"].values()))
    approved = sum(
        1 for st in report["statements"]
        if any(s == "approved" for s in st["evidence_status"].values()))
    print(f"profile-backed statements: {profile_backed}")
    print(f"statements with approved evidence: {approved}")
    print(f"unapproved: {report['unapproved'] or '(none)'}")
    print(f"unknown: {report['unknown'] or '(none)'}")
    return 0


# ---------------------------------------------------------------- evidence

def cmd_evidence_build(a: argparse.Namespace) -> int:
    profile = _load_json(a.profile)
    items = evidence_library.build_from_profile(
        profile, library_dir=_p(a.library_dir))
    print(f"derived {len(items)} items (all unapproved)")
    return 0


def cmd_evidence_list(a: argparse.Namespace) -> int:
    items = evidence_library.list_items(
        library_dir=_p(a.library_dir),
        approved_only=a.approved_only, kind=a.kind)
    for item in items:
        mark = "✓" if item["approved"] else "○"
        print(f"[{mark}] {item['evidence_id']} ({item['kind']}): "
              f"{item['text'][:80]}")
    return 0


def cmd_evidence_approve(a: argparse.Namespace) -> int:
    # approve_item raises KeyError on unknown ids (it never returns a
    # falsy value), so the "unknown" branch must catch, not test.
    try:
        evidence_library.approve_item(a.evidence_id,
                                      library_dir=_p(a.library_dir))
    except KeyError:
        print(f"unknown evidence id: {a.evidence_id}")
        return 1
    print(f"approved {a.evidence_id}")
    return 0


# --------------------------------------------------------------------- ats

def cmd_ats(a: argparse.Namespace) -> int:
    resume_md = Path(a.resume).read_text(encoding="utf-8")
    job = _load_json(a.job)
    profile = _load_json(a.profile)
    report = ats_check.ats_readiness_check(resume_md, job, profile)
    print(ats_check.render_report_text(report))
    return 0 if report["ready"] else 2


# -------------------------------------------------------------------- diff

def cmd_diff(a: argparse.Namespace) -> int:
    base = Path(a.base).read_text(encoding="utf-8")
    tailored = Path(a.tailored).read_text(encoding="utf-8")
    changes = diff_explain.explain_resume_diff(base, tailored)
    print(diff_explain.render_explanations_text(changes))
    return 0


# ------------------------------------------------------------------ packet

def cmd_packet_build(a: argparse.Namespace) -> int:
    profile = _load_json(a.profile)
    job = _load_json(a.job)
    pkt = packet.build_packet(
        a.job_id or job.get("id", "job"), profile, job,
        variant_id=a.variant, version_id=a.version,
        library_dir=_p(a.library_dir), variants_dir=_p(a.variants_dir),
        packets_dir=_p(a.packets_dir))
    print(f"packet {pkt['packet_id']} ({pkt['status']})")
    print(packet.render_packet_text(pkt))
    return 0


def cmd_packet_review(a: argparse.Namespace) -> int:
    if packet.mark_change_reviewed(a.packet_id, a.change_id,
                                   packets_dir=_p(a.packets_dir)):
        print(f"reviewed {a.change_id}")
        return 0
    print(f"unknown change: {a.change_id}")
    return 1


def cmd_packet_approve(a: argparse.Namespace) -> int:
    profile = _load_json(a.profile)
    job = _load_json(a.job)
    packet.refresh_checklist(
        a.packet_id, profile, job, library_dir=_p(a.library_dir),
        variants_dir=_p(a.variants_dir), packets_dir=_p(a.packets_dir))
    try:
        approved = packet.approve_packet(
            a.packet_id, packets_dir=_p(a.packets_dir))
    except ValueError as exc:
        print(f"blocked: {exc}")
        return 2
    print(f"approved {approved['packet_id']} "
          f"({approved['content_sha256'][:12]}…)")
    return 0


# ---------------------------------------------------------------- feedback

def cmd_feedback(a: argparse.Namespace) -> int:
    moment = success_feedback.success_moment(
        a.kind,
        {"state": a.state, "gate_open": a.gate_open,
         "user_initiated": a.user_initiated,
         "reduced_motion": a.reduced_motion},
        state_dir=_p(a.state_dir))
    if moment["allowed"]:
        print(moment["message"])
        print(f"(dismiss with: studio.py feedback-dismiss "
              f"{moment['dismiss_key']})")
    else:
        print("(no success moment — the gate stays quiet)")
    return 0


def cmd_feedback_dismiss(a: argparse.Namespace) -> int:
    if success_feedback.dismiss(a.dismiss_key, state_dir=_p(a.state_dir)):
        print(f"dismissed {a.dismiss_key}")
    else:
        print(f"already dismissed: {a.dismiss_key}")
    return 0


# ------------------------------------------------------------------ parser

def _add_store_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--variants-dir", default=None)
    p.add_argument("--library-dir", default=None)
    p.add_argument("--packets-dir", default=None)
    p.add_argument("--state-dir", default=None)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="studio.py", description="Veto Application Studio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("variant-create", help="create a resume variant")
    p.add_argument("name")
    p.add_argument("--job-id", default=None)
    _add_store_args(p)
    p.set_defaults(fn=cmd_variant_create)

    p = sub.add_parser("variant-tailor",
                       help="tailor + commit a new version")
    p.add_argument("variant")
    p.add_argument("--profile", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--job-id", default=None)
    _add_store_args(p)
    p.set_defaults(fn=cmd_variant_tailor)

    p = sub.add_parser("variant-approve", help="approve a version")
    p.add_argument("variant")
    p.add_argument("version")
    _add_store_args(p)
    p.set_defaults(fn=cmd_variant_approve)

    p = sub.add_parser("variant-restore", help="restore a prior version")
    p.add_argument("variant")
    p.add_argument("version", nargs="?", default=None)
    p.add_argument("--out", default=None)
    _add_store_args(p)
    p.set_defaults(fn=cmd_variant_restore)

    p = sub.add_parser("variant-trace", help="trace statements to evidence")
    p.add_argument("variant")
    p.add_argument("version", nargs="?", default=None)
    _add_store_args(p)
    p.set_defaults(fn=cmd_variant_trace)

    p = sub.add_parser("evidence-build", help="derive evidence from profile")
    p.add_argument("--profile", required=True)
    _add_store_args(p)
    p.set_defaults(fn=cmd_evidence_build)

    p = sub.add_parser("evidence-list", help="list evidence items")
    p.add_argument("--approved-only", action="store_true")
    p.add_argument("--kind", default=None)
    _add_store_args(p)
    p.set_defaults(fn=cmd_evidence_list)

    p = sub.add_parser("evidence-approve", help="approve an evidence item")
    p.add_argument("evidence_id")
    _add_store_args(p)
    p.set_defaults(fn=cmd_evidence_approve)

    p = sub.add_parser("ats", help="ATS readiness check")
    p.add_argument("--resume", required=True)
    p.add_argument("--job", default=None)
    p.add_argument("--profile", default=None)
    p.set_defaults(fn=cmd_ats)

    p = sub.add_parser("diff", help="explain resume changes")
    p.add_argument("--base", required=True)
    p.add_argument("--tailored", required=True)
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("packet-build", help="build an application packet")
    p.add_argument("--job", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--job-id", default=None)
    p.add_argument("--variant", default=None)
    p.add_argument("--version", default=None)
    _add_store_args(p)
    p.set_defaults(fn=cmd_packet_build)

    p = sub.add_parser("packet-review", help="mark a change reviewed")
    p.add_argument("packet_id")
    p.add_argument("change_id")
    _add_store_args(p)
    p.set_defaults(fn=cmd_packet_review)

    p = sub.add_parser("packet-approve", help="approve a packet")
    p.add_argument("packet_id")
    p.add_argument("--job", required=True)
    p.add_argument("--profile", required=True)
    _add_store_args(p)
    p.set_defaults(fn=cmd_packet_approve)

    p = sub.add_parser("feedback", help="earned-success moment")
    p.add_argument("--kind", default="packet-approved")
    p.add_argument("--state", default="success")
    p.add_argument("--gate-open", action="store_true")
    p.add_argument("--user-initiated", action="store_true")
    p.add_argument("--reduced-motion", action="store_true")
    _add_store_args(p)
    p.set_defaults(fn=cmd_feedback)

    p = sub.add_parser("feedback-dismiss", help="dismiss a success moment")
    p.add_argument("dismiss_key")
    _add_store_args(p)
    p.set_defaults(fn=cmd_feedback_dismiss)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
