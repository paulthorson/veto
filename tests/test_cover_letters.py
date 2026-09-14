"""Unit tests for the cover-letter module (no network).

Covers profile-only drafting, requirement-to-evidence mapping, the
"eager to develop" rule for missing requirements, honesty_scan
(invented metric, invented company, invented skill), the self-check
that a draft passes its own scan, and the approval store round-trip
(temp dir, never the real one).
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cover_letters


def _profile() -> dict:
    return {
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "location": "New York, NY",
        "years_experience": 5,
        "skills": ["Python", "PostgreSQL", "Docker"],
        "experience": [
            {
                "title": "Backend Engineer",
                "company": "Analytica",
                "dates": "2021-2024",
                "bullets": [
                    "Maintained legacy reporting dashboards.",
                    "Built Python ETL pipelines processing 10M rows daily.",
                    "Containerized services with Docker for staging deploys.",
                ],
            },
            {
                "title": "Junior Developer",
                "company": "Webshop",
                "dates": "2019-2021",
                "bullets": ["Fixed bugs in the storefront checkout."],
            },
        ],
        "education": [
            {"degree": "B.S. Computer Science", "school": "NYU", "dates": "2015-2019"}
        ],
    }


def _job() -> dict:
    return {
        "title": "Senior Backend Engineer",
        "company": "Fintech Co",
        "description": (
            "We need a backend engineer with deep Python experience and "
            "Docker expertise to build ETL pipelines. Kubernetes is a plus. "
            "PostgreSQL required."
        ),
    }


class DraftTests(unittest.TestCase):
    def test_draft_has_expected_structure(self):
        result = cover_letters.draft_cover_letter(_profile(), _job())
        letter = result["letter"]
        self.assertIn("Dear Hiring Manager,", letter)
        self.assertIn("Sincerely,", letter)
        self.assertIn("Ada Lovelace", letter)
        self.assertIn("Senior Backend Engineer", letter)
        self.assertIn("Fintech Co", letter)
        self.assertEqual(len(result["proofs"]), 2)

    def test_proof_paragraphs_map_to_profile_evidence(self):
        result = cover_letters.draft_cover_letter(_profile(), _job())
        profile_text = "\n".join(
            b
            for exp in _profile()["experience"]
            for b in exp["bullets"]
        )
        for proof in result["proofs"]:
            self.assertIn(proof["requirement"], result["matched_requirements"])
            # The evidence bullet is a real profile bullet.
            self.assertIn(proof["evidence_bullet"], profile_text)
            # ...and the requirement word shows up in the letter paragraph.
            self.assertIn(proof["requirement"], result["letter"])

    def test_missing_requirements_are_never_claimed(self):
        result = cover_letters.draft_cover_letter(_profile(), _job())
        self.assertIn("kubernetes", [m.lower() for m in result["missing_requirements"]])
        letter = result["letter"].lower()
        # "eager to" growth language must be present ...
        self.assertIn("eager to", letter)
        # ... and the missing skill must not be claimed as experience.
        for line in result["letter"].splitlines():
            if "kubernetes" in line.lower():
                self.assertTrue(
                    any(h in line.lower() for h in ("eager to", "developing", "deepen")),
                    f"missing skill claimed without hedge: {line}",
                )

    def test_draft_passes_its_own_honesty_scan(self):
        result = cover_letters.draft_cover_letter(_profile(), _job())
        self.assertEqual(result["honesty_self_check"], [])
        direct = cover_letters.honesty_scan(result["letter"], _profile(), _job())
        self.assertEqual(direct, [])

    def test_draft_handles_empty_profile(self):
        result = cover_letters.draft_cover_letter({}, _job())
        self.assertIn("Dear Hiring Manager,", result["letter"])
        self.assertEqual(result["proofs"], [])

    def test_draft_without_job_company(self):
        job = {"title": "Backend Engineer", "description": "Python and Docker."}
        result = cover_letters.draft_cover_letter(_profile(), job)
        self.assertIn("Backend Engineer", result["letter"])


class HonestyScanTests(unittest.TestCase):
    def test_catches_invented_metric(self):
        letter = (
            cover_letters.draft_cover_letter(_profile(), _job())["letter"]
            + "\n\nI increased revenue by 300% last year."
        )
        flags = cover_letters.honesty_scan(letter, _profile(), _job())
        claims = [f["claim"] for f in flags]
        self.assertTrue(
            any("300" in c for c in claims),
            f"invented metric not flagged: {flags}",
        )
        self.assertTrue(all("sentence" in f and "reason" in f for f in flags))

    def test_catches_invented_company(self):
        letter = (
            cover_letters.draft_cover_letter(_profile(), _job())["letter"]
            + "\n\nPreviously at Globex Corporation, I led the migration."
        )
        flags = cover_letters.honesty_scan(letter, _profile(), _job())
        claims = [f["claim"] for f in flags]
        self.assertTrue(
            any("Globex" in c for c in claims),
            f"invented company not flagged: {flags}",
        )

    def test_catches_invented_title(self):
        letter = (
            cover_letters.draft_cover_letter(_profile(), _job())["letter"]
            + "\n\nI served as Staff Engineer leading a 40-person org."
        )
        flags = cover_letters.honesty_scan(letter, _profile(), _job())
        claims = [f["claim"] for f in flags]
        self.assertTrue(
            any("Staff Engineer" in c for c in claims),
            f"invented title not flagged: {flags}",
        )

    def test_catches_unhedged_skill_claim(self):
        letter = (
            "Dear Hiring Manager,\n\nI am a Kubernetes expert with "
            "CKA certification. Thank you."
        )
        flags = cover_letters.honesty_scan(letter, _profile(), _job())
        self.assertTrue(
            any("kubernetes" in f["claim"].lower() for f in flags),
            f"unhedged skill claim not flagged: {flags}",
        )

    def test_hedged_skill_mention_is_allowed(self):
        letter = (
            "Dear Hiring Manager,\n\nKubernetes is an area I'm still "
            "developing \u2014 I'm eager to deepen my expertise there. "
            "Thank you."
        )
        self.assertEqual(cover_letters.honesty_scan(letter, _profile(), _job()), [])

    def test_real_profile_numbers_are_allowed(self):
        letter = (
            "Dear Hiring Manager,\n\nI built Python ETL pipelines "
            "processing 10M rows daily at Analytica. Thank you."
        )
        self.assertEqual(cover_letters.honesty_scan(letter, _profile(), _job()), [])

    def test_job_company_and_title_are_not_flagged(self):
        letter = (
            "Dear Hiring Manager,\n\nI'm excited to apply for the Senior "
            "Backend Engineer role at Fintech Co. Thank you."
        )
        self.assertEqual(cover_letters.honesty_scan(letter, _profile(), _job()), [])

    def test_empty_letter_is_clean(self):
        self.assertEqual(cover_letters.honesty_scan("", _profile(), _job()), [])


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = mock.patch.object(
            cover_letters, "COVER_LETTERS_DIR", Path(self._tmp.name)
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_approve_list_load_round_trip(self):
        letter = cover_letters.draft_cover_letter(_profile(), _job())["letter"]
        result = cover_letters.approve_letter("lever:abc123", letter)
        self.assertEqual(result["job_id"], "lever:abc123")
        self.assertTrue(Path(result["path"]).is_file())
        self.assertIn("lever_abc123", cover_letters.list_letters())
        loaded = cover_letters.load_letter("lever:abc123")
        self.assertEqual(loaded.strip(), letter.strip())

    def test_load_missing_returns_none(self):
        self.assertIsNone(cover_letters.load_letter("nope"))

    def test_list_empty_when_no_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                cover_letters, "COVER_LETTERS_DIR", Path(tmp) / "missing"
            ):
                self.assertEqual(cover_letters.list_letters(), [])

    def test_approve_empty_letter_raises(self):
        with self.assertRaises(ValueError):
            cover_letters.approve_letter("job1", "   ")

    def test_job_id_is_sanitized(self):
        result = cover_letters.approve_letter("../../evil", "Hello.")
        self.assertNotIn("..", Path(result["path"]).name)
        self.assertTrue(Path(result["path"]).parent.samefile(Path(self._tmp.name)))

    def test_unreadable_file_treated_as_absent(self):
        real = Path(self._tmp.name) / "unreadable.txt"
        real.write_text("hello", encoding="utf-8")
        with mock.patch.object(
            cover_letters, "_letter_path", return_value=real
        ), mock.patch.object(Path, "read_text", side_effect=OSError("boom")):
            self.assertIsNone(cover_letters.load_letter("unreadable"))


class PluginWiringTests(unittest.TestCase):
    def test_register_cli_returns_cover_letter_command(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = cover_letters.register_cli(sub)
        self.assertEqual(set(handlers), {"cover-letter"})

    def test_cli_list_handler(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = cover_letters.register_cli(sub)
        args = parser.parse_args(["cover-letter", "list"])
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                cover_letters, "COVER_LETTERS_DIR", Path(tmp)
            ):
                self.assertEqual(handlers["cover-letter"](args), 0)

    def test_cli_draft_requires_job_file(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = cover_letters.register_cli(sub)
        args = parser.parse_args(["cover-letter", "draft"])
        self.assertEqual(handlers["cover-letter"](args), 1)

    def test_register_tools_registers_five_tools(self):
        seen: list[str] = []

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen.append(fn.__name__)
                    return fn

                return deco

        cover_letters.register_tools(FakeMCP())
        self.assertEqual(
            seen,
            [
                "draft_cover_letter_tool",
                "scan_cover_letter",
                "approve_cover_letter",
                "list_cover_letters",
                "load_cover_letter",
            ],
        )


if __name__ == "__main__":
    unittest.main()
