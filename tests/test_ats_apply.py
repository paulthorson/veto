"""Unit tests for ats_apply.py — direct-ATS apply PLAN preview (fill-only).

No network access: apply_direct() never issues HTTP calls, so there is
nothing to mock. The submit path was deleted (legal-hardening commit 6);
these tests assert the fill-only contract: preview-only results, zero
network calls, no "verified" status re-arm.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import types
import unittest
from unittest import mock

import ats_apply


def _job_id(board: str, payload: str) -> str:
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{board}:{token}"


APPLICANT = {
    "full_name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": "+1 555-0100",
    "location": "New York, NY",
    "linkedin_url": "https://www.linkedin.com/in/adalovelace",
    "website": "",
}


def _no_bytes(obj) -> bool:
    """Recursively assert no bytes objects anywhere in a structure."""
    if isinstance(obj, bytes):
        return False
    if isinstance(obj, dict):
        return all(_no_bytes(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return all(_no_bytes(v) for v in obj)
    return True


class TestRegistry(unittest.TestCase):
    def test_no_board_offers_direct_apply(self):
        # 2026-09-10 verification: Ashby 401s, Lever needs a Lever-issued
        # key, Greenhouse needs an employer Job Board API key. And there
        # is no submit path left to re-arm: direct apply is unavailable
        # for every board, permanently.
        for board in ("ashby", "lever", "greenhouse"):
            desc = ats_apply.describe_apply_path(board)
            self.assertFalse(desc["direct_apply_available"], board)
            self.assertIn("browser", desc["recommended_path"].lower())

    def test_endpoint_status_documents_blockers(self):
        for board in ("ashby", "lever", "greenhouse"):
            info = ats_apply.endpoint_status(board)
            self.assertNotEqual(info["status"], "verified")
            self.assertTrue(info["reason"])
            self.assertTrue(info["sources"])

    def test_no_submit_function_survives(self):
        # The POST path was deleted: no _submit_verified, no
        # can_apply_direct re-arm mechanism, no httpx import.
        self.assertFalse(hasattr(ats_apply, "_submit_verified"))
        self.assertFalse(hasattr(ats_apply, "can_apply_direct"))
        self.assertFalse(hasattr(ats_apply, "httpx"))


class TestPreview(unittest.TestCase):
    def setUp(self):
        self.job = {"id": _job_id("ashby", "ashby:posting-123"),
                    "board": "ashby", "title": "Engineer"}

    def test_preview_makes_zero_http_calls(self):
        preview = ats_apply.apply_direct(
            "ashby", self.job, APPLICANT,
            resume_path="/tmp/resume.pdf", cover_letter="Hi",
            confirm=False)
        self.assertEqual(preview["mode"], "preview")
        self.assertEqual(preview["network_calls"], 0)
        self.assertEqual(preview["board"], "ashby")
        self.assertFalse(preview["direct_apply_available"])
        self.assertIn("endpoint", preview)
        self.assertIn("fields_for_manual_form", preview)
        self.assertIn("disclosure", preview)
        self.assertTrue(_no_bytes(preview))

    def test_preview_field_names_but_no_resume_bytes(self):
        preview = ats_apply.apply_direct("lever", self.job, APPLICANT,
                                         resume_path="/tmp/resume.pdf")
        fields = preview["fields_for_manual_form"]
        self.assertEqual(fields["full_name"], "Ada Lovelace")
        self.assertEqual(fields["email"], "ada@example.com")
        # Filename may be mentioned; raw bytes never are.
        self.assertTrue(_no_bytes(fields))
        self.assertNotIn("bytes", json.dumps(preview).lower()
                         .replace("bytes never included", ""))

    def test_confirm_true_still_returns_preview_only(self):
        # confirm=True no longer authorizes anything: there is no submit
        # path, so even a confirmed call returns the preview.
        out = ats_apply.apply_direct(
            "ashby", self.job, APPLICANT, confirm=True, dry_run=False)
        self.assertEqual(out["mode"], "preview")
        self.assertEqual(out["network_calls"], 0)
        self.assertFalse(out["direct_apply_available"])

    def test_confirmed_greenhouse_reports_key_requirement(self):
        job = {"id": _job_id("greenhouse", "stripe:12345"),
               "board": "greenhouse"}
        out = ats_apply.apply_direct(
            "greenhouse", job, APPLICANT, confirm=True, dry_run=False)
        self.assertEqual(out["mode"], "preview")
        self.assertEqual(out["endpoint_status"], "key_required")
        self.assertIn("note", out)

    def test_unknown_board_raises(self):
        with self.assertRaises(ValueError):
            ats_apply.apply_direct("workday", self.job, APPLICANT)

    def test_malformed_job_id_returns_error(self):
        out = ats_apply.apply_direct(
            "ashby", {"id": "not-a-valid-id"}, APPLICANT, confirm=True)
        self.assertFalse(out.get("ok", True))
        self.assertIn("error", out)


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class TestRegisterTools(unittest.TestCase):
    def test_apply_via_ats_registered_with_docstring(self):
        mcp = FakeMCP()
        ats_apply.register_tools(mcp)
        fn = mcp.tools["apply_via_ats"]
        self.assertIn("job_id", fn.__doc__)
        self.assertIn("never submits", fn.__doc__.lower())

    def test_apply_via_ats_preview_no_network(self):
        fake_server = types.ModuleType("server")
        fake_server._load_saved_profile = lambda: dict(APPLICANT)
        with mock.patch.dict(sys.modules, {"server": fake_server}):
            mcp = FakeMCP()
            ats_apply.register_tools(mcp)
            out = mcp.tools["apply_via_ats"](
                job_id=_job_id("ashby", "ashby:posting-123"))
        self.assertEqual(out["mode"], "preview")
        self.assertEqual(out["board"], "ashby")


class TestRegisterCli(unittest.TestCase):
    def test_apply_direct_command_flags(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        handler = ats_apply.register_cli(sub)["apply-direct"]
        self.assertTrue(callable(handler))
        args = parser.parse_args(
            ["apply-direct", "--job-id", "ashby:abc123"])
        self.assertEqual(args.job_id, "ashby:abc123")

    def test_apply_direct_preview_handler_exit_zero(self):
        fake_server = types.ModuleType("server")
        fake_server._load_saved_profile = lambda: dict(APPLICANT)
        fake_server._decode_job_id = lambda jid: ("ashby", "posting-123")
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        handler = ats_apply.register_cli(sub)["apply-direct"]
        args = parser.parse_args(
            ["apply-direct", "--job-id", _job_id("ashby", "ashby:1")])
        with mock.patch.dict(sys.modules, {"server": fake_server}):
            rc = handler(args)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
