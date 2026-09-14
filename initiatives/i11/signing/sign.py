#!/usr/bin/env python3
"""Signed packages: provenance, version pinning, upgrade review, revocation.

Threat model: a signed package proves *who published what bytes* and
*which version is installed*. It does not make code trustworthy by
itself — the registry's four review gates (registry/) do that. Signing
makes substitution, downgrade, and silent-upgrade attacks detectable.

Mechanics:
- ``package_hash(ext_dir)``: deterministic sha256 over the extension's
  files (sorted relative paths + bytes), excluding ``.data/`` and
  signature sidecars.
- ``sign_package`` / ``verify_package``: Ed25519 over the package hash.
  Asymmetric signatures give real publisher provenance: anyone can
  verify with the publisher's public key, and only the private-key
  holder can sign. The registry pins each publisher's public key, so
  install-time verification is against the PINNED key, not a
  self-asserted one.
- ``InstallRecord``: what is installed where, pinned to an exact
  version + hash. Upgrades require a complete review record (reviewer,
  verdict, date, notes) and a strictly newer version; the prior exact
  hash and bytes location are retained so a bad upgrade can be rolled
  back.
- ``RevocationList``: append-only; revoked (id, version) pairs refuse to
  install or load. Revocation is authenticated: entries require an
  Ed25519 signature by the pinned publisher key over the revocation
  payload — no anonymous/free-string revocation.

All three JSON stores are versioned, atomically written, keep a
``.bak`` of the last good state, and FAIL LOUDLY (``CorruptStoreError``)
on corruption instead of silently returning empty state.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)

from .store import SCHEMA_VERSION, CorruptStoreError, JsonStore

__all__ = [
    "SIGNATURE_FILE", "SIGNING_ALG", "SCHEMA_VERSION", "CorruptStoreError",
    "compare_versions", "package_hash", "generate_keypair",
    "sign_package", "verify_package",
    "revocation_payload", "sign_revocation",
    "InstallRecord", "InstallStore", "RevocationList",
]

SIGNATURE_FILE = ".veto-signature.json"
SIGNING_ALG = "ed25519-v1"

_EXCLUDE_DIRS = {".data", "__pycache__", ".git"}
_EXCLUDE_FILES = {SIGNATURE_FILE}


def package_hash(ext_dir: str | Path) -> str:
    """Deterministic sha256 over extension file bytes (sorted paths)."""
    ext_dir = Path(ext_dir)
    digest = hashlib.sha256()
    files = sorted(
        p for p in ext_dir.rglob("*")
        if p.is_file()
        and p.name not in _EXCLUDE_FILES
        and not any(part in _EXCLUDE_DIRS for part in p.relative_to(ext_dir).parts)
    )
    for path in files:
        rel = str(path.relative_to(ext_dir)).encode("utf-8")
        digest.update(len(rel).to_bytes(4, "big") + rel)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 publisher keypair.

    Returns (private_pem, public_hex). The private PEM is for the
    publisher's secure storage; the public hex is published/pinned in
    the registry.
    """
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode("ascii")
    public_hex = public.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw).hex()
    return private_pem, public_hex


def _load_private(pem_or_key: str | Ed25519PrivateKey) -> Ed25519PrivateKey:
    if isinstance(pem_or_key, Ed25519PrivateKey):
        return pem_or_key
    return serialization.load_pem_private_key(
        pem_or_key.encode("ascii"), password=None)


def _load_public(hex_or_key: str | Ed25519PublicKey) -> Ed25519PublicKey:
    if isinstance(hex_or_key, Ed25519PublicKey):
        return hex_or_key
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hex_or_key))


