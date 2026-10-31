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
import store
from test_index import use_test_log

use_test_log()

CLI = Path(__file__).with_name("cli.py")
Record = dict[str, object]


class SampleTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / "buildlog"
        proc = self.root / "proc"
        proc.mkdir(parents=True, exist_ok=True)
        for name, content in (
            ("cpu_pressure", "some total=12000000\nfull total=3000000\n"),
            ("io_pressure", "some total=4000000\nfull total=1000000\n"),
            ("stat", "cpu  100 20 30 400 50 6 7 8 90 10\ncpu0 1 2 3 4 5 6 7 8\n"),
        ):
            _ = (proc / name).write_text(content)
        for name, filename in (("CPU_PRESSURE", "cpu_pressure"), ("IO_PRESSURE", "io_pressure"), ("STAT", "stat")):
            _ = self.enterContext(patch.object(sample, name, proc / filename))
        _ = self.enterContext(patch.object(sample, "POWER_PROFILE_STATE", self.root / "absent-profile"))

    def test_cpu_ticks_uses_aggregate_and_excludes_guest_fields(self) -> None:
        ticks = sample.parse_cpu_ticks("cpu0 9 9 9 9 9 9 9 9\ncpu 100 20 30 400 50 6 7 8 900 100\n")
        self.assertEqual((ticks.idle, ticks.iowait, ticks.total), (400, 50, 621))

    def test_cpu_ticks_rejects_missing_short_and_malformed_aggregate(self) -> None:
        for text in ("cpu0 1 2 3 4 5 6 7 8\n", "cpu 1 2 3 4 5 6 7\n", "cpu 1 2 bad 4 5 6 7 8\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _ = sample.parse_cpu_ticks(text)

    def test_sample_records_cpu_io_and_tick_counters(self) -> None:
        reads: dict[Path, int] = {}
        original_read = Path.read_text

        def read_text(path: Path) -> str:
            reads[path] = reads.get(path, 0) + 1
            return original_read(path)

        with patch.object(Path, "read_text", read_text):
            record = self.power_profile_sample(self.root / "missing.ini")
        for filename in ("cpu_pressure", "io_pressure", "stat"):
            self.assertEqual(reads[self.root / "proc" / filename], 1)
        self.assertEqual(
            [record[name] for name in ("cpu_stall_some_us", "cpu_stall_full_us", "io_stall_some_us", "io_stall_full_us",
                                       "cpu_idle_ticks", "cpu_iowait_ticks", "cpu_total_ticks")],
            [12_000_000, 3_000_000, 4_000_000, 1_000_000, 400, 50, 621],
        )

    def test_cpu_pressure_without_full_keeps_some_and_other_readings(self) -> None:
        _ = (self.root / "proc/cpu_pressure").write_text("some total=1234567\n")
        record = self.power_profile_sample(self.root / "missing.ini")
        self.assertEqual(record["cpu_stall_some_us"], 1_234_567)
        self.assertIsNone(record["cpu_stall_full_us"])
        self.assertEqual(record["io_stall_full_us"], 1_000_000)
        self.assertEqual(record["cpu_total_ticks"], 621)

    def test_missing_optional_reading_only_nulls_its_own_counters(self) -> None:
        groups = {
            "cpu_pressure": ("cpu_stall_some_us", "cpu_stall_full_us"),
            "io_pressure": ("io_stall_some_us", "io_stall_full_us"),
            "stat": ("cpu_idle_ticks", "cpu_iowait_ticks", "cpu_total_ticks"),
        }
        for filename, fields in groups.items():
            with self.subTest(reading=filename):
                path = self.root / "proc" / filename
                original = path.read_text()
                path.unlink()
                try:
                    record = self.power_profile_sample(self.root / "missing.ini")
                    for field in fields:
                        self.assertIsNone(record[field])
                    for other_file, other_fields in groups.items():
                        if other_file != filename:
                            for field in other_fields:
                                self.assertIsInstance(record[field], int)
                    self.assertEqual(record["stall_some_us"], 100)
                    self.assertEqual(record["mem_used_bytes"], 12 * 1024)
                finally:
                    for log in self.root.glob("*/samples-*.jsonl"):
                        log.unlink()
                    _ = path.write_text(original)

    def test_malformed_optional_readings_still_write_memory_sample(self) -> None:
        _ = (self.root / "proc/stat").write_text("cpu 1 2 bad 4 5 6 7 8\n")
        _ = (self.root / "proc/io_pressure").write_text("some total=bad\nfull total=bad\n")
        record = self.power_profile_sample(self.root / "missing.ini")
        for field in ("cpu_idle_ticks", "cpu_iowait_ticks", "cpu_total_ticks", "io_stall_some_us", "io_stall_full_us"):
            self.assertIsNone(record[field])
        self.assertEqual(record["cpu_stall_some_us"], 12_000_000)
        self.assertEqual(record["stall_full_us"], 20)

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
        self.assertIsInstance(stalls, sample.StallTotals)
        self.assertEqual(stalls.some_us, 1_234_567)
        self.assertEqual(stalls.full_us, 456_789)

    def test_pressure_parser_can_leave_missing_scope_counters_unmeasured(self) -> None:
        pressure = "some avg10=0.00 total=1250000\nfull avg10=0.00 total=invalid\n"
        stalls = sample.parse_pressure(pressure, require_both=False)
        self.assertIsInstance(stalls, sample.PartialStallTotals)
        self.assertEqual(stalls.some_us, 1_250_000)
        self.assertIs(stalls.full_us, sample.Unmeasured.VALUE)
        with self.assertRaises(ValueError):
            _ = sample.parse_pressure(pressure)

    def test_boot_id_fixture_trims_newline_and_rejects_empty_text(self) -> None:
        boot_id = "d963a34a-50fa-48a8-8485-8b219c45fe02"
        self.assertEqual(sample.parse_boot_id(f"{boot_id}\n"), boot_id)
        with self.assertRaises(ValueError):
            _ = sample.parse_boot_id(" \n")

    def test_power_profile_state_reads_name_and_mtime_once(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / "state.ini"
        since = 1_792_031_396.125
        for name in ("performance", "balanced", "power-saver"):
            with self.subTest(profile=name):
                _ = state.write_text(f"[Other]\nProfile=ignored\n[State]\nProfile={name}\nCpuDriver=amd_pstate\n")
                os.utime(state, (since, since))
                with state.open() as handle:
                    descriptor = handle.fileno()
                    with patch.object(Path, "open", return_value=handle) as open_file, \
                         patch.object(os, "fstat", wraps=os.fstat) as stat_file, \
                         patch.object(handle, "read", wraps=handle.read) as read_file:
                        profile = sample.read_power_profile(state)
                self.assertEqual(profile, sample.PowerProfile(name, since))
                open_file.assert_called_once()
                stat_file.assert_called_once_with(descriptor)
                read_file.assert_called_once()

    def test_power_profile_path_replacement_keeps_open_file_profile_and_mtime(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / "state.ini"
        replacement = self.root / "replacement.ini"
        _ = state.write_text("[State]\nProfile=balanced\n")
        _ = replacement.write_text("[State]\nProfile=performance\n")
        since = 1_792_031_396.125
        replacement_since = since + 86400
        os.utime(state, (since, since))
        os.utime(replacement, (replacement_since, replacement_since))
        with state.open() as handle:
            descriptor = handle.fileno()
            # Replace the path after opening, before metadata or content is read.
            os.replace(replacement, state)
            with patch.object(Path, "open", return_value=handle) as open_file, \
                 patch.object(os, "fstat", wraps=os.fstat) as stat_file:
                profile = sample.read_power_profile(state)
        open_file.assert_called_once()
        stat_file.assert_called_once_with(descriptor)
        self.assertEqual(profile, sample.PowerProfile("balanced", since))
        self.assertEqual(state.read_text(), "[State]\nProfile=performance\n")
        self.assertEqual(os.stat(state).st_mtime, replacement_since)

    def test_power_profile_missing_file_is_unmeasured(self) -> None:
        self.assertIs(sample.read_power_profile(self.root / "missing.ini"), sample.Unmeasured.VALUE)

    def test_power_profile_without_state_or_value_is_unmeasured(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / "state.ini"
        for text in ("[Other]\nProfile=performance\n", "[State]\nCpuDriver=amd_pstate\n",
                     "[State]\nProfile=\n", "[State]\nProfile=   \n",
                     "[State]\nCpuDriver=amd_pstate\n[Other]\nProfile=performance\n"):
            with self.subTest(text=text):
                _ = state.write_text(text)
                self.assertIs(sample.read_power_profile(state), sample.Unmeasured.VALUE)

    def test_power_profile_unreadable_file_is_unmeasured(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / "state.ini"
        _ = state.write_text("[State]\nProfile=performance\n")
        with patch.object(Path, "open", side_effect=PermissionError("unreadable")):
            self.assertIs(sample.read_power_profile(state), sample.Unmeasured.VALUE)

    def power_profile_sample(self, state: Path) -> Record:
        proc = self.root / "proc"
        proc.mkdir(parents=True, exist_ok=True)
        _ = (proc / "meminfo").write_text("MemTotal: 16 kB\nMemAvailable: 4 kB\nSwapTotal: 8 kB\nSwapFree: 1 kB\n")
        _ = (proc / "pressure").write_text("some total=100\nfull total=20\n")
        _ = (proc / "boot_id").write_text("test-boot\n")
        with patch.dict(os.environ, {"BUILDLOG_DIR": str(self.root),
                                  "BUILDLOG_BUILDS_CGROUP": str(self.root / "absent-builds"),
                                  "BUILDLOG_CI_CGROUP": str(self.root / "absent-ci")}), \
             patch.object(sample, "MEMINFO", proc / "meminfo"), \
             patch.object(sample, "PRESSURE", proc / "pressure"), \
             patch.object(sample, "BOOT_ID", proc / "boot_id"), \
             patch.object(sample, "POWER_PROFILE_STATE", state), \
             patch.object(subprocess, "Popen", side_effect=AssertionError("sample must not fork")):
            _ = sample.sample()
        lines = next(self.root.glob("*/samples-*.jsonl")).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        return cast(Record, json.loads(lines[0]))

    def test_sample_writes_power_profile_and_utc_state_mtime(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.root / "state.ini"
        _ = state.write_text("[State]\nProfile=performance\n")
        since = 1_792_031_396.125
        os.utime(state, (since, since))
        record = self.power_profile_sample(state)
        self.assertEqual(record["kind"], "sample")
        self.assertEqual(record["power_profile"], "performance")
        self.assertEqual(record["power_profile_since"], store.utc_iso(since))

    def test_sample_without_power_profile_still_writes_null_fields(self) -> None:
        record = self.power_profile_sample(self.root / "missing.ini")
        self.assertEqual(record["kind"], "sample")
        self.assertIsNone(record["power_profile"])
        self.assertIsNone(record["power_profile_since"])

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
        proc.mkdir(parents=True, exist_ok=True)
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
            "sample.BOOT_ID = proc / 'boot_id'; "
            "from unittest.mock import patch; from contextlib import ExitStack; "
            "stack = ExitStack(); "
            "stack.enter_context(patch.object(sample, 'CPU_PRESSURE', proc / 'cpu_pressure')); "
            "stack.enter_context(patch.object(sample, 'IO_PRESSURE', proc / 'io_pressure')); "
            "stack.enter_context(patch.object(sample, 'STAT', proc / 'stat')); "
            "stack.enter_context(patch.object(sample, 'POWER_PROFILE_STATE', proc / 'absent-profile')); "
            "sys.exit(cli.main(['sample']))"
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
        proc.mkdir(parents=True, exist_ok=True)
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
