"""Real verify.sh Mac offload behavior with disposable tools."""
from __future__ import annotations

import unittest

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import final
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import BusyClaimRecord, VerifyMacOffloadFixture, VERIFY as VERIFY
import signal
from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S as HANG_GUARD_S, RUNNER_STUB
import ctypes
import time
from unittest import mock


@final
class VerifyMacFallbackTests(VerifyMacOffloadFixture):
    def test_excluded_mac_reason_is_recorded_without_calling_offload(self) -> None:
        for settings, reason in (("offload=on\n", "repo"), (f"offload=on\nlinux_only.{self.root.name}=sample\n", "linux_only")):
            with self.subTest(reason=reason):
                self.clear_observations()
                _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(settings)
                result = self.verify("test", "sample", "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.last_call()["local_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")

    def test_declined_mac_attempt_announces_only_local_execution(self) -> None:
        for reason in ("busy", "blocked", "disk"):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": "75"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
                self.assertNotIn("mac_test: running on the Mac", result.stdout + result.stderr)
                self.assertIn("verify.sh: running on natedev", result.stdout)
                self.assertEqual(len(self.local_runs()), 1)

    def test_mac_state_and_probe_diagnostics_survive_local_fallback(self) -> None:
        for reason, diagnostic in (("state", "mac state cannot be read: invalid block"),
                                   ("blocked", "retained expired block; waiting for removal"),
                                   ("unreachable", "ssh probe failed")):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_REASON": reason, "TEST_RUNNER_OUTPUT": diagnostic})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.assertEqual(self.last_call()["mac_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")

    def test_decline_runs_same_nextest_words_locally_after_runner(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--filter",
            "beta",
            "--features",
            "extra",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "busy",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": "runner output",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runner output", result.stdout)
        runners = self.runner_events()
        local = self.local_runs()
        self.assertEqual(len(runners), 1)
        self.assertEqual(len(local), 1)
        runner_arguments = runners[0]["args"]
        self.assertEqual(runner_arguments[0], "run")
        self.assertEqual(local[0]["args"][2], "--test-threads")
        self.assertGreater(int(local[0]["args"][3]), 0)
        self.assertEqual(
            runner_arguments[runner_arguments.index("--") + 1 :],
            local[0]["args"][4:],
        )
        self.assertIn("--filter-run", runner_arguments)
        self.assertNotIn("--no-cache", runner_arguments)
        self.assertFalse(runners[0]["token_held"])
        self.assertTrue(local[0]["token_held"])
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "busy")
        self.assertEqual(record["mac_holder"], "other tests")
        self.assertEqual(record["mac_s"], 3.25)
        self.assertNotIn("--no-cache", record["command"])

    def test_busy_claim_what_reaches_call_holder_through_the_real_offload_result(self) -> None:
        state = Path(self.environment["MAC_TEST_STATE_DIR"])
        state.mkdir()
        record: BusyClaimRecord = {"pid": os.getpid(), "proc_start": build_hold.process_start_time(os.getpid()),
                  "what": "render checks", "worktree": str(self.root),
                  "since": datetime.now(timezone.utc).isoformat()}
        _ = (state / "run.json").write_text(json.dumps(record))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            "VERIFY_MAC_RUNNER": str(VERIFY.parent.parent / "mac_test" / "offload.py")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("busy: render checks since", result.stderr)
        self.assertEqual(self.last_call()["mac_holder"], "render checks")
        self.assertEqual(self.last_call()["mac_reason"], "busy")

    def test_no_test_match_runs_locally_and_reaches_call_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "no_tests",
                "TEST_RUNNER_SECONDS": "4.5",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": (
                    "mac_test: no test matched on the Mac; "
                    "running on natedev instead"
                ),
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead",
            result.stdout,
        )
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "no_tests")
        self.assertEqual(record["mac_s"], 4.5)



