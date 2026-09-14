#!/usr/bin/env python3
"""Tests for ``initiatives.i04.schemas`` — the frozen Initiative 04 contracts.

All fixtures are synthetic and clearly labeled; no real employers,
people, or achievements appear anywhere in this file.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from initiatives.i04 import schemas  # noqa: E402

#: SYNTHETIC FIXTURE — not a real posting, not real experience.
SYNTHETIC_JOB_ID = "synthetic-job-001"
SYNTHETIC_HASH = "sha256:abc123def4567890"


def _good_entry() -> dict:
    return {
        "requirement": {
            "text": "5+ years Python",
            "source_quote": "5+ years of Python experience required",
            "kind": "must_have",
        },
        "status": "supported",
        "evidence": [
            {
                "evidence_id": "ev_001",
                "quote": "6 years Python building data pipelines",
                "profile_field": "experience[1].summary",
                "quantified": True,
            }
        ],
        "grill_question_id": None,
    }


def _good_map() -> dict:
    return {
        "schema": "veto/evidence-map/v1",
        "job_id": SYNTHETIC_JOB_ID,
        "job_title": "Senior Widget Engineer (synthetic)",
        "evidence_source_hash": SYNTHETIC_HASH,
        "created_at": "2026-09-13T18:00:00+00:00",
        "entries": [_good_entry()],
    }


class EvidenceMapValidationTest(unittest.TestCase):
    def test_valid_map_passes(self) -> None:
        self.assertEqual(schemas.validate_evidence_map(_good_map()), [])

    def test_non_dict_fails(self) -> None:
        self.assertTrue(schemas.validate_evidence_map([]))

    def test_wrong_schema_rejected(self) -> None:
        doc = _good_map()
        doc["schema"] = "veto/evidence-map/v2"
        self.assertTrue(schemas.validate_evidence_map(doc))

    def test_supported_requires_evidence(self) -> None:
        doc = _good_map()
        doc["entries"][0]["evidence"] = []
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("requires >= 1 evidence" in e for e in errors))

    def test_gap_must_not_carry_evidence(self) -> None:
        doc = _good_map()
        entry = doc["entries"][0]
        entry["status"] = "gap"
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("gap" in e and "must not carry" in e for e in errors))

    def test_gap_without_evidence_passes(self) -> None:
        doc = _good_map()
        entry = doc["entries"][0]
        entry["status"] = "gap"
        entry["evidence"] = []
        self.assertEqual(schemas.validate_evidence_map(doc), [])

    def test_grill_question_requires_link(self) -> None:
        doc = _good_map()
        entry = doc["entries"][0]
        entry["status"] = "grill_question"
        entry["evidence"] = []
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("grill_question_id" in e for e in errors))

    def test_grill_question_with_link_passes(self) -> None:
        doc = _good_map()
        entry = doc["entries"][0]
        entry["status"] = "grill_question"
        entry["evidence"] = []
        entry["grill_question_id"] = "grill-q-7"
        self.assertEqual(schemas.validate_evidence_map(doc), [])

    def test_grill_question_must_not_carry_evidence(self) -> None:
        doc = _good_map()
        entry = doc["entries"][0]
        entry["status"] = "grill_question"
        entry["grill_question_id"] = "grill-q-7"
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("must not carry" in e for e in errors))

    def test_missing_evidence_source_hash_fails(self) -> None:
        doc = _good_map()
        del doc["evidence_source_hash"]
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("evidence_source_hash" in e for e in errors))

    def test_invalid_status_rejected(self) -> None:
        doc = _good_map()
        doc["entries"][0]["status"] = "maybe"
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("status must be one of" in e for e in errors))

    def test_evidence_quote_must_be_verbatim_nonempty(self) -> None:
        doc = _good_map()
        doc["entries"][0]["evidence"][0]["quote"] = ""
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("verbatim" in e for e in errors))


class FitResultValidationTest(unittest.TestCase):
    def _good_map(self) -> dict:
        return {
            "schema": "veto/evidence-map/v1",
            "job_id": SYNTHETIC_JOB_ID,
            "job_title": "Senior Widget Engineer (synthetic)",
            "evidence_source_hash": "sha256:0123456789abcdef",
            "entries": [],
        }

    def _good(self) -> dict:
        return {
            "schema": "veto/fit-result/v1",
            "job_id": SYNTHETIC_JOB_ID,
            "job_title": "Senior Widget Engineer (synthetic)",
            "fit_score": 72,
            # Five-factor point sub-scores: skills<=50, seniority/salary/
            # location<=15, recency<=5, summing to fit_score.
            "components": {
                "skills": 36.0,
                "seniority": 12.0,
                "salary": 10.0,
                "location": 10.0,
                "recency": 4.0,
            },
            "provenance": {"kind": "static", "n": 0},
            "limitations": ["English-only keyword lists"],
            "evidence_map": self._good_map(),
        }

    def test_valid_passes(self) -> None:
        self.assertEqual(schemas.validate_fit_result(self._good()), [])

    def test_provenance_kinds_accepted(self) -> None:
        for kind, n in (("static", 0), ("personalized", 12), ("cohort-informed", 40)):
            doc = self._good()
            doc["provenance"] = {"kind": kind, "n": n}
            self.assertEqual(schemas.validate_fit_result(doc), [], kind)

    def test_static_with_sample_rejected(self) -> None:
        doc = self._good()
        doc["provenance"] = {"kind": "static", "n": 5}
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("must be 0" in e for e in errors))

    def test_unknown_kind_rejected(self) -> None:
        doc = self._good()
        doc["provenance"] = {"kind": "vibes", "n": 0}
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("provenance.kind" in e for e in errors))

    def test_score_bounds(self) -> None:
        doc = self._good()
        doc["fit_score"] = 101
        self.assertTrue(schemas.validate_fit_result(doc))


class ShareCardScrubTest(unittest.TestCase):
    #: SYNTHETIC FIXTURE — fake resume/JD text used only to anchor the
    #: scrub test. Nothing here is real.
    RAW_RESUME = (
        "Jane Synthetic\n"
        "6 years Python building data pipelines at Fictional Corp\n"
        "Reduced ETL runtime 40% with a bespoke orchestration layer\n"
    )
    RAW_JD = (
        "Synthetic Systems Inc. seeks a Senior Widget Engineer.\n"
        "5+ years Python. Fast-paced environment. Competitive salary.\n"
    )

    def _card(self) -> dict:
        return {
            "schema": "veto/share-card/v1",
            "job_id": SYNTHETIC_JOB_ID,
            "fit_score": 72,
            "components": {"skills": 80},
            "provenance": {"kind": "static", "n": 0},
            "limitations": ["English-only keyword lists"],
            "evidence": [
                {"requirement": "5+ years Python", "status": "supported",
                 "quote": "6 years Python building data pipelines"}
            ],
        }

    def test_clean_card_passes_scrub(self) -> None:
        errors = schemas.validate_share_card(
            self._card(), raw_resume_text=self.RAW_RESUME, raw_job_text=self.RAW_JD,
            allowed_quotes=["6 years Python building data pipelines"],
        )
        self.assertEqual(errors, [])

    def test_unselected_fragment_fails_scrub(self) -> None:
        # Same card, but the fragment was NOT user-selected: the
        # sliding-window check catches the verbatim 24+ char run.
        errors = schemas.validate_share_card(
            self._card(), raw_resume_text=self.RAW_RESUME, raw_job_text=self.RAW_JD,
        )
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_raw_resume_line_in_payload_fails(self) -> None:
        card = self._card()
        card["evidence"][0]["quote"] = (
            "Reduced ETL runtime 40% with a bespoke orchestration layer"
        )
        errors = schemas.validate_share_card(
            card, raw_resume_text=self.RAW_RESUME, raw_job_text=self.RAW_JD
        )
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_raw_jd_line_in_payload_fails(self) -> None:
        card = self._card()
        card["jd_note"] = "Synthetic Systems Inc. seeks a Senior Widget Engineer."
        errors = schemas.validate_share_card(
            card, raw_resume_text=self.RAW_RESUME, raw_job_text=self.RAW_JD
        )
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_long_quote_rejected(self) -> None:
        card = self._card()
        card["evidence"][0]["quote"] = "x" * 250
        errors = schemas.validate_share_card(
            card, raw_resume_text=self.RAW_RESUME, raw_job_text=self.RAW_JD,
            max_quote_chars=200,
        )
        self.assertTrue(any("exceeds 200" in e for e in errors))

    def test_wrong_schema_rejected(self) -> None:
        card = self._card()
        card["schema"] = "veto/share-card/v0"
        errors = schemas.validate_share_card(card)
        self.assertTrue(errors)

    def test_scrub_without_raw_inputs_refuses_to_certify(self) -> None:
        # A scrub with nothing to check against would be a silent pass;
        # the privacy gate must fail closed.
        errors = schemas.validate_share_card(self._card())
        self.assertTrue(any("without raw resume/job text" in e for e in errors))
        errors = schemas.validate_share_card(
            self._card(), raw_resume_text="   ", raw_job_text=""
        )
        self.assertTrue(any("without raw resume/job text" in e for e in errors))


class HashHelpersTest(unittest.TestCase):
    def test_canonical_json_deterministic(self) -> None:
        doc = {"b": 1, "a": [3, 2]}
        self.assertEqual(schemas.canonical_json(doc), '{"a":[3,2],"b":1}')

    def test_sha256_hex_length(self) -> None:
        self.assertEqual(len(schemas.sha256_hex("hello")), 16)
        self.assertEqual(len(schemas.sha256_hex("hello", 12)), 12)


class ComponentsContractTest(unittest.TestCase):
    """Item 3: components shape is pinned and validated."""

    def _good(self) -> dict:
        return FitResultValidationTest()._good()

    def test_five_factor_breakdown_passes(self) -> None:
        self.assertEqual(schemas.validate_fit_result(self._good()), [])

    def test_missing_factor_rejected(self) -> None:
        doc = self._good()
        del doc["components"]["recency"]
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("components.recency is required" in e for e in errors))

    def test_component_over_weight_rejected(self) -> None:
        doc = self._good()
        doc["components"]["skills"] = 101  # point contributions live in [0, 100]
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("point contribution in [0, 100]" in e for e in errors))

    def test_components_must_sum_to_fit_score(self) -> None:
        doc = self._good()
        doc["fit_score"] = 90  # components sum to 72
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("do not explain fit_score" in e for e in errors))

    def test_components_tolerate_rounding(self) -> None:
        # Producer rounds each component to 1 decimal; the sum may be a
        # hair off the integer score.
        doc = self._good()
        doc["components"] = {
            "skills": 35.9, "seniority": 12.1, "salary": 10.0,
            "location": 10.0, "recency": 4.0,
        }
        doc["fit_score"] = 72
        self.assertEqual(schemas.validate_fit_result(doc), [])

    def test_unknown_component_key_rejected(self) -> None:
        doc = self._good()
        doc["components"]["vibes"] = 5
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("unexpected axis 'vibes'" in e for e in errors))

    def test_extension_component_key_allowed(self) -> None:
        doc = self._good()
        doc["components"]["x_custom_factor"] = 5
        # x_-prefixed keys are producer extensions: ignored, not summed.
        self.assertEqual(schemas.validate_fit_result(doc), [])

    def test_non_numeric_component_rejected(self) -> None:
        doc = self._good()
        doc["components"]["skills"] = "high"
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("point contribution" in e for e in errors))


class EvidenceMapReferenceTest(unittest.TestCase):
    """Item 2: fit-result carries the EMBEDDED evidence map, validated in place.

    Docstring, validator, and producer agree: the envelope embeds the
    whole ``veto/evidence-map/v1`` document (not a pointer), and the
    validator checks the embedded map whenever present.
    """

    def _good(self) -> dict:
        return FitResultValidationTest()._good()

    def test_absent_evidence_map_allowed_validated_when_present(self) -> None:
        doc = self._good()
        del doc["evidence_map"]
        # The contract documents the embedded map; the validator checks
        # it whenever the envelope carries one.
        self.assertEqual(schemas.validate_fit_result(doc), [])

    def test_invalid_embedded_map_rejected_with_prefix(self) -> None:
        doc = self._good()
        doc["evidence_map"]["entries"] = [{"status": "maybe"}]
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any(e.startswith("evidence_map:") for e in errors))

    def test_unknown_top_level_key_rejected(self) -> None:
        doc = self._good()
        doc["freeform_note"] = "a free-text assertion smuggled in"
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("unexpected key 'freeform_note'" in e for e in errors))

    def test_extension_top_level_key_allowed(self) -> None:
        doc = self._good()
        doc["x_producer_note"] = "producer-defined extension"
        self.assertEqual(schemas.validate_fit_result(doc), [])


class TriStateExtensionDisciplineTest(unittest.TestCase):
    """Item 5: the tri-state's no-free-text-assertion rule is contractual."""

    def test_free_text_key_on_grill_question_entry_rejected(self) -> None:
        doc = _good_map()
        entry = doc["entries"][0]
        entry["status"] = "grill_question"
        entry["evidence"] = []
        entry["grill_question_id"] = "grill-q-7"
        entry["assertion"] = "probably qualified (free-text smuggling)"
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("unexpected key 'assertion'" in e for e in errors))

    def test_unknown_key_on_evidence_item_rejected(self) -> None:
        doc = _good_map()
        doc["entries"][0]["evidence"][0]["confidence_note"] = "looks good"
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("unexpected key 'confidence_note'" in e for e in errors))

    def test_x_prefixed_keys_allowed_on_entries(self) -> None:
        doc = _good_map()
        doc["entries"][0]["x_reviewer"] = "extension, not an assertion"
        doc["entries"][0]["evidence"][0]["x_span"] = [3, 19]
        self.assertEqual(schemas.validate_evidence_map(doc), [])


