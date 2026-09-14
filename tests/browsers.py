"""Shared helpers for the test suite: browser availability probes."""

from __future__ import annotations

import functools
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

BROWSER_KINDS = ("chromium", "firefox")


@functools.lru_cache(maxsize=None)
def browser_available(kind: str) -> bool:
    """True if Playwright can launch ``kind`` here (probe, ~seconds)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    browser = None
    try:
        with sync_playwright() as p:
            launcher = getattr(p, kind)
            browser = launcher.launch(
                headless=True, args=["--no-sandbox"], timeout=30000
            )
            browser.close()
        return True
    except Exception:
        return False
    finally:
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass


def available_browsers() -> list[str]:
    """Browsers that can actually launch in this environment."""
    return [b for b in BROWSER_KINDS if browser_available(b)]