@final
class VerifyMacRoutingTests(VerifyMacOffloadFixture):
    def test_repeated_routing_wait_shows_each_unchanged_diagnostic_once(self) -> None:
        _ = self.routing_fixture()
        self.environment.update({"BUILD_TARGETS_ESCALATE_S": "0", "TEST_FREE_NOW_FREE": "1"})
        process = self.start_routing(step_sleep=True)
        for step in range(1, 4):
            self.resume_routing_sleep(step)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-4").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(errors.count("verify.sh: natedev is held by hana for render checks.\n"), 1)
        self.assertEqual(errors.count("verify.sh: natedev has been held for 2 h by hana; ask them.\n"), 1)
        self.assertEqual(errors.count("verify.sh: waiting for the Mac (busy) or natedev's release.\n"), 1)
        self.assertEqual(errors.count("claim diagnostic: busy\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertNotIn("verify.sh: running on the Mac", output + errors)
        attempts = self.runner_events()
        self.assertEqual(len(attempts), 5)
        self.assertNotIn("--last-decline", attempts[0]["args"])
        for event in attempts[1:]:
            arguments = event["args"]
            self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")

    def test_routing_wait_shows_changed_holder_and_decline_reason_once(self) -> None:
        _ = self.routing_fixture()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_routing(step_sleep=True)
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, build_hold.HolderIdentity("julian", "julian"), "restore CI", build_hold.NoReleaseEta(), datetime.now().astimezone())
            (holders / "hana").unlink()
        self.resume_routing_sleep(1)
        self.resume_routing_sleep(2)
        _ = (self.runner_state / "busy-reason").write_text("blocked\n")
        self.resume_routing_sleep(3)
        self.resume_routing_sleep(4)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-5").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        for line in ("verify.sh: natedev is held by hana for render checks.\n",
                     "verify.sh: natedev is held by julian for restore CI.\n",
                     "verify.sh: waiting for the Mac (busy) or natedev's release.\n",
                     "verify.sh: waiting for the Mac (blocked) or natedev's release.\n"):
            self.assertEqual(errors.count(line), 1, output + errors)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (blocked)\n"), 1)
        attempts = self.runner_events()
        self.assertEqual([event["args"][event["args"].index("--last-decline") + 1] for event in attempts[1:]],
                         ["busy", "busy", "busy", "blocked", "blocked"])

    def test_hold_wait_includes_declined_retry_seconds_and_excludes_running_retry(self) -> None:
        _ = self.routing_fixture()
        clock = self.control_routing_clock()
        self.environment.update({"TEST_DECLINED_RETRY_SECONDS": "4.5", "TEST_RUNNER_SECONDS": "20",
                                 "TEST_FREE_NOW_FREE": "1"})
        process = self.start_routing(step_sleep=True)
        _ = clock.write_text("102")
        self.resume_routing_sleep(1)
        self.wait_for_path(self.runner_state / "routing-sleep-2")
        _ = clock.write_text("105")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-2").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 3)
        self.assertEqual(self.routing_record()["hold_wait_s"], 2 + 4.5 + 3)
        self.assertEqual(self.last_call()["mac_s"], 20)

    def test_busy_retry_to_blocked_or_malformed_answer_clears_call_holder(self) -> None:
        for mode, reason, expected in (("declined", "blocked", "blocked"), ("none", "", "runner")):
            fixture = VerifyMacOffloadFixture("runTest")
            try:
                fixture.setUp()
                _ = fixture.routing_fixture()
                process = fixture.start_routing(pause_retry=True)
                # start_routing sets a successful final answer; select this case's answer here.
                fixture.write_executable(fixture.runner, RUNNER_STUB.replace(
                    'mode = os.environ.get("TEST_RUNNER_MAC", "declined")', f'mode = "{mode}"').replace(
                    'os.environ.get("TEST_RUNNER_REASON", "")', f'"{reason}"'))
                _ = (fixture.runner_state / "mac-free").write_text("free\n")
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                fixture.wait_for_path(fixture.runner_state / "retry-running")
                with fixture.hold_environment():
                    build_hold.holder_path(build_hold.holder_directory(), "hana").unlink()
                _ = (fixture.runner_state / "continue-retry").write_text("continue\n")
                _ = (fixture.runner_state / "continue-sleep-2").write_text("continue\n")
                output, errors = process.communicate(timeout=HANG_GUARD_S)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(fixture.last_call()["mac_reason"], expected)
                self.assertEqual(fixture.last_call()["mac_holder"], "")
            finally:
                fixture.doCleanups()

    def test_released_hold_keeps_only_routing_wait_seconds(self) -> None:
        _ = self.routing_fixture()
        clock = self.control_routing_clock()
        process = self.start_routing()
        _ = clock.write_text("103")
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        record = self.routing_record()
        self.assertEqual(record["hold_wait_s"], 3)
        self.assertEqual(record["mem_wait_s"], "0")
        self.assertFalse((self.runner_state / "local-wait-added").exists())

    def test_default_routing_retry_is_paced_fifteen_seconds(self) -> None:
        _ = self.routing_fixture()
        _ = self.environment.pop("BUILD_TARGETS_POLL_S")
        process = self.start_routing()
        self.assertEqual((self.runner_state / "sleep-events").read_text().splitlines(), ["15"])
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_interrupted_routing_sleep_records_wait_and_cleans_pending_call(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle)
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(list((self.root / "tmp").glob("verify-hold.*")), [])



