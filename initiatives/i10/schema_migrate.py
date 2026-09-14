#!/usr/bin/env python3
"""Versioned schema migrations for Veto local data (Epic 2).

A migration is a small module under ``initiatives/i10/migrations/`` that
declares::

    MIGRATION_ID = "0002_outcome_types_v1"
    FROM_VERSION = {"outcome-event": "outcome-min-v0"}
    TO_VERSION   = {"outcome-event": "outcome-v1"}
    def forward(data_dir: Path) -> dict: ...
    def backward(data_dir: Path) -> dict: ...   # rollback
    def verify(data_dir: Path) -> dict: ...     # post-condition proof

The :class:`MigrationRunner` applies pending migrations in order and
records every action in an append-only journal
(``data_dir/_migrations.jsonl``): ``applied`` records, ``rolled_back``
records, and ``attempt_failed`` records. Rollback never deletes history —
it appends a rollback record and runs the migration's ``backward`` step,
so any migration can be re-run after rollback.

Backups
-------
Before a migration's ``forward`` step runs (real runs only), the runner
copies the data directory — everything except ``_backups`` itself — into
a timestamped snapshot::

    data_dir/_backups/<UTC-timestamp>-<migration_id>/

The snapshot is taken BEFORE ``forward`` mutates anything, so a failed
``verify`` leaves an automated recovery path: copy the snapshot's
contents back over the data directory. The snapshot name is recorded in
the migration's ``applied`` journal record. Retention: the newest
``MAX_SNAPSHOTS`` (10) snapshots are kept; older ones are pruned
automatically after each new snapshot.

Rollback is guarded the same way: ``rollback`` takes a pre-``backward``
snapshot (named ``<UTC-timestamp>-<migration_id>-rollback``) before
mutating anything. A ``backward`` step that raises is journaled as
``rollback_failed`` (with the snapshot name) and raises
``MigrationError`` — the migration stays applied in the journal and the
snapshot is the recovery path. Recovery is always an operator action:
copy the snapshot's contents back over the data directory; there is no
automatic data restore.

Snapshot/journal interplay (disclosure): snapshots capture
``data_dir/_migrations.jsonl`` as well as the data files, so restoring a
snapshot ALSO rolls the journal back to the snapshot moment. Any
``attempt_failed`` or ``rolled_back`` records appended after the
snapshot was taken are silently erased by a restore. The journal is
authoritative only for the current data directory; treat restored
snapshots as historical checkpoints, and copy the snapshot's journal
entry aside first if you need that history preserved.

Migration authors should still write idempotent ``forward`` steps:
``migrate`` is not atomic (a ``verify`` failure after a partial
``forward`` leaves side effects in place). The safe recovery is always
re-running the migration — or restoring its pre-migration snapshot.
``backward`` should likewise tolerate being run when its effects are
partially absent.

Dry runs
--------
``migrate(dry_run=True)`` is the operator's pre-production safety gate
and it is HONEST: pending migrations are applied in order — real
``forward`` + real ``verify`` — against a throwaway copy of the data
directory. The report's ``verify`` entry is ``{"dry_run": True,
"would_pass": <real bool>, "problems": [...]}``: ``would_pass`` carries
the genuine outcome of ``verify`` on the copy, and it is structurally
incapable of being mistaken for a passed check. A failing dry run raises
``MigrationError`` (nothing is written to the real data directory — no
journal, no backup, no file changes).

Journal
-------
The journal is append-only and durable: each record is written to a temp
file (existing content + new line), flushed, fsync'd, then atomically
renamed over the journal, followed by a directory fsync. Failed attempts
are journaled as ``attempt_failed`` records (with ``forward``'s detail
and the failure) BEFORE the ``MigrationError`` is raised, so a failed
migration is distinguishable from a never-attempted one. Corrupt journal
lines are skipped (never fatal); they are surfaced via
``journal_warnings()`` and ``status()["journal_corrupt_lines"]``.

Compatibility tests (``verify``) are first-class: ``migrate`` refuses to
mark a migration applied unless ``verify`` passes, and
``check_all`` re-runs every applied migration's ``verify`` to prove the
data directory is in a coherent state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
JOURNAL_NAME = "_migrations.jsonl"
BACKUP_DIR_NAME = "_backups"
MAX_SNAPSHOTS = 10  # newest snapshots kept; older ones pruned automatically


# ---------------------------------------------------------------------------
# Migration loading
# ---------------------------------------------------------------------------


@dataclass
class Migration:
    migration_id: str
    from_version: dict[str, str]
    to_version: dict[str, str]
    module: Any
    path: Path

    def forward(self, data_dir: Path) -> dict:
        return self.module.forward(data_dir)

    def backward(self, data_dir: Path) -> dict:
        return self.module.backward(data_dir)

    def verify(self, data_dir: Path) -> dict:
        return self.module.verify(data_dir)


def discover_migrations() -> list[Migration]:
    """Load every migration module in dependency order (by filename).

    Modules are loaded from their file path (not by dotted name) so
    migration filenames may start with a numeric ordering prefix such as
    ``0001_...``.

    Duplicate ``MIGRATION_ID`` values are rejected loudly with
    ``MigrationError``: silently applying two migrations under one id
    would corrupt the journal's applied/rolled-back accounting.
    """
    MIGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    migrations: list[Migration] = []
    seen: dict[str, Path] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(
            f"veto_migration_{path.stem}", path
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"Cannot load migration {path.name}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr in ("MIGRATION_ID", "FROM_VERSION", "TO_VERSION", "forward", "backward", "verify"):
            if not hasattr(module, attr):
                raise ValueError(f"Migration {path.name} missing {attr!r}")
        migration_id = module.MIGRATION_ID
        if migration_id in seen:
            raise MigrationError(
                f"Duplicate MIGRATION_ID {migration_id!r}: "
                f"{seen[migration_id].name} and {path.name} declare the same id"
            )
        seen[migration_id] = path
        migrations.append(
            Migration(
                migration_id=migration_id,
                from_version=dict(module.FROM_VERSION),
                to_version=dict(module.TO_VERSION),
                module=module,
                path=path,
            )
        )
    return migrations


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _journal_path(data_dir: Path) -> Path:
    return data_dir / JOURNAL_NAME


def journal_warnings(data_dir: Path) -> list[dict]:
    """Describe corrupt journal lines without raising.

    One torn or hand-edited line must never brick the runner: corrupt
    lines are skipped by :func:`journal_records` and reported here (and
    in ``status()["journal_corrupt_lines"]``) so the operator can repair
    or truncate the journal.
    """
    path = _journal_path(data_dir)
    warnings: list[dict] = []
    if not path.exists():
        return warnings
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except json.JSONDecodeError as exc:
            warnings.append(
                {
                    "line": lineno,
                    "error": f"{type(exc).__name__}: {exc}",
                    "content": stripped[:200],
                }
            )
    return warnings


def journal_records(data_dir: Path) -> list[dict]:
    """Read the journal, skipping corrupt lines (see :func:`journal_warnings`)."""
    path = _journal_path(data_dir)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # corrupt line: skipped, surfaced via journal_warnings()
    return records


def _append_journal(data_dir: Path, record: dict) -> None:
    """Append one journal record atomically and durably.

    Writes existing content plus the new line to a temp file in the same
    directory, flushes, fsyncs the file, atomically renames it over the
    journal, then fsyncs the directory. A crash mid-append can therefore
    never leave a half-written journal — and any torn line that does
    appear (e.g. external edit) is tolerated by :func:`journal_records`.
    """
    data_dir = Path(data_dir)
    path = _journal_path(data_dir)
    record = {"at": _utc_now(), **record}
    line = json.dumps(record, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(data_dir), prefix=JOURNAL_NAME + ".tmp."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if path.exists():
                fh.write(path.read_text(encoding="utf-8"))
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(str(data_dir), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def applied_ids(data_dir: Path) -> list[str]:
    """Migration ids currently applied (applied minus rolled back, in order)."""
    applied: list[str] = []
    for rec in journal_records(data_dir):
        if rec.get("action") == "applied" and rec["migration_id"] not in applied:
            applied.append(rec["migration_id"])
        elif rec.get("action") == "rolled_back" and rec["migration_id"] in applied:
            applied.remove(rec["migration_id"])
    return applied


def failed_attempt_ids(data_dir: Path) -> list[str]:
    """Migration ids with a journaled failed attempt that are not applied."""
    applied = set(applied_ids(data_dir))
    failed: list[str] = []
    for rec in journal_records(data_dir):
        mid = rec.get("migration_id")
        if (
            rec.get("action") == "attempt_failed"
            and mid not in applied
            and mid not in failed
        ):
            failed.append(mid)
    return failed


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class MigrationError(RuntimeError):
    pass


@dataclass
class MigrationRunner:
    data_dir: Path

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def pending(self) -> list[Migration]:
        done = set(applied_ids(self.data_dir))
        return [m for m in discover_migrations() if m.migration_id not in done]

    # -- backups ------------------------------------------------------

    def _snapshot_data_dir(self, migration_id: str) -> Path:
        """Copy the data dir (minus ``_backups``) to a timestamped snapshot.

        Taken BEFORE ``forward`` mutates anything, so a failed ``verify``
        leaves an automated recovery path: copy the snapshot's contents
        back over the data directory. Keeps the newest ``MAX_SNAPSHOTS``
        snapshots and prunes older ones.
        """
        backups = self.data_dir / BACKUP_DIR_NAME
        backups.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = backups / f"{stamp}-{migration_id}"
        suffix = 1
        while dest.exists():
            suffix += 1
            dest = backups / f"{stamp}-{migration_id}-{suffix}"
        shutil.copytree(
            self.data_dir, dest, ignore=shutil.ignore_patterns(BACKUP_DIR_NAME)
        )
        snapshots = sorted(p for p in backups.iterdir() if p.is_dir())
        for old in snapshots[:-MAX_SNAPSHOTS]:
            shutil.rmtree(old)
        return dest

    # -- migrate ------------------------------------------------------

    def migrate(self, *, dry_run: bool = False) -> list[dict]:
        """Apply every pending migration in order.

        Each migration is marked applied only after its ``verify`` passes.
        A failed ``forward`` or ``verify`` is journaled as
        ``attempt_failed`` (with ``forward``'s detail and the failure) and
        then raises ``MigrationError`` — the journal, not silence,
        records the attempt.

        With ``dry_run=True`` nothing is written to the real data
        directory: pending migrations run their real ``forward`` +
        ``verify`` in order against a throwaway copy, and the report's
        ``verify`` entry is ``{"dry_run": True, "would_pass": <bool>,
        "problems": [...]}``. A dry run that would fail raises
        ``MigrationError``.
        """
        if dry_run:
            return self._migrate_dry_run()
        results: list[dict] = []
        for mig in self.pending():
            snapshot = self._snapshot_data_dir(mig.migration_id)
            try:
                detail = mig.forward(self.data_dir)
            except Exception as exc:
                self._journal_failed_attempt(
                    mig, None, f"forward raised {type(exc).__name__}: {exc}"
                )
                raise MigrationError(
                    f"Migration {mig.migration_id} forward failed: {exc}"
                ) from exc
            try:
                verification = mig.verify(self.data_dir)
            except Exception as exc:
                self._journal_failed_attempt(
                    mig, detail, f"verify raised {type(exc).__name__}: {exc}"
                )
                raise MigrationError(
                    f"Migration {mig.migration_id} verify raised "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(verification, dict):
                self._journal_failed_attempt(
                    mig, detail, f"verify returned non-dict: {verification!r}"
                )
                raise MigrationError(
                    f"Migration {mig.migration_id} verify returned "
                    f"non-dict: {verification!r}"
                )
            ok = bool(verification.get("ok", False))
            if not ok:
                self._journal_failed_attempt(
                    mig, detail, f"verify failed: {verification}"
                )
                raise MigrationError(
                    f"Migration {mig.migration_id} failed verify: {verification}"
                )
            _append_journal(
                self.data_dir,
                {
                    "action": "applied",
                    "migration_id": mig.migration_id,
                    "from_version": mig.from_version,
                    "to_version": mig.to_version,
                    "detail": detail,
                    "snapshot": snapshot.name,
                },
            )
            results.append(
                {
                    "migration_id": mig.migration_id,
                    "applied": True,
                    "snapshot": snapshot.name,
                    "verify": verification,
                }
            )
        return results

    def _journal_failed_attempt(
        self, mig: Migration, detail: dict | None, error: str
    ) -> None:
        _append_journal(
            self.data_dir,
            {
                "action": "attempt_failed",
                "migration_id": mig.migration_id,
                "from_version": mig.from_version,
                "to_version": mig.to_version,
                "detail": detail,
                "error": error,
            },
        )

    def _migrate_dry_run(self) -> list[dict]:
        """Honest dry run: real forward+verify on a throwaway copy.

        Writes nothing to the real data directory (no journal, no
        backup, no file changes). Raises ``MigrationError`` if any
        pending migration would fail.
        """
        results: list[dict] = []
        with tempfile.TemporaryDirectory(prefix="veto-migrate-dryrun-") as tmp:
            work = Path(tmp) / "data"
            shutil.copytree(
                self.data_dir,
                work,
                ignore=shutil.ignore_patterns(BACKUP_DIR_NAME, JOURNAL_NAME),
            )
            for mig in self.pending():
                try:
                    mig.forward(work)
                    verification = mig.verify(work)
                except Exception as exc:
                    raise MigrationError(
                        f"Dry run of {mig.migration_id} raised "
                        f"{type(exc).__name__}: {exc} (nothing was written)"
                    ) from exc
                problems = (
                    list(verification.get("problems", []))
                    if isinstance(verification, dict)
                    else [f"verify returned non-dict: {verification!r}"]
                )
                would_pass = (
                    bool(verification.get("ok", False))
                    if isinstance(verification, dict)
                    else False
                )
                results.append(
                    {
                        "migration_id": mig.migration_id,
                        "applied": False,
                        "dry_run": True,
                        "verify": {
                            "dry_run": True,
                            "would_pass": would_pass,
                            "problems": problems,
                            "note": (
                                "Executed forward+verify against a throwaway "
                                "copy of the data directory; nothing was "
                                "written to the real data directory."
                            ),
                        },
                    }
                )
                if not would_pass:
                    raise MigrationError(
                        f"Dry run of {mig.migration_id} FAILED verify "
                        f"(nothing was written): {problems or verification}"
                    )
        return results

    def rollback(self, migration_id: str) -> dict:
        """Roll back one applied migration.

        Takes a pre-``backward`` snapshot (named
        ``<stamp>-<migration_id>-rollback``) BEFORE ``backward`` mutates
        anything, so a ``backward`` that corrupts data and raises leaves
        an automated recovery path. A failed ``backward`` is journaled as
        ``rollback_failed`` (with the snapshot name and the failure) and
        then raises ``MigrationError`` — the migration stays applied in
        the journal. The original ``applied`` record always stays in the
        journal (audit trail); a successful rollback appends a
        ``rolled_back`` record carrying the snapshot name.
        """
        migs = {m.migration_id: m for m in discover_migrations()}
        if migration_id not in migs:
            raise MigrationError(f"Unknown migration {migration_id!r}")
        if migration_id not in applied_ids(self.data_dir):
            raise MigrationError(f"Migration {migration_id!r} is not applied")
        # Refuse to roll back a migration that a later applied migration
        # depends on (later = after it in discovery order).
        order = [m.migration_id for m in discover_migrations()]
        later_applied = [
            mid
            for mid in applied_ids(self.data_dir)
            if order.index(mid) > order.index(migration_id)
        ]
        if later_applied:
            raise MigrationError(
                f"Cannot roll back {migration_id!r}: later migrations still "
                f"applied: {later_applied}"
            )
        snapshot = self._snapshot_data_dir(f"{migration_id}-rollback")
        try:
            detail = migs[migration_id].backward(self.data_dir)
        except Exception as exc:
            _append_journal(
                self.data_dir,
                {
                    "action": "rollback_failed",
                    "migration_id": migration_id,
                    "snapshot": snapshot.name,
                    "error": f"backward raised {type(exc).__name__}: {exc}",
                },
            )
            raise MigrationError(
                f"Rollback of {migration_id!r} failed "
                f"({type(exc).__name__}: {exc}); pre-rollback snapshot "
                f"kept at _backups/{snapshot.name} for recovery"
            ) from exc
        _append_journal(
            self.data_dir,
            {
                "action": "rolled_back",
                "migration_id": migration_id,
                "detail": detail,
                "snapshot": snapshot.name,
            },
        )
        return {
            "migration_id": migration_id,
            "rolled_back": True,
            "detail": detail,
            "snapshot": snapshot.name,
        }

    def check_all(self) -> list[dict]:
        """Re-run every applied migration's ``verify`` — the compatibility
        test suite for the current data directory."""
        migs = {m.migration_id: m for m in discover_migrations()}
        results = []
        for mid in applied_ids(self.data_dir):
            try:
                mig = migs[mid]
            except KeyError:
                raise MigrationError(
                    f"Migration {mid!r} is recorded as applied but its "
                    f"module file is missing from {MIGRATIONS_DIR}"
                ) from None
            verification = mig.verify(self.data_dir)
            results.append(
                {"migration_id": mid, "ok": bool(verification.get("ok")), "verify": verification}
            )
        return results

    def status(self) -> dict:
        records = journal_records(self.data_dir)
        return {
            "data_dir": str(self.data_dir),
            "applied": applied_ids(self.data_dir),
            "pending": [m.migration_id for m in self.pending()],
            "journal_records": len(records),
            "journal_corrupt_lines": len(journal_warnings(self.data_dir)),
            "failed_attempts": failed_attempt_ids(self.data_dir),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="veto-migrate", description=__doc__)
    parser.add_argument("--data-dir", default="", help="Veto data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="Show applied/pending migrations")
    p = sub.add_parser("migrate", help="Apply pending migrations")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("rollback", help="Roll back one migration")
    p.add_argument("migration_id")
    p = sub.add_parser("check", help="Re-run verify for all applied migrations")

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir or _default_data_dir()).expanduser()
    runner = MigrationRunner(data_dir)

    try:
        if args.command == "status":
            print(json.dumps(runner.status(), indent=2))
        elif args.command == "migrate":
            for r in runner.migrate(dry_run=args.dry_run):
                print(json.dumps(r, indent=2))
        elif args.command == "rollback":
            print(json.dumps(runner.rollback(args.migration_id), indent=2))
        elif args.command == "check":
            results = runner.check_all()
            for r in results:
                mark = "✓" if r["ok"] else "✗"
                print(f"  [{mark}] {r['migration_id']}")
            if not all(r["ok"] for r in results):
                return 1
    except MigrationError as exc:
        print(f"veto-migrate: error: {exc}", file=sys.stderr)
        return 1
    return 0


def _default_data_dir() -> str:
    rec = Path.home() / ".local" / "share" / "veto" / "install.json"
    if rec.exists():
        try:
            return json.loads(rec.read_text(encoding="utf-8"))["data_dir"]
        except (KeyError, json.JSONDecodeError, ValueError):
            pass
    return str(Path.cwd())


if __name__ == "__main__":
    sys.exit(main())
