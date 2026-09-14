#!/usr/bin/env python3
"""Test runner with a per-module summary table.

Discovers every test module under tests/ (so new suites are picked up
automatically) and prints a summary table at the end.

Usage (from the project directory)::

    .venv/bin/python tests/run.py
    .venv/bin/python tests/run.py -v          # verbose per-test output
    .venv/bin/python -m unittest discover -s tests   # plain unittest, no table
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = BASE_DIR / "tests"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _iter_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 0
    loader = unittest.TestLoader()
    full = loader.discover(str(TESTS_DIR), top_level_dir=str(BASE_DIR))

    # Group test cases by module for the summary table.
    by_module: dict[str, list] = {}
    for case in _iter_cases(full):
        by_module.setdefault(case.__class__.__module__, []).append(case)

    print("=" * 76)
    print("job-apply-mcp test suite")
    print("=" * 76)

    rows = []
    totals = {"ran": 0, "fail": 0, "err": 0, "skip": 0}
    failed: list[str] = []
    for module in sorted(by_module):
        cases = by_module[module]
        suite = unittest.TestSuite(cases)
        stream = open("/dev/null", "w") if verbosity == 0 else sys.stderr
        runner = unittest.TextTestRunner(stream=stream, verbosity=verbosity)
        started = time.time()
        result = runner.run(suite)
        elapsed = time.time() - started
        if verbosity == 0:
            stream.close()

        ran = result.testsRun
        n_fail, n_err = len(result.failures), len(result.errors)
        n_skip = len(result.skipped)
        short = module.replace("tests.", "")
        rows.append((short, ran, n_fail, n_err, n_skip, elapsed))
        totals["ran"] += ran
        totals["fail"] += n_fail
        totals["err"] += n_err
        totals["skip"] += n_skip
        if n_fail or n_err:
            failed.append(short)
        for test, reason in result.skipped:
            print(f"  SKIP {short}.{test._testMethodName}: {reason[:110]}")
        for test, tb in result.failures + result.errors:
            print(f"  FAIL {short}.{test._testMethodName}")
            # Print the traceback so CI logs are diagnosable.
            print("  " + "-" * 72)
            for line in tb.strip().splitlines():
                print(f"  {line}")
            print("  " + "-" * 72)

    print("\n" + "=" * 76)
    print(f"{'module':<42}{'ran':>5}{'fail':>6}{'err':>5}{'skip':>6}{'secs':>8}")
    print("-" * 76)
    for short, ran, n_fail, n_err, n_skip, elapsed in rows:
        print(f"{short:<42}{ran:>5}{n_fail:>6}{n_err:>5}{n_skip:>6}"
              f"{elapsed:>8.1f}")
    print("-" * 76)
    print(f"{'TOTAL':<42}{totals['ran']:>5}{totals['fail']:>6}"
          f"{totals['err']:>5}{totals['skip']:>6}")
    print("=" * 76)

    if failed:
        print("FAILED modules:", ", ".join(failed))
        return 1
    print("All suites passed (failures/errors: 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
