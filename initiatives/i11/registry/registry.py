#!/usr/bin/env python3
"""Curated registry: publish only extensions that pass all four reviews.

Gates (each recorded with reviewer, verdict, date, notes):
  1. compatibility — manifest validates; entrypoint loads; declared
     actions resolve; policy-kit suite passes. (automated)
  2. security      — import scan clean; no network exfiltration path;
     package signature verifies; PII handling matches the declared mode.
     (automated)
  3. ux            — web card renders required states (empty/loading/
     blocked/error); confirmation prompts use plain language; docs exist.
     (human)
  4. governance    — adversarial suite fully blocked; audit chain
     verifies; confirm-required actions re-prompt through the host.
     (human)

An extension is installable from the registry only when all four gates
record ``pass``. The registry is a local JSON file by default (local-first);
a remote index is a distribution concern for Initiative 10.

Governance invariants (enforced in code, not just documented):
- ``record_gate`` accepts HUMAN gates only (ux, governance). Automated
  gates can only be changed through ``override_automated_gate``, which
  requires a named human reviewer, a non-empty justification, and an
  explicit human authorization reference — recorded in the audit trail.
- Resubmission is key-continuous and version-monotonic: the pinned
  publisher key may never change under an ext_id, and a version older
  than the registered one is rejected. Same-version resubmission
  re-runs the automated gates and preserves prior human verdicts; a
  newer version resets human gates to pending (re-review required).
- ``installable`` re-hashes the bytes at the registered source path and
  re-verifies the Ed25519 signature against the pinned key — the
  gatekeeper checks, it never delegates to a stale recorded hash.
- Gate verdicts are append-only audited: every change records who, when,
  the prior verdict, and the new verdict. The current ``gates`` dict is
  a projection of that history.
- The store fails loudly on corruption (``CorruptStoreError``), keeps a
  ``.bak`` of the last good state, and is written atomically under an
  exclusive lock. The registry refuses to run without a RevocationList.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..manifest.schema import load_manifest, manifest_summary
from ..policy_kit.attacks import run_adversarial_suite
from ..policy_kit.checks import run_policy_suite
from ..sandbox.host import scan_imports
from ..signing.sign import compare_versions, package_hash, verify_package
from ..signing.sign import RevocationList
from ..signing.store import SCHEMA_VERSION, CorruptStoreError, JsonStore

__all__ = [
    "GATES", "HUMAN_GATES", "AUTOMATED_GATES", "SCHEMA_VERSION",
    "CorruptStoreError", "GateReview", "RegistryEntry", "Registry",
]

GATES = ("compatibility", "security", "ux", "governance")
HUMAN_GATES = ("ux", "governance")
AUTOMATED_GATES = ("compatibility", "security")

# pubkey_provenance values: "pinned" (submitter supplied a key the
# signature verified against), "self-asserted" (sidecar's embedded key
# only — integrity, not provenance), "none" (unsigned).


@dataclass
class GateReview:
    gate: str
    verdict: str  # "pass" | "fail" | "pending"
    reviewer: str
    date: str
    notes: str = ""


@dataclass
class RegistryEntry:
    ext_id: str
    version: str
    package_sha256: str
    publisher: str
    publisher_pubkey: str = ""
    pubkey_provenance: str = "none"
    source_path: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    gate_history: list[dict[str, Any]] = field(default_factory=list)
    published_at: int = 0

    def gate_status(self) -> dict[str, str]:
        return {g: self.gates.get(g, {}).get("verdict", "pending")
                for g in GATES}

    def publishable(self) -> bool:
        return all(self.gate_status()[g] == "pass" for g in GATES)


class Registry:
    def __init__(self, path: str | Path,
                 revocations: RevocationList) -> None:
        # Fail closed: a registry without revocation checks is a registry
        # that can never unpublish. There is no "revocations=None" mode.
        if revocations is None:
            raise ValueError(
                "Registry requires a RevocationList; refusing to run "
                "without revocation checks (fail closed).")
        self._store = JsonStore(path, kind="registry", payload_key="entries",
                                empty=dict, payload_type=dict)
        self._revocations = revocations

    # -- storage ---------------------------------------------------------------
    def _read_all(self) -> dict[str, dict[str, Any]]:
        return self._store.read()

    def _write_all(self, data: dict[str, dict[str, Any]]) -> None:
        self._store.write(data)

    def _entry(self, ext_id: str) -> RegistryEntry | None:
        raw = self._read_all().get(ext_id)
        if raw is None:
            return None
        return RegistryEntry(**raw)

    def _save(self, entry: RegistryEntry) -> None:
        data = self._read_all()
        data[entry.ext_id] = asdict(entry)
        self._write_all(data)

    # -- gate audit ------------------------------------------------------------
    def _set_gate(self, entry: RegistryEntry, gate: str, verdict: str,
                  reviewer: str, date: str, notes: str = "",
                  *, override: bool = False,
                  authorized_by: str = "") -> None:
        """Set a gate verdict and append the change to the audit history.
        History is append-only: prior verdicts are never edited in place."""
        prior = entry.gates.get(gate, {}).get("verdict", "none")
        entry.gates[gate] = asdict(GateReview(
            gate=gate, verdict=verdict, reviewer=reviewer, date=date,
            notes=notes))
        record: dict[str, Any] = {
            "gate": gate, "prior_verdict": prior, "new_verdict": verdict,
            "reviewer": reviewer, "date": date, "notes": notes,
            "override": override, "recorded_at": int(time.time()),
        }
        if authorized_by:
            record["authorized_by"] = authorized_by
        entry.gate_history.append(record)

    # -- submission ---------------------------------------------------------------
    def submit(self, ext_dir: str | Path, *, publisher: str,
               publisher_pubkey: str = "") -> dict[str, Any]:
        """Submit an extension for review. Runs the automated gates
        (compatibility, security); ux and governance need human reviewers
        and stay pending until recorded.

        Key continuity: if this ext_id was submitted before, the pinned
        publisher key must match — a key substitution is rejected.
        Version monotonicity: resubmitting an older version than the
        registered one is rejected.

        Resubmission policy (documented choice): a same-version
        resubmission re-runs the automated gates and PRESERVES prior
        human verdicts (the code under review did not change); a newer
        version resets human gates to pending (new code needs re-review).

        ``publisher_pubkey`` pins the publisher's Ed25519 public key: the
        signature is verified against the PINNED key (real provenance).
        Without it, verification falls back to the sidecar's embedded
        pubkey and the entry's ``pubkey_provenance`` is honestly recorded
        as ``"self-asserted"`` — integrity only, never presented as pinned.
        """
        ext_dir = Path(ext_dir)
        try:
            manifest = load_manifest(ext_dir)
        except Exception as exc:
            return {"ok": False, "error": f"manifest invalid: {exc}"}
        if self._revocations.is_revoked(manifest["id"],
                                        manifest["version"]):
            return {"ok": False,
                    "error": "this version was revoked; cannot submit"}

        ext_id = manifest["id"]
        new_version = manifest["version"]
        existing = self._entry(ext_id)
        prior_history = list(existing.gate_history) if existing else []

        # Key continuity: the pin, once set, is the identity of the
        # publisher for this ext_id. A different key is a substitution
        # attack, not an update.
        if existing and existing.publisher_pubkey:
            if (publisher_pubkey
                    and publisher_pubkey != existing.publisher_pubkey):
                return {"ok": False,
                        "error": "publisher key substitution rejected: "
                                 "the pinned key for this ext_id does not "
                                 "match the submitted key"}
            effective_pin = existing.publisher_pubkey
        else:
            effective_pin = publisher_pubkey

        # Version monotonicity: downgrades are rejected outright.
        if existing:
            cmp = compare_versions(new_version, existing.version)
            if cmp < 0:
                return {"ok": False,
                        "error": f"version downgrade rejected: {new_version} "
                                 f"is older than registered {existing.version}"}
            same_version = (cmp == 0)
        else:
            same_version = False

        # Automated gates --------------------------------------------------
        suite = run_policy_suite(ext_dir)
        sig = verify_package(
            ext_dir, public_key=effective_pin or None)
        scan = scan_imports(ext_dir)
        adv = run_adversarial_suite()

        entry = RegistryEntry(
            ext_id=ext_id, version=new_version,
            package_sha256=sig.get("package_sha256", ""),
            publisher=publisher,
            publisher_pubkey=effective_pin or str(sig.get("pubkey", "")),
            pubkey_provenance=("pinned" if effective_pin
                               else ("self-asserted" if sig.get("pubkey")
                                     else "none")),
            source_path=str(ext_dir.resolve()),
            capabilities=manifest_summary(manifest),
            gate_history=prior_history,
            published_at=int(time.time()),
        )
        today = time.strftime("%Y-%m-%d")

        self._set_gate(
            entry, "compatibility",
            "pass" if suite["failed"] == 0 else "fail",
            reviewer="policy-kit (automated)", date=today,
            notes=(f"{suite['passed']}/{suite['passed'] + suite['failed']} "
                   f"policy checks passed"))

        sec_notes = []
        sec_ok = True
        if not scan["ok"]:
            sec_ok = False
            sec_notes.append(f"banned imports: {scan['violations']}")
        if not sig["ok"]:
            sec_ok = False
            sec_notes.append(f"signature: {sig['error']}")
        if adv["breached"]:
            sec_ok = False
            sec_notes.append(f"adversarial breaches: {adv['breached']}")
        self._set_gate(
            entry, "security", "pass" if sec_ok else "fail",
            reviewer="policy-kit + signature check (automated)", date=today,
            notes="; ".join(sec_notes) or
                  f"import scan clean, signature ok, "
                  f"{adv['blocked']}/{adv['attacks']} attacks blocked")

        # Human gates -------------------------------------------------------
        for gate in HUMAN_GATES:
            preserved = (existing and same_version
                         and existing.gates.get(gate, {}).get("verdict")
                         in ("pass", "fail"))
            if preserved:
                prev = existing.gates[gate]
                self._set_gate(entry, gate, prev["verdict"],
                               reviewer=prev.get("reviewer", ""),
                               date=prev.get("date", ""),
                               notes=(prev.get("notes", "")
                                      + " [preserved on same-version "
                                        "resubmission]"))
            else:
                reason = ("new version requires re-review"
                          if existing and not same_version
                          else "awaiting human review")
                self._set_gate(entry, gate, "pending", reviewer="",
                               date="", notes=reason)

        self._save(entry)
        return {"ok": True, "entry": asdict(entry),
                "publishable": entry.publishable(),
                "pubkey_provenance": entry.pubkey_provenance,
                "note": "ux and governance gates require human review "
                        "before this extension is installable"}

    def record_gate(self, ext_id: str, gate: str, *, verdict: str,
                    reviewer: str, notes: str = "") -> dict[str, Any]:
        """Record a human review verdict — for HUMAN gates only
        (ux, governance).

        Automated gates (compatibility, security) are REFUSED here: a
        failed automated security gate must never be one ``record_gate``
        call away from ``pass``. Automated-gate changes require
        ``override_automated_gate`` with explicit human authorization.
        """
        if gate not in GATES:
            return {"ok": False, "error": f"unknown gate {gate!r}"}
        if gate in AUTOMATED_GATES:
            return {"ok": False,
                    "error": f"gate {gate!r} is automated; record_gate is "
                             "restricted to human gates (ux, governance). "
                             "Use override_automated_gate with explicit "
                             "human authorization."}
        if verdict not in ("pass", "fail"):
            return {"ok": False, "error": "verdict must be 'pass' or 'fail'"}
        if not reviewer or not reviewer.strip():
            return {"ok": False,
                    "error": "a named human reviewer is required"}
        entry = self._entry(ext_id)
        if entry is None:
            return {"ok": False, "error": f"{ext_id!r} not submitted"}
        self._set_gate(entry, gate, verdict, reviewer=reviewer.strip(),
                       date=time.strftime("%Y-%m-%d"), notes=notes)
        self._save(entry)
        return {"ok": True, "publishable": entry.publishable(),
                "gates": entry.gate_status()}

    def override_automated_gate(self, ext_id: str, gate: str, *,
                                verdict: str, reviewer: str, notes: str,
                                authorized_by: str) -> dict[str, Any]:
        """Separately-audited path for changing an AUTOMATED gate verdict.

        This exists because automated results are sometimes wrong (broken
        scanner, false positive) — but flipping one must be a deliberate,
        attributable human act, never a quiet ``record_gate``. Requires:
        a named human reviewer (not an automated one), a non-empty
        justification in ``notes``, and ``authorized_by`` — an explicit
        reference to the human authority for the override. The override is
        flagged in the append-only gate history.
        """
        if gate not in AUTOMATED_GATES:
            return {"ok": False,
                    "error": f"gate {gate!r} is not automated; "
                             "override_automated_gate is only for "
                             f"{AUTOMATED_GATES}. Use record_gate for human "
                             "gates."}
        if verdict not in ("pass", "fail"):
            return {"ok": False, "error": "verdict must be 'pass' or 'fail'"}
        if not reviewer or not reviewer.strip():
            return {"ok": False,
                    "error": "a named human reviewer is required"}
        if "(automated)" in reviewer:
            return {"ok": False,
                    "error": "override requires a human reviewer, not an "
                             "automated one"}
        if not notes or not notes.strip():
            return {"ok": False,
                    "error": "an override requires a written justification "
                             "in notes"}
        if not authorized_by or not authorized_by.strip():
            return {"ok": False,
                    "error": "an override requires explicit human "
                             "authorization (authorized_by)"}
        entry = self._entry(ext_id)
        if entry is None:
            return {"ok": False, "error": f"{ext_id!r} not submitted"}
        self._set_gate(entry, gate, verdict, reviewer=reviewer.strip(),
                       date=time.strftime("%Y-%m-%d"),
                       notes=notes.strip(), override=True,
                       authorized_by=authorized_by.strip())
        self._save(entry)
        return {"ok": True, "publishable": entry.publishable(),
                "gates": entry.gate_status(),
                "note": "automated-gate override recorded with human "
                        "authorization; see gate_history"}

    # -- install ---------------------------------------------------------------------
    def installable(self, ext_id: str) -> dict[str, Any]:
        """The gatekeeper checks; it never delegates to stale state.

        Beyond the recorded gate verdicts and the revocation list, this
        re-hashes the bytes at the registered source path and re-verifies
        the Ed25519 signature against the PINNED publisher key. A package
        whose bytes changed after registration — or whose key no longer
        verifies — is not installable even if the registry says pass.
        """
        entry = self._entry(ext_id)
        if entry is None:
            return {"ok": False, "error": f"{ext_id!r} not in registry"}
        if self._revocations.is_revoked(entry.ext_id, entry.version):
            return {"ok": False, "error": "this version was revoked"}
        if not entry.publishable():
            pending = [g for g, v in entry.gate_status().items()
                       if v != "pass"]
            return {"ok": False,
                    "error": f"gates not passed: {pending}; "
                             "curated registry publishes only fully-reviewed "
                             "extensions"}
        source = Path(entry.source_path)
        if not source.is_dir():
            return {"ok": False,
                    "error": f"registered source path {entry.source_path!r} "
                             "is missing; refusing to install from memory "
                             "of a hash"}
        current_hash = package_hash(source)
        if current_hash != entry.package_sha256:
            return {"ok": False,
                    "error": "package bytes changed after registration "
                             f"(registered {entry.package_sha256[:12]}…, "
                             f"now {current_hash[:12]}…); refusing install"}
        ver = verify_package(source,
                             public_key=entry.publisher_pubkey or None)
        if not ver["ok"]:
            return {"ok": False,
                    "error": "install-time signature verification failed "
                             f"against the pinned key: {ver['error']}"}
        return {"ok": True, "source_path": entry.source_path,
                "version": entry.version,
                "package_sha256": current_hash,
                "publisher_pubkey": entry.publisher_pubkey,
                "pubkey_provenance": entry.pubkey_provenance,
                "reverified": True}

    # -- revocation ------------------------------------------------------------------
    def revoke(self, ext_id: str, version: str, reason: str, *,
               signature: str) -> dict[str, Any]:
        """Revoke a version, authenticated against the PINNED publisher key
        for this ext_id. ``signature`` must be the hex Ed25519 signature by
        the pinned key holder over
        ``revocation_payload(ext_id, version, reason)`` (see signing).
        Refuses when the entry has no pinned key or the signature does not
        verify — there is no anonymous revocation path."""
        entry = self._entry(ext_id)
        if entry is None:
            return {"ok": False, "error": f"{ext_id!r} not in registry"}
        if entry.pubkey_provenance != "pinned" or not entry.publisher_pubkey:
            return {"ok": False,
                    "error": "revocation requires a pinned publisher key; "
                             f"{ext_id!r} was submitted with "
                             f"{entry.pubkey_provenance} provenance"}
        return self._revocations.revoke(
            ext_id, version, reason,
            publisher_pubkey=entry.publisher_pubkey, signature=signature)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for raw in self._read_all().values():
            entry = RegistryEntry(**raw)
            out.append({"ext_id": entry.ext_id, "version": entry.version,
                        "publisher": entry.publisher,
                        "pubkey_provenance": entry.pubkey_provenance,
                        "gates": entry.gate_status(),
                        "publishable": entry.publishable()})
        return sorted(out, key=lambda e: e["ext_id"])

    def gate_history(self, ext_id: str) -> list[dict[str, Any]] | None:
        """Append-only audit history of gate verdict changes for an entry."""
        entry = self._entry(ext_id)
        return list(entry.gate_history) if entry else None
