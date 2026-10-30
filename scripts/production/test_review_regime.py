#!/usr/bin/env python3
"""Tests for the review ledger: what names a row, and rows written before the plan was part of the name."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, final, override
from unittest import mock

from scripts.production import review_regime

STARTED = "2026-10-12T09:00:00-04:00"
MERGED = "2026-10-12T11:00:00-04:00"
FIRST_PLAN = "docs/plans/dashboard.md"
SECOND_PLAN = "docs/plans/dashboard-link.md"


@final
class ReviewRegimeTests(unittest.TestCase):
    ledger: Path = Path()

    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ledger = home / "review_regime.jsonl"
        _ = self.enterContext(cast(contextlib.AbstractContextManager[object],
                                   mock.patch.dict(os.environ, {"HOME": str(home)})))
        _ = self.enterContext(mock.patch.object(review_regime, "LEDGER", self.ledger))

    def add(self, *identity: str, started: str = STARTED, merged: str = MERGED) -> str:
        arguments = ["review_regime.py", "add", *identity, "--regime", "after", "--started", started,
                     "--merged", merged, "--holds", "0", "--merge-defects", "0"]
        output = io.StringIO()
        with (mock.patch("sys.argv", arguments), contextlib.redirect_stdout(output),
              contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(review_regime.main(), 0)
        return output.getvalue()

    def add_row(self, plan: str, phase: str) -> None:
        _ = self.add("--unit", "dashboard-unit", "--plan", plan, "--phase", phase)

    def test_a_units_second_plan_records_its_own_phase_1(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([(row.unit, row.plan, row.phase) for row in review_regime.read_rows()], [
            ("dashboard-unit", FIRST_PLAN, "1"),
            ("dashboard-unit", SECOND_PLAN, "1"),
        ])

    def test_the_same_unit_plan_and_phase_keeps_the_first_merge(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        before = self.ledger.read_bytes()

        output = self.add("--unit", "dashboard-unit", "--plan", FIRST_PLAN, "--phase", "1",
                          "--note", "repair merge", merged="2026-10-13T11:00:00-04:00")

        self.assertEqual(output, f"kept dashboard-unit phase 1 of {FIRST_PLAN}: "
                         + f"a row exists from the merge at {MERGED}; nothing added\n")
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(len(review_regime.read_rows()), 1)

    def test_a_repeated_phase_still_refuses_invalid_merge_times(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        before = self.ledger.read_bytes()
        for started, merged, expected in (
            ("2026-10-12T09:00:00", MERGED,
             "review_regime: --started '2026-10-12T09:00:00' needs a UTC offset"),
            (STARTED, "2026-10-12T11:00:00",
             "review_regime: --merged '2026-10-12T11:00:00' needs a UTC offset"),
            (STARTED, "2026-10-12T08:00:00-04:00", "review_regime: --merged is before --started"),
        ):
            with self.subTest(started=started, merged=merged):
                with self.assertRaises(SystemExit) as refused:
                    _ = self.add("--unit", "dashboard-unit", "--plan", FIRST_PLAN, "--phase", "1",
                                 started=started, merged=merged)
                self.assertEqual(str(refused.exception), expected)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_a_row_written_before_the_plan_was_recorded_reads_and_blocks_nothing(self) -> None:
        old = {"unit": "dashboard-unit", "phase": "1", "regime": "after", "started": STARTED, "merged": MERGED,
               "holds": 0, "merge_defects": 0}
        _ = self.ledger.write_text(json.dumps(old) + "\n")

        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([row.plan for row in review_regime.read_rows()], [None, SECOND_PLAN])

    def test_add_needs_the_plan(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            _ = self.add("--unit", "dashboard-unit", "--phase", "1")

        self.assertEqual(refused.exception.code, 2)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    _ = unittest.main()
