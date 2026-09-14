"""Tests for Initiative 05 evidence library (Epic 2).

All fixtures are synthetic and labeled as such. Every store write goes
to a temp dir — the real ``evidence_library/`` tree is never touched.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from initiatives.i05 import contracts, evidence_library


def _profile() -> dict:
    return {
        "full_name": "Synthetic Fixture",
        "skills": ["Python", "Docker"],
        "linkedin_url": "https://example.com/in/fixture",
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "FixtureCorp",
                "bullets": [
                    "Shipped Python services handling 1M requests a day.",
                    "Mentored junior engineers.",
                ],
            }
        ],
    }


class EvidenceLibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_from_profile_derives_only_real_facts(self):
        items = evidence_library.build_from_profile(
            _profile(), library_dir=self.dir)
        kinds = [i["kind"] for i in items]
        self.assertIn("achievement", kinds)
        self.assertIn("metric", kinds)  # the "1M requests" bullet
        self.assertIn("approved_phrase", kinds)  # skills
        self.assertIn("portfolio_link", kinds)  # linkedin_url
        # Everything starts as a draft.
        self.assertTrue(all(i["approved"] is False for i in items))
        # Profile-derived items are labeled as such.
        self.assertTrue(
            all(i["origin"] == "derived" for i in items))
        # Every item satisfies the 01-02 evidence contract.
        for item in items:
            self.assertEqual(contracts.validate_evidence(item), [])
        # Text is verbatim from the profile — nothing invented.
        texts = [i["text"] for i in items]
        self.assertIn(
            "Shipped Python services handling 1M requests a day.", texts)

    def test_add_item_requires_text_and_source(self):
        with self.assertRaises(ValueError):
            evidence_library.add_item("achievement", "", "experience[0]",
                                      library_dir=self.dir)
        with self.assertRaises(ValueError):
            evidence_library.add_item("achievement", "Did things.", "",
                                      library_dir=self.dir)
        with self.assertRaises(ValueError):
            evidence_library.add_item("rumor", "Did things.", "experience[0]",
                                      library_dir=self.dir)

    def test_add_item_without_profile_is_labeled_asserted(self):
        item = evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        self.assertEqual(item["origin"], "asserted")

    def test_approve_and_use_flow(self):
        item = evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        self.assertFalse(item["approved"])
        evidence_library.approve_item(item["evidence_id"],
                                      library_dir=self.dir)
        got = evidence_library.get_item(item["evidence_id"],
                                        library_dir=self.dir)
        self.assertTrue(got["approved"])
        self.assertIn("approved_at", got)

    def test_edit_resets_approval(self):
        item = evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        evidence_library.approve_item(item["evidence_id"],
                                      library_dir=self.dir)
        evidence_library.update_item(
            item["evidence_id"], "Synthetic win, revised.",
            library_dir=self.dir)
        got = evidence_library.get_item(item["evidence_id"],
                                        library_dir=self.dir)
        self.assertFalse(got["approved"])
        self.assertNotIn("approved_at", got)

    def test_update_downgrades_derived_origin_to_asserted(self):
        # Regression: edited text is caller-supplied, so it can no
        # longer be labeled "derived" ("derived" means text copied
        # verbatim from the profile). The edited item must rank as
        # caller-asserted, never profile-derived, in retrieval.
        items = evidence_library.build_from_profile(
            _profile(), library_dir=self.dir)
        derived = next(
            i for i in items
            if i["kind"] == "achievement"
            and i["text"] == "Shipped Python services handling 1M requests a day.")
        self.assertEqual(derived["origin"], "derived")
        evidence_library.update_item(
            derived["evidence_id"], "Led 200-person org through $50M IPO",
            library_dir=self.dir)
        evidence_library.approve_item(derived["evidence_id"],
                                      library_dir=self.dir)
        got = evidence_library.get_item(derived["evidence_id"],
                                        library_dir=self.dir)
        self.assertEqual(got["origin"], "asserted")
        # find_for_requirement must not present it as profile-derived.
        hits = evidence_library.find_for_requirement(
            "Led 200-person org through $50M IPO",
            library_dir=self.dir)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["origin"], "asserted")
        self.assertEqual(hits[0]["evidence_id"],
                         derived["evidence_id"])

    def test_delete_protects_approved_items(self):
        item = evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        evidence_library.approve_item(item["evidence_id"],
                                      library_dir=self.dir)
        with self.assertRaises(ValueError):
            evidence_library.delete_item(item["evidence_id"],
                                         library_dir=self.dir)
        # Unknown id -> False, not an exception.
        self.assertFalse(
            evidence_library.delete_item("ev_nope", library_dir=self.dir))

    def test_find_returns_only_approved(self):
        a = evidence_library.add_item(
            "achievement", "Shipped Python services.",
            "experience[0].bullets[0]", library_dir=self.dir)
        b = evidence_library.add_item(
            "achievement", "Shipped Python services.",
            "experience[0].bullets[0]", library_dir=self.dir)
        evidence_library.approve_item(a["evidence_id"],
                                      library_dir=self.dir)
        hits = evidence_library.find_for_requirement(
            "need python experience", library_dir=self.dir)
        self.assertEqual([h["evidence_id"] for h in hits],
                         [a["evidence_id"]])
        self.assertNotIn(b["evidence_id"], [h["evidence_id"] for h in hits])

    def test_find_empty_when_nothing_relevant(self):
        evidence_library.build_from_profile(_profile(), library_dir=self.dir)
        # Nothing approved yet -> no hits even with overlap.
        self.assertEqual(
            evidence_library.find_for_requirement(
                "python", library_dir=self.dir), [])
        self.assertEqual(
            evidence_library.find_for_requirement("", library_dir=self.dir),
            [])

    def test_record_use_tracks_provenance(self):
        item = evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        evidence_library.record_use(item["evidence_id"], "var_abc",
                                    library_dir=self.dir)
        got = evidence_library.get_item(item["evidence_id"],
                                        library_dir=self.dir)
        self.assertIn("var_abc", got["used_in"])
        with self.assertRaises(KeyError):
            evidence_library.record_use("ev_nope", "var_abc",
                                        library_dir=self.dir)

    def test_list_filters(self):
        evidence_library.build_from_profile(_profile(), library_dir=self.dir)
        metrics = evidence_library.list_items(kind="metric",
                                              library_dir=self.dir)
        self.assertTrue(metrics)
        self.assertTrue(all(m["kind"] == "metric" for m in metrics))
        self.assertEqual(
            evidence_library.list_items(approved_only=True,
                                        library_dir=self.dir), [])


class EvidenceLibraryEdgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_from_empty_profile_yields_nothing(self):
        self.assertEqual(
            evidence_library.build_from_profile({}, library_dir=self.dir),
            [])
        self.assertEqual(
            evidence_library.list_items(library_dir=self.dir), [])

    def test_approve_unknown_raises(self):
        with self.assertRaises(KeyError):
            evidence_library.approve_item("ev_nope", library_dir=self.dir)


# ---------------------------------------------------------------------------
# Blind-review rework: honesty, corrupt-store, idempotency, links_for_job
# ---------------------------------------------------------------------------


class HonestyModelTest(unittest.TestCase):
    """Major 2: a fabricated source must never look like a derived one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fabricated_source_rejected_when_profile_supplied(self):
        # The review's honesty attack: fake text + fake provenance.
        with self.assertRaises(ValueError):
            evidence_library.add_item(
                "achievement", "Led 200-person org through $50M IPO",
                "experience[0].bullets[0]",
                library_dir=self.dir, profile=_profile())

    def test_unresolvable_source_rejected(self):
        with self.assertRaises(ValueError):
            evidence_library.add_item(
                "achievement", "Mentored junior engineers.",
                "experience[7].bullets[0]",  # profile has 1 experience
                library_dir=self.dir, profile=_profile())

    def test_real_source_wrong_text_rejected(self):
        # The source exists but the text is not the profile's verbatim
        # wording — still a fabrication risk, still rejected.
        with self.assertRaises(ValueError):
            evidence_library.add_item(
                "achievement", "Mentored THOUSANDS of engineers.",
                "experience[0].bullets[1]",
                library_dir=self.dir, profile=_profile())

    def test_verified_add_item_passes_with_verbatim_text(self):
        item = evidence_library.add_item(
            "achievement", "Mentored junior engineers.",
            "experience[0].bullets[1]",
            library_dir=self.dir, profile=_profile())
        self.assertEqual(item["origin"], "asserted")
        self.assertFalse(item["approved"])

    def test_asserted_item_never_looks_derived(self):
        fabricated = evidence_library.add_item(
            "achievement", "Led 200-person org through $50M IPO",
            "experience[0].bullets[0]", library_dir=self.dir)
        derived = evidence_library.build_from_profile(
            _profile(), library_dir=self.dir)[0]
        self.assertEqual(fabricated["origin"], "asserted")
        self.assertEqual(derived["origin"], "derived")
        # The fabricated item is visibly different from the derived one.
        self.assertNotEqual(fabricated["origin"], derived["origin"])

    def test_find_marks_origin_and_prefers_derived(self):
        asserted = evidence_library.add_item(
            "achievement", "Shipped Python services.",
            "experience[0].bullets[0]", library_dir=self.dir)
        evidence_library.build_from_profile(_profile(), library_dir=self.dir)
        for item in evidence_library.list_items(
                approved_only=False, library_dir=self.dir):
            evidence_library.approve_item(item["evidence_id"],
                                          library_dir=self.dir)
        hits = evidence_library.find_for_requirement(
            "python services shipped", library_dir=self.dir)
        self.assertTrue(hits)
        # Every hit is labeled.
        self.assertTrue(all("origin" in h for h in hits))
        # On equal overlap the derived item outranks the asserted one.
        origins = [h["origin"] for h in hits]
        self.assertIn("asserted", origins)
        self.assertIn("derived", origins)
        self.assertLess(origins.index("derived"), origins.index("asserted"))


