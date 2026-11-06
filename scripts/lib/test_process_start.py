"""Check each process identity outcome on Linux and Darwin."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts.lib import process_start as identity
from scripts.direct import step_record


class ProcessStartTests(unittest.TestCase):
    def test_linux_result_types_and_stored_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"VERIFY_PROC_ROOT": directory}):
            process = Path(directory) / "42"
            process.mkdir()
            stat = process / "stat"
            for state, expected in (("S", identity.Started), ("Z", identity.Zombie)):
                _ = stat.write_text(f"42 (nested (name)) {state} " + "0 " * 18 + "123 0\n")
                result = identity.process_start(42)
                self.assertIs(type(result), expected)
                assert isinstance(result, (identity.Started, identity.Zombie))
                self.assertEqual(result.start, "123")
            stat.unlink()
            self.assertIs(type(identity.process_start(42)), identity.Gone)
            for content in ("42 (broken", "42 (name) S 0", "42 (name) S " + "0 " * 18 + "bad"):
                _ = stat.write_text(content)
                self.assertIs(type(identity.process_start(42)), identity.Unreadable)
            with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
                self.assertIs(type(identity.process_start(42)), identity.Unreadable)

    def test_darwin_result_types_and_stable_locale_and_zone(self) -> None:
        start = "Tue Nov  3 23:00:00 2026"
        cases = ((0, start + " S\n", "", identity.Started),
                 (0, start + " Z+\n", "", identity.Zombie),
                 (1, "", "", identity.Gone),
                 (1, "", "permission denied", identity.Unreadable),
                 (2, "", "ps failed", identity.Unreadable),
                 (0, "invalid", "", identity.Unreadable),
                 (0, "", "", identity.Unreadable))
        with mock.patch.object(sys, "platform", "darwin"):
            for status, output, error, expected in cases:
                with self.subTest(status=status, output=output), mock.patch.object(subprocess, "run",
                        return_value=subprocess.CompletedProcess([], status, output, error)) as run:
                    result = identity.process_start(42)
                    self.assertIs(type(result), expected)
                    if isinstance(result, (identity.Started, identity.Zombie)):
                        self.assertEqual(result.start, start)
                    self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
                    self.assertEqual(run.call_args.kwargs["env"]["TZ"], "UTC")
            for error in (PermissionError("denied"), subprocess.TimeoutExpired("ps", 5)):
                with mock.patch.object(subprocess, "run", side_effect=error):
                    self.assertIs(type(identity.process_start(42)), identity.Unreadable)

    def test_result_kinds_never_compare_equal(self) -> None:
        values = (identity.Started("123"), identity.Zombie("123"), identity.Gone(), identity.Unreadable("123"))
        for first in values:
            for second in values:
                self.assertEqual(first == second, type(first) is type(second))

    def test_step_record_policies_agree_on_both_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"VERIFY_STEPS_DIR": directory}):
            for platform, start in (("linux", "123"), ("darwin", "Tue Nov  3 23:00:00 2026")):
                record = step_record.StepRecord("call", 42, start, Path(directory), "check", None,
                    step_record.MacAllowed(), step_record.StepState.WAITING_HOLD, "now")
                step_record.write(record)
                cases = ((identity.Started(start), step_record.StepRecord),
                         (identity.Started("different"), step_record.StaleRecord),
                         (identity.Zombie(start), step_record.StaleRecord),
                         (identity.Gone(), step_record.StaleRecord),
                         (identity.Unreadable("denied"), step_record.UnreadableRecord))
                with mock.patch.object(sys, "platform", platform):
                    for result, expected in cases:
                        with self.subTest(platform=platform, result=type(result)), \
                                mock.patch.object(identity, "process_start", return_value=result):
                            self.assertIs(type(step_record.read("call")), expected)

    def test_darwin_step_recorder_writes_without_proc_and_stopped_needs_no_boot_id(self) -> None:
        start = "Tue Nov  3 23:00:00 2026"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"VERIFY_STEPS_DIR": directory, "VERIFY_PROC_ROOT": directory + "/no-proc"}), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, start + " S\n", "")), \
                mock.patch("sys.argv", ["step_record.py", "write", "--call-id", "mac", "--pid", "42",
                    "--kind", "check", "--runs-on", "mac_allowed", "--state", "waiting_hold"]):
            self.assertEqual(step_record.main(), 0)
            record = step_record.read("mac")
            self.assertIs(type(record), step_record.StepRecord)
            assert isinstance(record, step_record.StepRecord)
            self.assertEqual(record.proc_start, start)
            self.assertFalse(step_record.consume_stopped_build("mac", Path(directory) / "consumed"))

    def test_reader_does_not_load_argument_or_dataclass_machinery(self) -> None:
        result = subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-S", "-c",
            "import sys; from scripts.lib import process_start; "
            + "assert not set(sys.modules).intersection({\"argparse\", \"dataclasses\", \"inspect\"})"],
            capture_output=True, text=True, check=False, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
