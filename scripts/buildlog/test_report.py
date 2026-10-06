#!/usr/bin/env python3
"""Tests for report.py: a section per kind split by caller, then one summary row per kind."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import override
from unittest import mock

import disk
import ci
import index
import report
import rust_release
import sync
from test_index import STAMP, Record, call, ci_job, ci_run, encode, local_day, point_root_at, sample, step


class ReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    records: list[Record]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": temporary}))
        _ = self.enterContext(mock.patch.object(subprocess, "run"))
        _ = self.enterContext(mock.patch.object(ci, "gh_get"))
        self.records = []

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def render(self, day: str | None = None) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.report(connection, day or local_day(STAMP))

    def poll_stamp(self, at: datetime, complete: bool = True) -> None:
        path = self.root / "ci" / "polled.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({ci.REPOS[0]: {"polled_at": at.astimezone(UTC).isoformat(), "complete": complete}}))

    def ci_line(self, day: str) -> str:
        return next(line for line in self.render(day).splitlines() if line.startswith("CI: "))

    def waiting_rows(self, day: str = "2026-10-02") -> dict[str, list[str]]:
        lines = self.render(day).splitlines()
        self.assertEqual(lines[2], "### Waiting")
        start = lines.index("### Waiting")
        end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("### "))
        table = [line for line in lines[start:end] if line.startswith("|")]
        self.assertEqual(
            [cell.strip() for cell in table[0].strip("|").split("|")],
            ["Wait", "Longest", "Over 5 min", "Waited", "Total", "Worst"],
        )
        rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in table[2:]]
        self.assertEqual([row[0] for row in rows], ["Build-folder turn", "Memory admission", "CI queue"])
        return {row[0]: row[1:] for row in rows}

    def test_waiting_section_shows_each_tail_and_top_three(self) -> None:
        at = "2026-10-02T12:00:00Z"
        calls = [
            call(f"call-{name}-{wait}", started_at=at, worktree=f"/r/{name}", token_wait_s=wait)
            for name, wait in (("alpha", 600), ("alpha", 360), ("beta", 420), ("gamma", 240), ("delta", 0))
        ]
        calls.append(call("other-day", started_at="2026-10-01T12:00:00Z", token_wait_s=7200))
        steps = [
            step(f"step-{name}", started_at=at, worktree=f"/r/{name}", seat=seat, mem_wait_s=wait)
            for name, seat, wait in (("alpha", "impl", 900), ("beta", "test", 600), ("gamma", None, 360), ("delta", None, 0))
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *calls, *steps)
        jobs = [
            ci_job(number, name, "success", (f"2026-10-02T12:{minute:02d}:00Z", f"2026-10-02T12:{minute + wait // 60:02d}:00Z", f"2026-10-02T12:{minute + wait // 60 + 1:02d}:00Z"))
            for number, name, minute, wait in ((1, "Alpha", 0, 600), (2, "Beta", 12, 420), (3, "Gamma", 22, 360), (4, "Zero", 35, 0))
        ]
        jobs.append(ci_job(5, "Unknown", "success", ("2026-10-02T12:40:00Z", "unknown", "unknown")))
        first = ci_run(1, 1, jobs)
        carried = ci_run(2, 2, [ci_job(21, "Carried", "success", (
            "2026-10-02T13:00:00Z", "2026-10-02T12:10:00Z", "2026-10-02T12:11:00Z",
        ))])
        carried.update({"created_at": "2026-10-02T13:00:00Z", "started_at": "2026-10-02T13:00:00Z", "updated_at": "2026-10-02T13:10:00Z"})
        self.write(self.root / "ci" / "2026-10.jsonl", first, carried)

        rows = self.waiting_rows()
        zone = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone().strftime("%H:%M %Z")
        self.assertEqual(rows["Build-folder turn"], [
            f"10.0 min (alpha, {zone})", "3", "4 of 5 calls", "0.5 seat-hours",
            "alpha 16.0 min, beta 7.0 min, gamma 4.0 min",
        ])
        self.assertEqual(rows["Memory admission"], [
            f"15.0 min (alpha impl, {zone})", "3", "3 of 4 steps", "0.5 seat-hours",
            "alpha 15.0 min, beta 10.0 min, gamma 6.0 min",
        ])
        self.assertEqual(rows["CI queue"], [
            f"10.0 min (CI / Alpha, {zone})", "3", "3 of 4 jobs", "0.4 job-hours",
            "CI / Alpha 10.0 min, CI / Beta 7.0 min, CI / Gamma 6.0 min",
        ])

    def test_waiting_rows_remain_visible_when_no_wait_was_recorded(self) -> None:
        rows = self.waiting_rows()
        self.assertEqual(rows, {name: ["none", "", "", "", ""] for name in
                                ("Build-folder turn", "Memory admission", "CI queue")})

    def write_release(self, trial: rust_release.TrialOutcome, pin: str | None = "1.99.0") -> None:
        state: rust_release.ReleaseState = {
            "check_day": "2026-11-12",
            "stable_version": "1.100.0",
            "release_date": "2026-11-12",
            "pin": pin,
            "trial": trial,
            "text_sent": True,
        }
        rust_release.write_state(state)

    def test_report_shows_finished_rust_trial_line_once(self) -> None:
        self.write_release({
            "status": "finished", "version": "1.100.0", "warnings": 7, "warning_crates": 3,
            "errors": 0, "error_crates": 0, "mend_builds": True, "target_gib": 1.5, "mend_target_gib": 0.5,
        })
        lines = self.render().splitlines()
        expected = "Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: 7 new warnings in 3 crates, cargo-mend builds."
        self.assertEqual(1, lines.count(expected))

    def test_report_shows_waiting_rust_trial_reason(self) -> None:
        self.write_release({"status": "waiting", "version": "1.100.0", "reason": "513 GiB free, needs 550"})
        self.assertIn(
            "Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: waiting, 513 GiB free, needs 550.",
            self.render().splitlines(),
        )

    def test_report_shows_failed_rust_trial_step_and_reason(self) -> None:
        self.write_release({
            "status": "failed", "version": "1.100.0", "step": "clippy", "reason": "error: clippy broke",
            "target_gib": 1.5, "mend_target_gib": 0,
        })
        self.assertIn(
            "Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: failed, clippy: error: clippy broke.",
            self.render().splitlines(),
        )

    def test_report_omits_rust_line_without_state_or_after_pin_catches_up(self) -> None:
        self.assertNotIn("Rust 1.100.0 out", self.render())
        self.write_release({"status": "waiting", "version": "1.100.0", "reason": "awaiting quiet hours"}, pin="1.100.0")
        self.assertNotIn("Rust 1.100.0 out", self.render())

    def test_report_shows_finished_rust_errors_and_mend_result(self) -> None:
        for mend_builds, mend_text in ((True, "cargo-mend builds"), (False, "cargo-mend does not build")):
            with self.subTest(mend_builds=mend_builds):
                self.write_release({
                    "status": "finished", "version": "1.100.0", "warnings": 2, "warning_crates": 1,
                    "errors": 1, "error_crates": 1, "mend_builds": mend_builds,
                    "target_gib": 1.5, "mend_target_gib": 0.5,
                })
                self.assertIn(
                    f"Rust 1.100.0 out since 11-12; hana on 1.99.0. Trial: 2 new warnings in 1 crate, 1 error in 1 crate, {mend_text}.",
                    self.render().splitlines(),
                )

    def test_report_omits_rust_line_while_pin_is_absent(self) -> None:
        self.write_release({
            "status": "finished", "version": "1.100.0", "warnings": 0, "warning_crates": 0,
            "errors": 0, "error_crates": 0, "mend_builds": True,
            "target_gib": 0, "mend_target_gib": 0,
        }, pin=None)
        self.assertNotIn("Rust 1.100.0 out", self.render())

    def test_test_builds_split_whole_package_and_filter_calls_with_daily_hours_and_p75(self) -> None:
        started_at = "2026-10-04T12:00:00Z"
        records = [
            call(f"whole-{build}", started_at=started_at, build_s=build)
            for build in (360, 720, 1080, 1440)
        ] + [
            call(f"filter-{build}", started_at=started_at, command="test hana --filter one", build_s=build)
            for build in (90, 180, 270, 360)
        ]
        records += [
            call("unmeasured", started_at=started_at, command="test hana --filter two", build_s=None),
            call("other-day", build_s=3600),
            call("other-verb", started_at=started_at, command="check hana", verb="check", build_s=3600),
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *records)

        lines = self.render("2026-10-04").splitlines()
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Tests per edit")]
        self.assertIn("| 2026-10-04 | whole-package | 1.0 h | 18.0 min |", section)
        self.assertIn("| 2026-10-04 | --filter | 15.0 min | 4.5 min |", section)
        self.assertEqual(2, sum(line.startswith("| 2026-10-04 |") for line in section))

    def test_test_builds_show_fixed_baselines_and_one_temporary_note(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl", call("measured", started_at="2026-10-04T12:00:00Z", build_s=90))

        lines = self.render("2026-10-04").splitlines()
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Summary")]
        self.assertIn("Source: build log `verify.sh test` calls with measured `build_s`; p75 is the nearest rank.", section)
        self.assertIn("| Baseline 2026-10-01/02 | whole-package | 5.2 h | 2.3 min |", section)
        self.assertIn("| Baseline 2026-10-04 from 00:07 EDT | whole-package | 5.0 h | 1.7 min |", section)
        self.assertIn("| Baseline 2026-10-04 from 00:07 EDT | --filter | 14.4 h | 1.2 min |", section)
        note = "Temporary: kept until the user calls the result settled."
        self.assertEqual(1, section.count(note))
        self.assertEqual("", section[section.index(note) - 1])

    def test_test_builds_count_a_name_that_contains_filter_as_whole_package(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("named", started_at="2026-10-04T12:00:00Z", command="test demo--filter", build_s=90),
        )

        lines = self.render("2026-10-04").splitlines()
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Summary")]
        self.assertIn("| 2026-10-04 | whole-package | 1.5 min | 1.5 min |", section)
        self.assertFalse(any(line.startswith("| 2026-10-04 | --filter |") for line in section))

    def verify_call(
        self,
        number: int,
        *trees: str | None,
        seat: str | None = "seat-a",
        session: str = "session-a",
        verb: str = "test",
        command: str = "test hana",
        status: int | None = 0,
        outcome: str | None = None,
        minute: int | None = None,
        day: str = "2026-10-02",
    ) -> None:
        at = f"{day}T12:{(number * 5 if minute is None else minute):02d}:00.000Z"
        call_id = f"call-{number}"
        self.records.append(
            call(
                call_id,
                started_at=at,
                ended_at=at,
                delegate_session=seat,
                session=session,
                verb=verb,
                command=command,
                status=status,
                outcome=outcome or ("ran" if status == 0 else "failed"),
            )
        )
        for offset, tree in enumerate(trees):
            self.records.append(
                step(
                    f"{call_id}-step-{offset}",
                    started_at=at,
                    ended_at=at,
                    call_id=call_id,
                    tree_key=tree,
                    status=status,
                    caller="verify",
                    step="nextest" if verb == "test" else "clippy",
                )
            )

    def render_calls(self) -> str:
        self.write(self.root / "natedev" / "2026-10.jsonl", *self.records)
        return self.render()

    def per_edit_tables(self, day: str | None = None) -> tuple[list[list[str]], list[list[str]]]:
        self.write(self.root / "natedev" / "2026-10.jsonl", *self.records)
        _ = index.update()
        with closing(index.read_only()) as connection:
            lines = report.tests_per_edit_section(connection, day or local_day(STAMP))
        tables: list[list[list[str]]] = []
        previous = "other"
        for line in lines:
            if line.startswith("|"):
                if previous != "table":
                    tables.append([])
                tables[-1].append([cell.strip() for cell in line.strip("|").split("|")])
                previous = "table"
            else:
                previous = "other"
        self.assertEqual(len(tables), 2)
        return tables[0], tables[1]

    def cell(self, table: list[list[str]], label: str, heading: str) -> str:
        matching = [row for row in table[2:] if row[0] == label]
        self.assertEqual(len(matching), 1, f"row for {label}: {table}")
        return matching[0][table[0].index(heading)]

    def test_same_tree_has_no_edit_and_changed_tree_has_one(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-a")
        self.verify_call(2, "tree-b")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests"), "3")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "1")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests/edit"), "3.00")

    def test_lint_rewrite_inside_a_call_is_not_a_seat_edit(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-a", None, "tree-b", verb="lint", command="lint hana")
        self.verify_call(2, "tree-b")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "0")

    def test_call_without_known_tree_does_not_break_the_chain(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, None, verb="lint", command="lint hana")
        self.verify_call(2, "tree-b")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "1")

    def test_delegate_session_then_session_identifies_independent_seats(self) -> None:
        self.verify_call(0, "tree-a", seat="delegate-a", session="shared")
        self.verify_call(1, "tree-b", seat="delegate-b", session="shared")
        self.verify_call(2, "tree-b", seat="delegate-a", session="shared")
        self.verify_call(3, "tree-c", seat=None, session="fallback")
        self.verify_call(4, "tree-d", seat=None, session="fallback")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "2")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests"), "5")

    def test_whole_and_filtered_test_calls_both_count(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-b", command="test hana --filter parser")
        ratios, _ = self.per_edit_tables()
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests"), "2")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Edits"), "1")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Tests/edit"), "2.00")

    def test_daily_trend_sums_seats_and_shows_all_seven_days_newest_first(self) -> None:
        self.verify_call(0, "tree-a", day="2026-10-01", seat="seat-a")
        self.verify_call(1, "tree-b", day="2026-10-04", seat="seat-a")
        self.verify_call(2, "tree-x", day="2026-10-04", seat="seat-b")
        self.verify_call(3, "tree-y", day="2026-10-04", seat="seat-b")
        ratios, _ = self.per_edit_tables("2026-10-04")
        self.assertEqual(ratios[0], ["Day", "Tests", "Edits", "Tests/edit", "Target"])
        self.assertEqual([row[0] for row in ratios[2:]], [
            "2026-10-04", "2026-10-03", "2026-10-02", "2026-10-01",
            "2026-09-30", "2026-09-29", "2026-09-28",
        ])
        self.assertEqual(self.cell(ratios, "2026-10-04", "Tests"), "3")
        self.assertEqual(self.cell(ratios, "2026-10-04", "Edits"), "2")
        self.assertEqual(self.cell(ratios, "2026-10-03", "Tests"), "0")
        self.assertEqual(self.cell(ratios, "2026-10-03", "Target"), "—")

    def test_target_labels_below_on_target_and_above_at_two_decimals(self) -> None:
        self.verify_call(0, "tree-0", day="2026-10-01")
        for number in range(1, 9):
            self.verify_call(number, f"tree-{number}", verb="lint", command="lint hana", day="2026-10-01")
        self.verify_call(9, "tree-a", seat="other", day="2026-10-02", minute=0)
        self.verify_call(10, "tree-b", seat="other", day="2026-10-02", minute=5)
        self.verify_call(11, "tree-x", seat="third", day="2026-10-03", minute=0)
        self.verify_call(12, "tree-y", seat="third", day="2026-10-03", verb="lint", command="lint hana", minute=5)
        self.verify_call(13, "tree-z", seat="third", day="2026-10-03", verb="lint", command="lint hana", minute=10)
        ratios, _ = self.per_edit_tables("2026-10-03")
        self.assertEqual(self.cell(ratios, "2026-10-01", "Tests/edit"), "0.12")
        self.assertEqual(self.cell(ratios, "2026-10-01", "Target"), "below")
        self.assertEqual(self.cell(ratios, "2026-10-02", "Target"), "above")
        self.assertEqual(self.cell(ratios, "2026-10-03", "Target"), "on target")

    def test_failure_tracks_edits_since_green_and_minutes_until_next_green(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-b", verb="lint", command="lint hana", minute=5)
        self.verify_call(2, "tree-c", status=1, minute=10)
        self.verify_call(3, "tree-c", minute=35)
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "2–3", "Failures"), "1")
        self.assertEqual(self.cell(bins, "2–3", "Avg to next green"), "25.0 min")

    def test_failure_at_zero_edits_has_recovery_time_in_zero_bin(self) -> None:
        self.verify_call(0, "tree-a", minute=0, command="test hana --filter parser")
        self.verify_call(1, "tree-a", status=1, minute=10)
        self.verify_call(2, "tree-a", minute=25)
        _, bins = self.per_edit_tables()
        self.assertEqual([row[0] for row in bins[2:]], ["0", "1", "2–3", "4–7", "8+"])
        self.assertEqual(self.cell(bins, "0", "Failures"), "1")
        self.assertEqual(self.cell(bins, "0", "Avg to next green"), "15.0 min")
        self.assertNotIn("outside these bins", self.render_calls())

    def test_failed_outcome_without_status_counts_and_reused_green_recovers(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-b", minute=5, status=None, outcome="failed")
        self.verify_call(2, "tree-b", minute=10, status=0, outcome="interrupted")
        self.verify_call(3, "tree-b", minute=25, status=0, outcome="reused")
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "1", "Failures"), "1")
        self.assertEqual(self.cell(bins, "1", "Avg to next green"), "20.0 min")

    def test_nonzero_test_status_counts_as_failure_without_failed_outcome(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-a", minute=5, status=1, outcome="ran")
        self.verify_call(2, "tree-a", minute=15)
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "0", "Failures"), "1")
        self.assertEqual(self.cell(bins, "0", "Avg to next green"), "10.0 min")

    def test_failures_at_three_four_seven_and_eight_edits_use_correct_bins(self) -> None:
        number = 0
        for edits in (3, 4, 7, 8):
            seat = f"seat-{edits}"
            self.verify_call(number, "tree-0", seat=seat, minute=0)
            number += 1
            for changed in range(1, edits):
                self.verify_call(number, f"tree-{changed}", seat=seat, verb="lint", command="lint hana", minute=changed)
                number += 1
            self.verify_call(number, f"tree-{edits}", seat=seat, status=1, minute=edits)
            number += 1
            self.verify_call(number, f"tree-{edits}", seat=seat, minute=edits + 10)
            number += 1
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "1", "Failures"), "0")
        self.assertEqual(self.cell(bins, "2–3", "Failures"), "1")
        self.assertEqual(self.cell(bins, "4–7", "Failures"), "2")
        self.assertEqual(self.cell(bins, "8+", "Failures"), "1")

    def test_failure_without_later_green_counts_but_not_in_average(self) -> None:
        self.verify_call(0, "tree-a", minute=0)
        self.verify_call(1, "tree-b", verb="lint", command="lint hana", minute=5)
        self.verify_call(2, "tree-c", status=1, minute=10)
        _, bins = self.per_edit_tables()
        self.assertEqual(self.cell(bins, "2–3", "Failures"), "1")
        self.assertEqual(self.cell(bins, "2–3", "Avg to next green"), "")
        text = self.render_calls()
        self.assertIn("1 without a later green", text)

    def test_rendered_tests_per_edit_section_has_two_tables_target_and_source(self) -> None:
        self.verify_call(0, "tree-a")
        self.verify_call(1, "tree-b")
        text = self.render_calls()
        lines = text.splitlines()
        self.assertIn("### Tests per edit", lines)
        section_start = lines.index("### Tests per edit")
        section_end = next((index for index in range(section_start + 1, len(lines)) if lines[index].startswith("### ")), len(lines))
        section = lines[section_start:section_end]
        self.assertEqual(sum(line.startswith("|---") for line in section), 2)
        self.assertIn("Target", "\n".join(section))
        source_lines = [line for line in section if line.startswith("Source:")]
        self.assertEqual(len(source_lines), 2)
        self.assertRegex(source_lines[0], r"target \d")
        self.assertIn("2026-09-26–2026-10-02", source_lines[0])
        self.assertIn("2026-09-26–2026-10-02", source_lines[1])

    def memory_section(self) -> list[str]:
        lines = self.render().splitlines()
        start = lines.index("### Memory pressure")
        end = next((offset for offset in range(start + 1, len(lines)) if lines[offset].startswith("### ")), len(lines))
        return lines[start:end]

    def test_memory_pressure_ranks_five_stalled_steps_and_counts_same_host_overlap(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("s1", caller="verify", started_at="2026-10-02T12:00:00.000Z", ended_at="2026-10-02T12:10:00.000Z", mem_stall_some_s=12.0),
            step("s2", step="mend", started_at="2026-10-02T12:01:00.000Z", ended_at="2026-10-02T12:09:00.000Z", mem_stall_some_s=9.0),
            step("s3", started_at="2026-10-02T12:02:00.000Z", ended_at="2026-10-02T12:08:00.000Z", mem_stall_some_s=8.0),
            step("s4", started_at="2026-10-02T12:03:00.000Z", ended_at="2026-10-02T12:04:00.000Z", mem_stall_some_s=7.0),
            step("s5", started_at="2026-10-02T12:04:00.000Z", ended_at="2026-10-02T12:05:00.000Z", mem_stall_some_s=6.0),
            step("s6", started_at="2026-10-02T12:05:00.000Z", ended_at="2026-10-02T12:06:00.000Z", mem_stall_some_s=5.0),
        )
        self.write(
            self.root / "mac" / "2026-10.jsonl",
            step("other-host", host="mac", started_at="2026-10-02T12:00:00.000Z", ended_at="2026-10-02T12:10:00.000Z"),
        )
        section = self.memory_section()
        self.assertIn("| Caller | Kind | Stall | At once |", section)
        self.assertIn("| verify.sh (agents) (natedev) | clippy | 12.0 s | 1 |", section)
        self.assertIn("| agent (direct) (natedev) | mend | 9.0 s | 2 |", section)
        self.assertTrue(any("| 8.0 s | 3 |" in line for line in section))
        self.assertTrue(any("| 7.0 s | 4 |" in line for line in section))
        self.assertTrue(any("| 6.0 s | 4 |" in line for line in section))
        self.assertFalse(any("5.0 s" in line for line in section))
        self.assertEqual(sum(line.startswith("| ") and ("| clippy |" in line or "| mend |" in line) for line in section), 5)
        self.assertLess(self.render().index("### clippy"), self.render().index("### Memory pressure"))

    def test_memory_pressure_labels_temp_folder_stalls_as_scratch_and_counts_their_overlap(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("scratch", cwd="/tmp/scratch", mem_stall_some_s=30.0, ended_at="2026-10-02T12:10:00.000Z"),
            step("kept", caller="verify", mem_stall_some_s=5.0, ended_at="2026-10-02T12:10:00.000Z"),
        )
        section = self.memory_section()
        self.assertIn("| scratch (temp folders) | clippy | 30.0 s | 2 |", section)
        self.assertIn("| verify.sh (agents) | clippy | 5.0 s | 2 |", section)

    def test_memory_pressure_uses_sample_peaks_and_reboot_counter_deltas(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:00:00.000Z", mem_used_bytes=4 * 2**30, swap_used_bytes=2 * 2**30, stall_some_us=10_000_000, stall_full_us=2_000_000, builds_anon_bytes=2 * 2**30, ci_anon_bytes=3 * 2**30),
            sample("2026-10-02T12:01:00.000Z", mem_used_bytes=8 * 2**30, swap_used_bytes=7 * 2**30, stall_some_us=12_000_000, stall_full_us=3_000_000, builds_anon_bytes=4 * 2**30, ci_anon_bytes=5 * 2**30),
            sample("2026-10-02T12:02:00.000Z", boot_id="boot-b", mem_used_bytes=6 * 2**30, swap_used_bytes=5 * 2**30, stall_some_us=500_000, stall_full_us=100_000),
            sample("2026-10-02T12:03:00.000Z", boot_id="boot-b", mem_used_bytes=5 * 2**30, swap_used_bytes=4 * 2**30, stall_some_us=1_500_000, stall_full_us=500_000),
        )
        section = self.memory_section()
        self.assertTrue(any("Source:" in line and "60 s" in line and "step" in line and "stall" in line for line in section))
        self.assertTrue(any("8.0 GiB" in line and "7.0 GiB" in line for line in section))
        self.assertTrue(any("3.5 s" in line and "1.5 s" in line for line in section))
        self.assertTrue(any("peak builds 4.0 GiB, peak CI 5.0 GiB (process memory)" in line for line in section))
        self.assertIn("memory waits: none", section)

    def test_memory_pressure_sums_admission_waits_and_names_longest_caller(self) -> None:
        self.write(self.root / "natedev" / "2026-10.jsonl", step("s1", mem_wait_s=12, caller="verify"), step("s2", mem_wait_s=4))
        section = self.memory_section()
        self.assertIn("memory waits: 2 steps, total 16.0 s, longest 12.0 s (verify.sh (agents))", section)

    def test_memory_pressure_samples_without_stalled_steps_have_no_table(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", sample(STAMP))
        section = self.memory_section()
        self.assertFalse(any(line.startswith("|") for line in section))
        self.assertTrue(any("4.0 GiB" in line and "2.0 GiB" in line for line in section))
        self.assertTrue(any("some 0.0 s" in line and "full 0.0 s" in line for line in section))

    def test_memory_pressure_compares_adjacent_samples_per_host(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:00:00.000Z", stall_some_us=20_000_000, stall_full_us=5_000_000),
            sample("2026-10-02T12:02:00.000Z", stall_some_us=21_000_000, stall_full_us=5_200_000),
        )
        self.write(
            self.root / "mac" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:01:00.000Z", host="mac", stall_some_us=40_000_000, stall_full_us=8_000_000),
            sample("2026-10-02T12:03:00.000Z", host="mac", stall_some_us=42_000_000, stall_full_us=8_300_000),
        )
        section = self.memory_section()
        self.assertTrue(any("some 3.0 s" in line and "full 0.5 s" in line for line in section))

    def test_memory_pressure_ignores_counter_rise_across_a_long_sampling_gap(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-01T12:00:00.000Z", stall_some_us=10_000_000, stall_full_us=2_000_000),
            sample(STAMP, stall_some_us=12_000_000, stall_full_us=3_000_000),
        )
        section = self.memory_section()
        self.assertTrue(any("some 0.0 s" in line and "full 0.0 s" in line for line in section))

    def test_memory_pressure_counts_counter_rise_across_midnight_when_samples_are_close(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            time.tzset()
            self.write(
                self.root / "natedev" / "samples-2026-10.jsonl",
                sample("2026-10-01T23:59:00.000Z", stall_some_us=10_000_000, stall_full_us=2_000_000),
                sample("2026-10-02T00:01:00.000Z", stall_some_us=12_000_000, stall_full_us=3_000_000),
            )
            _ = index.update()
            with closing(index.read_only()) as connection:
                section = report.memory_pressure_section(connection, "2026-10-02", 0)
        self.assertTrue(any("some 2.0 s" in line and "full 1.0 s" in line for line in section))

    def test_memory_pressure_empty_day_shows_instrument_availability(self) -> None:
        lines = self.render().splitlines()
        self.assertIn("### Memory pressure", lines)
        self.assertIn("memory waits: none", lines)
        self.assertIn("sccache service: unavailable", lines)
        self.assertIn("unsliced steps: none", lines)
        self.assertIn("memory kills: none", lines)

    def test_kinds_by_caller_then_one_summary_row_per_kind(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("m1", step="mend", caller="verify", duration_s=40.0, mend_s=3.0, mend_check_s=37.0, mend_fixes=0),
            step("m2", step="mend", caller="cargo-port", duration_s=10.0, mend_s=4.0, mend_check_s=6.0),
            step("m3", step="mend", caller="cargo-port", duration_s=20.0, status=101),
            step("c1", step="clippy", caller="verify", duration_s=30.0, errors=2, status=101),
            step("x1", step="clippy", caller="agent", cwd="/tmp/claude/scratch", duration_s=1.0),
            call("v1", outcome="reused", saved_s=60),
        )
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "test", "success", ("2026-10-02T12:00:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:09:00Z"))]),
        )
        text = self.render()
        lines = text.splitlines()

        self.assertLess(lines.index("### clippy"), lines.index("### mend"))
        self.assertLess(lines.index("### mend"), lines.index("### Summary: successes"))
        self.assertLess(lines.index("### Summary: successes"), lines.index("### Summary: failures"))
        self.assertLess(lines.index("### Summary: failures"), lines.index("### Summary: all"))
        self.assertIn("| verify.sh (agents) | 1 | 0 | 40.0 s | 40.0 s | 40.0 s | 3.0 s | 3.0 s | 37.0 s | 37.0 s | 0 |", lines)
        self.assertIn("| cargo-port | 2 | 1 | 15.0 s | 20.0 s | 10.0 s – 20.0 s | 4.0 s | 4.0 s | 6.0 s | 6.0 s |  |", lines)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| clippy | 1 | 1.0 s | 1.0 s | 1.0 s |  |", successes)
        self.assertIn("| mend | 2 | 50.0 s | 25.0 s | 40.0 s |  |", successes)
        self.assertIn("| **All steps** | 3 | 51.0 s | 17.0 s | 40.0 s |  |", successes)
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| clippy | 1 | 30.0 s | 30.0 s | 30.0 s |  |", failures)
        self.assertIn("| mend | 1 | 20.0 s | 20.0 s | 20.0 s |  |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| mend | 3 | 1 | 1.2 min | 23.3 s | 40.0 s |  |", combined)
        self.assertIn("| **All steps** | 5 | 2 | 1.7 min | 20.2 s | 40.0 s |  |", combined)
        self.assertIn("| reused | 1 |  | 1.0 min |", lines)
        self.assertIn("| CI | 1 | 0 | 0 | 10.0 min | 10.0 min | 10.0 min |", lines)
        self.assertIn("| scratch (temp folders) | 1 | 0 | 1.0 s | 1.0 s | 1.0 s |  |  |  |  |", lines)
        self.assertNotIn("steps under a temp folder", text)

    def test_launch_in_temp_folder_counts_host_for_caller_labels(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("local", host="natedev", caller="verify", step="check"),
        )
        self.write(
            self.root / "mac" / "2026-10.jsonl",
            step("launch", host="mac", caller="brp-launch", step="build", cwd="/tmp/app"),
        )

        lines = self.render().splitlines()
        self.assertTrue(any(line.startswith("| verify.sh (agents) (natedev) |") for line in lines))
        self.assertTrue(any(line.startswith("| example launches (brp) (mac) |") for line in lines))

    def test_ci_queue_summary_when_all_queue_times_are_known(self) -> None:
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z"))]),
        )
        summary = next(line for line in self.render().splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "jobs queued 30.0 s on average, p95 30.0 s; CI never polled.")

    def test_ci_queue_p95_uses_the_same_jobs_as_average(self) -> None:
        at = datetime.fromisoformat("2026-10-02T12:00:00+00:00")
        jobs = [
            ci_job(number, f"Job {number}", "success", (
                at.isoformat(),
                (at + timedelta(seconds=wait)).isoformat(),
                (at + timedelta(seconds=wait + 1)).isoformat(),
            ))
            for number, wait in enumerate((10, 20, 30), 1)
        ]
        self.write(self.root / "ci" / "2026-10.jsonl", ci_run(1, 1, jobs))
        self.assertIn("jobs queued 20.0 s on average, p95 30.0 s", self.ci_line("2026-10-02"))

    def test_ci_queue_summary_uses_run_attempt_day_for_job_created_after_midnight(self) -> None:
        first_start = datetime(2026, 10, 1, 12).astimezone().isoformat()
        first_job_start = datetime(2026, 10, 1, 12, 0, 10).astimezone().isoformat()
        first_end = datetime(2026, 10, 1, 12, 2).astimezone().isoformat()
        rerun_start = datetime(2026, 10, 2, 23, 59).astimezone().isoformat()
        job_created = datetime(2026, 10, 3, 0, 1).astimezone().isoformat()
        job_started = datetime(2026, 10, 3, 0, 1, 30).astimezone().isoformat()
        job_completed = datetime(2026, 10, 3, 0, 3).astimezone().isoformat()
        first = ci_run(1, 1, [ci_job(11, "Test", "success", (first_start, first_job_start, first_end))])
        first.update({"created_at": first_start, "started_at": first_start, "updated_at": first_end})
        rerun = ci_run(1, 2, [ci_job(21, "Test", "success", (job_created, job_started, job_completed))])
        rerun.update({"created_at": rerun_start, "started_at": rerun_start, "updated_at": job_completed})
        self.write(self.root / "ci" / "2026-10.jsonl", first, rerun)

        summary = next(line for line in self.render("2026-10-02").splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "jobs queued 30.0 s on average, p95 30.0 s; CI never polled.")
        self.assertEqual(self.waiting_rows()["CI queue"][2], "1 of 1 jobs")
        self.assertIn("CI: no runs; CI never polled.", self.render("2026-10-03"))
        self.assertEqual(self.waiting_rows("2026-10-03")["CI queue"], ["none", "", "", "", ""])

    def test_ci_queue_summary_counts_unknown_times_but_not_skipped_jobs_as_left_out(self) -> None:
        original = ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")
        carried = ("2026-10-02T12:20:00Z", original[1], original[2])
        skipped = ("2026-10-02T12:21:00Z", "2026-10-02T12:21:00Z", "2026-10-02T12:20:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", original)]),
            ci_run(1, 2, [
                ci_job(21, "Test", "success", carried),
                ci_job(22, "Skipped", "skipped", skipped),
                ci_job(23, "Other", "success", ("2026-10-02T12:22:00Z", "unknown", "unknown")),
            ]),
        )
        summary = next(line for line in self.render().splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "jobs queued 30.0 s on average, p95 30.0 s, 2 without a known queue time left out; CI never polled.")

    def test_ci_queue_summary_when_no_job_has_a_known_queue_time(self) -> None:
        original = ("2026-10-02T12:00:00Z", "2026-10-02T12:00:30Z", "2026-10-02T12:02:30Z")
        carried = ("2026-10-03T12:20:00Z", original[1], original[2])
        rerun = ci_run(1, 2, [ci_job(21, "Test", "success", carried)])
        rerun.update({"created_at": "2026-10-03T12:20:00Z", "started_at": "2026-10-03T12:20:00Z", "updated_at": "2026-10-03T12:30:00Z"})
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Test", "success", original)]),
            rerun,
        )
        summary = next(line for line in self.render("2026-10-03").splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "no job has a known queue time (1 left out); CI never polled.")

    def test_ci_queue_summary_with_only_skipped_jobs_has_no_left_out_suffix(self) -> None:
        skipped = ("2026-10-02T12:01:00Z", "2026-10-02T12:01:00Z", "2026-10-02T12:00:59Z")
        self.write(
            self.root / "ci" / "2026-10.jsonl",
            ci_run(1, 1, [ci_job(11, "Skipped", "skipped", skipped)]),
        )
        summary = next(line for line in self.render().splitlines() if line.startswith("CI: "))
        self.assertEqual(summary.split("; ", 1)[1], "no job has a known queue time; CI never polled.")

    def test_scratch_steps_form_one_caller_per_kind_and_count_toward_peak_memory(self) -> None:
        gib = 1073741824
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("regular", step="fmt", duration_s=30.0, peak_mem_bytes=gib),
            step("scratch-tmp", step="fmt", cwd="/tmp/agent/crate", caller="verify", duration_s=10.0, peak_mem_bytes=2 * gib),
            step("scratch-private", step="doc", cwd="/private/var/folders/agent", status=101, peak_mem_bytes=4 * gib),
        )
        self.write(
            self.root / "macbook" / "2026-10.jsonl",
            step("scratch-mac", host="macbook", step="fmt", cwd="/var/folders/agent", caller="alias", duration_s=20.0),
        )

        lines = self.render().splitlines()
        fmt = lines[lines.index("### fmt") : lines.index("### doc")]
        self.assertIn("| scratch (temp folders) | 2 | 0 | 15.0 s | 20.0 s | 10.0 s – 20.0 s |", fmt)
        self.assertEqual(1, sum(line.startswith("| scratch (temp folders) |") for line in fmt))
        doc = lines[lines.index("### doc") : lines.index("### Summary: successes")]
        self.assertIn("| scratch (temp folders) | 1 | 1 | 10.0 s | 10.0 s | 10.0 s |  |  |  |", doc)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| fmt | 3 | 1.0 min | 20.0 s | 30.0 s | 2.0 GiB |", successes)
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| doc | 1 | 10.0 s | 10.0 s | 10.0 s | 4.0 GiB |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| **All steps** | 4 | 1 | 1.2 min | 17.5 s | 30.0 s | 4.0 GiB |", combined)

    def test_port_lint_calls_have_their_own_section(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("c1", step="clippy", caller="cargo-port", duration_s=30.0),
            call("v1", outcome="reused", saved_s=60),
            call("p1", tool="port-lint", outcome="reused", saved_s=90),
            call("p2", tool="port-lint", outcome="reused", saved_s=30),
            call("p3", tool="port-lint", outcome="deferred", status=75),
        )
        text = self.render()
        lines = text.splitlines()
        verify = lines[lines.index("### Agent calls (verify.sh)") : lines.index("### cargo-port calls (port-lint)")]
        self.assertIn("| reused | 1 |  | 1.0 min |", verify)
        port = lines[lines.index("### cargo-port calls (port-lint)") : lines.index("### CI") if "### CI" in lines else None]
        self.assertIn("| Outcome | Calls | Saved |", port)
        self.assertIn("| reused | 2 | 2.0 min |", port)
        self.assertIn("| deferred | 1 |  |", port)
        self.assertIn("Agent calls: 1 (1 reused), 1.0 min saved by pass records.", lines)
        self.assertIn("cargo-port calls: 3 (2 reused, 1 deferred), 2.0 min saved by recorded steps.", lines)

    def test_empty_day(self) -> None:
        text = self.render()
        self.assertIn("No build steps recorded.", text)
        self.assertIn("Agent calls: none.", text)
        self.assertIn("CI: no runs; CI never polled.", text)
        self.assertNotIn("cargo-port calls", text)

    def test_disk_section_precedes_summaries_with_steps(self) -> None:
        unit = 2**30
        snapshot: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [
                {"label": "~/rust", "bytes": unit},
                {"label": "/tmp", "bytes": 2 * unit},
                {"label": "CI runner 1", "bytes": unit},
                {"label": "CI runner 2", "bytes": unit},
            ],
            "used": 10 * unit,
            "free": 7 * unit,
            "floor": 500 * unit,
        }
        self.write(self.root / "natedev" / "2026-10.jsonl", step("one"))

        with mock.patch("report.disk.read_snapshot", return_value=snapshot) as read_snapshot:
            with mock.patch("sync.sync_time", return_value="12:14 EDT"):
                lines = self.render().splitlines()

        self.assertLess(lines.index("### Disk: natedev"), lines.index("### Summary: successes"))
        section = lines[lines.index("### Disk: natedev") : lines.index("### Summary: successes")]
        self.assertEqual(
            [
                "### Disk: natedev",
                "",
                "| Where | Size |",
                "|---|--:|",
                "| ~/rust | 1.0 GiB |",
                "| /tmp | 2.0 GiB |",
                "| CI runner 1 | 1.0 GiB |",
                "| CI runner 2 | 1.0 GiB |",
                "| other | 5.0 GiB |",
                "| free (floor 500.0 GiB) | 7.0 GiB |",
                "",
                "Measured by the buildlog disk job at 12:14 EDT: allocated blocks, each hard-linked file once.",
                "",
            ],
            section,
        )
        read_snapshot.assert_called_once_with()

    def test_disk_section_precedes_empty_summary_without_floor(self) -> None:
        snapshot: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [
                {"label": "~/rust", "bytes": 0},
                {"label": "/tmp", "bytes": 0},
                {"label": "CI runner 1", "bytes": 0},
                {"label": "CI runner 2", "bytes": 0},
            ],
            "used": 0,
            "free": 2**30,
            "floor": None,
        }
        with mock.patch("report.disk.read_snapshot", return_value=snapshot):
            lines = self.render().splitlines()

        self.assertLess(lines.index("### Disk: natedev"), lines.index("### Summary"))
        self.assertIn("| free | 1.0 GiB |", lines)
        self.assertNotIn("free (floor", "\n".join(lines))

    def test_disk_section_clamps_other_when_rows_exceed_used(self) -> None:
        unit = 2**30
        snapshot: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [{"label": "~/rust", "bytes": 30 * unit}],
            "used": 10 * unit,
            "free": unit,
            "floor": None,
        }
        with mock.patch("report.disk.read_snapshot", return_value=snapshot):
            lines = report.disk_section()

        self.assertIn("| other | 0.0 GiB |", lines)
        self.assertNotIn("| other | -20.0 GiB |", lines)

    def test_disk_table_reads_old_and_new_snapshot_shapes(self) -> None:
        base: disk.DiskSnapshot = {
            "measured_at": "2026-10-04T16:14:00+00:00",
            "host": "natedev",
            "rows": [{"label": "/tmp", "bytes": 2**30}],
            "used": 2 * 2**30,
            "free": 3 * 2**30,
            "floor": None,
        }
        disk.write_snapshot(base)
        old_lines = report.disk_section()
        expanded: disk.DiskSnapshot = {
            **base,
            "previous_measured_at": "2026-10-04T16:04:00+00:00",
            "outside_build_caches": [{
                "path": "/tmp/traces", "bytes": 2**30, "growth_bytes": 2**30,
                "largest_child_path": None, "largest_child_growth_bytes": None,
            }],
            "outside_build_cache_totals": [
                {"label": "/tmp", "bytes": 2**30, "growth_bytes": 2**30},
            ],
        }
        disk.write_snapshot(expanded)
        self.assertEqual(report.disk_section(), old_lines)
        self.assertIn("| /tmp | 1.0 GiB |", old_lines)

    def test_no_disk_section_without_snapshot(self) -> None:
        with mock.patch("report.disk.read_snapshot", return_value=None):
            lines = self.render().splitlines()

        self.assertFalse(any(line.startswith("### Disk:") for line in lines))
        self.assertIn("### Summary", lines)

    def test_sync_time_names_its_zone_and_an_earlier_day(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            now = datetime.fromisoformat("2026-10-03T15:00:00-04:00")
            self.assertEqual(sync.sync_time("2026-10-03T16:14:00+00:00", now), "12:14 EDT")
            self.assertEqual(sync.sync_time("2026-10-02T16:14:00+00:00", now), "2026-10-02 12:14 EDT")

    def test_nearest_rank_matches_p95_boundaries_and_single_value(self) -> None:
        self.assertEqual(report.nearest_rank(list(range(1, 21)), 95), 19)
        self.assertEqual(report.nearest_rank(list(range(1, 22)), 95), 20)
        self.assertEqual(report.nearest_rank([37], 95), 37)
        self.assertEqual(report.nearest_rank([360, 720, 1080, 1440], 75), 1080)

    def test_ci_freshness_for_today_and_a_past_day(self) -> None:
        now = datetime.now(UTC)
        today = now.astimezone().date().isoformat()
        yesterday = (now.astimezone().date() - timedelta(days=1)).isoformat()
        old = now - timedelta(hours=3)
        self.poll_stamp(old)
        stale = self.ci_line(today)
        self.assertTrue(stale.startswith("CI: no runs; "))
        self.assertIn(f"CI recorded through {sync.sync_time(old.isoformat(), now)}, no poll since", stale)

        self.poll_stamp(now - timedelta(minutes=30))
        self.assertEqual(self.ci_line(today), "CI: no runs.")

        self.poll_stamp(now, complete=False)
        capped = self.ci_line(today)
        self.assertIn(f"CI recorded through {sync.sync_time(now.isoformat(), now)}", capped)
        self.assertIn("the last poll was capped", capped)
        self.assertEqual(self.ci_line(yesterday), "CI: no runs.")

    def test_missing_ci_stamp_reports_never_polled(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        self.assertEqual(self.ci_line(today), "CI: no runs; CI never polled.")

    def test_ci_cancellation_count_and_p95_follow_workflow_runs(self) -> None:
        today = datetime.now().astimezone().date().isoformat()
        at = datetime.now(UTC).replace(microsecond=0)
        runs: list[Record] = []
        for number, (conclusion, duration) in enumerate((("success", 60), ("failure", 120), ("cancelled", 180)), 1):
            run = ci_run(number, 1, [])
            run.update({
                "conclusion": conclusion,
                "created_at": at.isoformat(),
                "started_at": at.isoformat(),
                "updated_at": (at + timedelta(seconds=duration)).isoformat(),
            })
            runs.append(run)
        self.write(self.root / "ci" / "2026-10.jsonl", *runs)
        lines = self.render(today).splitlines()
        self.assertIn("| Workflow | Runs | Failed | Cancelled | Avg | p95 | Range |", lines)
        self.assertIn("| CI | 3 | 1 | 1 | 2.0 min | 3.0 min | 1.0 min – 3.0 min |", lines)
        summary = next(line for line in lines if line.startswith("CI: "))
        self.assertIn("3 runs, 1 failed, 1 cancelled", summary)

        runs[2]["conclusion"] = "success"
        self.write(self.root / "ci" / "2026-10.jsonl", *runs)
        self.assertNotIn("cancelled", self.ci_line(today))

    def test_pause_note_precedes_last_good_mac_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _ = (self.root / "sync.json").write_text(json.dumps({
            "at": "2026-10-06T10:00:00Z", "peer": "mac", "ok": True,
            "last_ok": "2026-10-06T10:00:00Z",
        }))
        _ = (self.root / "sync_paused.json").write_text(json.dumps({
            "since": "2026-10-06T11:00:00Z", "why": "Mac hold",
        }))
        note = report.mac_note()
        self.assertTrue(note.startswith("Mac: sync paused since "))
        self.assertIn("(Mac hold). Mac rows as of the ", note)

    def test_every_step_kind_and_summary_places_p95_after_average(self) -> None:
        kinds = ("check", "clippy", "doc", "nextest", "mend", "fmt")
        records = [
            step(f"{kind}-{value}", step=kind, duration_s=float(value), finished_s=float(value * 2),
                 mend_s=float(value * 3), mend_check_s=float(value * 4))
            for kind in kinds for value in range(1, 22)
        ]
        self.write(self.root / "natedev" / "2026-10.jsonl", *records)
        lines = self.render().splitlines()
        for kind in kinds:
            with self.subTest(kind=kind):
                start = lines.index(f"### {kind}")
                head = [cell.strip() for cell in lines[start + 2].strip("|").split("|")]
                row = [cell.strip() for cell in lines[start + 4].strip("|").split("|")]
                self.assertEqual(head[head.index("Avg") + 1], "p95")
                self.assertEqual(row[head.index("Avg")], "11.0 s")
                self.assertEqual(row[head.index("p95")], "20.0 s")
                for title, expected in (("Build", "40.0 s"), ("Mend's own", "1.0 min"), ("Check", "1.3 min")):
                    if title in head:
                        self.assertEqual(head[head.index(title) + 1], f"{title} p95")
                        self.assertEqual(row[head.index(f"{title} p95")], expected)
        for name in ("successes", "failures", "all"):
            start = lines.index(f"### Summary: {name}")
            if name == "failures":
                continue
            head = [cell.strip() for cell in lines[start + 2].strip("|").split("|")]
            self.assertEqual(head[head.index("Avg") + 1], "p95")
            first = [cell.strip() for cell in lines[start + 4].strip("|").split("|")]
            self.assertEqual(first[head.index("p95")], "20.0 s")

    def test_failure_summary_and_edits_since_green_show_p95(self) -> None:
        for number, recovery in enumerate((5, 10, 20)):
            seat = f"seat-{number}"
            base = number * 15
            self.verify_call(number * 3, "tree-a", seat=seat, minute=base)
            self.verify_call(number * 3 + 1, "tree-b", seat=seat, status=1, minute=base + 1)
            self.verify_call(number * 3 + 2, "tree-b", seat=seat, minute=base + 1 + recovery)
        _, bins = self.per_edit_tables()
        self.assertEqual(bins[0][bins[0].index("Avg to next green") + 1], "p95 to next green")
        self.assertEqual(self.cell(bins, "1", "p95 to next green"), "20.0 min")

        self.write(self.root / "natedev" / "2026-10.jsonl", *self.records,
                   step("failed-one", step="fmt", duration_s=10.0, status=1),
                   step("failed-two", step="fmt", duration_s=20.0, status=1))
        lines = self.render().splitlines()
        start = lines.index("### Summary: failures")
        head = [cell.strip() for cell in lines[start + 2].strip("|").split("|")]
        row = [cell.strip() for cell in lines[start + 4].strip("|").split("|")]
        self.assertEqual(head[head.index("Avg") + 1], "p95")
        self.assertEqual(row[head.index("p95")], "20.0 s")


if __name__ == "__main__":
    _ = unittest.main()
