#!/usr/bin/env python3
"""Initiative 12 / Epic 3 — Transparent changelog.

Shows what shipped, what was rejected, what remains limited, and *why*.
Entries are curated records (not marketing copy): every entry names the
decision, the evidence or reason behind it (``source``: a verifiable
provenance pointer — a repo path, never an invented reference), and the
current limitation.

Sources:

* The entries file next to this module (see ``ENTRIES_FILE``) — curated,
  categorized entries. Curated entries are the source of truth for
  rejected/limited items.
* Optional git augmentation (convenience only) from ``git log``.

Categories: ``shipped`` | ``rejected`` | ``limited``. A changelog with no
rejected or limited entries is treated as a defect — transparency means
showing the no's.

Read/write discipline: :func:`load_entries` is a pure read — it never
writes and never seeds. Seeding is the explicit :func:`seed_entries`,
used only by the ``seed`` CLI command (and by :func:`add_entry` when the
file does not exist yet, which is a write path).

Backup/recovery: the entries file lives next to this module and is
currently UNTRACKED in git (the whole ``initiatives/i12`` package is new;
a later sweep commits it) — verified 2026-09-13 via ``git ls-files``.
Until it is committed, recovery means re-running the ``seed`` command.
Once committed, git history is the backup: restore with
``git checkout -- <entries file>``.

Minimal CLI: ``python -m initiatives.i12.changelog
{render [--with-git] | add-entry --date ... --source ... | seed [--force] | health}``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
ENTRIES_FILE = BASE_DIR / "CHANGELOG_ENTRIES.json"

CATEGORIES = ("shipped", "rejected", "limited")

#: Entry fields that must be present and non-empty.
_REQUIRED_FIELDS = ("date", "title", "why", "limitation", "source")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Conservative allowlist for a user-supplied git ref: no leading dash
#: (would be parsed as a flag), no whitespace or shell metacharacters.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./~^+:@-]*$")

#: Language that contradicts a "shipped" categorization at a glance.
_NOT_LAUNCHED_RE = re.compile(
    r"\bnot\s+(launched|live|public|shipped|released)\b", re.IGNORECASE
)


def _repo_root() -> Path:
    """Repo root for the git-log convenience view.

    Prefers the real git top-level; falls back to the module-file-derived
    tree root (no dependence on the caller's working directory).
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, cwd=BASE_DIR,
        )
    except (OSError, subprocess.SubprocessError):
        out = None
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip())
    return BASE_DIR.parent.parent