class CorruptStoreTest(unittest.TestCase):
    """Major 3: corrupt bytes are never silently overwritten."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "library.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _corrupt(self, text="{nope"):
        self.path.write_text(text, encoding="utf-8")

    def test_read_refuses_loudly(self):
        self._corrupt()
        with self.assertRaises(evidence_library.CorruptStoreError):
            evidence_library.list_items(library_dir=self.dir)

    def test_mutation_refuses_loudly_and_preserves_bytes(self):
        self._corrupt()
        before = self.path.read_bytes()
        with self.assertRaises(evidence_library.CorruptStoreError):
            evidence_library.add_item(
                "achievement", "Anything.", "experience[0]",
                library_dir=self.dir)
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_shape_refuses_loudly(self):
        self._corrupt(json.dumps({"items": "not-a-list"}))
        before = self.path.read_bytes()
        with self.assertRaises(evidence_library.CorruptStoreError):
            evidence_library.build_from_profile(
                _profile(), library_dir=self.dir)
        self.assertEqual(self.path.read_bytes(), before)

    def test_schema_mismatch_refuses_loudly(self):
        self._corrupt(json.dumps(
            {"schema": "i05-contracts/999", "items": []}))
        with self.assertRaises(evidence_library.CorruptStoreError):
            evidence_library.list_items(library_dir=self.dir)

    def test_healthy_store_round_trips_after_refusal(self):
        self._corrupt()
        with self.assertRaises(evidence_library.CorruptStoreError):
            evidence_library.get_item("ev_nope", library_dir=self.dir)
        # After manual recovery (fresh store), the library works again.
        self.path.unlink()
        item = evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        self.assertEqual(
            evidence_library.get_item(
                item["evidence_id"], library_dir=self.dir)["text"],
            "Synthetic win.")


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_from_profile_is_idempotent(self):
        first = evidence_library.build_from_profile(
            _profile(), library_dir=self.dir)
        second = evidence_library.build_from_profile(
            _profile(), library_dir=self.dir)
        self.assertTrue(first)
        self.assertEqual(second, [])
        self.assertEqual(
            len(evidence_library.list_items(library_dir=self.dir)),
            len(first))


class FsyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_fsyncs_before_rename(self):
        with mock.patch("os.fsync") as fsync:
            evidence_library.add_item(
                "achievement", "Synthetic win.",
                "experience[0].bullets[0]", library_dir=self.dir)
        self.assertTrue(fsync.called)
        self.assertTrue((self.dir / "library.json").is_file())


class LinksForJobTest(unittest.TestCase):
    """Major 1: links_for_job exists and honors the studio call site."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _approved_links(self):
        evidence_library.build_from_profile(_profile(), library_dir=self.dir)
        ids = []
        for item in evidence_library.list_items(
                kind="portfolio_link", library_dir=self.dir):
            evidence_library.approve_item(item["evidence_id"],
                                          library_dir=self.dir)
            ids.append(item["evidence_id"])
        return ids

    def test_links_for_job_signature_matches_studio_call_site(self):
        # studio.py: evidence_library.links_for_job(a.job_id,
        #            library_dir=_p(a.library_dir))
        ids = self._approved_links()
        self.assertTrue(ids)
        links = evidence_library.links_for_job(
            "job_123", library_dir=self.dir)
        self.assertEqual(len(links), len(ids))
        link = links[0]
        for field in ("evidence_id", "url", "label", "text", "source",
                      "origin", "job_id"):
            self.assertIn(field, link)
        self.assertEqual(link["job_id"], "job_123")
        self.assertTrue(link["url"].startswith("https://"))
        self.assertEqual(link["origin"], "derived")

    def test_links_for_job_only_approved_portfolio_links(self):
        evidence_library.add_item(
            "achievement", "Synthetic win.", "experience[0].bullets[0]",
            library_dir=self.dir)
        # Draft portfolio link must not leak into the tailor path.
        draft = evidence_library.add_item(
            "portfolio_link", "Draft: https://example.com/draft",
            "website", library_dir=self.dir)
        self.assertEqual(
            evidence_library.links_for_job("job_1",
                                           library_dir=self.dir), [])
        evidence_library.approve_item(draft["evidence_id"],
                                      library_dir=self.dir)
        links = evidence_library.links_for_job("job_1",
                                               library_dir=self.dir)
        self.assertEqual([l["evidence_id"] for l in links],
                         [draft["evidence_id"]])
        self.assertEqual(links[0]["origin"], "asserted")

    def test_links_for_job_is_read_only(self):
        self._approved_links()
        before = (self.dir / "library.json").read_bytes()
        evidence_library.links_for_job("job_1", library_dir=self.dir)
        self.assertEqual((self.dir / "library.json").read_bytes(), before)

    def test_links_for_job_empty_library(self):
        self.assertEqual(
            evidence_library.links_for_job("job_1",
                                           library_dir=self.dir), [])


if __name__ == "__main__":
    unittest.main()
