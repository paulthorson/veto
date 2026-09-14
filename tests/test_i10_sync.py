"""Initiative 10 — opt-in sync tests (synthetic fixtures only).

Sync is local-only by default; these tests prove opt-in behavior,
envelope opacity to the vault host, pairing-code agreement,
deterministic merge with no silent record loss, pre-merge snapshots,
UTC merge ordering, init/join guards, passphrase resolution without
argv, dataset-dispatched merge, exact envelope filename parsing,
atomic sequence allocation, and the doctor diagnostic.
"""

import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i10 import backup, sync

PASSPHRASE = "synthetic-sync-passphrase"


def _event(i: int, device: str = "a") -> dict:
    return {
        "event_id": f"synth-ev-{device}-{i:03d}",
        "application_id": f"synth-job-{i:03d}",
        "event_type": "applied",
        "occurred_at": "2026-09-01T10:00:00",
        "source": "synthetic-fixture",
        "role": "Synthetic Engineer",
        "provenance": {"actor": "test"},
        "schema_version": "outcome-min-v0",
        "recorded_at": "2026-09-01T10:00:01",
    }


def _data_dir(root: Path, device: str, n: int = 3) -> Path:
    d = root / f"data-{device}"
    (d / "profiles").mkdir(parents=True)
    (d / "outcomes.jsonl").write_text(
        "\n".join(json.dumps(_event(i, device)) for i in range(n)) + "\n",
        encoding="utf-8",
    )
    (d / "profiles" / "profile.json").write_text(
        json.dumps({"schema_version": "profile-v1", "updated_at": "2026-09-01T00:00:00"}),
        encoding="utf-8",
    )
    return d


class TestOptIn(unittest.TestCase):
    def test_sync_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp), "a")
            self.assertFalse(sync.status(d)["enabled"])
            with self.assertRaises(RuntimeError):
                sync.sync_round(d, PASSPHRASE)

    def test_enable_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp), "a")
            result = sync.enable(d, PASSPHRASE, str(Path(tmp) / "vault"), mode="init")
            self.assertTrue(result["enabled"])
            self.assertTrue(sync.status(d)["enabled"])
            self.assertRegex(result["pairing_code"], r"^(\w+-){5}\w+$")

    def test_disable_returns_to_local_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp), "a")
            sync.enable(d, PASSPHRASE, str(Path(tmp) / "vault"), mode="init")
            sync.disable(d)
            self.assertFalse(sync.status(d)["enabled"])


class TestEnableModes(unittest.TestCase):
    def test_mode_is_required_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp), "a")
            with self.assertRaises(TypeError):
                sync.enable(d, PASSPHRASE, str(Path(tmp) / "vault"))  # type: ignore[call-arg]
            with self.assertRaises(ValueError):
                sync.enable(d, PASSPHRASE, str(Path(tmp) / "vault"), mode="maybe")

    def test_init_refuses_existing_salt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            db = _data_dir(root, "b")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            # A second device re-running --init against the same vault must
            # refuse, not silently start a divergent group.
            with self.assertRaises(ValueError) as ctx:
                sync.enable(db, PASSPHRASE, str(root / "vault"), mode="init")
            self.assertIn("join", str(ctx.exception).lower())

    def test_join_refuses_missing_salt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = _data_dir(root, "a")
            # Pointing at a wrong/empty folder must refuse loudly instead of
            # silently generating a divergent salt.
            with self.assertRaises(ValueError) as ctx:
                sync.enable(d, PASSPHRASE, str(root / "empty-vault"), mode="join")
            self.assertIn("init", str(ctx.exception).lower())

    def test_join_reuses_salt_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            db = _data_dir(root, "b")
            ra = sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            rb = sync.enable(db, PASSPHRASE, str(root / "vault"), mode="join")
            self.assertEqual(ra["pairing_code"], rb["pairing_code"])
            self.assertEqual(ra["key_fingerprint"], rb["key_fingerprint"])

    def test_concurrent_init_detect_and_refuse(self):
        # Two devices racing --init: the loser of the atomic publish must
        # refuse, not overwrite the winner's salt.
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            (vault / sync.VAULT_SALT_NAME).write_text(
                base64.b64encode(os.urandom(16)).decode("ascii"), encoding="utf-8"
            )
            d = _data_dir(Path(tmp), "a")
            with mock.patch("os.link", side_effect=FileExistsError):
                with self.assertRaises(ValueError) as ctx:
                    sync._publish_salt_atomic(vault)
            self.assertIn("join", str(ctx.exception).lower())


