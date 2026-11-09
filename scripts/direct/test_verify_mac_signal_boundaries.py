"""Real verify.sh Mac signal boundaries with disposable tools."""
from __future__ import annotations

import unittest
import json
from pathlib import Path
from typing import cast, final
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture
from scripts.direct.verify_metadata import CargoMetadata
import signal
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacSignalBoundariesTests(VerifyMacOffloadFixture):
    def test_each_verb_uses_its_first_step_route(self) -> None:
        for arguments, attempts in ((("check", "sample"), 1), (("lint", "sample"), 0), (("final",), 0)):
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.runner_events()), attempts)

    def test_signal_before_runner_start_removes_result_without_local_work(self) -> None:
        for signal_number in (signal.SIGTERM, signal.SIGINT):
            for before_pid in (False, True):
                with self.subTest(signal_number=signal_number, before_pid=before_pid):
                    self.clear_observations()
                    self.check_signal_before_runner_start(delay_tee=False, before_pid=before_pid,
                                                          signal_number=signal_number)

    def test_signal_before_delayed_tee_opens_leaves_no_temp_log(self) -> None:
        self.check_signal_before_runner_start(delay_tee=True)

    def test_check_stop_before_delayed_tee_opens_leaves_no_temp_log(self) -> None:
        self.check_signal_before_runner_start(delay_tee=True, command="check")

    def test_build_stop_before_delayed_tee_opens_leaves_no_temp_log(self) -> None:
        self.check_signal_before_runner_start(delay_tee=True, command="build")

    def check_signal_before_runner_start(self, *, delay_tee: bool, command: str = "test",
                                        before_pid: bool = False,
                                        signal_number: signal.Signals = signal.SIGTERM) -> None:
        if command == "build":
            source = self.root / "src/main.rs"
            _ = source.write_text("fn main() {}\n")
            metadata = cast(CargoMetadata, json.loads(self.environment["TEST_METADATA"]))
            metadata["target_directory"] = str(self.root / "target")
            metadata["packages"][0]["targets"].append({
                "name": "sample", "kind": ["bin"], "test": True, "src_path": str(source),
                "required-features": [],
            })
            self.environment["TEST_METADATA"] = json.dumps(metadata)
        marker = self.runner_state / "before-runner"
        pid_boundary = self.runner_state / "pid-boundary"
        for name in ("before-runner", "pid-boundary", "signal-forwarded", "continue-parent", "continue-runner", "release-tee"):
            (self.runner_state / name).unlink(missing_ok=True)
        gate = self.root / "debug-gate.sh"
        tee_gate = '''tee() {
    "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_TEE_RELEASE"
    command tee "$@"
}
''' if delay_tee else ""
        _ = gate.write_text(
            "set -T\n"
            + tee_gate
            + r'''kill() {
    local status=0
    builtin kill "$@" || status=$?
    if [[ "${1:-}" == -s ]]; then
        printf 'forwarded\n' > "$TEST_SIGNAL_FORWARDED"
    fi
    return "$status"
}
before_mac_runner() {
    if [[ "$BASHPID" == "$$" ]]; then
        if [[ "$TEST_SIGNAL_BEFORE_PID" == 1 && "$BASH_COMMAND" == 'MAC_RUNNER_PID=$!' ]]; then
            trap - DEBUG
            printf 'before:%s\n' "${MAC_RUNNER_PID:-unassigned}" > "$TEST_PID_BOUNDARY"
            "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_CONTINUE_PARENT"
        elif [[ "$TEST_SIGNAL_BEFORE_PID" == 0 && "$BASH_COMMAND" == 'wait "$MAC_RUNNER_PID"' ]]; then
            trap - DEBUG
            printf 'after:%s\n' "$MAC_RUNNER_PID" > "$TEST_PID_BOUNDARY"
        fi
    fi
    if [[ "$BASH_COMMAND" == *'"$MAC_RUNNER"'* && "$BASH_COMMAND" == *'"${runner_args[@]}"'* ]]; then
        trap - DEBUG
        printf '%s\n' "$MAC_LOG_FILE" > "$TEST_BEFORE_RUNNER_MARKER"
        "$TEST_REAL_PYTHON" "$TEST_WAIT_HELPER" "$TEST_CONTINUE_RUNNER"
    fi
}
trap before_mac_runner DEBUG
''',
            encoding="utf-8",
        )
        process = self.start_verify(
            command,
            "sample",
            "--target", "mac",
            "--no-cache",
            extra={
                "BASH_ENV": str(gate),
                "TEST_BEFORE_RUNNER_MARKER": str(marker),
                "TEST_CONTINUE_RUNNER": str(self.runner_state / "continue-runner"),
                "TEST_TEE_RELEASE": str(self.runner_state / "release-tee"),
                "TEST_SIGNAL_BEFORE_PID": "1" if before_pid else "0",
                "TEST_PID_BOUNDARY": str(pid_boundary),
                "TEST_CONTINUE_PARENT": str(self.runner_state / "continue-parent"),
                "TEST_SIGNAL_FORWARDED": str(self.runner_state / "signal-forwarded"),
            },
        )
        try:
            self.wait_until(lambda: marker.exists() and bool(marker.read_text()),
                            "the runner boundary", (process,), directory=marker.parent)
            self.wait_until(lambda: pid_boundary.exists() and bool(pid_boundary.read_text()),
                            "the PID assignment boundary", (process,), directory=pid_boundary.parent)
            if before_pid:
                self.assertEqual(pid_boundary.read_text().strip(), "before:unassigned")
            else:
                self.assertGreater(int(pid_boundary.read_text().strip().removeprefix("after:")), 0)
            process.send_signal(signal_number)
            if before_pid:
                (self.runner_state / "continue-parent").touch()
            if signal_number == signal.SIGINT:
                self.wait_for_path(self.runner_state / "signal-forwarded", (process,))
                (self.runner_state / "continue-runner").touch()
            # Log removal proves that the parent completed signal cleanup.
            log = Path(marker.read_text().strip())
            self.wait_until(lambda: not log.exists(), "the Mac log removal", directory=log.parent)
            (self.runner_state / "continue-runner").touch()
            if delay_tee:
                (self.runner_state / "release-tee").touch()
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except BaseException:
            (self.runner_state / "continue-parent").touch()
            (self.runner_state / "continue-runner").touch()
            (self.runner_state / "release-tee").touch()
            self.stop_verify(process)
            raise

        self.assertEqual(
            process.returncode, 128 + signal_number, output + errors
        )
        self.assertFalse((self.runner_state / "runner-started").exists())
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertFalse(Path(self.environment["TEST_TOKEN_RECORD"]).exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])


if __name__ == "__main__":
    _ = unittest.main()
