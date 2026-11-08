#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Snapshot and instant-window CLI contracts against fixtures, never host state."""

from __future__ import annotations



import contextlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lint.memory_test_support import config_environment

from scripts.buildlog import index, memory, store
from scripts.lib import scratch
from scripts.lint import sweep
from scripts.buildlog.cgroup_test_support import CPU, GIB, IO_LINES, ZRAM_READING, slice_reading, write_cgroup, write_zram
from scripts.buildlog.test_index import Record, call, encode, point_root_at, sample, step
from scripts.lib import test_hang_guard
from scripts.buildlog import record

CLI = Path(__file__).with_name("cli.py")
START = "2026-10-04T23:30-07:00"
END = "2026-10-05T00:30-07:00"
START_UTC = "2026-10-05T06:30:00.000Z"
END_UTC = "2026-10-05T07:30:00.000Z"



def configured_local_host() -> str:
    """Read the local record host from the fixture's target configuration."""
    return record.local_step_identity().hosts[0]


def configured_host_cli(script: Path, *arguments: str) -> list[str]:
    """Use the configured record host in the child process."""
    return [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c",
            "import runpy, sys; "
            + "script = sys.argv.pop(1); sys.argv[0] = script; "
            + "from scripts.buildlog import record, store; store.host_name = lambda: record.local_step_identity().hosts[0]; "
            + "runpy.run_path(script, run_name='__main__')", str(script), *arguments]

def write_lint_config(root: Path, adjustment: int | str, *, include_adjustment: bool = True) -> Path:
    path = root / "lint.conf"
    _ = path.write_text(f"# whole: -1000..1000\nbuild_oom_score_adj={adjustment}\n" if include_adjustment else "clippy=on\n")
    return path


def cargo_output(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _ = (path / sweep.RUSTC_INFO).write_text("{}")
    return path


def journal_entry(at: datetime, message: str) -> Record:
    return {"__REALTIME_TIMESTAMP": str(int(at.timestamp() * 1_000_000)), "MESSAGE": message}


def earlyoom_message(pid: int, *, command: str = "rustc", adj: int | None = 500,
                     cmdline: str | None = None, signal: str = "SIGTERM") -> str:
    message = f'sending {signal} to process {pid} uid 1000 "{command}": oom_score 1053'
    if adj is not None:
        message += f", oom_score_adj {adj}"
    if cmdline is not None:
        message += f', cmdline "{cmdline}"'
    return message


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
        "zram": ZRAM_READING,
    }


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    fixture_root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory()))
    unittest.enterModuleContext(patch.dict(os.environ, config_environment(fixture_root)))
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


def isolated_journal(test: unittest.TestCase) -> MemoryWindowTests:
    fixture = MemoryWindowTests(methodName="runTest")
    test.addCleanup(fixture.doCleanups)
    fixture.setUp()
    fixture.stub_journal(early=[], kernel=[])
    _ = cast(object, test.enterContext(patch.dict(os.environ, {"PATH": fixture.env()["PATH"]})))
    return fixture


