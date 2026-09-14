#!/usr/bin/env python3
"""Tests for the Initiative 07 warm-path planner (epic 5): drafts only,
never sends; honest empty states; no invented people.
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import network_crm  # noqa: E402
import referrals  # noqa: E402
from initiatives.i07 import warm_path  # noqa: E402


class WarmPathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = {
            "network_file": network_crm.NETWORK_FILE,
            "connections_csv": referrals.CONNECTIONS_CSV,
            "connections_json": referrals.CONNECTIONS_JSON,
            "draft_outbox": warm_path.DRAFT_OUTBOX,
        }
        network_crm.NETWORK_FILE = tmp / "network.json"
        referrals.CONNECTIONS_CSV = tmp / "connections.csv"
        referrals.CONNECTIONS_JSON = tmp / "connections.json"
        warm_path.DRAFT_OUTBOX = tmp / "draft_outbox.jsonl"

    def tearDown(self):
        network_crm.NETWORK_FILE = self._orig["network_file"]
        referrals.CONNECTIONS_CSV = self._orig["connections_csv"]
        referrals.CONNECTIONS_JSON = self._orig["connections_json"]
        warm_path.DRAFT_OUTBOX = self._orig["draft_outbox"]
        self._tmp.cleanup()

    def _contact(self, name="Ada Lovelace", company="Initech", role="Engineer"):
        c = network_crm.add_contact(name=name, company=company, role=role)
        return c["id"]

    # -- planner ------------------------------------------------------------------

    def test_empty_network_honest_empty_plan(self):
        res = warm_path.plan_warm_path("Initech")
        self.assertTrue(res["ok"])
        self.assertEqual(res["plans"], [])
        self.assertIn("never", res["guidance"])
        self.assertIn("invent", res["guidance"])

    def test_crm_path_produces_draft(self):
        cid = self._contact()
        network_crm.log_interaction(cid, "coffee", "Great chat about Initech")
        res = warm_path.plan_warm_path("Initech")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["plans"]), 1)
        plan = res["plans"][0]
        self.assertEqual(plan["source"], "network_crm")
        self.assertEqual(plan["degrees"], 1)
        self.assertIn("Initech", plan["draft"]["body"])
        self.assertTrue(res["honesty_checklist"])

    def test_drafts_recorded_in_outbox_as_draft(self):
        self._contact()
        warm_path.plan_warm_path("Initech")
        drafts = warm_path.list_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "draft")

    def test_no_send_path_exists(self):
        for mod in (warm_path, network_crm, referrals):
            for name, fn in inspect.getmembers(mod, inspect.isfunction):
                self.assertNotIn(
                    "send",
                    name.lower(),
                    f"{mod.__name__}.{name} looks like a send path",
                )

    def test_discard_draft(self):
        self._contact()
        warm_path.plan_warm_path("Initech")
        draft_id = warm_path.list_drafts()[0]["draft_id"]
        self.assertTrue(warm_path.discard_draft(draft_id)["ok"])
        self.assertEqual(warm_path.list_drafts(), [])
        self.assertFalse(warm_path.discard_draft("nope")["ok"])

    def test_requires_company(self):
        self.assertFalse(warm_path.plan_warm_path("")["ok"])

    def test_second_degree_path(self):
        cid = self._contact(name="Grace Hopper", company="Acme")
        network_crm.log_interaction(cid, "email", "Grace mentioned Initech hiring")
        res = warm_path.plan_warm_path("Initech")
        self.assertTrue(res["ok"])
        self.assertTrue(res["plans"])
        self.assertEqual(res["plans"][0]["degrees"], 2)

    # -- honesty: drafts never fabricate employment -------------------------------

    def test_bridge_draft_names_target_only_as_ask(self):
        # Grace Hopper works at Acme; she merely MENTIONED Initech. The
        # bridge draft must never claim she works at Initech: her actual
        # employer is named, the target appears only as the ask.
        cid = self._contact(name="Grace Hopper", company="Acme")
        network_crm.log_interaction(cid, "email", "Grace mentioned Initech hiring")
        res = warm_path.plan_warm_path("Initech")
        self.assertTrue(res["ok"])
        plan = res["plans"][0]
        self.assertEqual(plan["degrees"], 2)
        body = plan["draft"]["body"]
        self.assertNotIn("I see you're at Initech", body)
        self.assertIn("Acme", body)  # her actual recorded employer
        self.assertIn("Initech", body)  # named only as the ask
        self.assertIn("introduc", body.lower())  # explicit intro-ask template

    def test_referral_draft_never_claims_false_employer(self):
        # Bob Builder is recorded at Globex (title overlap only — no
        # "same company" evidence). His draft must name Globex, never
        # claim Initech as his employer.
        referrals.CONNECTIONS_JSON.write_text(
            json.dumps(
                [{"name": "Bob Builder", "company": "Globex", "position": "Engineer"}]
            ),
            encoding="utf-8",
        )
        res = warm_path.plan_warm_path("Initech")
        self.assertTrue(res["ok"])
        plans = [p for p in res["plans"] if p["contact_name"] == "Bob Builder"]
        self.assertEqual(len(plans), 1)
        body = plans[0]["draft"]["body"]
        self.assertNotIn("I see you're at Initech", body)
        self.assertIn("Globex", body)

    def test_first_degree_draft_may_name_target_as_employer(self):
        # The gate: "I see you're at {X}" is honest exactly when the
        # contact is actually recorded at X (evidence "same company").
        self._contact()  # Ada Lovelace at Initech
        res = warm_path.plan_warm_path("Initech")
        plan = res["plans"][0]
        self.assertEqual(plan["degrees"], 1)
        self.assertIn("I see you're at Initech", plan["draft"]["body"])

    def test_draft_bodies_never_name_unrecorded_employer(self):
        # Sweep every produced draft: no body may claim employment at a
        # company the contact is not recorded at.
        cid = self._contact(name="Grace Hopper", company="Acme")
        network_crm.log_interaction(cid, "email", "Grace mentioned Initech hiring")
        referrals.CONNECTIONS_JSON.write_text(
            json.dumps(
                [{"name": "Bob Builder", "company": "Globex", "position": "Engineer"}]
            ),
            encoding="utf-8",
        )
        res = warm_path.plan_warm_path("Initech")
        self.assertTrue(res["plans"])
        for plan in res["plans"]:
            body = plan["draft"]["body"]
            actual = plan["contact_company"]
            self.assertIn(
                f"I see you're at {actual}", body,
                f"draft for {plan['contact_name']} must name their recorded "
                f"employer ({actual!r})",
            )
            for other in ("Initech", "Acme", "Globex"):
                if other != actual:
                    self.assertNotIn(f"I see you're at {other}", body)

    # -- dedup + rerun guard + outbox location ------------------------------------

    def test_dedup_on_name_and_company(self):
        self._contact(name="Sam Lee", company="Initech")  # CRM, 1st degree
        referrals.CONNECTIONS_JSON.write_text(
            json.dumps(
                [
                    {"name": "Sam Lee", "company": "Initech", "position": "Engineer"},
                    {"name": "Sam Lee", "company": "Globex", "position": "Engineer"},
                ]
            ),
            encoding="utf-8",
        )
        res = warm_path.plan_warm_path("Initech")
        sams = [p for p in res["plans"] if p["contact_name"] == "Sam Lee"]
        # Same name+company (CRM + radar) -> one draft; same name at a
        # different company -> a separate, honest draft.
        self.assertEqual(len(sams), 2)
        self.assertEqual(
            {p["contact_company"] for p in sams}, {"Initech", "Globex"}
        )

    def test_rerun_does_not_duplicate_drafts(self):
        self._contact()
        first = warm_path.plan_warm_path("Initech")
        n_after_first = len(warm_path.list_drafts())
        self.assertGreater(n_after_first, 0)
        second = warm_path.plan_warm_path("Initech")
        self.assertEqual(len(warm_path.list_drafts()), n_after_first)
        self.assertEqual(
            [p["draft_id"] for p in first["plans"]],
            [p["draft_id"] for p in second["plans"]],
        )

    def test_default_outbox_lives_outside_repo_tree(self):
        default = warm_path._default_outbox()
        self.assertNotIn(
            "job-apply-mcp",
            str(default),
            "default outbox must not live in the repo tree",
        )
        self.assertTrue(str(default).startswith(str(Path.home())))

    # -- network_crm draft logging ---------------------------------------------------

    def test_log_outreach_draft(self):
        cid = self._contact()
        d = network_crm.log_outreach_draft(
            cid, "linkedin", "Hello", "Hi Ada, ...", target_company="Initech"
        )
        self.assertEqual(d["status"], "draft")
        contact = network_crm._get_contact(network_crm._load_network(), cid)
        self.assertEqual(len(contact["outreach_drafts"]), 1)

    def test_log_outreach_draft_validates(self):
        cid = self._contact()
        with self.assertRaises(ValueError):
            network_crm.log_outreach_draft(cid, "", "s", "b")
        with self.assertRaises(ValueError):
            network_crm.log_outreach_draft(cid, "email", "s", "  ")

    # -- referrals plan_outreach_drafts -----------------------------------------------

    def test_plan_outreach_drafts(self):
        prospects = [
            {
                "name": "Alan Turing",
                "company": "Initech",
                "position": "Engineer",
                "evidence": ["same company"],
                "reasons": ["works at Initech"],
            }
        ]
        drafts = referrals.plan_outreach_drafts(
            prospects, job={"company": "Initech", "title": "Engineer"}
        )
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "draft")
        self.assertEqual(drafts[0]["evidence"], ["same company"])

    def test_plan_outreach_drafts_caps(self):
        prospects = [
            {"name": f"P{i}", "company": "Initech", "position": "E"} for i in range(10)
        ]
        drafts = referrals.plan_outreach_drafts(prospects, max_drafts=3)
        self.assertEqual(len(drafts), 3)


if __name__ == "__main__":
    unittest.main()
