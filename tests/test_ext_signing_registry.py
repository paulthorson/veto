#!/usr/bin/env python3
"""Signed packages + curated registry tests (Initiative 11, Epics 5-6).

Covers the KICK_BACK rework: fail-loud corrupt stores, human-gate-only
record_gate with an explicit override path, install-time re-verification,
key continuity + version monotonicity on resubmit, validated upgrade
reviews, authenticated revocation, rollback retention, append-only gate
audit, schema versions, and honest key provenance.
"""

import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i11.manifest.schema import load_manifest
from initiatives.i11.registry.registry import (
    GATES, HUMAN_GATES, AUTOMATED_GATES, Registry,
)
from initiatives.i11.signing.sign import (
    InstallRecord, InstallStore, RevocationList, generate_keypair,
    package_hash, revocation_payload, sign_package, sign_revocation,
    verify_package,
)
from initiatives.i11.signing.store import CorruptStoreError, SCHEMA_VERSION

REF_EXT = Path("initiatives/i11/reference/role-radar-digest")
_PRIV, _PUB = generate_keypair()
_OTHER_PRIV, _OTHER_PUB = generate_keypair()


def _copy_ref() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="veto-reg-"))
    import shutil
    shutil.copytree(REF_EXT, tmp / "ext")
    # Drop any signature sidecar so each test controls signing itself.
    (tmp / "ext" / ".veto-signature.json").unlink(missing_ok=True)
    return tmp / "ext"


def _set_version(ext: Path, version: str) -> None:
    manifest_path = ext / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2),
                             encoding="utf-8")


def _signed(ext, key=_PRIV, publisher="tester"):
    return sign_package(ext, publisher=publisher, private_key=key)


class TestSigning(unittest.TestCase):
    def test_sign_and_verify(self):
        ext = _copy_ref()
        env = _signed(ext)
        self.assertIn("package_sha256", env)
        self.assertEqual(env["alg"], "ed25519-v1")
        verdict = verify_package(ext, public_key=_PUB)
        self.assertTrue(verdict["ok"], verdict)
        self.assertTrue(verdict["pinned"])

    def test_tamper_detected(self):
        ext = _copy_ref()
        _signed(ext)
        (ext / "extension.py").write_text(
            (ext / "extension.py").read_text() + "\n# tampered\n")
        verdict = verify_package(ext, public_key=_PUB)
        self.assertFalse(verdict["ok"])

    def test_wrong_key_rejected(self):
        ext = _copy_ref()
        _signed(ext)
        verdict = verify_package(ext, public_key=_OTHER_PUB)
        self.assertFalse(verdict["ok"])

    def test_self_asserted_pubkey_flagged_not_pinned(self):
        # Without a pinned key, verification uses the sidecar's embedded
        # pubkey: integrity holds, but provenance is self-asserted.
        ext = _copy_ref()
        _signed(ext)
        verdict = verify_package(ext)
        self.assertTrue(verdict["ok"], verdict)
        self.assertFalse(verdict["pinned"])

    def test_unsigned_package_flagged(self):
        ext = _copy_ref()
        verdict = verify_package(ext, public_key=_PUB)
        self.assertFalse(verdict["ok"])
        self.assertIn("unsigned", verdict["error"])

    def test_hash_deterministic(self):
        ext = _copy_ref()
        self.assertEqual(package_hash(ext), package_hash(ext))

    def test_schema_version_written(self):
        tmp = Path(tempfile.mkdtemp())
        store = InstallStore(tmp / "installed.json")
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="abc",
            publisher="p", installed_at=1))
        doc = json.loads((tmp / "installed.json").read_text(
            encoding="utf-8"))
        self.assertEqual(doc["schema_version"], SCHEMA_VERSION)


