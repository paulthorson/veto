#!/usr/bin/env python3
"""Conformance tests for the Initiative 09 provider contract kit.

Covers: normalized schema validation, terms classification, budgets,
manifest completeness, the submission interface contract (headline
property: no silent drops, no misreported submission status), and the
per-connector conformance suite (the Q2 gate's "capability contract" half).
"""

import unittest

import ats_apply
from providers import _contract
from providers import ashby, greenhouse, lever
from providers._common import make_job_id
from providers._contract import (
    ConnectorManifest,
    JobDetail,
    JobPosting,
    SubmissionResult,
    SubmissionStatus,
    TermsClass,
    apply_signature_problems,
    budget_for,
    conformance_suite,
    manifest_for,
    submission_result_from,
    suite_passed,
    terms_for,
    validate_job_detail,
    validate_job_posting,
    validate_submission_result,
)


def _good_posting(**over):
    base = {
        "id": "greenhouse:dG9rZW46MTIz",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "url": "https://example.com/jobs/123",
        "board": "greenhouse",
        "snippet": "Backend Engineer — Acme",
    }
    base.update(over)
    return base


def _good_detail(**over):
    base = {
        "url": "https://example.com/jobs/123",
        "board": "greenhouse",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "New York, NY",
        "description": "Build things.",
        "apply_url": "https://example.com/apply/123",
    }
    base.update(over)
    return base


def _ats_direct_apply(job, applicant, *, confirm=False, dry_run=True):
    """SubmissionConnector adapter for the real ats_apply.apply_direct."""
    raw = ats_apply.apply_direct(
        str(job.get("board") or "ashby"),
        job,
        applicant,
        confirm=confirm,
        dry_run=dry_run,
    )
    return submission_result_from(raw, connector="ats_direct")


class SchemaValidationTests(unittest.TestCase):
    def test_posting_conforms(self):
        self.assertEqual(validate_job_posting(_good_posting()), [])

    def test_posting_missing_fields(self):
        problems = validate_job_posting({"id": "x", "title": "t"})
        self.assertTrue(any("company" in p for p in problems))

    def test_posting_id_must_be_self_describing(self):
        problems = validate_job_posting(_good_posting(id="not-a-job-id"))
        self.assertTrue(any("self-describing" in p for p in problems))

    def test_posting_id_prefix_must_match_board(self):
        problems = validate_job_posting(
            _good_posting(id="lever:abc123", board="greenhouse")
        )
        self.assertTrue(any("prefix" in p for p in problems))

    def test_posting_id_payload_must_be_base64url(self):
        problems = validate_job_posting(
            _good_posting(id="greenhouse:not base64!!", board="greenhouse")
        )
        self.assertTrue(any("base64url" in p for p in problems))

    def test_posting_real_make_job_id_conforms(self):
        posting = _good_posting(
            id=make_job_id("greenhouse", "acme:123"), board="greenhouse"
        )
        self.assertEqual(validate_job_posting(posting), [])

    def test_falsy_semantics_agree_between_validators(self):
        # Both validators treat None and "" as missing (previously they
        # disagreed: `not raw.get(key)` vs `in (None, "")`).
        self.assertTrue(
            any("title" in p for p in validate_job_posting(_good_posting(title="")))
        )
        self.assertTrue(
            any("title" in p for p in validate_job_detail(_good_detail(title="")))
        )
        self.assertTrue(
            any("title" in p for p in validate_job_posting(_good_posting(title=None)))
        )

    def test_detail_conforms(self):
        self.assertEqual(validate_job_detail(_good_detail()), [])

    def test_detail_error_shape_conforms(self):
        err = {"board": "lever", "payload": "x", "error": "not found"}
        self.assertEqual(validate_job_detail(err), [])

    def test_detail_error_shape_url_variant_conforms(self):
        err = {"url": "https://x", "board": "lever", "error": "gone"}
        self.assertEqual(validate_job_detail(err), [])

    def test_detail_error_rejects_extra_keys(self):
        # The documented shape is exact: extra keys fail.
        err = {
            "url": "https://x",
            "board": "ashby",
            "error": "gone",
            "note": "extra",
        }
        problems = validate_job_detail(err)
        self.assertTrue(any("unexpected keys" in p for p in problems))

    def test_detail_error_without_board_fails(self):
        self.assertTrue(validate_job_detail({"error": "boom"}))

    def test_job_posting_dataclass_roundtrip(self):
        posting = JobPosting(**{k: v for k, v in _good_posting().items()
                                 if k in JobPosting.__dataclass_fields__})
        self.assertEqual(validate_job_posting(posting.to_dict()), [])

    def test_job_detail_dataclass_roundtrip(self):
        detail = JobDetail(**{k: v for k, v in _good_detail().items()
                              if k in JobDetail.__dataclass_fields__})
        self.assertEqual(validate_job_detail(detail.to_dict()), [])


