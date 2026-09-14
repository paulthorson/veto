#!/usr/bin/env python3
"""Encrypted backup and restore for Veto (Epic 3).

``create_backup(data_dir, out_path, passphrase)``
    Collects ONLY the named stable schemas (see ``schemas.py``) plus any
    extra ``profiles/*.json`` files, builds a manifest with SHA-256
    checksums and record counts, packs manifest + files into a tar, then
    encrypts the tar with Fernet (AES-128-CBC + HMAC-SHA256, key from
    scrypt). The file header (magic + format marker + created_at +
    schema versions + file/record counts + manifest SHA-256 + salt) is
    plaintext; the tar — the manifest AND all user content, including
    filenames — is ciphertext. Nothing user-identifying is readable
    without the passphrase.

    Files NEVER covered by a backup: the legacy ``applications.json``
    (the primary applications store — not a named stable schema),
    ``backup_audit.jsonl``, and any other file that is not part of a
    named stable schema.

    Symlink policy (deliberate choice): symlinks inside the data dir are
    REJECTED with a loud ``ValueError`` at backup time — resolve them to
    real files first. Rationale: the encrypted tar payload drops
    non-regular members, so a symlinked schema file would land its
    target's checksum in the manifest while never reaching the payload —
    a bundle that fails its own 100%-integrity bar (``verify_backup``
    returns ok=False; ``restore`` refuses) with no warning at backup
    time. Failing fast at backup time is the only honest behavior.

``verify_backup(path, passphrase)``
    Decrypts, checks the manifest SHA-256 against the header value, and
    recomputes every checksum in the manifest. Loud in both directions:
    manifest entries missing from the payload AND payload members
    missing from the manifest are both reported as mismatches.
    Returns an integrity report; ``checksum_match_pct == 100.0`` is the
    release-candidate bar.

``restore(path, passphrase, data_dir, selective=None, *, dry_run=False,
confirm=False)``
    Restores from an encrypted backup. ``selective`` limits the restore
    to the named stable schemas (e.g. ``["outcome-event"]``); unknown
    names raise ``KeyError``. Exact safety semantics:
    * Nothing is written before every blob slated for restore is
      decrypted, matched to a manifest entry, and checksum-verified. A
      manifest entry missing from the payload, a payload member with no
      manifest entry, or a checksum mismatch raises ``ValueError`` and
      nothing is written.
    * No member is ever written outside ``data_dir``: each destination
      is resolved and required to be inside the resolved data dir
      (``Path.is_relative_to``); violations raise ``ValueError``.
    * If ``data_dir`` already contains files and ``confirm`` is false, a
      ``RestoreNeedsConfirmation`` (a ``ValueError``) is raised and
      nothing is written. ``dry_run`` never needs confirmation.
    * Otherwise a timestamped snapshot of the existing data dir is
      copied to the sibling directory
      ``<data_dir>.pre-restore-<UTC-timestamp>/`` (numeric suffix added
      on collision) before the first write, with ``symlinks=True`` so
      the snapshot preserves the pre-restore state exactly (no external
      tree is ever materialized into the snapshot). No snapshot is
      taken for an empty or nonexistent target.
    * Atomic-or-rollback: files are written one by one with a post-write
      read-back checksum; on ANY failure the data dir is restored from
      the snapshot (files that did not exist before are removed), a
      ``restore_rolled_back`` audit entry is appended, and the original
      error is re-raised. If the target did not exist before, it is
      removed instead; if it existed but was empty, only newly written
      files are removed. Rollback itself is best-effort: if it fails,
      the original error still propagates and the snapshot directory
      remains for manual recovery.
    * ``dry_run=True`` runs every check above and returns a report with
      ``planned_files`` set, but writes nothing, takes no snapshot, and
      needs no confirmation.

``drill(data_dir, passphrase)``
    Disaster-recovery drill: backup → snapshot live record ids →
    restore into a scratch directory (live store untouched) → verify
    100% checksum match → confirm zero record gap over every file the
    backup claims to cover (per-record ids for keyed schemas; per-file
    for the unkeyed ``profile`` schema, including extra globbed
    ``profiles/*.json`` files). Returns a drill report.
    COVERAGE IS STABLE SCHEMAS ONLY. The report's ``excluded`` field
    lists every file present in the data dir that the backup does not
    cover — notably the legacy ``applications.json`` (the primary
    applications store) and ``backup_audit.jsonl``. A PASS never implies
    full disaster-recovery coverage.

Passphrase policy:
    Minimum 20 characters, enforced by ``create_backup`` (shorter input
    raises ``ValueError``). The CLI offers ``--generate-passphrase`` on
    ``create`` to make a strong random 32-character passphrase (192
    bits); store it in a password manager — it cannot be recovered, and
    without it the backup is unreadable.
    Key derivation: scrypt n=2**16, r=8, p=1 (~64 MiB memory, under a
    second on modest hardware). The bundle is offline-attackable, so the
    KDF is the only rate limit on passphrase guessing: n=2**16 was
    chosen over n=2**14 (~16 MiB, ~0.1 s — too cheap) and n=2**17 (~5 s
    on modest hardware — too slow for an interactive tool and its test
    suite). The >=20-character floor carries the remaining
    offline-attack resistance.

Passphrase handling (Rule 1 — i10-wide convention):
    The backup passphrase is NEVER accepted on argv. A passphrase on the
    command line would be visible in the child process's argv via ``ps``
    and process accounting, so the CLI refuses it outright. It is
    resolved, in order, from:

    1. ``--passphrase-stdin`` — first line of stdin (scripted pipelines);
    2. ``--passphrase-env NAME`` — the named environment variable
       (default: ``VETO_BACKUP_PASSPHRASE``);
    3. an interactive ``getpass`` prompt, when stdin is a TTY.

    Migration note: the old ``--passphrase`` argv flag was removed in
    Initiative 10 because it exposed the secret in the child argv. Use
    ``VETO_BACKUP_PASSPHRASE=... python3 -m initiatives.i10.backup ...``,
    ``--passphrase-env NAME``, or ``--passphrase-stdin`` instead.
    ``scripts/veto-backup.sh`` performs the env-based handoff for you.

Audit: every action appends one line to ``backup_audit.jsonl`` inside
the data dir with timestamp, action, schema versions, file/record
counts, and the manifest checksum — never user content. The log is
rotated at 1 MiB (truncated to the newest 5000 lines before appending)
and is deliberately EXCLUDED from backups. After a disaster the
surviving audit trail is whatever copy the operator kept off-box; the
bundle header's plaintext ``manifest_sha256`` lets a recovered bundle
be correlated with that trail. Audit writes are best-effort: a failure
to write the audit log never fails the backup/restore itself.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import os
import secrets
import shutil
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schemas

MAGIC = b"VETOBKP1"
BACKUP_FORMAT = 2
# Reserved member name for the manifest inside the encrypted tar. Data
# members can never collide with it: registry files have fixed names and
# the profile glob only matches profiles/*.json.
MANIFEST_NAME = "__veto_manifest__.json"
AUDIT_NAME = "backup_audit.jsonl"
PASSPHRASE_ENV_DEFAULT = "VETO_BACKUP_PASSPHRASE"
MIN_PASSPHRASE_LEN = 20
# scrypt work factor: ~64 MiB / <1 s on modest hardware. Rationale for
# n=2**16 over 2**14 / 2**17 is documented in the module docstring.
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 1
_AUDIT_MAX_BYTES = 1_048_576  # 1 MiB
_AUDIT_KEEP_LINES = 5000


def _crypto():
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for encrypted backup. "
            "Install it with: pip install cryptography"
        ) from exc
    return Fernet, Scrypt, hashes


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    Fernet, Scrypt, hashes = _crypto()
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def generate_passphrase(nbytes: int = 24) -> str:
    """Generate a strong random passphrase.

    ``secrets.token_urlsafe(24)`` → 32 characters, 192 bits of entropy,
    comfortably above the 20-character minimum. The caller must store
    it: it cannot be recovered, and without it the backup is unreadable.
    """
    return secrets.token_urlsafe(nbytes)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _schema_files(data_dir: Path, schema: schemas.StableSchema) -> list[Path]:
    """Existing files for one stable schema, including extra globbed
    profile files (named profiles under profiles/)."""
    files = [data_dir / rel for rel in schema.files if (data_dir / rel).exists()]
    if schema.name == "profile":
        extra = sorted((data_dir / "profiles").glob("*.json"))
        files = sorted(set(files) | set(extra))
    return sorted(files)


def _collect_datasets(data_dir: Path) -> dict[str, list[Path]]:
    """{schema_name: [existing files]} for every stable schema."""
    data_dir = Path(data_dir)
    collected: dict[str, list[Path]] = {}
    for name, schema in schemas.SCHEMA_REGISTRY.items():
        files = _schema_files(data_dir, schema)
        if files:
            collected[name] = files
    return collected


def _record_count(path: Path) -> int:
    """Count records: JSONL = non-blank lines; JSON = 1."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    if path.suffix == ".jsonl":
        return sum(1 for line in text.splitlines() if line.strip())
    return 1


