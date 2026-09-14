#!/usr/bin/env python3
"""Sandboxed filesystem: extensions may only touch declared roots.

Deny-by-default. Every path is resolved (symlinks included) and must stay
inside one of the manifest-declared ``fs_roots`` (relative to the extension
directory) or the extension's own private ``.data`` directory. Path
traversal (``..``, absolute paths, symlinks escaping the root) raises
``SandboxViolation``. Violations are reported to the host audit log by the
caller — this module never writes audit entries itself.
"""

from __future__ import annotations

from pathlib import Path


class SandboxViolation(PermissionError):
    """An extension attempted filesystem access outside its declared roots."""


class QuotaExceeded(PermissionError):
    """An extension exceeded its bounded filesystem write quota."""


#: Bounded blast radius: per-file and per-extension (session) write caps.
MAX_WRITE_BYTES_PER_FILE = 1 * 1024 * 1024  # 1 MiB
MAX_WRITE_BYTES_TOTAL = 10 * 1024 * 1024  # 10 MiB per loaded extension


class SandboxFS:
    """Root-scoped file access for one loaded extension."""

    def __init__(self, ext_dir: str | Path, fs_roots: list[str]) -> None:
        self._ext_dir = Path(ext_dir).resolve()
        self._roots: list[Path] = []
        for root in fs_roots:
            candidate = (self._ext_dir / root).resolve()
            self._assert_inside(candidate, self._ext_dir,
                                f"fs_root {root!r} escapes the extension directory")
            self._roots.append(candidate)
        # Private per-extension data dir is always available.
        self._data_dir = (self._ext_dir / ".data").resolve()
        self._bytes_written = 0

    @staticmethod
    def _assert_inside(path: Path, root: Path, message: str) -> None:
        try:
            path.relative_to(root)
        except ValueError:
            raise SandboxViolation(message) from None

    def _resolve(self, rel: str | Path, *, write: bool = False) -> Path:
        rel_path = Path(rel)
        if rel_path.is_absolute():
            raise SandboxViolation(f"absolute paths are forbidden: {rel!r}")
        if ".." in rel_path.parts:
            raise SandboxViolation(f"'..' is forbidden in sandbox paths: {rel!r}")
        # Resolve against each root in order; the first root that contains
        # the resolved path wins. resolve() follows symlinks, so a symlink
        # pointing outside the root is caught by the containment check.
        for root in (self._data_dir, *self._roots):
            candidate = (root / rel_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            # For writes, the parent must exist inside the root; the file
            # itself may not exist yet.
            anchor = candidate if not write else candidate.parent
            try:
                anchor.relative_to(root)
            except ValueError:
                continue
            return candidate
        raise SandboxViolation(
            f"path {rel!r} is outside the declared fs_roots "
            f"{[str(r) for r in self._roots]}"
        )

    def _check_quota(self, nbytes: int) -> None:
        if nbytes > MAX_WRITE_BYTES_PER_FILE:
            raise QuotaExceeded(
                f"single write of {nbytes} bytes exceeds the per-file cap "
                f"({MAX_WRITE_BYTES_PER_FILE})")
        if self._bytes_written + nbytes > MAX_WRITE_BYTES_TOTAL:
            raise QuotaExceeded(
                f"extension write quota exceeded "
                f"({MAX_WRITE_BYTES_TOTAL} bytes total per load)")

    def read_text(self, rel: str | Path) -> str:
        return self._resolve(rel).read_text(encoding="utf-8")

    def write_text(self, rel: str | Path, content: str) -> None:
        data = content.encode("utf-8")
        self._check_quota(len(data))
        target = self._resolve(rel, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._bytes_written += len(data)

    def read_bytes(self, rel: str | Path) -> bytes:
        return self._resolve(rel).read_bytes()

    def write_bytes(self, rel: str | Path, content: bytes) -> None:
        self._check_quota(len(content))
        target = self._resolve(rel, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self._bytes_written += len(content)

    def exists(self, rel: str | Path) -> bool:
        try:
            return self._resolve(rel).exists()
        except SandboxViolation:
            return False

    def list_dir(self, rel: str | Path = ".") -> list[str]:
        target = self._resolve(rel)
        if not target.is_dir():
            raise SandboxViolation(f"not a directory: {rel!r}")
        return sorted(p.name for p in target.iterdir())

    @property
    def roots(self) -> list[str]:
        return [str(r) for r in self._roots]