@final
class VerifyMacReleaseTests(VerifyMacOffloadFixture):
    def test_busy_mac_and_held_local_wait_then_use_first_free_mac(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("waiting", errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        commands = [event["command"] for event in self.routing_events()]
        self.assertNotIn("finish-local", commands)
        registrations = [event for event in self.routing_events() if event["command"] == "register-local"]
        self.assertTrue(registrations)
        self.assertTrue(all(event["pid"] == process.pid for event in registrations))
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.last_call()["mac_holder"], "")

    def test_local_release_wins_before_another_mac_attempt(self) -> None:
        for released_by_command in (False, True):
            with self.subTest(released_by_command=released_by_command):
                # This test owns a separate fixture for each release variant.
                fixture = VerifyMacOffloadFixture("runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                cycle = fixture.routing_fixture()
                process = fixture.start_routing()
                if released_by_command:
                    fixture.release_local_hold()
                else:
                    (Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").unlink()
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                output, errors = process.communicate(timeout=HANG_GUARD_S)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(len(fixture.runner_events()), 1)
                self.assertEqual(len(fixture.local_runs()), 1)
                self.assertFalse((Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
                fixture.assert_no_pending_calls(cycle)
                self.assertEqual(fixture.routing_record()["target"], "natedev")

    def test_blocked_mac_and_held_local_wait_for_local_release(self) -> None:
        cycle = self.routing_fixture()
        # Exercise the real event waiter. A retry cannot rescue a missed release:
        # its interval is longer than the process hang guard, and a fixed sleep
        # is an immediate fixture error instead of a wall-clock assertion.
        self.environment["BUILD_TARGETS_POLL_S"] = "3600"
        self.write_executable(self.bin_directory / "sleep", 'raise SystemExit("routing must wake on release, not sleep")\n')
        self.write_executable(Path(self.environment["VERIFY_LOCAL_RELEASE_WAIT_SCRIPT"]), f'''
import os
from pathlib import Path
import sys
sys.path.insert(0, {str(VERIFY.parent)!r})
import local_release_wait
original_wait = local_release_wait.LocalReleaseEvents.wait
def observe_wait(self, seconds):
    (Path(os.environ["TEST_RUNNER_STATE"]) / "routing-sleep").write_text("watching\\n")
    original_wait(self, seconds)
local_release_wait.LocalReleaseEvents.wait = observe_wait
local_release_wait.wait_for_local_release(float(sys.argv[1]), sys.argv[2])
''')
        process = self.start_routing("blocked")
        self.release_local_hold()
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("blocked", errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(len(self.local_runs()), 1)
        self.assert_no_pending_calls(cycle)

    def test_local_release_before_routing_watcher_starts_needs_no_retry_wait(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "already-released"
        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            self.release_local_hold()
            with mock.patch.object(local_release_wait.LocalReleaseEvents, "wait",
                                   side_effect=AssertionError("an admitted call must not wait for another event")):
                local_release_wait.wait_for_local_release(3600, call_id)
        self.assert_no_pending_calls(cycle)

    def test_routing_without_inotify_polls_until_local_release(self) -> None:
        from scripts.direct import local_release_wait

        cycle = self.routing_fixture()
        call_id = "polling-routing"
        def release_on_poll(_seconds: float) -> None:
            self.release_local_hold()

        with mock.patch.dict(os.environ, self.environment), self.hold_environment():
            with mock.patch.object(ctypes, "CDLL", return_value=object()), \
                 mock.patch.object(time, "monotonic", return_value=100), \
                 mock.patch.object(time, "sleep", side_effect=release_on_poll) as polling:
                local_release_wait.wait_for_local_release(15, call_id)
            polling.assert_called_once_with(15)
            self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assert_no_pending_calls(cycle)

    def test_mac_run_withdraws_local_demand_before_another_waiter_is_released(self) -> None:
        cycle_path = self.routing_fixture()
        process = self.start_routing(pause_retry=True)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        self.assert_no_pending_calls(cycle_path)
        self.release_local_hold()
        self.assertFalse((Path(self.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        self.assertIsNone(process.poll())

        _ = (self.runner_state / "continue-retry").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])

    def test_interrupted_mac_retry_withdraws_registration_and_preserves_only_wait_time(self) -> None:
        cycle_path = self.routing_fixture()
        clock = self.control_routing_clock()
        process = self.start_routing(pause_retry=True)
        _ = clock.write_text("102")
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        _ = clock.write_text("500")
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle_path)
        record = self.routing_record()
        self.assertEqual(record["hold_wait_s"], 2)
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(self.last_call()["mac_holder"], "other tests")

    def test_free_mac_wins_when_local_hold_was_released_in_the_same_pass(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        self.release_local_hold()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")



if __name__ == "__main__":
    _ = unittest.main()
