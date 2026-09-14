#!/usr/bin/env python3
"""Unit tests for referrals.py (offline, fixture-driven)."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

import referrals


FIXTURE_CSV = """First Name,Last Name,URL,Email Address,Company,Position,Connected On
Ada,Lovelace,https://linkedin.com/in/ada,,Stripe Inc.,Senior Backend Engineer,01 Jan 2023
Grace,Hopper,,grace@example.com,Initech,Engineering Manager,15 Mar 2022
Alan,Turing,,,Acme Corp,Staff Software Engineer,02 Feb 2021
,,, ,,,,
Katherine,Johnson,,k@example.com,Stripe,DevOps Engineer,09 Sep 2020
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _profile() -> dict:
    return {
        "full_name": "Test User",
        "skills": ["Python", "Go", "Docker", "Kubernetes"],
        "target_titles": ["Backend Engineer", "DevOps Engineer"],
    }


class LoadConnectionsTest(unittest.TestCase):
    def test_parses_linkedin_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "Connections.csv", FIXTURE_CSV)
            contacts = referrals.load_connections(path)
        self.assertEqual(len(contacts), 4)  # blank row skipped
        ada = contacts[0]
        self.assertEqual(ada["name"], "Ada Lovelace")
        self.assertEqual(ada["company"], "Stripe Inc.")
        self.assertEqual(ada["position"], "Senior Backend Engineer")
        self.assertEqual(ada["source"], "csv")

    def test_missing_file_returns_empty(self):
        self.assertEqual(referrals.load_connections("/nonexistent/x.csv"), [])

    def test_renamed_columns_tolerated(self):
        text = "first,last,employer,job_title\nBob,Jones,Globex,Junior QA\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "c.csv", text)
            contacts = referrals.load_connections(path)
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["name"], "Bob Jones")
        self.assertEqual(contacts[0]["company"], "Globex")
        self.assertEqual(contacts[0]["position"], "Junior QA")

    def test_bom_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.csv"
            path.write_bytes(
                "First Name,Last Name,Company,Position\nZed,Zero,Acme,Dev\n".encode("utf-8-sig")
            )
            contacts = referrals.load_connections(path)
        self.assertEqual(contacts[0]["first_name"], "Zed")

    def test_json_fallback(self):
        data = [
            {"name": "Manual Person", "company": "Hooli", "position": "SRE"},
            {"name": "  ", "company": "X"},  # blank -> skipped
            {"first_name": "Split", "last_name": "Name", "company": "Y"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "connections.json", json.dumps(data)
            )
            contacts = referrals.load_connections_json(path)
        self.assertEqual(len(contacts), 2)
        self.assertEqual(contacts[0]["source"], "json")
        self.assertEqual(contacts[1]["name"], "Split Name")

    def test_network_prefers_csv_then_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            csv_path = _write(tmpdir / "c.csv", FIXTURE_CSV)
            json_path = _write(
                tmpdir / "c.json",
                json.dumps([{"name": "Fallback", "company": "Z"}]),
            )
            self.assertEqual(
                referrals.load_network(csv_path, json_path)[0]["name"],
                "Ada Lovelace",
            )
            # CSV missing -> JSON fallback
            got = referrals.load_network(tmpdir / "nope.csv", json_path)
            self.assertEqual(got[0]["name"], "Fallback")
            # Neither -> []
            self.assertEqual(
                referrals.load_network(tmpdir / "a.csv", tmpdir / "b.json"), []
            )


class TargetCompaniesTest(unittest.TestCase):
    def test_gathers_from_prefs_watches_applications(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            prefs = tmpdir / "preferences.json"
            prefs.write_text(
                json.dumps({"preferred_companies": ["Stripe", "stripe inc."]}),
                encoding="utf-8",
            )
            watches = tmpdir / "watches.json"
            watches.write_text(
                json.dumps(
                    {
                        "w1": {
                            "filters": {"preferred_companies": ["Initech"]}
                        },
                        "w2": {"filters": {}},
                    }
                ),
                encoding="utf-8",
            )
            apps = tmpdir / "applications.json"
            apps.write_text(
                json.dumps(
                    [
                        {"company": "Acme Corp"},
                        {"company": "Unknown"},
                        {"company": ""},
                    ]
                ),
                encoding="utf-8",
            )
            targets = referrals.target_companies(watches, apps, prefs)
        # deduped case-insensitively ("Stripe" vs "stripe inc."), order kept
        self.assertEqual(targets, ["Stripe", "Initech", "Acme Corp"])

    def test_missing_files_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            self.assertEqual(
                referrals.target_companies(
                    tmpdir / "w.json", tmpdir / "a.json", tmpdir / "p.json"
                ),
                [],
            )


class NormalizeCompanyTest(unittest.TestCase):
    def test_suffix_stripping(self):
        self.assertEqual(referrals.normalize_company("Stripe Inc."), "stripe")
        self.assertEqual(
            referrals.normalize_company("Acme Corporation"), "acme"
        )
        self.assertEqual(referrals.normalize_company("  Initech LLC "), "initech")


class SeniorityTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(referrals.seniority_level("Intern"), 1)
        self.assertEqual(referrals.seniority_level("Junior Engineer"), 1)
        self.assertEqual(referrals.seniority_level("Software Engineer"), 2)
        self.assertEqual(referrals.seniority_level("Senior Backend Engineer"), 4)
        self.assertEqual(referrals.seniority_level("Staff Engineer"), 5)
        self.assertEqual(referrals.seniority_level("Engineering Manager"), 2)
        self.assertEqual(referrals.seniority_level("VP of Engineering"), 7)
        self.assertEqual(referrals.seniority_level(""), 2)


class RankReferralsTest(unittest.TestCase):
    def _contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "c.csv", FIXTURE_CSV)
            return referrals.load_connections(path)

    def test_target_company_ranks_first_with_evidence(self):
        ranked = referrals.rank_referrals(
            self._contacts(), ["Stripe"], _profile()
        )
        # Katherine: target company + exact target seniority (mid) + DevOps
        # keyword overlap. Ada: target company + senior + backend overlap.
        self.assertEqual(ranked[0]["name"], "Katherine Johnson")
        self.assertEqual(ranked[1]["name"], "Ada Lovelace")
        for ref in ranked[:2]:
            self.assertTrue(ref["target_company"])
            self.assertIn("same company", ref["evidence"])
        # relationship strength is never inferred
        for ref in ranked:
            self.assertEqual(ref["relationship"], "connection")
        # Katherine (Stripe + DevOps overlap) outranks Alan (Acme, no overlap)
        names = [r["name"] for r in ranked]
        self.assertLess(names.index("Katherine Johnson"), names.index("Alan Turing"))

    def test_reasons_are_human_readable(self):
        ranked = referrals.rank_referrals(
            self._contacts(), ["Stripe"], _profile()
        )
        ada = ranked[0]
        self.assertTrue(any("target company" in r for r in ada["reasons"]))
        self.assertTrue(
            any("backend" in r or "engineer" in r for r in ada["reasons"])
        )

    def test_no_targets_still_ranks_by_overlap(self):
        ranked = referrals.rank_referrals(self._contacts(), [], _profile())
        self.assertEqual(len(ranked), 4)
        # Ada: senior + backend/python overlap should beat Grace (manager, no overlap)
        names = [r["name"] for r in ranked]
        self.assertLess(names.index("Ada Lovelace"), names.index("Grace Hopper"))

    def test_empty_inputs(self):
        self.assertEqual(referrals.rank_referrals([], ["Stripe"], _profile()), [])
        self.assertEqual(len(referrals.rank_referrals(self._contacts(), [])), 4)


class DraftOutreachTest(unittest.TestCase):
    def test_warm_draft(self):
        contact = {
            "name": "Ada Lovelace",
            "first_name": "Ada",
            "company": "Stripe",
            "position": "Senior Backend Engineer",
        }
        draft = referrals.draft_outreach(
            contact,
            {"title": "Backend Engineer", "company": "Stripe"},
            _profile(),
            kind="warm",
        )
        self.assertEqual(draft["kind"], "warm")
        self.assertEqual(draft["to"], "Ada Lovelace")
        self.assertIn("Stripe", draft["subject"])
        self.assertIn("Hi Ada,", draft["body"])
        self.assertIn("referral", draft["body"].lower())
        # no fabricated shared history
        self.assertNotIn("we worked together", draft["body"].lower())
        self.assertNotIn("mutual", draft["body"].lower())

    def test_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            referrals.draft_outreach({"name": "X"}, {}, {}, kind="lukewarm")
        # The cold LinkedIn variant was deleted (directive §6.2).
        with self.assertRaises(ValueError):
            referrals.draft_outreach({"name": "X"}, {}, {}, kind="cold")


if __name__ == "__main__":
    unittest.main()
