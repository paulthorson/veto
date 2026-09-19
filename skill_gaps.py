"""Skill-gap planner: find what your target jobs want that you don't have yet.

Workflow:
1. ``analyze_gaps(profile, jobs)`` — diff every target job's required
   keywords (via ``tailor.extract_job_keywords``) against the profile's
   skills; aggregate into a ranked gap list scored by
   frequency x seniority-weight.
2. ``gap_plan()`` — map each gap to the closest ``ai_proficiency`` track
   lesson (defensive import; degrades to a concrete practice-project
   idea when the module or a matching lesson is unavailable).
3. ``mark_gap_progress(skill, status)`` — track {planned, practicing,
   closed}. Closing is a self-attestation and requires a one-line
   evidence note; gaps are never auto-closed.

Honesty contract: a closed gap means *you said* you did the work and
wrote down what it was — this module never verifies it and never
presents practice as a credential or certification.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from tailor import extract_job_keywords
from filters import detect_seniority

BASE_DIR = Path(__file__).resolve().parent
STORE_PATH = BASE_DIR / "skill_gaps.json"
PROFILE_PATH = BASE_DIR / "profiles" / "profile.json"

_VALID_STATUSES = ("planned", "practicing", "closed")

# Seniority weight: a gap that senior/staff postings demand is more urgent
# than one that only appears in entry-level postings.
_SENIORITY_WEIGHT = {
    "entry": 0.8,
    "mid": 1.0,
    "senior": 1.25,
    "staff": 1.4,
    "principal": 1.5,
}

# Concrete practice-project ideas for common non-AI skills. These are
# *suggestions for reps*, not credentials — building the project is the
# evidence, and only the user can attest to that.
NON_AI_PROJECTS: dict[str, str] = {
    "kubernetes": (
        "Deploy a small web app to a local k3s/kind cluster: write the "
        "Deployment + Service YAML yourself, expose it, then break it and "
        "fix it. Write up the YAML decisions."
    ),
    "docker": (
        "Containerize one of your own scripts: multi-stage Dockerfile, "
        "non-root user, pinned base image. Document the image size before "
        "and after."
    ),
    "terraform": (
        "Write Terraform that provisions a throwaway cloud sandbox (VPC + "
        "one VM), apply, then destroy. Keep the plan output as your notes."
    ),
    "typescript": (
        "Convert a small JavaScript project to strict TypeScript: fix every "
        "'any', add interfaces for the data shapes, get tsc clean."
    ),
    "graphql": (
        "Build a tiny GraphQL API over data you already have (a JSON file "
        "counts): schema, two queries, one mutation, one resolver with a "
        "dataloader-style batch."
    ),
    "sql": (
        "Take a public dataset and answer 5 business questions with SQL "
        "only: joins, window functions, one CTE. Save the queries."
    ),
    "figma": (
        "Redesign one screen of an app you use: wireframe, then hi-fi "
        "mock, then a clickable prototype. Write 3 sentences on each "
        "design decision."
    ),
    "excel": (
        "Model something real in a spreadsheet: pivot tables, INDEX/MATCH "
        "or XLOOKUP, one dashboard chart. No manual copy-paste steps."
    ),
    "salesforce": (
        "In a free Trailhead playground: build a custom object with "
        "validation rules, one Flow, and one report. Screenshot the Flow."
    ),
    "tableau": (
        "Build a dashboard from a public dataset: 3 chart types, one "
        "parameter-driven filter, published to Tableau Public."
    ),
    "go": (
        "Write a small CLI in Go with goroutines doing real concurrent "
        "work (e.g. parallel URL fetcher): channels, WaitGroup, context "
        "cancellation, tests."
    ),
    "rust": (
        "Rewrite a Python script you own in Rust: fight the borrow "
        "checker, add error handling with Result, ship a binary."
    ),
}

_ai_proficiency: Any = None
_ai_proficiency_tried = False


def _ai_module() -> Any | None:
    """Import ai_proficiency defensively; None when unavailable."""
    global _ai_proficiency, _ai_proficiency_tried
    if not _ai_proficiency_tried:
        _ai_proficiency_tried = True
        try:
            import ai_proficiency as _mod

            _ai_proficiency = _mod
        except Exception:
            _ai_proficiency = None
    return _ai_proficiency


def _lesson_index() -> list[dict[str, Any]]:
    """Flatten every track's lessons into searchable entries."""
    mod = _ai_module()
    if mod is None:
        return []
    tracks = getattr(mod, "TRACKS", {}) or {}
    index: list[dict[str, Any]] = []
    for track, tdata in tracks.items():
        for lesson in tdata.get("lessons", []) or []:
            index.append(
                {
                    "track": track,
                    "lesson_id": lesson.get("id", ""),
                    "title": lesson.get("title", ""),
                    "skill": lesson.get("skill", ""),
                    "est_minutes": lesson.get("est_minutes"),
                }
            )
    return index


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def _best_lesson(skill: str) -> dict[str, Any] | None:
    """Closest ai_proficiency lesson for a gap skill, or None.

    Token-overlap scoring between the skill and each lesson's skill +
    title. Requires at least one shared meaningful token so unrelated
    lessons are never suggested.
    """
    skill_toks = _tokens(skill)
    if not skill_toks:
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for entry in _lesson_index():
        entry_toks = _tokens(entry["skill"] + " " + entry["title"])
        overlap = skill_toks & entry_toks
        # Substring hits count too ("rag" inside "RAG pipelines").
        sub_hits = sum(
            1
            for st in skill_toks
            for et in entry_toks
            if st != et and (st in et or et in st)
        )
        score = len(overlap) * 2 + sub_hits
        if score > best_score:
            best_score = score
            best = entry
    return best if best_score > 0 else None


