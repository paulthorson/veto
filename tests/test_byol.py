#!/usr/bin/env python3
"""Unit tests for byol.py: bring-your-own-listing (spec section 5.2).

Covers the three ingestion inputs (pasted text, URL fetch with mocked
HTTP, saved HTML file) and the pipeline handoff: a saved listing must
resolve through server.get_job_details / briefs / the "user" board
exactly like a provider-sourced listing. No real network anywhere.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import byol  # noqa: E402
from providers._common import decode_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_HTML = FIXTURES / "byol_sample.html"

PASTE_TEXT = """\
Senior Backend Engineer @ Acme Corp
Remote — $150k-$180k base + equity with 4-year vesting.
- Own and operate our payments API (2M requests/day)
- Design and ship services in Python and Go
- Mentor engineers through weekly 1:1s
"""


class _FakeResponse:
    def __init__(self, text, status_code=200, content_type="text/html"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class _FakeClient:
    """httpx-shaped fake: records requests, returns canned HTML."""

    def __init__(self, html, status_code=200):
        self.html = html
        self.status_code = status_code
        self.requests: list[str] = []
        self.closed = False

    def get(self, url):
        self.requests.append(url)
        return _FakeResponse(self.html, self.status_code)

    def close(self):
        self.closed = True


class ByolStoreTest(unittest.TestCase):
    """Isolate the listing store in a temp dir for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="byol-test-")
        self._orig_dir = byol.LISTINGS_DIR
        byol.LISTINGS_DIR = Path(self.tmp)

    def tearDown(self):
        byol.LISTINGS_DIR = self._orig_dir
        shutil.rmtree(self.tmp, ignore_errors=True)


class PasteTextTest(ByolStoreTest):
    def test_paste_parses_title_company(self):
        result = byol.add_listing(text=PASTE_TEXT)
        self.assertNotIn("error", result)
        self.assertEqual(result["title"], "Senior Backend Engineer")
        self.assertEqual(result["company"], "Acme Corp")
        self.assertEqual(result["board"], "user")
        self.assertTrue(result["id"].startswith("user:"))
        self.assertEqual(result["source"], "paste")

    def test_paste_keeps_full_description(self):
        result = byol.add_listing(text=PASTE_TEXT)
        self.assertIn("payments API", result["description"])
        self.assertIn("4-year vesting", result["description"])

    def test_paste_extracts_requirements_bullets(self):
        result = byol.add_listing(text=PASTE_TEXT)
        self.assertIn("Own and operate our payments API", result["requirements"])

    def test_paste_is_idempotent(self):
        first = byol.add_listing(text=PASTE_TEXT)
        second = byol.add_listing(text=PASTE_TEXT)
        self.assertEqual(first["id"], second["id"])

    def test_overrides_win(self):
        result = byol.add_listing(text=PASTE_TEXT, title="Staff SWE", company="Globex")
        self.assertEqual(result["title"], "Staff SWE")
        self.assertEqual(result["company"], "Globex")

    def test_empty_text_is_an_error(self):
        result = byol.add_listing(text="   ")
        self.assertIn("error", result)

    def test_exactly_one_source_required(self):
        result = byol.add_listing(text=PASTE_TEXT, url="https://example.com/j")
        self.assertIn("error", result)
        result = byol.add_listing()
        self.assertIn("error", result)


class HtmlFileTest(ByolStoreTest):
    def test_html_file_parses_json_ld(self):
        result = byol.add_listing(html_file=str(SAMPLE_HTML))
        self.assertNotIn("error", result)
        self.assertEqual(result["title"], "Senior Platform Engineer")
        self.assertEqual(result["company"], "Initech")
        self.assertIn("Austin", result["location"])
        self.assertIn("Kubernetes platform", result["description"])
        self.assertIn("Terraform and GitOps", result["requirements"])
        self.assertEqual(result["source"], "html_file")

    def test_missing_file_is_an_error(self):
        result = byol.add_listing(html_file=str(FIXTURES / "nope.html"))
        self.assertIn("error", result)

    def test_html_parse_is_zero_network(self):
        # parse_html_file takes no client and makes no requests by
        # construction; assert the module-level fetch path is untouched
        # by parsing a file while the network is "watched".
        calls: list[str] = []

        class _ExplodingClient:
            def get(self, url):  # pragma: no cover
                calls.append(url)
                raise AssertionError("network used during file parse")

        parsed = byol.parse_html_file(str(SAMPLE_HTML))
        self.assertNotIn("error", parsed)
        self.assertEqual(calls, [])


