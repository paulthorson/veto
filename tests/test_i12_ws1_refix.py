#!/usr/bin/env python3
"""Initiative 12 / WS1 — regression tests for the 2026-09-14 re-review fixes.

Covers the two BLOCKERs and the MAJOR findings from the fresh blind
framework re-review of WS1 (tools + contracts):

* scorer return-shape contract: readers must use the REAL keys returned by
  match.score_job (score, reasons, matched, missing, veto, veto_reason,
  components) — the nonexistent factors/vetoed/veto_reasons keys must never
  come back silently;
* packaging capabilities must not trust bare module names (sentinel check);
* decoder contract accepts only the "text" convention;
* prefs carry remote_only (the key the scorer honors), and remote_ok=False
  against a remote-looking job surfaces an explicit note;
* decoder runtime failures degrade to the unavailable envelope, not a raw
  traceback;
* CLI JSON inputs are validated (null job, non-list jobs, missing file).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i12 import contracts, tools
from initiatives.i12.tools import MiniProfile

SAMPLE_JD = """
Senior Backend Engineer — Acme Corp

Salary: $140,000 - $175,000. Equity: 0.1% options vesting over 4 years.

Responsibilities:
- Design and operate payment services in Python.
- Lead incident response and mentor junior engineers.
- Collaborate with product on roadmap planning.