def _load_store() -> dict[str, Any]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_store(store: dict[str, Any]) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _job_weight(job: dict[str, Any]) -> float:
    level = detect_seniority(str(job.get("title", "") or ""))
    return _SENIORITY_WEIGHT.get(level or "mid", 1.0)


def analyze_gaps(
    profile: dict[str, Any] | None = None,
    jobs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Diff target jobs' required keywords against the profile's skills.

    Args:
        profile: candidate profile (defaults to the saved onboarding
            profile). Only the profile's own skill list is used.
        jobs: target job dicts (title/description/requirements/snippet).

    Returns:
        {"gaps": [...], "markdown": ...} — each gap has the skill, how
        many target jobs want it, the frequency x seniority weighted
        score, and up to 3 example job titles. Existing progress statuses
        in skill_gaps.json are preserved across re-runs.
    """
    prof = dict(profile) if profile is not None else _load_profile()
    jobs = list(jobs) if jobs else []

    agg: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        weight = _job_weight(job)
        title = str(job.get("title", "") or "Untitled role")
        try:
            missing = extract_job_keywords(job, prof).get("missing", [])
        except Exception:
            continue
        for skill in missing:
            key = skill.lower().strip()
            entry = agg.setdefault(
                key, {"skill": skill, "job_count": 0, "weighted": 0.0, "example_jobs": []}
            )
            entry["job_count"] += 1
            entry["weighted"] += weight
            if title not in entry["example_jobs"] and len(entry["example_jobs"]) < 3:
                entry["example_jobs"].append(title)

    ranked = sorted(
        agg.values(),
        key=lambda e: (-e["weighted"], -e["job_count"], e["skill"].lower()),
    )
    gaps = [
        {
            "skill": e["skill"],
            "job_count": e["job_count"],
            "weighted_score": round(e["weighted"], 2),
            "example_jobs": e["example_jobs"],
        }
        for e in ranked
    ]

    # Persist only when the gap set actually changed, so a no-op analysis
    # (e.g. triggered by --help or a bare import) never creates or
    # rewrites the state file. Statuses the user already recorded are
    # preserved across re-runs.
    store = _load_store()
    old = store.get("gaps", {}) if isinstance(store.get("gaps"), dict) else {}
    now = int(time.time())
    merged: dict[str, Any] = {}
    for gap in gaps:
        key = gap["skill"].lower()
        prev = old.get(key, {}) if isinstance(old.get(key), dict) else {}
        merged[key] = {
            **gap,
            "status": prev.get("status", "planned"),
            "evidence": prev.get("evidence"),
            "updated_at": prev.get("updated_at", now),
        }
    if merged != old:
        store["gaps"] = merged
        store["analyzed_at"] = now
        store["jobs_analyzed"] = len(jobs)
        _save_store(store)

    lines = ["# Skill gaps", ""]
    if not gaps:
        lines.append("No gaps found — your skills cover every target job's keywords.")
    else:
        for i, gap in enumerate(gaps, 1):
            lines.append(
                f"{i}. **{gap['skill']}** — wanted by {gap['job_count']} job(s) "
                f"(score {gap['weighted_score']}); e.g. "
                + ", ".join(gap["example_jobs"])
            )
    return {"gaps": gaps, "jobs_analyzed": len(jobs), "markdown": "\n".join(lines)}


def gap_plan() -> dict[str, Any]:
    """Map each stored gap to a learning step.

    Prefers the closest ``ai_proficiency`` track lesson; falls back to a
    concrete practice-project idea (from NON_AI_PROJECTS, else a generic
    project template). Degrades gracefully when ai_proficiency is
    unavailable — the plan never fails, it just suggests projects.
    """
    store = _load_store()
    gaps = store.get("gaps", {}) if isinstance(store.get("gaps"), dict) else {}
    if not gaps:
        return {
            "plan": [],
            "markdown": "No gaps analyzed yet — run `skill-gaps analyze` first.",
        }

    plan: list[dict[str, Any]] = []
    for key in sorted(gaps, key=lambda k: -gaps[k].get("weighted_score", 0)):
        gap = gaps[key]
        skill = gap["skill"]
        lesson = _best_lesson(skill)
        if lesson is not None:
            step = {
                "type": "ai_proficiency_lesson",
                "skill": skill,
                "track": lesson["track"],
                "lesson_id": lesson["lesson_id"],
                "title": lesson["title"],
                "est_minutes": lesson.get("est_minutes"),
                "why": f"Closest lesson to '{skill}' in the {lesson['track']} track.",
            }
        elif key in NON_AI_PROJECTS:
            step = {
                "type": "practice_project",
                "skill": skill,
                "project": NON_AI_PROJECTS[key],
                "why": f"Hands-on project idea for '{skill}'.",
            }
        else:
            step = {
                "type": "practice_project",
                "skill": skill,
                "project": (
                    f"Build a small portfolio project that uses {skill} end "
                    "to end, document what you built and the decisions you "
                    "made, and link it from your resume. The write-up is the "
                    "evidence."
                ),
                "why": f"No built-in lesson or project idea for '{skill}' — generic project.",
            }
        step["status"] = gap.get("status", "planned")
        plan.append(step)

    store["plan"] = plan
    store["planned_at"] = int(time.time())
    _save_store(store)

    lines = ["# Gap-closing plan", ""]
    for i, step in enumerate(plan, 1):
        if step["type"] == "ai_proficiency_lesson":
            lines.append(
                f"{i}. **{step['skill']}** [{step['status']}] → lesson "
                f"`{step['lesson_id']}` \"{step['title']}\" "
                f"({step['track']} track, ~{step.get('est_minutes') or '?'} min)"
            )
        else:
            lines.append(
                f"{i}. **{step['skill']}** [{step['status']}] → project: {step['project']}"
            )
    return {"plan": plan, "markdown": "\n".join(lines)}


def mark_gap_progress(
    skill: str, status: str, evidence: str | None = None
) -> dict[str, Any]:
    """Record progress on a gap skill.

    Args:
        skill: the gap skill (case-insensitive; must come from a prior
            ``analyze_gaps`` run).
        status: one of planned | practicing | closed.
        evidence: required when closing — a one-line note of what you
            built or did (e.g. "shipped k3s homelab deploy, notes in
            ~/notes/k3s.md").

    Closing is a self-attestation: the module records your claim, never
    verifies it, and a closed gap is practice logged — never a
    credential or certification.
    """
    status = (status or "").strip().lower()
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"Unknown status {status!r}; use one of {', '.join(_VALID_STATUSES)}"
        )
    if status == "closed" and not (evidence or "").strip():
        raise ValueError(
            "Closing a gap requires a one-line evidence note — "
            "what did you build or do? (e.g. mark_gap_progress("
            "'kubernetes', 'closed', evidence='deployed k3s homelab'))"
        )

    store = _load_store()
    gaps = store.get("gaps", {}) if isinstance(store.get("gaps"), dict) else {}
    key = (skill or "").strip().lower()
    if key not in gaps:
        known = ", ".join(sorted(g["skill"] for g in gaps.values())) or "none"
        raise ValueError(
            f"Unknown gap skill {skill!r}. Known gaps: {known}. "
            "Run `skill-gaps analyze` first."
        )
    gaps[key]["status"] = status
    gaps[key]["evidence"] = (evidence or "").strip() or None
    gaps[key]["updated_at"] = int(time.time())
    _save_store(store)
    return {"skill": gaps[key]["skill"], **gaps[key]}


def _load_profile() -> dict[str, Any]:
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Initiative 06 — longitudinal practice gaps (observed, never invented)
# ---------------------------------------------------------------------------
#
# These functions read the interview-lab practice history
# (initiatives.i06.longitudinal) — per-session disclosed-rubric scores —
# and surface *observed* gaps plus the next drill. The hard boundary:
# recommendations come only from observed gaps (same disclosed rubric
# dimension below threshold in >= repeat_count of the last `window`
# completed sessions) or from the user's explicit focus selection.
# With neither, the engine says so and suggests a maintenance rep —
# it never invents weakness labels and never emits a generic course
# list.

def _i06_longitudinal() -> Any:
    """Import the longitudinal engine (same repo)."""
    from initiatives.i06 import longitudinal as _mod

    return _mod


#: Tests redirect the practice history/focus stores here; None = default.
_PRACTICE_HISTORY_PATH: Path | None = None
_PRACTICE_FOCUS_PATH: Path | None = None


def practice_observed_gaps(
    window: int = 3,
    repeat_count: int = 2,
    threshold: int = 70,
) -> dict[str, Any]:
    """Find observed practice gaps under the parameterized rule.

    Args:
        window: how many recent completed sessions to consider.
        repeat_count: minimum sessions below threshold to count.
        threshold: per-dimension "meets" score (0-100). The default
            (70) is pending the operator + independent-framework-reviewer
            approval per the roadmap Q1 gate; pass the approved value.

    Returns {"gaps", "rule", "sessions_considered", "sessions_total"}.
    Each gap has basis "observed" with the session evidence behind it.
    """
    mod = _i06_longitudinal()
    rule = mod.ObservedGapRule(window=window, repeat_count=repeat_count,
                               threshold=threshold)
    history, history_ok, history_note = mod._read_history_store(  # noqa: SLF001 - same project, test seams
        _PRACTICE_HISTORY_PATH or mod.HISTORY_PATH)
    if not history_ok:
        # Corrupt history must never look like a clean bill of health:
        # mirror the engine's own corrupt-path output shape.
        return {
            "gaps": [],
            "rule": mod._rule_payload(rule),  # noqa: SLF001 - same project, pending_approval
            "sessions_considered": 0,
            "sessions_total": 0,
            "history_ok": False,
            "history_note": history_note,
        }
    return mod.observed_gaps(history, rule)


def practice_progress() -> dict[str, Any]:
    """Per-dimension trends: which behaviors improved, which are steady
    or declining, and whether the latest session meets threshold.

    Trends are computed from the numbers only — dimension names come
    from the disclosed rubric; no weakness labels are invented.
    """
    mod = _i06_longitudinal()
    history, history_ok, history_note = mod._read_history_store(  # noqa: SLF001 - same project, test seams
        _PRACTICE_HISTORY_PATH or mod.HISTORY_PATH)
    if not history_ok:
        # Corrupt history means "could not read your history", never
        # an empty report. Mirrors the engine's own corrupt-path shape.
        return {
            "dimensions": [],
            "outcome_note": mod.link_outcomes([])["outcome_note"],
            "sessions_total": 0,
            "history_ok": False,
            "history_note": history_note,
        }
    return mod.progress_report(history)


def _recommend_drill_history_corrupt(mod: Any, history_note: str,
                                    focus_path: Any) -> dict[str, Any]:
    """Corrupt-history branch of :func:`recommend_next_drill`.

    Mirrors the longitudinal engine's own corrupt-path output shape
    (see ``recommend_next_drill`` in initiatives/i06/longitudinal.py):
    ``history_ok`` False, the note surfaced loudly, and never a clean
    "no gaps" maintenance rep — the record may be destroyed, not
    empty. The explicit focus store is still read with provenance
    (a valid focus is honored; an unreadable one is flagged).
    """
    rule = mod.DEFAULT_RULE
    history: list = []
    history_ok = False
    focus, focus_ok, focus_note = mod._read_focus_store(focus_path)  # noqa: SLF001 - same project

    focus_age = mod._focus_age_days(focus)  # noqa: SLF001 - same project
    stale = mod._focus_is_stale(focus)  # noqa: SLF001 - same project
    expired_focus: dict[str, Any] | None = None
    also_observed: list[dict[str, Any]] = []
    drill_warnings: list[str] = []
    recommendations: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []

    if focus and not stale:
        basis = "user_selected"
        targets = [{"dimension": focus["dimension"],
                    "basis": "user_selected",
                    "selected_at": focus.get("selected_at"),
                    "focus_age_days": focus_age,
                    "stale": False}]
    elif focus and stale:
        expired_focus = {"dimension": focus["dimension"],
                         "selected_at": focus.get("selected_at"),
                         "focus_age_days": focus_age}
        basis = "none"
    else:
        basis = "none"

    for target in targets:
        dim = target["dimension"]
        drills = mod.DRILLS.get(dim)
        if not drills:
            drill_warnings.append(
                f"No DRILLS entry for dimension {dim!r}: cannot render a "
                f"drill for this target. Add a DRILLS entry for {dim!r} — "
                f"the target is NOT being silently dropped.")
            continue
        for drill in drills:
            recommendations.append({
                "dimension": dim,
                "basis": target["basis"],
                "lab": drill["lab"],
                "mode": drill["mode"],
                "drill": drill["drill"],
                "reps": drill["reps"],
            })

    if basis == "none":
        # Never claim "no observed gaps" when the history could not be
        # read — the record may be destroyed, not empty.
        recommendations.append({
            "dimension": None,
            "basis": "none",
            "lab": "mock_interview",
            "mode": "hiring_manager",
            "drill": ("Holding pattern: your practice history could not "
                      "be loaded, so no recommendation can be made from "
                      "your recorded sessions. Recover the history file "
                      "first — do not treat this as a clean bill of "
                      "health."),
            "reps": "1 round",
        })

    lines = ["# Next drill", ""]
    lines.append(
        f"⚠️ Practice history could not be loaded ({history_note}). "
        f"The recommendation below does NOT reflect your recorded "
        f"sessions — recover the history file before trusting it.")
    lines.append("")
    if not focus_ok:
        lines.append(f"⚠️ Focus selection unreadable ({focus_note}) — "
                     f"proceeding without it.")
        lines.append("")
    if basis == "user_selected":
        target = targets[0]
        age = target.get("focus_age_days")
        age_txt = (f" (selected {age} day(s) ago; focus expires after "
                   f"{mod.FOCUS_TTL_DAYS} days)"
                   if age is not None else "")
        lines.append(f"Your selected focus: **{target['dimension']}**"
                     f"{age_txt}.")
    else:
        if expired_focus:
            lines.append(
                f"Your focus selection (**{expired_focus['dimension']}**, "
                f"selected {expired_focus['focus_age_days']} day(s) ago) "
                f"expired after {mod.FOCUS_TTL_DAYS} days. Gaps cannot be "
                f"assessed either — your practice history could not be "
                f"loaded, so nothing is labeled a weakness because nothing "
                f"could be measured. Suggested maintenance:")
    lines.append("")
    for i, rec in enumerate(recommendations, 1):
        dim = rec["dimension"] or "maintenance"
        lines.append(f"{i}. **{dim}** ({rec['lab']}, {rec['mode']}): "
                     f"{rec['drill']} [{rec['reps']}]")
    for warning in drill_warnings:
        lines += ["", f"⚠️ {warning}"]
    return {
        "basis": basis,
        "rule": mod._rule_payload(rule),  # noqa: SLF001 - same project, pending_approval
        "targets": targets,
        "also_observed": also_observed,
        "expired_focus": expired_focus,
        "recommendations": recommendations,
        "drill_warnings": drill_warnings,
        "sessions_total": len(history),
        "history_ok": history_ok,
        "history_note": history_note,
        "focus_ok": focus_ok,
        "focus_note": focus_note,
        "markdown": "\n".join(lines),
    }


def recommend_next_drill() -> dict[str, Any]:
    """Recommend the next drill from observed gaps or your selection.

    Priority: your explicit focus first, then observed gaps (worst
    average first). With neither, basis is "none" and the suggestion
    is a maintenance rep — explicitly not a weakness.
    """
    mod = _i06_longitudinal()
    history, history_ok, history_note = mod._read_history_store(  # noqa: SLF001 - same project, test seams
        _PRACTICE_HISTORY_PATH or mod.HISTORY_PATH)
    focus_path = _PRACTICE_FOCUS_PATH or mod.FOCUS_PATH
    if not history_ok:
        # Never a clean "no gaps": mirror the engine's own
        # corrupt-history output shape (focus still honored loudly).
        return _recommend_drill_history_corrupt(mod, history_note,
                                               focus_path)
    return mod.recommend_next_drill(history, focus_path=focus_path)


def select_practice_focus(dimension: str) -> dict[str, Any]:
    """Set your explicit practice focus (a user choice, not a system
    label). Takes priority over observed gaps in recommendations."""
    mod = _i06_longitudinal()
    return mod.select_focus(
        dimension, focus_path=_PRACTICE_FOCUS_PATH or mod.FOCUS_PATH)


def clear_practice_focus() -> dict[str, Any]:
    """Clear your explicit practice focus."""
    mod = _i06_longitudinal()
    mod.clear_focus(focus_path=_PRACTICE_FOCUS_PATH or mod.FOCUS_PATH)
    return {"cleared": True}


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("markdown") or json.dumps(result, indent=2))


def cmd_skill_gaps(args: Any) -> int:
    """CLI handler for `skill-gaps`."""
    action = (getattr(args, "action", "") or "").strip().lower()
    as_json = getattr(args, "json", False)
    if action == "analyze":
        jobs_path = getattr(args, "jobs", "") or ""
        if not jobs_path:
            raise SystemExit("analyze requires --jobs PATH to a JSON list of job dicts")
        jobs = json.loads(Path(jobs_path).read_text(encoding="utf-8"))
        profile_path = getattr(args, "profile", "") or str(PROFILE_PATH)
        try:
            profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            profile = {}
        _print_result(analyze_gaps(profile, jobs), as_json)
    elif action == "plan":
        _print_result(gap_plan(), as_json)
    elif action == "mark":
        _print_result(
            mark_gap_progress(
                getattr(args, "skill", ""),
                getattr(args, "status", ""),
                getattr(args, "evidence", None),
            ),
            as_json,
        )
    elif action == "observe":
        _print_result(
            practice_observed_gaps(
                window=int(getattr(args, "window", 3) or 3),
                repeat_count=int(getattr(args, "repeat", 2) or 2),
                threshold=int(getattr(args, "threshold", 70) or 70),
            ),
            as_json,
        )
    elif action == "progress":
        result = practice_progress()
        if not as_json and "markdown" not in result:
            lines = ["# Practice progress", ""]
            for dim in result.get("dimensions", []):
                delta = (f"{dim['delta']:+}" if dim["delta"] is not None
                         else "n/a")
                lines.append(
                    f"- **{dim['dimension']}**: {dim['sessions']} sessions, "
                    f"latest {dim['latest_score']}/100, trend "
                    f"{dim['trend']} (delta {delta})")
            lines += ["", result.get("outcome_note", "")]
            result = {**result, "markdown": "\n".join(lines)}
        _print_result(result, as_json)
    elif action == "recommend":
        _print_result(recommend_next_drill(), as_json)
    elif action == "focus":
        _print_result(select_practice_focus(getattr(args, "skill", "")),
                      as_json)
    elif action == "unfocus":
        _print_result(clear_practice_focus(), as_json)
    else:
        raise SystemExit(
            "Unknown action {!r}; use analyze|plan|mark|observe|progress|"
            "recommend|focus|unfocus".format(action))
    return 0


def register_tools(mcp: Any) -> None:
    """Register the skill-gap MCP tools on an MCP server instance."""
    _impl_analyze = globals()["analyze_gaps"]
    _impl_plan = globals()["gap_plan"]
    _impl_mark = globals()["mark_gap_progress"]
    _impl_observe = globals()["practice_observed_gaps"]
    _impl_progress = globals()["practice_progress"]
    _impl_recommend = globals()["recommend_next_drill"]
    _impl_focus = globals()["select_practice_focus"]
    _impl_unfocus = globals()["clear_practice_focus"]

    @mcp.tool()
    def analyze_skill_gaps(profile: dict, jobs: list) -> dict:
        """Diff target jobs' required keywords against profile skills.

        Args:
            profile: candidate profile dict (with a "skills" list).
            jobs: list of job dicts (title/description/requirements).

        Returns:
            Ranked gap list scored by frequency x seniority-weight, each
            with skill, job_count, weighted_score, example job titles.
        """
        return _impl_analyze(profile, jobs)

    @mcp.tool()
    def skill_gap_plan() -> dict:
        """Map each analyzed gap to a learning step.

        Returns:
            Plan entries pointing at the closest ai_proficiency lesson
            or a concrete practice-project idea, with current status.
        """
        return _impl_plan()

    @mcp.tool()
    def mark_gap_progress(skill: str, status: str, evidence: str = "") -> dict:
        """Record progress on a gap skill.

        Args:
            skill: gap skill from analyze_skill_gaps.
            status: planned | practicing | closed.
            evidence: one-line note of what you built/did — required
                when closing. Closing is a self-attestation, never a
                verified credential.
        """
        return _impl_mark(skill, status, evidence)

    @mcp.tool()
    def practice_observed_gaps(window: int = 3, repeat_count: int = 2,
                               threshold: int = 70) -> dict:
        """Find observed interview-practice gaps.

        An observed gap is a disclosed rubric dimension scoring below
        `threshold` in at least `repeat_count` of the last `window`
        completed practice sessions. Unscored dimensions never count.

        Args:
            window: recent completed sessions to consider.
            repeat_count: minimum sessions below threshold.
            threshold: per-dimension "meets" score (default 70,
                pending the operator + reviewer approval per the Q1 gate).
        """
        return _impl_observe(window, repeat_count, threshold)

    @mcp.tool()
    def practice_progress() -> dict:
        """Per-dimension practice trends: improved, steady, or declining.

        Computed from session scores only; no weakness labels invented.
        """
        return _impl_progress()

    @mcp.tool()
    def recommend_next_drill() -> dict:
        """Recommend the next drill from observed gaps or your focus.

        Your explicit focus wins; then observed gaps (worst first).
        With neither, suggests a maintenance rep — never a generic
        course list, never an invented weakness.
        """
        return _impl_recommend()

    @mcp.tool()
    def select_practice_focus(dimension: str) -> dict:
        """Set your explicit practice focus dimension.

        A user choice, not a system label; takes priority in
        recommend_next_drill.

        Args:
            dimension: one of structure, evidence, clarity, trade_offs,
                question_quality.
        """
        try:
            return _impl_focus(dimension)
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    def clear_practice_focus() -> dict:
        """Clear your explicit practice focus."""
        return _impl_unfocus()


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `skill-gaps` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser("skill-gaps", help="Analyze and close skill gaps.")
    p.add_argument(
        "action",
        help=("One of: analyze, plan, mark, observe, progress, recommend, "
              "focus, unfocus."),
    )
    p.add_argument("--jobs", default="", help="analyze: path to JSON list of job dicts.")
    p.add_argument(
        "--profile", default="", help="analyze: path to profile JSON (default: saved profile)."
    )
    p.add_argument("--skill", default="", help="mark: the gap skill; focus: the dimension.")
    p.add_argument("--status", default="", help="mark: planned | practicing | closed.")
    p.add_argument(
        "--evidence", default=None, help="mark: one-line evidence note (required to close)."
    )
    p.add_argument("--window", default="3", help="observe: recent sessions to consider.")
    p.add_argument("--repeat", default="2", help="observe: minimum sessions below threshold.")
    p.add_argument("--threshold", default="70", help="observe: per-dimension meets score.")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    return {"skill-gaps": cmd_skill_gaps}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skill-gaps")
    sub = parser.add_subparsers()
    handlers = register_cli(sub)
    args = parser.parse_args(argv)
    handler = handlers.get("skill-gaps")
    return handler(args) if handler else 2


if __name__ == "__main__":
    raise SystemExit(main())
