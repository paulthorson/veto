#!/usr/bin/env python3
"""Tests for Initiative 09 epic 2: ATS deepening (Greenhouse/Lever/Ashby).

All HTTP is mocked; no network. Asserts the enriched detail fields are
parsed from the official public APIs and that the honest apply-path
declarations (no direct submit) hold.
"""

import json
import unittest
from unittest import mock

import ats_apply
from providers import ashby, greenhouse, lever
from providers._contract import validate_job_detail


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, **kwargs):
        return FakeResponse(self._payload)


def _patch(testcase, module, payload):
    client = FakeClient(payload)
    testcase.addCleanup(mock.patch.stopall)
    mock.patch.object(module, "make_client", return_value=client).start()
    mock.patch.object(module, "polite_delay", return_value=None).start()


GREENHOUSE_DETAIL = {
    "id": 123,
    "title": "Backend Engineer",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
    "location": {"name": "New York, NY"},
    "company_name": "Acme",
    "content": "<p>Build things.</p>",
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "NYC HQ"}],
    "metadata": [{"name": "Employment Type", "value": "Full-time"}],
    "updated_at": "2026-09-01T12:00:00Z",
    "questions": [
        {"label": "First name", "required": True},
        {"label": "Portfolio", "required": False},
    ],
}

LEVER_DETAIL = {
    "id": "abc-123",
    "text": "Backend Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
    "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
    "categories": {
        "department": "Engineering",
        "team": "Platform",
        "commitment": "Full-time",
        "location": "New York",
    },
    "workplaceType": "remote",
    "descriptionPlain": "Build things.",
    "lists": [{"text": "Requirements", "content": "5 years"}],
    "createdAt": 1788220800000,  # 2026-09-01 UTC
}

ASHBY_BOARD = {
    "jobs": [
        {
            "id": "job-1",
            "title": "Backend Engineer",
            "department": "Engineering",
            "team": "Platform",
            "employmentType": "Full-Time",
            "location": "New York, NY",
            "isRemote": True,
            "jobUrl": "https://jobs.ashbyhq.com/acme/job-1",
            "applyUrl": "https://jobs.ashbyhq.com/acme/job-1/application",
            "descriptionPlain": "Build things.",
            "descriptionHtml": "<p>Build things.</p>",
            "publishedDate": "2026-09-01",
        }
    ]
}


class GreenhouseEnrichmentTests(unittest.TestCase):
    def test_enriched_detail_fields(self):
        _patch(self, greenhouse, GREENHOUSE_DETAIL)
        details = greenhouse.GreenhouseProvider(tokens=["acme"]).get_details(
            "acme:123"
        )
        self.assertEqual(details["application_questions"], 2)
        self.assertEqual(details["required_questions"], 1)
        self.assertEqual(details["offices"], ["NYC HQ"])
        self.assertEqual(details["custom_fields"], ["Employment Type"])
        self.assertEqual(details["employment_type"], "Full-time")
        self.assertEqual(details["posted_date"], "2026-09-01")
        self.assertIn("Greenhouse", details["readiness_note"])

    def test_enriched_detail_conforms_to_schema(self):
        _patch(self, greenhouse, GREENHOUSE_DETAIL)
        details = greenhouse.GreenhouseProvider(tokens=["acme"]).get_details(
            "acme:123"
        )
        self.assertEqual(validate_job_detail(details), [])

    def test_malformed_payload_still_errors(self):
        details = greenhouse.GreenhouseProvider(tokens=[]).get_details("nope")
        self.assertIn("error", details)


class LeverEnrichmentTests(unittest.TestCase):
    def test_enriched_detail_fields(self):
        _patch(self, lever, LEVER_DETAIL)
        details = lever.LeverProvider(tokens=["acme"]).get_details("acme:abc-123")
        self.assertEqual(details["departments"], ["Engineering", "Platform"])
        self.assertEqual(details["employment_type"], "Full-time")
        self.assertTrue(details["remote"])
        self.assertEqual(details["posted_date"], "2026-09-01")
        # Honest: the public API does not expose application questions.
        self.assertEqual(details["application_questions"], 0)
        self.assertIn("does not expose", details["readiness_note"])

    def test_enriched_detail_conforms_to_schema(self):
        _patch(self, lever, LEVER_DETAIL)
        details = lever.LeverProvider(tokens=["acme"]).get_details("acme:abc-123")
        self.assertEqual(validate_job_detail(details), [])

    def test_bad_created_at_falls_back_to_empty_posted_date(self):
        # Hostile or malformed vendor createdAt must never raise: the
        # detail shape is returned with posted_date "" instead.
        import copy

        for bad_created_at in ("not-a-timestamp", 10**22, -10**22):
            raw = copy.deepcopy(LEVER_DETAIL)
            raw["createdAt"] = bad_created_at
            _patch(self, lever, raw)
            details = lever.LeverProvider(tokens=["acme"]).get_details(
                "acme:abc-123"
            )
            self.assertEqual(details["posted_date"], "")
            self.assertEqual(validate_job_detail(details), [])
            mock.patch.stopall()


class AshbyEnrichmentTests(unittest.TestCase):
    def _provider(self):
        _patch(self, ashby, ASHBY_BOARD)
        prov = ashby.AshbyProvider(tokens=["acme"])
        # get_details re-fetches the board; FakeClient returns the board.
        return prov

    def test_enriched_detail_fields(self):
        details = self._provider().get_details("acme:job-1")
        self.assertEqual(details["departments"], ["Engineering", "Platform"])
        self.assertEqual(details["employment_type"], "Full-Time")
        self.assertTrue(details["remote"])
        self.assertEqual(details["application_questions"], 0)
        self.assertIn("401", details["readiness_note"])

    def test_enriched_detail_conforms_to_schema(self):
        details = self._provider().get_details("acme:job-1")
        self.assertEqual(validate_job_detail(details), [])

    def test_missing_posting_returns_error_shape(self):
        details = self._provider().get_details("acme:gone")
        self.assertIn("error", details)
        self.assertEqual(validate_job_detail(details), [])


class ApplyPathHonestyTests(unittest.TestCase):
    def test_no_board_allows_direct_apply(self):
        # Legal-hardening commit 6 deleted can_apply_direct entirely:
        # there is no direct-apply gate left to flip on for any board.
        self.assertFalse(hasattr(ats_apply, "can_apply_direct"))

    def test_describe_apply_path_names_browser_flow(self):
        for board in ("greenhouse", "lever", "ashby"):
            desc = ats_apply.describe_apply_path(board)
            self.assertFalse(desc["direct_apply_available"])
            self.assertIn("browser", desc["recommended_path"].lower())
            self.assertTrue(desc["official_interface_only"])
            self.assertTrue(desc["reason"])

    def test_describe_apply_path_unknown_board(self):
        desc = ats_apply.describe_apply_path("nope")
        self.assertEqual(desc["endpoint_status"], "unknown")
        self.assertFalse(desc["direct_apply_available"])


if __name__ == "__main__":
    unittest.main()