def _audit(data_dir: Path, action: str, detail: dict) -> None:
    """Best-effort audit append (never fails the operation itself)."""
    audit_path = Path(data_dir) / AUDIT_NAME
    try:
        if audit_path.exists() and audit_path.stat().st_size > _AUDIT_MAX_BYTES:
            lines = audit_path.read_text(encoding="utf-8").splitlines()
            audit_path.write_text(
                "\n".join(lines[-_AUDIT_KEEP_LINES:]) + "\n", encoding="utf-8"
            )
    except OSError:
        pass
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, **detail}
    try:
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


@dataclass
class BackupReport:
    path: str
    schema_versions: dict[str, str]
    files: int
    records: int
    manifest_sha256: str
    created_at: str


def create_backup(
    data_dir: str | Path, out_path: str | Path, passphrase: str
) -> BackupReport:
    """Create an encrypted backup. Passphrase must be >= 20 chars."""
    if not passphrase or len(passphrase) < MIN_PASSPHRASE_LEN:
        raise ValueError(
            f"Passphrase must be at least {MIN_PASSPHRASE_LEN} characters "
            "(use `create --generate-passphrase` to make a strong random one)."
        )
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data dir not found: {data_dir}")
    out_path = Path(out_path)

    datasets = _collect_datasets(data_dir)
    # Symlink policy: symlinks inside the data dir are REJECTED loudly at
    # backup time (documented choice — see the module docstring). The tar
    # payload drops non-regular members, so a symlinked schema file would
    # get its target's checksum into the manifest while the member itself
    # never reaches the payload: the bundle would fail its own
    # 100%-integrity bar (verify_backup ok=False, restore refuses) with
    # no warning at backup time. Dereference or remove the links first.
    symlinked = sorted(
        str(p.relative_to(data_dir))
        for files in datasets.values()
        for p in files
        if p.is_symlink()
    )
    if symlinked:
        raise ValueError(
            "create_backup refuses symlinks inside the data dir: resolve "
            "them to real files first. Symlinks found: "
            + ", ".join(symlinked)
        )
    manifest: dict[str, Any] = {"schema_versions": schemas.schema_versions(), "files": {}}
    total_records = 0
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for schema_name, files in datasets.items():
            for path in files:
                rel = str(path.relative_to(data_dir))
                checksum = _sha256_file(path)
                records = _record_count(path)
                manifest["files"][rel] = {
                    "schema": schema_name,
                    "sha256": checksum,
                    "records": records,
                    "bytes": path.stat().st_size,
                }
                total_records += records
                tar.add(str(path), arcname=rel)
        # The manifest travels INSIDE the encrypted tar: no filenames or
        # checksums are readable without the passphrase.
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        ti = tarfile.TarInfo(MANIFEST_NAME)
        ti.size = len(manifest_bytes)
        ti.mtime = int(time.time())
        tar.addfile(ti, io.BytesIO(manifest_bytes))
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    Fernet, _, _ = _crypto()
    salt = os.urandom(16)
    token = Fernet(_derive_key(passphrase, salt)).encrypt(tar_buf.getvalue())

    created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    header = {
        "magic": "VETOBKP1",
        "backup_format": BACKUP_FORMAT,
        "created_at": created_at,
        "schema_versions": manifest["schema_versions"],
        "files": len(manifest["files"]),
        "records": total_records,
        "manifest_sha256": manifest_sha,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
    }
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        fh.write(MAGIC)
        fh.write(len(header_bytes).to_bytes(4, "big"))
        fh.write(header_bytes)
        fh.write(token)

    report = BackupReport(
        path=str(out_path),
        schema_versions=manifest["schema_versions"],
        files=len(manifest["files"]),
        records=total_records,
        manifest_sha256=manifest_sha,
        created_at=created_at,
    )
    _audit(
        data_dir,
        "backup_created",
        {
            "files": report.files,
            "records": report.records,
            "manifest_sha256": manifest_sha,
            "schema_versions": report.schema_versions,
        },
    )
    return report


