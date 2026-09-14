#!/usr/bin/env python3
"""Developer scaffold tests (Initiative 11, Epic 4)."""

import ast
import compileall
import ast
import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i11.policy_kit.checks import run_policy_suite
from initiatives.i11.scaffold.template import scaffold_extension

EXPECTED_FILES = {
    "manifest.json", "extension.py", "docs/README.md", "web_card.html",
    "wizard_step.py", "cli_snippet.py", "mcp_snippet.py", "RETRO.md",
}


class TestScaffold(unittest.TestCase):
    def test_scaffold_generates_all_surfaces(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        result = scaffold_extension(
            "Demo Ext", description="demo", author="tester", out_dir=tmp,
            data_scopes=["jobs:read"], action_kinds=["read", "draft"])
        for expected in EXPECTED_FILES:
            self.assertIn(expected, result["files"], expected)
        out = Path(result["dir"])
        self.assertTrue((out / "manifest.json").is_file())
        self.assertTrue((out / "tests" / "test_demo_ext.py").is_file())

    def test_scaffolded_manifest_validates(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        result = scaffold_extension("Demo Ext 2", out_dir=tmp)
        from initiatives.i11.manifest.schema import load_manifest
        manifest = load_manifest(result["dir"])
        self.assertEqual(manifest["id"], "demo-ext-2")

    def test_fresh_scaffold_passes_policy_suite_out_of_the_box(self):
        # The scaffold promises policy-clean default output. Run the suite
        # on PRISTINE output — no rewriting of the generated code: if the
        # template is safe, nothing needs fixing up first.
        for kinds in (["read"], ["read", "draft", "confirm-required"],
                      ["preview", "notify"]):
            tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
            result = scaffold_extension("Safe Default", out_dir=tmp,
                                        action_kinds=kinds)
            report = run_policy_suite(result["dir"])
            failures = [r for r in report["results"] if not r["passed"]]
            self.assertEqual(failures, [],
                             f"policy failures for {kinds}: {failures}")

    def test_bad_name_rejected(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        with self.assertRaises(ValueError):
            scaffold_extension("!!!", out_dir=tmp)

    def test_collision_raises(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        scaffold_extension("Demo Ext", out_dir=tmp)
        with self.assertRaises(FileExistsError):
            scaffold_extension("Demo Ext", out_dir=tmp)


class TestActionKindValidation(unittest.TestCase):
    """C2: scaffold must reject duplicate/unknown action kinds loudly."""

    def test_duplicate_action_kinds_rejected(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        with self.assertRaises(ValueError) as ctx:
            scaffold_extension("Dup Ext", out_dir=tmp,
                               action_kinds=["read", "read"])
        self.assertIn("duplicate", str(ctx.exception).lower())
        # Nothing written on failure.
        self.assertFalse((tmp / "dup-ext").exists())

    def test_unknown_action_kind_rejected(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        with self.assertRaises(ValueError) as ctx:
            scaffold_extension("Weird Ext", out_dir=tmp,
                               action_kinds=["teleport"])
        self.assertIn("unknown", str(ctx.exception).lower())
        self.assertFalse((tmp / "weird-ext").exists())

    def test_all_known_kinds_accepted(self):
        from initiatives.i11.manifest.schema import ACTION_KINDS
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        result = scaffold_extension("All Kinds", out_dir=tmp,
                                    action_kinds=list(ACTION_KINDS))
        manifest_ids = {a["id"] for a in
                        json.loads(
                            (Path(result["dir"]) / "manifest.json")
                            .read_text())["permissions"]["actions"]}
        self.assertEqual(len(manifest_ids), len(ACTION_KINDS))


class TestHostileInterpolation(unittest.TestCase):
    """C3: hostile names/descriptions must stay inert text, never code."""

    HOSTILE = 'Inj"""\nimport os\n"""Ext'

    def test_hostile_name_cannot_inject_code(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        result = scaffold_extension(self.HOSTILE, out_dir=tmp,
                                    description='Desc"""\nx=1\n"""tail')
        out = Path(result["dir"])
        ext_py = out / "extension.py"
        src = ext_py.read_text()
        # The generated module must compile and contain no import of os
        # at all (the injected `import os` must not exist as code).
        compile(src, str(ext_py), "exec")
        tree = ast.parse(src)
        imports = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(imports, [], f"unexpected imports: {imports}")
        self.assertNotIn("\nimport os\n", src)
        # Every other generated Python file must also compile.
        for py in sorted(out.rglob("*.py")):
            compile(py.read_text(encoding="utf-8"), str(py), "exec")

    def test_hostile_name_escaped_in_html(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        result = scaffold_extension(self.HOSTILE, out_dir=tmp)
        card = (Path(result["dir"]) / "web_card.html").read_text()
        self.assertNotIn('"""', card)          # raw quotes never reach HTML
        self.assertNotIn("<script>", card)
        self.assertIn("Inj&quot;&quot;&quot;", card)

    def test_hostile_scopes_escaped_in_html(self):
        tmp = Path(tempfile.mkdtemp(prefix="veto-scaffold-"))
        result = scaffold_extension(
            "Scope Esc", out_dir=tmp,
            data_scopes=['jobs:read"><script>alert(1)</script>'])
        card = (Path(result["dir"]) / "web_card.html").read_text()
        self.assertNotIn("<script>", card)


if __name__ == "__main__":
    unittest.main()