class TestCorruptStoresFailLoud(unittest.TestCase):
    def _paths(self):
        tmp = Path(tempfile.mkdtemp())
        return (tmp / "registry.json", tmp / "installed.json",
                tmp / "rev.json")

    def test_corrupt_registry_raises_not_empty(self):
        reg_path, _, rev_path = self._paths()
        reg = Registry(reg_path, revocations=RevocationList(rev_path))
        ext = _copy_ref()
        _signed(ext)
        reg.submit(ext, publisher="tester", publisher_pubkey=_PUB)
        reg_path.write_text("not json{{{", encoding="utf-8")
        with self.assertRaises(CorruptStoreError) as ctx:
            reg.installable("role-radar-digest")
        self.assertIn("corrupt", str(ctx.exception).lower())
        # Also fails loudly on list(), not silently empty.
        with self.assertRaises(CorruptStoreError):
            reg.list()

    def test_corrupt_install_store_raises(self):
        _, inst_path, _ = self._paths()
        store = InstallStore(inst_path)
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="abc",
            publisher="p", installed_at=1))
        inst_path.write_text("[1, 2,", encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            store.get("e1")

    def test_corrupt_revocation_list_raises(self):
        _, _, rev_path = self._paths()
        rev = RevocationList(rev_path)
        sig = sign_revocation(_PRIV, "e1", "1.0.0", "r")
        rev.revoke("e1", "1.0.0", "r", publisher_pubkey=_PUB,
                   signature=sig)
        rev_path.write_text("garbage", encoding="utf-8")
        with self.assertRaises(CorruptStoreError):
            rev.is_revoked("e1", "1.0.0")

    def test_write_keeps_backup_of_last_good_state(self):
        _, inst_path, _ = self._paths()
        store = InstallStore(inst_path)
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="aaa",
            publisher="p", installed_at=1))
        store.record_install(InstallRecord(
            ext_id="e1", version="1.1.0", package_sha256="bbb",
            publisher="p", installed_at=2))
        bak = inst_path.with_name(inst_path.name + ".bak")
        self.assertTrue(bak.is_file(), "expected a .bak of last good state")
        bak_doc = json.loads(bak.read_text(encoding="utf-8"))
        self.assertEqual(bak_doc["records"]["e1"]["package_sha256"], "aaa")

    def test_legacy_bare_payload_still_reads(self):
        reg_path, _, rev_path = self._paths()
        # Pre-schema-version file: bare dict, no wrapper.
        reg_path.write_text(json.dumps({"e9": {
            "ext_id": "e9", "version": "1.0.0", "package_sha256": "x",
            "publisher": "p", "publisher_pubkey": "", "source_path": "",
            "capabilities": {}, "gates": {}}}), encoding="utf-8")
        reg = Registry(reg_path, revocations=RevocationList(rev_path))
        self.assertEqual(reg.list()[0]["ext_id"], "e9")


class TestInstallStore(unittest.TestCase):
    def _store(self):
        # Production wiring: InstallStore gets the deployment's
        # RevocationList so rollback can enforce the revoked-pair
        # invariant. Tests that exercise rollback must use this.
        tmp = Path(tempfile.mkdtemp())
        rev = RevocationList(tmp / "rev.json")
        return InstallStore(tmp / "installed.json", revocations=rev)

    def test_pin_and_upgrade_review(self):
        tmp = Path(tempfile.mkdtemp())
        store = InstallStore(tmp / "installed.json")
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="abc",
            publisher="p", installed_at=1))
        # Upgrade without a review is refused.
        bad = store.record_upgrade("e1", "1.1.0", "def", {})
        self.assertFalse(bad["ok"])
        good = store.record_upgrade("e1", "1.1.0", "def", {
            "reviewer": "human", "verdict": "pass",
            "date": "2026-09-13", "notes": "changelog reviewed"})
        self.assertTrue(good["ok"])
        self.assertEqual(store.get("e1")["version"], "1.1.0")
        self.assertEqual(store.get("e1")["upgraded_from"], "1.0.0")

    def test_upgrade_review_fields_validated(self):
        tmp = Path(tempfile.mkdtemp())
        store = InstallStore(tmp / "installed.json")
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="abc",
            publisher="p", installed_at=1))
        base = {"reviewer": "human", "verdict": "pass",
                "date": "2026-09-13", "notes": "ok"}
        # Missing reviewer.
        r = dict(base, reviewer="")
        self.assertFalse(store.record_upgrade(
            "e1", "1.1.0", "def", r)["ok"])
        # Missing notes.
        r = dict(base, notes="  ")
        self.assertFalse(store.record_upgrade(
            "e1", "1.1.0", "def", r)["ok"])
        # Malformed date.
        r = dict(base, date="13-09-2026")
        self.assertFalse(store.record_upgrade(
            "e1", "1.1.0", "def", r)["ok"])
        # Failing verdict.
        r = dict(base, verdict="fail")
        self.assertFalse(store.record_upgrade(
            "e1", "1.1.0", "def", r)["ok"])
        # Non-dict review.
        self.assertFalse(store.record_upgrade(
            "e1", "1.1.0", "def", "looks good")["ok"])

    def test_upgrade_downgrade_refused(self):
        tmp = Path(tempfile.mkdtemp())
        store = InstallStore(tmp / "installed.json")
        store.record_install(InstallRecord(
            ext_id="e1", version="1.1.0", package_sha256="abc",
            publisher="p", installed_at=1))
        review = {"reviewer": "human", "verdict": "pass",
                  "date": "2026-09-13", "notes": "ok"}
        # A downgrade can never be recorded as an upgrade — even with a
        # passing review.
        bad = store.record_upgrade("e1", "1.0.0", "def", review)
        self.assertFalse(bad["ok"])
        self.assertIn("downgrade", bad["error"].lower())
        same = store.record_upgrade("e1", "1.1.0", "def", review)
        self.assertFalse(same["ok"])
        self.assertEqual(store.get("e1")["version"], "1.1.0")

    def test_rollback_restores_prior_package(self):
        store = self._store()
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="aaa",
            publisher="p", installed_at=1, source_path="/pkg/1.0.0"))
        review = {"reviewer": "human", "verdict": "pass",
                  "date": "2026-09-13", "notes": "ok"}
        store.record_upgrade("e1", "1.1.0", "bbb", review,
                             source_path="/pkg/1.1.0")
        cur = store.get("e1")
        self.assertEqual(cur["previous_package_sha256"], "aaa")
        self.assertEqual(cur["previous_source_path"], "/pkg/1.0.0")
        rb = store.rollback("e1")
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["from"], "1.1.0")
        self.assertEqual(rb["to"], "1.0.0")
        cur = store.get("e1")
        self.assertEqual(cur["version"], "1.0.0")
        self.assertEqual(cur["package_sha256"], "aaa")
        self.assertEqual(cur["source_path"], "/pkg/1.0.0")
        # The rollback record describes what happened — it must not
        # synthesize a passing review for the restored version.
        rolled = cur["upgrade_review"]
        self.assertNotEqual(rolled.get("verdict"), "pass")
        self.assertEqual(rolled.get("reviewer"), "rollback")
        # A second rollback rolls forward to the bad release again.
        rb2 = store.rollback("e1")
        self.assertTrue(rb2["ok"], rb2)
        self.assertEqual(store.get("e1")["version"], "1.1.0")

    def test_rollback_without_prior_refused(self):
        store = self._store()
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="abc",
            publisher="p", installed_at=1))
        bad = store.rollback("e1")
        self.assertFalse(bad["ok"])
        self.assertIn("no retained prior", bad["error"])
        self.assertFalse(store.rollback("missing")["ok"])