def _dev_private_key() -> Ed25519PrivateKey:
    """Local-dev publisher key. Production publishers use managed keys;
    the registry pins each publisher's public key.

    Fails loudly if the key cannot be persisted: signing with an
    ephemeral key nobody can recover would make every signature from
    this machine unverifiable after restart, so we refuse instead of
    silently falling back.
    """
    key_file = Path.home() / ".veto" / "publisher_ed25519.pem"
    if key_file.is_file():
        # Let load errors propagate: a broken key file must not be
        # silently replaced by a fresh identity.
        return serialization.load_pem_private_key(
            key_file.read_bytes(), password=None)
    private = Ed25519PrivateKey.generate()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(private.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
    except OSError as exc:
        raise RuntimeError(
            f"refusing to sign with an ephemeral dev key: cannot persist "
            f"the publisher key at {key_file}: {exc}. Fix permissions on "
            f"~/.veto/ or supply an explicit private_key.") from exc
    return private


def compare_versions(a: str, b: str) -> int:
    """Compare dotted version strings. Returns -1 if a < b, 0 if equal,
    1 if a > b. Segments compare numerically when numeric, lexically
    otherwise; missing segments count as 0 (``1.0`` == ``1.0.0``)."""
    def parts(v: str) -> list[int | str]:
        out: list[int | str] = []
        for seg in str(v).split("."):
            out.append(int(seg) if seg.isdigit() else seg)
        return out

    pa, pb = parts(a), parts(b)
    for x, y in zip(pa, pb):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        return -1 if str(x) < str(y) else 1
    if len(pa) == len(pb):
        return 0
    longer, sign = (pa, 1) if len(pa) > len(pb) else (pb, -1)
    rest = longer[min(len(pa), len(pb)):]
    if all(r == 0 for r in rest):
        return 0
    return sign


def sign_package(ext_dir: str | Path, *, publisher: str,
                 private_key: str | Ed25519PrivateKey | None = None
                 ) -> dict[str, Any]:
    """Hash the package and write the Ed25519 signature sidecar."""
    ext_dir = Path(ext_dir)
    pkg_hash = package_hash(ext_dir)
    private = (_load_private(private_key) if private_key is not None
               else _dev_private_key())
    sig = private.sign(bytes.fromhex(pkg_hash)).hex()
    pubkey_hex = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    envelope = {
        "alg": SIGNING_ALG,
        "package_sha256": pkg_hash,
        "publisher": publisher,
        "pubkey": pubkey_hex,
        "signed_at": int(time.time()),
        "sig": sig,
    }
    (ext_dir / SIGNATURE_FILE).write_text(
        json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return envelope


def verify_package(ext_dir: str | Path, *,
                   public_key: str | Ed25519PublicKey | None = None
                   ) -> dict[str, Any]:
    """Verify the signature sidecar against current file bytes.

    If ``public_key`` is given, the signature must verify against that
    PINNED key (registry install path — real provenance). If omitted,
    the sidecar's embedded pubkey is used (self-asserted: proves the
    bytes are unchanged since signing, but not who signed).
    """
    ext_dir = Path(ext_dir)
    sidecar = ext_dir / SIGNATURE_FILE
    if not sidecar.is_file():
        return {"ok": False, "error": "no signature sidecar; package unsigned"}
    try:
        envelope = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"unreadable signature: {exc}"}
    if envelope.get("alg") != SIGNING_ALG:
        return {"ok": False,
                "error": f"unsupported signing alg {envelope.get('alg')!r}"}
    current = package_hash(ext_dir)
    if current != str(envelope.get("package_sha256", "")):
        return {"ok": False, "error": "package bytes changed after signing"}
    key_hex = public_key if public_key is not None else envelope.get("pubkey")
    if not key_hex:
        return {"ok": False, "error": "no public key to verify against"}
    try:
        public = _load_public(key_hex)
        public.verify(bytes.fromhex(str(envelope.get("sig", ""))),
                      bytes.fromhex(current))
    except Exception as exc:
        return {"ok": False,
                "error": f"signature invalid: {exc}"}
    shown_pubkey = (key_hex if isinstance(key_hex, str)
                    else public.public_bytes(
                        serialization.Encoding.Raw,
                        serialization.PublicFormat.Raw).hex())
    return {"ok": True, "publisher": envelope.get("publisher"),
            "package_sha256": current,
            "pubkey": shown_pubkey,
            "pinned": public_key is not None,
            "signed_at": envelope.get("signed_at")}


# ---------------------------------------------------------------------------
# Install records: version pinning + upgrade review
# ---------------------------------------------------------------------------

@dataclass
class InstallRecord:
    ext_id: str
    version: str
    package_sha256: str
    publisher: str
    installed_at: int
    source_path: str = ""
    upgraded_from: str | None = None
    upgrade_review: dict[str, Any] = field(default_factory=dict)
    # Rollback retention: the exact prior (version, hash, bytes location)
    # survives an upgrade so a bad release can be rolled back to
    # known-good bytes. Set automatically by record_install/record_upgrade.
    previous_package_sha256: str | None = None
    previous_source_path: str = ""


def _validate_upgrade_review(review: Any) -> str | None:
    """Return an error string if the upgrade review is incomplete."""
    if not isinstance(review, dict) or not review:
        return ("upgrade requires a review record "
                "{reviewer, verdict: 'pass', date, notes}")
    if review.get("verdict") != "pass":
        return ("upgrade refused: review verdict is "
                f"{review.get('verdict')!r}, need 'pass'")
    for field in ("reviewer", "date", "notes"):
        value = review.get(field)
        if not isinstance(value, str) or not value.strip():
            return (f"upgrade refused: review {field!r} is required and "
                    "must be a non-empty string")
    try:
        time.strptime(review["date"], "%Y-%m-%d")
    except ValueError:
        return ("upgrade refused: review 'date' must be YYYY-MM-DD, got "
                f"{review['date']!r}")
    return None


class InstallStore:
    """Pin installed extensions to exact (version, hash).

    Backed by a versioned, locked, fail-loud JSON store (see store.py).
    Upgrades keep the prior exact hash and bytes location so a bad
    release can be rolled back to known-good bytes.

    ``revocations`` is the shared ``RevocationList`` this store checks
    before restoring a prior package (wired by whoever owns the
    deployment's stores — e.g. the Registry, which already requires one).
    Passing it by constructor keeps the store's plain-JSON design (no
    import of the registry layer, which depends on *this* module).
    Production callers MUST pass it: ``rollback()`` fails closed without
    a list, because restoring a version it cannot prove non-revoked
    would silently violate this unit's own invariant ("revoked (id,
    version) pairs refuse to install or load").

    Trust assumption on hashes: ``package_sha256`` values recorded here
    are claims supplied by the caller — they are NOT cryptographically
    bound to a signature at record time. This store is install
    bookkeeping, not provenance. Authenticity enforcement lives at the
    registry layer: ``Registry.installable()`` re-hashes the extension's
    bytes and verifies the signature against the PINNED publisher key
    before anything counts as installable. Never treat a recorded hash
    alone as proof of who published the bytes.
    """

    def __init__(self, path: str | Path,
                 revocations: RevocationList | None = None) -> None:
        self._store = JsonStore(path, kind="install", payload_key="records",
                                empty=dict, payload_type=dict)
        self._revocations = revocations

    def _read_all(self) -> dict[str, dict[str, Any]]:
        return self._store.read()

    def _write_all(self, data: dict[str, dict[str, Any]]) -> None:
        self._store.write(data)

    def record_install(self, rec: InstallRecord) -> None:
        """Record an install (or an upgrade/rollback, which route through
        here). See the class docstring for the trust assumption: the
        recorded hash is a caller-supplied claim, not a signature-bound
        fact; authenticity is enforced at the registry layer."""
        # Retain the prior exact package (hash + bytes location) so a
        # bad upgrade can be rolled back. Explicitly-provided previous_*
        # fields (from record_upgrade/rollback) win over the stored ones.
        old = self._read_all().get(rec.ext_id)
        if old:
            if rec.previous_package_sha256 is None:
                rec.previous_package_sha256 = old.get("package_sha256")
            if not rec.previous_source_path:
                rec.previous_source_path = old.get("source_path", "")
            if rec.upgraded_from is None:
                rec.upgraded_from = old.get("version")
        data = self._read_all()
        data[rec.ext_id] = asdict(rec)
        self._write_all(data)

    def get(self, ext_id: str) -> dict[str, Any] | None:
        return self._read_all().get(ext_id)

    def record_upgrade(self, ext_id: str, new_version: str,
                       new_hash: str, review: dict[str, Any],
                       source_path: str = "") -> dict[str, Any]:
        """Record an upgrade. The review must be complete:
        {reviewer, verdict: 'pass', date (YYYY-MM-DD), notes} — all
        non-empty. ``new_version`` must be strictly greater than the
        installed version: a downgrade can never be recorded as an
        upgrade. The prior exact hash and bytes location are retained
        for rollback."""
        err = _validate_upgrade_review(review)
        if err:
            return {"ok": False, "error": err}
        current = self.get(ext_id)
        if current is None:
            return {"ok": False,
                    "error": f"{ext_id!r} is not installed; use install, "
                             "not upgrade"}
        if compare_versions(new_version, current.get("version", "")) <= 0:
            return {"ok": False,
                    "error": f"refusing downgrade-as-upgrade: {new_version} "
                             f"is not newer than installed "
                             f"{current.get('version')!r}"}
        if not new_hash or not isinstance(new_hash, str):
            return {"ok": False, "error": "new package hash is required"}
        rec = InstallRecord(
            ext_id=ext_id, version=new_version, package_sha256=new_hash,
            publisher=current.get("publisher", ""),
            source_path=source_path or current.get("source_path", ""),
            installed_at=int(time.time()),
            upgraded_from=current.get("version"),
            upgrade_review=dict(review),
            previous_package_sha256=current.get("package_sha256"),
            previous_source_path=current.get("source_path", ""),
        )
        self.record_install(rec)
        return {"ok": True, "from": current.get("version"), "to": new_version}

    def rollback(self, ext_id: str) -> dict[str, Any]:
        """Roll back to the retained prior package (exact hash + bytes
        location). Refuses when no prior package was retained.

        The target version is checked against the ``RevocationList``
        given at construction, and rollback is REFUSED when the target
        (id, version) pair has been revoked — a rollback is functionally
        a re-install of that version, and this unit's invariant is that
        revoked pairs refuse to install or load. With no revocation
        list wired in, rollback refuses fail-closed: restoring a version
        it cannot prove non-revoked would silently violate the invariant.

        Design trade-off (deliberate): refusal rather than a
        human-acknowledgment escape hatch. An acknowledgment path would
        be a human override around the same invariant we refuse to bend
        at install/load time, and ``RevocationList`` is append-only with
        no un-revoke path — revocation is terminal by design. An
        operator who genuinely needs the bytes can restore them outside
        the governed install path; this store will not present a revoked
        version as installed.

        The stored record says what actually happened
        (``verdict: "restored-previous-version"`` with the revocation
        check outcome) — rollback never synthesizes a passing review.
        """
        current = self.get(ext_id)
        if current is None:
            return {"ok": False, "error": f"{ext_id!r} is not installed"}
        prev_hash = current.get("previous_package_sha256")
        if not prev_hash:
            return {"ok": False,
                    "error": f"{ext_id!r} has no retained prior package; "
                             "cannot roll back"}
        target_version = current.get("upgraded_from") or current.get("version")
        if self._revocations is None:
            return {"ok": False,
                    "error": f"rollback refused: this InstallStore was "
                             "constructed without a RevocationList, so the "
                             f"target version ({ext_id} {target_version}) "
                             "cannot be proven non-revoked; construct "
                             "InstallStore(path, revocations=RevocationList) "
                             "to enable rollback"}
        if self._revocations.is_revoked(ext_id, target_version):
            entry = next(
                (e for e in self._revocations.list()
                 if e.get("ext_id") == ext_id
                 and compare_versions(str(e.get("version", "")),
                                      str(target_version)) == 0), {})
            return {"ok": False,
                    "error": (
                        f"rollback refused: {ext_id} {target_version} is "
                        f"REVOKED (reason: {entry.get('reason')!r}; "
                        f"revoked_by: {str(entry.get('revoked_by'))[:16]}...; "
                        f"revoked_at: {entry.get('revoked_at')}) — a "
                        "revoked (id, version) pair is never restored")}
        rec = InstallRecord(
            ext_id=ext_id,
            version=target_version,
            package_sha256=prev_hash,
            publisher=current.get("publisher", ""),
            source_path=(current.get("previous_source_path")
                         or current.get("source_path", "")),
            installed_at=int(time.time()),
            upgraded_from=current.get("version"),
            upgrade_review={
                "reviewer": "rollback",
                "verdict": "restored-previous-version",
                "date": time.strftime("%Y-%m-%d"),
                "notes": (f"restored retained prior package "
                          f"{target_version} after {current.get('version')}; "
                          "no new review was performed"),
                "revocation_check": (
                    f"passed: {ext_id} {target_version} is not revoked"),
            },
            # The bad release becomes the "prior" so a second rollback
            # returns to it (roll forward).
            previous_package_sha256=current.get("package_sha256"),
            previous_source_path=current.get("source_path", ""),
        )
        self.record_install(rec)
        return {"ok": True, "from": current.get("version"),
                "to": rec.version}


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

def revocation_payload(ext_id: str, version: str, reason: str) -> bytes:
    """Canonical bytes a publisher signs to authorize a revocation."""
    return json.dumps(
        {"ext_id": ext_id, "version": version, "reason": reason},
        sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_revocation(private_key: str | Ed25519PrivateKey, ext_id: str,
                    version: str, reason: str) -> str:
    """Sign a revocation payload with the publisher's private key.
    Returns the signature as hex."""
    return _load_private(private_key).sign(
        revocation_payload(ext_id, version, reason)).hex()


class RevocationList:
    """Append-only revocation list. Revoked (id, version) pairs refuse to
    install or load.

    Revocation is authenticated, not a free string: ``revoke`` requires
    an Ed25519 signature over the canonical revocation payload made by
    the holder of ``publisher_pubkey`` (the registry passes the key it
    pinned for that ext_id — see ``Registry.revoke`` — so only the
    publisher who owns the pinning can revoke). Entries record the
    authorizing pubkey and the signature, never a bare name.
    """

    def __init__(self, path: str | Path) -> None:
        self._store = JsonStore(path, kind="revocation",
                                payload_key="revocations", empty=list,
                                payload_type=list)

    def _read_all(self) -> list[dict[str, Any]]:
        return self._store.read()

    def revoke(self, ext_id: str, version: str, reason: str, *,
               publisher_pubkey: str, signature: str) -> dict[str, Any]:
        """Append a revocation entry. ``signature`` must be the hex
        Ed25519 signature by the holder of ``publisher_pubkey`` over
        ``revocation_payload(ext_id, version, reason)``; otherwise the
        revocation is refused. Returns ``{"ok": False, ...}`` on failure
        (never raises for a bad signature) so callers can report it."""
        if not publisher_pubkey or not signature:
            return {"ok": False,
                    "error": "revocation requires publisher_pubkey and a "
                             "signature over the revocation payload; "
                             "anonymous revocation is refused"}
        try:
            public = Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(publisher_pubkey))
            public.verify(bytes.fromhex(signature),
                          revocation_payload(ext_id, version, reason))
        except Exception as exc:
            return {"ok": False,
                    "error": f"revocation not authorized: signature does "
                             f"not verify against the publisher key: {exc}"}
        entries = self._read_all()
        entry = {"ext_id": ext_id, "version": version, "reason": reason,
                 "revoked_by": publisher_pubkey, "signature": signature,
                 "revoked_at": int(time.time())}
        entries.append(entry)
        self._store.write(entries)
        return {"ok": True, "entry": entry}

    def is_revoked(self, ext_id: str, version: str) -> bool:
        """Whether (ext_id, version) is revoked. Version equality uses
        the module's ``compare_versions`` semantics — ``'1.0'`` equals
        ``'1.0.0'`` — NOT raw string equality. A revocation recorded
        under one spelling of a version must block every spelling of
        the same version; exact-string matching was a bypass vector
        (revoke '1.0.0', restore '1.0') across rollback/install/load.

        Deliberate: entries are recorded verbatim (the Ed25519
        revocation signature binds the canonical payload including the
        verbatim version string), and normalization lives only on the
        check side. This keeps ``is_revoked`` coherent for all callers
        — rollback, submit, installable, and crew install — with no
        change to the trust-verification machinery."""
        return any(
            e.get("ext_id") == ext_id
            and compare_versions(str(e.get("version", "")),
                                 str(version)) == 0
            for e in self._read_all())

    def list(self) -> list[dict[str, Any]]:
        return self._read_all()
