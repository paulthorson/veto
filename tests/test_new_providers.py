#!/usr/bin/env python3
"""Unit tests for the Glassdoor provider stub.

The ZipRecruiter scraper was deleted (legal-hardening commit 5); its
tests were deleted with it. Glassdoor remains as a *documented* stub:
search raises NotImplementedError with the shared policy rationale from
providers/glassdoor.py (no public API + bot mitigation), so a search
surfaces an honest reason instead of fake-empty results.
"""

import unittest

from providers import glassdoor as gd


class TestGlassdoorStub(unittest.TestCase):
    def test_search_raises_not_implemented_with_clear_reason(self):
        provider = gd.GlassdoorProvider()
        with self.assertRaises(NotImplementedError) as ctx:
            provider.search("engineer", "New York, NY", limit=10,
                            remote_only=False)
        message = str(ctx.exception).lower()
        self.assertIn("not supported", message)
        self.assertIn("bot", message)  # anti-bot explanation

    def test_details_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            gd.GlassdoorProvider().get_details("https://example.com/job")

    def test_get_providers(self):
        providers = gd.get_providers()
        self.assertIn("glassdoor", providers)
        self.assertEqual(providers["glassdoor"].name, "glassdoor")


class TestGlassdoorServerStub(unittest.TestCase):
    """server.py keeps Glassdoor as a *documented* stub, not a TODO.

    The NotImplementedError message must be the shared policy rationale
    from providers/glassdoor.py (no public API + bot mitigation), so a
    search surfaces an honest reason instead of fake-empty results.
    """

    def test_server_stub_raises_documented_reason(self):
        import server

        provider = server.PROVIDERS["glassdoor"]
        self.assertEqual(provider.name, "glassdoor")
        with self.assertRaises(NotImplementedError) as ctx:
            provider.search(
                "engineer", "New York, NY", limit=5, remote_only=False
            )
        message = str(ctx.exception).lower()
        self.assertIn("not supported", message)
        self.assertIn("public api", message)
        with self.assertRaises(NotImplementedError) as ctx:
            provider.get_details("https://www.glassdoor.com/job/x")
        self.assertIn("not supported", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main()
