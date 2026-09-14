"""Tests for Initiative 05 application packet (Epic 5).

All fixtures are synthetic and labeled as such. Every store write goes
to temp dirs — real stores are never touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cover_letters
import tailor
from initiatives.i05 import evidence_library, packet, versions


def _profile() -> dict:
    # Synthetic fixture profile: deliberately fictional person, company,
    # and data, used only so the tailored resume is a realistic length.
    return {
        "full_name": "Synthetic Fixture",
        "email": "fixture@example.com",
        "phone": "555-010-2030",
        "location": "New York, NY",
        "linkedin_url": "https://example.com/in/fixture",
        "website": "https://example.com",
        "summary": (
            "Synthetic fixture profile used only for tests. Backend "
            "engineer with seven years of experience building Python web "
            "services and PostgreSQL data pipelines for synthetic "
            "workloads. Enjoys testing, code review, and writing "
            "documentation that keeps on-call rotations quiet."
        ),
        "skills": ["Python", "PostgreSQL", "Django", "Redis"],
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "FixtureCorp",
                "dates": "2020 - 2024",
                "bullets": [
                    "Shipped Python services handling 1M requests a day.",
                    "Cut PostgreSQL query times by half with indexes.",
                    "Built synthetic load tests that caught regressions "
                    "before release.",
                    "Mentored two junior engineers on testing and code "
                    "review.",
                ],
            },
            {
                "title": "Junior Backend Engineer",
                "company": "FixtureLabs",
                "dates": "2017 - 2020",
                "bullets": [
                    "Maintained Django APIs serving synthetic fixture data.",
                    "Wrote PostgreSQL migrations with careful "
                    "rollout playbooks.",
                    "Built dashboards that flagged slow endpoints within "
                    "minutes.",
                    "Documented onboarding guides used by every new hire.",
                ],
            },
        ],
        "education": [{"degree": "B.S.", "school": "Fixture University",
                       "dates": "2016 - 2020"}],
    }


def _job() -> dict:
    return {
        "id": "job-synth-1",
        "title": "Backend Engineer",
        "company": "HiringCorp",
        "description": "We need Python and PostgreSQL experience.",
    }


class PacketBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.variants = self.dir / "variants"
        self.library = self.dir / "library"
        self.packets = self.dir / "packets"
        self.profile = _profile()
        self.job = _job()

        # Approved evidence backing the matched skills.
        items = evidence_library.build_from_profile(
            self.profile, library_dir=self.library)
        for item in items:
            evidence_library.approve_item(item["evidence_id"],
                                          library_dir=self.library)

        # Tailored variant, approved.
        result = tailor.tailor_with_provenance(self.profile, self.job)
        letter = cover_letters.draft_cover_letter(
            self.profile, self.job)["letter"]
        self.variant = versions.create_variant(
            "backend-hiringcorp", job_id="job-synth-1",
            initial_resume_md=result["resume_md"],
            initial_cover_letter=letter,
            provenance=result["provenance"],
            variants_dir=self.variants)
        self.version_id = self.variant["versions"][0]["version_id"]
        versions.approve_version(
            self.variant["variant_id"], self.version_id,
            variants_dir=self.variants)

    def tearDown(self):
        self.tmp.cleanup()

    def _packet_kwargs(self):
        return {
            "library_dir": self.library,
            "variants_dir": self.variants,
            "packets_dir": self.packets,
        }

    def test_build_packet_bundles_everything(self):
        pkt = packet.build_packet(
            "job-synth-1", self.profile, self.job,
            variant_id=self.variant["variant_id"],
            version_id=self.version_id,
            **self._packet_kwargs())
        self.assertEqual(pkt["status"], "draft")
        self.assertTrue(pkt["resume_md"])
        self.assertTrue(pkt["cover_letter"])
        self.assertIn("answers", pkt)
        self.assertIn("Fit rationale", pkt["fit_rationale"])
        self.assertIn("Python", pkt["fit_rationale"])
        self.assertTrue(pkt["changes"])
        self.assertEqual(len(pkt["review_checklist"]), 5)

    def test_fit_rationale_names_missing_skills_as_not_claimed(self):
        rationale = packet.build_fit_rationale(
            self.profile, self.job,
            provenance={"matched_skills": ["Python"],
                        "missing_skills": ["Kubernetes"]},
            library_dir=self.library)
        self.assertIn("Not claimed", rationale)
        self.assertIn("Kubernetes", rationale)
        self.assertIn("Python", rationale)

    def test_full_review_flow_to_approval(self):
        pkt = packet.build_packet(
            "job-synth-1", self.profile, self.job,
            variant_id=self.variant["variant_id"],
            version_id=self.version_id,
            **self._packet_kwargs())
        pid = pkt["packet_id"]
        # Approval is blocked while changes are unreviewed.
        with self.assertRaises(ValueError):
            packet.approve_packet(pid, packets_dir=self.packets)
        for change in pkt["changes"]:
            self.assertTrue(
                packet.mark_change_reviewed(
                    pid, change["change_id"], packets_dir=self.packets))
        checklist = packet.refresh_checklist(
            pid, self.profile, self.job, **self._packet_kwargs())
        pending = [c for c in checklist if c["status"] == "pending"]
        self.assertEqual(pending, [],
                         f"pending: {[(c['id'], c['detail']) for c in pending]}")
        approved = packet.approve_packet(pid, packets_dir=self.packets)
        self.assertEqual(approved["status"], "approved")
        self.assertIn("content_sha256", approved)

    def test_build_without_variant(self):
        pkt = packet.build_packet(
            "job-synth-1", self.profile, self.job,
            **self._packet_kwargs())
        self.assertIsNone(pkt["variant_id"])
        statuses = {c["id"]: c["status"]
                    for c in pkt["review_checklist"]}
        self.assertEqual(statuses["version-approved"], "skipped")
        self.assertEqual(statuses["trace-clean"], "done")

    def test_render_packet_text(self):
        pkt = packet.build_packet(
            "job-synth-1", self.profile, self.job,
            variant_id=self.variant["variant_id"],
            version_id=self.version_id,
            **self._packet_kwargs())
        text = packet.render_packet_text(pkt)
        self.assertIn("Application packet", text)
        self.assertIn("Review checklist", text)
        self.assertIn("Fit rationale", text)

    def test_unknown_packet_raises(self):
        with self.assertRaises(KeyError):
            packet.refresh_checklist(
                "pkt_nope", self.profile, self.job,
                **self._packet_kwargs())

    def _approved_packet_id(self) -> str:
        pkt = packet.build_packet(
            "job-synth-1", self.profile, self.job,
            variant_id=self.variant["variant_id"],
            version_id=self.version_id,
            **self._packet_kwargs())
        pid = pkt["packet_id"]
        for change in pkt["changes"]:
            packet.mark_change_reviewed(
                pid, change["change_id"], packets_dir=self.packets)
        packet.refresh_checklist(
            pid, self.profile, self.job, **self._packet_kwargs())
        packet.approve_packet(pid, packets_dir=self.packets)
        return pid

    def test_review_edit_reverts_approval_to_draft(self):
        pid = self._approved_packet_id()
        pkt = packet.get_packet(pid, packets_dir=self.packets)
        self.assertEqual(pkt["status"], "approved")
        # Re-marking a change reviewed is a review-state edit: approval
        # must not survive it.
        change_id = pkt["changes"][0]["change_id"]
        packet.mark_change_reviewed(
            pid, change_id, packets_dir=self.packets)
        pkt = packet.get_packet(pid, packets_dir=self.packets)
        self.assertEqual(pkt["status"], "draft")
        self.assertNotIn("approved_at", pkt)
        self.assertNotIn("content_sha256", pkt)

    def test_refresh_with_new_blocker_reverts_approval(self):
        pid = self._approved_packet_id()
        # Introduce a blocker after approval: commit a fresh unapproved
        # version and point the packet at it.
        new_ver = versions.commit_version(
            self.variant["variant_id"], "x",
            variants_dir=self.variants)
        pkt = packet.get_packet(pid, packets_dir=self.packets)
        pkt["version_id"] = new_ver["version_id"]
        import json as _json
        p = next(self.packets.glob("*.json"))
        p.write_text(_json.dumps(pkt), encoding="utf-8")
        packet.refresh_checklist(
            pid, self.profile, self.job, **self._packet_kwargs())
        pkt = packet.get_packet(pid, packets_dir=self.packets)
        statuses = {c["id"]: c["status"] for c in pkt["review_checklist"]}
        self.assertEqual(statuses["version-approved"], "pending")
        self.assertEqual(pkt["status"], "draft")


class NoSubmissionPathTest(unittest.TestCase):
    def test_module_has_no_submit_capability(self):
        for name in dir(packet):
            self.assertNotIn("submit", name.lower())
        self.assertFalse(hasattr(packet, "submit_packet"))
        self.assertFalse(hasattr(packet, "submit"))

    def test_docstring_discloses_build_only(self):
        self.assertIn("NEVER submits", packet.__doc__)


if __name__ == "__main__":
    unittest.main()
