#!/usr/bin/env python3
"""Tests for ``initiatives.i04.evidence_map`` (Epic 2).

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

from initiatives.i04 import evidence_map  # noqa: E402
from initiatives.i04.schemas import validate_evidence_map  # noqa: E402

#: SYNTHETIC FIXTURE — fake JD. Nothing here is real.
SYNTHETIC_JD = """Senior Widget Engineer — Synthetic Systems Inc.

Requirements:
- 5+ years of Python experience
- Experience with Kubernetes in production
- Strong SQL skills

Nice to have:
- Familiarity with Rust
"""

#: SYNTHETIC FIXTURE — fake resume. Nothing here is real.
SYNTHETIC_RESUME = """Jane Synthetic

Experience
Senior Software Engineer, Fictional Corp
- 6 years Python building data pipelines; reduced ETL runtime 40%
- Ran Kubernetes in production across 12 services

Skills
Python, Kubernetes, Docker
"""


#: SYNTHETIC FIXTURE — resume whose evidence lines share lines with
#: contact values. Nothing here is real.
SYNTHETIC_RESUME_PII = """Jane Synthetic
jane.synthetic@example.com
+1 (555) 123-4567

Experience
Senior Software Engineer, Fictional Corp
- 6 years Python building data pipelines; reduced ETL runtime 40%
- Contact jane.synthetic@example.com for references; led 5 engineers
- Ran Kubernetes in production across 12 services

Skills
Python, Kubernetes, Docker
"""

#: SYNTHETIC FIXTURE — JD with a line break inside a requirement phrase.
#: The extracted keyword itself ("machine learning") still appears on
#: single JD lines; no extractor output spans a line break.
#: Nothing here is real.
SYNTHETIC_JD_LINE_SPAN = """Machine Learning Engineer — Synthetic Systems Inc.

