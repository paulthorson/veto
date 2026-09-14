#!/usr/bin/env python3
"""Initiative 12 / WS1 fixer tests — blind-review must-fix findings.

Proves: jd_demo degrades honestly on decoder return-shape drift (never
KeyError); fit_explainer/risk_check/role_compare degrade honestly when
:mod:`match` is missing (never a traceback); profile/prefs have a single
source of truth; render_text exists and is used; role_compare's truncated
flag is exact; decoder_capabilities()' ImportError path is shape-consistent.
"""

import argparse
import sys
import unittest
from unittest import mock

from initiatives.i12 import contracts, tools
from initiatives.i12.tools import MiniProfile

SAMPLE_JD = "Senior Backend Engineer. Salary: $140,000 - $175,000."


def _job(**kw):
    d = {"title": "Senior Backend Engineer", "company": "Acme Corp",
         "location": "Remote", "description": SAMPLE_JD}
    d.update(kw)
    return d


def _profile():
    return MiniProfile(skills=["Python"], seniority="senior",
                       locations=["Remote"], remote_ok=True,
                       min_salary=120000.0)


class JdDemoShapeDriftTest(unittest.TestCase):
    @staticmethod
    def _with_decoder(decode_ret, verdict_ret):
        """Patch the decoder with real `text`-signature functions.

        MagicMock stand-ins are rejected by the decoder contract's
        signature check (their params are (*args, **kwargs), not `text`),
        so shape-drift tests must use real functions.
        """
        import jd_decoder

        def decode_jd(text):
            return decode_ret

        def jd_verdict(text):
            return verdict_ret

        return (mock.patch.object(jd_decoder, "decode_jd", decode_jd),
                mock.patch.object(jd_decoder, "jd_verdict", jd_verdict))

    def test_empty_decoder_shape_never_keyerrors(self):
        dec, ver = self._with_decoder(
            {}, {"verdict": "caution", "reasons": ["thin JD"]})
        with dec, ver:
            out = tools.jd_demo(SAMPLE_JD)  # must not raise
        self.assertTrue(out["available"])
        self.assertIsNone(out["transparency_score"])
        self.assertIsNone(out["salary_range"])
        self.assertEqual(out["overwork_flags"], [])
        self.assertEqual(out["growth_signals"], [])
        self.assertEqual(out["green_flags"], [])
        self.assertEqual(out["top_reasons"], ["thin JD"])

    def test_mistyped_sections_degrade_to_empty(self):
        drifted = {"transparency": ["not", "a", "dict"],
                   "overwork": {"phrase": "not-a-list"},
                   "vagueness": None, "growth": None,
                   "green_flags": None, "markdown": "md"}
        dec, ver = self._with_decoder(
            drifted, {"verdict": "mixed", "reasons": []})
        with dec, ver:
            out = tools.jd_demo(SAMPLE_JD)
        self.assertTrue(out["available"])
        self.assertEqual(out["overwork_flags"], [])

    def test_non_dict_decoder_result_is_unavailable_not_crash(self):
        dec, ver = self._with_decoder(None, {})
        with dec, ver:
            out = tools.jd_demo(SAMPLE_JD)
        self.assertFalse(out["available"])
        self.assertIn("unexpected", out["message"])


class MatchMissingDegradationTest(unittest.TestCase):
    def _no_match(self):
        # None in sys.modules makes `import match` raise ImportError.
        return mock.patch.dict(sys.modules, {"match": None})

    def test_fit_explainer_degrades_without_match(self):
        with self._no_match():
            out = tools.fit_explainer(_job(), _profile())  # must not raise
        self.assertFalse(out["available"])
        self.assertEqual(out["tool"], "fit_explainer")
        self.assertIn("message", out)

    def test_risk_check_degrades_without_match(self):
        with self._no_match():
            out = tools.risk_check(_job(), _profile())
        self.assertFalse(out["available"])
        self.assertEqual(out["tool"], "risk_check")

    def test_role_compare_degrades_without_match(self):
        with self._no_match():
            out = tools.role_compare([_job()], _profile())
        self.assertFalse(out["available"])
        self.assertEqual(out["tool"], "role_compare")

    def test_envelope_carries_methodology_and_limitations(self):
        with self._no_match():
            out = tools.fit_explainer(_job(), _profile())
        self.assertIn("methodology", out)
        self.assertIn("limitations", out)
        self.assertFalse(out["submits_anything"])


