"""Initiative 10 — installer tests (synthetic; no real installs)."""

import json
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i10 import installer


class TestEnvironmentChecks(unittest.TestCase):
    def test_checks_return_named_results(self):
        checks = installer.check_environment()
        names = {c.name for c in checks}
        self.assertTrue({"python", "pip", "disk", "install-dir"} <= names)

    def test_python_check_passes_on_modern_python(self):
        checks = {c.name: c for c in installer.check_environment()}
        self.assertTrue(checks["python"].ok)

    def test_environment_ok_logic(self):
        ok = [installer.Check("a", True, "d")]
        bad = [installer.Check("a", False, "d", severity="error")]
        warn = [installer.Check("a", False, "d", severity="warning")]
        self.assertTrue(installer.environment_ok(ok))
        self.assertFalse(installer.environment_ok(bad))
        self.assertTrue(installer.environment_ok(warn))  # warnings never block


class TestInstallRecord(unittest.TestCase):
    def test_write_install_record_stamps_profile_v1(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install-root"
            answers = installer.OnboardingAnswers(
                full_name="Test User",
                email="test@example.com",
                data_dir=str(Path(tmp) / "data"),
                channel="stable",
            )
            with mock.patch.object(installer, "INSTALL_RECORD", root / "install.json"):
                rec_path = installer.write_install_record(answers, install_root=root)
            record = json.loads(rec_path.read_text(encoding="utf-8"))
            self.assertEqual(record["channel"], "stable")
            self.assertFalse(record["sync_opt_in"])  # local-only default
            profile = json.loads(
                (Path(tmp) / "data" / "profiles" / "profile.json").read_text(encoding="utf-8")
            )
            self.assertEqual(profile["schema_version"], "profile-v1")

    def test_write_install_record_is_idempotent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            answers = installer.OnboardingAnswers(data_dir=str(Path(tmp) / "data"))
            with mock.patch.object(installer, "INSTALL_RECORD", root / "install.json"):
                installer.write_install_record(answers, install_root=root)
                installer.write_install_record(answers, install_root=root)
            self.assertTrue((root / "install.json").exists())


class TestUninstallSafety(unittest.TestCase):
    def test_uninstall_never_purges_data_without_confirmation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            (data / "outcomes.jsonl").write_text("{}\n", encoding="utf-8")
            root = Path(tmp) / "iroot"
            root.mkdir()
            (root / "install.json").write_text(
                json.dumps({"data_dir": str(data)}), encoding="utf-8"
            )
            with mock.patch.object(installer, "INSTALL_RECORD", root / "install.json"):
                with self.assertRaises(ValueError):
                    installer.uninstall(root, purge_data=True, confirm="yes please")
            # Data survives the refused purge.
            self.assertTrue((data / "outcomes.jsonl").exists())

    def test_uninstall_purges_only_with_exact_confirmation(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            root = Path(tmp) / "iroot"
            root.mkdir()
            (root / "install.json").write_text(
                json.dumps({"data_dir": str(data)}), encoding="utf-8"
            )
            with mock.patch.object(installer, "INSTALL_RECORD", root / "install.json"):
                result = installer.uninstall(root, purge_data=True, confirm="PURGE MY DATA")
            self.assertEqual(result["purged_data"], [str(data)])
            self.assertFalse(data.exists())

    def test_uninstall_without_purge_keeps_data(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            root = Path(tmp) / "iroot"
            root.mkdir()
            (root / "install.json").write_text(
                json.dumps({"data_dir": str(data)}), encoding="utf-8")
            with mock.patch.object(installer, "INSTALL_RECORD", root / "install.json"):
                result = installer.uninstall(root)
            self.assertEqual(result["purged_data"], [])
            self.assertTrue(data.exists())

    def test_installer_never_clobbers_foreign_veto_shim(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            (fake_home / ".local" / "bin").mkdir(parents=True)
            foreign = fake_home / ".local" / "bin" / "veto"
            foreign.write_text("#!/usr/bin/env bash\necho user-tool\n", encoding="utf-8")
            with mock.patch.object(Path, "home", return_value=fake_home):
                shim = installer.install_shim(install_root=Path(tmp) / "iroot")
            # Untouched: still the user's file.
            self.assertEqual(
                foreign.read_text(encoding="utf-8"), "#!/usr/bin/env bash\necho user-tool\n"
            )
            self.assertEqual(shim, foreign)

    def test_uninstall_leaves_foreign_veto_shim_alone(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            (fake_home / ".local" / "bin").mkdir(parents=True)
            foreign = fake_home / ".local" / "bin" / "veto"
            foreign.write_text("#!/usr/bin/env bash\necho user-tool\n", encoding="utf-8")
            root = Path(tmp) / "iroot"
            root.mkdir()
            (root / "install.json").write_text(json.dumps({"data_dir": str(Path(tmp)/"d")}), encoding="utf-8")
            with mock.patch.object(Path, "home", return_value=fake_home):
                with mock.patch.object(installer, "INSTALL_RECORD", root / "install.json"):
                    result = installer.uninstall(root)
            self.assertTrue(foreign.exists())
            self.assertNotIn(str(foreign), result["removed"])


if __name__ == "__main__":
    unittest.main()
