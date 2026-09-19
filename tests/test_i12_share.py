#!/usr/bin/env python3
"""Initiative 12 / Epic 2 — shareable artifact tests (privacy-safe)."""

import json
import unittest

from initiatives.i12 import share
from initiatives.i12.privacy import ContentDetected


def _factors():
    return [{"name": "skills", "score": 82,
             "evidence_summary": "Python + distributed systems match posting needs"}]


def _clean_card():
    return share.score_card("Backend Engineer", "Acme", 82.5, _factors(),
                            "strong", ["Clear salary range"])


class ScoreCardTest(unittest.TestCase):
    def test_clean_card_builds(self):
        card = share.score_card("Backend Engineer", "Acme", 82.5, _factors(),
                                "strong", ["Clear salary range",
                                            "Concrete responsibilities"])
        self.assertEqual(card["kind"], "score_card")
        self.assertEqual(card["payload"]["fit_score"], 82.5)
        self.assertIn("methodology", card)
        self.assertIn("privacy_note", card)

    def test_resume_text_rejected(self):
        bad_factors = [{"name": "skills", "score": 90,
                        "evidence_summary": "Work Experience: Senior Engineer at "
                                            "Initech 2020-2024, led payments..."}]
        with self.assertRaises(ContentDetected):
            share.score_card("Backend Engineer", "Acme", 90, bad_factors,
                             "strong", ["ok"])

    def test_email_in_label_rejected(self):
        with self.assertRaises(ContentDetected):
            share.score_card("Backend Engineer", "someone@example.com", 80,
                             _factors(), "strong", ["ok"])

    def test_long_evidence_summary_rejected(self):
        bad = [{"name": "skills", "score": 90,
                "evidence_summary": "x" * 200}]
        with self.assertRaises(ValueError):
            share.score_card("Backend Engineer", "Acme", 90, bad,
                             "strong", ["ok"])


class InterviewPlanTest(unittest.TestCase):
    def test_clean_plan_builds(self):
        plan = share.interview_plan(
            "Backend Engineer",
            [{"area": "trade-offs", "why_short": "scored below bar twice",
              "drills": 3}],
            4)
        self.assertEqual(plan["kind"], "interview_plan")
        self.assertEqual(plan["payload"]["sessions_planned"], 4)


class ProgressSnapshotTest(unittest.TestCase):
    def test_counts_only(self):
        snap = share.progress_snapshot(
            3, {"jd_decode": 12, "risk_check": 5}, outcomes_recorded=4)
        self.assertEqual(snap["kind"], "progress_snapshot")
        self.assertEqual(snap["payload"]["workflows_completed"]["jd_decode"], 12)

    def test_no_company_detail_allowed(self):
        with self.assertRaises((ContentDetected, ValueError)):
            share.progress_snapshot(3, {"applied to Acme Corp!!": 1}, 0)


class RenderTest(unittest.TestCase):
    def test_markdown_has_methodology(self):
        card = share.score_card("Backend Engineer", "Acme", 82.5, _factors(),
                                "strong", ["Clear salary range"])
        md = share.render_markdown(card)
        self.assertIn(share.METHODOLOGY_URL, md)
        self.assertIn("82.5", md)
        self.assertIn("never includes", md)

    def test_methodology_link_is_not_a_relative_path(self):
        # C6: a relative repo path is meaningless once markdown leaves the
        # machine. Until the operator sets the real public URL, it must be an
        # explicit TBD placeholder — never an invented URL.
        self.assertTrue(share.METHODOLOGY_URL.startswith("TBD"))
        self.assertNotIn("docs/", share.METHODOLOGY_URL)
        self.assertNotIn("http", share.METHODOLOGY_URL)


class BuilderRejectionTest(unittest.TestCase):
    """C3: every builder raises ContentDetected on content-bearing input
    and builds on clean input."""

    def test_score_card_rejects_bare_name(self):
        # C2: a person's name in the whitelisted "name" field is content.
        bad = [{"name": "Alex Rivera", "score": 90,
                "evidence_summary": "clean summary"}]
        with self.assertRaises(ContentDetected):
            share.score_card("Backend Engineer", "Acme", 90, bad,
                             "strong", ["ok"])

    def test_score_card_rejects_quoted_prose(self):
        # C5: a long quoted span reads as verbatim pasted content.
        quote = ('Manager wrote "she consistently delivered complex platform '
                 'migrations ahead of schedule every single quarter" truly')
        bad = [{"name": "skills", "score": 90, "evidence_summary": quote}]
        with self.assertRaises(ContentDetected):
            share.score_card("Backend Engineer", "Acme", 90, bad,
                             "strong", ["ok"])

    def test_interview_plan_rejects_phone(self):
        with self.assertRaises(ContentDetected):
            share.interview_plan(
                "Backend Engineer",
                [{"area": "trade-offs",
                  "why_short": "call 415-555-0132 to discuss",
                  "drills": 3}],
                4)

    def test_interview_plan_rejects_email(self):
        with self.assertRaises(ContentDetected):
            share.interview_plan(
                "jane.doe@example.com",
                [{"area": "trade-offs", "why_short": "scored below bar",
                  "drills": 3}],
                4)

    def test_interview_plan_clean_builds(self):
        plan = share.interview_plan(
            "Backend Engineer",
            [{"area": "trade-offs", "why_short": "scored below bar twice",
              "drills": 3}],
            4)
        self.assertEqual(plan["kind"], "interview_plan")

    def test_progress_snapshot_rejects_content_key(self):
        # "resume" passes the token check but is a forbidden field name.
        with self.assertRaises(ContentDetected):
            share.progress_snapshot(3, {"resume": 1}, 0)

    def test_progress_snapshot_clean_builds(self):
        snap = share.progress_snapshot(
            3, {"jd_decode": 12}, outcomes_recorded=4)
        self.assertEqual(snap["kind"], "progress_snapshot")


