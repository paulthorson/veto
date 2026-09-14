#!/usr/bin/env python3
"""Adversarial tests (Initiative 11): every bypass attempt must fail.

These tests ARE the proof-of-value evidence: a new extension can prove
what it accesses and CANNOT bypass the core confirmation or governance
gates. Each attack below is a real hostile extension run through the
real Host. If any attack reports blocked=False, the design is wrong —
do not ship.
"""

import unittest

from initiatives.i11.policy_kit.attacks import ALL_ATTACKS, run_adversarial_suite
from initiatives.i11.policy_kit.checks import run_policy_suite

REF_EXT = "initiatives/i11/reference/role-radar-digest"


class TestAdversarialSuite(unittest.TestCase):
    def test_all_attacks_blocked(self):
        report = run_adversarial_suite()
        self.assertEqual(report["breached"], [],
                         f"BREACHES: {report['results']}")
        self.assertEqual(report["verdict"], "HOLD")
        self.assertEqual(report["blocked"], len(ALL_ATTACKS))

    def test_each_attack_individually(self):
        for name, fn in ALL_ATTACKS:
            with self.subTest(attack=name):
                blocked, evidence = fn()
                self.assertTrue(blocked, f"attack {name!r} succeeded: {evidence}")


class TestReferenceExtensionPolicy(unittest.TestCase):
    def test_reference_passes_full_policy_suite(self):
        report = run_policy_suite(REF_EXT)
        failures = [r for r in report["results"] if not r["passed"]]
        self.assertEqual(failures, [], f"policy failures: {failures}")
        self.assertEqual(report["failed"], 0)

    def test_reference_cannot_send_or_submit(self):
        # The reference extension declares no confirm-required actions and
        # no network destinations: there is no code path to submission.
        from initiatives.i11.manifest.schema import load_manifest
        manifest = load_manifest(REF_EXT)
        kinds = {a["kind"] for a in manifest["permissions"]["actions"]}
        self.assertNotIn("confirm-required", kinds)
        self.assertEqual(manifest["permissions"]["network"]["destinations"], [])


if __name__ == "__main__":
    unittest.main()
