"""Offline tests for skill_gaps.py. No network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import skill_gaps


def _job(title, description):
    return {"title": title, "description": description}


class SkillGapsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._store = skill_gaps.STORE_PATH
        self._tried = skill_gaps._ai_proficiency_tried
        self._mod = skill_gaps._ai_proficiency
        skill_gaps.STORE_PATH = self.tmp / "skill_gaps.json"
        skill_gaps._ai_proficiency_tried = False
        skill_gaps._ai_proficiency = None

    def tearDown(self):
        skill_gaps.STORE_PATH = self._store
        skill_gaps._ai_proficiency_tried = self._tried
        skill_gaps._ai_proficiency = self._mod

    def test_analyze_ranks_by_frequency(self):
        profile = {"skills": ["python"]}
        jobs = [
            _job("Backend Engineer", "We need kubernetes and docker experience."),
            _job("DevOps Engineer", "kubernetes required, plus terraform."),
            _job("Platform Engineer", "python skills needed."),
        ]
        result = skill_gaps.analyze_gaps(profile, jobs)
        skills = [g["skill"] for g in result["gaps"]]
        # python is in the profile -> never a gap
        self.assertNotIn("python", skills)
        # kubernetes appears in 2 jobs -> outranks docker
        self.assertLess(skills.index("kubernetes"), skills.index("docker"))
        k8s = next(g for g in result["gaps"] if g["skill"] == "kubernetes")
        self.assertEqual(k8s["job_count"], 2)
        self.assertEqual(len(k8s["example_jobs"]), 2)
        self.assertEqual(result["jobs_analyzed"], 3)

    def test_analyze_seniority_weight(self):
        profile = {"skills": []}
        jobs = [
            _job("Junior Analyst", "sql required."),
            _job("Staff Analyst", "tableau required."),
        ]
        result = skill_gaps.analyze_gaps(profile, jobs)
        by_skill = {g["skill"]: g for g in result["gaps"]}
        # staff (1.4) outranks a plain title (mid, 1.0) at equal frequency
        self.assertGreater(
            by_skill["tableau"]["weighted_score"], by_skill["sql"]["weighted_score"]
        )

    def test_analyze_persists_and_preserves_status(self):
        profile = {"skills": []}
        jobs = [_job("Engineer", "kubernetes needed.")]
        skill_gaps.analyze_gaps(profile, jobs)
        skill_gaps.mark_gap_progress("kubernetes", "practicing")
        skill_gaps.analyze_gaps(profile, jobs)  # re-run
        store = json.loads(skill_gaps.STORE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(store["gaps"]["kubernetes"]["status"], "practicing")

    def test_gap_plan_maps_ai_lesson(self):
        profile = {"skills": []}
        jobs = [_job("Data Analyst", "sql and tableau required for this role.")]
        skill_gaps.analyze_gaps(profile, jobs)
        plan = skill_gaps.gap_plan()
        self.assertTrue(plan["plan"])
        lesson_steps = [s for s in plan["plan"] if s["type"] == "ai_proficiency_lesson"]
        self.assertTrue(lesson_steps, f"no lesson mapped: {plan['plan']}")
        self.assertIn("track", lesson_steps[0])
        self.assertIn("lesson_id", lesson_steps[0])

    def test_gap_plan_non_ai_project(self):
        profile = {"skills": []}
        jobs = [_job("DevOps Engineer", "kubernetes and terraform required.")]
        skill_gaps.analyze_gaps(profile, jobs)
        plan = skill_gaps.gap_plan()
        by_skill = {s["skill"]: s for s in plan["plan"]}
        self.assertIn("kubernetes", by_skill)
        self.assertEqual(by_skill["kubernetes"]["type"], "practice_project")
        self.assertIn("project", by_skill["kubernetes"])

    def test_gap_plan_degrades_without_ai_module(self):
        profile = {"skills": []}
        jobs = [_job("Data Analyst", "sql required for this role.")]
        skill_gaps.analyze_gaps(profile, jobs)
        with mock.patch.object(skill_gaps, "_ai_module", return_value=None):
            skill_gaps._ai_proficiency_tried = True
            skill_gaps._ai_proficiency = None
            plan = skill_gaps.gap_plan()
        # Never fails; falls back to project suggestions.
        self.assertTrue(plan["plan"])
        self.assertTrue(all("project" in s or "lesson_id" in s for s in plan["plan"]))

    def test_gap_plan_no_gaps(self):
        result = skill_gaps.gap_plan()
        self.assertEqual(result["plan"], [])

    def test_mark_progress_invalid_status(self):
        profile = {"skills": []}
        skill_gaps.analyze_gaps(profile, [_job("Engineer", "kubernetes needed.")])
        with self.assertRaises(ValueError):
            skill_gaps.mark_gap_progress("kubernetes", "done")

    def test_mark_progress_close_requires_evidence(self):
        profile = {"skills": []}
        skill_gaps.analyze_gaps(profile, [_job("Engineer", "kubernetes needed.")])
        with self.assertRaises(ValueError):
            skill_gaps.mark_gap_progress("kubernetes", "closed")
        with self.assertRaises(ValueError):
            skill_gaps.mark_gap_progress("kubernetes", "closed", evidence="   ")

    def test_mark_progress_close_with_evidence(self):
        profile = {"skills": []}
        skill_gaps.analyze_gaps(profile, [_job("Engineer", "kubernetes needed.")])
        rec = skill_gaps.mark_gap_progress(
            "Kubernetes", "closed", evidence="deployed k3s homelab, notes written"
        )
        self.assertEqual(rec["status"], "closed")
        self.assertIn("k3s", rec["evidence"])

    def test_mark_progress_unknown_skill(self):
        with self.assertRaises(ValueError):
            skill_gaps.mark_gap_progress("cobol", "practicing")

    def test_register_cli(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        handlers = skill_gaps.register_cli(sub)
        self.assertEqual(set(handlers), {"skill-gaps"})
        args = parser.parse_args(["skill-gaps", "plan"])
        self.assertEqual(args.action, "plan")

    def test_register_tools(self):
        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn

                return deco

        skill_gaps.register_tools(FakeMCP())
        self.assertEqual(
            set(seen), {"analyze_skill_gaps", "skill_gap_plan", "mark_gap_progress"}
        )
        # Functional behavior of the tools is covered by the
        # direct-function tests above.
        self.assertTrue(callable(seen["skill_gap_plan"]))


if __name__ == "__main__":
    unittest.main()
