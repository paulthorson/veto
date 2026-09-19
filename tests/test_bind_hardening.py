#!/usr/bin/env python3
"""Commit 10 (spec section 8.1): localhost-default bind, bearer-token gate.

The spec's ``server.py:635-647`` uvicorn reference is stale (re-derived in
commit 1): there is no uvicorn anywhere in the tracked tree and ``server.py``
ends at stdio ``mcp.run()``. The only network-bind surface is ``webui.py``:

  - ``serve()`` binds the socket (:1871), default host ``"127.0.0.1"``
  - ``resolve_bind_host()`` (:1680) maps the explicit ``--host lan`` flag to
    the machine's LAN IP; ``0.0.0.0`` and explicit IPs pass through unchanged
  - ``cmd_serve()`` (:1920) defaults the host to ``"127.0.0.1"`` when the
    flag is absent
  - both CLI entry points default ``--host`` to ``127.0.0.1``
    (``webui._main`` :1989, ``cli.py serve`` :582)

Legal posture encoded here: the web UI is a localhost-only tool unless the
user BOTH passes an explicit ``--host`` flag AND holds the bearer token at
``~/.veto_webui_token`` (0600). Every ``/api/*`` route is gated by
``_Handler._require_auth`` (:1748) before routing — fail closed, never served
open. If a token file does not exist when a LAN bind is requested, the
server generates the token first and prints it once (``cmd_serve`` :1942-1955);
it never starts with the API unprotected. Removing one of these tests
changes that posture, not merely the behavior.

Convention: stdlib unittest only. Real sockets; ephemeral ports.

Run:  cd ~/workspace/veto && python3 -m pytest tests/test_bind_hardening.py
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
import unittest

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import webui  # noqa: E402


def _temp_token_dir(test):
    """Context manager: point VETO_WEBUI_TOKEN_FILE at a fresh temp dir."""
    @contextlib.contextmanager
    def _cm():
        d = tempfile.TemporaryDirectory()
        test.addCleanup(d.cleanup)
        path = Path(d.name) / "token"
        old = os.environ.get("VETO_WEBUI_TOKEN_FILE")
        os.environ["VETO_WEBUI_TOKEN_FILE"] = str(path)

        def restore():
            if old is None:
                os.environ.pop("VETO_WEBUI_TOKEN_FILE", None)
            else:
                os.environ["VETO_WEBUI_TOKEN_FILE"] = old

        test.addCleanup(restore)
        yield path
    return _cm()


def _get(base, path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(base + path, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class _CmdServeHarness:
    """Drive ``webui.cmd_serve`` with the real socket layer mocked out.

    ``run()`` returns ``(rc, stdout, bound_addr_tuple, server_instance,
    token_path)``. stdout is captured, never printed."""

    def __init__(self, test, lan_ip=None):
        self.test = test
        self.lan_ip = lan_ip

    def run(self, pre=None, **ns_kwargs):
        """Drive one ``cmd_serve`` boot.

        ``pre(token_path)`` runs before the boot (e.g. to plant a token
        file). Returns ``(rc, stdout, bound_addr_tuple, server_instance,
        token_path)``."""
        ns = argparse.Namespace(port=0, regenerate_token=False,
                                show_token=False, no_browser=True,
                                **ns_kwargs)
        patches = [mock.patch.object(webui, "_Server")]
        if self.lan_ip is not None:
            patches.append(mock.patch.object(webui, "resolve_lan_ip",
                                             return_value=self.lan_ip))
        with _temp_token_dir(self.test) as token_path, \
                contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(p) for p in patches]
            mock_cls = mocks[0]
            inst = mock_cls.return_value
            inst.serve_forever.side_effect = KeyboardInterrupt
            # serve() reads server_address[1] for the printed port.
            inst.server_address = ("?", 0)
            if pre is not None:
                pre(token_path)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = webui.cmd_serve(ns)
            bind_addr = mock_cls.call_args[0][0]
            return rc, buf.getvalue(), bind_addr, inst, token_path


class BindDefaultTests(unittest.TestCase):
    """The default bind is 127.0.0.1: no flag, no LAN exposure."""

    def test_serve_signature_default_host_is_loopback(self):
        host_param = inspect.signature(webui.serve).parameters["host"]
        self.assertEqual(host_param.default, "127.0.0.1",
                         "serve()'s default host drifted off loopback")

    def test_webui_main_argparse_default_host_is_loopback(self):
        captured = {}

        def fake_cmd_serve(args):
            captured["args"] = args
            return 0

        with mock.patch.object(webui, "cmd_serve",
                               side_effect=fake_cmd_serve):
            rc = webui._main([])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["args"].host, "127.0.0.1",
                         "webui --host default is no longer loopback")

    def test_cmd_serve_binds_loopback_when_host_flag_absent(self):
        # Namespace without a ``host`` attribute mirrors a CLI invocation
        # that never passed --host (cmd_serve falls back to 127.0.0.1).
        rc, out, bind_addr, inst, _ = _CmdServeHarness(self).run()
        self.assertEqual(rc, 0)
        self.assertEqual(bind_addr[0], "127.0.0.1",
                         f"no-flag boot bound {bind_addr[0]!r}, not loopback")
        self.assertNotIn("LAN URL", out)

    def test_real_socket_binds_loopback_only(self):
        """A real ``serve(0)`` must not listen on any LAN interface."""
        server = webui.serve(0, host="127.0.0.1", token="test-token-x")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertEqual(server.server_address[0], "127.0.0.1")
            # The kernel-recorded local address of the listening socket is
            # authoritative: a socket bound to 127.0.0.1 cannot accept
            # connections arriving on any other interface, even when the
            # machine has LAN interfaces.
            bound_ip, bound_port = server.socket.getsockname()[:2]
            self.assertEqual(bound_ip, "127.0.0.1")
            self.assertEqual(bound_port, port)
            # Sanity: loopback reaches the server.
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            sock.sendall(b"GET /api/kpis HTTP/1.0\r\n"
                         b"Authorization: Bearer test-token-x\r\n\r\n")
            sock.recv(64)
            sock.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)

    def test_cli_serve_help_advertises_loopback_default(self):
        pythons = [sys.executable]
        venv = BASE_DIR / ".venv" / "bin" / "python"
        if venv.exists():
            pythons.append(str(venv))
        proc = None
        for exe in pythons:
            probe = subprocess.run([exe, "-c", "import mcp"],
                                   capture_output=True, timeout=30)
            if probe.returncode == 0:
                proc = subprocess.run(
                    [exe, "cli.py", "serve", "--help"], cwd=BASE_DIR,
                    capture_output=True, text=True, timeout=60)
                break
        if proc is None:
            self.skipTest("no python with the mcp package for cli.py")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("127.0.0.1 (default)", proc.stdout)


class ExplicitFlagTests(unittest.TestCase):
    """Off-loopback binds happen only through an explicit --host flag."""

    def test_lan_flag_is_explicit_only(self):
        # The bare string "lan" is the flag value; resolve_bind_host maps it
        # to the machine's LAN IP, every other value passes through.
        lan_ip = webui.resolve_bind_host("lan")
        self.assertNotEqual(lan_ip, "127.0.0.1")
        self.assertEqual(webui.resolve_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(webui.resolve_bind_host("192.168.1.9"),
                         "192.168.1.9")
        self.assertEqual(webui.resolve_bind_host("0.0.0.0"), "0.0.0.0")

    def test_webui_main_passes_explicit_host_values_through(self):
        captured = {}

        def fake_cmd_serve(args):
            captured["args"] = args
            return 0

        with mock.patch.object(webui, "cmd_serve",
                               side_effect=fake_cmd_serve):
            webui._main(["--host", "lan"])
        self.assertEqual(captured["args"].host, "lan")
        with mock.patch.object(webui, "cmd_serve",
                               side_effect=fake_cmd_serve):
            webui._main(["--host", "192.168.1.9"])
        self.assertEqual(captured["args"].host, "192.168.1.9")

    def test_host_lan_binds_detected_lan_ip(self):
        rc, out, bind_addr, inst, _ = _CmdServeHarness(
            self, lan_ip="192.168.99.7").run(host="lan")
        self.assertEqual(rc, 0)
        self.assertEqual(bind_addr[0], "192.168.99.7")
        self.assertIn("LAN URL", out)
        lowered = out.lower()
        self.assertIn("cleartext", lowered)
        self.assertIn("trusted lan", lowered)

    def test_host_lan_resolution_failure_exits_nonzero(self):
        args = argparse.Namespace(host="lan", port=8765,
                                  regenerate_token=False, show_token=False,
                                  no_browser=True)
        err = io.StringIO()
        with mock.patch.object(webui, "resolve_lan_ip",
                               side_effect=OSError("no route to host")):
            with contextlib.redirect_stderr(err):
                rc = webui.cmd_serve(args)
        self.assertEqual(rc, 2)
        self.assertIn("lan", err.getvalue().lower())


class TokenGateLanTests(unittest.TestCase):
    """--host lan with no token file: generate the token first, then start.

    The server never boots with the API unprotected; the freshly generated
    token is printed exactly once (on creation) and the file lands at
    ~/.veto_webui_token mode 0600."""

    def test_lan_boot_without_token_file_generates_token_then_starts(self):
        seen = {}

        def _record(path):
            seen["exists_before"] = path.exists()

        rc, out, bind_addr, inst, token_path = _CmdServeHarness(
            self, lan_ip="192.168.99.7").run(pre=_record, host="lan")
        self.assertFalse(seen["exists_before"],
                         "test setup error: token file already existed")
        self.assertEqual(rc, 0)
        self.assertTrue(token_path.exists(), "token file was not created")
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        file_token = token_path.read_text(encoding="utf-8").strip()
        self.assertTrue(file_token, "token file is empty")
        self.assertEqual(inst.auth_token, file_token,
                         "server did not install the generated token")
        self.assertEqual(bind_addr[0], "192.168.99.7")
        # Token printed once on creation; never echoed on later boots.
        self.assertIn(file_token, out)
        rc2, out2, _, _, _ = _CmdServeHarness(
            self, lan_ip="192.168.99.7").run(
                pre=lambda p: p.write_text(file_token + "\n",
                                           encoding="utf-8"), host="lan")
        self.assertEqual(rc2, 0)
        self.assertNotIn(file_token, out2,
                         "existing token must not be reprinted on boot")

    def test_token_file_loose_perms_tightened_before_serving(self):
        def _make_loose(path):
            path.write_text("existing-token\n", encoding="utf-8")
            os.chmod(path, 0o644)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out, bind_addr, inst, token_path = _CmdServeHarness(
                self).run(pre=_make_loose)
        self.assertEqual(rc, 0)
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        self.assertIn("tightened to 0600", err.getvalue())
        self.assertEqual(inst.auth_token, "existing-token")


class ApiGatePrecedesRoutingTests(unittest.TestCase):
    """Every /api/* route requires the bearer token — including paths that
    don't exist, proving the gate runs before routing (webui.py:1781/1828).
    The static web UI stays open without a token (it is not an API route)."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.token_path = Path(d.name) / "token"
        old = os.environ.get("VETO_WEBUI_TOKEN_FILE")
        os.environ["VETO_WEBUI_TOKEN_FILE"] = str(self.token_path)

        def restore():
            if old is None:
                os.environ.pop("VETO_WEBUI_TOKEN_FILE", None)
            else:
                os.environ["VETO_WEBUI_TOKEN_FILE"] = old

        self.addCleanup(restore)
        self.token = webui.load_or_create_token()
        self.server = webui.serve(0, host="127.0.0.1", token=self.token)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    def test_unknown_api_path_401s_without_token(self):
        status, body = _get(self.base, "/api/definitely-not-a-route")
        self.assertEqual(status, 401)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "unauthorized")

    def test_all_known_api_routes_require_auth(self):
        for path in ("/api/tools", "/api/kpis", "/api/run"):
            with self.subTest(path=path):
                status, _ = _get(self.base, path)
                self.assertEqual(status, 401,
                                 f"{path} served without a token")
                status, _ = _get(self.base, path, token="wrong-token")
                self.assertEqual(status, 401,
                                 f"{path} accepted a wrong token")

    def test_post_run_requires_auth(self):
        req = urllib.request.Request(
            self.base + "/api/run",
            data=json.dumps({"tool": "x", "action": "y",
                             "params": {}}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
        else:
            self.fail("POST /api/run accepted an unauthenticated request")

    def test_correct_token_reaches_all_api_routes(self):
        for path in ("/api/tools", "/api/kpis"):
            with self.subTest(path=path):
                status, body = _get(self.base, path, token=self.token)
                self.assertEqual(status, 200)
                json.loads(body)  # valid JSON

    def test_static_index_stays_open(self):
        # The static dashboard is not an API route; it intentionally needs
        # no token (the browser login gate handles the human side).
        status, _ = _get(self.base, "/")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
