"""Real verify.sh Mac offload behavior with disposable tools."""
from __future__ import annotations

import unittest

import subprocess
from datetime import datetime
from pathlib import Path
from typing import final
from scripts.build_hold import build_hold
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, HANG_GUARD_S as HANG_GUARD_S, VERIFY as VERIFY
from scripts.direct.verify_mac_offload_test_support import TARGETS_WRAPPER
import os
import signal
import sys
from scripts.direct.verify_mac_offload_test_support import processes_carrying


@final
class VerifyMacRetryTests(VerifyMacOffloadFixture):
    def release_memory(self, meminfo: Path) -> None:
        # A polling reader must not see the empty interval of a truncated file.
        ready = meminfo.with_suffix(".ready")
        _ = ready.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        _ = ready.replace(meminfo)

    def test_queued_local_test_leaves_for_a_freed_mac_and_ends_with_its_failure(self) -> None:
        _ = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        self.assertEqual(len(self.runner_events()), 1)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertIn("another machine is free after", output)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 2)
        # The step gave the token back for its memory wait and left for the Mac
        # without taking it again.
        self.assertFalse(runners[1]["token_held"])
        arguments = runners[1]["args"]
        self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")
        self.assertEqual(set(self.free_now_settings()), {self.environment["MAC_TEST_CONFIG"]})
        self.assertEqual(set(self.free_now_roots()), {f"--repo-root {self.root}"})
        self.assertEqual(self.routing_record()["target"], "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 3.25))

    def test_a_step_that_left_for_the_mac_ends_without_the_token_a_peer_now_holds(self) -> None:
        _ = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        # The step gave the token back for its memory wait, and a peer takes it.
        lock = self.board / "locks" / "cargo.d"
        self.assertFalse(lock.exists())
        peer = subprocess.Popen(["sleep", "600"])

        def stop_peer() -> None:
            peer.kill()
            _ = peer.wait(timeout=5)

        self.addCleanup(stop_peer)
        acquired = subprocess.run(["bash", str(VERIFY.with_name("board.sh")), "acquire", str(self.board),
                                   "peer", "cargo", "--pid", str(peer.pid)],
                                  capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(acquired.returncode, 0, acquired.stderr)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac"], "passed_full")
        self.assertEqual((lock / "holder_pid").read_text(), str(peer.pid))

    def test_hold_that_lands_during_the_memory_wait_still_leaves_for_a_freed_mac(self) -> None:
        meminfo = self.hold_memory()
        mac_free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(mac_free),
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        with self.hold_environment():
            _ = build_hold.start_hold(Path(self.environment["BUILD_HOLD_DIR"]), build_hold.HolderIdentity("hana", "hana"),
                                      "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone())
            cycle = build_hold.read_cycle()
            assert cycle is not None
        cycle_path = self.root / "release" / cycle["id"] / "cycle.json"

        def waiting_on_the_hold() -> bool:
            return any(Path(self.environment["TMPDIR"]).glob("verify-hold.*/waiter"))

        # Memory frees while natedev is held: the step moves to the hold wait,
        # where the Mac is still busy.
        self.release_memory(meminfo)
        self.wait_until(waiting_on_the_hold, "the hold wait", (process,))
        self.assertEqual(len(self.runner_events()), 1)
        _ = mac_free.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertIn("leaving natedev's hold wait to run there", output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)
        self.assert_no_pending_calls(cycle_path)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "failed")

    def test_mac_busy_again_after_leaving_returns_to_the_local_wait(self) -> None:
        meminfo = self.hold_memory()
        process = self.start_verify("test", "sample", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"), **self.leave_once()})
        self.addCleanup(self.stop_verify, process)
        # The Mac claim declined again; the local run waits with the hook still set.
        self.wait_for_path(self.runner_state / "free-now-after-leave", (process,))
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.local_runs()), 1)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(output.count("verify.sh: running on natedev"), 2)
        self.assertEqual(self.routing_record()["target"], "natedev")
        record = self.last_call()
        self.assertEqual((record["mac"], record["mac_reason"]), ("declined", "busy"))

    def test_leaving_memory_wait_after_release_runs_on_the_mac(self) -> None:
        cycle = self.routing_fixture()
        _ = self.hold_memory()
        once = self.runner_state / "free-once"
        process = self.start_routing(once=once)
        self.release_local_hold()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        # Routing asks free-now once after its sleep; later calls are the hook's.
        self.wait_until(lambda: len(self.free_now_settings()) >= 2, "the leave hook", (process,))
        self.assertEqual(self.local_runs(), [])
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = once.write_text("free\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 2)

        self.assert_no_pending_calls(cycle)
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertEqual(self.last_call()["mac"], "passed_filter")

    def test_lost_mac_run_retries_from_the_memory_wait(self) -> None:
        self.check_temporary_mac_decline("memory", "lost", "")

    def test_excluded_mac_is_never_tried_while_local_memory_waits(self) -> None:
        meminfo = self.hold_memory()
        settings = Path(self.environment["MAC_TEST_CONFIG"])
        _ = settings.write_text(f"offload=on\nlinux_only.{self.root.name}=sample\n")
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_permanent_mac_decline_has_no_leave_hook(self) -> None:
        meminfo = self.hold_memory()
        self.set_mac_answer("declined", "no_tests", 75)
        clock = self.control_routing_clock()
        self.environment["TEST_FREE_NOW_FREE"] = "1"
        process = self.start_verify("test", "sample", "--no-cache")
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "sleep-events", (process,))
        _ = clock.write_text("1000")
        self.release_memory(meminfo)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(self.free_now_settings(), [])
        self.assertEqual(len(self.local_runs()), 1)



@final
class VerifyMacRetryClockTests(VerifyMacOffloadFixture):
    def test_idle_marker_bypasses_memory_retry_spacing(self) -> None:
        self.check_idle_marker("memory")

    def test_idle_marker_bypasses_routing_retry_spacing(self) -> None:
        self.check_idle_marker("routing")

    def test_idle_marker_bypasses_token_retry_spacing(self) -> None:
        self.check_idle_marker("token")

    def test_runner_decline_retries_from_the_routing_wait(self) -> None:
        self.check_temporary_mac_decline("routing", "none", "")

    def test_lost_mac_run_retries_from_a_later_hold_wait(self) -> None:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
            "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILD_TARGETS_POLL_S": "0.05"})
        self.check_temporary_mac_decline("hold", "lost", "", repeat_decline=False)

    def test_lost_mac_run_retries_when_a_hold_starts_after_routing(self) -> None:
        self.check_temporary_mac_decline("first_hold", "lost", "", repeat_decline=False)



