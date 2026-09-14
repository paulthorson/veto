#!/usr/bin/env python3
"""Extension platform surface tests via crew.py (Initiative 11 wiring)."""

import unittest

import crew

REF_EXT = "initiatives/i11/reference/role-radar-digest"


class TestCrewExtensionSurface(unittest.TestCase):
    def test_verify_reference(self):
        result = crew.ext_verify(REF_EXT)
        self.assertTrue(result["ok"], result)

    def test_verify_missing_dir_fails_closed(self):
        result = crew.ext_verify("/nonexistent/extension")
        self.assertFalse(result["ok"])

    def test_policy_test_reference(self):
        report = crew.ext_policy_test(REF_EXT)
        self.assertEqual(report["failed"], 0, report["results"])

    def test_adversarial_holds(self):
        report = crew.ext_adversarial()
        self.assertEqual(report["verdict"], "HOLD")
        self.assertEqual(report["breached"], [])

    def test_audit_verify(self):
        verdict = crew.ext_audit_verify()
        self.assertTrue(verdict["ok"], verdict)

    def test_install_and_run_reference(self):
        installed = crew.ext_install(REF_EXT)
        self.assertTrue(installed["ok"], installed)
        self.assertEqual(installed["extension"], "role-radar-digest")
        listed = crew.ext_list()
        ids = [e["id"] for e in listed["extensions"]]
        self.assertIn("role-radar-digest", ids)
        ran = crew.ext_run("role-radar-digest", "role-radar-digest-read")
        self.assertTrue(ran["ok"], ran)
        drafted = crew.ext_run("role-radar-digest", "role-radar-digest-draft")
        self.assertTrue(drafted["ok"], drafted)

    def test_run_unknown_extension_fails_closed(self):
        result = crew.ext_run("no-such-ext", "no-action")
        self.assertFalse(result["ok"])

    def test_register_cli_includes_extension(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        commands = crew.register_cli(sub)
        self.assertIn("crew", commands)
        self.assertIn("extension", commands)


if __name__ == "__main__":
    unittest.main()
