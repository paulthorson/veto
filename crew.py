#!/usr/bin/env python3
"""Governed AI crew for veto-mcp.

Eleven agent personas drawn from the repo's agentic-governance harnesses
(``governance/engine.py``) and the framework's role harnesses
(``/tmp/agentic-governance/harnesses/``): CEO (routes work, escalates),
Chief of Staff (advisory funnel), producing workers (PM, engineer, UX,
QA, researcher), job-search specialists (scout, tailor, coach), and
Adversarial review, with the spawn -> work -> review -> merge lifecycle
implemented in ``governance.engine.run_governed_job``.

Every persona card is based directly on its framework harness — identity,
what it owns, what it never does, and stop conditions — adapted from the
framework's generic domain to the job-search domain, with the harness's
prohibitions and stop conditions kept intact in spirit.

The persona ``persona`` texts are written as system-prompt fragments an
LLM orchestrator can adopt verbatim. This module does NOT impersonate an
LLM: ``run_agent`` runs a persona's task through the governed lifecycle
(spawn veto-screen, work, adversarial review, verdict ledger) and returns
the lifecycle report. When the governance framework is unavailable the
deterministic compliance gates still hold (fail-safe) and the report is
marked ``compliance-only``, per ``governance/risk_constitution.md``.

Nothing here submits applications, sends messages, or bypasses confirm
gates: every crew plan ends with a ``red`` adversarial review job and a
human confirm gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Persona cards (from /tmp/agentic-governance/harnesses/*, job-search adapted)
# ---------------------------------------------------------------------------

PERSONAS: dict[str, dict[str, Any]] = {
    "ceo": {
        "id": "ceo",
        "name": "Chief",
        "role": "CEO harness — routes work, paces, resolves by precedent, escalates",
        "persona": (
            "You are the Chief, the CEO harness of this job-search crew, built on the "
            "framework CEO harness. You route, pace, and resolve by precedent — you do "
            "not invent policy, and when there is no precedent you escalate rather than "
            "deciding. Operating principles: be decisive but risk-aware; never soften a "
            "veto by citing precedent — vetoes are absolute and only a human clears "
            "them; kill redundant loops without asking (two bots rejecting the same "
            "artifact back and forth, or three turns with no new artifact); NEVER bypass "
            "a confirm gate or merge anything yourself — the human is the only merge "
            "authority; keep plans short and every job pointed at a concrete artifact; "
            "log every escalation and its resolution."
        ),
        "allowed_modules": [
            "crew", "grill", "match", "tailor", "cover_letters",
            "mock_interview", "soft_skills", "ai_proficiency", "briefs",
        ],
        "risk_tier": "low",
        "escalation_policy": (
            "Mandatory escalation to the human: anything with a veto attached; "
            "anything legal/compliance/privacy; anything that would change the "
            "rules; any novel case with no precedent (or an arguable match); any "
            "case escalated three or more times; anything flagged high risk even "
            "without a clear reason. Never invent policy to cover a gap."
        ),
    },
    "chief_of_staff": {
        "id": "chief_of_staff",
        "name": "Chief of Staff",
        "role": "Advisory funnel — triages escalations into decision-ready items",
        "persona": (
            "You are the Chief of Staff, built on the framework Cos harness, running "
            "advisory-only in this single-team context. You funnel, triage, and present — "
            "you do not invent policy, clear vetoes, or route producing work inside the "
            "team (that remains the Chief's job). Operating principles: every escalation "
            "becomes a decision-ready item — one-line question, labeled answer options "
            "(yes/no or a/b/c/d plus free response), what is blocked, why it reached you, "
            "and a P0/P1 priority; return anything that is not decision-ready; if you "
            "cannot tell P0 from P1, treat it as P0; you propose governance changes for "
            "the human to gate, never apply them yourself."
        ),
        "allowed_modules": ["crew", "briefs", "analytics", "lifecycle"],
        "risk_tier": "low",
        "escalation_policy": (
            "Advisory-only: package for the human, never decide. If two workers "
            "disagree, do not pick a winner — package the disagreement. Quiet "
            "hours never erase a P0; they only defer it to the head of the queue."
        ),
    },
    "pm": {
        "id": "pm",
        "name": "PM",
        "role": "Worker — owns the roadmap and specs; turns goals into acceptance criteria",
        "persona": (
            "You are the crew's product manager, built on the framework PM harness. "
            "You own the problem, not the solution: every goal becomes a brief with a "
            "problem statement, who it affects and how it hurts them, the business goal "
            "it ties to, measurable success criteria, and at least two genuinely "
            "different approaches with tradeoffs. Operating principles: you never "
            "specify UI, choose a tech stack, write user stories, or recommend a single "
            "approach as the only approach; you never fill a required field with a "
            "placeholder — 'TBD' is a stop condition, not an answer; if you cannot tie "
            "the work to a goal or produce two real approaches, stop and escalate to "
            "the Chief; prioritize ruthlessly and escalate scope conflicts."
        ),
        "allowed_modules": [
            "briefs", "match", "analytics", "lifecycle", "skill_gaps",
            "offer_compare", "referrals",
        ],
        "risk_tier": "low",
        "escalation_policy": (
            "Escalate scope conflicts and un-tieable goals to ceo/human. Stop "
            "(never placeholder) when a brief field cannot be filled honestly. "
            "Read-only planning: no direct sends, submits, or publishes."
        ),
    },
    "eng": {
        "id": "eng",
        "name": "Engineer",
        "role": "Worker — implements to spec; tests first-class; small atomic changes",
        "persona": (
            "You are the crew's engineer, built on the framework engineer harness. "
            "You implement the design as specified — you are not the arbiter of what "
            "should be built. Operating principles: never silently simplify a design, "
            "drop an accessibility requirement, or substitute an easier interaction; "
            "if something is expensive to build, you say so and escalate, you do not "
            "decide; work in small atomic changes with tests first-class; never ship "
            "a partial implementation as complete; never mark work done without QA "
            "green; if a spec lacks acceptance criteria, reject it back rather than "
            "guessing."
        ),
        "allowed_modules": [
            "tailor", "cover_letters", "resume_builder", "linkedin_optimizer",
            "radar", "match", "grill", "filters", "mock_interview",
            "soft_skills", "ai_proficiency", "offer_compare", "skill_gaps",
            "briefs", "doctor", "analytics",
        ],
        "risk_tier": "medium",
        "escalation_policy": (
            "Escalate genuine technical blockers and expensive-to-build "
            "requirements to ceo/human. QA red blocks handoff — route it up, "
            "do not negotiate it away."
        ),
    },
    "ux": {
        "id": "ux",
        "name": "Designer",
        "role": "Worker — owns the pixel design system and visual QA",
        "persona": (
            "You are the crew's designer, built on the framework UX harness. You own "
            "the solution to a problem you did not define — you never redefine the "
            "problem to suit a solution. Operating principles: never accept an "
            "invalid brief (missing fields or placeholders go back to the PM); never "
            "choose an approach because it is easier to build; never omit "
            "accessibility because it was not explicitly requested; demos must feel "
            "alive and every surface follows the pixel design system; record which "
            "approach you chose and why, so a cheap choice leaves fingerprints."
        ),
        "allowed_modules": ["site", "radar", "briefs"],
        "risk_tier": "low",
        "escalation_policy": (
            "Reject invalid briefs back to pm. Escalate unauthorized design "
            "decisions to ceo. Accessibility is non-negotiable — never waived "
            "for schedule."
        ),
    },
    "qa": {
        "id": "qa",
        "name": "QA",
        "role": "Worker — runs the full suite; hunts regressions; blocks merge on red",
        "persona": (
            "You are the crew's QA engineer, built on the framework QA harness. You "
            "verify against the story, not the implementation — if the code and the "
            "story disagree, the story wins. Operating principles: never mark "
            "something passed because it 'works differently but acceptably'; never "
            "narrow a test to match what was built; results map one-to-one against "
            "acceptance criteria with a pass/fail per item and no aggregated "
            "verdicts; report up to the Chief, never negotiate privately with eng; "
            "you BLOCK the merge on red, with an adversarial mindset toward happy paths."
        ),
        "allowed_modules": ["tests"],
        "risk_tier": "low",
        "escalation_policy": (
            "Block on red and escalate to ceo/human. If acceptance criteria are "
            "untestable as written, stop and escalate rather than inventing an "
            "interpretation."
        ),
    },
    "researcher": {
        "id": "researcher",
        "name": "Researcher",
        "role": "Worker — gathers external facts with cited sources",
        "persona": (
            "You are the crew's researcher, built on the framework researcher "
            "harness. You establish what is true before anyone plans against it — "
            "you do not decide what should be done about it. Operating principles: "
            "never present a claim without the source it came from; never cite a "
            "source you did not open; never resolve a contradiction by choosing the "
            "convenient side; never fill an evidence gap with a plausible inference; "
            "never recommend a course of action (that is the PM's and UX's work); "
            "state confidence per claim and name what remains unknown — a gap named "
            "is a gap the crew can plan around."
        ),
        "allowed_modules": ["browser.search", "browser.open"],
        "risk_tier": "low",
        "escalation_policy": (
            "If the research question has no stopping condition, reject it to "
            "ceo. If the evidence contradicts the premise, stop and escalate — "
            "never hand pm a brief-shaped answer to a question that should not "
            "be asked. Never extend your own bound."
        ),
    },
    "scout": {
        "id": "scout",
        "name": "Scout",
        "role": "Worker — finds and vets job leads",
        "persona": (
            "You are Scout, the crew's job finder. You search boards, run every lead "
            "through the grill, and rank survivors by fit score. "
            "Operating principles: be skeptical and evidence-driven — a lead is guilty "
            "until proven innocent; kill bad leads fast and say exactly why (vetoed, "
            "weak fit, stale posting); tag every result with its risk tier; prefer "
            "official-tier boards; never scrape beyond the daily budget or touch "
            "logged-in automation without explicit human supervision."
        ),
        "allowed_modules": ["grill", "match", "radar", "filters", "watch", "briefs"],
        "risk_tier": "medium",
        "escalation_policy": (
            "Escalate when a board rate-limits or CAPTCHAs (circuit-break, do not "
            "retry aggressively), when a lead looks like a scam, or when the user "
            "has not acknowledged scraping-tier ToS risk."
        ),
    },
    "tailor_agent": {
        "id": "tailor_agent",
        "name": "Tailor",
        "role": "Worker — tailors resumes and cover letters",
        "persona": (
            "You are Tailor, the crew's resume specialist. You adapt resumes and cover "
            "letters to specific postings using only facts from the candidate's profile. "
            "Operating principles: be meticulous and honesty-obsessed — every claim must "
            "trace to profile evidence; where evidence is missing, report the gap, never "
            "invent it; show the diff and wait for explicit approval before anything is "
            "treated as final; a tailored draft is a draft until the human approves it."
        ),
        "allowed_modules": ["tailor", "cover_letters", "resume_builder", "linkedin_optimizer"],
        "risk_tier": "low",
        "escalation_policy": (
            "Escalate when the profile lacks evidence for a key job requirement "
            "(report the gap, do not fill it), or when asked to produce claims "
            "that cannot be traced to the profile."
        ),
    },
    "coach": {
        "id": "coach",
        "name": "Coach",
        "role": "Worker — interview prep, drills, negotiation, AI proficiency",
        "persona": (
            "You are Coach, the crew's interview trainer. You run mock interviews, "
            "soft-skill drills, negotiation sims, and AI-proficiency lessons. "
            "Operating principles: be tough but encouraging — in drills you are the "
            "adversary (skeptical follow-ups, hardball offers), in review you are the "
            "ally (labeled coach feedback, concrete next rep); attack the argument, "
            "never the person; praise is specific, criticism is actionable; practice "
            "reps are practice, never credentials."
        ),
        "allowed_modules": ["mock_interview", "soft_skills", "ai_proficiency", "briefs"],
        "risk_tier": "low",
        "escalation_policy": (
            "Escalate when a user shows distress or asks for real-world guarantees "
            "('will I get the offer?') — you train, you do not predict outcomes."
        ),
    },
    "red": {
        "id": "red",
        "name": "Red",
        "role": "Adversarial reviewer — red-teams worker output before merge",
        "persona": (
            "You are Red, the crew's adversarial reviewer, built on the framework's "
            "adversarial agents. You red-team worker output before anything merges. "
            "Operating principles: findings only, never edits — you do not fix, you "
            "report; ask: would this get vetoed by the grill? is every claim honest "
            "and traceable? what is the weakest part an interviewer would attack? "
            "Be the skeptic the worker was too close to be; rank findings by severity; "
            "a clean review says so plainly, a dirty one blocks with reasons. You may "
            "delegate deep checks to the specialist adversaries in "
            "AVAILABLE_RED_SPECIALISTS (see red_specialists()), but you remain the "
            "single voice that reports findings — you hold no veto yourself beyond "
            "blocking with reasons; only the human clears."
        ),
        "allowed_modules": ["grill", "tailor", "cover_letters"],
        "risk_tier": "low",
        "escalation_policy": (
            "Escalate (block) when you find invented claims, veto-grade job leads, "
            "or anything that would embarrass the candidate if sent. You cannot "
            "approve — only the human merge authority can."
        ),
    },
}

_REQUIRED_PERSONA_FIELDS = {
    "id", "name", "role", "persona", "allowed_modules",
    "risk_tier", "escalation_policy",
}


def _personas_valid() -> tuple[bool, list[str]]:
    """Check every persona card carries all required fields."""
    problems: list[str] = []
    for pid, card in PERSONAS.items():
        missing = _REQUIRED_PERSONA_FIELDS - set(card)
        if missing:
            problems.append(f"{pid}: missing {sorted(missing)}")
        if card.get("id") != pid:
            problems.append(f"{pid}: id field mismatch")
    return (not problems, problems)


# ---------------------------------------------------------------------------
# Red-team specialist adversaries (from /tmp/agentic-governance/agents/*)
# ---------------------------------------------------------------------------
# The `red` reviewer can delegate deep checks to these specialists. Each
# holds the framework's adversarial contract: findings only, never edits;
# those marked holds_veto carry a hard veto that only a human can clear.

AVAILABLE_RED_SPECIALISTS: list[dict[str, Any]] = [
    {"id": "comp-compliance-adversary",
     "name": "Compliance Adversary",
     "focus": "Reviews work against the risk constitution — scraping tiers, "
              "submit gates, ToS risk. Hard veto.",
     "holds_veto": True},
    {"id": "doc-docs-adversary",
     "name": "Documentation Adversary",
     "focus": "Reviews docs and knowledge content for accuracy, completeness, "
              "usability. Hard veto.",
     "holds_veto": True},
    {"id": "eng-critic",
     "name": "Engineering Critic",
     "focus": "Mechanical gate: correctness, secrets, test coverage, "
              "reversibility of a code change. Never writes code.",
     "holds_veto": False},
    {"id": "eng-ops-advocate",
     "name": "Operations Advocate",
     "focus": "Speaks only for production and the people relying on it. "
              "Hard production-harm veto.",
     "holds_veto": True},
    {"id": "eng-reliability-reviewer",
     "name": "Reliability Reviewer",
     "focus": "Walkthroughs a change as first-deploy, midnight-pager, and "
              "stranger-handoff personas. Reports stalls and failure modes.",
     "holds_veto": False},
    {"id": "ops-ops-adversary",
     "name": "Ops & Reliability Adversary",
     "focus": "Reviews deployments, rollback, and disaster recovery. Hard veto.",
     "holds_veto": True},
    {"id": "priv-privacy-adversary",
     "name": "Data & Privacy Adversary",
     "focus": "Reviews data handling, retention, consent (PII in profiles and "
              "session stores). Hard veto.",
     "holds_veto": True},
    {"id": "prod-product-adversary",
     "name": "Product & Market Adversary",
     "focus": "Reviews career and product decisions for unvalidated assumptions "
              "and weak evidence. Hard veto.",
     "holds_veto": True},
    {"id": "prom-prompt-adversary",
     "name": "Prompt Adversary",
     "focus": "Reviews persona prompts and instructions for injection, drift, "
              "and safety overrides. Hard veto.",
     "holds_veto": True},
    {"id": "qa-critic",
     "name": "QA Critic",
     "focus": "Mechanical gate: acceptance-criteria testability, coverage "
              "honesty, release readiness. Never writes tests.",
     "holds_veto": False},
    {"id": "qa-edge-case-reviewer",
     "name": "Edge-Case Reviewer",
     "focus": "Walkthroughs a test plan as first-timer, hurried, screen-reader, "
              "and distracted personas. Reports missed paths.",
     "holds_veto": False},
    {"id": "qa-quality-advocate",
     "name": "Quality Advocate",
     "focus": "Speaks only for the end user. Hard user-harm veto.",
     "holds_veto": True},
    {"id": "res-context-reviewer",
     "name": "Context Reviewer",
     "focus": "Use-tests research output as decision-maker, skeptic, "
              "implementer, and next-researcher. Reports failures of use.",
     "holds_veto": False},
    {"id": "res-critic",
     "name": "Research Critic",
     "focus": "Mechanical gate: method-claim fit, evidence attribution, source "
              "soundness, synthesis honesty. Never produces research.",
     "holds_veto": False},
    {"id": "res-evidence-advocate",
     "name": "Evidence Advocate",
     "focus": "Holds every claim to what the evidence actually supports. "
              "Hard unsupported-claim veto.",
     "holds_veto": True},
    {"id": "sec-security-adversary",
     "name": "Security Adversary",
     "focus": "Reviews code, config, and dependencies for vulnerabilities and "
              "secret exposure. Hard veto.",
     "holds_veto": True},
    {"id": "univ-universal-adversary",
     "name": "Universal Adversary",
     "focus": "Domain-free pass: unstated assumptions, blind spots, "
              "irrecoverable harm. Hard veto.",
     "holds_veto": True},
    {"id": "ux-critic",
     "name": "UX Critic",
     "focus": "Mechanical gate: design-system token compliance, completeness, "
              "genuine options explored. Never generates UI.",
     "holds_veto": False},
    {"id": "ux-cx-advocate",
     "name": "CX-Quality Advocate",
     "focus": "Speaks only for the person using the product. Hard "
              "customer-harm veto.",
     "holds_veto": True},
    {"id": "ux-evaluative-uxr",
     "name": "Evaluative UXR",
     "focus": "Walkthroughs a flow as first-timer, hurried, screen-reader, and "
              "distracted personas. Reports stalls.",
     "holds_veto": False},
]

_REQUIRED_SPECIALIST_FIELDS = {"id", "name", "focus", "holds_veto"}


def red_specialists() -> list[dict[str, Any]]:
    """List the adversary personas the `red` reviewer can delegate to."""
    return [dict(s) for s in AVAILABLE_RED_SPECIALISTS]


# ---------------------------------------------------------------------------
# Governance bridge (defensive)
# ---------------------------------------------------------------------------


def _governance_available() -> bool:
    try:
        from governance import adapter
        return bool(adapter.governance_enabled())
    except Exception:
        return False


def _run_governed(action: str, work_fn: Any, detail: str = "") -> dict[str, Any] | None:
    """Run through governance.engine.run_governed_job, or None if unavailable."""
    try:
        from governance import engine
        fn = getattr(engine, "run_governed_job", None)
        if fn is None:
            return None
        return fn(action, work_fn, detail=detail)
    except Exception:
        return None


# Deterministic fail-safe screen used when the framework is unavailable
# (and as a pre-check before the governed run). Mirrors the constitution's
# hard lines: no auto-submit, no CAPTCHA bypass, no credential use.
_BLOCKED_PATTERNS = [
    (r"\bsubmit\b.*\bapplication\b", "auto-submit of applications is human-only"),
    (r"\bbypass\b.*captcha", "CAPTCHA bypass is forbidden"),
    (r"\bcaptcha\b.*\bbypass", "CAPTCHA bypass is forbidden"),
    (r"\bpassword\b|\bcredential\b.*\bstore\b", "no credential storage/handling"),
    (r"\blog\s*in\b.*\bautomat", "no unsupervised logged-in automation"),
]


def compliance_screen(task: str) -> dict[str, Any]:
    """Deterministic fail-safe screen. Never raises."""
    flags: list[str] = []
    lowered = (task or "").lower()
    for pattern, reason in _BLOCKED_PATTERNS:
        if re.search(pattern, lowered):
            flags.append(reason)
    return {"allowed": not flags, "flags": flags}


# ---------------------------------------------------------------------------
# Work execution (deterministic, no LLM)
# ---------------------------------------------------------------------------


def _do_work(persona: dict[str, Any], task: str, context: dict[str, Any]) -> dict[str, Any]:
    """Produce the persona's deterministic work product for the task."""
    pid = persona["id"]
    envelope: dict[str, Any] = {
        "persona_id": pid,
        "task": task,
        "context_keys": sorted((context or {}).keys()),
        "system_prompt": persona["persona"],
        "allowed_modules": persona["allowed_modules"],
    }
    if pid == "ceo":
        envelope["plan"] = ceo_plan(task)
        envelope["note"] = (
            "CEO job: decompose + route. Adopt the system_prompt, then execute "
            "each plan job in order via run_agent."
        )
    elif pid == "chief_of_staff":
        envelope["queue_item"] = {
            "question": task,
            "options": ["approve", "reject", "needs more work"],
            "blocked": (context or {}).get("blocked", ""),
            "priority": (context or {}).get("priority", "P1"),
            "note": "Advisory-only: package for the human; do not decide.",
        }
    elif pid == "pm":
        envelope["brief_template"] = {
            "problem_statement": "",
            "who_it_affects": "",
            "business_goal": "",
            "success_criteria": "",
            "approaches": ["", ""],
            "rule": "At least two genuinely different approaches. "
                    "No placeholders — TBD is a stop, not an answer.",
        }
    elif pid == "researcher":
        envelope["evidence_template"] = {
            "question": task,
            "findings": [],
            "contradictions": [],
            "unknowns": [],
            "coverage": "",
            "rule": "Every claim carries its source. "
                    "Name what remains unknown.",
        }
    elif pid == "qa":
        envelope["report_rule"] = (
            "Results map one-to-one against acceptance criteria. "
            "No aggregated verdicts. Red blocks merge."
        )
    elif pid == "red":
        artifact = (context or {}).get("artifact", "")
        envelope["checklist"] = [
            "Would the grill veto this? Which rule and why?",
            "Is every factual claim traceable to the profile or a cited source?",
            "What is the weakest part a skeptical interviewer would attack?",
            "Does anything promise an outcome (offer, callback) we cannot guarantee?",
        ]
        envelope["heuristic_scan"] = red_team_scan(artifact) if artifact else None
        envelope["specialists"] = [s["id"] for s in AVAILABLE_RED_SPECIALISTS]
        envelope["rule"] = (
            "Report findings only. You do not edit the artifact. "
            "Delegate deep checks to specialists via red_specialists()."
        )
    else:
        envelope["job_card"] = {
            "role": persona["role"],
            "escalation_policy": persona["escalation_policy"],
            "first_step": (
                f"Use one of {persona['allowed_modules'][0]} first, then proceed "
                "through the task. Escalate per policy."
            ),
        }
    return envelope


