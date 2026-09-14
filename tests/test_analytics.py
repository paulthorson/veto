#!/usr/bin/env python3
"""Unit tests for analytics.py (no network, deterministic timestamps)."""

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import analytics

NOW = datetime(2026, 2, 1, 12, 0, 0, tzinfo=timezone.utc)


def _entry(job_id, board, stage, history, submitted_at=None):
    return {
        "job_id": job_id,
        "board": board,
        "title": f"Title {job_id}",
        "company": "Acme",
        "location": "New York, NY",
        "stage": stage,
        "stage_history": history,
        "submitted_at": submitted_at or history[0]["at"],
        "follow_up_due": None,
    }


def _ev(stage, at):
    return {"stage": stage, "at": at, "note": ""}


def sample_applications():
    """Five entries with fixed timestamps covering every interesting case."""
    return [
        # A: greenhouse, applied -> interviewing (4d to first response)
        _entry(
            "greenhouse:aaa", "greenhouse", "interviewing",
            [_ev("applied", "2026-01-01T09:00:00+00:00"),
             _ev("interviewing", "2026-01-05T09:00:00+00:00")],
        ),
        # B: greenhouse, applied -> rejected (9d to first response)
        _entry(
            "greenhouse:bbb", "greenhouse", "rejected",
            [_ev("applied", "2026-01-01T09:00:00+00:00"),
             _ev("rejected", "2026-01-10T09:00:00+00:00")],
        ),
        # C: lever, stuck in applied since 2026-01-01 -> stale
        _entry(
            "lever:ccc", "lever", "applied",
            [_ev("applied", "2026-01-01T09:00:00+00:00")],
        ),
        # D: lever, applied -> interviewing -> offer (2d to first response)
        _entry(
            "lever:ddd", "lever", "offer",
            [_ev("applied", "2026-01-20T09:00:00+00:00"),
             _ev("interviewing", "2026-01-22T09:00:00+00:00"),
             _ev("offer", "2026-01-25T09:00:00+00:00")],
        ),
        # E: ashby, applied -> withdrawn (1d; not a "response")
        _entry(
            "ashby:eee", "ashby", "withdrawn",
            [_ev("applied", "2026-01-28T09:00:00+00:00"),
             _ev("withdrawn", "2026-01-29T09:00:00+00:00")],
        ),
    ]


class TestFunnel(unittest.TestCase):
    def test_counts_and_rates(self):
        f = analytics.funnel(sample_applications())
        self.assertEqual(f["total"], 5)
        self.assertEqual(f["current_stage_counts"]["applied"], 1)
        self.assertEqual(f["current_stage_counts"]["interviewing"], 1)
        self.assertEqual(f["current_stage_counts"]["offer"], 1)
        self.assertEqual(f["current_stage_counts"]["rejected"], 1)
        self.assertEqual(f["current_stage_counts"]["withdrawn"], 1)
        self.assertEqual(f["current_stage_counts"]["ghosted"], 0)
        # reached interviewing: A and D (B went straight to rejected)
        self.assertEqual(f["reached_interviewing"], 2)
        self.assertEqual(f["reached_offer"], 1)
        self.assertEqual(f["applied_to_interview_rate"], 0.4)
        self.assertEqual(f["interview_to_offer_rate"], 0.5)
        self.assertEqual(f["applied_to_offer_rate"], 0.2)

    def test_empty_is_safe(self):
        f = analytics.funnel([])
        self.assertEqual(f["total"], 0)
        self.assertEqual(f["applied_to_interview_rate"], 0.0)
        self.assertEqual(f["interview_to_offer_rate"], 0.0)


class TestResponseRateByBoard(unittest.TestCase):
    def test_per_board(self):
        r = analytics.response_rate_by_board(sample_applications())
        self.assertEqual(r["greenhouse"], {"applied": 2, "responded": 2, "rate": 1.0})
        self.assertEqual(r["lever"], {"applied": 2, "responded": 1, "rate": 0.5})
        self.assertEqual(r["ashby"], {"applied": 1, "responded": 0, "rate": 0.0})

    def test_empty(self):
        self.assertEqual(analytics.response_rate_by_board([]), {})