class TermsClassificationTests(unittest.TestCase):
    def test_official_boards_classified(self):
        for name in ("greenhouse", "lever", "ashby"):
            terms, basis, robots_url = terms_for(name)
            self.assertEqual(terms, TermsClass.OFFICIAL_API, name)
            self.assertTrue(basis, name)
            self.assertTrue(robots_url.startswith("https://"), name)

    def test_scraping_boards_classified(self):
        # Legal-hardening commit 5 deleted the ZipRecruiter scraper: no
        # board is classified HTML_SCRAPING anymore, and ziprecruiter
        # must NOT retain a scraping-tier classification.
        terms, _, _ = terms_for("ziprecruiter")
        self.assertNotEqual(terms, TermsClass.HTML_SCRAPING)

    def test_ats_direct_classified(self):
        # The module's own first-party connector must be classifiable by
        # its own gate (was: fell through to HONEST_STUB, terms.classified
        # could never pass).
        terms, basis, robots_url = terms_for("ats_direct")
        self.assertEqual(terms, TermsClass.OFFICIAL_API)
        self.assertTrue(basis)
        self.assertTrue(robots_url.startswith("https://"))

    def test_registries_agree(self):
        # One authoritative registry story: every terms entry has a
        # manifest and vice versa.
        self.assertEqual(
            set(_contract.TERMS_CLASSIFICATION),
            set(_contract.PROVIDER_MANIFESTS),
        )

    def test_unknown_provider_defaults_to_honest_stub(self):
        terms, basis, _ = terms_for("definitely-not-a-board")
        self.assertEqual(terms, TermsClass.HONEST_STUB)
        self.assertIn("No classification registered", basis)

    def test_unknown_provider_gets_zero_budget(self):
        self.assertEqual(budget_for("definitely-not-a-board"), (0, 3600))

    def test_registered_search_budgets_are_sane(self):
        # ziprecruiter was removed from the registry (legal-hardening
        # commit 5 deleted its scraper); only surviving boards are listed.
        for name in ("greenhouse", "lever", "ashby"):
            daily, cooldown = budget_for(name)
            self.assertGreater(daily, 0, name)
            self.assertGreater(cooldown, 0, name)

    def test_key_gated_stub_declares_zero_budget(self):
        # adzuna has no credentials and stays a stub: its declared budget
        # must reflect that honestly (was: 1000/day while its manifest
        # calls it an honest stub that raises RuntimeError on search).
        daily, cooldown = budget_for("adzuna")
        self.assertEqual(daily, 0)
        self.assertGreater(cooldown, 0)

    def test_honest_stub_declares_zero_budget(self):
        self.assertEqual(budget_for("glassdoor")[0], 0)

    def test_non_search_connector_declares_zero_search_budget(self):
        # ats_direct is an "ats" connector: it never searches.
        self.assertEqual(budget_for("ats_direct")[0], 0)


class ManifestTests(unittest.TestCase):
    def test_every_registered_manifest_is_complete(self):
        for manifest in _contract.all_manifests():
            self.assertEqual(
                manifest.validate(), [],
                f"manifest {manifest.name!r} incomplete",
            )

    def test_manifest_for_unknown_is_none(self):
        self.assertIsNone(manifest_for("definitely-not-a-board"))

    def test_ats_direct_manifest_declares_no_direct_submit(self):
        manifest = manifest_for("ats_direct")
        self.assertIsNotNone(manifest)
        blocked = " ".join(manifest.why_it_may_be_blocked).lower()
        self.assertIn("401", blocked)
        self.assertIn("api key", blocked)
        sent = " ".join(manifest.what_data_it_sends).lower()
        self.assertIn("nothing today", sent)

    def test_incomplete_manifest_fails_validation(self):
        bad = ConnectorManifest(
            name="x", connector_type="job_provider",
            what_it_can_do=["a"], why_it_may_be_blocked=[],
            what_data_it_sends=["b"], what_requires_confirmation=["c"],
        )
        problems = bad.validate()
        self.assertTrue(any("why_it_may_be_blocked" in p for p in problems))


