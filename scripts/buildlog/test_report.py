#!/usr/bin/env python3
"""Tests for report.py: a section per kind split by caller, then one summary row per kind."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import override
from unittest import mock

import index
import report
from test_index import STAMP, Record, call, ci_job, ci_run, encode, local_day, point_root_at, sample, step


class ReportTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "buildlog"
        point_root_at(self, self.root)

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def render(self) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.report(connection, local_day(STAMP))

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

    def test_memory_pressure_excludes_temp_folder_stalls_but_counts_their_overlap(self) -> None:
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("scratch", cwd="/tmp/scratch", mem_stall_some_s=30.0, ended_at="2026-10-02T12:10:00.000Z"),
            step("kept", caller="verify", mem_stall_some_s=5.0, ended_at="2026-10-02T12:10:00.000Z"),
        )
        section = self.memory_section()
        self.assertIn("| verify.sh (agents) | clippy | 5.0 s | 2 |", section)
        self.assertFalse(any("30.0 s" in line for line in section))
        self.assertIn("1 steps under a temp folder (scratch and test builds) are left out.", self.render())

    def test_memory_pressure_uses_sample_peaks_and_reboot_counter_deltas(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-02T12:00:00.000Z", mem_used_bytes=4 * 2**30, swap_used_bytes=2 * 2**30, stall_some_us=10_000_000, stall_full_us=2_000_000),
            sample("2026-10-02T12:01:00.000Z", mem_used_bytes=8 * 2**30, swap_used_bytes=7 * 2**30, stall_some_us=12_000_000, stall_full_us=3_000_000),
            sample("2026-10-02T12:02:00.000Z", boot_id="boot-b", mem_used_bytes=6 * 2**30, swap_used_bytes=5 * 2**30, stall_some_us=500_000, stall_full_us=100_000),
            sample("2026-10-02T12:03:00.000Z", boot_id="boot-b", mem_used_bytes=5 * 2**30, swap_used_bytes=4 * 2**30, stall_some_us=1_500_000, stall_full_us=500_000),
        )
        section = self.memory_section()
        self.assertTrue(any("Source:" in line and "60 s" in line and "step" in line and "stall" in line for line in section))
        self.assertTrue(any("8.0 GiB" in line and "7.0 GiB" in line for line in section))
        self.assertTrue(any("3.5 s" in line and "1.5 s" in line for line in section))

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

    def test_memory_pressure_empty_day_has_one_message_and_no_table(self) -> None:
        lines = self.render().splitlines()
        self.assertIn("Memory pressure: no samples and no step stalls.", lines)
        self.assertNotIn("### Memory pressure", lines)
        self.assertFalse(any(line.startswith("Source: 60 s machine samples") for line in lines))

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
        self.assertIn("| verify.sh (agents) | 1 | 0 | 40.0 s | 40.0 s | 3.0 s | 37.0 s | 0 |", lines)
        self.assertIn("| cargo-port | 2 | 1 | 15.0 s | 10.0 s – 20.0 s | 4.0 s | 6.0 s |  |", lines)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| mend | 2 | 50.0 s | 25.0 s |  |", successes)
        self.assertIn("| **All steps** | 2 | 50.0 s | 25.0 s |  |", successes)
        self.assertFalse(any(line.startswith("| clippy") for line in successes))
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| clippy | 1 | 30.0 s | 30.0 s |  |", failures)
        self.assertIn("| mend | 1 | 20.0 s | 20.0 s |  |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| mend | 3 | 1 | 1.2 min | 23.3 s |  |", combined)
        self.assertIn("| **All steps** | 4 | 2 | 1.7 min | 25.0 s |  |", combined)
        self.assertIn("| reused | 1 |  | 1.0 min |", lines)
        self.assertIn("| CI | 1 | 0 | 10.0 min | 10.0 min |", lines)
        self.assertIn("1 steps under a temp folder (scratch and test builds) are left out.", text)
        self.assertNotIn("agent (direct)", text)

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
        self.assertIn("CI: no runs.", text)
        self.assertNotIn("cargo-port calls", text)

    def test_sync_time_names_its_zone_and_an_earlier_day(self) -> None:
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            now = datetime.fromisoformat("2026-10-03T15:00:00-04:00")
            self.assertEqual(report.sync_time("2026-10-03T16:14:00+00:00", now), "12:14 EDT")
            self.assertEqual(report.sync_time("2026-10-02T16:14:00+00:00", now), "2026-10-02 12:14 EDT")


if __name__ == "__main__":
    _ = unittest.main()
