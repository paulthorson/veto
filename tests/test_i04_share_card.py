#!/usr/bin/env python3
"""Tests for ``initiatives.i04.share_card`` (Epic 5) — privacy-critical.

All fixtures are synthetic and clearly labeled; no real employers,
people, or achievements appear anywhere in this file.

The privacy tests below are adversarial: they try to smuggle raw
resume/JD content into the card through every field and assert the
scrub refuses to build.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from initiatives.i04 import fit_explain, share_card  # noqa: E402
from initiatives.i04.schemas import SHARE_CARD_SCHEMA, validate_share_card  # noqa: E402

#: SYNTHETIC FIXTURES — fake resume/JD. Nothing here is real.
RAW_RESUME = """Jane Synthetic
jane.synthetic@example.com | (555) 123-4567

Experience
Senior Software Engineer, Fictional Corp
- 6 years Python building data pipelines; reduced ETL runtime 40%
- Ran Kubernetes in production across 12 services

Skills
Python, Kubernetes, Docker
"""

RAW_JD = """Senior Widget Engineer — Synthetic Systems Inc.
$140k-$180k salary range. Confidential internal hiring memo: req #4491.

Requirements:
- 5+ years of Python experience
- Experience with Kubernetes in production
"""


def _fit_result() -> dict:
    outcome = fit_explain.fit_explain(
        RAW_RESUME, RAW_JD, "synthetic-job-001",
        job_title="Senior Widget Engineer (synthetic)",
    )
    assert outcome["validation_errors"] == [], outcome["validation_errors"]
    return outcome["fit_result"]


class BuildShareCardTest(unittest.TestCase):
    def test_card_builds_with_selected_evidence(self) -> None:
        result = _fit_result()
        python_ids = [
            item["evidence_id"]
            for entry in result["evidence_map"]["entries"]
            if entry["requirement"]["text"] == "python"
            for item in entry["evidence"]
        ]
        self.assertTrue(python_ids)
        card = share_card.build_share_card(
            result, python_ids[:1], RAW_RESUME, RAW_JD
        )
        self.assertEqual(card["schema"], SHARE_CARD_SCHEMA)
        self.assertEqual(len(card["evidence"]), 1)
        self.assertIn("h", card)
        # Selected fragment is present (user opted in)...
        self.assertIn("Python", card["evidence"][0]["quote"])
        # ...but the quote is capped.
        self.assertLessEqual(len(card["evidence"][0]["quote"]), 200)

    def test_no_excerpt_field_on_fit_cards(self) -> None:
        result = _fit_result()
        card = share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)
        self.assertNotIn("x", card)
        self.assertNotIn("excerpt", str(card).lower())

    def test_raw_content_smuggled_via_limitations_rejected(self) -> None:
        # Adversarial: raw resume content smuggled through a
        # non-evidence field (not user-selected) must fail the scrub.
        result = _fit_result()
        result["limitations"] = list(result["limitations"]) + [
            "Jane Synthetic jane.synthetic@example.com | (555) 123-4567"
        ]
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_raw_content_smuggled_via_jd_verdict_rejected(self) -> None:
        # Adversarial: raw JD content smuggled through the verdict
        # reasons must fail the scrub.
        result = _fit_result()
        result["jd_verdict"]["reasons"] = [
            {"reason": "Confidential internal hiring memo: req #4491."}
        ]
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_scrub_without_raw_inputs_fails_closed(self) -> None:
        result = _fit_result()
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], "", "")

    def test_unknown_evidence_id_selects_nothing(self) -> None:
        result = _fit_result()
        card = share_card.build_share_card(
            result, ["ev_does_not_exist"], RAW_RESUME, RAW_JD
        )
        self.assertEqual(card["evidence"], [])

    def test_envelope_fit_score_none_rejected(self) -> None:
        # Envelope validation (C3): a malformed fit result must fail
        # closed before any card is built.
        result = _fit_result()
        result["fit_score"] = None
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_envelope_fit_score_out_of_range_rejected(self) -> None:
        result = _fit_result()
        result["fit_score"] = 500
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_envelope_bad_provenance_kind_rejected(self) -> None:
        result = _fit_result()
        result["provenance"] = {"kind": "bogus", "n": 0}
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_unhashable_evidence_id_fails_closed(self) -> None:
        # C4: a non-string (unhashable) evidence_id must raise
        # ShareCardPrivacyError, never a raw TypeError.
        result = _fit_result()
        first_entry = result["evidence_map"]["entries"][0]
        first_entry["evidence"].append(
            {"evidence_id": {"bad": "dict"}, "quote": "innocuous quote"}
        )
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_raw_content_smuggled_via_job_title_rejected(self) -> None:
        # Adversarial smuggling probe through a non-evidence field
        # (coordinated with the fixed window scrub in schemas.py).
        result = _fit_result()
        result["job_title"] = (
            "Senior Widget Engineer — Synthetic Systems Inc. "
            "$140k-$180k salary range. Confidential internal hiring "
            "memo: req #4491."
        )
        with self.assertRaises(share_card.ShareCardPrivacyError):
            share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)

    def test_privacy_note_present(self) -> None:
        result = _fit_result()
        card = share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)
        self.assertIn("No raw resume", card["privacy_note"])


class ShareLinkCodecTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        result = _fit_result()
        card = share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)
        fragment = share_card.encode_share_payload(card)
        self.assertNotIn("=", fragment)
        decoded = share_card.decode_share_payload(fragment)
        self.assertEqual(decoded["fit_score"], card["fit_score"])
        self.assertEqual(decoded["h"], card["h"])

    def test_injected_unknown_key_rejected(self) -> None:
        # C2: decode a valid fragment, inject an unknown key, re-encode
        # with the ORIGINAL hash (still valid — the hash only covers
        # canonical keys). Decoding must reject the unknown key.
        result = _fit_result()
        card = share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)
        # Forge the attack fragment directly: unknown key included,
        # original "h" untouched (it stays valid because the hash only
        # covers canonical keys).
        wire = {key: card[key] for key in share_card._KEY_ORDER}
        wire["injected"] = "attacker-controlled value"
        wire["h"] = card["h"]
        raw = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
        fragment = (
            base64.urlsafe_b64encode(raw.encode("utf-8"))
            .rstrip(b"=")
            .decode("ascii")
        )
        with self.assertRaises(ValueError):
            share_card.decode_share_payload(fragment)

    def test_tampered_fragment_rejected(self) -> None:
        result = _fit_result()
        card = share_card.build_share_card(result, [], RAW_RESUME, RAW_JD)
        fragment = share_card.encode_share_payload(card)
        tampered = ("A" if fragment[0] != "A" else "B") + fragment[1:]
        with self.assertRaises(ValueError):
            share_card.decode_share_payload(tampered)

    def test_encode_without_hash_refused(self) -> None:
        with self.assertRaises(ValueError):
            share_card.encode_share_payload({"schema": SHARE_CARD_SCHEMA})

    def test_malformed_fragment_rejected(self) -> None:
        with self.assertRaises(ValueError):
            share_card.decode_share_payload("!!!not-base64!!!")


class ScrubSemanticsTest(unittest.TestCase):
    def test_selected_fragment_allowed_unselected_blocked(self) -> None:
        payload = {
            "schema": SHARE_CARD_SCHEMA,
            "evidence": [{"quote": "6 years Python building data pipelines"}],
            "sneaky": "Ran Kubernetes in production across 12 services",
        }
        errors = validate_share_card(
            payload,
            raw_resume_text=RAW_RESUME,
            raw_job_text=RAW_JD,
            allowed_quotes=["6 years Python building data pipelines"],
        )
        self.assertTrue(any("privacy violation" in e for e in errors))

    def test_selected_fragment_alone_passes(self) -> None:
        payload = {
            "schema": SHARE_CARD_SCHEMA,
            "evidence": [{"quote": "6 years Python building data pipelines"}],
        }
        errors = validate_share_card(
            payload,
            raw_resume_text=RAW_RESUME,
            raw_job_text=RAW_JD,
            allowed_quotes=["6 years Python building data pipelines"],
        )
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