class TestTimeToFirstResponse(unittest.TestCase):
    def test_medians(self):
        r = analytics.time_to_first_response(sample_applications())
        self.assertEqual(r["greenhouse"]["median_days"], 6.5)  # median(4, 9)
        self.assertEqual(r["greenhouse"]["samples"], 2)
        self.assertEqual(r["lever"]["median_days"], 2.0)
        self.assertEqual(r["lever"]["samples"], 1)
        self.assertEqual(r["ashby"]["median_days"], 1.0)

    def test_board_with_no_stage_change_reports_none(self):
        apps = [_entry("x:1", "glassdoor", "applied",
                       [_ev("applied", "2026-01-15T09:00:00+00:00")])]
        r = analytics.time_to_first_response(apps)
        self.assertIsNone(r["glassdoor"]["median_days"])
        self.assertEqual(r["glassdoor"]["samples"], 0)


class TestStaleApplications(unittest.TestCase):
    def test_only_stale_applied(self):
        stale = analytics.stale_applications(
            sample_applications(), days=14, now=NOW
        )
        self.assertEqual(len(stale), 1)
        item = stale[0]
        self.assertEqual(item["job_id"], "lever:ccc")
        self.assertEqual(item["board"], "lever")
        self.assertEqual(item["days_stale"], 31)

    def test_custom_threshold(self):
        stale = analytics.stale_applications(
            sample_applications(), days=60, now=NOW
        )
        self.assertEqual(stale, [])

    def test_non_applied_stages_never_stale(self):
        apps = [_entry("x:1", "greenhouse", "interviewing",
                       [_ev("applied", "2025-01-01T09:00:00+00:00"),
                        _ev("interviewing", "2025-01-02T09:00:00+00:00")])]
        stale = analytics.stale_applications(apps, days=14, now=NOW)
        self.assertEqual(stale, [])


class TestGenerateReport(unittest.TestCase):
    def test_report_shape_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "applications.json"
            path.write_text(json.dumps(sample_applications()), encoding="utf-8")
            report = analytics.generate_report(
                path=path, stale_days=14, now=NOW
            )
        self.assertEqual(report["total_applications"], 5)
        self.assertEqual(report["generated_at"], NOW.isoformat())
        self.assertEqual(report["funnel"]["total"], 5)
        self.assertIn("greenhouse", report["response_rate_by_board"])
        self.assertIn("lever", report["time_to_first_response_days"])
        self.assertEqual(len(report["stale_applications"]), 1)
        self.assertEqual(report["stale_days"], 14)

    def test_missing_file_gives_empty_report(self):
        report = analytics.generate_report(
            path="/tmp/definitely-not-here-12345.json", now=NOW
        )
        self.assertEqual(report["total_applications"], 0)
        self.assertEqual(report["stale_applications"], [])


class FakeMCP:
    """Minimal stand-in for the MCP server's @mcp.tool() decorator."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class TestWiring(unittest.TestCase):
    def test_register_tools(self):
        mcp = FakeMCP()
        registered = analytics.register_tools(mcp)
        self.assertIn("application_analytics", mcp.tools)
        self.assertIn("application_analytics", registered)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "applications.json"
            path.write_text(json.dumps(sample_applications()), encoding="utf-8")
            with mock.patch.object(analytics, "APPLICATIONS_FILE", path):
                report = mcp.tools["application_analytics"]()
        self.assertEqual(report["total_applications"], 5)

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        handlers = analytics.register_cli(sub)
        self.assertIn("analytics", handlers)
        args = parser.parse_args(["analytics", "--json"])
        self.assertTrue(args.json)
        self.assertEqual(args.stale_days, 14)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "applications.json"
            path.write_text(json.dumps(sample_applications()), encoding="utf-8")
            args.applications = str(path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = handlers["analytics"](args)
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["total_applications"], 5)

    def test_cmd_analytics_human_readable(self):
        args = argparse.Namespace(
            json=False, stale_days=14, applications=None
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "applications.json"
            path.write_text(json.dumps(sample_applications()), encoding="utf-8")
            args.applications = str(path)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = analytics.cmd_analytics(args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Applications: 5", out)
            self.assertIn("Conversion:", out)
            self.assertIn("greenhouse", out)


if __name__ == "__main__":
    unittest.main()
