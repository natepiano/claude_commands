"""Build holds with temporary holder files and synthetic machine readings."""

from __future__ import annotations

import json
import io
import fcntl
import os
import re
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
from sessions import SessionRecord
from user_zone import user_zone


SCRIPT = Path(__file__).with_name("build_hold.py")
MEMORY_GATE = Path(__file__).resolve().parent.parent / "lint" / "memory_gate.sh"


def write_holder(folder: Path, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
    path = folder / name
    _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
    return path


def recorded_hold_events(root: Path, expected: int) -> list[HoldEventRecord]:
    deadline = time.monotonic() + 5
    while True:
        rows = [cast(HoldEventRecord, json.loads(line))
                for path in root.rglob("*.jsonl") for line in path.read_text().splitlines()]
        if len(rows) >= expected or time.monotonic() >= deadline:
            return sorted(rows, key=lambda row: (row["at"], row["event"] != "began"))
        time.sleep(0.01)


class IsolatedBuildHoldTest(unittest.TestCase):
    scratch: Path = Path()

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        meminfo = self.scratch / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch),
            "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.scratch / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.scratch / "registration"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_ADMISSION_FILE": "",
            "BUILDLOG_HOLD_STARTED_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "MAC_TEST_STATE_DIR": str(self.scratch / "mac-state"),
            "BUILDLOG_MEMINFO": str(meminfo),
            # No test reads the machine's own session records, or passes for the session that runs it.
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "",
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.scratch / "active"),
            "PLAN_DELEGATE_BOARD_DIR": "",
            "PLAN_DELEGATE_SESSION_DIR": "",
        }))
        self.addCleanup(self.reap_recorders)

    def reap_recorders(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "hold-event-recorder":
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())


class ImportTests(IsolatedBuildHoldTest):
    def test_helper_imports_from_its_sibling_directory(self) -> None:
        self.assertEqual(build_hold.__name__, "build_hold")


