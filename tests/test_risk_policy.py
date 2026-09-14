"""Tests for governance/risk_policy.py — framework-governed risk adjudication.

The framework is stubbed (no real agentic-governance install needed);
compliance state is pointed at a temp file.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import compliance
from governance import risk_policy


class _FakeMod:
    """Stand-in for the framework server module."""

    def __init__(self, veto_hits=(), fail_veto=False):
        self.veto_hits = list(veto_hits)
        self.fail_veto = fail_veto
        self.recorded = []

    def check_veto(self, domain, text):
        if self.fail_veto:
            raise RuntimeError("veto backend down")
        return {"veto_hits": list(self.veto_hits)}

    def record_verdict(self, domain, verdict, summary, ticket, case_tag):
        self.recorded.append(
            {"verdict": verdict, "ticket": ticket, "case_tag": case_tag}
        )
        return {"recorded": True}


class RiskPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cpath = Path(self.tmp.name) / "compliance.json"
        self.state = compliance._default_state()
        compliance.save_state(self.state, self.cpath)
        self.fake = _FakeMod()
        patcher = mock.patch.object(
            risk_policy.adapter, "load_framework", return_value=self.fake
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        # Route the real compliance file location at the temp state so
        # adjudicate_* (which take no path arg) use it.
        cpatcher = mock.patch.object(
            compliance, "COMPLIANCE_FILE", self.cpath
        )
        self.addCleanup(cpatcher.stop)
        cpatcher.start()

    def _fresh(self):
        return compliance.load_state(self.cpath)

    # -- search ------------------------------------------------------

    def test_search_allowed_official(self):
        out = risk_policy.adjudicate_search("greenhouse", "python")
        self.assertTrue(out["allowed"])
        self.assertEqual(out["tier"], "official")
        self.assertEqual(out["governance"], "adjudicated")
        self.assertTrue(
            any(r["ticket"].startswith("job-apply:risk:search")
                for r in self.fake.recorded)
        )

    def test_search_blocked_by_compliance_gate(self):
        # strict mode blocks scraping-tier before the framework is consulted
        st = self._fresh()
        compliance.set_mode("strict", st, self.cpath)
        out = risk_policy.adjudicate_search("linkedin", "python")
        self.assertFalse(out["allowed"])
        self.assertIn("strict", out["reason"])
        self.assertFalse(
            [r for r in self.fake.recorded if r["verdict"] == "pass"
             and "search:linkedin" in r["ticket"]]
        )

    def test_search_blocked_by_veto_hit(self):
        st = self._fresh()
        compliance.acknowledge_risks(st, self.cpath)
        self.fake.veto_hits = ["tos-violation"]
        out = risk_policy.adjudicate_search("linkedin", "python")
        self.assertFalse(out["allowed"])
        self.assertIn("veto", out["reason"])

    def test_search_veto_error_fails_closed(self):
        st = self._fresh()
        compliance.acknowledge_risks(st, self.cpath)
        self.fake.fail_veto = True
        out = risk_policy.adjudicate_search("linkedin", "python")
        self.assertFalse(out["allowed"])

    def test_search_no_framework_falls_back_to_compliance(self):
        with mock.patch.object(
            risk_policy.adapter, "load_framework",
            side_effect=risk_policy.adapter.GovernanceUnavailable("nope"),
        ):
            out = risk_policy.adjudicate_search("greenhouse", "python")
        self.assertTrue(out["allowed"])
        self.assertEqual(out["governance"], "compliance-only")
        self.assertIn("warning", out)

    # -- apply -------------------------------------------------------

    def test_apply_scraping_browser_blocked_by_policy(self):
        out = risk_policy.adjudicate_apply(
            "linkedin", "Engineer", "Acme", via="browser"
        )
        self.assertFalse(out["allowed"])
        self.assertIn("fill-only", out["reason"])

    def test_apply_official_ats_allowed(self):
        out = risk_policy.adjudicate_apply(
            "lever", "Engineer", "Acme", via="ats"
        )
        self.assertTrue(out["allowed"])
        self.assertNotIn("fill_only", out)

    def test_apply_daily_cap_blocks(self):
        st = self._fresh()
        for _ in range(compliance.DEFAULT_DAILY_APPLY_CAP):
            compliance.record_application(st, self.cpath)
            st = self._fresh()
        out = risk_policy.adjudicate_apply("lever", "E", "A", via="ats")
        self.assertFalse(out["allowed"])
        self.assertIn("cap", out["reason"])

    # -- automation --------------------------------------------------

    def test_automation_veto_blocks(self):
        self.fake.veto_hits = ["bulk"]
        out = risk_policy.adjudicate_automation(
            "bulk-apply", "apply to 500 jobs at once"
        )
        self.assertFalse(out["allowed"])

    def test_automation_allowed_records_verdict(self):
        out = risk_policy.adjudicate_automation(
            "queue-run", "process due queue items at human pace"
        )
        self.assertTrue(out["allowed"])
        self.assertTrue(
            any("automation:queue-run" in r["ticket"] for r in self.fake.recorded)
        )

    # -- audit -------------------------------------------------------

    def test_audit_flags_repeated_blocks(self):
        st = self._fresh()
        # "linkedin" was removed from the board registry (legal-hardening
        # commit 4); use a surviving board for the repeated-block audit.
        for _ in range(3):
            compliance.record_block("greenhouse", st, self.cpath)
            st = self._fresh()
        audit = risk_policy.audit_risk(st)
        severities = [f["severity"] for f in audit["findings"]]
        self.assertIn("critical", severities)

    def test_audit_clean_state(self):
        audit = risk_policy.audit_risk(self._fresh())
        self.assertTrue(audit["findings"])


if __name__ == "__main__":
    unittest.main()