class ProfilePrefsSingleSourceTest(unittest.TestCase):
    def test_prefs_derived_from_canonical_profile_dict(self):
        prof = tools._mini_profile_dict(_profile())
        prefs = tools._profile_prefs(prof)
        # The scorer honors `remote_only` (remote roles only); the demo
        # profile cannot express "remote only", so it is always False here.
        # `remote_ok` must NOT leak into prefs (it is not a scorer key).
        self.assertEqual(prefs,
                         {"locations": ["Remote"], "remote_only": False,
                          "salary_min": 120000.0})
        # No caller-built second literal survives: prefs keys are a subset
        # view of the canonical dict, salary_min included only when set.
        p2 = MiniProfile(skills=["Go"])
        prefs2 = tools._profile_prefs(tools._mini_profile_dict(p2))
        self.assertNotIn("salary_min", prefs2)
        self.assertEqual(prefs2["locations"], [])
        self.assertFalse(prefs2["remote_only"])


class RenderTextTest(unittest.TestCase):
    def test_render_text_implements_docstring_promise(self):
        out = tools.jd_demo(SAMPLE_JD)
        text = tools.render_text(out)
        self.assertIn("jd_decoder (demo)", text)
        self.assertIn("methodology:", text)
        # Lists render as indented lines, not a JSON dump.
        self.assertIn("  - ", text)
        self.assertNotIn("{", text.splitlines()[0])


class RoleCompareTruncatedTest(unittest.TestCase):
    def test_truncated_false_at_exactly_max(self):
        jobs = [_job(title=f"R{i}") for i in range(tools.MAX_COMPARE_JOBS)]
        out = tools.role_compare(jobs, _profile())
        self.assertEqual(out["compared"], tools.MAX_COMPARE_JOBS)
        self.assertFalse(out["truncated"])

    def test_truncated_true_only_when_input_exceeded_max(self):
        jobs = [_job(title=f"R{i}") for i in range(tools.MAX_COMPARE_JOBS + 2)]
        out = tools.role_compare(jobs, _profile())
        self.assertEqual(out["compared"], tools.MAX_COMPARE_JOBS)
        self.assertTrue(out["truncated"])


class DecoderCapabilitiesShapeTest(unittest.TestCase):
    def test_import_error_path_returns_consistent_shape(self):
        with mock.patch("importlib.import_module",
                        side_effect=ImportError("no jd_decoder here")):
            caps = contracts.decoder_capabilities()
        by_name = {c.name: c for c in caps}
        # Same two records as the success path — callers iterate blindly.
        self.assertEqual(set(by_name), {"decode_jd", "jd_verdict"})
        for c in caps:
            self.assertFalse(c.available)
            self.assertEqual(c.provider, "jd_decoder")
            self.assertEqual(c.contract_version,
                             contracts.DECODER_CONTRACT_VERSION)
            self.assertIn("import failed", c.note)


class PackageCliHardeningTest(unittest.TestCase):
    def test_lazy_import_degrades_honestly(self):
        from initiatives.i12 import __main__ as i12main
        mod, reason = i12main._lazy_i12_module("no_such_module_xyz")
        self.assertIsNone(mod)
        self.assertIn("not available", reason)

    def test_broken_module_degrades_without_traceback(self):
        from initiatives.i12 import __main__ as i12main
        args = argparse.Namespace(kind="score-card", role="R", company="C",
                                  score=1.0, verdict="v", reasons="a|b",
                                  factors_json="[]", sessions=1, weeks=1,
                                  outcomes=1, markdown=False)
        with mock.patch.object(i12main.importlib, "import_module",
                               side_effect=ImportError("broken")):
            rc = i12main.cmd_share(args)  # must not raise
        self.assertEqual(rc, 1)

    def test_all_five_subcommands_help_cleanly(self):
        from initiatives.i12 import __main__ as i12main
        parser = i12main.build_parser()
        for cmd in ("tools", "share", "changelog", "onboarding", "telemetry"):
            with self.assertRaises(SystemExit) as cm:
                parser.parse_args([cmd, "--help"])
            self.assertEqual(cm.exception.code, 0, cmd)


if __name__ == "__main__":
    unittest.main()
