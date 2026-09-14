#!/usr/bin/env python3
"""Directive readme-hardening §7.2: no shipped file may contain an
unfilled placeholder.

"Shipped" = tracked by git (`git ls-files`). This test FAILS until the
author fills every placeholder — that failure is INTENTIONAL (the
directive requires it). Known current failures:
  - SECURITY.md: [SECURITY CONTACT — to be filled]
  - docs/i12/methodology.md: an unnamed contracted role ("to be filled")
"""
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

MARKERS = ("to be filled", "WILL BE SUPPLIED")

# Extensions never scanned as text.
SKIP_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".zip", ".gz", ".pyc",
})


def shipped_text_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO,
        capture_output=True, text=True, check=True,
    )
    files = []
    for rel in out.stdout.split("\0"):
        if not rel:
            continue
        path = REPO / rel
        if path == SELF:
            # This file necessarily names the markers it searches for.
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


class TestNoUnfilledPlaceholders(unittest.TestCase):
    def test_no_unfilled_placeholders_in_shipped_files(self):
        hits = []
        for path in shipped_text_files():
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            for marker in MARKERS:
                if marker in text:
                    hits.append(f"{path.relative_to(REPO)}: {marker!r}")
        self.assertEqual(
            hits, [],
            "Unfilled placeholders in shipped files — the author must fill "
            "them:\n" + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
