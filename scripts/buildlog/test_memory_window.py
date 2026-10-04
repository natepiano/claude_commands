#!/usr/bin/env python3
"""Snapshot and instant-window CLI contracts against fixtures, never host state."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

import index
import memory
import store
from test_index import Record, call, encode, point_root_at, sample, step

CLI = Path(__file__).with_name("cli.py")
GIB = 2**30
START = "2026-10-04T23:30-07:00"
END = "2026-10-05T00:30-07:00"
START_UTC = "2026-10-05T06:30:00.000Z"
END_UTC = "2026-10-05T07:30:00.000Z"


def present_slice(high: int, peak: int, *, max_events: int = 0, stall_some_us: int = 0) -> Record:
    return {
        "state": "present",
        "events": {"high": high, "max": max_events, "oom_kill": 0},
        "peak_bytes": peak,
        "swap_peak_bytes": GIB,
        "high": 26 * GIB,
        "max": 34 * GIB,
        "swap_max": 4 * GIB,
        "stall_some_us": stall_some_us,
    }


def snapshot(at: str, high: int, peak: int, *, ci: Record | None = None) -> Record:
    return {
        "kind": "memory_snapshot",
        "at": at,
        "host": "natedev",
        "slices": {"builds": present_slice(high, peak), "ci": ci or {"state": "absent"}},
        "zram": {"state": "present", "data_bytes": 3 * GIB, "compressed_bytes": GIB},
    }


class MemoryWindowTests(unittest.TestCase):
    base: Path = Path()
    root: Path = Path()
    fake_bin: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "buildlog"
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        point_root_at(self, self.root)

    def env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "BUILDLOG_DIR": str(self.root),
            "TZ": "America/New_York",
            "PATH": f"{self.fake_bin}:{os.environ.get('PATH', '')}",
            "BUILDLOG_BUILDS_CGROUP": str(self.base / "builds.slice"),
            "BUILDLOG_CI_CGROUP": str(self.base / "ci.slice"),
            "BUILDLOG_ZRAM": str(self.base / "zram0"),
            **extra,
        }

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=self.base,
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def stub_journal(self) -> None:
        early = {
            "__REALTIME_TIMESTAMP": str(int(datetime.fromisoformat("2026-10-05T06:59:00+00:00").timestamp() * 1_000_000)),
            "MESSAGE": 'sending SIGTERM to process 123 uid 1000 "rustc": memory low',
        }
        kernel_builds = {
            "__REALTIME_TIMESTAMP": str(int(datetime.fromisoformat("2026-10-05T07:01:00+00:00").timestamp() * 1_000_000)),
            "MESSAGE": "oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/user.slice/builds.slice,task_memcg=/x,task=rustc,pid=234,uid=1000",
        }
        kernel_ci = {
            "__REALTIME_TIMESTAMP": str(int(datetime.fromisoformat("2026-10-05T07:02:00+00:00").timestamp() * 1_000_000)),
            "MESSAGE": "oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/hana.slice/hana-ci.slice,task_memcg=/x,task=rustc,pid=345,uid=1001",
        }
        data = self.base / "journal.json"
        _ = data.write_text(json.dumps({"early": [early], "kernel": [kernel_builds, kernel_ci]}))
        journal = self.fake_bin / "journalctl"
        script = "\n".join([
            "#!/usr/bin/env python3",
            "import json, sys",
            f"with open({str(self.base / 'journal-args.jsonl')!r}, 'a', encoding='utf-8') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')",
            f"rows = json.load(open({str(data)!r}, encoding='utf-8'))",
            "kind = 'kernel' if '-k' in sys.argv else 'early'",
            "for row in rows[kind]: print(json.dumps(row))",
        ])
        _ = journal.write_text(script + "\n")
        journal.chmod(0o755)

    def test_snapshot_cli_prints_and_appends_exactly_one_record_from_fake_cgroups(self) -> None:
        builds = self.base / "builds.slice"
        builds.mkdir()
        _ = (builds / "memory.events").write_text("low 0\nhigh 2\nmax 3\noom 0\noom_kill 1\n")
        for name, value in (
            ("memory.pressure", "some avg10=0.00 total=1250000\nfull avg10=0.00 total=500000"),
            ("memory.peak", "8589934592"),
            ("memory.swap.peak", "1073741824"),
            ("memory.high", "27917287424"),
            ("memory.max", "max"),
            ("memory.swap.max", "4294967296"),
        ):
            _ = (builds / name).write_text(value + "\n")
        zram = self.base / "zram0"
        zram.mkdir()
        _ = (zram / "mm_stat").write_text("3221225472 1073741824 0 0 0\n")

        result = self.cli("snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        recorded = cast(Record, json.loads(lines[0]))
        self.assertEqual(recorded["kind"], "memory_snapshot")
        self.assertEqual(recorded["host"], socket.gethostname().split(".")[0])
        self.assertTrue(str(recorded["at"]).endswith("Z"))
        slices = cast(dict[str, Record], recorded["slices"])
        self.assertEqual(slices["builds"], {
            "state": "present", "events": {"high": 2, "max": 3, "oom_kill": 1},
            "peak_bytes": 8 * GIB, "swap_peak_bytes": GIB,
            "high": 26 * GIB, "max": "max", "swap_max": 4 * GIB,
            "stall_some_us": 1_250_000,
        })
        self.assertEqual(slices["ci"], {"state": "absent"})
        self.assertEqual(recorded["zram"], {"state": "present", "data_bytes": 3 * GIB, "compressed_bytes": GIB})
        files = list(self.root.glob("*/*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual([json.loads(line) for line in files[0].read_text().splitlines()], [recorded])
        self.assertFalse((self.root / "index.sqlite").exists())

    def test_missing_or_unreadable_slice_pressure_has_named_unavailable_state(self) -> None:
        builds = self.base / "builds.slice"
        builds.mkdir()
        for name, value in (
            ("memory.events", "high 0\nmax 0\noom_kill 0\n"),
            ("memory.peak", "0"),
            ("memory.swap.peak", "0"),
            ("memory.high", "max"),
            ("memory.max", "max"),
            ("memory.swap.max", "max"),
        ):
            _ = (builds / name).write_text(value)
        state = memory.slice_state(builds)
        self.assertEqual(state["state"], "present")
        if state["state"] == "present":
            self.assertEqual(state["stall_some_us"], "unavailable")
        (builds / "memory.pressure").mkdir()
        state = memory.slice_state(builds)
        self.assertEqual(state["state"], "present")
        if state["state"] == "present":
            self.assertEqual(state["stall_some_us"], "unavailable")

    def test_ci_window_reports_max_hits_oom_kills_and_stall_seconds(self) -> None:
        self.stub_journal()
        first_ci = present_slice(0, 6 * GIB, max_events=2, stall_some_us=1_500_000)
        last_ci = present_slice(0, 7 * GIB, max_events=5, stall_some_us=4_750_000)
        cast(dict[str, int], last_ci["events"])["oom_kill"] = 1
        self.write(self.root / "natedev" / "2026-10.jsonl",
                   snapshot(START_UTC, 0, GIB, ci=first_ci),
                   snapshot(END_UTC, 0, GIB, ci=last_ci))
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CI: high +0, max +3, oom_kill +1; stall +3.2 s; peak 7.0", result.stdout)

    def test_old_snapshot_reports_stall_unavailable(self) -> None:
        self.stub_journal()
        first = snapshot(START_UTC, 0, GIB, ci=present_slice(0, GIB))
        _ = cast(dict[str, Record], first["slices"])["ci"].pop("stall_some_us")
        last = snapshot(END_UTC, 0, GIB, ci=present_slice(0, GIB, stall_some_us=2_000_000))
        self.write(self.root / "natedev" / "2026-10.jsonl", first, last)
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CI: high +0, max +0, oom_kill +0; stall unavailable", result.stdout)

    def test_peak_that_rose_in_the_window_is_the_window_peak(self) -> None:
        self.stub_journal()
        first = snapshot(START_UTC, 0, 8 * GIB)
        last = snapshot(END_UTC, 0, 10 * GIB)
        self.write(self.root / "natedev" / "2026-10.jsonl", first, last)
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("peak 10.0 GiB, limit 34.0 GiB; swap peak at most 1.0 GiB (no new high in the window)", result.stdout)

    def test_reset_reports_stall_total_since_reset(self) -> None:
        self.stub_journal()
        first = snapshot(START_UTC, 5, 8 * GIB, ci=present_slice(5, 8 * GIB, stall_some_us=8_000_000))
        last = snapshot(END_UTC, 2, 4 * GIB, ci=present_slice(2, 4 * GIB, stall_some_us=1_750_000))
        self.write(self.root / "natedev" / "2026-10.jsonl", first, last)
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CI: reset in the window; since the reset high 2, max 0, oom_kill 0; stall 1.8 s", result.stdout)

    def test_stall_counter_reset_never_reports_negative_stall(self) -> None:
        self.stub_journal()
        first = snapshot(START_UTC, 0, GIB, ci=present_slice(0, GIB, stall_some_us=8_000_000))
        last = snapshot(END_UTC, 0, GIB, ci=present_slice(0, GIB, stall_some_us=1_500_000))
        self.write(self.root / "natedev" / "2026-10.jsonl", first, last)
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CI: reset in the window; since the reset high 0, max 0, oom_kill 0; stall 1.5 s", result.stdout)

    def test_window_crosses_pdt_midnight_and_uses_snapshot_deltas_and_journals(self) -> None:
        self.stub_journal()
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            snapshot(START_UTC, 5, 8 * GIB),
            snapshot(END_UTC, 8, 8 * GIB),
            step("before", started_at="2026-10-05T06:59:00.000Z", mem_wait_s=900, slice="fallback"),
            step("after", started_at="2026-10-05T07:01:00.000Z", mem_wait_s=30, slice="builds"),
            call("once", started_at="2026-10-05T07:01:00.000Z", mem_kills=1, mem_kill_stopped=False),
            call("twice", started_at="2026-10-05T07:02:00.000Z", mem_kills=2, mem_kill_stopped=True),
        )
        self.write(
            self.root / "natedev" / "samples-2026-10.jsonl",
            sample("2026-10-05T06:59:00.000Z", sccache_in_service=0),
            sample("2026-10-05T07:00:00.000Z", sccache_in_service=0),
            sample("2026-10-05T07:01:00.000Z", sccache_in_service=1),
        )
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        self.assertIn("Memory, 2026-10-04 23:30 PDT to 2026-10-05 00:30 PDT", output)
        self.assertIn("earlyoom kills: 1", output)
        self.assertIn("23:59 PDT rustc pid 123", output)
        self.assertIn("kernel OOM kills: 2 (builds 1, CI 1, elsewhere 0)", output)
        self.assertIn("builds: high +3, max +0, oom_kill +0; stall +0.0 s; "
                      + "peak at most 8.0 GiB (no new high in the window), limit 34.0 GiB; "
                      + "swap peak at most 1.0 GiB (no new high in the window), limit 4.0 GiB", output)
        self.assertIn("CI: slice absent", output)
        self.assertIn("memory waits: 2 steps, total 15.5 min, longest 15.0 min", output)
        self.assertIn("1 reached the 15-min limit", output)
        self.assertIn("unsliced steps: 1 fell back to a plain run", output)
        self.assertIn("memory kills: 1 calls passed after a re-run, 1 stopped after a step was killed twice", output)
        self.assertIn("sccache outside its service: 2 min — 23:59–00:00 PDT", output)
        self.assertIn("zram: 3.0 GiB stored in 1.0 GiB (3.0:1)", output)
        invocations = [cast(list[str], json.loads(line)) for line in (self.base / "journal-args.jsonl").read_text().splitlines()]
        self.assertEqual(len(invocations), 2)
        self.assertEqual(invocations[0][:2], ["-u", "earlyoom"])
        self.assertEqual(invocations[1][0], "-k")
        for invocation in invocations:
            self.assertIn("--since", invocation)
            self.assertIn("--until", invocation)
            self.assertIn("-o", invocation)
            self.assertIn("json", invocation)
            self.assertIn("--no-pager", invocation)
            self.assertTrue(invocation[invocation.index("--since") + 1].startswith("@"))
            self.assertTrue(invocation[invocation.index("--until") + 1].startswith("@"))

    def test_missing_snapshot_is_named_and_offset_is_required(self) -> None:
        self.stub_journal()
        self.write(self.root / "natedev" / "2026-10.jsonl", snapshot(START_UTC, 1, GIB))
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("builds: no snapshot within 2 min of 00:30 PDT", result.stdout)
        for first, last in (("2026-10-04T23:30", END), (START, "2026-10-05T00:30"), (END, START), ("bad", END)):
            with self.subTest(first=first, last=last):
                invalid = self.cli("memory", first, last)
                self.assertEqual(invalid.returncode, 2)
                self.assertIn("usage: buildlog memory", invalid.stderr)

    def test_two_single_step_kills_can_pass_but_twice_killed_step_stops_call(self) -> None:
        self.stub_journal()
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("two-steps", started_at="2026-10-05T07:01:00.000Z", mem_kills=2, mem_kill_stopped=False),
            call("one-step", started_at="2026-10-05T07:02:00.000Z", mem_kills=2, mem_kill_stopped=True),
        )
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory kills: 1 calls passed after a re-run, 1 stopped after a step was killed twice", result.stdout)

    def test_memory_window_counts_only_current_host_records(self) -> None:
        self.stub_journal()
        host = store.host_name()
        other = "other-host" if host != "other-host" else "another-host"
        at = "2026-10-05T07:00:00.000Z"
        self.write(
            self.root / host / "2026-10.jsonl",
            step("local", host=host, started_at=at, mem_wait_s=10, slice="fallback"),
            call("local", host=host, started_at=at, mem_kills=1),
        )
        self.write(
            self.root / other / "2026-10.jsonl",
            step("foreign", host=other, started_at=at, mem_wait_s=30, slice="none"),
            call("foreign", host=other, started_at=at, mem_kills=1, outcome="failed", status=1),
        )
        self.write(self.root / host / "samples-2026-10.jsonl", sample(at, host=host, sccache_in_service=1))
        self.write(self.root / other / "samples-2026-10.jsonl", sample(at, host=other, sccache_in_service=0))

        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory waits: 1 steps, total 10.0 s, longest 10.0 s", result.stdout)
        self.assertIn("sccache in its service: every sampled minute", result.stdout)
        self.assertIn("unsliced steps: 1 fell back to a plain run", result.stdout)
        self.assertIn("memory kills: 1 calls passed after a re-run", result.stdout)
        self.assertNotIn("never tried a scope", result.stdout)
        self.assertNotIn("failed later", result.stdout)

    def test_unsliced_steps_name_fallback_and_never_tried_reasons_separately_and_together(self) -> None:
        self.stub_journal()
        at = "2026-10-05T07:00:00.000Z"
        for slices, expected in (
            (("fallback",), "unsliced steps: 1 fell back to a plain run"),
            (("none",), "unsliced steps: 1 never tried a scope"),
            (("fallback", "none"), "unsliced steps: 1 fell back to a plain run, 1 never tried a scope"),
        ):
            with self.subTest(slices=slices):
                self.write(self.root / "natedev" / "2026-10.jsonl", *(
                    step(f"step-{number}", started_at=at, slice=slice_name)
                    for number, slice_name in enumerate(slices)
                ))
                result = self.cli("memory", START, END)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)

    def test_memory_kills_separate_passed_failed_and_stopped_calls(self) -> None:
        self.stub_journal()
        at = "2026-10-05T07:00:00.000Z"
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            call("passed", started_at=at, mem_kills=1, outcome="ran", status=0),
            call("failed", started_at=at, mem_kills=1, outcome="failed", status=1),
            call("interrupted", started_at=at, mem_kills=1, outcome="interrupted", status=None),
            call("stopped", started_at=at, mem_kills=2, mem_kill_stopped=True, outcome="failed", status=137),
        )
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "memory kills: 1 calls passed after a re-run, 2 failed later, 1 stopped after a step was killed twice",
            result.stdout,
        )

    def test_failed_journal_reads_are_unavailable_and_empty_reads_are_zero(self) -> None:
        journal = self.fake_bin / "journalctl"
        _ = journal.write_text("#!/bin/sh\nexit 1\n")
        journal.chmod(0o755)
        failed = self.cli("memory", START, END)
        self.assertEqual(failed.returncode, 0, failed.stderr)
        self.assertIn("earlyoom kills: journal unavailable", failed.stdout)
        self.assertIn("kernel OOM kills: journal unavailable", failed.stdout)
        _ = journal.write_text("#!/bin/sh\nexit 0\n")
        empty = self.cli("memory", START, END)
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("earlyoom kills: 0", empty.stdout)
        self.assertIn("kernel OOM kills: 0", empty.stdout)

    def test_missing_journal_command_is_unavailable(self) -> None:
        with patch("memory.subprocess.run", side_effect=FileNotFoundError):
            result = memory.journal_messages([], datetime.fromisoformat(START), datetime.fromisoformat(END))
        self.assertIs(result, memory.JournalUnavailable.VALUE)

    def test_submillisecond_window_includes_only_stored_instants_inside_edges(self) -> None:
        self.stub_journal()
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            step("before-start", started_at="2026-10-05T07:00:00.000Z", mem_wait_s=10),
            step("after-start", started_at="2026-10-05T07:00:00.001Z", mem_wait_s=20),
            step("before-end", started_at="2026-10-05T07:00:00.002Z", mem_wait_s=30),
            step("after-end", started_at="2026-10-05T07:00:00.003Z", mem_wait_s=40),
        )
        result = self.cli("memory", "2026-10-05T07:00:00.000500+00:00", "2026-10-05T07:00:00.002500+00:00")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("memory waits: 2 steps, total 50.0 s", result.stdout)

    def test_submillisecond_snapshot_search_excludes_instant_before_left_edge(self) -> None:
        instant = datetime.fromisoformat("2026-10-05T07:00:00.000500+00:00")
        host = store.host_name()
        before = snapshot("2026-10-05T06:58:00.000Z", 1, GIB)
        inside = snapshot("2026-10-05T06:58:00.001Z", 2, GIB)
        before["host"] = host
        inside["host"] = host
        self.write(self.root / host / "2026-10.jsonl", before, inside)
        _ = index.update()
        connection = index.read_only()
        try:
            found = memory.nearest_snapshot(connection, instant)
        finally:
            connection.close()
        self.assertEqual(found["kind"], "memory_snapshot")
        if found["kind"] == "memory_snapshot":
            self.assertEqual(found["at"], "2026-10-05T06:58:00.001Z")

    def test_counter_or_peak_reset_reports_end_snapshot_as_lower_bound(self) -> None:
        self.stub_journal()
        self.write(
            self.root / "natedev" / "2026-10.jsonl",
            snapshot(START_UTC, 5, 8 * GIB, ci=present_slice(3, 6 * GIB, max_events=4)),
            snapshot(END_UTC, 2, 4 * GIB, ci=present_slice(1, 6 * GIB, max_events=1)),
        )
        result = self.cli("memory", START, END)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("builds: reset in the window; since the reset high 2, max 0, oom_kill 0; stall 0.0 s; peak 4.0", result.stdout)
        self.assertIn("CI: reset in the window; since the reset high 1, max 1, oom_kill 0; stall 0.0 s; peak 6.0", result.stdout)
        self.assertNotIn("no new high in the window", result.stdout)


if __name__ == "__main__":
    _ = unittest.main()
