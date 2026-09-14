#!/usr/bin/env python3
"""Manifest validation tests (Initiative 11, Epic 1)."""

import json
import tempfile
import unittest
from pathlib import Path

from initiatives.i11.manifest.schema import (
    ManifestError, load_manifest, manifest_summary, validate_manifest,
)


def _good() -> dict:
    return {
        "manifest_version": 1,
        "id": "test-ext",
        "version": "1.2.3",
        "name": "Test",
        "description": "d",
        "permissions": {
            "data": ["jobs:read"],
            "network": {"destinations": []},
            "actions": [{"id": "a1", "kind": "read"}],
            "pii": "redact",
            "sandbox": {"fs_roots": [], "rate_limit": {"calls_per_minute": 60}},
        },
    }


class TestManifest(unittest.TestCase):
    def test_valid_manifest_normalizes(self):
        m = validate_manifest(_good())
        self.assertEqual(m["id"], "test-ext")
        self.assertEqual(m["permissions"]["pii"], "redact")

    def test_unknown_data_scope_rejected(self):
        bad = _good()
        bad["permissions"]["data"] = ["everything:read"]
        with self.assertRaises(ManifestError):
            validate_manifest(bad)

    def test_unknown_action_kind_rejected(self):
        bad = _good()
        bad["permissions"]["actions"] = [{"id": "x", "kind": "launch-missiles"}]
        with self.assertRaises(ManifestError):
            validate_manifest(bad)

    def test_network_destination_must_be_bare_hostname(self):
        bad = _good()
        bad["permissions"]["network"] = {"destinations": ["https://evil.example/x"]}
        with self.assertRaises(ManifestError):
            validate_manifest(bad)

    def test_fs_root_traversal_rejected(self):
        bad = _good()
        bad["permissions"]["sandbox"] = {"fs_roots": ["../../etc"]}
        with self.assertRaises(ManifestError):
            validate_manifest(bad)

    def test_confirm_required_keeps_flag_but_other_kinds_normalized(self):
        m = _good()
        m["permissions"]["actions"] = [
            {"id": "s", "kind": "confirm-required", "confirm_required": True},
            {"id": "r", "kind": "read", "confirm_required": True},
        ]
        out = validate_manifest(m)
        kinds = {a["id"]: a["confirm_required"] for a in out["permissions"]["actions"]}
        self.assertTrue(kinds["s"])
        self.assertFalse(kinds["r"])

    def test_load_manifest_from_dir(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "manifest.json").write_text(json.dumps(_good()))
        m = load_manifest(tmp)
        self.assertEqual(m["id"], "test-ext")

    def test_missing_manifest_raises(self):
        with self.assertRaises(ManifestError):
            load_manifest(Path(tempfile.mkdtemp()))

    def test_summary_marks_confirm_capability(self):
        m = _good()
        m["permissions"]["actions"] = [
            {"id": "s", "kind": "confirm-required", "confirm_required": True}]
        s = manifest_summary(validate_manifest(m))
        self.assertTrue(s["can_submit_anything"])

    def test_bad_semver_rejected(self):
        bad = _good()
        bad["version"] = "v1"
        with self.assertRaises(ManifestError):
            validate_manifest(bad)


if __name__ == "__main__":
    unittest.main()
