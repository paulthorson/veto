#!/usr/bin/env python3
"""Tests for ``initiatives.i04.resume_decoder`` and ``initiatives.i04.aliases``.

All fixtures are synthetic and clearly labeled; no real employers,
people, or achievements appear anywhere in this file.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from initiatives.i04 import aliases  # noqa: E402
from initiatives.i04 import resume_decoder  # noqa: E402

#: SYNTHETIC FIXTURE — a fake resume. Nothing here is real.
SYNTHETIC_RESUME = """Jane Synthetic
jane.synthetic@example.com | (555) 123-4567 | linkedin.com/in/janesynthetic

Summary
Senior software engineer with 8 years of experience building data platforms.

Experience
Senior Software Engineer, Fictional Corp
- 6 years Python building data pipelines; reduced ETL runtime 40%
- Led migration to k8s across 12 services

Software Engineer, Imaginary LLC
- Built REST APIs in Go; shipped CI/CD pipelines

Skills
Python, Go, Kubernetes, Docker, PostgreSQL, Airflow, Terraform

Education
B.S. Computer Science, Placeholder University
"""


class AliasesTest(unittest.TestCase):
    def test_alias_resolves(self) -> None:
        self.assertEqual(aliases.canonicalize("k8s"), "Kubernetes")
        self.assertEqual(aliases.canonicalize("K8S"), "Kubernetes")
        self.assertEqual(aliases.canonicalize("js"), "JavaScript")

    def test_unknown_returned_unchanged(self) -> None:
        self.assertEqual(aliases.canonicalize("COBOL++"), "COBOL++")

    def test_known_aliases(self) -> None:
        self.assertIn("k8s", aliases.known_aliases("Kubernetes"))


class DecodeResumeTest(unittest.TestCase):
    def test_sections_detected(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        for section in ("summary", "experience", "skills", "education"):
            self.assertIn(section, result["sections_detected"], section)

    def test_skills_found_with_quotes(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        by_skill = {s["skill"]: s for s in result["skills_found"]}
        self.assertIn("Python", by_skill)
        self.assertIn("Kubernetes", by_skill)
        # k8s alias resolves to the canonical name, matched_as records it.
        self.assertEqual(by_skill["Kubernetes"]["matched_as"], "k8s")
        self.assertTrue(by_skill["Python"]["quote"])

    def test_years_claims(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        claims = result["years_claims"]
        self.assertTrue(any(c["years"] == 8 for c in claims))
        six = [c for c in claims if c["years"] == 6]
        self.assertTrue(six)
        self.assertEqual(six[0]["skill"], "Python")

    def test_seniority_inferred_with_quote(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        self.assertIsNotNone(result["seniority"])
        self.assertEqual(result["seniority"]["label"], "senior")
        self.assertEqual(result["seniority"]["confidence"], "inferred")
        self.assertTrue(result["seniority"]["quote"])

    def test_pii_shapes_only(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        pii = result["pii_present"]
        self.assertTrue(pii["email"])
        self.assertTrue(pii["phone"])
        self.assertTrue(pii["url"])
        # Values must never appear in the decoded output.
        blob = str(result)
        self.assertNotIn("jane.synthetic@example.com", blob)
        self.assertNotIn("(555) 123-4567", blob)
        self.assertTrue(result["pii_redacted"])
        # Masked markers appear where the contact line was.
        self.assertIn("[email redacted]", blob)
        self.assertIn("[phone redacted]", blob)

    def test_pii_in_experience_bullet_masked(self) -> None:
        text = "Experience\nSoftware Engineer\n- Contact me at boss@example.com for details"
        result = resume_decoder.decode_resume(text)
        blob = str(result["profile"])
        self.assertNotIn("boss@example.com", blob)
        self.assertIn("[email redacted]", blob)

    def test_pii_in_summary_section_masked(self) -> None:
        # REGRESSION (Rule 1 blocker): contact info inside the Summary
        # section used to flow raw into profile["summary"]. The summary
        # is the grill-compatible profile's free-text field — it must
        # be masked like every other emitted string.
        text = (
            "Summary\n"
            "Reach me at boss@example.com or (555) 999-8888\n"
            "\n"
            "Experience\n"
            "Software Engineer\n"
        )
        result = resume_decoder.decode_resume(text)
        blob = str(result["profile"])
        self.assertNotIn("boss@example.com", blob)
        self.assertNotIn("(555) 999-8888", blob)
        self.assertIn("[email redacted]", blob)
        self.assertIn("[phone redacted]", blob)
        # Blob-wide: no raw contact value anywhere in the output.
        self.assertNotIn("boss@example.com", str(result))

    def test_quote_redacted_before_truncation(self) -> None:
        # REGRESSION (Rule 1 audit): quotes are redacted BEFORE the
        # 200-char truncation. An email straddling the cut used to
        # survive as a raw PII fragment ("boss@exa") because the
        # truncated fragment no longer matched the email regex.
        line = "x" * 190 + "boss@example.com Python expert"
        text = "Experience\nSoftware Engineer\n- " + line
        result = resume_decoder.decode_resume(text)
        quotes = [s["quote"] for s in result["skills_found"]]
        self.assertTrue(quotes)
        # The straddling email must not leak as a raw fragment in any
        # quote (this fails if redaction runs after truncation).
        for quote in quotes:
            self.assertNotIn("boss@", quote)
        self.assertIn("[email redacted]", str(result))

    def test_skill_no_substring_hallucination(self) -> None:
        # REGRESSION: bare substring matching fabricated
        # "explicit"-confidence skills — "Java" inside "JavaScript",
        # "Rust" inside "trust", "Agile" inside "fragile".
        text = "Experience\nWorked on JavaScript; earned trust; fragile systems"
        result = resume_decoder.decode_resume(text)
        names = {s["skill"] for s in result["skills_found"]}
        self.assertNotIn("Java", names)
        self.assertNotIn("Rust", names)
        self.assertNotIn("Agile", names)
        # The real token still matches as a whole word.
        self.assertIn("JavaScript", names)

    def test_seniority_highest_rung_wins(self) -> None:
        # REGRESSION: the fixed-order ladder made the LOWEST rung win —
        # "Senior Software Engineer (ex-intern)" resolved to intern.
        text = "Experience\nSenior Software Engineer (ex-intern)\n- did stuff"
        result = resume_decoder.decode_resume(text)
        self.assertIsNotNone(result["seniority"])
        self.assertEqual(result["seniority"]["label"], "senior")

    def test_seniority_title_preferred_over_bullet(self) -> None:
        # A title keyword outranks a bullet keyword even when the bullet
        # names a more senior role.
        text = (
            "Experience\n"
            "Senior Software Engineer\n"
            "- Mentored an intern during onboarding\n"
        )
        result = resume_decoder.decode_resume(text)
        self.assertEqual(result["seniority"]["label"], "senior")

    def test_seniority_highest_title_wins(self) -> None:
        text = (
            "Experience\n"
            "Junior Developer, Fictional Co\n"
            "Senior Software Engineer, Imaginary LLC\n"
        )
        result = resume_decoder.decode_resume(text)
        self.assertEqual(result["seniority"]["label"], "senior")

    def test_seniority_falls_back_to_bullet_when_no_title_keyword(self) -> None:
        text = (
            "Experience\n"
            "Software Engineer\n"
            "- Reported to the director of engineering\n"
        )
        result = resume_decoder.decode_resume(text)
        self.assertEqual(result["seniority"]["label"], "director")

    def test_section_header_requires_full_line(self) -> None:
        # REGRESSION: unanchored header search promoted a body line
        # starting with "Experience" into a section header.
        text = "Summary\nExperience building APIs for happy clients\n"
        result = resume_decoder.decode_resume(text)
        self.assertIn("summary", result["sections_detected"])
        self.assertNotIn("experience", result["sections_detected"])
        self.assertIn(
            "Experience building APIs for happy clients",
            result["sections"]["summary"],
        )

    def test_years_multiword_skill_attributed(self) -> None:
        # REGRESSION: single-token capture meant multi-word skills
        # ("Machine Learning") could never be attributed.
        text = "Experience\nSoftware Engineer\n- 4 years Machine Learning experience"
        result = resume_decoder.decode_resume(text)
        claims = [c for c in result["years_claims"] if c["years"] == 4]
        self.assertTrue(claims)
        self.assertEqual(claims[0]["skill"], "Machine Learning")

    def test_years_alias_only_name_not_attributed(self) -> None:
        # Alias canonical names that skills_found never emits (e.g.
        # "Infrastructure as Code") must not appear as years-claim
        # attribution either — attribution stays consistent.
        text = (
            "Experience\nSoftware Engineer\n"
            "- 3 years Infrastructure as Code experience"
        )
        result = resume_decoder.decode_resume(text)
        claims = [c for c in result["years_claims"] if c["years"] == 3]
        self.assertTrue(claims)
        self.assertIsNone(claims[0]["skill"])

    def test_years_skill_attributed_despite_trailing_punctuation(self) -> None:
        # REGRESSION: the capture glued trailing sentence punctuation
        # onto the token ("5 years of Python." captured "Python."),
        # defeating attribution.
        text = "Experience\nSoftware Engineer\n- 5 years of Python."
        result = resume_decoder.decode_resume(text)
        claims = [c for c in result["years_claims"] if c["years"] == 5]
        self.assertTrue(claims)
        self.assertEqual(claims[0]["skill"], "Python")

    def test_us_phone_limitation_disclosed(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        self.assertTrue(
            any("US-format" in note for note in result["limitations"])
        )

    def test_empty_text_reports_absence(self) -> None:
        result = resume_decoder.decode_resume("")
        self.assertEqual(result["skills_found"], [])
        self.assertIsNone(result["seniority"])
        self.assertTrue(any("No resume text" in note for note in result["limitations"]))

    def test_no_skills_section_limitation(self) -> None:
        result = resume_decoder.decode_resume("Just a paragraph of prose.\nNothing structured here.")
        self.assertTrue(
            any("No curated skills matched" in note for note in result["limitations"])
        )

    def test_profile_shape_grill_compatible(self) -> None:
        result = resume_decoder.decode_resume(SYNTHETIC_RESUME)
        profile = result["profile"]
        self.assertIn("Python", profile["skills"])
        self.assertTrue(profile["experience"])
        self.assertTrue(profile["summary"])

    def test_hash_stable_and_short(self) -> None:
        a = resume_decoder.decode_resume(SYNTHETIC_RESUME)["resume_text_hash"]
        b = resume_decoder.decode_resume(SYNTHETIC_RESUME)["resume_text_hash"]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)


if __name__ == "__main__":
    unittest.main()
