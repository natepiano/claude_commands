"""Offload cleanup leftovers contracts."""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
import subprocess
from typing import cast
import unittest
from unittest import mock
from scripts.mac_test import offload
from scripts.mac_test.offload_test_commands import SELECTOR_STUB, write_shell
from scripts.mac_test.offload_test_support import HANA_COMMAND_PATTERN, SelectorEvent
from scripts.mac_test.offload_cleanup_test_support import SeededCleanupFixture


class OffloadCommandTests(SeededCleanupFixture):
    def command_with_changed_cleanup_timeout(self, value: str) -> subprocess.CompletedProcess[str]:
        self.config["leftover_timeout_s"] = "10"
        self.sync_config()
        self.fixture["run_cleanup_line"] = ""
        self.sync_fixture()
        stream = offload.ProcessController.stream_mac_run

        def change_settings(controller: offload.ProcessController,
                            arguments: Sequence[str]) -> offload.RemoteRunStatus:
            answer = stream(controller, arguments)
            self.config["leftover_timeout_s"] = value
            self.sync_config()
            return answer

        with mock.patch.object(offload.ProcessController, "stream_mac_run", change_settings):
            return self.command(separate_stderr=True)

    def test_completed_run_reads_the_cleanup_timeout_after_execution(self) -> None:
        with mock.patch.object(offload, "check_leftovers", wraps=offload.check_leftovers) as cleanup:
            result = self.command_with_changed_cleanup_timeout("23")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_wire("passed", "")
        self.assertEqual(cleanup.call_count, 1)
        connection = cast(offload.SshConnectionSettings, cleanup.call_args.args[0])
        self.assertEqual(connection.leftover_timeout_s, 23)
        self.assertEqual(len(self.leftover_events()), 1)
        self.assertFalse(self.claim_file.exists())
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_completed_run_keeps_the_claim_when_the_cleanup_timeout_becomes_unusable(self) -> None:
        result = self.command_with_changed_cleanup_timeout("broken")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_wire("passed", "")
        self.assertTrue(self.claim_file.exists())
        self.assertEqual(self.remote_claim()["remote_host"], "mac")
        self.assertEqual(self.leftover_events(), [])
        self.assertIn("leftover_timeout_s", result.stderr)
        self.assertIn("stays busy", result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_leftover_check_uses_connection_settings_and_the_recorded_host(self) -> None:
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        connection = offload.SshConnectionSettings("current-host", 7, 19)
        repository = offload.KnownRepository("hana")
        response = subprocess.CompletedProcess(["ssh"], 0, "leftovers=0\nrc=0\n", "")
        with mock.patch.object(subprocess, "run", return_value=response) as run:
            result = offload.check_leftovers(connection, "retained-host", repository)
        self.assertEqual(result, offload.LeftoversEnded())
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[-2], "retained-host")
        self.assertNotIn("current-host", arguments)
        self.assertIn("ConnectTimeout=7", arguments)
        self.assertEqual(arguments[-1], offload.leftover_check_script(repository))
        self.assertEqual(run.call_args.kwargs["timeout"], 19)

    def test_connection_reader_ignores_execution_values_and_reads_changes_at_use(self) -> None:
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        self.config["free_floor_gib"] = "broken"
        self.config["host"] = "first-host"
        self.config["connect_timeout_s"] = "7"
        self.config["leftover_timeout_s"] = "19"
        self.sync_config()
        self.assertEqual(offload.read_connection_settings(self.config_path),
                         offload.SshConnectionSettings("first-host", 7, 19))
        self.config["host"] = "second-host"
        self.config["connect_timeout_s"] = "8"
        self.config["leftover_timeout_s"] = "21"
        self.sync_config()
        self.assertEqual(offload.read_connection_settings(self.config_path),
                         offload.SshConnectionSettings("second-host", 8, 21))

    def test_unconfirmed_cleanup_checks_leftovers_before_release_and_keeps_test_result(self) -> None:
        self.fixture["run_cleanup_line"] = ""
        self.fixture["run_ssh_status"] = 255
        for status in (0, 42):
            for outcome in ("clean", "remaining", "missing_status", "ssh_error"):
                with self.subTest(status=status, outcome=outcome):
                    self.reset_leftover_case()
                    self.fixture["run_status_line"] = f"mac_exit={status}"
                    self.set_leftover_answer(outcome)
                    result = self.process_command(separate_stderr=True)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assert_wire("passed" if status == 0 else "failed", "")
                    self.assertEqual(result.stdout.count("Mac build output cleanup unconfirmed\n"), 1)
                    checks = self.leftover_events()
                    self.assertEqual(len(checks), 1)
                    self.assertTrue(checks[0]["claimed"], checks[0])
                    self.assert_leftover_selectors(checks[0]["args"][-1])
                    if outcome == "clean":
                        self.assertFalse(self.claim_file.exists())
                    else:
                        self.assert_claim_is_kept()
                        self.assert_leftover_diagnostic(result.stderr, outcome)

    def test_lost_run_time_includes_remote_cleanup(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "partial output"}
        ]
        self.fixture["run_status_reported"] = False
        self.sync_fixture()
        result = self.command_with_cleanup_clock()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("partial output\n", result.stdout)
        self.assertIn(
            "mac_test: the Mac runner exited with status 0 without a final status; local fallback requires admission\n",
            result.stdout,
        )
        wire = self.wire_result()
        self.assertEqual(wire["mac"], "lost")
        self.assertEqual(wire["reason"], "")
        recorded_seconds = float(wire["seconds"])
        self.assertEqual(recorded_seconds, 8.0)
        self.assertFalse(self.claim_file.exists())
        cleanup_events = [
            event
            for event in self.events_for("ssh")
            if "select_leftovers()" in event["args"][-1]
        ]
        self.assertEqual(len(cleanup_events), 1, self.events())
        cleanup = cleanup_events[0]
        self.assertTrue(cleanup["claimed"], cleanup)
        self.assert_leftover_selectors(cleanup["args"][-1])

    def test_lost_run_keeps_claim_when_leftover_check_does_not_prove_completion(self) -> None:
        self.fixture["run_status_reported"] = False
        for outcome in ("remaining", "missing_status", "timeout", "ssh_error"):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                self.set_leftover_answer(outcome)
                result = self.command_with_cleanup_clock(separate_stderr=True)
                self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                self.assert_wire("lost", "")
                self.assertIn("mac_test: the Mac runner exited with status 0 without a final status; local fallback requires admission\n", result.stdout)
                self.assert_leftover_diagnostic(result.stderr, outcome)
                self.assert_claim_is_kept()
                self.assertEqual(len(self.leftover_events()), 1)
                self.assert_leftover_selectors(self.leftover_events()[0]["args"][-1])
                self.assertEqual(float(self.wire_result()["seconds"]), 13.0 if outcome == "timeout" else 8.0)

    def test_leftover_selectors_complete_under_sh_and_zsh_before_releasing_claim(self) -> None:
        selectors = self.stub_directory / "selectors"
        selectors.mkdir()
        for name in ("pgrep", "lsof", "sleep"):
            write_shell(selectors / name, SELECTOR_STUB)
        self.fixture["run_status_reported"] = False
        self.config["leftover_timeout_s"] = "300"
        self.sync_config()
        for shell in ("sh", "zsh"):
            for pgrep_status, lsof_status in ((1, 1), (1, 0), (2, 0)):
                with self.subTest(shell=shell, pgrep_status=pgrep_status, lsof_status=lsof_status):
                    self.reset_leftover_case()
                    self.fixture["selector_shell"] = shell
                    self.fixture["selector_pgrep_status"] = pgrep_status
                    self.fixture["selector_lsof_status"] = lsof_status
                    self.sync_fixture()
                    result = self.process_command(separate_stderr=True)
                    self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                    self.assert_wire("lost", "")
                    cleanup = self.leftover_events()
                    self.assertEqual(len(cleanup), 1)
                    self.assert_leftover_selectors(cleanup[0]["args"][-1])
                    path = self.stub_directory / "selector-events.jsonl"
                    calls = [cast(SelectorEvent, json.loads(line)) for line in path.read_text().splitlines()]
                    names = [call["command"] for call in calls]
                    self.assertIn("pgrep", names)
                    if pgrep_status == 1:
                        self.assertIn("lsof", names)
                    for call in calls:
                        if call["command"] == "pgrep":
                            self.assertEqual(call["args"], ["-u", str(os.getuid()), "-f", HANA_COMMAND_PATTERN])
                        if call["command"] == "lsof":
                            self.assertEqual(call["args"], ["-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"])
                    answer = (self.stub_directory / "selector-answer").read_text()
                    if pgrep_status == 1 and lsof_status == 0:
                        self.assertIn("sleep", names)
                        self.assertEqual(answer.splitlines(), ["leftovers=0", "rc=0"])
                        self.assertFalse(self.claim_file.exists())
                    else:
                        self.assertNotIn("rc=0", answer.splitlines())
                        self.assert_claim_is_kept()
                        self.assert_leftover_diagnostic(result.stderr, "missing_status")



if __name__ == "__main__":
    _ = unittest.main()
