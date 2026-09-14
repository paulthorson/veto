"""Tests for Initiative 05 ATS readiness check (Epic 3).

All fixtures are synthetic and labeled as such.

The QA contract is a release blocker: these tests fail if ANY
user-facing copy implies or predicts vendor ranking — both in emitted
messages and in module prose outside the contract-definition block.
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i05 import ats_check


def _profile() -> dict:
    return {
        "full_name": "Synthetic Fixture",
        "email": "fixture@example.com",
        "phone": "555-010-2030",
        "location": "New York, NY",
        "linkedin_url": "https://example.com/in/fixture",
        "website": "https://example.com",
        "summary": "Synthetic fixture profile for tests.",
        "skills": ["Python", "PostgreSQL"],
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "FixtureCorp",
                "dates": "2020 - 2024",
                "bullets": [
                    "Shipped Python services handling 1M requests a day.",
                ],
            }
        ],
        "education": [{"degree": "B.S.", "school": "Fixture University",
                       "dates": "2016 - 2020"}],
    }


def _resume_md() -> str:
    # A genuinely complete resume: comfortably inside the module's own
    # 80-1600 word heuristic so the "good resume is ready" tests are
    # honest about what the module claims.
    return (
        "# Synthetic Fixture\n"
        "fixture@example.com | 555-010-2030 | New York, NY\n"
        "https://example.com/in/fixture\n"
        "\n"
        "## Summary\n"
        "Synthetic fixture backend engineer with six years building "
        "Python services, PostgreSQL schemas, and deployment pipelines "
        "for high-traffic products.\n"
        "\n"
        "## Experience\n"
        "### Backend Engineer — FixtureCorp\n"
        "*2020 - 2024*\n"
        "- Shipped Python services handling 1M requests a day.\n"
        "- Cut p99 latency in half by tuning PostgreSQL indexes and "
        "connection pooling.\n"
        "- Led the migration from cron scripts to a scheduled task "
        "runner with full observability.\n"
        "\n"
        "### Junior Developer — FixtureLabs\n"
        "*2016 - 2020*\n"
        "- Built internal dashboards used by forty engineers every week.\n"
        "- Wrote integration tests that caught regressions before release.\n"
        "\n"
        "## Skills\n"
        "Python, PostgreSQL, Docker, CI pipelines\n"
    )


def _parser_hostile_md() -> str:
    """Blind-review probe: tables detected, zero contact info, 2019
    words, no bullets, no date lines. Returned ready=True before the
    rework; must not after it."""
    filler = ("word " * 2001).strip()
    return (
        "# Probe Candidate\n"
        "## Experience\n"
        "| Role | Years |\n"
        "|---|---|\n"
        "| Engineer | 5 |\n"
        "## Skills\n"
        + filler + "\n"
    )


def _job() -> dict:
    return {
        "id": "job-synth-1",
        "title": "Backend Engineer",
        "description": "We need Python and PostgreSQL experience.",
    }


class CopyContractTest(unittest.TestCase):
    def test_no_banned_phrase_in_any_emitted_message(self):
        # Battery of inputs: good resume, empty resume, partial resume.
        resumes = [
            _resume_md(),
            "",
            "# Name Only\n",
            _resume_md().replace("## Skills\nPython, PostgreSQL, Docker, "
                                 "CI pipelines\n", ""),
            _parser_hostile_md(),
        ]
        for md in resumes:
            report = ats_check.ats_readiness_check(md, _job(), _profile())
            for finding in report["findings"]:
                violations = ats_check.check_copy(finding["message"])
                self.assertEqual(
                    violations, [],
                    f"banned phrase in {finding['message']!r}")
            text = ats_check.render_report_text(report)
            self.assertEqual(ats_check.check_copy(text), [])

    def test_module_prose_outside_contract_block_is_clean(self):
        src = Path(ats_check.__file__).read_text(encoding="utf-8")
        begin = src.index("# COPY_CONTRACT_BEGIN")
        end = src.index("# COPY_CONTRACT_END")
        outside = src[:begin] + src[end:]
        violations = ats_check.check_copy(outside)
        self.assertEqual(
            violations, [],
            f"module prose violates its own QA contract: {violations}")

    def test_check_copy_detects_violations(self):
        self.assertTrue(
            ats_check.check_copy("This will rank you #1 in Workday"))
        self.assertTrue(
            ats_check.check_copy("beat the ATS with these keywords"))
        self.assertEqual(ats_check.check_copy("parseable and complete"), [])


class StructureCheckTest(unittest.TestCase):
    def test_good_resume_passes_structure(self):
        findings = ats_check.check_structure(_resume_md())
        by_area = {}
        for f in findings:
            by_area.setdefault(f["area"], []).append(f["severity"])
        self.assertNotIn("fail", by_area["structure"])

    def test_empty_resume_fails_loudly(self):
        report = ats_check.ats_readiness_check("", _job(), _profile())
        self.assertFalse(report["ready"])
        self.assertGreater(report["summary"]["fail"], 0)

    def test_table_warns(self):
        md = _resume_md() + "\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        findings = ats_check.check_structure(md)
        tables = [f for f in findings if "Tables" in f["message"]]
        self.assertTrue(tables)
        self.assertEqual(tables[0]["severity"], "warn")

    def test_pipe_contact_line_is_not_a_table(self):
        # tailor.py joins contact bits with " | " — that is not a table.
        findings = ats_check.check_structure(_resume_md())
        self.assertFalse(
            [f for f in findings if "Tables were detected" in f["message"]])

    def test_no_date_lines_is_honest_skip(self):
        # Date lines in a format the check cannot see must not earn a
        # "consistent" verdict.
        md = _resume_md().replace("*2020 - 2024*", "2020 - 2024").replace(
            "*2016 - 2020*", "2016 - 2020")
        findings = ats_check.check_structure(md)
        skipped = [f for f in findings if f["key"] == "dates_none"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["severity"], "warn")
        self.assertIn("skipped", skipped[0]["message"])
        self.assertNotIn("consistent", skipped[0]["message"])
        self.assertFalse(skipped[0]["blocking"])

    def test_date_lines_examined_can_still_pass(self):
        findings = ats_check.check_structure(_resume_md())
        ok = [f for f in findings if f["key"] == "dates_ok"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["severity"], "pass")

    def test_bad_date_lines_warn(self):
        md = _resume_md().replace("*2020 - 2024*", "*sometime*")
        findings = ats_check.check_structure(md)
        warned = [f for f in findings if f["key"] == "dates_warn"]
        self.assertEqual(len(warned), 1)
        self.assertEqual(warned[0]["severity"], "warn")
        self.assertTrue(warned[0]["blocking"])


class ReadinessVerdictTest(unittest.TestCase):
    def test_probe_fixture_is_2019_words(self):
        self.assertEqual(len(_parser_hostile_md().split()), 2019)

    def test_parser_hostile_probe_is_not_ready(self):
        # The blind-review blocker: tables + no contact + 2019 words +
        # no bullets + all fields missing returned ready=True before
        # the rework. The verdict must never claim readiness for it.
        report = ats_check.ats_readiness_check(
            _parser_hostile_md(), None, {})
        self.assertFalse(report["ready"])
        text = ats_check.render_report_text(report)
        self.assertIn("needs attention", text)
        self.assertNotIn("— ready", text)
        blocking_keys = {f["key"] for f in report["findings"]
                         if f["blocking"]}
        self.assertTrue(
            {"tables_found", "contact_missing", "length_warn",
             "bullets_missing", "field_partial"} <= blocking_keys)

    def test_advisory_warnings_alone_do_not_block_ready(self):
        # A skipped date check is inconclusive, not evidence of a bad
        # document — the verdict stays honest either way.
        md = _resume_md().replace("*2020 - 2024*", "2020 - 2024").replace(
            "*2016 - 2020*", "2016 - 2020")
        report = ats_check.ats_readiness_check(md, _job(), _profile())
        self.assertTrue(report["ready"])
        self.assertTrue(
            any(f["key"] == "dates_none" for f in report["findings"]))

    def test_good_resume_is_ready(self):
        report = ats_check.ats_readiness_check(
            _resume_md(), _job(), _profile())
        self.assertTrue(report["ready"])
        text = ats_check.render_report_text(report)
        self.assertIn("ATS readiness check", text)
        # Summary counts add up.
        s = report["summary"]
        self.assertEqual(
            s["pass"] + s["warn"] + s["fail"], len(report["findings"]))


class KeywordCoverageTest(unittest.TestCase):
    def test_full_coverage_passes(self):
        findings = ats_check.check_keyword_coverage(
            _resume_md(), _job(), _profile())
        self.assertEqual(findings[0]["severity"], "pass")

    def test_partial_coverage_warns_with_missing_list(self):
        md = _resume_md().replace("PostgreSQL", "a database")
        findings = ats_check.check_keyword_coverage(md, _job(), _profile())
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertIn("PostgreSQL", findings[0]["message"])
        # Content matching is advisory — it never gates readiness.
        self.assertFalse(findings[0]["blocking"])

    def test_no_keywords_is_not_a_failure(self):
        findings = ats_check.check_keyword_coverage(
            _resume_md(), {"title": "x"}, _profile())
        self.assertEqual(findings[0]["severity"], "pass")

    def test_broken_tailor_does_not_kill_report(self):
        bad = types.ModuleType("tailor")

        def _boom(job_details, profile):
            raise RuntimeError("synthetic tailor failure")

        bad.extract_job_keywords = _boom
        with mock.patch.dict(sys.modules, {"tailor": bad}):
            findings = ats_check.check_keyword_coverage(
                _resume_md(), _job(), _profile())
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertIn("skipped", findings[0]["message"])
        self.assertFalse(findings[0]["blocking"])


class FieldCompletenessTest(unittest.TestCase):
    def test_complete_profile_passes(self):
        findings = ats_check.check_field_completeness(_profile())
        self.assertEqual(findings[0]["severity"], "pass")

    def test_missing_fields_listed(self):
        profile = dict(_profile())
        del profile["phone"]
        del profile["website"]
        findings = ats_check.check_field_completeness(profile)
        self.assertEqual(findings[0]["severity"], "warn")
        self.assertIn("phone", findings[0]["message"])
        self.assertIn("website", findings[0]["message"])


class ExportCheckTest(unittest.TestCase):
    def test_exports_render(self):
        findings = ats_check.check_exports(_profile())
        self.assertEqual(findings[0]["severity"], "pass")
        self.assertTrue(
            any("PDF backend" in f["message"] for f in findings))

    def test_export_claim_is_scoped_to_the_profile(self):
        # Blind-review major: the check renders the profile, so the
        # copy must never imply the resume markdown was rendered.
        findings = ats_check.check_exports(_profile())
        self.assertIn("Profile", findings[0]["message"])
        self.assertNotIn("resume", findings[0]["message"].lower())

    def test_empty_profile_export_check_is_skipped(self):
        # Rendering an empty profile says nothing — say so honestly.
        findings = ats_check.check_exports({})
        self.assertEqual(findings[0]["severity"], "pass")
        self.assertIn("skipped", findings[0]["message"])
        self.assertIn("profile", findings[0]["message"].lower())

    def test_report_ready_flag(self):
        report = ats_check.ats_readiness_check(
            _resume_md(), _job(), _profile())
        self.assertTrue(report["ready"])
        text = ats_check.render_report_text(report)
        self.assertIn("ATS readiness check", text)
        # Summary counts add up.
        s = report["summary"]
        self.assertEqual(
            s["pass"] + s["warn"] + s["fail"], len(report["findings"]))


if __name__ == "__main__":
    unittest.main()
