"""Real verify.sh Mac offload behavior with disposable tools."""
from __future__ import annotations

import unittest

import os
import select
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast, final
from unittest import mock
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, HANG_GUARD_S as HANG_GUARD_S, VERIFY as VERIFY, stub_build_tool_path as stub_build_tool_path, CARGO_STUB as CARGO_STUB, RUSTC_STUB as RUSTC_STUB, CallRecord as CallRecord


@final
class VerifyMacOptionsTests(VerifyMacOffloadFixture):
    def test_fixture_waits_for_a_detached_child_to_finish_before_checking_for_leaks(self) -> None:
        release = self.runner_state / "recorder-release"
        child = subprocess.Popen(
            [sys.executable, self.environment["TEST_WAIT_HELPER"], str(release)],
            env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        observe_exits = select.select
        observed: list[int] = []

        def release_on_exit_wait(readers: list[int], writers: list[int], errors: list[int],
                                 timeout: float) -> tuple[list[int], list[int], list[int]]:
            observed.extend(readers)
            release.touch()
            return observe_exits(readers, writers, errors, timeout)

        try:
            with mock.patch.object(select, "select", side_effect=release_on_exit_wait):
                self.assert_nothing_outlives()
            output, errors = child.communicate(timeout=HANG_GUARD_S)
            self.assertTrue(observed, "cleanup did not observe the live child's exit")
            self.assertEqual(child.returncode, 0, output + errors)
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.communicate(timeout=HANG_GUARD_S)

    def test_private_memory_pressure_blocks_local_work_until_its_release_event(self) -> None:
        pressure = Path(self.environment["BUILDLOG_MEMORY_PRESSURE"])
        _ = pressure.write_text("full avg10=20.00 avg60=0.00 avg300=0.00 total=0\n")
        waiting = self.runner_state / "pressure-wait"
        release = self.runner_state / "pressure-release"
        self.write_executable(self.bin_directory / "sleep", '''#!/bin/sh
: > "$TEST_PRESSURE_WAIT"
exec "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_PRESSURE_RELEASE"
''')
        process = self.start_verify("check", "sample", extra={
            "TEST_PRESSURE_WAIT": str(waiting), "TEST_PRESSURE_RELEASE": str(release),
        })
        try:
            self.wait_for_path(waiting, (process,))
            self.assertEqual([event for event in self.cargo_events() if event["args"][:1] == ["check"]],
                             [], "pressure did not block local execution")
            ready = pressure.with_suffix(".ready")
            _ = ready.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
            _ = ready.replace(pressure)
            release.touch()
            output, errors = process.communicate(timeout=HANG_GUARD_S)
            self.assertEqual(process.returncode, 0, output + errors)
            self.assertIn("memory pressure: full avg10 20.0", output + errors)
            self.assertEqual(len([event for event in self.cargo_events()
                                  if event["args"][:1] == ["check"]]), 1)
        finally:
            if process.poll() is None:
                self.stop_verify(process)

    def test_build_tools_cannot_use_the_host_path(self) -> None:
        host_tools = self.root / "host-tools"
        host_tools.mkdir()
        reached = self.root / "host-build-tool-ran"
        for name in ("cargo", "rustc", "nextest"):
            self.write_executable(host_tools / name, f'#!/bin/sh\ntouch "{reached}"\nexit 98\n')
        private_tools = self.root / "private-tools"
        private_tools.mkdir()
        with mock.patch.dict(os.environ, {"PATH": f"{host_tools}{os.pathsep}{os.environ['PATH']}"}):
            private_path = stub_build_tool_path(private_tools)
        for name in ("cargo", "rustc", "nextest"):
            with self.subTest(tool=name):
                result = subprocess.run([name, "--version"], env={**self.environment, "PATH": private_path},
                                        capture_output=True, text=True, check=False, timeout=HANG_GUARD_S)
                self.assertEqual(result.returncode, 99, result.stdout + result.stderr)
        self.assertFalse(reached.exists())

    def test_hold_fixture_reaps_its_recorders_without_waiting_for_an_existing_recorder(self) -> None:
        # The real hold dispatcher starts this child and its reaper. Only a
        # join at the fixture boundary releases the child to write completion.
        tree = self.root / "record-tree/scripts"
        (tree / "build_hold").mkdir(parents=True)
        (tree / "buildlog").mkdir()
        release, completed = self.root / "recorder-release", self.root / "recorder-completed"
        self.write_executable(tree / "buildlog/record.py", f'''
from pathlib import Path
from scripts.direct.wait_for_test_state import wait_for_file
wait_for_file(Path({str(release)!r}))
Path({str(completed)!r}).touch()
'''.replace("from scripts.direct.wait_for_test_state import wait_for_file",
            "import sys\nsys.path.insert(0, " + repr(str(VERIFY.resolve().parents[2])) + ")\n"
            + "from scripts.direct.wait_for_test_state import wait_for_file"))
        outside_release = threading.Event()
        outside = threading.Thread(target=outside_release.wait, name="hold-event-recorder", daemon=True)
        outside.start()
        original_join = threading.Thread.join
        owned: list[threading.Thread] = []

        def release_on_join(thread: threading.Thread, timeout: float | None = None) -> None:
            if thread.name == "hold-event-recorder":
                self.assertIsNot(thread, outside, "fixture tried to wait for a recorder it did not start")
                owned.append(thread)
                release.touch()
            original_join(thread, timeout)

        existing = set(threading.enumerate())
        try:
            with mock.patch.object(build_hold, "__file__", str(tree / "build_hold/build_hold.py")), \
                    mock.patch.object(threading.Thread, "join", release_on_join):
                with self.hold_environment():
                    _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                        build_hold.HolderIdentity("hana", "hana"), "fixture recording", build_hold.NoReleaseEta(),
                        datetime.now().astimezone())
                self.assertTrue(completed.exists(), "fixture returned while its recorder still owned its files")
            self.assertEqual(len(owned), 1)
            self.assertFalse(owned[0].is_alive())
            self.assertTrue(outside.is_alive())
        finally:
            release.touch()
            outside_release.set()
            for thread in threading.enumerate():
                if thread is outside or (thread not in existing and thread.name == "hold-event-recorder"):
                    original_join(thread, HANG_GUARD_S)
                    self.assertFalse(thread.is_alive())

    def test_hold_fixture_timeout_reaps_all_children_and_owned_threads_before_restoring_home(self) -> None:
        tree = self.root / "blocked-record-tree/scripts"
        (tree / "build_hold").mkdir(parents=True)
        (tree / "buildlog").mkdir()
        ready = self.root / "blocked-recorders"
        ready.mkdir()
        self.write_executable(tree / "buildlog/record.py", f'''
import os, signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}, sys.argv[4]).write_text(str(os.getpid()))
signal.pause()
''')
        children: list[subprocess.Popen[bytes]] = []
        threads: list[threading.Thread] = []
        launch = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)
        start = threading.Thread.start
        caller_home = os.environ.get("HOME")

        def capture_child(*arguments: object, **options: object) -> subprocess.Popen[bytes]:
            child = launch(*arguments, **options)
            children.append(child)
            return child

        def capture_thread(thread: threading.Thread) -> None:
            if thread.name == "hold-event-recorder":
                threads.append(thread)
            start(thread)

        try:
            with mock.patch.object(build_hold, "__file__", str(tree / "build_hold/build_hold.py")), \
                    mock.patch.object(subprocess, "Popen", side_effect=capture_child), \
                    mock.patch.object(threading.Thread, "start", capture_thread):
                with self.assertRaisesRegex(AssertionError, "fixture hold recorder never exited"):
                    with self.hold_environment(timeout=0):
                        for key in ("first", "second"):
                            _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]),
                                build_hold.HolderIdentity(key, key), "blocked recording", build_hold.NoReleaseEta(),
                                datetime.now().astimezone())
                            self.wait_for_path(ready / key)
                self.assertEqual(len(children), 2)
                self.assertEqual(len(threads), 2)
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(os.environ.get("HOME"), caller_home)
        finally:
            # Also contain a deliberately broken implementation during regression checks.
            for child in children:
                if child.poll() is None:
                    child.kill()
                _ = child.wait()
            for thread in threads:
                thread.join(timeout=HANG_GUARD_S)

    def test_local_flag_requires_a_non_option_reason_before_any_work(self) -> None:
        expected = (
            'verify.sh: --local needs a reason: --local "<why this test cannot pass on '
            'macOS>". Leave it off to run on the Mac.\n'
        )
        cases = (
            ("test", "sample", "--local"),
            ("test", "sample", "--local", ""),
            ("test", "sample", "--local", " \t "),
            ("test", "sample", "--local", "--filter", "alpha"),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, expected)
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_local_flag_refuses_a_mac_reservation_before_any_work(self) -> None:
        reasons = (
            "Mac reserved for demo",
            "RESERVATION: demo has the Mac",
            "showrunner ruling: no Mac claim",
            "Mac held by demo",
            "the Mac is blocked",
            "Mac busy, hold off",
        )

        for reason in reasons:
            with self.subTest(reason=reason):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify("test", "sample", "--local", reason)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    result.stderr,
                    "verify.sh: --local is only for a test that cannot pass on macOS, not to keep "
                    + f'tests off the Mac ("{reason}"). To reserve the Mac, its holder runs '
                    + "/build:hold hold <why> target mac: tests then skip the Mac by themselves, "
                    + "/build:status shows the hold, and it ends at its expiry. Leave --local off.\n",
                )
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_local_flag_accepts_linux_reasons_that_contain_reservation_letters(self) -> None:
        for reason in ("uses a threshold only Linux reports", "needs Wayland; the household fixture is Linux only"):
            with self.subTest(reason=reason):
                result = self.verify("test", "sample", "--local", reason, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.clear_observations()

    def test_local_flag_skips_runner_records_reason_and_shares_pass_key(self) -> None:
        first_reason = 'needs Linux path handling with a "quoted" mount'
        first = self.verify(
            "test",
            "sample",
            "--local",
            first_reason,
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        first_record = self.last_call()
        self.assertEqual(first_record["command"], "test sample")
        self.assertEqual(first_record["mac"], "declined")
        self.assertEqual(first_record["mac_reason"], "local_flag")
        self.assertEqual(first_record["local_reason"], first_reason)
        self.assertNotIn(first_reason, first_record["command"])
        saved_results = self.saved_results()
        self.assertEqual(len(saved_results), 1)

        self.clear_observations()
        without_flag = self.verify("test", "sample")
        self.assertEqual(
            without_flag.returncode, 0, without_flag.stdout + without_flag.stderr
        )
        self.assertIn("PASS (recorded)", without_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)

        self.clear_observations()
        second_reason = "requires Linux-only process behavior"
        with_flag = self.verify("test", "sample", "--local", second_reason)
        self.assertEqual(with_flag.returncode, 0, with_flag.stdout + with_flag.stderr)
        self.assertIn("PASS (recorded)", with_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)
        second_record = self.last_call()
        self.assertEqual(second_record["local_reason"], second_reason)
        self.assertEqual(second_record["command"], "test sample")

    def test_local_flag_parses_before_and_after_filter(self) -> None:
        reason = "uses Linux filesystem semantics"
        cases = (
            ("test", "sample", "--local", reason, "--filter", "alpha"),
            ("test", "sample", "--filter", "alpha", "--local", reason),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                local = self.local_runs()
                self.assertEqual(len(local), 1)
                self.assertNotIn("--local", local[0]["args"])
                self.assertNotIn(reason, local[0]["args"])
                record = self.last_call()
                self.assertEqual(record["command"], "test sample --filter alpha")
                self.assertEqual(record["mac_reason"], "local_flag")
                self.assertEqual(record["local_reason"], reason)

    def test_missing_runner_skips_offload(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"VERIFY_MAC_RUNNER": str(self.root / "absent-runner.py")},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)



@final
class VerifyMacConfigurationTests(VerifyMacOffloadFixture):
    def test_local_first_target_runs_without_trying_free_mac(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        self.assertIn("verify.sh: running on natedev", result.stdout)
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_alternate_target_settings_are_passed_to_runner(self) -> None:
        alternate = self.root / "alternate Mac.conf"
        _ = alternate.write_text(f"offload=on\nlinux_only.{self.root.name}=\n")
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("mac settings.conf", alternate.name))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache",
                             extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(arguments[arguments.index("--settings") + 1], str(alternate))
        self.assertIn("mac_test: running on the Mac (macOS, host Mac); tree copied in 0.0 s", result.stdout)
        self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
        self.assertEqual(self.routing_record()["target"], "mac")

    def test_unreadable_local_first_route_refuses_before_either_machine_starts(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        for outcome, reason, status in (("passed", "", "0"), ("declined", "busy", "75")):
            with self.subTest(outcome=outcome):
                self.clear_observations()
                result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
                    "TEST_RUNNER_MAC": outcome, "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": status})
                self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
                self.assertIn("build hold check register-local failed (exit 12); step refused", result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual([event for event in self.cargo_events()
                                  if event["args"][:1] != ["metadata"]], [])
                self.assertIn("natedev's hold state cannot be read", result.stderr)

    def test_unreadable_local_first_route_without_eligible_mac_exits_twelve(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(f"linux_only.{self.root.name}=sample\n")
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertIn("natedev's hold state cannot be read", result.stderr)

    def test_runner_result_uses_fixture_temp_directory(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "declined", "TEST_RUNNER_STATUS": "75"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runner = self.runner_events()[0]
        result_index = runner["args"].index("--result") + 1
        self.assertTrue(
            Path(runner["args"][result_index]).is_relative_to(self.root / "tmp"),
            runner,
        )

    def test_missing_temp_root_is_created_before_verify_runs(self) -> None:
        missing = self.root / "new-session" / "tmp"
        result = self.verify("test", "sample", "--no-cache", extra={"TMPDIR": str(missing)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(missing.is_dir())
        self.assertEqual(len(self.local_runs()), 1)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(Path(arguments[arguments.index("--result") + 1]).parent, missing)

    def test_lint_entry_points_create_missing_temp_roots(self) -> None:
        reader = self.root / "lint-config.sh"
        _ = reader.write_text("lint_config_enabled() { return 0; }\n")
        lint = VERIFY.parent.parent / "lint"
        for entry in ("invoke.sh", "lint"):
            with self.subTest(entry=entry):
                missing = self.root / entry / "tmp"
                arguments = (["bash", "-c", 'set -euo pipefail; source "$1"; run_once printf "step ran\\n"',
                              "temp-root-test", str(lint / entry)] if entry == "invoke.sh" else
                             ["bash", str(lint / entry), "fmt", "--all", "--check"])
                result = subprocess.run(arguments, cwd=self.root,
                    env={**self.environment, "TMPDIR": str(missing)}, capture_output=True,
                    text=True, check=False, timeout=HANG_GUARD_S)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(missing.is_dir())



@final
class VerifyMacResultsTests(VerifyMacOffloadFixture):
    def test_lost_run_continues_locally(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "lost", "TEST_RUNNER_STATUS": "75"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "lost")
        self.assertEqual(record["mac_s"], 3.25)

    def test_mac_failure_stops_before_token_and_local_run(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertFalse(runners[0]["token_held"])
        self.assertEqual(self.saved_results(), [])
        record = self.last_call()
        self.assertEqual(record["status"], 42)
        self.assertEqual(record["mac"], "failed")
        self.assertEqual(record["mac_reason"], "")
        self.assertEqual(record["mac_s"], 3.25)

    def test_unusable_runner_results_continue_locally(self) -> None:
        cases = (
            ("none", "75"),
            ("none", "1"),
            ("empty", "75"),
            ("directory", "75"),
            ("other", "75"),
            ("passed", "1"),
            ("failed", "0"),
        )
        for mode, status in cases:
            with self.subTest(mode=mode, status=status):
                self.clear_observations()
                result = self.verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_MAC": mode, "TEST_RUNNER_STATUS": status},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                record = self.last_call()
                self.assertEqual(record["mac"], "declined")
                self.assertEqual(record["mac_reason"], "runner")
                self.assertIsNone(record["mac_s"])

    def test_full_mac_pass_ends_the_call_with_a_mac_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", result.stdout)
        self.assertEqual(self.local_runs(), [])
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("passed on the Mac", saved[0].read_text())
        self.assertIn("host=mac\n", saved[0].read_text())
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 3.25))

        self.clear_observations()
        again = self.verify("test", "sample", extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("PASS (recorded)", again.stdout)
        self.assertEqual(self.runner_events(), [])

        # --local asks for Linux, so the Mac's pass does not answer it.
        self.clear_observations()
        local = self.verify("test", "sample", "--local", "needs Linux process behavior")
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        self.assertNotIn("PASS (recorded)", local.stdout)
        self.assertEqual(len(self.local_runs()), 1)
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertNotIn("host=mac", saved[0].read_text())

    def test_filtered_mac_pass_ends_the_call_with_a_mac_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", result.stdout)
        self.assertEqual(self.local_runs(), [])
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        record = self.last_call()
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["mac"], "passed_filter")
        self.assertEqual(record["mac_s"], 3.25)



# Compatibility for fixtures that use the shared shell/tool contract.
VerifyMacOffloadTests = VerifyMacOffloadFixture


if __name__ == "__main__":
    _ = unittest.main()
