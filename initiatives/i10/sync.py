#!/usr/bin/env python3
"""Opt-in end-to-end encrypted device sync (Epic 4).

Local-only is the default and stays the default. Sync does nothing until
the user explicitly runs ``enable`` with a passphrase. When enabled:

* Every sync payload is an opaque encrypted envelope (Fernet, key from
  scrypt over the user's sync passphrase). The sync vault — a directory
  the user points at a shared folder, NAS, or USB stick — ever sees
  ciphertext only.
* Merge is deterministic and offline-safe:
  - ``outcome-event`` / ``evidence-store`` (append-only JSONL): union by
    record id (``event_id`` / ``record_id``), never duplicated.
  - ``profile`` files: last-writer-wins by embedded ``updated_at``.
* Sync moves ONLY the named stable schemas from ``schemas.py``.

Passphrase handling (i10-wide convention — NEVER argv):
  ``--passphrase`` does not exist and never will: argv is visible in
  ``ps`` output and shell history. The passphrase is resolved, in order,
  from the ``VETO_SYNC_PASSPHRASE`` environment variable, piped stdin
  (first line, when stdin is not a TTY), or an interactive ``getpass``
  prompt (no echo). See :func:`resolve_passphrase`.

Honest threat model (read before trusting a peer):
  * SAME PASSPHRASE = FULLY TRUSTED GROUP. Any envelope in the vault
    that decrypts with the group key is merged — there is no per-device
    allowlist and no way to revoke one device without rotating the
    passphrase on every device. The 6-word pairing code is a TYPO
    CHECK, not a trust ceremony: it only proves both devices derived
    the same key from what was typed. Compare it out of band to catch
    a mistyped passphrase before syncing.
  * NO FORWARD SECRECY. One group key encrypts the entire vault
    history; anyone who learns the passphrase can decrypt every past
    envelope, not just future ones.
  * PLAINTEXT METADATA VISIBLE TO THE VAULT HOST. The envelope
    plaintext header carries only the magic bytes (``VETOSYNC1``); the
    envelope filename (``envelope-<device12>-<seq>.vetosync``) and file
    mtimes reveal each device's random 12-hex pseudonym, how many
    envelopes it published, sequence numbers, and sync timing. The KDF
    salt file is plaintext (salts are not secret). Content itself is
    ciphertext only.

Timestamps and migration:
  Every merge-ordering timestamp written by this module is UTC ISO-8601
  with an explicit ``Z`` suffix (e.g. ``2026-09-13T19:05:18Z``), and all
  ``recorded_at`` / ``created_at`` / ``updated_at`` comparisons
  normalize to epoch seconds via :func:`_ts_order_key`, so ordering is
  correct across timezones.
  Pre-1.1 versions wrote naive device-local stamps (no zone). Those are
  interpreted as UTC in comparisons: naive-vs-naive ordering keeps its
  legacy relative order, but MIXED naive/aware comparisons may
  mis-order. Prefer aware stamps going forward.

Never-silent-discard guarantee:
  ``merge_envelope`` rewrites local files, so before rewriting it
  snapshots every affected file into
  ``<data_dir>/sync_premerge_backups/<utc-stamp>-<ns>-<rand>/`` (the 5
  most recently created snapshots are kept; restore manually if
  needed). The ``<ns>`` field is a nanosecond timestamp, so snapshot
  order is total and pruning keeps the newest even when several
  merges land within the same second. JSONL lines that fail
  to parse and records that fail schema validation are NEVER silently
  dropped: each is logged with its key/hash/line-number and reason in
  the merge report AND the audit log. Valid local records are never
  deleted.

Transport is a pluggable :class:`Vault` — the built-in
:class:`DirectoryVault` reads/writes envelopes in a local directory.
A network transport can be added later behind the same interface; the
envelope format does not change.

Audit: ``sync_audit.jsonl`` records counts, hashes, line numbers and
reasons only — never user content, never the passphrase.

``doctor`` verifies a deployment without reading source: vault
readable/writable, salt agreement, envelopes decryptable with the
current key, and sequence sanity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import schemas

CONFIG_NAME = "sync_config.json"
AUDIT_NAME = "sync_audit.jsonl"
ENVELOPE_MAGIC = b"VETOSYNC1"
# The KDF salt is shared through the vault (plaintext salt file) so every
# device that knows the passphrase derives the SAME key. The salt is not
# secret; the passphrase is. Published atomically by the first device.
VAULT_SALT_NAME = "sync-salt.b64"
# Pre-merge snapshots: last N kept, restore manually.
PREMERGE_DIR_NAME = "sync_premerge_backups"
PREMERGE_KEEP = 5
# Cap per-file dropped-record detail so one corrupt file can't blow up
# the report; the count is always exact.
MAX_DROPPED_PER_FILE = 50
# i10-wide passphrase convention: env var, piped stdin, or getpass. NEVER argv.
PASSPHRASE_ENV_VAR = "VETO_SYNC_PASSPHRASE"

_WORDS = (
    "amber bronze cedar dawn ember frost grove harbor iris jade kelp lumen "
    "maple north opal pine quartz ridge stone timber umber vale willow xenon "
    "yarrow zephyr"
).split()


# ---------------------------------------------------------------------------
# Crypto helpers (same KDF construction as backup)
# ---------------------------------------------------------------------------


def _crypto():
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:
        raise RuntimeError(
            "The 'cryptography' package is required for sync. "
            "Install it with: pip install cryptography"
        ) from exc
    return Fernet, Scrypt


def derive_sync_key(passphrase: str, salt: bytes) -> bytes:
    Fernet, Scrypt = _crypto()
    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def key_fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def pairing_code(key: bytes) -> str:
    """6-word code derived from the key fingerprint.

    This is a TYPO CHECK, not a trust ceremony: both devices show the
    same code only if the typed passphrases derive the same key. It
    proves nothing about device identity — anyone with the passphrase
    is a full member of the sync group (see module docstring).
    """
    digest = hashlib.sha256(b"veto-pairing:" + key).digest()
    return "-".join(_WORDS[b % len(_WORDS)] for b in digest[:6])


# ---------------------------------------------------------------------------
# Timestamps: always UTC, comparisons timezone-aware
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with an explicit ``Z`` suffix.

    Lexicographically sortable; used for every merge-ordering timestamp
    this module writes (envelope ``created_at``, config ``enabled_at``,
    audit ``at``).
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_order_key(ts: Any) -> tuple[int, float]:
    """Sortable key for a merge-ordering timestamp.

    Aware stamps normalize to UTC epoch seconds, so comparisons are
    correct across timezones. Naive stamps (written by pre-1.1 sync in
    device-local time, no zone) are interpreted as UTC — naive-vs-naive
    comparisons keep their legacy relative order, but mixed naive/aware
    comparisons may mis-order (documented caveat). Empty or unparseable
    stamps sort before everything else (stable; matches the old ``""``
    behavior).
    """
    if ts is None:
        return (0, 0.0)
    s = str(ts).strip()
    if not s:
        return (0, 0.0)
    if s[-1] in ("Z", "z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return (0, 0.0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # legacy naive: see docstring
    return (1, dt.timestamp())


# ---------------------------------------------------------------------------
# Passphrase resolution (i10-wide convention: NEVER argv)
# ---------------------------------------------------------------------------


def resolve_passphrase(prompt: str = "Sync passphrase: ") -> str:
    """Resolve the sync passphrase WITHOUT ever taking it from argv.

    ``--passphrase`` flags are banned i10-wide: argv is visible in
    ``ps`` output and shell history. Resolution order:

    1. ``VETO_SYNC_PASSPHRASE`` environment variable,
    2. piped stdin (first line) when stdin is not a TTY,
    3. interactive ``getpass`` prompt (no echo).
    """
    env = os.environ.get(PASSPHRASE_ENV_VAR)
    if env:
        return env
    if not sys.stdin.isatty():
        lines = sys.stdin.read().splitlines()
        if lines and lines[0].strip():
            return lines[0].strip()
        raise ValueError(
            "No passphrase provided: stdin is empty. Set "
            "VETO_SYNC_PASSPHRASE, pipe the passphrase on stdin, or run "
            "interactively."
        )
    import getpass

    pw = getpass.getpass(prompt)
    if not pw:
        raise ValueError("No passphrase entered.")
    return pw


# ---------------------------------------------------------------------------
# Config (opt-in state)
# ---------------------------------------------------------------------------


def _config_path(data_dir: Path) -> Path:
    return data_dir / CONFIG_NAME


def load_config(data_dir: str | Path) -> dict:
    path = _config_path(Path(data_dir))
    if not path.exists():
        return {"enabled": False}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_config(data_dir: Path, config: dict) -> None:
    _config_path(data_dir).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _audit(data_dir: Path, action: str, detail: dict) -> None:
    with (data_dir / AUDIT_NAME).open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"at": utc_now_iso(), "action": action, **detail},
                sort_keys=True,
            )
            + "\n"
        )


def _publish_salt_atomic(vault_path: Path) -> bytes:
    """First-device salt publish: write a temp file, then atomically link
    it into place. Detects and refuses if a salt appeared concurrently
    (two devices racing ``--init``): the loser must ``--join`` instead.

    Falls back to atomic exclusive-create on filesystems without hard
    link support (e.g. FAT USB sticks).
    """
    salt = os.urandom(16)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    target = vault_path / VAULT_SALT_NAME
    tmp = vault_path / f".{VAULT_SALT_NAME}.{uuid.uuid4().hex}.tmp"
    tmp.write_text(salt_b64, encoding="utf-8")
    try:
        try:
            os.link(tmp, target)  # atomic; raises FileExistsError if raced
        except FileExistsError:
            raise ValueError(
                "A sync salt appeared in the vault concurrently. Refusing "
                "to overwrite it — re-run enable with mode='join' (--join) "
                "instead of --init."
            )
        except OSError:
            # No hard-link support (FAT/exFAT USB): atomic exclusive create.
            try:
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raise ValueError(
                    "A sync salt appeared in the vault concurrently. "
                    "Refusing to overwrite it — re-run enable with "
                    "mode='join' (--join) instead of --init."
                )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(salt_b64)
    finally:
        tmp.unlink(missing_ok=True)
    return salt


def _read_vault_salt(vault_path: Path) -> bytes:
    salt_path = vault_path / VAULT_SALT_NAME
    if not salt_path.exists():
        raise ValueError(
            "No sync salt in this vault — nothing to join. Run the first "
            "device with mode='init' (--init), or check the vault path: a "
            "wrong/empty folder here means key divergence later, so join "
            "refuses instead of silently starting a new group."
        )
    return base64.b64decode(salt_path.read_text(encoding="utf-8").strip())


def enable(data_dir: str | Path, passphrase: str, vault_path: str, *, mode: str) -> dict:
    """Opt IN to sync. Explicit, reversible, local-only until the user
    points the vault at shared storage themselves.

    ``mode`` is required and never guessed — silently generating or
    reusing a salt is exactly how a device pointed at the wrong/empty
    folder gets key divergence that surfaces later as a misleading
    error:

    * ``"init"`` — first device. Refuses if the vault already holds a
      sync salt (use ``"join"``). Publishes the new salt atomically.
    * ``"join"`` — additional device. Refuses if the vault has no sync
      salt yet (use ``"init"`` on the first device). Reuses the vault's
      salt so the same passphrase derives the same key.

    Trust model (honest): everyone who knows the passphrase is a full
    member of the sync group — any envelope that decrypts with the
    group key merges. The 6-word pairing code is a typo check, not a
    trust ceremony: compare it out of band on both devices to catch a
    mistyped passphrase. There is no per-device allowlist.
    """
    if mode not in ("init", "join"):
        raise ValueError("enable() requires mode='init' or mode='join'.")
    if not passphrase or len(passphrase) < 8:
        raise ValueError("Sync passphrase must be at least 8 characters.")
    data_dir = Path(data_dir)
    vault_dir = Path(vault_path).expanduser()
    vault_dir.mkdir(parents=True, exist_ok=True)
    if mode == "init":
        if (vault_dir / VAULT_SALT_NAME).exists():
            raise ValueError(
                "This vault already has a sync salt (another device "
                "initialized it). Re-run with mode='join' (--join) to join "
                "it, or point --vault at an empty directory to start a new "
                "sync group."
            )
        salt = _publish_salt_atomic(vault_dir)
    else:
        salt = _read_vault_salt(vault_dir)
    key = derive_sync_key(passphrase, salt)
    config = {
        "enabled": True,
        "device_id": uuid.uuid4().hex[:12],
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "key_fingerprint": key_fingerprint(key),
        "vault_path": str(Path(vault_path).expanduser()),
        "enabled_at": utc_now_iso(),
        "sequence": 0,
    }
    _save_config(data_dir, config)
    Path(config["vault_path"]).mkdir(parents=True, exist_ok=True)
    _audit(data_dir, "sync_enabled", {"device_id": config["device_id"], "mode": mode})
    return {
        "enabled": True,
        "device_id": config["device_id"],
        "pairing_code": pairing_code(key),
        "key_fingerprint": config["key_fingerprint"],
    }


def disable(data_dir: str | Path) -> dict:
    data_dir = Path(data_dir)
    cfg = load_config(data_dir)
    cfg["enabled"] = False
    _save_config(data_dir, cfg)
    _audit(data_dir, "sync_disabled", {})
    return {"enabled": False}


# ---------------------------------------------------------------------------
# Envelope format
# ---------------------------------------------------------------------------


@dataclass
class Envelope:
    device_id: str
    sequence: int
    created_at: str
    schema_versions: dict[str, str]
    datasets: dict[str, Any]  # schema_name -> {rel_path: [records] | {doc}}

    def to_bytes(self, key: bytes) -> bytes:
        Fernet, _ = _crypto()
        body = json.dumps(
            {
                "device_id": self.device_id,
                "sequence": self.sequence,
                "created_at": self.created_at,
                "schema_versions": self.schema_versions,
                "datasets": self.datasets,
            },
            sort_keys=True,
        ).encode("utf-8")
        token = Fernet(key).encrypt(body)
        # Plaintext header is minimized: magic only. created_at and the
        # device id live inside the encrypted body. The FILENAME still
        # reveals the device pseudonym, sequence, and timing to the
        # vault host (documented in the module docstring).
        header = json.dumps({"magic": "VETOSYNC1"}, sort_keys=True).encode("utf-8")
        return ENVELOPE_MAGIC + len(header).to_bytes(4, "big") + header + token

    @staticmethod
    def from_bytes(raw: bytes, key: bytes) -> "Envelope":
        if len(raw) < 13 or raw[:9] != ENVELOPE_MAGIC:
            raise ValueError(
                "Not a Veto sync envelope (bad magic) — file is corrupt or "
                "not a sync envelope at all."
            )
        hlen = int.from_bytes(raw[9:13], "big")
        token = raw[13 + hlen :]
        Fernet, _ = _crypto()
        try:
            body = json.loads(Fernet(key).decrypt(token))
        except Exception as exc:
            # Fernet raises InvalidToken for a wrong key AND for corrupted
            # bytes; the two are indistinguishable by design, so the
            # message says exactly that instead of blaming the passphrase.
            raise ValueError(
                "Envelope decryption failed: wrong sync passphrase OR "
                "corrupted envelope bytes (indistinguishable by design)."
            ) from exc
        return Envelope(
            device_id=body["device_id"],
            sequence=body["sequence"],
            created_at=body["created_at"],
            schema_versions=body["schema_versions"],
            datasets=body["datasets"],
        )


def _record_hash(record: Any) -> str:
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _read_records_detailed(path: Path) -> tuple[Any, list[dict]]:
    """Read a dataset file, returning ``(records, dropped)``.

    ``dropped`` entries carry line number, sha256 hash of the raw line,
    and reason — never record content. Corrupt lines are reported here
    so merge/sync can surface them instead of silently discarding them.
    """
    dropped: list[dict] = []
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        out = []
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                dropped.append(
                    {
                        "line": lineno,
                        "hash": hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16],
                        "reason": f"not JSON: {exc.msg}",
                    }
                )
        return out, dropped
    try:
        return json.loads(text), dropped
    except json.JSONDecodeError as exc:
        dropped.append(
            {
                "line": None,
                "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                "reason": f"not JSON: {exc.msg}",
            }
        )
        return None, dropped


def _collect_datasets(data_dir: Path) -> tuple[dict[str, Any], list[dict]]:
    """Gather the publishable datasets plus a list of local lines that
    could not be parsed (reported by sync_round, never silently lost —
    the local file itself is untouched by collection)."""
    datasets: dict[str, Any] = {}
    dropped: list[dict] = []
    for name, schema in schemas.SCHEMA_REGISTRY.items():
        files: dict[str, Any] = {}
        rels = list(schema.files)
        if name == "profile":
            rels += sorted(
                str(p.relative_to(data_dir))
                for p in (data_dir / "profiles").glob("*.json")
                if str(p.relative_to(data_dir)) not in rels
            )
        for rel in rels:
            path = data_dir / rel
            if path.exists():
                records, file_dropped = _read_records_detailed(path)
                files[rel] = records
                for d in file_dropped:
                    dropped.append({"source": "local-unparseable", "rel": rel, **d})
        if files:
            datasets[name] = files
    return datasets, dropped


def build_envelope(data_dir: str | Path, key: bytes, device_id: str, sequence: int) -> Envelope:
    datasets, _dropped = _collect_datasets(Path(data_dir))
    return Envelope(
        device_id=device_id,
        sequence=sequence,
        created_at=utc_now_iso(),
        schema_versions=schemas.schema_versions(),
        datasets=datasets,
    )


# ---------------------------------------------------------------------------
# Merge (deterministic, offline-safe, never silently discards)
# ---------------------------------------------------------------------------


def _merge_append_only(
    local: list[dict], remote: list[dict], key: str
) -> tuple[list[dict], int, int]:
    """Union by record key; remote wins on key collision for correctable
    fields only when it carries a newer ``recorded_at``/``created_at``
    (timezone-aware comparison)."""
    by_key: dict[str, dict] = {}
    for rec in local:
        if isinstance(rec, dict) and rec.get(key):
            by_key[str(rec[key])] = rec
    added = 0
    updated = 0
    for rec in remote:
        if not isinstance(rec, dict) or not rec.get(key):
            continue
        k = str(rec[key])
        if k not in by_key:
            by_key[k] = rec
            added += 1
        else:
            old, new = by_key[k], rec
            old_ts = old.get("recorded_at") or old.get("created_at")
            new_ts = new.get("recorded_at") or new.get("created_at")
            if _ts_order_key(new_ts) > _ts_order_key(old_ts):
                by_key[k] = new
                updated += 1
    return list(by_key.values()), added, updated


# Snapshot dir names embed a nanosecond timestamp so creation order is
# total: {compact-utc-stamp}-{time_ns:020d}-{6 random hex} (the stamp keeps
# utc_now_iso's trailing "Z", e.g. 20260914T000057Z-...). Lexicographic
# order == chronological order, so same-second snapshots no longer sort by
# their random suffix. Pre-fix dirs ({stamp}-{rand}, no ns field) fall back
# to mtime_ns, which is the same epoch-nanosecond scale.
_SNAPSHOT_NAME_RE = re.compile(r"^\d{8}T\d{6}Z?-(\d{20})-[0-9a-f]{6}$")


def _snapshot_before_merge(data_dir: Path, rel: str, state: dict) -> Path:
    """Copy a local file about to be rewritten into the current merge's
    snapshot (created lazily on first use).

    Pruning is deliberately NOT done here: it runs once, AFTER the
    merge's copies have landed (see :func:`merge_envelope`). Pruning
    before the copy could delete the very directory the merge is
    writing into, and the copy would then silently re-create it —
    leaving more than PREMERGE_KEEP behind."""
    if "dir" not in state:
        stamp = utc_now_iso().replace("-", "").replace(":", "")
        snap = data_dir / PREMERGE_DIR_NAME / f"{stamp}-{time.time_ns():020d}-{uuid.uuid4().hex[:6]}"
        snap.mkdir(parents=True, exist_ok=True)
        state["dir"] = snap
    snap = state["dir"]
    src = data_dir / rel
    if src.is_file():
        dst = snap / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return snap


def _snapshot_order_key(p: Path) -> int:
    """Creation-order key for a snapshot dir (newest sorts last)."""
    m = _SNAPSHOT_NAME_RE.match(p.name)
    if m:
        return int(m.group(1))
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


def _prune_snapshots(data_dir: Path) -> None:
    """Keep only the PREMERGE_KEEP most recently CREATED snapshots.

    Callers must invoke this AFTER the current merge's snapshot copies
    have landed — never before — so the directory being written into is
    always the newest and always survives."""
    root = data_dir / PREMERGE_DIR_NAME
    if not root.is_dir():
        return
    snaps = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=_snapshot_order_key,
    )
    for old in snaps[:-PREMERGE_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def _cap_dropped(dropped: list[dict]) -> dict:
    return {
        "dropped": dropped[:MAX_DROPPED_PER_FILE],
        "dropped_truncated": max(0, len(dropped) - MAX_DROPPED_PER_FILE),
        "dropped_total": len(dropped),
    }


def _merge_append_only_file(
    data_dir: Path, rel: str, schema: Any, payload: Any, snap_state: dict
) -> dict:
    """Merge one JSONL file of an append-only dataset (``record_key`` set).

    Dispatch is by dataset, not payload shape: a non-list payload here
    is a shape error, never a profile LWW. Invalid records (local or
    remote) are reported with key/hash/reason instead of silently
    vanishing.
    """
    if not isinstance(payload, list):
        return {
            "error": (
                f"shape mismatch: dataset {schema.name!r} expects a JSONL "
                f"list for {rel}, got {type(payload).__name__} — refusing "
                "to merge as a profile document."
            )
        }
    dest = data_dir / rel
    dropped: list[dict] = []
    local, local_dropped = _read_records_detailed(dest) if dest.exists() else ([], [])
    if not isinstance(local, list):
        local = []
    for d in local_dropped:
        dropped.append({"source": "local", "rel": rel, **d})
    valid_local = []
    for rec in local:
        problems = schema.validator(rec)
        if problems:
            dropped.append(
                {
                    "source": "local",
                    "rel": rel,
                    "key": str(rec.get(schema.record_key))
                    if isinstance(rec, dict)
                    else None,
                    "line": None,
                    "hash": _record_hash(rec),
                    "reason": "schema validation: " + "; ".join(problems),
                }
            )
        else:
            valid_local.append(rec)
    valid_remote = []
    for rec in payload:
        if not isinstance(rec, dict) or not rec.get(schema.record_key):
            dropped.append(
                {
                    "source": "remote",
                    "rel": rel,
                    "key": None,
                    "line": None,
                    "hash": _record_hash(rec),
                    "reason": f"missing record key {schema.record_key!r}",
                }
            )
            continue
        problems = schema.validator(rec)
        if problems:
            dropped.append(
                {
                    "source": "remote",
                    "rel": rel,
                    "key": str(rec.get(schema.record_key)),
                    "line": None,
                    "hash": _record_hash(rec),
                    "reason": "schema validation: " + "; ".join(problems),
                }
            )
            continue
        valid_remote.append(rec)
    merged, added, updated = _merge_append_only(valid_local, valid_remote, schema.record_key)
    new_text = "\n".join(json.dumps(r, sort_keys=True) for r in merged) + "\n"
    if dest.exists() and dest.read_bytes() == new_text.encode("utf-8"):
        # No-op merge: output is byte-identical to the local file, so
        # there is nothing to snapshot and nothing to rewrite. Skipping
        # keeps unchanged merges from churning the snapshot history.
        return {"added": added, "updated": updated, "total": len(merged), **_cap_dropped(dropped)}
    _snapshot_before_merge(data_dir, rel, snap_state)
    dest.write_text(new_text, encoding="utf-8")
    return {"added": added, "updated": updated, "total": len(merged), **_cap_dropped(dropped)}


def _merge_lww_file(
    data_dir: Path, rel: str, schema: Any, payload: Any, snap_state: dict
) -> dict:
    """Merge one document file of an LWW dataset (no ``record_key``).

    Last-writer-wins by embedded ``updated_at`` (timezone-aware). A
    corrupt local document is reported, not silently replaced.
    """
    if not isinstance(payload, dict):
        return {
            "error": (
                f"shape mismatch: dataset {schema.name!r} expects a JSON "
                f"document for {rel}, got {type(payload).__name__}."
            )
        }
    problems = schema.validator(payload)
    if problems:
        return {
            "replaced": False,
            "error": f"remote payload failed validation: {'; '.join(problems)}",
        }
    dest = data_dir / rel
    dropped: list[dict] = []
    local, local_dropped = _read_records_detailed(dest) if dest.exists() else ({}, [])
    for d in local_dropped:
        dropped.append({"source": "local", "rel": rel, **d})
    if not isinstance(local, dict):
        if dest.exists():
            dropped.append(
                {
                    "source": "local",
                    "rel": rel,
                    "key": None,
                    "line": None,
                    "hash": _record_hash(local),
                    "reason": "local file is not a JSON object",
                }
            )
        local = {}
    lts = local.get("updated_at")
    rts = payload.get("updated_at")
    if _ts_order_key(rts) >= _ts_order_key(lts):
        if local == payload:
            # No-op merge: identical document — skip snapshot and rewrite
            # so unchanged merges don't churn the snapshot history.
            return {"replaced": False, **_cap_dropped(dropped)}
        _snapshot_before_merge(data_dir, rel, snap_state)
        dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return {"replaced": True, **_cap_dropped(dropped)}
    return {"replaced": False, **_cap_dropped(dropped)}


def merge_envelope(data_dir: str | Path, envelope: Envelope, own_device_id: str) -> dict:
    """Merge a peer envelope into the local data dir.

    Never silently discards local data:

    * every local file rewritten here is first snapshotted to
      ``sync_premerge_backups/<utc-stamp>-<ns>-<rand>/`` (the 5 most
      recently created snapshots are kept; pruning runs after the
      merge's copies land, so the snapshot being written always
      survives);
    * JSONL lines that fail to parse and records that fail schema
      validation are reported — each with key/hash/line-number and
      reason — in the merge report AND the audit log, never dropped
      silently. Valid local records are never deleted.

    Dispatch is by dataset name (``schemas.get_schema``), not payload
    shape: ``outcomes_meta.json`` (a dict under ``outcome-event``) can
    never fall into profile LWW. Unknown datasets and shape mismatches
    are reported per-file; they do not abort the rest of the merge.

    Trust: any envelope that decrypts with the group key merges. There
    is no per-device allowlist — possession of the passphrase is full
    membership (see module docstring).
    """
    if envelope.device_id == own_device_id:
        return {"merged": False, "reason": "own envelope"}
    data_dir = Path(data_dir)
    report: dict[str, Any] = {"merged": True, "peer": envelope.device_id[:8], "datasets": {}}
    snap_state: dict = {}
    for name, files in envelope.datasets.items():
        try:
            schema = schemas.get_schema(name)
        except KeyError as exc:
            report["datasets"][name] = {"error": str(exc)}
            continue
        ds_report: dict[str, Any] = {}
        for rel, payload in files.items():
            dest = data_dir / rel
            # Path-traversal guard.
            try:
                inside = dest.resolve().is_relative_to(data_dir.resolve())
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                ds_report[rel] = {
                    "error": f"Refusing to write outside data dir: {rel}"
                }
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if schema.record_key:
                ds_report[rel] = _merge_append_only_file(
                    data_dir, rel, schema, payload, snap_state
                )
            else:
                ds_report[rel] = _merge_lww_file(data_dir, rel, schema, payload, snap_state)
        report["datasets"][name] = ds_report
    if "dir" in snap_state:
        # Prune AFTER the snapshot copies have landed: the directory this
        # merge just wrote into is the newest and always survives.
        _prune_snapshots(data_dir)
        report["premerge_snapshot"] = snap_state["dir"].name
    all_dropped: list[dict] = []
    dropped_per_file: list[dict] = []
    dropped_total = 0
    dropped_truncated = 0
    for name, ds in report["datasets"].items():
        if not isinstance(ds, dict):
            continue
        for rel, fr in ds.items():
            if not isinstance(fr, dict):
                continue
            all_dropped.extend(fr.get("dropped", []))
            file_total = int(fr.get("dropped_total", 0))
            file_truncated = int(fr.get("dropped_truncated", 0))
            dropped_total += file_total
            dropped_truncated += file_truncated
            if file_total:
                dropped_per_file.append(
                    {
                        "dataset": name,
                        "rel": rel,
                        "dropped_total": file_total,
                        "dropped_truncated": file_truncated,
                    }
                )
    _audit(
        data_dir,
        "sync_merged",
        {
            "peer": envelope.device_id[:8],
            "snapshot": report.get("premerge_snapshot", ""),
            # Exact counts: "dropped" below is capped at
            # MAX_DROPPED_PER_FILE per file, but these totals (and the
            # per-file breakdown) always carry the true numbers so the
            # durable record agrees with the returned report.
            "dropped_total": dropped_total,
            "dropped_truncated": dropped_truncated,
            "dropped": all_dropped,
            "dropped_per_file": dropped_per_file,
        },
    )
    return report


# ---------------------------------------------------------------------------
# Vault transport (pluggable; DirectoryVault built in)
# ---------------------------------------------------------------------------

_ENVELOPE_RE = re.compile(r"^envelope-([0-9a-fA-F]{12})-(\d{6})\.vetosync$")


def parse_envelope_filename(name: str) -> tuple[str, int] | None:
    """Parse an envelope filename into ``(device_id, sequence)``.

    Fields are matched exactly against
    ``envelope-<12hex>-<6digits>.vetosync`` — never by substring, so a
    device id that merely contains another id can never collide.
    Returns ``None`` for non-conforming names (ignored by listing).
    """
    m = _ENVELOPE_RE.match(name)
    if not m:
        return None
    return m.group(1), int(m.group(2))


class SequenceCollision(Exception):
    """Raised when publishing an envelope sequence this device already
    published (concurrent sync_round). The caller bumps and retries."""


class DirectoryVault:
    """The vault is a directory the user controls (shared folder, NAS,
    USB). It stores opaque ciphertext envelopes only — the host learns
    nothing about content, but the plaintext envelope filenames and file
    mtimes do reveal device pseudonyms, envelope counts, sequence
    numbers, and sync timing (see module docstring)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def publish(self, envelope_bytes: bytes, device_id: str, sequence: int) -> str:
        """Atomically publish one envelope via exclusive-create. Raises
        :class:`SequenceCollision` if this device already published that
        sequence — the caller must bump the sequence and retry."""
        name = f"envelope-{device_id}-{sequence:06d}.vetosync"
        target = self.path / name
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise SequenceCollision(f"envelope already published: {name}")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(envelope_bytes)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return name

    def list(self, own_device_id: str = "") -> list[Path]:
        """Peer envelopes only: exact device-id match excludes our own;
        non-conforming filenames are ignored."""
        found = []
        for p in self.path.glob("*.vetosync"):
            parsed = parse_envelope_filename(p.name)
            if parsed is None:
                continue
            dev, _seq = parsed
            if dev == own_device_id:
                continue
            found.append(p)
        return sorted(found)

    def fetch(self, path: Path) -> bytes:
        return path.read_bytes()


