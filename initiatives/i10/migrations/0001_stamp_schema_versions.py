"""Migration 0001 — stamp schema versions.

Ensures every stable dataset file present in the data directory carries
an explicit schema-version marker:

* ``profiles/*.json`` — adds ``"schema_version": "profile-v1"`` when the
  key is absent (legacy wizard-written profiles predate the marker).
* Writes ``data_dir/_schema_versions.json`` with the registry's
  {schema: version} map for the release-channel compatibility gate.

Idempotent. ``backward`` removes the marker file and strips the
``schema_version`` markers this migration added. Which profiles those are
comes from the journal: ``forward`` records every stamped profile name in
its ``applied`` record's ``detail`` (``stamped_profiles``), and
``backward`` reads that record back. Only profiles whose marker is still
exactly ``"profile-v1"`` are unstamped (a profile already carrying the
marker before this migration ran is left alone, as is one whose marker
was changed afterwards). If the journal has no ``applied`` record for
this migration, ``backward`` removes only the marker file and reports
that the stamped set was unknown.
"""

from __future__ import annotations

import json
from pathlib import Path

MIGRATION_ID = "0001_stamp_schema_versions"
FROM_VERSION = {"profile": "(unmarked)", "outcome-event": "outcome-min-v0"}
TO_VERSION = {"profile": "profile-v1", "outcome-event": "outcome-min-v0"}

MARKER_FILE = "_schema_versions.json"


def _stamp_path() -> Path:
    # Absolute import: migration modules are loaded from their file path
    # (numeric filename prefix), so relative imports are unavailable.
    from initiatives.i10 import schemas

    return schemas.schema_versions()


def forward(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    stamped: list[str] = []
    for profile in sorted((data_dir / "profiles").glob("*.json")):
        try:
            doc = json.loads(profile.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(doc, dict) and "schema_version" not in doc:
            doc["schema_version"] = "profile-v1"
            profile.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            stamped.append(profile.name)
    marker = data_dir / MARKER_FILE
    marker.write_text(json.dumps(_stamp_path(), indent=2) + "\n", encoding="utf-8")
    return {"stamped_profiles": stamped, "marker": MARKER_FILE}


def _stamped_by_this_migration(data_dir: Path) -> tuple[list[str], bool]:
    """Return (profile names this migration stamped, known).

    Reads the most recent ``applied`` record for this migration from the
    journal and returns its ``detail["stamped_profiles"]``. ``known`` is
    False when no such record exists (e.g. journal lost) — the caller
    then strips nothing it cannot prove it added.
    """
    from initiatives.i10 import schema_migrate

    for rec in reversed(schema_migrate.journal_records(data_dir)):
        if rec.get("action") == "applied" and rec.get("migration_id") == MIGRATION_ID:
            detail = rec.get("detail") or {}
            return list(detail.get("stamped_profiles", [])), True
    return [], False


def backward(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    stamped, known = _stamped_by_this_migration(data_dir)
    unstamped: list[str] = []
    for name in stamped:
        profile = data_dir / "profiles" / name
        try:
            doc = json.loads(profile.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        # Only strip what forward provably added: the exact value it
        # writes. A pre-existing marker (or one changed since) is kept.
        if isinstance(doc, dict) and doc.get("schema_version") == "profile-v1":
            del doc["schema_version"]
            profile.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            unstamped.append(name)
    removed_marker = False
    marker = data_dir / MARKER_FILE
    if marker.exists():
        marker.unlink()
        removed_marker = True
    return {
        "removed_marker": removed_marker,
        "unstamped_profiles": unstamped,
        "stamped_profiles_known": known,
    }


def verify(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    problems: list[str] = []
    marker = data_dir / MARKER_FILE
    if not marker.exists():
        problems.append("schema-version marker file missing")
    else:
        try:
            doc = json.loads(marker.read_text(encoding="utf-8"))
            if doc.get("profile") != "profile-v1":
                problems.append("marker does not record profile-v1")
        except (json.JSONDecodeError, OSError) as exc:
            problems.append(f"marker unreadable: {exc}")
    for profile in sorted((data_dir / "profiles").glob("*.json")):
        try:
            doc = json.loads(profile.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(doc, dict) and doc.get("schema_version") != "profile-v1":
            problems.append(f"{profile.name}: missing profile-v1 marker")
    return {"ok": not problems, "problems": problems}