def _sanitize_ref(ref: str) -> str:
    """Validate a user-supplied git ref before it reaches argv.

    The ref is passed as a single argv element (no shell), but a leading
    dash would still be parsed as a git flag, so anything outside the
    allowlist is rejected outright.
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    if not _REF_RE.match(ref):
        raise ValueError(f"unsafe git ref: {ref!r}")
    return ref


def _default_entries() -> list[dict[str, Any]]:
    return [
        {
            "date": "2026-09-13",
            "category": "shipped",
            "title": "Public growth machinery built — launch-gated, not yet public "
                     "(Initiative 12 build)",
            "why": "Roadmap Q3 scope; builds the analytics tripwire, public "
                   "tool demos, shareable artifacts, and education library "
                   "before any public surface exists.",
            "limitation": "Launch-gated: nothing in this package goes public "
                          "until Paul explicitly orders a launch (hard "
                          "boundary in initiatives/i12/__init__.py).",
            "source": "initiatives/i12/tools.py, contracts.py, share.py, "
                      "changelog.py, onboarding.py, telemetry.py (new modules, "
                      "working tree 2026-09-13); Q3 2027 scope and launch "
                      "gate in initiatives/i12/__init__.py and "
                      "initiatives/i12/README.md",
        },
        {
            "date": "2026-09-13",
            "category": "rejected",
            "title": "Vanity metrics as analytics north star",
            "why": "Annual roadmap guardrail: never optimize applications per "
                   "day. Growth analytics measure qualified activation and "
                   "workflow completion only.",
            "limitation": "N/A — rejected outright.",
            "source": "docs/i12/education/qualified-applications.md: "
                      "'Never optimize applications-per-day'",
        },
        {
            "date": "2026-09-13",
            "category": "rejected",
            "title": "Resume-text collection in product analytics",
            "why": "Local-first constraint + Q3 exit gate. Analytics events "
                   "carry metadata tokens only; the tripwire shuts analytics "
                   "off if any content field appears.",
            "limitation": "N/A — rejected outright.",
            "source": "initiatives/i12/telemetry.py — Q3 exit-gate tripwire: "
                      "analytics shuts off, quarantines, and deletes the live "
                      "copy if any content field appears in a payload",
        },
        {
            "date": "2026-09-13",
            "category": "limited",
            "title": "Public tools are demos, not the product",
            "why": "The full fit engine needs the local profile and evidence "
                   "library; the public demos use compact typed profiles and "
                   "say so.",
            "limitation": "Fit explainer cannot use your real resume; "
                          "install the local app for evidence-backed scoring.",
            "source": "initiatives/i12/tools.py — MiniProfile: compact typed "
                      "skill list, no resume text",
        },
        {
            "date": "2026-09-13",
            "category": "limited",
            "title": "Install path is manual until Initiative 10 ships",
            "why": "Packaging contract pinned (contracts.py); implementation "
                   "belongs to Initiative 10. Faking an installer would be "
                   "worse than a manual path.",
            "limitation": "Current path: clone the repo, follow the README "
                          "quickstart.",
            "source": "initiatives/i12/contracts.py — PACKAGING_EXPECTED_API; "
                      "packaging_capabilities() reports all-unavailable "
                      "pending Initiative 10",
        },
    ]


def _validate_entry(e: Any) -> None:
    """Validate one entry. Raises ValueError on anything malformed."""
    if not isinstance(e, dict):
        raise ValueError(f"changelog entry must be an object: {e!r}")
    if e.get("category") not in CATEGORIES:
        raise ValueError(f"changelog entry has bad category: {e!r}")
    for k in _REQUIRED_FIELDS:
        v = e.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"changelog entry missing non-empty {k!r}: {e!r}")
    if not _DATE_RE.match(e["date"]):
        raise ValueError(
            f"changelog entry date must be YYYY-MM-DD: {e['date']!r}")
    try:
        datetime.strptime(e["date"], "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"changelog entry date is not a real date: {e['date']!r}") from None


def _check_duplicates(entries: list[dict[str, Any]]) -> None:
    """Idempotency guard: (date, title) must be unique."""
    seen: set[tuple[str, str]] = set()
    for e in entries:
        key = (e["date"], e["title"])
        if key in seen:
            raise ValueError(
                f"duplicate changelog entry (date+title): {key!r}")
        seen.add(key)


def _atomic_write_json(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write-temp-then-rename: readers never see a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load_entries() -> list[dict[str, Any]]:
    """Load and validate curated changelog entries.

    Pure read: never writes, never seeds. Returns ``[]`` when the entries
    file does not exist yet (seed it with :func:`seed_entries`).
    """
    if not ENTRIES_FILE.exists():
        return []
    try:
        raw = ENTRIES_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read changelog entries file {ENTRIES_FILE}: {exc}"
        ) from exc
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"changelog entries file is not valid JSON: {ENTRIES_FILE}: {exc}"
        ) from exc
    if not isinstance(entries, list):
        raise ValueError(
            f"changelog entries file must hold a JSON array: {ENTRIES_FILE}")
    for e in entries:
        _validate_entry(e)
    _check_duplicates(entries)
    return entries


def seed_entries(force: bool = False) -> bool:
    """Write the curated seed entries. This is the explicit init path —
    the only place defaults are created. Returns True when it wrote,
    False when the file already existed and ``force`` was not given.
    """
    if ENTRIES_FILE.exists() and not force:
        return False
    entries = _default_entries()
    for e in entries:
        _validate_entry(e)
    _check_duplicates(entries)
    _atomic_write_json(ENTRIES_FILE, entries)
    return True


def add_entry(date: str, category: str, title: str, why: str,
              limitation: str, source: str = "") -> dict[str, Any]:
    """Append a curated entry. Rejections and limitations are first-class.

    ``why``, ``limitation`` and ``source`` must be non-empty; ``date`` must
    be YYYY-MM-DD; ``(date, title)`` must be unique (idempotency guard).
    This is a write path: when the entries file does not exist yet it is
    seeded first so additions never silently drop the curated defaults.
    """
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}")
    entry = {"date": date, "category": category, "title": title,
             "why": why, "limitation": limitation, "source": source}
    _validate_entry(entry)
    if not ENTRIES_FILE.exists():
        seed_entries()
    entries = load_entries()
    if any(e["date"] == date and e["title"] == title for e in entries):
        raise ValueError(
            f"duplicate changelog entry (date+title): {(date, title)!r}")
    entries.append(entry)
    _atomic_write_json(ENTRIES_FILE, entries)
    return entry


def consistency_warnings(
    entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Flag shipped entries whose limitation reads as not-launched.

    A ``shipped`` entry saying "NOT launched" contradicts its category at a
    glance — retitle or recategorize the entry instead of shipping the
    contradiction.
    """
    if entries is None:
        entries = load_entries()
    warnings = []
    for e in entries:
        if e.get("category") == "shipped" and _NOT_LAUNCHED_RE.search(
            e.get("limitation") or ""
        ):
            warnings.append(
                f"Shipped entry {e['date']} {e['title']!r} carries "
                "not-launched language in its limitation — retitle it or "
                "recategorize it."
            )
    return warnings