def _read_bundle(path: Path) -> tuple[dict, bytes]:
    """Read the plaintext header and the encrypted payload.

    Bundles written by the old format (plaintext manifest section) are
    rejected loudly: they must be re-created with the current version.
    """
    raw = path.read_bytes()
    if raw[:8] != MAGIC:
        raise ValueError(f"{path} is not a Veto backup file (bad magic).")
    off = 8
    hlen = int.from_bytes(raw[off : off + 4], "big")
    off += 4
    header = json.loads(raw[off : off + hlen])
    off += hlen
    if header.get("backup_format", 1) != BACKUP_FORMAT:
        raise ValueError(
            f"{path} was created by an older Veto backup format (its manifest "
            "was stored in plaintext); re-create the backup with the current "
            "version."
        )
    return header, raw[off:]


def _validate_manifest(manifest: object) -> dict:
    """Validate the decrypted manifest's entry shape.

    ``create_backup`` writes ``{"schema_versions": ..., "files": {rel: {
    "schema", "sha256", "records", "bytes"}}}``. Hand-crafted or corrupted
    bundles may not; ``verify_backup`` and ``restore`` index
    ``meta["schema"]``/``meta["sha256"]`` directly, which would leak a
    ``KeyError`` on a malformed entry. Validate once here (inside
    ``_decrypt_payload``) so the malformed case raises the ``ValueError``
    the ``_decrypt_payload`` docstring promises, on every code path.
    """
    if not isinstance(manifest, dict):
        raise ValueError("Backup manifest is corrupt (not a JSON object).")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Backup manifest is corrupt ('files' is not an object).")
    required = ("schema", "sha256", "records")
    for rel, meta in files.items():
        if not isinstance(meta, dict) or any(k not in meta for k in required):
            raise ValueError(
                f"Backup manifest is corrupt (entry {rel!r} must be an object "
                f"with keys {sorted(required)})."
            )
    return manifest


