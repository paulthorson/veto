#!/usr/bin/env python3
"""Unit tests for radar.py: fit-score radar charts (no network)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import radar  # noqa: E402


def _result(**over):
    base = {
        "title": "Senior Backend Engineer",
        "company": "Acme <Corp>",
        "score": 72,
        "veto": False,
        "veto_reason": None,
        "reasons": ["12/15 skills matched", "seniority fit: senior"],
        "components": {
            "skills": 40.0,
            "seniority": 15.0,
            "salary": 9.0,
            "location": 12.0,
            "recency": 4.0,
        },
    }
    base.update(over)
    return base


class TestNormalize(unittest.TestCase):
    def test_scales_to_100(self):
        norm = radar.normalize_components({"skills": 25.0})
        self.assertEqual(norm["skills"], 50.0)

    def test_clamps(self):
        norm = radar.normalize_components({"skills": 999.0, "salary": -5})
        self.assertEqual(norm["skills"], 100.0)
        self.assertEqual(norm["salary"], 0.0)

    def test_missing_axes_zero(self):
        norm = radar.normalize_components({})
        self.assertTrue(all(v == 0.0 for v in norm.values()))
        self.assertEqual(set(norm), set(radar.AXES))

    def test_bad_values_zero(self):
        norm = radar.normalize_components({"skills": "nope", "salary": None})
        self.assertEqual(norm["skills"], 0.0)
        self.assertEqual(norm["salary"], 0.0)


class TestRadarSvg(unittest.TestCase):
    def test_svg_structure(self):
        svg = radar.radar_svg(_result())
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
        self.assertIn("<polygon", svg)
        for axis in radar.AXES:
            self.assertIn(axis, svg)

    def test_score_and_title_shown(self):
        svg = radar.radar_svg(_result())
        self.assertIn(">72<", svg)
        self.assertIn("Senior Backend Engineer", svg)

    def test_html_escapes_title(self):
        svg = radar.radar_svg(_result())
        self.assertIn("Acme &lt;Corp&gt;", svg)
        self.assertNotIn("Acme <Corp>", svg)

    def test_veto_stamp(self):
        svg = radar.radar_svg(_result(score=20, veto=True))
        self.assertIn("VETOED", svg)
        self.assertIn(radar.VETO_RED, svg)

    def test_no_veto_no_stamp(self):
        svg = radar.radar_svg(_result())
        self.assertNotIn("VETOED", svg)

    def test_custom_title_and_color(self):
        svg = radar.radar_svg(_result(), title="Custom", color="#41e6ff")
        self.assertIn("Custom", svg)
        self.assertIn("#41e6ff", svg)

    def test_empty_result_does_not_crash(self):
        svg = radar.radar_svg({})
        self.assertIn("<svg", svg)


class TestCompareSvg(unittest.TestCase):
    def test_overlays_two(self):
        svg = radar.compare_svg([("A", _result()), ("B", _result(score=40))])
        self.assertIn("(72)", svg)
        self.assertIn("(40)", svg)
        self.assertIn(radar.COMPARE_COLORS[1], svg)

    def test_caps_at_four(self):
        svg = radar.compare_svg([(str(i), _result()) for i in range(6)])
        # 4 legend swatches max
        self.assertEqual(svg.count('width="10" height="10"'), 4)


class TestRadarHtml(unittest.TestCase):
    def test_single_job_page(self):
        page = radar.radar_html([("A @ Acme", _result())])
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn("12/15 skills matched", page)
        self.assertIn("72/100", page)

    def test_multi_job_page(self):
        page = radar.radar_html(
            [("A", _result()), ("B", _result(score=30, veto=True))],
            title="Compare <jobs>",
        )
        self.assertIn("Compare &lt;jobs&gt;", page)
        self.assertIn("[VETOED]", page)


if __name__ == "__main__":
    unittest.main()


class TestRadarPluginWiring(unittest.TestCase):
    def _result(self):
        return {
            "score": 82,
            "veto": False,
            "components": {
                "skills": 45.0, "seniority": 12.0, "salary": 10.0,
                "location": 12.0, "recency": 3.0,
            },
            "title": "Backend Engineer",
            "company": "Acme",
        }

    def test_register_tools_exposes_two_tools(self):
        seen = {}

        class FakeMCP:
            def tool(self):
                def deco(fn):
                    seen[fn.__name__] = fn
                    return fn
                return deco

        radar.register_tools(FakeMCP())
        self.assertEqual(set(seen), {"radar_chart", "radar_compare_page"})
        svg = seen["radar_chart"](self._result(), title="T")
        self.assertIn("<svg", svg)
        page = seen["radar_compare_page"](
            [{"label": "A", "result": self._result()}], title="T"
        )
        self.assertIn("<html", page)

    def test_register_cli_returns_radar_command(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        cmds = radar.register_cli(sub)
        self.assertIn("radar", cmds)
        self.assertTrue(callable(cmds["radar"]))

    def test_cli_requires_input_file(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        cmds = radar.register_cli(sub)
        args = argparse.Namespace(
            result_file="", compare_file="", title="T", size=400,
            html=False, out="",
        )
        self.assertEqual(cmds["radar"](args), 2)
