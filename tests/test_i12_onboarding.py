#!/usr/bin/env python3
"""Initiative 12 / Epic 5 — onboarding experiment tests (no dark patterns)."""

import unittest

from initiatives.i12 import onboarding
from initiatives.i12.onboarding import (
    CONSENT_TOGGLES,
    DARK_PATTERN_OVERRIDES,
    OVERRIDE_AUDIT,
    DarkPatternDetected,
    OnboardingStep,
    PreCheckedConsent,
    assign_path,
    assert_no_dark_patterns,
    build_paths,
    check_consent_defaults,
    check_dark_patterns,
    detect_qualified_activation,
    experiment_summary,
    register_consent_toggle,
    register_dark_pattern_override,
)


class DarkPatternTest(unittest.TestCase):
    def test_false_scarcity_caught(self):
        findings = check_dark_patterns("Only 3 spots left! Sign up now.")
        self.assertTrue(any(f["pattern"] == "false scarcity" for f in findings))

    def test_false_urgency_caught(self):
        findings = check_dark_patterns("Hurry, act now before it's gone!")
        self.assertTrue(any(f["pattern"] == "false urgency" for f in findings))

    def test_confirm_shaming_caught(self):
        findings = check_dark_patterns(
            "No, I hate getting interviews — skip this step")
        self.assertTrue(any(f["pattern"] == "confirm-shaming" for f in findings))

    def test_clean_copy_passes(self):
        findings = check_dark_patterns(
            "Paste any job description. Veto decodes it on your device — "
            "nothing is uploaded, and you don't need an account.")
        self.assertEqual(findings, [])

    def test_step_construction_rejects_dark_patterns(self):
        with self.assertRaises(DarkPatternDetected):
            OnboardingStep(id="x", title="Hurry!", body="Only 2 left!",
                           cta="Act now")

    def test_all_builtin_steps_are_clean(self):
        # build_paths would raise at definition time if any step were dirty;
        # this asserts the full grid builds.
        paths = build_paths()
        self.assertEqual(len(paths), 9)


class ExperimentDesignTest(unittest.TestCase):
    def test_nine_cells(self):
        paths = build_paths()
        self.assertEqual(len(paths), 3 * 3)
        for rm in onboarding.ROLE_MATURITY:
            for tc in onboarding.TECH_COMFORT:
                self.assertIn(f"onboard-{rm}-{tc}", paths)

    def test_assignment_deterministic(self):
        a = assign_path("session-123", "switching", "low")
        b = assign_path("session-123", "switching", "low")
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.goal_workflow, "risk_check")

    def test_assignment_unknown_cell_rejected(self):
        with self.assertRaises(ValueError):
            assign_path("s", "nope", "low")

    def test_summary_shape(self):
        s = experiment_summary()
        self.assertEqual(len(s["paths"]), 9)
        self.assertIn("qualified_activation", s["primary_outcome"])
        self.assertIn("never application volume", s["primary_outcome"])

    def test_every_path_has_goal_workflow(self):
        for p in build_paths().values():
            self.assertTrue(p.goal_workflow)
            self.assertTrue(p.steps)


