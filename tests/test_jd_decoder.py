#!/usr/bin/env python3
"""Unit tests for jd_decoder.py: deterministic JD analysis (no network)."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jd_decoder  # noqa: E402


TRAP_JD = """\
Wanted: Rockstar Developer Ninja!
Join our fast-paced team where you'll wear many hats in a work hard play
hard culture. We offer unlimited PTO and competitive salary. Must be
always on for our 24/7 product. You'll leverage synergies to disrupt the
paradigm.
"""

GOOD_JD = """\
Senior Backend Engineer — $140k-$170k base salary + stock options with
4-year vesting.
- Own and operate our payments API serving 2M requests/day
- Design and ship new services in Python and Go
- Mentor junior engineers through weekly 1:1s
- Participate in on-call rotation (1 week in 6, compensated)
Remote-first, flexible hours. Learning budget of $2,000/year. Clear
promotion path to staff.
Must-haves: 5+ years backend experience. Nice-to-haves: fintech background.
"""


class TestSalaryDetection(unittest.TestCase):
    def test_range_formats(self):
        cases = [
            "$120k-$150k",
            "$120,000 - $150,000",
            "£60,000–£75,000",
            "USD 100k to 130k",
            "€90K-€110K",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                d = jd_decoder.decode_jd(f"Role. Salary {raw} per year.")
                rng = d["transparency"]["salary_range"]
                self.assertIsNotNone(rng, f"no range detected in {raw!r}")
                self.assertIn(raw.split("-")[0].strip("–—")[:4], rng["raw"])

    def test_range_quote_is_evidence(self):
        d = jd_decoder.decode_jd("Pay: $120k-$150k. Great team.")
        quote = d["transparency"]["salary_range"]["quote"]
        self.assertIn("$120k-$150k", quote)

    def test_competitive_salary_is_mention_not_range(self):
        d = jd_decoder.decode_jd("We pay a competitive salary. Apply now.")
        t = d["transparency"]
        self.assertIsNone(t["salary_range"])
        self.assertIsNotNone(t["salary_mention"])
        self.assertIn("competitive salary", t["salary_mention"]["quote"])

    def test_no_salary_scores_zero_transparency(self):
        d = jd_decoder.decode_jd("Great role. Apply now.")
        self.assertEqual(d["transparency"]["score"], 0)
        self.assertIsNone(d["transparency"]["salary_range"])


class TestEquity(unittest.TestCase):
    def test_concrete_equity(self):
        d = jd_decoder.decode_jd(
            "Compensation includes stock options with 4-year vesting."
        )
        eq = d["transparency"]["equity"]
        self.assertIsNotNone(eq)
        self.assertTrue(eq["concrete"])

    def test_vague_equity(self):
        d = jd_decoder.decode_jd("You'll get equity and a great culture.")
        eq = d["transparency"]["equity"]
        self.assertIsNotNone(eq)
        self.assertFalse(eq["concrete"])

    def test_no_equity(self):
        d = jd_decoder.decode_jd("Salary $100k-$120k. No mention otherwise.")
        self.assertIsNone(d["transparency"]["equity"])


class TestOverworkLanguage(unittest.TestCase):
    def test_unlimited_pto_flagged(self):
        d = jd_decoder.decode_jd("We offer unlimited PTO and snacks.")
        hits = {h["phrase"]: h for h in d["overwork"]}
        self.assertIn("unlimited PTO", hits)
        self.assertIn("unlimited PTO", hits["unlimited PTO"]["quote"])
        # Plain-English read, not a moral judgment.
        self.assertIn("less time off", hits["unlimited PTO"]["read"])

    def test_all_trap_phrases_quoted(self):
        d = jd_decoder.decode_jd(TRAP_JD)
        phrases = {h["phrase"] for h in d["overwork"]}
        for expected in (
            "fast-paced", "wear many hats", "work hard play hard",
            "unlimited PTO", "always on", "24/7",
        ):
            with self.subTest(phrase=expected):
                self.assertIn(expected, phrases)
        for hit in d["overwork"]:
            self.assertIn(
                hit["phrase"].lower(), hit["quote"].lower(),
                "every flag must carry its evidence quote",
            )

    def test_clean_jd_has_no_flags(self):
        self.assertEqual(jd_decoder.decode_jd(GOOD_JD)["overwork"], [])

    def test_reads_are_advice_not_morals(self):
        d = jd_decoder.decode_jd(TRAP_JD)
        for hit in d["overwork"]:
            read = hit["read"].lower()
            self.assertNotIn("evil", read)
            self.assertNotIn("shame", read)
            self.assertIn("ask", read)  # each read suggests a question


class TestVagueness(unittest.TestCase):
    def test_buzzwords_detected(self):
        d = jd_decoder.decode_jd(TRAP_JD)
        v = d["vagueness"]
        found = {b["buzzword"] for b in v["buzzwords"]}
        self.assertTrue({"rockstar", "ninja"} <= found)
        self.assertGreaterEqual(v["buzzword_density_per_100_words"], 3)
        self.assertEqual(v["responsibilities"], 0)

    def test_responsibilities_counted(self):
        d = jd_decoder.decode_jd(GOOD_JD)
        v = d["vagueness"]
        self.assertEqual(v["responsibilities"], 4)
        self.assertEqual(v["buzzword_count"], 0)
        self.assertGreater(v["score"], 50)

    def test_trap_is_vague(self):
        d = jd_decoder.decode_jd(TRAP_JD)
        self.assertLess(d["vagueness"]["score"], 50)


class TestGrowthAndGreenFlags(unittest.TestCase):
    def test_growth_signals_quoted(self):
        d = jd_decoder.decode_jd(GOOD_JD)
        signals = {g["signal"]: g["quote"] for g in d["growth"]}
        self.assertIn("mentoring", signals)
        self.assertIn("learning budget", signals)
        self.assertIn("promotion path", signals)
        for signal, quote in signals.items():
            self.assertTrue(quote.strip(), f"{signal} missing quote")

    def test_green_flags(self):
        d = jd_decoder.decode_jd(GOOD_JD)
        flags = {f["flag"]: f["quote"] for f in d["green_flags"]}
        self.assertIn("remote flexibility", flags)
        self.assertIn("flexible schedule", flags)
        self.assertIn("salary range disclosed", flags)
        self.assertIn("nice-to-haves listed", flags)
        self.assertIn("$140k-$170k", flags["salary range disclosed"])


class TestVerdict(unittest.TestCase):
    def test_good_jd_is_strong(self):
        r = jd_decoder.jd_verdict(GOOD_JD)
        self.assertEqual(r["verdict"], "strong")
        self.assertGreaterEqual(r["score"], 70)

    def test_trap_jd_is_caution(self):
        r = jd_decoder.jd_verdict(TRAP_JD)
        self.assertEqual(r["verdict"], "caution")
        self.assertLess(r["score"], 40)

    def test_reasons_grounded_in_quotes(self):
        r = jd_decoder.jd_verdict(GOOD_JD)
        self.assertEqual(len(r["reasons"]), 3)
        for reason in r["reasons"]:
            with self.subTest(reason=reason["reason"]):
                # Quoted reasons must quote the actual posting text.
                if reason["quote"] is not None:
                    snippet = reason["quote"].replace("…", "").strip()
                    core = " ".join(snippet.split())
                    # The quote's core words must come from the JD.
                    self.assertTrue(
                        any(w.lower() in GOOD_JD.lower() for w in core.split()[:5]),
                        "reason quote not grounded in the JD",
                    )

    def test_trap_reasons_cite_trap_language(self):
        r = jd_decoder.jd_verdict(TRAP_JD)
        quoted = " ".join(
            (x["quote"] or "") for x in r["reasons"]
        ).lower()
        self.assertTrue(
            "unlimited pto" in quoted or "fast-paced" in quoted
            or "wear many hats" in quoted,
            "top reasons should cite the trap language",
        )

    def test_empty_text_does_not_crash(self):
        r = jd_decoder.jd_verdict("")
        self.assertIn(r["verdict"], ("strong", "mixed", "caution"))
        self.assertEqual(len(r["reasons"]), 1)

    def test_deterministic(self):
        self.assertEqual(
            jd_decoder.jd_verdict(GOOD_JD)["score"],
            jd_decoder.jd_verdict(GOOD_JD)["score"],
        )
        self.assertEqual(
            jd_decoder.decode_jd(TRAP_JD), jd_decoder.decode_jd(TRAP_JD)
        )

    def test_verdict_labels_bounded(self):
        for text in (GOOD_JD, TRAP_JD, "Hello world", ""):
            r = jd_decoder.jd_verdict(text)
            self.assertIn(r["verdict"], ("strong", "mixed", "caution"))
            self.assertGreaterEqual(r["score"], 0)
            self.assertLessEqual(r["score"], 100)


class TestPluginWiring(unittest.TestCase):
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
        jd_decoder.register_tools(mcp)
        self.assertEqual(set(mcp.tools), {"decode_jd", "jd_verdict"})
        out = mcp.tools["decode_jd"]("Salary $50k-$60k.")
        self.assertIsNotNone(out["transparency"]["salary_range"])
        verdict = mcp.tools["jd_verdict"]("Salary $50k-$60k.")
        self.assertIn(verdict["verdict"], ("strong", "mixed", "caution"))

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mapping = jd_decoder.register_cli(sub)
        self.assertEqual(set(mapping), {"decode-jd"})
        handler = mapping["decode-jd"]
        args = argparse.Namespace(
            text="We offer unlimited PTO.", file=None, verdict=True, json=True
        )
        self.assertEqual(handler(args), 0)

    def test_cli_rejects_empty(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mapping = jd_decoder.register_cli(sub)
        args = argparse.Namespace(text="", file=None, verdict=False, json=False)
        self.assertEqual(mapping["decode-jd"](args), 2)


if __name__ == "__main__":
    unittest.main()