class OwningSessionTests(IsolatedBuildHoldTest):
    active: Path = Path()
    delegate_run: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.active = self.scratch / "active"
        self.active.mkdir()
        self.delegate_run = self.scratch / "run"

    def marker(self, session_id: str, run: Path) -> Path:
        marker = self.active / session_id
        _ = marker.write_text(str(run) + "\n")
        return marker

    def test_board_marker_wins_over_inherited_session_id(self) -> None:
        _ = self.marker("run-owner", self.delegate_run)
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "CLAUDE_CODE_SESSION_ID": "stale-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_board_directory_takes_priority_over_session_directory(self) -> None:
        other_run = self.scratch / "other-run"
        _ = self.marker("board-owner", self.delegate_run)
        newer = self.marker("session-owner", other_run)
        os.utime(newer, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "PLAN_DELEGATE_SESSION_DIR": str(other_run),
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("board-owner"))

    def test_marker_content_ignores_surrounding_whitespace(self) -> None:
        marker = self.marker("run-owner", self.delegate_run)
        _ = marker.write_text(f" \t{self.delegate_run}\n \t")
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_session_directory_is_used_when_board_directory_is_unset_or_empty(self) -> None:
        _ = self.marker("run-owner", self.delegate_run)
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_SESSION_DIR": str(self.delegate_run)}):
            for board_is_set in (False, True):
                with self.subTest(board_is_set=board_is_set):
                    if board_is_set:
                        os.environ["PLAN_DELEGATE_BOARD_DIR"] = ""
                    else:
                        _ = os.environ.pop("PLAN_DELEGATE_BOARD_DIR", None)
                    self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_no_matching_marker_falls_back_to_shell_session(self) -> None:
        _ = self.marker("another-owner", self.scratch / "another-run")
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_no_run_uses_shell_session_without_marker_lookup(self) -> None:
        _ = self.marker("another-owner", self.delegate_run)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "shell-session"}):
            with mock.patch.object(Path, "iterdir", side_effect=AssertionError("No run needs no marker lookup")):
                self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_no_matching_marker_and_no_shell_session_has_no_owner(self) -> None:
        _ = self.marker("another-owner", self.scratch / "another-run")
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.NoOwningSession())

    def test_no_run_and_no_shell_session_has_no_owner(self) -> None:
        self.assertEqual(build_hold.owning_session(), build_hold.NoOwningSession())

    def test_newest_matching_marker_wins(self) -> None:
        older = self.marker("z-earlier-owner", self.delegate_run)
        newer = self.marker("a-later-owner", self.delegate_run)
        _ = self.marker("other-run-owner", self.scratch / "other-run")
        os.utime(older, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        os.utime(newer, ns=(1_100_000_000_000_000_000, 1_100_000_000_000_000_000))
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("a-later-owner"))

    def test_missing_active_directory_is_skipped(self) -> None:
        missing = self.scratch / "missing-active"
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_ACTIVE_DIR": str(missing),
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))
        self.assertFalse(missing.exists())

    def test_unreadable_active_directory_is_skipped(self) -> None:
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            with mock.patch.object(Path, "iterdir", side_effect=PermissionError("active directory denied")):
                self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_unreadable_marker_is_skipped(self) -> None:
        _ = self.marker("run-owner", self.delegate_run)
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("marker denied")):
                self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_invalid_marker_encoding_does_not_hide_readable_marker(self) -> None:
        _ = (self.active / "invalid-marker").write_bytes(b"\xff")
        _ = self.marker("run-owner", self.delegate_run)
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run)}):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("run-owner"))

    def test_marker_subdirectories_are_skipped(self) -> None:
        (self.active / "not-a-marker").mkdir()
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(self.delegate_run),
            "CLAUDE_CODE_SESSION_ID": "shell-session",
        }):
            self.assertEqual(build_hold.owning_session(), build_hold.OwningSession("shell-session"))

    def test_owner_results_are_frozen(self) -> None:
        for owner in (build_hold.OwningSession("run-owner"), build_hold.NoOwningSession()):
            with self.subTest(owner=type(owner).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(owner, "session_id", "changed")


class OwningSessionMarkTests(IsolatedBuildHoldTest):
    @override
    def setUp(self) -> None:
        super().setUp()
        active = self.scratch / "active"
        active.mkdir()
        run = self.scratch / "run"
        _ = (active / "run-owner").write_text(str(run) + "\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": str(run),
            "CLAUDE_CODE_SESSION_ID": "stale-session",
        }))
        now = datetime.now().astimezone()
        folder = self.scratch / "holders"
        folder.mkdir()
        _ = write_holder(folder, "holder", now.isoformat(), "admission test")
        cycle = build_hold.open_cycle(now)
        cycle["holders"] = {"holder": {"since": now.isoformat(), "released_at": ""}}
        cycle["release_started_at"] = now.isoformat()
        cycle["recipients"] = {"run-owner": "owner", "stale-session": "stale"}
        cycle["entries"] = [build_hold.new_entry("run-owner", "owner"), build_hold.new_entry("stale-session", "stale")]
        for entry in cycle["entries"]:
            build_hold.store_release_state(entry, build_hold.ReleasedAwaitingAdmission(now))
        build_hold.save_cycle(cycle)

    def entries(self) -> list[build_hold.ReleaseEntry]:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle["entries"]

    def command(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = build_hold.main(list(arguments))
        return code, output.getvalue().strip()

    def test_mark_without_explicit_id_credits_owner_for_wait_and_return(self) -> None:
        self.assertEqual(self.command("mark", "--state", "WaitingForMemory"),
                         (0, "run-owner: WaitingForMemory"))
        self.assertEqual([entry["state"] for entry in self.entries()],
                         ["WaitingForMemory", "ReleasedAwaitingAdmission"])
        self.assertEqual(self.command("mark", "--state", "MemoryGateReturned", "--outcome", "Granted"),
                         (0, "run-owner: MemoryGateReturned"))
        self.assertEqual([entry["state"] for entry in self.entries()],
                         ["MemoryGateReturned", "ReleasedAwaitingAdmission"])
        self.assertEqual(self.entries()[0].get("outcome"), "Granted")

    def test_explicit_mark_id_wins_without_resolving_owner(self) -> None:
        with mock.patch.object(build_hold, "owning_session", side_effect=AssertionError("Explicit id wins")):
            self.assertEqual(self.command("mark", "--session-id", "stale-session", "--state", "WaitingForMemory"),
                             (0, "stale-session: WaitingForMemory"))
        self.assertEqual([entry["state"] for entry in self.entries()],
                         ["ReleasedAwaitingAdmission", "WaitingForMemory"])

    def test_explicit_empty_mark_id_does_not_use_owner(self) -> None:
        self.assertEqual(self.command("mark", "--session-id", "", "--state", "WaitingForMemory"),
                         (0, "NoSessionId"))
        self.assertEqual([entry["state"] for entry in self.entries()],
                         ["ReleasedAwaitingAdmission", "ReleasedAwaitingAdmission"])

    def test_mark_without_owner_prints_no_session_and_keeps_cycle_unchanged(self) -> None:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        path = self.scratch / "release" / cycle["id"] / "cycle.json"
        before = path.read_bytes()
        with mock.patch.dict(os.environ, {
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
            "CLAUDE_CODE_SESSION_ID": "", "BUILDLOG_CALL_ID": "unowned-call",
        }):
            self.assertEqual(self.command("mark", "--state", "MemoryGateReturned", "--outcome", "Granted"),
                             (0, "NoSessionId"))
        self.assertEqual(path.read_bytes(), before)

    def test_wait_after_release_admits_shell_session_without_an_entry(self) -> None:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["entries"] = []
        build_hold.save_cycle(cycle)
        code, output = self.command("wait")
        self.assertEqual(code, 0)
        self.assertIn("release under way; stale-session builds now", output)
        self.assertEqual(self.entries(), [])

    def test_nothing_to_build_still_answers_for_shell_session(self) -> None:
        code, output = self.command("nothing-to-build")
        self.assertEqual(code, 0)
        self.assertIn("[stale-session]: NothingToBuild", output)
        self.assertEqual([entry["state"] for entry in self.entries()],
                         ["ReleasedAwaitingAdmission", "NothingToBuild"])


class HolderTests(IsolatedBuildHoldTest):
    def test_no_holder_and_legacy_file_with_iso_instant(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch) / "holders"
            self.assertIsInstance(build_hold.read_holders(folder), build_hold.NoHolders)
            folder.mkdir()
            _ = (folder / "old-seat").write_text("old seat, 2026-10-04T10:56:00-07:00, the focused test\n")
            state = build_hold.read_holders(folder)
            self.assertIsInstance(state, build_hold.ActiveHolders)
            assert isinstance(state, build_hold.ActiveHolders)
            self.assertEqual(len(state.holders), 1)
            self.assertEqual(state.holders[0].name, "old-seat")
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
    def test_long_username_cargo_keeps_quiet_check_busy(self) -> None:
        user = "natepiano"

        def fake_ps(command: list[str], *, text: bool) -> str:
            self.assertTrue(text)
            self.assertEqual(command[:2], ["ps", "-eo"])
            shown_user = user if "user:32=" in command[2] else user[:7] + "+"
            return f"77 {shown_user} 2.0 cargo\n"

        with mock.patch.object(subprocess, "check_output", side_effect=fake_ps):
            processes = build_hold.read_processes()
        verdict = build_hold.quiet_verdict(1.0, build_hold.KnownCores(32), processes, user)
        self.assertIsInstance(verdict, build_hold.Busy)
        assert isinstance(verdict, build_hold.Busy)
        self.assertIn("1 cargo", "; ".join(verdict.reasons))
        high_load = build_hold.quiet_verdict(8.0, build_hold.KnownCores(32), processes, user)
        assert isinstance(high_load, build_hold.Busy)
        self.assertIn("natepiano cargo", "; ".join(high_load.reasons))

    def test_quiet_requires_both_low_load_and_no_user_build(self) -> None:
        others = [build_hold.Process(41, "ci", "cargo", 80.0)]
        self.assertIsInstance(build_hold.quiet_verdict(7.99, build_hold.KnownCores(32), others, "nate"), build_hold.Quiet)
        self.assertIsInstance(build_hold.quiet_verdict(8.0, build_hold.KnownCores(32), others, "nate"), build_hold.Busy)
        own = [build_hold.Process(42, "nate", "rustc", 12.0)]
        self.assertIsInstance(build_hold.quiet_verdict(1.0, build_hold.KnownCores(32), own, "nate"), build_hold.Busy)
        nextest = [build_hold.Process(43, "nate", "cargo-nextest", 1.0)]
        self.assertIsInstance(build_hold.quiet_verdict(1.0, build_hold.KnownCores(32), nextest, "nate"), build_hold.Busy)

    def test_unknown_cores_names_uncertainty_while_known_cores_are_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": scratch}):
            unknown = build_hold.wait_for_quiet(
                0, clock=lambda: 0.0, load=lambda: 1.0,
                cores=lambda: build_hold.UnknownCores(), processes=lambda: [], user="nate",
            )
            known = build_hold.wait_for_quiet(
                0, clock=lambda: 0.0, load=lambda: 1.0,
                cores=lambda: build_hold.KnownCores(32), processes=lambda: [], user="nate",
            )
        self.assertEqual(unknown, build_hold.Busy(("core count unavailable, so the load limit cannot be judged",)))
        self.assertIsInstance(known, build_hold.Quiet)

    def test_cpu_count_is_converted_to_a_core_state(self) -> None:
        with tempfile.TemporaryDirectory() as scratch, mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": scratch}):
            with mock.patch.object(os, "cpu_count", return_value=None):
                self.assertIsInstance(build_hold.read_cores(), build_hold.UnknownCores)
            with mock.patch.object(os, "cpu_count", return_value=32):
                self.assertEqual(build_hold.read_cores(), build_hold.KnownCores(32))

    def test_own_builds_are_grouped_by_command_largest_first(self) -> None:
        processes = [
            *[build_hold.Process(pid, "nate", "rustc", 1.0) for pid in range(34)],
            *[build_hold.Process(pid, "nate", "cargo-nextest", 1.0) for pid in range(34, 37)],
            *[build_hold.Process(pid, "nate", "cargo", 1.0) for pid in range(37, 43)],
            build_hold.Process(43, "ci", "rustc", 1.0),
        ]
        verdict = build_hold.quiet_verdict(1.0, build_hold.KnownCores(32), processes, "nate")
        self.assertEqual(verdict, build_hold.Busy(("your builds still running: 34 rustc, 6 cargo, 3 cargo-nextest",)))

    def test_high_load_for_entire_wait_names_ci_and_other_session(self) -> None:
        elapsed = 0.0
        sleeps: list[float] = []
        running = [
            build_hold.Process(101, "ci", "rustc", 91.0),
            build_hold.Process(102, "other", "cargo", 55.0),
        ]

        def clock() -> float:
            return elapsed

        def sleep(seconds: float) -> None:
            nonlocal elapsed
            sleeps.append(seconds)
            elapsed += seconds

        verdict = build_hold.wait_for_quiet(
            600,
            clock=clock,
            sleep=sleep,
            load=lambda: 9.0,
            cores=lambda: build_hold.KnownCores(32),
            processes=lambda: running,
            user="nate",
        )
        self.assertIsInstance(verdict, build_hold.Busy)
        assert isinstance(verdict, build_hold.Busy)
        self.assertEqual(elapsed, 600)
        self.assertEqual(sleeps, [10] * 60)
        reasons = "; ".join(verdict.reasons)
        self.assertIn("ci rustc", reasons)
        self.assertIn("other cargo", reasons)
        output = io.StringIO()
        with mock.patch.object(build_hold, "wait_for_quiet", return_value=verdict), redirect_stdout(output):
            self.assertEqual(build_hold.main(["quiet", "--max-wait", "600"]), 1)
        self.assertIn("ci rustc", output.getvalue())
        self.assertIn("other cargo", output.getvalue())

    def test_own_cargo_remains_busy_even_after_load_falls(self) -> None:
        verdict = build_hold.wait_for_quiet(
            0,
            clock=lambda: 0.0,
            sleep=lambda seconds: self.fail(f"unexpected sleep: {seconds}"),
            load=lambda: 1.0,
            cores=lambda: build_hold.KnownCores(32),
            processes=lambda: [build_hold.Process(77, "nate", "cargo", 2.0)],
            user="nate",
        )
        self.assertIsInstance(verdict, build_hold.Busy)
        assert isinstance(verdict, build_hold.Busy)
        self.assertIn("cargo", "; ".join(verdict.reasons))


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
        with mock.patch.object(subprocess, "run", side_effect=command), redirect_stdout(output), redirect_stderr(errors):
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
        return subprocess.run(
            ["python3", str(SCRIPT), *args],
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
            waited = self.run_cli(self.scratch / "holders", "wait")
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

    def test_wait_mark_and_recipient_set_aside_damaged_record(self) -> None:
        commands = (
            ("wait",),
            ("mark", "--session-id", "first", "--state", "MemoryGateReturned", "--outcome", "Granted"),
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
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assert_record_set_aside(path, content, result.stdout, scratch)
                    if command[0] == "wait":
                        self.assertIn("no hold cycle: wait for the release broadcast", result.stdout)

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

    def test_idle_mark_wait_and_recipient_record_leave_release_directory_absent(self) -> None:
        release = self.scratch / "release"
        self.assertFalse(release.exists())
        for arguments in (
            ("mark", "--session-id", "idle", "--state", "MemoryGateReturned", "--outcome", "Granted"),
            ("wait",),
            ("record-recipient", "--session-id", "idle", "--name", "idle session"),
        ):
            with self.subTest(arguments=arguments), mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "idle"}):
                result = self.run_cli(self.scratch / "holders", *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(release.exists())

    def test_idle_memory_gate_mark_does_not_spawn_python(self) -> None:
        bin_dir = self.scratch / "bin"
        bin_dir.mkdir()
        called = self.scratch / "python-called"
        python = bin_dir / "python3"
        _ = python.write_text('#!/usr/bin/env bash\ntouch "$PYTHON_CALLED"\n')
        python.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; build_hold_mark MemoryGateReturned Granted', "bash", str(MEMORY_GATE)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                 "CLAUDE_CODE_SESSION_ID": "idle", "PYTHON_CALLED": str(called)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(called.exists())
        self.assertFalse((self.scratch / "release").exists())

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
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertEqual(len(recorded_hold_events(self.scratch / "buildlog", 1)), 1)

    def test_recipient_wait_command_registers_its_session_id(self) -> None:
        folder = self.scratch / "holders"
        held = self.run_cli(folder, "hold", "--holder", "holder", "--for", "the focused test")
        self.assertEqual(held.returncode, 0, held.stderr)
        received = subprocess.run(
            ["python3", str(SCRIPT), "wait"], capture_output=True, text=True, check=False,
            env={**os.environ, "CLAUDE_CODE_SESSION_ID": "received-session"},
        )
        self.assertEqual(received.returncode, 0, received.stderr)
        self.assertIn("registered received-session", received.stdout)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual([entry["session_id"] for entry in cycle["entries"]], ["received-session"])
        self.assertEqual(cycle["entries"][0]["state"], "AwaitingRelease")

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


class ReleaseStateTests(IsolatedBuildHoldTest):
    def begin(self, *session_ids: str, recipients: tuple[tuple[str, str], ...] = ()) -> datetime:
        now = datetime.now().astimezone()
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("holder", "holder"), "the test", build_hold.NoReleaseEta(), now)
        build_hold.record_recipients([("shared name", session_id) for session_id in session_ids])
        for session_id in session_ids:
            self.assertIn("registered", build_hold.register_wait(session_id))
        build_hold.record_recipients(list(recipients))
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = now.isoformat()
        build_hold.save_cycle(cycle)
        return now

    def entries(self) -> list[build_hold.ReleaseEntry]:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle["entries"]

    def advance(self, now: datetime) -> tuple[bool, str]:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return build_hold.advance_release(cycle, now, clock=lambda: now)

    def test_same_name_sessions_receive_distinct_direct_sends_in_arrival_order(self) -> None:
        now = self.begin("session-a", "session-b")
        sent: list[tuple[str, str]] = []

        def deliver(entry: build_hold.ReleaseEntry, socket: str) -> build_hold.ReleaseDelivered:
            sent.append((entry["session_id"], socket))
            return build_hold.ReleaseDelivered()

        def socket_for(session_id: str) -> build_hold.RecipientLive:
            return build_hold.RecipientLive(f"/tmp/{session_id}.sock")

        with mock.patch.object(build_hold, "socket_for", side_effect=socket_for), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            complete, _ = self.advance(now)
            self.assertFalse(complete)
            self.assertEqual(sent, [("session-a", "/tmp/session-a.sock")])
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            self.assertIn("MemoryGateReturned", build_hold.mark_gate("session-a", "MemoryGateReturned", "Granted"))
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            complete, detail = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1))
            self.assertFalse(complete)
            self.assertEqual(len(sent), 1)
            self.assertIn("session-b", detail)
            self.assertIn("at ", detail)
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                self.assertEqual(build_hold.main(["status"]), 0)
            self.assertIn("next shared name [session-b] at ", status_output.getvalue())
            complete, _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertFalse(complete)
            self.assertEqual(sent, [
                ("session-a", "/tmp/session-a.sock"),
                ("session-b", "/tmp/session-b.sock"),
            ])

    def test_delayed_unbound_build_yields_to_next_session_after_one_minute(self) -> None:
        now = self.begin("first", "second")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            sent.append(entry["session_id"])
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            self.assertEqual(sent, ["first"])
            _ = self.advance(now + timedelta(minutes=10))
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.NoAdmissionAck(now))
            self.assertFalse(self.entries()[0]["session_pending"])
            self.assertEqual(sent, ["first", "second"])

    def test_memory_wait_past_five_minutes_still_blocks_next_release(self) -> None:
        now = self.begin("first", "second")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            sent.append(entry["session_id"])
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            _ = build_hold.mark_gate("first", "WaitingForMemory")
            waiting = build_hold.aware_instant(self.entries()[0].get("wait_started_at", ""))
            _ = self.advance(waiting + timedelta(minutes=6))
            self.assertEqual(sent, ["first"])
            self.assertEqual(self.entries()[0]["state"], "WaitingForMemory")
            _ = build_hold.mark_gate("first", "MemoryGateReturned", "TimedOut")
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertEqual(sent, ["first", "second"])

    def test_memory_wait_fallback_starts_after_limit_and_settle(self) -> None:
        now = self.begin("first", "second")
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()), \
             mock.patch.dict(os.environ, {"BUILDLOG_MEM_WAIT_LIMIT_S": "900"}):
            _ = self.advance(now)
            _ = build_hold.mark_gate("first", "WaitingForMemory")
            waiting = build_hold.aware_instant(self.entries()[0].get("wait_started_at", ""))
            deadline = waiting + timedelta(seconds=900 + build_hold.RELEASE_SETTLE_S)
            _ = self.advance(deadline - timedelta(seconds=1))
            self.assertEqual(self.entries()[0]["state"], "WaitingForMemory")
            self.assertEqual(self.entries()[1]["state"], "AwaitingRelease")
            _ = self.advance(deadline)
            self.assertEqual(self.entries()[0]["state"], "NoAdmissionAck")
            self.assertEqual(self.entries()[1]["state"], "ReleasedAwaitingAdmission")

    def test_no_admission_ack_is_not_early(self) -> None:
        now = self.begin("first", "second")
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()):
            _ = self.advance(now)
            released = build_hold.aware_instant(self.entries()[0].get("released_at", ""))
            _ = self.advance(released + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1))
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            self.assertEqual(self.entries()[1]["state"], "AwaitingRelease")
            _ = self.advance(released + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.NoAdmissionAck(released))
            self.assertEqual(self.entries()[1]["state"], "ReleasedAwaitingAdmission")

    def test_unreadable_session_records_are_a_failed_delivery_not_a_gone_session(self) -> None:
        now = self.begin("unknown", "next")

        def socket_for(session_id: str) -> build_hold.RecipientLive | build_hold.RecipientLookupFailed:
            if session_id == "unknown":
                return build_hold.RecipientLookupFailed("sessions: one or more registry files could not be read")
            return build_hold.RecipientLive("/tmp/next.sock")

        errors = io.StringIO()
        with mock.patch.object(build_hold, "socket_for", side_effect=socket_for), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as send, redirect_stderr(errors):
            _ = self.advance(now)
        self.assertEqual(self.entries()[0]["state"], "DeliveryFailed")
        self.assertEqual(self.entries()[1]["state"], "ReleasedAwaitingAdmission")
        self.assertEqual(send.call_count, 1)
        self.assertIn("one or more registry files could not be read", errors.getvalue())

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

    def test_delivery_states_and_gone_recipient_advance_immediately(self) -> None:
        now = self.begin("failed", "gone", "queued")
        sockets = {"failed": build_hold.RecipientLive("/tmp/failed.sock"), "gone": build_hold.RecipientGone(), "queued": build_hold.RecipientLive("/tmp/queued.sock")}
        def socket_for(session_id: str) -> build_hold.RecipientLive | build_hold.RecipientGone:
            return sockets[session_id]

        with mock.patch.object(build_hold, "socket_for", side_effect=socket_for), \
             mock.patch.object(build_hold, "send_release", side_effect=[build_hold.ReleaseSendFailed("send failed"), build_hold.ReleaseQueued()]) as send:
            _ = self.advance(now)
            self.assertEqual(self.entries()[0]["state"], "DeliveryFailed")
            self.assertEqual(self.entries()[1]["state"], "RecipientGone")
            self.assertEqual(self.entries()[2]["state"], "DeliveryQueued")
            self.assertEqual(send.call_count, 2)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(build_hold.main(["status"]), 0)
            for state in ("DeliveryFailed", "RecipientGone", "DeliveryQueued"):
                self.assertIn(state, output.getvalue())
            self.assertIn("MemoryGateReturned", build_hold.mark_gate("queued", "MemoryGateReturned", "MeminfoUnavailable"))
            self.assertEqual(self.entries()[2].get("outcome"), "MeminfoUnavailable")

    def test_registration_after_release_is_admitted_without_an_entry_or_message(self) -> None:
        now = self.begin("first")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            sent.append(entry["session_id"])
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            build_hold.record_recipients([("late", "late")])
            self.assertEqual(build_hold.register_wait("late"), "release under way; late builds now")
            _ = build_hold.mark_gate("first", "MemoryGateReturned", "Granted")
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertEqual(sent, ["first"])
            self.assertEqual([entry["session_id"] for entry in self.entries()], ["first"])

    def test_unregistered_recipient_is_released_after_registered_memory_gate(self) -> None:
        now = self.begin("first", recipients=(("late name", "late"),))
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as send:
            _ = self.advance(now)
            self.assertIn("WaitingForMemory", build_hold.mark_gate("first", "WaitingForMemory"))
            complete, _ = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S))
            self.assertFalse(complete)
            self.assertEqual([entry["session_id"] for entry in self.entries()], ["first"])
            self.assertEqual(send.call_count, 1)
            self.assertIn("MemoryGateReturned", build_hold.mark_gate("first", "MemoryGateReturned", "Granted"))
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1))
            self.assertEqual(send.call_count, 1)
            complete, _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertFalse(complete)
            self.assertEqual([entry["session_id"] for entry in self.entries()], ["first", "late"])
            self.assertEqual(self.entries()[1]["state"], "ReleasedAwaitingAdmission")
            self.assertEqual(send.call_count, 2)
            self.assertTrue((self.scratch / "holders" / "holder").exists())

    def test_delivery_clock_starts_on_delivery_and_queue_clock_on_attempt(self) -> None:
        now = self.begin("first")
        current = now
        outcome: build_hold.ReleaseDelivered | build_hold.ReleaseQueued | build_hold.ReleaseSendFailed = build_hold.ReleaseDelivered()

        def clock() -> datetime:
            return current

        def deliver(_entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered | build_hold.ReleaseQueued | build_hold.ReleaseSendFailed:
            nonlocal current
            current += timedelta(seconds=40)
            return outcome

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            cycle = build_hold.read_cycle()
            assert cycle is not None
            _ = build_hold.advance_release(cycle, now, clock=clock)
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.ReleasedAwaitingAdmission(now + timedelta(seconds=40)))
            _ = build_hold.advance_release(cycle, now + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S), clock=clock)
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            entry = cycle["entries"][0]
            build_hold.store_release_state(entry, build_hold.AwaitingRelease())
            entry["delivery"] = {"generation": 0, "outcome": "NotAttempted"}
            build_hold.save_cycle(cycle)
            current = now + timedelta(minutes=1)
            outcome = build_hold.ReleaseQueued()
            _ = build_hold.advance_release(cycle, current, clock=clock)
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.DeliveryQueued(now + timedelta(minutes=1)))
            build_hold.store_release_state(entry, build_hold.AwaitingRelease())
            entry["delivery"] = {"generation": 0, "outcome": "NotAttempted"}
            build_hold.save_cycle(cycle)
            current = now + timedelta(minutes=2)
            outcome = build_hold.ReleaseSendFailed("send failed")
            _ = build_hold.advance_release(cycle, current, clock=clock)
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.DeliveryFailed(now + timedelta(minutes=2)))

    def test_malformed_release_instant_is_a_read_error(self) -> None:
        _ = self.begin("first")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        for fields in ({}, {"released_at": "not an instant"}, {"released_at": datetime.now().isoformat()},
                       {"released_at": datetime.now().astimezone().isoformat(), "attempted_at": datetime.now().astimezone().isoformat()}):
            with self.subTest(fields=fields):
                cycle["entries"][0] = cast(build_hold.ReleaseEntry, cast(object, {"session_id": "first", "name": "first", "state": "ReleasedAwaitingAdmission", **fields}))
                build_hold.save_cycle(cycle)
                with self.assertRaises(build_hold.ReleaseRecordReadError):
                    _ = build_hold.read_cycle()

    def test_status_keeps_holder_and_reports_damaged_release_record(self) -> None:
        _ = self.begin("first")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["entries"][0] = build_hold.new_entry("first", "first")
        cycle["entries"][0].update({"state": "ReleasedAwaitingAdmission", "released_at": "broken"})
        build_hold.save_cycle(cycle)
        path = self.scratch / "release" / cycle["id"] / "cycle.json"
        before = path.read_bytes()
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = build_hold.main(["status"])
        self.assertEqual(result, 0, errors.getvalue())
        self.assertEqual(errors.getvalue(), "")
        self.assertIn("holder since", output.getvalue())
        self.assertIn("release record could not be read: invalid released_at for first", output.getvalue())
        self.assertIn("/build:hold release sets it aside and ends the hold", output.getvalue())
        self.assertEqual(path.read_bytes(), before)

    def test_new_hold_replaces_damaged_cycle_when_no_holder_remains(self) -> None:
        _ = self.begin("first")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        old_id = cycle["id"]
        cycle["entries"][0] = build_hold.new_entry("first", "first")
        cycle["entries"][0].update({"state": "ReleasedAwaitingAdmission", "released_at": "broken"})
        build_hold.save_cycle(cycle)
        (self.scratch / "holders" / "holder").unlink()
        notice = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("new-holder", "new-holder"), "new test",
                                       build_hold.NoReleaseEta(), datetime.now().astimezone())
        self.assertTrue(notice.startswith("natedev is held for new test."), notice)
        current = build_hold.read_cycle()
        assert current is not None
        self.assertNotEqual(current["id"], old_id)
        self.assertEqual(current["entries"], [])
        self.assertTrue((self.scratch / "holders" / "new-holder").exists())

    def test_new_hold_sets_aside_damaged_cycle_while_holder_remains(self) -> None:
        _ = self.begin("first")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["entries"][0] = build_hold.new_entry("first", "first")
        cycle["entries"][0].update({"state": "ReleasedAwaitingAdmission", "released_at": "broken"})
        build_hold.save_cycle(cycle)
        output = io.StringIO()
        with redirect_stdout(output):
            notice = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("new-holder", "new-holder"), "new test",
                                           build_hold.NoReleaseEta(), datetime.now().astimezone())
        self.assertTrue(notice.startswith("natedev is held for new test."), notice)
        self.assertIn("this hold now releases every session at once", output.getvalue() + notice)
        self.assertIsNone(build_hold.read_cycle())
        self.assertTrue((self.scratch / "holders" / "holder").exists())
        self.assertTrue((self.scratch / "holders" / "new-holder").exists())

    def test_resume_sets_aside_damaged_release_record_and_ends_hold(self) -> None:
        _ = self.begin("first")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["entries"][0] = build_hold.new_entry("first", "first")
        cycle["entries"][0].update({"state": "ReleasedAwaitingAdmission", "released_at": "broken"})
        build_hold.save_cycle(cycle)
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = build_hold.main(["release", "--resume"])
        self.assertEqual(result, 0, errors.getvalue())
        self.assertIn("this hold now releases every session at once", output.getvalue())
        self.assertIn("released, builds may resume.", output.getvalue())
        self.assertFalse((self.scratch / "holders" / "holder").exists())

    def test_old_cycle_registration_and_mark_do_not_reach_new_hold(self) -> None:
        _ = self.begin("old-session")
        old_cycle = build_hold.read_cycle()
        assert old_cycle is not None
        (self.scratch / "holders" / "holder").unlink()
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("new-holder", "new-holder"), "new test",
                                  build_hold.NoReleaseEta(), datetime.now().astimezone())
        current = build_hold.read_cycle()
        assert current is not None
        self.assertNotEqual(current["id"], old_cycle["id"])
        self.assertEqual(current["entries"], [])
        self.assertEqual(build_hold.mark_gate("old-session", "MemoryGateReturned", "Granted"), "mark ignored")
        self.assertIn("registered", build_hold.register_wait("new-session"))
        self.assertEqual([entry["session_id"] for entry in self.entries()], ["new-session"])

    def test_empty_roster_waits_for_named_registration_timeout(self) -> None:
        now = self.begin()
        complete, detail = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S - 1))
        self.assertFalse(complete)
        self.assertIn("waiting for registrations", detail)
        complete, detail = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S))
        self.assertTrue(complete)
        self.assertIn("NoRegistration", detail)

    def test_recorded_recipient_without_wait_receives_release_immediately(self) -> None:
        now = self.begin(recipients=(("late name", "missing-session"),))
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as send:
            complete, detail = self.advance(now)
            self.assertFalse(complete)
            self.assertIn("late name", detail)
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            send.assert_called_once()
            complete, _ = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S))
            self.assertFalse(complete)
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")

    def test_registration_waits_for_release_lock(self) -> None:
        _ = self.begin()
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["release_started_at"] = ""
        build_hold.save_cycle(cycle)
        started = threading.Event()
        finished = threading.Event()

        def register() -> None:
            started.set()
            _ = build_hold.register_wait("late")
            finished.set()

        with build_hold.release_lock():
            worker = threading.Thread(target=register)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.1))
            self.assertEqual(self.entries(), [])
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual([entry["session_id"] for entry in self.entries()], ["late"])

    def test_overlapping_holder_instants_survive_file_removal(self) -> None:
        folder = self.scratch / "holders"
        now = datetime.now().astimezone()
        first_since = now - timedelta(minutes=10)
        second_since = now - timedelta(minutes=5)
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("first", "first"), "first test", build_hold.NoReleaseEta(), first_since)
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("second", "second"), "second test", build_hold.NoReleaseEta(), second_since)
        partial = build_hold.release_cycle(folder, "first")
        self.assertIn("still held by second", partial)
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual(cycle["holders"]["first"]["since"], first_since.isoformat())
        self.assertTrue(cycle["holders"]["first"]["released_at"])
        self.assertEqual(cycle["holders"]["second"]["since"], second_since.isoformat())
        cycle["release_started_at"] = (now - timedelta(minutes=2)).isoformat()
        build_hold.save_cycle(cycle)
        final = build_hold.release_cycle(folder, "second")
        self.assertIn("builds may resume", final)
        after = build_hold.read_cycle()
        assert after is not None
        self.assertTrue(after["holders"]["second"]["released_at"])
        self.assertFalse((folder / "second").exists())

    def test_resumed_release_finishes_and_clears_last_holder(self) -> None:
        now = self.begin("first")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["holders"]["holder"]["released_at"] = (now - timedelta(minutes=2)).isoformat()
        cycle["entries"][0] = build_hold.new_entry("first", "shared name")
        build_hold.store_release_state(cycle["entries"][0], build_hold.MemoryGateReturned(
            now - timedelta(seconds=build_hold.RELEASE_SETTLE_S + 1), "Granted"))
        cycle["entries"][0]["session_pending"] = False
        cycle["entries"][0]["delivery"] = {"generation": cycle.get("holder_generation", 1), "outcome": "Delivered"}
        build_hold.save_cycle(cycle)
        output = io.StringIO()
        with redirect_stdout(output):
            status = build_hold.main(["release", "--resume"])
        self.assertEqual(status, 0)
        self.assertIn("builds may resume", output.getvalue())
        self.assertFalse((self.scratch / "holders" / "holder").exists())

    def test_lookup_and_send_use_session_id_socket(self) -> None:
        calls: list[list[str]] = []

        def command(argv: list[str], *, capture_output: bool, text: bool) -> subprocess.CompletedProcess[str]:
            self.assertTrue(capture_output)
            self.assertTrue(text)
            calls.append(argv)
            if "sessions.py" in argv[1]:
                return subprocess.CompletedProcess(argv, 0, "/tmp/registered.sock\n", "")
            return subprocess.CompletedProcess(argv, 0, "sent\n", "")

        entry = build_hold.new_entry("session-a", "shared name")
        with mock.patch.object(subprocess, "run", side_effect=command):
            socket = build_hold.socket_for("session-a")
            self.assertEqual(socket, build_hold.RecipientLive("/tmp/registered.sock"))
            assert isinstance(socket, build_hold.RecipientLive)
            self.assertEqual(build_hold.send_release(entry, socket.socket), build_hold.ReleaseDelivered())
        self.assertEqual(calls[0][-2:], ["socket", "session:session-a"])
        self.assertIn("--to", calls[1])
        self.assertEqual(calls[1][calls[1].index("--to") + 1], "uds:/tmp/registered.sock")
        self.assertNotIn("shared name", calls[1])
        notice = calls[1][calls[1].index("--text") + 1]
        self.assertTrue(notice.startswith("/build:hold: your hold has been released."), notice)
        self.assertIn("build_hold.py nothing-to-build", notice)

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

    def test_a_session_with_nothing_to_build_releases_the_next_at_once(self) -> None:
        now = self.begin("first", "second")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
            sent.append(entry["session_id"])
            return build_hold.ReleaseDelivered()

        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            self.assertEqual(sent, ["first"])
            answered = subprocess.run(
                ["python3", str(SCRIPT), "nothing-to-build"], capture_output=True, text=True, check=False,
                env={**os.environ, "CLAUDE_CODE_SESSION_ID": "first"},
            )
            self.assertEqual(answered.returncode, 0, answered.stderr)
            self.assertEqual(answered.stdout.strip(),
                             "shared name [first]: NothingToBuild; the release moves on to the next session")
            self.assertEqual(set(self.entries()[0]), {"session_id", "name", "state", "answered_at", "calls", "active_call_id", "active_released_at", "session_pending", "session_admitted_generation", "delivery"})
            _ = build_hold.aware_instant(self.entries()[0].get("answered_at", ""))
            complete, detail = self.advance(now)
            self.assertFalse(complete)
            self.assertEqual(sent, ["first", "second"])
            self.assertIn("second", detail)
            self.assertIn("NothingToBuild", build_hold.answer_nothing_to_build("second"))
            complete, detail = self.advance(now)
            self.assertTrue(complete)
            self.assertEqual(detail, "shared name [first]: NothingToBuild; shared name [second]: NothingToBuild")

    def test_nothing_to_build_answers_only_a_release_still_awaiting_admission(self) -> None:
        now = self.begin("first", "second")
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()):
            _ = self.advance(now)
        self.assertEqual(build_hold.answer_nothing_to_build("unregistered"),
                         "nothing-to-build ignored: this session is not registered")
        self.assertEqual(build_hold.answer_nothing_to_build("second"),
                         "nothing-to-build ignored: shared name [second]: AwaitingRelease")
        self.assertIn("WaitingForMemory", build_hold.mark_gate("first", "WaitingForMemory"))
        self.assertEqual(build_hold.answer_nothing_to_build("first"),
                         "nothing-to-build ignored: shared name [first]: WaitingForMemory")
        self.assertEqual([entry["state"] for entry in self.entries()], ["WaitingForMemory", "AwaitingRelease"])
        self.assertEqual(build_hold.answer_nothing_to_build(""), "NoSessionId")

    def test_a_queued_release_can_answer_nothing_to_build(self) -> None:
        now = self.begin("first", "second")
        with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/tmp/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseQueued()):
            _ = self.advance(now)
            self.assertEqual(self.entries()[0]["state"], "DeliveryQueued")
            self.assertIn("NothingToBuild", build_hold.answer_nothing_to_build("first"))
            _ = self.advance(now)
        self.assertEqual([entry["state"] for entry in self.entries()], ["NothingToBuild", "DeliveryQueued"])