class EvidenceBindingTest(unittest.TestCase):
    """Item 4: evidence_source_hash is verifiable, not decorative.

    The format is enforced by ``validate_evidence_map`` (``sha256:`` +
    16 hex chars); the binding itself is recomputed and compared by
    ``verify_evidence_binding`` so stale maps cannot be silently
    reused.
    """

    SOURCE = "Jane Synthetic\n6 years Python building data pipelines\n"

    def _map(self, source: str) -> dict:
        return {
            "schema": "veto/evidence-map/v1",
            "job_id": SYNTHETIC_JOB_ID,
            "evidence_source_hash": schemas.evidence_source_hash_for(source),
            "entries": [],
        }

    def test_hash_scheme_is_documented_and_stable(self) -> None:
        h = schemas.evidence_source_hash_for(self.SOURCE)
        self.assertTrue(h.startswith("sha256:"))
        self.assertEqual(len(h), len("sha256:") + 16)
        self.assertEqual(h, schemas.evidence_source_hash_for(self.SOURCE))

    def test_matching_source_verifies(self) -> None:
        self.assertEqual(
            schemas.verify_evidence_binding(self._map(self.SOURCE), self.SOURCE),
            [],
        )

    def test_edited_source_invalidates_binding(self) -> None:
        errors = schemas.verify_evidence_binding(
            self._map(self.SOURCE), self.SOURCE + "Extra line.\n"
        )
        self.assertTrue(any("stale" in e for e in errors))

    def test_quote_not_in_source_rejected(self) -> None:
        doc = self._map(self.SOURCE)
        doc["entries"] = [
            {
                "requirement": {
                    "text": "Python",
                    "source_quote": "Python required",
                    "kind": "must_have",
                },
                "status": "supported",
                "evidence": [
                    {
                        "evidence_id": "ev_001",
                        "quote": "10 years Rust at Nowhere Inc",
                        "profile_field": "experience[0]",
                        "quantified": True,
                    }
                ],
            }
        ]
        errors = schemas.verify_evidence_binding(doc, self.SOURCE)
        self.assertTrue(any("not a verbatim substring" in e for e in errors))

    def test_malformed_recorded_hash_fails_validation(self) -> None:
        doc = self._map(self.SOURCE)
        doc["evidence_source_hash"] = "not-a-hash"
        errors = schemas.validate_evidence_map(doc)
        self.assertTrue(any("evidence_source_hash" in e for e in errors))

    def test_missing_source_text_fails_closed(self) -> None:
        errors = schemas.verify_evidence_binding(self._map(self.SOURCE), None)
        self.assertTrue(errors)

    def test_non_dict_doc_fails_closed(self) -> None:
        errors = schemas.verify_evidence_binding(None, self.SOURCE)
        self.assertTrue(errors)


