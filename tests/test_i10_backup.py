"""Initiative 10 — backup/restore/drill tests (synthetic fixtures only).

No real user data is touched: every test builds a scratch data directory
with labeled synthetic records. The HOSTILE tests craft malicious or
tampered bundles by hand (bypassing ``create_backup``) to prove the
defenses actually hold.
"""

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i10 import backup

PASSPHRASE = "synthetic-test-passphrase"


def _event(i: int) -> dict:
    return {
        "event_id": f"synth-event-{i:03d}",
        "application_id": f"synth-job-{i:03d}",
        "event_type": "applied",
        "occurred_at": "2026-09-01T10:00:00",
        "source": "synthetic-fixture",
        "role": "Synthetic Engineer",
        "provenance": {"actor": "test"},
        "schema_version": "outcome-min-v0",
        "recorded_at": "2026-09-01T10:00:01",
    }


def _make_data_dir(root: Path, n_events: int = 5) -> Path:
    d = root / "data"
    (d / "profiles").mkdir(parents=True)
    (d / "outcomes.jsonl").write_text(
        "\n".join(json.dumps(_event(i)) for i in range(n_events)) + "\n",
        encoding="utf-8",
    )
    (d / "outcomes_meta.json").write_text(
        json.dumps({"capture_enabled_at": "2026-09-01T00:00:00"}), encoding="utf-8"
    )
    (d / "applications.json").write_text(
        json.dumps([{"job_id": f"synth-job-{i:03d}"} for i in range(n_events)]),
        encoding="utf-8",
    )
    (d / "profiles" / "profile.json").write_text(
        json.dumps({"schema_version": "profile-v1", "full_name": "Synthetic User"}),
        encoding="utf-8",
    )
    (d / "evidence_store.jsonl").write_text(
        json.dumps({
            "record_id": "synth-ev-001",
            "kind": "note",
            "application_id": "synth-job-000",
            "created_at": "2026-09-01T10:00:00",
            "schema_version": "evidence-v0",
            "title": "synthetic",
        }) + "\n",
        encoding="utf-8",
    )
    return d


# ---------------------------------------------------------------------------
# Hostile-bundle helpers: build/tamper bundles by hand, bypassing create_backup
# ---------------------------------------------------------------------------


