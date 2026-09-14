#!/usr/bin/env python3
"""Directive readme-hardening §8.2: channel_consent.key and
channel_consent.jsonl must be gitignored.

Capability report §6: channel_consent.key defaults to
<repo>/channel_consent.key, a sibling of the consent log — not under
~/.veto. A user committing their own HMAC key is a real outcome of that
default, so the gitignore entry is load-bearing and gets a regression
test.
"""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CONSENT_FILES = ("channel_consent.key", "channel_consent.jsonl")


class TestConsentFilesGitignored(unittest.TestCase):
    def test_consent_key_and_log_are_gitignored(self):
        proc = subprocess.run(
            ["git", "check-ignore", *CONSENT_FILES],
            cwd=REPO, capture_output=True, text=True,
        )
        ignored = set(proc.stdout.split())
        missing = [f for f in CONSENT_FILES if f not in ignored]
        self.assertEqual(
            missing, [],
            f"These consent files are NOT gitignored and could be committed: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
