#!/usr/bin/env python3
"""Tests for linkedin_optimizer.py — LinkedIn profile audit + rewrites.

Stdlib unittest only. No network, no file I/O beyond module import —
profiles are plain dicts, so no store redirection is needed. The key
honesty tests assert that rewrites never introduce facts (numbers,
employers, titles) absent from the profile.

Run:  cd ~/workspace/job-apply-mcp && .venv/bin/python -m unittest discover -s tests -v
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import linkedin_optimizer as lo


def _strong_profile(**overrides):
    profile = {
        "first_name": "Ada",
        "headline": "Senior Backend Engineer | 8 yrs | Python, Kubernetes, AWS | ex-Acme Corp",
        "summary": (
            "I'm Ada, a backend engineer with 8 years of experience building "
            "high-throughput services. I led a team of 6 engineers and cut "
            "p99 latency by 40% while growing traffic 3x. My core toolkit: "
            "Python, Kubernetes, AWS, PostgreSQL. Open to Staff Engineer "
            "roles — happy to connect."
        ),
        "target_titles": ["Staff Engineer", "Senior Backend Engineer"],
        "years_experience": 8,
        "skills": ["Python", "Kubernetes", "AWS", "PostgreSQL", "Docker", "gRPC"],
        "experience": [
            {
                "title": "Senior Backend Engineer",
                "company": "Acme Corp",
                "dates": "2020-2024",
                "bullets": [
                    "Led a team of 6 engineers shipping payments platform",
                    "Cut p99 latency by 40% across 12 services",
                    "Grew request volume 3x to 2M requests/day",
                ],
            }
        ],
        "education": [
            {"degree": "B.S. Computer Science", "school": "State University"}
        ],
    }
    profile.update(overrides)
    return profile


def _trap_facts():
    """Facts that must never appear in rewrites unless in the profile."""
    return [
        "Stanford", "Google", "Meta", "Fortune 500", "$1B", "Harvard",
        "unicorn", "10x engineer",
    ]


class TestAudit(unittest.TestCase):
    def test_empty_profile_scores_low_with_fixes(self):
        result = lo.audit_profile({})
        self.assertLess(result["overall"], 40)
        self.assertTrue(result["top_fixes"])
        for section in ("headline", "about", "experience_bullets", "skills_coverage"):
            self.assertIn(section, result["sections"])

    def test_strong_profile_scores_high(self):
        result = lo.audit_profile(_strong_profile(), "engineer")
        self.assertGreaterEqual(result["overall"], 70)
        self.assertEqual(result["target_role"], "engineer")
        self.assertGreaterEqual(result["sections"]["headline"]["score"], 70)

    def test_missing_headline_zero(self):
        result = lo.audit_profile(_strong_profile(headline=""))
        self.assertEqual(result["sections"]["headline"]["score"], 0)

    def test_overlong_headline_flagged(self):
        result = lo.audit_profile(_strong_profile(headline="x" * 250))
        self.assertIn("trim", " ".join(result["sections"]["headline"]["fixes"]).lower())

    def test_about_first_person_and_proof(self):
        result = lo.audit_profile(_strong_profile(), None)
        about = result["sections"]["about"]
        self.assertGreaterEqual(about["score"], 70)

    def test_about_third_person_flagged(self):
        p = _strong_profile(summary="Seasoned engineer. Responsible for systems.")
        fixes = lo.audit_profile(p)["sections"]["about"]["fixes"]
        self.assertTrue(any("first person" in f for f in fixes))

    def test_bullets_action_verb_and_quantified(self):
        result = lo.audit_profile(_strong_profile())
        bullets = result["sections"]["experience_bullets"]
        self.assertGreaterEqual(bullets["score"], 70)

    def test_weak_bullets_flagged(self):
        p = _strong_profile(experience=[{
            "title": "Engineer", "company": "X", "dates": "2020",
            "bullets": ["Responsible for various tasks", "Helped with stuff"],
        }])
        bullets = lo.audit_profile(p)["sections"]["experience_bullets"]
        self.assertLess(bullets["score"], 60)
        self.assertTrue(bullets["fixes"])

    def test_skills_coverage_target_role(self):
        result = lo.audit_profile(_strong_profile(), "engineer")
        skills = result["sections"]["skills_coverage"]
        self.assertGreater(skills["score"], 0)
        self.assertLessEqual(skills["score"], 100)

    def test_audit_never_raises_on_sparse_profile(self):
        lo.audit_profile({"skills": []})
        lo.audit_profile({"experience": [{"bullets": None}]})


class TestResolveRole(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(lo.resolve_role("backend developer"), "engineer")
        self.assertEqual(lo.resolve_role("Product Manager"), "product")
        self.assertEqual(lo.resolve_role("data scientist"), "data")
        self.assertEqual(lo.resolve_role("UX designer"), "design")
        self.assertEqual(lo.resolve_role("account executive"), "sales")
        self.assertEqual(lo.resolve_role("growth"), "marketing")

    def test_unknown_and_empty(self):
        self.assertIsNone(lo.resolve_role("astronaut"))
        self.assertIsNone(lo.resolve_role(""))
        self.assertIsNone(lo.resolve_role(None))


class TestRewriteHeadline(unittest.TestCase):
    def test_uses_profile_facts(self):
        p = _strong_profile()
        result = lo.rewrite_headline(p, "engineer")
        h = result["headline"]
        self.assertIn("Senior Backend Engineer", h)
        self.assertIn("8 yrs", h)
        self.assertIn("Python", h)
        self.assertLessEqual(len(h), lo.HEADLINE_LIMIT)

    def test_no_experience_falls_back_to_target_titles(self):
        p = _strong_profile(experience=[])
        result = lo.rewrite_headline(p, "engineer")
        self.assertIn("Staff Engineer", result["headline"])
        for trap in _trap_facts():
            self.assertNotIn(trap, result["headline"])

    def test_no_invented_title_without_any_source(self):
        p = {"skills": ["Python"]}
        result = lo.rewrite_headline(p, "backend engineer")
        self.assertIn("Aspiring", result["headline"])
        self.assertNotIn("Senior", result["headline"])

    def test_deterministic(self):
        p = _strong_profile()
        self.assertEqual(
            lo.rewrite_headline(p, "engineer"),
            lo.rewrite_headline(p, "engineer"),
        )


class TestRewriteAboutHonesty(unittest.TestCase):
    def _blob(self, profile):
        return lo._profile_blob(profile)

    def test_numbers_come_from_profile_only(self):
        p = _strong_profile()
        about = lo.rewrite_about(p)["about"]
        blob = self._blob(p)
        for num in re.findall(r"\d[\d,]*(?:\.\d+)?", about):
            self.assertIn(num, blob, f"number {num!r} not in profile")

    def test_no_trap_facts(self):
        p = _strong_profile()
        about = lo.rewrite_about(p)["about"]
        for trap in _trap_facts():
            self.assertNotIn(trap, about)

    def test_employers_come_from_profile(self):
        p = _strong_profile()
        about = lo.rewrite_about(p)["about"]
        # Only employer in profile is Acme Corp; nothing else company-like invented.
        self.assertIn("Acme Corp", about)
        self.assertNotIn("Initech", about)

    def test_proof_lines_reuse_user_bullets(self):
        p = _strong_profile()
        result = lo.rewrite_about(p)
        self.assertIn("Cut p99 latency by 40% across 12 services", result["about"])
        self.assertIn("built_from", result)
        self.assertTrue(result["built_from"])

    def test_no_proof_without_numbers(self):
        p = _strong_profile(experience=[{
            "title": "Engineer", "company": "X", "dates": "2020",
            "bullets": ["Helped with various tasks"],
        }])
        about = lo.rewrite_about(p)["about"]
        blob = self._blob(p)
        for num in re.findall(r"\d[\d,]*(?:\.\d+)?", about):
            self.assertIn(num, blob)

    def test_first_person_voice(self):
        about = lo.rewrite_about(_strong_profile())["about"]
        self.assertRegex(about, r"\bI'm\b")

    def test_deterministic(self):
        p = _strong_profile()
        self.assertEqual(lo.rewrite_about(p), lo.rewrite_about(p))


class TestKeywordGap(unittest.TestCase):
    def test_missing_ranked_by_importance(self):
        p = {"skills": ["Python"], "summary": "I write Python."}
        result = lo.keyword_gap(p, "engineer")
        self.assertEqual(result["target_role"], "engineer")
        missing = result["missing"]
        self.assertTrue(missing)
        importances = [m["importance"] for m in missing]
        self.assertEqual(importances, sorted(importances, reverse=True))
        # python is present -> not in missing
        self.assertNotIn("python", [m["keyword"] for m in missing])

    def test_tips_never_encourage_invention(self):
        result = lo.keyword_gap({"skills": []}, "product")
        for m in result["missing"]:
            self.assertIn("genuinely", m["tip"])

    def test_no_role_returns_note(self):
        result = lo.keyword_gap(_strong_profile(), None)
        self.assertEqual(result["missing"], [])

    def test_full_coverage_empty_missing(self):
        blob_words = " ".join(kw for kw, _ in lo.ROLE_KEYWORDS["sales"])
        p = {"skills": [blob_words], "summary": blob_words}
        result = lo.keyword_gap(p, "sales")
        self.assertEqual(result["missing"], [])


class TestPluginWiring(unittest.TestCase):
    def test_register_cli(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = lo.register_cli(sub)
        self.assertEqual(list(handlers), ["linkedin"])
        self.assertTrue(callable(handlers["linkedin"]))
        args = parser.parse_args(["linkedin", "audit", "--json"])
        self.assertEqual(args.action, "audit")
        self.assertTrue(args.json)

    def test_register_tools(self):
        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn
                return deco

        lo.register_tools(FakeMCP())
        self.assertEqual(
            set(seen),
            {"linkedin_audit", "linkedin_headline", "linkedin_about",
             "linkedin_keyword_gap"},
        )

    def test_cmd_linkedin_actions(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = lo.register_cli(sub)
        with mock.patch.object(
            lo, "_load_profile", return_value=_strong_profile()
        ):
            for action in ("audit", "headline", "about", "keywords"):
                args = parser.parse_args(
                    ["linkedin", action, "--target-role", "engineer", "--json"]
                )
                with mock.patch("builtins.print"):
                    self.assertEqual(handlers["linkedin"](args), 0)

    def test_cmd_linkedin_missing_profile_ok(self):
        import argparse
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = lo.register_cli(sub)
        with mock.patch.object(lo, "_load_profile", return_value={}):
            args = parser.parse_args(["linkedin", "audit"])
            with mock.patch("builtins.print"):
                self.assertEqual(handlers["linkedin"](args), 0)


if __name__ == "__main__":
    unittest.main()