class StrengthenedRulesTest(unittest.TestCase):
    """Each banned category from the docstring must catch its documented
    bypass strings (blind-review KICK_BACK findings)."""

    def assert_caught(self, text, pattern_name):
        findings = check_dark_patterns(text)
        self.assertTrue(
            any(f["pattern"] == pattern_name for f in findings),
            f"{text!r} was not caught as {pattern_name}: {findings}")

    def test_resetting_countdown_caught(self):
        self.assert_caught(
            "Your private offer expires in 04:59 — claim it below.",
            "false urgency")
        self.assert_caught("04:59 left to lock in your price", "false urgency")
        self.assert_caught("Only 04:59 remaining — hurry!", "false urgency")

    def test_nagging_loop_caught(self):
        self.assert_caught(
            "Wait! Are you sure? Users who skip this step almost never "
            "get hired.",
            "nagging loop")
        self.assert_caught("Are you sure you want to skip this step?",
                           "nagging loop")

    def test_confirm_shaming_decline_variant_caught(self):
        self.assert_caught("No thanks, I'd rather not get hired.",
                           "confirm-shaming")

    def test_roach_motel_delete_via_email_caught(self):
        self.assert_caught(
            "To delete your account, email privacy@example.com.",
            "roach motel")

    def test_hidden_cost_paywall_without_free_caught(self):
        self.assert_caught("Unlock all features for only $9/month.",
                           "hidden cost")
        self.assert_caught("After your trial you'll be charged $29.",
                           "hidden cost")

    def test_all_eight_step_definitions_stay_clean(self):
        """The strengthened rules must not false-positive on existing copy."""
        steps = {s.id: s for p in build_paths().values() for s in p.steps}
        self.assertEqual(len(steps), 8)  # 8 step definitions across 9 paths
        for sid, step in sorted(steps.items()):
            findings = check_dark_patterns(
                f"{step.title}\n{step.body}\n{step.cta}")
            live = [f for f in findings if f.get("overridden") != "true"]
            self.assertEqual(live, [], f"step {sid} flagged: {live}")

    def test_no_override_needed_for_existing_copy(self):
        self.assertEqual(DARK_PATTERN_OVERRIDES, {})


class ConsentLayerTest(unittest.TestCase):
    def test_pre_checked_consent_rejected_at_registration(self):
        with self.assertRaises(PreCheckedConsent):
            register_consent_toggle("test-x", default_on=True,
                                    description="test toggle",
                                    surface="test")
        # Registration of the attempt is audited even though it raised.
        self.assertIn("test-x", CONSENT_TOGGLES)
        del CONSENT_TOGGLES["test-x"]

    def test_check_consent_defaults_fails_closed(self):
        CONSENT_TOGGLES["test-bad"] = {"default_on": True}
        try:
            with self.assertRaises(PreCheckedConsent):
                check_consent_defaults()
        finally:
            del CONSENT_TOGGLES["test-bad"]
        self.assertEqual(
            check_consent_defaults(), ["analytics-opt-in"])

    def test_analytics_opt_in_registered_default_off(self):
        self.assertFalse(CONSENT_TOGGLES["analytics-opt-in"]["default_on"])

    def test_docstring_does_not_claim_copy_checker_bans_consent(self):
        # The honest split: copy rules never list pre-checked consent.
        names = {name for _, name, _ in onboarding.DARK_PATTERN_RULES}
        self.assertNotIn("pre-checked consent", names)


class OverrideHatchTest(unittest.TestCase):
    def tearDown(self):
        DARK_PATTERN_OVERRIDES.clear()
        OVERRIDE_AUDIT.clear()

    def test_override_suppresses_exact_matched_text_only(self):
        copy = "Only 3 spots left in this honest, real workshop!"
        findings = check_dark_patterns(copy, apply_overrides=False)
        self.assertTrue(findings)
        matched = findings[0]["matched_text"]
        register_dark_pattern_override(
            findings[0]["pattern"], matched,
            reason="real workshop with 3 seats; scarcity is evidenced",
            approved_by="review-board")
        # Exact text now passes through assert...
        assert_no_dark_patterns(copy, "test")
        # ...but is still reported as overridden (auditable, not silent).
        reported = check_dark_patterns(copy)
        self.assertEqual(reported[0].get("overridden"), "true")
        self.assertEqual(reported[0]["override_approved_by"], "review-board")
        # A different string with the same pattern still raises.
        with self.assertRaises(DarkPatternDetected):
            assert_no_dark_patterns("Only 9 spots left!", "test2")

    def test_override_audit_trail(self):
        register_dark_pattern_override("false urgency", "Hurry",
                                       reason="r", approved_by="a")
        self.assertEqual(len(OVERRIDE_AUDIT), 1)
        self.assertEqual(OVERRIDE_AUDIT[0]["approved_by"], "a")
        self.assertIn("approved_at", OVERRIDE_AUDIT[0])


