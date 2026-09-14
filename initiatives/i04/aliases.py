#!/usr/bin/env python3
"""Curated skill-alias list owned by Initiative 04.

The native decoder is deterministic (no embeddings), so synonym recall
comes from this hand-maintained table: ``k8s`` ↔ ``Kubernetes``,
``js`` ↔ ``JavaScript``, etc. Every alias maps to one canonical skill
name; matching is case-insensitive.

Ownership (spike open question 1, resolved as a decision record here):
Initiative 04 owns this list. Quarterly spot-check cadence: the reviewer
verifies a sample of aliases still resolves correctly on the parity
corpus. Additions are code-reviewed like any other change — never
auto-generated from user data.

Format: ``ALIASES[alias_lower] = canonical_name``.
"""

from __future__ import annotations

ALIASES: dict[str, str] = {
    # Orchestration / infra
    "k8s": "Kubernetes",
    "kube": "Kubernetes",
    "k3s": "Kubernetes",
    # Languages
    "js": "JavaScript",
    "ts": "TypeScript",
    "py": "Python",
    "golang": "Go",
    "rb": "Ruby",
    # Frameworks / runtimes
    "reactjs": "React",
    "react.js": "React",
    "node": "Node.js",
    "nodejs": "Node.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    # Data
    "postgres": "PostgreSQL",
    "pg": "PostgreSQL",
    "mysql": "MySQL",
    "mongo": "MongoDB",
    "mongodb": "MongoDB",
    "es": "Elasticsearch",
    "elasticsearch": "Elasticsearch",
    "ml": "Machine Learning",
    "ai": "Artificial Intelligence",
    "nlp": "Natural Language Processing",
    # Cloud / devops
    "aws": "AWS",
    "gcp": "Google Cloud",
    "az": "Azure",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    "iac": "Infrastructure as Code",
    "terraform": "Terraform",
    # Practices
    "tdd": "Test-Driven Development",
    "bdd": "Behavior-Driven Development",
    "agile": "Agile",
    "scrum": "Scrum",
    "ux": "UX Design",
    "ui": "UI Design",
}

#: Reverse index: canonical name -> set of known aliases (built once).
CANONICAL_TO_ALIASES: dict[str, set[str]] = {}
for _alias, _canonical in ALIASES.items():
    CANONICAL_TO_ALIASES.setdefault(_canonical, set()).add(_alias)


def canonicalize(skill: str) -> str:
    """Return the canonical name for a skill or alias (case-insensitive).

    Unknown inputs are returned unchanged — the decoder never invents
    canonical forms for skills it doesn't know.
    """
    key = skill.strip().lower()
    return ALIASES.get(key, skill.strip())


def known_aliases(canonical: str) -> set[str]:
    """All known aliases for a canonical skill name (may be empty)."""
    return set(CANONICAL_TO_ALIASES.get(canonical, ()))
