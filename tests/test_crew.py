#!/usr/bin/env python3
"""Offline tests for the governed AI crew module."""

from __future__ import annotations

import argparse
import unittest

import crew


class FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class PersonaCardTests(unittest.TestCase):
    REQUIRED = {
        "id", "name", "role", "persona", "allowed_modules",
        "risk_tier", "escalation_policy",
    }
    EXPECTED_IDS = {
        "ceo", "chief_of_staff", "pm", "eng", "ux", "qa", "researcher",
        "scout", "tailor_agent", "coach", "red",
    }

    def test_all_eleven_personas_exist(self):
        self.assertEqual(set(crew.PERSONAS), self.EXPECTED_IDS)

    def test_every_card_has_all_required_fields(self):
        for pid, card in crew.PERSONAS.items():
            missing = self.REQUIRED - set(card)
            self.assertFalse(missing, f"{pid} missing {missing}")
            self.assertEqual(card["id"], pid, f"{pid} id mismatch")
            self.assertTrue(card["persona"].strip(), f"{pid} persona empty")
            self.assertTrue(card["allowed_modules"], f"{pid} no modules")
            self.assertTrue(card["escalation_policy"].strip(), f"{pid} no policy")

    def test_red_reports_findings_only(self):
        persona = crew.PERSONAS["red"]["persona"].lower()
        self.assertIn("never edit", persona)

    def test_tailor_obsessed_with_honesty(self):
        persona = crew.PERSONAS["tailor_agent"]["persona"].lower()
        self.assertIn("never", persona)
        self.assertIn("invent", persona)

    def test_pm_never_placeholders(self):
        persona = crew.PERSONAS["pm"]["persona"].lower()
        self.assertIn("tbd", persona)
        self.assertIn("stop", persona)

    def test_pm_read_only_modules(self):
        mods = crew.PERSONAS["pm"]["allowed_modules"]
        self.assertTrue(mods)

    def test_eng_never_ships_partial(self):
        persona = crew.PERSONAS["eng"]["persona"].lower()
        self.assertIn("never ship", persona)

    def test_ux_accessibility_non_negotiable(self):
        persona = crew.PERSONAS["ux"]["persona"].lower()
        self.assertIn("accessibility", persona)

    def test_qa_blocks_merge_on_red(self):
        persona = crew.PERSONAS["qa"]["persona"].lower()
        self.assertIn("block", persona)

    def test_researcher_never_recommends(self):
        persona = crew.PERSONAS["researcher"]["persona"].lower()
        self.assertIn("never recommend", persona)

    def test_ceo_never_invents_policy(self):
        persona = crew.PERSONAS["ceo"]["persona"].lower()
        self.assertIn("do not invent policy", persona)

    def test_chief_of_staff_advisory_only(self):
        persona = crew.PERSONAS["chief_of_staff"]["persona"].lower()
        self.assertIn("advisory-only", persona)

    def test_chief_of_staff_never_clears_vetoes(self):
        persona = crew.PERSONAS["chief_of_staff"]["persona"].lower()
        self.assertIn("clear vetoes", persona)


class RedSpecialistTests(unittest.TestCase):
    def test_twenty_specialists(self):
        self.assertEqual(len(crew.AVAILABLE_RED_SPECIALISTS), 20)

    def test_every_specialist_has_required_fields(self):
        required = {"id", "name", "focus", "holds_veto"}
        for s in crew.AVAILABLE_RED_SPECIALISTS:
            missing = required - set(s)
            self.assertFalse(missing, f"{s.get('id')} missing {missing}")

    def test_red_specialists_listing(self):
        listing = crew.red_specialists()
        self.assertEqual(len(listing), 20)
        ids = {s["id"] for s in listing}
        for expected in ("eng-critic", "qa-critic", "ux-critic", "res-critic",
                          "sec-security-adversary", "priv-privacy-adversary"):
            self.assertIn(expected, ids)

    def test_listing_returns_copies(self):
        listing = crew.red_specialists()
        listing[0]["focus"] = "MUTATED"
        self.assertNotEqual(crew.AVAILABLE_RED_SPECIALISTS[0]["focus"], "MUTATED")

    def test_veto_holders_are_boolean(self):
        for s in crew.red_specialists():
            self.assertIsInstance(s["holds_veto"], bool)