class ReleaseTestMocks(TypedDict):
    lookup: mock.Mock
    send: mock.Mock


class ClosedQueueTests(IsolatedBuildHoldTest):
    now: datetime = datetime.min
    release_mocks: ReleaseTestMocks = {"lookup": mock.Mock(), "send": mock.Mock()}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now().astimezone() - timedelta(hours=1)
        self.release_mocks = {
            "lookup": cast(mock.Mock, self.enterContext(mock.patch.object(
                build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")))),
            "send": cast(mock.Mock, self.enterContext(mock.patch.object(
                build_hold, "send_release", return_value=build_hold.ReleaseDelivered()))),
        }

    def cycle(self) -> build_hold.HoldCycle:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle

    def begin(self, *keys: str, calls: tuple[tuple[str, str], ...] = (), release: bool = True) -> None:
        (self.scratch / "holders" / "holder").unlink(missing_ok=True)
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("holder", "holder"), "closed queue test",
                                  build_hold.NoReleaseEta(), self.now)
        build_hold.record_recipients([(key, key) for key in keys])
        for key in keys:
            _ = build_hold.register_wait(key)
        for key, call_id in calls:
            _ = build_hold.register_wait(key, call_id=call_id)
        if release:
            self.start_release()
        self.release_mocks["lookup"].reset_mock()
        self.release_mocks["send"].reset_mock()

    def start_release(self) -> None:
        cycle = self.cycle()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)

    def entry(self, key: str) -> build_hold.ReleaseEntry:
        return next(entry for entry in self.cycle()["entries"] if entry["session_id"] == key)

    def record_bytes(self) -> bytes:
        return (self.scratch / "release" / self.cycle()["id"] / "cycle.json").read_bytes()

    def set_state(self, key: str, state: build_hold.ReleaseState, *, pending: bool = False) -> None:
        cycle = self.cycle()
        entry = next(entry for entry in cycle["entries"] if entry["session_id"] == key)
        build_hold.store_release_state(entry, state)
        entry["session_pending"] = pending
        build_hold.save_cycle(cycle)

    def mark(self, key: str, state: str, call_id: str = "") -> str:
        with mock.patch.object(build_hold, "datetime", wraps=datetime) as clock:
            cast(mock.Mock, clock.now).return_value = self.now
            return build_hold.mark_gate(key, state, "Granted", call_id=call_id, cycle_id=self.cycle()["id"])

    def advance(self) -> tuple[bool, str]:
        return build_hold.advance_release(self.cycle(), self.now, clock=lambda: self.now)

    def sent(self) -> list[str]:
        calls = cast(list[tuple[tuple[build_hold.ReleaseEntry, str], dict[str, object]]],
                     self.release_mocks["send"].call_args_list)
        return [arguments[0]["session_id"] for arguments, _keywords in calls]

    def test_calls_arriving_through_a_whole_release_never_extend_it(self) -> None:
        self.begin("first", "second", "third")
        started = self.now
        limit = 3 * (build_hold.RELEASE_SETTLE_S + build_hold.RELEASE_POLL_S)
        complete = False
        poll = 0
        while (self.now - started).total_seconds() <= limit:
            complete, _ = self.advance()
            for entry in self.cycle()["entries"]:
                if entry["state"] in ("ReleasedAwaitingAdmission", "DeliveryQueued"):
                    self.assertIn("MemoryGateReturned", self.mark(entry["session_id"], "MemoryGateReturned"))
            before = self.cycle()
            entries = len(before["entries"])
            calls = sum(len(entry["calls"]) for entry in before["entries"])
            for key in (f"new-{poll}", "first", "third"):
                call_id = f"late-{key}-{poll}"
                _ = build_hold.register_wait(key, call_id=call_id)
                answer = build_hold.read_admission(key, call_id)
                self.assertIsInstance(answer, build_hold.AdmittedTurn,
                                      f"poll {poll}, {key}: {answer}; entries {entries}, calls {calls}")
            after = self.cycle()
            self.assertEqual(len(after["entries"]), entries)
            self.assertEqual(sum(len(entry["calls"]) for entry in after["entries"]), calls)
            if complete:
                break
            poll += 1
            self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        elapsed = (self.now - started).total_seconds()
        print(f"Closed release: {elapsed:.1f} seconds, {poll + 1} polls, {len(self.sent())} messages")
        self.assertTrue(complete, f"Release still open after {elapsed:.1f} seconds")
        self.assertEqual(self.sent(), ["first", "second", "third"])
        with mock.patch.object(build_hold, "datetime", wraps=datetime) as clock, \
             mock.patch.object(time, "sleep", side_effect=AssertionError("Release should be complete")):
            cast(mock.Mock, clock.now).return_value = self.now
            _ = build_hold.release_cycle(self.scratch / "holders", "holder")
        self.assertFalse((self.scratch / "holders" / "holder").exists())

    def test_late_call_from_a_new_key_leaves_the_record_bytes_unchanged(self) -> None:
        self.begin("owner")
        before = self.record_bytes()
        self.assertEqual(build_hold.register_wait("late", call_id="late-call"),
                         "release under way; late builds now")
        self.assertIsInstance(build_hold.read_admission("late", "late-call"), build_hold.AdmittedTurn)
        self.assertEqual(self.record_bytes(), before)

    def test_late_call_is_admitted_after_each_finished_state(self) -> None:
        states: tuple[build_hold.ReleaseState, ...] = (
            build_hold.NothingToBuild(self.now), build_hold.MemoryGateReturned(self.now, "Granted"),
            build_hold.NoAdmissionAck(self.now), build_hold.NoRegistration(), build_hold.NoPendingTurn(),
            build_hold.RecipientGone(), build_hold.DeliveryFailed(self.now))
        for state in states:
            with self.subTest(state=type(state).__name__):
                self.begin("owner", "peer")
                self.set_state("owner", state)
                before = self.cycle()
                _ = build_hold.register_wait("owner", call_id="late-call")
                self.assertIsInstance(build_hold.read_admission("owner", "late-call"), build_hold.AdmittedTurn)
                self.assertEqual(self.cycle(), before)

    def test_second_call_from_a_queued_session_builds_at_once_and_its_turn_stays(self) -> None:
        self.begin("first", "second", calls=(("second", "waiting-call"),))
        _ = self.advance()
        before = self.record_bytes()
        _ = build_hold.register_wait("second", call_id="late-call")
        self.assertIsInstance(build_hold.read_admission("second", "late-call"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("second", "waiting-call"), build_hold.AwaitingTurn)
        self.assertIsInstance(build_hold.read_admission("second"), build_hold.AwaitingTurn)
        self.assertEqual(self.record_bytes(), before)
        _ = build_hold.answer_nothing_to_build("first")
        _ = self.advance()
        self.assertEqual(self.entry("second")["active_call_id"], "waiting-call")

    def test_second_call_during_a_bound_turn_builds_at_once_and_adds_no_turn(self) -> None:
        self.begin("owner", "peer", calls=(("owner", "first-call"),))
        _ = self.advance()
        before = self.record_bytes()
        _ = build_hold.register_wait("owner", call_id="second-call")
        self.assertIsInstance(build_hold.read_admission("owner", "second-call"), build_hold.AdmittedTurn)
        self.assertEqual(self.record_bytes(), before)
        _ = self.mark("owner", "MemoryGateReturned", "first-call")
        self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner", "peer"])
        self.assertNotIn("second-call", [call["call_id"] for call in self.entry("owner")["calls"]])

    def test_call_into_an_open_turn_still_takes_it(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("owner", "peer")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                          else build_hold.ReleaseDelivered())
                _ = self.advance()
                self.assertIsInstance(build_hold.read_admission("owner", "new-call"), build_hold.AwaitingTurn)
                self.assertIn("registered", build_hold.register_wait("owner", call_id="new-call"))
                self.assertIsInstance(build_hold.read_admission("owner", "new-call"), build_hold.AdmittedTurn)
                self.assertEqual(self.entry("owner")["active_call_id"], "new-call")
                _ = self.mark("owner", "MemoryGateReturned", "new-call")
                self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner"])
                self.now += timedelta(seconds=1)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner", "peer"])

    def test_call_registered_during_the_hold_keeps_its_turn(self) -> None:
        self.begin("owner", calls=(("owner", "first"), ("owner", "second")))
        _ = self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "first"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("owner", "second"), build_hold.AwaitingTurn)
        _ = self.mark("owner", "MemoryGateReturned", "first")
        self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "second"), build_hold.AdmittedTurn)
        _ = self.mark("owner", "MemoryGateReturned", "second")
        self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        self.assertTrue(self.advance()[0])
        self.assertEqual(self.sent(), ["owner"])

    def test_session_wait_after_the_release_began_adds_no_entry(self) -> None:
        self.begin("owner")
        before = self.record_bytes()
        self.assertEqual(build_hold.register_wait("late"), "release under way; late builds now")
        self.assertEqual(self.record_bytes(), before)

    def test_wait_line_agrees_with_the_admission_answer(self) -> None:
        for case in ("new", "served", "queued-session", "bound-session", "late-call", "listed-call"):
            with self.subTest(case=case):
                self.begin("owner", "peer", calls=(("owner", "active"), ("peer", "waiting")))
                _ = self.advance()
                key, call_id = "owner", ""
                if case == "new":
                    key = "unknown"
                elif case == "served":
                    self.set_state("owner", build_hold.NoAdmissionAck(self.now))
                    cycle = self.cycle()
                    cycle["entries"][0]["active_call_id"] = ""
                    build_hold.save_cycle(cycle)
                elif case in ("queued-session", "late-call", "listed-call"):
                    key = "peer"
                    call_id = "late" if case == "late-call" else "waiting" if case == "listed-call" else ""
                before = self.cycle()
                answer = build_hold.read_admission(key, call_id)
                admitted = case in ("new", "served", "bound-session", "late-call")
                self.assertIsInstance(answer, build_hold.AdmittedTurn if admitted else build_hold.AwaitingTurn)
                line = build_hold.register_wait(key, call_id=call_id)
                ending = "builds now" if admitted else "keeps its queued turn"
                self.assertEqual(line, f"release under way; {key} {ending}")
                self.assertEqual(build_hold.read_admission(key, call_id), answer)
                self.assertEqual(self.cycle(), before)

    def test_launch_from_a_served_or_unknown_session_is_admitted(self) -> None:
        self.begin("owner", "peer")
        self.set_state("owner", build_hold.NoAdmissionAck(self.now))
        for key in ("owner", "unknown"):
            with self.subTest(key=key):
                self.assertIsInstance(build_hold.read_admission(key), build_hold.AdmittedTurn)

    def test_launch_from_a_queued_session_waits_for_its_turn(self) -> None:
        self.begin("owner", "peer")
        _ = self.advance()
        self.assertIsInstance(build_hold.read_admission("peer"), build_hold.AwaitingTurn)
        before = self.record_bytes()
        self.assertEqual(build_hold.register_wait("peer"), "release under way; peer keeps its queued turn")
        self.assertEqual(self.record_bytes(), before)

    def test_recipient_recorded_after_the_release_began_gets_no_turn(self) -> None:
        self.begin("owner")
        before = self.record_bytes()
        build_hold.record_recipients([("late name", "late")])
        self.assertEqual(self.record_bytes(), before)
        _ = self.advance()
        _ = build_hold.answer_nothing_to_build("owner")
        self.assertTrue(self.advance()[0])
        self.assertEqual(self.sent(), ["owner"])

    def test_holder_joining_during_a_release_reopens_the_queue(self) -> None:
        self.begin("owner")
        generation = self.cycle().get("holder_generation", 1)
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("new-holder", "new-holder"), "new hold",
                                  build_hold.NoReleaseEta(), self.now + timedelta(seconds=1))
        self.assertEqual(self.cycle()["release_started_at"], "")
        self.assertGreater(self.cycle().get("holder_generation", 1), generation)
        self.assertIsInstance(build_hold.read_admission("late", "new-call"), build_hold.AwaitingTurn)
        self.assertIn("registered", build_hold.register_wait("late", call_id="new-call"))
        self.assertEqual(self.entry("late")["calls"][0]["call_id"], "new-call")

    def test_before_the_release_begins_every_answer_is_unchanged(self) -> None:
        for case in ("new", "served", "queued", "missing-call", "dead-call", "listed-call", "open", "active",
                     "admitted-call", "admitted-session", "queued-open", "memory", "active-launch", "dead-open"):
            with self.subTest(case=case):
                self.begin("owner", "peer", calls=(("owner", "call"),), release=False)
                key, call_id = "owner", ""
                cycle = self.cycle()
                entry = cycle["entries"][0]
                if case == "new":
                    key = "unknown"
                elif case == "served":
                    build_hold.store_release_state(entry, build_hold.NoAdmissionAck(self.now))
                    entry["session_pending"] = False
                elif case in ("missing-call", "dead-call", "listed-call", "active"):
                    call_id = "missing" if case == "missing-call" else "call"
                    if case == "dead-call":
                        entry["calls"][0]["proc_start"] = "gone-process"
                    if case == "active":
                        entry["active_call_id"] = "call"
                        build_hold.store_release_state(entry, build_hold.ReleasedAwaitingAdmission(self.now))
                elif case in ("open", "queued-open", "memory", "dead-open"):
                    entry["delivery"] = {"generation": cycle.get("holder_generation", 1), "outcome": "Delivered"}
                    if case == "dead-open":
                        call_id = "call"
                        entry["calls"][0]["proc_start"] = "gone-process"
                    else:
                        entry["calls"] = []
                    state = (build_hold.WaitingForMemory(self.now, self.now) if case == "memory"
                             else build_hold.DeliveryQueued(self.now) if case == "queued-open"
                             else build_hold.ReleasedAwaitingAdmission(self.now))
                    build_hold.store_release_state(entry, state)
                elif case == "admitted-call":
                    call_id = "call"
                    entry["calls"][0]["admitted_generation"] = cycle.get("holder_generation", 1)
                elif case == "admitted-session":
                    entry["session_admitted_generation"] = cycle.get("holder_generation", 1)
                elif case == "active-launch":
                    entry["active_call_id"] = "call"
                    build_hold.store_release_state(entry, build_hold.ReleasedAwaitingAdmission(self.now))
                build_hold.save_cycle(cycle)
                expected = (build_hold.AdmittedTurn if case in
                            ("open", "active", "admitted-call", "admitted-session", "queued-open", "memory")
                            else build_hold.AwaitingTurn)
                self.assertIsInstance(build_hold.read_admission(key, call_id), expected)

    def test_marks_from_a_late_call_leave_the_record_unchanged(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("owner")
                self.set_state("owner", build_hold.NoAdmissionAck(self.now))
                cycle = self.cycle()
                cycle["entries"][0]["delivery"] = {
                    "generation": cycle.get("holder_generation", 1),
                    "outcome": "QueuedUnconfirmed" if queued else "Delivered"}
                build_hold.save_cycle(cycle)
                before = self.record_bytes()
                expected = self.cycle()
                for state in ("WaitingForMemory", "MemoryGateReturned"):
                    self.assertEqual(self.mark("owner", state, "late-call"), "mark ignored")
                build_hold.remove_call("owner", "late-call")
                if queued:
                    expected["entries"][0]["delivery"]["outcome"] = "Delivered"
                    self.assertEqual(self.cycle(), expected)
                else:
                    self.assertEqual(self.record_bytes(), before)

    def test_session_mark_cannot_reopen_a_gone_or_failed_turn(self) -> None:
        for state in (build_hold.RecipientGone(), build_hold.DeliveryFailed(self.now)):
            with self.subTest(state=type(state).__name__):
                self.begin("owner", "peer")
                self.set_state("owner", state)
                cycle = self.cycle()
                cycle["entries"][0]["delivery"] = {
                    "generation": cycle.get("holder_generation", 1),
                    "outcome": "RecipientGone" if isinstance(state, build_hold.RecipientGone) else "DeliveryFailed"}
                build_hold.save_cycle(cycle)
                for mark in ("WaitingForMemory", "MemoryGateReturned"):
                    self.assertEqual(self.mark("owner", mark), "mark ignored")
                    self.assertEqual(build_hold.read_release_state(self.entry("owner")), state)
                _ = self.advance()
                self.assertEqual(self.sent(), ["peer"])

    def test_bound_call_mark_still_moves_a_gone_or_failed_turn(self) -> None:
        for state in (build_hold.RecipientGone(), build_hold.DeliveryFailed(self.now)):
            with self.subTest(state=type(state).__name__):
                self.begin("owner", "peer", calls=(("owner", "active"),))
                cycle = self.cycle()
                entry = cycle["entries"][0]
                entry["active_call_id"] = "active"
                entry["active_released_at"] = self.now.isoformat()
                entry["delivery"] = {
                    "generation": cycle.get("holder_generation", 1),
                    "outcome": "RecipientGone" if isinstance(state, build_hold.RecipientGone) else "DeliveryFailed"}
                build_hold.store_release_state(entry, state)
                build_hold.save_cycle(cycle)
                self.assertIn("WaitingForMemory", self.mark("owner", "WaitingForMemory", "active"))
                self.assertIn("MemoryGateReturned", self.mark("owner", "MemoryGateReturned", "active"))

    def test_memory_wait_ends_at_the_next_poll_when_its_session_left(self) -> None:
        self.begin("owner", "peer")
        _ = self.advance()
        _ = self.mark("owner", "WaitingForMemory")
        delivery = self.entry("owner")["delivery"].copy()
        def lookup(key: str) -> build_hold.RecipientGone | build_hold.RecipientLive:
            return build_hold.RecipientGone() if key == "owner" else build_hold.RecipientLive("/test/socket")

        self.release_mocks["lookup"].side_effect = lookup
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        _ = self.advance()
        self.assertEqual(self.entry("owner")["state"], "RecipientGone")
        self.assertFalse(self.entry("owner")["session_pending"])
        self.assertEqual(self.entry("owner")["delivery"], delivery)
        self.assertEqual(self.sent(), ["owner", "peer"])

    def test_memory_wait_ends_at_the_next_poll_when_its_call_process_is_gone(self) -> None:
        self.begin("owner", "peer", calls=(("owner", "active"),))
        _ = self.advance()
        _ = self.mark("owner", "WaitingForMemory", "active")
        cycle = self.cycle()
        cycle["entries"][0]["calls"][0]["proc_start"] = "gone-process"
        build_hold.save_cycle(cycle)
        self.release_mocks["lookup"].reset_mock()
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        _ = self.advance()
        self.assertEqual(self.entry("owner")["calls"], [])
        self.assertEqual(self.sent(), ["owner", "peer"])
        self.assertEqual(self.release_mocks["lookup"].call_args_list, [mock.call("peer")])

    def test_live_build_in_the_memory_gate_holds_later_sessions(self) -> None:
        for kind in ("call", "live-session", "unknown-session"):
            with self.subTest(kind=kind):
                calls = (("owner", "active"),) if kind == "call" else ()
                self.begin("owner", "peer", calls=calls)
                _ = self.advance()
                call_id = "active" if kind == "call" else ""
                _ = self.mark("owner", "WaitingForMemory", call_id)
                self.release_mocks["lookup"].reset_mock()
                self.release_mocks["lookup"].return_value = (
                    build_hold.RecipientLookupFailed("registry unavailable") if kind == "unknown-session"
                    else build_hold.RecipientLive("/test/socket"))
                self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
                _ = self.advance()
                self.assertEqual(self.entry("peer")["state"], "AwaitingRelease")
                self.assertEqual(self.sent(), ["owner"])
                if kind == "call":
                    self.release_mocks["lookup"].assert_not_called()
                else:
                    self.assertEqual(self.release_mocks["lookup"].call_args_list, [mock.call("owner")])
                _ = self.mark("owner", "MemoryGateReturned", call_id)
                self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner"])
                self.release_mocks["lookup"].return_value = build_hold.RecipientLive("/test/socket")
                self.now += timedelta(seconds=1)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner", "peer"])

    def test_holder_joining_after_a_cut_turn_sends_one_message_for_the_new_generation(self) -> None:
        self.begin("owner", "peer")
        _ = self.advance()
        self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
        self.assertIsInstance(build_hold.read_admission("owner"), build_hold.AdmittedTurn)
        self.now += timedelta(seconds=1)
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("new-holder", "new-holder"), "another hold",
                                  build_hold.NoReleaseEta(), self.now)
        self.assertIsInstance(build_hold.read_admission("owner"), build_hold.AwaitingTurn)
        _ = build_hold.register_wait("owner")
        self.start_release()
        _ = self.advance()
        _ = build_hold.answer_nothing_to_build("peer")
        _ = self.advance()
        self.assertEqual(self.sent().count("owner"), 2)
        _ = build_hold.answer_nothing_to_build("owner")
        _ = self.advance()
        self.assertEqual(self.sent().count("owner"), 2)


