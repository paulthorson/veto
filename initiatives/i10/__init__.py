"""Initiative 10 — Packaging, backup & secure sync.

One-command installer, versioned schema migrations, encrypted backup &
restore with disaster-recovery drills, opt-in end-to-end encrypted device
sync, progressive-web-app shell, and stable/preview release channels.

Contract-first: sync and backup move ONLY named stable schemas declared
in :mod:`initiatives.i10.schemas`. The outcome-event contract
(``outcome-min-v0``) is published by Initiative 01
(``docs/outcome-event-contract.md``) and frozen for Q4; the profile and
evidence-store contracts are NOT yet published, so this package defines
the schema interface it requires (see ``schemas.py``) and will integrate
with the real contracts when they land.

Safety: local-only is the default. Nothing here sends user data anywhere
unless the user explicitly opts in (sync) or chooses an output path
(backup). Audit logs record counts and hashes only — never user content.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = [
    "installer",
    "schemas",
    "schema_migrate",
    "backup",
    "sync",
    "channels",
]
