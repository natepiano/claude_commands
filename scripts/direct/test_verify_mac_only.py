"""Mac-only verify routing through the existing offload fixture."""
from __future__ import annotations

import signal
import json
import os
import shlex
import shutil
from pathlib import Path
import unittest
from typing import cast, final

from scripts.direct.verify_mac_offload_test_support import HANG_GUARD_S, SLEEP_STUB, VerifyMacOffloadFixture


@final
class VerifyMacOnlyTests(VerifyMacOffloadFixture):
    def test_readonly_exports_reach_runner_without_shell_assignment(self) -> None:
        for name, value in (
            ("SHELLOPTS", "braceexpand:errexit:hashall:interactive-comments:nounset:pipefail"),
            ("UID", str(os.getuid())),
        ):
            with self.subTest(name=name):
                self.clear_observations()
                passed = {name: value, "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"}
                result = self.verify("test", "sample", "--mac", "why", "--env", name, extra=passed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = cast(dict[str, str], json.loads((self.runner_state / "forwarded-environment.json").read_text()))
                self.assertEqual(observed, {name: value})
                self.assertEqual(len(self.runner_events()), 1)
                self.assertEqual(self.local_runs(), [])
                self.clear_observations()
                control = self.verify("test", "sample", "--mac", "why", "--no-cache", extra=passed)
                self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
                self.assertEqual(len(self.runner_events()), 1)
                self.assertEqual(json.loads((self.runner_state / "forwarded-environment.json").read_text()), {})

    def test_int_during_long_busy_poll_stops_every_child(self) -> None:
        real_sleep = shutil.which("sleep")
        assert real_sleep is not None
        self.write_executable(self.bin_directory / "sleep", '#!/bin/sh\n'
            + 'printf "sleeping\\n" > "$TEST_RUNNER_STATE/routing-sleep"\n'
            + f'exec {shlex.quote(real_sleep)} "$@"\n')
        process = self.start_verify("test", "sample", "--mac", "why", extra={
            "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_REASON": "busy",
            "BUILD_TARGETS_POLL_S": "3600"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "routing-sleep", (process,))
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, output + errors)
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(self.last_call()["status"], 130)
        self.assertEqual(self.local_runs(), [])
        self.assert_nothing_outlives()

    def test_busy_then_blocked_refuses_instead_of_polling(self) -> None:
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        process = self.start_verify("test", "sample", "--mac", "why", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "never-free"),
            "TEST_STEP_ROUTING_SLEEP": "1"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "routing-sleep", (process,))
        _ = (self.runner_state / "busy-reason").write_text("blocked\n")
        _ = (self.runner_state / "continue-sleep-1").write_text("continue\n")
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 2, output + errors)
        self.assertIn("verify.sh: test --mac refused: the Mac is blocked (/build:status names the holder and when it lifts); nothing ran on natedev.\n", errors)
        self.assertEqual(len(self.runner_events()), 2)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac_reason"], "blocked")

    def test_forwarded_environment_uses_callers_values_despite_shell_bindings(self) -> None:
        for name in ("name", "PKG", "MAC_REASON", "ARGS"):
            with self.subTest(name=name):
                self.clear_observations()
                value = "original 'value'\nwith trailing newline\n"
                result = self.verify("test", "sample", "--mac", "why", "--env", name, extra={
                    name: value, "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = cast(dict[str, str], json.loads((self.runner_state / "forwarded-environment.json").read_text()))
                self.assertEqual(observed, {name: value})
                self.clear_observations()
                previous_calls = self.call_records()
                refused = self.verify("test", "sample", "--mac", "why", "--env", name)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertEqual(refused.stderr, f"verify.sh: --env {name}: not set here; set it on the call (NAME=value verify.sh …).\n")
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.call_records(), previous_calls)

    def test_busy_then_pass_never_runs_on_natedev(self) -> None:
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        free = self.runner_state / "mac-free"
        process = self.start_verify("test", "sample", "--mac", "needs macOS", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(free), "TEST_RUNNER_MAC": "passed",
            "TEST_RUNNER_STATUS": "0", "TEST_FREE_NOW_FREE": "1",
            "TEST_PAUSE_ROUTING_SLEEP": "1",
        })
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "routing-sleep", (process,))
        _ = free.write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertGreaterEqual(len(self.runner_events()), 2)
        self.assertIn("Mac only (needs macOS)", errors)
        self.assertEqual(self.last_call()["mac"], "passed_full")
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)

    def test_policy_refusals_start_nothing(self) -> None:
        cases = (
            (("test", "sample", "--mac"), '--mac needs a reason: --mac "<why this test must run on macOS>".'),
            (("test", "sample", "--mac", "  "), '--mac needs a reason: --mac "<why this test must run on macOS>".'),
            (("test", "sample", "--mac", "--filter"), '--mac needs a reason: --mac "<why this test must run on macOS>".'),
            (("check", "sample", "--mac", "why"), '--mac applies only to test.'),
            (("test", "sample", "--mac", "why", "--local", "requires Linux"), '--mac and --local cannot be used together.'),
            (("test", "sample", "--env", "SHOTS"), '--env and --fetch need --mac "<why>".'),
            (("test", "sample", "--fetch", "target/shots"), '--env and --fetch need --mac "<why>".'),
            (("test", "sample", "--mac", "why", "--env"), '--env : not a variable name.'),
            (("test", "sample", "--mac", "why", "--env", "1SHOTS"), '--env 1SHOTS: not a variable name.'),
            (("test", "sample", "--mac", "why", "--env", "UNSET_SHOTS"), '--env UNSET_SHOTS: not set here; set it on the call (NAME=value verify.sh …).'),
            (("test", "sample", "--mac", "why", "--fetch"), '--fetch needs a folder relative to the repository root, such as target/shots.'),
            (("test", "sample", "--mac", "why", "--fetch", ""), '--fetch needs a folder relative to the repository root, such as target/shots.'),
            (("test", "sample", "--mac", "why", "--fetch", "target/a", "--fetch", "target/b"), '--fetch is given twice; name one folder.'),
            (("test", "sample", "--mac", "why", "--fetch", "src/shots"), '--fetch src/shots: git does not ignore it; name a folder the tests write, such as one under target/.'),
        )
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "verify.sh: " + message + "\n")
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.call_records(), [])

    def test_forwarding_rejects_reserved_names_and_nonplain_folders(self) -> None:
        for name in ("PATH", "HOME", "MACOSX_DEPLOYMENT_TARGET", "RUSTFLAGS", "RUSTDOCFLAGS",
                     "BUILDLOG_X", "LINT_X", "CARGO_X", "RUSTC", "RUSTC_WRAPPER", "NEXTEST_X",
                     "DYLD_X", "MAC_TEST_X", "VERIFY_X"):
            with self.subTest(name=name):
                result = self.verify("test", "sample", "--mac", "why", "--env", name)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, f"verify.sh: --env {name}: the runner sets it, or it changes what Cargo builds; it is not forwarded.\n")
        for folder in ("/tmp/shots", "-shots", ".", "..", "target/../shots", "target/./shots", "target//shots", "target/shots//", "target/shots x", "target/$shots"):
            with self.subTest(folder=folder):
                result = self.verify("test", "sample", "--mac", "why", "--fetch", folder)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, f"verify.sh: --fetch {folder.removesuffix('/')}: not a plain relative folder.\n")
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.cargo_events(), [])
        self.assertEqual(self.call_records(), [])

    def test_runner_refusals_never_fall_back_or_retry(self) -> None:
        reasons = {
            "blocked": "the Mac is blocked (/build:status names the holder and when it lifts)",
            "unreachable": "the Mac cannot be reached (/build:status --probe)",
            "backoff": "the Mac was unreachable a short time ago (retry backoff)",
            "disk": "the Mac is below its free-space floor", "off": "Mac offload is off",
            "repo": "this repository has no Mac setting", "linux_only": "sample is Linux only on the Mac",
            "state": "the Mac claim state cannot be read", "copy": "the tree copy to the Mac failed",
            "no_tests": "no test matched on the Mac", "lost": "the link to the Mac dropped during the run",
            "runner": "the Mac runner gave no usable result",
        }
        for reason, text in reasons.items():
            with self.subTest(reason=reason):
                self.clear_observations()
                mode = "lost" if reason == "lost" else "none" if reason == "runner" else "declined"
                result = self.verify("test", "sample", "--mac", "why", extra={
                    "TEST_RUNNER_MAC": mode, "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": "75"})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(f"verify.sh: test --mac refused: {text}; nothing ran on natedev.\n", result.stderr)
                self.assertEqual(len(self.runner_events()), 1)
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(self.routing_record()["target"], "")
                self.assertEqual(self.last_call()["outcome"], "failed")
                self.assertEqual(self.last_call()["status"], 2)
                self.assertEqual(self.last_call()["local_reason"], "")

    def test_eligibility_refuses_before_runner_and_records_decline(self) -> None:
        self.write_executable(self.bin_directory / "ssh", '#!/bin/sh\nexit 255\n')
        result = self.verify("test", "sample", "--mac", "needs macOS")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the Mac cannot be reached (/build:status --probe)", result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac"], "declined")
        self.assertEqual(self.last_call()["mac_reason"], "unreachable")
        self.assertEqual(self.last_call()["mac_s"], 0)
        self.assertEqual(self.routing_record()["target"], "")

    def test_mac_failure_keeps_runner_status(self) -> None:
        result = self.verify("test", "sample", "--mac", "why", extra={
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"})
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac"], "failed")
        self.assertEqual(self.last_call()["status"], 42)
        self.assertEqual(self.routing_record()["target"], "mac")

    def test_only_mac_pass_answers_a_mac_only_call(self) -> None:
        local = self.verify("test", "sample", "--local", "needs Linux")
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        self.clear_observations()
        remote = self.verify("test", "sample", "--mac", "needs macOS", extra={
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(remote.returncode, 0, remote.stdout + remote.stderr)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(self.local_runs(), [])
        self.clear_observations()
        reused = self.verify("test", "sample", "--mac", "another reason")
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertIn("PASS (recorded)", reused.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])

    def test_current_eligibility_refuses_even_with_a_cached_mac_pass(self) -> None:
        passed = self.verify("test", "sample", "--mac", "why", extra={
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.clear_observations()
        self.write_executable(self.bin_directory / "ssh", '#!/bin/sh\nexit 255\n')
        refused = self.verify("test", "sample", "--mac", "why")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("the Mac cannot be reached", refused.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac_reason"], "unreachable")

    def test_local_first_target_is_skipped_for_mac_only(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        result = self.verify("test", "sample", "--mac", "why", extra={
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(self.local_runs(), [])

    def test_forwarding_and_fetch_strip_flags_and_bypass_cache(self) -> None:
        passed = {"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0", "TEST_RUNNER_FETCH": "fetched", "SHOTS": "", "OTHER_SHOTS": "a secret\nvalue"}
        first = self.verify("test", "sample", extra=passed)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        cache = {path: path.read_bytes() for path in self.saved_results()}
        for arguments, forwarded in (
            (("--env", "SHOTS", "sample", "--mac", "why", "--env", "OTHER_SHOTS", "--env", "SHOTS"), ["--env", "SHOTS", "--env", "OTHER_SHOTS", "--env", "SHOTS"]),
            (("sample", "--fetch", "target/shots/", "--mac", "why"), ["--fetch", "target/shots"]),
        ):
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify("test", *arguments, extra=passed)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                event = self.runner_events()[0]
                index = event["args"].index(forwarded[0])
                self.assertEqual(event["args"][index:index + len(forwarded)], forwarded)
                self.assertNotIn("a secret\nvalue", event["args"])
                self.assertNotIn("--mac", event["args"])
                self.assertEqual(self.last_call()["command"], "test sample")
                self.assertEqual({path: path.read_bytes() for path in self.saved_results()}, cache)
                self.assertEqual(self.local_runs(), [])

    def test_missing_fetch_result_fails_pass_but_not_mac_failure(self) -> None:
        for word in ("missing", "too_large", "failed", "not_run", "absent"):
            with self.subTest(word=word):
                self.clear_observations()
                result = self.verify("test", "sample", "--mac", "why", "--fetch", "target/shots", extra={
                    "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
                    **({"TEST_RUNNER_FETCH": word} if word != "absent" else {})})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                expected = "failed" if word == "absent" else word
                self.assertIn(f"--fetch target/shots brought nothing back ({expected}).", result.stderr)
                self.assertEqual(self.last_call()["mac"], "passed_full")
                self.assertEqual(self.last_call()["outcome"], "failed")
                self.assertEqual(self.last_call()["status"], 2)
                self.assertEqual(self.local_runs(), [])
                self.assertEqual(self.saved_results(), [])
        result = self.verify("test", "sample", "--mac", "why", "--fetch", "target/shots", extra={
            "TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42", "TEST_RUNNER_FETCH": "missing"})
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)

    def test_term_during_busy_wait_stops_every_child(self) -> None:
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        process = self.start_verify("test", "sample", "--mac", "why", extra={
            "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_REASON": "busy",
            "TEST_PAUSE_ROUTING_SLEEP": "1"})
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "routing-sleep", (process,))
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=HANG_GUARD_S)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(self.last_call()["status"], 143)
        self.assertEqual(self.local_runs(), [])
        self.assert_nothing_outlives()

    def test_missing_runner_refuses_instead_of_running_locally(self) -> None:
        self.runner.unlink()
        result = self.verify("test", "sample", "--mac", "why")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("the Mac runner gave no usable result", result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["mac_reason"], "runner")


if __name__ == "__main__":
    _ = unittest.main()