HolderFileContent = TypedDict("HolderFileContent", {"holder": str, "since": str, "for": str, "release_eta": str})


class ReleaseDriverTests(IsolatedBuildHoldTest):
    now: datetime = datetime.min
    replaced_at: datetime = datetime.min
    sent: list[str] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now().astimezone() - timedelta(hours=1)
        self.sent = []
        _ = self.enterContext(mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")))
        _ = self.enterContext(mock.patch.object(build_hold, "send_release", side_effect=self.deliver))
        clock = cast(mock.Mock, self.enterContext(mock.patch.object(build_hold, "datetime", wraps=datetime)))
        cast(mock.Mock, clock.now).side_effect = self.clock_now

    def clock_now(self, *zones: tzinfo) -> datetime:
        return self.now.astimezone(zones[0]) if zones else self.now

    def deliver(self, entry: build_hold.ReleaseEntry, _socket: str) -> build_hold.ReleaseDelivered:
        self.sent.append(entry["session_id"])
        return build_hold.ReleaseDelivered()

    def cycle(self) -> build_hold.HoldCycle:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle

    def begin(self, name: str = "holder") -> Path:
        folder = self.scratch / "holders"
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity(name, name), "first hold", build_hold.NoReleaseEta(), self.now)
        build_hold.record_recipients([("first", "first"), ("second", "second"), ("third", "third")])
        _ = build_hold.register_wait("first", call_id="first-call")
        return folder

    def finish_turn(self, _seconds: float) -> None:
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        for entry in self.cycle()["entries"]:
            if entry["state"] in ("ReleasedAwaitingAdmission", "DeliveryQueued"):
                _ = build_hold.answer_nothing_to_build(entry["session_id"])

    def test_driver_stops_when_its_holder_places_a_new_hold(self) -> None:
        folder = self.begin()
        replaced = False

        def sleep(seconds: float) -> None:
            nonlocal replaced
            if not replaced:
                self.now += timedelta(seconds=seconds)
                self.replaced_at = self.now
                _ = build_hold.start_hold(folder, build_hold.HolderIdentity("holder", "holder"), "new hold", build_hold.NoReleaseEta(), self.now)
                replaced = True
            else:
                self.finish_turn(seconds)

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            line = build_hold.release_cycle(folder, "holder")
        self.assertEqual(line, self.stop_line())
        self.assertEqual(self.sent, ["first"])
        holder = build_hold.read_holder(folder / "holder")
        self.assertEqual(holder.since, self.replaced_at)
        cycle = self.cycle()
        self.assertEqual(cycle["holders"]["holder"]["released_at"], "")
        self.assertEqual(cycle["release_started_at"], "")
        self.assertTrue(all(entry["state"] == "AwaitingRelease" for entry in cycle["entries"]))

    def test_status_and_driver_stop_use_user_zone_when_machine_zone_differs(self) -> None:
        with mock.patch.dict(os.environ, {"TZ": "UTC", "USER_ZONE": "America/New_York"}):
            try:
                time.tzset()
                self.test_driver_stops_when_its_holder_places_a_new_hold()
                self.assertIn(self.replaced_at.astimezone(user_zone()).strftime("%H:%M %Z"), self.stop_line())
            finally:
                time.tzset()

    def stop_line(self, name: str = "holder") -> str:
        return (f"release stopped: {name} placed a new hold at {self.replaced_at.astimezone(user_zone()):%H:%M %Z}; "
                "that hold's release serves the sessions still waiting.")

    def record_path(self) -> Path:
        return self.scratch / "release" / self.cycle()["id"] / "cycle.json"

    def resume_error(self, folder: Path) -> str:
        state = build_hold.read_holders(folder)
        assert isinstance(state, build_hold.ActiveHolders)
        return ("a hold is in force (" + "; ".join(build_hold.holder_text(holder) for holder in state.holders)
                + "); --resume only continues a release already begun. Release that hold without --resume.")

    def replace_during_sleep(self, folder: Path, *, another: bool = False, release_new: bool = False) -> str:
        replaced = False
        saved: list[bytes] = []

        def sleep(seconds: float) -> None:
            nonlocal replaced
            if replaced:
                self.finish_turn(seconds)
                return
            replaced = True
            self.now += timedelta(seconds=seconds)
            self.replaced_at = self.now
            _ = build_hold.start_hold(folder, build_hold.HolderIdentity("holder", "holder"), "new hold", build_hold.NoReleaseEta(), self.now)
            if another:
                _ = build_hold.start_hold(folder, build_hold.HolderIdentity("other", "other"), "other hold", build_hold.NoReleaseEta(), self.now)
            if release_new:
                with mock.patch.object(time, "sleep", side_effect=RuntimeError("Driver paused at its poll")):
                    with self.assertRaisesRegex(RuntimeError, "Driver paused at its poll"):
                        _ = build_hold.release_cycle(folder, "holder")
            saved.append(self.record_path().read_bytes())

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            line = build_hold.release_cycle(folder, "holder")
        self.assertEqual(line, self.stop_line())
        self.assertEqual(self.record_path().read_bytes(), saved[0])
        self.assertTrue((folder / "holder").is_file())
        return line

    def test_sessions_left_waiting_get_one_message_from_the_new_holds_release(self) -> None:
        folder = self.begin()
        _ = self.replace_during_sleep(folder)
        expected_error = self.resume_error(folder)
        before = self.record_path().read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--resume"]), 1)
        self.assertEqual(errors.getvalue(), f"build_hold: {expected_error}\n")
        self.assertEqual(self.record_path().read_bytes(), before)
        with mock.patch.object(time, "sleep", side_effect=self.finish_turn), redirect_stdout(io.StringIO()):
            line = build_hold.release_cycle(folder, "holder")
        self.assertTrue(line.startswith("released, builds may resume."))
        self.assertEqual(self.sent, ["first", "first", "second", "third"])
        self.assertFalse((folder / "holder").exists())

    def test_resume_refuses_a_new_hold_whose_name_uses_a_released_holds_file(self) -> None:
        folder = self.begin("night run")
        with mock.patch.object(time, "sleep", side_effect=RuntimeError("Driver paused at its poll")), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Driver paused at its poll"):
                _ = build_hold.release_cycle(folder, "night run")
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        self.replaced_at = self.now
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("night-run", "night-run"), "new hold", build_hold.NoReleaseEta(), self.now)
        self.assertEqual([holder.name for holder in build_hold.live_holds(self.cycle(), folder)], ["night-run"])
        before = self.record_path().read_bytes()
        for arguments in (["release", "--resume"], ["release", "--resume", "--holder", "night run"],
                          ["release", "--resume", "--holder", "night-run"]):
            with self.subTest(arguments=arguments):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(time, "sleep", side_effect=self.finish_turn), \
                     redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(build_hold.main(arguments), 1)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), f"build_hold: {self.resume_error(folder)}\n")
                self.assertEqual(self.record_path().read_bytes(), before)
                self.assertEqual(build_hold.read_holder(folder / "night-run").since, self.replaced_at)
                self.assertEqual(self.sent, ["first"])

    def test_release_command_stops_when_another_name_replaces_its_hold_file(self) -> None:
        folder = self.begin("night run")
        replaced = False
        saved: list[bytes] = []

        def sleep(seconds: float) -> None:
            nonlocal replaced
            if replaced:
                self.finish_turn(seconds)
                return
            replaced = True
            self.now += timedelta(seconds=seconds)
            self.replaced_at = self.now
            _ = build_hold.start_hold(folder, build_hold.HolderIdentity("night-run", "night-run"), "new hold", build_hold.NoReleaseEta(), self.now)
            saved.append(self.record_path().read_bytes())

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--holder", "night run"]), 0)
        self.assertEqual(output.getvalue().splitlines()[-1], self.stop_line("night-run"))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.sent, ["first"])
        self.assertEqual(build_hold.read_holder(folder / "night-run").since, self.replaced_at)
        self.assertEqual(self.record_path().read_bytes(), saved[0])

    def test_driver_stops_when_the_new_hold_was_released_within_one_poll(self) -> None:
        folder = self.begin()
        _ = self.replace_during_sleep(folder, release_new=True)
        self.assertTrue(self.cycle()["holders"]["holder"]["released_at"])
        self.assertTrue(self.cycle()["release_started_at"])
        self.assertEqual(self.sent, ["first", "first"])

    def pause_release(self, folder: Path, name: str) -> None:
        with mock.patch.object(time, "sleep", side_effect=RuntimeError("Driver paused at its poll")), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Driver paused at its poll"):
                _ = build_hold.release_cycle(folder, name)

    def test_release_with_a_holder_since_that_is_not_a_time_ends_the_hold(self) -> None:
        for arguments in (["release", "--holder", "holder"], ["release", "--resume"],
                          ["release", "--resume", "--holder", "holder"]):
            with self.subTest(arguments=arguments):
                folder = self.begin()
                path = self.record_path()
                cycle = self.cycle()
                cycle["holders"]["holder"]["since"] = "not a time"
                build_hold.save_cycle(cycle)
                before = path.read_bytes()
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(build_hold.main(arguments), 0, errors.getvalue())
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(output.getvalue().splitlines()[-1],
                                 "released, builds may resume. No hold cycle: broadcast this release to every session.")
                self.assertFalse((folder / "holder").exists())
                copies = list(path.parent.glob("cycle.json.damaged-*"))
                self.assertEqual(len(copies), 1)
                self.assertEqual(copies[0].read_bytes(), before)
                self.assertFalse((self.scratch / "release" / "current").exists())
                self.assertEqual(self.sent, [])

    def test_resume_for_a_missing_holder_refuses_other_holds_before_held_nothing(self) -> None:
        folder = self.begin()
        path = self.record_path()
        before = path.read_bytes()
        with self.assertRaises(ValueError) as refused:
            _ = build_hold.release_cycle(folder, "missing", build_hold.ResumedRelease())
        self.assertEqual(str(refused.exception), self.resume_error(folder))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((folder / "holder").is_file())
        self.assertEqual(self.sent, [])
        (folder / "holder").unlink()
        with self.assertRaises(ValueError) as absent:
            _ = build_hold.release_cycle(folder, "missing", build_hold.ResumedRelease())
        self.assertEqual(str(absent.exception), "missing held nothing")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.sent, [])

    def test_resume_under_another_name_keeps_the_recorded_release_time(self) -> None:
        folder = self.begin("night run")
        self.pause_release(folder, "night run")
        released_at = self.cycle()["holders"]["night run"]["released_at"].encode()
        self.assertTrue(released_at)
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(time, "sleep", side_effect=self.finish_turn), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--resume", "--holder", "night-run"]), 0)
        self.assertEqual(self.cycle()["holders"]["night run"]["released_at"].encode(), released_at)
        self.assertTrue(output.getvalue().splitlines()[-1].startswith("released, builds may resume."))
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(self.sent, ["first", "second", "third"])
        self.assertFalse((folder / "night-run").exists())

    def test_plain_release_under_the_old_name_releases_the_current_hold_instance(self) -> None:
        folder = self.begin("night run")
        self.pause_release(folder, "night run")
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("night-run", "night-run"), "new hold", build_hold.NoReleaseEta(), self.now)
        self.pause_release(folder, "night run")
        holder = build_hold.read_holder(folder / "night-run")
        matching = [instants for key, instants in self.cycle()["holders"].items()
                    if build_hold.holder_path(folder, key).name == holder.key
                    and build_hold.aware_instant(instants["since"]) == holder.since]
        self.assertTrue(any(instants["released_at"] for instants in matching),
                        "The current hold instance must have a recorded release")
        self.assertEqual(build_hold.live_holds(self.cycle(), folder), ())

    def test_release_records_the_files_since_when_its_holder_entry_is_missing(self) -> None:
        folder = self.begin()
        holder = build_hold.read_holder(folder / "holder")
        cycle = self.cycle()
        del cycle["holders"]["holder"]
        build_hold.save_cycle(cycle)
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        self.pause_release(folder, "holder")
        recorded = self.cycle()["holders"]["holder"]
        self.assertEqual(build_hold.aware_instant(recorded["since"]), holder.since)
        self.assertTrue(recorded["released_at"])
        self.assertEqual(build_hold.live_holds(self.cycle(), folder), ())

    def test_resume_lookup_selects_the_released_instance_in_the_holder_file(self) -> None:
        folder = self.begin("night run")
        self.pause_release(folder, "night run")
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("night-run", "night-run"), "new hold", build_hold.NoReleaseEta(), self.now)
        self.pause_release(folder, "night-run")
        since = build_hold.read_holder(folder / "night-run").since
        release = build_hold.release_cycle
        selected: list[str] = []

        def resume(directory: Path, name: str, start: build_hold.FirstRelease | build_hold.ResumedRelease) -> str:
            selected.append(name)
            self.assertEqual(build_hold.aware_instant(self.cycle()["holders"][name]["since"]), since)
            return release(directory, name, start)

        with mock.patch.object(build_hold, "release_cycle", side_effect=resume), \
             mock.patch.object(time, "sleep", side_effect=self.finish_turn), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(build_hold.main(["release", "--resume"]), 0)
        self.assertEqual(selected, ["night-run"])
        self.assertFalse((folder / "night-run").exists())

    def test_replaced_hold_keeps_its_file_when_another_holder_joined(self) -> None:
        folder = self.begin()
        _ = self.replace_during_sleep(folder, another=True)
        self.assertTrue((folder / "other").is_file())
        self.assertEqual(self.sent, ["first"])
        self.assertEqual(self.cycle()["holders"]["holder"]["released_at"], "")

    def test_another_holder_joining_ends_the_driver_as_before(self) -> None:
        folder = self.begin()

        def sleep(seconds: float) -> None:
            self.now += timedelta(seconds=seconds)
            _ = build_hold.start_hold(folder, build_hold.HolderIdentity("other", "other"), "other hold", build_hold.NoReleaseEta(), self.now)

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            line = build_hold.release_cycle(folder, "holder")
        self.assertEqual(line, "released; still held by " + build_hold.holder_text(build_hold.read_holder(folder / "other")))
        self.assertFalse((folder / "holder").exists())
        self.assertTrue((folder / "other").exists())
        self.assertEqual(self.sent, ["first"])

    def test_release_under_a_name_with_a_space_lists_only_the_other_holder(self) -> None:
        folder = self.begin("night run")
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("other", "other"), "other hold", build_hold.NoReleaseEta(), self.now)
        expected = "released; still held by " + build_hold.holder_text(build_hold.read_holder(folder / "other"))
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--holder", "night run"]), 0)
        self.assertEqual(output.getvalue(), expected + "\n")
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse((folder / "night-run").exists())
        self.assertTrue((folder / "other").is_file())
        self.assertEqual(self.sent, [])

    def test_driver_ends_when_another_release_finished_the_queue(self) -> None:
        folder = self.begin()
        finished = False

        def sleep(_seconds: float) -> None:
            nonlocal finished
            self.assertFalse(finished, "The old driver must end on its next pass")
            finished = True
            with mock.patch.object(time, "sleep", side_effect=self.finish_turn):
                _ = build_hold.release_cycle(folder, "holder")

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            try:
                line = build_hold.release_cycle(folder, "holder")
            except ValueError as error:
                self.fail(f"A completed release must end with a line, not {error}")
        self.assertEqual(line, "release ended: another release command finished it; builds may resume.")
        self.assertFalse((folder / "holder").exists())
        self.assertEqual(self.sent, ["first", "second", "third"])

    def test_resume_refuses_a_hold_placed_after_its_lookup(self) -> None:
        folder = self.begin()
        cycle = self.cycle()
        cycle["holders"]["holder"]["released_at"] = self.now.isoformat()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        self.now += timedelta(seconds=1)
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("holder", "holder"), "new hold after lookup", build_hold.NoReleaseEta(), self.now)
        before = self.record_path().read_bytes()
        with self.assertRaises(ValueError) as refused:
            _ = build_hold.release_cycle(folder, "holder", build_hold.ResumedRelease())
        self.assertEqual(str(refused.exception), self.resume_error(folder))
        self.assertEqual(self.record_path().read_bytes(), before)
        self.assertTrue((folder / "holder").is_file())
        self.assertEqual(self.sent, [])

    def test_release_command_ends_and_names_every_remaining_holder(self) -> None:
        folder = self.begin()
        finished = False

        def sleep(seconds: float) -> None:
            nonlocal finished
            self.assertFalse(finished, "The old driver must end on its next pass")
            finished = True
            self.now += timedelta(seconds=seconds)
            _ = build_hold.start_hold(folder, build_hold.HolderIdentity("other", "other"), "other hold", build_hold.NoReleaseEta(), self.now)
            _ = build_hold.start_hold(folder, build_hold.HolderIdentity("third-holder", "third-holder"), "third hold", build_hold.NoReleaseEta(), self.now)
            _ = build_hold.release_cycle(folder, "holder")

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--holder", "holder"]), 0)
        state = build_hold.read_holders(folder)
        assert isinstance(state, build_hold.ActiveHolders)
        expected = ("release ended: another release command finished it; still held by "
                    + "; ".join(build_hold.holder_text(holder) for holder in state.holders))
        self.assertEqual(output.getvalue().splitlines()[-1], expected)
        self.assertNotIn("builds may resume", output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual([holder.name for holder in state.holders], ["other", "third-holder"])
        self.assertEqual(self.sent, ["first"])

    def test_release_command_ends_with_exit_zero_when_no_holder_remains(self) -> None:
        folder = self.begin()
        finished = False

        def sleep(_seconds: float) -> None:
            nonlocal finished
            self.assertFalse(finished, "The old driver must end on its next pass")
            finished = True
            with mock.patch.object(time, "sleep", side_effect=self.finish_turn):
                _ = build_hold.release_cycle(folder, "holder")

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--holder", "holder"]), 0)
        self.assertEqual(output.getvalue().splitlines()[-1],
                         "release ended: another release command finished it; builds may resume.")
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse((folder / "holder").exists())
        self.assertEqual(self.sent, ["first", "second", "third"])

    def test_replacement_hold_stops_the_driver_before_it_reads_a_damaged_record(self) -> None:
        folder = self.begin()
        path = self.record_path()
        current = self.scratch / "release" / "current"
        cycle_id = self.cycle()["id"]
        replaced = False
        files: list[str] = []

        def sleep(seconds: float) -> None:
            nonlocal replaced
            self.assertFalse(replaced, "A replacement must stop the next pass")
            replaced = True
            self.now += timedelta(seconds=seconds)
            self.replaced_at = self.now
            _ = path.write_text("{broken")
            _ = build_hold.start_hold(folder, build_hold.HolderIdentity("holder", "holder"), "new hold", build_hold.NoReleaseEta(), self.now)
            _ = path.write_text("{still broken")
            _ = current.write_text(cycle_id + "\n")
            files.extend(sorted(file.name for file in path.parent.iterdir()))

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            line = build_hold.release_cycle(folder, "holder")
        self.assertEqual(line, self.stop_line())
        self.assertEqual(build_hold.read_holder(folder / "holder").since, self.replaced_at)
        self.assertEqual(self.sent, ["first"])
        self.assertEqual(path.read_text(), "{still broken")
        self.assertEqual(sorted(file.name for file in path.parent.iterdir()), files)
        self.assertEqual(current.read_text(), cycle_id + "\n")

    def test_unchanged_hold_with_a_damaged_record_at_a_later_pass_is_released(self) -> None:
        folder = self.begin()
        path = self.record_path()
        original = build_hold.read_holder(folder / "holder").since

        def sleep(_seconds: float) -> None:
            self.assertEqual(build_hold.read_holder(folder / "holder").since, original)
            _ = path.write_text("{broken")

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            line = build_hold.release_cycle(folder, "holder")
        self.assertEqual(line, "released, builds may resume. No hold cycle: broadcast this release to every session.")
        self.assertFalse((folder / "holder").exists())
        copies = list(path.parent.glob("cycle.json.damaged-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "{broken")
        self.assertEqual(self.sent, ["first"])

    def test_removed_holder_ends_the_driver_without_reading_a_damaged_record(self) -> None:
        folder = self.begin()
        path = self.record_path()
        files: list[str] = []

        def sleep(_seconds: float) -> None:
            with mock.patch.object(time, "sleep", side_effect=self.finish_turn):
                _ = build_hold.release_cycle(folder, "holder")
            _ = path.write_text("{broken")
            files.extend(sorted(file.name for file in path.parent.iterdir()))

        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--holder", "holder"]), 0)
        self.assertEqual(output.getvalue().splitlines()[-1],
                         "release ended: another release command finished it; builds may resume.")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(path.read_text(), "{broken")
        self.assertEqual(sorted(file.name for file in path.parent.iterdir()), files)
        self.assertEqual(self.sent, ["first", "second", "third"])

    def test_hold_under_a_name_with_a_space_is_in_force_until_released(self) -> None:
        folder = self.begin("night run")
        self.assertEqual([holder.name for holder in build_hold.live_holds(self.cycle(), folder)], ["night run"])
        seen: list[tuple[build_hold.Holder, ...]] = []

        def sleep(seconds: float) -> None:
            seen.append(build_hold.live_holds(self.cycle(), folder))
            self.finish_turn(seconds)

        with mock.patch.object(time, "sleep", side_effect=sleep), redirect_stdout(io.StringIO()):
            _ = build_hold.release_cycle(folder, "night run")
        self.assertTrue(seen)
        self.assertTrue(all(not holders for holders in seen))
        self.assertEqual(build_hold.live_holds(self.cycle(), folder), ())
        self.assertFalse(build_hold.holder_path(folder, "night run").exists())

    def test_resume_refuses_while_a_hold_is_in_force(self) -> None:
        folder = self.begin()
        for arguments in (["release", "--resume"], ["release", "--resume", "--holder", "holder"]):
            with self.subTest(arguments=arguments):
                before = self.record_path().read_bytes()
                expected = self.resume_error(folder)
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(time, "sleep", side_effect=self.finish_turn), redirect_stdout(output), redirect_stderr(errors):
                    code = build_hold.main(arguments)
                self.assertEqual(code, 1, output.getvalue())
                self.assertEqual(errors.getvalue(), f"build_hold: {expected}\n")
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(self.record_path().read_bytes(), before)
                self.assertTrue((folder / "holder").is_file())

    def test_resume_continues_a_release_with_no_hold_in_force(self) -> None:
        folder = self.begin()
        cycle = self.cycle()
        cycle["holders"]["holder"]["released_at"] = self.now.isoformat()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(time, "sleep", side_effect=self.finish_turn), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(build_hold.main(["release", "--resume"]), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertTrue(output.getvalue().splitlines()[-1].startswith("released, builds may resume."))
        self.assertFalse((folder / "holder").exists())
        self.assertEqual(self.sent, ["first", "second", "third"])

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
                    expected: HolderFileContent = {"holder": "holder", "since": self.now.isoformat(), "for": "new purpose", "release_eta": "unknown"}
                    self.assertEqual(content, expected)
                    seen.append(source)
                    replace(source, destination)

                with mock.patch.object(os, "replace", side_effect=rename):
                    _ = build_hold.write_hold(folder, build_hold.HolderIdentity("holder", "holder"), "new purpose", build_hold.NoReleaseEta(), self.now)
                self.assertEqual(seen, [folder.parent / f"{folder.name}.{path.name}.tmp"])
                self.assertEqual(build_hold.read_holder(path).purpose, "new purpose")


class ToldSessionReleaseTests(IsolatedBuildHoldTest):
    now: datetime = datetime.min
    release_mocks: dict[str, mock.Mock] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now().astimezone() - timedelta(hours=1)
        self.release_mocks = {
            "lookup": cast(mock.Mock, self.enterContext(mock.patch.object(
                build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")))),
            "send": cast(mock.Mock, self.enterContext(mock.patch.object(
                build_hold, "send_release", return_value=build_hold.ReleaseDelivered()))),
        }

    def begin(self, *recipients: str, registered: tuple[str, ...] = (), calls: tuple[tuple[str, str], ...] = (),
              started_calls: tuple[tuple[str, str], ...] = ()) -> None:
        folder = self.scratch / "holders"
        (folder / "holder").unlink(missing_ok=True)
        _ = build_hold.start_hold(folder, build_hold.HolderIdentity("holder", "holder"), "release coverage", build_hold.NoReleaseEta(), self.now)
        build_hold.record_recipients([(f"name {key}", key) for key in recipients])
        for key in registered:
            _ = build_hold.register_wait(key)
        for key, call_id in calls:
            self.call(key, call_id)
        for key, call_id in started_calls:
            self.call(key, call_id, started=True)
        cycle = self.cycle()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        self.release_mocks["lookup"].reset_mock()
        self.release_mocks["send"].reset_mock()

    def cycle(self) -> build_hold.HoldCycle:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle

    def entry(self, key: str) -> build_hold.ReleaseEntry:
        return next(entry for entry in self.cycle()["entries"] if entry["session_id"] == key)

    def sent(self) -> list[str]:
        calls = cast(list[tuple[tuple[build_hold.ReleaseEntry, str], dict[str, object]]], self.release_mocks["send"].call_args_list)
        return [arguments[0]["session_id"] for arguments, _keywords in calls]

    def advance(self) -> tuple[bool, str]:
        return build_hold.advance_release(self.cycle(), self.now, clock=lambda: self.now)

    def call(self, key: str, call_id: str, *, started: bool = False) -> None:
        self.assertIn("registered", build_hold.register_wait(key, call_id=call_id, started=started))

    def returned(self, key: str, call_id: str = "") -> datetime:
        self.assertIn("MemoryGateReturned", build_hold.mark_gate(
            key, "MemoryGateReturned", "Granted", call_id=call_id, cycle_id=self.cycle()["id"]))
        return build_hold.aware_instant(self.entry(key).get("wait_ended_at", ""))

    def final_line(self) -> str:
        # All turns are already over; freeze the driver's wall clock at the injected test time.
        with mock.patch.object(build_hold, "datetime", wraps=datetime) as clock, \
             mock.patch.object(time, "sleep", side_effect=AssertionError("Release should be complete")):
            cast(mock.Mock, clock.now).return_value = self.now
            return build_hold.release_cycle(self.scratch / "holders", "holder")

    def test_three_told_sessions_are_saved_together_and_serve_one_turn_at_a_time(self) -> None:
        for ending in ("gate", "nothing", "timeout"):
            with self.subTest(ending=ending):
                self.begin("z-first", "a-second", "m-third")
                snapshots: list[list[str]] = []

                def lookup(_key: str) -> build_hold.RecipientLive:
                    snapshots.append([entry["session_id"] for entry in self.cycle()["entries"]])
                    return build_hold.RecipientLive("/test/socket")

                self.release_mocks["lookup"].side_effect = lookup
                complete, detail = self.advance()
                self.assertFalse(complete)
                self.assertNotIn("waiting for registrations", detail)
                self.assertEqual(snapshots, [["z-first", "a-second", "m-third"]])
                self.assertEqual(self.sent(), ["z-first"])
                self.assertEqual(self.entry("a-second")["state"], "AwaitingRelease")
                if ending == "gate":
                    returned = self.returned("z-first")
                    self.now = returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                    _ = self.advance()
                    self.assertEqual(self.sent(), ["z-first"])
                    self.now += timedelta(seconds=1)
                elif ending == "nothing":
                    self.assertIn("NothingToBuild", build_hold.answer_nothing_to_build("z-first"))
                else:
                    self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                    _ = self.advance()
                    self.assertEqual(self.sent(), ["z-first"])
                    self.now += timedelta(seconds=1)
                _ = self.advance()
                if ending == "timeout":
                    self.assertEqual(self.entry("z-first")["state"], "NoAdmissionAck")
                self.assertEqual(self.sent(), ["z-first", "a-second"])
                self.assertEqual(self.entry("m-third")["state"], "AwaitingRelease")
                _ = build_hold.answer_nothing_to_build("a-second")
                _ = self.advance()
                self.assertEqual(self.sent(), ["z-first", "a-second", "m-third"])
                _ = build_hold.answer_nothing_to_build("m-third")
                self.assertTrue(self.advance()[0])

    def test_departed_session_ends_turn_on_next_poll_without_changing_delivery(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("first", "second")
                self.release_mocks["lookup"].side_effect = None
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                           else build_hold.ReleaseDelivered())
                _ = self.advance()
                delivery = self.entry("first")["delivery"].copy()
                self.assertEqual(delivery["outcome"], "QueuedUnconfirmed" if queued else "Delivered")

                def lookup(key: str) -> build_hold.RecipientGone | build_hold.RecipientLive:
                    return build_hold.RecipientGone() if key == "first" else build_hold.RecipientLive("/test/socket")

                self.release_mocks["lookup"].reset_mock()
                self.release_mocks["lookup"].side_effect = lookup
                self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
                self.assertFalse(self.advance()[0])
                self.assertEqual(self.entry("first")["state"], "RecipientGone")
                self.assertFalse(self.entry("first")["session_pending"])
                self.assertEqual(self.entry("first")["delivery"], delivery)
                self.assertEqual(self.sent(), ["first", "second"])
                self.assertEqual(self.release_mocks["lookup"].call_args_list,
                                 [mock.call("first"), mock.call("second")])
                _ = build_hold.answer_nothing_to_build("second")
                self.assertTrue(self.advance()[0])
                line = self.final_line()
                if queued:
                    self.assertIn("Not reached, tell them yourself: name first (queued, no answer)", line)
                    self.assertNotIn("session gone", line)
                else:
                    self.assertNotIn("Not reached", line)

    def test_lookup_failure_preserves_silent_turn_until_normal_deadline_without_waiters(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("owner")
                self.release_mocks["lookup"].return_value = build_hold.RecipientLive("/test/socket")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                           else build_hold.ReleaseDelivered())
                started = self.now
                _ = self.advance()
                delivery = self.entry("owner")["delivery"].copy()
                self.release_mocks["lookup"].reset_mock()
                self.release_mocks["lookup"].return_value = build_hold.RecipientLookupFailed("registry denied")
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    for elapsed in (build_hold.RELEASE_POLL_S, build_hold.RELEASE_SETTLE_S,
                                    build_hold.NO_ADMISSION_ACK_S - 1):
                        self.now = started + timedelta(seconds=elapsed)
                        self.assertFalse(self.advance()[0])
                        self.assertEqual(self.entry("owner")["state"], "DeliveryQueued" if queued else "ReleasedAwaitingAdmission")
                        self.assertEqual(self.entry("owner")["delivery"], delivery)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(self.release_mocks["lookup"].call_args_list, [mock.call("owner")] * 3)
                self.release_mocks["lookup"].reset_mock()
                self.now = started + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
                self.assertTrue(self.advance()[0])
                self.release_mocks["lookup"].assert_not_called()
                self.assertEqual(build_hold.read_release_state(self.entry("owner")), build_hold.NoAdmissionAck(started))
                self.assertFalse(self.entry("owner")["session_pending"])
                self.assertEqual(self.entry("owner")["delivery"], delivery)
                self.assertEqual(self.sent(), ["owner"])

    def test_silent_delivered_and_queued_turns_yield_at_one_minute_without_deadline_lookup(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("first", "second")
                self.release_mocks["lookup"].side_effect = None
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                           else build_hold.ReleaseDelivered())
                started = self.now
                _ = self.advance()
                self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                self.assertFalse(self.advance()[0])
                self.assertEqual(self.entry("first")["state"], "DeliveryQueued" if queued else "ReleasedAwaitingAdmission")
                self.assertEqual(self.entry("second")["state"], "AwaitingRelease")
                self.assertEqual(self.sent(), ["first"])

                def lookup(key: str) -> build_hold.RecipientLive:
                    self.assertNotEqual(key, "first", "The expired turn must end before a session lookup")
                    return build_hold.RecipientLive("/test/socket")

                self.release_mocks["lookup"].reset_mock()
                self.release_mocks["lookup"].side_effect = lookup
                self.now += timedelta(seconds=1)
                self.assertFalse(self.advance()[0])
                self.assertEqual(build_hold.read_release_state(self.entry("first")), build_hold.NoAdmissionAck(started))
                self.assertFalse(self.entry("first")["session_pending"])
                self.assertEqual(self.sent(), ["first", "second"])
                self.assertEqual(self.release_mocks["lookup"].call_args_list, [mock.call("second")])

    def test_late_call_preserves_the_last_silent_turns_full_timeout(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("owner")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                          else build_hold.ReleaseDelivered())
                started = self.now
                _ = self.advance()
                self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S + build_hold.RELEASE_POLL_S)
                self.assertFalse(self.advance()[0])
                before = self.cycle()
                self.assertEqual(build_hold.register_wait("late", call_id="waiting-call"),
                                 "release under way; late builds now")
                self.assertIsInstance(build_hold.read_admission("late", "waiting-call"), build_hold.AdmittedTurn)
                self.assertEqual(self.cycle(), before)
                self.assertFalse(self.advance()[0])
                self.assertEqual(self.sent(), ["owner"])
                self.now = started + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
                self.assertTrue(self.advance()[0])
                self.assertEqual(build_hold.read_release_state(self.entry("owner")), build_hold.NoAdmissionAck(started))

    def test_bound_call_keeps_full_turn_and_skips_session_lookups(self) -> None:
        for queued in (False, True):
            for ending in ("gate", "timeout"):
                with self.subTest(queued=queued, ending=ending):
                    self.begin("owner", "peer")
                    self.release_mocks["lookup"].side_effect = None
                    self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                               else build_hold.ReleaseDelivered())
                    started = self.now
                    _ = self.advance()
                    self.call("owner", "building-call")
                    self.assertIsInstance(build_hold.read_admission("owner", "building-call"), build_hold.AdmittedTurn)
                    self.release_mocks["lookup"].reset_mock()
                    self.release_mocks["lookup"].side_effect = AssertionError("A bound call needs no session lookup")
                    for elapsed in (build_hold.RELEASE_SETTLE_S, build_hold.NO_ADMISSION_ACK_S - 1):
                        self.now = started + timedelta(seconds=elapsed)
                        self.assertFalse(self.advance()[0])
                        self.assertEqual(self.entry("owner")["active_call_id"], "building-call")
                        self.assertEqual(self.entry("peer")["state"], "AwaitingRelease")
                        self.assertEqual(self.sent(), ["owner"])
                    if ending == "gate":
                        self.now = self.returned("owner", "building-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                        self.assertFalse(self.advance()[0])
                        self.assertEqual(self.sent(), ["owner"])
                        self.now += timedelta(seconds=1)
                    else:
                        self.now = started + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
                    self.release_mocks["lookup"].assert_not_called()
                    self.release_mocks["lookup"].side_effect = None
                    _ = self.advance()
                    if ending == "timeout":
                        self.assertEqual(build_hold.read_release_state(self.entry("owner")), build_hold.NoAdmissionAck(started))
                    self.assertEqual(self.entry("owner")["active_call_id"], "")
                    self.assertEqual(self.sent(), ["owner", "peer"])
                    self.assertEqual(self.release_mocks["lookup"].call_args_list, [mock.call("peer")])

    def test_call_after_cut_turn_is_admitted_without_a_second_message(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("owner", "peer")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                          else build_hold.ReleaseDelivered())
                _ = self.advance()
                self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S)
                _ = self.advance()
                self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
                before = self.cycle()
                self.assertEqual(build_hold.register_wait("owner", call_id="late-build"),
                                 "release under way; owner builds now")
                self.assertIsInstance(build_hold.read_admission("owner", "late-build"), build_hold.AdmittedTurn)
                self.assertEqual(self.cycle(), before)
                _ = build_hold.answer_nothing_to_build("peer")
                self.assertTrue(self.advance()[0])
                self.assertEqual(self.sent(), ["owner", "peer"])

    def test_late_calls_add_no_turn_while_told_sessions_release_in_order(self) -> None:
        self.begin("first", "second", "third")
        _ = self.advance()
        for index in range(3):
            self.assertIn("builds now", build_hold.register_wait(f"late-{index}", call_id=f"call-{index}"))
            self.assertIsInstance(build_hold.read_admission(f"late-{index}", f"call-{index}"), build_hold.AdmittedTurn)
            _ = self.advance()
            self.assertEqual(self.sent(), ["first"])
        _ = build_hold.answer_nothing_to_build("first")
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "second"])
        for index in range(3, 6):
            self.assertIn("builds now", build_hold.register_wait(f"late-{index}", call_id=f"call-{index}"))
            self.assertIsInstance(build_hold.read_admission(f"late-{index}", f"call-{index}"), build_hold.AdmittedTurn)
            _ = self.advance()
            self.assertEqual(self.sent(), ["first", "second"])
        _ = build_hold.answer_nothing_to_build("second")
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "second", "third"])
        _ = build_hold.answer_nothing_to_build("third")
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "second", "third"])
        self.assertTrue(self.advance()[0])

    def test_call_registered_by_queued_told_session_uses_its_existing_turn(self) -> None:
        self.begin("first", "second", "third", registered=("first",), calls=(("second", "second-call"),))
        _ = self.advance()
        _ = build_hold.answer_nothing_to_build("first")
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "second"])
        self.assertEqual(self.entry("second")["active_call_id"], "second-call")
        self.assertIsInstance(build_hold.read_admission("second", "second-call"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("second"), build_hold.AdmittedTurn)
        self.now = self.returned("second", "second-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "second", "third"])

    def test_resumed_call_waits_for_current_turn_and_settle_then_precedes_unstarted_turns(self) -> None:
        self.begin("first", "resumed", "second", "third", registered=("second", "third"),
                   started_calls=(("first", "first-call"), ("resumed", "resumed-call")))
        _ = self.advance()
        order = [entry["session_id"] for entry in self.cycle()["entries"]]
        self.assertEqual(order, ["first", "resumed", "second", "third"])
        self.assertEqual(self.entry("first")["active_call_id"], "first-call")
        self.assertEqual(self.sent(), ["first"])
        self.now += timedelta(seconds=build_hold.RELEASE_POLL_S)
        _ = self.advance()
        self.assertEqual([entry["session_id"] for entry in self.cycle()["entries"]], order)
        self.assertEqual(self.entry("resumed")["state"], "AwaitingRelease")
        self.assertIsInstance(build_hold.read_admission("resumed", "resumed-call"), build_hold.AwaitingTurn)
        self.assertEqual(self.sent(), ["first"])
        self.release_mocks["lookup"].assert_called_once_with("first")
        self.now = self.returned("first", "first-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
        _ = self.advance()
        self.assertEqual([entry["session_id"] for entry in self.cycle()["entries"]], order)
        self.assertEqual(self.entry("resumed")["state"], "AwaitingRelease")
        self.assertEqual(self.sent(), ["first"])
        self.release_mocks["lookup"].assert_called_once_with("first")
        self.now += timedelta(seconds=1)
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "resumed"])
        self.assertEqual(self.entry("resumed")["active_call_id"], "resumed-call")
        self.assertIsInstance(build_hold.read_admission("resumed", "resumed-call"), build_hold.AdmittedTurn)
        for key in ("second", "third"):
            self.assertEqual(self.entry(key)["state"], "AwaitingRelease")
        self.now = self.returned("resumed", "resumed-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "resumed", "second"])
        _ = build_hold.answer_nothing_to_build("second")
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "resumed", "second", "third"])

    def test_call_registering_after_delivery_takes_open_turn_but_not_memory_wait(self) -> None:
        for turn in ("delivered", "queued", "memory"):
            with self.subTest(turn=turn):
                self.begin("owner", "peer")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if turn == "queued"
                                                           else build_hold.ReleaseDelivered())
                _ = self.advance()
                if turn == "memory":
                    self.assertEqual(build_hold.mark_gate("owner", "WaitingForMemory"), "owner: WaitingForMemory")
                original_clock = self.entry("owner")["active_released_at"]
                self.now += timedelta(seconds=1)
                if turn == "memory":
                    self.assertIn("builds now", build_hold.register_wait("owner", call_id="new-call"))
                else:
                    self.call("owner", "new-call")
                self.assertEqual(self.entry("owner")["active_released_at"], original_clock)
                if turn == "memory":
                    self.assertIsInstance(build_hold.read_admission("owner", "new-call"), build_hold.AdmittedTurn)
                    self.assertEqual(self.entry("owner")["active_call_id"], "")
                    _ = self.advance()
                    self.assertEqual(self.sent(), ["owner"])
                    self.assertEqual(self.entry("owner")["state"], "WaitingForMemory")
                    continue
                self.assertIsInstance(build_hold.read_admission("owner", "new-call"), build_hold.AdmittedTurn)
                self.assertEqual(self.entry("owner")["active_call_id"], "new-call")
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner"])
                self.now = self.returned("owner", "new-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner"])
                self.now += timedelta(seconds=1)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner", "peer"])

    def test_second_call_during_bound_turn_is_admitted_without_another_turn(self) -> None:
        self.begin("owner", "peer")
        _ = self.advance()
        self.call("owner", "call-a")
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)
        before = self.cycle()
        self.assertIn("builds now", build_hold.register_wait("owner", call_id="call-b"))
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)
        self.assertEqual(self.cycle(), before)
        self.now = self.returned("owner", "call-a") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner", "peer"])
        self.assertEqual(self.entry("owner")["active_call_id"], "")

    def test_call_joining_session_turn_keeps_original_admission_deadline(self) -> None:
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.begin("owner", "peer")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if queued
                                                           else build_hold.ReleaseDelivered())
                _ = self.advance()
                original_state = build_hold.read_release_state(self.entry("owner"))
                original_clock = self.entry("owner")["active_released_at"]
                self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S - 1)
                self.call("owner", "new-call")
                self.assertIsInstance(build_hold.read_admission("owner", "new-call"), build_hold.AdmittedTurn)
                self.assertEqual(build_hold.read_release_state(self.entry("owner")), original_state)
                self.assertEqual(self.entry("owner")["active_released_at"], original_clock)
                _ = self.advance()
                self.assertEqual(self.sent(), ["owner"])
                self.now += timedelta(seconds=1)
                _ = self.advance()
                self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
                self.assertEqual(self.entry("owner")["active_call_id"], "")
                self.assertEqual(self.sent(), ["owner", "peer"])

    def test_failed_notification_started_call_holds_resumed_call_until_mark_or_timeout(self) -> None:
        for gone in (False, True):
            for ending in ("mark", "timeout"):
                with self.subTest(gone=gone, ending=ending):
                    self.begin("owner", "resumed", "told", registered=("told",),
                               started_calls=(("owner", "active-call"), ("resumed", "resumed-call")))
                    self.release_mocks["lookup"].return_value = build_hold.RecipientGone() if gone else build_hold.RecipientLive("/test/socket")
                    self.release_mocks["send"].return_value = build_hold.ReleaseSendFailed("send failed")
                    with redirect_stderr(io.StringIO()):
                        _ = self.advance()
                    self.assertEqual(self.entry("owner")["state"], "RecipientGone" if gone else "DeliveryFailed")
                    self.assertEqual(self.entry("owner")["active_call_id"], "active-call")
                    order = [entry["session_id"] for entry in self.cycle()["entries"]]
                    self.assertEqual(order, ["owner", "resumed", "told"])
                    self.release_mocks["lookup"].return_value = build_hold.RecipientLive("/test/socket")
                    self.release_mocks["send"].return_value = build_hold.ReleaseDelivered()
                    self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S - 1)
                    _ = self.advance()
                    self.assertEqual([entry["session_id"] for entry in self.cycle()["entries"]], order)
                    self.assertEqual(self.entry("resumed")["state"], "AwaitingRelease")
                    self.assertIsInstance(build_hold.read_admission("resumed", "resumed-call"), build_hold.AwaitingTurn)
                    self.assertNotIn("resumed", self.sent())
                    self.release_mocks["lookup"].assert_called_once_with("owner")
                    if ending == "mark":
                        self.now = self.returned("owner", "active-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1)
                        _ = self.advance()
                        self.assertEqual([entry["session_id"] for entry in self.cycle()["entries"]], order)
                        self.assertEqual(self.entry("resumed")["state"], "AwaitingRelease")
                        self.assertNotIn("resumed", self.sent())
                        self.release_mocks["lookup"].assert_called_once_with("owner")
                    self.now += timedelta(seconds=1)
                    _ = self.advance()
                    self.assertEqual(self.entry("owner")["active_call_id"], "")
                    self.assertEqual(self.release_mocks["lookup"].call_count, 2)
                    self.assertEqual(self.entry("resumed")["active_call_id"], "resumed-call")
                    self.assertIsInstance(build_hold.read_admission("resumed", "resumed-call"), build_hold.AdmittedTurn)
                    self.assertEqual(self.sent()[-1], "resumed")
                    self.assertEqual(self.entry("told")["state"], "AwaitingRelease")
                    self.now = self.returned("resumed", "resumed-call") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
                    _ = self.advance()
                    self.assertEqual(self.entry("told")["state"], "ReleasedAwaitingAdmission")
                    self.assertEqual(self.sent()[-2:], ["resumed", "told"])

    def test_pending_call_on_terminal_entry_prevents_told_queue_insertion(self) -> None:
        self.begin("owner", "told", calls=(("owner", "pending"),))
        cycle = self.cycle()
        entry = cycle["entries"][0]
        build_hold.store_release_state(entry, build_hold.DeliveryFailed(self.now))
        entry["session_pending"] = False
        build_hold.save_cycle(cycle)
        self.assertFalse(self.advance()[0])
        self.assertEqual([entry["session_id"] for entry in self.cycle()["entries"]], ["owner"])
        self.assertEqual(self.sent(), [])
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner"])
        self.assertEqual(self.entry("owner")["active_call_id"], "pending")

    def test_final_line_names_unreached_recipients_in_entry_order(self) -> None:
        self.begin("queued", "lookup", "gone", "failed", registered=("gone", "failed", "lookup", "queued"))

        def lookup(key: str) -> build_hold.RecipientLive | build_hold.RecipientGone | build_hold.RecipientLookupFailed:
            if key == "gone":
                return build_hold.RecipientGone()
            if key == "lookup":
                return build_hold.RecipientLookupFailed("registry unreadable")
            return build_hold.RecipientLive("/test/socket")

        self.release_mocks["lookup"].side_effect = lookup
        self.release_mocks["send"].side_effect = [build_hold.ReleaseSendFailed("send failed"), build_hold.ReleaseQueued()]
        with redirect_stderr(io.StringIO()):
            complete, _ = self.advance()
        self.assertFalse(complete)
        self.assertEqual([self.entry(key)["delivery"]["outcome"] for key in ("gone", "failed", "lookup", "queued")],
                         ["RecipientGone", "DeliveryFailed", "DeliveryFailed", "QueuedUnconfirmed"])
        self.assertEqual(self.sent(), ["failed", "queued"])
        self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
        self.assertTrue(self.advance()[0])
        self.assertEqual(self.entry("queued")["state"], "NoAdmissionAck")
        line = self.final_line()
        self.assertTrue(line.startswith("released, builds may resume. "))
        self.assertTrue(line.endswith(" Not reached, tell them yourself: name gone (session gone), name failed (send failed), name lookup (send failed), name queued (queued, no answer)"), line)

    def test_each_unavailable_recipient_attempt_ends_without_waiting_for_admission(self) -> None:
        for failure in ("gone", "lookup", "send"):
            with self.subTest(failure=failure):
                self.begin("owner")
                self.release_mocks["lookup"].return_value = (build_hold.RecipientGone() if failure == "gone" else
                                            build_hold.RecipientLookupFailed("registry denied") if failure == "lookup" else
                                            build_hold.RecipientLive("/test/socket"))
                self.release_mocks["send"].return_value = build_hold.ReleaseSendFailed("send denied")
                with redirect_stderr(io.StringIO()):
                    self.assertTrue(self.advance()[0])
                line = self.final_line()
                self.assertIn("name owner (session gone)" if failure == "gone" else "name owner (send failed)", line)
                self.assertEqual(self.release_mocks["send"].call_count, int(failure == "send"))

    def test_queued_answer_confirms_delivery_and_removes_final_warning(self) -> None:
        for answer in ("WaitingForMemory", "MemoryGateReturned", "nothing"):
            with self.subTest(answer=answer):
                self.begin("owner")
                self.release_mocks["send"].return_value = build_hold.ReleaseQueued()
                _ = self.advance()
                self.assertEqual(self.entry("owner")["delivery"]["outcome"], "QueuedUnconfirmed")
                if answer == "nothing":
                    _ = build_hold.answer_nothing_to_build("owner")
                else:
                    if answer == "WaitingForMemory":
                        self.assertIn("WaitingForMemory", build_hold.mark_gate("owner", "WaitingForMemory"))
                        self.assertEqual(self.entry("owner")["delivery"]["outcome"], "Delivered")
                    self.now = self.returned("owner") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
                self.assertEqual(self.entry("owner")["delivery"]["outcome"], "Delivered")
                self.assertTrue(self.advance()[0])
                self.assertNotIn("Not reached", self.final_line())
                self.assertEqual(self.sent(), ["owner"])

    def test_delivered_silent_sessions_are_reached_without_final_warning(self) -> None:
        self.begin("first", "second")
        started = self.now
        _ = self.advance()
        self.now += timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        _ = self.advance()
        self.assertEqual(build_hold.read_release_state(self.entry("first")), build_hold.NoAdmissionAck(started))
        self.assertEqual(self.sent(), ["first", "second"])
        self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S - 1)
        self.assertFalse(self.advance()[0])
        self.assertEqual(self.entry("second")["state"], "ReleasedAwaitingAdmission")
        self.now += timedelta(seconds=1)
        self.assertTrue(self.advance()[0])
        self.assertEqual([entry["state"] for entry in self.cycle()["entries"]], ["NoAdmissionAck", "NoAdmissionAck"])
        self.assertEqual(self.sent(), ["first", "second"])
        self.assertNotIn("Not reached", self.final_line())

    def test_late_nothing_to_build_confirms_queued_delivery_without_reopening_turn(self) -> None:
        self.begin("owner", "peer")
        self.release_mocks["send"].side_effect = [build_hold.ReleaseQueued(), build_hold.ReleaseDelivered()]
        _ = self.advance()
        self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
        _ = self.advance()
        self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
        self.assertEqual(self.sent(), ["owner", "peer"])
        expected = f"nothing-to-build ignored: {build_hold.entry_text(self.entry('owner'))}"
        self.assertEqual(build_hold.answer_nothing_to_build("owner"), expected)
        self.assertEqual(self.entry("owner")["delivery"]["outcome"], "Delivered")
        self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
        self.assertEqual(self.entry("peer")["state"], "ReleasedAwaitingAdmission")
        _ = build_hold.answer_nothing_to_build("peer")
        self.assertTrue(self.advance()[0])
        self.assertNotIn("Not reached", self.final_line())

    def test_late_gate_mark_confirms_queued_delivery_only_for_current_generation(self) -> None:
        for state in ("WaitingForMemory", "MemoryGateReturned"):
            for current_generation in (False, True):
                with self.subTest(state=state, current_generation=current_generation):
                    self.begin("owner", "peer")
                    self.now += timedelta(seconds=1)
                    _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("holder", "holder"), "renewed hold",
                                              build_hold.NoReleaseEta(), self.now)
                    cycle = self.cycle()
                    cycle["release_started_at"] = self.now.isoformat()
                    build_hold.save_cycle(cycle)
                    self.release_mocks["send"].side_effect = [build_hold.ReleaseQueued(), build_hold.ReleaseDelivered()]
                    _ = self.advance()
                    self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
                    _ = self.advance()
                    self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
                    cycle = self.cycle()
                    generation = cycle.get("holder_generation", 1)
                    self.assertGreater(generation, 1)
                    mark_generation = generation if current_generation else generation - 1
                    self.assertEqual(build_hold.mark_gate("owner", "unknown-state", generation=generation), "mark ignored")
                    self.assertEqual(self.entry("owner")["delivery"]["outcome"], "QueuedUnconfirmed")
                    self.assertEqual(build_hold.mark_gate("owner", state, "Granted", cycle_id=cycle["id"],
                                                         generation=mark_generation), "mark ignored")
                    self.assertEqual(self.entry("owner")["delivery"]["outcome"],
                                     "Delivered" if current_generation else "QueuedUnconfirmed")
                    self.assertEqual(self.entry("owner")["state"], "NoAdmissionAck")
                    self.assertEqual(self.entry("peer")["state"], "ReleasedAwaitingAdmission")
                    _ = build_hold.answer_nothing_to_build("peer")
                    self.assertTrue(self.advance()[0])
                    line = self.final_line()
                    if current_generation:
                        self.assertNotIn("Not reached", line)
                    else:
                        self.assertIn("Not reached, tell them yourself: name owner (queued, no answer)", line)

    def test_call_gate_marks_preserve_delivery_and_second_call_sends_no_second_release(self) -> None:
        for outcome in ("delivered", "queued", "failed", "gone"):
            with self.subTest(outcome=outcome):
                self.begin("owner", calls=(("owner", "call-a"), ("owner", "call-b")))
                self.release_mocks["lookup"].return_value = build_hold.RecipientGone() if outcome == "gone" else build_hold.RecipientLive("/test/socket")
                self.release_mocks["send"].return_value = (build_hold.ReleaseQueued() if outcome == "queued" else
                                          build_hold.ReleaseSendFailed("send failed") if outcome == "failed" else
                                          build_hold.ReleaseDelivered())
                with redirect_stderr(io.StringIO()):
                    _ = self.advance()
                self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)
                self.now = self.returned("owner", "call-a") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
                expected = "RecipientGone" if outcome == "gone" else "DeliveryFailed" if outcome == "failed" else "Delivered"
                self.assertEqual(self.entry("owner")["delivery"],
                                 {"generation": self.cycle().get("holder_generation", 1), "outcome": expected})
                _ = self.advance()
                self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)
                self.assertEqual(self.release_mocks["lookup"].call_count, 1)
                self.assertEqual(self.release_mocks["send"].call_count, int(outcome != "gone"))
                self.now = self.returned("owner", "call-b") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
                self.assertTrue(self.advance()[0])
                line = self.final_line()
                if outcome in {"failed", "gone"}:
                    self.assertIn("name owner (session gone)" if outcome == "gone" else "name owner (send failed)", line)
                else:
                    self.assertNotIn("Not reached", line)

    def test_new_holder_generation_permits_one_new_notification(self) -> None:
        self.begin("owner", calls=(("owner", "call-a"),))
        _ = self.advance()
        self.now = self.returned("owner", "call-a") + timedelta(seconds=build_hold.RELEASE_SETTLE_S)
        prior = self.entry("owner")["delivery"]["generation"]
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second-holder", "second-holder"), "new hold", build_hold.NoReleaseEta(), self.now)
        cycle = self.cycle()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner", "owner"])
        self.assertGreater(self.entry("owner")["delivery"]["generation"], prior)
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner", "owner"])

    def test_told_session_with_only_interrupted_call_still_receives_its_release(self) -> None:
        self.begin("owner", calls=(("owner", "interrupted"),))
        build_hold.remove_call("owner", "interrupted")
        self.assertEqual(self.entry("owner")["calls"], [])
        self.assertFalse(self.entry("owner")["session_pending"])
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner"])
        self.assertEqual(self.entry("owner")["state"], "ReleasedAwaitingAdmission")

    def test_untold_key_with_nothing_pending_is_never_looked_up_or_named(self) -> None:
        self.begin("told", calls=(("untold", "interrupted"),))
        build_hold.remove_call("untold", "interrupted")
        _ = self.advance()
        self.assertEqual(self.release_mocks["lookup"].call_args_list, [mock.call("told")])
        self.assertEqual(self.entry("untold")["state"], "NoPendingTurn")
        _ = build_hold.answer_nothing_to_build("told")
        self.assertTrue(self.advance()[0])
        self.assertNotIn("Not reached", self.final_line())

    def test_verify_call_key_is_never_notified_or_named_even_in_recipients(self) -> None:
        self.begin("verify:call-a", calls=(("verify:call-a", "call-a"),))
        _ = self.advance()
        self.release_mocks["lookup"].assert_not_called()
        self.release_mocks["send"].assert_not_called()
        self.assertIsInstance(build_hold.read_admission("verify:call-a", "call-a"), build_hold.AdmittedTurn)
        self.now += timedelta(seconds=build_hold.NO_ADMISSION_ACK_S)
        self.assertTrue(self.advance()[0])
        cycle = self.cycle()
        cycle["entries"][0]["delivery"] = {"generation": cycle.get("holder_generation", 1), "outcome": "RecipientGone"}
        build_hold.save_cycle(cycle)
        self.assertNotIn("Not reached", self.final_line())

    def test_legacy_no_registration_entry_is_loaded_and_delivered(self) -> None:
        self.begin("owner")
        cycle = self.cycle()
        entry = build_hold.new_entry("owner", "name owner")
        build_hold.store_release_state(entry, build_hold.NoRegistration())
        _ = cast(dict[str, object], cast(object, entry)).pop("delivery")
        cycle["entries"] = [entry]
        build_hold.save_cycle(cycle)
        self.assertTrue(self.entry("owner")["session_pending"])
        self.assertEqual(self.entry("owner")["delivery"], {"generation": 0, "outcome": "NotAttempted"})
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner"])
        self.assertEqual(self.entry("owner")["state"], "ReleasedAwaitingAdmission")

    def test_phase_three_record_without_delivery_loads_without_rewriting(self) -> None:
        self.begin("owner", registered=("owner",))
        cycle = self.cycle()
        _ = cast(dict[str, object], cast(object, cycle["entries"][0])).pop("delivery")
        build_hold.save_cycle(cycle)
        path = self.scratch / "release" / cycle["id"] / "cycle.json"
        before = path.read_bytes()
        self.assertEqual(self.entry("owner")["delivery"], {"generation": 0, "outcome": "NotAttempted"})
        self.assertEqual(path.read_bytes(), before)
        _ = self.advance()
        self.assertEqual(self.sent(), ["owner"])

    def test_told_session_registering_after_queue_insertion_is_delivered_once(self) -> None:
        self.begin("first", "second")
        _ = self.advance()
        _ = build_hold.register_wait("second")
        self.assertEqual([entry["session_id"] for entry in self.cycle()["entries"]], ["first", "second"])
        _ = build_hold.answer_nothing_to_build("first")
        _ = self.advance()
        _ = build_hold.register_wait("second")
        _ = self.advance()
        self.assertEqual(self.sent(), ["first", "second"])
        _ = build_hold.answer_nothing_to_build("second")
        self.assertTrue(self.advance()[0])
        self.assertEqual(self.sent(), ["first", "second"])

    def test_empty_recipient_list_keeps_registration_wait_and_timeout_text(self) -> None:
        self.begin()
        self.now += timedelta(seconds=build_hold.NO_REGISTRATION_S - 1)
        self.assertEqual(self.advance(), (False, "waiting for registrations"))
        self.now += timedelta(seconds=1)
        self.assertEqual(self.advance(), (True, "NoRegistration timeout; no recipients registered"))
        self.release_mocks["lookup"].assert_not_called()
        self.release_mocks["send"].assert_not_called()