class SubmissionContractTests(unittest.TestCase):
    """Headline property: no silent drops, no misreported status."""

    def test_vocabulary_is_closed_and_has_no_bypass(self):
        values = {s.value for s in SubmissionStatus}
        self.assertEqual(
            values,
            {
                "draft", "submitted", "failed", "paused",
                "captcha_blocked", "rate_limited", "needs_confirmation",
                "unavailable",
            },
        )
        self.assertFalse(any("bypass" in v for v in values))

    def test_unknown_status_fails_validation(self):
        forged = SubmissionResult(status="captcha_bypassed", connector="x")
        problems = validate_submission_result(forged)
        self.assertTrue(any("unknown submission status" in p for p in problems))

    def test_empty_status_fails_validation(self):
        forged = {"status": "", "connector": "x"}
        self.assertTrue(validate_submission_result(forged))

    def test_submitted_requires_confirmation_evidence(self):
        bare = SubmissionResult(status=SubmissionStatus.SUBMITTED, connector="x")
        problems = validate_submission_result(bare)
        self.assertTrue(any("confirmation evidence" in p for p in problems))
        good = SubmissionResult(
            status=SubmissionStatus.SUBMITTED, connector="x",
            confirmation="app-123",
        )
        self.assertEqual(validate_submission_result(good), [])

    def test_failed_requires_error(self):
        silent = SubmissionResult(status=SubmissionStatus.FAILED, connector="x")
        problems = validate_submission_result(silent)
        self.assertTrue(any("no error recorded" in p for p in problems))

    def test_draft_must_not_claim_submission(self):
        lying = SubmissionResult(
            status=SubmissionStatus.DRAFT, connector="x", confirmation="app-1"
        )
        problems = validate_submission_result(lying)
        self.assertTrue(any("must not claim a submission" in p for p in problems))

    def test_honest_results_validate(self):
        for result in (
            SubmissionResult(status=SubmissionStatus.DRAFT, connector="x"),
            SubmissionResult(
                status=SubmissionStatus.FAILED, connector="x", error="boom"
            ),
            SubmissionResult(
                status=SubmissionStatus.PAUSED, connector="x",
                error="paused: captcha",
            ),
        ):
            self.assertEqual(validate_submission_result(result), [], result)

    def test_apply_signature_accepts_contract_shape(self):
        def good(job, applicant, *, confirm=False, dry_run=True):
            return SubmissionResult(status=SubmissionStatus.DRAFT)

        self.assertEqual(apply_signature_problems(good), [])

    def test_apply_signature_rejects_wrong_defaults(self):
        def bad(job, applicant, *, confirm=True, dry_run=False):
            return SubmissionResult(status=SubmissionStatus.DRAFT)

        problems = apply_signature_problems(bad)
        self.assertTrue(any("confirm" in p for p in problems))

    def test_apply_signature_rejects_missing_params(self):
        def bad(job):
            return SubmissionResult(status=SubmissionStatus.DRAFT)

        problems = apply_signature_problems(bad)
        self.assertTrue(any("applicant" in p for p in problems))

    # --- submission_result_from: grounded in the real modules -------------

    def test_ats_preview_maps_to_draft(self):
        raw = ats_apply.apply_direct(
            "ashby",
            {"id": make_job_id("ashby", "acme:123")},
            {"full_name": "T"},
            confirm=False,
            dry_run=True,
        )
        result = submission_result_from(raw, connector="ats_direct")
        self.assertEqual(result.status, SubmissionStatus.DRAFT)
        self.assertEqual(validate_submission_result(result), [])

    def test_ats_confirmed_never_submits_still_preview(self):
        # Legal-hardening commit 6: apply_direct is preview-only — there
        # is no submit path left, so confirm=True/dry_run=False still
        # returns the dry-run preview (DRAFT), never a network submit.
        result = _ats_direct_apply(
            {"id": make_job_id("ashby", "acme:123"), "board": "ashby"},
            {"full_name": "T"},
            confirm=True,
            dry_run=False,
        )
        self.assertEqual(result.status, SubmissionStatus.DRAFT)
        self.assertEqual(validate_submission_result(result), [])

    def test_ats_ok_true_maps_to_submitted(self):
        result = submission_result_from(
            {"ok": True, "board": "ashby", "status_code": 200,
             "confirmation": None},
            connector="ats_direct",
        )
        self.assertEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertEqual(result.confirmation, "http-200")
        self.assertEqual(validate_submission_result(result), [])

    def test_ats_ok_true_without_evidence_fails_validation(self):
        result = submission_result_from(
            {"ok": True, "board": "ashby"}, connector="ats_direct"
        )
        self.assertEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertTrue(validate_submission_result(result))

    def test_ats_cap_refusal_maps_to_rate_limited(self):
        result = submission_result_from(
            {"ok": False, "board": "ashby",
             "error": "Daily application cap reached (5/5)."},
            connector="ats_direct",
        )
        self.assertEqual(result.status, SubmissionStatus.RATE_LIMITED)

    def test_ats_429_maps_to_rate_limited(self):
        result = submission_result_from(
            {"ok": False, "board": "ashby", "status_code": 429,
             "error": "too many"},
            connector="ats_direct",
        )
        self.assertEqual(result.status, SubmissionStatus.RATE_LIMITED)

    def test_ats_generic_error_maps_to_failed(self):
        result = submission_result_from(
            {"ok": False, "board": "ashby", "error": "Malformed job_id"},
            connector="ats_direct",
        )
        self.assertEqual(result.status, SubmissionStatus.FAILED)
        self.assertEqual(validate_submission_result(result), [])

    def test_browser_paused_captcha_maps_to_captcha_blocked(self):
        result = submission_result_from(
            {
                "ok": False, "submitted": False, "paused": True,
                "rescue": {"reason": "captcha"},
                "final_url": "https://example.com/apply",
                "error": "Paused: captcha detected",
            },
            connector="browser",
        )
        self.assertEqual(result.status, SubmissionStatus.CAPTCHA_BLOCKED)
        self.assertEqual(validate_submission_result(result), [])

    def test_browser_paused_other_reason_maps_to_paused(self):
        result = submission_result_from(
            {
                "ok": False, "submitted": False, "paused": True,
                "rescue": {"reason": "auth_loss"},
                "error": "Paused: auth_loss",
            },
            connector="browser",
        )
        self.assertEqual(result.status, SubmissionStatus.PAUSED)

    def test_browser_submitted_maps_to_submitted(self):
        result = submission_result_from(
            {
                "ok": True, "submitted": True,
                "screenshot_after_submit": "/tmp/shot.png",
                "final_url": "https://example.com/done",
                "error": None,
            },
            connector="browser",
        )
        self.assertEqual(result.status, SubmissionStatus.SUBMITTED)
        self.assertEqual(validate_submission_result(result), [])

    def test_browser_fill_only_maps_to_draft(self):
        # confirm=False: form filled and screenshotted, nothing submitted.
        result = submission_result_from(
            {
                "ok": True, "submitted": False, "fields_detected": 5,
                "screenshot": "/tmp/preview.png",
                "final_url": "https://example.com/apply",
                "error": None,
            },
            connector="browser",
        )
        self.assertEqual(result.status, SubmissionStatus.DRAFT)

    def test_browser_error_maps_to_failed(self):
        result = submission_result_from(
            {"ok": False, "submitted": False, "error": "Navigation failed"},
            connector="browser",
        )
        self.assertEqual(result.status, SubmissionStatus.FAILED)

    # NOTE (legal-hardening commit 3): the "easy" connector vocabulary was
    # deleted with easy_apply.py. The old easy_* mapping tests asserted
    # mappings for a module that no longer exists; the remaining
    # test_easy_unknown_status_raises_loudly pins the loud rejection of
    # unrecognized shapes.

    def test_easy_unknown_status_raises_loudly(self):
        with self.assertRaises(ValueError):
            submission_result_from(
                {"status": "definitely_new", "board": "linkedin"},
                connector="easy",
            )

    def test_unrecognized_shape_raises_loudly(self):
        with self.assertRaises(ValueError):
            submission_result_from({"frobnicate": True}, connector="x")

    def test_source_auto_detection(self):
        # NOTE (legal-hardening commit 3): the old {"status": "preview"}
        # easy_apply shape is gone with easy_apply.py — only ats/browser
        # shapes remain auto-detectable.
        self.assertEqual(
            submission_result_from(
                {"ok": False, "submitted": False, "error": "x"}
            ).status,
            SubmissionStatus.FAILED,
        )
        self.assertEqual(
            submission_result_from(
                {"mode": "preview", "dry_run": True, "board": "ashby"}
            ).status,
            SubmissionStatus.DRAFT,
        )


