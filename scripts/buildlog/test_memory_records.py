#!/usr/bin/env python3
"""Memory record and daily report contracts, using only temporary cgroup trees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override

from test_index import Record, call, encode, point_root_at, sample, step

HERE = Path(__file__).parent
CLI = HERE / "cli.py"
RECORD = HERE / "record.py"
AT = "2026-10-04T21:00:00.000Z"


class MemoryRecordTests(unittest.TestCase):
    base: Path = Path()
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "buildlog"
        point_root_at(self, self.root)

    def env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "BUILDLOG_DIR": str(self.root), "TZ": "America/New_York", **extra}

    def run_script(self, script: Path, *args: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=self.base,
            env=self.env(**(extra or {})),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def records(self) -> list[Record]:
        return [
            cast(Record, json.loads(line))
            for path in self.root.glob("*/*.jsonl")
            for line in path.read_text().splitlines()
        ]

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def test_sample_distinguishes_service_process_empty_and_absent_slice(self) -> None:
        proc = self.base / "proc"
        proc.mkdir()
        _ = (proc / "meminfo").write_text(
            "MemTotal: 16384 kB\nMemAvailable: 4096 kB\nSwapTotal: 8192 kB\nSwapFree: 1024 kB\n"
        )
        _ = (proc / "pressure").write_text("some total=100\nfull total=20\n")
        _ = (proc / "boot_id").write_text("d963a34a-50fa-48a8-8485-8b219c45fe02\n")
        builds = self.base / "builds.slice"
        service = builds / "sccache.service"
        service.mkdir(parents=True)
        _ = (service / "cgroup.procs").write_text("123\n")
        command = (
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "import sample, cli; proc = Path(sys.argv[2]); "
            "sample.MEMINFO = proc / 'meminfo'; sample.PRESSURE = proc / 'pressure'; "
            "sample.BOOT_ID = proc / 'boot_id'; sys.exit(cli.main(['sample']))"
        )
        env = self.env(BUILDLOG_BUILDS_CGROUP=str(builds), BUILDLOG_CI_CGROUP=str(self.base / "absent-ci"),
                       BUILDLOG_ZRAM=str(self.base / "absent-zram"))
        def take_sample() -> None:
            result = subprocess.run(
                [sys.executable, "-c", command, str(HERE), str(proc)],
                env=env, capture_output=True, text=True, check=False, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        take_sample()
        _ = (service / "cgroup.procs").write_text("")
        take_sample()
        (service / "cgroup.procs").unlink()
        take_sample()
        _ = builds.rename(self.base / "moved.slice")
        take_sample()
        self.assertEqual([row["sccache_in_service"] for row in self.records() if row["kind"] == "sample"], [1, 0, 0, None])

    def test_step_slice_and_distinct_call_kill_outcomes_reach_index(self) -> None:
        marker = self.base / "scope.marker"
        _ = marker.write_text("ran\n")
        for peak in (str(marker), str(self.base / "missing.marker"), ""):
            result = self.run_script(
                RECORD, "step", "0", "1791147600", "1791147601", "0", "", peak, "cargo", "clippy",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        for count, stopped in (("1", "0"), ("2", "1")):
            result = self.run_script(
                RECORD, "call", "ran", "0", "0", "0", "1", "", "0", "1", "test", "hana",
                extra={"BUILDLOG_MEM_KILLS": count, "BUILDLOG_MEM_KILL_STOPPED": stopped},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_script(RECORD, "call", "ran", "0", "0", "0", "1", "", "0", "1", "test", "hana")
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.records()
        self.assertEqual([row["slice"] for row in records if row["kind"] == "step"], ["builds", "fallback", "none"])
        self.assertEqual([row["mem_kills"] for row in records if row["kind"] == "call"], [1, 2, 0])
        self.assertEqual([row["mem_kill_stopped"] for row in records if row["kind"] == "call"], [False, True, False])
        query = self.run_script(CLI, "query", "--json", "SELECT slice FROM steps ORDER BY rowid")
        self.assertEqual(query.returncode, 0, query.stderr)
        self.assertEqual([row["slice"] for row in cast(list[dict[str, object]], json.loads(query.stdout))], ["builds", "fallback", "none"])
        calls = self.run_script(CLI, "query", "--json", "SELECT mem_kills, mem_kill_stopped FROM calls ORDER BY rowid")
        self.assertEqual(calls.returncode, 0, calls.stderr)
        self.assertEqual(cast(list[dict[str, object]], json.loads(calls.stdout)), [
            {"mem_kills": 1, "mem_kill_stopped": 0},
            {"mem_kills": 2, "mem_kill_stopped": 1},
            {"mem_kills": 0, "mem_kill_stopped": 0},
        ])

    def test_daily_report_names_both_unsliced_reasons_and_groups_consecutive_zero_samples(self) -> None:
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-04T21:00:00.000Z", sccache_in_service=0),
            sample("2026-10-04T21:01:00.000Z", sccache_in_service=0),
            sample("2026-10-04T21:03:00.000Z", sccache_in_service=1),
            sample("2026-10-04T21:04:00.000Z", sccache_in_service=0),
        )
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("fallback", started_at=AT, slice="fallback"),
            step("none", started_at=AT, slice="none"),
            step("builds", started_at=AT, slice="builds"),
            call("once", started_at=AT, mem_kills=1, mem_kill_stopped=False),
            call("twice", started_at=AT, mem_kills=2, mem_kill_stopped=True),
        )
        result = self.run_script(CLI, "report", "2026-10-04")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sccache outside its service: 3 min — 14:00–14:01 PDT, 14:04 PDT", result.stdout)
        self.assertIn("unsliced steps: 1 fell back to a plain run, 1 never tried a scope", result.stdout)
        self.assertIn("memory kills: 1 calls passed after a re-run, 1 stopped after a step was killed twice", result.stdout)

    def test_daily_report_marks_unavailable_service_and_no_events(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", sample(AT, sccache_in_service=None))
        result = self.run_script(CLI, "report", "2026-10-04")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sccache service: unavailable", result.stdout)
        self.assertIn("unsliced steps: none", result.stdout)
        self.assertIn("memory kills: none", result.stdout)

    def test_daily_report_marks_every_sampled_minute_in_service(self) -> None:
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", sample(AT, sccache_in_service=1))
        result = self.run_script(CLI, "report", "2026-10-04")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sccache in its service: every sampled minute", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