class TestRollbackRevocation(unittest.TestCase):
    """Regression tests for the re-review veto: rollback() must consult
    the RevocationList and never silently restore a revoked (id,
    version) pair, and must never synthesize a passing review record."""

    def _store_and_rev(self):
        tmp = Path(tempfile.mkdtemp())
        rev = RevocationList(tmp / "rev.json")
        store = InstallStore(tmp / "installed.json", revocations=rev)
        return store, rev

    def _two_versions(self, store):
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0.0", package_sha256="aaa",
            publisher="p", installed_at=1, source_path="/pkg/1.0.0"))
        review = {"reviewer": "human", "verdict": "pass",
                  "date": "2026-09-13", "notes": "ok"}
        store.record_upgrade("e1", "1.1.0", "bbb", review,
                             source_path="/pkg/1.1.0")

    def test_revoke_then_rollback_refused(self):
        # The exact repro from the veto: install 1.0.0 -> upgrade 1.1.0
        # -> authenticated revoke of 1.0.0 -> rollback must REFUSE, not
        # silently restore the revoked bytes.
        store, rev = self._store_and_rev()
        self._two_versions(store)
        reason = "malicious exfiltration found post-release"
        sig = sign_revocation(_PRIV, "e1", "1.0.0", reason)
        self.assertTrue(rev.revoke(
            "e1", "1.0.0", reason, publisher_pubkey=_PUB,
            signature=sig)["ok"])
        self.assertTrue(rev.is_revoked("e1", "1.0.0"))
        rb = store.rollback("e1")
        self.assertFalse(rb["ok"], rb)
        # Loud error naming the revocation: version + reason.
        self.assertIn("revok", rb["error"].lower())
        self.assertIn("1.0.0", rb["error"])
        self.assertIn(reason, rb["error"])
        # State untouched: the revoked version is NOT restored.
        cur = store.get("e1")
        self.assertEqual(cur["version"], "1.1.0")
        self.assertEqual(cur["package_sha256"], "bbb")

    def test_rollback_without_revocation_list_refused_fail_closed(self):
        # Without a RevocationList wired in, rollback cannot prove the
        # target version is not revoked, so it refuses rather than
        # silently violating the invariant.
        tmp = Path(tempfile.mkdtemp())
        store = InstallStore(tmp / "installed.json")  # no revocations
        self._two_versions(store)
        rb = store.rollback("e1")
        self.assertFalse(rb["ok"], rb)
        self.assertIn("RevocationList", rb["error"])
        self.assertEqual(store.get("e1")["version"], "1.1.0")

    def test_rollback_non_revoked_version_still_works(self):
        store, rev = self._store_and_rev()
        self._two_versions(store)
        # A different version is revoked; the rollback target is clean.
        sig = sign_revocation(_PRIV, "e1", "1.0.1", "never shipped")
        rev.revoke("e1", "1.0.1", "never shipped",
                   publisher_pubkey=_PUB, signature=sig)
        rb = store.rollback("e1")
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["to"], "1.0.0")
        cur = store.get("e1")
        self.assertEqual(cur["version"], "1.0.0")
        self.assertEqual(cur["package_sha256"], "aaa")
        self.assertEqual(cur["source_path"], "/pkg/1.0.0")

    def test_rollback_never_synthesizes_passing_review(self):
        store, rev = self._store_and_rev()
        self._two_versions(store)
        self.assertTrue(store.rollback("e1")["ok"])
        review = store.get("e1")["upgrade_review"]
        # The fabricated {'reviewer': 'rollback (operator)',
        # 'verdict': 'pass'} must be gone: the record says what actually
        # happened, not that the restored version passed review.
        self.assertNotEqual(review.get("verdict"), "pass")
        self.assertNotEqual(review.get("reviewer"), "rollback (operator)")
        self.assertEqual(review.get("reviewer"), "rollback")
        self.assertEqual(review.get("verdict"), "restored-previous-version")
        self.assertIn("revocation_check", review)
        self.assertIn("not revoked", review["revocation_check"])

    def test_rollback_refused_across_version_spelling_revoked_1_0_0(self):
        # The blind re-review repro: install recorded as '1.0', revocation
        # authenticated as '1.0.0' — rollback must still REFUSE. The
        # refusal must name the revocation (reason) so an operator can
        # see what blocked it.
        store, rev = self._store_and_rev()
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0", package_sha256="aaa",
            publisher="p", installed_at=1, source_path="/pkg/1.0"))
        review = {"reviewer": "human", "verdict": "pass",
                  "date": "2026-09-13", "notes": "ok"}
        store.record_upgrade("e1", "1.1.0", "bbb", review,
                             source_path="/pkg/1.1.0")
        reason = "malicious exfiltration found post-release"
        sig = sign_revocation(_PRIV, "e1", "1.0.0", reason)
        self.assertTrue(rev.revoke(
            "e1", "1.0.0", reason, publisher_pubkey=_PUB,
            signature=sig)["ok"])
        # The check side sees them as the same version even though the
        # strings differ.
        self.assertTrue(rev.is_revoked("e1", "1.0"))
        rb = store.rollback("e1")
        self.assertFalse(rb["ok"], rb)
        self.assertIn("revok", rb["error"].lower())
        self.assertIn("1.0", rb["error"])
        self.assertIn(reason, rb["error"])
        cur = store.get("e1")
        self.assertEqual(cur["version"], "1.1.0")
        self.assertEqual(cur["package_sha256"], "bbb")

    def test_rollback_refused_across_version_spelling_revoked_1_0(self):
        # The mirror direction: install recorded as '1.0.0', revocation
        # authenticated as '1.0' — rollback must still REFUSE.
        store, rev = self._store_and_rev()
        self._two_versions(store)
        reason = "supply-chain compromise"
        sig = sign_revocation(_PRIV, "e1", "1.0", reason)
        self.assertTrue(rev.revoke(
            "e1", "1.0", reason, publisher_pubkey=_PUB,
            signature=sig)["ok"])
        self.assertTrue(rev.is_revoked("e1", "1.0.0"))
        rb = store.rollback("e1")
        self.assertFalse(rb["ok"], rb)
        self.assertIn("revok", rb["error"].lower())
        self.assertIn("1.0.0", rb["error"])
        self.assertIn(reason, rb["error"])
        self.assertEqual(store.get("e1")["version"], "1.1.0")

    def test_rollback_semantically_different_version_not_conflated(self):
        # Normalization must not conflate genuinely different versions:
        # revoking '1.1' must not block rollback of '1.0'.
        store, rev = self._store_and_rev()
        store.record_install(InstallRecord(
            ext_id="e1", version="1.0", package_sha256="aaa",
            publisher="p", installed_at=1, source_path="/pkg/1.0"))
        review = {"reviewer": "human", "verdict": "pass",
                  "date": "2026-09-13", "notes": "ok"}
        store.record_upgrade("e1", "1.1.0", "bbb", review,
                             source_path="/pkg/1.1.0")
        sig = sign_revocation(_PRIV, "e1", "1.1", "bad newer build")
        self.assertTrue(rev.revoke(
            "e1", "1.1", "bad newer build", publisher_pubkey=_PUB,
            signature=sig)["ok"])
        self.assertFalse(rev.is_revoked("e1", "1.0"))
        rb = store.rollback("e1")
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["to"], "1.0")