Requirements:
- Experience with
Machine Learning models in production
"""


class BuildEvidenceMapTest(unittest.TestCase):
    def setUp(self) -> None:
        result = evidence_map.build_evidence_map(
            SYNTHETIC_JD, SYNTHETIC_RESUME, "synthetic-job-001",
            job_title="Senior Widget Engineer (synthetic)",
        )
        self.result = result
        self.doc = result["evidence_map"]

    def test_doc_validates_against_frozen_schema(self) -> None:
        self.assertEqual(self.result["validation_errors"], [])
        self.assertEqual(validate_evidence_map(self.doc), [])

    def test_python_supported_with_verbatim_quote(self) -> None:
        entries = {e["requirement"]["text"]: e for e in self.doc["entries"]}
        self.assertIn("python", entries)
        entry = entries["python"]
        self.assertEqual(entry["status"], "supported")
        self.assertTrue(entry["evidence"])
        quote = entry["evidence"][0]["quote"]
        self.assertIn("6 years Python", quote)
        # Verbatim: the quote is a substring of the resume text.
        self.assertIn(quote, SYNTHETIC_RESUME)
        self.assertTrue(entry["evidence"][0]["quantified"])

    def test_kubernetes_supported(self) -> None:
        entries = {e["requirement"]["text"]: e for e in self.doc["entries"]}
        self.assertIn("kubernetes", entries)
        self.assertEqual(entries["kubernetes"]["status"], "supported")

    def test_rust_is_gap(self) -> None:
        entries = {e["requirement"]["text"]: e for e in self.doc["entries"]}
        rust = entries.get("rust")
        self.assertIsNotNone(rust)
        self.assertEqual(rust["status"], "gap")
        self.assertEqual(rust["evidence"], [])

    def test_unquantified_keyword_becomes_grill_question(self) -> None:
        jd = "Requirements:\n- Python experience required\n"
        resume = "Experience\nDeveloper\n- Used Python for scripting tasks\n"
        result = evidence_map.build_evidence_map(jd, resume, "syn-2")
        self.assertEqual(result["validation_errors"], [])
        entries = {e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]}
        entry = entries["python"]
        self.assertEqual(entry["status"], "grill_question")
        self.assertTrue(entry["grill_question_id"])
        self.assertEqual(entry["evidence"], [])
        # Companion question is keyed by the same id.
        companions = {
            q["grill_question_id"]: q for q in result["suggested_grill_questions"]
        }
        self.assertIn(entry["grill_question_id"], companions)

    def test_years_shortfall_is_gap(self) -> None:
        jd = "Requirements:\n- 10+ years of Python experience\n"
        resume = "Experience\nDeveloper\n- 3 years Python building tools; shipped 5 releases\n"
        result = evidence_map.build_evidence_map(jd, resume, "syn-3")
        self.assertEqual(result["validation_errors"], [])
        entries = {e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]}
        # The phrase extractor yields a python keyword; years claim (3)
        # falls short of 10 -> gap, never upgraded.
        py_entries = [e for k, e in entries.items() if "python" in k]
        self.assertTrue(py_entries)
        self.assertTrue(all(e["status"] == "gap" for e in py_entries))

    def test_evidence_source_hash_anchors_resume_text(self) -> None:
        h1 = self.doc["evidence_source_hash"]
        result2 = evidence_map.build_evidence_map(
            SYNTHETIC_JD, SYNTHETIC_RESUME + "\nExtra line.", "synthetic-job-001"
        )
        self.assertNotEqual(h1, result2["evidence_map"]["evidence_source_hash"])

    def test_empty_jd_yields_empty_entries(self) -> None:
        result = evidence_map.build_evidence_map("", SYNTHETIC_RESUME, "syn-4")
        self.assertEqual(result["validation_errors"], [])
        self.assertEqual(result["evidence_map"]["entries"], [])

    def test_must_have_classification(self) -> None:
        entries = {e["requirement"]["text"]: e for e in self.doc["entries"]}
        self.assertEqual(entries["python"]["requirement"]["kind"], "must_have")
        self.assertEqual(entries["rust"]["requirement"]["kind"], "nice_to_have")

    def test_alias_only_match_becomes_grill_question(self) -> None:
        # JD asks for Kubernetes; resume only ever says "k8s". The
        # keyword never appears literally -> ambiguity for the grill.
        jd = "Requirements:\n- Kubernetes experience required\n"
        resume = (
            "Experience\nEngineer\n"
            "- Ran k8s in production across 12 services\n"
        )
        result = evidence_map.build_evidence_map(jd, resume, "syn-alias")
        self.assertEqual(result["validation_errors"], [])
        entries = {e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]}
        entry = entries["kubernetes"]
        self.assertEqual(entry["status"], "grill_question")
        self.assertTrue(entry["grill_question_id"])

    def test_grill_question_ids_namespaced_by_job(self) -> None:
        jd = "Requirements:\n- Python experience required\n"
        resume = "Experience\nDeveloper\n- Used Python for scripting tasks\n"
        r1 = evidence_map.build_evidence_map(jd, resume, "job-a")
        r2 = evidence_map.build_evidence_map(jd, resume, "job-b")
        e1 = {e["requirement"]["text"]: e for e in r1["evidence_map"]["entries"]}["python"]
        e2 = {e["requirement"]["text"]: e for e in r2["evidence_map"]["entries"]}["python"]
        self.assertNotEqual(e1["grill_question_id"], e2["grill_question_id"])

    # --- Blind-review rework regression tests (2026-09-13) ---

    def _assert_all_supported_quotes_verbatim(
        self, jd: str, resume: str
    ) -> None:
        result = evidence_map.build_evidence_map(jd, resume, "verbatim-check")
        self.assertEqual(result["validation_errors"], [])
        for entry in result["evidence_map"]["entries"]:
            if entry["status"] == "supported":
                self.assertTrue(entry["evidence"], entry["requirement"])
                for ev in entry["evidence"]:
                    self.assertIn(
                        ev["quote"],
                        resume,
                        f"supported quote is not a verbatim resume "
                        f"substring: {ev['quote']!r}",
                    )

    def test_supported_quotes_are_verbatim_substrings(self) -> None:
        # The headline invariant: every supported quote is a verbatim
        # substring of the resume text — across the standard fixture,
        # a PII-heavy resume (evidence lines share lines with contact
        # values), and a skills-list-only resume.
        self._assert_all_supported_quotes_verbatim(
            SYNTHETIC_JD, SYNTHETIC_RESUME
        )
        self._assert_all_supported_quotes_verbatim(
            SYNTHETIC_JD, SYNTHETIC_RESUME_PII
        )
        self._assert_all_supported_quotes_verbatim(
            "Requirements:\n- Python experience required\n"
            "- SQL experience required\n",
            "Jane Synthetic\n\nSkills\nPython, SQL, Docker\n",
        )

    def test_normalized_skill_never_becomes_verbatim_quote(self) -> None:
        # The decoder normalizes "k8s" to the canonical "Kubernetes",
        # but a supported quote may never carry the canonical name when
        # the resume never said it. Alias-only evidence routes to the
        # grill — never to a fabricated verbatim quote.
        jd = "Requirements:\n- Kubernetes experience required\n"
        resume = "Skills\nk8s, Docker\n- 3 years Docker in production\n"
        result = evidence_map.build_evidence_map(jd, resume, "syn-norm")
        self.assertEqual(result["validation_errors"], [])
        entries = {e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]}
        self.assertEqual(entries["kubernetes"]["status"], "grill_question")
        for entry in entries.values():
            for ev in entry["evidence"]:
                self.assertIn(ev["quote"], resume)
                self.assertNotIn("Kubernetes", ev["quote"])

    def test_via_alias_honest_attribution(self) -> None:
        # via_alias is True only when the match genuinely came via alias
        # normalization — a literal keyword hit is never misattributed,
        # even though the decoder normalizes skills internally.
        from initiatives.i04.resume_decoder import decode_resume

        alias_only = (
            "Experience\nEngineer\n"
            "- Ran k8s in production across 12 services\n"
        )
        decoded = decode_resume(alias_only)
        found, via_alias = evidence_map._find_evidence(
            decoded, "kubernetes", alias_only
        )
        self.assertIsNotNone(found)
        self.assertTrue(via_alias)
        self.assertEqual(found["matched_as"], "k8s")
        self.assertIn(found["quote"], alias_only)

        literal = (
            "Experience\nEngineer\n"
            "- Ran Kubernetes in production across 12 services\n"
        )
        decoded_literal = decode_resume(literal)
        found2, via_alias2 = evidence_map._find_evidence(
            decoded_literal, "kubernetes", literal
        )
        self.assertIsNotNone(found2)
        self.assertFalse(via_alias2)
        self.assertIn(found2["quote"], literal)

    def test_lookup_candidates_case_insensitive_canonical(self) -> None:
        # A lowercased canonical keyword must still reach the alias
        # table: _lookup_candidates("kubernetes") must include "k8s".
        cands = evidence_map._lookup_candidates("kubernetes")
        self.assertIn("k8s", cands)
        cands_upper = evidence_map._lookup_candidates("KUBERNETES")
        self.assertIn("k8s", cands_upper)
        # ...and a canonical hit is not misreported as an alias hit.
        cands_alias = evidence_map._lookup_candidates("k8s")
        self.assertIn("Kubernetes", cands_alias)

    def test_source_quote_returns_none_when_no_jd_line_mentions_keyword(
        self,
    ) -> None:
        self.assertIsNone(
            evidence_map._source_quote("Requirements:\n- Team player\n", "cobol")
        )
        self.assertIsNotNone(
            evidence_map._source_quote(
                "Requirements:\n- 5+ years of Python experience\n", "python"
            )
        )

    def test_source_quote_is_verbatim_jd_line(self) -> None:
        # The phrase keyword "machine learning" is mentioned on single
        # JD lines (the line break sits between "with" and "Machine",
        # not inside the keyword): source_quote is the verbatim JD
        # line, never the bare keyword.
        result = evidence_map.build_evidence_map(
            SYNTHETIC_JD_LINE_SPAN, SYNTHETIC_RESUME, "syn-linespan"
        )
        self.assertEqual(result["validation_errors"], [])
        entries = {
            e["requirement"]["text"]: e
            for e in result["evidence_map"]["entries"]
        }
        self.assertIn("machine learning", entries)
        sq = entries["machine learning"]["requirement"]["source_quote"]
        self.assertIn(sq, SYNTHETIC_JD_LINE_SPAN)
        self.assertIn("machine learning", sq.lower())

    def test_supported_quote_capped_but_verbatim(self) -> None:
        # A very long evidence line is capped at 300 chars; the capped
        # prefix is still a verbatim substring.
        long_line = "- " + "Python " * 100 + "; shipped 7 releases"
        resume = f"Experience\nDeveloper\n{long_line}\n"
        jd = "Requirements:\n- Python experience required\n"
        result = evidence_map.build_evidence_map(jd, resume, "syn-long")
        self.assertEqual(result["validation_errors"], [])
        entries = {e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]}
        entry = entries["python"]
        self.assertEqual(entry["status"], "supported")
        quote = entry["evidence"][0]["quote"]
        self.assertLessEqual(len(quote), 300)
        self.assertIn(quote, resume)


class HonestDegradationTest(unittest.TestCase):
    """A keyword mention the map cannot quote honestly must never
    become a supported entry — it degrades to a grill question."""

    def test_redacted_contact_line_degrades_to_grill_question(self) -> None:
        # The keyword and a number share a line with a PII email. The
        # decoder redacts the address, so the decoded line is not
        # verbatim; the raw line cannot be substituted in either (it
        # carries the contact value the decoder redacted). The mention
        # is real, so this is a grill question — not a gap, and never
        # a supported quote.
        jd = "Requirements:\n- Python experience required\n"
        resume = (
            "Experience\nDeveloper\n"
            "- 6 years Python, reach me at jane.synth@example.com, "
            "shipped 5 releases\n"
        )
        result = evidence_map.build_evidence_map(jd, resume, "syn-pii2")
        self.assertEqual(result["validation_errors"], [])
        entries = {
            e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]
        }
        entry = entries["python"]
        self.assertEqual(entry["status"], "grill_question")
        self.assertTrue(entry["grill_question_id"])
        self.assertEqual(entry["evidence"], [])
        # No evidence quote anywhere carries the redacted contact value.
        for e in result["evidence_map"]["entries"]:
            for ev in e["evidence"]:
                self.assertNotIn("[email redacted]", ev["quote"])
                self.assertNotIn("jane.synth@example.com", ev["quote"])

    def test_emission_guard_degrades_unquotable_match(self) -> None:
        # Defense in depth: even if _find_evidence ever returned a
        # non-verbatim quote, build_evidence_map must not emit it as
        # supported.
        jd = "Requirements:\n- Python experience required\n"
        resume = "Experience\nDeveloper\n- Used Python daily across 5 services\n"
        real = evidence_map._find_evidence

        def fake(decoded, keyword, resume_text):
            item, via_alias = real(decoded, keyword, resume_text)
            self.assertIsNotNone(item)
            return dict(item, quote="FABRICATED - not in the resume"), via_alias

        evidence_map._find_evidence = fake
        try:
            result = evidence_map.build_evidence_map(jd, resume, "syn-guard")
        finally:
            evidence_map._find_evidence = real
        self.assertEqual(result["validation_errors"], [])
        entries = {
            e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]
        }
        entry = entries["python"]
        self.assertEqual(entry["status"], "grill_question")
        self.assertTrue(entry["grill_question_id"])

    def test_skills_only_match_quotes_verbatim_source_line(self) -> None:
        # Keyword appears only in the skills section. The profile's
        # joined canonical skill list is a decoder construction, not
        # resume text — the quote must be the actual source line.
        jd = "Requirements:\n- Python experience required\n"
        resume = "Experience\nDeveloper\n- Did things\n\nSkills\nPython 3, Docker\n"
        result = evidence_map.build_evidence_map(jd, resume, "syn-skills")
        self.assertEqual(result["validation_errors"], [])
        entries = {
            e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]
        }
        entry = entries["python"]
        self.assertEqual(entry["status"], "supported")
        quote = entry["evidence"][0]["quote"]
        self.assertEqual(quote, "Python 3, Docker")
        self.assertIn(quote, resume)


class AliasAttributionEndToEndTest(unittest.TestCase):
    """via_alias, exercised through build_evidence_map."""

    def _entries(self, jd: str, resume: str, job_id: str):
        result = evidence_map.build_evidence_map(jd, resume, job_id)
        self.assertEqual(result["validation_errors"], [])
        entries = {
            e["requirement"]["text"]: e for e in result["evidence_map"]["entries"]
        }
        companions = {
            q["grill_question_id"]: q for q in result["suggested_grill_questions"]
        }
        return entries, companions

    def test_via_alias_reports_resume_wording(self) -> None:
        # JD says Kubernetes; the resume only ever says "k8s" (skills
        # section). The grill question must name the resume's actual
        # wording, not the decoder's canonical form.
        jd = "Requirements:\n- Kubernetes experience required\n"
        resume = "Experience\nEngineer\n- Did things\n\nSkills\nk8s, Docker\n"
        entries, companions = self._entries(jd, resume, "syn-va")
        entry = entries["kubernetes"]
        self.assertEqual(entry["status"], "grill_question")
        question = companions[entry["grill_question_id"]]["question"]
        self.assertIn("k8s", question)
        self.assertIn("only via an alias", question)

    def test_alias_jd_keyword_against_canonical_resume_wording(self) -> None:
        # JD says "k8s" (phrase extraction); the resume says
        # "Kubernetes". Genuine alias match -> grill question.
        # (The period terminates the extracted phrase: without it the
        # extractor yields "k8s in production", a different keyword.)
        jd = "Requirements:\n- Experience with k8s.\n"
        resume = "Experience\nEngineer\n- Ran Kubernetes across 12 clusters\n"
        entries, _ = self._entries(jd, resume, "syn-va2")
        self.assertEqual(entries["k8s"]["status"], "grill_question")

    def test_literal_alias_keyword_match_is_not_via_alias(self) -> None:
        # Both sides say "k8s" — a literal match, fully supported with
        # a verbatim quote.
        jd = "Requirements:\n- Experience with k8s.\n"
        resume = "Experience\nEngineer\n- Ran k8s across 12 clusters\n"
        entries, _ = self._entries(jd, resume, "syn-va3")
        entry = entries["k8s"]
        self.assertEqual(entry["status"], "supported")
        self.assertIn(entry["evidence"][0]["quote"], resume)


if __name__ == "__main__":
    unittest.main()