def _craft_bundle(
    tmp: Path,
    members: dict[str, bytes],
    manifest_files: dict,
    passphrase: str,
    name: str = "hostile.vetobackup",
) -> Path:
    """Assemble a bundle with full control over tar members and manifest."""
    Fernet, _, _ = backup._crypto()
    salt = os.urandom(16)
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for arcname, blob in members.items():
            ti = tarfile.TarInfo(arcname)
            ti.size = len(blob)
            tar.addfile(ti, io.BytesIO(blob))
        manifest = {
            "schema_versions": {
                "outcome-event": "outcome-min-v0",
                "profile": "profile-v1",
                "evidence-store": "evidence-v0",
            },
            "files": manifest_files,
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        ti = tarfile.TarInfo(backup.MANIFEST_NAME)
        ti.size = len(manifest_bytes)
        tar.addfile(ti, io.BytesIO(manifest_bytes))
    token = Fernet(backup._derive_key(passphrase, salt)).encrypt(tar_buf.getvalue())
    header = {
        "magic": "VETOBKP1",
        "backup_format": backup.BACKUP_FORMAT,
        "created_at": "2026-09-13T00:00:00",
        "schema_versions": manifest["schema_versions"],
        "files": len(manifest_files),
        "records": 0,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "salt_b64": base64.b64encode(salt).decode("ascii"),
    }
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    out = tmp / name
    with out.open("wb") as fh:
        fh.write(backup.MAGIC)
        fh.write(len(header_bytes).to_bytes(4, "big"))
        fh.write(header_bytes)
        fh.write(token)
    return out


def _repack_bundle(
    bundle: Path, tmp: Path, passphrase: str, *, drop=None, corrupt=None
) -> Path:
    """Re-encrypt a bundle with a tar member dropped or corrupted (the
    manifest still describes the original content)."""
    header, token = backup._read_bundle(bundle)
    Fernet, _, _ = backup._crypto()
    salt = base64.b64decode(header["salt_b64"])
    payload = Fernet(backup._derive_key(passphrase, salt)).decrypt(token)
    src = tarfile.open(fileobj=io.BytesIO(payload), mode="r")
    out_buf = io.BytesIO()
    with tarfile.open(fileobj=out_buf, mode="w") as dst:
        for m in src.getmembers():
            if m.name == drop:
                continue
            fh = src.extractfile(m)
            blob = fh.read() if fh else b""
            if corrupt and m.name == corrupt and blob:
                blob = bytes([blob[0] ^ 0xFF]) + blob[1:]
            ti = tarfile.TarInfo(m.name)
            ti.size = len(blob)
            dst.addfile(ti, io.BytesIO(blob))
    src.close()
    new_token = Fernet(backup._derive_key(passphrase, salt)).encrypt(out_buf.getvalue())
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    out = tmp / "tampered.vetobackup"
    with out.open("wb") as fh:
        fh.write(backup.MAGIC)
        fh.write(len(header_bytes).to_bytes(4, "big"))
        fh.write(header_bytes)
        fh.write(new_token)
    return out


def _tamper_header_sha(bundle: Path, tmp: Path) -> Path:
    """Corrupt the header's manifest_sha256 (re-spliced header)."""
    raw = bundle.read_bytes()
    hlen = int.from_bytes(raw[8:12], "big")
    header = json.loads(raw[12 : 12 + hlen])
    header["manifest_sha256"] = "0" * 64
    header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
    out = tmp / "badsha.vetobackup"
    with out.open("wb") as fh:
        fh.write(backup.MAGIC)
        fh.write(len(header_bytes).to_bytes(4, "big"))
        fh.write(header_bytes)
        fh.write(raw[12 + hlen :])
    return out


def _snapshot_dirs(target: Path):
    return [
        p
        for p in target.parent.iterdir()
        if p.is_dir() and p.name.startswith(target.name + ".pre-restore-")
    ]


class TestBackup(unittest.TestCase):
    def test_create_and_verify_100_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            report = backup.create_backup(d, out, PASSPHRASE)
            # 5 events + outcomes_meta + profile + evidence. The legacy
            # applications.json is NOT a named stable schema, so backup
            # deliberately excludes it (contract-first rule).
            self.assertEqual(report.records, 5 + 1 + 1 + 1)
            integrity = backup.verify_backup(out, PASSPHRASE)
            self.assertTrue(integrity.ok)
            self.assertEqual(integrity.checksum_match_pct, 100.0)
            self.assertEqual(integrity.files_ok, integrity.files_checked)

    def test_wrong_passphrase_fails_decryption(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            with self.assertRaises(ValueError):
                backup.verify_backup(out, "wrong-passphrase-00000000")

    def test_short_passphrase_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            with self.assertRaises(ValueError):
                backup.create_backup(d, Path(tmp) / "b", "short")

    def test_passphrase_minimum_is_20_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            with self.assertRaises(ValueError):
                backup.create_backup(d, Path(tmp) / "b1", "x" * 19)
            report = backup.create_backup(d, Path(tmp) / "b2", "x" * 20)
            self.assertEqual(report.files, 4)

    def test_generate_passphrase_meets_policy_and_roundtrips(self):
        pp = backup.generate_passphrase()
        self.assertGreaterEqual(len(pp), 20)
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, pp)
            self.assertTrue(backup.verify_backup(out, pp).ok)

    def test_bundle_plaintext_has_no_user_content(self):
        # The manifest now travels inside the encrypted tar: no
        # user-identifying content may appear in the raw bundle bytes.
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            raw = out.read_bytes()
            self.assertNotIn(b"Synthetic Engineer", raw)
            self.assertNotIn(b"synth-event", raw)

    def test_named_profile_filename_not_in_plaintext(self):
        # HOSTILE: a user-named profile must not leak its filename into
        # any plaintext portion of the bundle.
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            named = d / "profiles" / "Jane Doe - personal job search.json"
            named.write_text(
                json.dumps(
                    {"schema_version": "profile-v1", "full_name": "Jane Doe"}
                ),
                encoding="utf-8",
            )
            out = Path(tmp) / "b.vetobackup"
            report = backup.create_backup(d, out, PASSPHRASE)
            self.assertEqual(report.files, 5)
            raw = out.read_bytes()
            self.assertNotIn(b"Jane Doe", raw)
            self.assertNotIn(b"personal job search", raw)
            # ... but the file itself must still round-trip intact.
            target = Path(tmp) / "restored"
            backup.restore(out, PASSPHRASE, target)
            self.assertEqual(
                (target / "profiles" / named.name).read_text(encoding="utf-8"),
                named.read_text(encoding="utf-8"),
            )

    def test_bundle_plaintext_has_no_checksums_or_manifest(self):
        # A file's SHA-256 hex is user-derived; it must not appear in the
        # raw bundle outside the encrypted payload.
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            raw = out.read_bytes()
            digest = hashlib.sha256(
                (d / "outcomes.jsonl").read_bytes()
            ).hexdigest().encode("ascii")
            self.assertNotIn(digest, raw)

    def test_header_manifest_sha256_is_verified(self):
        # The header's manifest_sha256 was a dead field; it is now checked.
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            bad = _tamper_header_sha(out, Path(tmp))
            with self.assertRaises(ValueError):
                backup.verify_backup(bad, PASSPHRASE)
            with self.assertRaises(ValueError):
                backup.restore(bad, PASSPHRASE, Path(tmp) / "r")

    def test_create_rejects_nonexistent_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                backup.create_backup(
                    Path(tmp) / "no-such-dir", Path(tmp) / "b", PASSPHRASE
                )

    def test_create_backup_rejects_symlinked_file(self):
        # A symlinked schema file would get its target's checksum into
        # the manifest while the tar payload drops the non-regular
        # member — a bundle that fails its own 100%-integrity bar with
        # no warning. create_backup must refuse loudly at backup time.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            external = tmp / "external"
            external.mkdir()
            (external / "real.json").write_text(
                json.dumps({"schema_version": "profile-v1"}), encoding="utf-8"
            )
            (d / "profiles" / "linked.json").symlink_to(external / "real.json")
            with self.assertRaises(ValueError):
                backup.create_backup(d, tmp / "b.vetobackup", PASSPHRASE)

    def test_audit_log_rotates_at_1mib(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            audit = d / backup.AUDIT_NAME
            line = json.dumps({"at": "2026-01-01T00:00:00", "action": "x"}) + "\n"
            audit.write_text(line * 24000, encoding="utf-8")  # > 1 MiB
            self.assertGreater(audit.stat().st_size, 1_048_576)
            backup.create_backup(d, Path(tmp) / "b.vetobackup", PASSPHRASE)
            lines = audit.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 5000 + 1)  # kept tail + new entry
            self.assertLess(audit.stat().st_size, 1_048_576 + 4096)

    def test_audit_log_has_counts_not_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            backup.create_backup(d, Path(tmp) / "b.vetobackup", PASSPHRASE)
            audit = (d / backup.AUDIT_NAME).read_text(encoding="utf-8")
            self.assertIn("backup_created", audit)
            self.assertNotIn("Synthetic Engineer", audit)


class TestRestore(unittest.TestCase):
    def test_full_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = Path(tmp) / "restored"
            report = backup.restore(out, PASSPHRASE, target)
            self.assertEqual(report.verify.checksum_match_pct, 100.0)
            self.assertEqual(
                (target / "outcomes.jsonl").read_text(encoding="utf-8"),
                (d / "outcomes.jsonl").read_text(encoding="utf-8"),
            )

    def test_selective_restore_only_named_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = Path(tmp) / "restored"
            report = backup.restore(out, PASSPHRASE, target, selective=["outcome-event"])
            self.assertEqual(report.selective, ["outcome-event"])
            self.assertTrue((target / "outcomes.jsonl").exists())
            self.assertFalse((target / "profiles" / "profile.json").exists())

    def test_selective_restore_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            out = Path(tmp) / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            with self.assertRaises(KeyError):
                backup.restore(out, PASSPHRASE, Path(tmp) / "r", selective=["resume-pdf"])

    def test_restore_refuses_path_traversal(self):
        # HOSTILE: crafted bundle with member "../restored_evil/pwned.txt"
        # and a matching manifest entry. The old startswith() guard let
        # this through; restore must refuse and write nothing outside.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            blob = b"pwned"
            evil = "../restored_evil/pwned.txt"
            hostile = _craft_bundle(
                tmp,
                {evil: blob},
                {
                    evil: {
                        "schema": "profile",
                        "sha256": hashlib.sha256(blob).hexdigest(),
                        "records": 1,
                        "bytes": len(blob),
                    }
                },
                PASSPHRASE,
            )
            target = tmp / "restored"
            with self.assertRaises(ValueError):
                backup.restore(hostile, PASSPHRASE, target)
            self.assertFalse((tmp / "restored_evil").exists())
            self.assertFalse((tmp / "restored_evil" / "pwned.txt").exists())
            # Nothing was written at all: the guard fires before any write.
            self.assertFalse(target.exists())

    def test_restore_tampered_bundle_raises(self):
        # HOSTILE: manifest entry missing from the tar payload (was
        # silently skipped); restore must refuse loudly instead.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            tampered = _repack_bundle(
                out, tmp, PASSPHRASE, drop="evidence_store.jsonl"
            )
            integrity = backup.verify_backup(tampered, PASSPHRASE)
            self.assertFalse(integrity.ok)
            self.assertTrue(
                any("missing from bundle payload" in m for m in integrity.mismatches)
            )
            target = tmp / "restored"
            with self.assertRaises(ValueError):
                backup.restore(tampered, PASSPHRASE, target)
            self.assertFalse(target.exists())

    def test_restore_extra_member_without_manifest_entry_raises(self):
        # HOSTILE: payload member with no manifest entry must also be
        # loud (the old code silently skipped it).
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            header, token = backup._read_bundle(out)
            Fernet, _, _ = backup._crypto()
            salt = base64.b64decode(header["salt_b64"])
            payload = Fernet(backup._derive_key(PASSPHRASE, salt)).decrypt(token)
            src = tarfile.open(fileobj=io.BytesIO(payload), mode="r")
            out_buf = io.BytesIO()
            with tarfile.open(fileobj=out_buf, mode="w") as dst:
                for m in src.getmembers():
                    fh = src.extractfile(m)
                    blob = fh.read() if fh else b""
                    ti = tarfile.TarInfo(m.name)
                    ti.size = len(blob)
                    dst.addfile(ti, io.BytesIO(blob))
                extra = tarfile.TarInfo("smuggled.txt")
                extra_blob = b"smuggled"
                extra.size = len(extra_blob)
                dst.addfile(extra, io.BytesIO(extra_blob))
            src.close()
            new_token = Fernet(backup._derive_key(PASSPHRASE, salt)).encrypt(
                out_buf.getvalue()
            )
            header_bytes = json.dumps(header, sort_keys=True).encode("utf-8")
            smuggled = tmp / "smuggled.vetobackup"
            with smuggled.open("wb") as fh:
                fh.write(backup.MAGIC)
                fh.write(len(header_bytes).to_bytes(4, "big"))
                fh.write(header_bytes)
                fh.write(new_token)
            integrity = backup.verify_backup(smuggled, PASSPHRASE)
            self.assertFalse(integrity.ok)
            self.assertTrue(
                any("not in manifest" in m for m in integrity.mismatches)
            )
            with self.assertRaises(ValueError):
                backup.restore(smuggled, PASSPHRASE, tmp / "r")

    def test_restore_needs_confirmation_on_nonempty_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = tmp / "live"
            target.mkdir()
            (target / "outcomes.jsonl").write_text("NEWER LIVE DATA\n", encoding="utf-8")
            with self.assertRaises(backup.RestoreNeedsConfirmation):
                backup.restore(out, PASSPHRASE, target)
            # Nothing was written: newer live data is intact.
            self.assertEqual(
                (target / "outcomes.jsonl").read_text(encoding="utf-8"),
                "NEWER LIVE DATA\n",
            )
            self.assertEqual(_snapshot_dirs(target), [])

    def test_restore_takes_pre_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = tmp / "live"
            target.mkdir()
            (target / "outcomes.jsonl").write_text("OLD LIVE DATA\n", encoding="utf-8")
            report = backup.restore(out, PASSPHRASE, target, confirm=True)
            self.assertIsNotNone(report.snapshot)
            snaps = _snapshot_dirs(target)
            self.assertEqual(len(snaps), 1)
            self.assertEqual(
                (snaps[0] / "outcomes.jsonl").read_text(encoding="utf-8"),
                "OLD LIVE DATA\n",
            )
            # ... and the restore itself still landed the new content.
            self.assertEqual(
                (target / "outcomes.jsonl").read_text(encoding="utf-8"),
                (d / "outcomes.jsonl").read_text(encoding="utf-8"),
            )

    def test_restore_rollback_on_mid_restore_failure(self):
        # HOSTILE: a failure halfway through the write loop must not
        # leave a half-old/half-new data dir — rollback restores the
        # pre-restore state exactly.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = tmp / "live"
            target.mkdir()
            (target / "outcomes.jsonl").write_text("OLD LIVE DATA\n", encoding="utf-8")

            real_write = backup._write_restored_file
            calls: list = []

            def flaky(dest, blob):
                calls.append(dest)
                if len(calls) == 2:
                    raise OSError("simulated disk failure")
                return real_write(dest, blob)

            with mock.patch.object(
                backup, "_write_restored_file", side_effect=flaky
            ):
                with self.assertRaises(OSError):
                    backup.restore(out, PASSPHRASE, target, confirm=True)
            self.assertGreaterEqual(len(calls), 2)
            # Rolled back: old content intact, no half-written new files.
            self.assertEqual(
                (target / "outcomes.jsonl").read_text(encoding="utf-8"),
                "OLD LIVE DATA\n",
            )
            self.assertFalse((target / "outcomes_meta.json").exists())
            self.assertFalse((target / "evidence_store.jsonl").exists())
            # Snapshot remains for manual recovery; rollback was audited.
            self.assertEqual(len(_snapshot_dirs(target)), 1)
            audit = (target / backup.AUDIT_NAME).read_text(encoding="utf-8")
            self.assertIn("restore_rolled_back", audit)

    def test_snapshot_preserves_symlink_and_copies_no_external_bytes(self):
        # The pre-restore snapshot must preserve the live state EXACTLY:
        # a symlink stays a symlink (symlinks=True), never a
        # materialized copy of the external tree (disk-exhaustion /
        # symlink-cycle hazard during disaster recovery).
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            external = tmp / "external"
            external.mkdir()
            (external / "big.bin").write_bytes(b"E" * 4096)
            target = tmp / "live"
            (target / "profiles").mkdir(parents=True)
            (target / "outcomes.jsonl").write_text("OLD LIVE DATA\n", encoding="utf-8")
            (target / "profiles" / "extlink").symlink_to(external)
            report = backup.restore(out, PASSPHRASE, target, confirm=True)
            snaps = _snapshot_dirs(target)
            self.assertEqual(len(snaps), 1)
            snap_link = snaps[0] / "profiles" / "extlink"
            self.assertTrue(snap_link.is_symlink(), "snapshot dereferenced the symlink")
            self.assertEqual(os.readlink(snap_link), str(external))
            # No external bytes were duplicated into the snapshot.
            dupes = [p for p in snaps[0].rglob("*") if p.is_file()
                     and p.read_bytes() == b"E" * 4096]
            self.assertEqual(dupes, [])
            # And the live dir's symlink survived the restore untouched.
            self.assertTrue((target / "profiles" / "extlink").is_symlink())
            self.assertEqual(report.verify.checksum_match_pct, 100.0)

    def test_rollback_restores_symlink_as_symlink(self):
        # A mid-restore failure must roll back to the pre-restore state
        # exactly: a live symlink must come back as a symlink, never a
        # materialized real directory.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            external = tmp / "external"
            external.mkdir()
            (external / "data.txt").write_text("external", encoding="utf-8")
            target = tmp / "live"
            (target / "profiles").mkdir(parents=True)
            (target / "outcomes.jsonl").write_text("OLD LIVE DATA\n", encoding="utf-8")
            link = target / "profiles" / "extlink"
            link.symlink_to(external)

            real_write = backup._write_restored_file
            calls: list = []

            def flaky(dest, blob):
                calls.append(dest)
                if len(calls) == 2:
                    raise OSError("simulated disk failure")
                return real_write(dest, blob)

            with mock.patch.object(backup, "_write_restored_file", side_effect=flaky):
                with self.assertRaises(OSError):
                    backup.restore(out, PASSPHRASE, target, confirm=True)
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(
                (target / "outcomes.jsonl").read_text(encoding="utf-8"),
                "OLD LIVE DATA\n",
            )
            # The symlink was restored as a symlink, not a real directory.
            self.assertTrue(link.is_symlink(), "rollback materialized the symlink")
            self.assertFalse(link.is_dir() and not link.is_symlink())
            self.assertEqual(
                (link / "data.txt").read_text(encoding="utf-8"), "external"
            )

    def test_restore_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = tmp / "restored"
            report = backup.restore(out, PASSPHRASE, target, dry_run=True)
            self.assertTrue(report.dry_run)
            self.assertEqual(report.planned_files, 4)
            self.assertEqual(report.files_restored, 0)
            self.assertTrue(report.verify.ok)
            self.assertFalse(target.exists())
            self.assertEqual(_snapshot_dirs(target), [])

    def test_cli_restore_exits_nonzero_on_integrity_failure(self):
        # Tampered member outside the selected schemas: restore succeeds
        # for the selection, but the report is not ok → CLI exits nonzero.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            tampered = _repack_bundle(
                out, tmp, PASSPHRASE, corrupt="evidence_store.jsonl"
            )
            target = tmp / "restored"
            with mock.patch.dict(
                os.environ, {"VETO_BACKUP_PASSPHRASE": PASSPHRASE}
            ):
                rc = backup.main(
                    [
                        "restore",
                        "--in",
                        str(tampered),
                        "--data-dir",
                        str(target),
                        "--selective",
                        "outcome-event",
                    ]
                )
            self.assertNotEqual(rc, 0)
            # The selected schema was still restored; the failure is in
            # the integrity report, surfaced via the exit code.
            self.assertTrue((target / "outcomes.jsonl").exists())

    def test_cli_restore_nonempty_without_yes_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            target = tmp / "live"
            target.mkdir()
            (target / "outcomes.jsonl").write_text("x\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"VETO_BACKUP_PASSPHRASE": PASSPHRASE}
            ), mock.patch.object(sys.stdin, "isatty", return_value=False):
                rc = backup.main(
                    ["restore", "--in", str(out), "--data-dir", str(target)]
                )
            self.assertNotEqual(rc, 0)
            self.assertEqual(
                (target / "outcomes.jsonl").read_text(encoding="utf-8"), "x\n"
            )

    def test_cli_verify_exits_nonzero_on_tampered_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            d = _make_data_dir(tmp)
            out = tmp / "b.vetobackup"
            backup.create_backup(d, out, PASSPHRASE)
            tampered = _repack_bundle(
                out, tmp, PASSPHRASE, drop="evidence_store.jsonl"
            )
            with mock.patch.dict(
                os.environ, {"VETO_BACKUP_PASSPHRASE": PASSPHRASE}
            ):
                rc = backup.main(["verify", "--in", str(tampered)])
            self.assertNotEqual(rc, 0)


