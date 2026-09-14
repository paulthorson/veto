#!/usr/bin/env python3
"""Side-by-side diff explanations — Epic 4 of Initiative 05.

Every proposed resume or cover-letter change gets a plain-language
explanation BEFORE approval. Explanations are mechanical: they describe
what changed and why (which job keyword drove it, which evidence backs
it) — they never invent facts.

A change is ``pending`` until explicitly marked reviewed; the packet
checklist (see :mod:`initiatives.i05.packet`) requires every change
reviewed before approval.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from typing import Any


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", str(line or "")).strip().lower()


def _change_id(*bits: str) -> str:
    h = hashlib.sha256("\n".join(bits).encode("utf-8")).hexdigest()
    return f"chg_{h[:12]}"


def _section_of(lines: list[str], idx: int) -> str:
    section = ""
    for line in lines[: idx + 1]:
        s = line.strip()
        if s.startswith("## "):
            section = s[3:].strip()
    return section


def _evidence_for(
    statement: str, statement_map: list[dict[str, Any]]
) -> list[str]:
    needle = _norm(statement)
    for entry in statement_map or []:
        if _norm(entry.get("statement", "")) == needle:
            return list(entry.get("evidence_ids", []) or [])
    return []


def explain_resume_diff(
    base_md: str,
    tailored_md: str,
    matched_skills: list[str] | None = None,
    statement_map: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Explain every change between base and tailored resume.

    Returns ``[{"change_id", "type", "explanation", "before", "after",
    "evidence_ids", "reviewed"}]``. Types: ``reorder`` (a bullet moved),
    ``summary`` (summary rewritten), ``skills`` (Skills section
    added/changed), ``added`` / ``removed`` (anything else).
    ``matched_skills`` lets explanations name the job keyword that drove
    a reorder; ``statement_map`` attaches evidence ids.
    """
    matched = [str(s) for s in (matched_skills or [])]
    base_lines = str(base_md or "").splitlines()
    new_lines = str(tailored_md or "").splitlines()
    sm = difflib.SequenceMatcher(a=base_lines, b=new_lines, autojunk=False)

    changes: list[dict[str, Any]] = []

    def skill_hint(text: str) -> str:
        low = _norm(text)
        hits = [
            s for s in matched
            if re.search(r"(?<![a-z0-9+#])" + re.escape(s.lower())
                         + r"(?![a-z0-9+#])", low)
        ]
        if hits:
            return f" It mentions {', '.join(hits)}, a keyword from the posting."
        return ""

    # First pass: gather deleted and inserted lines with section context.
    deleted: list[tuple[str, str]] = []  # (line, section)
    inserted: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            for k in range(i1, i2):
                s = base_lines[k].strip()
                if s:
                    deleted.append((base_lines[k],
                                    _section_of(base_lines, k)))
        if tag in ("insert", "replace"):
            for k in range(j1, j2):
                s = new_lines[k].strip()
                if s:
                    inserted.append((new_lines[k],
                                     _section_of(new_lines, k)))

    # A line deleted in one place and inserted in another is a move.
    remaining_deleted = list(deleted)
    for line, section in inserted:
        needle = _norm(line)
        match_idx = next(
            (n for n, (dline, _) in enumerate(remaining_deleted)
             if _norm(dline) == needle),
            None,
        )
        if match_idx is not None:
            del remaining_deleted[match_idx]
            changes.append({
                "change_id": _change_id("reorder", line),
                "type": "reorder",
                "explanation": (
                    f"Moved the bullet {line.strip()!r} within "
                    f"{section or 'the resume'}." + skill_hint(line)
                    + " The wording is unchanged — only its position moved."
                ),
                "before": None,
                "after": line,
                "evidence_ids": _evidence_for(line, statement_map or []),
                "reviewed": False,
            })
        else:
            s = line.strip()
            ctype = "added"
            if section.lower() == "summary":
                ctype = "summary"
                expl = (f"Summary rewritten to: {s!r}." + skill_hint(line)
                        + " Built only from profile facts and matched keywords.")
            elif section.lower() == "skills":
                ctype = "skills"
                expl = (f"Skills section now lists: {s!r}."
                        + " Only skills already in the profile are listed.")
            else:
                expl = f"Added to {section or 'the resume'}: {s!r}."
            changes.append({
                "change_id": _change_id("added", line),
                "type": ctype,
                "explanation": expl,
                "before": None,
                "after": line,
                "evidence_ids": _evidence_for(line, statement_map or []),
                "reviewed": False,
            })

    for line, section in remaining_deleted:
        s = line.strip()
        ctype = "removed"
        if section.lower() == "summary":
            ctype = "summary"
        elif section.lower() == "skills":
            ctype = "skills"
        changes.append({
            "change_id": _change_id("removed", line),
            "type": ctype,
            "explanation": f"Removed from {section or 'the resume'}: {s!r}.",
            "before": line,
            "after": None,
            "evidence_ids": [],
            "reviewed": False,
        })

    # Stable order: reorders first (most common), then others.
    order = {"reorder": 0, "summary": 1, "skills": 2, "added": 3,
             "removed": 4}
    changes.sort(key=lambda c: (order.get(c["type"], 9), c["change_id"]))
    # De-dupe identical change ids (repeated lines).
    seen: set[str] = set()
    unique = []
    for c in changes:
        if c["change_id"] not in seen:
            seen.add(c["change_id"])
            unique.append(c)
    return unique


