"""Initiative 06 WS-B: longitudinal practice tracking + drill recommendation.

Covers initiatives.i06.longitudinal (observed-gap rule, history,
progress, recommendations) and the skill_gaps.py integration.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import skill_gaps  # noqa: E402
from initiatives.i06 import longitudinal as L  # noqa: E402


def _rec(dim_scores, sid="s", **kw):
    return {"source": "mock_interview", "label": "lab", "mode": "classic",
            "session_id": sid, "job_id": "", "dimension_scores": dim_scores,
            "unscored_dimensions": [], "overall": 70,
            "completed_at": "2026-09-13T00:00:00+00:00", **kw}


GOOD = {"structure": 85, "evidence": 80, "clarity": 90, "trade_offs": 75,
        "question_quality": None}
BAD_EVIDENCE = {"structure": 85, "evidence": 40, "clarity": 90,
                "trade_offs": 75, "question_quality": None}


class TestObservedGapRule(unittest.TestCase):
    def test_defaults_match_roadmap_baseline(self):
        rule = L.ObservedGapRule()
        self.assertEqual((rule.window, rule.repeat_count, rule.threshold),
                         (3, 2, 70))
        self.assertIn("2 of the", rule.describe().replace("at least 2 of",
                                                          "2 of"))

    def test_validation(self):
        with self.assertRaises(ValueError):
            L.ObservedGapRule(window=0)
        with self.assertRaises(ValueError):
            L.ObservedGapRule(repeat_count=4, window=3)
        with self.assertRaises(ValueError):
            L.ObservedGapRule(threshold=101)

    def test_rule_is_parameterized(self):
        # A different approved threshold changes the verdict.
        history = [_rec(GOOD, sid="a"),
                   _rec({**GOOD, "evidence": 75}, sid="b"),
                   _rec({**GOOD, "evidence": 60}, sid="c")]
        strict = L.observed_gaps(history, L.ObservedGapRule(threshold=90))
        loose = L.observed_gaps(history, L.ObservedGapRule(threshold=70))
        self.assertTrue(any(g["dimension"] == "evidence"
                            for g in strict["gaps"]))
        self.assertFalse(any(g["dimension"] == "evidence"
                             for g in loose["gaps"]))


class TestObservedGaps(unittest.TestCase):
    def test_gap_needs_two_of_three_below(self):
        history = [_rec(BAD_EVIDENCE, sid="a"),
                   _rec(GOOD, sid="b"),
                   _rec(BAD_EVIDENCE, sid="c")]
        out = L.observed_gaps(history)
        self.assertEqual(len(out["gaps"]), 1)
        gap = out["gaps"][0]
        self.assertEqual(gap["dimension"], "evidence")
        self.assertEqual(gap["basis"], "observed")
        self.assertEqual(gap["sessions_below_threshold"], 2)
        self.assertEqual(gap["sessions_considered"], 3)

    def test_single_bad_session_is_not_a_gap(self):
        history = [_rec(GOOD, sid="a"), _rec(GOOD, sid="b"),
                   _rec(BAD_EVIDENCE, sid="c")]
        out = L.observed_gaps(history)
        self.assertEqual(out["gaps"], [])

    def test_unscored_dimensions_never_count(self):
        rec = _rec({"structure": 85}, sid="a")
        out = L.observed_gaps([rec, rec, rec])
        self.assertEqual(out["gaps"], [])

    def test_window_limits_history(self):
        old = [_rec(BAD_EVIDENCE, sid=f"old{i}") for i in range(5)]
        new = [_rec(GOOD, sid=f"new{i}") for i in range(3)]
        out = L.observed_gaps(old + new)
        self.assertEqual(out["gaps"], [])
        self.assertEqual(out["sessions_considered"], 3)
        self.assertEqual(out["sessions_total"], 8)


class TestFocus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._focus = Path(self._tmp.name) / "focus.json"

    def test_select_and_clear(self):
        f = L.select_focus("clarity", focus_path=self._focus)
        self.assertEqual(f["dimension"], "clarity")
        self.assertEqual(f["basis"], "user_selected")
        self.assertEqual(L.current_focus(self._focus)["dimension"],
                         "clarity")
        L.clear_focus(self._focus)
        self.assertIsNone(L.current_focus(self._focus))

    def test_unknown_dimension_rejected(self):
        with self.assertRaises(ValueError):
            L.select_focus("charisma", focus_path=self._focus)


class TestRecommend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._focus = Path(self._tmp.name) / "focus.json"

    def test_observed_gap_drives_specific_drill(self):
        history = [_rec(BAD_EVIDENCE, sid="a"),
                   _rec(GOOD, sid="b"),
                   _rec(BAD_EVIDENCE, sid="c")]
        out = L.recommend_next_drill(history, focus_path=self._focus)
        self.assertEqual(out["basis"], "observed")
        self.assertTrue(out["recommendations"])
        for rec in out["recommendations"]:
            self.assertEqual(rec["dimension"], "evidence")
            self.assertEqual(rec["basis"], "observed")
            # A drill names a lab, a mode, and a concrete rep — never a
            # generic course list.
            self.assertIn(rec["lab"], ("mock_interview", "soft_skills"))
            self.assertTrue(rec["drill"])
            self.assertIn("evidence", out["markdown"])

    def test_user_focus_beats_observed_gaps(self):
        history = [_rec(BAD_EVIDENCE, sid="a"),
                   _rec(BAD_EVIDENCE, sid="b"),
                   _rec(BAD_EVIDENCE, sid="c")]
        L.select_focus("clarity", focus_path=self._focus)
        out = L.recommend_next_drill(history, focus_path=self._focus)
        self.assertEqual(out["basis"], "user_selected")
        self.assertTrue(all(r["dimension"] == "clarity"
                            for r in out["recommendations"]))

    def test_no_gaps_no_focus_is_maintenance_not_weakness(self):
        history = [_rec(GOOD, sid="a"), _rec(GOOD, sid="b")]
        out = L.recommend_next_drill(history, focus_path=self._focus)
        self.assertEqual(out["basis"], "none")
        self.assertEqual(len(out["recommendations"]), 1)
        rec = out["recommendations"][0]
        self.assertIsNone(rec["dimension"])
        blob = json.dumps(out).lower()
        # No invented weakness labels: no dimension is ever called a
        # weakness. (Saying "nothing is labeled a weakness" is the
        # honest denial, and is allowed.)
        for label in ("weak at", "weakness is", "your weakness",
                      "poor performer"):
            self.assertNotIn(label, blob)
        self.assertNotIn("**evidence**", out["markdown"])
        self.assertIn("no observed gaps", out["markdown"].lower())


class TestProgress(unittest.TestCase):
    def test_trends_from_numbers_only(self):
        history = [
            _rec({"evidence": 40}, sid="a",
                 completed_at="2026-09-10T00:00:00+00:00"),
            _rec({"evidence": 55}, sid="b",
                 completed_at="2026-09-11T00:00:00+00:00"),
            _rec({"evidence": 80}, sid="c",
                 completed_at="2026-09-12T00:00:00+00:00"),
        ]
        out = L.progress_report(history)
        ev = next(d for d in out["dimensions"]
                  if d["dimension"] == "evidence")
        self.assertEqual(ev["trend"], "improving")
        self.assertGreater(ev["delta"], 0)
        self.assertTrue(ev["meets_threshold"])
        # Outcome note degrades gracefully when capture is inactive.
        # Isolated from ambient repo state: the repo root may or may not
        # carry an outcomes.jsonl (Initiative 01 capture), which must not
        # decide this test's result.
        inactive = ([], "No outcome events found (Initiative 01 capture not "
                        "active) — readiness below is practice-only.")
        with mock.patch.object(L, "_load_outcome_events",
                               return_value=inactive):
            out = L.progress_report(history)
        self.assertIn("practice-only", out["outcome_note"])

    def test_insufficient_data_labeled(self):
        out = L.progress_report([_rec({"evidence": 40}, sid="a")])
        ev = next(d for d in out["dimensions"]
                  if d["dimension"] == "evidence")
        self.assertEqual(ev["trend"], "insufficient_data")


class TestSkillGapsIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._history = tmp / "hist.json"
        self._focus = tmp / "focus.json"
        p1 = mock.patch.object(skill_gaps, "_PRACTICE_HISTORY_PATH",
                               self._history)
        p2 = mock.patch.object(skill_gaps, "_PRACTICE_FOCUS_PATH",
                               self._focus)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        for i, sid in enumerate(("a", "b", "c")):
            L.record_result(source="mock_interview", label="lab",
                            dimension_scores=BAD_EVIDENCE if i != 1 else GOOD,
                            mode="classic", session_id=sid,
                            history_path=self._history)

    def test_observe_finds_evidence_gap(self):
        out = skill_gaps.practice_observed_gaps()
        self.assertEqual([g["dimension"] for g in out["gaps"]],
                         ["evidence"])

    def test_recommend_uses_observed_gap(self):
        out = skill_gaps.recommend_next_drill()
        self.assertEqual(out["basis"], "observed")

    def test_focus_round_trip(self):
        skill_gaps.select_practice_focus("trade_offs")
        out = skill_gaps.recommend_next_drill()
        self.assertEqual(out["basis"], "user_selected")
        skill_gaps.clear_practice_focus()
        out = skill_gaps.recommend_next_drill()
        self.assertEqual(out["basis"], "observed")

    def test_progress_report(self):
        out = skill_gaps.practice_progress()
        self.assertTrue(out["dimensions"])


class TestSkillGapsCorruptHistory(unittest.TestCase):
    """Rule-1 regression: a corrupt practice-history file must surface
    loudly (history_ok False), never as a clean "no gaps" / empty
    report — the same defect fixed in the longitudinal engine."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._history = tmp / "hist.json"
        self._focus = tmp / "focus.json"
        self._history.write_text("{not valid json", encoding="utf-8")
        p1 = mock.patch.object(skill_gaps, "_PRACTICE_HISTORY_PATH",
                               self._history)
        p2 = mock.patch.object(skill_gaps, "_PRACTICE_FOCUS_PATH",
                               self._focus)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_observed_gaps_corrupt_history_is_loud(self):
        out = skill_gaps.practice_observed_gaps()
        self.assertFalse(out["history_ok"])
        self.assertIn("corrupt", out["history_note"].lower())
        self.assertEqual(out["gaps"], [])
        # Rule payload with the pending_approval gate still ships.
        self.assertTrue(out["rule"]["pending_approval"])

    def test_progress_corrupt_history_is_loud(self):
        out = skill_gaps.practice_progress()
        self.assertFalse(out["history_ok"])
        self.assertIn("corrupt", out["history_note"].lower())
        self.assertEqual(out["dimensions"], [])

    def test_recommend_corrupt_history_is_loud_never_clean(self):
        out = skill_gaps.recommend_next_drill()
        self.assertFalse(out["history_ok"])
        self.assertIn("corrupt", out["history_note"].lower())
        self.assertTrue(out["rule"]["pending_approval"])
        blob = json.dumps(out).lower()
        # Never a clean "no gaps" bill of health on corrupt history.
        self.assertNotIn("no observed gaps in your recent", blob)
        self.assertIn("could not be loaded", out["markdown"])

    def test_wrong_schema_history_is_corrupt(self):
        self._history.write_text(json.dumps({"oops": "not a list"}),
                                 encoding="utf-8")
        out = skill_gaps.practice_observed_gaps()
        self.assertFalse(out["history_ok"])
        self.assertIn("wrong schema", out["history_note"].lower())
        self.assertEqual(out["gaps"], [])

    def test_ok_path_unchanged(self):
        # Valid history through the seam behaves exactly as before.
        scores = {"structure": 85, "evidence": 40, "clarity": 90,
                  "trade_offs": 75, "question_quality": 80}
        self._history.write_text(json.dumps(
            [_rec(scores, sid="a"), _rec(scores, sid="b")]),
            encoding="utf-8")
        out = skill_gaps.practice_observed_gaps()
        self.assertTrue(out["history_ok"])
        self.assertEqual([g["dimension"] for g in out["gaps"]],
                         ["evidence"])
        out = skill_gaps.practice_progress()
        self.assertTrue(out["history_ok"])
        self.assertTrue(out["dimensions"])
        out = skill_gaps.recommend_next_drill()
        self.assertTrue(out["history_ok"])
        self.assertEqual(out["basis"], "observed")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# New regression tests for the KICK_BACK findings
