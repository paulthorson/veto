"""Tests for Initiative 05 cross-initiative schema contracts.

All fixtures are synthetic and labeled as such; no real person,
employer, or achievement is referenced.
"""

from __future__ import annotations

import unittest

from initiatives.i05 import contracts


def _link(**kw):
    base = {
        "link_id": "lnk_synth_1",
        "requirement_id": "req_synth_python",
        "evidence_id": "ev_synth_1",
        "quote": "shipped python services",
        "confidence": 0.9,
    }
    base.update(kw)
    return base


def _evidence(**kw):
    base = {
        "evidence_id": "ev_synth_1",
        "kind": "achievement",
        "text": "Synthetic fixture: shipped python services.",
        "source": "experience[0].bullets[0]",
        "approved": True,
    }
    base.update(kw)
    return base


def _provenance(**kw):
    base = {
        "score_id": "score_synth_1",
        "factors": {"skills": {"weight": 0.5, "value": 0.8,
                               "evidence_ids": ["ev_synth_1"]}},
        "inputs_hash": "deadbeef",
        "created_at": "2026-09-13T00:00:00+00:00",
    }
    base.update(kw)
    return base


class ContractVersionTest(unittest.TestCase):
    def test_version_is_pinned(self):
        self.assertEqual(contracts.CONTRACT_VERSION, "i05-contracts/1")


class LinkContractTest(unittest.TestCase):
    def test_valid_link_passes(self):
        self.assertEqual(contracts.validate_link(_link()), [])

    def test_missing_fields_reported(self):
        problems = contracts.validate_link({"link_id": "x"})
        self.assertTrue(any("requirement_id" in p for p in problems))
        self.assertTrue(any("evidence_id" in p for p in problems))

    def test_non_dict_rejected(self):
        self.assertTrue(contracts.validate_link("nope"))

    def test_confidence_bounds_enforced(self):
        self.assertTrue(contracts.validate_link(_link(confidence=1.5)))
        self.assertTrue(contracts.validate_link(_link(confidence=-0.1)))
        self.assertEqual(contracts.validate_link(_link(confidence=0.0)), [])
        self.assertEqual(contracts.validate_link(_link(confidence=1.0)), [])
        self.assertTrue(contracts.validate_link(_link(confidence="high")))

    def test_normalize_stamps_schema_version(self):
        out = contracts.normalize_link(_link())
        self.assertEqual(out["schema"], contracts.CONTRACT_VERSION)

    def test_normalize_rejects_bad_link(self):
        with self.assertRaises(ValueError):
            contracts.normalize_link({"link_id": "x"})

    def test_links_for_requirement_filters_and_sorts(self):
        links = [
            _link(link_id="a", requirement_id="r1", confidence=0.4),
            _link(link_id="b", requirement_id="r1", confidence=0.9),
            _link(link_id="c", requirement_id="r2", confidence=0.99),
            {"bogus": True},
        ]
        out = contracts.links_for_requirement(links, "r1")
        self.assertEqual([l["link_id"] for l in out], ["b", "a"])


class EvidenceContractTest(unittest.TestCase):
    def test_valid_item_passes(self):
        self.assertEqual(contracts.validate_evidence(_evidence()), [])

    def test_missing_fields_reported(self):
        problems = contracts.validate_evidence({"evidence_id": "x"})
        self.assertTrue(any("kind" in p for p in problems))

    def test_unknown_kind_rejected(self):
        self.assertTrue(contracts.validate_evidence(
            _evidence(kind="rumor")))

    def test_approved_evidence_filters(self):
        items = [
            _evidence(evidence_id="ok"),
            _evidence(evidence_id="draft", approved=False),
            _evidence(evidence_id="bad", kind="rumor"),
        ]
        out = contracts.approved_evidence(items)
        self.assertEqual([i["evidence_id"] for i in out], ["ok"])

    def test_approved_evidence_uses_truthy_gate(self):
        # RE-review MINOR: approved_evidence used `is True` identity
        # while trace_report uses a truthy gate — an item with
        # approved=1 was "approved" in trace_report but dropped here.
        # Both gates are now truthy.
        items = [
            _evidence(evidence_id="int_one", approved=1),
            _evidence(evidence_id="int_zero", approved=0),
            _evidence(evidence_id="empty_str", approved=""),
        ]
        out = contracts.approved_evidence(items)
        self.assertEqual([i["evidence_id"] for i in out], ["int_one"])

    def test_normalize_rejects_bad_item(self):
        with self.assertRaises(ValueError):
            contracts.normalize_evidence({"evidence_id": "x"})


class ProvenanceContractTest(unittest.TestCase):
    def test_valid_record_passes(self):
        self.assertEqual(contracts.validate_provenance(_provenance()), [])

    def test_missing_fields_reported(self):
        problems = contracts.validate_provenance({"score_id": "x"})
        self.assertTrue(any("factors" in p for p in problems))


class SchemaPinningTest(unittest.TestCase):
    """The module contract promises a schema bump "fails loudly instead
    of misreading fields" — the validators now enforce the stamp."""

    def test_schema_mismatch_fails_loudly(self):
        self.assertTrue(any("schema" in p for p in
                            contracts.validate_link(
                                _link(schema="other/9"))))
        self.assertTrue(any("schema" in p for p in
                            contracts.validate_evidence(
                                _evidence(schema="other/9"))))
        self.assertTrue(any("schema" in p for p in
                            contracts.validate_provenance(
                                _provenance(schema="other/9"))))

    def test_normalize_rejects_mismatched_schema(self):
        with self.assertRaises(ValueError):
            contracts.normalize_link(_link(schema="other/9"))

    def test_unstamped_records_still_pass(self):
        # A1/A2: plain dicts from teams that have not landed yet carry
        # no stamp and must keep validating.
        self.assertEqual(contracts.validate_link(_link()), [])
        self.assertEqual(contracts.validate_evidence(_evidence()), [])
        self.assertEqual(contracts.validate_provenance(_provenance()), [])

    def test_current_schema_stamp_passes(self):
        self.assertEqual(
            contracts.validate_link(
                _link(schema=contracts.CONTRACT_VERSION)), [])


if __name__ == "__main__":
    unittest.main()
