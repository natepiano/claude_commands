"""Mac-only verify routing through the existing offload fixture."""
from __future__ import annotations

import signal
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast, final, override

from scripts.direct.verify_mac_offload_test_support import SLEEP_STUB, VERIFY, VerifyMacOffloadFixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacOnlyTests(VerifyMacOffloadFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.environment.update({
            "ROSTER_RUN_ROOT": str(self.root / "roster-runs"),
            "UNIT_RUN_ROOT": str(self.root / "unit-runs"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
        })

    def parser_results(self, cases: list[tuple[str, ...]]) -> list[subprocess.CompletedProcess[str]]:
        repository = self.root / "parser-repository"
        repository.mkdir(exist_ok=True)
        git = shutil.which("git")
        assert git is not None
        _ = subprocess.run([git, "init", "-q", str(repository)], check=True)
        _ = (repository / ".gitignore").write_text("target/\n")
        output = self.root / "parser-results"
        output.mkdir(exist_ok=True)
        commands = ["set -u", shell_functions(VERIFY, "parse_routing_arguments")]
        for index, arguments in enumerate(cases):
            commands.append(
                f"( CMD={shlex.quote(arguments[0])}; VERIFY_CALL_ENVIRONMENT=(); "
                + f"parse_routing_arguments {shlex.join(arguments[1:])} ) "
                + f">{shlex.quote(str(output / f'{index}.out'))} "
                + f"2>{shlex.quote(str(output / f'{index}.err'))}; "
                + f"printf '%s' \"$?\" >{shlex.quote(str(output / f'{index}.status'))}"
            )
        environment = {**self.environment, "PATH": str(Path(git).parent) + os.pathsep + self.environment["PATH"]}
        _ = subprocess.run(["bash", "-c", "\n".join(commands)], cwd=repository, env=environment, check=True)
        return [
            subprocess.CompletedProcess(
                arguments, int((output / f"{index}.status").read_text()),
                (output / f"{index}.out").read_text(), (output / f"{index}.err").read_text(),
            )
            for index, arguments in enumerate(cases)
        ]

    def isolate_automatic_environment(self) -> dict[str, str]:
        selected_names = {
            "NO_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "FORCE_COLOR", "CI", "TZ", "LANG",
            "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC",
            "LC_TIME", "LC_ADDRESS", "LC_IDENTIFICATION", "LC_MEASUREMENT", "LC_NAME",
            "LC_PAPER", "LC_TELEPHONE",
            "RUST_LOG", "RUST_BACKTRACE", "RUST_LIB_BACKTRACE",
        }
        self.environment = {
            name: value for name, value in self.environment.items()
            if name not in selected_names
            and not name.startswith(("HANA_", "WGPU_", "BEVY_", "INSTA_", "TRACE_"))
        }
        return {
            "TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "verify",
            "COLUMNS": "80", "LINES": "24",
        }

    def mac_result(self, *, extra: dict[str, str], fetch: str = "") -> subprocess.CompletedProcess[str]:
        """Pass a runner result through the real result reader and call recorder."""
        functions = shell_functions(
            VERIFY, "verify_helper", "target_for_settings", "target_platform", "step_state", "release_wait_claim",
            "phase_wait_finish", "collect_routing_wait", "collect_hold_wait", "note_event",
            "try_mac_test", "check_mac_only_result", "mac_only_refuse", "try_reserved_target",
        )
        # These calls start after routing. Other tests keep the watcher and admission boundaries.
        script = functions + r'''
set -euo pipefail
CMD=test PKG=sample ARGS=(sample) MAC_ONLY=1 LOCAL_ONLY=0
MAC_ENV_NAMES=() VERIFY_MAC_ENVIRONMENT=() NEXTEST_ARGS=()
MAC_VERB=nextest MAC_SOURCE_ROOT="" MAC_IDLE_MARKER=""
MAC_FINISH_CALL=1 FILTER_RUN=0 CALL_NOTED=0 CACHE_DIR=""
STEP_RECORD_ACTIVE=0 STEP_RUNS_ON=mac_allowed
TREE_WATCH_PID=caller-owned ROUTING_WAIT_STARTED=""
BUILDLOG_MAC="" BUILDLOG_MAC_REASON="" BUILDLOG_MAC_S="" BUILDLOG_MAC_HOLDER=""
BUILDLOG_MAC_COPY="" BUILDLOG_MAC_COPY_S=""
BUILDLOG_MAC_COPY_COUNT="" BUILDLOG_MAC_COPY_NATEDEV_S=""
BUILDLOG_LOCAL_REASON=""
export BUILDLOG_CALL_ID="result-$$" BUILDLOG_LOCAL_REASON
STEP_TARGET_TABLE=$(printf 'mac\teligible\t-\t%s\tmacos' "$MAC_SETTINGS")
MAC_LEAVE_SETTINGS=$MAC_SETTINGS MAC_LEAVE_REASON=""
try_reserved_target
'''
        return subprocess.run(
            ["bash", "-c", script], cwd=self.root,
            env={**self.environment, "PY": sys.executable, "MAC_RUNNER": str(self.runner),
                 "MAC_SETTINGS": self.environment["MAC_TEST_CONFIG"],
                 "DOC_REPO_TOP": str(self.root), "HOLD_WAIT_DIR": str(self.root / "tmp"),
                 "BUILDLOG_RECORD": str(self.home / ".claude/scripts/buildlog/record.py"),
                 "MAC_FETCH": fetch, "MAC_FETCH_GIVEN": "1" if fetch else "0", **extra},
            text=True, capture_output=True, timeout=TEST_HANG_GUARD_SECS,
        )

    def test_readonly_exports_reach_runner_without_shell_assignment(self) -> None:
        automatic_environment = self.isolate_automatic_environment()
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
                self.assertEqual(observed, {**automatic_environment, name: value})
                self.assertEqual(len(self.runner_events()), 1)
                self.assertEqual(self.local_runs(), [])
                self.clear_observations()
                control = self.verify("test", "sample", "--mac", "why", "--no-cache", extra=passed)
                self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
                self.assertEqual(len(self.runner_events()), 1)
                self.assertEqual(json.loads((self.runner_state / "forwarded-environment.json").read_text()), automatic_environment)

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
        automatic_environment = self.isolate_automatic_environment()
        names = ("name", "PKG", "MAC_REASON", "ARGS")
        for name in names:
            with self.subTest(name=name):
                self.clear_observations()
                value = "original 'value'\nwith trailing newline\n"
                result = self.verify("test", "sample", "--mac", "why", "--env", name, extra={
                    name: value, "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                observed = cast(dict[str, str], json.loads((self.runner_state / "forwarded-environment.json").read_text()))
                self.assertEqual(observed, {**automatic_environment, name: value})
        self.clear_observations()
        previous_calls = self.call_records()
        results = self.parser_results([("test", "sample", "--mac", "why", "--env", name) for name in names])
        for name, refused in zip(names, results, strict=True):
            with self.subTest(unset=name):
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
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])
        self.assertGreaterEqual(len(self.runner_events()), 2)
        self.assertIn("Mac only (needs macOS)", errors)
        self.assertEqual(self.last_call()["mac"], "passed_full")
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)

    def test_policy_refusals_start_nothing(self) -> None:
        result = self.verify("test", "sample", "--env", "SHOTS")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr, 'verify.sh: --env and --fetch need --mac "<why>".\n')
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.cargo_events(), [])
        self.assertEqual(self.call_records(), [])
        cases = (
            (("final", "--saved", "--mac"), '--mac needs a reason: --mac "<why this command must run on macOS>".'),
            (("lint", "--mac", "  "), '--mac needs a reason: --mac "<why this command must run on macOS>".'),
            (("example", "--name", "sample", "--mac", "--filter"), '--mac needs a reason: --mac "<why this command must run on macOS>".'),
            (("test", "sample", "--test", "case", "--mac", "why", "--local", "requires Linux"), '--mac and --local cannot be used together.'),
            (("test", "sample", "--fetch", "target/shots"), '--env and --fetch need --mac "<why>".'),
            (("test", "sample", "--mac", "why", "--env"), '--env : not a variable name.'),
            (("test", "sample", "--mac", "why", "--env", "1SHOTS"), '--env 1SHOTS: not a variable name.'),
            (("test", "sample", "--mac", "why", "--env", "UNSET_SHOTS"), '--env UNSET_SHOTS: not set here; set it on the call (NAME=value verify.sh …).'),
            (("test", "sample", "--mac", "why", "--fetch"), '--fetch needs a folder relative to the repository root, such as target/shots.'),
            (("test", "sample", "--mac", "why", "--fetch", ""), '--fetch needs a folder relative to the repository root, such as target/shots.'),
            (("test", "sample", "--mac", "why", "--fetch", "target/a", "--fetch", "target/b"), '--fetch is given twice; name one folder.'),
            (("test", "sample", "--mac", "why", "--fetch", "src/shots"), '--fetch src/shots: git does not ignore it; name a folder the tests write, such as one under target/.'),
        )
        results = self.parser_results([case[0] for case in cases])
        for (arguments, message), result in zip(cases, results, strict=True):
            with self.subTest(arguments=arguments):
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, "verify.sh: " + message + "\n")
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.call_records(), [])

    def test_forwarding_rejects_reserved_names_and_nonplain_folders(self) -> None:
        names = (
            "PATH", "HOME", "MACOSX_DEPLOYMENT_TARGET", "RUSTFLAGS", "RUSTDOCFLAGS",
            "BUILDLOG_X", "LINT_X", "CARGO_X", "RUSTC", "RUSTC_WRAPPER", "NEXTEST_X",
            "DYLD_X", "MAC_TEST_X", "VERIFY_X",
        )
        folders = (
            "/tmp/shots", "-shots", ".", "..", "target/../shots", "target/./shots",
            "target//shots", "target/shots//", "target/shots x", "target/$shots",
        )
        results = self.parser_results(
            [("test", "sample", "--mac", "why", "--env", name) for name in names]
            + [("test", "sample", "--mac", "why", "--fetch", folder) for folder in folders]
        )
        for name, result in zip(names, results[:len(names)], strict=True):
            with self.subTest(name=name):
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stderr, f"verify.sh: --env {name}: the runner sets it, or it changes what Cargo builds; it is not forwarded.\n")
        for folder, result in zip(folders, results[len(names):], strict=True):
            with self.subTest(folder=folder):
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
                result = self.mac_result(extra={
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
        config = self.root / "build_targets.conf"
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
                result = self.mac_result(fetch="target/shots", extra={
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
        result = self.mac_result(fetch="target/shots", extra={
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
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
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