# ---------------------------------------------------------------------------

def _gap_history():
    return [_rec(BAD_EVIDENCE, sid="a"),
            _rec(GOOD, sid="b"),
            _rec(BAD_EVIDENCE, sid="c")]


class TestLoadProvenance(unittest.TestCase):
    """Rule 1 (ops advocate): corrupt state must never look like absent
    state. Every public output carries history_ok; record_result
    refuses to overwrite corruption without explicit recovery; writes
    are atomic with a .bak backup."""

    def test_corrupt_history_is_surfaced_not_absent(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            p.write_text("{not valid json{{{")
            history, ok, note = L._read_history_store(p)
            self.assertFalse(ok)
            self.assertEqual(history, [])
            self.assertIn("corrupt", note.lower())
            with mock.patch.object(L, "HISTORY_PATH", p):
                focus = Path(td) / "focus.json"
                out = L.recommend_next_drill(focus_path=focus)
                self.assertFalse(out["history_ok"])
                self.assertEqual(out["basis"], "none")
                # Never a clean "no gaps" bill of health:
                self.assertNotIn("no observed gaps",
                                 out["markdown"].lower())
                self.assertIn("could not be loaded", out["markdown"])
                gaps = L.observed_gaps()
                self.assertFalse(gaps["history_ok"])
                self.assertEqual(gaps["gaps"], [])
                prog = L.progress_report()
                self.assertFalse(prog["history_ok"])

    def test_missing_history_is_fresh_not_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            history, ok, note = L._read_history_store(p)
            self.assertTrue(ok)
            self.assertEqual(history, [])
            self.assertIn("fresh", note.lower())

    def test_wrong_schema_history_is_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            p.write_text('{"not": "a list"}')
            _, ok, note = L._read_history_store(p)
            self.assertFalse(ok)
            self.assertIn("schema", note.lower())

    def test_record_result_refuses_to_overwrite_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            p.write_text("garbage{{{")
            before = p.read_bytes()
            with self.assertRaises(L.CorruptHistoryError):
                L.record_result(source="mock_interview", label="lab",
                                dimension_scores={"structure": 80},
                                session_id="s1", history_path=p)
            # The corrupt file is untouched — no silent data loss.
            self.assertEqual(p.read_bytes(), before)

    def test_record_result_explicit_recovery_keeps_backup(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            p.write_text("garbage{{{")
            rec = L.record_result(source="mock_interview", label="lab",
                                  dimension_scores={"structure": 80},
                                  session_id="s1", history_path=p,
                                  recover=True)
            self.assertTrue(rec["recovered"])
            self.assertTrue(rec["history_ok"])
            bak = Path(str(p) + ".bak")
            self.assertTrue(bak.exists())
            self.assertEqual(bak.read_bytes(), b"garbage{{{")
            # The new history file is valid and holds the record.
            data = json.loads(p.read_text())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["session_id"], "s1")

    def test_atomic_write_keeps_backup_after_torn_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            L._save_json_atomic(p, [{"v": 1}])
            L._save_json_atomic(p, [{"v": 2}])
            bak = Path(str(p) + ".bak")
            self.assertEqual(json.loads(bak.read_text()), [{"v": 1}])
            # Simulate a torn write: truncate the live file mid-stream.
            with p.open("r+b") as fh:
                fh.truncate(5)
            history, ok, note = L._read_history_store(p)
            self.assertFalse(ok)
            self.assertIn("corrupt", note.lower())
            # Previous backup is intact for recovery.
            self.assertEqual(json.loads(bak.read_text()), [{"v": 1}])
            # No stray tmp file left behind.
            self.assertFalse((p.parent / (p.name + ".tmp")).exists())

    def test_explicit_history_reports_ok(self):
        out = L.observed_gaps(_gap_history())
        self.assertTrue(out["history_ok"])


class TestFocusStaleness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._focus = Path(self._tmp.name) / "focus.json"

    def _write_focus(self, days_ago):
        sel = (datetime.now(timezone.utc)
               - timedelta(days=days_ago)).isoformat()
        self._focus.write_text(json.dumps(
            {"dimension": "clarity", "basis": "user_selected",
             "selected_at": sel}))

    def test_ttl_is_named_constant(self):
        self.assertEqual(L.FOCUS_TTL_DAYS, 30)

    def test_stale_focus_expires_to_observed_gaps(self):
        self._write_focus(45)
        out = L.recommend_next_drill(_gap_history(),
                                     focus_path=self._focus)
        self.assertEqual(out["basis"], "observed")
        self.assertEqual(out["expired_focus"]["dimension"], "clarity")
        self.assertIn("expired", out["markdown"].lower())
        self.assertTrue(all(r["dimension"] == "evidence"
                            for r in out["recommendations"]))

    def test_fresh_focus_surfaces_also_observed(self):
        self._write_focus(5)
        out = L.recommend_next_drill(_gap_history(),
                                     focus_path=self._focus)
        self.assertEqual(out["basis"], "user_selected")
        self.assertIsNone(out["expired_focus"])
        self.assertEqual([g["dimension"] for g in out["also_observed"]],
                         ["evidence"])
        self.assertIn("Also observed", out["markdown"])
        self.assertIn("5 day(s) ago", out["markdown"])
        # Focus still wins the recommendation itself.
        self.assertTrue(all(r["dimension"] == "clarity"
                            for r in out["recommendations"]))

    def test_fresh_focus_reports_selection_age(self):
        self._write_focus(0)
        out = L.recommend_next_drill(_gap_history(),
                                     focus_path=self._focus)
        target = out["targets"][0]
        self.assertEqual(target["focus_age_days"], 0)
        self.assertFalse(target["stale"])


class TestStateLocation(unittest.TestCase):
    def test_state_dir_outside_repo(self):
        self.assertNotEqual(L.STATE_DIR, L.BASE_DIR)
        self.assertFalse(
            str(L.HISTORY_PATH).startswith(str(L.BASE_DIR) + os.sep))
        self.assertFalse(
            str(L.FOCUS_PATH).startswith(str(L.BASE_DIR) + os.sep))
        self.assertEqual(L.HISTORY_PATH.name, "i06_practice_history.json")
        self.assertEqual(L.FOCUS_PATH.name, "i06_practice_focus.json")
        self.assertEqual(L.STATE_DIR.name, "i06")
        self.assertEqual(L.STATE_DIR.parent.name, "veto")

    def test_migrate_moves_legacy_files(self):
        with tempfile.TemporaryDirectory() as td:
            legacy_dir = Path(td) / "repo"
            legacy_dir.mkdir()
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            (legacy_dir / "i06_practice_history.json").write_text('[{"a":1}]')
            with mock.patch.object(L, "BASE_DIR", legacy_dir):
                L._migrate_repo_root_state(state_dir)
            self.assertTrue(
                (state_dir / "i06_practice_history.json").exists())
            self.assertFalse(
                (legacy_dir / "i06_practice_history.json").exists())

    def test_migrate_never_overwrites_new_location(self):
        with tempfile.TemporaryDirectory() as td:
            legacy_dir = Path(td) / "repo"
            legacy_dir.mkdir()
            state_dir = Path(td) / "state"
            state_dir.mkdir()
            (legacy_dir / "i06_practice_history.json").write_text("old")
            (state_dir / "i06_practice_history.json").write_text("new")
            with mock.patch.object(L, "BASE_DIR", legacy_dir):
                L._migrate_repo_root_state(state_dir)
            self.assertEqual(
                (state_dir / "i06_practice_history.json").read_text(),
                "new")
            self.assertTrue(
                (legacy_dir / "i06_practice_history.json").exists())


class TestRecordValidation(unittest.TestCase):
    def _ok(self, **kw):
        args = dict(source="mock_interview", label="lab",
                    dimension_scores={"structure": 80}, session_id="s1")
        args.update(kw)
        return args

    def test_scores_clamped_to_0_100(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "h.json"
            for bad in (101, -1, 1000):
                with self.assertRaises(ValueError):
                    L.record_result(
                        **self._ok(dimension_scores={"structure": bad},
                                   history_path=p))
            # Boundaries are fine.
            L.record_result(
                **self._ok(dimension_scores={"structure": 0,
                                             "evidence": 100},
                           history_path=p))

    def test_scores_must_be_int_not_none_without_meaning(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "h.json"
            for bad in ("85", 85.5, True, [80]):
                with self.assertRaises(ValueError):
                    L.record_result(
                        **self._ok(dimension_scores={"structure": bad},
                                   history_path=p))
            # None means "unscored" and is recorded as such.
            rec = L.record_result(
                **self._ok(dimension_scores={"structure": None},
                           history_path=p))
            self.assertEqual(rec["dimension_scores"], {})
            self.assertEqual(rec["unscored_dimensions"], ["structure"])

    def test_unknown_dimension_key_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "h.json"
            with self.assertRaises(ValueError) as ctx:
                L.record_result(
                    **self._ok(dimension_scores={"charisma": 90},
                               history_path=p))
            self.assertIn("charisma", str(ctx.exception))

    def test_overall_validated(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "h.json"
            with self.assertRaises(ValueError):
                L.record_result(**self._ok(overall=150, history_path=p))
            rec = L.record_result(**self._ok(overall=70, history_path=p))
            self.assertEqual(rec["overall"], 70)

    def test_source_and_label_required(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "h.json"
            with self.assertRaises(ValueError):
                L.record_result(**self._ok(source="", history_path=p))
            with self.assertRaises(ValueError):
                L.record_result(**self._ok(label="  ", history_path=p))


class TestOutcomeDatetimeOrdering(unittest.TestCase):
    def test_latest_by_datetime_not_lexicographic(self):
        # "2026-9-3" sorts AFTER "2026-09-13" lexicographically ("9">"0")
        # but is chronologically earlier. The old string >= comparison
        # picked the wrong event.
        events = [
            {"application_id": "a1", "event_type": "offered",
             "recorded_at": "2026-09-13T00:00:00+00:00",
             "occurred_at": "2026-09-13T00:00:00+00:00", "role": "r"},
            {"application_id": "a1", "event_type": "rejected",
             "recorded_at": "2026-9-3T00:00:00+00:00",
             "occurred_at": "2026-09-03T00:00:00+00:00", "role": "r"},
        ]
        history = [_rec({"structure": 80}, sid="x", job_id="a1")]
        with mock.patch.object(L, "_load_outcome_events",
                               return_value=(events, "note")):
            out = L.link_outcomes(history)
        self.assertEqual(out["records"][0]["linked_outcome"]["event_type"],
                         "offered")

    def test_unparseable_timestamps_ignored_for_ordering(self):
        events = [
            {"application_id": "a1", "event_type": "offered",
             "recorded_at": "not-a-date",
             "occurred_at": "2026-09-13T00:00:00+00:00", "role": "r"},
            {"application_id": "a1", "event_type": "interviewed",
             "recorded_at": "2026-09-10T00:00:00+00:00",
             "occurred_at": "2026-09-10T00:00:00+00:00", "role": "r"},
        ]
        history = [_rec({"structure": 80}, sid="x", job_id="a1")]
        with mock.patch.object(L, "_load_outcome_events",
                               return_value=(events, "note")):
            out = L.link_outcomes(history)
        # The parseable event wins over the unparseable one.
        self.assertEqual(out["records"][0]["linked_outcome"]["event_type"],
                         "interviewed")


class TestDrillWarnings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._focus = Path(self._tmp.name) / "focus.json"

    def test_missing_drills_entry_warns_loudly(self):
        drills = dict(L.DRILLS)
        del drills["evidence"]
        with mock.patch.object(L, "DRILLS", drills):
            out = L.recommend_next_drill(_gap_history(),
                                         focus_path=self._focus)
        self.assertTrue(out["drill_warnings"])
        self.assertIn("evidence", out["drill_warnings"][0])
        self.assertIn("No DRILLS entry", out["markdown"])
        # The target is not silently dropped: no empty drill section.
        self.assertEqual(out["recommendations"], [])


class TestPendingApproval(unittest.TestCase):
    def test_rule_payload_carries_pending_approval(self):
        out = L.observed_gaps([_rec(GOOD, sid="a")])
        self.assertTrue(out["rule"]["pending_approval"])
        self.assertEqual(out["rule"]["approval_deadline"], "2027-01-10")
        self.assertIn("2027-01-10", out["rule"]["approval_note"])

    def test_recommend_payload_carries_pending_approval(self):
        with tempfile.TemporaryDirectory() as td:
            focus = Path(td) / "focus.json"
            out = L.recommend_next_drill([_rec(GOOD, sid="a")],
                                         focus_path=focus)
            self.assertTrue(out["rule"]["pending_approval"])
            self.assertEqual(out["rule"]["approval_deadline"],
                             "2027-01-10")


class TestHistoryRotation(unittest.TestCase):
    def test_rotation_caps_live_history_and_archives(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.json"
            with mock.patch.object(L, "MAX_HISTORY_RECORDS", 3):
                for i in range(5):
                    L.record_result(source="mock_interview", label="lab",
                                    dimension_scores={"structure": 80},
                                    session_id=f"s{i}", history_path=p)
            data = json.loads(p.read_text())
            self.assertEqual(len(data), 3)
            self.assertEqual([r["session_id"] for r in data],
                             ["s2", "s3", "s4"])
            archive = Path(str(p) + ".archive.jsonl")
            self.assertTrue(archive.exists())
            lines = [json.loads(line) for line in
                     archive.read_text().splitlines() if line.strip()]
            self.assertEqual([r["session_id"] for r in lines],
                             ["s0", "s1"])

    def test_rotation_cap_is_named_constant(self):
        self.assertEqual(L.MAX_HISTORY_RECORDS, 200)


class TestTrendMath(unittest.TestCase):
    def _history(self, scores):
        return [_rec({"evidence": s}, sid=f"s{i}",
                     completed_at=f"2026-09-{10 + i:02d}T00:00:00+00:00")
                for i, s in enumerate(scores)]

    def test_steady_band(self):
        out = L.progress_report(self._history([80, 82, 79, 81]))
        ev = next(d for d in out["dimensions"]
                  if d["dimension"] == "evidence")
        self.assertEqual(ev["trend"], "steady")
        self.assertEqual(ev["delta"], -1.0)

    def test_declining(self):
        out = L.progress_report(self._history([90, 88, 60, 58]))
        ev = next(d for d in out["dimensions"]
                  if d["dimension"] == "evidence")
        self.assertEqual(ev["trend"], "declining")

    def test_trend_thresholds_are_named_constants(self):
        self.assertEqual(L.TREND_IMPROVING_DELTA, 5.0)
        self.assertEqual(L.TREND_DECLINING_DELTA, -5.0)

    def test_progress_carries_history_ok(self):
        out = L.progress_report(self._history([80, 82]))
        self.assertTrue(out["history_ok"])
