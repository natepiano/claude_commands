"""Build holds with temporary holder files and synthetic machine readings."""

from __future__ import annotations

import json
import io
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
            "BUILDLOG_MEMINFO": str(meminfo),
        }))


class ImportTests(IsolatedBuildHoldTest):
    def test_helper_imports_from_its_sibling_directory(self) -> None:
        self.assertEqual(build_hold.__name__, "build_hold")


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
            {"session_id": "session-old", "name": "old", "state": "AwaitingRelease"},
            {"session_id": "session-other", "name": "other", "state": "AwaitingRelease"},
        ]
        build_hold.save_cycle(cycle)
        return cycle

    def test_rename_holder_moves_file_and_cycle_names(self) -> None:
        old_path = self.holder("old")
        _ = self.cycle()

        self.assertEqual(build_hold.rename_holder("old", "new"),
                         ["build hold holder", "build hold cycle"])

        new_path = old_path.with_name("new")
        self.assertFalse(old_path.exists())
        self.assertEqual(cast(dict[str, object], json.loads(new_path.read_text()))["holder"], "new")
        cycle = build_hold.read_cycle()
        assert cycle is not None
        self.assertEqual(set(cycle["holders"]), {"new"})
        self.assertEqual(cycle["recipients"], {"session-old": "new", "session-other": "other"})
        self.assertEqual([entry["name"] for entry in cycle["entries"]], ["new", "other"])

    def test_rename_holder_refuses_two_holder_files_without_changes(self) -> None:
        old_path = self.holder("old")
        new_path = self.holder("new")
        cycle = self.cycle()
        cycle_path = self.scratch / "release" / cycle["id"] / "cycle.json"
        before = (old_path.read_bytes(), new_path.read_bytes(), cycle_path.read_bytes())

        with self.assertRaisesRegex(ValueError, "both 'old' and 'new'"):
            _ = build_hold.rename_holder("old", "new")

        self.assertEqual((old_path.read_bytes(), new_path.read_bytes(), cycle_path.read_bytes()), before)

    def test_rename_holder_without_old_file_changes_nothing(self) -> None:
        cycle = self.cycle()
        cycle_path = self.scratch / "release" / cycle["id"] / "cycle.json"
        before = cycle_path.read_bytes()

        self.assertEqual(build_hold.rename_holder("old", "new"), [])

        self.assertEqual(cycle_path.read_bytes(), before)
        self.assertFalse((self.scratch / "holders" / "new").exists())


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
        cycle["entries"][0] = {"session_id": "first", "name": "first", "state": "ReleasedAwaitingAdmission", "released_at": "broken"}
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
        cycle["entries"][0] = {"session_id": "first", "name": "first", "state": "ReleasedAwaitingAdmission", "released_at": "broken"}
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
        cycle["entries"][0] = {"session_id": "first", "name": "first", "state": "ReleasedAwaitingAdmission", "released_at": "broken"}
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
        cycle["entries"][0] = {"session_id": "first", "name": "first", "state": "ReleasedAwaitingAdmission", "released_at": "broken"}
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
        cycle["entries"][0] = {
            "session_id": "first", "name": "shared name", "state": "MemoryGateReturned",
            "wait_ended_at": (now - timedelta(seconds=build_hold.RELEASE_SETTLE_S + 1)).isoformat(),
            "outcome": "Granted",
        }
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
            self.assertEqual(set(self.entries()[0]), {"session_id", "name", "state", "answered_at"})
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


if __name__ == "__main__":
    _ = unittest.main()