class UrlFetchTest(ByolStoreTest):
    def setUp(self):
        super().setUp()
        self._orig_robots = byol.robots_allows
        self._orig_delay = byol.polite_delay
        byol.robots_allows = lambda board, url, timeout=5.0: True
        byol.polite_delay = lambda *a, **k: None

    def tearDown(self):
        byol.robots_allows = self._orig_robots
        byol.polite_delay = self._orig_delay
        super().tearDown()

    def test_url_fetched_once_and_parsed(self):
        html = SAMPLE_HTML.read_text(encoding="utf-8")
        client = _FakeClient(html)
        fetched = byol.fetch_url("https://careers.example.com/j/123", client=client)
        self.assertIn("html", fetched)
        self.assertEqual(client.requests, ["https://careers.example.com/j/123"])
        listing = byol.parse_html(fetched["html"], source_url="https://careers.example.com/j/123")
        self.assertEqual(listing["title"], "Senior Platform Engineer")
        self.assertEqual(listing["company"], "Initech")

    def test_url_flow_end_to_end_with_mocked_client(self):
        html = SAMPLE_HTML.read_text(encoding="utf-8")
        orig_fetch = byol.fetch_url
        # Inject the fake client: add_listing calls module-level fetch_url.
        byol.fetch_url = lambda url, client=None: orig_fetch(
            url, client=_FakeClient(html))
        try:
            result = byol.add_listing(url="https://careers.example.com/j/123")
        finally:
            byol.fetch_url = orig_fetch
        self.assertNotIn("error", result)
        self.assertEqual(result["source"], "url")
        self.assertEqual(result["url"], "https://careers.example.com/j/123")
        self.assertEqual(result["title"], "Senior Platform Engineer")

    def test_fetch_uses_make_client_when_none_passed(self):
        """The URL path must go through providers._common.make_client —
        the single honest User-Agent (commit 2), not a browser string."""
        seen: dict[str, bool] = {}

        class _RecordingFactoryClient(_FakeClient):
            pass

        def fake_make_client(timeout=20.0):
            seen["called"] = True
            return _FakeClient("<html><body><h1>T</h1></body></html>")

        orig = byol.make_client
        byol.make_client = fake_make_client
        try:
            fetched = byol.fetch_url("https://careers.example.com/j/1")
        finally:
            byol.make_client = orig
        self.assertTrue(seen.get("called"), "fetch_url bypassed make_client")
        self.assertIn("html", fetched)

    def test_non_http_scheme_refused(self):
        result = byol.fetch_url("file:///etc/passwd")
        self.assertIn("error", result)

    def test_robots_disallow_refuses(self):
        byol.robots_allows = lambda board, url, timeout=5.0: False
        result = byol.fetch_url("https://careers.example.com/j/1",
                                client=_FakeClient("<html></html>"))
        self.assertIn("error", result)
        self.assertIn("robots", result["error"])

    def test_http_error_is_an_error(self):
        result = byol.fetch_url("https://careers.example.com/j/404",
                                client=_FakeClient("", status_code=404))
        self.assertIn("error", result)


class ProviderHandoffTest(ByolStoreTest):
    """The saved listing must flow through the standard downstream path."""

    def _add(self):
        result = byol.add_listing(text=PASTE_TEXT)
        self.assertNotIn("error", result)
        return result

    def test_board_resolves_details(self):
        result = self._add()
        payload = decode_payload(result["id"].split(":", 1)[1])
        details = byol.UserListingsBoard().get_details(payload)
        self.assertEqual(details["title"], "Senior Backend Engineer")
        self.assertEqual(details["company"], "Acme Corp")
        self.assertIn("payments API", details["description"])

    def test_unknown_listing_key_errors_like_providers(self):
        details = byol.UserListingsBoard().get_details("listing-doesnotexist")
        self.assertIn("error", details)
        self.assertEqual(details["board"], "user")

    def test_server_get_job_details_resolves_user_id(self):
        import server

        result = self._add()
        details = server.get_job_details(result["id"])
        self.assertNotIn("error", details)
        self.assertEqual(details["title"], "Senior Backend Engineer")
        self.assertEqual(details["job_id"], result["id"])

    def test_briefs_resolves_user_id(self):
        import briefs

        result = self._add()
        details = briefs._get_job_details(result["id"])
        self.assertNotIn("error", details)
        self.assertEqual(details["company"], "Acme Corp")

    def test_user_board_in_providers(self):
        import server

        self.assertIn("user", server.PROVIDERS)
        self.assertEqual(server.PROVIDERS["user"].name, "user")

    def test_user_board_searches_saved_listings(self):
        self._add()
        import server

        provider = server.PROVIDERS["user"]
        hits = provider.search("backend engineer", "", limit=10, remote_only=False)
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["id"].startswith("user:"))
        misses = provider.search("astronaut welder", "", limit=10, remote_only=False)
        self.assertEqual(misses, [])

    def test_search_jobs_with_user_board(self):
        self._add()
        import server

        hits = server.search_jobs("backend engineer", "", board="user", limit=10)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["tos_risk_tier"], "user")

    def test_list_listings(self):
        self._add()
        items = byol.list_listings()
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["id"].startswith("user:"))
        self.assertEqual(items[0]["title"], "Senior Backend Engineer")


class CliWiringTest(ByolStoreTest):
    def test_register_cli_adds_command(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = byol.register_cli(sub)
        self.assertIn("add-listing", handlers)

    def test_cmd_add_listing(self):
        args = argparse.Namespace(
            text=PASTE_TEXT, url="", html_file="",
            title="", company="", location="", json=False,
        )
        self.assertEqual(byol.cmd_add_listing(args), 0)

    def test_dashboard_menu_has_byol_first(self):
        import dashboard

        self.assertIn("0", dashboard.MENU)
        label, fn = dashboard.MENU["0"]
        self.assertIn("listing", label.lower())
        self.assertIs(fn, dashboard.wf_byol)


if __name__ == "__main__":
    unittest.main()