class TestMalformedManifest(unittest.TestCase):
    def _malformed_bundle(self, tmp, manifest_files, members=None):
        blob = b"payload"
        if members is None:
            members = {"profiles/profile.json": blob}
        return _craft_bundle(tmp, members, manifest_files, PASSPHRASE)

    def test_malformed_entry_missing_keys_raises_valueerror(self):
        # HOSTILE: hand-crafted bundle whose manifest entry lacks the
        # keys verify/restore index ("sha256", "records"). The old code
        # leaked KeyError; the _decrypt_payload contract promises
        # ValueError.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hostile = self._malformed_bundle(
                tmp, {"profiles/profile.json": {"schema": "profile"}}
            )
            with self.assertRaises(ValueError):
                backup.verify_backup(hostile, PASSPHRASE)
            with self.assertRaises(ValueError):
                backup.restore(hostile, PASSPHRASE, tmp / "r")

    def test_entry_not_an_object_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hostile = self._malformed_bundle(
                tmp, {"profiles/profile.json": "definitely-not-a-dict"}
            )
            with self.assertRaises(ValueError):
                backup.verify_backup(hostile, PASSPHRASE)
            with self.assertRaises(ValueError):
                backup.restore(hostile, PASSPHRASE, tmp / "r")

    def test_files_section_not_an_object_raises_valueerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            hostile = self._malformed_bundle(tmp, ["profiles/profile.json"])
            with self.assertRaises(ValueError):
                backup.verify_backup(hostile, PASSPHRASE)
            with self.assertRaises(ValueError):
                backup.restore(hostile, PASSPHRASE, tmp / "r")


