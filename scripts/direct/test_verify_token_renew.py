"""Pin long-step ownership, lint input identity, and claimed Mac handoffs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import cast

from scripts.direct import verify_lint_scope_test_support as lint_support
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, BOARD, VERIFY
from scripts.direct.wait_for_test_state import wait_until

SCRIPTS = VERIFY.parent.parent


def shell_function(path: Path, name: str) -> str:
    source = path.read_text()
    start = source.index(name + "() {")
    return source[start:source.index("\n}\n", start) + 3]


class LongStepOwnershipTests(VerifyTokenWaitFixture):
    def board(self, board: Path, *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(BOARD), words[0], str(board), *words[1:]],
                              env=self.environment, capture_output=True, text=True, timeout=30)

    def test_running_step_renews_past_its_original_hold_and_stops_on_release(self) -> None:
        board = self.root / "board"
        process = self.start_check({**self.environment, "UNIT_RUN_DIR": str(board),
                                    "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                                    "VERIFY_TOKEN_HOLD_S": "2", "VERIFY_TOKEN_RENEW_S": "0.1"})
        lock = board / "locks" / "cargo.d"
        try:
            self.wait_for_running(self.root / "running", process)
            original_expiry = int((lock / "expires").read_text())
            # Crossing the original deadline proves renewal during a step, not
            # just an extension before launch. Watch the real board's writes.
            wait_until(lambda: int((lock / "expires").read_text() or "0") > original_expiry + 2,
                       lock, processes=(process,))
            rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
            self.assertNotEqual(rival.returncode, 0, rival.stdout + rival.stderr)
            self.assertEqual((lock / "holder").read_text().strip(), "first")
        finally:
            (self.root / "release").touch()
            output, errors = process.communicate(timeout=300)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertFalse(lock.exists())
        # There must be no heartbeat left in the call's session after release.
        self.assertEqual(self.live_group_processes(process.pid), [])

    def test_expired_matching_live_step_keeps_ownership_but_dead_holder_is_freed(self) -> None:
        board = self.root / "board"
        process = self.start_check({**self.environment, "UNIT_RUN_DIR": str(board),
                                    "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                                    "VERIFY_TOKEN_RENEW_S": "60"})
        lock = board / "locks" / "cargo.d"
        try:
            self.wait_for_running(self.root / "running", process)
            _ = (lock / "expires").write_text("0")
            rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
            self.assertNotEqual(rival.returncode, 0, rival.stdout + rival.stderr)
            self.assertIsNone(process.poll())
            self.end_group(process.pid)
            _ = process.communicate(timeout=300)
            rival = self.board(board, "acquire", "second", "cargo", "--pid", str(os.getpid()))
            self.assertEqual(rival.returncode, 0, rival.stdout + rival.stderr)
        finally:
            (self.root / "release").touch()
            self.end_group(process.pid)
            _ = process.communicate(timeout=300)
            _ = self.board(board, "release", "second", "cargo", "--pid", str(os.getpid()))

    def test_every_step_renews_before_launching_its_command(self) -> None:
        harness = self.root / "two-steps.sh"
        _ = harness.write_text("set -eu\n" + shell_function(VERIFY, "renew_token")
                               + shell_function(VERIFY, "buildlog_exec") + r'''
BOARD_HELPER=$1 BOARD_DIR=$2 BOARD_SLOT=holder TOKEN_HOLD_S=30 TOKEN_RENEW_S=60
TOKEN_HELD=1 PY=$3 STEP_OWNER_PY=""
bash "$BOARD_HELPER" acquire "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$ --hold 30
buildlog_exec_unowned() {
    test "$(cat "$BOARD_DIR/locks/cargo.d/expires")" -gt 0 || return 99
    printf 'command started after renewal\n'
}
for step in first second; do
    printf 0 > "$BOARD_DIR/locks/cargo.d/expires"
    buildlog_exec true
done
bash "$BOARD_HELPER" release "$BOARD_DIR" "$BOARD_SLOT" cargo --pid $$
''')
        result = subprocess.run(["bash", str(harness), str(BOARD), str(self.root / "board"), sys.executable],
                                env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("command started after renewal"), 2)

    def test_renew_requires_the_matching_holder_pid(self) -> None:
        board = self.root / "board"
        answer = self.board(board, "acquire", "same", "cargo", "--pid", str(os.getpid()))
        self.assertEqual(answer.returncode, 0, answer.stderr)
        lock = board / "locks" / "cargo.d"
        _ = (lock / "expires").write_text("0")
        wrong = self.board(board, "renew", "same", "cargo", "--pid", str(os.getppid()), "--hold", "30")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual((lock / "expires").read_text(), "0")
        right = self.board(board, "renew", "same", "cargo", "--pid", str(os.getpid()), "--hold", "30")
        self.assertEqual(right.returncode, 0, right.stderr)
        self.assertGreater(int((lock / "expires").read_text()), 0)
        _ = self.board(board, "release", "same", "cargo", "--pid", str(os.getpid()))


class LintInputIdentityTests(lint_support.VerifyLintScopeFixture):
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
        self.assert_fix_and_judgment_calls()
        self.assertNotIn("tree changed", result.stdout + result.stderr)

    def test_lost_token_status_is_not_cached_as_a_tree_lint_failure(self) -> None:
        cache = self.enable_cache()
        self.environment["TEST_CLIPPY_STATUS"] = "125"
        result = self.invoke_verify("lint", "sample", cwd=self.root)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertEqual(list(cache.glob("*.fail")), [])
        self.environment["TEST_CLIPPY_STATUS"] = "0"
        _ = self.verify("lint", "sample")
        self.assert_fix_and_judgment_calls()

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
        self.write_executable(self.root / "bin" / "cargo", cargo)
        self.environment["TEST_PRIVATE_INVOKE"] = str(invoke)
        with mock.patch.object(lint_support, "VERIFY", verify):
            _ = self.verify("lint", "sample")
            self.assert_fix_and_judgment_calls()
            self.assertEqual(list(cache.glob("*.pass")), [], "old loaded code certified the new script")
            _ = self.verify("lint", "sample")
            self.assert_fix_and_judgment_calls()
            self.assertEqual(len(list(cache.glob("*.pass"))), 1)
            _ = self.verify("lint", "sample")
            self.assertEqual(self.calls("clippy"), [])

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
                    self.assert_fix_and_judgment_calls()


class MemoryQueueIdentityTests(unittest.TestCase):
    def test_token_contention_and_hold_recheck_record_each_wait_second_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            (home / ".claude").symlink_to(SCRIPTS.parent, target_is_directory=True)
            _ = (root / "meminfo").write_text("MemAvailable: 1 kB\n")
            harness = root / "recheck-wait.sh"
            _ = harness.write_text('set -eu\nsource "$1"\n'
                                   + 'eval "$(declare -f buildlog_wait_for_memory | sed \'1s/buildlog_wait_for_memory/buildlog_wait_for_memory_only/\')"\n'
                                   + shell_function(VERIFY, "buildlog_wait_for_memory") + r'''
unset EPOCHSECONDS SECONDS
EPOCHSECONDS=100 SECONDS=0
BUILDLOG_MEM_WAIT_SINCE="" BUILDLOG_MEM_WAIT_FIRST_FILE="$TMPDIR/first-wait"
TOKEN_USED=1 TOKEN_HELD=0 TOKEN_WAIT_FILE="" acquisitions=0 hold_checks=0 hold_waits=0
step_state() { :; }
release_token() { :; }
try_token_now() { return 1; }
acquire_token() {
    acquisitions=$(( acquisitions + 1 ))
    if (( acquisitions == 1 )); then EPOCHSECONDS=$(( EPOCHSECONDS + 20 )); fi
    TOKEN_WAITED=0
}
sleep() { EPOCHSECONDS=$(( EPOCHSECONDS + 20 )); SECONDS=$(( SECONDS + 20 )); }
build_hold_wait_local() {
    hold_waits=$(( hold_waits + 1 ))
    if (( hold_waits == 2 )); then EPOCHSECONDS=$(( EPOCHSECONDS + 20 )); fi
}
build_hold_check_local() { hold_checks=$(( hold_checks + 1 )); (( hold_checks > 1 )) || return 10; }
buildlog_memory_python() {
    if [[ "${2:-}" == check ]]; then
        local polls=0
        if [[ -f "$TMPDIR/polls" ]]; then read -r polls < "$TMPDIR/polls"; fi
        polls=$(( polls + 1 )); printf '%s\n' "$polls" > "$TMPDIR/polls"
        if (( polls == 1 || polls == 4 )); then
            printf 'hold|100|100|fallback|0|0|0|0|fixture||repo|nextest|1|held|tree\n'
        else
            printf 'admit|100|100|fallback|0|0|0|0|fixture|reservation|repo|nextest|100|reserved|tree\n'
        fi
    else
        "$HOME/.claude/scripts/lib/py" "$@"
    fi
}
buildlog_release_memory() { :; }
nextest_share_end() { :; }
sweep_after_step() { :; }
buildlog_begin() { BUILDLOG_START=100 BUILDLOG_PEAK="" BUILDLOG_TREE=""; }
buildlog_now() { BUILDLOG_NOW=$EPOCHSECONDS; }
buildlog_exec() { :; }
BUILDLOG_SYNC=1 BUILDLOG_CALLER=verify BUILDLOG_CALL_ID=recheck-wait
run_once cargo nextest run
printf '%s\n' "$(( EPOCHSECONDS - 100 ))" > "$TMPDIR/elapsed"
printf '%s\n' "$acquisitions:$hold_checks" > "$TMPDIR/rechecks"
''')
            environment = {**os.environ, "HOME": str(home), "TMPDIR": str(root), "BUILDLOG_OFF": "0",
                           "BUILDLOG_DIR": str(root / "buildlog"), "BUILDLOG_MEMINFO": str(root / "meminfo"),
                           "BUILD_HOLD_DIR": str(root / "no-hold"), "UNIT_ACTIVE_DIR": str(root / "active"),
                           "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
                           "MAC_TEST_STATE_DIR": str(root / "mac")}
            result = subprocess.run(["bash", str(harness), str(SCRIPTS / "lint" / "invoke.sh")],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = [cast(dict[str, object], json.loads(line))
                    for path in (root / "buildlog").glob("*/*.jsonl")
                    for line in path.read_text().splitlines()]
            steps = [row for row in rows if row.get("kind") == "step"]
            self.assertEqual(len(steps), 1, rows)
            self.assertEqual((root / "rechecks").read_text().strip(), "2:2")
            elapsed = int((root / "elapsed").read_text())
            self.assertEqual(elapsed, 80)
            self.assertEqual(steps[0]["mem_wait_s"], elapsed)

    def test_repeated_declined_mac_attempts_reuse_the_first_memory_wait_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / "meminfo").write_text("MemAvailable: 1 kB\n")
            harness = root / "retry.sh"
            _ = harness.write_text('set -eu\nsource "$1"\n' + shell_function(VERIFY, "run_local_test") + r'''
# A valid checker reply, still held; no fake admission or reservation.
buildlog_memory_python() { printf 'hold|100|100|fallback|0|0|0|0|fixture||repo|nextest|1|held|tree\n'; }
buildlog_memory_line() { :; }
sleep() { :; }
mac_retry_hook() { echo true; }
start_copy_watch() { :; }
stop_copy_watch() { :; }
remember_mac_retry() { :; }
try_mac_test() { EPOCHSECONDS=$(( EPOCHSECONDS + 100 )); }
unset EPOCHSECONDS
EPOCHSECONDS=100
HOLD_WAIT_DIR=$2 MAC_LEAVE_SETTINGS=settings MAC_RETRY_AFTER=0 MAC_LEAVE_REASON=busy
MAC_COPY_WATCH=/does-not-exist MAC_RUNNER=/does-not-exist LINT_LEFT_QUEUE_STATUS=75
NEXTEST_ARGS=(run) rounds=0
run_nextest() {
    rounds=$(( rounds + 1 ))
    # Match run()'s subshell: memory-gate globals cannot reach run_local_test.
    (
        buildlog_wait_for_memory cargo nextest
        printf '%s:%s\n' "$BUILDLOG_MEM_WAIT_FROM" "$BUILDLOG_MEM_WAIT_SINCE" >> "$2/ages"
    )
    if (( rounds < 3 )); then return 75; fi
}
run_local_test
'''.replace('"$2/ages"', '"$HOLD_WAIT_DIR/ages"'))
            environment = {**os.environ, "BUILDLOG_MEMINFO": str(root / "meminfo"),
                           "BUILDLOG_OFF": "1", "BUILD_HOLD_DIR": str(root / "no-hold")}
            result = subprocess.run(["bash", str(harness), str(SCRIPTS / "lint" / "memory_gate.sh"), str(root)],
                                    env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "ages").read_text().splitlines(), ["100:", "100:100", "100:100"])


    def test_two_declined_mac_attempts_record_the_wait_from_its_first_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            (home / ".claude").symlink_to(SCRIPTS.parent, target_is_directory=True)
            _ = (root / "meminfo").write_text("MemAvailable: 1 kB\n")
            harness = root / "record-wait.sh"
            _ = harness.write_text('set -eu\nsource "$1"\n' + shell_function(VERIFY, "run_local_test") + r'''
unset EPOCHSECONDS SECONDS
EPOCHSECONDS=100 SECONDS=0
HOLD_WAIT_DIR=$2 MAC_LEAVE_SETTINGS=settings MAC_RETRY_AFTER=0 MAC_LEAVE_REASON=busy
MAC_COPY_WATCH=/does-not-exist MAC_RUNNER=/does-not-exist
NEXTEST_ARGS=(run) rounds=0
mac_retry_hook() { echo true; }
remember_mac_retry() { :; }
try_mac_test() {
    printf 'declined\n' >> "$HOLD_WAIT_DIR/mac-attempts"
    EPOCHSECONDS=$(( EPOCHSECONDS + 100 ))
}
sleep() { EPOCHSECONDS=$(( EPOCHSECONDS + 10 )); }
build_hold_wait_local() { :; }
build_hold_check_local() { :; }
buildlog_release_memory() { :; }
nextest_share_end() { :; }
sweep_after_step() { :; }
buildlog_begin() { BUILDLOG_START=100 BUILDLOG_PEAK="" BUILDLOG_TREE=""; }
buildlog_now() { BUILDLOG_NOW=$EPOCHSECONDS; }
buildlog_memory_python() {
    if [[ "${2:-}" == check ]]; then
        if (( rounds == 3 && EPOCHSECONDS >= 310 )); then
            printf 'admit|100|100|fallback|0|0|0|0|fixture|reservation|repo|nextest|100|reserved|tree\n'
        else
            printf 'hold|100|100|fallback|0|0|0|0|fixture||repo|nextest|1|held|tree\n'
        fi
    else
        "$HOME/.claude/scripts/lib/py" "$@"
    fi
}
# Keep run_once, buildlog_end, memory_gate and record.py real. Only the
# command itself and admission replies are controlled by this fixture.
buildlog_exec() { :; }
run_nextest() {
    rounds=$(( rounds + 1 ))
    ( run_once cargo nextest run )
}
BUILDLOG_SYNC=1 BUILDLOG_CALLER=verify BUILDLOG_CALL_ID=first-wait
run_local_test
''')
            environment = {**os.environ, "HOME": str(home), "BUILDLOG_OFF": "0",
                           "BUILDLOG_DIR": str(root / "buildlog"),
                           "BUILDLOG_MEMINFO": str(root / "meminfo"),
                           "BUILD_HOLD_DIR": str(root / "no-hold"),
                           "XDG_STATE_HOME": str(root / "state"), "XDG_CACHE_HOME": str(root / "cache"),
                           "UNIT_ACTIVE_DIR": str(root / "active"), "MAC_TEST_STATE_DIR": str(root / "mac")}
            result = subprocess.run(["bash", str(harness), str(SCRIPTS / "lint" / "invoke.sh"), str(root)],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "mac-attempts").read_text().splitlines(), ["declined", "declined"])
            rows = [cast(dict[str, object], json.loads(line))
                    for path in (root / "buildlog").glob("*/*.jsonl")
                    for line in path.read_text().splitlines()]
            steps = [row for row in rows if row.get("kind") == "step"]
            self.assertEqual(len(steps), 1, rows)
            self.assertEqual(steps[0]["mem_wait_s"], 210)
            self.assertIn("memory free after 3 min 30 s: starting", result.stderr)


class MacWaitClaimTests(unittest.TestCase):
    def test_competing_waiters_cannot_leave_without_a_claim_and_runner_adopts_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "sample"
            repo.mkdir()
            _ = subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
            state = root / "state"
            settings = root / "mac.conf"
            _ = settings.write_text("offload=on\nlinux_only.sample=\n")
            environment = {**os.environ, "MAC_TEST_STATE_DIR": str(state), "BUILDLOG_OFF": "1",
                           "MAC_TEST_CONFIG": str(settings), "BUILD_HOLD_DIR": str(root / "holds")}
            script = SCRIPTS / "mac_test" / "mac_test.py"
            command = [sys.executable, str(script), "claim-wait", "--pid", str(os.getpid()),
                       "--settings", str(settings), "--repo-root", str(repo), "--package", "sample"]
            def claim(_contender: int = 0) -> subprocess.CompletedProcess[str]:
                return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
            with ThreadPoolExecutor(max_workers=2) as executor:
                attempts = list(executor.map(claim, range(2)))
            self.assertEqual(sorted(answer.returncode for answer in attempts), [0, 1],
                             [(answer.stdout, answer.stderr) for answer in attempts])
            claims = list((state / "claims").glob("*.json"))
            self.assertEqual(len(claims), 1)
            before = cast(dict[str, object], json.loads(claims[0].read_text()))
            self.assertEqual(before["pid"], os.getpid())
            # Use the runner's actual CLI conversion, then adopt with the Mac
            # coordinator. Patch only watcher startup (a system service).
            sys.path.insert(0, str(SCRIPTS / "mac_test"))
            from scripts.mac_test import mac_test, offload
            request_args = offload.parser().parse_args([
                "run", "--repo-root", str(repo), "--package", "sample", "--call-id", "wait-handoff", "--claim-owner-pid", str(os.getpid())],
                namespace=offload.CliArguments())
            request = offload.request_from_arguments(request_args)
            words = offload.claim_arguments(request, offload.read_config(settings))
            self.assertIn("--from-pid", words)
            with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as runner:
                words[words.index("--pid") + 1] = str(runner.pid)
                args = mac_test.parser().parse_args(words[2:], namespace=mac_test.CliArguments())
                with mock.patch.dict(os.environ, environment), mock.patch.object(mac_test, "start_claim_watcher"):
                    self.assertEqual(mac_test.command_claim(args, mac_test.StatePaths(state)), 0)
                after = cast(dict[str, object], json.loads(claims[0].read_text()))
                self.assertEqual(after["pid"], runner.pid)
                self.assertEqual(after["since"], before["since"])
                self.assertEqual(claim().returncode, 1, "adoption opened the scope to another waiter")
                _ = runner.communicate(timeout=30)


if __name__ == "__main__":
    _ = unittest.main()
