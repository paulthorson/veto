"""Initiative 07 — Mentor matching & warm paths.

Subpackage implementing the roadmap's consent-based network value:

* :mod:`initiatives.i07.consent` — two-sided consent handshake. No match
  becomes an introduction until BOTH people explicitly approve; either can
  withdraw at any time. Contact details are sealed until mutual consent.
* :mod:`initiatives.i07.safety` — block, report, rate limits, deletion, and
  the segregation guard that keeps mentorship data out of
  job-application decisions.
* :mod:`initiatives.i07.session_kit` — agenda, consent-gated context
  packet, question builder, notes, follow-up, lightweight feedback.
* :mod:`initiatives.i07.warm_path` — warm-path planner: identifies
  user-provided connections and DRAFTS outreach. It never sends anything.

The package's public contracts live in ``CONTRACTS.md``. Integration
snippets for cli.py / webui.py / dashboard.py / server.py (owned by the
integration sweep — this package does not edit them) live in
``integration_notes.md``.

Stdlib only.
"""

__all__ = ["consent", "safety", "session_kit", "warm_path"]