class CallAdmissionTests(IsolatedBuildHoldTest):
    now: datetime = datetime.min

    @override
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now().astimezone()
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("holder", "holder"), "the test", build_hold.NoReleaseEta(), self.now)
        _ = self.enterContext(mock.patch.object(build_hold, "process_start_time", return_value="test-process"))
        _ = self.enterContext(mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")))
        _ = self.enterContext(mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()))

    def cycle(self) -> build_hold.HoldCycle:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        return cycle

    def register(self, key: str, call_id: str) -> None:
        _ = build_hold.register_wait(key, call_id=call_id, pid=12345, proc_start="test-process")

    def advance(self) -> None:
        cycle = self.cycle()
        cycle["release_started_at"] = self.now.isoformat()
        build_hold.save_cycle(cycle)
        _ = build_hold.advance_release(cycle, self.now)

    def complete(self, key: str, call_id: str) -> None:
        _ = build_hold.mark_gate(key, "MemoryGateReturned", "Granted", call_id=call_id, cycle_id=self.cycle()["id"])
        self.now = datetime.now().astimezone() + timedelta(seconds=build_hold.RELEASE_SETTLE_S + 1)

    def write_legacy_completed_session(self) -> str:
        cycle = self.cycle()
        cycle["entries"] = [cast(build_hold.ReleaseEntry, cast(object, {
            "session_id": "owner", "name": "owner", "state": "MemoryGateReturned",
            "wait_ended_at": self.now.isoformat(), "outcome": "Granted",
        }))]
        _ = cycle.pop("holder_generation", 1)
        _ = cycle.pop("holder_signature", "")
        build_hold.save_cycle(cycle)
        return cycle["id"]

    def test_waiter_key_uses_marker_owner_before_shell_id(self) -> None:
        active = self.scratch / "active"
        active.mkdir()
        run = self.scratch / "delegate-run"
        _ = (active / "owner").write_text(str(run) + "\n")
        with mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": str(run), "CLAUDE_CODE_SESSION_ID": "stale"}):
            self.assertEqual(build_hold.waiter_key("call-a"), "owner")

    def test_unowned_waiter_key_names_the_verification_call(self) -> None:
        self.assertEqual(build_hold.waiter_key("call-a"), "verify:call-a")

    def test_two_calls_share_owner_and_receive_distinct_turns(self) -> None:
        self.register("owner", "call-a")
        self.register("owner", "call-b")
        self.assertEqual(len(self.cycle()["entries"]), 1)
        self.assertEqual([call["call_id"] for call in self.cycle()["entries"][0]["calls"]], ["call-a", "call-b"])
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AwaitingTurn)
        self.complete("owner", "call-a")
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)

    def test_session_registration_and_call_share_release_and_mark_advances_to_peer(self) -> None:
        _ = build_hold.register_wait("owner")
        self.register("owner", "seat-call")
        self.register("peer", "peer-call")
        with mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()) as notification:
            self.advance()
            self.assertEqual(notification.call_count, 1)
            notified = cast(build_hold.ReleaseEntry, notification.call_args.args[0])
            self.assertEqual(notified["session_id"], "owner")
        self.assertIsInstance(build_hold.read_admission("owner", "seat-call"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("owner"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("peer", "peer-call"), build_hold.AwaitingTurn)
        entry = self.cycle()["entries"][0]
        self.assertEqual(entry["active_call_id"], "seat-call")
        _ = build_hold.mark_gate("owner", "MemoryGateReturned", "Granted", call_id="seat-call", cycle_id=self.cycle()["id"])
        self.assertEqual(self.cycle()["entries"][0]["state"], "MemoryGateReturned")
        self.now = datetime.now().astimezone() + timedelta(seconds=build_hold.RELEASE_SETTLE_S + 1)
        self.advance()
        self.assertIsInstance(build_hold.read_admission("peer", "peer-call"), build_hold.AdmittedTurn)
        self.assertLess(build_hold.RELEASE_SETTLE_S + 1, build_hold.NO_ADMISSION_ACK_S)

    def test_call_after_nothing_to_build_is_admitted_without_record_change(self) -> None:
        _ = build_hold.register_wait("owner")
        self.advance()
        self.assertIn("NothingToBuild", build_hold.answer_nothing_to_build("owner"))
        before = self.cycle()
        self.register("owner", "seat-call")
        self.assertEqual(self.cycle(), before)
        self.assertIsInstance(build_hold.read_admission("owner", "seat-call"), build_hold.AdmittedTurn)
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "seat-call"), build_hold.AdmittedTurn)

    def test_call_after_completed_turn_is_admitted_without_record_change(self) -> None:
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second", "second"), "another test", build_hold.NoReleaseEta(), self.now)
        self.register("owner", "call-a")
        self.advance()
        self.complete("owner", "call-a")
        before = self.cycle()
        self.register("owner", "call-b")
        self.assertEqual(self.cycle(), before)
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)

    def test_registration_rejoins_after_each_terminal_entry_state(self) -> None:
        states: tuple[build_hold.ReleaseState, ...] = (
            build_hold.NothingToBuild(self.now), build_hold.MemoryGateReturned(self.now, "Granted"),
            build_hold.NoAdmissionAck(self.now), build_hold.NoRegistration(),
            build_hold.RecipientGone(), build_hold.DeliveryFailed(self.now),
        )
        for index, state in enumerate(states):
            with self.subTest(state=type(state).__name__):
                key = f"owner-{index}"
                cycle = self.cycle()
                cycle["release_started_at"] = ""
                entry = build_hold.new_entry(key, key)
                entry["session_pending"] = False
                build_hold.store_release_state(entry, state)
                cycle["entries"].append(entry)
                build_hold.save_cycle(cycle)
                self.register(key, "new-call")
                self.assertIsInstance(build_hold.read_admission(key, "new-call"), build_hold.AwaitingTurn)

    def test_active_call_admission_for_each_release_state(self) -> None:
        states: tuple[tuple[build_hold.ReleaseState, bool], ...] = (
            (build_hold.AwaitingRelease(), False),
            (build_hold.DeliveryQueued(self.now), True),
            (build_hold.ReleasedAwaitingAdmission(self.now), True),
            (build_hold.WaitingForMemory(self.now, self.now), True),
            (build_hold.MemoryGateReturned(self.now, "Granted"), True),
            (build_hold.RecipientGone(), True), (build_hold.DeliveryFailed(self.now), True),
        )
        self.register("owner", "call-a")
        for state, admitted in states:
            with self.subTest(state=type(state).__name__):
                cycle = self.cycle()
                entry = cycle["entries"][0]
                entry["active_call_id"] = "call-a"
                entry["calls"][0]["admitted_generation"] = cycle.get("holder_generation", 1) if isinstance(state, build_hold.MemoryGateReturned) else 0
                build_hold.store_release_state(entry, state)
                build_hold.save_cycle(cycle)
                expected = build_hold.AdmittedTurn if admitted else build_hold.AwaitingTurn
                self.assertIsInstance(build_hold.read_admission("owner", "call-a"), expected)

    def test_unowned_turn_is_granted_without_socket_notification(self) -> None:
        self.register("verify:call-a", "call-a")
        with mock.patch.object(build_hold, "socket_for", side_effect=AssertionError("No session socket")):
            with mock.patch.object(build_hold, "send_release", side_effect=AssertionError("No session notification")):
                self.advance()
        self.assertIsInstance(build_hold.read_admission("verify:call-a", "call-a"), build_hold.AdmittedTurn)

    def test_notification_failures_still_admit_the_live_call(self) -> None:
        for gone in (True, False):
            with self.subTest(gone=gone):
                key = f"owner-{gone}"
                cycle = self.cycle()
                cycle["release_started_at"] = ""
                build_hold.save_cycle(cycle)
                self.register(key, "call-a")
                with mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientGone() if gone else build_hold.RecipientLive("/test/socket")):
                    with mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseSendFailed("send failed")):
                        self.advance()
                self.assertIsInstance(build_hold.read_admission(key, "call-a"), build_hold.AdmittedTurn)
                entry = next(entry for entry in self.cycle()["entries"] if entry["session_id"] == key)
                self.assertEqual(entry["state"], "RecipientGone" if gone else "DeliveryFailed")
                self.complete(key, "call-a")

    def test_cancel_removes_one_call_and_preserves_its_peer(self) -> None:
        self.register("owner", "call-a")
        self.register("owner", "call-b")
        self.advance()
        build_hold.remove_call("owner", "call-a")
        self.assertEqual([call["call_id"] for call in self.cycle()["entries"][0]["calls"]], ["call-b"])
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)

    def test_vanished_process_is_dropped_without_removing_live_peer(self) -> None:
        self.register("owner", "gone")
        self.register("owner", "live")

        def live(call: build_hold.PendingCall) -> bool:
            return call["call_id"] == "live"

        with mock.patch.object(build_hold, "call_is_live", side_effect=live):
            self.advance()
        self.assertEqual([call["call_id"] for call in self.cycle()["entries"][0]["calls"]], ["live"])
        self.assertIsInstance(build_hold.read_admission("owner", "live"), build_hold.AdmittedTurn)

    def test_process_identity_rejects_a_reused_pid(self) -> None:
        call: build_hold.PendingCall = {"call_id": "old", "pid": 12345, "proc_start": "old-start", "started": False, "admitted_generation": 0}
        with mock.patch.object(build_hold, "process_start_time", return_value="new-start"):
            self.assertFalse(build_hold.call_is_live(call))
        with mock.patch.object(build_hold, "process_start_time", return_value=""):
            self.assertFalse(build_hold.call_is_live(call))

    def test_session_mark_and_nothing_to_build_do_not_complete_active_call(self) -> None:
        self.register("owner", "seat-call")
        self.advance()
        _ = build_hold.mark_gate("owner", "MemoryGateReturned", "Granted")
        _ = build_hold.answer_nothing_to_build("owner")
        entry = self.cycle()["entries"][0]
        self.assertEqual(entry["active_call_id"], "seat-call")
        self.assertNotEqual(entry["state"], "MemoryGateReturned")
        self.assertIsInstance(build_hold.read_admission("owner"), build_hold.AdmittedTurn)

    def test_wrong_call_or_cycle_cannot_mark_an_active_turn(self) -> None:
        self.register("owner", "call-a")
        self.advance()
        before = self.cycle()
        _ = build_hold.mark_gate("owner", "MemoryGateReturned", "Granted", call_id="call-b", cycle_id=before["id"])
        _ = build_hold.mark_gate("owner", "MemoryGateReturned", "Granted", call_id="call-a", cycle_id="f" * 32)
        self.assertEqual(self.cycle(), before)

    def test_admission_survives_completion_of_the_gate_in_same_generation(self) -> None:
        self.register("owner", "call-a")
        self.register("owner", "call-b")
        self.advance()
        self.complete("owner", "call-a")
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AdmittedTurn)
        self.register("owner", "call-a")
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)

    def test_new_holder_invalidates_admission_and_resuming_call_precedes_unstarted_calls(self) -> None:
        self.register("owner-a", "started")
        self.advance()
        self.complete("owner-a", "started")
        cycle_id = self.cycle()["id"]
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second", "second"), "another test", build_hold.NoReleaseEta(), self.now)
        self.assertEqual(self.cycle()["id"], cycle_id)
        self.assertIsInstance(build_hold.read_admission("owner-a", "started"), build_hold.AwaitingTurn)
        self.register("owner-b", "pending")
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner-a", "started"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("owner-b", "pending"), build_hold.AwaitingTurn)

    def test_legacy_completed_session_keeps_its_admission_without_rewriting_record(self) -> None:
        cycle_id = self.write_legacy_completed_session()
        path = self.scratch / "release" / cycle_id / "cycle.json"
        before = path.read_bytes()
        self.assertEqual(build_hold.read_admission("owner"), build_hold.AdmittedTurn(cycle_id, 1))
        self.assertEqual(path.read_bytes(), before)

    def test_new_holder_invalidates_legacy_completed_session_admission(self) -> None:
        cycle_id = self.write_legacy_completed_session()
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second", "second"), "another test",
                                  build_hold.NoReleaseEta(), self.now + timedelta(seconds=1))
        self.assertEqual(build_hold.read_admission("owner"), build_hold.AwaitingTurn(cycle_id, 2))
        _ = build_hold.register_wait("owner")
        self.advance()
        self.assertEqual(build_hold.read_admission("owner"), build_hold.AdmittedTurn(cycle_id, 2))

    def test_missing_cycle_has_a_semantic_admission_result(self) -> None:
        (self.scratch / "release" / "current").unlink()
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.NoCycle)

    def test_changed_cycle_does_not_keep_old_admission(self) -> None:
        self.register("owner", "call-a")
        self.advance()
        old_id = self.cycle()["id"]
        fresh = build_hold.open_cycle(self.now)
        self.assertNotEqual(fresh["id"], old_id)
        decision = build_hold.read_admission("owner", "call-a")
        self.assertIsInstance(decision, build_hold.AwaitingTurn)
        assert isinstance(decision, build_hold.AwaitingTurn)
        self.assertEqual(decision.cycle_id, fresh["id"])

    def test_unreadable_cycle_returns_diagnostic_without_repairing_record(self) -> None:
        path = self.scratch / "release" / self.cycle()["id"] / "cycle.json"
        _ = path.write_text("{broken")
        before = path.read_bytes()
        decision = build_hold.read_admission("owner", "call-a")
        self.assertIsInstance(decision, build_hold.AdmissionReadError)
        assert isinstance(decision, build_hold.AdmissionReadError)
        self.assertTrue(decision.reason)
        self.assertEqual(path.read_bytes(), before)


    def test_reused_pid_is_dropped_by_driver_before_grant(self) -> None:
        self.register("owner", "reused")
        self.register("owner", "live")
        cycle = self.cycle()
        cycle["entries"][0]["calls"][0]["proc_start"] = "previous-process"
        build_hold.save_cycle(cycle)
        self.advance()
        self.assertEqual([call["call_id"] for call in self.cycle()["entries"][0]["calls"]], ["live"])
        self.assertIsInstance(build_hold.read_admission("owner", "live"), build_hold.AdmittedTurn)

    def test_driver_drops_vanished_active_call_and_admits_its_peer(self) -> None:
        self.register("owner", "gone")
        self.register("owner", "live")
        self.advance()
        self.assertEqual(self.cycle()["entries"][0]["active_call_id"], "gone")

        def live(call: build_hold.PendingCall) -> bool:
            return call["call_id"] == "live"

        with mock.patch.object(build_hold, "call_is_live", side_effect=live):
            self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "live"), build_hold.AdmittedTurn)

    def test_resuming_call_precedes_unstarted_peer_under_same_owner(self) -> None:
        self.register("owner", "started")
        self.advance()
        self.complete("owner", "started")
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second", "second"), "another test", build_hold.NoReleaseEta(), self.now)
        self.register("owner", "pending")
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "started"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("owner", "pending"), build_hold.AwaitingTurn)

    def test_hold_notice_says_verify_waits_and_launch_retries_without_appending_wait(self) -> None:
        notice = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second", "second"), "another test", build_hold.NoReleaseEta(), self.now)
        self.assertEqual(notice, "natedev is held for another test. verify.sh waits by itself; an app launch is refused until the release, so launch again after it. Tests that can run on the Mac keep running there. Stop only cargo you started outside verify.sh. No release after 2 h: ask me.")


    def test_queued_notification_authorizes_the_active_call(self) -> None:
        self.register("owner", "call-a")
        with mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseQueued()):
            self.advance()
        self.assertEqual(self.cycle()["entries"][0]["state"], "DeliveryQueued")
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)
        self.assertIn("WaitingForMemory", build_hold.mark_gate("owner", "WaitingForMemory", call_id="call-a", cycle_id=self.cycle()["id"]))
        self.complete("owner", "call-a")
        self.assertIsInstance(build_hold.read_admission("owner", "call-a"), build_hold.AdmittedTurn)


    def test_old_generation_mark_cannot_complete_a_new_turn_for_same_call(self) -> None:
        self.register("owner", "call-a")
        self.advance()
        before = self.cycle()
        generation = before.get("holder_generation", 1)
        _ = build_hold.mark_gate("owner", "WaitingForMemory", call_id="call-a", cycle_id=before["id"], generation=generation)
        _ = build_hold.start_hold(self.scratch / "holders", build_hold.HolderIdentity("second", "second"), "another test", build_hold.NoReleaseEta(), self.now)
        self.advance()
        current = self.cycle()
        self.assertNotEqual(current.get("holder_generation", 1), generation)
        _ = build_hold.mark_gate("owner", "MemoryGateReturned", "Granted", call_id="call-a", cycle_id=before["id"], generation=generation)
        self.assertEqual(self.cycle(), current)



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
                    ["python3", str(SCRIPT), "hold", "--holder", "alice", "--for", "render checks"],
                    env=dict(os.environ), capture_output=True, text=True, timeout=1.5, check=False)
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
            self.assertEqual(build_hold.natedev_holds(self.scratch / "holders"), build_hold.NatedevHolds((), ()))

    def test_holder_file_without_a_cycle_is_in_force(self) -> None:
        folder = self.scratch / "holders"
        folder.mkdir()
        _ = write_holder(folder, "session-id", "2026-10-12T10:00:00+00:00", "render checks")
        result = build_hold.natedev_holds(folder)
        self.assertEqual([holder.key for holder in result.in_force], ["session-id"])
        self.assertEqual(result.releasing, ())

    def test_released_at_with_matching_since_puts_file_in_releasing_and_reads_holders_once(self) -> None:
        folder = self.begin()
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["holders"]["session-id"]["released_at"] = datetime.now(timezone.utc).isoformat()
        build_hold.save_cycle(cycle)
        with mock.patch.object(build_hold, "read_holders", wraps=build_hold.read_holders) as read:
            result = build_hold.natedev_holds(folder)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result.in_force, ())
        self.assertEqual([holder.key for holder in result.releasing], ["session-id"])

    def test_present_holder_and_damaged_cycle_raise_release_record_read_error(self) -> None:
        folder = self.begin()
        _ = (self.scratch / "release" / "current").write_text("damaged")
        with self.assertRaises(build_hold.ReleaseRecordReadError):
            _ = build_hold.natedev_holds(folder)

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

if __name__ == "__main__":
    _ = unittest.main()