@contextmanager
def _sync_lock(data_dir: Path) -> Iterator[None]:
    """Exclusive inter-process lock for one data dir's sync_round, so two
    local sync processes can't allocate the same sequence or interleave
    merges. Best-effort on platforms without fcntl."""
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(data_dir / "sync.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Sync round
# ---------------------------------------------------------------------------


def sync_round(data_dir: str | Path, passphrase: str) -> dict:
    """One sync round: publish our envelope, fetch and merge every peer
    envelope. Returns a report with counts only (plus dropped-record
    detail — keys/hashes/line-numbers/reasons, never content).

    Fails loudly and precisely:

    * vault salt missing or different from this device's salt ->
      ``ValueError`` naming the salt mismatch (wrong vault / tampered
      salt file), NOT a passphrase error;
    * passphrase not deriving the recorded key fingerprint ->
      ``ValueError`` (wrong passphrase);
    * a peer envelope that fails to decrypt -> ``ValueError`` saying it
      is either the wrong passphrase or corrupted bytes
      (indistinguishable by design).

    Trust: any envelope that decrypts with the group key merges — no
    per-device allowlist (see module docstring). One bad peer envelope
    is recorded in the report and does not abort the rest of the round.
    """
    data_dir = Path(data_dir)
    cfg = load_config(data_dir)
    if not cfg.get("enabled"):
        raise RuntimeError("Sync is not enabled (local-only default). Run enable first.")
    vault = DirectoryVault(cfg["vault_path"])

    # Salt agreement BEFORE touching crypto: distinguishes "wrong vault
    # or tampered salt file" from "wrong passphrase".
    salt_path = vault.path / VAULT_SALT_NAME
    if not salt_path.exists():
        raise ValueError(
            "No sync salt in the vault — it was deleted or this is not "
            "the vault this device was enabled against. Refusing to sync."
        )
    vault_salt_b64 = salt_path.read_text(encoding="utf-8").strip()
    if vault_salt_b64 != cfg.get("salt_b64"):
        raise ValueError(
            "Vault salt does not match this device's salt: wrong vault, "
            "or the salt file was replaced. Refusing to sync rather than "
            "derive a divergent key."
        )
    salt = base64.b64decode(cfg["salt_b64"])
    key = derive_sync_key(passphrase, salt)
    if key_fingerprint(key) != cfg["key_fingerprint"]:
        raise ValueError("Wrong sync passphrase.")

    device_id = cfg["device_id"]
    with _sync_lock(data_dir):
        datasets, unpublished_dropped = _collect_datasets(data_dir)
        schema_versions = schemas.schema_versions()
        created_at = utc_now_iso()
        sequence = int(cfg.get("sequence", 0))
        published = None
        for _attempt in range(25):
            sequence += 1
            envelope = Envelope(
                device_id=device_id,
                sequence=sequence,
                created_at=created_at,
                schema_versions=schema_versions,
                datasets=datasets,
            )
            try:
                published = vault.publish(envelope.to_bytes(key), device_id, sequence)
                break
            except SequenceCollision:
                continue  # concurrent local sync_round won this sequence; retry higher
        if published is None:
            raise RuntimeError(
                "Could not allocate a fresh envelope sequence after 25 attempts."
            )
        cfg["sequence"] = sequence
        _save_config(data_dir, cfg)

        merged_reports = []
        for path in vault.list(own_device_id=device_id):
            try:
                peer = Envelope.from_bytes(vault.fetch(path), key)
            except ValueError as exc:
                merged_reports.append(
                    {"merged": False, "envelope": path.name, "error": str(exc)}
                )
                continue
            try:
                merged_reports.append(merge_envelope(data_dir, peer, device_id))
            except Exception as exc:
                merged_reports.append(
                    {"merged": False, "envelope": path.name, "error": str(exc)}
                )

    _audit(
        data_dir,
        "sync_round",
        {
            "published": published,
            "peers_merged": len(merged_reports),
            "sequence": sequence,
            "unpublished_unparseable": len(unpublished_dropped),
        },
    )
    return {
        "published": published,
        "sequence": sequence,
        "peers_merged": len(merged_reports),
        "merges": merged_reports,
        "unpublished_unparseable": unpublished_dropped,
        "pairing_code": pairing_code(key),
    }


def status(data_dir: str | Path) -> dict:
    cfg = load_config(data_dir)
    return {
        "enabled": bool(cfg.get("enabled")),
        "device_id": cfg.get("device_id"),
        "vault_path": cfg.get("vault_path"),
        "sequence": cfg.get("sequence", 0),
    }


def doctor(data_dir: str | Path, passphrase: str) -> dict:
    """Diagnose "sync isn't converging" without reading source.

    Checks, each reported plainly:

    * sync enabled in local config;
    * vault directory present, readable AND writable (probe file);
    * vault salt present and agreeing with this device's salt;
    * passphrase derives the recorded key fingerprint;
    * every peer envelope in the vault decrypts with the current key;
    * sequence sanity: the vault holds no envelope from this device id
      newer than this data dir's recorded sequence (a newer one means
      another process — or a cloned data dir — is publishing).

    Returns ``{"ok": bool, "checks": {...}}`` with counts/hashes only,
    never content.
    """
    data_dir = Path(data_dir)
    checks: dict[str, Any] = {}
    cfg = load_config(data_dir)
    checks["enabled"] = bool(cfg.get("enabled"))

    vault_path = cfg.get("vault_path")
    vault_ok = bool(vault_path) and Path(vault_path).is_dir()
    checks["vault_dir_present"] = vault_ok

    writable = False
    if vault_ok:
        probe = Path(vault_path) / f".veto-sync-probe-{uuid.uuid4().hex[:8]}"
        try:
            probe.write_text("probe", encoding="utf-8")
            probe.unlink()
            writable = True
        except OSError as exc:
            checks["vault_writable_error"] = str(exc)
    checks["vault_writable"] = writable

    salt_ok = False
    if vault_ok:
        salt_file = Path(vault_path) / VAULT_SALT_NAME
        if not salt_file.exists():
            checks["salt_error"] = "no salt file in vault"
        else:
            vault_salt_b64 = salt_file.read_text(encoding="utf-8").strip()
            salt_ok = vault_salt_b64 == cfg.get("salt_b64")
            if not salt_ok:
                checks["salt_error"] = (
                    "vault salt differs from this device's salt "
                    "(wrong vault or replaced salt file)"
                )
    else:
        checks["salt_error"] = "vault dir missing"
    checks["salt_agrees"] = salt_ok

    key = None
    fp_ok = False
    if cfg.get("salt_b64") and passphrase:
        try:
            key = derive_sync_key(passphrase, base64.b64decode(cfg["salt_b64"]))
            fp_ok = key_fingerprint(key) == cfg.get("key_fingerprint")
        except Exception:
            fp_ok = False
    checks["passphrase_matches"] = fp_ok

    env_total = 0
    env_ok = 0
    env_failed: list[dict] = []
    max_own_seq = 0
    if vault_ok and fp_ok and key is not None:
        for p in sorted(Path(vault_path).glob("*.vetosync")):
            parsed = parse_envelope_filename(p.name)
            if parsed is None:
                continue
            dev, seq = parsed
            env_total += 1
            if dev == cfg.get("device_id"):
                max_own_seq = max(max_own_seq, seq)
                continue
            try:
                Envelope.from_bytes(p.read_bytes(), key)
                env_ok += 1
            except ValueError as exc:
                env_failed.append({"file": p.name, "error": str(exc)})
    checks["peer_envelopes_total"] = env_total
    checks["peer_envelopes_decryptable"] = env_ok
    if env_failed:
        checks["peer_envelopes_failed"] = env_failed

    local_seq = int(cfg.get("sequence", 0))
    checks["local_sequence"] = local_seq
    checks["vault_max_own_sequence"] = max_own_seq
    seq_ok = max_own_seq <= local_seq
    checks["sequence_sane"] = seq_ok
    if not seq_ok:
        checks["sequence_warning"] = (
            "vault holds a newer envelope from this device id than this "
            "data dir recorded — another process or a cloned data dir may "
            "be publishing."
        )

    ok = all(
        [
            checks["enabled"],
            vault_ok,
            writable,
            salt_ok,
            fp_ok,
            not env_failed,
            seq_ok,
        ]
    )
    return {"ok": ok, "checks": checks}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="veto-sync",
        description="Opt-in end-to-end encrypted device sync. "
        "Passphrase via VETO_SYNC_PASSPHRASE, piped stdin, or getpass — "
        "never argv.",
    )
    parser.add_argument("--data-dir", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enable", help="Opt in to encrypted sync")
    p.add_argument("--vault", required=True, help="Vault directory (shared folder/NAS/USB)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--init", action="store_true", help="First device: create the sync group")
    g.add_argument("--join", action="store_true", help="Join an existing sync group")

    sub.add_parser("disable", help="Opt out (local-only)")
    sub.add_parser("status", help="Show sync status")
    sub.add_parser("sync", help="Run one sync round")
    sub.add_parser("pairing-code", help="Show the pairing code for this device")
    sub.add_parser("doctor", help="Diagnose sync health without reading source")

    args = parser.parse_args(argv)
    if args.command == "enable":
        mode = "init" if args.init else "join"
        print(
            json.dumps(
                enable(args.data_dir, resolve_passphrase(), args.vault, mode=mode),
                indent=2,
            )
        )
    elif args.command == "disable":
        print(json.dumps(disable(args.data_dir), indent=2))
    elif args.command == "status":
        print(json.dumps(status(args.data_dir), indent=2))
    elif args.command == "sync":
        print(
            json.dumps(sync_round(args.data_dir, resolve_passphrase()), indent=2, default=str)
        )
    elif args.command == "pairing-code":
        cfg = load_config(args.data_dir)
        if not cfg.get("salt_b64"):
            raise RuntimeError("Sync is not enabled. Run enable first.")
        key = derive_sync_key(resolve_passphrase(), base64.b64decode(cfg["salt_b64"]))
        print(pairing_code(key))
    elif args.command == "doctor":
        print(json.dumps(doctor(args.data_dir, resolve_passphrase()), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
