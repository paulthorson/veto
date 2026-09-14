"""Initiative 06 — Interview & skills lab.

Workstream modules:
* ``rubric`` — disclosed interview rubric (5 dimensions, weights,
  deterministic scoring rules, rubric card).
* ``modes`` — 5 interview modes (recruiter, hiring_manager, peer,
  executive, adversarial) with disclosed persona/weights/questions.
* ``longitudinal`` — practice history, observed-gap rule, trends,
  next-drill recommendations.
* ``scenarios`` — 4 soft-skill lab scenarios (conflict, influence,
  negotiation, ambiguous_stakeholder) with disclosed setup and
  behavior signals.
* ``ai_lab`` — role-based AI proficiency lab exercises (ethics,
  evaluation, failure_analysis) per track.
* ``voice`` — optional voice input privacy contract (text default;
  local/connected behind explicit consent; fail-closed).

Wiring into the existing modules lives in the module files
(mock_interview.py, soft_skills.py, skill_gaps.py, ai_proficiency.py)
themselves; ``integration_notes.md`` holds the exact snippets for the
UI shells (cli.py, server.py, webui.py, dashboard.py) which this
initiative may not edit.
"""

from __future__ import annotations

from initiatives.i06 import ai_lab, longitudinal, modes, rubric, scenarios, voice

__all__ = ["ai_lab", "longitudinal", "modes", "rubric", "scenarios",
           "voice"]