class AdversarialScrubTest(unittest.TestCase):
    """Item 1 (BLOCKER): the 24-char window check is alignment-independent.

    A verbatim 24-char run smuggled into a non-evidence field must be
    caught at EVERY alignment offset 0-13. The old step-12 stride let
    runs misaligned to the grid pass; step 1 closes that.
    """

    RAW_RESUME = (
        "Jane Synthetic\n"
        "6 years Python building data pipelines at Fictional Corp\n"
        "Reduced ETL runtime 40% with a bespoke orchestration layer\n"
    )
    RAW_JD = (
        "Synthetic Systems Inc. seeks a Senior Widget Engineer.\n"
        "5+ years Python. Fast-paced environment. Competitive salary.\n"
    )
    #: 24-char verbatim run of the raw resume (boundary length).
    FRAG24 = "years Python building da"
    #: 30-char verbatim run of the raw resume.
    FRAG30 = "years Python building data pip"

    def setUp(self) -> None:
        self.assertIn(self.FRAG24, self.RAW_RESUME)
        self.assertEqual(len(self.FRAG24), 24)
        self.assertIn(self.FRAG30, self.RAW_RESUME)
        self.assertEqual(len(self.FRAG30), 30)

    def _card(self) -> dict:
        return {
            "schema": "veto/share-card/v1",
            "job_id": SYNTHETIC_JOB_ID,
            "limitations": ["All synthetic fixture text"],
            "evidence": [
                {"requirement": "5+ years Python", "status": "supported",
                 "quote": "6 years Python building data pipelines"}
            ],
        }

    def _scrub(self, card: dict) -> list[str]:
        return schemas.validate_share_card(
            card,
            raw_resume_text=self.RAW_RESUME,
            raw_job_text=self.RAW_JD,
            allowed_quotes=["6 years Python building data pipelines"],
        )

    def test_24char_run_caught_at_every_offset_in_every_field(self) -> None:
        for offset in range(14):  # 0..13: every step-12 misalignment
            for field in ("limitations", "job_title", "jd_verdict"):
                card = self._card()
                smuggled = "Q" * offset + self.FRAG24 + "Z" * 7
                if field == "limitations":
                    card["limitations"] = ["All synthetic fixture text", smuggled]
                elif field == "job_title":
                    card["job_title"] = smuggled
                else:
                    card["jd_verdict"] = {
                        "verdict": "synthetic",
                        "reasons": [{"reason": smuggled}],
                    }
                errors = self._scrub(card)
                self.assertTrue(
                    any("privacy violation" in e for e in errors),
                    f"leak evaded scrub: offset={offset} field={field}",
                )

    def test_30char_misaligned_run_caught(self) -> None:
        card = self._card()
        card["limitations"] = ["Q" * 7 + self.FRAG30 + "Z" * 3]
        errors = self._scrub(card)
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_reviewer_exact_attack_28char_at_raw_offset_1(self) -> None:
        # The blind review's exact attack: a 28-char verbatim run
        # starting at RAW offset 1 — misaligned to the old step-12 grid
        # of raw-text windows, which let it pass. The payload-side
        # step-1 walk catches it regardless of alignment.
        line = "6 years Python building data pipelines at Fictional Corp"
        run28 = line[1:29]
        self.assertEqual(len(run28), 28)
        self.assertIn(run28, self.RAW_RESUME)
        card = self._card()
        card["limitations"] = ["pad" + run28 + "pad"]
        errors = self._scrub(card)
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_leak_smuggled_in_dict_key_caught_at_every_offset(self) -> None:
        # Dict keys are payload strings too: a verbatim run hiding in
        # a key must trip the scrub at every alignment. (Probe found
        # the old walker skipped keys entirely.)
        line = "6 years Python building data pipelines at Fictional Corp"
        for offset in range(13):
            run = line[offset : offset + 24]
            self.assertIn(run, self.RAW_RESUME)
            card = self._card()
            card["x_notes"] = {">" + run + "<": "value"}
            errors = self._scrub(card)
            self.assertTrue(
                any("privacy violation" in e for e in errors),
                f"key leak evaded scrub: offset={offset}",
            )

    def test_reflowed_line_break_does_not_hide_leak(self) -> None:
        # Raw line "6 years Python building data pipelines at Fictional
        # Corp" re-flowed with spaces instead of the newline still trips
        # the whitespace-collapsed corpus check.
        card = self._card()
        card["job_title"] = (
            "X" * 5 + "pipelines at Fictional Corp Reduced ETL runtime"
        )
        errors = self._scrub(card)
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_clean_card_still_passes(self) -> None:
        self.assertEqual(self._scrub(self._card()), [])


