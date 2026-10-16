#!/usr/bin/env python3
"""Tests for the review ledger: what names a row, and rows written before the plan was part of the name."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import final, override
from unittest import mock

import review_regime

STARTED = "2026-10-12T09:00:00-04:00"
MERGED = "2026-10-12T11:00:00-04:00"
FIRST_PLAN = "docs/plans/dashboard.md"
SECOND_PLAN = "docs/plans/dashboard-link.md"


@final
class ReviewRegimeTests(unittest.TestCase):
    ledger: Path = Path()

    @override
    def setUp(self) -> None:
        self.ledger = Path(self.enterContext(tempfile.TemporaryDirectory())) / "review_regime.jsonl"
        _ = self.enterContext(mock.patch.object(review_regime, "LEDGER", self.ledger))

    def add(self, *identity: str) -> None:
        arguments = ["review_regime.py", "add", *identity, "--regime", "after", "--started", STARTED,
                     "--merged", MERGED, "--holds", "0", "--merge-defects", "0"]
        with (mock.patch("sys.argv", arguments), contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            _ = review_regime.main()

    def add_row(self, plan: str, phase: str) -> None:
        self.add("--unit", "dashboard-unit", "--plan", plan, "--phase", phase)

    def test_a_units_second_plan_records_its_own_phase_1(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([(row.unit, row.plan, row.phase) for row in review_regime.read_rows()], [
            ("dashboard-unit", FIRST_PLAN, "1"),
            ("dashboard-unit", SECOND_PLAN, "1"),
        ])

    def test_the_same_unit_plan_and_phase_is_refused(self) -> None:
        self.add_row(FIRST_PLAN, "1")

        with self.assertRaises(SystemExit) as refused:
            self.add_row(FIRST_PLAN, "1")

        self.assertEqual(str(refused.exception), f"review_regime: dashboard-unit phase 1 of {FIRST_PLAN} already has a row")
        self.assertEqual(len(review_regime.read_rows()), 1)

    def test_a_row_written_before_the_plan_was_recorded_reads_and_blocks_nothing(self) -> None:
        old = {"unit": "dashboard-unit", "phase": "1", "regime": "after", "started": STARTED, "merged": MERGED,
               "holds": 0, "merge_defects": 0}
        _ = self.ledger.write_text(json.dumps(old) + "\n")

        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([row.plan for row in review_regime.read_rows()], [None, SECOND_PLAN])

    def test_add_needs_the_plan(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            self.add("--unit", "dashboard-unit", "--phase", "1")

        self.assertEqual(refused.exception.code, 2)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    _ = unittest.main()
