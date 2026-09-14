"""Vendored-copy tests for the terminal theme (job-apply-mcp root).

These tests exercise the VENDORED ``veto_theme.py`` / ``theme.json`` at the
repo root — the copy produced by ``scripts/sync-design-system.sh`` with the
flat-layout ``_THEME_PATH`` patch — not the monorepo source. The vendored
wiring is the riskiest part of the terminal integration, so it gets its own
suite: fail-soft import with a missing theme, plus paint/divider/
supports_color behavior through the vendored module.

Written in unittest style so both ``tests/run.py`` (unittest discovery)
and pytest can run it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDORED_MODULE = os.path.join(REPO_ROOT, "veto_theme.py")
VENDORED_THEME = os.path.join(REPO_ROOT, "theme.json")


def load_vendored(name=None):
    """Import the vendored veto_theme.py from the repo root under a unique
    module name, so this suite can never accidentally pick up the monorepo
    source copy."""
    name = name or "vendored_veto_theme_{}".format(uuid.uuid4().hex)
    spec = importlib.util.spec_from_file_location(name, VENDORED_MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


class _Tty(io.StringIO):
    def isatty(self):
        return True


class VendoredThemeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vt = load_vendored()

    def _colored_env(self):
        """Context manager forcing supports_color() == True."""
        stack = contextlib.ExitStack()
        # Save and clear any color-disabling vars; restored on exit.
        saved = {
            k: os.environ.pop(k, None) for k in ("NO_COLOR", "CLICOLOR")
        }
        stack.callback(
            lambda: os.environ.update(
                {k: v for k, v in saved.items() if v is not None}
            )
        )
        stack.enter_context(
            mock.patch.dict(os.environ, {"TERM": "xterm"}, clear=False)
        )
        stack.enter_context(mock.patch.object(self.vt.sys, "stdout", _Tty()))
        return stack

    def _no_color_env(self):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False)
        )
        stack.enter_context(mock.patch.object(self.vt.sys, "stdout", _Tty()))
        return stack

    # --- vendoring wiring -------------------------------------------------

    def test_theme_path_is_flat_layout(self):
        # The sync script patches _THEME_PATH to the flat layout: theme.json
        # lives next to the vendored module at the repo root.
        expected = os.path.join(REPO_ROOT, "theme.json")
        self.assertEqual(
            os.path.normpath(self.vt._THEME_PATH), os.path.normpath(expected)
        )

    def test_vendored_theme_json_matches_module(self):
        with open(VENDORED_THEME, encoding="utf-8") as f:
            theme = json.load(f)
        self.assertEqual(theme["version"], "0.1.0")
        self.assertIn("brand", theme["roles"])

    # --- import-time fail-soft --------------------------------------------

    def _load_without_theme(self, theme_text):
        """Copy the vendored module to an empty temp dir (optionally with a
        theme.json) and import it there."""
        tmp = tempfile.mkdtemp(prefix="veto-theme-vendored-")
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(VENDORED_MODULE, os.path.join(tmp, "veto_theme.py"))
        if theme_text is not None:
            with open(os.path.join(tmp, "theme.json"), "w",
                      encoding="utf-8") as f:
                f.write(theme_text)
        name = "vendored_veto_theme_naked_{}".format(uuid.uuid4().hex)
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(tmp, "veto_theme.py")
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module, err.getvalue()

    def test_missing_theme_imports_with_fallback(self):
        module, err = self._load_without_theme(None)
        self.assertIn("veto_theme: warning", err)
        self.assertIn("fallback", err)
        self.assertTrue(module.USING_FALLBACK_THEME)
        for role in ("display", "success", "danger", "muted", "border"):
            self.assertIn(role, module.ROLES)
        self.assertEqual(len(module.divider("SCORE A JOB")), 60)

    def test_corrupt_theme_imports_with_fallback(self):
        module, err = self._load_without_theme("{not valid json")
        self.assertIn("veto_theme: warning", err)
        self.assertTrue(module.USING_FALLBACK_THEME)

    # --- behavior through the vendored module ------------------------------

    def test_paint_unknown_role_raises_in_all_modes(self):
        with self._colored_env():
            with self.assertRaises(KeyError):
                self.vt.paint("not-a-role", "x")
        with self._no_color_env():
            with self.assertRaises(KeyError):
                self.vt.paint("not-a-role", "x")
            self.assertEqual(self.vt.paint("danger", "x"), "x")

    def test_paint_degrades_without_color(self):
        with self._no_color_env():
            self.assertEqual(self.vt.paint("success", "done"), "done")

    def test_term_dumb_disables_color(self):
        with self._colored_env():
            with mock.patch.dict(os.environ, {"TERM": "dumb"}):
                self.assertFalse(self.vt.supports_color())
                self.assertNotIn("\x1b", self.vt.paint("danger", "boom"))

    def test_divider_truncates_overlong_labels(self):
        with self._no_color_env():
            self.assertEqual(len(self.vt.divider("A" * 100)), 60)
            self.assertEqual(len(self.vt.divider("SCORE A JOB")), 60)

    def test_status_mark_none(self):
        with self._no_color_env():
            self.assertEqual(self.vt.status_mark(None), "?")
            self.assertEqual(self.vt.status_mark("ok"), "✓")

    def test_style_empty_roles_emits_no_escape(self):
        with self._colored_env():
            self.assertEqual(self.vt.style("x", []), "x")

    def test_brand_and_danger_have_distinct_256_codes(self):
        with self._no_color_env():
            self.assertNotEqual(
                self.vt.ansi256("brand"), self.vt.ansi256("danger")
            )


if __name__ == "__main__":
    unittest.main()
