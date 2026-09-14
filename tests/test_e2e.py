"""End-to-end tests.

Browser E2E: drives a REAL browser (Chromium and/or Firefox via
Playwright — whichever installed) against a local test form:
confirm=False fills + screenshots without submitting; confirm=True
is accepted for compatibility but changes nothing (fill-only).
Skipped gracefully when no browser is available.

Live E2E: search_jobs() against the real server code (network).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tests.browsers import available_browsers  # noqa: E402

AVAILABLE = available_browsers()

FORM_URL = (BASE_DIR / "tests" / "fixtures" / "apply_form.html").as_uri()

E2E_PROFILE = {
    "full_name": "Ada Lovelace",
    "email": "ada@example.com",
    "phone": "+1 555-0100",
    "location": "New York, NY",
    "linkedin_url": "https://www.linkedin.com/in/adalovelace",
    "website": "https://adalovelace.dev",
    "cover_letter": "Dear hiring team, I would love this role.",
}


def _screenshots_now() -> set[str]:
    d = BASE_DIR / "screenshots"
    if not d.is_dir():
        return set()
    return {p.name for p in d.glob("*.png")}


class TestBrowserE2E(unittest.TestCase):
    """Real-browser form fill against the local fixture form."""

    def _run_fill(self, browser_kind: str, confirm: bool) -> dict:
        # Local import so the module imports even without playwright.
        from browser_apply import apply_via_browser

        with tempfile.NamedTemporaryFile(
            suffix=".pdf", delete=False
        ) as fh:
            fh.write(b"%PDF-1.4 fake resume")
            resume = fh.name
        try:
            return apply_via_browser(
                FORM_URL,
                profile=dict(E2E_PROFILE),
                resume_path=resume,
                headless=True,
                confirm=confirm,
                browser_kind=browser_kind,
            )
        finally:
            Path(resume).unlink(missing_ok=True)

    def _check_browser(self, browser_kind: str) -> None:
        if browser_kind not in AVAILABLE:
            self.skipTest(
                f"{browser_kind} not available "
                "(run: .venv/bin/python -m playwright install "
                f"{browser_kind})"
            )

    def test_dry_run_fills_without_submitting(self):
        for browser_kind in ("chromium", "firefox"):
            with self.subTest(browser=browser_kind):
                self._check_browser(browser_kind)
                before = _screenshots_now()
                result = self._run_fill(browser_kind, confirm=False)
                self.assertTrue(result["ok"], f"browser error: {result['error']}")
                # Fill-only (legal-hardening commit 6): no submission
                # receipt exists anymore — the report carries the fill.
                self.assertNotIn("submitted", result)
                self.assertNotIn("submit_clicked", result)
                self.assertFalse(result.get("browser_open"))
                self.assertIn("handoff", result)
                self.assertGreaterEqual(result["fields_detected"], 8)
                filled = result["fields_filled"]
                self.assertEqual(filled.get("email"), "ada@example.com")
                self.assertEqual(filled.get("first_name"), "Ada")
                self.assertEqual(filled.get("last_name"), "Lovelace")
                self.assertEqual(filled.get("location"), "New York, NY")
                self.assertIn("resume", filled)
                # Attestations are always left to the human:
                self.assertNotIn("eeo_consent", filled)
                shot = result.get("screenshot")
                self.assertTrue(shot and Path(shot).is_file(),
                                "preview screenshot missing")
                # clean up screenshots this run created
                for name in _screenshots_now() - before:
                    (BASE_DIR / "screenshots" / name).unlink(missing_ok=True)


class TestLiveSearchE2E(unittest.TestCase):
    """Real network: search_jobs through the actual server code."""

    def test_search_returns_results(self):
        import server

        try:
            results = server.search_jobs(
                "software engineer", "New York, NY",
                board="all", limit=5,
            )
        except Exception as exc:
            self.skipTest(f"network unavailable: {exc}")
        if not results:
            self.skipTest(
                "no results — boards may be blocking this sandbox IP")
        boards = {r["board"] for r in results}
        self.assertTrue(boards, "expected at least one provider's results")
        for job in results:
            for key in ("id", "title", "company", "location",
                        "url", "board", "snippet"):
                self.assertIn(key, job)


if __name__ == "__main__":
    unittest.main()
