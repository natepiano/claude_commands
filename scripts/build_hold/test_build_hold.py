"""Build holds with temporary holder files and synthetic machine readings."""

from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, tzinfo
from pathlib import Path
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

import build_hold


SCRIPT = Path(__file__).with_name("build_hold.py")


def write_holder(folder: Path, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
    path = folder / name
    _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
    return path


class ImportTests(unittest.TestCase):
    def test_helper_imports_from_its_sibling_directory(self) -> None:
        self.assertEqual(build_hold.__name__, "build_hold")


class HolderTests(unittest.TestCase):
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


class QuietTests(unittest.TestCase):
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


class CommandTests(unittest.TestCase):
    def run_cli(self, folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "BUILD_HOLD_DIR": str(folder)},
        )

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
            self.assertEqual(final.stdout.strip(), "released, builds may resume.")
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


if __name__ == "__main__":
    _ = unittest.main()
