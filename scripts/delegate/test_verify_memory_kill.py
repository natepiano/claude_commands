#!/usr/bin/env python3
"""Exercise verify.sh's kill classifiers without starting its build runner."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


VERIFY = Path(__file__).with_name("verify.sh")


def classifier_functions() -> str:
    source = VERIFY.read_text()
    functions: list[str] = []
    for name in ("memory_kill_in_journal", "step_was_killed_for_memory", "lint_failure_is_the_tree", "run"):
        start = source.index(f"\n{name}() {{") + 1
        end = source.index("\n}\n", start) + 2
        functions.append(source[start:end])
    return "\n".join(functions)


class MemoryKillTests(unittest.TestCase):
    def check_classification(self, log: str, journal: str, status: int, expected_memory: bool, expected_tree: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log_file = root / "step.log"
            _ = log_file.write_text(log)
            journal_stub = root / "journalctl"
            _ = journal_stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$JOURNAL_FIXTURE\"\n")
            journal_stub.chmod(0o755)
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'CMD=lint; EXIT_STATUS="$2"; RUN_LOG="$1"; '
                'step_was_killed_for_memory "$RUN_LOG" "2026-10-04 12:00:00 -0400" "2026-10-04 12:01:00 -0400"; '
                'printf "memory=%s\\n" "$?"; '
                'lint_failure_is_the_tree; printf "tree=%s\\n" "$?"'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(log_file), str(status)],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "JOURNAL_FIXTURE": journal},
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
            journal_stub = root / "journalctl"
            _ = journal_stub.write_text("#!/bin/sh\necho 'earlyoom: sending SIGKILL to process 123 uid 991 \"rustc\"'\n")
            journal_stub.chmod(0o755)
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'run_once() { if [[ ! -e "$ATTEMPTS" ]]; then touch "$ATTEMPTS"; echo "signal: 9"; return 137; '
                'else echo "error: unused import"; return 1; fi; }; '
                'CMD=lint; RUN_LOG="$1"; run cargo test > "$RUN_LOG" 2>&1; EXIT_STATUS=$?; '
                'lint_failure_is_the_tree; printf "status=%s tree=%s\\n" "$EXIT_STATUS" "$?"'
            )
            result = subprocess.run(
                ["bash", "-c", script, "bash", str(root / "run.log")],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "TMPDIR": str(root), "ATTEMPTS": str(root / "attempts")},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status=1 tree=0", result.stdout)
            self.assertIn("signal: 9", (root / "run.log").read_text())
            self.assertIn("error: unused import", (root / "run.log").read_text())

    def test_memory_kill_retries_once_then_exits_137_on_second_kill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal_stub = root / "journalctl"
            _ = journal_stub.write_text("#!/bin/sh\necho 'Memory cgroup out of memory'\n")
            journal_stub.chmod(0o755)
            script = "set -o pipefail\n" + classifier_functions() + "\n" + (
                'run_once() { printf x >> "$ATTEMPTS"; echo "signal: 15"; return 143; }; '
                'run cargo test; code=$?; printf "status=%s attempts=%s\\n" "$code" "$(wc -c < "$ATTEMPTS")"'
            )
            result = subprocess.run(
                ["bash", "-c", script],
                env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "TMPDIR": str(root), "ATTEMPTS": str(root / "attempts")},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status=137 attempts=2", result.stdout)
            self.assertIn("killed for memory twice: cargo test", result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
