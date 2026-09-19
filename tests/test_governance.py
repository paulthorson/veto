#!/usr/bin/env python3
"""Unit tests for governance/adapter.py.

All framework interaction goes through a fake framework module injected by
monkeypatching ``adapter.load_framework`` — the real governance framework is
never required. One integration test exercises the real /tmp/agentic-governance
clone (read-only: verdicts are redirected to a temp file).
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from governance import adapter
from governance.adapter import GovernanceUnavailable


# ---------------------------------------------------------------------------
# Fake framework
# ---------------------------------------------------------------------------


def make_fake_framework(
    veto_hits=(),
    review_verdict="REVIEW_REQUIRED",
    fail_veto=False,
    fail_review=False,
    fail_record=False,
):
    """Build a fake adversarial_mcp.server module."""
    recorded = []

    def check_veto(domain, text):
        if fail_veto:
            raise RuntimeError("veto backend exploded")
        return {
            "domain": domain,
            "veto_triggered": bool(veto_hits),
            "veto_hits": list(veto_hits),
        }

    def run_review(domain, work, context="", source="app"):
        if fail_review:
            raise RuntimeError("review backend exploded")
        return {
            "domain": domain,
            "verdict": review_verdict,
            "veto_triggered": False,
            "veto_hits": [],
            "agents": ["univ-adversarial-universal"],
            "review_prompt": "ADVERSARIAL PROMPT",
        }

    def record_verdict(domain, verdict, summary, ticket="", rule="", case_tag=""):
        if fail_record:
            raise RuntimeError("ledger exploded")
        rec = {
            "domain": domain,
            "verdict": verdict,
            "summary": summary,
            "ticket": ticket,
            "rule": rule,
            "case_tag": case_tag,
        }
        recorded.append(rec)
        return {"recorded": True, "record": rec}

    def list_domains():
        return ["universal"]

    mod = types.SimpleNamespace(
        check_veto=check_veto,
        run_review=run_review,
        record_verdict=record_verdict,
        list_domains=list_domains,
        recorded=recorded,
    )
    return mod


QUALIFIED_PROFILE = {
    "full_name": "Ada Lovelace",
    "skills": ["Python", "SQL", "AWS", "Docker"],
    "years_experience": "5",
    "experience": [
        {
            "title": "Backend Engineer",
            "company": "Acme",
            "description": "Built APIs with Python and Postgres",
        }
    ],
    "target_titles": ["Backend Engineer"],
}

UNQUALIFIED_PROFILE = {
    "full_name": "Bob Newbie",
    "skills": ["Excel", "Customer Service"],
    "years_experience": "1",
    "experience": [{"title": "Support Rep", "company": "Shop"}],
}

BACKEND_JOB = {
    "title": "Backend Engineer",
    "description": (
        "We are hiring a backend engineer with 3+ years of experience in "
        "Python, SQL, AWS and Docker. Kubernetes experience is a plus."
    ),
    "requirements": "3+ years of professional software experience.",
}

SENIOR_INFRA_JOB = {
    "title": "Senior Infrastructure Engineer",
    "description": (
        "Senior distributed systems role. 7+ years of experience required "
        "with Kubernetes, Terraform, and distributed systems at scale."
    ),
    "requirements": "",
}

JOB_REF = {
    "job_id": "linkedin:abc123",
    "title": "Backend Engineer",
    "company": "Acme",
    "location": "Remote",
    "board": "linkedin",
    "url": "https://example.com/jobs/1",
}


class TestDiscovery(unittest.TestCase):
    def test_find_root_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("GOVERNANCE_ROOT")
            os.environ["GOVERNANCE_ROOT"] = tmp
            try:
                # Isolate the home-directory fallbacks too: a framework
                # installed at ~/agentic-governance must not leak in.
                with mock.patch.object(Path, "home", return_value=Path(tmp)):
                    self.assertIsNone(adapter.find_governance_root())
            finally:
                if old is None:
                    del os.environ["GOVERNANCE_ROOT"]
                else:
                    os.environ["GOVERNANCE_ROOT"] = old

    def test_find_root_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mcp" / "adversarial_mcp").mkdir(parents=True)
            (Path(tmp) / "mcp" / "adversarial_mcp" / "server.py").touch()
            old = os.environ.get("GOVERNANCE_ROOT")
            os.environ["GOVERNANCE_ROOT"] = tmp
            try:
                self.assertEqual(adapter.find_governance_root(), Path(tmp))
            finally:
                if old is None:
                    del os.environ["GOVERNANCE_ROOT"]
                else:
                    os.environ["GOVERNANCE_ROOT"] = old

    def test_enabled_false_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("GOVERNANCE_ROOT")
            old_home = os.environ.get("HOME")
            os.environ["GOVERNANCE_ROOT"] = tmp
            os.environ["HOME"] = tmp  # hide any ~/agentic-governance
            adapter._CACHE["module"] = None
            try:
                self.assertFalse(adapter.governance_enabled())
            finally:
                if old_root is None:
                    os.environ.pop("GOVERNANCE_ROOT", None)
                else:
                    os.environ["GOVERNANCE_ROOT"] = old_root
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
                adapter._CACHE["module"] = None

    def test_fastmcp_shim_identity(self):
        adapter._install_fastmcp_shim()
        deco = sys.modules["mcp.server.fastmcp"].FastMCP("x").tool()

        def fn(a):
            return a

        self.assertIs(deco(fn), fn)


class TestGate(unittest.TestCase):
    def setUp(self):
        self._original = adapter.load_framework
        adapter._CACHE["module"] = None

    def tearDown(self):
        adapter.load_framework = self._original
        adapter._CACHE["module"] = None

    def _gate(self, fake, **kw):
        adapter.load_framework = lambda: fake  # noqa: E731
        args = {
            "job_id": JOB_REF["job_id"],
            "job": dict(JOB_REF),
            "job_details": dict(BACKEND_JOB),
            "profile": dict(QUALIFIED_PROFILE),
            "cover_letter": "",
        }
        args.update(kw)
        return adapter.governance_gate(**args)

    def test_veto_hit_blocks_and_records(self):
        fake = make_fake_framework(veto_hits=["data loss"])
        gate = self._gate(fake)
        self.assertTrue(gate["enabled"])
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["reason"], "veto")
        resp = gate["response"]
        self.assertEqual(resp["status"], "governance_blocked")
        self.assertIn("data loss", resp["veto_hits"])
        self.assertEqual(len(fake.recorded), 1)
        rec = fake.recorded[0]
        self.assertEqual(rec["verdict"], "veto")
        self.assertEqual(rec["ticket"], "veto:linkedin:abc123")
        self.assertEqual(rec["domain"], "universal")

    def test_clean_check_passes_through(self):
        fake = make_fake_framework()
        gate = self._gate(fake)
        self.assertFalse(gate["blocked"])
        self.assertEqual(gate["warnings"], [])
        self.assertIsNotNone(gate["review"])
        self.assertEqual(gate["review"]["verdict"], "REVIEW_REQUIRED")

    def test_veto_check_error_fails_closed(self):
        fake = make_fake_framework(fail_veto=True)
        gate = self._gate(fake)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["reason"], "governance_error")
        self.assertEqual(gate["response"]["status"], "governance_blocked")
        self.assertEqual(len(fake.recorded), 1)  # the error was recorded

    def test_review_error_warns_not_blocks(self):
        fake = make_fake_framework(fail_review=True)
        gate = self._gate(fake)
        self.assertFalse(gate["blocked"])
        self.assertTrue(
            any("non-blocking" in w for w in gate["warnings"]),
            gate["warnings"],
        )

    def test_review_kickback_blocks(self):
        fake = make_fake_framework(review_verdict="KICK_BACK")
        gate = self._gate(fake)
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["reason"], "review_veto")

    def test_missing_framework_disables_gate(self):
        def _raise():
            raise GovernanceUnavailable("nope")

        adapter.load_framework = _raise  # noqa: E731
        gate = adapter.governance_gate(
            job_id="x", job={}, job_details={}, profile={}, cover_letter=""
        )
        self.assertEqual(gate, {"enabled": False})
        self.assertFalse(adapter.governance_enabled())

    def test_qualification_block_records_veto(self):
        fake = make_fake_framework()
        gate = self._gate(
            fake, profile=dict(UNQUALIFIED_PROFILE), job_details=dict(SENIOR_INFRA_JOB)
        )
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["reason"], "qualification")
        self.assertLess(gate["response"]["assessment"]["score"], 0.4)
        self.assertEqual(fake.recorded[0]["verdict"], "veto")

    def test_qualification_borderline_warns(self):
        fake = make_fake_framework()
        profile = {
            "full_name": "Cara Mid",
            "skills": ["Python", "SQL"],
            "years_experience": "5",
        }
        job = {
            "title": "Backend Engineer",
            "description": (
                "Backend engineer, 5 years experience. Required: Python, SQL, "
                "AWS, Docker, Kubernetes, Terraform."
            ),
            "requirements": "",
        }
        gate = self._gate(fake, profile=profile, job_details=job)
        self.assertFalse(gate["blocked"])
        assessment = gate["assessment"]
        self.assertGreaterEqual(assessment["score"], 0.4)
        self.assertLess(assessment["score"], 0.65)
        self.assertTrue(
            any(w.startswith("qualification_warning") for w in gate["warnings"]),
            gate["warnings"],
        )

    def test_qualification_unknown_does_not_block(self):
        fake = make_fake_framework()
        gate = self._gate(fake, profile=None)
        self.assertFalse(gate["blocked"])
        self.assertTrue(gate["assessment"]["qualification_unknown"])
        self.assertTrue(
            any("qualification_unknown" in w for w in gate["warnings"]),
            gate["warnings"],
        )

    def test_cover_letter_honesty_blocks(self):
        fake = make_fake_framework()
        gate = self._gate(
            fake,
            cover_letter=(
                "I bring 8 years of Kubernetes experience and deep "
                "Terraform expertise to every team."
            ),
        )
        self.assertTrue(gate["blocked"])
        self.assertEqual(gate["reason"], "cover_letter_honesty")
        hits = gate["response"]["veto_hits"]
        self.assertTrue(any("kubernetes" in h for h in hits), hits)

    def test_cover_letter_clean_passes(self):
        fake = make_fake_framework()
        gate = self._gate(
            fake,
            cover_letter=(
                "I have 5 years of experience building backends with "
                "Python, SQL and AWS."
            ),
        )
        self.assertFalse(gate["blocked"])

    def test_record_verdict_never_raises(self):
        fake = make_fake_framework(fail_record=True)
        adapter.load_framework = lambda: fake  # noqa: E731
        out = adapter.record_application_verdict("x", True, "ok")
        self.assertFalse(out["recorded"])
        self.assertIn("error", out)


class TestAssessQualification(unittest.TestCase):
    def test_qualified_profile(self):
        a = adapter.assess_qualification(dict(BACKEND_JOB), dict(QUALIFIED_PROFILE))
        self.assertTrue(a["qualified"])
        self.assertGreaterEqual(a["score"], 0.65)
        for skill in ("python", "sql", "aws", "docker"):
            self.assertIn(skill, a["matched_skills"])
        self.assertIn("kubernetes", a["missing_skills"])
        self.assertFalse(a["severe_experience_gap"])
        self.assertIn("proceed", a["guidance"])

    def test_unqualified_profile(self):
        a = adapter.assess_qualification(dict(SENIOR_INFRA_JOB), dict(UNQUALIFIED_PROFILE))
        self.assertFalse(a["qualified"])
        self.assertLess(a["score"], 0.4)
        self.assertTrue(a["severe_experience_gap"])
        self.assertEqual(a["experience_years_required"], 7.0)
        self.assertEqual(a["experience_years_user"], 1.0)

    def test_no_profile_is_unknown(self):
        a = adapter.assess_qualification(dict(BACKEND_JOB), None)
        self.assertIsNone(a["qualified"])
        self.assertTrue(a["qualification_unknown"])
        self.assertIn("wizard", a["guidance"])

    def test_no_extractable_requirements_is_borderline(self):
        a = adapter.assess_qualification(
            {"title": "Mystery Role", "description": "Come join us!", "requirements": ""},
            dict(QUALIFIED_PROFILE),
        )
        self.assertAlmostEqual(a["score"], 0.55)
        self.assertFalse(a["qualified"])  # below 0.65, but not a block

    def test_years_extraction(self):
        self.assertEqual(
            adapter._extract_years_of_experience("3+ years of experience"), 3.0
        )
        self.assertEqual(
            adapter._extract_years_of_experience("7+ years experience"), 7.0
        )
        self.assertIsNone(
            adapter._extract_years_of_experience("2 years warranty included")
        )


class TestRealFrameworkReadOnly(unittest.TestCase):
    """Exercise the real /tmp/agentic-governance clone without modifying it:
    verdicts are redirected to a temp file."""

    def test_real_check_veto_and_review(self):
        root = Path("/tmp/agentic-governance")
        if not (root / "mcp" / "adversarial_mcp" / "server.py").is_file():
            self.skipTest("reference clone not present")
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("GOVERNANCE_ROOT")
            old_log = os.environ.get("GOVERNANCE_VERDICT_LOG")
            os.environ["GOVERNANCE_ROOT"] = str(root)
            os.environ["GOVERNANCE_VERDICT_LOG"] = str(Path(tmp) / "v.jsonl")
            adapter._CACHE["module"] = None
            try:
                self.assertTrue(adapter.governance_enabled())
                mod = adapter.load_framework()
                veto = mod.check_veto(
                    domain="universal",
                    text="plan risks irrecoverable harm and data loss",
                )
                self.assertTrue(veto["veto_triggered"])
                ra = adapter.review_application(
                    dict(JOB_REF), dict(QUALIFIED_PROFILE), ""
                )
                self.assertTrue(ra["passed"])
                log_lines = Path(tmp, "v.jsonl").read_text().strip().splitlines()
                # run_review appends exactly one "review" verdict record
                self.assertEqual(len(log_lines), 1)
                import json as _json

                self.assertEqual(_json.loads(log_lines[0])["kind"], "review")
            finally:
                if old_root is None:
                    os.environ.pop("GOVERNANCE_ROOT", None)
                else:
                    os.environ["GOVERNANCE_ROOT"] = old_root
                if old_log is None:
                    os.environ.pop("GOVERNANCE_VERDICT_LOG", None)
                else:
                    os.environ["GOVERNANCE_VERDICT_LOG"] = old_log
                adapter._CACHE["module"] = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
