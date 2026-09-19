"""CLI help grouping + stderr verbosity (Workstream 5: deferred audit polish).

- --help groups commands under the dashboard's FIND / TAILOR / APPLY /
  TRAIN / WIN / GOVERN / UTILITIES headings; every registered command
  appears exactly once.
- stderr carries errors and genuine warnings only: a successful read-only
  command leaves stderr silent, routine INFO chatter is gated behind
  --verbose, and --quiet drops to errors only.
"""

from __future__ import annotations

import io
import logging
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import argparse  # noqa: E402

import cli  # noqa: E402
import server  # noqa: E402

_LOG = logging.getLogger("veto-mcp")
_COMMAND_LINE_RE = re.compile(r"^    ([a-z][a-z0-9-]*)(\s|$)")
_real_boards = server.list_boards


def _subparser_choices(parser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices.keys())
    raise AssertionError("no subparsers action found")


def _group_of_command(help_text: str, command: str) -> str:
    group = None
    for line in help_text.splitlines():
        heading = re.fullmatch(r"([A-Z]+):", line.strip())
        if heading:
            group = heading.group(1)
            continue
        match = _COMMAND_LINE_RE.match(line)
        if match and match.group(1) == command:
            assert group is not None, f"{command} listed before any group heading"
            return group
    raise AssertionError(f"{command} not found in --help output")


class TestHelpGrouping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = cli.build_parser()
        cls.help_text = cls.parser.format_help()
        cls.commands = _subparser_choices(cls.parser)

    def test_help_exits_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                self.parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_group_headings_present_in_dashboard_order(self):
        positions = [self.help_text.index(f"{group}:") for group in cli._GROUP_ORDER]
        self.assertEqual(positions, sorted(positions), "group headings out of order")
        for group in cli._GROUP_ORDER:
            with self.subTest(group=group):
                self.assertIn(f"\n{group}:\n", self.help_text)

    def test_every_command_listed_exactly_once(self):
        found = [
            match.group(1)
            for line in self.help_text.splitlines()
            if (match := _COMMAND_LINE_RE.match(line))
        ]
        self.assertEqual(
            sorted(found),
            self.commands,
            "every registered command must appear exactly once in --help",
        )

    def test_no_flat_positional_dump(self):
        self.assertNotIn("positional arguments:", self.help_text)

    def test_core_commands_grouped(self):
        expected = {
            "boards": "FIND",
            "search": "FIND",
            "show": "FIND",
            "apply": "APPLY",
            "applications": "APPLY",
            "dashboard": "UTILITIES",
            "serve": "UTILITIES",
            "wizard": "UTILITIES",
            "profile": "UTILITIES",
        }
        for command, group in expected.items():
            with self.subTest(command=command):
                self.assertEqual(_group_of_command(self.help_text, command), group)

    def test_plugin_commands_grouped(self):
        expected = {
            "match": "FIND",
            "radar": "FIND",
            "decode-jd": "FIND",
            "cover-letter": "TAILOR",
            "linkedin": "TAILOR",
            "apply-direct": "APPLY",
            "queue-add": "APPLY",
            "queue-list": "APPLY",
            "queue-run": "APPLY",
            "grill-start": "APPLY",
            "mock-start": "TRAIN",
            "mock-answer": "TRAIN",
            "mock-summary": "TRAIN",
            "soft-skills": "TRAIN",
            "ai-skills": "TRAIN",
            "mentors": "TRAIN",
            "skill-gaps": "TRAIN",
            "analytics": "WIN",
            "company-brief": "WIN",
            "prep-interview": "WIN",
            "email-scan": "WIN",
            "email-followup": "WIN",
            "followups": "WIN",
            "referrals": "WIN",
            "offers": "WIN",
            "network": "WIN",
            "autopsy": "WIN",
            "crew": "GOVERN",
            "streaks": "GOVERN",
            "doctor": "UTILITIES",
        }
        for command, group in expected.items():
            with self.subTest(command=command):
                self.assertEqual(_group_of_command(self.help_text, command), group)

    def test_verbosity_flags_in_help(self):
        self.assertIn("--verbose", self.help_text)
        self.assertIn("--quiet", self.help_text)


class TestStderrChatter(unittest.TestCase):
    def setUp(self):
        self._old_level = _LOG.level

    def tearDown(self):
        _LOG.setLevel(self._old_level)

    def _run_with_emit(self, argv, emit):
        records = io.StringIO()
        handler = logging.StreamHandler(records)
        root = logging.getLogger()
        root.addHandler(handler)
        out, err = io.StringIO(), io.StringIO()
        try:
            with mock.patch.object(
                server, "list_boards", side_effect=lambda: (emit(), _real_boards())[1]
            ), redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
        finally:
            root.removeHandler(handler)
        return rc, records.getvalue(), err.getvalue()

    def test_successful_command_leaves_stderr_silent(self):
        rc, _records, err = self._run_with_emit(["boards"], lambda: None)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_info_chatter_suppressed_by_default(self):
        rc, records, _err = self._run_with_emit(
            ["boards"], lambda: _LOG.info("Greenhouse: 3 jobs for 'x' in ''")
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("Greenhouse: 3 jobs", records)

    def test_verbose_restores_info_chatter(self):
        rc, records, _err = self._run_with_emit(
            ["--verbose", "boards"], lambda: _LOG.info("Greenhouse: 3 jobs for 'x' in ''")
        )
        self.assertEqual(rc, 0)
        self.assertIn("Greenhouse: 3 jobs", records)

    def test_warning_still_passes_by_default(self):
        rc, records, _err = self._run_with_emit(
            ["boards"], lambda: _LOG.warning("a genuine warning")
        )
        self.assertEqual(rc, 0)
        self.assertIn("a genuine warning", records)

    def test_quiet_suppresses_warnings(self):
        rc, records, _err = self._run_with_emit(
            ["--quiet", "boards"], lambda: _LOG.warning("a genuine warning")
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("a genuine warning", records)

    def test_error_still_passes_when_quiet(self):
        rc, records, _err = self._run_with_emit(
            ["--quiet", "boards"], lambda: _LOG.error("a genuine error")
        )
        self.assertEqual(rc, 0)
        self.assertIn("a genuine error", records)


if __name__ == "__main__":
    unittest.main()