_SUPERLATIVES = [
    "best", "top 1%", "guaranteed", "always", "never failed",
    "world-class", "unparalleled", "revolutionary",
]
_ACHIEVEMENT_VERBS = ["led", "built", "drove", "launched", "founded", "grew", "scaled"]


def red_team_scan(text: str) -> dict[str, Any]:
    """Deterministic heuristic scan of a draft artifact. Advisory only.

    Flags absolute superlatives and achievement claims that lack numbers —
    the two most common honesty smells. Never invents facts about the text.
    """
    lowered = (text or "").lower()
    findings: list[dict[str, str]] = []
    for word in _SUPERLATIVES:
        if word in lowered:
            findings.append({
                "kind": "superlative",
                "detail": f"Absolute claim '{word}' — soften or evidence it.",
            })
    for verb in _ACHIEVEMENT_VERBS:
        for m in re.finditer(rf"\bI\s+{verb}\b", text or "", re.IGNORECASE):
            window = (text or "")[max(0, m.start() - 80): m.end() + 80]
            if not re.search(r"\d", window):
                findings.append({
                    "kind": "unquantified_claim",
                    "detail": f"'{verb}' claim without a nearby number — add a metric or scope.",
                })
                break
    return {
        "findings": findings,
        "verdict": "clean" if not findings else f"{len(findings)} flag(s) to review",
        "note": "Heuristic scan only — a human still reads the artifact.",
    }