class ImmutabilityTest(unittest.TestCase):
    """C1 (part 1): built artifacts are deep-frozen — post-scan mutation
    is impossible, so it cannot be a publish bypass."""

    def test_top_level_mutation_blocked(self):
        card = _clean_card()
        with self.assertRaises(TypeError):
            card["payload"] = {}

    def test_nested_mutation_blocked(self):
        card = _clean_card()
        with self.assertRaises(TypeError):
            card["payload"]["role"] = "jane.doe@example.com"

    def test_nested_list_mutation_blocked(self):
        card = _clean_card()
        with self.assertRaises(AttributeError):
            card["payload"]["top_reasons"].append("evil")

    def test_dict_helpers_blocked(self):
        card = _clean_card()
        for op in (lambda: card.update({}), lambda: card.pop("kind"),
                   lambda: card.setdefault("x", 1), lambda: card.clear()):
            with self.assertRaises(TypeError):
                op()

    def test_frozen_artifact_still_serializes(self):
        card = _clean_card()
        round_tripped = json.loads(json.dumps(card))
        self.assertEqual(round_tripped["payload"]["fit_score"], 82.5)


class RenderGateTest(unittest.TestCase):
    """C1 (part 2): render_markdown re-scans at render time (fail closed).

    The old bypass: build clean, mutate the mutable dict, render — the
    published bytes were never scanned. Both halves are now closed.
    """

    def test_render_rescans_forged_dict(self):
        # A dict that never went through a builder, carrying content.
        forged = {"share_version": "1", "kind": "score_card",
                  "methodology": share.METHODOLOGY_URL,
                  "privacy_note": "x",
                  "payload": {"role": "Backend Engineer", "company": "Acme",
                              "fit_score": 80.0, "verdict": "strong",
                              "top_reasons": [],
                              "factors": [{"name": "skills", "score": 80,
                                            "evidence_summary":
                                            "reach me at jane.doe@example.com"}]}}
        with self.assertRaises(ContentDetected):
            share.render_markdown(forged)

    def test_render_catches_tampered_serialization(self):
        # The exact old bypass shape: clean build -> serialize -> tamper ->
        # render. The tampered bytes must never publish.
        card = _clean_card()
        tampered = json.loads(json.dumps(card))
        tampered["payload"]["role"] = "Backend Engineer jane.doe@example.com"
        with self.assertRaises(ContentDetected):
            share.render_markdown(tampered)

    def test_render_accepts_clean_plain_dict(self):
        card = _clean_card()
        plain = json.loads(json.dumps(card))  # plain dict, never frozen
        md = share.render_markdown(plain)
        self.assertIn("Backend Engineer", md)

    def test_render_fails_closed_on_deeply_nested_forged_dict(self):
        # F1: the old depth-6 cutoff silently passed content nested 7+
        # deep, so this hostile dict published its email through
        # render_markdown. The email sits 8 levels deep inside a
        # hand-built progress_snapshot dict — render must raise
        # ContentDetected and the email must not appear in any output.
        deep = {"email": "bob@example.com"}
        for i in range(8):
            deep = {f"l{i}": deep}
        forged = {"share_version": "1", "kind": "progress_snapshot",
                  "methodology": share.METHODOLOGY_URL,
                  "privacy_note": "x",
                  "payload": {"weeks_active": 3,
                              "workflows_completed": {"jd_decode": 4,
                                                      "smuggled": deep},
                              "outcomes_recorded": 1,
                              "note": "Counts only."}}
        with self.assertRaises(ContentDetected) as ctx:
            share.render_markdown(forged)
        self.assertNotIn("bob@example.com", str(ctx.exception))

    def test_render_clean_artifact(self):
        md = share.render_markdown(_clean_card())
        self.assertIn("# Veto Score Card", md)


class MalformedInputTest(unittest.TestCase):
    """C10: malformed input fails with a clear ValueError, never raw KeyError."""

    def test_non_dict_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            share.render_markdown("not a dict")
        self.assertIn("must be a dict", str(ctx.exception))

    def test_missing_payload_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            share.render_markdown({"kind": "score_card"})
        self.assertIn("payload", str(ctx.exception))

    def test_unknown_kind_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            share.render_markdown({"kind": "weird", "payload": {}})
        self.assertIn("unknown kind", str(ctx.exception))

    def test_malformed_factor_entry_raises_value_error(self):
        card = _clean_card()
        broken = json.loads(json.dumps(card))
        broken["payload"]["factors"] = ["not-a-dict"]
        with self.assertRaises(ValueError) as ctx:
            share.render_markdown(broken)
        self.assertIn("factor entry", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
