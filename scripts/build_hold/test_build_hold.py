"""Build holds with temporary holder files and synthetic machine readings."""

from __future__ import annotations

import json
import io
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

import build_hold


SCRIPT = Path(__file__).with_name("build_hold.py")
MEMORY_GATE = Path(__file__).resolve().parent.parent / "lint" / "memory_gate.sh"


def write_holder(folder: Path, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
    path = folder / name
    _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
    return path


class IsolatedBuildHoldTest(unittest.TestCase):
    scratch: Path = Path()

    @override
    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        meminfo = self.scratch / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n")
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch),
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

    def test_wait_still_registers_shell_session(self) -> None:
        cycle = build_hold.read_cycle()
        assert cycle is not None
        cycle["entries"] = []
        build_hold.save_cycle(cycle)
        code, output = self.command("wait")
        self.assertEqual(code, 0)
        self.assertIn("registered stale-session", output)
        self.assertEqual([entry["session_id"] for entry in self.entries()], ["stale-session"])

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
                notice = build_hold.release_hold(folder, "first")
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
        key, label = build_hold.holder_of(given)
        return build_hold.write_hold(folder, key, "one test", build_hold.release_request(None, None),
                                     datetime.now().astimezone(), label)

    def test_a_hold_is_kept_by_session_id_so_a_rename_between_hold_and_release_loses_nothing(self) -> None:
        folder = self.scratch / "holders"
        self.session("id-1", "old-name")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "id-1"}):
            self.assertIn("/build_hold from old-name:", self.hold(folder, None))
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
        self.assertEqual(build_hold.release_hold(folder, "id-1"), "released, builds may resume.")

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
                self.assertIn("/build_hold release sets it aside and ends the hold", status.stdout)
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
                self.assertIn("/build_hold release sets it aside and ends the hold", result.stdout)
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

    def test_hold_message_tells_recipient_to_register_at_once(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            result = self.run_cli(Path(scratch), "hold", "--holder", "slot", "--for", "the focused test")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("build_hold.py wait", result.stdout)

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
            _ = build_hold.write_hold(folder, "slot", "clock change", request, now)
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
                with mock.patch.dict(os.environ, {"TZ": "America/New_York", "BUILD_HOLD_DIR": str(folder)}):
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
    def begin(self, *session_ids: str) -> datetime:
        now = datetime.now().astimezone()
        _ = build_hold.start_hold(self.scratch / "holders", "holder", "the test", build_hold.NoReleaseEta(), now)
        build_hold.record_recipients([("shared name", session_id) for session_id in session_ids])
        for session_id in session_ids:
            self.assertIn("registered", build_hold.register_wait(session_id))
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
        return build_hold.advance_release(cycle, now)

    def test_same_name_sessions_receive_distinct_direct_sends_in_arrival_order(self) -> None:
        now = self.begin("session-a", "session-b")
        sent: list[tuple[str, str]] = []

        def deliver(entry: build_hold.ReleaseEntry, socket: str) -> int:
            sent.append((entry["session_id"], socket))
            return 0

        def socket_for(session_id: str) -> str:
            return f"/tmp/{session_id}.sock"

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

    def test_delayed_first_build_keeps_next_session_held(self) -> None:
        now = self.begin("first", "second")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> int:
            sent.append(entry["session_id"])
            return 0

        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            cycle = build_hold.read_cycle()
            assert cycle is not None
            cycle["entries"][0]["released_at"] = (now - timedelta(minutes=10)).isoformat()
            build_hold.save_cycle(cycle)
            _ = self.advance(now)
            self.assertEqual(sent, ["first"])
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            _ = build_hold.mark_gate("first", "MemoryGateReturned", "Granted")
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1))
            self.assertEqual(sent, ["first"])
            _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertEqual(sent, ["first", "second"])

    def test_memory_wait_past_five_minutes_still_blocks_next_release(self) -> None:
        now = self.begin("first", "second")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> int:
            sent.append(entry["session_id"])
            return 0

        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
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
        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", return_value=0), \
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
        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", return_value=0):
            _ = self.advance(now)
            released = build_hold.aware_instant(self.entries()[0].get("released_at", ""))
            _ = self.advance(released + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S - 1))
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            _ = self.advance(released + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S))
            self.assertEqual(self.entries()[0]["state"], "NoAdmissionAck")
            self.assertEqual(self.entries()[1]["state"], "ReleasedAwaitingAdmission")

    def test_unreadable_session_records_are_a_failed_delivery_not_a_gone_session(self) -> None:
        now = self.begin("unknown", "next")

        def socket_for(session_id: str) -> str | None:
            if session_id == "unknown":
                raise build_hold.SessionLookupUnavailable("sessions: one or more registry files could not be read")
            return "/tmp/next.sock"

        errors = io.StringIO()
        with mock.patch.object(build_hold, "socket_for", side_effect=socket_for), \
             mock.patch.object(build_hold, "send_release", return_value=0) as send, redirect_stderr(errors):
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
            self.assertEqual(build_hold.socket_for("session-a"), "/tmp/late.sock")
            self.assertEqual(sleep.call_count, 2)
        with mock.patch.object(subprocess, "run", side_effect=answers(3, 3, 3)), mock.patch.object(time, "sleep"), \
             self.assertRaisesRegex(build_hold.SessionLookupUnavailable, "records unreadable"):
            _ = build_hold.socket_for("session-a")
        with mock.patch.object(subprocess, "run", side_effect=answers(1)), mock.patch.object(time, "sleep") as sleep:
            self.assertIsNone(build_hold.socket_for("session-a"))
            sleep.assert_not_called()

    def test_delivery_states_and_gone_recipient_advance_immediately(self) -> None:
        now = self.begin("failed", "gone", "queued")
        sockets = {"failed": "/tmp/failed.sock", "gone": None, "queued": "/tmp/queued.sock"}
        def socket_for(session_id: str) -> str | None:
            return sockets[session_id]

        with mock.patch.object(build_hold, "socket_for", side_effect=socket_for), \
             mock.patch.object(build_hold, "send_release", side_effect=[3, 1]) as send:
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

    def test_registration_during_release_joins_its_turn(self) -> None:
        now = self.begin("first")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> int:
            sent.append(entry["session_id"])
            return 0

        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            build_hold.record_recipients([("late", "late")])
            self.assertIn("registered", build_hold.register_wait("late"))
            _ = build_hold.mark_gate("first", "MemoryGateReturned", "Granted")
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertEqual(sent, ["first", "late"])

    def test_registration_after_timeout_rejoins_after_earlier_memory_gate(self) -> None:
        now = self.begin("first")
        build_hold.record_recipients([("late name", "late")])
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> int:
            sent.append(entry["session_id"])
            return 0

        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            _ = self.advance(now)
            self.assertIn("WaitingForMemory", build_hold.mark_gate("first", "WaitingForMemory"))
            complete, _ = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S))
            self.assertFalse(complete)
            self.assertEqual(self.entries()[1]["state"], "NoRegistration")
            self.assertIn("registered third", build_hold.register_wait("third"))
            self.assertIn("registered late", build_hold.register_wait("late"))
            self.assertEqual([entry["session_id"] for entry in self.entries()], ["first", "third", "late"])
            self.assertEqual(self.entries()[2]["state"], "AwaitingRelease")
            self.assertTrue((self.scratch / "holders" / "holder").exists())
            self.assertIn("MemoryGateReturned", build_hold.mark_gate("first", "MemoryGateReturned", "Granted"))
            returned = build_hold.aware_instant(self.entries()[0].get("wait_ended_at", ""))
            complete, _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S - 1))
            self.assertFalse(complete)
            self.assertEqual(sent, ["first"])
            complete, _ = self.advance(returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertFalse(complete)
            self.assertEqual(sent, ["first", "third"])
            self.assertIn("MemoryGateReturned", build_hold.mark_gate("third", "MemoryGateReturned", "Granted"))
            third_returned = build_hold.aware_instant(self.entries()[1].get("wait_ended_at", ""))
            complete, _ = self.advance(third_returned + timedelta(seconds=build_hold.RELEASE_SETTLE_S))
            self.assertFalse(complete)
            self.assertEqual(sent, ["first", "third", "late"])
            self.assertTrue((self.scratch / "holders" / "holder").exists())

    def test_delivery_clock_starts_on_delivery_and_queue_clock_on_attempt(self) -> None:
        now = self.begin("first")
        current = now
        outcome = 0

        def clock() -> datetime:
            return current

        def deliver(_entry: build_hold.ReleaseEntry, _socket: str) -> int:
            nonlocal current
            current += timedelta(seconds=40)
            return outcome

        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", side_effect=deliver):
            cycle = build_hold.read_cycle()
            assert cycle is not None
            _ = build_hold.advance_release(cycle, now, clock=clock)
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.ReleasedAwaitingAdmission(now + timedelta(seconds=40)))
            _ = build_hold.advance_release(cycle, now + timedelta(seconds=build_hold.NO_ADMISSION_ACK_S), clock=clock)
            self.assertEqual(self.entries()[0]["state"], "ReleasedAwaitingAdmission")
            entry = cycle["entries"][0]
            build_hold.store_release_state(entry, build_hold.AwaitingRelease())
            build_hold.save_cycle(cycle)
            current = now + timedelta(minutes=1)
            outcome = 1
            _ = build_hold.advance_release(cycle, current, clock=clock)
            self.assertEqual(build_hold.read_release_state(self.entries()[0]), build_hold.DeliveryQueued(now + timedelta(minutes=1)))
            build_hold.store_release_state(entry, build_hold.AwaitingRelease())
            build_hold.save_cycle(cycle)
            current = now + timedelta(minutes=2)
            outcome = 3
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
        self.assertIn("/build_hold release sets it aside and ends the hold", output.getvalue())
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
        notice = build_hold.start_hold(self.scratch / "holders", "new-holder", "new test",
                                       build_hold.NoReleaseEta(), datetime.now().astimezone())
        self.assertIn("new-holder", notice)
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
            notice = build_hold.start_hold(self.scratch / "holders", "new-holder", "new test",
                                           build_hold.NoReleaseEta(), datetime.now().astimezone())
        self.assertIn("new-holder", notice)
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
        _ = build_hold.start_hold(self.scratch / "holders", "new-holder", "new test",
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

    def test_recorded_recipient_without_wait_reaches_no_registration_state(self) -> None:
        now = self.begin()
        build_hold.record_recipients([("late name", "missing-session")])
        complete, detail = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S - 1))
        self.assertFalse(complete)
        self.assertIn("late name", detail)
        complete, detail = self.advance(now + timedelta(seconds=build_hold.NO_REGISTRATION_S))
        self.assertTrue(complete)
        self.assertIn("NoRegistration", detail)
        self.assertEqual(self.entries()[0]["state"], "NoRegistration")

    def test_registration_waits_for_release_lock(self) -> None:
        _ = self.begin()
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
        _ = build_hold.start_hold(folder, "first", "first test", build_hold.NoReleaseEta(), first_since)
        _ = build_hold.start_hold(folder, "second", "second test", build_hold.NoReleaseEta(), second_since)
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
            self.assertEqual(socket, "/tmp/registered.sock")
            assert socket is not None
            self.assertEqual(build_hold.send_release(entry, socket), 0)
        self.assertEqual(calls[0][-2:], ["socket", "session:session-a"])
        self.assertIn("--to", calls[1])
        self.assertEqual(calls[1][calls[1].index("--to") + 1], "uds:/tmp/registered.sock")
        self.assertNotIn("shared name", calls[1])
        self.assertIn("build_hold.py nothing-to-build", calls[1][calls[1].index("--text") + 1])

    def test_a_session_with_nothing_to_build_releases_the_next_at_once(self) -> None:
        now = self.begin("first", "second")
        sent: list[str] = []

        def deliver(entry: build_hold.ReleaseEntry, _socket: str) -> int:
            sent.append(entry["session_id"])
            return 0

        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
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
            self.assertEqual(set(self.entries()[0]), {"session_id", "name", "state", "answered_at", "calls", "active_call_id", "active_released_at", "session_pending", "session_admitted_generation"})
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
        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", return_value=0):
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
        with mock.patch.object(build_hold, "socket_for", return_value="/tmp/socket"), \
             mock.patch.object(build_hold, "send_release", return_value=1):
            _ = self.advance(now)
            self.assertEqual(self.entries()[0]["state"], "DeliveryQueued")
            self.assertIn("NothingToBuild", build_hold.answer_nothing_to_build("first"))
            _ = self.advance(now)
        self.assertEqual([entry["state"] for entry in self.entries()], ["NothingToBuild", "DeliveryQueued"])


