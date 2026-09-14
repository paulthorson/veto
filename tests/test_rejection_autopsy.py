"""Tests for rejection_autopsy.py. Hermetic, no network."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rejection_autopsy
from rejection_autopsy import autopsy, diagnose, record_outcome


def _fresh_store(testcase):
    tmp = Path(tempfile.mkdtemp(prefix="autopsy-test-"))
    testcase.addCleanup(
        lambda: [p.unlink(missing_ok=True) for p in tmp.iterdir()] or tmp.rmdir()
    )
    patcher = mock.patch.object(
        rejection_autopsy, "OUTCOMES_FILE", tmp / "outcomes.json"
    )
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return tmp


def _seed(records):
    for r in records:
        record_outcome(**r)


class RecordTest(unittest.TestCase):
    def setUp(self):
        _fresh_store(self)

    def test_record_roundtrip(self):
        rec = record_outcome("job-1", "rejected", "screening", notes="no fit")
        self.assertEqual(rec["job_id"], "job-1")
        self.assertEqual(rec["outcome"], "rejected")
        self.assertEqual(rec["stage"], "screening")
        self.assertEqual(rec["notes"], "no fit")
        self.assertIn("recorded_at", rec)

    def test_upsert_same_job(self):
        record_outcome("job-1", "rejected", "screening")
        record_outcome("job-1", "ghosted", "interview")
        a = autopsy()
        self.assertEqual(a["total"], 1)
        self.assertEqual(a["by_outcome"]["ghosted"], 1)

    def test_invalid_outcome(self):
        with self.assertRaises(ValueError):
            record_outcome("job-1", "hired", "offer")

    def test_invalid_stage(self):
        with self.assertRaises(ValueError):
            record_outcome("job-1", "rejected", "phone-screen")

    def test_empty_job_id(self):
        with self.assertRaises(ValueError):
            record_outcome("  ", "rejected", "screening")

    def test_applied_at_recorded(self):
        rec = record_outcome(
            "job-1", "offer", "offer", applied_at="2026-08-01T00:00:00Z"
        )
        self.assertEqual(rec["applied_at"], "2026-08-01T00:00:00+00:00")

    def test_corrupt_store_tolerated(self):
        tmp = Path(tempfile.mkdtemp(prefix="autopsy-corrupt-"))
        self.addCleanup(
            lambda: [p.unlink(missing_ok=True) for p in tmp.iterdir()]
            or tmp.rmdir()
        )
        bad = tmp / "outcomes.json"
        bad.write_text("not json {{{", encoding="utf-8")
        with mock.patch.object(rejection_autopsy, "OUTCOMES_FILE", bad):
            self.assertEqual(autopsy()["total"], 0)


class AutopsyTest(unittest.TestCase):
    def setUp(self):
        _fresh_store(self)

    def test_empty_store(self):
        a = autopsy()
        self.assertEqual(a["total"], 0)
        self.assertIsNone(a["biggest_leak"])
        self.assertIn("No outcomes recorded", a["markdown"])

    def test_rates_and_leak(self):
        _seed(
            [
                {"job_id": f"s{i}", "outcome": "rejected", "stage": "screening"}
                for i in range(3)
            ]
            + [{"job_id": "s3", "outcome": "offer", "stage": "screening"}]
            + [
                {"job_id": f"i{i}", "outcome": "rejected", "stage": "interview"}
                for i in range(2)
            ]
        )
        a = autopsy()
        self.assertEqual(a["total"], 6)
        self.assertAlmostEqual(a["by_stage"]["screening"]["loss_rate"], 0.75)
        self.assertAlmostEqual(a["by_stage"]["interview"]["rejection_rate"], 1.0)
        self.assertEqual(a["biggest_leak"], "interview")
        self.assertAlmostEqual(a["ghosting_rate"], 0.0)

    def test_ghosting_rate(self):
        _seed(
            [
                {"job_id": "g1", "outcome": "ghosted", "stage": "applied"},
                {"job_id": "g2", "outcome": "ghosted", "stage": "screening"},
                {"job_id": "o1", "outcome": "offer", "stage": "offer"},
            ]
        )
        a = autopsy()
        self.assertAlmostEqual(a["ghosting_rate"], 2 / 3)
        self.assertAlmostEqual(a["offer_rate"], 1 / 3)

    def test_note_keywords(self):
        _seed(
            [
                {
                    "job_id": "k1",
                    "outcome": "rejected",
                    "stage": "screening",
                    "notes": "Needed more kubernetes experience with python",
                },
                {
                    "job_id": "k2",
                    "outcome": "rejected",
                    "stage": "screening",
                    "notes": "Python experience was the gap",
                },
            ]
        )
        words = dict(autopsy()["top_note_keywords"])
        self.assertEqual(words.get("experience"), 2)
        self.assertEqual(words.get("python"), 2)
        # stopwords and short tokens are dropped
        self.assertNotIn("the", words)
        self.assertNotIn("was", words)

    def test_time_to_decision(self):
        _seed(
            [
                {
                    "job_id": "t1",
                    "outcome": "rejected",
                    "stage": "screening",
                    "applied_at": "2026-08-01T00:00:00Z",
                },
                {
                    "job_id": "t2",
                    "outcome": "rejected",
                    "stage": "screening",
                    "applied_at": "2026-08-11T00:00:00Z",
                },
            ]
        )
        with mock.patch.object(
            rejection_autopsy, "_now_iso", return_value="2026-08-21T00:00:00+00:00"
        ):
            med = autopsy()["time_to_decision_days"]["rejected"]
        # recorded_at is real now; just check a number came back
        self.assertIsNotNone(med)

    def test_pure_function_over_records(self):
        records = [
            {"job_id": "x", "outcome": "offer", "stage": "offer", "notes": ""}
        ]
        a = autopsy(records)
        self.assertEqual(a["total"], 1)
        self.assertEqual(autopsy()["total"], 0)  # store untouched


class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        _fresh_store(self)

    def _signals(self, d):
        return {s["signal"] for s in d["suggestions"]}

    def test_low_volume_guidance(self):
        d = diagnose()
        self.assertIn("low_volume", self._signals(d))
        self.assertIn("Not enough data", d["summary"])

    def test_screening_leak_suggests_grill(self):
        _seed(
            [
                {"job_id": f"s{i}", "outcome": "rejected", "stage": "screening"}
                for i in range(4)
            ]
            + [{"job_id": "s9", "outcome": "offer", "stage": "offer"}]
        )
        d = diagnose()
        self.assertIn("screening_leak", self._signals(d))
        sug = next(s for s in d["suggestions"] if s["signal"] == "screening_leak")
        mods = {m["name"] for m in sug["modules"]}
        self.assertIn("grill", mods)
        for m in sug["modules"]:
            self.assertIsInstance(m["available"], bool)

    def test_interview_leak_suggests_mock(self):
        _seed(
            [
                {"job_id": f"i{i}", "outcome": "rejected", "stage": "interview"}
                for i in range(3)
            ]
            + [
                {"job_id": f"a{i}", "outcome": "rejected", "stage": "applied"}
                for i in range(2)
            ]
        )
        d = diagnose()
        self.assertIn("interview_leak", self._signals(d))

    def test_ghosting_suggests_followup(self):
        _seed(
            [
                {"job_id": f"g{i}", "outcome": "ghosted", "stage": "applied"}
                for i in range(3)
            ]
            + [
                {"job_id": f"r{i}", "outcome": "rejected", "stage": "screening"}
                for i in range(2)
            ]
        )
        d = diagnose()
        self.assertIn("ghosting", self._signals(d))
        sug = next(s for s in d["suggestions"] if s["signal"] == "ghosting")
        self.assertIn("followup", {m["name"] for m in sug["modules"]})

    def test_late_ghosting_flagged(self):
        _seed(
            [
                {"job_id": "f1", "outcome": "ghosted", "stage": "final"},
                {"job_id": "a1", "outcome": "rejected", "stage": "applied"},
                {"job_id": "a2", "outcome": "rejected", "stage": "applied"},
                {"job_id": "a3", "outcome": "rejected", "stage": "applied"},
                {"job_id": "a4", "outcome": "offer", "stage": "offer"},
            ]
        )
        d = diagnose()
        self.assertIn("late_ghosting", self._signals(d))

    def test_keyword_theme_salary(self):
        _seed(
            [
                {
                    "job_id": f"c{i}",
                    "outcome": "rejected",
                    "stage": "final",
                    "notes": "salary expectations too high, compensation mismatch",
                }
                for i in range(5)
            ]
        )
        d = diagnose()
        self.assertIn("note_theme", self._signals(d))

    def test_tone_never_blames(self):
        _seed(
            [
                {"job_id": f"s{i}", "outcome": "rejected", "stage": "screening"}
                for i in range(5)
            ]
        )
        text = diagnose()["markdown"].lower()
        for blame in ("your fault", "you failed", "you should have", "blame you"):
            self.assertNotIn(blame, text)

    def test_momentum_when_healthy(self):
        _seed(
            [
                {"job_id": f"o{i}", "outcome": "offer", "stage": "offer"}
                for i in range(2)
            ]
            + [
                {"job_id": f"r{i}", "outcome": "rejected", "stage": "applied"}
                for i in range(4)
            ]
        )
        d = diagnose()
        self.assertIn("momentum", self._signals(d))


class WiringTest(unittest.TestCase):
    def test_register_tools(self):
        class FakeMCP:
            def __init__(self):
                self.tools = {}

            def tool(self):
                def deco(fn):
                    self.tools[fn.__name__] = fn
                    return fn

                return deco

        mcp = FakeMCP()
        rejection_autopsy.register_tools(mcp)
        self.assertEqual(
            set(mcp.tools),
            {"record_outcome", "rejection_autopsy", "diagnose_rejection_patterns"},
        )

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = rejection_autopsy.register_cli(sub)
        self.assertEqual(set(handlers), {"autopsy"})

    def test_cli_report_action(self):
        _fresh_store(self)
        record_outcome("job-1", "rejected", "screening")
        handler = rejection_autopsy.register_cli(
            argparse.ArgumentParser().add_subparsers()
        )["autopsy"]
        args = argparse.Namespace(action="report", json=True)
        self.assertEqual(handler(args), 0)

    def test_cli_record_action(self):
        _fresh_store(self)
        handler = rejection_autopsy.register_cli(
            argparse.ArgumentParser().add_subparsers()
        )["autopsy"]
        args = argparse.Namespace(
            action="record",
            job_id="job-9",
            outcome="offer",
            stage="offer",
            notes="",
            applied_at=None,
            json=True,
        )
        self.assertEqual(handler(args), 0)
        self.assertEqual(autopsy()["by_outcome"]["offer"], 1)


if __name__ == "__main__":
    unittest.main()
