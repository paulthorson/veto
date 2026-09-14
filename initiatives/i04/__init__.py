"""Initiative 04 — Native fit decoder & career graph.

On-device, deterministic (stdlib-only) resume+job analysis: no model, no
API, no network. Everything here is a pure function of text in, JSON out,
so the Python/browser parity harness model used for ``jd_decoder`` extends
to these surfaces without new machinery.

Modules:

* ``schemas`` — frozen schema contracts (``veto/evidence-map/v1``,
  ``veto/fit-result/v1``, ``veto/share-card/v1``) plus validators.
  The requirement→evidence link schema in this package is the contract
  Initiative 05 builds against.
* ``resume_decoder`` — on-device resume parsing (sections, skills,
  seniority, years).
* ``evidence_map`` — requirement → supported evidence / gap /
  grill-question links.
* ``fit_explain`` — the resume+job fit explanation (Epic 1 surface).
* ``role_compare`` — compare up to four jobs across fit, risk,
  compensation, location, readiness.
* ``career_graph`` — read-model over Initiative 01 outcome events:
  trend strengths, gaps, seniority, target-role movement.
* ``share_card`` — privacy-safe share payload: scores + selected
  evidence, never raw resume or job-description text.
* ``file_ingest`` — PDF/DOCX text extraction with an explicit preview
  gate before any analysis runs.
* ``aliases`` — curated skill-alias list (k8s ↔ Kubernetes, ...).

Honesty invariant (inherited from ``jd_decoder``): nothing is inferred
beyond the text in front of the decoder; absence is reported as absence;
every claim carries its verbatim quote.
"""

from __future__ import annotations

SCHEMA_EVIDENCE_MAP = "veto/evidence-map/v1"
SCHEMA_FIT_RESULT = "veto/fit-result/v1"
SCHEMA_SHARE_CARD = "veto/share-card/v1"

__all__ = [
    "SCHEMA_EVIDENCE_MAP",
    "SCHEMA_FIT_RESULT",
    "SCHEMA_SHARE_CARD",
]
