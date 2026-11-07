"""Route a queued lint to the Mac before local repairs start."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import override

from scripts.build_queue.admission_test_support import caller_admission
from scripts.direct.verify_hold_wait_test_support import VerifyHoldTools
from scripts.direct.verify_lint_scope_test_support import CARGO_STUB, SCRIPTS, TAPLO_STUB
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


class VerifyLintFreeMacTests(VerifyHoldTools):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.write_executable(self.bin_directory / "cargo", CARGO_STUB)
        self.write_executable(self.bin_directory / "taplo", TAPLO_STUB)
        config = self.root / "lint.conf"
        _ = write_config_fixture(config, "mend=on\nfmt=on\nclippy=on\n")
        self.environment.update({
            "LINT_CONFIG_READER": str(SCRIPTS / "lint/lint_config.sh"),
            "LINT_CONFIG_FILE": str(config),
            "LINT_SKIP_SWEEP": "1",
            "TEST_TAPLO_EVENTS": str(self.root / "taplo-events.jsonl"),
            "TEST_TAPLO_STATUS": "0",
            "TEST_RUNNER_MAC": "passed",
            "TEST_RUNNER_STATUS": "0",
        })

    def queued_lint(self) -> subprocess.Popen[str]:
        _ = self.hold_memory()
        process = self.start_verify("lint", "sample")
        self.addCleanup(self.stop_verify, process)
        return process

    def finish_lint(self, process: subprocess.Popen[str]) -> str:
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        return output + errors

    def release_memory(self) -> None:
        admission = caller_admission(self, self.root, self.environment)
        with admission.loop_turn():
            admission.free(64 * 1024**3)

    def local_mend(self) -> list[list[str]]:
        return [row["args"] for row in self.cargo_events()
                if row["args"][:1] == ["mend"] and "--version" not in row["args"]]

    def mac_verbs(self) -> list[str]:
        words = [row["args"] for row in self.runner_events()]
        for arguments in words:
            self.assertNotIn("--fix", arguments)
        return [arguments[arguments.index("--verb") + 1] for arguments in words]

    def test_clean_lint_leaves_memory_for_the_mac_without_local_repairs(self) -> None:
        process = self.queued_lint()
        output = self.finish_lint(process)
        self.assertIn("another machine is free after", output)
        self.assertEqual(self.mac_verbs(), ["mend-check", "clippy"])
        self.assertEqual(self.local_mend(), [])
        local = [row["args"] for row in self.cargo_events()]
        self.assertTrue(any("fmt" in arguments and "--all" in arguments for arguments in local), local)
        self.assertFalse(any(arguments[:1] == ["clippy"] for arguments in local), local)
        self.assertEqual(list(Path(self.environment["TMPDIR"]).glob("tmp.*")), [])

    def test_mac_finding_returns_to_memory_and_repairs_on_natedev(self) -> None:
        self.environment.update({"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "101"})
        process = self.queued_lint()
        self.wait_until(lambda: len(self.runner_events()) == 2, "both Mac judgments", (process,),
                        directory=self.runner_state)
        self.wait_for_memory_wait(process)
        self.assertEqual(self.local_mend(), [])
        self.set_mac_answer("passed", "", 0)
        self.release_memory()
        _ = self.finish_lint(process)
        self.assertEqual(self.mac_verbs(), ["mend-check", "clippy", "clippy"])
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])
        local = [row["args"] for row in self.cargo_events()]
        self.assertFalse(any(arguments[:1] == ["clippy"] for arguments in local), local)

    def test_busy_mac_keeps_the_lint_in_the_local_memory_queue(self) -> None:
        self.environment["TEST_RUNNER_BUSY_UNTIL"] = str(self.runner_state / "mac-free")
        process = self.queued_lint()
        self.wait_for_path(self.runner_state / "free-now-events", (process,))
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_mend(), [])
        self.release_memory()
        _ = self.finish_lint(process)
        self.assertEqual(self.mac_verbs(), ["clippy"])
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])

    def test_linux_only_workspace_never_leaves_the_local_memory_queue(self) -> None:
        settings = Path(self.environment["MAC_TEST_CONFIG"])
        _ = write_config_fixture(settings, f"offload=on\nlinux_only.{self.root.name}=sample\n", template="mac_test")
        memory_hook = self.root / "memory-hook"
        interpreter = self.root / "observe-memory-python"
        self.write_executable(interpreter, """#!/bin/bash
case "$1:$2" in
    */admission_client.py:admit|*/admission_client.py:launch)
        printf '%s\n' "$LINT_QUEUE_LEAVE_HOOK" > "$TEST_MEMORY_HOOK" ;;
esac
exec "$TEST_REAL_PYTHON" "$@"
""")
        self.environment.update({"CLAUDE_PYTHON": str(interpreter), "TEST_MEMORY_HOOK": str(memory_hook)})
        process = self.queued_lint()
        self.wait_for_memory_wait(process)
        self.assertNotIn("free-now", memory_hook.read_text())
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_mend(), [])
        self.release_memory()
        _ = self.finish_lint(process)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_mend(), [["mend", "--all-targets", "--workspace", "--fix"]])
        self.assertEqual(self.free_now_settings(), [])


if __name__ == "__main__":
    _ = unittest.main()