class ConformanceSuiteTests(unittest.TestCase):
    def _run(self, provider):
        results = conformance_suite(
            provider,
            search_fixture=[_good_posting(id=f"{provider.name}:abc",
                                          board=provider.name)],
            detail_fixture=_good_detail(board=provider.name),
        )
        return results

    def test_greenhouse_passes_conformance(self):
        results = self._run(greenhouse.GreenhouseProvider(tokens=[]))
        self.assertTrue(suite_passed(results),
                        [r for r in results if not r["passed"]])

    def test_lever_passes_conformance(self):
        results = self._run(lever.LeverProvider(tokens=[]))
        self.assertTrue(suite_passed(results),
                        [r for r in results if not r["passed"]])

    def test_ashby_passes_conformance(self):
        results = self._run(ashby.AshbyProvider(tokens=[]))
        self.assertTrue(suite_passed(results),
                        [r for r in results if not r["passed"]])

    def test_ats_direct_passes_conformance(self):
        # The module's own first-party connector is now certifiable by its
        # own gate: terms classified, registries agree, and the dry-run
        # submit path reports honestly through the adapter.
        class AtsDirectStub:
            name = "ats_direct"

        results = conformance_suite(
            AtsDirectStub(),
            apply=_ats_direct_apply,
            apply_job={
                "id": make_job_id("ashby", "acme:123"),
                "board": "ashby",
            },
            apply_applicant={"full_name": "T", "email": "t@example.com"},
        )
        self.assertTrue(suite_passed(results),
                        [r for r in results if not r["passed"]])
        by_check = {r["check"]: r for r in results}
        self.assertTrue(by_check["submit.no_silent_drops"]["passed"])
        self.assertTrue(by_check["submit.unknown_status_rejected"]["passed"])
        self.assertTrue(by_check["registry.agreement"]["passed"])

    def test_suite_flags_bad_fixture(self):
        results = conformance_suite(
            greenhouse.GreenhouseProvider(tokens=[]),
            search_fixture=[{"id": "x"}],
        )
        self.assertFalse(suite_passed(results))
        failed = [r["check"] for r in results if not r["passed"]]
        self.assertIn("schema.search_rows", failed)

    def test_suite_flags_unregistered_provider(self):
        class Mystery:
            name = "mystery-board"
        results = conformance_suite(Mystery())
        failed = [r["check"] for r in results if not r["passed"]]
        self.assertIn("manifest.registered", failed)
        self.assertIn("terms.classified", failed)

    def test_suite_flags_silent_drop(self):
        # An apply callable that returns None is a silent drop: the gate
        # must fail it.
        class AtsStub:
            name = "ats_direct"

        def _drops(job, applicant, *, confirm=False, dry_run=True):
            return None

        results = conformance_suite(AtsStub(), apply=_drops)
        failed = [r["check"] for r in results if not r["passed"]]
        self.assertIn("submit.no_silent_drops", failed)

    def test_suite_flags_unknown_status(self):
        # An apply callable reporting outside the vocabulary fails.
        class AtsStub:
            name = "ats_direct"

        def _lies(job, applicant, *, confirm=False, dry_run=True):
            return SubmissionResult(status="totally_submitted")

        results = conformance_suite(AtsStub(), apply=_lies)
        failed = [r["check"] for r in results if not r["passed"]]
        self.assertIn("submit.no_silent_drops", failed)

    def test_suite_flags_dishonest_preview(self):
        # A dry run claiming submission fails preview honesty.
        class AtsStub:
            name = "ats_direct"

        def _lies(job, applicant, *, confirm=False, dry_run=True):
            return SubmissionResult(
                status=SubmissionStatus.SUBMITTED, confirmation="app-1"
            )

        results = conformance_suite(AtsStub(), apply=_lies)
        failed = [r["check"] for r in results if not r["passed"]]
        self.assertIn("submit.preview_honest", failed)


if __name__ == "__main__":
    unittest.main()
