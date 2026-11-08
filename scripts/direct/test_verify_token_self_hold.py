"""A step releases its token, and a detached sweep takes no token."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


VERIFY = Path(__file__).with_name("verify.sh")
INVOKE = VERIFY.parent.parent / "lint/invoke.sh"


def functions(path: Path, names: tuple[str, ...]) -> str:
    source = path.read_text()
    selected: list[str] = []
    for name in names:
        marker = f"\n{name}() {{"
        if marker not in source and name in ("run_step", "finish_step_token"):
            continue
        start = source.index(marker) + 1
        end = source.index("\n}\n", start) + 2
        selected.append(source[start:end])
    return "\n".join(selected)


class VerifyTokenSelfHoldTests(unittest.TestCase):
    def execute(self, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="verify-token-step-") as temporary:
            root = Path(temporary)
            environment = {**os.environ, "TEST_ROOT": str(root), "TMPDIR": str(root)}
            script = r'''
set -uo pipefail
BOARD_DIR="$TEST_ROOT/board"; BOARD_SLOT=step
BOARD_HELPER="$TEST_BOARD"
TOKEN_USED=1; TOKEN_HELD=0; TOKEN_WAITED=0
TOKEN_WAIT_FILE="$TEST_ROOT/token-wait"
CMD=lint; LOCAL_ONLY=1; STEP_RUNS_ON=natedev_only; STEP_RAN_ON_MAC=0
LINT_LEFT_QUEUE_STATUS=75; SWEEP_EVERY_S=0
XDG_RUNTIME_DIR="$TEST_ROOT/runtime"
mkdir -p "$XDG_RUNTIME_DIR/lint-sweep"
step_state() { :; }
step_stopped_by_hold() { return 1; }
step_was_killed_for_memory() { return 1; }
buildlog_step_compiles() { [[ "$1" == cargo* ]]; }
route_compiling_step() {
    STEP_RAN_ON_MAC=0
    if [[ "${ROUTE_STATUS:-0}" != 0 ]]; then
        acquire_token local || return $?
        return "$ROUTE_STATUS"
    fi
}
acquire_token() {
    sync_token_state
    [[ "$TOKEN_HELD" == 0 ]] || return 0
    bash "$BOARD_HELPER" acquire "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$ --wait 0 >/dev/null || return $?
    token_acquired
}
run_once() {
    if buildlog_step_compiles "$@"; then acquire_token local || return $?; fi
    "$@"
}
'''
            script += functions(VERIFY, ("sync_token_state", "token_acquired", "release_token",
                                          "release_wait_claim", "step_writes_tree", "step_can_retry", "finish_step_token", "run", "run_step"))
            script += "\n" + functions(INVOKE, ("sweep_after_step",)) + "\n" + body
            return subprocess.run(
                ["bash", "-c", script], env={**environment, "TEST_BOARD": str(VERIFY.with_name("board.sh"))},
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )

    def test_detached_sweep_takes_no_token_and_the_next_local_step_starts(self) -> None:
        result = self.execute(r'''
mkfifo "$TEST_ROOT/sweep-finished"
invoke_sweep() {
    run true
    printf 'finished\n' > "$TEST_ROOT/sweep-finished"
}
run true || exit $?
# Release the first step explicitly to isolate the detached sweep.
release_token || exit $?
board_before=$(cat "$BOARD_DIR/board.log")
sweep_after_step true
read -r completed < "$TEST_ROOT/sweep-finished"
[[ "$completed" == finished ]] || exit 91
[[ ! -d "$BOARD_DIR/locks/cargo.d" ]] || { echo 'sweep holds the token' >&2; exit 92; }
[[ "$(cat "$BOARD_DIR/board.log")" == "$board_before" ]] || { echo 'sweep took the token' >&2; exit 96; }
run true || exit $?
echo next-step-started
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("next-step-started", result.stdout)

    def test_each_step_releases_only_the_token_it_acquires(self) -> None:
        for command, status, route in (("true", 0, 0), ("false", 1, 0), ("cargo", 0, 0),
                                       ("cargo", 42, 0), ("cargo_exit", 3, 0), ("cargo", 42, 42)):
            for held_before in (0, 1):
                with self.subTest(command=command, status=status, route=route, held_before=held_before):
                    result = self.execute(f'''
cargo() {{ return {status}; }}
cargo_exit() {{ exit {status}; }}
ROUTE_STATUS={route}
if [[ {held_before} == 1 ]]; then acquire_token local || exit $?; fi
step_status=0
run {command} || step_status=$?
[[ "$step_status" == {status} ]] || exit 93
if [[ {held_before} == 1 ]]; then
    [[ -d "$BOARD_DIR/locks/cargo.d" ]] || exit 94
    release_token || exit $?
else
    [[ ! -d "$BOARD_DIR/locks/cargo.d" ]] || {{ echo 'step holds the token after return' >&2; exit 95; }}
fi
''')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    _ = unittest.main()