class AssignmentIdentityTest(unittest.TestCase):
    def test_no_phantom_variant_suffix(self):
        path = assign_path("session-xyz", "new", "low")
        self.assertEqual(path.id, "onboard-new-low")
        self.assertNotRegex(path.id, r"-v\d+$")

    def test_summary_paths_match_assignable_ids(self):
        assignable = {assign_path("s", rm, tc).id
                      for rm in onboarding.ROLE_MATURITY
                      for tc in onboarding.TECH_COMFORT}
        summarized = {p["id"] for p in experiment_summary()["paths"]}
        self.assertEqual(assignable, summarized)
        self.assertEqual(len(summarized), 9)

    def test_assignment_logged_without_raw_token(self):
        before = len(onboarding.ASSIGNMENT_LOG)
        assign_path("super-secret-token", "senior", "high")
        entry = onboarding.ASSIGNMENT_LOG[-1]
        self.assertEqual(len(onboarding.ASSIGNMENT_LOG), before + 1)
        self.assertEqual(entry["path_id"], "onboard-senior-high")
        blob = str(entry)
        self.assertNotIn("super-secret-token", blob)
        self.assertIn("diagnostic_bucket", entry)
        self.assertIn("assigned_at", entry)

    def test_analysis_plan_forbids_cross_cell_raw_comparison(self):
        plan = experiment_summary()["analysis_plan"]
        self.assertIn("INVALID", plan)
        self.assertIn("within-cell", plan)


class OutcomeDetectionTest(unittest.TestCase):
    def _path(self):
        return build_paths()["onboard-switching-low"]  # goal: risk_check

    def test_activated_on_goal_workflow_completion(self):
        events = [
            {"type": "workflow_completed", "workflow": "risk_check",
             "completed": True, "duration_s": 42, "session_id": "s1"},
            {"type": "onboarding_step_completed", "step_id": "welcome"},
        ]
        verdict = detect_qualified_activation(events, self._path())
        self.assertTrue(verdict["activated"])
        self.assertEqual(verdict["workflow"], "risk_check")
        event = verdict["event"]
        self.assertEqual(set(event),
                         set(onboarding.QUALIFIED_ACTIVATION_SCHEMA))
        self.assertEqual(event["path_id"], "onboard-switching-low")
        self.assertEqual(event["steps_completed"], 1)

    def test_not_activated_by_other_workflow_only(self):
        events = [{"type": "workflow_completed", "workflow": "jd_decode",
                   "completed": True, "session_id": "s1"}]
        verdict = detect_qualified_activation(events, self._path())
        self.assertFalse(verdict["activated"])
        self.assertIsNone(verdict["event"])

    def test_not_activated_by_incomplete_workflow(self):
        events = [{"type": "workflow_completed", "workflow": "risk_check",
                   "completed": False, "session_id": "s1"}]
        self.assertFalse(
            detect_qualified_activation(events, self._path())["activated"])

    def test_empty_session_not_activated(self):
        verdict = detect_qualified_activation([], self._path())
        self.assertFalse(verdict["activated"])
        self.assertIn("none", verdict["reason"])

    def test_role_compare_goal_is_defined(self):
        self.assertIn("role_compare", onboarding.GOAL_WORKFLOWS)
        path = build_paths()["onboard-senior-high"]
        self.assertEqual(path.goal_workflow, "role_compare")

    def test_unknown_goal_workflow_rejected(self):
        from initiatives.i12.onboarding import OnboardingPath
        with self.assertRaises(ValueError):
            OnboardingPath(id="x", role_maturity="new", tech_comfort="low",
                           steps=(), goal_workflow="apply_spam")


if __name__ == "__main__":
    unittest.main()
