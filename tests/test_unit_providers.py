"""Unit tests for job-board provider parsers (no network).

Feeds realistic fixtures (tests/fixtures.py) into each provider's parser
and asserts the canonical job-dict shape plus job-ID round-tripping.
Providers that are not implemented yet are skipped with a clear reason.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import server  # noqa: E402
from tests.fixtures import (  # noqa: E402
    ASHBY_POSTING,
    GREENHOUSE_JOB,
    LEVER_POSTING,
    REQUIRED_JOB_KEYS,
)


def _find_parser(board: str):
    """Locate a provider's job-normalising callable, or return None.

    Supports a module-level ``parse_job``/``parse_posting``/``to_job``
    function or a ``*Provider`` class exposing ``parse_job``.
    """
    try:
        mod = importlib.import_module(f"providers.{board}")
    except ImportError:
        return None
    for name in ("parse_job", "parse_posting", "posting_to_job", "to_job"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    for attr in dir(mod):
        if attr.lower().endswith("provider"):
            cls = getattr(mod, attr)
            fn = getattr(cls, "parse_job", None) or getattr(cls, "parse_posting", None)
            if callable(fn):
                return fn() if isinstance(fn, staticmethod) else fn
    return None


def _assert_job_shape(testcase: unittest.TestCase, job: dict, board: str) -> None:
    testcase.assertIsInstance(job, dict)
    missing = REQUIRED_JOB_KEYS - set(job.keys())
    testcase.assertFalse(missing, f"{board}: missing keys {missing}")
    testcase.assertEqual(job["board"], board)
    testcase.assertTrue(job["id"], f"{board}: empty id")
    testcase.assertTrue(job["url"].startswith("http"), f"{board}: bad url")
    # Job-ID round trip: encode -> decode routes back to the same board+url.
    encoded = server._encode_job_id(board, job["url"])
    decoded_board, decoded_url = server._decode_job_id(encoded)
    testcase.assertEqual(decoded_board, board)
    testcase.assertEqual(decoded_url, job["url"])


class TestGreenhouseParser(unittest.TestCase):
    def test_parse(self):
        parse = _find_parser("greenhouse")
        if parse is None:
            self.skipTest("providers/greenhouse.py not implemented yet")
        _assert_job_shape(self, parse(GREENHOUSE_JOB), "greenhouse")


class TestLeverParser(unittest.TestCase):
    def test_parse(self):
        parse = _find_parser("lever")
        if parse is None:
            self.skipTest("providers/lever.py not implemented yet")
        _assert_job_shape(self, parse(LEVER_POSTING), "lever")


class TestAshbyParser(unittest.TestCase):
    def test_parse(self):
        parse = _find_parser("ashby")
        if parse is None:
            self.skipTest("providers/ashby.py not implemented yet")
        _assert_job_shape(self, parse(ASHBY_POSTING), "ashby")


class TestJobIdCodec(unittest.TestCase):
    def test_round_trip(self):
        for board, url in [
            ("linkedin", "https://www.linkedin.com/jobs/view/x-1234567890"),
            ("greenhouse", "https://boards.greenhouse.io/stripe/jobs/5432109"),
        ]:
            encoded = server._encode_job_id(board, url)
            self.assertTrue(encoded.startswith(board + ":"))
            self.assertNotIn("=", encoded)  # padding stripped
            b2, u2 = server._decode_job_id(encoded)
            self.assertEqual((b2, u2), (board, url))

    def test_malformed_raises(self):
        for bad in ["", "nocolon", ":token", "board:"]:
            with self.assertRaises(ValueError, msg=bad):
                server._decode_job_id(bad)


if __name__ == "__main__":
    unittest.main()
