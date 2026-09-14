#!/usr/bin/env python3
"""Unit tests for network_crm.py (offline, tmp-dir store)."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import network_crm


def _old_iso(days_ago: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat(timespec="seconds")


class NetworkCrmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = network_crm.NETWORK_FILE
        network_crm.NETWORK_FILE = Path(self.tmp.name) / "network.json"
        self.addCleanup(setattr, network_crm, "NETWORK_FILE", self._orig)

    def _read_store(self) -> dict:
        return json.loads(network_crm.NETWORK_FILE.read_text(encoding="utf-8"))

    def _backdate_last_interaction(self, contact_id: str, days_ago: int) -> None:
        store = self._read_store()
        old = _old_iso(days_ago)
        store[contact_id]["last_interaction_at"] = old
        for entry in store[contact_id]["interactions"]:
            entry["at"] = old
        network_crm.NETWORK_FILE.write_text(json.dumps(store), encoding="utf-8")

    # -- add_contact ------------------------------------------------------

    def test_add_contact_happy_path(self):
        card = network_crm.add_contact(
            "Ada Lovelace",
            "Stripe Inc.",
            role="Senior Backend Engineer",
            industry="fintech",
            linkedin_url="https://www.linkedin.com/in/adalovelace",
            source="conference",
            notes="Met at PyCon.",
        )
        self.assertTrue(card["id"].startswith("contact-"))
        self.assertEqual(card["name"], "Ada Lovelace")
        self.assertEqual(card["company"], "Stripe Inc.")
        self.assertEqual(card["interactions"], [])
        self.assertEqual(len(network_crm.list_contacts()), 1)

    def test_add_contact_requires_name_and_company(self):
        with self.assertRaises(ValueError):
            network_crm.add_contact("", "Acme")
        with self.assertRaises(ValueError):
            network_crm.add_contact("  ", "Acme")
        with self.assertRaises(ValueError):
            network_crm.add_contact("Bob", "")
        with self.assertRaises(ValueError):
            network_crm.add_contact("Bob", "   ")

    def test_add_contact_validates_linkedin_url(self):
        with self.assertRaises(ValueError):
            network_crm.add_contact(
                "Bob", "Acme", linkedin_url="https://example.com/bob"
            )
        with self.assertRaises(ValueError):
            network_crm.add_contact(
                "Bob", "Acme", linkedin_url="https://linkedin.com/jobs/123"
            )
        # Empty is fine; company URLs are fine.
        c1 = network_crm.add_contact("Bob", "Acme")
        self.assertEqual(c1["linkedin_url"], "")
        c2 = network_crm.add_contact(
            "Carol", "Acme", linkedin_url="https://linkedin.com/company/acme"
        )
        self.assertTrue(c2["linkedin_url"].startswith("https://linkedin.com"))

    def test_add_contact_is_idempotent(self):
        first = network_crm.add_contact("Ada Lovelace", "Stripe")
        second = network_crm.add_contact(
            "Ada Lovelace", "Stripe", notes="updated notes"
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(network_crm.list_contacts()), 1)
        self.assertEqual(network_crm.list_contacts()[0]["notes"], "updated notes")

    # -- log_interaction --------------------------------------------------

    def test_log_interaction_happy_path(self):
        card = network_crm.add_contact("Grace Hopper", "Initech")
        entry = network_crm.log_interaction(
            card["id"], "call", "Discussed backend role on her team."
        )
        self.assertEqual(entry["kind"], "call")
        self.assertIn("at", entry)
        stored = network_crm.list_contacts()[0]
        self.assertEqual(len(stored["interactions"]), 1)
        self.assertEqual(stored["last_interaction_at"], entry["at"])

    def test_log_interaction_rejects_bad_kind(self):
        card = network_crm.add_contact("Grace Hopper", "Initech")
        with self.assertRaises(ValueError):
            network_crm.log_interaction(card["id"], "telepathy", "hi")
        with self.assertRaises(ValueError):
            network_crm.log_interaction(card["id"], "CALL", "hi")

    def test_log_interaction_rejects_unknown_contact_and_empty_summary(self):
        with self.assertRaises(ValueError):
            network_crm.log_interaction("contact-nope", "call", "hi")
        card = network_crm.add_contact("Grace Hopper", "Initech")
        with self.assertRaises(ValueError):
            network_crm.log_interaction(card["id"], "call", "   ")

    def test_log_interaction_due_date(self):
        card = network_crm.add_contact("Grace Hopper", "Initech")
        entry = network_crm.log_interaction(
            card["id"], "followup", "Send resume", due="2026-09-20"
        )
        self.assertEqual(entry["due"], "2026-09-20")
        with self.assertRaises(ValueError):
            network_crm.log_interaction(card["id"], "followup", "x", due="20-09-2026")
        with self.assertRaises(ValueError):
            network_crm.log_interaction(card["id"], "followup", "x", due="not-a-date")

    # -- nudge_list -------------------------------------------------------

    def test_nudge_list_flags_stale_contacts(self):
        fresh = network_crm.add_contact("Fresh Friend", "Acme")
        network_crm.log_interaction(fresh["id"], "call", "recent chat")
        stale = network_crm.add_contact("Stale Steve", "Initech")
        network_crm.log_interaction(stale["id"], "coffee", "long ago")
        self._backdate_last_interaction(stale["id"], 30)
        nudges = network_crm.nudge_list()
        ids = [n["contact_id"] for n in nudges]
        self.assertIn(stale["id"], ids)
        self.assertNotIn(fresh["id"], ids)
        nudge = next(n for n in nudges if n["contact_id"] == stale["id"])
        self.assertEqual(nudge["stale_days"], 30)
        self.assertTrue(nudge["suggested_next_step"])

    def test_nudge_list_ranks_stalest_first(self):
        a = network_crm.add_contact("A Person", "Acme")
        network_crm.log_interaction(a["id"], "call", "x")
        self._backdate_last_interaction(a["id"], 25)
        b = network_crm.add_contact("B Person", "Acme")
        network_crm.log_interaction(b["id"], "call", "x")
        self._backdate_last_interaction(b["id"], 60)
        nudges = network_crm.nudge_list()
        self.assertEqual(nudges[0]["contact_id"], b["id"])
        self.assertEqual(nudges[1]["contact_id"], a["id"])

    def test_nudge_list_never_contacted_uses_created_at(self):
        card = network_crm.add_contact("Never Nora", "Acme")
        store = self._read_store()
        store[card["id"]]["created_at"] = _old_iso(40)
        network_crm.NETWORK_FILE.write_text(json.dumps(store), encoding="utf-8")
        nudges = network_crm.nudge_list()
        self.assertEqual(len(nudges), 1)
        self.assertIn("Reach out and introduce yourself", nudges[0]["suggested_next_step"])

    def test_nudge_list_flags_overdue_followup(self):
        card = network_crm.add_contact("Promiser Pete", "Acme")
        network_crm.log_interaction(
            card["id"], "followup", "Send portfolio", due="2020-01-01"
        )
        nudges = network_crm.nudge_list()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]["overdue_followups"], 1)
        self.assertIn("overdue", nudges[0]["reasons"][0])
        self.assertIn("send it now", nudges[0]["suggested_next_step"].lower())

    def test_nudge_list_ignores_future_due_dates(self):
        card = network_crm.add_contact("Planner Pam", "Acme")
        network_crm.log_interaction(
            card["id"], "followup", "Send portfolio", due="2999-01-01"
        )
        self.assertEqual(network_crm.nudge_list(), [])

    def test_nudge_list_empty_when_all_fresh(self):
        card = network_crm.add_contact("Fresh Friend", "Acme")
        network_crm.log_interaction(card["id"], "call", "just talked")
        self.assertEqual(network_crm.nudge_list(), [])

    # -- warm_path_to -----------------------------------------------------

    def test_warm_path_first_degree(self):
        network_crm.add_contact("Ada Lovelace", "Acme Inc.", role="Engineer")
        network_crm.add_contact("Grace Hopper", "Initech", role="Manager")
        paths = network_crm.warm_path_to("acme")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]["degrees"], 1)
        self.assertEqual(paths[0]["contact_name"], "Ada Lovelace")
        self.assertIn("works at", paths[0]["via"])

    def test_warm_path_second_degree_via_mention(self):
        network_crm.add_contact(
            "Bob Jones",
            "Initech",
            role="Engineer",
            notes="Ex-colleague now at Acme Corp, happy to intro.",
        )
        paths = network_crm.warm_path_to("Acme")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]["degrees"], 2)
        self.assertEqual(paths[0]["contact_name"], "Bob Jones")
        self.assertIn("warm intro", paths[0]["via"])

    def test_warm_path_second_degree_via_interaction_history(self):
        card = network_crm.add_contact("Carol Danvers", "Globex", role="PM")
        network_crm.log_interaction(
            card["id"], "call", "Carol said her friend at Acme is hiring."
        )
        paths = network_crm.warm_path_to("Acme Inc")
        self.assertTrue(any(p["degrees"] == 2 for p in paths))

    def test_warm_path_no_path_returns_empty(self):
        network_crm.add_contact("Lonely Lou", "Initech", notes="No useful mentions.")
        self.assertEqual(network_crm.warm_path_to("Acme"), [])

    def test_warm_path_empty_network_returns_empty(self):
        self.assertEqual(network_crm.warm_path_to("Acme"), [])

    def test_warm_path_requires_company(self):
        with self.assertRaises(ValueError):
            network_crm.warm_path_to("")
        with self.assertRaises(ValueError):
            network_crm.warm_path_to("   ")

    def test_warm_path_never_invents_relationships(self):
        # No mention of the target anywhere: no path, nothing invented.
        network_crm.add_contact(
            "Bob Jones", "Initech", notes="Met at a conference in Austin."
        )
        self.assertEqual(network_crm.warm_path_to("SpaceX"), [])

    def test_warm_path_mention_is_transparent_not_invented(self):
        # A bare mention IS surfaced (per spec: notes/history are the
        # 2nd-degree signal), but the via text quotes the evidence —
        # it never claims a relationship the notes don't support.
        network_crm.add_contact(
            "Bob Jones", "Initech", notes="Loves reading about SpaceX."
        )
        paths = network_crm.warm_path_to("SpaceX")
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0]["degrees"], 2)
        self.assertIn("mentioned", paths[0]["via"])

    # -- wiring -----------------------------------------------------------

    def test_register_cli_returns_network_command(self):
        import argparse

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        handlers = network_crm.register_cli(subs)
        self.assertEqual(set(handlers), {"network"})
        args = parser.parse_args(
            ["network", "add", "--name", "Test", "--company", "Acme"]
        )
        self.assertEqual(handlers["network"](args), 0)
        self.assertEqual(len(network_crm.list_contacts()), 1)

    def test_cli_nudges_and_warm_path(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        card = network_crm.add_contact("Ada Lovelace", "Acme")
        self._backdate_last_interaction(card["id"], 30)
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        handlers = network_crm.register_cli(subs)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                handlers["network"](parser.parse_args(["network", "nudges"])), 0
            )
            self.assertEqual(
                handlers["network"](
                    parser.parse_args(["network", "warm-path", "--company", "Acme"])
                ),
                0,
            )
        out = buf.getvalue()
        self.assertIn("Ada Lovelace", out)

    def test_register_tools_registers_five_tools(self):
        seen: list[str] = []

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen.append(fn.__name__)
                    return fn

                return deco

        network_crm.register_tools(FakeMCP())
        self.assertEqual(
            seen,
            ["add_contact", "list_contacts", "log_interaction", "nudge_list", "warm_path_to"],
        )


if __name__ == "__main__":
    unittest.main()
