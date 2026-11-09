#!/usr/bin/env python3
"""Exercise verify.sh's kill classifiers without starting its build runner."""

from __future__ import annotations

from datetime import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast

from scripts.lib.shell_test_support import shell_function
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.memory_kill_test_support import (
    EARLYOOM_MESSAGE, KILL_AT, failed_compile_log, install_journal, journal_event,
)


VERIFY = Path(__file__).with_name("verify.sh")
# A Codex worker running this suite must not make verify.sh detach its call.
WORKER_FREE_ENVIRONMENT = {name: value for name, value in os.environ.items()
                           if name not in {"CODEX_THREAD_ID", "CODEX_SESSION_ID", "UNIT_DETACHED_RUN"}}

WORKER_FREE_ENVIRONMENT.update({"VERIFY_START_DIR": str(VERIFY.parent), "PY": sys.executable})


def classifier_functions() -> str:
    # Keep routing outside these classifier tests. Token tests cover admission.
    functions = [
        'route_compiling_step() { STEP_RAN_ON_MAC=0; STEP_RUNS_ON=natedev_only; }',
        'step_state() { :; }',
        'step_stopped_by_hold() { return 1; }',
        'collect_hold_wait() { :; }',
    ]
    for path, name in (
        (VERIFY.parent.parent / "lint" / "invoke.sh", "buildlog_step_compiles"),
        *((VERIFY, name) for name in (
            "memory_kill_in_journal", "step_was_killed_for_memory", "lint_failure_is_the_tree",
            "release_wait_claim", "step_writes_tree", "finish_step_token", "run", "run_step", "note_event")),
    ):
        functions.append(shell_function(path.read_text(), name))
    return "\n".join(functions)


