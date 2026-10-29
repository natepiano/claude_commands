"""Build holds with temporary holder files and synthetic machine readings."""

from __future__ import annotations

import json
import io
import fcntl
import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TypedDict, cast, override
from unittest import mock
from zoneinfo import ZoneInfo

import build_hold
import unit_runs
from sessions import SessionRecord


# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

SCRIPT = Path(__file__).with_name("build_hold.py")
MEMORY_GATE = Path(__file__).resolve().parent.parent / "lint" / "memory_gate.sh"
# Loaded by every Python process a test starts. It opens a writer on the test's
# FIFO and passes it to each child it starts, so a detached child (the CLI's
# hold-event recorder) holds a writer until it exits, and the test's cleanup
# reads EOF only once nothing it started can still write into its scratch
# directory.
CHILD_LIFETIME = r'''import os
import subprocess

try:
    writer = os.open(os.environ["BUILD_HOLD_TEST_LIFETIME_FIFO"], os.O_WRONLY | os.O_NONBLOCK)
except (KeyError, OSError):
    writer = -1
if writer >= 0:
    original_popen = subprocess.Popen

    class LifetimeChild(original_popen):
        def __init__(self, *args, **kwargs):
            kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), writer)
            super().__init__(*args, **kwargs)

    subprocess.Popen = LifetimeChild
'''