We value work-life balance: no on-call heroics, 40-hour weeks, flexible hours.
We are looking for 5+ years of experience building distributed systems.
"""


def _job(**kw):
    d = {"title": "Senior Backend Engineer", "company": "Acme Corp",
         "location": "Remote", "description": SAMPLE_JD}
    d.update(kw)
    return d


def _profile(**kw):
    d = dict(skills=["Python", "distributed systems", "mentoring"],
             seniority="senior", locations=["Remote"], remote_ok=True)
    d.update(kw)
    return MiniProfile(**d)


def _vetoed_job(**kw):
    """A job the real scorer vetoes (skill gap + salary floor miss)."""
    d = {"title": "Senior Backend Engineer", "company": "Acme Corp",
         "location": "Antarctica",
         "description": "Python distributed systems 5+ years required. "
                        "Salary: $140,000 - $175,000."}
    d.update(kw)
    return d


def _weak_profile():
    return MiniProfile(skills=["COBOL"], seniority="entry",
                       locations=["NYC"], remote_ok=False, min_salary=500000)


class MatchContractTest(unittest.TestCase):
    def test_expected_keys_match_real_scorer(self):
        import match
        import inspect
        src = inspect.getsource(match.score_job)
        for key in ("score", "reasons", "matched", "missing", "veto",
                    "veto_reason", "components"):
            self.assertIn(f'"{key}"', src,
                          f"pinned key {key!r} not found in match.score_job source")

    def test_check_match_result_accepts_real_shape(self):
        import match
        result = match.score_job(_job(), {"skills": ["Python"]},
                                 {"locations": ["Remote"], "remote_only": False})
        self.assertIs(contracts.check_match_result(result), result)

    def test_check_match_result_rejects_drift(self):
        with self.assertRaises(contracts.MatchContractViolation):
            contracts.check_match_result({"score": 50})

    def test_check_match_result_rejects_non_dict(self):
        with self.assertRaises(contracts.MatchContractViolation):
            contracts.check_match_result([1, 2, 3])

    def test_match_contract_version_pinned(self):
        self.assertEqual(contracts.MATCH_CONTRACT_VERSION, "1.0")
        self.assertIn("match_contract_version", contracts.contract_report())


class FitExplainerRealKeysTest(unittest.TestCase):
    def test_factors_have_nonempty_why(self):
        out = tools.fit_explainer(_job(), _profile())
        self.assertTrue(out.get("total_score") is not None)
        factors = out["factors"]
        self.assertEqual(len(factors), 5)
        # At least the skills factor must have an explanation on this JD.
        skills = next(f for f in factors if f["factor"] == "skills")
        self.assertTrue(skills["why"],
                        "skills factor explanation was empty — real keys unread")

    def test_veto_path_reachable(self):
        out = tools.fit_explainer(_vetoed_job(), _weak_profile())
        self.assertTrue(out["vetoed"], "veto path unreachable")
        self.assertTrue(out["veto_reason"])

    def test_blocker_path_fires_in_risk_check(self):
        out = tools.risk_check(_vetoed_job(), _weak_profile())
        self.assertNotEqual(out.get("available"), False)
        self.assertGreater(out["blockers"], 0,
                           "vetoed job produced no blocker finding")
        severities = [f["severity"] for f in out["findings"]]
        self.assertIn("blocker", severities)

    def test_shape_drift_degrades_loudly(self):
        bad = {"score": 1}  # missing pinned keys
        with mock.patch("initiatives.i12.tools._import_match") as imp:
            imp.return_value.score_job = lambda *a: bad
            out = tools.fit_explainer(_job(), _profile())
        self.assertFalse(out["available"])
        self.assertIn("changed shape", out["message"])


class RemotePreferenceTest(unittest.TestCase):
    def test_prefs_use_remote_only_not_remote_ok(self):
        prefs = tools._profile_prefs({"locations": ["NYC"], "remote_ok": False,
                                      "salary_min": 100000})
        self.assertIn("remote_only", prefs)
        self.assertNotIn("remote_ok", prefs)
        self.assertEqual(prefs["salary_min"], 100000)

    def test_remote_mismatch_note_fires(self):
        prof = _profile(remote_ok=False)
        out = tools.fit_explainer(_job(), prof)  # job location is Remote
        self.assertTrue(out["remote_preference_note"],
                        "remote_ok=False vs remote job produced no note")

    def test_remote_mismatch_note_absent_when_ok(self):
        out = tools.fit_explainer(_job(), _profile())
        self.assertFalse(out["remote_preference_note"])


class DecoderHardeningTest(unittest.TestCase):
    def test_runtime_exception_degrades_honestly(self):
        with mock.patch("jd_decoder.decode_jd",
                        side_effect=RuntimeError("boom")):
            out = tools.jd_demo("some text")
        self.assertFalse(out["available"])
        self.assertNotIn("Traceback", out["message"])

    def test_truncation_is_disclosed(self):
        import inspect as _inspect
        default_max = _inspect.signature(
            tools.jd_demo).parameters["max_chars"].default
        out = tools.jd_demo("x" * (default_max + 10))
        self.assertTrue(out["available"])
        self.assertTrue(out["truncated"])
        self.assertIn(str(default_max), out["truncation_note"])

    def test_decoder_contract_rejects_args_convention(self):
        import types
        fake = types.ModuleType("jd_decoder")
        exec("def decode_jd(args): return {}\n"
             "def jd_verdict(args): return {}\n"
             "def render_text(obj): return ''\n",
             fake.__dict__)
        with mock.patch.dict(sys.modules, {"jd_decoder": fake}):
            caps = {c.name: c for c in contracts.decoder_capabilities()}
        self.assertFalse(caps["decode_jd"].available)
        self.assertIn("signature drift", caps["decode_jd"].note or "")


class PackagingSentinelTest(unittest.TestCase):
    def test_bare_module_without_sentinel_not_trusted(self):
        fake = mock.MagicMock()
        fake.install = lambda: None  # like PyPI's `installer` lib
        with mock.patch.dict(sys.modules, {"installer": fake}):
            with mock.patch("importlib.import_module") as imp:
                def _side(name):
                    if name == "installer":
                        return fake
                    raise ImportError(name)
                imp.side_effect = _side
                caps = contracts.packaging_capabilities()
        self.assertTrue(all(not c.available for c in caps),
                        "bare module without sentinel was trusted")
        self.assertTrue(all("not yet shipped" in c.provider for c in caps))

    def test_sentinel_module_trusted(self):
        fake = mock.MagicMock()
        fake.VETO_PACKAGING_CONTRACT = contracts.PACKAGING_CONTRACT_VERSION
        fake.install = lambda: None
        fake.install_status = lambda: {}
        with mock.patch("importlib.import_module", return_value=fake):
            caps = contracts.packaging_capabilities()
        by_name = {c.name: c for c in caps}
        self.assertTrue(by_name["install"].available)


class CliJsonValidationTest(unittest.TestCase):
    def _write(self, content: str) -> str:
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        fh.write(content)
        fh.close()
        return fh.name

    def _run_cmd_tools(self, demo, **flags):
        from initiatives.i12 import __main__ as m
        args = mock.Mock(demo=demo, text="", skills="",
                         job_json=flags.get("job_json", ""),
                         jobs_json=flags.get("jobs_json", ""),
                         json=True)
        with mock.patch("builtins.print"):
            return m.cmd_tools(args)

    def test_null_job_json_rejected(self):
        path = self._write("null")
        try:
            self.assertEqual(
                self._run_cmd_tools("fit", job_json=path), 2)
        finally:
            Path(path).unlink()

    def test_non_list_jobs_json_rejected(self):
        path = self._write('{"not": "a list"}')
        try:
            self.assertEqual(
                self._run_cmd_tools("compare", jobs_json=path), 2)
        finally:
            Path(path).unlink()

    def test_missing_file_rejected(self):
        self.assertEqual(
            self._run_cmd_tools("fit", job_json="/no/such/file.json"), 2)

    def test_invalid_json_rejected(self):
        path = self._write("{not json")
        try:
            self.assertEqual(
                self._run_cmd_tools("risk", job_json=path), 2)
        finally:
            Path(path).unlink()

    def test_non_dict_job_entry_degrades_in_compare(self):
        out = tools.role_compare([1, "x", None], _profile())
        self.assertTrue(all(r["readiness"] == "invalid input"
                            for r in out["rows"]))

    def test_methodology_gate_blocks_post_review_tbd(self):
        import initiatives.i12 as pkg
        with mock.patch.object(tools, "METHODOLOGY_URL",
                               "TBD — placeholder not yet set"):
            with mock.patch.object(pkg, "__status__", "shipped", create=True):
                with self.assertRaises(RuntimeError):
                    tools._methodology_url()
            with mock.patch.object(pkg, "__status__", "built-pending-review",
                                   create=True):
                self.assertTrue(tools._methodology_url().startswith("TBD"))


if __name__ == "__main__":
    unittest.main()
