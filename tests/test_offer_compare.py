#!/usr/bin/env python3
"""Offline tests for the offer comparison module."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import offer_compare


class _TempStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_offers = offer_compare.OFFERS_PATH
        self._orig_weights = offer_compare.WEIGHTS_PATH
        offer_compare.OFFERS_PATH = Path(self.tmp.name) / "offers.json"
        offer_compare.WEIGHTS_PATH = Path(self.tmp.name) / "offer_weights.json"

    def tearDown(self):
        offer_compare.OFFERS_PATH = self._orig_offers
        offer_compare.WEIGHTS_PATH = self._orig_weights
        self.tmp.cleanup()


def _offer(**overrides):
    base = {
        "name": "Acme",
        "base": 150000,
        "bonus": 15000,
        "equity_total": 200000,
        "equity_years": 4,
        "benefits_value": 20000,
        "pto_days": 20,
        "remote": "hybrid",
        "location": "New York, NY",
        "growth_score": 7,
        "notes": "",
    }
    base.update(overrides)
    return base


class AddOfferTests(_TempStore):
    def test_add_offer_round_trip(self):
        res = offer_compare.add_offer(**_offer())
        self.assertTrue(res["ok"])
        offer = res["offer"]
        self.assertTrue(offer["id"].startswith("offer-"))
        self.assertEqual(offer["name"], "Acme")
        self.assertIn("created_at", offer)
        disk = json.loads(offer_compare.OFFERS_PATH.read_text())
        self.assertEqual(len(disk), 1)
        self.assertEqual(disk[0]["id"], offer["id"])

    def test_remote_normalized_case_insensitive(self):
        res = offer_compare.add_offer(**_offer(remote="REMOTE"))
        self.assertEqual(res["offer"]["remote"], "remote")

    def test_validation_errors(self):
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(name="   "))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(base=-1))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(base=True))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(remote="moon"))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(growth_score=11))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(growth_score=0))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(location_score=101))
        with self.assertRaises(ValueError):
            offer_compare.add_offer(**_offer(equity_years=0))

    def test_get_and_list(self):
        res = offer_compare.add_offer(**_offer())
        offer_id = res["offer"]["id"]
        self.assertEqual(offer_compare.get_offer(offer_id)["name"], "Acme")
        self.assertIsNone(offer_compare.get_offer("offer-nope"))
        listed = offer_compare.list_offers()
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["offers"]), 1)


class CompMathTests(_TempStore):
    def test_total_comp_4yr(self):
        # 4*(150000+15000) + 200000 + 4*20000 = 660000 + 200000 + 80000
        offer = _offer()
        self.assertEqual(offer_compare.total_comp_4yr(offer), 940000)

    def test_total_comp_4yr_defaults(self):
        offer = {"base": 100000}
        self.assertEqual(offer_compare.total_comp_4yr(offer), 400000)


class WeightsTests(_TempStore):
    def test_default_weights(self):
        self.assertEqual(
            offer_compare.get_weights(),
            {
                "compensation": 40.0,
                "growth": 20.0,
                "benefits": 15.0,
                "flexibility": 15.0,
                "location_fit": 10.0,
            },
        )

    def test_set_weights_full_override(self):
        res = offer_compare.set_weights(
            {
                "compensation": 50.0,
                "growth": 20.0,
                "benefits": 10.0,
                "flexibility": 10.0,
                "location_fit": 10.0,
            }
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["weights"]["compensation"], 50.0)
        # persisted
        self.assertEqual(offer_compare.get_weights()["compensation"], 50.0)

    def test_set_weights_partial_merge(self):
        offer_compare.set_weights(
            {
                "compensation": 50.0,
                "growth": 20.0,
                "benefits": 10.0,
                "flexibility": 10.0,
                "location_fit": 10.0,
            }
        )
        res = offer_compare.set_weights({"compensation": 45.0, "growth": 25.0})
        self.assertEqual(res["weights"]["compensation"], 45.0)
        self.assertEqual(res["weights"]["growth"], 25.0)
        self.assertEqual(res["weights"]["benefits"], 10.0)

    def test_set_weights_must_sum_to_100(self):
        with self.assertRaises(ValueError):
            offer_compare.set_weights({"compensation": 90.0})
        with self.assertRaises(ValueError):
            offer_compare.set_weights({"compensation": -10.0})

    def test_set_weights_unknown_dimension(self):
        with self.assertRaises(ValueError):
            offer_compare.set_weights(
                {
                    "compensation": 40.0,
                    "growth": 20.0,
                    "benefits": 15.0,
                    "flexibility": 15.0,
                    "vibes": 10.0,
                }
            )


class CompareTests(_TempStore):
    def _two_offers(self):
        a = offer_compare.add_offer(**_offer(name="LowPay"))["offer"]
        b = offer_compare.add_offer(
            **_offer(name="HighPay", base=250000, remote="remote")
        )["offer"]
        return a, b

    def test_compare_no_offers(self):
        res = offer_compare.compare_offers()
        self.assertFalse(res["ok"])
        self.assertIn("error", res)

    def test_compare_ranking_and_breakdown(self):
        self._two_offers()
        res = offer_compare.compare_offers()
        self.assertTrue(res["ok"])
        ranked = res["offers"]
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(ranked[0]["offer"]["name"], "HighPay")
        self.assertEqual(ranked[1]["rank"], 2)
        for entry in ranked:
            self.assertEqual(
                set(entry["dimensions"]),
                {"compensation", "growth", "benefits", "flexibility",
                 "location_fit"},
            )
            for score in entry["dimensions"].values():
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, 100)
        # HighPay has the max comp -> compensation score 100
        self.assertEqual(ranked[0]["dimensions"]["compensation"], 100.0)
        # markdown mentions both offers
        self.assertIn("HighPay", res["markdown"])
        self.assertIn("LowPay", res["markdown"])

    def test_compare_single_offer_all_max(self):
        offer_compare.add_offer(**_offer())
        res = offer_compare.compare_offers()
        self.assertTrue(res["ok"])
        entry = res["offers"][0]
        self.assertEqual(entry["dimensions"]["compensation"], 100.0)
        self.assertEqual(entry["dimensions"]["benefits"], 100.0)

    def test_compare_respects_custom_weights(self):
        a, b = self._two_offers()
        # Growth-only ranking: both have growth 7 -> tie broken by name;
        # flip one growth score and confirm it wins.
        offer_compare.set_weights(
            {
                "compensation": 0.0,
                "growth": 100.0,
                "benefits": 0.0,
                "flexibility": 0.0,
                "location_fit": 0.0,
            }
        )
        res = offer_compare.compare_offers()
        self.assertTrue(res["ok"])
        # both growth 7 -> identical totals
        self.assertEqual(
            res["offers"][0]["weighted_total"], res["offers"][1]["weighted_total"]
        )

    def test_location_score_differentiates(self):
        offer_compare.add_offer(**_offer(name="A", location_score=90))
        offer_compare.add_offer(**_offer(name="B", location_score=10))
        res = offer_compare.compare_offers()
        dims = {
            e["offer"]["name"]: e["dimensions"]["location_fit"]
            for e in res["offers"]
        }
        self.assertEqual(dims["A"], 90.0)
        self.assertEqual(dims["B"], 10.0)

    def test_location_score_defaults_neutral(self):
        offer_compare.add_offer(**_offer(name="A"))
        res = offer_compare.compare_offers()
        self.assertEqual(
            res["offers"][0]["dimensions"]["location_fit"], 50.0
        )


class ReportTests(_TempStore):
    def _two_offers(self):
        a = offer_compare.add_offer(**_offer(name="LowPay"))["offer"]
        b = offer_compare.add_offer(
            **_offer(name="HighPay", base=250000, remote="remote")
        )["offer"]
        return a, b

    def test_report_breakdown_math(self):
        a, _ = self._two_offers()
        rep = offer_compare.offer_report(a["id"])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["rank"], 2)
        self.assertEqual(rep["of"], 2)
        md = rep["markdown"]
        # explicit 4-yr math shown
        self.assertIn("4 x (", md)
        self.assertIn("$940,000", md)
        # derived figures labeled estimated
        self.assertIn("(estimated)", md)
        # honesty footer
        self.assertIn("came from you", md)

    def test_report_gap_language(self):
        a, _ = self._two_offers()
        rep = offer_compare.offer_report(a["id"])
        md = rep["markdown"]
        self.assertIn("What would need to be true", md)
        self.assertIn("behind", md)
        self.assertIn("HighPay", md)
        # comp path is present even when not winnable alone
        self.assertIn("- compensation:", md)

    def test_report_winnable_comp_path_translated_to_dollars(self):
        # Compensation-only weights: gap closes exactly on comp.
        offer_compare.set_weights(
            {
                "compensation": 100.0,
                "growth": 0.0,
                "benefits": 0.0,
                "flexibility": 0.0,
                "location_fit": 0.0,
            }
        )
        a = offer_compare.add_offer(**_offer(name="LowPay"))["offer"]
        b = offer_compare.add_offer(
            **_offer(name="MidPay", base=152000)
        )["offer"]
        rep = offer_compare.offer_report(a["id"])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["rank"], 2)
        md = rep["markdown"]
        # gap is winnable on compensation alone -> dollar translation
        self.assertIn("4-yr total comp", md)
        self.assertIn("$8,000", md)

    def test_report_leader(self):
        _, b = self._two_offers()
        rep = offer_compare.offer_report(b["id"])
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["rank"], 1)
        self.assertIn("currently leads", rep["markdown"])

    def test_report_unknown_id(self):
        rep = offer_compare.offer_report("offer-nope")
        self.assertFalse(rep["ok"])
        self.assertIn("error", rep)

    def test_report_no_location_score_hint(self):
        a, _ = self._two_offers()
        rep = offer_compare.offer_report(a["id"])
        self.assertIn("no location score set", rep["markdown"])


class WiringTests(_TempStore):
    def test_register_cli_returns_offers_handler(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mapping = offer_compare.register_cli(sub)
        self.assertIn("offers", mapping)
        self.assertTrue(callable(mapping["offers"]))

    def test_register_tools_registers(self):
        registered = []

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    registered.append(fn.__name__)
                    return fn

                return deco

        offer_compare.register_tools(FakeMCP())
        for name in (
            "add_offer",
            "list_offers",
            "compare_offers",
            "offer_report",
            "set_offer_weights",
            "get_offer_weights",
        ):
            self.assertIn(name, registered)

    def test_mcp_tool_wrappers_call_through(self):
        calls = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    calls[fn.__name__] = fn
                    return fn

                return deco

        offer_compare.register_tools(FakeMCP())
        res = calls["add_offer"]("Acme", 150000)
        self.assertTrue(res["ok"])
        self.assertEqual(res["offer"]["name"], "Acme")
        self.assertTrue(calls["compare_offers"]()["ok"])
        self.assertTrue(
            calls["offer_report"](res["offer"]["id"])["ok"]
        )
        self.assertTrue(calls["get_offer_weights"]()["ok"])
        set_res = calls["set_offer_weights"](
            {
                "compensation": 50.0,
                "growth": 20.0,
                "benefits": 10.0,
                "flexibility": 10.0,
                "location_fit": 10.0,
            }
        )
        self.assertTrue(set_res["ok"])

    def test_cli_end_to_end(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = offer_compare.register_cli(sub)

        def run(argv):
            args = parser.parse_args(["offers"] + argv)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = handlers["offers"](args)
            return code, buf.getvalue()

        code, out = run(
            ["add", "--name", "Acme", "--base", "150000", "--remote", "hybrid"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Stored offer-", out)

        code, out = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("Acme", out)

        code, out = run(["compare"])
        self.assertEqual(code, 0)
        self.assertIn("Offer comparison", out)

        code, out = run(["add", "--name", "Beta", "--base", "250000"])
        self.assertEqual(code, 0)
        offers = json.loads(offer_compare.OFFERS_PATH.read_text())
        beta_id = next(o["id"] for o in offers if o["name"] == "Beta")

        code, out = run(["report", "--offer-id", beta_id])
        self.assertEqual(code, 0)
        self.assertIn("Offer report", out)

        code, out = run(["weights"])
        self.assertEqual(code, 0)
        self.assertIn("compensation 40", out)

        code, out = run(
            ["weights", "--set", "compensation=50", "--set", "growth=50",
             "--set", "benefits=0", "--set", "flexibility=0",
             "--set", "location_fit=0"]
        )
        self.assertEqual(code, 0)

        # invalid add -> nonzero exit, no crash
        code, out = run(["add", "--name", "Bad", "--base", "-5"])
        self.assertEqual(code, 1)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
