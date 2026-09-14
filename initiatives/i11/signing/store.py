#!/usr/bin/env python3
"""Shared JSON store machinery for the signing/registry units.

Every persistent JSON store in these units (registry, install records,
revocation list) goes through :class:`JsonStore`:

- **Schema version.** Files are written as
  ``{"schema_version": 1, "<payload_key>": <payload>}``. Reads accept
  legacy bare payloads (dict or list, no wrapper) written before the
  schema version existed, and migrate them on the next write.
- **Fail loud on corruption.** A file that is not valid JSON, has an
  unexpected shape, or carries an unknown schema version raises
  :class:`CorruptStoreError` — it is never silently treated as empty.
  A corrupt registry must never masquerade as "nothing submitted"; a
  corrupt revocation list must never masquerade as "nothing revoked".
- **Backup of the last good state.** Every successful write preserves
  the previous valid file at ``<path>.bak`` before replacing it, so an
  operator can recover after corruption or a bad write.
- **Atomic writes.** Content goes to a temp file in the same directory,
  is fsynced, then ``os.replace``d over the target.
- **File locking.** Writers take an exclusive ``flock``; readers take a
  shared one. This plus the documented single-writer discipline (one
  process owns each store file; concurrent writers are not supported)
  prevents torn reads/writes from cooperating processes.

``fcntl.flock`` is POSIX-only; these stores are local-first by design
(the registry docstring says a remote index is Initiative 10's
distribution concern), so that trade is explicit.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class CorruptStoreError(Exception):
    """A store file exists but cannot be trusted. Raised instead of
    returning empty state — see module docstring."""


class JsonStore:
    """A small JSON document store with schema version, backups,
    atomic writes, and advisory locking.

    ``payload_key`` names the wrapper key holding the payload
    (e.g. ``"entries"``); ``empty`` is the value returned when the
    file does not exist yet (first run — not corruption).
    ``payload_type`` is the expected Python type of the payload.
    """

    def __init__(self, path: str | Path, *, kind: str, payload_key: str,
                 empty: Any, payload_type: type = dict) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._kind = kind
        self._payload_key = payload_key
        self._empty = empty
        self._payload_type = payload_type

    @property
    def path(self) -> Path:
        return self._path

    @property
    def backup_path(self) -> Path:
        return self._path.with_name(self._path.name + ".bak")

    # -- reads ------------------------------------------------------------
    def read(self) -> Any:
        """Return the payload. Raises CorruptStoreError on any file that
        exists but is not a valid, expected-shape, known-schema store."""
        if not self._path.is_file():
            # Missing file is first run, not corruption.
            return self._empty() if callable(self._empty) else self._empty
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    text = fh.read()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            data = json.loads(text)
        except OSError as exc:
            raise CorruptStoreError(
                f"{self._kind} store at {self._path} is unreadable: {exc}. "
                f"Last good backup (if any): {self.backup_path}") from exc
        except json.JSONDecodeError as exc:
            raise CorruptStoreError(
                f"{self._kind} store at {self._path} is corrupt "
                f"(invalid JSON at line {exc.lineno}, column {exc.colno}: "
                f"{exc.msg}). Refusing to treat it as empty. "
                f"Last good backup (if any): {self.backup_path}") from exc
        return self._unwrap(data)

    def _unwrap(self, data: Any) -> Any:
        """Accept the versioned wrapper or a legacy bare payload."""
        if isinstance(data, dict) and "schema_version" in data:
            version = data["schema_version"]
            if version != SCHEMA_VERSION:
                raise CorruptStoreError(
                    f"{self._kind} store at {self._path} has schema_version "
                    f"{version!r}; this build understands "
                    f"{SCHEMA_VERSION}. Refusing to guess.")
            payload = data.get(self._payload_key, self._empty_payload())
        elif isinstance(data, dict) and self._payload_type is dict:
            # Legacy: bare dict payload, no wrapper.
            payload = data
        elif isinstance(data, list) and self._payload_type is list:
            # Legacy: bare list payload, no wrapper.
            payload = data
        else:
            raise CorruptStoreError(
                f"{self._kind} store at {self._path} has unexpected shape "
                f"{type(data).__name__}; expected {self._payload_type.__name__} "
                f"payload. Refusing to treat it as empty. "
                f"Last good backup (if any): {self.backup_path}")
        if not isinstance(payload, self._payload_type):
            raise CorruptStoreError(
                f"{self._kind} store at {self._path}: payload has unexpected "
                f"type {type(payload).__name__}; expected "
                f"{self._payload_type.__name__}. Refusing to treat it as "
                f"empty. Last good backup (if any): {self.backup_path}")
        return payload

    def _empty_payload(self) -> Any:
        return self._empty() if callable(self._empty) else self._empty

    # -- writes -----------------------------------------------------------
    def write(self, payload: Any) -> None:
        """Atomically replace the store, preserving the previous valid
        state at ``<path>.bak``. Takes an exclusive lock while writing."""
        if not isinstance(payload, self._payload_type):
            raise TypeError(
                f"{self._kind} store payload must be "
                f"{self._payload_type.__name__}, got "
                f"{type(payload).__name__}")
        doc = {"schema_version": SCHEMA_VERSION,
               self._payload_key: payload}
        text = json.dumps(doc, indent=2, sort_keys=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name + ".",
            suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            # Preserve the last good state before replacing it.
            if self._path.is_file():
                try:
                    with open(self._path, "r", encoding="utf-8") as fh:
                        json.load(fh)  # only back up a valid file
                    os.replace(self._path, self.backup_path)
                except (OSError, json.JSONDecodeError):
                    # Current file is unreadable/corrupt: leave it (and any
                    # older backup) alone rather than backing up garbage.
                    pass
            os.replace(tmp_name, self._path)
            self._fsync_dir()
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _fsync_dir(self) -> None:
        try:
            fd = os.open(str(self._path.parent), os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
