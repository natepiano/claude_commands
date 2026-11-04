"""Check journal attribution independently of the shell retry loop."""

import io
import subprocess
import unittest
from unittest.mock import patch

from scripts.direct import memory_kill as kills
from scripts.direct.memory_kill_test_support import (
    EARLYOOM_MESSAGE, KILL_AT, failed_compile_log, journal_event,
)


class MemoryKillClassifierTests(unittest.TestCase):
    def test_legacy_signal_uses_original_journal_text_matches(self) -> None:
        for log in ("signal: 9", "SIGKILL", "SIGTERM", "signal: 15",
                    "error: linking with `cc` failed: signal: 9"):
            for message in ('sending SIGTERM to process 123 uid 1000 "hana-1a2b3c"',
                            "Memory cgroup out of memory", "Out of memory: Killed process 123 (ld)"):
                with self.subTest(log=log, message=message), patch.object(kills, "journal_output", return_value=message):
                    self.assertTrue(kills.was_memory_kill(log, "1", "2"))

    def test_signal_without_kill_evidence_is_refused(self) -> None:
        with patch.object(kills, "journal_output", return_value="process exited after 0.100 seconds"):
            self.assertFalse(kills.was_memory_kill("signal: 9", "1", "2"))

    def test_wrapper_exit_requires_timestamped_matching_rustc_kill(self) -> None:
        for message, expected in ((EARLYOOM_MESSAGE, True),
                                  (EARLYOOM_MESSAGE.replace("--crate-name hana", "--crate-name other"), False),
                                  (EARLYOOM_MESSAGE.replace('"rustc"', '"cc"'), False),
                                  (EARLYOOM_MESSAGE.replace("--crate-name hana", ""), False)):
            with self.subTest(message=message), patch.object(kills, "journal_output", return_value=journal_event(message)):
                self.assertEqual(kills.was_memory_kill(failed_compile_log(), str(KILL_AT), str(KILL_AT)), expected)

    def test_missing_malformed_and_outside_journal_times_are_refused(self) -> None:
        for journal in (journal_event(at=KILL_AT - 1), journal_event(at=KILL_AT + 1),
                        '{"MESSAGE":"kill"}', "null", "[]", "bad JSON",
                        '{"__REALTIME_TIMESTAMP":{},"MESSAGE":"kill"}'):
            with self.subTest(journal=journal), patch.object(kills, "journal_output", return_value=journal):
                self.assertFalse(kills.was_memory_kill(failed_compile_log(), str(KILL_AT), str(KILL_AT)))

    def test_real_diagnostics_veto_wrapper_retry_before_querying_journal(self) -> None:
        for diagnostic in ("error[E0308]: mismatched types", "error: unused import",
                           '{"level":"error"}', '{"message":{"level":"error"}}'):
            with self.subTest(diagnostic=diagnostic), patch.object(kills, "journal_output") as journal:
                self.assertFalse(kills.was_memory_kill(diagnostic + "\n" + failed_compile_log(), "1", "2"))
                journal.assert_not_called()

    def test_invalid_attempt_windows_are_refused(self) -> None:
        for start, end in (("", "2"), ("nan", "2"), ("1", "inf"), ("2", "1"), ("0", "2")):
            with self.subTest(start=start, end=end), patch.object(kills, "journal_output") as journal:
                self.assertFalse(kills.was_memory_kill("signal: 9", start, end))
                journal.assert_not_called()

    def test_journal_command_and_unavailable_service(self) -> None:
        command = ["journalctl", "-u", "earlyoom", "--since", "@1.0", "--until", "@2.0", "--no-pager", "-o", "json"]
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(command, 0, "evidence")) as run:
            self.assertEqual(kills.journal_output(1.0, 2.0, ("-u", "earlyoom"), json_output=True), "evidence")
            run.assert_called_once_with(command, capture_output=True, text=True, check=False)
        with patch.object(subprocess, "run", side_effect=OSError("journal unavailable")):
            self.assertEqual(kills.journal_output(1.0, 2.0, ("-k",), json_output=False), "")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(command, 1, "kill", "journal denied")):
            self.assertEqual(kills.journal_output(1.0, 2.0, ("-k",), json_output=False), "")

    def test_failed_journal_preserves_both_output_streams(self) -> None:
        result = subprocess.CompletedProcess(["journalctl"], 1, "partial journal", "journal denied")
        errors = io.StringIO()
        with patch.object(subprocess, "run", return_value=result), patch("sys.stderr", errors):
            self.assertEqual(kills.journal_output(1.0, 2.0, ("-k",), json_output=False), "")
        self.assertIn("stdout: partial journal", errors.getvalue())
        self.assertIn("stderr: journal denied", errors.getvalue())

    def test_cli_refuses_missing_log_or_arguments(self) -> None:
        self.assertEqual(kills.main([]), 1)
        self.assertEqual(kills.main(["1", "2", "/nonexistent/step.log"]), 1)


if __name__ == "__main__":
    _ = unittest.main()