class CeoPlanTests(unittest.TestCase):
    def test_interview_routes_to_coach(self):
        jobs = crew.ceo_plan("interview prep for Stripe")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("coach", pids)

    def test_resume_routes_to_tailor(self):
        jobs = crew.ceo_plan("tailor my resume for this role")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("tailor_agent", pids)

    def test_find_jobs_routes_to_scout(self):
        jobs = crew.ceo_plan("find backend jobs in fintech")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("scout", pids)

    def test_negotiation_routes_to_coach(self):
        jobs = crew.ceo_plan("negotiate my salary offer")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("coach", pids)

    def test_design_routes_to_ux(self):
        jobs = crew.ceo_plan("design a demo for the dashboard")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("ux", pids)

    def test_test_routes_to_qa(self):
        jobs = crew.ceo_plan("test for regressions before release")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("qa", pids)

    def test_research_routes_to_researcher(self):
        jobs = crew.ceo_plan("research salary benchmarks with cited sources")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("researcher", pids)

    def test_build_routes_to_eng(self):
        jobs = crew.ceo_plan("build the new feature to spec")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("eng", pids)

    def test_roadmap_routes_to_pm(self):
        jobs = crew.ceo_plan("write a roadmap spec with acceptance criteria")
        pids = [j["persona_id"] for j in jobs]
        self.assertIn("pm", pids)

    def test_unknown_goal_starts_with_scout_recon(self):
        jobs = crew.ceo_plan("organize my sock drawer")
        self.assertEqual(jobs[0]["persona_id"], "scout")
        self.assertIn("Recon", jobs[0]["task"])

    def test_red_is_last_worker_job_before_human_gate(self):
        for goal in [
            "find jobs", "tailor my resume", "interview prep",
            "negotiate salary", "design a demo", "test for regressions",
            "research benchmarks", "build the feature", "write a roadmap spec",
            "something totally unrecognized",
        ]:
            jobs = crew.ceo_plan(goal)
            self.assertGreaterEqual(len(jobs), 2, goal)
            self.assertEqual(jobs[-2]["persona_id"], "red", goal)
            self.assertEqual(jobs[-1]["persona_id"], "human", goal)

    def test_jobs_have_required_shape(self):
        for job in crew.ceo_plan("find jobs and tailor my resume"):
            self.assertIn("persona_id", job)
            self.assertIn("task", job)
            self.assertIn("expected_artifact", job)

    def test_no_duplicate_worker_personas(self):
        jobs = crew.ceo_plan("find jobs, tailor resume, interview prep")
        worker_pids = [j["persona_id"] for j in jobs if j["persona_id"] != "human"]
        self.assertEqual(len(worker_pids), len(set(worker_pids)))


class RunAgentTests(unittest.TestCase):
    def _stub_governed(self, calls):
        import governance.engine as eng
        real = eng.run_governed_job

        def fake(action, work_fn, detail=""):
            calls.append((action, detail))
            return {
                "action": action, "spawn": {"allowed": True},
                "result": {"ok": True, "value": work_fn()},
                "review": {"fake": True}, "verdict": {"recorded": True},
            }

        eng.run_governed_job = fake
        return real

    def test_governed_path_uses_run_governed_job(self):
        import governance.engine as eng
        calls = []
        real = self._stub_governed(calls)
        try:
            orig = crew._governance_available
            crew._governance_available = lambda: True
            try:
                report = crew.run_agent("coach", "mock interview prep")
            finally:
                crew._governance_available = orig
        finally:
            eng.run_governed_job = real
        self.assertTrue(calls, "run_governed_job was not called")
        self.assertEqual(report["persona_id"], "coach")
        self.assertTrue(report["result"]["ok"])

    def test_governance_unavailable_marks_compliance_only(self):
        orig = crew._governance_available
        crew._governance_available = lambda: False
        try:
            report = crew.run_agent("scout", "find jobs")
        finally:
            crew._governance_available = orig
        self.assertEqual(report.get("mode"), "compliance-only")
        self.assertTrue(report["result"]["ok"])
        self.assertIn("compliance-only", str(report["verdict"]))

    def test_unknown_persona_never_raises(self):
        report = crew.run_agent("wizard", "do magic")
        self.assertFalse(report["ok"])
        self.assertIn("unknown persona", report["error"])

    def test_blocked_task_is_vetoed_by_compliance_screen(self):
        report = crew.run_agent("scout", "submit my application automatically")
        self.assertFalse(report["ok"])
        self.assertEqual(report.get("mode"), "compliance-only")
        self.assertFalse(report["spawn"]["allowed"])

    def test_captcha_bypass_blocked(self):
        report = crew.run_agent("scout", "bypass the captcha on the form")
        self.assertFalse(report["ok"])

    def test_ceo_run_produces_plan(self):
        orig = crew._governance_available
        crew._governance_available = lambda: False
        try:
            report = crew.run_agent("ceo", "interview prep for Stripe")
        finally:
            crew._governance_available = orig
        plan = report["result"]["value"]["plan"]
        pids = [j["persona_id"] for j in plan]
        self.assertIn("coach", pids)
        self.assertEqual(pids[-2], "red")
        self.assertEqual(pids[-1], "human")

    def test_new_personas_run(self):
        orig = crew._governance_available
        crew._governance_available = lambda: False
        try:
            for pid in ("pm", "eng", "ux", "qa", "researcher", "chief_of_staff"):
                report = crew.run_agent(pid, "do your thing")
                self.assertTrue(report["result"]["ok"], pid)
                self.assertEqual(report["persona_id"], pid)
        finally:
            crew._governance_available = orig

    def test_red_run_lists_specialists(self):
        orig = crew._governance_available
        crew._governance_available = lambda: False
        try:
            report = crew.run_agent("red", "review this", {"artifact": "draft"})
        finally:
            crew._governance_available = orig
        value = report["result"]["value"]
        self.assertEqual(len(value["specialists"]), 20)