def explain_letter_diff(
    base_letter: str,
    new_letter: str,
) -> list[dict[str, Any]]:
    """Paragraph-level explanations for cover-letter changes."""
    base_paras = [p.strip() for p in str(base_letter or "").split("\n\n")
                  if p.strip()]
    new_paras = [p.strip() for p in str(new_letter or "").split("\n\n")
                 if p.strip()]
    sm = difflib.SequenceMatcher(a=base_paras, b=new_paras, autojunk=False)
    changes: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for para in base_paras[i1:i2]:
            changes.append({
                "change_id": _change_id("letter-removed", para),
                "type": "removed",
                "explanation": f"Removed paragraph: {para[:120]!r}…",
                "before": para,
                "after": None,
                "evidence_ids": [],
                "reviewed": False,
            })
        for para in new_paras[j1:j2]:
            changes.append({
                "change_id": _change_id("letter-added", para),
                "type": "added",
                "explanation": (
                    f"Added paragraph: {para[:120]!r}… "
                    "Verify every claim traces to the profile."
                ),
                "before": None,
                "after": para,
                "evidence_ids": [],
                "reviewed": False,
            })
    return changes


def side_by_side(
    base_md: str, tailored_md: str
) -> list[dict[str, Any]]:
    """Line-pair data for side-by-side UI rendering.

    Returns ``[{"left", "right", "kind"}]`` where kind is
    ``same`` | ``changed`` | ``added`` | ``removed``.
    """
    base_lines = str(base_md or "").splitlines()
    new_lines = str(tailored_md or "").splitlines()
    sm = difflib.SequenceMatcher(a=base_lines, b=new_lines, autojunk=False)
    pairs: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                pairs.append({"left": base_lines[i1 + k],
                              "right": new_lines[j1 + k], "kind": "same"})
        elif tag == "replace":
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                pairs.append({
                    "left": base_lines[i1 + k] if i1 + k < i2 else "",
                    "right": new_lines[j1 + k] if j1 + k < j2 else "",
                    "kind": "changed",
                })
        elif tag == "delete":
            for k in range(i1, i2):
                pairs.append({"left": base_lines[k], "right": "",
                              "kind": "removed"})
        else:  # insert
            for k in range(j1, j2):
                pairs.append({"left": "", "right": new_lines[k],
                              "kind": "added"})
    return pairs


def mark_reviewed(
    changes: list[dict[str, Any]], change_id: str
) -> bool:
    """Mark one change reviewed. Returns True when found."""
    for change in changes:
        if change.get("change_id") == change_id:
            change["reviewed"] = True
            return True
    return False


def pending_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changes not yet marked reviewed — approval requires this empty."""
    return [c for c in changes if not c.get("reviewed")]


def render_explanations_text(changes: list[dict[str, Any]]) -> str:
    """Plain-text rendering of change explanations (terminal/phone)."""
    if not changes:
        return "No changes proposed — the tailored resume matches the base.\n"
    lines = [f"{len(changes)} proposed change(s):", ""]
    for i, c in enumerate(changes, 1):
        mark = "✓" if c.get("reviewed") else "○"
        lines.append(f"{i}. [{mark}] ({c['type']}) {c['explanation']}")
    lines.append("")
    pending = pending_changes(changes)
    lines.append(
        f"{len(pending)} change(s) still need review before approval."
        if pending else "All changes reviewed — ready for approval.")
    return "\n".join(lines) + "\n"
