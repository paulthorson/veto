#!/usr/bin/env python3
"""Tests for the phone-access wizard section of wizard.py (--phone).

Covers ``_lan_ip``, the ``phone_access`` choice-validation loop, and both
``_phone_tailscale`` exit-code branches (exit 1 whenever setup is
incomplete, so scripted callers can detect unfinished setup).

Convention mirrors tests/test_dashboard_wizards.py: stdlib unittest only,
module boundaries mocked so no test touches the network, a real Tailscale
binary, or stdin.

Run:  cd ~/workspace/job-apply-mcp && .venv/bin/python -m pytest tests/test_wizard_phone.py
"""

from __future__ import annotations

import contextlib
import io
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import wizard  # noqa: E402

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _run(fn, *args):
    """Run a function, capturing stdout; return (returncode, printed text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------------------
# _lan_ip
# ---------------------------------------------------------------------------


class LanIpTests(unittest.TestCase):
    def test_lan_ip_never_raises_and_is_non_loopback_ipv4(self):
        ip = wizard._lan_ip()
        if ip is not None:
            self._assert_good(ip)

    def _assert_good(self, ip):
        self.assertIsInstance(ip, str)
        self.assertRegex(ip, _IPV4_RE)
        self.assertFalse(
            ip.startswith("127."),
            f"_lan_ip() must not return a loopback address, got {ip!r}",
        )


# ---------------------------------------------------------------------------
# phone_access choice validation
# ---------------------------------------------------------------------------


class PhoneAccessChoiceTests(unittest.TestCase):
    def test_invalid_then_valid_choice_reruns_validation(self):
        """Invalid input loops; the first valid pick dispatches correctly."""
        with mock.patch.object(
            wizard, "ask", side_effect=["bogus", "2"]
        ) as ask_mock, mock.patch.object(
            wizard, "_phone_tailscale", return_value=0
        ) as tailscale_mock:
            rc, out = _run(wizard.phone_access)

        self.assertEqual(rc, 0)
        self.assertEqual(ask_mock.call_count, 2)
        self.assertIn("Please choose one of", out)
        tailscale_mock.assert_called_once_with()

    def test_first_valid_choice_dispatches_without_looping(self):
        with mock.patch.object(
            wizard, "ask", return_value="1"
        ) as ask_mock, mock.patch.object(
            wizard, "_phone_wifi", return_value=0
        ) as wifi_mock:
            rc, out = _run(wizard.phone_access)

        self.assertEqual(rc, 0)
        self.assertEqual(ask_mock.call_count, 1)
        self.assertNotIn("Please choose one of", out)
        wifi_mock.assert_called_once_with()


# ---------------------------------------------------------------------------
# _phone_tailscale exit codes
# ---------------------------------------------------------------------------


class PhoneTailscaleTests(unittest.TestCase):
    def _run_result(self, returncode, stdout=""):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_not_installed_returns_1_and_prints_guidance(self):
        with mock.patch("shutil.which", return_value=None):
            rc, out = _run(wizard._phone_tailscale)
        self.assertEqual(
            rc, 1, "incomplete setup (tailscale not installed) must be exit 1"
        )
        self.assertIn("isn't installed", out)
        self.assertIn("wizard.py --phone", out)

    def test_installed_but_not_connected_returns_1(self):
        with mock.patch("shutil.which", return_value="/usr/bin/tailscale"), \
                mock.patch(
                    "subprocess.run",
                    return_value=self._run_result(returncode=1, stdout=""),
                ):
            rc, out = _run(wizard._phone_tailscale)
        self.assertEqual(
            rc, 1, "incomplete setup (tailscale not connected) must be exit 1"
        )
        self.assertIn("tailscale up", out)

    def test_connected_returns_0_and_shows_tailnet_url(self):
        with mock.patch("shutil.which", return_value="/usr/bin/tailscale"), \
                mock.patch(
                    "subprocess.run",
                    return_value=self._run_result(
                        returncode=0, stdout="100.64.0.1\n"
                    ),
                ):
            rc, out = _run(wizard._phone_tailscale)
        self.assertEqual(rc, 0)
        self.assertIn(f"http://100.64.0.1:{wizard.WEBUI_PORT}", out)


if __name__ == "__main__":
    unittest.main()
