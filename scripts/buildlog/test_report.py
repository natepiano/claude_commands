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

import disk
import index
import report
from test_index import STAMP, Record, call, ci_job, ci_run, encode, local_day, point_root_at, step


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

    def render(self, day: str | None = None) -> str:
        _ = index.update()
        with closing(index.read_only()) as connection:
            return report.report(connection, day or local_day(STAMP))

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
        section = lines[lines.index("### Test builds (temporary)") : lines.index("### Summary")]
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
        self.assertIn("| clippy | 1 | 1.0 s | 1.0 s |  |", successes)
        self.assertIn("| mend | 2 | 50.0 s | 25.0 s |  |", successes)
        self.assertIn("| **All steps** | 3 | 51.0 s | 17.0 s |  |", successes)
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| clippy | 1 | 30.0 s | 30.0 s |  |", failures)
        self.assertIn("| mend | 1 | 20.0 s | 20.0 s |  |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| mend | 3 | 1 | 1.2 min | 23.3 s |  |", combined)
        self.assertIn("| **All steps** | 5 | 2 | 1.7 min | 20.2 s |  |", combined)
        self.assertIn("| reused | 1 |  | 1.0 min |", lines)
        self.assertIn("| CI | 1 | 0 | 10.0 min | 10.0 min |", lines)
        self.assertIn("| scratch (temp folders) | 1 | 0 | 1.0 s | 1.0 s |  |  |  |", lines)
        self.assertNotIn("steps under a temp folder", text)

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
        self.assertIn("| scratch (temp folders) | 2 | 0 | 15.0 s | 10.0 s – 20.0 s |", fmt)
        self.assertEqual(1, sum(line.startswith("| scratch (temp folders) |") for line in fmt))
        doc = lines[lines.index("### doc") : lines.index("### Summary: successes")]
        self.assertIn("| scratch (temp folders) | 1 | 1 | 10.0 s | 10.0 s |  |  |", doc)
        successes = lines[lines.index("### Summary: successes") : lines.index("### Summary: failures")]
        self.assertIn("| fmt | 3 | 1.0 min | 20.0 s | 2.0 GiB |", successes)
        failures = lines[lines.index("### Summary: failures") : lines.index("### Summary: all")]
        self.assertIn("| doc | 1 | 10.0 s | 10.0 s | 4.0 GiB |", failures)
        combined = lines[lines.index("### Summary: all") :]
        self.assertIn("| **All steps** | 4 | 1 | 1.2 min | 17.5 s | 4.0 GiB |", combined)

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
            with mock.patch("report.sync_time", return_value="12:14 EDT"):
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
            self.assertEqual(report.sync_time("2026-10-03T16:14:00+00:00", now), "12:14 EDT")
            self.assertEqual(report.sync_time("2026-10-02T16:14:00+00:00", now), "2026-10-02 12:14 EDT")


if __name__ == "__main__":
    _ = unittest.main()