class MemoryWindowTests(unittest.TestCase):
    base: Path = Path()
    root: Path = Path()
    fake_bin: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        _ = self.enterContext(patch.object(store, "host_name", side_effect=configured_local_host))
        self.enterContext(patch.dict(os.environ, config_environment(self.base)))
        self.root = self.base / "buildlog"
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        point_root_at(self, self.root)

    def test_window_report_uses_the_recorded_cap_and_room_formatter(self) -> None:
        from scripts.build_queue import queue_snapshot
        from scripts.build_queue.queue_evidence import room_text
        from scripts.lint import memory_admit as gate

        self.stub_journal(early=[], kernel=[])
        start, end = datetime.fromisoformat(START_UTC), datetime.fromisoformat(END_UTC)
        configuration = gate.read_memory_config()
        inputs = gate.QueueInputs([], (), (), gate.HostMemory(30 * GIB, 60 * GIB),
            gate.NoPressureHold(), gate.ReadyToStart(), gate.NoHeadAdmissionOverride(), start.timestamp())
        directory = self.root / "admission"
        directory.mkdir(parents=True)
        queue_snapshot.write_queue_snapshot(directory, inputs, configuration.policy)
        _ = index.update()
        with index.read_only() as connection:
            text = memory.window_report(connection, start, end)
        room = gate.memory_room(inputs.host, (), configuration.policy)
        self.assertIn(room_text(room), text)
        self.assertIn("Cap 27.0 GiB", text)
        with index.read_only() as connection:
            older = memory.window_report(connection, start - timedelta(days=2), start - timedelta(days=1))
        self.assertNotIn("Cap 27.0 GiB", older)
        self.assertIn("Cap: no recorded admission pass in this window", older)

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
            configured_host_cli(CLI, *args),
            cwd=self.base,
            env=self.env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
        )

    def write(self, path: Path, *records: Record) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(b"".join(encode(record) for record in records))

    def stub_journal(self, *, early: list[Record] | None = None,
                     kernel: list[Record] | None = None, exit_code: int = 0) -> None:
        default_early = journal_entry(datetime.fromisoformat("2026-10-05T06:59:00+00:00"),
                                      'sending SIGTERM to process 123 uid 1000 "rustc": memory low')
        kernel_builds = journal_entry(datetime.fromisoformat("2026-10-05T07:01:00+00:00"),
            "oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/user.slice/builds.slice,task_memcg=/x,task=rustc,pid=234,uid=1000")
        kernel_ci = journal_entry(datetime.fromisoformat("2026-10-05T07:02:00+00:00"),
            "oom-kill:constraint=CONSTRAINT_MEMCG,oom_memcg=/hana.slice/hana-ci.slice,task_memcg=/x,task=rustc,pid=345,uid=1001")
        data = self.base / "journal.json"
        _ = data.write_text(json.dumps({"early": [default_early] if early is None else early,
                                       "kernel": [kernel_builds, kernel_ci] if kernel is None else kernel}))
        journal = self.fake_bin / "journalctl"
        script = "\n".join([
            "#!/usr/bin/env python3",
            "import json, sys",
            f"with open({str(self.base / 'journal-args.jsonl')!r}, 'a', encoding='utf-8') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')",
            f"if {exit_code}: sys.exit({exit_code})",
            f"rows = json.load(open({str(data)!r}, encoding='utf-8'))",
            "kind = 'kernel' if '-k' in sys.argv else 'early'",
            "for row in rows[kind]: print(json.dumps(row))",
        ])
        _ = journal.write_text(script + "\n")
        journal.chmod(0o755)

    def earlyoom(self) -> list[memory.EarlyoomKill] | memory.JournalUnavailable:
        with patch.dict(os.environ, self.env()), patch.object(store, "host_name", return_value="natedev"):
            return memory.earlyoom_kills(datetime.fromisoformat(START_UTC), datetime.fromisoformat(END_UTC))

    def test_earlyoom_reader_classifies_gate_target_programs_and_other_kills(self) -> None:
        target = cargo_output(self.base / "cargo-output")
        at = datetime.fromisoformat(START_UTC)
        messages = [
            earlyoom_message(101, cmdline="/usr/bin/rustc --crate-name hana"),
            earlyoom_message(102, command="hana-test", signal="SIGKILL", adj=200,
                             cmdline=f"{target}/debug/deps/hana-abc --test"),
            earlyoom_message(103, command="python", adj=200,
                             cmdline="/usr/bin/python /work/target/debug/helper"),
            earlyoom_message(104, command="legacy", adj=None),
        ]
        self.stub_journal(early=[
            journal_entry(at - timedelta(seconds=1), messages[0]),
            journal_entry(at, "sending SIGTERM when mem avail <=  5.00% and swap free <= 100.00%,"),
            *(journal_entry(at + timedelta(minutes=number), message)
              for number, message in enumerate(messages)),
            journal_entry(datetime.fromisoformat(END_UTC), messages[0]),
        ], kernel=[])
        kills = self.earlyoom()
        if not isinstance(kills, list):
            self.fail("the fixture journal was unavailable")
        self.assertEqual([(kill.at, kill.pid, kill.command) for kill in kills], [
            (at, 101, "rustc"), (at + timedelta(minutes=1), 102, "hana-test"),
            (at + timedelta(minutes=2), 103, "python"), (at + timedelta(minutes=3), 104, "legacy"),
        ])
        self.assertEqual([kill.oom_score_adj for kill in kills],
                         [500, 200, 200, memory.OomScoreAdjUnavailable.VALUE])
        self.assertEqual([kill.is_build for kill in kills], [True, True, False, False])
        self.assertEqual([kill.outside_build_gate for kill in kills], [False, True, False, False])
        self.assertEqual(kills[1].cmdline, f"{target}/debug/deps/hana-abc --test")
        self.assertIs(kills[3].cmdline, memory.CmdlineUnavailable.VALUE)

    def test_earlyoom_unavailable_is_distinct_from_an_empty_journal_and_mac_never_reads_it(self) -> None:
        self.stub_journal(early=[], kernel=[], exit_code=1)
        self.assertIs(self.earlyoom(), memory.JournalUnavailable.VALUE)
        self.stub_journal(early=[], kernel=[])
        self.assertEqual(self.earlyoom(), [])
        before = (self.base / "journal-args.jsonl").read_bytes()
        with patch.dict(os.environ, self.env()), patch.object(store, "host_name", return_value="macbook"):
            result = memory.earlyoom_kills(datetime.fromisoformat(START_UTC), datetime.fromisoformat(END_UTC))
        self.assertIs(result, memory.JournalUnavailable.VALUE)
        self.assertEqual((self.base / "journal-args.jsonl").read_bytes(), before)

    def test_target_program_with_unquoted_apostrophe_arguments_is_a_build_kill(self) -> None:
        target = cargo_output(self.base / "cargo-output")
        at = datetime.fromisoformat(START_UTC)
        self.stub_journal(early=[journal_entry(at, earlyoom_message(
            105, adj=200, cmdline=f"{target}/debug/deps/test-abc --case O'Reilly"))], kernel=[])
        kills = self.earlyoom()
        if not isinstance(kills, list):
            self.fail("the fixture journal was unavailable")
        self.assertEqual(len(kills), 1)
        self.assertTrue(kills[0].is_build)
        self.assertTrue(kills[0].outside_build_gate)
        self.assertEqual(kills[0].cmdline, f"{target}/debug/deps/test-abc --case O'Reilly")

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
        write_zram(zram)

        result = self.cli("snapshot")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        recorded = cast(Record, json.loads(lines[0]))
        self.assertEqual(recorded["kind"], "memory_snapshot")
        self.assertEqual(recorded["host"], configured_local_host())
        self.assertTrue(str(recorded["at"]).endswith("Z"))
        slices = cast(dict[str, Record], recorded["slices"])
        self.assertEqual({key: slices["builds"][key] for key in (
            "state", "events", "peak_bytes", "swap_peak_bytes", "high", "max", "swap_max", "stall_some_us"
        )}, {
            "state": "present", "events": {"high": 2, "max": 3, "oom_kill": 1},
            "peak_bytes": 8 * GIB, "swap_peak_bytes": GIB,
            "high": 26 * GIB, "max": "max", "swap_max": 4 * GIB,
            "stall_some_us": 1_250_000,
        })
        self.assertEqual(slices["ci"], {"state": "absent"})
        self.assertEqual(recorded["zram"], ZRAM_READING)
        files = list(self.root.glob("*/*.jsonl"))
        self.assertEqual(len(files), 1)
        self.assertEqual([json.loads(line) for line in files[0].read_text().splitlines()], [recorded])
        self.assertFalse(index.index_path_in(self.root).exists())

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

    def test_usage_hours_cli_reads_only_isolated_index_and_prints_counted_memory(self) -> None:
        stamp = "2026-10-02T12:00:00Z"
        slices = {"builds": slice_reading(12 * GIB), "ci": {"state": "absent"}}
        self.write(self.root / "natedev/samples-2026-10.jsonl",
                   sample(stamp, slices=slices))
        result = self.cli("usage-hours", "2026-10-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Average GiB held", result.stdout)
        row = next(line for line in result.stdout.splitlines() if "2026-10-02 05:00 -0700" in line)
        self.assertIn("12.0 (1)", row)
        self.assertIn("3.0 (1)", row)
        self.assertTrue(index.index_path_in(self.root).is_file())

    def test_slice_capture_keeps_whole_charge_components_and_stacked_devices_separate(self) -> None:
        builds = self.base / "builds.slice"
        write_cgroup(builds)
        state = memory.slice_state(builds)
        self.assertEqual(state["state"], "present")
        if state["state"] != "present":
            self.fail("fixture slice was lost")
        self.assertEqual(state["current_bytes"], 12 * GIB)
        self.assertEqual(state["swap_current_bytes"], 2 * GIB)
        self.assertEqual(state["max_bytes"], 44 * GIB)
        self.assertEqual(state["stat"], {"anon": 4 * GIB, "file": 6 * GIB,
                         "kernel": 2 * GIB, "shmem": GIB, "inactive_file": 3 * GIB, "sock": 17,
                         "file_mapped": 19, "file_dirty": 23, "file_writeback": 29, "active_file": 31, "slab": 37})
        self.assertNotIn("zswap", state["stat"])
        self.assertEqual(state["cpu"], CPU)
        self.assertEqual(state["identity"], builds.stat().st_ino)
        traffic = state["io"]
        self.assertEqual(traffic["state"], "measured")
        if traffic["state"] != "measured":
            self.fail("fixture IO was lost")
        self.assertEqual([row["raw"] for row in traffic["devices"]], list(IO_LINES))
        self.assertEqual([row["device"] for row in traffic["devices"]], ["254:0", "259:0", "253:0"])

    def test_unreadable_slice_file_preserves_other_fields_and_ci(self) -> None:
        builds, ci = self.base / "builds.slice", self.base / "ci.slice"
        write_cgroup(builds, with_io=False)
        write_cgroup(ci)
        (builds / "memory.current").unlink()
        (builds / "memory.current").mkdir()
        with patch.dict(os.environ, self.env()):
            result = memory.snapshot()
        slices = result["slices"]
        head = slices["builds"]
        self.assertEqual(head["state"], "present")
        if head["state"] != "present":
            self.fail("one unavailable file discarded slice")
        self.assertEqual(head["current_bytes"], "unavailable")
        self.assertEqual(head["io"], {"state": "unavailable"})
        self.assertEqual(head["cpu"], CPU)
        self.assertEqual(head["stat"]["anon"], 4 * GIB)
        other = slices["ci"]
        self.assertEqual(other["state"], "present")
        if other["state"] == "present":
            self.assertEqual(other["current_bytes"], 12 * GIB)

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
        with patch("scripts.buildlog.memory.subprocess.run", side_effect=FileNotFoundError):
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

    def test_old_tick_and_standalone_snapshots_show_the_same_slice_memory(self) -> None:
        records: list[Record] = []
        expected_slices: object = {}
        for at, high, peak in ((START_UTC, 2, 8 * GIB), (END_UTC, 5, 12 * GIB)):
            standalone = snapshot(at, high, peak, ci=present_slice(high, peak))
            expected_slices = standalone["slices"]
            old_snapshot = {**standalone, "host": "old"}
            old_sample = sample(at, host="old", slices={"builds": {"state": "absent"}, "ci": {"state": "absent"}})
            tick_snapshot: Record = {"kind": "memory_snapshot", "host": "tick", "at": at, "zram": standalone["zram"]}
            tick_sample = sample(at, host="tick", slices=expected_slices)
            new_sample = sample(at, host="new-log", slices=expected_slices, zram=standalone["zram"])
            standalone_snapshot = {**standalone, "host": "standalone"}
            records.extend((old_sample, old_snapshot, tick_sample, tick_snapshot, new_sample, standalone_snapshot))
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", *records)
        _ = index.update()
        connection = index.read_only()
        reports: list[str] = []
        try:
            with patch.object(memory, "journal_messages", return_value=memory.JournalUnavailable.VALUE):
                for host in ("old", "tick", "new-log", "standalone"):
                    with self.subTest(host=host), patch.object(store, "host_name", return_value=host):
                        found = memory.nearest_snapshot(connection, datetime.fromisoformat(END_UTC))
                        self.assertEqual(found["kind"], "memory_snapshot")
                        if found["kind"] != "memory_snapshot":
                            self.fail("the snapshot is unavailable")
                        self.assertEqual(found["slices"], expected_slices)
                        self.assertEqual(found["zram"]["state"], "present")
                        reports.append(memory.window_report(connection, datetime.fromisoformat(START_UTC),
                                                            datetime.fromisoformat(END_UTC)))
        finally:
            connection.close()
        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0], reports[2])
        self.assertEqual(reports[0], reports[3])
        self.assertIn("builds: high +3, max +0, oom_kill +0", reports[0])
        self.assertIn("peak 12.0 GiB", reports[0])
        self.assertIn("CI: high +3", reports[0])
        self.assertIn("zram: 3.0 GiB stored in 1.0 GiB", reports[0])

    def test_tick_report_uses_sample_slices_with_different_timestamp_text(self) -> None:
        records: list[Record] = []
        expected_slices: object = {}
        for at, high, peak in ((START_UTC, 2, 8 * GIB), (END_UTC, 5, 12 * GIB)):
            reading = snapshot(at, high, peak, ci=present_slice(high, peak))
            expected_slices = reading["slices"]
            for host, sample_at in (("format", at.replace(".000Z", "Z")),
                                    ("second", at.replace(":00.000Z", ":01.000Z"))):
                records.extend((
                    sample(sample_at, host=host, slices=expected_slices),
                    {"kind": "memory_snapshot", "host": host, "at": at, "zram": reading["zram"]},
                ))
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", *records)
        _ = index.update()
        connection = index.read_only()
        try:
            with patch.object(memory, "journal_messages", return_value=memory.JournalUnavailable.VALUE):
                for host in ("format", "second"):
                    with self.subTest(host=host), patch.object(store, "host_name", return_value=host):
                        found = memory.nearest_snapshot(connection, datetime.fromisoformat(END_UTC))
                        self.assertEqual(found["kind"], "memory_snapshot")
                        if found["kind"] != "memory_snapshot":
                            self.fail("the sample slices are unavailable")
                        self.assertEqual(found["slices"], expected_slices)
                        report = memory.window_report(connection, datetime.fromisoformat(START_UTC),
                                                      datetime.fromisoformat(END_UTC))
                        self.assertIn("builds: high +3, max +0, oom_kill +0", report)
                        self.assertIn("peak 12.0 GiB", report)
                        self.assertIn("CI: high +3", report)
                        self.assertIn("zram: 3.0 GiB stored in 1.0 GiB", report)
        finally:
            connection.close()

    def test_zram_report_marks_absent_and_missing_readings_unavailable(self) -> None:
        records: list[Record] = []
        for at, high, peak in ((START_UTC, 2, 8 * GIB), (END_UTC, 5, 12 * GIB)):
            reading = snapshot(at, high, peak)
            records.extend((
                sample(at, host="absent", slices=reading["slices"], zram={"state": "absent"}),
                sample(at, host="missing", slices=reading["slices"]),
            ))
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", *records)
        _ = index.update()
        connection = index.read_only()
        try:
            with patch.object(memory, "journal_messages", return_value=memory.JournalUnavailable.VALUE):
                for host in ("absent", "missing"):
                    with self.subTest(host=host), patch.object(store, "host_name", return_value=host):
                        report = memory.window_report(connection, datetime.fromisoformat(START_UTC),
                                                      datetime.fromisoformat(END_UTC))
                        self.assertEqual([line for line in report.splitlines() if line.startswith("zram:")],
                                         ["zram: unavailable"])
        finally:
            connection.close()

    def test_zram_uses_nearest_reading_and_prefers_snapshot_on_equal_distance(self) -> None:
        records: list[Record] = []
        for host, snapshot_at in (("nearer-sample", "2026-10-05T07:29:59.000Z"),
                                  ("equal-distance", END_UTC.replace(".000Z", "Z"))):
            reading = snapshot(snapshot_at, 2, 8 * GIB)
            records.extend((
                {**reading, "host": host},
                sample(END_UTC, host=host, slices=reading["slices"],
                       zram={"state": "present", "data_bytes": 4 * GIB, "compressed_bytes": GIB}),
            ))
        self.write(self.root / "natedev" / "samples-2026-10.jsonl", *records)
        _ = index.update()
        connection = index.read_only()
        try:
            for host, data in (("nearer-sample", 4 * GIB), ("equal-distance", 3 * GIB)):
                with self.subTest(host=host), patch.object(store, "host_name", return_value=host):
                    found = memory.nearest_snapshot(connection, datetime.fromisoformat(END_UTC))
                    if found["kind"] != "memory_snapshot":
                        self.fail("The zram reading is unavailable.")
                    self.assertEqual(found["zram"], {"state": "present", "data_bytes": data,
                                                   "compressed_bytes": GIB})
        finally:
            connection.close()

    def test_tick_without_slice_readings_is_unavailable(self) -> None:
        host = store.host_name()
        self.write(self.root / host / "samples-2026-10.jsonl", {
            "kind": "memory_snapshot", "host": host, "at": END_UTC, "zram": {"state": "absent"},
        })
        _ = index.update()
        connection = index.read_only()
        try:
            found = memory.nearest_snapshot(connection, datetime.fromisoformat(END_UTC))
        finally:
            connection.close()
        self.assertEqual(found, {"kind": "unavailable"})

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


