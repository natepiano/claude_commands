"""Delivery outcomes used by the refusal watcher."""
import subprocess
import unittest
from unittest.mock import patch
from scripts.shutdown import restart_support as settle
NOW = "2026-10-09T21:49:10+00:00"

class SendOutcomeTests(unittest.TestCase):
    def test_confirmed_send_outcomes_preserve_restart_delivery_states(self) -> None:
        cases: tuple[tuple[int, str, settle.MessageSendOutcome], ...] = (
            (0, "SENT: fixture", settle.MessageSent(kind="sent")),
            (0, "SKIPPED: repeated", settle.MessageWindowSkipped(kind="window skipped", reason="repeated")),
            (0, "SKIPPED: key 'fixture' acknowledged at " + NOW,
             settle.MessageAcknowledged(kind="acknowledged", reason="key 'fixture' acknowledged at " + NOW)),
            (0, "KEPT: silence", settle.MessageKept(kind="kept", reason="silence")),
            (0, "REPLACED: revised", settle.MessageReplaced(kind="replaced", reason="revised")),
            (1, "QUEUED: offline", settle.MessageQueued(kind="queued", reason="offline")),
        )
        for code, output, expected in cases:
            with self.subTest(output=output), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")):
                self.assertEqual(settle.send_message("fixture", "summary", "message"), expected)

    def test_confirmed_send_failures_raise_without_recording_delivery(self) -> None:
        for code, header in ((3, "FAILED"), (2, "REFUSED"), (4, "HELD"), (5, "PENDING")):
            with self.subTest(header=header), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, header + ": unavailable", "")):
                with self.assertRaisesRegex(RuntimeError, header + ": unavailable"):
                    _ = settle.send_message("fixture", "summary", "message")

    def test_unconfirmed_send_results_preserve_restart_fallback_states(self) -> None:
        for code, output in ((0, ""), (0, "QUEUED: inconsistent"), (1, ""), (1, "SENT: inconsistent")):
            with self.subTest(code=code, output=output), patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")):
                result = settle.send_message("fixture", "summary", "message")
                self.assertEqual(result, settle.MessageUnconfirmed(kind="unconfirmed", reason="send returned no confirmed outcome"))
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 3, "SENT: inconsistent", "")):
            self.assertEqual(settle.send_message("fixture", "summary", "message"),
                             settle.MessageUnconfirmed(kind="unconfirmed", reason="send returned no confirmed outcome"))

    def test_retry_reports_without_recording_delivery(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 6, "RETRY: active", "")):
            with self.assertRaisesRegex(RuntimeError, "needs a retry: active"):
                _ = settle.send_message("fixture", "summary", "message")
