#!/usr/bin/env python3
"""Tests for the webui.py /api/tools catalog.

Covers the catalog staleness fix for the dashboard's second-wave guided
wizards (watch 20, apply dry-run 21, grill 22, briefs 23, email scan 24,
analytics 25, free-form search 26): every wizard module must have a
matching catalog entry, every catalog action must resolve to a real
handler, and the /api/tools payload must be JSON-serializable.

Convention mirrors tests/test_dashboard_wizards.py: stdlib unittest only,
every module boundary mocked so the tests never touch the real state
files (``watches.json``, ``grill_sessions.json``), never hit the network,
and never send anything. The email scan handler must always call the
scan with ``apply_updates=False`` (proposals only).

Run:  cd ~/workspace/job-apply-mcp && .venv/bin/python -m pytest tests/test_webui_catalog.py
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import webui  # noqa: E402

VALID_TYPES = {"string", "integer", "number", "boolean", "object", "array"}

# Dashboard menu key -> (catalog tool id, catalog action ids) for the
# seven second-wave guided wizards (dashboard.py print_menu groups:
# FIND: 20/26, APPLY: 21/22, TRAIN: 23, WIN: 24/25).
WIZARD_COVERAGE = {
    "20": ("watch", {"list", "add", "check", "remove"}),
    "21": ("search", {"record_apply"}),       # guided dry-run -> confirm-gated record
    "21b": ("apply_queue", {"add", "list", "remove"}),  # drip queue alternative
    "22": ("grill", {"session", "start", "answer", "status", "qa_pairs",
                      "cancel", "keywords"}),
    "23": ("briefs", {"company_brief", "interview_prep"}),
    "24": ("email_sync", {"scan", "draft_followup"}),
    "25": ("analytics", {"report", "funnel", "response_rate", "stale"}),
    "26": ("search", {"query"}),
}


def _tool(tool_id):
    for t in webui.TOOLS:
        if t["id"] == tool_id:
            return t
    return None


class CatalogIntegrityTests(unittest.TestCase):
    def test_tool_ids_unique(self):
        ids = [t["id"] for t in webui.TOOLS]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate ids: {ids}")

    def test_every_action_has_valid_shape(self):
        for t in webui.TOOLS:
            for key in ("id", "group", "name", "description", "actions"):
                self.assertIn(key, t, f"tool {t.get('id')} missing {key}")
            for a in t["actions"]:
                for key in ("id", "description", "handler", "params"):
                    self.assertIn(key, a,
                                  f"{t['id']}/{a.get('id')} missing {key}")
                self.assertTrue(callable(a["handler"]),
                                f"{t['id']}/{a['id']} handler not callable")
                for p in a["params"]:
                    self.assertIn(p["type"], VALID_TYPES,
                                  f"{t['id']}/{a['id']} bad param type {p}")
                    self.assertIn("name", p)
                    self.assertIn("required", p)

    def test_action_lookup_covers_every_tool_action(self):
        for t in webui.TOOLS:
            for a in t["actions"]:
                self.assertIn((t["id"], a["id"]), webui._ACTION_LOOKUP,
                              f"{t['id']}/{a['id']} not in _ACTION_LOOKUP")

    def test_api_tools_payload_serializes(self):
        # Mirror the do_GET /api/tools serialization exactly.
        tools = []
        for t in webui.TOOLS:
            tools.append({
                "id": t["id"], "group": t["group"], "name": t["name"],
                "description": t["description"],
                "actions": [
                    {"id": a["id"], "description": a["description"],
                     "params": a["params"]}
                    for a in t["actions"]
                ],
            })
        payload = json.dumps({"tools": tools})
        decoded = json.loads(payload)
        self.assertEqual(len(decoded["tools"]), len(webui.TOOLS))
        ids = [t["id"] for t in decoded["tools"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_new_tools_use_dashboard_groups(self):
        expect = {"watch": "FIND", "grill": "APPLY", "briefs": "TRAIN",
                  "email_sync": "WIN"}
        for tool_id, group in expect.items():
            self.assertEqual(_tool(tool_id)["group"], group,
                             f"{tool_id} should be in group {group}")

    def test_no_em_dashes_in_new_copy(self):
        src = inspect.getsource(webui)
        for tool_id in ("watch", "grill", "briefs", "email_sync"):
            t = _tool(tool_id)
            body = t["description"] + " ".join(
                a["description"] for a in t["actions"])
            self.assertNotIn("\u2014", body,
                             f"em dash in {tool_id} catalog copy")


class WizardCoverageTests(unittest.TestCase):
    def test_every_wizard_module_has_a_catalog_tool(self):
        for menu_key, (tool_id, action_ids) in WIZARD_COVERAGE.items():
            t = _tool(tool_id)
            self.assertIsNotNone(t, f"menu {menu_key}: no tool {tool_id!r}")
            have = {a["id"] for a in t["actions"]}
            self.assertTrue(action_ids <= have,
                            f"menu {menu_key}: {tool_id} missing {action_ids - have}")

    def test_grill_session_action_uses_public_accessor(self):
        # The catalog must route through the public get_grill_session(),
        # not the private session-store internals.
        src = inspect.getsource(webui._h_grill_session)
        self.assertIn("get_grill_session", src)
        self.assertNotIn("_load_sessions", src)

    def test_email_scan_action_never_applies_updates(self):
        src = inspect.getsource(webui._h_email_scan)
        self.assertIn("apply_updates=False", src)


class HandlerSmokeTests(unittest.TestCase):
    """Drive the new handlers through webui.run_action with fakes."""

    def _fake_module(self, name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        self.addCleanup(sys.modules.pop, name, None)
        return mod

    # -- watch -------------------------------------------------------------
    def test_watch_add_list_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = webui._WATCHES_PATH
            webui._WATCHES_PATH = Path(tmp) / "watches.json"
            self.addCleanup(setattr, webui, "_WATCHES_PATH", old)
            resp = webui.run_action("watch", "add",
                                    {"name": "py", "query": "python dev",
                                     "board": "greenhouse"})
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["data"]["watch"]["name"], "py")
            resp = webui.run_action("watch", "list", {})
            self.assertTrue(resp["ok"])
            self.assertEqual(len(resp["data"]["watches"]), 1)
            resp = webui.run_action("watch", "remove", {"name": "py"})
            self.assertTrue(resp["ok"])
            self.assertTrue(resp["data"]["removed"])

    def test_watch_add_requires_name_and_query(self):
        resp = webui.run_action("watch", "add", {"name": "x"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("_status"), 400)

    def test_watch_check_baseline_then_reports_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = webui._WATCHES_PATH
            webui._WATCHES_PATH = Path(tmp) / "watches.json"
            self.addCleanup(setattr, webui, "_WATCHES_PATH", old)
            jobs = {"jobs": []}

            def search_jobs(**kwargs):
                return list(jobs["jobs"])

            self._fake_module("server", search_jobs=search_jobs)
            webui.run_action("watch", "add",
                             {"name": "py", "query": "python"})
            resp = webui.run_action("watch", "check", {"name": "py"})
            self.assertTrue(resp["ok"], resp)
            self.assertEqual(resp["data"]["results"]["py"]["new_jobs"], [])
            jobs["jobs"] = [{"id": "g:1", "title": "Dev", "company": "Acme",
                             "board": "greenhouse"}]
            resp = webui.run_action("watch", "check", {"name": "py"})
            self.assertTrue(resp["ok"], resp)
            new_jobs = resp["data"]["results"]["py"]["new_jobs"]
            self.assertEqual(len(new_jobs), 1)
            self.assertEqual(new_jobs[0]["id"], "g:1")

    # -- grill -------------------------------------------------------------
    def test_grill_session_start_answer_status(self):
        state = {"sessions": {}}

        def get_grill_session(job_id):
            return state["sessions"].get(job_id)

        def start_grill(job_id, job, profile):
            qs = [{"id": "q1", "kind": "question",
                   "question": "What did you ship?"}]
            state["sessions"][job_id] = {"job_id": job_id, "questions": qs,
                                         "answers": {}, "complete": False}
            return {"session_id": "s1", "questions": qs}

        def record_answer(job_id, question_id, answer):
            sess = state["sessions"][job_id]
            sess["answers"][question_id] = answer
            sess["complete"] = True
            return {"recorded": True, "complete": True,
                    "answered": 1, "total": 1}

        def grill_status(job_id):
            sess = state["sessions"].get(job_id) or {}
            return {"complete": sess.get("complete", False), "answered": 1,
                    "total": 1, "qa_pairs": []}

        self._fake_module("grill", get_grill_session=get_grill_session,
                          start_grill=start_grill,
                          record_answer=record_answer,
                          grill_status=grill_status)
        resp = webui.run_action("grill", "session", {"job_id": "j:1"})
        self.assertTrue(resp["ok"])
        self.assertIsNone(resp["data"]["session"])
        resp = webui.run_action(
            "grill", "start",
            {"job_id": "j:1", "job": {"title": "Dev", "company": "Acme",
                                     "description": "build things"}})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(len(resp["data"]["questions"]), 1)
        resp = webui.run_action(
            "grill", "answer",
            {"job_id": "j:1", "question_id": "q1", "answer": "shipped v2"})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["data"]["complete"])
        resp = webui.run_action("grill", "session", {"job_id": "j:1"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"]["session"]["job_id"], "j:1")

    # -- briefs ------------------------------------------------------------
    def test_briefs_company_brief(self):
        def company_brief(company):
            return {"company": company, "verified": False, "markdown": "# x"}

        def prep_interview(job_id, profile=None):
            return {"job_id": job_id, "markdown": "# prep"}

        self._fake_module("briefs", company_brief=company_brief,
                          prep_interview=prep_interview)
        resp = webui.run_action("briefs", "company_brief",
                                {"company": "Acme"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"]["company"], "Acme")
        resp = webui.run_action("briefs", "interview_prep",
                                {"job_id": "g:1"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"]["job_id"], "g:1")

    # -- email_sync ---------------------------------------------------------
    def test_email_scan_is_proposals_only(self):
        seen = {}

        def scan_recruiter_emails(days=14, apply_updates=False):
            seen["days"] = days
            seen["apply_updates"] = apply_updates
            return {"scanned": 0, "matched": 0, "proposed_updates": [],
                    "applied_updates": []}

        def draft_followup(application_id, kind="check_in"):
            return {"application_id": application_id, "kind": kind,
                    "to": "", "subject": "s", "body": "b", "note": ""}

        self._fake_module("email_sync",
                          scan_recruiter_emails=scan_recruiter_emails,
                          draft_followup=draft_followup)
        resp = webui.run_action("email_sync", "scan", {"days": 7})
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(seen["days"], 7)
        self.assertFalse(seen["apply_updates"],
                         "email scan must be proposals-only")
        self.assertEqual(resp["data"]["applied_updates"], [])
        resp = webui.run_action("email_sync", "draft_followup",
                                {"application_id": "g:1", "kind": "nudge"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["data"]["kind"], "nudge")

    # -- unknown tool -------------------------------------------------------
    def test_unknown_tool_action_is_400(self):
        resp = webui.run_action("nope", "nothing", {})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("_status"), 400)


if __name__ == "__main__":
    unittest.main()