def _decrypt_payload(
    path: Path, passphrase: str
) -> tuple[dict, dict, dict[str, bytes]]:
    """Decrypt the bundle and return ``(header, manifest, blobs)``.

    Verifies the manifest SHA-256 against the header value. Raises
    ``ValueError`` on wrong passphrase, corrupt header/payload, missing
    manifest, manifest SHA-256 mismatch, or malformed manifest entries.
    """
    header, token = _read_bundle(path)
    Fernet, _, _ = _crypto()
    try:
        salt = base64.b64decode(header["salt_b64"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Backup header is corrupt (bad salt).") from exc
    try:
        payload = Fernet(_derive_key(passphrase, salt)).decrypt(token)
    except Exception as exc:
        raise ValueError("Decryption failed: wrong passphrase or corrupt file.") from exc

    blobs: dict[str, bytes] = {}
    manifest: dict | None = None
    manifest_bytes: bytes | None = None
    try:
        tar_buf = io.BytesIO(payload)
        with tarfile.open(fileobj=tar_buf, mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                fh = tar.extractfile(member)
                if fh is None:
                    continue
                data = fh.read()
                if member.name == MANIFEST_NAME:
                    manifest_bytes = data
                    manifest = json.loads(data.decode("utf-8"))
                else:
                    blobs[member.name] = data
    except tarfile.TarError as exc:
        raise ValueError("Backup payload is not a valid tar archive.") from exc
    if manifest is None or manifest_bytes is None:
        raise ValueError("Backup payload is corrupt: manifest missing from encrypted tar.")
    manifest = _validate_manifest(manifest)
    if hashlib.sha256(manifest_bytes).hexdigest() != header.get("manifest_sha256"):
        raise ValueError("Backup manifest failed its SHA-256 check against the header.")
    return header, manifest, blobs


@dataclass
class IntegrityReport:
    ok: bool
    files_checked: int
    files_ok: int
    checksum_match_pct: float
    mismatches: list[str] = field(default_factory=list)


def verify_backup(path: str | Path, passphrase: str) -> IntegrityReport:
    """Decrypt the bundle, check the manifest SHA-256 against the header,
    and recompute every checksum in the manifest.

    Loud in both directions: a manifest entry missing from the payload
    and a payload member missing from the manifest are both reported as
    mismatches (never silently skipped).
    """
    path = Path(path)
    header, manifest, blobs = _decrypt_payload(path, passphrase)

    mismatches: list[str] = []
    ok_count = 0
    for rel, meta in manifest["files"].items():
        blob = blobs.get(rel)
        if blob is None:
            mismatches.append(f"{rel}: in manifest but missing from bundle payload")
            continue
        if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
            mismatches.append(f"{rel}: checksum mismatch")
        else:
            ok_count += 1
    for name in blobs:
        if name not in manifest["files"]:
            mismatches.append(f"{name}: in bundle payload but not in manifest")

    total = len(manifest["files"])
    pct = (ok_count / total * 100.0) if total else 100.0
    return IntegrityReport(
        ok=not mismatches,
        files_checked=total,
        files_ok=ok_count,
        checksum_match_pct=pct,
        mismatches=mismatches,
    )


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class RestoreNeedsConfirmation(ValueError):
    """The restore target already contains files and ``confirm`` was false.

    Nothing was written. Re-run with ``confirm=True`` (CLI: ``--yes`` or
    answer the TTY prompt) after deciding the live data may be
    overwritten — a timestamped pre-restore snapshot is taken first.
    """


def _dir_has_files(d: Path) -> bool:
    return d.is_dir() and any(p.is_file() for p in d.rglob("*"))


def _write_restored_file(dest: Path, blob: bytes) -> None:
    """Single write step of restore (separate function so tests can inject
    a mid-restore failure)."""
    dest.write_bytes(blob)


def _snapshot_data_dir(data_dir: Path) -> Path:
    """Copy the existing data dir aside to a timestamped sibling."""
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    snap = data_dir.parent / f"{data_dir.name}.pre-restore-{ts}"
    i = 0
    while snap.exists():
        i += 1
        snap = data_dir.parent / f"{data_dir.name}.pre-restore-{ts}-{i}"
    # symlinks=True: a snapshot must preserve the pre-restore state
    # EXACTLY. With the default (False), copytree would dereference
    # symlinks — materializing an arbitrarily large external tree into
    # the snapshot during disaster recovery (disk exhaustion; a symlink
    # cycle recurses until crash) and silently replacing a live symlink
    # with a real directory.
    shutil.copytree(data_dir, snap, symlinks=True)
    return snap


def _rollback_restore(
    data_dir: Path,
    snapshot: Path | None,
    existed: bool,
    written_new: list[Path],
) -> None:
    """Best-effort rollback to the pre-restore state.

    If rollback itself fails, the error is swallowed here: the caller
    re-raises the original failure and the snapshot directory remains
    for manual recovery.
    """
    try:
        if snapshot is not None:
            if data_dir.exists() or data_dir.is_symlink():
                shutil.rmtree(data_dir)
            # symlinks=True, same as the snapshot: rollback restores the
            # pre-restore state exactly, symlink-for-symlink, never a
            # materialized directory where a symlink used to be.
            shutil.copytree(snapshot, data_dir, symlinks=True)
        elif not existed:
            if data_dir.exists() or data_dir.is_symlink():
                shutil.rmtree(data_dir)
        else:  # existed but was empty: only remove what we created
            for p in written_new:
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


@dataclass
class RestoreReport:
    data_dir: str
    files_restored: int
    records_restored: int
    selective: list[str]
    verify: IntegrityReport
    dry_run: bool = False
    snapshot: str | None = None
    planned_files: int = 0


def restore(
    path: str | Path,
    passphrase: str,
    data_dir: str | Path,
    selective: list[str] | None = None,
    *,
    dry_run: bool = False,
    confirm: bool = False,
) -> RestoreReport:
    """Restore from an encrypted backup.

    ``selective`` limits the restore to the named stable schemas
    (e.g. ``["outcome-event"]``); unknown names raise ``KeyError``.

    Exact safety semantics:
    * Nothing is written before every blob slated for restore is
      decrypted, matched to a manifest entry, and checksum-verified. A
      manifest entry missing from the payload, a payload member with no
      manifest entry, or a checksum mismatch raises ``ValueError`` and
      nothing is written.
    * No member is ever written outside ``data_dir``: each destination
      is resolved and required to be inside the resolved data dir
      (``Path.is_relative_to``); violations raise ``ValueError`` and
      nothing is written.
    * If ``data_dir`` already contains files and ``confirm`` is false, a
      ``RestoreNeedsConfirmation`` (a ``ValueError``) is raised and
      nothing is written. ``dry_run`` never needs confirmation.
    * Otherwise a timestamped snapshot of the existing data dir is
      copied to the sibling directory
      ``<data_dir>.pre-restore-<UTC-timestamp>/`` (numeric suffix added
      on collision) before the first write, with ``symlinks=True`` so
      the snapshot preserves the pre-restore state exactly (no external
      tree is ever materialized into the snapshot). No snapshot is
      taken for an empty or nonexistent target.
    * Atomic-or-rollback: files are written one by one with a post-write
      read-back checksum; on ANY failure the data dir is restored from
      the snapshot (files that did not exist before are removed), a
      ``restore_rolled_back`` audit entry is appended, and the original
      error is re-raised. If the target did not exist before, it is
      removed instead; if it existed but was empty, only newly written
      files are removed.
    * ``dry_run=True`` runs every check above and returns a report with
      ``planned_files`` set, but writes nothing, takes no snapshot, and
      needs no confirmation.
    """
    path = Path(path)
    data_dir = Path(data_dir)
    if selective:
        for name in selective:
            schemas.get_schema(name)  # raises KeyError on unknown names

    header, manifest, blobs = _decrypt_payload(path, passphrase)

    wanted = set(selective) if selective else None
    # Loud pre-write integrity checks: nothing is written until every
    # blob slated for restore is verified against the manifest.
    planned: list[tuple[Path, bytes, dict]] = []
    data_dir_resolved = data_dir.resolve()
    for rel, meta in manifest["files"].items():
        if wanted is not None and meta["schema"] not in wanted:
            continue
        blob = blobs.get(rel)
        if blob is None:
            raise ValueError(
                f"Refusing restore: {rel} is in the manifest but missing "
                "from the bundle payload."
            )
        if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
            raise ValueError(f"Refusing restore: checksum mismatch for {rel}.")
        dest = data_dir / rel
        # Path-traversal guard: resolved destination must stay inside the
        # resolved data dir (is_relative_to, not a startswith prefix
        # check, so sibling dirs like "<data>_evil" cannot slip through).
        if not dest.resolve().is_relative_to(data_dir_resolved):
            raise ValueError(f"Refusing to restore outside data dir: {rel}")
        planned.append((dest, blob, meta))
    for name in blobs:
        if name not in manifest["files"]:
            raise ValueError(
                f"Refusing restore: bundle contains {name} with no manifest entry."
            )

    if not dry_run and not confirm and _dir_has_files(data_dir):
        raise RestoreNeedsConfirmation(
            f"Refusing to restore into non-empty data dir {data_dir} without "
            "explicit confirmation (pass confirm=True). Nothing was written."
        )

    if dry_run:
        integrity = verify_backup(path, passphrase)
        records = sum(meta["records"] for _, _, meta in planned)
        if data_dir.is_dir():
            _audit(
                data_dir,
                "restore_dry_run",
                {"planned_files": len(planned), "planned_records": records},
            )
        return RestoreReport(
            data_dir=str(data_dir),
            files_restored=0,
            records_restored=0,
            selective=sorted(wanted) if wanted else [],
            verify=integrity,
            dry_run=True,
            snapshot=None,
            planned_files=len(planned),
        )

    existed = data_dir.exists()
    snapshot = _snapshot_data_dir(data_dir) if _dir_has_files(data_dir) else None
    data_dir.mkdir(parents=True, exist_ok=True)

    restored = 0
    records = 0
    written_new: list[Path] = []
    try:
        for dest, blob, meta in planned:
            is_new = not dest.exists()
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write_restored_file(dest, blob)
            if is_new:
                written_new.append(dest)
            if _sha256_file(dest) != meta["sha256"]:
                raise ValueError(
                    f"Post-write checksum mismatch for {dest.name}; rolling back."
                )
            restored += 1
            records += meta["records"]
    except Exception as exc:
        _rollback_restore(data_dir, snapshot, existed, written_new)
        if data_dir.is_dir():
            _audit(
                data_dir,
                "restore_rolled_back",
                {
                    "error": str(exc),
                    "snapshot": str(snapshot) if snapshot else None,
                },
            )
        raise

    integrity = verify_backup(path, passphrase)
    _audit(
        data_dir,
        "restore",
        {
            "files": restored,
            "records": records,
            "selective": sorted(wanted) if wanted else [],
            "checksum_match_pct": integrity.checksum_match_pct,
            "snapshot": str(snapshot) if snapshot else None,
        },
    )
    return RestoreReport(
        data_dir=str(data_dir),
        files_restored=restored,
        records_restored=records,
        selective=sorted(wanted) if wanted else [],
        verify=integrity,
        dry_run=False,
        snapshot=str(snapshot) if snapshot else None,
        planned_files=restored,
    )


# ---------------------------------------------------------------------------
# Disaster-recovery drill
# ---------------------------------------------------------------------------


def _record_ids(data_dir: Path) -> dict[str, set[str]]:
    """{schema_name: set of record ids} for gap analysis.

    Covers exactly the file set ``_collect_datasets`` covers, including
    extra globbed ``profiles/*.json`` files. Schemas with a record_key
    are keyed per record; schemas without one (``profile``: one JSON
    document per file) are keyed per file by relative path, so a lost
    profile file still shows up as a gap.
    """
    data_dir = Path(data_dir)
    ids: dict[str, set[str]] = {}
    for name, schema in schemas.SCHEMA_REGISTRY.items():
        found: set[str] = set()
        for path in _schema_files(data_dir, schema):
            rel = str(path.relative_to(data_dir))
            if not schema.record_key:
                found.add(f"file:{rel}")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    key = rec.get(schema.record_key)
                    if key:
                        found.add(str(key))
        ids[name] = found
    return ids


def _excluded_from_backup(data_dir: Path) -> list[str]:
    """Relative paths of files present in the data dir that no backup
    covers (not part of any named stable schema)."""
    covered = {
        str(p.relative_to(data_dir))
        for files in _collect_datasets(data_dir).values()
        for p in files
    }
    return sorted(
        str(p.relative_to(data_dir))
        for p in data_dir.rglob("*")
        if p.is_file() and str(p.relative_to(data_dir)) not in covered
    )


@dataclass
class DrillReport:
    ok: bool
    checksum_match_pct: float
    record_gap: dict[str, int]
    detail: str
    excluded: list[str] = field(default_factory=list)


def drill(data_dir: str | Path, passphrase: str) -> DrillReport:
    """Full disaster-recovery drill with zero risk to live data:

    1. Snapshot live record ids per stable schema (and the list of files
       the backup does NOT cover).
    2. Create an encrypted backup of the live data dir.
    3. Restore into a scratch directory (live store untouched).
    4. Verify 100% checksum match on the backup.
    5. Compare restored record ids against the snapshot — any missing id
       is an unrecoverable record gap, over every file the backup claims
       to cover.

    COVERAGE IS STABLE SCHEMAS ONLY. Explicitly excluded from the
    backup (and therefore from any PASS): the legacy
    ``applications.json`` (the primary applications store),
    ``backup_audit.jsonl``, and any other file that is not part of a
    named stable schema. The returned report's ``excluded`` field lists
    the excluded files found; a PASS never implies full
    disaster-recovery coverage.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data dir not found: {data_dir}")
    before = _record_ids(data_dir)
    excluded = _excluded_from_backup(data_dir)
    with tempfile.TemporaryDirectory(prefix="veto-drill-") as tmp:
        backup_path = Path(tmp) / "drill.vetobackup"
        create_backup(data_dir, backup_path, passphrase)
        integrity = verify_backup(backup_path, passphrase)
        if not integrity.ok:
            return DrillReport(
                False,
                integrity.checksum_match_pct,
                {},
                f"FAIL: backup integrity failed: {integrity.mismatches}",
                excluded,
            )
        scratch = Path(tmp) / "restored"
        restore(backup_path, passphrase, scratch)
        after = _record_ids(scratch)

    gap: dict[str, int] = {}
    for name, ids in before.items():
        missing = ids - after.get(name, set())
        gap[name] = len(missing)
    ok = integrity.checksum_match_pct == 100.0 and all(v == 0 for v in gap.values())
    _audit(
        data_dir,
        "drill",
        {
            "ok": ok,
            "checksum_match_pct": integrity.checksum_match_pct,
            "record_gap": gap,
            "excluded": excluded,
        },
    )
    if ok:
        detail = (
            "PASS (stable schemas only): 100% checksum match, zero record gap "
            "over everything the backup covers. "
            f"Excluded from backup (NOT covered by this PASS): {excluded or ['(none)']}."
        )
    else:
        detail = (
            f"FAIL: checksum {integrity.checksum_match_pct}%, gap {gap}, "
            f"excluded {excluded}"
        )
    return DrillReport(ok, integrity.checksum_match_pct, gap, detail, excluded)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_passphrase_args(p: argparse.ArgumentParser) -> None:
    """Attach the passphrase-ingestion options.

    There is deliberately NO ``--passphrase`` argv flag: a passphrase on
    the command line is visible via ``ps`` / process accounting. See the
    module docstring (Rule 1 — i10-wide convention).
    """
    p.add_argument(
        "--passphrase-env",
        default=PASSPHRASE_ENV_DEFAULT,
        metavar="NAME",
        help=(
            "Name of the environment variable holding the backup passphrase "
            f"(default: {PASSPHRASE_ENV_DEFAULT}). The passphrase is never "
            "accepted on the command line; it must be at least 20 characters "
            "(or use `create --generate-passphrase`)."
        ),
    )
    p.add_argument(
        "--passphrase-stdin",
        action="store_true",
        help="Read the passphrase from stdin (first line) instead of the environment.",
    )


def _resolve_passphrase(args: argparse.Namespace) -> str:
    """Resolve the backup passphrase without ever touching argv.

    Order: ``--passphrase-stdin`` → ``--passphrase-env NAME`` → interactive
    ``getpass`` prompt (TTY only). Exits with an error when no passphrase
    is available anywhere.
    """
    if args.passphrase_stdin:
        passphrase = sys.stdin.readline().rstrip("\r\n")
        if not passphrase:
            raise SystemExit("error: --passphrase-stdin given but stdin was empty.")
        return passphrase
    env_name = args.passphrase_env or ""
    if env_name:
        passphrase = os.environ.get(env_name, "")
        if passphrase:
            return passphrase
    if sys.stdin.isatty():
        try:
            passphrase = getpass.getpass("Backup passphrase: ")
        except (EOFError, OSError, KeyboardInterrupt):
            raise SystemExit("\nerror: passphrase prompt aborted.")
        if passphrase:
            return passphrase
    raise SystemExit(
        "error: no backup passphrase available. Set the "
        f"${env_name or PASSPHRASE_ENV_DEFAULT} environment variable, "
        "pass --passphrase-stdin, or run on a TTY for an interactive prompt."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="veto-backup",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "create",
        help="Create an encrypted backup (passphrase: 20+ chars)",
        description=(
            "Create an encrypted backup of the named stable schemas.\n\n"
            "Passphrase policy: at least 20 characters, never on argv\n"
            "(--passphrase-stdin, --passphrase-env, or interactive prompt).\n"
            "Pass --generate-passphrase to have the CLI create a strong random\n"
            "passphrase for you — store it in a password manager, it cannot\n"
            "be recovered and without it the backup is unreadable.\n\n"
            "Coverage: named stable schemas only (+ profiles/*.json). NOT\n"
            "backed up: legacy applications.json (the primary applications\n"
            "store), backup_audit.jsonl, and any other non-schema files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument(
        "--generate-passphrase",
        action="store_true",
        help=(
            "Generate a strong random 32-character passphrase, print it once "
            "to stderr, and use it for this backup."
        ),
    )
    _add_passphrase_args(p)

    p = sub.add_parser("verify", help="Verify backup integrity (exit 1 on failure)")
    p.add_argument("--in", dest="inp", required=True)
    _add_passphrase_args(p)

    p = sub.add_parser(
        "restore",
        help="Restore from a backup (exit 1 on integrity failure)",
        description=(
            "Restore stable-schema files from an encrypted backup.\n\n"
            "Safety semantics:\n"
            "  * If the target data dir already contains files, restore refuses\n"
            "    unless --yes is given (or you confirm at the TTY prompt).\n"
            "  * A timestamped snapshot of the existing data dir is copied to a\n"
            "    sibling <data>.pre-restore-<timestamp>/ directory first.\n"
            "  * Restore is atomic-or-rollback: every blob is checksum-verified\n"
            "    before anything is written, and on any failure the data dir is\n"
            "    rolled back to the snapshot (or removed if it did not exist).\n"
            "  * --dry-run performs every check and reports what would be\n"
            "    restored without writing anything.\n"
            "Exits nonzero when the backup fails integrity verification."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--data-dir", required=True)
    p.add_argument(
        "--selective", default="", help="Comma-separated schema names to restore (default: all)"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the backup and report what would be restored; write nothing.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Confirm restore into a non-empty data dir without prompting.",
    )
    _add_passphrase_args(p)

    p = sub.add_parser(
        "drill",
        help="Run a disaster-recovery drill (stable schemas only)",
        description=(
            "Disaster-recovery drill: backup → restore into a scratch dir →\n"
            "verify 100% checksums → confirm zero record gap over everything\n"
            "the backup covers. The live store is never touched.\n\n"
            "COVERAGE IS STABLE SCHEMAS ONLY. Explicitly NOT backed up and\n"
            "therefore NOT covered by a PASS: legacy applications.json (the\n"
            "primary applications store), backup_audit.jsonl, and any other\n"
            "file that is not part of a named stable schema. The drill report\n"
            "lists the excluded files found in the data dir; a PASS never\n"
            "implies full disaster-recovery coverage."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data-dir", required=True)
    _add_passphrase_args(p)

    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            if args.generate_passphrase:
                passphrase = generate_passphrase()
                print(
                    "Generated backup passphrase — store it in a password "
                    "manager. It cannot be recovered; without it the backup "
                    "is unreadable. Reuse it via $VETO_BACKUP_PASSPHRASE for "
                    "verify/restore/drill:",
                    file=sys.stderr,
                )
                print(passphrase, file=sys.stderr)
            else:
                passphrase = _resolve_passphrase(args)
            r = create_backup(args.data_dir, args.out, passphrase)
            print(json.dumps(r.__dict__, indent=2))
        elif args.command == "verify":
            passphrase = _resolve_passphrase(args)
            r = verify_backup(args.inp, passphrase)
            print(json.dumps(r.__dict__, indent=2))
            return 0 if r.ok else 1
        elif args.command == "restore":
            passphrase = _resolve_passphrase(args)
            sel = [s.strip() for s in args.selective.split(",") if s.strip()] or None
            target = Path(args.data_dir)
            if (
                not args.dry_run
                and not args.yes
                and _dir_has_files(target)
                and sys.stdin.isatty()
            ):
                answer = input(
                    f"Target {target} already contains files. Restore will "
                    "overwrite stable-schema files (a timestamped pre-restore "
                    "snapshot is taken first). Continue? [y/N] "
                )
                if answer.strip().lower() in ("y", "yes"):
                    args.yes = True
                else:
                    print("restore aborted.", file=sys.stderr)
                    return 1
            r = restore(
                args.inp,
                passphrase,
                args.data_dir,
                sel,
                dry_run=args.dry_run,
                confirm=args.yes,
            )
            print(
                json.dumps(
                    {**r.__dict__, "verify": r.verify.__dict__}, indent=2, default=str
                )
            )
            return 0 if r.verify.ok else 1
        elif args.command == "drill":
            passphrase = _resolve_passphrase(args)
            r = drill(args.data_dir, passphrase)
            print(json.dumps(r.__dict__, indent=2))
            return 0 if r.ok else 1
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