def run_agent(
    persona_id: str,
    task: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a persona's task through the governed lifecycle. Never raises.

    spawn: veto-screen (governance adjudication, or the deterministic
    compliance screen when the framework is unavailable). work: the
    persona's deterministic work product. review: framework adversarial
    review (advisory). merge: verdict recorded to the ledger.

    Returns the lifecycle report dict.
    """
    context = context or {}
    try:
        persona = PERSONAS.get(persona_id)
        if persona is None:
            return {
                "ok": False,
                "persona_id": persona_id,
                "error": f"unknown persona: {persona_id!r}",
                "known_personas": sorted(PERSONAS),
            }

        # Fail-safe deterministic screen always applies.
        screen = compliance_screen(task)
        if not screen["allowed"]:
            return {
                "ok": False,
                "persona_id": persona_id,
                "mode": "compliance-only",
                "spawn": {"allowed": False, "flags": screen["flags"]},
                "result": None,
                "review": None,
                "verdict": {"recorded": False},
                "note": "Blocked by the deterministic compliance screen.",
            }

        def work_fn() -> dict[str, Any]:
            return _do_work(persona, task, context)

        if _governance_available():
            report = _run_governed(f"crew:{persona_id}", work_fn, detail=task or "")
            if report is not None:
                report["persona_id"] = persona_id
                report["ok"] = True
                return report

        # Framework unavailable: fail-safe — deterministic gates hold,
        # decision marked compliance-only per the constitution.
        try:
            value = work_fn()
            result: dict[str, Any] = {"ok": True, "value": value}
        except Exception as exc:  # captured, never raised
            result = {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "persona_id": persona_id,
            "mode": "compliance-only",
            "spawn": {"allowed": True, "screen": "deterministic", "flags": []},
            "result": result,
            "review": {"advisory": "framework unavailable — no adversarial review ran"},
            "verdict": {"recorded": False, "note": "compliance-only: ledger write skipped"},
        }
    except Exception as exc:  # never raises, by contract
        return {"ok": False, "persona_id": persona_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# CEO planning (deterministic goal decomposition)
# ---------------------------------------------------------------------------

# (keywords, persona_id, task template, expected artifact)
_ROUTES: list[tuple[tuple[str, ...], str, str, str]] = [
    (
        ("find", "search", "hunt", "lead", "job posting"),
        "scout",
        "Find and vet job leads for: {goal}",
        "Ranked shortlist with fit scores and veto reasons",
    ),
    (
        ("company", "brief", "target companies"),
        "scout",
        "Research the target companies behind: {goal}",
        "Company brief(s)",
    ),
    (
        ("tailor", "resume", "cv", "cover letter"),
        "tailor_agent",
        "Tailor resume/cover letter for: {goal}",
        "Tailored draft + diff, approved by human",
    ),
    (
        ("interview", "prep", "mock", "practice", "star"),
        "coach",
        "Run interview prep for: {goal}",
        "Prep plan + mock session report",
    ),
    (
        ("negotiat", "offer", "salary", "compensation"),
        "coach",
        "Prepare negotiation strategy for: {goal}",
        "Negotiation plan + sim report",
    ),
    (
        ("skill", "learn", "ai", "course", "lesson"),
        "coach",
        "Build a skill plan for: {goal}",
        "Learning plan + exercise results",
    ),
    (
        ("roadmap", "spec", "priorit", "product brief", "acceptance criteria"),
        "pm",
        "Turn the goal into a spec: {goal}",
        "Product brief with problem, success criteria, and two approaches",
    ),
    (
        ("build", "implement", "code", "feature", "fix", "bug"),
        "eng",
        "Implement to spec: {goal}",
        "Atomic change + tests, QA green",
    ),
    (
        ("design", "demo", "visual", "pixel", "accessibility", "a11y", "ui"),
        "ux",
        "Design the experience for: {goal}",
        "Pixel-system-compliant design + rationale",
    ),
    (
        ("test", "regression", "quality"),
        "qa",
        "Verify against the story: {goal}",
        "Test plan + per-criterion results",
    ),
    (
        ("research", "benchmark", "evidence", "sources", "compare options"),
        "researcher",
        "Establish what is true: {goal}",
        "Evidence pack with cited sources and named gaps",
    ),
]

_HUMAN_GATE = {
    "persona_id": "human",
    "task": "Review the red-team findings and approve or reject the crew's output",
    "expected_artifact": "human approval (explicit)",
}


def ceo_plan(goal: str) -> list[dict[str, str]]:
    """Deterministically decompose a goal into an ordered job list.

    Pure function, no LLM. Keyword-routes the goal to worker jobs, then
    ALWAYS appends a ``red`` adversarial review job and a human confirm
    gate. Unknown goals start with a scout reconnaissance job.
    """
    lowered = (goal or "").lower()
    jobs: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(pid: str, task: str, artifact: str) -> None:
        if pid not in seen:
            seen.add(pid)
            jobs.append({
                "persona_id": pid,
                "task": task,
                "expected_artifact": artifact,
            })

    matched = False
    for keywords, pid, task_tpl, artifact in _ROUTES:
        if any(k in lowered for k in keywords):
            matched = True
            add(pid, task_tpl.format(goal=goal), artifact)
    if not matched:
        add(
            "scout",
            f"Recon: clarify the goal and gather facts — '{goal}'",
            "Reconnaissance brief",
        )

    # Adversarial review is ALWAYS the last worker job, then the human gate.
    jobs.append({
        "persona_id": "red",
        "task": f"Red-team the crew's output for: {goal} (veto risk, honesty, weakest part)",
        "expected_artifact": "Red-team findings report",
    })
    jobs.append(dict(_HUMAN_GATE))
    return jobs


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def crew_status() -> dict[str, Any]:
    """Personas with risk tiers, plus governance availability."""
    valid, problems = _personas_valid()
    specialists = red_specialists()
    specialist_problems = [
        s["id"] for s in specialists
        if _REQUIRED_SPECIALIST_FIELDS - set(s)
    ]
    return {
        "personas": [
            {
                "id": card["id"],
                "name": card["name"],
                "role": card["role"],
                "risk_tier": card["risk_tier"],
                "allowed_modules": card["allowed_modules"],
                "escalation_policy": card["escalation_policy"],
            }
            for card in PERSONAS.values()
        ],
        "cards_valid": valid,
        "card_problems": problems,
        "red_specialists": len(specialists),
        "red_specialists_valid": not specialist_problems,
        "red_specialist_problems": specialist_problems,
        "governance_available": _governance_available(),
        "lifecycle": ["spawn", "work", "review", "merge"],
        "note": (
            "Every plan ends with a red-team review job and a human confirm gate. "
            "The crew never submits applications or sends messages. "
            "chief_of_staff is advisory-only in this single-team context."
        ),
    }


# ---------------------------------------------------------------------------
# Plugin wiring
# ---------------------------------------------------------------------------


def register_tools(mcp: Any) -> None:
    """Register crew tools on an MCP server instance."""
    _impl_run = globals()["run_agent"]
    _impl_plan = globals()["ceo_plan"]
    _impl_status = globals()["crew_status"]
    _impl_scan = globals()["red_team_scan"]
    _impl_specialists = globals()["red_specialists"]

    @mcp.tool()
    def crew_run(persona_id: str, task: str, context: dict | None = None) -> dict:
        """Run a crew persona's task through the governed lifecycle
        (spawn veto-screen -> work -> adversarial review -> verdict ledger).

        Args:
            persona_id: one of ceo, chief_of_staff, pm, eng, ux, qa,
                researcher, scout, tailor_agent, coach, red.
            task: the task for the persona.
            context: optional dict, e.g. {"artifact": "..."} for red-team review.

        Returns:
            The lifecycle report dict. Never raises.
        """
        return _impl_run(persona_id, task, context)

    @mcp.tool()
    def crew_plan(goal: str) -> dict:
        """Decompose a goal into an ordered crew job list (deterministic).

        Args:
            goal: the human's goal in plain language.

        Returns:
            Dict with the ordered jobs; always ends with a red review job
            and a human confirm gate.
        """
        return {"goal": goal, "jobs": _impl_plan(goal)}

    @mcp.tool()
    def crew_status_report() -> dict:
        """List crew personas with risk tiers and governance availability."""
        return _impl_status()

    @mcp.tool()
    def crew_red_scan(text: str) -> dict:
        """Heuristic red-team scan of a draft (superlatives, unquantified
        claims). Advisory only — a human still reads the artifact.

        Args:
            text: the draft text to scan.
        """
        return _impl_scan(text)

    @mcp.tool()
    def crew_red_specialists() -> dict:
        """List the specialist adversary personas the red reviewer can
        delegate deep checks to."""
        return {"specialists": _impl_specialists()}

    # -- Governed extension platform (Initiative 11) -----------------------
    # Human gate: confirmation tokens are minted ONLY on human surfaces
    # (terminal `extension confirm`, web UI). These MCP tools can request
    # and run, never mint.

    @mcp.tool()
    def extensions_list() -> dict:
        """List loaded extensions with their declared capabilities."""
        return ext_list()

    @mcp.tool()
    def extensions_verify(ext_dir: str) -> dict:
        """Verify an extension directory (manifest + import scan). Fail closed.

        Args:
            ext_dir: path to the extension directory.
        """
        return ext_verify(ext_dir)

    @mcp.tool()
    def extensions_run(ext_id: str, action_id: str,
                       params: dict | None = None,
                       confirmation_token: str | None = None) -> dict:
        """Run an extension action through the broker.

        Args:
            ext_id: extension id.
            action_id: declared action id.
            params: action params.
            confirmation_token: host-minted token for confirm-required
                actions (minted by a human via `extension confirm`).
        """
        return ext_run(ext_id, action_id, params, confirmation_token)

    @mcp.tool()
    def extensions_request_confirmation(ext_id: str, action_id: str,
                                        params: dict | None = None) -> dict:
        """Create a pending confirmation request for a confirm-required
        action. A human approves it on the terminal or web UI.

        Args:
            ext_id: extension id.
            action_id: declared action id.
            params: action params.
        """
        return ext_request(ext_id, action_id, params)

    @mcp.tool()
    def extensions_pending_confirmations() -> dict:
        """List confirmation requests awaiting human approval."""
        return ext_pending()

    @mcp.tool()
    def extensions_policy_test(ext_dir: str) -> dict:
        """Run the reusable policy test kit against an extension.

        Args:
            ext_dir: path to the extension directory.
        """
        return ext_policy_test(ext_dir)

    @mcp.tool()
    def extensions_scaffold(name: str, description: str = "",
                            author: str = "") -> dict:
        """Generate a new extension project from the template.

        Args:
            name: extension name.
            description: what it does.
            author: author name.
        """
        return ext_scaffold(name, description=description, author=author)


def cmd_crew(args: Any) -> int:
    """CLI handler for `crew`."""
    import json as _json

    action = args.action
    as_json = getattr(args, "json", False)

    def emit(payload: Any) -> int:
        print(_json.dumps(payload, indent=2, default=str))
        return 0

    if action == "personas":
        return emit(crew_status())
    if action == "plan":
        return emit({"goal": args.goal, "jobs": ceo_plan(args.goal)})
    if action == "run":
        context = _json.loads(args.context) if getattr(args, "context", None) else None
        return emit(run_agent(args.persona_id, args.task, context))
    if action == "scan":
        return emit(red_team_scan(args.text))
    if action == "specialists":
        return emit({"specialists": red_specialists()})
    print(f"unknown action: {action}")
    return 2


def register_cli(subparsers: Any) -> dict[str, Any]:
    """Add `crew` to an argparse subparsers.

    Returns a {command: handler} mapping the caller can merge into its
    own dispatch table (cli.py-style).
    """
    p = subparsers.add_parser("crew", help="Governed AI crew.")
    p.add_argument(
        "action",
        choices=["personas", "plan", "run", "scan", "specialists"],
        help="personas|plan|run|scan|specialists",
    )
    p.add_argument("--goal", help="Goal to decompose (plan).")
    p.add_argument("--persona-id", help="Persona id (run).")
    p.add_argument("--task", help="Task text (run).")
    p.add_argument("--context", help="JSON context dict (run).")
    p.add_argument("--text", help="Draft text to red-team scan (scan).")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")
    commands = {"crew": cmd_crew}
    # Governed extension platform (Initiative 11): separate command group so
    # existing core commands are never overwritten.
    commands.update(register_extension_cli(subparsers))
    return commands


# ---------------------------------------------------------------------------
# Extension platform (Initiative 11 — governed extension platform)
# ---------------------------------------------------------------------------
# Terminal + MCP surface for the extension host in initiatives/i11/.
# Every sensitive operation is brokered by initiatives.i11.sandbox.host.Host:
# the extension can never self-confirm, bypass policy, or weaken audit.
#
# Deliberate v1 constraint: confirmation tokens are minted ONLY on human
# surfaces (terminal `extension confirm`, local web UI). The MCP/agent
# surface can request and run, never mint — the human gate stays human.

_EXT_HOST = None


def _ext_host():
    """Lazy singleton extension Host for this process."""
    global _EXT_HOST
    if _EXT_HOST is None:
        from initiatives.i11.sandbox.host import Host
        _EXT_HOST = Host()
    return _EXT_HOST


def ext_list() -> dict:
    """List loaded extensions with their declared capabilities."""
    return {"extensions": _ext_host().list_loaded()}


def ext_verify(ext_dir: str) -> dict:
    """Verify an extension directory (manifest + import scan). Fail closed."""
    return _ext_host().verify_extension(ext_dir)


def ext_install(ext_dir: str) -> dict:
    """Verify, signature-check, revocation-check, load, and pin an extension."""
    from initiatives.i11.signing.sign import (
        InstallRecord, InstallStore, RevocationList, package_hash,
        verify_package,
    )
    from initiatives.i11.manifest.schema import load_manifest
    import time as _time

    host = _ext_host()
    verdict = host.verify_extension(ext_dir)
    if not verdict["ok"]:
        return {"ok": False, "stage": verdict["stage"],
                "error": verdict.get("error")}
    manifest = load_manifest(ext_dir)
    sig = verify_package(ext_dir)
    base = str(Path(__file__).resolve().parent)
    revocations = RevocationList(Path(base) / "extensions_revocations.json")
    if revocations.is_revoked(manifest["id"], manifest["version"]):
        return {"ok": False, "error": "this version was revoked; refusing install"}
    # Reviewed API (initiatives/i11/sandbox/host.py:734): trust is DERIVED
    # inside load_extension from the packaging signature — there is no
    # caller-supplied trust label, and passing one would violate the
    # reviewed security design. unsafe_local_only=True is the explicit,
    # reviewed opt-in for the CLI install path: this terminal command is a
    # human operator's deliberate act, acknowledging the residual risk of
    # in-process execution documented in
    # initiatives/i11/sandbox/SECURITY_RESIDUAL.md. Unsigned packages get
    # the honest ``local-unsigned`` label from _derive_trust — never
    # elevated by the caller. The revocation check above stays in place.
    loaded = host.load_extension(ext_dir, unsafe_local_only=True)
    store = InstallStore(Path(base) / "extensions_installed.json")
    store.record_install(InstallRecord(
        ext_id=manifest["id"], version=manifest["version"],
        package_sha256=sig.get("package_sha256") or package_hash(ext_dir),
        publisher=sig.get("publisher") or "local",
        installed_at=int(_time.time()),
    ))
    return {"ok": True, "extension": manifest["id"],
            "version": manifest["version"],
            "trust": loaded.trust,
            "signature": {"ok": sig["ok"],
                          "error": sig.get("error")},
            "capabilities": verdict["manifest"]}


def ext_run(ext_id: str, action_id: str,
            params: dict | None = None,
            confirmation_token: str | None = None) -> dict:
    """Run an extension action through the broker.

    confirm-required actions need a host-minted token (see `extension
    confirm` on the terminal or the web UI). Without one the run is
    refused — the extension can never self-confirm.
    """
    host = _ext_host()
    if ext_id not in [e["id"] for e in host.list_loaded()]:
        return {"ok": False,
                "error": f"extension {ext_id!r} is not loaded; "
                         f"use `extension install <dir>` first"}
    try:
        return host.run_extension_action(
            ext_id, action_id,
            confirmation_token=confirmation_token, **(params or {}))
    except (PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def ext_request(ext_id: str, action_id: str,
                params: dict | None = None) -> dict:
    """Create a pending confirmation request for a confirm-required action.

    Returns the pending id for the human to review (`extension pending`,
    then `extension confirm <pending-id>` on the terminal).
    """
    host = _ext_host()
    loaded = host._loaded.get(ext_id)  # noqa: SLF001
    if loaded is None:
        return {"ok": False, "error": f"extension {ext_id!r} is not loaded"}
    try:
        pending = host._request_confirmation(  # noqa: SLF001
            ext_id, action_id, {"action": action_id,
                                "params": params or {}})
    except (PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "pending_id": pending.pending_id,
            "action": action_id,
            "note": "Human approval required: run "
                    f"`extension confirm {pending.pending_id}` on the terminal "
                    "or approve in the web UI."}


def ext_pending() -> dict:
    """List pending confirmation requests awaiting human approval."""
    return {"pending": _ext_host().confirmations.pending_requests()}


def ext_confirm(pending_id: str) -> dict:
    """Mint a confirmation token for a pending request. HUMAN SURFACE ONLY.

    Call this on the terminal / web UI after the human explicitly approves
    the pending request. The token is single-use and bound to the exact
    action + params. Never expose this to extension code or autonomous
    agent flows without human approval in the loop.
    """
    try:
        token = _ext_host().confirmations.mint(pending_id)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "confirmation_token": token,
            "note": "Single-use, expires in 15 minutes, bound to the exact "
                    "action and params. Pass to `extension run --token`."}


def ext_policy_test(ext_dir: str) -> dict:
    """Run the reusable policy test kit against an extension directory."""
    from initiatives.i11.policy_kit.checks import run_policy_suite
    return run_policy_suite(ext_dir)


def ext_adversarial() -> dict:
    """Run the adversarial suite: 8 attacks that must ALL be blocked."""
    from initiatives.i11.policy_kit.attacks import run_adversarial_suite
    return run_adversarial_suite()


def ext_audit_verify() -> dict:
    """Verify the hash-chained extension audit log."""
    return _ext_host().audit.verify()


def ext_scaffold(name: str, description: str = "", author: str = "",
                 out_dir: str = "extensions",
                 data_scopes: list | None = None,
                 action_kinds: list | None = None) -> dict:
    """Generate a new extension project from the template."""
    from initiatives.i11.scaffold.template import scaffold_extension
    base = Path(__file__).resolve().parent
    try:
        return scaffold_extension(
            name, description=description, author=author,
            out_dir=base / out_dir,
            data_scopes=data_scopes, action_kinds=action_kinds)
    except (ValueError, FileExistsError) as exc:
        return {"ok": False, "error": str(exc)}


def cmd_extension(args: Any) -> int:
    """CLI handler for `extension`."""
    import json as _json

    action = args.action

    def emit(payload: Any) -> int:
        print(_json.dumps(payload, indent=2, default=str))
        return 0

    def params() -> dict:
        out: dict[str, str] = {}
        for item in getattr(args, "param", None) or []:
            if "=" not in item:
                print(f"ignoring malformed --param {item!r} (want KEY=VALUE)")
                continue
            key, value = item.split("=", 1)
            out[key] = value
        return out

    if action == "list":
        return emit(ext_list())
    if action == "verify":
        return emit(ext_verify(args.dir))
    if action == "install":
        return emit(ext_install(args.dir))
    if action == "run":
        return emit(ext_run(args.ext_id, args.action_id, params(),
                            getattr(args, "token", None)))
    if action == "request":
        return emit(ext_request(args.ext_id, args.action_id, params()))
    if action == "pending":
        return emit(ext_pending())
    if action == "confirm":
        return emit(ext_confirm(args.pending_id))
    if action == "policy-test":
        return emit(ext_policy_test(args.dir))
    if action == "adversarial":
        return emit(ext_adversarial())
    if action == "audit-verify":
        return emit(ext_audit_verify())
    if action == "scaffold":
        scopes = (args.scopes.split(",") if getattr(args, "scopes", None)
                  else None)
        kinds = (args.kinds.split(",") if getattr(args, "kinds", None)
                 else None)
        return emit(ext_scaffold(args.name,
                                 description=getattr(args, "description", "") or "",
                                 author=getattr(args, "author", "") or "",
                                 out_dir=getattr(args, "out_dir", None) or "extensions",
                                 data_scopes=scopes, action_kinds=kinds))
    print(f"unknown action: {action}")
    return 2


def register_extension_cli(subparsers: Any) -> dict[str, Any]:
    """Add `extension` to an argparse subparsers (cli.py-style)."""
    p = subparsers.add_parser(
        "extension", help="Governed extension platform (Initiative 11).")
    p.add_argument(
        "action",
        choices=["list", "verify", "install", "run", "request", "pending",
                 "confirm", "policy-test", "adversarial", "scaffold",
                 "audit-verify"],
        help="list|verify|install|run|request|pending|confirm|policy-test|"
             "adversarial|scaffold|audit-verify",
    )
    p.add_argument("--dir", help="Extension directory (verify/install/policy-test).")
    p.add_argument("--ext-id", help="Extension id (run/request).")
    p.add_argument("--action-id", help="Declared action id (run/request).")
    p.add_argument("--param", action="append", default=[],
                   help="Action param as KEY=VALUE (repeatable).")
    p.add_argument("--token", help="Host-minted confirmation token (run).")
    p.add_argument("--pending-id", help="Pending confirmation id (confirm).")
    p.add_argument("--name", help="Extension name (scaffold).")
    p.add_argument("--description", default="", help="Description (scaffold).")
    p.add_argument("--author", default="", help="Author (scaffold).")
    p.add_argument("--out-dir", default="extensions",
                   help="Output dir for scaffolded project.")
    p.add_argument("--scopes",
                   help="Comma-separated data scopes (scaffold).")
    p.add_argument("--kinds",
                   help="Comma-separated action kinds (scaffold).")
    p.add_argument("--json", action="store_true",
                   help="Machine-readable JSON output.")
    return {"extension": cmd_extension}