class BuildOutputProgramTests(unittest.TestCase):
    base: Path = Path()

    @override
    def setUp(self) -> None:
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_rustc_marker_identifies_an_unrecorded_build_program(self) -> None:
        target = cargo_output(self.base / "unrecorded-output")
        self.assertTrue(memory.build_output_program(str(target / "debug/deps/test")))

    def test_cargo_target_folder_is_not_a_build_program(self) -> None:
        target = cargo_output(self.base / "output")
        self.assertFalse(memory.build_output_program(str(target)))

    def test_cache_tag_without_a_build_tree_is_not_a_build_program(self) -> None:
        registry = self.base / "registry"
        scratch.mark_cargo_cache(registry)
        self.assertFalse(memory.build_output_program(str(registry / "tool")))

    def test_plain_folder_does_not_identify_a_build_program(self) -> None:
        plain = self.base / "target/debug"
        plain.mkdir(parents=True)
        self.assertFalse(memory.build_output_program(str(plain / "app")))

    def test_relative_program_does_not_identify_a_build(self) -> None:
        target = cargo_output(self.base / "target")
        self.enterContext(contextlib.chdir(self.base))
        self.assertFalse(memory.build_output_program(str((target / "debug/app").relative_to(self.base))))

    def test_program_symlink_uses_the_resolved_cargo_folder(self) -> None:
        target = cargo_output(self.base / "output")
        alias = self.base / "app"
        alias.symlink_to(target / "debug/app")
        self.assertTrue(memory.build_output_program(str(alias)))

    def test_removed_cargo_folder_does_not_keep_its_build_class(self) -> None:
        target = cargo_output(self.base / "removed-output")
        marker = target / sweep.RUSTC_INFO
        program = str(target / "debug/app")
        self.assertTrue(memory.build_output_program(program))
        marker.unlink()
        target.rmdir()
        self.assertFalse(memory.build_output_program(program))

    def test_gate_adjustment_keeps_build_and_outside_gate_rules(self) -> None:
        self.enterContext(patch.dict(os.environ, config_environment(self.base)))
        target = cargo_output(self.base / "output")
        _ = write_lint_config(Path(os.environ["CONF_DIR"]), 321)
        at = datetime.fromisoformat(START_UTC)
        for program in (str(target / "debug/app"), "/usr/bin/python"):
            with self.subTest(program=program):
                kill = memory.EarlyoomKill(at, 100, "app", 321, program)
                self.assertTrue(kill.is_build)
                self.assertFalse(kill.outside_build_gate)


if __name__ == "__main__":
    _ = unittest.main()
