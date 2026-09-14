"""Request hygiene tests (legal-hardening commit 9, spec section 7).

* robots.txt fails CLOSED: if it cannot be fetched or parsed, the target
  URL is treated as disallowed and must never be requested (spec 7.2).
* 403/429 stop a provider and start a cooldown via compliance.record_block
  (cooldown constants at compliance.py 94-96, handling at 315-335); there
  is no retry-through path.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import compliance  # noqa: E402
from providers import _common  # noqa: E402


class _FakeResp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Minimal stand-in for httpx.Client used as a context manager."""

    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        assert url.endswith("/robots.txt"), url
        if self._exc is not None:
            raise self._exc
        return self._resp


def _robots_allows(resp=None, exc=None):
    _common.clear_robots_cache()
    fake = _FakeClient(resp=resp, exc=exc)
    with mock.patch.object(_common, "make_client", return_value=fake):
        return _common.robots_allows("user", "https://example.com/jobs/1")


class FailClosedRobotsTests(unittest.TestCase):
    def test_fetch_failure_is_disallowed(self):
        self.assertFalse(_robots_allows(exc=ConnectionError("no network")))

    def test_missing_robots_txt_is_disallowed(self):
        self.assertFalse(_robots_allows(resp=_FakeResp(404, "")))

    def test_empty_robots_body_is_disallowed(self):
        self.assertFalse(_robots_allows(resp=_FakeResp(200, "")))

    def test_parse_failure_is_disallowed(self):
        with mock.patch.object(
            _common.urllib.robotparser, "RobotFileParser",
            side_effect=ValueError("bad robots"),
        ):
            self.assertFalse(
                _robots_allows(resp=_FakeResp(200, "User-agent: *\nDisallow: /"))
            )

    def test_disallow_still_disallows(self):
        self.assertFalse(
            _robots_allows(resp=_FakeResp(200, "User-agent: *\nDisallow: /"))
        )

    def test_explicit_allow_still_allows(self):
        self.assertTrue(
            _robots_allows(resp=_FakeResp(200, "User-agent: *\nDisallow:"))
        )


class BlockCooldownTests(unittest.TestCase):
    def setUp(self):
        # record_block/record_block_cleared persist via save_state: point
        # the real compliance.json at a temp file so these tests can
        # never poison the working tree (or a later test in the suite).
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(
            compliance,
            "COMPLIANCE_FILE",
            Path(self._tmp.name) / "compliance.json",
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def _fresh_state(self):
        return compliance._default_state()

    def test_block_trips_cooldown_and_stops_provider(self):
        state = self._fresh_state()
        cooldown = compliance.record_block("greenhouse", state=state)
        self.assertEqual(cooldown, compliance.BLOCK_COOLDOWN_BASE_SECONDS)
        allowed, reason = compliance.check_search_allowed(
            "greenhouse", state=state
        )
        self.assertFalse(allowed)
        self.assertIn("cooling down", reason)

    def test_cooldown_doubles_on_repeat_blocks(self):
        # Consecutive blocks with no intervening clear keep escalating.
        state = self._fresh_state()
        first = compliance.record_block("lever", state=state)
        second = compliance.record_block("lever", state=state)
        self.assertEqual(first, compliance.BLOCK_COOLDOWN_BASE_SECONDS)
        self.assertEqual(second, first * 2)

    def test_cooldown_is_capped(self):
        state = self._fresh_state()
        cooldown = 0
        for _ in range(20):
            cooldown = compliance.record_block("ashby", state=state)
        self.assertEqual(cooldown, compliance.BLOCK_COOLDOWN_MAX_SECONDS)

    def test_cleared_block_resets_cooldown_to_base(self):
        state = self._fresh_state()
        compliance.record_block("adzuna", state=state)
        compliance.record_block_cleared("adzuna", state=state)
        cooldown = compliance.record_block("adzuna", state=state)
        self.assertEqual(cooldown, compliance.BLOCK_COOLDOWN_BASE_SECONDS)


if __name__ == "__main__":
    unittest.main()
