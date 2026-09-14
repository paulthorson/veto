"""Initiative 10 — release channels + PWA bundle tests (synthetic)."""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from initiatives.i10 import channels, pwa

REPO_ROOT = Path(__file__).resolve().parent.parent


def _data_dir(root: Path) -> Path:
    d = root / "data"
    (d / "profiles").mkdir(parents=True)
    (d / "outcomes.jsonl").write_text(
        json.dumps({
            "event_id": "synth-1",
            "application_id": "synth-job-1",
            "event_type": "applied",
            "occurred_at": "2026-09-01T10:00:00",
            "source": "synthetic-fixture",
            "role": "Synthetic Engineer",
            "provenance": {"actor": "test"},
            "schema_version": "outcome-min-v0",
        }) + "\n",
        encoding="utf-8",
    )
    (d / "profiles" / "profile.json").write_text(
        json.dumps({"schema_version": "profile-v1"}), encoding="utf-8"
    )
    return d


class TestChannels(unittest.TestCase):
    def test_channel_manifests_readable(self):
        versions = channels.current_versions()
        self.assertIn("stable", versions)
        self.assertIn("preview", versions)
        self.assertEqual(
            versions["stable"]["schema_versions"]["outcome-event"], "outcome-min-v0"
        )

    def test_unknown_channel_rejected(self):
        with self.assertRaises(ValueError):
            channels.read_channel("nightly")

    def test_migration_notes_render(self):
        notes = channels.migration_notes("0.0.9", "0.1.0")
        self.assertIn("0.0.9 → 0.1.0", notes)
        self.assertIn("outcome-min-v0", notes)
        self.assertIn("rollback", notes.lower())
        self.assertIn("Back up before upgrading", notes)

    def test_compatibility_checks_pass_on_healthy_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp))
            results = channels.check_compatibility(d)
            self.assertTrue(results)
            failed = [r.name for r in results if not r.ok]
            self.assertEqual(failed, [], f"failing checks: {failed}")
            self.assertTrue(channels.compatibility_ok(results))

    def test_compatibility_checks_fail_on_corrupt_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp))
            (d / "outcomes.jsonl").write_text(
                json.dumps({"event_id": "bad", "schema_version": "outcome-min-v0"}) + "\n",
                encoding="utf-8",
            )
            results = channels.check_compatibility(d)
            self.assertFalse(channels.compatibility_ok(results))

    def test_promote_blocked_on_failing_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _data_dir(Path(tmp))
            (d / "outcomes.jsonl").write_text("garbage\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                channels.promote_preview_to_stable(d)


class TestPwaBundle(unittest.TestCase):
    def _internal_results(self):
        # Bundle-internal coherence, excluding the webui-wiring gate
        # (covered explicitly below).
        return pwa.validate_bundle(check_wiring=False)

    def test_bundle_internal_checks_pass(self):
        results = self._internal_results()
        failed = [(c.name, c.detail) for c in results if not c.ok]
        self.assertEqual(failed, [], f"failing PWA checks: {failed}")
        self.assertTrue(pwa.bundle_ok(results))

    def test_bundle_not_shippable_without_webui_wiring(self):
        # Honest fail (F1/F2): the webui.py serving/registration wiring has
        # not landed, so the bundle is NOT installable as-shipped and
        # validate_bundle() must be red, not green.
        results = pwa.validate_bundle()
        wiring = [c for c in results if c.name == "webui-wiring"]
        self.assertEqual(len(wiring), 1)
        self.assertFalse(wiring[0].ok, "webui-wiring must fail until the wiring lands")
        self.assertIn("integration_notes.md", wiring[0].detail)
        self.assertFalse(pwa.bundle_ok(results))

    def test_webui_wiring_check_passes_with_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            webui = Path(tmp) / "webui.py"
            webui.write_text("\n".join(pwa.WEBUI_WIRING_MARKERS), encoding="utf-8")
            check = pwa.check_webui_wiring(webui)
            self.assertTrue(check.ok, check.detail)

    def test_webui_wiring_check_lists_missing_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            webui = Path(tmp) / "webui.py"
            webui.write_text("# nothing wired yet\n", encoding="utf-8")
            check = pwa.check_webui_wiring(webui)
            self.assertFalse(check.ok)
            self.assertIn("/service-worker.js", check.detail)

    def test_version_stamp_fresh_and_stale_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "pwa"
            shutil.copytree(pwa.PWA_DIR, bundle)
            # Fresh stamp validates...
            self.assertEqual(
                pwa.read_stamp(bundle), pwa.bundle_fingerprint(bundle)[:16]
            )
            results = pwa.validate_bundle(pwa_dir=bundle, check_wiring=False)
            fresh = [c for c in results if c.name == "sw-version-fresh"][0]
            self.assertTrue(fresh.ok, fresh.detail)
            # ...and any byte change under pwa/ makes it stale (CI red).
            with (bundle / "service-worker.js").open("a", encoding="utf-8") as fh:
                fh.write("\n/* drift */\n")
            results = pwa.validate_bundle(pwa_dir=bundle, check_wiring=False)
            fresh = [c for c in results if c.name == "sw-version-fresh"][0]
            self.assertFalse(fresh.ok)
            self.assertIn("stamp", fresh.detail)

    def test_stamp_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "pwa"
            shutil.copytree(pwa.PWA_DIR, bundle)
            (bundle / "sw-version.js").unlink()
            version = pwa.write_version_stamp(bundle)
            self.assertEqual(version, pwa.bundle_fingerprint(bundle)[:16])
            self.assertEqual(pwa.read_stamp(bundle), version)

    def test_single_owner_invariant(self):
        # B1: the worker must never fan out queue messages; the page owns
        # the queue.
        results = {c.name: c for c in self._internal_results()}
        self.assertTrue(results["sw-single-owner"].ok, results["sw-single-owner"].detail)

    def test_manifest_is_installable(self):
        manifest = json.loads(
            (pwa.PWA_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["display"], "standalone")
        self.assertTrue(manifest["icons"])
        self.assertTrue(manifest["start_url"].startswith("/"))
        self.assertTrue(manifest.get("id"), "manifest needs an explicit id (F13)")
        self.assertTrue(
            any("maskable" in str(i.get("purpose", "")) for i in manifest["icons"]),
            "manifest needs a maskable icon purpose (F13)",
        )

    def test_deferred_queue_api_surface(self):
        q = (pwa.PWA_DIR / "deferred-queue.js").read_text(encoding="utf-8")
        for api in ("submit", "enqueueRequest", "replay", "confirmReplay",
                    "pending", "deadLetter", "retryDead", "discardDead",
                    "indexedDB", "Idempotency-Key"):
            self.assertIn(api, q)


class TestPwaJsHarness(unittest.TestCase):
    """Runtime-level tests (F7): the node harness loads the REAL
    service-worker.js and deferred-queue.js with stubbed platform globals
    and drives install/fetch/queue/replay end to end."""

    def test_node_harness_passes(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available — cannot run the JS runtime harness")
        harness = Path(__file__).parent / "i10_pwa_harness.mjs"
        proc = subprocess.run(
            [node, str(harness)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"node PWA harness failed:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("0 failed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