class NestedExtensionPolicyTest(unittest.TestCase):
    """The additionalProperties policy is contractual at EVERY object
    level: an undeclared non-``x_`` key on a nested object (provenance,
    jd_verdict, evidence_summary, share-card evidence items) is a
    violation, not a convention."""

    def _scrub(self, card: dict) -> list[str]:
        return schemas.validate_share_card(
            card,
            raw_resume_text=ShareCardScrubTest.RAW_RESUME,
            raw_job_text=ShareCardScrubTest.RAW_JD,
            allowed_quotes=["6 years Python building data pipelines"],
        )

    def test_fit_provenance_unknown_key_rejected(self) -> None:
        doc = FitResultValidationTest()._good()
        doc["provenance"]["analyst_note"] = "free-text smuggling"
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(
            any("unexpected key 'analyst_note'" in e for e in errors)
        )

    def test_fit_provenance_x_key_allowed(self) -> None:
        doc = FitResultValidationTest()._good()
        doc["provenance"]["x_sampler"] = "producer extension"
        self.assertEqual(schemas.validate_fit_result(doc), [])

    def test_fit_jd_verdict_unknown_key_rejected(self) -> None:
        doc = FitResultValidationTest()._good()
        doc["jd_verdict"] = {
            "verdict": "mixed",
            "score": 3,
            "reasons": [],
            "note": "smuggled",
        }
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(any("unexpected key 'note'" in e for e in errors))

    def test_fit_evidence_summary_unknown_key_rejected(self) -> None:
        doc = FitResultValidationTest()._good()
        doc["evidence_summary"] = {
            "supported": 1,
            "gaps": 0,
            "grill_questions": 0,
            "comment": "smuggled",
        }
        errors = schemas.validate_fit_result(doc)
        self.assertTrue(
            any("unexpected key 'comment'" in e for e in errors)
        )

    def test_fit_nested_x_keys_allowed(self) -> None:
        doc = FitResultValidationTest()._good()
        doc["jd_verdict"] = {
            "verdict": "mixed",
            "score": 3,
            "reasons": [],
            "x_reader": "v2",
        }
        doc["evidence_summary"] = {
            "supported": 1,
            "gaps": 0,
            "grill_questions": 0,
            "x_source": "decoder",
        }
        self.assertEqual(schemas.validate_fit_result(doc), [])

    def test_share_card_provenance_unknown_key_rejected(self) -> None:
        card = ShareCardScrubTest()._card()
        card["provenance"] = {"kind": "static", "n": 0, "note": "smuggled"}
        errors = self._scrub(card)
        self.assertTrue(any("unexpected key 'note'" in e for e in errors))

    def test_share_card_jd_verdict_score_rejected(self) -> None:
        # The share-card jd_verdict is the reduced shape: no ``score``
        # (scores stay on the private fit result).
        card = ShareCardScrubTest()._card()
        card["jd_verdict"] = {"verdict": "mixed", "reasons": [], "score": 4}
        errors = self._scrub(card)
        self.assertTrue(any("unexpected key 'score'" in e for e in errors))

    def test_share_card_evidence_item_unknown_key_rejected(self) -> None:
        card = ShareCardScrubTest()._card()
        card["evidence"][0]["confidence"] = "high (smuggled assertion)"
        errors = self._scrub(card)
        self.assertTrue(
            any("unexpected key 'confidence'" in e for e in errors)
        )

    def test_share_card_nested_x_keys_allowed(self) -> None:
        card = ShareCardScrubTest()._card()
        card["provenance"]["x_sampler"] = "ok"
        card["jd_verdict"] = {"verdict": "mixed", "reasons": [], "x_v": 2}
        card["evidence"][0]["x_span"] = [3, 19]
        self.assertEqual(self._scrub(card), [])

    def test_non_dict_nested_objects_do_not_raise(self) -> None:
        doc = FitResultValidationTest()._good()
        doc["jd_verdict"] = "mixed"
        doc["evidence_summary"] = ["supported"]
        self.assertIsInstance(schemas.validate_fit_result(doc), list)
        card = ShareCardScrubTest()._card()
        card["jd_verdict"] = "mixed"
        card["evidence"] = ["not-a-dict"]
        self.assertIsInstance(self._scrub(card), list)


