"""Tests for Initiative 05 earned-success feedback (Epic 6).

The delight gate is a release blocker: these tests encode the full
state matrix. Delight appears ONLY for user-initiated success with
Initiative 00's gate open; it is absent in every prohibited state,
always dismissible, and inert under reduced motion.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from initiatives.i05 import success_feedback


class GateMatrixTest(unittest.TestCase):
    def test_delight_only_for_user_initiated_gated_success(self):
        for row in success_feedback.state_matrix():
            expected = (
                row["state"] == "success"
                and row["gate_open"]
                and row["user_initiated"]
            )
            self.assertEqual(
                row["allowed"], expected,
                f"wrong gate for {row}")

    def test_prohibited_states_never_delight(self):
        for state in success_feedback.PROHIBITED_STATES:
            moment = success_feedback.success_moment(
                "packet-approved",
                {"state": state, "gate_open": True,
                 "user_initiated": True})
            self.assertFalse(moment["allowed"])
            self.assertIsNone(moment["message"])

    def test_gate_closed_blocks_even_on_success(self):
        moment = success_feedback.success_moment(
            "packet-approved",
            {"state": "success", "gate_open": False,
             "user_initiated": True})
        self.assertFalse(moment["allowed"])

    def test_system_initiated_success_does_not_delight(self):
        moment = success_feedback.success_moment(
            "packet-approved",
            {"state": "success", "gate_open": True,
             "user_initiated": False})
        self.assertFalse(moment["allowed"])

    def test_unknown_kind_never_delights(self):
        moment = success_feedback.success_moment(
            "confetti-explosion",
            {"state": "success", "gate_open": True,
             "user_initiated": True})
        self.assertFalse(moment["allowed"])

    def test_unknown_state_fails_closed(self):
        # A missing state must not default to the permissive value.
        moment = success_feedback.success_moment(
            "packet-approved",
            {"gate_open": True, "user_initiated": True})
        self.assertFalse(moment["allowed"])
        self.assertIsNone(moment["message"])

    def test_allowed_moment_has_message_and_key(self):
        moment = success_feedback.success_moment(
            "packet-approved",
            {"state": "success", "gate_open": True,
             "user_initiated": True})
        self.assertTrue(moment["allowed"])
        self.assertTrue(moment["message"])
        self.assertTrue(moment["dismiss_key"])


class DismissalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dismissed_moment_never_resurfaces(self):
        ctx = {"state": "success", "gate_open": True,
               "user_initiated": True}
        moment = success_feedback.success_moment(
            "version-approved", ctx, state_dir=self.state_dir)
        self.assertTrue(moment["allowed"])
        self.assertTrue(
            success_feedback.dismiss(moment["dismiss_key"],
                                     state_dir=self.state_dir))
        self.assertTrue(
            success_feedback.is_dismissed(
                moment["dismiss_key"], state_dir=self.state_dir))
        again = success_feedback.success_moment(
            "version-approved", ctx, state_dir=self.state_dir)
        self.assertFalse(again["allowed"])
        self.assertIsNone(again["message"])

    def test_double_dismiss_is_idempotent(self):
        key = "i05:review-complete"
        self.assertTrue(
            success_feedback.dismiss(key, state_dir=self.state_dir))
        self.assertFalse(
            success_feedback.dismiss(key, state_dir=self.state_dir))

    def test_dismissal_persists_across_reads(self):
        key = "i05:evidence-approved"
        success_feedback.dismiss(key, state_dir=self.state_dir)
        # Fresh load from disk.
        self.assertTrue(
            success_feedback.is_dismissed(key, state_dir=self.state_dir))


class ReducedMotionTest(unittest.TestCase):
    def test_animation_is_always_none(self):
        # Even in the most permissive context, the payload carries no
        # animation directives — delight is words, not motion.
        for ctx in (
            {"state": "success", "gate_open": True,
             "user_initiated": True, "reduced_motion": True},
            {"state": "success", "gate_open": True,
             "user_initiated": True, "reduced_motion": False},
            {"state": "success", "gate_open": True,
             "user_initiated": True},
        ):
            moment = success_feedback.success_moment("packet-approved", ctx)
            self.assertIsNone(moment["animation"])

    def test_reduced_motion_flag_round_trips(self):
        moment = success_feedback.success_moment(
            "packet-approved",
            {"state": "success", "gate_open": True,
             "user_initiated": True, "reduced_motion": True})
        self.assertTrue(moment["reduced_motion"])
        self.assertTrue(moment["allowed"])  # motion never blocks delight


if __name__ == "__main__":
    unittest.main()
