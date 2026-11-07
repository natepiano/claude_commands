"""Check lease renewal, admission retry, and loaded lint input identity."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event
import time
import unittest
from unittest import mock

from scripts.direct import verify_lint_scope_test_support as lint_support
from scripts.direct.verify_lint_cache_test_support import VerifyLintCacheFixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, BOARD, VERIFY
from scripts.direct.wait_for_test_state import wait_until

SCRIPTS = VERIFY.parent.parent


def token_renewal_functions() -> str:
    return shell_functions(VERIFY, "start_token_renewer", "stop_token_renewer", "with_token_renewal")


def token_admission_functions() -> str:
    return shell_functions(VERIFY, "record_token_wait", "buildlog_wait_for_memory")



class LongStepOwnershipTests(VerifyTokenWaitFixture):
    def board(self, board: Path, *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(BOARD), words[0], str(board), *words[1:]],
                              env=self.environment, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)

    def test_running_step_renews_past_its_original_hold_and_stops_on_release(self) -> None:
        _ = write_config_fixture(self.root / "direct.conf", "VERIFY_TOKEN_HOLD_SECONDS=2\nVERIFY_TOKEN_RENEW_SECONDS=1")
        board = self.root / "board"
        process = self.start_owned_step({**self.environment, "UNIT_RUN_DIR": str(board),
                                    "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                                    })
        lock = board / "locks" / "cargo.d"
        try:
            self.wait_for_running(self.root / "running", process)
            original_expiry = int((lock / "expires").read_text())
            # Observe renewal after the original lease expires.
            wait_until(lambda: int((lock / "expires").read_text() or "0") > original_expiry + 2,
                       lock, processes=(process,))
            rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
            self.assertNotEqual(rival.returncode, 0, rival.stdout + rival.stderr)
            self.assertEqual((lock / "holder").read_text().strip(), "first")
        finally:
            (self.root / "release").touch()
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertFalse(lock.exists())
        # There must be no heartbeat left in the call's session after release.
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_expired_matching_live_step_keeps_ownership_but_dead_holder_is_freed(self) -> None:
        board = self.root / "board"
        process = self.start_owned_step({**self.environment, "UNIT_RUN_DIR": str(board),
                                    "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                                    })
        lock = board / "locks" / "cargo.d"
        try:
            self.wait_for_running(self.root / "running", process)
            _ = (lock / "expires").write_text("0")
            rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
            self.assertNotEqual(rival.returncode, 0, rival.stdout + rival.stderr)
            self.assertIsNone(process.poll())
            self.end_group(process.pid)
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
            self.assertEqual(rival.returncode, 0, rival.stdout + rival.stderr)
        finally:
            (self.root / "release").touch()
            self.end_group(process.pid)
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            _ = self.board(board, "release", "second", "cargo", "--pid", str(os.getpid()))

    def test_every_step_renews_before_launching_its_command(self) -> None:
        harness = self.root / "two-steps.sh"
        _ = harness.write_text("set -eu\n" + shell_functions(VERIFY, "renew_token")
                               + token_renewal_functions()
                               + shell_functions(VERIFY, "buildlog_exec") + r'''
BOARD_HELPER=$1 BOARD_DIR=$2 BOARD_SLOT=holder STEP_RECORD_SCRIPT="$TEST_STEP_SCRIPT"
source "${STEP_RECORD_SCRIPT%/*}/../lib/conf.sh"
TOKEN_HOLD_S="$(conf_value direct VERIFY_TOKEN_HOLD_SECONDS)"
TOKEN_RENEW_S="$(conf_value direct VERIFY_TOKEN_RENEW_SECONDS)"
TOKEN_HELD=1 PY=$3 STEP_OWNER_PY=""
HOLD_WAIT_DIR="$TEST_HOLD_DIR"
bash "$BOARD_HELPER" acquire "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$ --hold 30
buildlog_exec_unowned() {
    test "$(cat "$BOARD_DIR/locks/cargo.d/expires")" -gt 0 || return 99
    printf 'command started after renewal\n'
}
for step in first second; do
    printf 0 > "$BOARD_DIR/locks/cargo.d/expires"
    buildlog_exec true
done
nested_step() {
    test "$outer" = "$TOKEN_RENEWER_PID"
    kill -0 "$outer"
    printf 'nested renewal uses the same process\n'
}
nested_span() {
    local outer=$TOKEN_RENEWER_PID
    with_token_renewal nested_step
    kill -0 "$outer"
}
with_token_renewal nested_span
test -z "${TOKEN_RENEWER_PID:-}"
bash "$BOARD_HELPER" release "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$
''')
        result = subprocess.run(["bash", str(harness), str(BOARD), str(self.root / "board"), sys.executable],
                                env={**self.environment, "TEST_STEP_SCRIPT": str(VERIFY.with_name("step_record.py")), "TEST_HOLD_DIR": str(self.root)}, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("command started after renewal"), 2)
        self.assertIn("nested renewal uses the same process", result.stdout)

    def memory_wait_process(self, *, lose_at_launch: bool = False, scoped_attempt: bool = False,
                            board_helper: Path = BOARD, noncompiling: bool = False) -> subprocess.Popen[str]:
        # Use the real board and ownership guard with controlled admission replies.
        _ = write_config_fixture(self.root / "direct.conf", "VERIFY_TOKEN_HOLD_SECONDS=2\nVERIFY_TOKEN_RENEW_SECONDS=1")
        source = VERIFY.read_text()
        owner_start = source.index("STEP_OWNER_PY='\n")
        owner_end = source.index("\nrelease_token() {", owner_start)
        harness = self.root / "held-memory.sh"
        _ = harness.write_text('set -eu\nsource "$4"\n'
                               + source[owner_start:owner_end]
                               + shell_functions(VERIFY, "release_token")
                               + token_admission_functions() + r'''
BOARD_HELPER=$1 BOARD_DIR=$2 BOARD_SLOT=first
source "${STEP_RECORD_SCRIPT%/*}/../lib/conf.sh"
TOKEN_HOLD_S="$(conf_value direct VERIFY_TOKEN_HOLD_SECONDS)"
TOKEN_RENEW_S="$(conf_value direct VERIFY_TOKEN_RENEW_SECONDS)"
TOKEN_USED=1 TOKEN_HELD=0 TOKEN_WAIT_FILE="$TEST_ROOT/token-waits" TOKEN_WAITED=0 PY=$3
BUILDLOG_MEM_WAIT_S=0 BUILDLOG_MEM_WAIT_FROM=0 BUILDLOG_MEM_OUTCOME=Granted passes=0
HOLD_WAIT_DIR="$TEST_ROOT/hold"
mkdir -p "$HOLD_WAIT_DIR"
eval "$(declare -f buildlog_exec_unowned | sed '1s/buildlog_exec_unowned/buildlog_exec_actual/')"
buildlog_exec_unowned() {
    if [[ "$TEST_LOSE_AT_LAUNCH" == 1 && ! -f "$TEST_ROOT/lost-at-launch" ]]; then
        : > "$TEST_ROOT/lost-at-launch"
        bash "$BOARD_HELPER" release "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$
    fi
    buildlog_exec_actual "$@"
}
step_state() { :; }
sync_token_state() { :; }
buildlog_token_wait() { buildlog_release_memory 0; }
token_acquired() { TOKEN_HELD=1; }
build_hold_wait_local() { :; }
build_hold_check_local() { :; }
buildlog_begin() {
    BUILDLOG_START="" BUILDLOG_PEAK="" BUILDLOG_MEM_MAX_PATH="" BUILDLOG_MEM_SAMPLING_CONTEXT=""
    if [[ "$TEST_SCOPED_ATTEMPT" == 1 ]]; then
        BUILDLOG_PEAK="$TEST_ROOT/attempt.$RANDOM.peak"
    fi
}
buildlog_end() {
    printf '%s\n' "$1" >> "$TEST_ROOT/recorded-statuses"
    [[ -z "$BUILDLOG_PEAK" ]] || rm -f "$BUILDLOG_PEAK"
}
# Run the real scope shell without requiring a user systemd manager.
systemd-run() {
    while [[ "$1" != -- ]]; do shift; done
    shift
    printf '%s\n' "$BUILDLOG_PEAK" >> "$TEST_ROOT/scoped-attempts"
    "$@"
}
sweep_after_step() { :; }
buildlog_release_memory() {
    rm -f "$TEST_ROOT/reservation"
    BUILDLOG_MEM_RESERVATION=""
}
# Use the real board for token ownership.
acquire_token() {
    TOKEN_WAITED=0
    [[ "$TOKEN_HELD" == 0 ]] || return 0
    : > "$TEST_ROOT/token-wait"
    bash "$BOARD_HELPER" acquire "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$ --hold "$TOKEN_HOLD_S" --wait 30
    TOKEN_HELD=1
    if [[ -f "$TEST_ROOT/acquisitions" ]]; then TOKEN_WAITED=7; fi
    printf 'acquired\n' >> "$TEST_ROOT/acquisitions"
    rm -f "$TEST_ROOT/token-wait"
}
buildlog_wait_for_memory_only() {
    passes=$(( passes + 1 ))
    if (( passes == 1 )); then
        cp "$BOARD_DIR/locks/cargo.d/expires" "$TEST_ROOT/original-expiry"
        : > "$TEST_ROOT/memory-wait"
        "$PY" "$TEST_WAIT_HELPER" "$TEST_RELEASE"
    fi
    BUILDLOG_MEM_RESERVATION="$TEST_ROOT/reservation"
    : > "$BUILDLOG_MEM_RESERVATION"
    printf '%s\n' "$TOKEN_HELD" >> "$TEST_ROOT/admission-holders"
}
trap 'stop_token_renewer; release_token; buildlog_release_memory; rm -rf "$HOLD_WAIT_DIR"' EXIT
if [[ "$TEST_NONCOMPILING" == 1 ]]; then
    buildlog_step_compiles() { return 1; }
    acquire_token
fi
run_once cargo check
release_token
''')
        # The fake cargo command uses the real launcher and ownership guard.
        environment = {**self.environment, "TEST_ROOT": str(self.root), "STEP_RECORD_SCRIPT": str(VERIFY.with_name("step_record.py")),
                       "TEST_LOSE_AT_LAUNCH": "1" if lose_at_launch else "0",
                       "TEST_SCOPED_ATTEMPT": "1" if scoped_attempt else "0",
                       "TEST_REAL_BOARD": str(BOARD), "TEST_NONCOMPILING": "1" if noncompiling else "0",
                       "TEST_BLOCK": "1", "TEST_RUNNING": str(self.root / "command-ran")}
        process = subprocess.Popen(["bash", str(harness), str(board_helper), str(self.root / "board"), sys.executable,
                                    str(SCRIPTS / "lint" / "invoke.sh")],
                                   cwd=self.root, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.started_groups.add(process.pid)
        self.addCleanup(self.finish_memory_wait, process)
        return process

    def finish_memory_wait(self, process: subprocess.Popen[str]) -> None:
        (self.root / "release").touch()
        if process.poll() is None:
            self.end_group(process.pid)
        _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_memory_wait_renews_the_held_token_until_the_step_runs(self) -> None:
        process = self.memory_wait_process()
        lock = self.root / "board" / "locks" / "cargo.d"
        self.wait_for_running(self.root / "memory-wait", process)
        original_expiry = int((self.root / "original-expiry").read_text())
        wait_until(lambda: int((lock / "expires").read_text() or "0") > original_expiry,
                   lock, processes=(process,))
        # Try another acquisition after the initial lease expires, during admission.
        _ = Event().wait(max(0, original_expiry + 1 - time.time()))
        rival = self.board(self.root / "board", "acquire", "second", "cargo", "--pid", str(os.getpid()))
        try:
            self.assertNotEqual(rival.returncode, 0, rival.stdout + rival.stderr)
            self.assertGreater(int((lock / "expires").read_text()), original_expiry)
            self.assertFalse((self.root / "command-ran").exists())
        finally:
            if rival.returncode == 0:
                _ = self.board(self.root / "board", "release", "second", "cargo", "--pid", str(os.getpid()))
            (self.root / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertTrue((self.root / "command-ran").exists())
        self.assertFalse(lock.exists())
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_busy_board_guard_does_not_stop_memory_wait_renewal(self) -> None:
        helper = self.root / "traced-board.sh"
        _ = helper.write_text('exec 8>>"$TEST_ROOT/board.trace"\n'
                              + 'export BASH_XTRACEFD=8\n'
                              + 'exec bash -x "$TEST_REAL_BOARD" "$@"\n')
        process = self.memory_wait_process(board_helper=helper)
        self.wait_for_running(self.root / "memory-wait", process)
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        original_expiry = int((self.root / "original-expiry").read_text())
        trace = self.root / "board.trace"
        config = self.root / "direct.conf"
        original_config = config.read_text()
        _ = write_config_fixture(config, "TOKEN_GUARD_ATTEMPTS=2\nTOKEN_GUARD_DELAY_MS=1")
        try:
            with (board / "locks" / "cargo.guard").open("a") as guard:
                fcntl.flock(guard, fcntl.LOCK_EX)
                # The trace closes after both failed retries return status 2.
                wait_until(lambda: "+ status=75" in trace.read_text(), trace, processes=(process,))
        finally:
            _ = config.write_text(original_config)
        # Observe renewed expiry before another sender tries to acquire the token.
        wait_until(lambda: int((lock / "expires").read_text() or "0") > original_expiry,
                   lock, processes=(process,))
        # Try another acquisition after the initial lease expires.
        _ = Event().wait(max(0, original_expiry + 1 - time.time()))
        rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
        try:
            self.assertNotEqual(rival.returncode, 0, rival.stdout + rival.stderr)
            self.assertGreater(int((lock / "expires").read_text()), original_expiry)
            self.assertFalse((self.root / "command-ran").exists())
        finally:
            if rival.returncode == 0:
                _ = self.board(board, "release", "second", "cargo", "--pid", str(os.getpid()))
            (self.root / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertTrue((self.root / "command-ran").exists())
        self.assertFalse(lock.exists())
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_token_lost_during_memory_wait_is_reacquired_before_the_step(self) -> None:
        process = self.memory_wait_process()
        self.wait_for_running(self.root / "memory-wait", process)
        # Release ownership during admission. Keep TOKEN_HELD stale until the launch check.
        released = self.board(self.root / "board", "release", "first", "cargo", "--pid", str(process.pid))
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        (self.root / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertTrue((self.root / "command-ran").exists())
        self.assertEqual((self.root / "acquisitions").read_text().splitlines(), ["acquired", "acquired"])
        self.assertEqual((self.root / "token-waits").read_text().splitlines(), ["7"])
        self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_lost_token_releases_memory_before_waiting_to_reacquire(self) -> None:
        process = self.memory_wait_process()
        board = self.root / "board"
        self.wait_for_running(self.root / "memory-wait", process)
        released = self.board(board, "release", "first", "cargo", "--pid", str(process.pid))
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
        self.assertEqual(rival.returncode, 0, rival.stdout + rival.stderr)
        try:
            (self.root / "release").touch()
            self.wait_for_running(self.root / "token-wait", process)
            self.assertFalse((self.root / "reservation").exists(),
                             "token queue kept memory needed by its current holder")
            self.assertFalse((self.root / "command-ran").exists())
        finally:
            _ = self.board(board, "release", "second", "cargo", "--pid", str(os.getpid()))
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertTrue((self.root / "command-ran").exists())
        self.assertEqual((self.root / "admission-holders").read_text().splitlines(), ["1", "1"])
        self.assertEqual((self.root / "recorded-statuses").read_text().splitlines(), ["0"])
        self.assertEqual((self.root / "token-waits").read_text().splitlines(), ["7"])
        self.assertFalse((self.root / "reservation").exists())
        self.assertFalse((self.root / "hold").exists())
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_ownership_guard_refusal_readmits_before_running_and_records_only_the_step(self) -> None:
        process = self.memory_wait_process(lose_at_launch=True, scoped_attempt=True)
        self.wait_for_running(self.root / "memory-wait", process)
        (self.root / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertTrue((self.root / "command-ran").exists())
        self.assertEqual((self.root / "acquisitions").read_text().splitlines(), ["acquired", "acquired"])
        self.assertEqual((self.root / "admission-holders").read_text().splitlines(), ["1", "1"])
        self.assertEqual((self.root / "recorded-statuses").read_text().splitlines(), ["0"])
        self.assertEqual((self.root / "token-waits").read_text().splitlines(), ["7"])
        self.assertFalse((self.root / "reservation").exists())
        self.assertFalse((self.root / "hold").exists())
        self.assertEqual(len((self.root / "scoped-attempts").read_text().splitlines()), 2)
        self.assertEqual(list(self.root.glob("attempt.*.peak")), [], "discarded scope kept its peak file")
        self.assertEqual(list(self.root.glob("attempt.*.peak.cgroup")), [])
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_unstarted_noncompiling_step_reacquires_before_its_command(self) -> None:
        process = self.memory_wait_process(lose_at_launch=True, noncompiling=True)
        self.wait_for_running(self.root / "command-ran", process)
        (self.root / "release").touch()
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual((self.root / "acquisitions").read_text().splitlines(), ["acquired", "acquired"])
        self.assertEqual((self.root / "recorded-statuses").read_text().splitlines(), ["0"])
        self.assertFalse((self.root / "board/locks/cargo.d").exists())

    def test_renew_requires_the_matching_holder_pid(self) -> None:
        board = self.root / "board"
        answer = self.board(board, "acquire", "same", "cargo", "--pid", str(os.getpid()))
        self.assertEqual(answer.returncode, 0, answer.stderr)
        lock = board / "locks" / "cargo.d"
        _ = (lock / "expires").write_text("0")
        wrong = self.board(board, "renew", "same", "cargo", "--pid", str(os.getppid()), "--hold", "30")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual((lock / "expires").read_text(), "0")
        _ = write_config_fixture(self.root / "direct.conf", "TOKEN_GUARD_ATTEMPTS=2\nTOKEN_GUARD_DELAY_MS=1")
        with (board / "locks/cargo.guard").open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            busy = self.board(board, "renew", "same", "cargo", "--pid", str(os.getpid()), "--hold", "30")
            self.assertEqual(busy.returncode, 2, busy.stdout + busy.stderr)
            self.assertEqual((lock / "expires").read_text(), "0")
        right = self.board(board, "renew", "same", "cargo", "--pid", str(os.getpid()), "--hold", "30")
        self.assertEqual(right.returncode, 0, right.stderr)
        self.assertGreater(int((lock / "expires").read_text()), 0)
        _ = self.board(board, "release", "same", "cargo", "--pid", str(os.getpid()))

class LintInputIdentityTests(VerifyLintCacheFixture):
    def assert_lint_calls(self, *, fixes: int = 0) -> None:
        judgment = ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"]
        expected = [judgment]
        if fixes:
            fix = ["--jobs", "8", "--fix", "--allow-dirty", "--allow-staged", "--allow-no-vcs", *judgment]
            expected = [judgment, *([fix] * fixes), judgment]
        self.assertEqual(self.calls("clippy"), expected)

    def private_scripts(self) -> tuple[Path, Path]:
        scripts = Path(self.environment["HOME"]) / ".claude" / "scripts"
        scripts.unlink()
        scripts.mkdir()
        for entry in SCRIPTS.iterdir():
            target = scripts / entry.name
            if entry.name in ("direct", "lint"):
                target.mkdir()
                for child in entry.iterdir():
                    destination = target / child.name
                    if child.name in ("verify.sh", "invoke.sh"):
                        _ = shutil.copy2(child, destination)
                    else:
                        destination.symlink_to(child, target_is_directory=child.is_dir())
            else:
                target.symlink_to(entry, target_is_directory=entry.is_dir())
        return scripts / "direct" / "verify.sh", scripts / "lint" / "invoke.sh"

    def test_script_edits_during_lint_add_no_fix_pass_or_tree_change_notice(self) -> None:
        verify, invoke = self.private_scripts()
        _ = self.enable_cache()
        cargo = lint_support.CARGO_STUB.replace('if arguments[:1] == ["clippy"]:', '''if arguments[:1] == ["clippy"]:
    for name in ("TEST_PRIVATE_VERIFY", "TEST_PRIVATE_INVOKE", "LINT_CONFIG_FILE"):
        with Path(os.environ[name]).open("a") as script:
            script.write("\\n# changed while clippy ran\\n")
if arguments[:1] == ["clippy"]:''')
        self.write_executable(self.root / "bin" / "cargo", cargo)
        self.environment.update(TEST_PRIVATE_VERIFY=str(verify), TEST_PRIVATE_INVOKE=str(invoke))
        with mock.patch.object(lint_support, "VERIFY", verify):
            result = self.verify("lint", "sample")
        self.assert_lint_calls()
        self.assertNotIn("tree changed", result.stdout + result.stderr)

    def test_script_edit_during_fix_cannot_cache_a_pass_for_the_new_script(self) -> None:
        verify, invoke = self.private_scripts()
        cache = self.enable_cache()
        cargo = lint_support.CARGO_STUB.replace('if arguments[:1] == ["clippy"]:', '''if arguments[:1] == ["clippy"] and "--fix" in arguments:
    script = Path(os.environ["TEST_PRIVATE_INVOKE"])
    marker = script.with_suffix(".edited")
    if not marker.exists():
        with script.open("a") as output:
            output.write("\\n# updated once during the fix pass\\n")
        marker.touch()
if arguments[:1] == ["clippy"]:''')
        from scripts.direct.test_verify_lint_judgment import FIRST_JUDGMENT, LINT_FINDING
        self.write_executable(self.root / "bin" / "cargo", FIRST_JUDGMENT + cargo)
        self.environment.update({"TEST_CLIPPY_FIRST_STATUS": "101",
                                 "TEST_CLIPPY_FIRST_DIAGNOSTIC": LINT_FINDING})
        self.environment["TEST_PRIVATE_INVOKE"] = str(invoke)
        with mock.patch.object(lint_support, "VERIFY", verify):
            _ = self.verify("lint", "sample")
            self.assert_lint_calls(fixes=1)
            self.assertEqual(list(cache.glob("*.pass")), [], "old loaded code certified the new script")
            _ = self.verify("lint", "sample")
            self.assert_lint_calls(fixes=1)
            self.assertEqual(len(list(cache.glob("*.pass"))), 1)
            _ = self.verify("lint", "sample")
            self.assertEqual(self.calls("clippy"), [])


    def test_lost_token_status_is_not_cached_as_a_tree_lint_failure(self) -> None:
        cache = self.enable_cache()
        self.environment["TEST_CLIPPY_STATUS"] = "125"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertEqual(list(cache.glob("*.fail")), [])
        self.environment["TEST_CLIPPY_STATUS"] = "0"
        _ = self.verify("lint", "sample")
        self.assert_lint_calls()

    def test_each_lint_script_edit_invalidates_a_previously_reused_pass(self) -> None:
        verify, invoke = self.private_scripts()
        _ = self.enable_cache()
        with mock.patch.object(lint_support, "VERIFY", verify):
            _ = self.verify("lint", "sample")
            _ = self.verify("lint", "sample")
            self.assertEqual(self.calls("clippy"), [])
            for script in (verify, invoke, self.config):
                with self.subTest(script=script.name):
                    with script.open("a") as output:
                        _ = output.write("\n# cache input changed\n")
                    _ = self.verify("lint", "sample")
                    self.assert_lint_calls()

if __name__ == "__main__":
    _ = unittest.main()