class NeverRaisesTest(unittest.TestCase):
    """Item 6: the 'never raise on malformed input' guarantee is real."""

    def _card(self) -> dict:
        return {"schema": "veto/share-card/v1", "limitations": ["x"] * 3}

    def test_none_payload_no_raise(self) -> None:
        self.assertTrue(schemas.validate_share_card(None))

    def test_none_raw_inputs_fail_closed_no_raise(self) -> None:
        errors = schemas.validate_share_card(
            self._card(), raw_resume_text=None, raw_job_text=12345
        )
        self.assertTrue(any("must be strings" in e for e in errors))

    def test_non_string_allowed_quotes_ignored_no_raise(self) -> None:
        errors = schemas.validate_share_card(
            self._card(),
            raw_resume_text="x" * 100,
            allowed_quotes=[None, 123, ""],
        )
        self.assertIsInstance(errors, list)

    def test_deeply_nested_payload_no_recursion_error(self) -> None:
        nested: Any = ["clean leaf"]
        for _ in range(3000):
            nested = [nested]
        card = self._card()
        card["deep"] = nested
        errors = schemas.validate_share_card(card, raw_resume_text="y" * 100)
        self.assertIsInstance(errors, list)

    def test_deeply_nested_quotes_no_recursion_error(self) -> None:
        nested: Any = {"quote": "ok"}
        for _ in range(3000):
            nested = {"child": nested, "quote": "ok"}
        card = self._card()
        card["deep"] = nested
        errors = schemas.validate_share_card(card, raw_resume_text="y" * 100)
        self.assertIsInstance(errors, list)

    def test_validators_reject_non_dicts_without_raising(self) -> None:
        self.assertTrue(schemas.validate_evidence_map(None))
        self.assertTrue(schemas.validate_evidence_map("nope"))
        self.assertTrue(schemas.validate_fit_result(None))
        self.assertTrue(schemas.validate_fit_result([1, 2]))

    def test_non_string_dict_keys_fail_gracefully(self) -> None:
        # Unhashable-hostile shapes (int keys, set/tuple values) are
        # violations or skips — never exceptions.
        card = {1: "x", "schema": "veto/share-card/v1"}
        errors = schemas.validate_share_card(card, raw_resume_text="y" * 100)
        self.assertTrue(any("unexpected key 1" in e for e in errors))
        card2 = {"schema": "veto/share-card/v1", "x_s": {"a", "b"}}
        self.assertIsInstance(
            schemas.validate_share_card(card2, raw_resume_text="y" * 100), list
        )

    def test_bool_max_quote_chars_rejected_no_raise(self) -> None:
        errors = schemas.validate_share_card(
            self._card(), raw_resume_text="y" * 100, max_quote_chars=True
        )
        self.assertTrue(any("max_quote_chars must be an integer" in e for e in errors))

    def test_canonical_json_mixed_key_types_no_raise(self) -> None:
        # sort_keys=True would raise TypeError on mixed-type keys; the
        # fallback stringifies keys instead.
        self.assertIsInstance(schemas.canonical_json({1: "a", "b": 2}), str)

    def test_canonical_json_never_raises(self) -> None:
        self.assertIsInstance(schemas.canonical_json(object()), str)
        circular: dict = {}
        circular["self"] = circular
        self.assertIsInstance(schemas.canonical_json(circular), str)
        # Deterministic for normal input (unchanged behavior).
        self.assertEqual(schemas.canonical_json({"b": 1, "a": [3, 2]}), '{"a":[3,2],"b":1}')

    def test_sha256_hex_valid_inputs(self) -> None:
        self.assertEqual(len(schemas.sha256_hex("hello")), 16)
        self.assertEqual(len(schemas.sha256_hex("hello", 12)), 12)
        # Deterministic for the evidence-hash scheme.
        self.assertEqual(
            schemas.sha256_hex("abc"), schemas.sha256_hex("abc")
        )


if __name__ == "__main__":
    unittest.main()
