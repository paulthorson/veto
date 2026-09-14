#!/usr/bin/env python3
"""Tests for webui.py token auth, LAN binding, and the serve CLI flags.

Starts the real ``webui._Server`` on 127.0.0.1 with an ephemeral port and a
temp token file (via the ``VETO_WEBUI_TOKEN_FILE`` env override), then
exercises the API over HTTP with urllib. The confirm-gating of destructive
actions is unchanged and stays covered by tests/test_dashboard.py.

The token value is never printed: assertions compare it in memory, and the
two tests that must observe printed output capture it into buffers.

Convention: stdlib unittest only.

Run:  cd ~/workspace/job-apply-mcp && python3 -m pytest tests/test_webui_auth.py
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import io
import json
import os
import re
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

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _http(base, path, token="__absent__", method="GET", data=None):
    """One HTTP call against the test server.

    ``token="__absent__"`` (the default) sends no Authorization header;
    any other value is sent as ``Authorization: Bearer <token>``.
    """
    headers = {}
    if token != "__absent__":
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


class _TempTokenEnv:
    """Redirect VETO_WEBUI_TOKEN_FILE into a temp dir for one test case."""

    def __init__(self, test):
        self._test = test

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self._test.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "token"
        self._old = os.environ.get("VETO_WEBUI_TOKEN_FILE")
        os.environ["VETO_WEBUI_TOKEN_FILE"] = str(self.path)

        def restore():
            if self._old is None:
                os.environ.pop("VETO_WEBUI_TOKEN_FILE", None)
            else:
                os.environ["VETO_WEBUI_TOKEN_FILE"] = self._old

        self._test.addCleanup(restore)
        return self

    def __exit__(self, exc_type, exc, tb):
        # Teardown runs through the registered addCleanup callbacks so the
        # temp dir outlives the test body; nothing to do here.
        return False


class WebuiAuthHttpTests(unittest.TestCase):
    """HTTP-level auth behavior against a real _Server instance."""

    def setUp(self):
        with _TempTokenEnv(self) as env:
            self.token_path = env.path
        self.token = webui.load_or_create_token()  # honors the env override
        self.server = webui.serve(0, host="127.0.0.1", token=self.token)
        self.assertIsInstance(self.server, webui._Server)
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()
        # LIFO cleanup: stop the server before the temp dir is removed.
        self.addCleanup(self._thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{port}"

    # -- 401 cases ------------------------------------------------------
    def test_no_token_401(self):
        status, body = _http(self.base, "/api/tools")
        self.assertEqual(status, 401)
        self.assertEqual(body, {"ok": False, "error": "unauthorized"})

    def test_wrong_token_401(self):
        cases = {
            "wrong bearer": "Bearer wrong-token",
            "empty bearer": "Bearer ",
            "bare 'Bearer'": "Bearer",
            "basic scheme": "Basic abc123",
        }
        for name, header_value in cases.items():
            with self.subTest(name):
                req = urllib.request.Request(
                    self.base + "/api/tools",
                    headers={"Authorization": header_value})
                try:
                    with urllib.request.urlopen(req, timeout=10):
                        self.fail("expected 401")
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 401)
                    self.assertEqual(json.loads(exc.read().decode("utf-8")),
                                     {"ok": False, "error": "unauthorized"})

    def test_post_run_requires_auth_too(self):
        status, body = _http(
            self.base, "/api/run", method="POST",
            data={"tool": "watch", "action": "list", "params": {}})
        self.assertEqual(status, 401)
        self.assertEqual(body, {"ok": False, "error": "unauthorized"})

    def test_401_never_echoes_the_token(self):
        status, body = _http(self.base, "/api/kpis", token="wrong")
        self.assertEqual(status, 401)
        self.assertNotIn(self.token, json.dumps(body))

    def test_401_carries_www_authenticate_bearer(self):
        req = urllib.request.Request(self.base + "/api/tools")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("expected 401")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            self.assertEqual(exc.headers.get("WWW-Authenticate"), "Bearer")

    def test_percent_encoded_api_path_still_hits_auth_gate(self):
        # "/%61pi/tools" unquotes to "/api/tools" — the gate must see the
        # normalized path, so this is a 401, not a 404 passthrough.
        status, _ = _http(self.base, "/%61pi/tools")
        self.assertEqual(status, 401)

    def test_non_ascii_bearer_returns_401_not_500(self):
        # hmac.compare_digest raises TypeError on non-ASCII str; the gate
        # must turn that into a 401, not a 500.
        port = self.server.server_address[1]
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        with sock:
            sock.sendall(
                ("GET /api/tools HTTP/1.1\r\n"
                 f"Host: 127.0.0.1:{port}\r\n"
                 "Authorization: Bearer caf\xe9\r\n"
                 "Connection: close\r\n\r\n").encode("latin-1"))
            resp = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                resp += chunk
        status_line = resp.split(b"\r\n", 1)[0]
        self.assertIn(b"401", status_line, resp[:200])

    # -- 200 cases ------------------------------------------------------
    def test_correct_token_200_on_all_api_routes(self):
        status, body = _http(self.base, "/api/tools", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("tools", body)
        self.assertTrue(body["tools"])

        status, body = _http(self.base, "/api/kpis", token=self.token)
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)

        status, body = _http(
            self.base, "/api/run", token=self.token, method="POST",
            data={"tool": "watch", "action": "list", "params": {}})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertIn("watches", body.get("data", {}))

    def test_static_index_open_without_token(self):
        # Static files stay open (no auth); the body is HTML, not JSON.
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            self.assertIn(b"<html", resp.read()[:4096].lower())

    # -- token file -----------------------------------------------------
    def test_token_file_created_with_0600(self):
        self.assertTrue(self.token_path.is_file())
        mode = stat.S_IMODE(self.token_path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"token file mode is {mode:04o}")
        # token_urlsafe(32) -> 43 chars; non-empty either way
        self.assertGreaterEqual(len(self.token), 32)
        # stable across restarts: a second load returns the same token
        self.assertEqual(webui.load_or_create_token(), self.token)

    def test_existing_token_file_with_loose_perms_is_tightened(self):
        loose = self.token_path.with_name("loose-token")
        loose.write_text("existing-token-value\n", encoding="utf-8")
        os.chmod(loose, 0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            token = webui.load_or_create_token(loose)
        self.assertEqual(token, "existing-token-value")
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o600)
        self.assertIn("0600", err.getvalue())

    def test_regenerate_replaces_token_and_old_token_401s(self):
        old = self.token
        new = webui.regenerate_token()
        self.assertNotEqual(new, old)
        self.assertEqual(self.token_path.read_text(encoding="utf-8").strip(), new)
        self.assertEqual(stat.S_IMODE(self.token_path.stat().st_mode), 0o600)
        # A server started after regeneration accepts only the new token.
        server2 = webui.serve(0, host="127.0.0.1", token=new)
        thread = threading.Thread(target=server2.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server2.server_close)
        self.addCleanup(server2.shutdown)
        base2 = f"http://127.0.0.1:{server2.server_address[1]}"
        status, _ = _http(base2, "/api/tools", token=old)
        self.assertEqual(status, 401)
        status, body = _http(base2, "/api/tools", token=new)
        self.assertEqual(status, 200)
        self.assertIn("tools", body)


class ServeFailClosedTests(unittest.TestCase):
    """Fail closed: the API is never served without bearer auth."""

    def _start(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_serve_without_token_still_requires_auth(self):
        # serve(token=None) falls back to the token file — never open.
        with _TempTokenEnv(self):
            token = webui.load_or_create_token()
            server = webui.serve(0, host="127.0.0.1")
            self.assertIsNotNone(server.auth_token)
            base = self._start(server)
            status, _ = _http(base, "/api/tools")
            self.assertEqual(status, 401)
            status, body = _http(base, "/api/tools", token=token)
            self.assertEqual(status, 200)
            self.assertIn("tools", body)

    def test_raw_httpserver_without_auth_token_uses_token_file(self):
        # A bare http.server.HTTPServer wrapping _Handler has no auth_token
        # attribute; the gate falls back to a read-only token file lookup.
        with _TempTokenEnv(self):
            token = webui.load_or_create_token()
            server = http.server.HTTPServer(("127.0.0.1", 0), webui._Handler)
            self.assertFalse(hasattr(server, "auth_token"))
            base = self._start(server)
            status, _ = _http(base, "/api/tools")
            self.assertEqual(status, 401)
            status, body = _http(base, "/api/tools", token=token)
            self.assertEqual(status, 200)
            self.assertIn("tools", body)

    def test_raw_httpserver_denies_when_token_file_missing(self):
        # No token file at all: deny, and never create the file on the
        # request path.
        with _TempTokenEnv(self) as env:
            server = http.server.HTTPServer(("127.0.0.1", 0), webui._Handler)
            base = self._start(server)
            status, _ = _http(base, "/api/tools")
            self.assertEqual(status, 401)
            self.assertFalse(env.path.exists(),
                             "auth gate must not create the token file")


class BootTokenPrintTests(unittest.TestCase):
    """The token is printed only when the file is (re)created."""

    def _boot(self):
        args = argparse.Namespace(host="127.0.0.1", port=0,
                                  regenerate_token=False, show_token=False,
                                  no_browser=True)
        buf = io.StringIO()
        with mock.patch.object(webui._Server, "serve_forever",
                               side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(buf):
            rc = webui.cmd_serve(args)
        return rc, buf.getvalue()

    def test_token_printed_on_first_creation_not_on_second_boot(self):
        with _TempTokenEnv(self) as env:
            self.assertFalse(env.path.exists())
            rc, out = self._boot()
            self.assertEqual(rc, 0)
            token = env.path.read_text(encoding="utf-8").strip()
            self.assertIn(token, out)
            rc, out = self._boot()
            self.assertEqual(rc, 0)
            self.assertNotIn(token, out)

    def test_show_token_prints_token_and_exits_zero(self):
        with _TempTokenEnv(self) as env:
            token = webui.load_or_create_token()
            args = argparse.Namespace(host="127.0.0.1", port=8765,
                                      regenerate_token=False, show_token=True,
                                      no_browser=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = webui.cmd_serve(args)
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), token)


class LanResolverTests(unittest.TestCase):
    def test_resolve_lan_ip_returns_plausible_ipv4(self):
        ip = webui.resolve_lan_ip()
        self.assertRegex(ip, _IPV4_RE)
        self.assertTrue(all(0 <= int(o) <= 255 for o in ip.split(".")),
                        f"implausible octets in {ip!r}")

    def test_resolve_bind_host(self):
        self.assertEqual(webui.resolve_bind_host("lan"), webui.resolve_lan_ip())
        self.assertEqual(webui.resolve_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(webui.resolve_bind_host("192.168.1.9"), "192.168.1.9")
        self.assertEqual(webui.resolve_bind_host("0.0.0.0"), "0.0.0.0")

    def test_host_lan_failure_exits_nonzero(self):
        args = argparse.Namespace(host="lan", port=8765,
                                  regenerate_token=False, no_browser=True)
        err = io.StringIO()
        with mock.patch.object(webui, "resolve_lan_ip",
                               side_effect=OSError("no route to host")):
            with contextlib.redirect_stderr(err):
                rc = webui.cmd_serve(args)
        self.assertEqual(rc, 2)
        self.assertIn("lan", err.getvalue().lower())


class StartupOutputTests(unittest.TestCase):
    """Bind URL / LAN URL / token lines, captured — never printed."""

    def _captured(self, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            webui._print_startup(*args, **kwargs)
        return buf.getvalue()

    def test_loopback_prints_bind_url_and_token_but_no_lan_url(self):
        out = self._captured("127.0.0.1", 8765, "tok", show_token=True)
        self.assertIn("http://127.0.0.1:8765", out)
        self.assertNotIn("LAN URL", out)
        self.assertIn("tok", out)

    def test_lan_host_prints_lan_url(self):
        out = self._captured("192.168.1.5", 8765, "tok", show_token=True)
        self.assertIn("http://192.168.1.5:8765", out)
        self.assertIn("LAN URL", out)

    def test_show_token_false_hides_token(self):
        out = self._captured("127.0.0.1", 8765, "tok", show_token=False)
        self.assertNotIn("tok", out)

    def test_off_loopback_prints_cleartext_warning(self):
        out = self._captured("192.168.1.5", 8765, "tok", show_token=False)
        lowered = out.lower()
        self.assertIn("no tls", lowered)
        self.assertIn("cleartext", lowered)
        self.assertIn("trusted lan", lowered)
        self.assertIn("tailscale", lowered)

    def test_zero_host_warns_all_interfaces_no_unconnectable_url(self):
        out = self._captured("0.0.0.0", 8765, "tok", show_token=False)
        self.assertIn("ALL interfaces", out)
        self.assertNotIn("http://0.0.0.0:", out)
        self.assertIn("LAN URL", out)


class RegenerateFlagTests(unittest.TestCase):
    """`serve --regenerate-token` via a real CLI subprocess (output captured)."""

    def test_regenerate_flag_replaces_token_and_prints_it(self):
        with _TempTokenEnv(self) as env:
            old = webui.load_or_create_token()
            proc = subprocess.run(
                [sys.executable, "cli.py", "serve",
                 "--regenerate-token", "--no-browser"],
                cwd=BASE_DIR,
                env={**os.environ,
                     "VETO_WEBUI_TOKEN_FILE": str(env.path)},
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = proc.stdout.strip().splitlines()[-1]
        current = env.path.read_text(encoding="utf-8").strip()
        self.assertEqual(printed, current)  # the flag prints the new token
        self.assertNotEqual(current, old)
        self.assertEqual(stat.S_IMODE(env.path.stat().st_mode), 0o600)

    def test_regenerate_flag_prints_restart_notice(self):
        with _TempTokenEnv(self) as env:
            webui.load_or_create_token()
            args = argparse.Namespace(host="127.0.0.1", port=8765,
                                      regenerate_token=True, show_token=False,
                                      no_browser=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = webui.cmd_serve(args)
            self.assertEqual(rc, 0)
            out = buf.getvalue().lower()
            self.assertIn("restart", out)
            self.assertIn("old token stays valid until restart", out)


if __name__ == "__main__":
    unittest.main()