class TestRevocation(unittest.TestCase):
    def _rev(self):
        tmp = Path(tempfile.mkdtemp())
        return RevocationList(tmp / "rev.json")

    def test_revoke_with_publisher_signature_blocks(self):
        rev = self._rev()
        self.assertFalse(rev.is_revoked("e1", "1.0.0"))
        sig = sign_revocation(_PRIV, "e1", "1.0.0",
                              "malicious exfiltration")
        res = rev.revoke("e1", "1.0.0", "malicious exfiltration",
                         publisher_pubkey=_PUB, signature=sig)
        self.assertTrue(res["ok"], res)
        self.assertTrue(rev.is_revoked("e1", "1.0.0"))
        self.assertFalse(rev.is_revoked("e1", "1.0.1"))
        self.assertEqual(rev.list()[0]["revoked_by"], _PUB)

    def test_anonymous_revocation_refused(self):
        rev = self._rev()
        res = rev.revoke("e1", "1.0.0", "r", publisher_pubkey="",
                         signature="")
        self.assertFalse(res["ok"])
        self.assertFalse(rev.is_revoked("e1", "1.0.0"))

    def test_wrong_key_signature_refused(self):
        rev = self._rev()
        # Signed by a different key than the claimed publisher key.
        sig = sign_revocation(_OTHER_PRIV, "e1", "1.0.0", "r")
        res = rev.revoke("e1", "1.0.0", "r", publisher_pubkey=_PUB,
                         signature=sig)
        self.assertFalse(res["ok"])
        self.assertIn("not authorized", res["error"])
        self.assertFalse(rev.is_revoked("e1", "1.0.0"))

    def test_tampered_reason_refused(self):
        rev = self._rev()
        sig = sign_revocation(_PRIV, "e1", "1.0.0", "real reason")
        res = rev.revoke("e1", "1.0.0", "different reason",
                         publisher_pubkey=_PUB, signature=sig)
        self.assertFalse(res["ok"])
        self.assertFalse(rev.is_revoked("e1", "1.0.0"))

    def test_revocation_payload_canonical(self):
        self.assertEqual(
            revocation_payload("e1", "1.0.0", "r"),
            b'{"ext_id":"e1","reason":"r","version":"1.0.0"}')

    def test_is_revoked_normalizes_version_spellings(self):
        # The re-review finding: is_revoked must treat '1.0' == '1.0.0'
        # per the module's compare_versions semantics, in BOTH
        # directions — a revocation is not evadable by re-spelling.
        rev = self._rev()
        reason = "malicious exfiltration"
        sig = sign_revocation(_PRIV, "e1", "1.0.0", reason)
        self.assertTrue(rev.revoke(
            "e1", "1.0.0", reason, publisher_pubkey=_PUB,
            signature=sig)["ok"])
        self.assertTrue(rev.is_revoked("e1", "1.0"))
        self.assertTrue(rev.is_revoked("e1", "1.0.0"))
        self.assertTrue(rev.is_revoked("e1", "1.0.0.0"))
        # Semantically different versions are NOT conflated.
        self.assertFalse(rev.is_revoked("e1", "1.1"))
        self.assertFalse(rev.is_revoked("e1", "1.0.1"))
        self.assertFalse(rev.is_revoked("e1", "0.1.0"))

    def test_is_revoked_normalizes_version_spellings_reverse(self):
        # Revocation recorded as '1.0' blocks queries for '1.0.0'.
        rev = self._rev()
        reason = "malicious exfiltration"
        sig = sign_revocation(_PRIV, "e1", "1.0", reason)
        self.assertTrue(rev.revoke(
            "e1", "1.0", reason, publisher_pubkey=_PUB,
            signature=sig)["ok"])
        self.assertTrue(rev.is_revoked("e1", "1.0.0"))
        self.assertFalse(rev.is_revoked("e1", "1.1"))


