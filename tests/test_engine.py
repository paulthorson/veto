"""Tests for governance/engine.py — limen engine bridge."""

import unittest
from unittest import mock

from governance import engine


class EngineTest(unittest.TestCase):
    def test_detect_engine_shape(self):
        with mock.patch.object(engine.adapter, "find_governance_root",
                               return_value=None), \
             mock.patch.object(engine.adapter, "governance_enabled",
                               return_value=False), \
             mock.patch("shutil.which", return_value=None):
            status = engine.detect_engine()
        self.assertFalse(status["limen_installed"])
        self.assertIsNone(status["wizard_engine"])
        self.assertEqual(status["lifecycle"], ["spawn", "work", "review", "merge"])

    def test_wizard_engine_missing(self):
        with mock.patch.object(engine.adapter, "find_governance_root",
                               return_value=None):
            self.assertIsNone(engine.wizard_engine())

    def test_run_governed_job_success(self):
        spawn = {"allowed": True, "reason": "", "governance": "adjudicated",
                 "verdict": {"recorded": True}}
        review = {"verdict": "PASS", "notes": "fine"}
        fake_mod = mock.Mock()
        fake_mod.run_review.return_value = review
        with mock.patch.object(engine.risk_policy, "adjudicate_automation",
                               return_value=spawn), \
             mock.patch.object(engine.risk_policy, "record_risk_verdict",
                               return_value={"recorded": True}), \
             mock.patch.object(engine.adapter, "load_framework",
                               return_value=fake_mod), \
             mock.patch.object(engine, "detect_engine",
                               return_value={"active_engine": "none"}):
            report = engine.run_governed_job(
                "queue-run", lambda: {"applied": 2}, "test")
        self.assertTrue(report["spawn"]["allowed"])
        self.assertTrue(report["result"]["ok"])
        self.assertEqual(report["result"]["value"], {"applied": 2})
        self.assertEqual(report["review"], review)
        self.assertTrue(report["verdict"]["recorded"])

    def test_run_governed_job_spawn_veto_stops_work(self):
        called = []
        spawn = {"allowed": False, "reason": "veto", "governance": "adjudicated",
                 "verdict": {"recorded": True}}
        with mock.patch.object(engine.risk_policy, "adjudicate_automation",
                               return_value=spawn), \
             mock.patch.object(engine, "detect_engine",
                               return_value={"active_engine": "none"}):
            report = engine.run_governed_job(
                "bulk", lambda: called.append(1) or {}, "test")
        self.assertFalse(report["spawn"]["allowed"])
        self.assertEqual(called, [])
        self.assertIsNone(report["result"])

    def test_run_governed_job_work_error_captured(self):
        spawn = {"allowed": True, "reason": "", "governance": "adjudicated",
                 "verdict": {"recorded": True}}

        def boom():
            raise RuntimeError("kaput")

        with mock.patch.object(engine.risk_policy, "adjudicate_automation",
                               return_value=spawn), \
             mock.patch.object(engine.risk_policy, "record_risk_verdict",
                               return_value={"recorded": True}), \
             mock.patch.object(engine.adapter, "load_framework",
                               side_effect=engine.adapter.GovernanceUnavailable("x")), \
             mock.patch.object(engine, "detect_engine",
                               return_value={"active_engine": "none"}):
            report = engine.run_governed_job("x", boom, "test")
        self.assertFalse(report["result"]["ok"])
        self.assertIn("kaput", report["result"]["error"])
        # review failure is advisory, verdict still recorded
        self.assertIn("warning", report["review"])
        self.assertTrue(report["verdict"]["recorded"])


if __name__ == "__main__":
    unittest.main()
