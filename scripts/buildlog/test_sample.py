#!/usr/bin/env python3
"""Tests for fixture parsing and one isolated machine memory sample."""

from __future__ import annotations

import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import cli
import sample
from test_index import use_test_log

use_test_log()

CLI = Path(__file__).with_name("cli.py")
Record = dict[str, object]


class SampleTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"

    def test_meminfo_fixture_gives_used_memory_and_swap_bytes(self) -> None:
        meminfo = (
            "MemTotal:       16384 kB\n"
            "MemFree:         1024 kB\n"
            "MemAvailable:    4096 kB\n"
            "SwapTotal:       8192 kB\n"
            "SwapFree:        1024 kB\n"
        )
        memory = sample.parse_meminfo(meminfo)
        self.assertIsInstance(memory, sample.MachineMemoryUse)
        self.assertEqual(memory.memory_bytes, 12_288 * 1024)
        self.assertEqual(memory.swap_bytes, 7_168 * 1024)

    def test_pressure_fixture_gives_some_and_full_microsecond_totals(self) -> None:
        pressure = (
            "some avg10=0.00 avg60=0.01 avg300=0.02 total=1234567\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=456789\n"
        )
        stalls = sample.parse_pressure(pressure)
        self.assertIsInstance(stalls, sample.MemoryStallTotals)
        self.assertEqual(stalls.some_us, 1_234_567)
        self.assertEqual(stalls.full_us, 456_789)

    def test_pressure_parser_can_leave_missing_scope_counters_unmeasured(self) -> None:
        pressure = "some avg10=0.00 total=1250000\nfull avg10=0.00 total=invalid\n"
        stalls = sample.parse_pressure(pressure, require_both=False)
        self.assertIsInstance(stalls, sample.PartialMemoryStallTotals)
        self.assertEqual(stalls.some_us, 1_250_000)
        self.assertIs(stalls.full_us, sample.Unmeasured.VALUE)
        with self.assertRaises(ValueError):
            _ = sample.parse_pressure(pressure)

    def test_boot_id_fixture_trims_newline_and_rejects_empty_text(self) -> None:
        boot_id = "d963a34a-50fa-48a8-8485-8b219c45fe02"
        self.assertEqual(sample.parse_boot_id(f"{boot_id}\n"), boot_id)
        with self.assertRaises(ValueError):
            _ = sample.parse_boot_id(" \n")

    def test_slice_anon_reads_fixture_and_marks_missing_slice_unmeasured(self) -> None:
        builds = self.root / "builds.slice"
        builds.mkdir(parents=True)
        _ = (builds / "memory.stat").write_text("file 99\nanon 123456\n")
        self.assertEqual(sample.anon_bytes(builds), 123456)
        self.assertIs(sample.anon_bytes(self.root / "missing.slice"), sample.Unmeasured.VALUE)

    def test_sample_on_a_host_without_proc_prints_one_line_and_exits_nonzero(self) -> None:
        missing = self.root / "no-proc" / "meminfo"
        command = (
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); import sample, cli; "
            "sample.MEMINFO = Path(sys.argv[2]); sys.exit(cli.main(['sample']))"
        )
        result = subprocess.run(
            [sys.executable, "-c", command, str(CLI.parent), str(missing)],
            env={**os.environ, "BUILDLOG_DIR": str(self.root)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.splitlines(), ["buildlog sample is Linux only (requires /proc)."])
        self.assertEqual(list(self.root.rglob("*.jsonl")), [])

    def test_cli_sample_appends_sample_and_snapshot_under_buildlog_dir(self) -> None:
        proc = self.root / "proc"
        proc.mkdir(parents=True)
        _ = (proc / "meminfo").write_text("MemTotal: 16384 kB\nMemAvailable: 4096 kB\nSwapTotal: 8192 kB\nSwapFree: 1024 kB\n")
        _ = (proc / "pressure").write_text("some total=100\nfull total=20\n")
        _ = (proc / "boot_id").write_text("d963a34a-50fa-48a8-8485-8b219c45fe02\n")
        builds = self.root / "builds.slice"
        ci = self.root / "ci.slice"
        builds.mkdir(parents=True)
        ci.mkdir()
        _ = (builds / "memory.stat").write_text("anon 123456\n")
        _ = (ci / "memory.stat").write_text("anon 654321\n")
        for cgroup in (builds, ci):
            for name, value in (
                ("memory.events", "high 0\nmax 0\noom_kill 0\n"),
                ("memory.peak", "123456\n"),
                ("memory.swap.peak", "0\n"),
                ("memory.high", "max\n"),
                ("memory.max", "max\n"),
                ("memory.swap.max", "max\n"),
                ("memory.pressure", "some total=2500000\nfull total=100000\n"),
            ):
                _ = (cgroup / name).write_text(value)
        environment = {**os.environ, "BUILDLOG_DIR": str(self.root), "BUILDLOG_BUILDS_CGROUP": str(builds),
                       "BUILDLOG_CI_CGROUP": str(ci), "BUILDLOG_ZRAM": str(self.root / "absent-zram")}
        command = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import sample, cli; proc = Path(sys.argv[2]); "
            "sample.MEMINFO = proc / 'meminfo'; sample.PRESSURE = proc / 'pressure'; "
            "sample.BOOT_ID = proc / 'boot_id'; sys.exit(cli.main(['sample']))"
        )
        result = subprocess.run(
            [sys.executable, "-c", command, str(CLI.parent), str(proc)],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list(self.root.glob("*/*.jsonl"))
        host = socket.gethostname().split(".")[0]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent.name, host)
        self.assertRegex(files[0].name, r"^samples-\d{4}-\d{2}\.jsonl$")
        lines = files[0].read_text().splitlines()
        self.assertEqual(len(lines), 2)
        record = cast(Record, json.loads(lines[0]))
        self.assertEqual(record["kind"], "sample")
        self.assertEqual(record["host"], host)
        self.assertRegex(str(record["at"]), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(files[0].name, "samples-" + str(record["at"])[:7] + ".jsonl")
        self.assertTrue(datetime.fromisoformat(str(record["at"])))
        self.assertTrue(re.fullmatch(r"[0-9a-f-]{36}", str(record["boot_id"])))
        self.assertEqual(record["builds_anon_bytes"], 123456)
        self.assertEqual(record["ci_anon_bytes"], 654321)
        for field in ("mem_used_bytes", "swap_used_bytes", "stall_some_us", "stall_full_us"):
            self.assertIsInstance(record[field], int)
            self.assertGreaterEqual(cast(int, record[field]), 0)
        self.assertFalse((self.root / "index.sqlite").exists())
        snapshot = cast(Record, json.loads(lines[1]))
        self.assertEqual(snapshot["kind"], "memory_snapshot")
        self.assertEqual(snapshot["at"], record["at"])
        self.assertEqual(snapshot["host"], host)
        slices = cast(dict[str, Record], snapshot["slices"])
        self.assertEqual(slices["ci"]["stall_some_us"], 2_500_000)

    def test_snapshot_failure_still_keeps_minute_sample(self) -> None:
        proc = self.root / "proc"
        proc.mkdir(parents=True)
        _ = (proc / "meminfo").write_text("MemTotal: 16 kB\nMemAvailable: 4 kB\nSwapTotal: 8 kB\nSwapFree: 1 kB\n")
        _ = (proc / "pressure").write_text("some total=100\nfull total=20\n")
        _ = (proc / "boot_id").write_text("test-boot\n")
        stderr = io.StringIO()
        with patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root),
                                  "BUILDLOG_BUILDS_CGROUP": str(self.root / "absent-builds"),
                                  "BUILDLOG_CI_CGROUP": str(self.root / "absent-ci"),
                                  "BUILDLOG_ZRAM": str(self.root / "absent-zram")}), \
             patch.object(sample, "MEMINFO", proc / "meminfo"), \
             patch.object(sample, "PRESSURE", proc / "pressure"), \
             patch.object(sample, "BOOT_ID", proc / "boot_id"), \
             patch("memory.write_snapshot", side_effect=OSError("snapshot failed")), \
             redirect_stderr(stderr):
            self.assertEqual(cli.main(["sample"]), 0)
        lines = next(self.root.glob("*/samples-*.jsonl")).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(cast(Record, json.loads(lines[0]))["kind"], "sample")
        self.assertIn("memory snapshot unavailable: snapshot failed", stderr.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
