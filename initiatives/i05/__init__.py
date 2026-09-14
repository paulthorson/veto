"""Initiative 05 — Application studio.

Versioned resume variants, evidence library, ATS readiness check,
side-by-side change explanations, application packets, and earned
success feedback. Pure local logic — no LLM calls, no network, and
NEVER any application submission (see :mod:`packet`).

Hard rules shared by every module here:

* NEVER invent experience, employers, achievements, metrics, titles,
  companies, dates, or phrasing. Everything traces to the user's real
  profile or to user-approved evidence items.
* Test fixtures are synthetic and clearly labeled as such.
* UI copy for the ATS check must never imply or predict vendor
  ranking — see :mod:`ats_check` ``COPY_CONTRACT``.
"""