def git_highlights(since_ref: str = "") -> list[str]:
    """Convenience: recent commit subjects. Curated entries stay authoritative."""
    since_ref = _sanitize_ref(since_ref)
    cmd = ["git", "log", "--pretty=format:%h %ad %s", "--date=short", "-n", "25"]
    if since_ref:
        cmd.insert(2, f"{since_ref}..HEAD")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                             cwd=_repo_root())
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def render_markdown(include_git: bool = False) -> str:
    """Render the transparent changelog as markdown."""
    entries = load_entries()
    by_cat: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES}
    for e in entries:
        by_cat[e["category"]].append(e)
    # A changelog with no rejections is a defect: say so out loud.
    warnings = []
    if not by_cat["rejected"]:
        warnings.append(
            "> ⚠️ This changelog lists no rejected items. That is a "
            "transparency defect — real roadmaps say no."
        )
    if not by_cat["limited"]:
        warnings.append(
            "> ⚠️ This changelog lists no known limitations. That is a "
            "transparency defect — every tool has limits."
        )
    for w in consistency_warnings(entries):
        warnings.append(f"> ⚠️ {w}")
    lines = ["# Veto — Transparent changelog", "",
             "_What shipped, what was rejected, what remains limited, and why._",
             ""]
    lines.extend(warnings)
    if warnings:
        lines.append("")
    for cat in CATEGORIES:
        lines.append(f"## {cat.title()}")
        lines.append("")
        for e in sorted(by_cat[cat], key=lambda x: x["date"], reverse=True):
            lines.append(f"### {e['title']}")
            lines.append(f"_{e['date']}_")
            lines.append("")
            lines.append(f"**Why:** {e['why']}")
            lines.append("")
            lines.append(f"**Limitation:** {e['limitation']}")
            lines.append("")
            lines.append(f"**Source:** {e['source']}")
            lines.append("")
    if include_git:
        lines.append("## Recent commits (context, not decisions)")
        lines.append("")
        for h in git_highlights():
            lines.append(f"- {h}")
        lines.append("")
    return "\n".join(lines)


def health() -> dict[str, Any]:
    """Changelog transparency health: counts per category + defect flags."""
    entries = load_entries()
    counts = {c: sum(1 for e in entries if e["category"] == c) for c in CATEGORIES}
    return {
        "total": len(entries),
        "by_category": counts,
        "missing_rejections": counts["rejected"] == 0,
        "missing_limitations": counts["limited"] == 0,
        "consistency_warnings": consistency_warnings(entries),
    }


def main(argv: list[str] | None = None) -> int:
    """Minimal CLI: render | add-entry | seed | health."""
    ap = argparse.ArgumentParser(
        prog="python -m initiatives.i12.changelog",
        description="Transparent changelog: render, add curated entries, "
                    "seed defaults, or check transparency health.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="render the changelog as markdown")
    r.add_argument("--with-git", action="store_true",
                   help="append recent commit subjects (context only)")

    a = sub.add_parser("add-entry", help="append a curated entry")
    a.add_argument("--date", required=True, help="YYYY-MM-DD")
    a.add_argument("--category", required=True, choices=CATEGORIES)
    a.add_argument("--title", required=True)
    a.add_argument("--why", required=True)
    a.add_argument("--limitation", required=True)
    a.add_argument("--source", required=True,
                   help="verifiable provenance pointer (repo path, never invented)")

    s = sub.add_parser("seed", help="write the curated seed entries")
    s.add_argument("--force", action="store_true",
                   help="overwrite an existing entries file")

    sub.add_parser("health", help="transparency health as JSON")

    args = ap.parse_args(argv)
    if args.cmd == "render":
        print(render_markdown(include_git=args.with_git))
    elif args.cmd == "add-entry":
        add_entry(args.date, args.category, args.title, args.why,
                  args.limitation, args.source)
        print("entry added")
    elif args.cmd == "seed":
        print("seeded" if seed_entries(force=args.force)
              else "already seeded (use --force to overwrite)")
    elif args.cmd == "health":
        print(json.dumps(health(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
