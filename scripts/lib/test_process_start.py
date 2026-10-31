"""Process identity observations agree with the step recorder's stale policy."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from typing import TYPE_CHECKING
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
if TYPE_CHECKING:
    from . import process_start as starts
else:
    import process_start as starts
from scripts.direct import step_record as steps


class ProcessStartTests(unittest.TestCase):
    def test_linux_observations_and_record_policy(self) -> None:
        cases = (
            ("live", b"42 (a name with ) and \xff) S " + b"0 " * 18 + b"123 0", starts.Started("123"), steps.StepRecord),
            ("zombie", b"42 (verify) Z " + b"0 " * 18 + b"123 0", starts.Zombie("123"), steps.StaleRecord),
            ("gone", None, starts.Gone(), steps.StaleRecord),
            ("malformed", b"42 (verify) S broken", starts.Unreadable(""), steps.UnreadableRecord),
            ("reused", b"42 (verify) S " + b"0 " * 18 + b"456 0", starts.Started("456"), steps.StaleRecord),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stat = root / "proc/42/stat"
            stat.parent.mkdir(parents=True)
            with patch.dict(os.environ, {"VERIFY_PROC_ROOT": str(root / "proc"), "VERIFY_STEPS_DIR": str(root / "records")}), \
                    patch.object(sys, "platform", "linux"):
                for name, data, expected, record_type in cases:
                    with self.subTest(case=name):
                        if data is None:
                            stat.unlink(missing_ok=True)
                        else:
                            _ = stat.write_bytes(data)
                        actual = starts.read_process_start(42)
                        if isinstance(expected, starts.Unreadable):
                            self.assertIsInstance(actual, starts.Unreadable)
                        else:
                            self.assertEqual(actual, expected)
                        record = steps.StepRecord("call", 42, "123", root, "check", None,
                                                  steps.MacAllowed(), steps.StepState.ON_MAC, "now")
                        steps.write(record)
                        self.assertIsInstance(steps.read("call"), record_type)

    def test_linux_read_error_does_not_claim_the_process_is_gone(self) -> None:
        with patch.object(sys, "platform", "linux"), \
                patch.object(Path, "read_bytes", side_effect=PermissionError("stat denied")):
            result = starts.read_process_start(42)
        self.assertIsInstance(result, starts.Unreadable)
        if isinstance(result, starts.Unreadable):
            self.assertIn("stat denied", result.reason)

    def test_darwin_observations_use_one_locale_and_record_policy(self) -> None:
        start = "Fri Oct 30 12:34:56 2026"
        cases = (
            ("live", 0, "S " + start, "", starts.Started(start), steps.StepRecord),
            ("zombie", 0, "Z+ " + start, "", starts.Zombie(start), steps.StaleRecord),
            ("gone", 1, "", "", starts.Gone(), steps.StaleRecord),
            ("unreadable", 2, "", "ps denied", starts.Unreadable("ps denied"), steps.UnreadableRecord),
            ("malformed", 0, "S not-a-start", "", starts.Unreadable(""), steps.UnreadableRecord),
            ("reused", 0, "S Thu Oct 29 12:34:56 2026", "", starts.Started("Thu Oct 29 12:34:56 2026"), steps.StaleRecord),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"VERIFY_PROC_ROOT": str(root / "absent"), "VERIFY_STEPS_DIR": str(root / "records"),
                                         "LC_ALL": "fr_FR.UTF-8", "TZ": "Pacific/Honolulu"}), \
                    patch.object(sys, "platform", "darwin"):
                for name, status, stdout, stderr, expected, record_type in cases:
                    with self.subTest(case=name), patch.object(subprocess, "run", return_value=
                            subprocess.CompletedProcess(["ps"], status, stdout, stderr)) as ps:
                        actual = starts.read_process_start(42)
                        if name == "malformed":
                            self.assertIsInstance(actual, starts.Unreadable)
                        else:
                            self.assertEqual(actual, expected)
                        self.assertEqual(ps.call_args.kwargs["env"]["LC_ALL"], "C")
                        self.assertEqual(ps.call_args.kwargs["env"]["TZ"], "UTC")
                        record = steps.StepRecord("call", 42, start, root, "check", None,
                                                  steps.MacAllowed(), steps.StepState.ON_MAC, "now")
                        steps.write(record)
                        self.assertIsInstance(steps.read("call"), record_type)
                        if isinstance(expected, starts.Started) and expected.start == start:
                            self.assertEqual(steps.process_start(42), start)

    def test_darwin_ps_failure_and_timeout_are_unreadable(self) -> None:
        for error in (PermissionError("ps denied"), subprocess.TimeoutExpired("ps", 5)):
            with self.subTest(error=error), patch.object(sys, "platform", "darwin"), \
                    patch.object(subprocess, "run", side_effect=error):
                self.assertIsInstance(starts.read_process_start(42), starts.Unreadable)

    def test_canonical_start_matches_without_another_process_read(self) -> None:
        for host in ("linux", "darwin"):
            with self.subTest(host=host), patch.object(sys, "platform", host), \
                    patch.object(subprocess, "run", side_effect=AssertionError("unneeded ps")):
                self.assertTrue(starts.matches_stored_start(42, starts.Started("123"), "123"))
                self.assertTrue(starts.matches_stored_start(42, starts.Zombie("123"), "123"))
                if host == "linux":
                    self.assertFalse(starts.matches_stored_start(42, starts.Started("456"), "123"))

    def test_legacy_darwin_start_matches_only_the_same_canonical_identity(self) -> None:
        canonical = "Fri Oct 30 12:34:56 2026"
        legacy = "Fri Oct 30 08:34:56 2026"
        cases = (
            ("live", starts.Started(canonical), 0, "S " + legacy, "S " + canonical, True),
            ("zombie", starts.Zombie(canonical), 0, "Z " + legacy, "Z " + canonical, True),
            ("reused", starts.Started(canonical), 0, "S Fri Oct 30 08:35:56 2026", "", False),
            ("unreadable", starts.Started(canonical), 2, "", "", False),
            ("gone", starts.Started(canonical), 1, "", "", False),
            ("reused_during_read", starts.Started(canonical), 0, "S " + legacy,
             "S Fri Oct 30 12:35:56 2026", False),
            ("state_changed", starts.Started(canonical), 0, "S " + legacy, "Z " + canonical, False),
        )
        for name, observed, status, local, confirmed, expected in cases:
            reads = [subprocess.CompletedProcess(["ps"], status, local, "")]
            if local.endswith(legacy):
                reads.append(subprocess.CompletedProcess(["ps"], 0, confirmed, ""))
            with self.subTest(case=name), patch.object(sys, "platform", "darwin"), \
                    patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8", "TZ": "America/New_York"}), \
                    patch.object(subprocess, "run", side_effect=reads) as ps:
                self.assertEqual(starts.matches_stored_start(42, observed, legacy), expected)
                self.assertEqual(ps.call_args_list[0].kwargs["env"]["LC_ALL"], "C")
                self.assertEqual(ps.call_args_list[0].kwargs["env"]["TZ"], "America/New_York")
                self.assertEqual(ps.call_count, len(reads))
                if len(reads) == 2:
                    self.assertEqual(ps.call_args_list[1].kwargs["env"]["TZ"], "UTC")


if __name__ == "__main__":
    _ = unittest.main()
