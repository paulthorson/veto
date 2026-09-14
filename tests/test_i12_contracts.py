#!/usr/bin/env python3
"""Initiative 12 — contract tests: Initiative 04 decoder + Initiative 10 packaging."""

import unittest

from initiatives.i12 import contracts


class DecoderContractTest(unittest.TestCase):
    def test_decoder_capabilities_resolve(self):
        caps = {c.name: c for c in contracts.decoder_capabilities()}
        self.assertIn("decode_jd", caps)
        self.assertIn("jd_verdict", caps)
        for c in caps.values():
            self.assertEqual(c.contract_version, contracts.DECODER_CONTRACT_VERSION)
            self.assertEqual(c.provider, "jd_decoder")

    def test_decoder_functions_callable_with_text_first_param(self):
        import jd_decoder
        import inspect
        for fname in ("decode_jd", "jd_verdict"):
            fn = getattr(jd_decoder, fname)
            params = list(inspect.signature(fn).parameters)
            self.assertTrue(params and params[0] == "text",
                            f"{fname} signature drifted: {params}")

    def test_contract_report_shape(self):
        rep = contracts.contract_report()
        self.assertIn("decoder", rep)
        self.assertIn("packaging", rep)
        self.assertEqual(rep["decoder_contract_version"], "1.0")


class PackagingContractTest(unittest.TestCase):
    def test_packaging_currently_unavailable(self):
        caps = contracts.packaging_capabilities()
        names = [c.name for c in caps]
        for expected, _ in contracts.PACKAGING_EXPECTED_API:
            self.assertIn(expected, names)
        # Honest answer today: nothing available.
        self.assertTrue(all(not c.available for c in caps))

    def test_require_raises_never_fakes(self):
        with self.assertRaises(contracts.PackagingUnavailable):
            contracts.require_packaging_capability("install")

    def test_install_status_is_honest(self):
        st = contracts.install_path_status()
        self.assertFalse(st["available"])
        self.assertIn("not released yet", st["message"])
        self.assertIn("README", st["message"])


if __name__ == "__main__":
    unittest.main()
