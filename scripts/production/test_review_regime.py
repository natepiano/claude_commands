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
        _ = self.add("--unit", "dashboard", "--plan", plan, "--phase", phase)

    def report(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            review_regime.report(None)
        return output.getvalue()

    def test_add_stores_and_reports_simplify_fields_without_using_the_note(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "2",
                     "--simplify-findings", "0", "--simplify-minutes", "2.5", "--note", "user note")

        record = cast(dict[str, object], json.loads(self.ledger.read_text().splitlines()[1]))
        self.assertEqual(record["simplify_findings"], 0)
        self.assertEqual(record["simplify_minutes"], 2.5)
        self.assertEqual(record["note"], "user note")
        row = review_regime.read_rows()[1]
        self.assertEqual((row.simplify_findings, row.simplify_minutes), (0, 2.5))
        report = self.report()
        self.assertIn("| simplify findings per phase (mean) | - | - | 0.0 |", report)
        self.assertIn("| simplify minutes per phase (mean) | - | - | 2.5 |", report)

    def test_add_without_simplify_fields_preserves_the_legacy_record_and_report(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                     "--ux-findings", "2", "--code-findings", "3", "--review-minutes", "14")
        row = review_regime.read_rows()[0]
        self.assertEqual((row.ux_findings, row.code_findings, row.review_minutes), (2, 3, 14))
        self.assertEqual((row.simplify_findings, row.simplify_minutes), (None, None))
        report = self.report()
        self.assertNotIn("simplify", report)
        self.assertIn("| UX reviewer findings per phase (mean) | - | - | 2.0 |", report)
        self.assertIn("| code findings per phase (mean) | - | - | 3.0 |", report)

    def test_legacy_add_keeps_the_serialized_record_bytes(self) -> None:
        _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1")
        expected = {
            "unit": "dashboard", "plan": FIRST_PLAN, "phase": "1", "regime": "after",
            "started": STARTED, "merged": MERGED, "holds": 0, "merge_defects": 0,
            "ux_findings": None, "code_findings": None, "review_minutes": None,
            "ux_check_minutes": None, "ux_repair_minutes": None, "note": None, "excluded": None,
        }
        self.assertEqual(self.ledger.read_bytes(), (json.dumps(expected) + "\n").encode())

    def test_read_ignores_invalid_simplify_values_and_reports_recorded_zero_minutes(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        record = cast(dict[str, object], json.loads(self.ledger.read_text()))
        for findings, minutes, expected_findings, expected_minutes in (
            ("4", 0, None, 0.0),
            (4, "invalid", 4, None),
        ):
            with self.subTest(findings=findings, minutes=minutes):
                record.update({"simplify_findings": findings, "simplify_minutes": minutes})
                _ = self.ledger.write_text(json.dumps(record) + "\n")
                row = review_regime.read_rows()[0]
                report = self.report()
                for field, actual, expected in (
                    ("simplify findings per phase", row.simplify_findings, expected_findings),
                    ("simplify minutes per phase", row.simplify_minutes, expected_minutes),
                ):
                    if expected is None:
                        self.assertIsNone(actual)
                        self.assertNotIn(field, report)
                    else:
                        self.assertEqual(actual, expected)
                        self.assertIn(f"| {field} (mean) | - | - | {expected:.1f} |", report)

    def test_each_simplify_flag_records_only_its_own_field(self) -> None:
        for phase, flag, field, value, absent in (
            ("1", "--simplify-findings", "simplify_findings", "4", "simplify_minutes"),
            ("2", "--simplify-minutes", "simplify_minutes", "0", "simplify_findings"),
        ):
            with self.subTest(flag=flag):
                _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", phase, flag, value)
                record = cast(dict[str, object], json.loads(self.ledger.read_text().splitlines()[-1]))
                self.assertEqual(record[field], float(value))
                self.assertNotIn(absent, record)

    def test_a_units_second_plan_records_its_own_phase_1(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([(row.unit, row.plan, row.phase) for row in review_regime.read_rows()], [
            ("dashboard", FIRST_PLAN, "1"),
            ("dashboard", SECOND_PLAN, "1"),
        ])

    def test_the_same_unit_plan_and_phase_keeps_the_first_merge(self) -> None:
        self.add_row(FIRST_PLAN, "1")
        before = self.ledger.read_bytes()

        output = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                          "--note", "repair merge", merged="2026-10-13T11:00:00-04:00")

        self.assertEqual(output, f"kept dashboard phase 1 of {FIRST_PLAN}: "
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
                    _ = self.add("--unit", "dashboard", "--plan", FIRST_PLAN, "--phase", "1",
                                 started=started, merged=merged)
                self.assertEqual(str(refused.exception), expected)
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_a_row_written_before_the_plan_was_recorded_reads_and_blocks_nothing(self) -> None:
        old = {"unit": "dashboard", "phase": "1", "regime": "after", "started": STARTED, "merged": MERGED,
               "holds": 0, "merge_defects": 0}
        _ = self.ledger.write_text(json.dumps(old) + "\n")

        self.add_row(SECOND_PLAN, "1")

        self.assertEqual([row.plan for row in review_regime.read_rows()], [None, SECOND_PLAN])

    def test_add_needs_the_plan(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            _ = self.add("--unit", "dashboard", "--phase", "1")

        self.assertEqual(refused.exception.code, 2)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    _ = unittest.main()
