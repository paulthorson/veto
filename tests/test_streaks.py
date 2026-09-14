"""Offline tests for streaks.py. No network; store redirected to tmp dir."""

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import streaks


def _ts(day: date) -> float:
    """Noon local on the given day — safely inside the day boundary."""
    return datetime(day.year, day.month, day.day, 12, 0).timestamp()


class StreaksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self._old_store = streaks.STORE_PATH
        self._old_today = streaks._TODAY_OVERRIDE
        streaks.STORE_PATH = Path(self.tmp.name) / "streaks.json"
        streaks._TODAY_OVERRIDE = date(2026, 9, 10)  # a Thursday

    def tearDown(self):
        streaks.STORE_PATH = self._old_store
        streaks._TODAY_OVERRIDE = self._old_today
        self.tmp.cleanup()

    def _log_days_ago(self, kind, days_ago_list):
        today = date(2026, 9, 10)
        for n in days_ago_list:
            streaks.log_rep(kind, ts=_ts(today - timedelta(days=n)))

    # -- log_rep ------------------------------------------------------
    def test_log_rep_stores_entry(self):
        res = streaks.log_rep("drill")
        self.assertTrue(res["ok"])
        self.assertEqual(res["rep"]["kind"], "drill")
        self.assertEqual(res["today_count"], 1)
        data = json.loads(streaks.STORE_PATH.read_text())
        self.assertEqual(len(data["reps"]), 1)

    def test_log_rep_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            streaks.log_rep("nap")

    # -- goals --------------------------------------------------------
    def test_default_goals(self):
        g = streaks.goals()["goals"]
        self.assertEqual(g["drill"], streaks.DEFAULT_GOALS["drill"])
        self.assertEqual(set(g), set(streaks.REP_KINDS))

    def test_set_goal_override(self):
        streaks.set_goal("drill", 5)
        self.assertEqual(streaks.goals()["goals"]["drill"], 5)

    def test_set_goal_rejects_bad_target(self):
        with self.assertRaises(ValueError):
            streaks.set_goal("drill", 0)
        with self.assertRaises(ValueError):
            streaks.set_goal("drill", -3)
        with self.assertRaises(ValueError):
            streaks.set_goal("nap", 3)

    # -- streaks ------------------------------------------------------
    def test_current_streak_consecutive(self):
        self._log_days_ago("drill", [0, 1, 2])
        s = streaks.streaks()
        self.assertEqual(s["kinds"]["drill"]["current"], 3)
        self.assertEqual(s["kinds"]["drill"]["longest"], 3)

    def test_streak_forgives_empty_today(self):
        # Reps yesterday + day before, nothing today: streak still alive.
        self._log_days_ago("drill", [1, 2])
        self.assertEqual(streaks.streaks()["kinds"]["drill"]["current"], 2)

    def test_streak_breaks_after_full_missed_day(self):
        self._log_days_ago("drill", [2, 3])
        self.assertEqual(streaks.streaks()["kinds"]["drill"]["current"], 0)

    def test_longest_picks_best_run(self):
        self._log_days_ago("drill", [0, 1, 2, 5, 6])
        s = streaks.streaks()["kinds"]["drill"]
        self.assertEqual(s["longest"], 3)
        self.assertEqual(s["current"], 3)

    def test_overall_counts_any_kind(self):
        self._log_days_ago("drill", [0])
        self._log_days_ago("ai_lesson", [1])
        self._log_days_ago("application", [2])
        overall = streaks.streaks()["overall"]
        self.assertEqual(overall["current"], 3)
        self.assertEqual(overall["today"], 1)

    def test_today_counts_vs_goals(self):
        streaks.set_goal("drill", 5)
        self._log_days_ago("drill", [0, 0, 0])
        info = streaks.streaks()["kinds"]["drill"]
        self.assertEqual(info["today"], 3)
        self.assertEqual(info["goal"], 5)

    # -- today() ------------------------------------------------------
    def test_today_progress_bar_format(self):
        streaks.set_goal("drill", 5)
        self._log_days_ago("drill", [0, 0, 0])
        out = streaks.today()
        drill_line = next(l for l in out["lines"] if l.startswith("drills"))
        self.assertIn("[######----]", drill_line)
        self.assertIn("3/5", drill_line)

    # -- weekly_recap -------------------------------------------------
    def test_weekly_recap_totals_and_encouragement(self):
        self._log_days_ago("drill", [0, 0, 1, 2, 6])
        self._log_days_ago("application", [0])
        recap = streaks.weekly_recap()
        self.assertEqual(recap["totals"]["drill"], 5)
        self.assertEqual(recap["totals"]["application"], 1)
        self.assertEqual(recap["total_reps"], 6)
        # Encouragement must reference the real numbers, not generic fluff.
        self.assertIn("5", recap["encouragement"])
        self.assertIn("drills", recap["encouragement"])

    def test_weekly_recap_empty_week(self):
        recap = streaks.weekly_recap()
        self.assertEqual(recap["total_reps"], 0)
        self.assertIn("No reps logged this week yet", recap["encouragement"])

    def test_weekly_recap_detects_lost_streak(self):
        # 4-day streak 9-12 days ago, nothing since.
        self._log_days_ago("negotiation_round", [9, 10, 11, 12])
        recap = streaks.weekly_recap()
        self.assertTrue(
            any("ended at 4 days" in s for s in recap["streaks_lost"]),
            recap["streaks_lost"],
        )

    def test_weekly_recap_detects_new_record(self):
        self._log_days_ago("drill", [0, 1, 2])
        recap = streaks.weekly_recap()
        self.assertTrue(
            any("new record 3-day streak" in s for s in recap["streaks_gained"]),
            recap["streaks_gained"],
        )

    # -- share_card ---------------------------------------------------
    def test_share_card_only_real_reps(self):
        self._log_days_ago("negotiation_round", [0, 1])
        self._log_days_ago("drill", [0])
        card = streaks.share_card()["card"]
        self.assertIn("2 negotiation rounds", card)
        self.assertIn("1 drills", card)
        # Kinds with zero reps must not appear.
        self.assertNotIn("AI lessons", card)
        self.assertNotIn("mock interviews", card)

    def test_share_card_empty(self):
        card = streaks.share_card()["card"]
        self.assertIn("No reps logged yet", card)

    # -- robustness ---------------------------------------------------
    def test_corrupt_store_tolerated(self):
        streaks.STORE_PATH.write_text("not json{{{")
        s = streaks.streaks()
        self.assertEqual(s["overall"]["current"], 0)

    # -- CLI ----------------------------------------------------------
    def test_cli_register_and_log(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = streaks.register_cli(sub)
        self.assertIn("streaks", handlers)
        args = argparse.Namespace(
            action="log", kind="drill", target=None, json=True
        )
        self.assertEqual(handlers["streaks"](args), 0)
        self.assertEqual(streaks.streaks()["kinds"]["drill"]["today"], 1)

    def test_cli_log_requires_kind(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = streaks.register_cli(sub)
        args = argparse.Namespace(action="log", kind=None, target=None, json=True)
        self.assertEqual(handlers["streaks"](args), 1)

    # -- MCP wiring ---------------------------------------------------
    def test_register_tools_smoke(self):
        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn

                return deco

        streaks.register_tools(FakeMCP())
        for name in (
            "log_streak_rep",
            "streak_status",
            "today_progress",
            "set_streak_goal",
            "streak_goals",
            "weekly_recap_report",
            "share_progress_card",
        ):
            self.assertIn(name, seen)
        seen["log_streak_rep"]("ai_lesson")
        self.assertEqual(seen["streak_status"]()["kinds"]["ai_lesson"]["today"], 1)


if __name__ == "__main__":
    unittest.main()
