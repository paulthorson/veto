#!/usr/bin/env python3
"""Initiative 12 — Public growth & education (Veto annual roadmap, Q3 2027 scope).

Built early (2026-09-13) per Paul's directive: the machinery is built and
tested now; NOTHING in this package goes public without Paul's explicit order.

Submodules:

* :mod:`initiatives.i12.contracts` — pinned contract mirrors for Initiative 04
  (native fit decoder interface) and Initiative 10 (packaging/install
  interface). The public tools build against these contracts in parallel;
  adapters resolve against the real modules when they land.
* :mod:`initiatives.i12.tools` — interactive public tools: JD decoder demo,
  fit explainer, application-risk check, role-comparison demo.
* :mod:`initiatives.i12.share` — privacy-safe shareable artifacts: score
  cards, interview plans, progress snapshots (all scrubbed; methodology
  links attached).
* :mod:`initiatives.i12.changelog` — transparent changelog: shipped, rejected,
  limited, and why.
* :mod:`initiatives.i12.onboarding` — onboarding experiments across role
  maturity x technical comfort; dark patterns banned by rule, checked in code.
* :mod:`initiatives.i12.telemetry` — consent-based product analytics with the
  Q3 exit-gate tripwire: any content field in a telemetry payload or public
  artifact shuts analytics off, quarantines data, and requires a named
  contracted security/privacy specialist's clearing entry plus independent
  framework-reviewer verification before re-enable. Paul retains veto.

CLI entry: ``python -m initiatives.i12 <subcommand>``.

HARD BOUNDARIES (enforced, not promised):

* No application submissions anywhere in this package. Risk checks are
  proposal-only.
* No CAPTCHA bypass, no fake users/testimonials, no vanity-metric optimization
  (the annual roadmap guardrail — never optimize applications-per-day — applies
  to every analytics query in :mod:`initiatives.i12.telemetry`).
* No public launches, visibility changes, social posts, or marketing pushes.
"""
from __future__ import annotations

__version__ = "0.1.0"
__roadmap_initiative__ = "12"
__status__ = "built-pending-review"  # NOT public; Paul must order any launch.

__all__ = [
    "contracts",
    "tools",
    "share",
    "changelog",
    "onboarding",
    "telemetry",
]
