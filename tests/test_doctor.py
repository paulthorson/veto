#!/usr/bin/env python3
"""Tests for the setup doctor (doctor.py).

Stdlib unittest only. Module-level paths are monkeypatched to temp
dirs; subprocess and the heavy server import are stubbed. The real
``profiles/``, ``compliance.json`` etc. are never touched.

Run:  cd ~/workspace/veto && python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import compliance
import doctor


def _good_profile():
    return {
        "full_name": "Ada Lovelace",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+1 555-0100",
        "location": "New York, NY",
        "linkedin_url": "https://www.linkedin.com/in/adalovelace",
        "website": "",
        "cover_letter": "",
    }


class TestCheckProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "profile.json"

    def test_missing_profile_fails(self):
        result = doctor.check_profile(path=self.profile)
        self.assertFalse(result["ok"])
        self.assertIn("wizard", result["detail"])

    def test_good_profile_passes(self):
        self.profile.write_text(json.dumps(_good_profile()), encoding="utf-8")
        result = doctor.check_profile(path=self.profile)
        self.assertTrue(result["ok"])
        self.assertIn("Ada Lovelace", result["detail"])

    def test_missing_keys_fail(self):
        data = _good_profile()
        del data["email"]
        self.profile.write_text(json.dumps(data), encoding="utf-8")
        result = doctor.check_profile(path=self.profile)
        self.assertFalse(result["ok"])
        self.assertIn("email", result["detail"])


class TestCheckCompliance(unittest.TestCase):
    def test_summary_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "compliance.json"
            with mock.patch.object(compliance, "COMPLIANCE_FILE", fake):
                result = doctor.check_compliance()
        self.assertTrue(result["ok"])
        self.assertIn("mode=", result["detail"])
        self.assertIn("applications_today=", result["detail"])


class TestCheckPlaywright(unittest.TestCase):
    def _run(self, **kwargs):
        proc = mock.MagicMock()
        proc.configure_mock(**kwargs)
        with mock.patch("subprocess.run", return_value=proc) as m:
            result = doctor.check_playwright()
        return result, m

    def test_installed_and_browsers(self):
        with tempfile.TemporaryDirectory() as tmp:
            browsers = Path(tmp) / ".cache" / "ms-playwright"
            (browsers / "chromium-1").mkdir(parents=True)
            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                result, _ = self._run(returncode=0, stdout="Version 1.0\n")
        self.assertTrue(result["ok"])
        self.assertEqual(result["severity"], "warning")

    def test_not_installed_is_warning_not_error(self):
        result, _ = self._run(returncode=1, stdout="", stderr="nope")
        self.assertFalse(result["ok"])
        self.assertEqual(result["severity"], "warning")

    def test_subprocess_crash_is_warning(self):
        with mock.patch(
            "subprocess.run", side_effect=OSError("nope")
        ):
            result = doctor.check_playwright()
        self.assertFalse(result["ok"])
        self.assertEqual(result["severity"], "warning")


class TestCheckSessions(unittest.TestCase):
    def test_missing_dir_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = doctor.check_sessions(path=Path(tmp) / "nope")
        self.assertFalse(result["ok"])
        self.assertEqual(result["severity"], "warning")

    def test_counts_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "linkedin.json").write_text("{}", encoding="utf-8")
            result = doctor.check_sessions(path=d)
        self.assertTrue(result["ok"])
        self.assertIn("1 saved session", result["detail"])


class TestCheckBoards(unittest.TestCase):
    def test_lists_boards(self):
        fake_server = mock.MagicMock()
        fake_server.list_boards.return_value = [
            {"board": "greenhouse"},
            {"board": "lever"},
        ]
        with mock.patch.dict(sys.modules, {"server": fake_server}):
            result = doctor.check_boards()
        self.assertTrue(result["ok"])
        self.assertIn("2 board(s)", result["detail"])

    def test_import_failure(self):
        with mock.patch.dict(sys.modules, {"server": None}):
            result = doctor.check_boards()
        self.assertFalse(result["ok"])


class TestRunDoctor(unittest.TestCase):
    def test_shape_and_never_raises(self):
        # A crashing check must degrade to a failure entry, never kill
        # the doctor; warning-severity misses must not fail it.
        good = {"name": "x", "ok": True, "detail": "d", "severity": "error"}
        warn = {"name": "y", "ok": False, "detail": "d", "severity": "warning"}
        with mock.patch.object(
            doctor, "check_profile", side_effect=Exception("boom")
        ), mock.patch.object(doctor, "check_compliance",
                             return_value=good), \
            mock.patch.object(doctor, "check_playwright",
                              return_value=warn), \
            mock.patch.object(doctor, "check_sessions",
                              return_value=warn), \
            mock.patch.object(doctor, "check_preferences",
                              return_value=good), \
            mock.patch.object(doctor, "check_boards",
                              return_value=good):
            report = doctor.run_doctor()
        self.assertIn("ok", report)
        self.assertIn("checks", report)
        self.assertEqual(len(report["checks"]), 6)
        for check in report["checks"]:
            self.assertEqual(
                set(check), {"name", "ok", "detail", "severity"}
            )
        crashed = next(
            c for c in report["checks"] if "check crashed" in c["detail"]
        )
        self.assertFalse(crashed["ok"])
        # The crashed error-severity check fails the doctor...
        self.assertFalse(report["ok"])

    def test_warnings_do_not_fail_doctor(self):
        good = {"name": "x", "ok": True, "detail": "d", "severity": "error"}
        warn = {"name": "y", "ok": False, "detail": "d", "severity": "warning"}
        with mock.patch.object(doctor, "check_profile", return_value=good), \
             mock.patch.object(doctor, "check_compliance", return_value=good), \
             mock.patch.object(doctor, "check_playwright", return_value=warn), \
             mock.patch.object(doctor, "check_sessions", return_value=warn), \
             mock.patch.object(doctor, "check_preferences", return_value=good), \
             mock.patch.object(doctor, "check_boards", return_value=good):
            report = doctor.run_doctor()
        self.assertTrue(report["ok"])
        json.dumps(report)  # JSON-serializable

    def test_register_contracts(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command", required=True)
        doctor.register_cli(sub)
        args = parser.parse_args(["doctor"])
        self.assertTrue(callable(args.func))

        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn

                return deco

        doctor.register_tools(FakeMCP())
        self.assertEqual(set(seen), {"compliance_status"})
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "compliance.json"
            with mock.patch.object(compliance, "COMPLIANCE_FILE", fake):
                summary = seen["compliance_status"]()
        self.assertIn("mode", summary)


if __name__ == "__main__":
    unittest.main()