class RedTeamScanTests(unittest.TestCase):
    def test_flags_superlatives(self):
        scan = crew.red_team_scan("I am the best engineer, guaranteed results.")
        kinds = {f["kind"] for f in scan["findings"]}
        self.assertIn("superlative", kinds)

    def test_flags_unquantified_claim(self):
        scan = crew.red_team_scan("I led the migration to the new platform.")
        kinds = {f["kind"] for f in scan["findings"]}
        self.assertIn("unquantified_claim", kinds)

    def test_quantified_claim_passes(self):
        scan = crew.red_team_scan("I led the migration for 40 engineers, cutting deploy time 30%.")
        kinds = {f["kind"] for f in scan["findings"]}
        self.assertNotIn("unquantified_claim", kinds)

    def test_clean_text_verdict(self):
        scan = crew.red_team_scan("I am a backend engineer with 5 years of experience.")
        self.assertEqual(scan["verdict"], "clean")

    def test_empty_text(self):
        scan = crew.red_team_scan("")
        self.assertEqual(scan["verdict"], "clean")


class WiringTests(unittest.TestCase):
    def test_register_tools(self):
        mcp = FakeMcp()
        crew.register_tools(mcp)
        for name in ("crew_run", "crew_plan", "crew_status_report",
                      "crew_red_scan", "crew_red_specialists"):
            self.assertIn(name, mcp.tools, f"missing tool {name}")

    def test_register_cli(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        mapping = crew.register_cli(sub)
        self.assertEqual(mapping,
                         {"crew": crew.cmd_crew,
                          "extension": crew.cmd_extension})
        args = parser.parse_args(["crew", "plan", "--goal", "find jobs"])
        self.assertEqual(args.action, "plan")
        args = parser.parse_args(["crew", "specialists"])
        self.assertEqual(args.action, "specialists")
        args = parser.parse_args(["extension", "list"])
        self.assertEqual(args.action, "list")

    def test_crew_status_shape(self):
        status = crew.crew_status()
        self.assertEqual(len(status["personas"]), 11)
        self.assertTrue(status["cards_valid"])
        self.assertEqual(status["red_specialists"], 20)
        self.assertTrue(status["red_specialists_valid"])
        self.assertIsInstance(status["governance_available"], bool)
        self.assertEqual(status["lifecycle"], ["spawn", "work", "review", "merge"])

    def test_cmd_crew_plan(self):
        args = argparse.Namespace(
            action="plan", goal="find jobs", persona_id=None, task=None,
            context=None, text=None, json=True,
        )
        self.assertEqual(crew.cmd_crew(args), 0)

    def test_cmd_crew_personas(self):
        args = argparse.Namespace(
            action="personas", goal=None, persona_id=None, task=None,
            context=None, text=None, json=True,
        )
        self.assertEqual(crew.cmd_crew(args), 0)

    def test_cmd_crew_specialists(self):
        args = argparse.Namespace(
            action="specialists", goal=None, persona_id=None, task=None,
            context=None, text=None, json=True,
        )
        self.assertEqual(crew.cmd_crew(args), 0)


if __name__ == "__main__":
    unittest.main()
