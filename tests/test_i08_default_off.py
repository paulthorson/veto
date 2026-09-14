"""Initiative 08 — default-off and no-transmission proof.

The hard contract: nothing is transmitted by default. This is proven
three ways:
  1. Static: contribute.py and initiatives/i08/* import no network or
     subprocess capability at all (stdlib data/path libs only).
  2. Behavioral: preview stages nothing; submit refuses without preview
     and without confirmation; purposes are all off by default.
  3. Gate order: the quarantine lock is checked before the kill switch,
     and no settings toggle can clear it.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import contribute  # noqa: E402
from initiatives.i08 import purposes, quarantine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_IMPORTS = {
    "socket", "urllib", "urllib.request", "urllib.parse", "requests",
    "httpx", "http.client", "http.server", "smtplib", "ftplib",
    "subprocess", "os",  # os only via explicit allowlist below
}

# Modules allowed to import os (file permissions, randomness only).
OS_ALLOWED = {"initiatives/i08/quarantine.py"}


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


class NoTransmissionTestCase(unittest.TestCase):
    def test_no_network_or_subprocess_imports(self):
        offenders = []
        files = [REPO_ROOT / "contribute.py"] + sorted(
            (REPO_ROOT / "initiatives" / "i08").glob("*.py"))
        for path in files:
            rel = str(path.relative_to(REPO_ROOT))
            imported = _imports_of(path)
            for bad in FORBIDDEN_IMPORTS - {"os"}:
                if bad in imported:
                    offenders.append(f"{rel} imports {bad}")
            if "os" in imported and rel not in OS_ALLOWED:
                offenders.append(f"{rel} imports os")
        self.assertEqual(offenders, [],
                         "transmission-capable imports found:\n"
                         + "\n".join(offenders))

    def test_no_dynamic_network_constructs(self):
        """No __import__ tricks, no exec/eval of remote code."""
        offenders = []
        files = [REPO_ROOT / "contribute.py"] + sorted(
            (REPO_ROOT / "initiatives" / "i08").glob("*.py"))
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in ("__import__", "eval(", "exec("):
                if token in text:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)} contains {token!r}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class DefaultOffTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        store = {
            "sess_complete": {
                "session_id": "sess_complete",
                "company": "Initech",
                "role": "Senior Backend Engineer",
                "questions": [{"id": "q1", "kind": "behavioral",
                               "question": "Tell me about yourself."}],
                "answers": {"q1": {"answer": (
                    "I led the billing rebuild and mentored engineers."),
                    "overall": 82}},
                "complete": True,
                "created": "2026-09-01T00:00:00+00:00",
            },
        }
        mock_file = root / "mock_sessions.json"
        mock_file.write_text(json.dumps(store), encoding="utf-8")
        contrib_dir = root / "contributions"
        patchers = [
            mock.patch.object(
                contribute, "_source_paths",
                return_value={"mock_interview": mock_file,
                              "soft_skills": mock_file,
                              "ai_proficiency": mock_file}),
            mock.patch.object(contribute, "CONTRIB_DIR", contrib_dir),
            mock.patch.object(contribute, "OUTBOX_DIR",
                              contrib_dir / "outbox"),
            mock.patch.object(contribute, "CONSENT_PATH",
                              contrib_dir / "consent.json"),
            mock.patch.object(contribute, "SETTINGS_PATH",
                              contrib_dir / "settings.json"),
            mock.patch.object(contribute, "PREVIEWED_PATH",
                              contrib_dir / "previewed.json"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_purposes_all_off_by_default(self):
        for slug in purposes.purpose_definitions():
            state = purposes.consent_state(slug, contribute.CONTRIB_DIR)
            self.assertFalse(state["granted"],
                             f"{slug} granted by default")

    def test_submit_without_preview_refused(self):
        # No preview was ever recorded for this hash: submit must refuse
        # (and per the contract, a hash mismatch quarantines).
        result = contribute.submit_contribution(["sess_complete"],
                                                confirmed=True)
        self.assertIn("error", result)
        self.assertFalse(result.get("staged", False))

    def test_submit_without_confirmation_refused(self):
        result = contribute.submit_contribution(["sess_complete"],
                                                confirmed=False)
        self.assertIn("error", result)
        self.assertFalse(result.get("staged", False))

    def test_preview_stages_nothing(self):
        def _outbox():
            return sorted(contribute.OUTBOX_DIR.glob("*.json")) \
                if contribute.OUTBOX_DIR.is_dir() else []
        before = _outbox()
        contribute.preview_contribution(["sess_complete"])
        self.assertEqual(before, _outbox(),
                         "preview must not stage any outbox file")

    def test_quarantine_lock_checked_before_killswitch(self):
        """The lock overrides the kill switch in both directions."""
        # With no quarantine, kill switch state is readable and separate.
        self.assertFalse(quarantine.is_quarantined())
        # Enabling contributions does not (and cannot) lift a lock —
        # covered by the quarantine drill; here assert the gate order in
        # submit: lock first, then kill switch.
        import inspect
        src = inspect.getsource(contribute.submit_contribution)
        lock_pos = src.find("is_quarantined")
        switch_pos = src.find("contributions_enabled")
        self.assertGreater(lock_pos, -1)
        self.assertGreater(switch_pos, -1)
        self.assertLess(lock_pos, switch_pos,
                        "quarantine lock must be checked before kill switch")


if __name__ == "__main__":
    unittest.main()
