"""Real verify entry point cancellation with private Cargo and Git fixtures."""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TypedDict, cast

from scripts.direct.verify_tree_cancel_test_support import VerifyTreeCancelFixture
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.process_group import send_process_group


class CompletedLintRun(TypedDict):
    exit_code: int


class VerifyTreeCancelTests(VerifyTreeCancelFixture):
    def test_group_term_removes_lint_memory_wait_and_all_verify_processes(self) -> None:
        memory = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = memory.write_text("MemAvailable: 0 kB\n")
        process = self.start_verify("lint", supervised=True)
        wait_until(lambda: bool(self.admission.server.waiters) or process.poll() is not None,
                   self.admission.state_dir, processes=(process,))
        self.assertIsNone(process.poll(), "lint exited before admission registered its queue place")
        with self.admission.loop_turn():
            owners = [waiter.request.owner.pid for waiter in self.admission.server.waiters.values()]
        self.assertTrue(owners, "lint did not enter the admission queue")
        for pid in owners:
            group = os.getpgid(pid)
            self.started_groups.add(group)
            self.addCleanup(self.end_group, group)
        send_process_group(process.pid, signal.SIGTERM)
        try:
            output, errors = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("lint kept a verify process after its group received SIGTERM")
        self.assertEqual(process.returncode, 0, output + errors)
        record = cast(CompletedLintRun, json.loads((self.control / "lint-run.json").read_text()))
        log = (self.control / "lint-run.log").read_text()
        self.assertEqual(record["exit_code"], 143, log)
        self.wait_for_group_exit(process.pid, timeout=5)
        for group in self.started_groups:
            self.assertEqual(self.live_group_processes(group), [], "lint kept a verify process")
        with self.admission.loop_turn():
            self.assertEqual(self.admission.server.waiters, {}, "lint kept its admission queue place")
            self.assertEqual(self.admission.server.reservations, {}, "lint kept its admission reservation")
        self.assertEqual(list((self.board / "token_waiters").glob("*")), [])
        self.assertEqual(list((self.root / "verify-steps").glob("*.json")), [])
        self.assert_no_cache_verdict()

    def test_group_term_stops_lint_and_releases_admission(self) -> None:
        for stage in ("mend", "clippy"):
            with self.subTest(stage=stage):
                running = self.control / "running"
                running.unlink(missing_ok=True)
                process = self.start_verify("lint", TEST_BLOCK_STAGE=stage)
                self.wait_for_running(running, process)
                self.assert_resources_held()
                send_process_group(process.pid, signal.SIGTERM)
                try:
                    output, errors = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    self.fail(f"lint {stage} did not stop after the group received SIGTERM")
                self.assertEqual(process.returncode, 143, output + errors)
                self.wait_for_group_exit(process.pid, timeout=5)
                self.assert_resources_released()
                self.assert_no_cache_verdict()

    def test_second_term_during_cleanup_removes_the_step_record(self) -> None:
        python = self.control / "python"
        self.write_shell_tool(python,
            'if [ "${1##*/}" = step_record.py ] && [ "${2:-}" = state ] '
            + '&& [ "${5:-}" = waiting_hold ] && [ -e "$TEST_CONTROL/cleanup-armed" ]; then\n'
            + '    touch "$TEST_CONTROL/cleanup-started"\n'
            + '    ' + shlex.quote(sys.executable) + ' "$TEST_WAIT_HELPER" "$TEST_CONTROL/cleanup-release"\n'
            + 'fi\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        process = self.start_verify("lint", TEST_BLOCK_STAGE="mend", CLAUDE_PYTHON=str(python))
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        _ = (self.control / "cleanup-armed").touch()
        process.send_signal(signal.SIGTERM)
        self.wait_for_running(self.control / "cleanup-started", process)
        process.send_signal(signal.SIGTERM)
        _ = (self.control / "cleanup-release").touch()
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, output + errors)
        self.wait_for_group_exit(process.pid, timeout=5)
        self.assert_resources_released()
        self.assert_no_cache_verdict()

    def test_lint_rewrites_finish_without_cancelling(self) -> None:
        # The initial judgment must report a finding before fixes are allowed.
        cargo = self.root / "stubs" / "cargo"
        source = cargo.read_text().replace(
            'if stage == os.environ.get("TEST_BLOCK_STAGE", "test"):',
            '''if stage == "clippy" and not (root / "fix.rewritten").exists():
    print("error: unneeded return statement")
    print("  = note: `-D clippy::needless-return` implied by `-D warnings`")
    raise SystemExit(101)
if stage == os.environ.get("TEST_BLOCK_STAGE", "test"):''')
        _ = cargo.write_text(source)
        process = self.start_verify("lint", TEST_BLOCK_STAGE="clippy", TEST_FINISH="1",
                                    TEST_REWRITE_STAGES="mend,fix,fmt", TEST_FAIL_FIRST_JUDGMENT="1")
        self.release_lint_rewrites(process)
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        _ = (self.control / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertNotIn("cancelled", output + errors)
        for stage in ("mend", "fix", "fmt"):
            self.assertTrue((self.control / (stage + ".rewritten")).exists(), f"{stage} never rewrote an input")
        self.assertEqual(len(list((self.board / "verify_cache").glob("*.pass"))), 1,
                         "lint rewrites prevented its final verdict from being recorded")
        self.assert_resources_released()

    def test_edit_during_clippy_judgment_cancels_lint(self) -> None:
        process = self.start_verify("lint", TEST_BLOCK_STAGE="clippy")
        self.wait_for_running(self.control / "running", process)
        self.assert_resources_held()
        self.edit_source()
        self.assert_cancelled(process, "lint")
        judgments = [args for args in self.cargo_calls() if args[:1] == ["clippy"] and "--fix" not in args]
        self.assertEqual(len(judgments), 1, "cancelled call restarted clippy")


if __name__ == "__main__":
    _ = unittest.main()