@final
class VerifyMacCopyTests(VerifyMacOffloadFixture):
    def test_a_stopped_call_leaves_no_step_or_copy_running(self) -> None:
        process, copy = self.start_copy_call(copy={"TEST_COPY_WAIT": "1"})
        cargo = int((self.runner_state / "cargo-compiling").read_text())
        # The step runs in its own process group, so a kill of verify's group alone misses it.
        self.assertNotEqual(os.getpgid(cargo), process.pid)
        self.assertTrue({cargo, copy} <= processes_carrying(self.child_marker()))
        # Both folders carry the run's pid, and the copy folder sits in the hold folder, so
        # tmp_sweep.py can tie a folder a kill leaves behind to its run.
        holds = list((self.root / "tmp").glob(f"verify-hold.{process.pid}.*"))
        self.assertEqual(len(holds), 1, list((self.root / "tmp").iterdir()))
        self.assertEqual(len(list(holds[0].glob(f"verify-copy.{process.pid}.*"))), 1)
        self.stop_verify(process)
        self.assertEqual(processes_carrying(self.child_marker()), set())

    def test_copy_failure_first_stops_natedev_and_exits_with_the_copy_status(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "failed", "TEST_COPY_STATUS": "42"})
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 42, output + errors)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        self.assertEqual(self.saved_results(), [])
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 2.5))

    def test_copy_filtered_pass_first_stops_natedev_and_records_a_mac_pass(self) -> None:
        process, _ = self.start_copy_call("--filter", "alpha", copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_filter", 2.5))

    def test_copy_full_pass_first_stops_natedev_and_ends_the_call(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assertEqual((self.runner_state / "cargo-stopped").read_text(), str(signal.SIGTERM))
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 2.5))

    def test_natedev_finishing_first_stops_the_copy_and_its_result_stands(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_WAIT": "1"})
        _ = (self.runner_state / "cargo-release").write_text("release\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual((self.runner_state / "copy-signal").read_text(), str(signal.SIGTERM))
        self.assertFalse((self.runner_state / "cargo-stopped").exists())
        self.assertEqual(len(self.saved_results()), 1)
        self.assert_copy_record("natedev_won", "natedev")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"]), (0, "declined", "busy"))

    def test_new_test_claim_makes_the_copy_give_way_and_natedev_continues(self) -> None:
        mac_test_script = str(VERIFY.parent.parent / "mac_test" / "mac_test.py")
        # No second copy after the first gives way: the next check is an hour off.
        self.environment["BUILD_TARGETS_POLL_S"] = "3600"
        process, copy = self.start_copy_call(copy={"TEST_COPY_WAIT": "1", "TEST_COPY_CLAIM": mac_test_script})

        def claim(*words: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([sys.executable, mac_test_script, *words, "--pid", str(os.getpid())],
                                  env=self.environment, capture_output=True, text=True, check=False,
                                  timeout=HANG_GUARD_S)

        new_test = claim("claim", "--what", "nextest other", "--worktree", str(self.root))
        self.addCleanup(claim, "release")
        self.assertEqual(new_test.returncode, 0, new_test.stdout + new_test.stderr)
        self.assertIn("claimed", new_test.stdout)
        # The claim signals the copy; its handler records the signal some time later on a loaded machine.
        self.wait_for_path(self.runner_state / "copy-signal", (process,))
        self.assertEqual((self.runner_state / "copy-signal").read_text(), str(signal.SIGTERM))
        self.wait_for_reaped(copy, (process,))
        self.assertIsNone(process.poll())
        _ = (self.runner_state / "cargo-release").write_text("release\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertFalse((self.runner_state / "cargo-stopped").exists())
        self.assertEqual(len(self.saved_results()), 1)
        self.assert_copy_record("gave_way", "natedev")
        self.assertEqual(claim("release").returncode, 0)



@final
class VerifyMacSignalsTests(VerifyMacOffloadFixture):
    def test_other_verbs_never_call_runner(self) -> None:
        for arguments in (("check", "sample"), ("lint", "sample"), ("final",)):
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])

    def test_runner_receives_each_forwarded_signal_before_local_work(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_observations()
                process = self.start_verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_HOLD": "1"},
                )
                try:
                    self.wait_for_path(self.runner_state / "runner-signals-ready")
                    process.send_signal(signal_number)
                    output, errors = process.communicate(timeout=HANG_GUARD_S)
                except BaseException:
                    self.stop_verify(process)
                    raise
                self.assertEqual(
                    process.returncode, 128 + signal_number, output + errors
                )
                self.assertEqual(
                    (self.runner_state / "runner-signal").read_text(
                        encoding="utf-8"
                    ),
                    str(signal_number),
                )
                self.assertEqual(self.local_runs(), [])
                runners = self.runner_events()
                self.assertEqual(len(runners), 1)
                self.assertFalse(runners[0]["token_held"])

    def test_signal_before_runner_start_removes_result_without_local_work(self) -> None:
        marker = self.runner_state / "before-runner"
        gate = self.root / "debug-gate.sh"
        _ = gate.write_text(
            "set -T\n"
            + "before_mac_runner() {\n"
            + "    if [[ \"$BASH_COMMAND\" == *'\"$MAC_RUNNER\"'* "
            + "&& \"$BASH_COMMAND\" == *'\"${runner_args[@]}\"'* ]]; then\n"
            + "        trap - DEBUG\n"
            + "        : > \"$TEST_BEFORE_RUNNER_MARKER\"\n"
            + "        while [[ ! -e \"$TEST_CONTINUE_RUNNER\" ]]; do\n"
            + "            sleep 0.01\n"
            + "        done\n"
            + "    fi\n"
            + "}\n"
            + "trap before_mac_runner DEBUG\n",
            encoding="utf-8",
        )
        process = self.start_verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "BASH_ENV": str(gate),
                "TEST_BEFORE_RUNNER_MARKER": str(marker),
                "TEST_CONTINUE_RUNNER": str(self.runner_state / "continue-runner"),
            },
        )
        try:
            self.wait_for_path(marker)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=HANG_GUARD_S)
        except BaseException:
            self.stop_verify(process)
            raise

        self.assertEqual(
            process.returncode, 128 + signal.SIGTERM, output + errors
        )
        self.assertFalse((self.runner_state / "runner-started").exists())
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertFalse(Path(self.environment["TEST_TOKEN_RECORD"]).exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])



if __name__ == "__main__":
    _ = unittest.main()