class CallAdmissionTests(IsolatedBuildHoldTest):
    now: datetime = datetime.min

    @override
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime.now().astimezone()
        _ = build_hold.start_hold(self.scratch / "holders", "holder", "the test", build_hold.NoReleaseEta(), self.now)
        _ = self.enterContext(mock.patch.object(build_hold, "process_start_time", return_value="test-process"))
        _ = self.enterContext(mock.patch.object(build_hold, "socket_for", return_value="/test/socket"))
        _ = self.enterContext(mock.patch.object(build_hold, "send_release", return_value=0))

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
        with mock.patch.object(build_hold, "send_release", return_value=0) as notification:
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

    def test_call_after_owner_answered_nothing_to_build_rejoins(self) -> None:
        _ = build_hold.register_wait("owner")
        self.advance()
        self.assertIn("NothingToBuild", build_hold.answer_nothing_to_build("owner"))
        self.register("owner", "seat-call")
        self.assertIsInstance(build_hold.read_admission("owner", "seat-call"), build_hold.AwaitingTurn)
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "seat-call"), build_hold.AdmittedTurn)

    def test_call_after_completed_turn_rejoins_while_another_holder_remains(self) -> None:
        _ = build_hold.start_hold(self.scratch / "holders", "second", "another test", build_hold.NoReleaseEta(), self.now)
        self.register("owner", "call-a")
        self.advance()
        self.complete("owner", "call-a")
        self.register("owner", "call-b")
        self.assertIsInstance(build_hold.read_admission("owner", "call-b"), build_hold.AwaitingTurn)
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
                self.register(key, "call-a")
                with mock.patch.object(build_hold, "socket_for", return_value=None if gone else "/test/socket"):
                    with mock.patch.object(build_hold, "send_release", return_value=3):
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
        self.register("owner-b", "pending")
        cycle_id = self.cycle()["id"]
        _ = build_hold.start_hold(self.scratch / "holders", "second", "another test", build_hold.NoReleaseEta(), self.now)
        self.assertEqual(self.cycle()["id"], cycle_id)
        self.assertIsInstance(build_hold.read_admission("owner-a", "started"), build_hold.AwaitingTurn)
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
        _ = build_hold.start_hold(self.scratch / "holders", "second", "another test",
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
        self.register("owner", "pending")
        _ = build_hold.start_hold(self.scratch / "holders", "second", "another test", build_hold.NoReleaseEta(), self.now)
        self.advance()
        self.assertIsInstance(build_hold.read_admission("owner", "started"), build_hold.AdmittedTurn)
        self.assertIsInstance(build_hold.read_admission("owner", "pending"), build_hold.AwaitingTurn)

    def test_hold_notice_preserves_the_existing_session_instructions(self) -> None:
        notice = build_hold.start_hold(self.scratch / "holders", "second", "another test", build_hold.NoReleaseEta(), self.now)
        self.assertEqual(notice, "/build_hold from second: stop any cargo or verify.sh you are running and start none until I release. No release after 2 h: ask me. For: another test Run python3 ~/.claude/scripts/build_hold/build_hold.py wait now in this session.")


    def test_queued_notification_authorizes_the_active_call(self) -> None:
        self.register("owner", "call-a")
        with mock.patch.object(build_hold, "send_release", return_value=1):
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
        _ = build_hold.start_hold(self.scratch / "holders", "second", "another test", build_hold.NoReleaseEta(), self.now)
        self.advance()
        current = self.cycle()
        self.assertNotEqual(current.get("holder_generation", 1), generation)
        _ = build_hold.mark_gate("owner", "MemoryGateReturned", "Granted", call_id="call-a", cycle_id=before["id"], generation=generation)
        self.assertEqual(self.cycle(), current)


if __name__ == "__main__":
    _ = unittest.main()
