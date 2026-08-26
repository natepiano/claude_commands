#!/usr/bin/env python3
"""Lock the four-phase parser model to a representative historical run.

The fixture is a full six-phase run: it carries the clean and warmup log
vocabulary the pipeline no longer emits, plus the retired completion banner.
Both are load-bearing. The retired vocabulary must parse into the same eval,
review, fix, and verify cells recorded in EXPECTED_PROJECT_CELLS, and the
retired banner must still close the run, so archived logs keep reading as
finished rather than in-progress.
"""

import re
import sys
import unittest
from pathlib import Path
from typing import ClassVar, override


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from fix_report_parse import (  # noqa: E402
    MONITOR_FILTER_REGEX,
    PHASES,
    ParseResult,
    match_completion_banner,
    parse_log,
)


FIXTURE = Path(__file__).parent / "fixtures" / "six-phase-run.log"
EXPECTED_PHASES: tuple[str, ...] = ("eval", "review", "fix", "verify")
EXPECTED_PROJECT_CELLS: dict[str, tuple[str, ...]] = {
    "fixture_alpha": ("OK:exhausted", "OK", "OK", "OK"),
    "fixture_beta": ("OK:no-findings", "OK", "SKIP:no-open-findings", "-"),
    "fixture_gamma": (
        "SKIP:already-at-cap-of-2-findings",
        "-",
        "SKIP:no-open-findings",
        "-",
    ),
}


class ReportPhaseRegressionTest(unittest.TestCase):
    result: ClassVar[ParseResult]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.result = parse_log(FIXTURE)

    def test_historical_completion_banner_finishes_run(self) -> None:
        self.assertEqual(self.result.status, "complete")
        self.assertEqual(self.result.elapsed, "0m 55s")

    def test_all_completion_banner_generations_are_recognized(self) -> None:
        banners = (
            "=== Fix complete (1m 2s) ===",
            "=== Clean-fix complete (1m 2s) ===",
            "=== Clean-fix Rust clean + rebuild complete (1m 2s) ===",
        )
        for banner in banners:
            with self.subTest(banner=banner):
                self.assertIsNotNone(match_completion_banner(banner))
        self.assertIsNone(match_completion_banner("ordinary log line"))

    def test_monitor_filter_matches_completion_and_done_lines(self) -> None:
        python_regex = MONITOR_FILTER_REGEX.replace("[[:space:]]", r"\s")
        self.assertIsNotNone(
            re.search(
                python_regex,
                "2026-01-01 00:00:00 === Fix complete (1m 2s) ===",
            )
        )
        self.assertIsNotNone(
            re.search(python_regex, "=== Done: 1 created, 0 failed ===")
        )
        self.assertIsNone(re.search(python_regex, " Compiling serde v1.0"))

    def test_parser_exposes_only_surviving_phases(self) -> None:
        self.assertEqual(PHASES, EXPECTED_PHASES)
        self.assertEqual(tuple(self.result.stats), EXPECTED_PHASES)
        self.assertEqual(
            tuple(phase for phase in EXPECTED_PHASES if self.result.stats[phase].present),
            EXPECTED_PHASES,
        )
        self.assertNotIn("clean", self.result.stats)
        self.assertNotIn("warmup", self.result.stats)
        for row in self.result.rows.values():
            self.assertEqual(tuple(row), EXPECTED_PHASES)
            self.assertNotIn("clean", row)
            self.assertNotIn("warmup", row)

    def test_surviving_project_cells_match_pre_edit_baseline(self) -> None:
        actual = {
            project: tuple(row[phase].render() for phase in EXPECTED_PHASES)
            for project, row in self.result.rows.items()
        }
        self.assertEqual(actual, EXPECTED_PROJECT_CELLS)


if __name__ == "__main__":
    _ = unittest.main()
