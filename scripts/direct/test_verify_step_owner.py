"""Step ownership excludes detached work and preserves shell exit codes."""

from __future__ import annotations

import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from typing import TypedDict, cast, override
import unittest

from scripts.direct.test_verify_mac_offload import stub_build_tool_path
from scripts.direct.wait_for_test_state import wait_until


VERIFY = Path(__file__).with_name("verify.sh")
INVOKE = VERIFY.parent.parent / "lint" / "invoke.sh"
BOARD = VERIFY.with_name("board.sh")
WAIT = VERIFY.with_name("wait_for_test_state.py")


class StepOwnership(TypedDict):
    group: int
    start: str


class StepProcess(TypedDict):
    pid: int
    group: int


def shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}", source)
    if match is None:
        raise AssertionError(f"missing shell function: {name}")
    return match.group(0)


class VerifyStepOwnerTests(unittest.TestCase):
    root: Path = Path()
    environment: dict[str, str] = {}
    shell: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="verify-owner-test-")))
        for name in ("home", "tmp", "runtime", "state", "buildlog", "stubs"):
            (self.root / name).mkdir()
        self.environment = {
            "PATH": stub_build_tool_path(self.root / "stubs"),
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_PEAK": "",
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "BOARD_DIR": str(self.root / "board"),
            "BOARD_HELPER": str(BOARD),
            "TOKEN_HELD": "1",
            "TEST_ROOT": str(self.root),
            "TEST_PYTHON": sys.executable,
            "TEST_WAIT": str(WAIT),
            "TEST_COMMAND": str(self.root / "step.py"),
        }
        verify = VERIFY.read_text()
        start = verify.index("STEP_OWNER_PY='\n")
        end = verify.index("\nrelease_token() {", start)
        invoke = INVOKE.read_text()
        self.shell = "\n".join((
            "set -euo pipefail",
            shell_function(invoke, "buildlog_exec"),
            verify[start:end],
            shell_function(invoke, "sweep_after_step"),
            'mkdir -p "$BOARD_DIR/locks/cargo.d"',
            'printf "%s" "$$" > "$BOARD_DIR/locks/cargo.d/holder_pid"',
            'printf fixture > "$BOARD_DIR/locks/cargo.d/holder"',
        ))
        _ = (self.root / "step.py").write_text('''import json
import os
from pathlib import Path
import signal
import sys
sys.path.insert(0, sys.argv[1])
from scripts.direct.wait_for_test_state import wait_for_file

signal.signal(signal.SIGTERM, signal.SIG_DFL)
root = Path(os.environ["TEST_ROOT"])
name = sys.argv[2]
ready = root / (name + ".pid")
temporary = ready.with_suffix(".tmp")
temporary.write_text(json.dumps({"pid": os.getpid(), "group": os.getpgrp()}))
temporary.replace(ready)
wait_for_file(root / (name + ".release"))
(root / (name + ".finished")).touch()
''')
        self.environment["TEST_REPO"] = str(VERIFY.parent.parent.parent)

    def wrapper(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", self.shell + '\nbuildlog_exec "$@"', "step-owner-test", *command],
            cwd=self.root, env=self.environment, capture_output=True, text=True,
            check=False, timeout=300, start_new_session=True)

    def background_sweep(self) -> subprocess.Popen[str]:
        script = self.shell + '''
buildlog_exec "$TEST_PYTHON" "$TEST_COMMAND" "$TEST_REPO" real &
real_step=$!
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/real.pid"
invoke_sweep() {
    local status=0
    buildlog_exec "$TEST_PYTHON" "$TEST_COMMAND" "$TEST_REPO" sweep || status=$?
    printf '%s' "$status" > "$TEST_ROOT/sweep.status.tmp"
    mv "$TEST_ROOT/sweep.status.tmp" "$TEST_ROOT/sweep.status"
}
SWEEP_EVERY_S=300
sweep_after_step fixture-step
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/sweep.pid"
: > "$TEST_ROOT/both-started"
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/real.release"
wait "$real_step"
: > "$TEST_ROOT/step-done"
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/cleanup.release"
bash "$BOARD_HELPER" release "$BOARD_DIR" fixture cargo --pid "$$"
: > "$TEST_ROOT/cleanup-done"
"$TEST_PYTHON" "$TEST_WAIT" "$TEST_ROOT/sweep.status"
'''
        process = subprocess.Popen(
            ["bash", "-c", script], cwd=self.root, env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(self.finish_background_sweep, process)
        return process

    def finish_background_sweep(self, process: subprocess.Popen[str]) -> None:
        for name in ("real.release", "cleanup.release", "sweep.release"):
            (self.root / name).touch()
        try:
            output, errors = process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.communicate(timeout=300)
            raise
        self.assertEqual(process.returncode, 0, output + errors)

    def wait_for(self, name: str, process: subprocess.Popen[str]) -> None:
        path = self.root / name
        wait_until(lambda: path.exists() or process.poll() is not None,
                   self.root, processes=(process,))
        self.assertTrue(path.exists(), f"fixture exited before {name}")

    def step_process(self, name: str) -> StepProcess:
        return cast(StepProcess, json.loads((self.root / (name + ".pid")).read_text()))

    def test_background_sweep_leaves_the_real_step_as_the_owner(self) -> None:
        process = self.background_sweep()
        self.wait_for("both-started", process)
        record = cast(StepOwnership, json.loads(
            (self.root / "board" / "locks" / "cargo.d" / "owned").read_text()))
        self.assertEqual(record["group"], self.step_process("real")["group"])
        self.assertNotEqual(record["group"], self.step_process("sweep")["group"])
        self.assertFalse((self.root / "real.finished").exists())
        self.assertFalse((self.root / "sweep.finished").exists())

    def test_releasing_after_a_step_leaves_its_background_sweep_running(self) -> None:
        process = self.background_sweep()
        self.wait_for("both-started", process)
        (self.root / "real.release").touch()
        self.wait_for("step-done", process)
        self.assertFalse((self.root / "sweep.finished").exists())
        (self.root / "cleanup.release").touch()
        self.wait_for("cleanup-done", process)
        self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())
        (self.root / "sweep.release").touch()
        self.wait_for("sweep.status", process)
        self.assertEqual((self.root / "sweep.status").read_text(), "0")
        self.assertTrue((self.root / "sweep.finished").exists())

    def test_signal_deaths_of_the_command_and_owner_use_shell_exit_codes(self) -> None:
        for victim in ("os.getpid()", "os.getppid()"):
            for signum, expected in ((signal.SIGKILL, 137), (signal.SIGTERM, 143)):
                with self.subTest(victim=victim, signal=signum):
                    source = ("import os, signal; signal.signal(signal.SIGTERM, signal.SIG_DFL); "
                              f"os.kill({victim}, {int(signum)})")
                    result = self.wrapper(sys.executable, "-c", source)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_normal_step_exit_codes_pass_through(self) -> None:
        for status in (0, 23, 125, 255):
            with self.subTest(status=status):
                result = self.wrapper(sys.executable, "-c", f"raise SystemExit({status})")
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