class TestRegistry(unittest.TestCase):
    def _registry(self):
        tmp = Path(tempfile.mkdtemp())
        return Registry(tmp / "registry.json",
                        revocations=RevocationList(tmp / "rev.json"))

    def _submitted(self, reg, ext, key=_PUB, priv=_PRIV):
        _signed(ext, key=priv)
        result = reg.submit(ext, publisher="tester", publisher_pubkey=key)
        self.assertTrue(result["ok"], result)
        return result

    def test_submit_runs_automated_gates(self):
        ext = _copy_ref()
        reg = self._registry()
        result = self._submitted(reg, ext)
        gates = result["entry"]["gates"]
        self.assertEqual(gates["compatibility"]["verdict"], "pass")
        self.assertEqual(gates["security"]["verdict"], "pass")
        self.assertEqual(gates["ux"]["verdict"], "pending")
        self.assertEqual(gates["governance"]["verdict"], "pending")
        self.assertEqual(result["pubkey_provenance"], "pinned")
        # Not installable until humans pass ux + governance.
        self.assertFalse(reg.installable("role-radar-digest")["ok"])

    def test_all_gates_pass_makes_installable(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in ("ux", "governance"):
            rec = reg.record_gate("role-radar-digest", gate,
                                  verdict="pass", reviewer="human-reviewer",
                                  notes="reviewed")
            self.assertTrue(rec["ok"])
        installable = reg.installable("role-radar-digest")
        self.assertTrue(installable["ok"], installable)
        self.assertEqual(installable["publisher_pubkey"], _PUB)
        self.assertTrue(installable["reverified"])
        self.assertEqual(installable["pubkey_provenance"], "pinned")

    def test_failed_gate_blocks_install(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        reg.record_gate("role-radar-digest", "ux", verdict="pass",
                        reviewer="uxr", notes="ok")
        rec = reg.record_gate("role-radar-digest", "governance",
                              verdict="fail", reviewer="reviewer",
                              notes="audit gap")
        self.assertFalse(rec["publishable"])
        self.assertFalse(reg.installable("role-radar-digest")["ok"])

    def test_submit_with_wrong_pinned_key_fails_security_gate(self):
        ext = _copy_ref()
        _signed(ext)  # signed with _PRIV
        reg = self._registry()
        result = reg.submit(ext, publisher="tester",
                            publisher_pubkey=_OTHER_PUB)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["entry"]["gates"]["security"]["verdict"],
                         "fail")

    def test_four_gates_exist(self):
        self.assertEqual(tuple(GATES),
                         ("compatibility", "security", "ux", "governance"))
        self.assertEqual(tuple(HUMAN_GATES), ("ux", "governance"))
        self.assertEqual(tuple(AUTOMATED_GATES),
                         ("compatibility", "security"))

    # --- KICK_BACK fix: record_gate is human-gates only -------------------
    def test_record_gate_refuses_automated_gate(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in AUTOMATED_GATES:
            # A failed automated security gate must never be one call away
            # from "pass".
            res = reg.record_gate("role-radar-digest", gate,
                                  verdict="pass", reviewer="mallory",
                                  notes="trust me")
            self.assertFalse(res["ok"], gate)
            self.assertIn("automated", res["error"])
        # The automated verdicts are unchanged.
        entry_gates = reg.installable("role-radar-digest")
        self.assertFalse(entry_gates["ok"])  # humans still pending

    def test_record_gate_requires_named_human_reviewer(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        res = reg.record_gate("role-radar-digest", "ux", verdict="pass",
                              reviewer="  ", notes="ok")
        self.assertFalse(res["ok"])

    def test_override_automated_gate_needs_human_authorization(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        # Missing authorization.
        res = reg.override_automated_gate(
            "role-radar-digest", "security", verdict="pass",
            reviewer="paul", notes="false positive in scanner",
            authorized_by="")
        self.assertFalse(res["ok"])
        self.assertIn("authorization", res["error"])
        # Missing justification.
        res = reg.override_automated_gate(
            "role-radar-digest", "security", verdict="pass",
            reviewer="paul", notes=" ", authorized_by="paul")
        self.assertFalse(res["ok"])
        # Automated reviewer is not a human.
        res = reg.override_automated_gate(
            "role-radar-digest", "security", verdict="pass",
            reviewer="policy-kit (automated)", notes="x",
            authorized_by="paul")
        self.assertFalse(res["ok"])
        # Human gates cannot go through the override path.
        res = reg.override_automated_gate(
            "role-radar-digest", "ux", verdict="pass",
            reviewer="paul", notes="x", authorized_by="paul")
        self.assertFalse(res["ok"])

    def test_override_automated_gate_audited(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        res = reg.override_automated_gate(
            "role-radar-digest", "security", verdict="fail",
            reviewer="paul", notes="scanner missed an exfil path",
            authorized_by="paul (human authority)")
        self.assertTrue(res["ok"], res)
        history = reg.gate_history("role-radar-digest")
        overrides = [h for h in history
                     if h["gate"] == "security" and h["override"]]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0]["authorized_by"],
                         "paul (human authority)")
        self.assertEqual(overrides[0]["reviewer"], "paul")
        self.assertFalse(reg.installable("role-radar-digest")["ok"])

    # --- KICK_BACK fix: install-time re-verification ----------------------
    def test_installable_reverifies_bytes(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in HUMAN_GATES:
            reg.record_gate("role-radar-digest", gate, verdict="pass",
                            reviewer="human", notes="ok")
        self.assertTrue(reg.installable("role-radar-digest")["ok"])
        # Tamper with the bytes after registration: the gatekeeper must
        # catch it even though the recorded gates all say pass.
        (ext / "extension.py").write_text(
            (ext / "extension.py").read_text() + "\n# tampered\n")
        bad = reg.installable("role-radar-digest")
        self.assertFalse(bad["ok"])
        self.assertIn("changed after registration", bad["error"])

    def test_installable_refuses_missing_source(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in HUMAN_GATES:
            reg.record_gate("role-radar-digest", gate, verdict="pass",
                            reviewer="human", notes="ok")
        import shutil
        shutil.rmtree(ext)
        bad = reg.installable("role-radar-digest")
        self.assertFalse(bad["ok"])
        self.assertIn("missing", bad["error"])

    # --- KICK_BACK fix: key continuity + version monotonicity -------------
    def test_resubmission_key_substitution_rejected(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext, key=_PUB, priv=_PRIV)
        _signed(ext, key=_OTHER_PRIV)
        res = reg.submit(ext, publisher="tester",
                         publisher_pubkey=_OTHER_PUB)
        self.assertFalse(res["ok"])
        self.assertIn("key substitution", res["error"])

    def test_resubmission_downgrade_rejected(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        _set_version(ext, "0.2.0")
        _signed(ext)
        res = reg.submit(ext, publisher="tester", publisher_pubkey=_PUB)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["entry"]["version"], "0.2.0")
        _set_version(ext, "0.1.9")
        _signed(ext)
        res = reg.submit(ext, publisher="tester", publisher_pubkey=_PUB)
        self.assertFalse(res["ok"])
        self.assertIn("downgrade", res["error"])

    def test_new_version_resets_human_gates(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in HUMAN_GATES:
            reg.record_gate("role-radar-digest", gate, verdict="pass",
                            reviewer="human", notes="ok")
        self.assertTrue(reg.installable("role-radar-digest")["ok"])
        _set_version(ext, "0.2.0")
        _signed(ext)
        res = reg.submit(ext, publisher="tester", publisher_pubkey=_PUB)
        self.assertTrue(res["ok"], res)
        # New code needs re-review: human gates are pending again.
        self.assertEqual(res["entry"]["gates"]["ux"]["verdict"], "pending")
        self.assertEqual(
            res["entry"]["gates"]["governance"]["verdict"], "pending")
        self.assertFalse(reg.installable("role-radar-digest")["ok"])

    def test_same_version_resubmission_preserves_human_verdicts(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        reg.record_gate("role-radar-digest", "ux", verdict="pass",
                        reviewer="uxr", notes="card renders")
        reg.record_gate("role-radar-digest", "governance", verdict="fail",
                        reviewer="gov", notes="audit gap")
        _signed(ext)  # re-run: same bytes, same version
        res = reg.submit(ext, publisher="tester", publisher_pubkey=_PUB)
        self.assertTrue(res["ok"], res)
        gates = res["entry"]["gates"]
        self.assertEqual(gates["ux"]["verdict"], "pass")
        self.assertEqual(gates["ux"]["reviewer"], "uxr")
        self.assertEqual(gates["governance"]["verdict"], "fail")
        self.assertEqual(gates["governance"]["reviewer"], "gov")

    # --- KICK_BACK fix: honest key provenance ------------------------------
    def test_submit_without_pubkey_flags_self_asserted(self):
        ext = _copy_ref()
        _signed(ext)
        reg = self._registry()
        result = reg.submit(ext, publisher="tester")
        self.assertTrue(result["ok"], result)
        entry = result["entry"]
        self.assertEqual(entry["pubkey_provenance"], "self-asserted")
        self.assertNotEqual(entry["pubkey_provenance"], "pinned")
        # ...and the flag survives a round trip through the store.
        listed = {e["ext_id"]: e for e in reg.list()}
        self.assertEqual(
            listed["role-radar-digest"]["pubkey_provenance"],
            "self-asserted")

    # --- KICK_BACK fix: gate audit trail -----------------------------------
    def test_gate_history_is_append_only(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        reg.record_gate("role-radar-digest", "ux", verdict="fail",
                        reviewer="uxr", notes="no empty state")
        reg.record_gate("role-radar-digest", "ux", verdict="pass",
                        reviewer="uxr", notes="fixed")
        history = reg.gate_history("role-radar-digest")
        ux_changes = [h for h in history if h["gate"] == "ux"]
        # submit(pending) + fail + pass = 3 records; prior verdicts kept.
        self.assertEqual(len(ux_changes), 3)
        self.assertEqual(
            [(h["prior_verdict"], h["new_verdict"]) for h in ux_changes],
            [("none", "pending"), ("pending", "fail"), ("fail", "pass")])
        for h in ux_changes:
            self.assertIn("reviewer", h)
            self.assertIn("date", h)
            self.assertIn("recorded_at", h)

    # --- KICK_BACK fix: authenticated revocation via the registry ----------
    def test_registry_revoke_bound_to_pinned_key(self):
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in HUMAN_GATES:
            reg.record_gate("role-radar-digest", gate, verdict="pass",
                            reviewer="human", notes="ok")
        self.assertTrue(reg.installable("role-radar-digest")["ok"])
        reason = "exfiltration found post-review"
        sig = sign_revocation(_PRIV, "role-radar-digest", "0.1.0", reason)
        res = reg.revoke("role-radar-digest", "0.1.0", reason,
                         signature=sig)
        self.assertTrue(res["ok"], res)
        self.assertFalse(reg.installable("role-radar-digest")["ok"])
        # A signature from anyone else is refused.
        bad_sig = sign_revocation(_OTHER_PRIV, "role-radar-digest",
                                  "0.1.0", reason)
        res = reg.revoke("role-radar-digest", "0.1.0", reason,
                         signature=bad_sig)
        self.assertFalse(res["ok"])

    def test_submit_refuses_revoked_version_spelling_variant(self):
        # Install path: the registry entry is 0.1.0 and the revocation
        # was authenticated as '0.1.0'; a resubmission under the
        # compare-equal spelling '0.01.0' (valid semver per the manifest
        # schema) must be refused as revoked, not re-run through the
        # gates.
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        reason = "exfiltration found post-review"
        sig = sign_revocation(_PRIV, "role-radar-digest", "0.1.0", reason)
        res = reg.revoke("role-radar-digest", "0.1.0", reason,
                         signature=sig)
        self.assertTrue(res["ok"], res)
        variant = _copy_ref()
        _set_version(variant, "0.01.0")
        _signed(variant)
        bad = reg.submit(variant, publisher="tester",
                         publisher_pubkey=_PUB)
        self.assertFalse(bad["ok"], bad)
        self.assertIn("revoked", bad["error"])

    def test_installable_refuses_revoked_version_spelling_variant(self):
        # Load path: the revocation was authenticated as '0.01.0' while
        # the registered entry is 0.1.0 — installable() must still
        # refuse it as revoked. The revocation check precedes gate
        # evaluation, so the refusal names the revocation itself.
        ext = _copy_ref()
        reg = self._registry()
        self._submitted(reg, ext)
        for gate in HUMAN_GATES:
            reg.record_gate("role-radar-digest", gate, verdict="pass",
                            reviewer="human", notes="ok")
        reason = "exfiltration found post-review"
        sig = sign_revocation(_PRIV, "role-radar-digest", "0.01.0", reason)
        res = reg.revoke("role-radar-digest", "0.01.0", reason,
                         signature=sig)
        self.assertTrue(res["ok"], res)
        bad = reg.installable("role-radar-digest")
        self.assertFalse(bad["ok"], bad)
        self.assertIn("revoked", bad["error"])

    def test_registry_revoke_refused_without_pinned_key(self):
        ext = _copy_ref()
        _signed(ext)
        reg = self._registry()
        result = reg.submit(ext, publisher="tester")  # self-asserted
        self.assertTrue(result["ok"], result)
        res = reg.revoke("role-radar-digest", "0.1.0", "r",
                         signature="00" * 64)
        self.assertFalse(res["ok"])
        self.assertIn("pinned", res["error"])

    # --- KICK_BACK fix: fail closed without a revocation list --------------
    def test_registry_requires_revocation_list(self):
        tmp = Path(tempfile.mkdtemp())
        with self.assertRaises(ValueError) as ctx:
            Registry(tmp / "registry.json", revocations=None)
        self.assertIn("RevocationList", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
