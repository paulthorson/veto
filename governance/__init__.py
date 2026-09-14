"""Governance package: gates job applications through the user's agentic
governance framework (adversarial-agents / agentic-governance).

Public API lives in :mod:`governance.adapter`.
"""

from .adapter import (
    BORDERLINE_THRESHOLD,
    GOVERNANCE_DOMAIN,
    QUALIFIED_THRESHOLD,
    GovernanceError,
    GovernanceUnavailable,
    assess_qualification,
    check_cover_letter_honesty,
    find_governance_root,
    governance_enabled,
    governance_gate,
    load_framework,
    load_saved_profile,
    record_application_verdict,
    review_application,
)

__all__ = [
    "BORDERLINE_THRESHOLD",
    "GOVERNANCE_DOMAIN",
    "QUALIFIED_THRESHOLD",
    "GovernanceError",
    "GovernanceUnavailable",
    "assess_qualification",
    "check_cover_letter_honesty",
    "find_governance_root",
    "governance_enabled",
    "governance_gate",
    "load_framework",
    "load_saved_profile",
    "record_application_verdict",
    "review_application",
]
