"""Offload routing and command arguments."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields
import io
from pathlib import Path
from typing import cast, final, override
from unittest import mock
import offload
from offload_test_support import OffloadCommandFixture, FILTER, HANA, HANA_COMMAND_PATTERN


class LintVerbTests(unittest.TestCase):
    def test_check_and_mend_check_use_the_public_lint_command_without_rewrites(self) -> None:
        lint = Path(__file__).resolve().parents[1] / "lint" / "lint"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invoke = root / ".claude/scripts/lint/invoke.sh"
            invoke.parent.mkdir(parents=True)
            _ = invoke.write_text(
                'run() { printf "%s\\n" "$@"; }\n'
                + 'invoke_mend() { printf "%s\\n" mend "$@"; }\n')
            environment = {**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "buildlog")}
            for verb, expected in (("check", ["cargo", "check", "--workspace", "--all-features"]),
                                   ("mend-check", ["mend", "--fail-on-warn", "--workspace", "--all-features"])):
                with self.subTest(verb=verb):
                    answer = subprocess.run(["bash", str(lint), verb, "--workspace", "--all-features"],
                                            env=environment, cwd=root, capture_output=True, text=True, check=False)
                    self.assertEqual(answer.returncode, 0, answer.stderr)
                    self.assertEqual(answer.stdout.splitlines(), expected)


class OffloadSettingsTests(unittest.TestCase):
    root: Path = Path()
    settings: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = self.root / "mac_test.conf"
        _ = self.settings.write_text("", encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "MAC_TEST_CONFIG": str(self.settings),
            "MAC_TEST_STATE_DIR": str(self.root / "state"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch("subprocess.run", side_effect=AssertionError("No external command expected")))
        _ = self.enterContext(mock.patch("subprocess.Popen", side_effect=AssertionError("No external command expected")))

    def test_explicit_settings_path_controls_every_offload_field(self) -> None:
        settings = self.root / "other-mac.conf"
        _ = settings.write_text(
            "offload=on\nhost=other-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n"
            + "unreachable_backoff_s=4\nfree_floor_gib=5\nmac_budget_gib=6\n"
            + "mac_budget_gib.hana=7\nlinux_only.hana=local-package\n",
            encoding="utf-8",
        )
        config = offload.read_config(settings)
        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "other-mac")
        self.assertEqual(config.connect_timeout_s, 2.0)
        self.assertEqual(config.probe_timeout_s, 3.0)
        self.assertEqual(config.unreachable_backoff_s, 4.0)
        self.assertEqual(config.free_floor_gib, 5.0)
        self.assertEqual(config.default_target_budget_gib, "6")
        self.assertEqual(config.values["mac_budget_gib.hana"], "7")
        self.assertEqual(config.values["linux_only.hana"], "local-package")

    def test_no_argument_config_reader_keeps_existing_defaults(self) -> None:
        expected = offload.OffloadConfig(False, "mac", 6.0, 15.0, 300.0, 60.0, "24", {})
        self.assertEqual(offload.read_config(), expected)
        self.assertEqual(offload.read_config(self.settings), expected)

    def test_no_argument_config_reader_still_uses_environment_path(self) -> None:
        _ = self.settings.write_text("offload=on\nhost=environment-mac\nprobe_timeout_s=7\n", encoding="utf-8")
        expected = offload.OffloadConfig(
            True, "environment-mac", 6.0, 7.0, 300.0, 60.0, "24",
            {"offload": "on", "host": "environment-mac", "probe_timeout_s": "7"},
        )
        self.assertEqual(offload.read_config(), expected)

    def test_path_reader_keeps_invalid_numeric_defaults(self) -> None:
        _ = self.settings.write_text(
            "connect_timeout_s=bad\nprobe_timeout_s=0\nunreachable_backoff_s=-1\nfree_floor_gib=nan\n",
            encoding="utf-8",
        )
        config = offload.read_config(self.settings)
        self.assertEqual(
            (config.connect_timeout_s, config.probe_timeout_s, config.unreachable_backoff_s, config.free_floor_gib),
            (6.0, 15.0, 300.0, 60.0),
        )

    def test_reachability_probe_runs_only_a_read_only_remote_command(self) -> None:
        _ = self.settings.write_text("host=other-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n", encoding="utf-8")
        config = offload.read_config(self.settings)
        controller = offload.ProcessController()
        calls: list[tuple[list[str], offload.CommandDeadline]] = []

        def capture(
            arguments: list[str],
            deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT,
        ) -> offload.CapturedCommandResult:
            calls.append((arguments, deadline))
            return offload.CommandOutput(0, "", "")

        with mock.patch.object(controller, "capture", side_effect=capture):
            self.assertEqual(offload.probe_reachability(controller, config), offload.ReachableHost())
        self.assertEqual(len(calls), 1)
        arguments, deadline = calls[0]
        self.assertEqual(arguments[:-1], [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "other-mac",
        ])
        self.assertEqual(arguments[-1], "true")
        self.assertNotRegex(arguments[-1], r"\b(?:mkdir|touch|tee|rm|mv|cp)\b|[>]")
        self.assertEqual(deadline, offload.CommandTimeLimit(3.0))
        self.assertFalse((self.root / "state").exists())

    def test_failed_reachability_command_reports_unreachable(self) -> None:
        config = offload.read_config(self.settings)
        failures: tuple[offload.CapturedCommandResult, ...] = (
            offload.CommandOutput(255, "", "connection refused"),
            offload.CommandOutput(1, "", "remote command failed"),
            offload.COMMAND_TIMED_OUT,
            offload.COMMAND_COULD_NOT_START,
        )
        for failure in failures:
            with self.subTest(result=type(failure).__name__):
                controller = offload.ProcessController()
                with mock.patch.object(controller, "capture", return_value=failure):
                    result = offload.probe_reachability(controller, config)
                self.assertIsInstance(result, offload.UnreachableHost)
                if isinstance(result, offload.UnreachableHost):
                    self.assertTrue(result.reason)

    def test_resource_probe_needs_no_process_count_and_ignores_legacy_counts(self) -> None:
        wire = "free_gib=120\nhost=Mac\nrc=0\n"
        expected = offload.ProbeSnapshot(120.0, "Mac")
        self.assertEqual(offload.parse_probe(wire), expected)
        self.assertEqual(offload.parse_probe("procs=12\n" + wire), expected)
        self.assertEqual(offload.parse_probe("procs=unreadable\n" + wire), expected)
        self.assertNotIn("processes", {field.name for field in fields(expected)})
        script = offload.probe_script(offload.KnownRepository("hana"))
        self.assertNotIn("pgrep", script)
        self.assertNotIn("procs", script)

    def test_leftover_check_requires_count_and_printed_success_on_the_named_host(self) -> None:
        config = offload.read_config(self.settings)
        for answer, expected in (("leftovers=0\nrc=0\n", offload.LeftoversEnded()),
                                 ("leftovers=4\nrc=0\n", offload.LeftoversRemain(4))):
            with self.subTest(answer=answer):
                response = subprocess.CompletedProcess(["ssh"], 0, answer, "")
                with mock.patch.object(subprocess, "run", return_value=response) as run:
                    self.assertEqual(offload.check_leftovers(config, "kept-host", HANA), expected)
                arguments = cast(list[str], run.call_args.args[0])
                self.assertEqual(arguments[:-1], ["ssh", *offload.ssh_options(config), "kept-host"])
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
                self.assertIn('pgrep -u "$(id -u)" -f "$mirror_command_pattern"', arguments[-1])
                self.assertIn(f"mirror_command_pattern={shlex.quote(HANA_COMMAND_PATTERN)}\n", arguments[-1])
        for answer in ("leftovers=0\n", "rc=0\n", "leftovers=-1\nrc=0\n", "leftovers=bad\nrc=0\n"):
            with self.subTest(answer=answer):
                response = subprocess.CompletedProcess(["ssh"], 0, answer, "")
                with mock.patch.object(subprocess, "run", return_value=response):
                    result = offload.check_leftovers(config, "kept-host", HANA)
                self.assertIsInstance(result, offload.LeftoverCheckFailed)
                if isinstance(result, offload.LeftoverCheckFailed):
                    self.assertIn("answer could not be read", result.reason)

    def test_leftover_check_reports_timeout_start_failure_and_ssh_error(self) -> None:
        config = offload.read_config(self.settings)
        for error, expected in ((subprocess.TimeoutExpired("ssh", 10), "timed out after 10"),
                                (FileNotFoundError("ssh missing"), "could not start")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(subprocess, "run", side_effect=error):
                    result = offload.check_leftovers(config, "kept-host", HANA)
                self.assertIsInstance(result, offload.LeftoverCheckFailed)
                if isinstance(result, offload.LeftoverCheckFailed):
                    self.assertIn(expected, result.reason)
                    self.assertIn("10", result.reason)
        response = subprocess.CompletedProcess(["ssh"], 255, "", "connection\n refused\tby host")
        with mock.patch.object(subprocess, "run", return_value=response):
            result = offload.check_leftovers(config, "kept-host", HANA)
        self.assertIsInstance(result, offload.LeftoverCheckFailed)
        if isinstance(result, offload.LeftoverCheckFailed):
            self.assertEqual(result.reason, "connection refused by host")

    def test_leftover_check_uses_configured_timeout_and_reports_it(self) -> None:
        _ = self.settings.write_text("leftover_timeout_s=0.125\n")
        config = offload.read_config(self.settings)
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 0.125)) as run:
            result = offload.check_leftovers(config, "kept-host", HANA)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.125)
        self.assertEqual(result, offload.LeftoverCheckFailed("SSH leftover check timed out after 0.125 s"))
        for value in ("bad", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                _ = self.settings.write_text(f"leftover_timeout_s={value}\n")
                self.assertEqual(offload.read_config(self.settings).leftover_timeout_s, 10)

    def test_probe_that_cannot_start_reports_its_limit_through_run(self) -> None:
        _ = self.settings.write_text("offload=on\nlinux_only.hana=\nprobe_timeout_s=2.5\n")
        destination = self.root / "result"
        responses = [offload.COMMAND_COULD_NOT_START, offload.CommandOutput(0, "", "")]

        def complete_claim(arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
            _ = arguments
            attempt.answer = offload.FreshMacClaim()

        with mock.patch.object(sys, "argv", ["offload.py", "run", "--repo-root", str(self.root),
                                             "--package", "hana", "--call-id", "probe-start", "--result", str(destination)]), \
             mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")), \
             mock.patch.object(offload.ProcessController, "complete_claim", side_effect=complete_claim), \
             mock.patch.object(offload.ProcessController, "capture", side_effect=responses) as capture, \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(offload.main(), 75)
        self.assertEqual(output.getvalue(), "mac_test: not running on the Mac (unreachable)\n")
        self.assertRegex(errors.getvalue(), r"^mac_test: could not reach the Mac: .*could not start.*2\.5.*\n$")
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(destination.read_text().splitlines()[:2], ["mac=declined", "reason=unreachable"])
        self.assertTrue((self.root / "state" / "unreachable_until").exists())

    def test_checked_in_settings_send_cargo_liner_to_the_mac_except_cargo_berth(self) -> None:
        config = offload.read_config(Path(__file__).resolve().parents[2] / "config" / "mac_test.conf")
        self.assertEqual(config.values["linux_only.cargo-liner"], "cargo-berth")
        self.assertEqual(config.default_target_budget_gib, "24")
        self.assertNotIn("mac_budget_gib.cargo-liner", config.values)
        offload.write_backoff(time.time() + 60)
        expected = {"cargo-port": "backoff", "cargo-tile": "backoff", "tui_pane": "backoff", "cargo-berth": "linux_only"}
        for package, reason in expected.items():
            with self.subTest(package=package):
                request = offload.OffloadRequest(self.root, package, "cargo-liner", offload.NO_RESULT_FILE, ())
                with mock.patch.object(offload, "repository_identity",
                                       return_value=offload.KnownRepository("cargo-liner")):
                    result = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
                self.assertEqual(result.reason if isinstance(result, offload.DeclinedMacRun) else result, reason)


@final
class BusyHolderTests(unittest.TestCase):
    def test_busy_claim_line_keeps_the_job_and_kept_claim_description(self) -> None:
        for what in ("cargo nextest -p hana", "leftovers of cargo test on prior-mac", "job since last week"):
            with self.subTest(what=what):
                self.assertEqual(offload.claim_holder(f"busy: {what} since 13:05\n"), offload.KnownMacJob(what))

    def test_busy_claim_without_the_expected_line_has_unreadable_holder(self) -> None:
        for line in ("", "busy", "busy: task", "busy: task since yesterday", "busy: task since 13:05\nextra"):
            with self.subTest(line=line):
                self.assertIsInstance(offload.claim_holder(line), offload.UnreadableMacHolder)

    def test_only_busy_results_write_the_holder_line(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "result"
            for holder, expected in ((offload.KnownMacJob("render checks"), "render checks"),
                                     (offload.LostRunLeftovers(), "leftovers"),
                                     (offload.UnreadableMacHolder(), "unreadable")):
                with self.subTest(holder=expected), redirect_stdout(io.StringIO()):
                    self.assertEqual(offload.write_result(offload.BusyMacDecline(holder, 0.25), offload.ResultFile(path)), 75)
                    self.assertIn(f"holder={expected}\n", path.read_text())
            with redirect_stdout(io.StringIO()):
                _ = offload.write_result(offload.PassedOnMac(0.5), offload.ResultFile(path))
            self.assertNotIn("holder=", path.read_text())


class OffloadCommandTests(OffloadCommandFixture):
    def test_configuration_declines_before_claiming(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.assert_declined(self.command(), "off")
        self.assertEqual(self.events(), [])

        self.config["offload"] = "on"
        _ = self.config.pop("linux_only.hana")
        self.sync_config()
        self.assert_declined(self.command(), "repo")
        self.assertEqual(self.events(), [])

        self.config["linux_only.hana"] = "hana,platform_only"
        self.sync_config()
        self.assert_declined(self.command(), "linux_only")
        self.assertEqual(self.events(), [])

    def test_directory_outside_git_declines_as_unknown_repository(self) -> None:
        result = self.command(repository=self.unknown_directory)
        self.assert_declined(result, "repo")
        self.assertEqual(self.events(), [])

    def test_unreachable_status_sets_backoff_without_a_second_ssh(self) -> None:
        self.fixture["probe_status"] = 255
        self.sync_fixture()
        self.assert_declined(self.command(), "unreachable")
        self.assertEqual(len(self.events_for("ssh")), 1)

        self.clear_events()
        self.assert_declined(self.command(), "backoff")
        self.assertEqual(self.events(), [])

    def test_invalid_backoff_file_does_not_skip_probe(self) -> None:
        self.state_directory.mkdir()
        _ = (self.state_directory / "unreachable_until").write_text(
            "not a number\n", encoding="utf-8"
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.stub_directory / "probe-started").exists())
        self.assertEqual(len(self.events_for("ssh")), 2)

    def test_probe_timeout_declines_as_unreachable(self) -> None:
        self.config["probe_timeout_s"] = "0.1"
        self.fixture["hold_probe"] = True
        self.sync_config()
        self.sync_fixture()
        original_launch = subprocess.Popen
        original_communicate = cast(Callable[[subprocess.Popen[str], str | None, float | None], tuple[str, str]],
                                    original_launch.communicate)

        def communicate(process: subprocess.Popen[str], input: str | None = None,
                        timeout: float | None = None) -> tuple[str, str]:
            if timeout == 0.1:
                self.wait_for_path(self.stub_directory / "probe-started")
                raise subprocess.TimeoutExpired(process.args, 0.1)
            return original_communicate(process, input, timeout)

        with mock.patch.object(original_launch, "communicate", communicate):
            result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertRegex(result.stderr, r"^mac_test: could not reach the Mac: .*0\.1.*\n$")
        self.assertRegex(result.stderr, r"(?i)tim(?:e|ed).*out|limit|finish")
        self.assertTrue((self.stub_directory / "probe-started").exists())
        self.assertEqual(len(self.events_for("ssh")), 1)

    def test_probe_without_final_status_declines_as_unreachable(self) -> None:
        self.fixture["probe_lines"] = self.fixture["probe_lines"][:-1]
        self.sync_fixture()
        result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertEqual(result.stderr, "mac_test: could not reach the Mac: the probe answer could not be read\n")
        self.assertEqual(len(self.events_for("ssh")), 1)

    def test_failed_probe_prints_collapsed_ssh_error_before_unreachable_decline(self) -> None:
        self.fixture["probe_status"] = 255
        self.fixture["probe_error"] = "ssh:\tconnection refused\n  host mac is unavailable  "
        self.sync_fixture()
        result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertEqual(result.stderr, "mac_test: could not reach the Mac: ssh: connection refused host mac is unavailable\n")
        self.assertEqual(self.events_for("rsync"), [])
        self.assertTrue((self.state_directory / "unreachable_until").exists())

    def test_other_build_processes_do_not_decline_mac_tests(self) -> None:
        self.fixture["probe_lines"] = [
            "procs=1",
            "free_gib=120",
            "host=Mac",
            "rc=0",
        ]
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertEqual(len(self.events_for("ssh")), 2)
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_run_settings_selects_alternate_host_and_budget_over_environment_default(self) -> None:
        alternate = self.root / "alternate settings.conf"
        values = {**self.config, "host": "alternate-mac", "mac_budget_gib.hana": "8"}
        _ = alternate.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        self.config["offload"] = "off"
        self.sync_config()
        result = self.command(settings=alternate)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        ssh = self.events_for("ssh")
        self.assertEqual(ssh[0]["args"][-2], "alternate-mac")
        self.assertEqual(ssh[1]["args"][-2], "alternate-mac")
        self.assertIn("LINT_SWEEP_BUDGET_GIB=8", ssh[1]["args"][-1])
        remote_words = shlex.split(ssh[1]["args"][-1].rpartition("; echo mac_exit=$?")[0])
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertEqual(remote_words[lint_index + 1 :], ["nextest"])
        self.assertIn("alternate-mac:.local/state/mac-test/mirror/hana/", self.events_for("rsync")[0]["args"])

    def test_low_disk_declines_after_probe(self) -> None:
        self.fixture["probe_lines"] = ["free_gib=59", "host=Mac", "rc=0"]
        self.sync_fixture()
        self.assert_declined(self.command(), "disk")
        self.assertEqual(self.events_for("rsync"), [])

    def test_battery_power_does_not_decline_mac_tests(self) -> None:
        self.assertNotIn("pmset", offload.probe_script(offload.KnownRepository("hana")))
        self.fixture["probe_lines"] = ["power=battery", "free_gib=120", "host=Mac", "rc=0"]
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")

    def test_claim_exit_reasons_never_reach_external_commands(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.state_directory.mkdir()
        block = {
            "version": 2,
            "holder": "alice",
            "for": "demo",
            "since": now.isoformat(),
            "expires": (now + timedelta(hours=1)).isoformat(),
            "session": None,
            "showrunner": None,
            "state": "active",
            "ci": "not_configured",
            "free_message": {"kind": "not_needed"},
        }
        _ = (self.state_directory / "block.json").write_text(
            json.dumps(block), encoding="utf-8"
        )
        self.assert_declined(self.command(), "blocked")
        self.assertEqual(self.events(), [])

        (self.state_directory / "block.json").unlink()
        claimed = self.state_command(
            "claim",
            "--pid",
            str(os.getpid()),
            "--what",
            "other tests",
            "--worktree",
            str(self.repository),
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        busy = self.command()
        self.assertEqual(busy.returncode, 75, busy.stdout)
        self.assertTrue(busy.stdout.endswith("mac_test: not running on the Mac (busy)\n"), busy.stdout)
        self.assertIn("busy", busy.stdout)
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "other tests")
        self.assertTrue(self.claim_file.exists())
        self.assertEqual(self.events(), [])
        released = self.state_command("release", "--pid", str(os.getpid()))
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

        _ = (self.state_directory / "block.json").write_text(
            "damaged\n", encoding="utf-8"
        )
        unreadable = self.command()
        self.assert_declined(unreadable, "state")
        self.assertIn("state unreadable", unreadable.stdout)
        self.assertIn("block.json", unreadable.stdout)
        self.assertEqual(self.events(), [])

    def test_retained_expired_block_still_declines_and_prints_holder_diagnostic(self) -> None:
        self.state_directory.mkdir()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": (now - timedelta(hours=2)).isoformat(),
                 "expires": (now - timedelta(seconds=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active",
                 "ci": "off_by_this_block", "free_message": {"kind": "not_needed"}}
        path = self.state_directory / "block.json"
        _ = path.write_text(json.dumps(block))
        before = path.read_bytes()
        result = self.command()
        self.assert_declined(result, "blocked")
        self.assertIn("blocked by alice: restore CI first", result.stdout)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.events(), [])

    def test_repeated_busy_decline_is_silent_and_preserves_wire_result(self) -> None:
        claimed = self.state_command("claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(self.repository))
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        first = self.command(separate_stderr=True)
        first_wire = self.wire_result()
        self.assertEqual(first.stdout, "mac_test: not running on the Mac (busy)\n")
        self.assertTrue(first.stderr)
        repeated = self.command(last_decline="busy", separate_stderr=True)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (75, "", ""))
        self.assertEqual(repeated.returncode, first.returncode)
        self.assertEqual(self.wire_result()["mac"], first_wire["mac"])
        self.assertEqual(self.wire_result()["reason"], first_wire["reason"])
        self.assertEqual(self.wire_result().get("holder"), "other tests")
        self.assertEqual(self.events(), [])
        self.assertTrue(self.claim_file.exists())

    def test_changed_decline_prints_normally_and_repeated_blocked_decline_is_silent(self) -> None:
        self.state_directory.mkdir()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": now.isoformat(), "expires": (now + timedelta(hours=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active",
                 "ci": "not_configured", "free_message": {"kind": "not_needed"}}
        _ = (self.state_directory / "block.json").write_text(json.dumps(block))
        first = self.command(separate_stderr=True)
        changed = self.command(last_decline="busy", separate_stderr=True)
        self.assertEqual((changed.returncode, changed.stdout, changed.stderr),
                         (first.returncode, first.stdout, first.stderr))
        self.assertEqual(changed.stdout, "mac_test: not running on the Mac (blocked)\n")
        self.assertIn("blocked by alice: restore CI first", changed.stderr)
        self.assert_wire("declined", "blocked")
        repeated = self.command(last_decline="blocked", separate_stderr=True)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (75, "", ""))
        self.assert_wire("declined", "blocked")
        self.assertEqual(self.events(), [])

    def test_copy_failure_releases_claim(self) -> None:
        self.fixture["rsync_status"] = 23
        self.sync_fixture()
        self.assert_declined(self.command(), "copy")
        self.assertEqual(len(self.events_for("ssh")), 1)
        self.assertEqual(len(self.events_for("rsync")), 1)
        self.assert_claimed_commands()

    def test_each_mac_verb_keeps_arguments_and_names_its_claim(self) -> None:
        self.write_portable_workspace()
        self.config["mac_skip.hana"] = "test(skip-this)"
        self.sync_config()
        for verb in ("nextest", "check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                words = ("--workspace", "--all-features")
                result = self.command(*words, verb=verb)
                self.assertEqual(result.returncode, 0, result.stdout)
                sent = shlex.split(self.events_for("ssh")[-1]["args"][-1].rpartition("; echo mac_exit=$?")[0])
                position = sent.index("~/.claude/scripts/lint/lint")
                self.assertEqual(sent[position + 1:], [verb, *words])
                if verb == "doc":
                    self.assertIn("LINT_CONFIG_FORCE=1", sent[:position])
                # The local claim is read by rsync before remote execution.
                held = self.events_for("rsync")[-1]
                self.assertTrue(held["claimed"])
                self.assertEqual(held.get("what"), f"{verb} hana")

    def test_no_package_names_the_mac_claim_for_the_workspace(self) -> None:
        self.write_portable_workspace()
        result = self.command("--workspace", verb="check", package="")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.events_for("rsync")[-1].get("what"), "check workspace")

    def test_exit_four_is_no_tests_only_for_nextest(self) -> None:
        self.write_portable_workspace()
        self.fixture["run_status_line"] = "mac_exit=4"
        self.sync_fixture()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                result = self.command("--workspace", verb=verb)
                self.assertEqual(result.returncode, 4, result.stdout)
                self.assertIn("fix the finding in the local worktree", result.stdout)
                self.assertNotIn("only a test", result.stdout)
                self.assert_wire("failed", "")
        result = self.command("--workspace")
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("mac_test: no test matched on the Mac; running on natedev instead", result.stdout)
        self.assert_wire("declined", "no_tests")

    def test_non_test_verbs_never_apply_nextest_skip_words_or_start_a_copy(self) -> None:
        self.write_portable_workspace()
        self.config["mac_skip.hana"] = "test(skip-this)"
        self.sync_config()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                result = self.command("-E", "leave-this-argument", verb=verb)
                self.assertEqual(result.returncode, 0, result.stdout)
                remote = self.events_for("ssh")[-1]["args"][-1]
                self.assertIn("-E leave-this-argument", remote)
                self.assertNotIn("skip-this", remote)
                self.assertFalse(self.events_for("rsync")[-1].get("copy_claim"))
                for option in ("copy", "filter"):
                    with self.subTest(option=option):
                        result = self.command(verb=verb, copy=option == "copy", filter_run=option == "filter")
                        self.assertEqual(result.returncode, 2, result.stdout)

    def test_linux_only_workspace_member_declines_every_non_test_verb_before_claim(self) -> None:
        self.write_portable_workspace()
        self.config["linux_only.hana"] = "portable"
        self.sync_config()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                self.assert_declined(self.command("--workspace", verb=verb), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_implicit_linux_member_blocks_workspace_verbs_through_target_and_shared_dependencies(self) -> None:
        for section in ("dependencies", "dev-dependencies", "build-dependencies", 'target.\'cfg(unix)\'.dependencies'):
            with self.subTest(section=section):
                self.write_portable_workspace()
                _ = (self.repository / "Cargo.toml").write_text(
                    '[workspace]\nmembers=["portable"]\n[workspace.dependencies]\nimplicit={path="implicit"}\n')
                member = self.repository / "implicit"
                member.mkdir(exist_ok=True)
                _ = (member / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
                with (self.repository / "portable/Cargo.toml").open("a") as output:
                    _ = output.write(f'[{section}]\nimplicit={{workspace=true}}\n')
                self.config["linux_only.hana"] = "linux-part"
                self.sync_config()
                for verb in ("check", "clippy", "mend-check", "doc"):
                    self.assert_declined(self.command("--workspace", verb=verb), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_unreadable_implicit_membership_refuses_workspace_before_claim(self) -> None:
        self.write_portable_workspace()
        with (self.repository / "portable/Cargo.toml").open("a") as output:
            _ = output.write('[dependencies]\nmissing={path="../missing"}\n')
        for verb in ("check", "clippy", "mend-check", "doc"):
            self.assert_declined(self.command("--workspace", verb=verb), "repo")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_doc_copies_the_supplied_snapshot_without_working_tree_ignores(self) -> None:
        self.write_portable_workspace()
        snapshot = self.root / "staged-doc"
        snapshot.mkdir()
        _ = (snapshot / "Cargo.toml").write_text('[package]\nname="staged"\nversion="0.1.0"\n')
        _ = (self.repository / ".gitignore").write_text("staged-file.rs\n")
        _ = (snapshot / "staged-file.rs").write_text("staged source")
        result = self.command("--workspace", verb="doc", source=offload.StagedDocSnapshot(snapshot))
        self.assertEqual(result.returncode, 0, result.stdout)
        copied, = self.events_for("rsync")
        self.assertIn(str(snapshot) + "/", copied["args"])
        self.assertNotIn(str(self.repository) + "/", copied["args"])
        self.assertNotIn("/staged-file.rs", copied.get("exclude", ""))
        self.config["linux_only.hana"] = "staged"
        self.sync_config()
        self.assert_declined(self.command("--workspace", verb="doc", source=offload.StagedDocSnapshot(snapshot)), "linux_only")
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_remote_test_command_sets_development_deployment_target_before_nextest(self) -> None:
        words = ("--no-fail-fast", "unit_case")
        result = self.command(*words)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        remote = self.events_for("ssh")[-1]["args"][-1]
        remote_words = shlex.split(remote.rpartition("; echo mac_exit=$?")[0])
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertEqual(remote_words[lint_index + 1 :], ["nextest", *words])

    def test_probe_copy_and_run_arguments_preserve_nextest_words(self) -> None:
        _ = (self.repository / ".gitignore").write_text(
            "generated/\n", encoding="utf-8"
        )
        generated = self.repository / "generated"
        generated.mkdir()
        _ = (generated / "artifact").write_text("ignored\n", encoding="utf-8")
        words = ("-E", FILTER, "--no-fail-fast", "unit_case")
        result = self.command(*words, filter_run=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse(self.claim_file.exists())
        self.assert_claimed_commands()

        events = self.events()
        self.assertEqual([event["command"] for event in events], ["ssh", "rsync", "ssh"])
        ssh_arguments = [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "mac",
        ]
        probe = events[0]["args"]
        self.assertEqual(probe[: len(ssh_arguments)], ssh_arguments)
        probe_script = probe[len(ssh_arguments)]
        for fragment in (
            ".local/state/mac-test/mirror/hana",
            'df -g "$HOME"',
            "hostname -s",
        ):
            self.assertIn(fragment, probe_script)
        self.assertNotIn("loadavg", probe_script)
        self.assertNotIn("pgrep", probe_script)
        self.assertNotIn("procs", probe_script)
        self.assertNotIn("pmset", probe_script)

        copy = events[1]
        exclude_argument = next(
            argument
            for argument in copy["args"]
            if argument.startswith("--exclude-from=")
        )
        self.assertEqual(
            copy["args"],
            [
                "-rlpc",
                "--delete",
                exclude_argument,
                "-e",
                "ssh -o BatchMode=yes -o ConnectTimeout=6 "
                + "-o ServerAliveInterval=15 -o ServerAliveCountMax=3",
                f"{self.repository}/",
                "mac:.local/state/mac-test/mirror/hana/",
            ],
        )
        self.assertEqual(copy.get("exclude"), "/.git\n/target/\n/generated/\n")

        run = events[2]["args"]
        self.assertEqual(run[: len(ssh_arguments)], ssh_arguments)
        remote = run[-1]
        run_command, separator, status_command = remote.rpartition("; echo mac_exit=$?")
        self.assertEqual(separator, "; echo mac_exit=$?")
        self.assertIn("mac_cleanup=", status_command)
        remote_words = shlex.split(run_command)
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertEqual(
            remote_words[lint_index :],
            ["~/.claude/scripts/lint/lint", "nextest", *words],
        )
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertIn("PATH=$HOME/.cargo/bin:$PATH", remote_words)
        self.assertIn("BUILDLOG_CALLER=verify-mac", remote_words)
        self.assertIn("BUILDLOG_CALL_ID=call-17", remote_words)
        self.assertIn("BUILDLOG_SYNC=1", remote_words)
        self.assertIn("LINT_SWEEP_BUDGET_GIB=64", remote_words)
        self.assertIn(shlex.quote(FILTER), remote)

    def test_non_ascii_ignored_directory_is_in_the_exclude_file(self) -> None:
        _ = (self.repository / ".gitignore").write_text(
            "café/\n", encoding="utf-8"
        )
        ignored_directory = self.repository / "café"
        ignored_directory.mkdir()
        _ = (ignored_directory / "artifact").write_text(
            "ignored\n", encoding="utf-8"
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        copy = self.events_for("rsync")[0]
        self.assertIn("/café/\n", copy.get("exclude", ""))

    def test_repository_skip_is_added_to_filter(self) -> None:
        self.config["mac_skip.hana"] = "test(slow) | test(gpu)"
        self.sync_config()
        result = self.command("-E", FILTER, filter_run=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        remote = self.events_for("ssh")[-1]["args"][-1]
        run_command, separator, status_command = remote.rpartition("; echo mac_exit=$?")
        self.assertEqual(separator, "; echo mac_exit=$?")
        self.assertIn("mac_cleanup=", status_command)
        remote_words = shlex.split(run_command)
        self.assertIn(f"({FILTER}) & not (test(slow) | test(gpu))", remote_words)
        self.assertNotIn(FILTER, remote_words)

    def test_unwritable_result_destination_returns_internal_error(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.result_path: Path = self.root / "missing" / "result"
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("mac_test: not running on the Mac (off)\n", result.stdout)
        self.assertIn("FileNotFoundError", result.stdout)
        self.assertFalse(self.result_path.exists())


if __name__ == "__main__":
    _ = unittest.main()