class TestPassphraseResolution(unittest.TestCase):
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {sync.PASSPHRASE_ENV_VAR: "env-pass"}):
            with mock.patch.object(sync.sys, "stdin", io.StringIO("stdin-pass\n")):
                self.assertEqual(sync.resolve_passphrase(), "env-pass")

    def test_piped_stdin(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sync.PASSPHRASE_ENV_VAR, None)
            with mock.patch.object(sync.sys, "stdin", io.StringIO("piped-pass\n")):
                self.assertEqual(sync.resolve_passphrase(), "piped-pass")

    def test_empty_stdin_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sync.PASSPHRASE_ENV_VAR, None)
            with mock.patch.object(sync.sys, "stdin", io.StringIO("")):
                with self.assertRaises(ValueError):
                    sync.resolve_passphrase()

    def test_interactive_getpass(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(sync.PASSPHRASE_ENV_VAR, None)
            fake_stdin = io.StringIO("")
            fake_stdin.isatty = lambda: True  # type: ignore[method-assign]
            with mock.patch.object(sync.sys, "stdin", fake_stdin):
                with mock.patch("getpass.getpass", return_value="typed-pass") as gp:
                    self.assertEqual(sync.resolve_passphrase(), "typed-pass")
                    gp.assert_called_once()

    def test_cli_has_no_passphrase_flag(self):
        # --passphrase must not exist anywhere in the CLI (argv-visible).
        for cmd in ("enable", "sync", "pairing-code", "doctor"):
            argv = ["--data-dir", "x", cmd]
            if cmd == "enable":
                argv += ["--vault", "y", "--init"]
            with self.assertRaises(SystemExit):
                sync.main(argv + ["--passphrase", "nope"])

    def test_cli_enable_via_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = _data_dir(root, "a")
            env = {sync.PASSPHRASE_ENV_VAR: PASSPHRASE}
            with mock.patch.dict(os.environ, env):
                rc = sync.main(
                    ["--data-dir", str(d), "enable", "--vault", str(root / "vault"), "--init"]
                )
            self.assertEqual(rc, 0)
            self.assertTrue(sync.status(d)["enabled"])


class TestPairing(unittest.TestCase):
    def test_same_passphrase_same_pairing_code(self):
        salt = os.urandom(16)
        k1 = sync.derive_sync_key(PASSPHRASE, salt)
        k2 = sync.derive_sync_key(PASSPHRASE, salt)
        self.assertEqual(sync.pairing_code(k1), sync.pairing_code(k2))

    def test_different_passphrase_different_code(self):
        salt = os.urandom(16)
        k1 = sync.derive_sync_key(PASSPHRASE, salt)
        k2 = sync.derive_sync_key("another-synthetic-passphrase", salt)
        self.assertNotEqual(sync.pairing_code(k1), sync.pairing_code(k2))

    def test_threat_model_documented_honestly(self):
        doc = sync.__doc__
        self.assertIn("fully trusted group", doc.lower().replace("=", " ").replace("  ", " "))
        self.assertIn("no forward secrecy", doc.lower())
        self.assertIn("typo", doc.lower())


class TestSyncRound(unittest.TestCase):
    def test_two_devices_converge_with_no_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            da = _data_dir(root, "a", n=3)
            db = _data_dir(root, "b", n=4)
            sync.enable(da, PASSPHRASE, str(vault), mode="init")
            sync.enable(db, PASSPHRASE, str(vault), mode="join")

            ra = sync.sync_round(da, PASSPHRASE)
            self.assertEqual(ra["peers_merged"], 0)  # first device: no peers yet
            rb = sync.sync_round(db, PASSPHRASE)
            self.assertEqual(rb["peers_merged"], 1)
            ra2 = sync.sync_round(da, PASSPHRASE)
            self.assertEqual(ra2["peers_merged"], 1)

            for d in (da, db):
                ids = {
                    json.loads(line)["event_id"]
                    for line in (d / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
                self.assertEqual(len(ids), 7)  # 3 + 4, union, no dupes

    def test_vault_sees_ciphertext_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            sync.sync_round(da, PASSPHRASE)
            blobs = list((root / "vault").glob("*.vetosync"))
            self.assertTrue(blobs)
            raw = blobs[0].read_bytes()
            self.assertNotIn(b"Synthetic Engineer", raw)
            self.assertNotIn(b"synth-ev-a", raw)

    def test_wrong_passphrase_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            with self.assertRaises(ValueError) as ctx:
                sync.sync_round(da, "wrong-synthetic-passphrase")
            self.assertIn("passphrase", str(ctx.exception).lower())

    def test_vault_salt_mismatch_is_not_a_passphrase_error(self):
        # C8: tampered/replaced salt must be diagnosed as a salt problem,
        # not misreported as a wrong passphrase.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            (root / "vault" / sync.VAULT_SALT_NAME).write_text(
                base64.b64encode(os.urandom(16)).decode("ascii"), encoding="utf-8"
            )
            with self.assertRaises(ValueError) as ctx:
                sync.sync_round(da, PASSPHRASE)
            msg = str(ctx.exception).lower()
            self.assertIn("salt", msg)
            self.assertNotIn("passphrase", msg)

    def test_corrupt_envelope_error_says_passphrase_or_corruption(self):
        # C8: undecryptable peer envelope names both possibilities since
        # Fernet cannot distinguish them.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            salt = base64.b64decode(sync.load_config(da)["salt_b64"])
            good_key = sync.derive_sync_key(PASSPHRASE, salt)
            other_key = sync.derive_sync_key("different-synthetic-passphrase", salt)
            env = sync.build_envelope(da, other_key, "peer1", 1)
            with self.assertRaises(ValueError) as ctx:
                sync.Envelope.from_bytes(env.to_bytes(other_key), good_key)
            msg = str(ctx.exception).lower()
            self.assertIn("passphrase", msg)
            self.assertIn("corrupt", msg)

    def test_bad_magic_is_not_a_passphrase_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            salt = base64.b64decode(sync.load_config(da)["salt_b64"])
            key = sync.derive_sync_key(PASSPHRASE, salt)
            with self.assertRaises(ValueError) as ctx:
                sync.Envelope.from_bytes(b"NOTANENVELOPE-xxxx", key)
            self.assertNotIn("passphrase", str(ctx.exception).lower())

    def test_audit_log_has_counts_not_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            sync.sync_round(da, PASSPHRASE)
            audit = (da / sync.AUDIT_NAME).read_text(encoding="utf-8")
            self.assertNotIn("Synthetic Engineer", audit)


class TestMerge(unittest.TestCase):
    def test_newer_remote_record_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a", n=1)
            env = sync.Envelope(
                device_id="peer1",
                sequence=1,
                created_at="2026-09-02T00:00:00Z",
                schema_versions={},
                datasets={
                    "outcome-event": {
                        "outcomes.jsonl": [
                            {**_event(0, "a"), "recorded_at": "2026-09-02T00:00:00Z", "role": "Updated Role"}
                        ]
                    }
                },
            )
            report = sync.merge_envelope(da, env, "local")
            self.assertTrue(report["merged"])
            rec = json.loads((da / "outcomes.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(rec["role"], "Updated Role")

    def test_own_envelope_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            env = sync.Envelope("local", 1, "2026-09-02T00:00:00Z", {}, {})
            self.assertFalse(sync.merge_envelope(da, env, "local")["merged"])


class TestTimestamps(unittest.TestCase):
    def test_utc_stamps_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp), "a")
            sync.enable(d, PASSPHRASE, str(Path(tmp) / "vault"), mode="init")
            cfg = sync.load_config(d)
            self.assertRegex(cfg["enabled_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            env = sync.build_envelope(d, b"0" * 44, "dev1", 1)
            self.assertRegex(env.created_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_aware_stamps_compare_across_zones(self):
        # Same instant in different zones compares equal; later wins.
        self.assertEqual(
            sync._ts_order_key("2026-09-13T19:00:00+00:00"),
            sync._ts_order_key("2026-09-13T15:00:00-04:00"),
        )
        self.assertEqual(
            sync._ts_order_key("2026-09-13T19:00:00Z"),
            sync._ts_order_key("2026-09-13T15:00:00-04:00"),
        )
        self.assertGreater(
            sync._ts_order_key("2026-09-13T19:00:01Z"),
            sync._ts_order_key("2026-09-13T15:00:00-04:00"),
        )

    def test_naive_legacy_stamps_treated_as_utc(self):
        # Documented migration rule: pre-1.1 naive stamps are read as UTC.
        self.assertEqual(
            sync._ts_order_key("2026-09-01T10:00:00"),
            sync._ts_order_key("2026-09-01T10:00:00+00:00"),
        )
        # Naive-vs-naive keeps legacy relative order.
        self.assertGreater(
            sync._ts_order_key("2026-09-02T00:00:00"),
            sync._ts_order_key("2026-09-01T00:00:00"),
        )

    def test_merge_prefers_newer_across_zones(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)  # recorded_at naive legacy
            env = sync.Envelope(
                device_id="peer1",
                sequence=1,
                created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={
                    "outcome-event": {
                        "outcomes.jsonl": [
                            {
                                **_event(0, "a"),
                                # Same wall-clock as local but +00:00 vs
                                # local naive-read-as-UTC: one second later.
                                "recorded_at": "2026-09-01T10:00:02+00:00",
                                "role": "Zone Aware Role",
                            }
                        ]
                    }
                },
            )
            sync.merge_envelope(da, env, "local")
            rec = json.loads((da / "outcomes.jsonl").read_text(encoding="utf-8").strip())
            self.assertEqual(rec["role"], "Zone Aware Role")


class TestNeverSilentDiscard(unittest.TestCase):
    def test_corrupt_line_reported_snapshotted_and_preserved_in_backup(self):
        # RULE 1: a corrupt local line + sync_round -> valid records kept,
        # corrupt line VISIBLE in merge report + audit log, pre-merge
        # snapshot exists holding the original bytes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            da = _data_dir(root, "a", n=3)
            db = _data_dir(root, "b", n=4)
            sync.enable(da, PASSPHRASE, str(vault), mode="init")
            sync.enable(db, PASSPHRASE, str(vault), mode="join")

            corrupt = "THIS IS NOT JSON {{{"
            with (da / "outcomes.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(corrupt + "\n")

            ra = sync.sync_round(da, PASSPHRASE)
            # Build side surfaces the unparseable line instead of hiding it.
            self.assertTrue(
                any(
                    d.get("line") == 4
                    for d in ra["unpublished_unparseable"]
                ),
                f"expected corrupt line 4 in report, got {ra['unpublished_unparseable']}",
            )

            sync.sync_round(db, PASSPHRASE)
            ra2 = sync.sync_round(da, PASSPHRASE)
            self.assertEqual(ra2["peers_merged"], 1)
            merge_report = ra2["merges"][0]
            self.assertTrue(merge_report["merged"])

            # Valid records preserved: 3 local + 4 remote, union, no dupes.
            ids = {
                json.loads(line)["event_id"]
                for line in (da / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            self.assertEqual(len(ids), 7)
            # Corrupt line is gone from the rewritten file...
            self.assertNotIn(corrupt, (da / "outcomes.jsonl").read_text(encoding="utf-8"))

            # ...but VISIBLE in the merge report with line number + hash.
            dropped = merge_report["datasets"]["outcome-event"]["outcomes.jsonl"]["dropped"]
            corrupt_drops = [d for d in dropped if d.get("line") == 4]
            self.assertEqual(len(corrupt_drops), 1)
            self.assertIn("hash", corrupt_drops[0])
            self.assertIn("reason", corrupt_drops[0])
            self.assertNotIn(corrupt, json.dumps(dropped))  # no content leaked

            # ...and in the audit log.
            audit = (da / sync.AUDIT_NAME).read_text(encoding="utf-8")
            self.assertIn("sync_merged", audit)
            self.assertIn('"line": 4', audit)

            # ...and the pre-merge snapshot holds the original bytes.
            snap_name = merge_report["premerge_snapshot"]
            snap_file = da / sync.PREMERGE_DIR_NAME / snap_name / "outcomes.jsonl"
            self.assertTrue(snap_file.is_file())
            self.assertIn(corrupt, snap_file.read_text(encoding="utf-8"))

    def test_invalid_remote_record_dropped_visibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            bad = {**_event(9, "b"), "event_type": "not-a-real-type"}
            env = sync.Envelope(
                device_id="peer1",
                sequence=1,
                created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"outcome-event": {"outcomes.jsonl": [bad, _event(8, "b")]}},
            )
            report = sync.merge_envelope(da, env, "local")
            fr = report["datasets"]["outcome-event"]["outcomes.jsonl"]
            self.assertEqual(fr["added"], 1)  # only the valid one merged
            bad_drops = [d for d in fr["dropped"] if d.get("key") == "synth-ev-b-009"]
            self.assertEqual(len(bad_drops), 1)
            self.assertIn("event_type", bad_drops[0]["reason"])
            ids = {
                json.loads(line)["event_id"]
                for line in (da / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            self.assertNotIn("synth-ev-b-009", ids)
            self.assertIn("synth-ev-b-008", ids)

    def test_snapshots_pruned_to_keep_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            for i in range(sync.PREMERGE_KEEP + 3):
                env = sync.Envelope(
                    device_id="peer1", sequence=i + 1, created_at="2026-09-13T19:00:00Z",
                    schema_versions={},
                    # Distinct event per merge: every merge really changes
                    # the file, so every merge takes a snapshot (no-op
                    # merges deliberately take none — see
                    # test_noop_merge_takes_no_snapshot).
                    datasets={"outcome-event": {"outcomes.jsonl": [_event(100 + i, "b")]}},
                )
                sync.merge_envelope(da, env, "local")
            snaps = list((da / sync.PREMERGE_DIR_NAME).iterdir())
            self.assertEqual(len(snaps), sync.PREMERGE_KEEP)
            # The survivors must be the 5 NEWEST, not 5 arbitrary ones:
            # the newest snapshot holds the pre-merge state of the 8th
            # merge (events b-100..b-106 present, b-107 not yet merged).
            newest = max(snaps, key=lambda p: p.name)
            snap_text = (newest / "outcomes.jsonl").read_text(encoding="utf-8")
            self.assertIn("synth-ev-b-106", snap_text)
            self.assertNotIn("synth-ev-b-107", snap_text)

    def test_noop_merge_takes_no_snapshot(self):
        # A merge that changes nothing must not create a snapshot dir or
        # rewrite the file — otherwise every sync round churns the
        # snapshot history even when the data already converged.
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            payload = [_event(7, "b")]
            env = sync.Envelope(
                device_id="peer1", sequence=1, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"outcome-event": {"outcomes.jsonl": payload}},
            )
            r1 = sync.merge_envelope(da, env, "local")
            self.assertEqual(r1["datasets"]["outcome-event"]["outcomes.jsonl"]["added"], 1)
            snaps1 = sorted(p.name for p in (da / sync.PREMERGE_DIR_NAME).iterdir())
            self.assertEqual(len(snaps1), 1)
            before = (da / "outcomes.jsonl").read_bytes()

            env.sequence = 2  # same payload, second merge is a no-op
            r2 = sync.merge_envelope(da, env, "local")
            fr = r2["datasets"]["outcome-event"]["outcomes.jsonl"]
            self.assertEqual(fr["added"], 0)
            self.assertEqual(fr["updated"], 0)
            self.assertNotIn("premerge_snapshot", r2)
            self.assertEqual(
                sorted(p.name for p in (da / sync.PREMERGE_DIR_NAME).iterdir()), snaps1
            )
            self.assertEqual((da / "outcomes.jsonl").read_bytes(), before)

            # Same for LWW: an identical document is not "replaced".
            prof = json.loads((da / "profiles" / "profile.json").read_text(encoding="utf-8"))
            env_p = sync.Envelope(
                device_id="peer1", sequence=3, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"profile": {"profiles/profile.json": prof}},
            )
            rp = sync.merge_envelope(da, env_p, "local")
            self.assertFalse(rp["datasets"]["profile"]["profiles/profile.json"]["replaced"])
            self.assertEqual(
                sorted(p.name for p in (da / sync.PREMERGE_DIR_NAME).iterdir()), snaps1
            )

    def test_sync_merged_audit_carries_exact_drop_counts(self):
        # The merge report caps "dropped" at MAX_DROPPED_PER_FILE per
        # file, but the durable audit record must carry the EXACT
        # per-file dropped_total/dropped_truncated so the audit log
        # never disagrees with the returned report on how much was
        # dropped.
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            bad = [{"nope": f"missing-record-key-{i}"} for i in range(60)]
            good = [_event(999, "b")]
            env = sync.Envelope(
                device_id="peer1", sequence=1, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"outcome-event": {"outcomes.jsonl": bad + good}},
            )
            report = sync.merge_envelope(da, env, "local")
            fr = report["datasets"]["outcome-event"]["outcomes.jsonl"]
            self.assertEqual(fr["dropped_total"], 60)
            self.assertEqual(fr["dropped_truncated"], 10)
            self.assertEqual(len(fr["dropped"]), sync.MAX_DROPPED_PER_FILE)

            entries = [
                json.loads(line)
                for line in (da / sync.AUDIT_NAME).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            merged_entries = [e for e in entries if e.get("action") == "sync_merged"]
            self.assertTrue(merged_entries, "expected a sync_merged audit entry")
            audit = merged_entries[-1]
            self.assertEqual(audit["dropped_total"], 60)
            self.assertEqual(audit["dropped_truncated"], 10)
            self.assertEqual(
                audit["dropped_per_file"],
                [
                    {
                        "dataset": "outcome-event",
                        "rel": "outcomes.jsonl",
                        "dropped_total": 60,
                        "dropped_truncated": 10,
                    }
                ],
            )


class TestDispatchByDataset(unittest.TestCase):
    def test_outcomes_meta_dict_does_not_fall_into_profile_lww(self):
        # C9: outcomes_meta.json is a dict under outcome-event (record_key
        # set) -> shape error, never profile LWW.
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            env = sync.Envelope(
                device_id="peer1", sequence=1, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"outcome-event": {"outcomes_meta.json": {"some": "dict"}}},
            )
            report = sync.merge_envelope(da, env, "local")
            fr = report["datasets"]["outcome-event"]["outcomes_meta.json"]
            self.assertIn("error", fr)
            self.assertIn("shape mismatch", fr["error"])
            self.assertFalse((da / "outcomes_meta.json").exists())

    def test_unknown_dataset_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            env = sync.Envelope(
                device_id="peer1", sequence=1, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"nope": {"x.json": []}},
            )
            report = sync.merge_envelope(da, env, "local")
            self.assertIn("error", report["datasets"]["nope"])

    def test_profile_still_lww(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            env = sync.Envelope(
                device_id="peer1", sequence=1, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={
                    "profile": {
                        "profiles/profile.json": {
                            "schema_version": "profile-v1",
                            "updated_at": "2026-09-13T19:00:00Z",
                            "note": "remote wins",
                        }
                    }
                },
            )
            report = sync.merge_envelope(da, env, "local")
            self.assertTrue(report["datasets"]["profile"]["profiles/profile.json"]["replaced"])
            doc = json.loads((da / "profiles" / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["note"], "remote wins")

    def test_path_traversal_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            da = _data_dir(Path(tmp), "a", n=1)
            env = sync.Envelope(
                device_id="peer1", sequence=1, created_at="2026-09-13T19:00:00Z",
                schema_versions={},
                datasets={"profile": {"../evil.json": {"schema_version": "profile-v1"}}},
            )
            report = sync.merge_envelope(da, env, "local")
            fr = report["datasets"]["profile"]["../evil.json"]
            self.assertIn("error", fr)
            self.assertFalse((Path(tmp) / "evil.json").exists())


class TestEnvelopeFilenames(unittest.TestCase):
    def test_parse_exact_fields(self):
        self.assertEqual(
            sync.parse_envelope_filename("envelope-abc123def456-000007.vetosync"),
            ("abc123def456", 7),
        )
        self.assertIsNone(sync.parse_envelope_filename("envelope-abc123-000007.vetosync"))
        self.assertIsNone(sync.parse_envelope_filename("envelope-abc123def4567-000007.vetosync"))
        self.assertIsNone(sync.parse_envelope_filename("random.txt"))

    def test_list_matches_device_id_exactly_not_substring(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = sync.DirectoryVault(Path(tmp))
            own = "abc123def456"
            names = [
                f"envelope-{own}-000001.vetosync",          # own -> excluded
                "envelope-abc123def4567-000001.vetosync",  # 13 hex: malformed -> ignored
                "envelope-abc123-000001.vetosync",          # short: malformed -> ignored
                "envelope-aaaabbbbcccc-000001.vetosync",    # peer -> listed
                "notes.vetosync",                           # malformed -> ignored
            ]
            for n in names:
                (Path(tmp) / n).write_bytes(b"x")
            listed = [p.name for p in vault.list(own_device_id=own)]
            self.assertEqual(listed, ["envelope-aaaabbbbcccc-000001.vetosync"])


class TestSequenceAllocation(unittest.TestCase):
    def test_concurrent_publish_bumps_sequence(self):
        # C11: a pre-existing envelope file for our next sequence (a
        # concurrent local sync_round won it) -> bump and retry, never
        # overwrite.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            da = _data_dir(root, "a")
            sync.enable(da, PASSPHRASE, str(root / "vault"), mode="init")
            device_id = sync.load_config(da)["device_id"]
            squatter = root / "vault" / f"envelope-{device_id}-000001.vetosync"
            squatter.write_bytes(b"already-published-by-concurrent-round")
            report = sync.sync_round(da, PASSPHRASE)
            self.assertEqual(report["sequence"], 2)
            self.assertTrue(report["published"].endswith("-000002.vetosync"))
            self.assertEqual(
                squatter.read_bytes(), b"already-published-by-concurrent-round"
            )


class TestDoctor(unittest.TestCase):
    def _enabled_pair(self, root: Path):
        vault = root / "vault"
        da = _data_dir(root, "a")
        db = _data_dir(root, "b")
        sync.enable(da, PASSPHRASE, str(vault), mode="init")
        sync.enable(db, PASSPHRASE, str(vault), mode="join")
        sync.sync_round(da, PASSPHRASE)
        sync.sync_round(db, PASSPHRASE)
        return da, db, vault

    def test_doctor_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            da, _db, _vault = self._enabled_pair(Path(tmp))
            result = sync.doctor(da, PASSPHRASE)
            self.assertTrue(result["ok"], result["checks"])
            checks = result["checks"]
            self.assertTrue(checks["enabled"])
            self.assertTrue(checks["vault_writable"])
            self.assertTrue(checks["salt_agrees"])
            self.assertTrue(checks["passphrase_matches"])
            self.assertGreaterEqual(checks["peer_envelopes_decryptable"], 1)
            self.assertTrue(checks["sequence_sane"])

    def test_doctor_flags_salt_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            da, _db, vault = self._enabled_pair(Path(tmp))
            (vault / sync.VAULT_SALT_NAME).write_text(
                base64.b64encode(os.urandom(16)).decode("ascii"), encoding="utf-8"
            )
            result = sync.doctor(da, PASSPHRASE)
            self.assertFalse(result["ok"])
            self.assertFalse(result["checks"]["salt_agrees"])
            self.assertIn("salt_error", result["checks"])

    def test_doctor_flags_wrong_passphrase(self):
        with tempfile.TemporaryDirectory() as tmp:
            da, _db, _vault = self._enabled_pair(Path(tmp))
            result = sync.doctor(da, "wrong-synthetic-passphrase")
            self.assertFalse(result["ok"])
            self.assertFalse(result["checks"]["passphrase_matches"])

    def test_doctor_flags_undecryptable_peer_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            da, db, vault = self._enabled_pair(Path(tmp))
            # Corrupt a peer envelope's ciphertext in place.
            peer_files = [
                p for p in vault.glob("*.vetosync")
                if sync.parse_envelope_filename(p.name)[0] != sync.load_config(da)["device_id"]
            ]
            self.assertTrue(peer_files)
            raw = bytearray(peer_files[0].read_bytes())
            raw[-10] ^= 0xFF
            peer_files[0].write_bytes(bytes(raw))
            result = sync.doctor(da, PASSPHRASE)
            self.assertFalse(result["ok"])
            self.assertTrue(result["checks"]["peer_envelopes_failed"])

    def test_doctor_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            da, _db, _vault = self._enabled_pair(Path(tmp))
            with mock.patch.dict(os.environ, {sync.PASSPHRASE_ENV_VAR: PASSPHRASE}):
                rc = sync.main(["--data-dir", str(da), "doctor"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