class MemoryKillTests(unittest.TestCase):
    def recorded_call_after_kills(self, *, stopped: bool) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_journal(root, "Memory cgroup out of memory\n")
            attempts = root / "attempts"
            attempts.mkdir()
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'run_once() { local step=$1; if [[ "$MODE" == stopped || ! -e "$ATTEMPTS_DIR/$step" ]]; '
                'then touch "$ATTEMPTS_DIR/$step"; echo "signal: 15"; return 143; fi; return 0; }; '
                'CMD=test; ARGS=(hana); CALL_NOTED=0; CACHE_DIR=""; SECONDS=0; '
                'if run first > /dev/null; then code=0; else code=$?; fi; '
                'if [[ "$MODE" == recovered ]]; then run second > /dev/null; code=$?; fi; '
                'outcome=ran; [[ "$code" -eq 0 ]] || outcome=failed; '
                'note_event "$outcome" 0 0 "" 0 "$code"'
            )
            result = subprocess.run(
                ["bash", "-c", script], cwd=root,
                env={**WORKER_FREE_ENVIRONMENT, "PATH": f"{root}:{os.environ['PATH']}", "MODE": "stopped" if stopped else "recovered",
                     "ATTEMPTS_DIR": str(attempts), "MEM_KILL_FILE": str(root / "kills"),
                     "PY": sys.executable, "BUILDLOG_RECORD": str(VERIFY.parent.parent / "buildlog" / "record.py"),
                     "BUILDLOG_DIR": str(root / "buildlog")},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            files = list((root / "buildlog").glob("*/*.jsonl"))
            self.assertEqual(len(files), 1)
            return cast(dict[str, object], json.loads(files[0].read_text().splitlines()[0]))

    def test_twice_killed_step_records_stopped_exit_and_two_single_kills_record_recovery(self) -> None:
        stopped = self.recorded_call_after_kills(stopped=True)
        recovered = self.recorded_call_after_kills(stopped=False)
        self.assertEqual((stopped["status"], stopped["mem_kills"], stopped["mem_kill_stopped"]), (137, 2, True))
        self.assertEqual((recovered["status"], recovered["mem_kills"], recovered["mem_kill_stopped"]), (0, 2, False))

    def check_classification(self, log: str, journal: str, status: int, expected_memory: bool, expected_tree: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log_file = root / "step.log"
            _ = log_file.write_text(log)
            install_journal(root, journal)
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'CMD=lint; EXIT_STATUS="$2"; RUN_LOG="$1"; '
                'step_was_killed_for_memory "$RUN_LOG" "2026-10-04 12:00:00 -0400" "2026-10-04 12:01:00 -0400"; '
                'printf "memory=%s\\n" "$?"; '
                'lint_failure_is_the_tree; printf "tree=%s\\n" "$?"'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(log_file), str(status)],
                env={**WORKER_FREE_ENVIRONMENT, "PATH": f"{root}:{os.environ['PATH']}"},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                [f"memory={0 if expected_memory else 1}", f"tree={0 if expected_tree else 1}"],
            )

    def test_signal_fifteen_with_earlyoom_kill_is_memory_kill(self) -> None:
        journal = '\n'.join([
            'earlyoom[1946855]: sending SIGTERM to process 1639069 uid 991 "rustc": oom_score 1331, oom_score_adj 500, VmRSS 3287 MiB, cmdline "..."',
            'kill_release: pid=1639069: process_mrelease pidfd=4 success',
            'process 1639069 exited after 0.100 seconds',
        ])
        self.check_classification("process failed: signal: 15\n", journal, 137, True, False)

    def test_sigkill_earlyoom_message_is_memory_kill(self) -> None:
        self.check_classification(
            "process failed: signal: 9\n",
            'earlyoom[1946855]: sending SIGKILL to process 1639069 uid 991 "rustc": oom_score 1331',
            137, True, False,
        )

    def test_plain_sigterm_without_journal_kill_is_not_memory_kill(self) -> None:
        self.check_classification("process failed: SIGTERM\n", "", 1, False, True)

    def test_genuine_lint_failure_is_tree_failure(self) -> None:
        self.check_classification("error: unused import\n", "", 1, False, True)

    def test_interrupted_lint_is_not_tree_failure(self) -> None:
        for status in (130, 143):
            with self.subTest(status=status):
                self.check_classification("interrupted\n", "", status, False, False)

    def test_memory_kill_then_lint_error_is_tree_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_journal(root, 'earlyoom: sending SIGKILL to process 123 uid 991 "rustc"\n')
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'run_once() { if [[ ! -e "$ATTEMPTS" ]]; then touch "$ATTEMPTS"; echo "signal: 9"; return 137; '
                'else echo "error: unused import"; return 1; fi; }; '
                'CMD=lint; RUN_LOG="$1"; run cargo test > "$RUN_LOG" 2>&1; EXIT_STATUS=$?; '
                'lint_failure_is_the_tree; printf "status=%s tree=%s kills=%s\\n" "$EXIT_STATUS" "$?" "$(wc -c < "$MEM_KILL_FILE")"'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(root / "run.log")],
                env={**WORKER_FREE_ENVIRONMENT, "PATH": f"{root}:{os.environ['PATH']}", "TMPDIR": str(root), "ATTEMPTS": str(root / "attempts"), "MEM_KILL_FILE": str(root / "kills")},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status=1 tree=0 kills=1", result.stdout)
            self.assertIn("signal: 9", (root / "run.log").read_text())
            self.assertIn("error: unused import", (root / "run.log").read_text())

    def test_memory_kill_retries_once_then_exits_137_on_second_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_journal(root, "Memory cgroup out of memory\n")
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'run_once() { printf x >> "$ATTEMPTS"; echo "signal: 15"; return 143; }; '
                'run cargo test | cat; code=${PIPESTATUS[0]}; printf "status=%s attempts=%s kills=%s\\n" '
                '"$code" "$(wc -c < "$ATTEMPTS")" "$(wc -c < "$MEM_KILL_FILE")"'
            )
            result = subprocess.run(
                ["bash", "-c", script],
                env={**WORKER_FREE_ENVIRONMENT, "PATH": f"{root}:{os.environ['PATH']}", "TMPDIR": str(root), "ATTEMPTS": str(root / "attempts"), "MEM_KILL_FILE": str(root / "kills")},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status=137 attempts=2 kills=2", result.stdout)
            self.assertIn("killed for memory twice: cargo test", result.stderr)


    def retry_result(
        self, log: str, journal: str, second_status: int = 0, *, first_status: int = 101,
    ) -> tuple[int, int, int, str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_journal(root, journal)
            script = "set -o pipefail\n" + classifier_functions() + r'''
date() {
    if [[ ! -e "$ATTEMPTS" ]]; then
        printf '%s\n' "$TEST_STARTED"
    else
        printf '%s\n' "$TEST_ENDED"
    fi
}
run_once() {
    printf x >> "$ATTEMPTS"
    if [[ $(wc -c < "$ATTEMPTS") == 1 ]]; then
        printf '%s\n' "$TEST_STEP_LOG"
        return "$TEST_FIRST_STATUS"
    fi
    echo 'second attempt finished'
    return "$TEST_SECOND_STATUS"
}
CMD=test
run cargo nextest run
status=$?
kills=0
[[ ! -f "$MEM_KILL_FILE" ]] || kills=$(wc -c < "$MEM_KILL_FILE")
printf 'result=%s,%s,%s\n' "$status" "$(wc -c < "$ATTEMPTS")" "$kills"
'''
            result = subprocess.run(["bash", "-c", script], cwd=root,
                                    env={**WORKER_FREE_ENVIRONMENT, "PATH": f"{root}:{os.environ['PATH']}",
                                         "ATTEMPTS": str(root / "attempts"), "MEM_KILL_FILE": str(root / "kills"),
                                         "TEST_STEP_LOG": log, "TEST_FIRST_STATUS": str(first_status),
                                         "TEST_STARTED": datetime.fromtimestamp(KILL_AT - 1).astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                                         "TEST_ENDED": datetime.fromtimestamp(KILL_AT + 1).astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                                         "TEST_SECOND_STATUS": str(second_status), "TMPDIR": str(root)},
                                    capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = next(line.removeprefix("result=") for line in result.stdout.splitlines()
                          if line.startswith("result="))
            status, attempts, kills = map(int, record.split(","))
            self.assertEqual(list(root.glob("tmp*")), [], "attempt logs must be removed")
            return status, attempts, kills, result.stderr

    def test_sccache_exit_two_with_exact_earlyoom_message_retries_once(self) -> None:
        journal = journal_event() + journal_event("process 1202214 exited after 0.100 seconds", KILL_AT + 0.1)
        for first_status in (1, 2, 47, 101, 137):
            for second_status in (0, 47):
                with self.subTest(first_status=first_status, second_status=second_status):
                    status, attempts, kills, errors = self.retry_result(
                        failed_compile_log(), journal, second_status, first_status=first_status,
                    )
                    self.assertEqual((status, attempts, kills), (second_status, 2, 1))
                    self.assertEqual(errors.count("retrying once"), 1)

    def test_real_compiler_diagnostic_and_other_crate_kill_never_retry(self) -> None:
        log = "error[E0308]: mismatched types\n --> crates/hana/src/main.rs:1:1\n" + failed_compile_log()
        for crate in ("other_crate", "hana"):
            with self.subTest(killed_crate=crate):
                journal = journal_event(EARLYOOM_MESSAGE.replace("--crate-name hana", "--crate-name " + crate))
                self.assertEqual(self.retry_result(log, journal)[:3], (101, 1, 0))

    def test_unrelated_or_out_of_window_kill_never_retries(self) -> None:
        for journal in (journal_event(at=KILL_AT - 10), journal_event(at=KILL_AT + 10),
                        journal_event(EARLYOOM_MESSAGE.replace("--crate-name hana", "--crate-name other_crate")),
                        journal_event(EARLYOOM_MESSAGE.replace("--crate-name hana", "")),
                        json.dumps({"MESSAGE": EARLYOOM_MESSAGE}), "journal unavailable"):
            with self.subTest(journal=journal):
                self.assertEqual(self.retry_result(failed_compile_log(), journal)[:3], (101, 1, 0))

    def test_missing_failed_crate_and_real_plain_or_json_diagnostics_refuse_retry(self) -> None:
        for log in ("process failed: exit status: 2\n", "error: unused import\n" + failed_compile_log(),
                    '{"level":"error","message":"mismatched types"}\n' + failed_compile_log(),
                    '{"reason":"compiler-message","message":{"level":"error"}}\n' + failed_compile_log()):
            with self.subTest(log=log):
                self.assertEqual(self.retry_result(log, journal_event())[:3], (101, 1, 0))

    def test_sigkill_spelling_with_matching_crate_and_journal_still_retries(self) -> None:
        self.assertEqual(self.retry_result(failed_compile_log("process failed: SIGKILL"), journal_event())[:3],
                         (0, 2, 1))

    def test_nextest_test_binary_kill_retries_once(self) -> None:
        journal = 'sending SIGKILL to process 123 uid 1000 "hana-1a2b3c": oom_score 1064\n'
        self.assertEqual(self.retry_result("test hana-1a2b3c failed: signal: 9\n", journal)[:3], (0, 2, 1))

    def test_linker_kill_retries_once(self) -> None:
        journal = 'sending SIGKILL to process 123 uid 1000 "cc": oom_score 1064\n'
        self.assertEqual(self.retry_result("error: linking with `cc` failed: signal: 9\n", journal)[:3], (0, 2, 1))

    def test_build_script_kill_and_kernel_oom_retries_once(self) -> None:
        self.assertEqual(self.retry_result("build-script-build failed: SIGTERM\n",
                                          "Out of memory: Killed process 123 (build-script)\n")[:3], (0, 2, 1))


if __name__ == "__main__":
    _ = unittest.main()
