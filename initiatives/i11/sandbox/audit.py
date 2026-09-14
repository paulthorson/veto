#!/usr/bin/env python3
"""Hash-chained extension audit log.

Every brokered action appends an entry chained to the previous entry's
hash, so tampering with history breaks the chain. Extension code gets a
read-only view of its own entries (``entries_for`` returns plain data —
never a write handle); only the Host holds the append path.

Append is O(1): the previous entry's hash is cached in memory and
recovered by reading only the last file line at startup, instead of
re-scanning the whole file per append. Size caps bound unbounded
growth; a capped append returns an entry dict with an ``"error"`` key
and never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Bounded blast radius: per-host caps on audit growth.
MAX_AUDIT_ENTRIES = 100_000
MAX_AUDIT_BYTES = 20 * 1024 * 1024  # 20 MiB


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ExtensionAudit:
    def __init__(self, log_path: str | Path) -> None:
        self._path = Path(log_path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if not self._path.exists():
                self._path.touch()
        except OSError:
            pass
        # O(1) recovery: read only the last line, never the whole file.
        self._last = "GENESIS"
        self._count = 0
        try:
            size = self._path.stat().st_size
        except OSError:
            size = 0
        if size:
            try:
                with self._path.open("rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    pos = fh.tell()
                    chunk = b""
                    while pos > 0 and b"\n" not in chunk:
                        step = min(4096, pos)
                        pos -= step
                        fh.seek(pos)
                        chunk = fh.read(step) + chunk
                    lines = chunk.rsplit(b"\n", 2)
                    tail = lines[-1] if lines[-1] else lines[-2]
                    entry = json.loads(tail.decode("utf-8"))
                    self._last = entry.get("entry_hash", "GENESIS")
            except (OSError, ValueError, IndexError):
                self._last = "GENESIS"

    def append(self, ext_id: str, event: str,
               detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one audit entry. Host-only; never raises."""
        if self._count >= MAX_AUDIT_ENTRIES:
            return {"ok": False, "error": "audit entry cap reached",
                    "extension": ext_id, "event": event}
        try:
            if self._path.stat().st_size >= MAX_AUDIT_BYTES:
                return {"ok": False, "error": "audit byte cap reached",
                        "extension": ext_id, "event": event}
        except OSError:
            pass
        entry: dict[str, Any] = {
            "ts": _utcnow(),
            "extension": ext_id,
            "event": event,
            "detail": detail or {},
            "prev_hash": self._last,
        }
        body = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        entry["entry_hash"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        except OSError:
            pass  # audit write failure must not break the action it records
        else:
            self._last = entry["entry_hash"]
            self._count += 1
        return entry

    def verify(self) -> dict[str, Any]:
        """Recompute the hash chain. Returns {ok, entries, error}."""
        prev = "GENESIS"
        count = 0
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as exc:
                        return {"ok": False, "entries": count,
                                "error": f"line {lineno}: invalid JSON: {exc}"}
                    if entry.get("prev_hash") != prev:
                        return {"ok": False, "entries": count,
                                "error": f"line {lineno}: hash chain broken "
                                         "(history was tampered with)"}
                    check = dict(entry)
                    claimed = check.pop("entry_hash", None)
                    body = json.dumps(check, sort_keys=True,
                                      separators=(",", ":"))
                    if hashlib.sha256(body.encode("utf-8")).hexdigest() != claimed:
                        return {"ok": False, "entries": count,
                                "error": f"line {lineno}: entry hash mismatch"}
                    prev = claimed
                    count += 1
        except OSError as exc:
            return {"ok": False, "entries": count, "error": str(exc)}
        return {"ok": True, "entries": count, "error": None}

    def entries_for(self, ext_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Read-only view. The host may share an extension's own entries
        with it; the extension can never modify them."""
        out: list[dict[str, Any]] = []
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("extension") == ext_id:
                        out.append(entry)
        except OSError:
            pass
        return out[-limit:]

    @property
    def path(self) -> Path:
        return self._path