class TestDrill(unittest.TestCase):
    def test_drill_passes_with_zero_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp), n_events=12)
            report = backup.drill(d, PASSPHRASE)
            self.assertTrue(report.ok, report.detail)
            self.assertEqual(report.checksum_match_pct, 100.0)
            self.assertTrue(all(v == 0 for v in report.record_gap.values()))

    def test_drill_leaves_live_store_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            before = sorted(p.name for p in d.iterdir())
            backup.drill(d, PASSPHRASE)
            # Only the audit log may be appended.
            after = sorted(p.name for p in d.iterdir())
            self.assertEqual(set(after) - set(before), {backup.AUDIT_NAME})

    def test_drill_excluded_names_applications_json_and_no_full_coverage_claim(self):
        # The legacy applications.json is the primary applications store
        # and is NOT covered: the report must say so explicitly and must
        # never imply complete DR coverage.
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            report = backup.drill(d, PASSPHRASE)
            self.assertTrue(report.ok, report.detail)
            self.assertIn("applications.json", report.excluded)
            self.assertIn("stable schemas only", report.detail.lower())
            self.assertIn("excluded", report.detail.lower())

    def test_drill_gap_analysis_covers_extra_named_profiles(self):
        # Extra globbed profiles/*.json files are part of the backup, so
        # the gap analysis must cover them (the profile schema has no
        # record_key; files are keyed by relative path).
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            extra = d / "profiles" / "extra - named profile.json"
            extra.write_text(
                json.dumps({"schema_version": "profile-v1"}), encoding="utf-8"
            )
            report = backup.drill(d, PASSPHRASE)
            self.assertTrue(report.ok, report.detail)
            self.assertEqual(report.record_gap.get("profile"), 0)

    def test_drill_rejects_nonexistent_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                backup.drill(Path(tmp) / "no-such-dir", PASSPHRASE)

    def test_record_ids_covers_unkeyed_profile_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _make_data_dir(Path(tmp))
            extra = d / "profiles" / "extra.json"
            extra.write_text(
                json.dumps({"schema_version": "profile-v1"}), encoding="utf-8"
            )
            ids = backup._record_ids(d)
            self.assertIn("file:profiles/extra.json", ids["profile"])
            self.assertIn("file:profiles/profile.json", ids["profile"])
            self.assertIn("synth-event-000", ids["outcome-event"])
            extra.unlink()
            self.assertNotIn("file:profiles/extra.json", backup._record_ids(d)["profile"])


if __name__ == "__main__":
    unittest.main()
