#!/usr/bin/env python3
"""Policy-kit suite for the Role Radar Digest extension. Fails closed."""
import unittest
from pathlib import Path

from initiatives.i11.policy_kit.checks import run_policy_suite

EXT_DIR = Path(__file__).resolve().parent.parent


class TestPolicy(unittest.TestCase):
    def test_policy_suite_passes(self):
        report = run_policy_suite(EXT_DIR)
        failures = [r for r in report["results"] if not r["passed"]]
        self.assertEqual(failures, [],
                         f"policy failures: {failures}")


if __name__ == "__main__":
    unittest.main()