def write_holder(folder: Path, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
    path = folder / name
    _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
    return path


def recorded_hold_events(root: Path, expected: int) -> list[HoldEventRecord]:
    deadline = time.monotonic() + TEST_HANG_GUARD_SECS
    while True:
        rows = [cast(HoldEventRecord, json.loads(line))
                for path in root.rglob("[0-9][0-9][0-9][0-9]-*.jsonl") for line in path.read_text().splitlines()]
        if len(rows) >= expected or time.monotonic() >= deadline:
            return sorted(rows, key=lambda row: (row["at"], row["event"] != "began"))
        time.sleep(0.01)


class IsolatedBuildHoldTest(unittest.TestCase):
    scratch: Path = Path()
    lifetime_reader: int = -1
    lifetime_writer: int = -1

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        proc = self.scratch / "proc"
        proc.mkdir()
        _ = self.enterContext(mock.patch.object(build_hold, "PROC_ROOT", proc))
        meminfo = self.scratch / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        lifetime = self.scratch / "child-lifetime"
        lifetime.mkdir()
        _ = (lifetime / "sitecustomize.py").write_text(CHILD_LIFETIME)
        tools = self.scratch / "tools"
        tools.mkdir()
        systemctl = tools / "systemctl"
        _ = systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o755)
        fifo = lifetime / "writers"
        os.mkfifo(fifo)
        self.lifetime_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        # Held until cleanup, so the FIFO has had a writer and reports EOF
        # once the last one closes, even when a test starts no child.
        self.lifetime_writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch),
            "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.scratch / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "BUILD_HOLD_ENFORCER_DIR": str(self.scratch / "enforcer"),
            "BUILD_HOLD_CGROUP_ROOT": str(self.scratch / "cgroup"),
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "MAC_TEST_STATE_DIR": str(self.scratch / "mac-state"),
            "BUILDLOG_MEMINFO": str(meminfo),
            # No test reads the machine's own session records, or passes for the session that runs it.
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "",
            "UNIT_ACTIVE_DIR": str(self.scratch / "active"),
            "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "",
            "BUILD_HOLD_TEST_LIFETIME_FIFO": str(fifo),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(lifetime), os.environ.get("PYTHONPATH", "")))),
        }))
        self.addCleanup(self.reap_recorders)
        self.addCleanup(self.wait_for_detached_writers)

    def wait_for_detached_writers(self) -> None:
        """Wait until no process a test started holds its FIFO writer.

        A child opens its writer only when its interpreter loads
        sitecustomize. Under load the FIFO can report EOF before a slow child
        gets there, and the read then finds that child's writer and no data,
        so wait again until a read returns EOF.
        """
        os.close(self.lifetime_writer)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        try:
            while True:
                remaining = max(deadline - time.monotonic(), 0)
                readable, _, _ = select.select([self.lifetime_reader], [], [], remaining)
                self.assertTrue(readable, "Detached children did not finish before scratch cleanup")
                try:
                    if os.read(self.lifetime_reader, 1) == b"":
                        return
                except BlockingIOError:
                    pass
        finally:
            os.close(self.lifetime_reader)

    def reap_recorders(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "hold-event-recorder":
                # A recorder runs a short subprocess; under load it can take far longer than its usual second.
                thread.join(timeout=60)
                self.assertFalse(thread.is_alive())


class OwningSessionTests(IsolatedBuildHoldTest):
    active: Path = Path()
    unit_run: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.active = self.scratch / "active"
        self.active.mkdir()
        self.unit_run = self.scratch / "run"

    def marker(self, session_id: str, run: Path) -> Path:
        marker = self.active / session_id
        _ = marker.write_text(str(run) + "\n")
        return marker

    def test_board_marker_wins_over_inherited_session_id(self) -> None:
        _ = self.marker("run-owner", self.unit_run)
        with mock.patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.unit_run),
            "CLAUDE_CODE_SESSION_ID": "stale-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_board_directory_takes_priority_over_session_directory(self) -> None:
        other_run = self.scratch / "other-run"
        _ = self.marker("board-owner", self.unit_run)
        newer = self.marker("session-owner", other_run)
        os.utime(newer, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
        with mock.patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.unit_run),
            "UNIT_SESSION_DIR": str(other_run),
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("board-owner"))

    def test_a_marker_under_either_run_folder_name_owns_the_run(self) -> None:
        for name in ("unit", "delegate"):
            active = self.scratch / name / "active"
            active.mkdir(parents=True)
            _ = (active / f"{name}-owner").write_text(f"{self.scratch / name / 'run'}\n")
        environment = {name: value for name, value in os.environ.items() if name not in ("UNIT_ACTIVE_DIR", "PLAN_DELEGATE_ACTIVE_DIR")}
        with mock.patch.object(unit_runs, "RUN_ROOT", self.scratch / "unit"), \
                mock.patch.object(unit_runs, "FORMER_RUN_ROOT", self.scratch / "delegate"):
            for name in ("unit", "delegate"):
                environment["UNIT_RUN_DIR"] = str(self.scratch / name / "run")
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(build_hold.owning_session(), build_hold.OwningSession(f"{name}-owner"))

    def test_marker_content_ignores_surrounding_whitespace(self) -> None:
        marker = self.marker("run-owner", self.unit_run)
        _ = marker.write_text(f" \t{self.unit_run}\n \t")
        with mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.unit_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_session_directory_is_used_when_board_directory_is_unset_or_empty(self) -> None:
        _ = self.marker("run-owner", self.unit_run)
        with mock.patch.dict(os.environ, {"UNIT_SESSION_DIR": str(self.unit_run)}):
            for board_is_set in (False, True):
                with self.subTest(board_is_set=board_is_set):
                    if board_is_set:
                        os.environ["UNIT_RUN_DIR"] = ""
                    else:
                        _ = os.environ.pop("UNIT_RUN_DIR", None)
                    self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_no_matching_marker_falls_back_to_shell_session(self) -> None:
        _ = self.marker("another-owner", self.scratch / "another-run")
        with mock.patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.unit_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_no_run_uses_shell_session_without_marker_lookup(self) -> None:
        _ = self.marker("another-owner", self.unit_run)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "shell-session"}):
            with mock.patch.object(Path, "iterdir", side_effect=AssertionError("No run needs no marker lookup")):
                self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_no_matching_marker_and_no_shell_session_has_no_owner(self) -> None:
        _ = self.marker("another-owner", self.scratch / "another-run")
        with mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.unit_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.NoOwningSession())

    def test_no_run_and_no_shell_session_has_no_owner(self) -> None:
        self.assertEqual(build_hold.owning_session(), build_hold.NoOwningSession())

    def test_newest_matching_marker_wins(self) -> None:
        older = self.marker("z-earlier-owner", self.unit_run)
        newer = self.marker("a-later-owner", self.unit_run)
        _ = self.marker("other-run-owner", self.scratch / "other-run")
        os.utime(older, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        os.utime(newer, ns=(1_100_000_000_000_000_000, 1_100_000_000_000_000_000))
        with mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.unit_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("a-later-owner"))

    def test_missing_active_directory_is_skipped(self) -> None:
        missing = self.scratch / "missing-active"
        with mock.patch.dict(os.environ, {
            "UNIT_ACTIVE_DIR": str(missing),
            "UNIT_RUN_DIR": str(self.unit_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))
        self.assertFalse(missing.exists())

    def test_unreadable_active_directory_is_skipped(self) -> None:
        with mock.patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.unit_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            with mock.patch.object(Path, "iterdir", side_effect=PermissionError("active directory denied")):
                self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_unreadable_marker_is_skipped(self) -> None:
        _ = self.marker("run-owner", self.unit_run)
        with mock.patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.unit_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("marker denied")):
                self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_invalid_marker_encoding_does_not_hide_readable_marker(self) -> None:
        _ = (self.active / "invalid-marker").write_bytes(b"\xff")
        _ = self.marker("run-owner", self.unit_run)
        with mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.unit_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_marker_subdirectories_are_skipped(self) -> None:
        (self.active / "not-a-marker").mkdir()
        with mock.patch.dict(os.environ, {
            "UNIT_RUN_DIR": str(self.unit_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_owner_results_are_frozen(self) -> None:
        for owner in (build_hold.OwningSession("run-owner"), build_hold.NoOwningSession()):
            with self.subTest(owner=type(owner).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(owner, "session_id", "changed")




class HolderTests(IsolatedBuildHoldTest):
    def test_no_holder_and_legacy_file_with_iso_instant(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch) / "holders"
            self.assertIsInstance(build_hold.read_holders(folder), build_hold.NoHolders)
            folder.mkdir()
            _ = (folder / "old-worker").write_text("old worker, 2026-10-04T10:56:00-07:00, the focused test\n")
            state = build_hold.read_holders(folder)
            self.assertIsInstance(state, build_hold.ActiveHolders)
            assert isinstance(state, build_hold.ActiveHolders)
            self.assertEqual(len(state.holders), 1)
            self.assertEqual(state.holders[0].name, "old-worker")
            self.assertEqual(state.holders[0].since, datetime.fromisoformat("2026-10-04T10:56:00-07:00"))
            self.assertIn("the focused test", state.holders[0].purpose)
            self.assertIsInstance(state.holders[0].release, build_hold.UnknownReleaseEta)

    def test_legacy_without_instant_uses_mtime_and_whole_line(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            path = folder / "unreadable-old-format"
            line = "test the release gate after CI settles"
            _ = path.write_text(line + "\n")
            instant = datetime.fromisoformat("2026-10-04T10:51:00+00:00")
            os.utime(path, (instant.timestamp(), instant.timestamp()))
            state = build_hold.read_holders(folder)
            assert isinstance(state, build_hold.ActiveHolders)
            self.assertEqual(state.holders[0].purpose, line)
            self.assertAlmostEqual(state.holders[0].since.timestamp(), instant.timestamp(), delta=1)
            self.assertIsNotNone(state.holders[0].since.tzinfo)
            self.assertIsInstance(state.holders[0].release, build_hold.UnknownReleaseEta)

    def test_json_holders_are_ordered_by_since_and_release_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            _ = write_holder(folder, "later", "2026-10-04T10:56:00-07:00", "the later test")
            _ = write_holder(folder, "earlier", "2026-10-04T10:50:00-07:00", "the earlier test", "2026-10-04T11:15:00-07:00")
            state = build_hold.read_holders(folder)
            assert isinstance(state, build_hold.ActiveHolders)
            self.assertEqual([holder.name for holder in state.holders], ["earlier", "later"])
            self.assertIsInstance(state.holders[0].release, build_hold.KnownReleaseEta)
            self.assertIsInstance(state.holders[1].release, build_hold.UnknownReleaseEta)
            known = state.holders[0].release
            assert isinstance(known, build_hold.KnownReleaseEta)
            self.assertEqual(known.at, datetime.fromisoformat("2026-10-04T11:15:00-07:00"))

    def test_each_regular_file_remains_a_hold_when_content_is_bad(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            _ = (folder / "broken").write_text('{"holder": 17, "since": false}\n')
            (folder / "subdirectory").mkdir()
            state = build_hold.read_holders(folder)
            assert isinstance(state, build_hold.ActiveHolders)
            self.assertEqual([holder.name for holder in state.holders], ["broken"])

    def test_file_removed_during_read_is_not_an_active_hold(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            vanished = write_holder(folder, "vanished", "2026-10-04T10:50:00-07:00", "old work")
            read_text = Path.read_text

            def read_or_release(path: Path) -> str:
                if path == vanished:
                    raise FileNotFoundError(path)
                return read_text(path)

            with mock.patch.object(Path, "read_text", read_or_release):
                self.assertIsInstance(build_hold.read_holders(folder), build_hold.NoHolders)

    def test_release_reports_remaining_holder_when_listed_file_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            _ = write_holder(folder, "first", "2026-10-04T10:50:00-07:00", "first work")
            _ = write_holder(folder, "remaining", "2026-10-04T10:51:00-07:00", "remaining work")
            vanished = write_holder(folder, "vanished", "2026-10-04T10:52:00-07:00", "old work")
            read_holder = build_hold.read_holder

            def read_or_release(path: Path) -> build_hold.Holder:
                if path == vanished:
                    raise FileNotFoundError(path)
                return read_holder(path)

            with mock.patch.object(build_hold, "read_holder", read_or_release):
                notice = build_hold.release_hold(folder, build_hold.read_holder(build_hold.holder_path(folder, "first")))
            self.assertEqual(notice, "released; still held by remaining (for remaining work, release eta unknown)")


class RenameHolderTests(IsolatedBuildHoldTest):
    def holder(self, name: str) -> Path:
        folder = self.scratch / "holders"
        folder.mkdir(exist_ok=True)
        return write_holder(folder, name, "2026-10-07T10:00:00+00:00", "rename work")

    def cycle(self) -> build_hold.HoldCycle:
        cycle = build_hold.open_cycle(datetime.fromisoformat("2026-10-07T10:00:00+00:00"))
        cycle["holders"] = {"old": {"since": "2026-10-07T10:00:00+00:00", "released_at": ""}}
        cycle["recipients"] = {"session-old": "old", "session-other": "other"}
        cycle["entries"] = [
            build_hold.new_entry("session-old", "old"),
            build_hold.new_entry("session-other", "other"),
        ]
        build_hold.save_cycle(cycle)
        return cycle

class QuietTests(IsolatedBuildHoldTest):
    def write_process_stat(self, process: build_hold.Process, utime: int, stime: int, started: int = 1) -> Path:
        path = self.scratch / "proc" / str(process.pid) / "stat"
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = ["0"] * 20
        fields[0] = "R"
        fields[11], fields[12], fields[19] = str(utime), str(stime), str(started)
        _ = path.write_text(f"{process.pid} ({process.command} worker)) " + " ".join(fields) + "\n")
        return path

    def test_long_username_cargo_keeps_quiet_check_busy(self) -> None:
        user = "natepiano"

        def fake_ps(command: list[str], *, text: bool) -> str:
            self.assertTrue(text)
            self.assertEqual(command, ["ps", "-eo", "pid=,user:32=,comm="])
            return f"77 {user} cargo\n"

        with mock.patch.object(subprocess, "check_output", side_effect=fake_ps):
            processes = build_hold.read_processes()
        self.assertEqual(build_hold.quiet_verdict(processes, user),
                         build_hold.Busy(("your builds still running: 1 cargo",)))

    def test_any_users_build_is_busy(self) -> None:
        for owner in ("nate", "ci-runner"):
            for command in ("cargo", "rustc", "cargo-nextest"):
                with self.subTest(owner=owner, command=command):
                    processes = [build_hold.Process(41, owner, command)]
                    label = "your builds still running" if owner == "nate" else owner
                    self.assertEqual(build_hold.quiet_verdict(processes, "nate"),
                                     build_hold.Busy((f"{label}: 1 {command}",)))

    def test_high_load_without_builds_is_quiet_without_waiting(self) -> None:
        processes = [build_hold.Process(41, "ci-runner", "python3")]
        with mock.patch.object(os, "getloadavg", return_value=(14.0, 12.0, 10.0)):
            verdict = build_hold.wait_for_quiet(
                600, clock=lambda: 0.0,
                sleep=lambda seconds: self.fail(f"unexpected sleep: {seconds}"),
                processes=lambda: processes, user="nate",
            )
        self.assertEqual(verdict, build_hold.Quiet(tuple(processes)))

    def test_success_line_has_load_and_five_heaviest_by_current_cpu(self) -> None:
        idle = build_hold.Process(1, "nate", "cargo-port")
        processes = [
            idle,
            build_hold.Process(2, "nate", "python3"),
            build_hold.Process(3, "nate", "cargo-berth"),
            build_hold.Process(4, "ci-runner", "agent"),
            build_hold.Process(5, "nate", "claude"),
            build_hold.Process(6, "nate", "helper"),
            build_hold.Process(7, "nate", "cargo-tile"),
        ]
        for process in processes:
            _ = self.write_process_stat(process, 1_000_000 if process == idle else 100, 50)
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            # Two elapsed seconds test the actual clock interval, including delayed sleep.
            for process, percent in zip(processes[1:], (99, 130, 20, 60, 1, 30), strict=True):
                _ = self.write_process_stat(process, 100 + percent, 50 + percent)

        clock = iter((10.0, 12.0))
        sample = build_hold.heaviest_processes

        def heaviest(processes: tuple[build_hold.Process, ...]) -> tuple[build_hold.CurrentProcessCpu, ...]:
            return sample(processes, proc_root=self.scratch / "proc", clock=lambda: next(clock), sleep=sleep)

        verdict = build_hold.wait_for_quiet(0, clock=lambda: 0.0, processes=lambda: processes, user="nate")
        output = io.StringIO()
        with mock.patch.object(build_hold, "wait_for_quiet", return_value=verdict), \
             mock.patch.object(build_hold, "heaviest_processes", side_effect=heaviest), \
             mock.patch.object(os, "sysconf", return_value=100) as sysconf, \
             mock.patch.object(os, "getloadavg", return_value=(12.3, 10.0, 9.0)), redirect_stdout(output):
            self.assertEqual(build_hold.main(["quiet"]), 0)
        sysconf.assert_called_once_with("SC_CLK_TCK")
        self.assertEqual(sleeps, [1])
        self.assertEqual(output.getvalue(),
            "builds are quiet: no build runs. 1-minute load 12.3 is other work: "
            + "nate cargo-berth 130%, nate python3 99%, nate claude 60%, "
            + "nate cargo-tile 30%, ci-runner agent 20%\n")

    def test_process_that_ends_during_cpu_window_is_skipped(self) -> None:
        process = build_hold.Process(41, "ci-runner", "python3")
        stat = self.write_process_stat(process, 100, 50)
        clock = iter((0.0, 1.0))

        def sleep(seconds: float) -> None:
            self.assertEqual(seconds, 1)
            stat.unlink()

        with mock.patch.object(os, "sysconf", return_value=100):
            readings = build_hold.heaviest_processes(
                [process], proc_root=self.scratch / "proc", clock=lambda: next(clock), sleep=sleep,
            )
        self.assertEqual(readings, ())

    def test_reused_pid_is_skipped(self) -> None:
        process = build_hold.Process(41, "ci-runner", "python3")
        _ = self.write_process_stat(process, 100, 50, started=1)
        clock = iter((0.0, 1.0))

        def sleep(seconds: float) -> None:
            self.assertEqual(seconds, 1)
            _ = self.write_process_stat(process, 200, 150, started=2)

        with mock.patch.object(os, "sysconf", return_value=100):
            readings = build_hold.heaviest_processes(
                [process], proc_root=self.scratch / "proc", clock=lambda: next(clock), sleep=sleep,
            )
        self.assertEqual(readings, ())

    def test_success_line_when_no_processes_are_visible(self) -> None:
        output = io.StringIO()
        with mock.patch.object(build_hold, "wait_for_quiet", return_value=build_hold.Quiet(())), \
             mock.patch.object(build_hold, "heaviest_processes", return_value=()), \
             mock.patch.object(os, "getloadavg", return_value=(0.0, 0.0, 0.0)), redirect_stdout(output):
            self.assertEqual(build_hold.main(["quiet", "--max-wait", "0"]), 0)
        self.assertEqual(output.getvalue(),
            "builds are quiet: no build runs. 1-minute load 0 is other work: none visible\n")

    def test_wait_ends_when_builds_end(self) -> None:
        readings = iter([
            [build_hold.Process(41, "ci-runner", "rustc")],
            [build_hold.Process(42, "nate", "python3")],
        ])
        sleeps: list[float] = []
        verdict = build_hold.wait_for_quiet(
            600, clock=lambda: 0.0, sleep=sleeps.append,
            processes=lambda: next(readings), user="nate",
        )
        self.assertIsInstance(verdict, build_hold.Quiet)
        self.assertEqual(sleeps, [10])

    def test_builds_are_grouped_by_user_then_command_largest_first(self) -> None:
        processes = [
            *[build_hold.Process(pid, "nate", "rustc") for pid in range(34)],
            *[build_hold.Process(pid, "nate", "cargo-nextest") for pid in range(34, 37)],
            *[build_hold.Process(pid, "nate", "cargo") for pid in range(37, 43)],
            build_hold.Process(43, "ci-runner", "rustc"),
            build_hold.Process(44, "ci-runner", "rustc"),
        ]
        verdict = build_hold.quiet_verdict(processes, "nate")
        self.assertEqual(verdict, build_hold.Busy((
            "your builds still running: 34 rustc, 6 cargo, 3 cargo-nextest", "ci-runner: 2 rustc",
        )))

    def test_builds_for_entire_wait_name_ci_and_other_user_without_cpu_sample(self) -> None:
        elapsed = 0.0
        sleeps: list[float] = []
        running = [build_hold.Process(101, "ci", "rustc"), build_hold.Process(102, "other", "cargo")]

        def clock() -> float:
            return elapsed

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            sleeps.append(seconds)
            elapsed += seconds

        verdict = build_hold.wait_for_quiet(
            600, clock=clock, sleep=sleep, processes=lambda: running, user="nate",
        )
        self.assertEqual(verdict, build_hold.Busy(("ci: 1 rustc", "other: 1 cargo")))
        self.assertEqual(elapsed, 600)
        self.assertEqual(sleeps, [10] * 60)
        output = io.StringIO()
        with mock.patch.object(build_hold, "wait_for_quiet", return_value=verdict), \
             mock.patch.object(build_hold, "heaviest_processes", side_effect=AssertionError("unexpected CPU sample")), \
             mock.patch.object(os, "getloadavg", side_effect=AssertionError("unexpected load read")), redirect_stdout(output):
            self.assertEqual(build_hold.main(["quiet", "--max-wait", "600"]), 1)
        self.assertEqual(output.getvalue(), "ci: 1 rustc; other: 1 cargo\n")


class TargetCommandTests(IsolatedBuildHoldTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {
            "BUILD_TARGETS_CONFIG": str(SCRIPT.parent.parent.parent / "config/build_targets.conf"),
        }))

    def run_with_mac_stand_in(
        self, arguments: list[str], exit_code: int = 0, *, check_block_holder: bool = False,
    ) -> tuple[int, str, str, list[list[str]]]:
        calls: list[list[str]] = []
        expected_script = SCRIPT.resolve().parent.parent / "mac_test/mac_test.py"

        def command(argv: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[:2], [sys.executable, str(expected_script)])
            self.assertTrue(options.get("capture_output"))
            self.assertTrue(options.get("text"))
            calls.append(argv[2:])
            if check_block_holder:
                block = self.scratch / "stand-in-block-holder"
                holder = argv[argv.index("--holder") + 1]
                if argv[2] == "block":
                    _ = block.write_text(holder)
                elif block.exists():
                    stored = block.read_text()
                    if stored != holder:
                        return subprocess.CompletedProcess(argv, 1, f"blocked by {stored}\n", "")
                    block.unlink()
            return subprocess.CompletedProcess(argv, exit_code, "Mac stand-in result\n", "Mac stand-in detail\n")

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(build_hold.hold_enforcer(), "start"), \
             mock.patch.object(subprocess, "run", side_effect=command), redirect_stdout(output), redirect_stderr(errors):
            try:
                status = build_hold.main(arguments)
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
        self.reap_recorders()
        return status, output.getvalue(), errors.getvalue(), calls

    def assert_no_natedev_record(self) -> None:
        self.assertFalse((self.scratch / "holders").exists())
        self.assertFalse((self.scratch / "release" / "current").exists())
        self.assertEqual(recorded_hold_events(self.scratch / "buildlog", 0), [])

    def test_mac_hold_forwards_block_holder_and_reason_without_a_natedev_record(self) -> None:
        status, output, errors, calls = self.run_with_mac_stand_in(
            ["hold", "--target", "mac", "--holder", "alice", "--for", "renderer checks"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(calls, [["block", "--holder", "alice", "--for", "renderer checks"]])
        self.assertEqual(output, "Mac stand-in result\n")
        self.assertEqual(errors, "Mac stand-in detail\n")
        self.assert_no_natedev_record()

    def test_mac_release_forwards_unblock_holder_and_child_exit_codes(self) -> None:
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                status, output, errors, calls = self.run_with_mac_stand_in(
                    ["release", "--target", "mac", "--holder", "alice"], exit_code=exit_code)
                self.assertEqual(status, exit_code, errors)
                self.assertEqual(calls, [["unblock", "--holder", "alice"]])
                self.assertEqual(output, "Mac stand-in result\n")
                self.assertEqual(errors, "Mac stand-in detail\n")
                self.assert_no_natedev_record()

    def test_mac_hold_returns_the_block_child_failure(self) -> None:
        status, output, errors, calls = self.run_with_mac_stand_in(
            ["hold", "--target", "mac", "--holder", "alice", "--for", "renderer checks"], exit_code=9)
        self.assertEqual(status, 9, errors)
        self.assertEqual(calls, [["block", "--holder", "alice", "--for", "renderer checks"]])
        self.assertEqual(output, "Mac stand-in result\n")
        self.assertEqual(errors, "Mac stand-in detail\n")
        self.assert_no_natedev_record()

    def session(self, name: str, *former: str) -> None:
        path = self.scratch / "renamed-session.sock"
        if not path.exists():
            connection = socket.socket(socket.AF_UNIX)
            connection.bind(str(path))
            self.addCleanup(connection.close)
        directory = self.scratch / "sessions"
        directory.mkdir(exist_ok=True)
        record: SessionRecord = {
            "pid": os.getpid(), "sessionId": "renamed-session", "name": name,
            "formerNames": list(former), "messagingSocketPath": str(path), "updatedAt": 1,
            "tmux": "",
        }
        _ = (directory / "renamed-session.json").write_text(json.dumps(record))

    def test_mac_release_after_session_rename_releases_own_block_and_keeps_others(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "renamed-session"}):
            for given in ([], ["--holder", "first-name"], ["--holder", "current-name"]):
                with self.subTest(given=given):
                    self.session("first-name")
                    status, _, errors, calls = self.run_with_mac_stand_in(
                        ["hold", "--target", "mac", "--for", "renderer checks"], check_block_holder=True)
                    self.assertEqual(status, 0, errors)
                    self.assertEqual(calls, [["block", "--holder", "first-name", "--for", "renderer checks"]])
                    self.session("current-name", "first-name", "middle-name")
                    status, output, errors, calls = self.run_with_mac_stand_in(
                        ["release", "--target", "mac", *given], check_block_holder=True)
                    self.assertEqual(status, 0, errors)
                    self.assertEqual(calls, [["unblock", "--holder", "current-name"],
                                             ["unblock", "--holder", "first-name"]])
                    self.assertEqual(output, "Mac stand-in result\n")
                    self.assertEqual(errors, "Mac stand-in detail\n")
                    self.assertFalse((self.scratch / "stand-in-block-holder").exists())
                    self.assert_no_natedev_record()
                    status, _, errors, _ = self.run_with_mac_stand_in(
                        ["hold", "--target", "mac", "--holder", "someone-else", "--for", "renderer checks"],
                        check_block_holder=True)
                    self.assertEqual(status, 0, errors)
                    status, output, errors, calls = self.run_with_mac_stand_in(
                        ["release", "--target", "mac", *given], check_block_holder=True)
                    self.assertEqual(status, 1)
                    self.assertEqual(calls, [["unblock", "--holder", "current-name"]])
                    self.assertEqual(output, "blocked by someone-else\n")
                    self.assertEqual(errors, "")
                    self.assertEqual((self.scratch / "stand-in-block-holder").read_text(), "someone-else")
                    self.assert_no_natedev_record()

    def test_default_hold_and_release_record_only_natedev_and_never_call_mac(self) -> None:
        status, output, errors, calls = self.run_with_mac_stand_in(
            ["hold", "--holder", "alice", "--for", "renderer checks"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(calls, [])
        self.assertTrue(output.startswith("natedev is held for renderer checks."), output)
        holder = build_hold.read_holder(self.scratch / "holders" / "alice")
        self.assertEqual((holder.name, holder.purpose), ("alice", "renderer checks"))
        (self.scratch / "release" / "current").unlink()
        status, _, errors, calls = self.run_with_mac_stand_in(["release", "--holder", "alice"])
        self.assertEqual(status, 0, errors)
        self.assertEqual(calls, [])
        self.assertFalse((self.scratch / "holders" / "alice").exists())
        rows = recorded_hold_events(self.scratch / "buildlog", 2)
        self.assertEqual([(row["target"], row["event"], row["holder"]) for row in rows],
                         [("natedev", "began", "alice"), ("natedev", "ended", "alice")])

    def test_unknown_hold_and_release_targets_list_the_configured_names(self) -> None:
        for arguments in (["hold", "--for", "renderer checks"], ["release"]):
            with self.subTest(action=arguments[0]):
                status, output, errors, calls = self.run_with_mac_stand_in(
                    [*arguments, "--target", "missing", "--holder", "alice"])
                self.assertEqual(status, 2)
                self.assertEqual((output + errors).splitlines(),
                                 ["unknown target missing; configured targets: mac, natedev"])
                self.assertEqual(calls, [])
                self.assert_no_natedev_record()

    def test_unknown_target_with_an_unreadable_config_names_the_reason(self) -> None:
        missing = self.scratch / "missing-targets.conf"
        incomplete = self.scratch / "incomplete-targets.conf"
        _ = incomplete.write_text("targets=mac\n", encoding="utf-8")
        invalid_utf8 = self.scratch / "invalid-utf8-targets.conf"
        _ = invalid_utf8.write_bytes(b"\xff")
        for config, reason in (
            (missing, f"targets: expected distinct target names in {missing}"),
            (incomplete, "kind.mac: expected local or ssh, got ''"),
            (invalid_utf8, "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"),
        ):
            for arguments in (["hold", "--for", "renderer checks"], ["release"]):
                with self.subTest(config=config.name, action=arguments[0]), mock.patch.dict(
                    os.environ, {"BUILD_TARGETS_CONFIG": str(config)},
                ), mock.patch.object(build_hold, "holder_of") as holder_lookup:
                    status, output, errors, calls = self.run_with_mac_stand_in(
                        [*arguments, "--target", "missing", "--holder", "alice"])
                    self.assertEqual(status, 2)
                    self.assertEqual(output, f"unknown target missing; the target config cannot be read: {reason}\n")
                    self.assertEqual(errors, "")
                    holder_lookup.assert_not_called()
                    self.assertEqual(calls, [])
                    self.assert_no_natedev_record()

    def test_mac_hold_refuses_release_eta_and_zone_before_calling_block(self) -> None:
        for option, value in (("--release-eta", "23:59"), ("--zone", "UTC")):
            with self.subTest(option=option):
                status, output, errors, calls = self.run_with_mac_stand_in(
                    ["hold", "--target", "mac", "--holder", "alice", "--for", "renderer checks", option, value])
                self.assertEqual(status, 2)
                self.assertEqual((output + errors).splitlines(), [f"{option} is not supported with --target mac"])
                self.assertEqual(calls, [])
                self.assert_no_natedev_record()

    def test_mac_release_refuses_resume_before_calling_unblock(self) -> None:
        status, output, errors, calls = self.run_with_mac_stand_in(
            ["release", "--target", "mac", "--holder", "alice", "--resume"])
        self.assertEqual(status, 2)
        self.assertEqual((output + errors).splitlines(), ["--resume is not supported with --target mac"])
        self.assertEqual(calls, [])
        self.assert_no_natedev_record()


class CommandTests(IsolatedBuildHoldTest):
    def run_cli(self, folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
        audit_args = ["--proc-root", str(self.scratch / "proc")] if args[0] in {"hold", "status", "audit"} else []
        return subprocess.run(
            ["python3", str(SCRIPT), *args, *audit_args],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "BUILD_HOLD_DIR": str(folder)},
        )

    def damage_record(self, kind: str, scratch: Path, *, two_holders: bool = False) -> tuple[Path, bytes]:
        folder = scratch / "holders"
        for name in (("first", "second") if two_holders else ("first",)):
            held = self.run_cli(folder, "hold", "--holder", name, "--for", f"{name} test")
            self.assertEqual(held.returncode, 0, held.stderr)
        release = scratch / "release"
        cycle_id = (release / "current").read_text().strip()
        path = release / "current" if kind == "current" else release / cycle_id / "cycle.json"
        content = b"invalid-current\n" if kind == "current" else b"{invalid json\n"
        _ = path.write_bytes(content)
        return path, content

    def assert_record_set_aside(self, path: Path, content: bytes, output: str, scratch: Path) -> None:
        copies = list(path.parent.glob(path.name + ".damaged-*"))
        self.assertEqual(len(copies), 1)
        copy = copies[0]
        self.assertRegex(copy.name, "^" + re.escape(path.name) + r"\.damaged-\d{8}T\d{6}Z$")
        self.assertEqual(copy.read_bytes(), content)
        self.assertFalse(path.exists())
        self.assertFalse((scratch / "release" / "current").exists())
        self.assertIn(f"; set aside as {copy}; this hold now releases every session at once", output)

    def session(self, session_id: str, name: str, *former: str) -> None:
        """Record a live Claude session, or its rename: this test process stands in for it."""
        path = self.scratch / f"{session_id}.sock"
        if not path.exists():
            held = socket.socket(socket.AF_UNIX)
            held.bind(str(path))
            self.addCleanup(held.close)
        (self.scratch / "sessions").mkdir(exist_ok=True)
        _ = (self.scratch / "sessions" / f"{session_id}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": session_id, "name": name, "formerNames": list(former),
            "messagingSocketPath": str(path), "updatedAt": 1}))

    def hold(self, folder: Path, given: str | None) -> str:
        identity = build_hold.holder_of(given)
        return build_hold.write_hold(folder, identity, "one test", build_hold.release_request(None, None),
                                     datetime.now().astimezone())

    def test_session_rename_keeps_begin_and_end_events_under_the_session_id(self) -> None:
        folder = self.scratch / "holders"
        self.session("id-1", "old-name")
        _ = build_hold.start_hold(folder, build_hold.holder_of("old-name"), "render checks",
                                  build_hold.NoReleaseEta(), datetime.now(timezone.utc))
        self.session("id-1", "new-name", "old-name")
        (self.scratch / "release" / "current").unlink()
        _ = build_hold.release_cycle(folder, build_hold.held_key(folder, "new-name"))
        rows = recorded_hold_events(self.scratch / "buildlog", 2)
        self.assertEqual([(row["event"], row["hold_key"], row["holder"]) for row in rows],
                         [("began", "id-1", "old-name"), ("ended", "id-1", "new-name")])

    def test_a_hold_is_kept_by_session_id_so_a_rename_between_hold_and_release_loses_nothing(self) -> None:
        folder = self.scratch / "holders"
        self.session("id-1", "old-name")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id-1"}):
            self.assertIn("natedev is held for one test.", self.hold(folder, None))
        self.assertEqual([path.name for path in folder.iterdir()], ["id-1"])
        self.session("id-1", "new-name", "old-name")
        holders = build_hold.read_holders(folder)
        assert isinstance(holders, build_hold.ActiveHolders)
        self.assertEqual([(holder.name, holder.key) for holder in holders.holders], [("new-name", "id-1")])
        # The name it had when it took the hold still finds it, and so does no name at all.
        with self.assertRaisesRegex(ValueError, "someone-else held nothing"):
            _ = build_hold.held_key(folder, "someone-else")
        self.assertEqual(build_hold.held_key(folder, "old-name"), "id-1")
        self.assertEqual(build_hold.held_key(folder, "new-name"), "id-1")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id-1"}):
            self.assertEqual(build_hold.held_key(folder, None), "id-1")
        self.assertEqual(build_hold.release_hold(folder, build_hold.read_holder(build_hold.holder_path(folder, "id-1"))), "released, builds may resume.")

    def test_a_hold_taken_under_a_name_before_is_released_by_the_session_that_had_the_name(self) -> None:
        folder = self.scratch / "holders"
        # No live session is called this yet, so the hold is kept under the name, as every hold once was.
        _ = self.hold(folder, "old-name")
        self.assertEqual([path.name for path in folder.iterdir()], ["old-name"])
        self.session("id-1", "new-name", "old-name")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id-1"}):
            self.assertEqual(build_hold.held_key(folder, None), "old-name")
        self.assertEqual(build_hold.held_key(folder, "new-name"), "old-name")
        # With no name and no session, there is no holder to look up.
        refused = self.run_cli(folder, "hold", "--for", "one test")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("give --holder: this is not a live Claude session", refused.stderr)

    def test_last_release_sets_aside_each_damaged_record(self) -> None:
        for kind in ("cycle", "current"):
            scratch = self.scratch / kind
            scratch.mkdir()
            with self.subTest(kind=kind):
                with mock.patch.dict(os.environ, {
                    "BUILD_HOLD_DIR": str(scratch / "holders"),
                    "BUILD_HOLD_RELEASE_DIR": str(scratch / "release"),
                }):
                    path, content = self.damage_record(kind, scratch)
                    result = self.run_cli(scratch / "holders", "release", "--holder", "first")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_record_set_aside(path, content, result.stdout, scratch)
                    self.assertEqual(result.stdout.splitlines()[-1], "released, builds may resume. No hold cycle: broadcast this release to every session.")
                    self.assertFalse((scratch / "holders" / "first").exists())

    def test_malformed_cycle_shapes_set_aside_on_release_and_report_on_status(self) -> None:
        for label, error_field in (("null entry", "entry 0"), ("number entry", "entry 0"),
                                   ("holders list", "holders"), ("null holder", "holder"),
                                   ("holder instant", "holder first"), ("missing holders", "holders"),
                                   ("recipients list", "recipients"), ("recipient name", "recipients"),
                                   ("null release start", "release_started_at"),
                                   ("top-level list", "hold cycle")):
            scratch = self.scratch / label.replace(" ", "-")
            scratch.mkdir()
            with self.subTest(shape=label), mock.patch.dict(os.environ, {
                "BUILD_HOLD_DIR": str(scratch / "holders"),
                "BUILD_HOLD_RELEASE_DIR": str(scratch / "release"),
            }):
                held = self.run_cli(scratch / "holders", "hold", "--holder", "first", "--for", "first test")
                self.assertEqual(held.returncode, 0, held.stderr)
                release = scratch / "release"
                path = release / (release / "current").read_text().strip() / "cycle.json"
                valid = cast(dict[str, object], json.loads(path.read_text()))
                malformed_cases: dict[str, object] = {
                    "null entry": {**valid, "entries": [None]},
                    "number entry": {**valid, "entries": [1]},
                    "holders list": {**valid, "holders": []},
                    "null holder": {**valid, "holders": {"first": None}},
                    "holder instant": {**valid, "holders": {"first": {"since": 1, "released_at": ""}}},
                    "missing holders": {key: value for key, value in valid.items() if key != "holders"},
                    "recipients list": {**valid, "recipients": []},
                    "recipient name": {**valid, "recipients": {"session": 1}},
                    "null release start": {**valid, "release_started_at": None},
                    "top-level list": [],
                }
                malformed = malformed_cases[label]
                content = (json.dumps(malformed) + "\n").encode()
                _ = path.write_bytes(content)
                status = self.run_cli(scratch / "holders", "status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertIn("release record could not be read:", status.stdout)
                self.assertIn(error_field, status.stdout)
                self.assertIn("/build:hold release sets it aside and ends the hold", status.stdout)
                self.assertEqual(path.read_bytes(), content)
                result = self.run_cli(scratch / "holders", "release", "--holder", "first")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_record_set_aside(path, content, result.stdout, scratch)
                self.assertEqual(result.stdout.splitlines()[-1], "released, builds may resume. No hold cycle: broadcast this release to every session.")
                self.assertFalse((scratch / "holders" / "first").exists())

    def test_dangling_cycle_link_is_set_aside_instead_of_current(self) -> None:
        held = self.run_cli(self.scratch / "holders", "hold", "--holder", "first", "--for", "first test")
        self.assertEqual(held.returncode, 0, held.stderr)
        release = self.scratch / "release"
        path = release / (release / "current").read_text().strip() / "cycle.json"
        path.unlink()
        path.symlink_to("missing-cycle.json")
        result = self.run_cli(self.scratch / "holders", "release", "--holder", "first")
        self.assertEqual(result.returncode, 0, result.stderr)
        copies = list(path.parent.glob("cycle.json.damaged-*"))
        self.assertEqual(len(copies), 1)
        self.assertTrue(copies[0].is_symlink())
        self.assertEqual(copies[0].readlink(), Path("missing-cycle.json"))
        self.assertFalse(path.is_symlink())
        self.assertFalse((release / "current").exists())
        self.assertIn(f"set aside as {copies[0]}", result.stdout)
        self.assertFalse((self.scratch / "holders" / "first").exists())

    def test_partial_release_sets_aside_damaged_record_and_names_remaining_holder(self) -> None:
        for kind in ("cycle", "current"):
            scratch = self.scratch / kind
            scratch.mkdir()
            with self.subTest(kind=kind), mock.patch.dict(os.environ, {
                "BUILD_HOLD_DIR": str(scratch / "holders"),
                "BUILD_HOLD_RELEASE_DIR": str(scratch / "release"),
            }):
                path, content = self.damage_record(kind, scratch, two_holders=True)
                result = self.run_cli(scratch / "holders", "release", "--holder", "first")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_record_set_aside(path, content, result.stdout, scratch)
                self.assertIn("still held by second", result.stdout)
                self.assertFalse((scratch / "holders" / "first").exists())
                self.assertTrue((scratch / "holders" / "second").exists())

    def test_resume_sets_aside_damaged_current_and_releases_only_holder(self) -> None:
        path, content = self.damage_record("current", self.scratch)
        result = self.run_cli(self.scratch / "holders", "release", "--resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_record_set_aside(path, content, result.stdout, self.scratch)
        self.assertIn("released, builds may resume.", result.stdout)
        self.assertFalse((self.scratch / "holders" / "first").exists())

    def test_resume_after_earlier_damage_does_not_release_holder(self) -> None:
        path, content = self.damage_record("cycle", self.scratch)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "waiting-session"}):
            waited = self.run_cli(self.scratch / "holders", "record-recipient", "--session-id", "waiting-session", "--name", "waiting")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assert_record_set_aside(path, content, waited.stdout, self.scratch)
        resumed = self.run_cli(self.scratch / "holders", "release", "--resume")
        self.assertEqual(resumed.returncode, 1)
        self.assertIn("release needs --holder, or --resume for an active release", resumed.stderr)
        self.assertTrue((self.scratch / "holders" / "first").exists())

    def test_intact_cycle_final_line_does_not_request_broadcast(self) -> None:
        held = self.run_cli(self.scratch / "holders", "hold", "--holder", "first", "--for", "first test")
        self.assertEqual(held.returncode, 0, held.stderr)
        release = self.scratch / "release"
        path = release / (release / "current").read_text().strip() / "cycle.json"
        cycle = cast(dict[str, object], json.loads(path.read_text()))
        cycle["release_started_at"] = (datetime.now().astimezone() - timedelta(minutes=2)).isoformat()
        _ = path.write_text(json.dumps(cycle) + "\n")
        result = self.run_cli(self.scratch / "holders", "release", "--holder", "first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().startswith("released, builds may resume."))
        self.assertNotIn("No hold cycle: broadcast this release to every session.", result.stdout)

    def test_new_holder_after_damage_shares_broadcast_release(self) -> None:
        for kind in ("cycle", "current"):
            scratch = self.scratch / kind
            scratch.mkdir()
            with self.subTest(kind=kind), mock.patch.dict(os.environ, {
                "BUILD_HOLD_DIR": str(scratch / "holders"),
                "BUILD_HOLD_RELEASE_DIR": str(scratch / "release"),
            }):
                path, content = self.damage_record(kind, scratch)
                old_cycles = list((scratch / "release").glob("*/cycle.json"))
                held = self.run_cli(scratch / "holders", "hold", "--holder", "second", "--for", "second test")
                self.assertEqual(held.returncode, 0, held.stderr)
                self.assert_record_set_aside(path, content, held.stdout, scratch)
                self.assertTrue((scratch / "holders" / "second").exists())
                self.assertEqual(list((scratch / "release").glob("*/cycle.json")), [] if kind == "cycle" else old_cycles)
                first = self.run_cli(scratch / "holders", "release", "--holder", "first")
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertIn("still held by second", first.stdout)
                final = self.run_cli(scratch / "holders", "release", "--holder", "second")
                self.assertEqual(final.returncode, 0, final.stderr)
                self.assertEqual(final.stdout.strip(), "released, builds may resume. No hold cycle: broadcast this release to every session.")
                self.assertFalse((scratch / "holders" / "second").exists())

    def test_sole_holder_wait_ignores_damaged_cycle_and_recipient_can_set_it_aside(self) -> None:
        commands = (
            ("wait",),
            ("record-recipient", "--session-id", "first", "--name", "first session"),
        )
        for kind in ("cycle", "current"):
            for command in commands:
                scratch = self.scratch / (kind + "-" + command[0])
                scratch.mkdir()
                with self.subTest(kind=kind, command=command[0]), mock.patch.dict(os.environ, {
                    "BUILD_HOLD_DIR": str(scratch / "holders"),
                    "BUILD_HOLD_RELEASE_DIR": str(scratch / "release"),
                    "CLAUDE_CODE_SESSION_ID": "first",
                }):
                    path, content = self.damage_record(kind, scratch)
                    result = self.run_cli(scratch / "holders", *command)
                    if command[0] == "wait":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(path.read_bytes(), content)
                        self.assertTrue((scratch / "holders" / "first").is_file())
                        self.assertEqual(list(path.parent.glob(path.name + ".damaged-*")), [])
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assert_record_set_aside(path, content, result.stdout, scratch)

    def test_status_reports_recovery_without_changing_damaged_record(self) -> None:
        for kind in ("cycle", "current"):
            scratch = self.scratch / kind
            scratch.mkdir()
            with self.subTest(kind=kind), mock.patch.dict(os.environ, {
                "BUILD_HOLD_DIR": str(scratch / "holders"),
                "BUILD_HOLD_RELEASE_DIR": str(scratch / "release"),
            }):
                path, content = self.damage_record(kind, scratch)
                result = self.run_cli(scratch / "holders", "status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("release record could not be read:", result.stdout)
                self.assertIn("/build:hold release sets it aside and ends the hold", result.stdout)
                self.assertEqual(path.read_bytes(), content)
                self.assertTrue((scratch / "release" / "current").exists())
                self.assertEqual(list(path.parent.glob(path.name + ".damaged-*")), [])

    def test_idle_wait_and_recipient_record_leave_release_directory_absent(self) -> None:
        release = self.scratch / "release"
        self.assertFalse(release.exists())
        for arguments in (
            ("wait",),
            ("record-recipient", "--session-id", "idle", "--name", "idle session"),
        ):
            with self.subTest(arguments=arguments), mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "idle"}):
                result = self.run_cli(self.scratch / "holders", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(release.exists())


    def test_hold_writes_unknown_and_status_names_it(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            result = self.run_cli(folder, "hold", "--holder", "slot one", "--for", "the focused test")
            self.assertEqual(result.returncode, 0, result.stderr)
            path = folder / "slot-one"
            fields = cast(dict[str, object], json.loads(path.read_text()))
            self.assertEqual(fields["holder"], "slot one")
            self.assertEqual(fields["for"], "the focused test")
            self.assertEqual(fields["release_eta"], "unknown")
            self.assertIsNotNone(datetime.fromisoformat(cast(str, fields["since"])).tzinfo)
            status = self.run_cli(folder, "status")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("slot one", status.stdout)
            self.assertIn("the focused test", status.stdout)
            self.assertIn("unknown", status.stdout)

    def test_hold_message_tells_recipient_verify_waits_without_registering(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            result = self.run_cli(Path(scratch), "hold", "--holder", "slot", "--for", "the focused test")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("verify.sh waits by itself", result.stdout)
            self.assertNotIn("build_hold.py wait", result.stdout)
            self.assertIn("tests and builds outside the hold are stopped", result.stdout)
            self.assertEqual(len(recorded_hold_events(self.scratch / "buildlog", 1)), 1)

    def test_recipient_wait_command_registers_its_session_id(self) -> None:
        folder = self.scratch / "holders"
        held = self.run_cli(folder, "hold", "--holder", "holder", "--for", "the focused test")
        self.assertEqual(held.returncode, 0, held.stderr)
        received = subprocess.run(
            ["python3", str(SCRIPT), "wait"], capture_output=True, text=True, check=False,
            env={**os.environ, "CLAUDE_CODE_SESSION_ID": "received-session"},
        )
        self.assertEqual(received.returncode, 10, received.stderr)
        self.assertIn("natedev is held by holder", received.stdout)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual([entry["session_id"] for entry in cycle["entries"]], ["received-session"])
        self.assertEqual(cycle["entries"][0]["delivery"], "NotAttempted")

    def test_wait_status_guards_shell_build_until_last_other_holder_releases(self) -> None:
        folder = self.scratch / "holders"
        events = self.scratch / "build-events"
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "owner"}):
            def attempt(phase: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["bash", "-c", 'python3 "$1" wait && printf "%s\\n" "$2" >> "$3"',
                     "wait-and-build", str(SCRIPT), phase, str(events)],
                    capture_output=True, text=True, check=False,
                )

            free = attempt("no hold")
            self.assertEqual(free.returncode, 0, free.stderr)
            held = self.run_cli(folder, "hold", "--holder", "owner", "--for", "owner test")
            self.assertEqual(held.returncode, 0, held.stderr)
            own = attempt("sole holder")
            self.assertEqual(own.returncode, 0, own.stderr)
            cycle = build_hold.read_cycle()
            assert cycle is not None
            self.assertEqual(cycle["recipients"], {})
            other = self.run_cli(folder, "hold", "--holder", "other", "--for", "other test")
            self.assertEqual(other.returncode, 0, other.stderr)
            shared = attempt("must not build with two holders")
            self.assertEqual(shared.returncode, 10, shared.stderr)
            self.assertTrue((folder / "other").is_file())
            partial = self.run_cli(folder, "release", "--holder", "owner")
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertIn("still held by other", partial.stdout)
            blocked = attempt("must not build before release")
            self.assertEqual(blocked.returncode, 10, blocked.stderr)
            self.assertEqual(events.read_text().splitlines(), ["no hold", "sole holder"])
            released = self.run_cli(folder, "release", "--holder", "other")
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertFalse((folder / "other").exists())
            after_release = attempt("released")
            self.assertEqual(after_release.returncode, 0, after_release.stderr)
            self.assertEqual(events.read_text().splitlines(), ["no hold", "sole holder", "released"])

    def test_wait_without_session_id_refuses_other_holder_until_release(self) -> None:
        folder = self.scratch / "holders"
        held = self.run_cli(folder, "hold", "--holder", "other", "--for", "other test")
        self.assertEqual(held.returncode, 0, held.stderr)
        blocked = self.run_cli(folder, "wait")
        self.assertEqual(blocked.returncode, 10, blocked.stderr)
        self.assertTrue((folder / "other").is_file())
        released = self.run_cli(folder, "release", "--holder", "other")
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(self.run_cli(folder, "wait").returncode, 0)

    def test_overlapping_holders_share_one_cycle_and_next_hold_starts_another(self) -> None:
        folder = self.scratch / "holders"
        release = self.scratch / "release"
        first = self.run_cli(folder, "hold", "--holder", "first", "--for", "first test")
        self.assertEqual(first.returncode, 0, first.stderr)
        current = release / "current"
        first_cycle = current.read_text().strip()
        self.assertTrue((release / first_cycle).is_dir())
        second = self.run_cli(folder, "hold", "--holder", "second", "--for", "second test")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(current.read_text().strip(), first_cycle)
        partial = self.run_cli(folder, "release", "--holder", "first")
        self.assertEqual(partial.returncode, 0, partial.stderr)
        self.assertTrue((folder / "second").exists())
        self.assertEqual(current.read_text().strip(), first_cycle)
        cycle_path = release / first_cycle / "cycle.json"
        cycle = cast(dict[str, object], json.loads(cycle_path.read_text()))
        cycle["release_started_at"] = (datetime.now().astimezone() - timedelta(minutes=2)).isoformat()
        _ = cycle_path.write_text(json.dumps(cycle) + "\n")
        final = self.run_cli(folder, "release", "--holder", "second")
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertFalse((folder / "second").exists())
        next_hold = self.run_cli(folder, "hold", "--holder", "third", "--for", "third test")
        self.assertEqual(next_hold.returncode, 0, next_hold.stderr)
        self.assertNotEqual(current.read_text().strip(), first_cycle)

    def test_release_names_remaining_holder_then_allows_builds(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            first = write_holder(folder, "first", "2026-10-04T10:50:00-07:00", "the first test")
            second = write_holder(folder, "second", "2026-10-04T10:56:00-07:00", "the second test")
            partial = self.run_cli(folder, "release", "--holder", "first")
            self.assertEqual(partial.returncode, 0, partial.stderr)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertIn("released; still held by second (for the second test, release eta unknown)", partial.stdout)
            self.assertNotIn("builds may resume", partial.stdout)
            final = self.run_cli(folder, "release", "--holder", "second")
            self.assertEqual(final.returncode, 0, final.stderr)
            self.assertEqual(final.stdout.strip(), "released, builds may resume. No hold cycle: broadcast this release to every session.")
            self.assertFalse(second.exists())
            self.assertEqual(self.run_cli(folder, "status").stdout.strip(), "no build hold")

    def test_release_without_own_file_fails_and_keeps_other_hold(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            other = write_holder(folder, "other", "2026-10-04T10:56:00-07:00", "the other test")
            result = self.run_cli(folder, "release", "--holder", "absent")
            self.assertEqual(result.returncode, 1)
            self.assertIn("held nothing", result.stdout + result.stderr)
            self.assertTrue(other.exists())

    def test_hold_rejects_a_past_eta(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            now = datetime.now(ZoneInfo("America/New_York"))
            if now.hour == 0 and now.minute == 0:
                self.skipTest("the local day has no earlier HH:MM yet")
            past = "00:00"
            result = self.run_cli(folder, "hold", "--holder", "slot", "--for", "the test", "--release-eta", past, "--zone", "America/New_York")
            self.assertEqual(result.returncode, 1)
            self.assertFalse((folder / "slot").exists())

    def test_release_eta_uses_offset_at_release_wall_time(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            now = datetime.fromisoformat("2026-11-01T00:30:00-07:00")
            request = build_hold.release_request("02:30", "America/Los_Angeles")
            _ = build_hold.write_hold(folder, build_hold.HolderIdentity("slot", "slot"), "clock change", request, now)
            fields = cast(dict[str, object], json.loads((folder / "slot").read_text()))
            self.assertEqual(fields["release_eta"], "2026-11-01T02:30:00-08:00")

    def test_release_eta_uses_named_zone_when_machine_zone_differs(self) -> None:
        class FixedDatetime(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return datetime.fromisoformat("2026-10-04T21:00:00-04:00")

        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            output = io.StringIO()
            errors = io.StringIO()
            try:
                with mock.patch.dict(os.environ, {"TZ": "America/New_York", "USER_ZONE": "America/New_York", "BUILD_HOLD_DIR": str(folder)}):
                    time.tzset()
                    with mock.patch.object(build_hold, "datetime", FixedDatetime), redirect_stdout(output), redirect_stderr(errors):
                        result = build_hold.main(["hold", "--holder", "slot", "--for", "the test", "--release-eta", "22:00", "--zone", "America/Los_Angeles"])
            finally:
                time.tzset()
            self.assertEqual(result, 0, errors.getvalue())
            fields = cast(dict[str, object], json.loads((folder / "slot").read_text()))
            self.assertEqual(fields["release_eta"], "2026-10-04T22:00:00-07:00")

    def test_release_eta_requires_zone(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            result = self.run_cli(folder, "hold", "--holder", "slot", "--for", "the test", "--release-eta", "22:00")
            self.assertEqual(result.returncode, 1)
            self.assertIn("--zone", result.stderr)
            self.assertIn("--release-eta", result.stderr)
            self.assertFalse((folder / "slot").exists())

    def test_release_eta_rejects_unknown_zone(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            result = self.run_cli(folder, "hold", "--holder", "slot", "--for", "the test", "--release-eta", "22:00", "--zone", "Pacific/Nowhere")
            self.assertEqual(result.returncode, 1)
            self.assertIn("Pacific/Nowhere", result.stderr)
            self.assertFalse((folder / "slot").exists())








HolderFileContent = TypedDict("HolderFileContent", {"holder": str, "since": str, "for": str, "release_eta": str})









class ReleaseDeliveryTests(IsolatedBuildHoldTest):
    def test_a_lookup_is_tried_again_before_it_is_called_unavailable(self) -> None:
        def answers(*codes: int) -> list[subprocess.CompletedProcess[str]]:
            return [subprocess.CompletedProcess([], code, "/tmp/late.sock\n" if code == 0 else "",
                                                "records unreadable\n" if code == 3 else "") for code in codes]

        with mock.patch.object(subprocess, "run", side_effect=answers(3, 3, 0)), \
             mock.patch.object(time, "sleep") as sleep:
            self.assertEqual(build_hold.socket_for("session-a"), build_hold.RecipientLive("/tmp/late.sock"))
            self.assertEqual(sleep.call_count, 2)
        with mock.patch.object(subprocess, "run", side_effect=answers(3, 3, 3)), mock.patch.object(time, "sleep"):
            self.assertEqual(build_hold.socket_for("session-a"), build_hold.RecipientLookupFailed("records unreadable"))
        with mock.patch.object(subprocess, "run", side_effect=answers(1)), mock.patch.object(time, "sleep") as sleep:
            self.assertEqual(build_hold.socket_for("session-a"), build_hold.RecipientGone())
            sleep.assert_not_called()


    def test_send_exit_codes_have_named_delivery_results(self) -> None:
        entry = build_hold.new_entry("session-a", "shared name")
        for code, expected in (
            (0, build_hold.ReleaseDelivered()), (1, build_hold.ReleaseQueued()),
            (2, build_hold.ReleaseSendFailed("recipient refused")),
            (3, build_hold.ReleaseSendFailed("recipient refused")),
        ):
            with self.subTest(code=code):
                result = subprocess.CompletedProcess([], code, "", "recipient refused")
                with mock.patch.object(subprocess, "run", return_value=result):
                    self.assertEqual(build_hold.send_release(entry, "/test/socket"), expected)
        with mock.patch.object(subprocess, "run", side_effect=OSError("send unavailable")):
            self.assertEqual(build_hold.send_release(entry, "/test/socket"),
                             build_hold.ReleaseSendFailed("send unavailable"))


    def test_empty_or_unavailable_socket_lookup_has_named_results(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(build_hold.socket_for("session-a"), build_hold.RecipientGone())
        with mock.patch.object(subprocess, "run", side_effect=OSError("lookup unavailable")), \
             mock.patch.object(time, "sleep"):
            self.assertEqual(build_hold.socket_for("session-a"), build_hold.RecipientLookupFailed("lookup unavailable"))


class AtomicWriteTests(IsolatedBuildHoldTest):
    now: datetime = datetime(2026, 10, 28, tzinfo=timezone.utc)

    def record_path(self) -> Path:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return self.scratch / "release" / cycle["id"] / "cycle.json"

    def test_cycle_record_is_whole_at_the_moment_it_is_renamed(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                cycle = build_hold.open_cycle(self.now)
                path = self.record_path()
                old = path.read_bytes() if existing else b""
                if not existing:
                    path.unlink()
                cycle["release_started_at"] = self.now.isoformat()
                whole = (json.dumps(cycle, ensure_ascii=False) + "\n").encode()
                replace = os.replace
                seen: list[Path] = []

                def rename(source: Path, destination: Path) -> None:
                    self.assertEqual(destination, path)
                    self.assertEqual(path.read_bytes() if path.exists() else b"", old)
                    self.assertEqual(source.read_bytes(), whole)
                    seen.append(source)
                    replace(source, destination)

                with mock.patch.object(os, "replace", side_effect=rename):
                    build_hold.save_cycle(cycle)
                self.assertEqual(seen, [path.with_suffix(".tmp")])
                self.assertEqual(path.read_bytes(), whole)


    def test_current_is_whole_at_the_moment_it_is_renamed(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                path = self.scratch / "release" / "current"
                if existing:
                    _ = build_hold.open_cycle(self.now)
                else:
                    path.unlink(missing_ok=True)
                old = path.read_bytes() if path.exists() else b""
                replace = os.replace
                seen: list[bytes] = []

                def rename(source: Path, destination: Path) -> None:
                    if destination == path:
                        self.assertEqual(path.read_bytes() if path.exists() else b"", old)
                        new = source.read_bytes()
                        self.assertRegex(new.decode(), r"^[0-9a-f]{32}\n$")
                        self.assertEqual(source, path.with_name("current.tmp"))
                        self.assertTrue((path.parent / new.decode().strip() / "cycle.json").is_file())
                        seen.append(new)
                    replace(source, destination)

                with mock.patch.object(os, "replace", side_effect=rename):
                    cycle = build_hold.open_cycle(self.now)
                self.assertEqual(seen, [(cycle["id"] + "\n").encode()])
                self.assertEqual(path.read_bytes(), seen[0])


    def test_holder_file_is_whole_at_the_moment_it_is_renamed(self) -> None:
        folder = self.scratch / "holders"
        for existing in (False, True):
            with self.subTest(existing=existing):
                path = build_hold.holder_path(folder, "holder")
                if existing:
                    _ = build_hold.write_hold(folder, build_hold.HolderIdentity("holder", "holder"), "old purpose", build_hold.NoReleaseEta(), self.now)
                else:
                    path.unlink(missing_ok=True)
                old = path.read_bytes() if path.exists() else b""
                old_files = list(folder.iterdir()) if folder.exists() else []
                replace = os.replace
                seen: list[Path] = []

                def rename(source: Path, destination: Path) -> None:
                    self.assertEqual(destination, path)
                    self.assertEqual(path.read_bytes() if path.exists() else b"", old)
                    self.assertEqual(list(folder.iterdir()), old_files)
                    self.assertEqual(source, folder.parent / f"{folder.name}.{path.name}.tmp")
                    content = cast(object, json.loads(source.read_text()))
                    expected: dict[str, object] = {"holder": "holder", "since": self.now.isoformat(), "for": "new purpose", "release_eta": "unknown", "keep": []}
                    self.assertEqual(content, expected)
                    seen.append(source)
                    replace(source, destination)

                with mock.patch.object(os, "replace", side_effect=rename):
                    _ = build_hold.write_hold(folder, build_hold.HolderIdentity("holder", "holder"), "new purpose", build_hold.NoReleaseEta(), self.now)
                self.assertEqual(seen, [folder.parent / f"{folder.name}.{path.name}.tmp"])
                self.assertEqual(build_hold.read_holder(path).purpose, "new purpose")


class HoldEventRecord(TypedDict):
    target: str
    event: str
    hold_key: str
    holder: str
    reason: str
    at: str
    until: str


class HoldRecordingTests(IsolatedBuildHoldTest):
    def events(self, expected: int = 1) -> list[HoldEventRecord]:
        return recorded_hold_events(self.scratch / "buildlog", expected)

    def begin(self, key: str = "session-id", name: str = "old name") -> Path:
        folder = self.scratch / "holders"
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity(key, name), "render checks",
                                  build_hold.NoReleaseEta(), datetime.now(timezone.utc))
        return folder

    def test_locked_log_does_not_delay_hold_or_keep_the_release_lock(self) -> None:
        log = self.scratch / "buildlog" / socket.gethostname().split(".")[0] / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
        log.parent.mkdir(parents=True)
        with log.open("a+") as held_log:
            fcntl.flock(held_log, fcntl.LOCK_EX)
            try:
                result = subprocess.run(
                    ["python3", str(SCRIPT), "hold", "--holder", "alice", "--for", "render checks",
                     "--proc-root", str(self.scratch / "proc")],
                    env=dict(os.environ), capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("natedev is held for render checks.", result.stdout)
                self.assertTrue((self.scratch / "holders" / "alice").is_file())
                with (self.scratch / "release" / "release.lock").open("a+") as coordination:
                    fcntl.flock(coordination, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(coordination, fcntl.LOCK_UN)
                self.assertEqual(log.read_text(), "")
            finally:
                fcntl.flock(held_log, fcntl.LOCK_UN)
        self.assertEqual([(row["event"], row["hold_key"]) for row in self.events()],
                         [("began", "alice")])

    def test_recorder_starts_after_release_lock_closes_and_finished_children_are_reaped(self) -> None:
        launched: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def launch(command: list[str], *, stdin: int, stdout: int, stderr: int,
                   start_new_session: bool, close_fds: bool) -> subprocess.Popen[bytes]:
            with (self.scratch / "release" / "release.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
            process = real_popen(command, stdin=stdin, stdout=stdout,
                                 stderr=stderr, start_new_session=start_new_session, close_fds=close_fds)
            launched.append(process)
            self.assertTrue(start_new_session)
            self.assertTrue(close_fds)
            return process

        with mock.patch.object(subprocess, "Popen", side_effect=launch):
            _ = self.begin()
        self.reap_recorders()
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0].returncode, 0)
        with self.assertRaises(ChildProcessError):
            _ = os.waitpid(launched[0].pid, os.WNOHANG)

    def test_no_holder_files_skip_a_damaged_current_record(self) -> None:
        release = self.scratch / "release"
        release.mkdir()
        _ = (release / "current").write_text("damaged")
        with mock.patch.object(build_hold, "read_cycle", side_effect=AssertionError("no files need no cycle")):
            self.assertEqual(build_hold.natedev_holds(self.scratch / "holders"), build_hold.NatedevHolds(()))

    def test_holder_file_without_a_cycle_is_in_force(self) -> None:
        folder = self.scratch / "holders"
        folder.mkdir()
        _ = write_holder(folder, "session-id", "2026-10-12T10:00:00+00:00", "render checks")
        result = build_hold.natedev_holds(folder)
        self.assertEqual([holder.key for holder in result.in_force], ["session-id"])

    def test_released_at_with_matching_since_does_not_end_a_present_hold(self) -> None:
        folder = self.begin()
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["holders"]["session-id"]["released_at"] = datetime.now(timezone.utc).isoformat()
        build_hold.save_cycle(cycle)
        with mock.patch.object(build_hold, "read_holders", wraps=build_hold.read_holders) as read:
            result = build_hold.natedev_holds(folder)
        self.assertEqual(read.call_count, 1)
        self.assertEqual([holder.key for holder in result.in_force], ["session-id"])

    def test_present_holder_and_damaged_cycle_stay_in_force(self) -> None:
        folder = self.begin()
        _ = (self.scratch / "release" / "current").write_text("damaged")
        self.assertEqual([holder.key for holder in build_hold.natedev_holds(folder).in_force], ["session-id"])

    def test_repeated_hold_and_renamed_release_keep_one_begin_and_the_file_key(self) -> None:
        folder = self.begin("unsafe name", "old name")
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("unsafe name", "new name"), "new work",
                                  build_hold.NoReleaseEta(), datetime.now(timezone.utc))
        self.assertEqual(len(self.events()), 1)
        _ = (self.scratch / "release" / "current").unlink()
        _ = build_hold.release_cycle(folder, "unsafe name")
        records = self.events(2)
        self.assertEqual([(row["event"], row["target"], row["hold_key"]) for row in records],
                         [("began", "natedev", "unsafe-name"), ("ended", "natedev", "unsafe-name")])
        self.assertEqual([(row["holder"], row["reason"]) for row in records],
                         [("old name", "render checks"), ("new name", "new work")])
        self.assertTrue(all(datetime.fromisoformat(row["at"].replace("Z", "+00:00")).utcoffset() == timedelta(0)
                            for row in records))
        self.assertFalse((folder / "unsafe-name").exists())

    def test_partial_and_final_release_write_end_events_only_after_file_removal(self) -> None:
        folder = self.begin()
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("other", "other"), "other work",
                                  build_hold.NoReleaseEta(), datetime.now(timezone.utc))
        _ = build_hold.release_cycle(folder, "session-id")
        self.assertFalse((folder / "session-id").exists())
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        build_hold.save_cycle(cycle)
        _ = build_hold.release_cycle(folder, "other")
        self.assertFalse((folder / "other").exists())
        self.assertEqual([(row["event"], row["hold_key"]) for row in self.events(4)],
                         [("began", "session-id"), ("began", "other"), ("ended", "session-id"), ("ended", "other")])

    def test_status_shows_holder_since_and_release_eta_in_user_zone(self) -> None:
        folder = self.scratch / "holders"
        folder.mkdir()
        _ = write_holder(folder, "holder", "2026-10-12T16:00:00+00:00", "render checks", "2026-10-12T17:00:00+00:00")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "America/New_York"}), redirect_stdout(output):
            try:
                time.tzset()
                self.assertEqual(build_hold.main(["status"]), 0)
            finally:
                time.tzset()
        self.assertIn("holder since 12:00 EDT, for render checks - release eta: 13:00 EDT", output.getvalue())

    def test_event_write_failure_keeps_the_hold_notice_and_file(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=OSError("cannot start recorder")):
            folder = self.begin()
        self.assertTrue((folder / "session-id").exists())

class ImmediateReleaseTests(IsolatedBuildHoldTest):
    now: datetime = datetime(2026, 10, 28, tzinfo=timezone.utc)

    def test_interrupted_old_release_keeps_wait_and_local_checks_held_until_unlink(self) -> None:
        import build_targets
        self.begin()
        cycle = self.cycle()
        cycle["holders"]["holder"]["released_at"] = self.now.isoformat()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        config = self.scratch / "targets.conf"
        _ = config.write_text("targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
        events: list[str] = []

        def remove_on_wait(_seconds: float) -> None:
            events.append("waited with file")
            self.assertTrue((self.scratch / "holders/holder").is_file())
            (self.scratch / "holders/holder").unlink()
            events.append("file removed")

        with mock.patch.dict(os.environ, {"BUILD_TARGETS_CONFIG": str(config), "CLAUDE_CODE_SESSION_ID": "waiter"}), \
             mock.patch.object(build_hold, "socket_for") as lookup, \
             mock.patch.object(build_targets, "wait_clock", return_value=self.now), \
             mock.patch.object(build_targets, "wait_sleep", side_effect=remove_on_wait), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with mock.patch.object(build_hold, "release_hold", side_effect=OSError("interrupted before unlink")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    _ = build_hold.release_cycle(self.scratch / "holders", "holder", clock=lambda: self.now)
            self.assertEqual(build_hold.main(["wait"]), 10)
            self.assertEqual(build_targets.command_check_local(), 10)
            self.assertEqual(build_targets.command_register_local("call", pid=123), 10)
            self.assertIn("wait for release", build_hold.register_wait("waiter"))
            self.assertEqual(build_hold.notify_release(self.cycle()), "")
            lookup.assert_not_called()
            self.assertEqual(build_targets.command_wait_local("call", caller="lint"), 0)
            events.append("wait returned")
        self.assertEqual(events, ["waited with file", "file removed", "wait returned"])

    def test_release_unlinks_before_saving_release_fields_and_before_notice(self) -> None:
        self.begin()
        build_hold.record_recipients([("waiting", "one")])
        save = build_hold.save_cycle
        events: list[str] = []

        def saved(cycle: build_hold.HoldCycle) -> None:
            self.assertFalse((self.scratch / "holders/holder").exists())
            self.assertEqual(cycle["holders"]["holder"]["released_at"], self.now.isoformat())
            events.append("started" if cycle["release_started_at"] else "released")
            save(cycle)

        def send(_entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            self.assertFalse((self.scratch / "holders/holder").exists())
            self.assertIn("started", events)
            events.append("notice")
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "save_cycle", side_effect=saved), \
             mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/fake")), \
             mock.patch.object(build_hold, "send_release", side_effect=send):
            _ = build_hold.release_cycle(self.scratch / "holders", "holder", clock=lambda: self.now)
            _ = build_hold.notify_release(self.cycle())
        self.assertEqual(events[:2], ["released", "started"])
        self.assertEqual(events.count("notice"), 1)

    def test_same_session_rehold_resets_release_and_adds_keep_runs(self) -> None:
        self.begin()
        cycle = self.cycle()
        cycle["holders"]["holder"]["released_at"] = self.now.isoformat()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        for pid in (10, 20):
            _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("holder", "holder"),
                                      "again", build_hold.NoReleaseEta(), self.now,
                                      (build_hold.ProcessIdentity(pid, str(pid * 10)),))
        self.assertEqual(self.cycle()["release_started_at"], "")
        self.assertEqual(self.cycle()["holders"]["holder"]["released_at"], "")
        self.assertIn("wait for release", build_hold.register_wait("waiter"))
        holder, = build_hold.natedev_holds(self.scratch / "holders").in_force
        self.assertEqual(holder.keep, (build_hold.ProcessIdentity(10, "100"), build_hold.ProcessIdentity(20, "200")))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(build_hold.main(["status"]), 0)
        self.assertIn("kept by holder: pid 10, start 100", output.getvalue())
        self.assertIn("kept by holder: pid 20, start 200", output.getvalue())

    def begin(self, name: str = "holder") -> None:
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity(name, name),
                                  "tests", build_hold.NoReleaseEta(), self.now)

    def cycle(self) -> build_hold.HoldCycle:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle

    def test_release_never_waits_on_an_idle_waiter(self) -> None:
        self.begin()
        for session in ("idle", "ready"):
            self.assertIn("wait for release", build_hold.register_wait(session, session))
        sent: list[str] = []

        def send(entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            self.assertFalse((self.scratch / "holders/holder").exists())
            sent.append(entry["session_id"])
            # Neither session acknowledges or reports that its turn ended.
            return build_hold.ReleaseDelivered()

        with mock.patch.object(time, "sleep", side_effect=AssertionError("release must not sleep")), \
             mock.patch.object(build_hold, "wait_for_quiet", side_effect=AssertionError("release must not wait")), \
             mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test")), \
             mock.patch.object(build_hold, "send_release", side_effect=send):
            result = build_hold.release_cycle(self.scratch / "holders", "holder", clock=lambda: self.now)
            self.assertEqual(result, "released, builds may resume.")
            self.assertFalse((self.scratch / "holders/holder").exists())
            self.assertCountEqual(sent, ["idle", "ready"])
            self.assertEqual(build_hold.notify_release(self.cycle()), "")
            self.assertCountEqual(sent, ["idle", "ready"])

    def test_last_release_removes_holder_before_each_notice_and_never_sends_twice(self) -> None:
        self.begin()
        build_hold.record_recipients([("same name", "one"), ("same name", "two")])
        _ = build_hold.register_wait("one")
        _ = build_hold.register_wait("two")
        sent: list[str] = []

        def send(entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            self.assertFalse((self.scratch / "holders" / "holder").exists())
            self.assertTrue(self.cycle()["holders"]["holder"]["released_at"])
            sent.append(entry["session_id"])
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=send):
            self.assertEqual(build_hold.release_cycle(self.scratch / "holders", "holder"), "released, builds may resume.")
            self.assertEqual(build_hold.notify_release(self.cycle()), "")
        self.assertEqual(sent, ["one", "two"])
        self.assertEqual([row["event"] for row in recorded_hold_events(self.scratch / "buildlog", 2)], ["began", "ended"])

    def test_failed_queued_and_gone_notices_are_named_and_not_retried(self) -> None:
        self.begin()
        build_hold.record_recipients([("failed", "a"), ("queued", "b"), ("gone", "c"), ("lookup", "d")])
        with mock.patch.object(build_hold, "socket_for", side_effect=[
            build_hold.RecipientLive("/a"), build_hold.RecipientLive("/b"), build_hold.RecipientGone(),
            build_hold.RecipientLookupFailed("unreadable registry"),
        ]) as lookup, mock.patch.object(build_hold, "send_release", side_effect=[
            build_hold.ReleaseSendFailed("refused"), build_hold.ReleaseQueued(),
        ]) as send:
            result = build_hold.release_cycle(self.scratch / "holders", "holder")
            self.assertEqual(build_hold.notify_release(self.cycle()), result.removeprefix("released, builds may resume."))
        self.assertEqual(lookup.call_count, 4)
        self.assertEqual(send.call_count, 2)
        self.assertIn("Not reached, tell them yourself: failed (send failed), queued (queued, delivery unconfirmed), gone (session gone), lookup (send failed)", result)

    def test_another_in_force_holder_keeps_hold_until_its_release(self) -> None:
        self.begin()
        self.begin("other")
        build_hold.record_recipients([("waiting", "one")])
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as send:
            self.assertIn("released; still held by other", build_hold.release_cycle(self.scratch / "holders", "holder"))
            self.assertTrue((self.scratch / "holders" / "other").is_file())
            send.assert_not_called()
            self.assertEqual(build_hold.release_cycle(self.scratch / "holders", "other"), "released, builds may resume.")
            send.assert_called_once()

    def test_old_turn_fields_read_without_changing_status_or_blocking_a_new_hold(self) -> None:
        self.begin()
        cycle = self.cycle()
        path = self.scratch / "release" / cycle["id"] / "cycle.json"
        old = cast(dict[str, object], json.loads(path.read_text()))
        old.update(holder_generation=3, active="one", turn=9)
        old["entries"] = [{"session_id": "one", "name": "old recipient", "state": "WaitingForMemory",
                           "calls": [{"call_id": "old", "admitted_generation": 3}], "turn": {"started_at": "old"},
                           "delivery": {"generation": 3, "outcome": "Delivered"}}]
        _ = path.write_text(json.dumps(old))
        self.assertEqual(self.cycle()["entries"][0]["delivery"], "Delivered")
        self.assertIn("old recipient", " ".join(build_hold.cycle_status_lines(self.cycle())))
        self.assertEqual(json.loads(path.read_text()), old)
        self.begin("new-holder")
        self.assertIn("new-holder", self.cycle()["holders"])

    def test_notice_says_builds_can_start_and_needs_no_answer(self) -> None:
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            _ = build_hold.send_release(build_hold.new_entry("one", "recipient"), "/test")
        arguments = cast(list[str], run.call_args.args[0])
        notice = arguments[arguments.index("--text") + 1]
        self.assertIn("Start your builds", notice)
        self.assertIn("memory gate orders them", notice)
        self.assertIn("No answer is needed", notice)


class BuildAuditTests(IsolatedBuildHoldTest):
    proc: Path = Path()
    holders: Path = Path()
    log: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.proc = self.scratch / "proc"
        boot = self.proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        _ = boot.write_text("boot-one\n")
        self.holders = self.scratch / "holders"
        self.holders.mkdir()
        _ = write_holder(self.holders, "holder", "2026-10-28T12:00:00+00:00", "app test")
        self.log = self.scratch / "buildlog"

    def process(self, pid: int, command: str, *, parent: int = 1, start: int = 0,
                environment: dict[str, str] | None = None, argv: tuple[str, ...] = ()) -> Path:
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ["S", str(parent), *(["0"] * 17), str(start or pid * 10), "0"]
        _ = (directory / "stat").write_text(f"{pid} ({command}) " + " ".join(fields))
        _ = (directory / "comm").write_text(command + "\n")
        _ = (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in (argv or (command, "test"))) + b"\0")
        _ = (directory / "environ").write_bytes(b"\0".join(f"{key}={value}".encode()
                                                          for key, value in (environment or {}).items()) + b"\0")
        if not (directory / "cwd").is_symlink():
            (directory / "cwd").symlink_to(self.scratch)
        return directory

    def admission(self, pid: int, start: str) -> None:
        ledger = self.log / "admission"
        ledger.mkdir(parents=True, exist_ok=True)
        _ = (ledger / f"{pid}.reservation").write_text(json.dumps({"pid": pid, "start_time": start}))

    def builds(self) -> tuple[build_hold.AuditedBuild, ...]:
        return build_hold.audit_builds(self.proc, build_hold.natedev_holds(self.holders).in_force, self.log)

    def rows(self) -> list[dict[str, object]]:
        self.reap_recorders()
        return [cast(dict[str, object], json.loads(line)) for path in self.log.glob("*/hold-builds-*.jsonl")
                for line in path.read_text().splitlines()]

    def test_each_class_and_cargo_nextest_compiler_group(self) -> None:
        _ = self.process(10, "cargo-tile-real", environment={"CLAUDE_CODE_SESSION_ID": "holder"})
        _ = self.process(20, "bash", argv=("bash", "/scripts/direct/verify.sh"))
        self.admission(20, "200")
        _ = self.process(21, "cargo", parent=20, environment={"CLAUDE_CODE_SESSION_ID": "verified"})
        _ = self.process(22, "cargo-nextest", parent=21)
        _ = self.process(23, "rustc", parent=22)
        _ = self.process(24, "rustdoc", parent=22)
        _ = self.process(30, "cargo-nextest", environment={"CLAUDE_CODE_SESSION_ID": "other"})
        _ = self.process(40, "cargo")
        builds = self.builds()
        self.assertEqual([(item.process.identity.pid, item.classification) for item in builds],
                         [(10, "holder"), (21, "admitted"), (30, "other_session"), (40, "no_session")])
        self.assertEqual([item.pid for item in builds[1].children], [22, 23, 24])

    def test_nearest_agent_ancestor_with_unreadable_environment(self) -> None:
        _ = self.process(10, "claude", environment={"CLAUDE_CODE_SESSION_ID": "farther"})
        _ = self.process(20, "codex", parent=10, environment={"CODEX_THREAD_ID": "nearer"})
        _ = self.process(25, "python3", parent=20)
        directory = self.process(30, "cargo", parent=25)
        (directory / "environ").unlink()
        (directory / "environ").mkdir()
        build, = self.builds()
        self.assertEqual(build.owner, build_hold.OwningSession("nearer"))
        self.assertEqual(build.classification, "other_session")

    def test_claude_ancestor_without_session_id_has_process_identity(self) -> None:
        _ = self.process(10, "claude")
        _ = self.process(20, "cargo", parent=10)
        build, = self.builds()
        self.assertEqual(build.owner, build_hold.OwningSession("claude:10:100"))

    def test_worker_run_maps_to_owning_session_without_changing_environment(self) -> None:
        active = self.scratch / "active"
        active.mkdir()
        _ = (active / "holder").write_text("/unit/run\n")
        _ = self.process(10, "cargo", environment={"UNIT_RUN_DIR": "/unit/run", "UNIT_ACTIVE_DIR": str(active)})
        build, = self.builds()
        self.assertEqual(build.owner, build_hold.OwningSession("holder"))
        self.assertEqual(build.classification, "holder")
        self.assertEqual(os.environ["UNIT_RUN_DIR"], "")

    def test_session_environment_precedes_worker_mapping(self) -> None:
        _ = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "direct", "UNIT_RUN_DIR": "/unit/run"})
        build, = self.builds()
        self.assertEqual(build.owner, build_hold.OwningSession("direct"))

    def test_second_holder_means_caller_does_not_hold_only_its_own(self) -> None:
        _ = write_holder(self.holders, "second", "2026-10-28T12:00:00+00:00", "app test")
        _ = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "holder"})
        build, = self.builds()
        self.assertEqual(build.classification, "other_session")

    def test_reused_pid_does_not_match_admission_or_new_parent(self) -> None:
        _ = self.process(10, "claude", start=400, environment={"CLAUDE_CODE_SESSION_ID": "new-parent"})
        _ = self.process(20, "cargo", parent=10, start=200)
        self.admission(20, "199")
        build, = self.builds()
        self.assertEqual(build.classification, "no_session")
        self.assertEqual(build.owner, build_hold.NoOwningSession())

    def test_pid_reused_during_environment_read_is_discarded(self) -> None:
        directory = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "old"})
        read_bytes = Path.read_bytes

        def read(path: Path) -> bytes:
            data = read_bytes(path)
            if path == directory / "environ":
                _ = (directory / "stat").write_text("10 (cargo) S 1 " + "0 " * 17 + "101 0")
            return data

        with mock.patch.object(Path, "read_bytes", read):
            self.assertEqual(self.builds(), ())

    def test_orphan_compilers_are_visible_and_missing_environment_is_no_session(self) -> None:
        directory = self.process(10, "rustc")
        (directory / "environ").unlink()
        _ = self.process(20, "rustdoc", environment={"CLAUDE_CODE_SESSION_ID": "docs"})
        self.assertEqual([item.classification for item in self.builds()], ["no_session", "other_session"])

    def test_unadmitted_verify_environment_is_not_an_admission(self) -> None:
        _ = self.process(10, "cargo", environment={"BUILDLOG_CALLER": "verify", "BUILDLOG_CALL_ID": "call",
                                                   "CLAUDE_CODE_SESSION_ID": "other"})
        build, = self.builds()
        self.assertEqual(build.classification, "other_session")

    def test_build_scope_marks_admission_without_memory_reservation(self) -> None:
        directory = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "verified"})
        _ = (directory / "cgroup").write_text("0::/user.slice/user-1000.slice/builds.slice/run-123.scope\n")
        build, = self.builds()
        self.assertEqual(build.classification, "admitted")
        _ = (directory / "cgroup").write_text("0::/user.slice/not-builds.slice/run-123.scope\n")
        build, = self.builds()
        self.assertEqual(build.classification, "other_session")

    def test_two_passes_record_once_per_build_per_hold_and_new_pid_lifetime(self) -> None:
        _ = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "holder"})
        _ = self.process(20, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "verified"})
        self.admission(20, "200")
        _ = self.process(30, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "other"})
        _ = self.process(40, "cargo")
        _ = build_hold.audit_hold(self.holders, self.proc)
        builds = build_hold.audit_hold(self.holders, self.proc)
        rows = self.rows()
        self.assertEqual(sorted((row["pid"], row["class"]) for row in rows),
                         [(30, "other_session"), (40, "no_session")])
        self.assertTrue(all(row["boot_id"] == "boot-one" for row in rows))
        self.assertNotIn("pid 40", "\n".join(build_hold.audit_lines(builds)))
        _ = self.process(30, "cargo", start=301, environment={"CLAUDE_CODE_SESSION_ID": "other"})
        _ = build_hold.audit_hold(self.holders, self.proc)
        self.assertEqual(len(self.rows()), 3)
        _ = write_holder(self.holders, "holder", "2026-10-28T13:00:00+00:00", "new app test")
        _ = build_hold.audit_hold(self.holders, self.proc)
        self.assertEqual(len(self.rows()), 5)

    def test_hold_keep_cli_captures_start_time_adds_runs_and_reports_stop_choices(self) -> None:
        _ = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "other"})
        _ = self.process(20, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "other"})
        output = io.StringIO()
        with redirect_stdout(output):
            for pid in (10, 20):
                self.assertEqual(build_hold.main(["hold", "--holder", "holder", "--for", "app test",
                                                 "--proc-root", str(self.proc), "--keep", str(pid)]), 0)
            self.assertEqual(build_hold.main(["hold", "--holder", "holder", "--for", "app test",
                                             "--proc-root", str(self.proc), "--keep", "999"]), 1)
        holder, = build_hold.natedev_holds(self.holders).in_force
        self.assertEqual(holder.keep, (build_hold.ProcessIdentity(10, "100"), build_hold.ProcessIdentity(20, "200")))
        self.assertIn("kept: other, pid 10", output.getvalue())
        self.assertIn("--keep 20", output.getvalue())

    def test_hold_status_and_audit_print_builds_and_only_record_no_session(self) -> None:
        _ = self.process(10, "cargo", environment={"CLAUDE_CODE_SESSION_ID": "other"})
        _ = self.process(20, "cargo", argv=("cargo", "check", "--package", "unowned"))
        (self.holders / "holder").unlink()
        for action in ("hold", "status", "audit"):
            args = [action, "--proc-root", str(self.proc)]
            if action == "hold":
                args.extend(["--holder", "holder", "--for", "app test"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(build_hold.main(args), 0)
            self.assertIn("other_session: other, pid 10: cargo test", output.getvalue())
            if action == "audit":
                self.assertNotIn("unowned", output.getvalue())
            else:
                self.assertIn("--keep 20", output.getvalue())
            if action == "hold":
                self.assertIn("tests and builds outside the hold are stopped", output.getvalue())
        self.assertEqual(len(self.rows()), 2)

    def test_audit_read_failure_does_not_fail_hold_or_status(self) -> None:
        missing_proc = self.scratch / "missing-proc"
        for args in (["hold", "--holder", "holder", "--for", "app test"], ["status"]):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(build_hold.main([*args, "--proc-root", str(missing_proc)]), 0)
            self.assertIn("Build audit unavailable:", output.getvalue())
            self.assertTrue((self.holders / "holder").is_file())


if __name__ == "__main__":
    _ = unittest.main()
