"""Real verify.sh Mac signal boundaries with disposable tools."""
from __future__ import annotations

import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
import signal
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacSignalBoundariesTests(VerifyMacOffloadFixture):
    def test_other_verbs_never_call_runner(self) -> None:
        for arguments in (("check", "sample"), ("lint", "sample"), ("final",)):
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])

    def test_signal_before_runner_start_removes_result_without_local_work(self) -> None:
        self.check_signal_before_runner_start(delay_tee=False)

    def test_signal_before_delayed_tee_opens_leaves_no_temp_log(self) -> None:
        self.check_signal_before_runner_start(delay_tee=True)

    def check_signal_before_runner_start(self, *, delay_tee: bool) -> None:
        marker = self.runner_state / "before-runner"
        gate = self.root / "debug-gate.sh"
        tee_gate = '''tee() {
    "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_TEE_RELEASE"
    command tee "$@"
}
''' if delay_tee else ""
        _ = gate.write_text(
            "set -T\n"
            + tee_gate
            + "before_mac_runner() {\n"
            + "    if [[ \"$BASH_COMMAND\" == *'\"$MAC_RUNNER\"'* "
            + "&& \"$BASH_COMMAND\" == *'\"${runner_args[@]}\"'* ]]; then\n"
            + "        trap - DEBUG\n"
            + '        printf \'%s\\n\' "$MAC_LOG_FILE" > "$TEST_BEFORE_RUNNER_MARKER"\n'
            + '        "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_CONTINUE_RUNNER"\n'
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
                "TEST_TEE_RELEASE": str(self.runner_state / "release-tee"),
            },
        )
        try:
            self.wait_until(lambda: marker.exists() and bool(marker.read_text()),
                            "the runner boundary", (process,), directory=marker.parent)
            process.send_signal(signal.SIGTERM)
            # Bash handles a queued trap after its foreground command returns.
            # Release that command by event; the pending TERM must still stop
            # verify before the runner command itself can execute.
            (self.runner_state / "continue-runner").touch()
            if delay_tee:
                log = Path(marker.read_text().strip())
                self.wait_until(lambda: not log.exists(), "the Mac log removal", directory=log.parent)
                (self.runner_state / "release-tee").touch()
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except BaseException:
            (self.runner_state / "release-tee").touch()
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
