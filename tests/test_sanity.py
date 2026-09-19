"""Sanity tests: everything compiles, MCP stdio works, CLI works.

- py_compile every .py in the project (excluding .venv)
- MCP stdio smoke: initialize -> tools/list shows the expected tools
- CLI: --help exits 0, `boards` lists boards (read-only, no network)
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

PYTHON = sys.executable
EXPECTED_TOOLS = {
    "list_boards", "search_jobs", "get_job_details",
    "apply_to_job", "track_applications",
}


class TestCompiles(unittest.TestCase):
    def test_all_py_compile(self):
        import py_compile

        failures = []
        for path in sorted(BASE_DIR.rglob("*.py")):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                py_compile.compile(str(path), doraise=True)
            except py_compile.PyCompileError as exc:
                failures.append(f"{path.name}: {exc}")
        self.assertFalse(failures, "compile failures:\n" + "\n".join(failures))


def _rpc_request(proc, method, params=None, req_id=1):
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    proc.stdin.write((json.dumps(payload) + "\n").encode())
    proc.stdin.flush()


def _rpc_read(proc, timeout=30):
    import select

    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("no JSON-RPC response from MCP server")
    line = proc.stdout.readline()
    return json.loads(line.decode())


class TestMCPStdio(unittest.TestCase):
    def test_initialize_and_tools_list(self):
        proc = subprocess.Popen(
            [PYTHON, "server.py"],
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _rpc_request(proc, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sanity-test", "version": "0"},
            })
            resp = _rpc_read(proc)
            self.assertIn("result", resp)
            self.assertIn("serverInfo", resp["result"])
            self.assertEqual(resp["result"]["serverInfo"]["name"], "veto")

            proc.stdin.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            proc.stdin.flush()

            _rpc_request(proc, "tools/list", {}, req_id=2)
            resp = _rpc_read(proc)
            tools = {t["name"] for t in resp["result"]["tools"]}
            missing = EXPECTED_TOOLS - tools
            self.assertFalse(missing, f"missing tools: {missing}")
        finally:
            for stream in (proc.stdin, proc.stderr):
                try:
                    stream.close()
                except (BrokenPipeError, OSError):
                    pass
            proc.terminate()
            proc.wait(timeout=10)


class TestCLI(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [PYTHON, "cli.py", *args],
            cwd=str(BASE_DIR),
            capture_output=True, text=True, timeout=120,
        )

    def test_help_exits_zero(self):
        result = self._run("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("search", result.stdout)

    def test_boards(self):
        result = self._run("boards")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.lower()
        self.assertIn("greenhouse", out)
        self.assertIn("adzuna", out)
        # Deleted scrapers must not appear as supported boards.
        self.assertNotIn("indeed", out)
        self.assertNotIn("ziprecruiter", out)

    def test_search_help(self):
        result = self._run("search", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--location", result.stdout)


if __name__ == "__main__":
    unittest.main()
