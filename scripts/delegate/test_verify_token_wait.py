#!/usr/bin/env python3
"""Verify call records isolate cargo token time from other pre-run work."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast, override

VERIFY = Path(__file__).with_name("verify.sh")
BOARD = VERIFY.with_name("board.sh")
Record = dict[str, object]


def restore_sigint() -> None:
    _ = signal.signal(signal.SIGINT, signal.SIG_DFL)


class VerifyTokenWaitTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    environment: dict[str, str]  # pyright: ignore[reportUninitializedInstanceVariable]

    started_groups: set[int] = set()

    @override
    def setUp(self) -> None:
        self.started_groups = set()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        home = self.root / "home"
        home.mkdir()
        (home / ".claude").symlink_to(VERIFY.parent.parent.parent, target_is_directory=True)
        stubs = self.root / "stubs"
        stubs.mkdir()
        cargo = stubs / "cargo"
        _ = cargo.write_text(r'''#!/bin/sh
if [ "$1" = metadata ]; then
    printf "metadata\n" >> "$METADATA_CALLS"
    printf '%s\n' '{"packages":[{"name":"sample","targets":[{"name":"sample","kind":["lib"],"test":true}]}]}'
    exit 0
fi
printf "%s\n" "$*" >> "$CARGO_CALLS"
if [ "$1" = check ]; then
    if [ "${TEST_REQUIRE_OWNED:-0}" = 1 ]; then
        python3 - "$TEST_OWNED_RECORD" "$$" "$TEST_CALLER_GROUP" <<'PY'
import json
import os
from pathlib import Path
import sys

record = json.loads(Path(sys.argv[1]).read_text())
assert record["group"] == os.getpgid(int(sys.argv[2]))
assert record["group"] != int(sys.argv[3])
PY
        [ "$?" = 0 ] || exit 99
        : > "$TEST_OWNED_CONFIRMED"
    fi
    if [ "${TEST_BLOCK:-0}" = 1 ]; then
        printf '%s\n' "$$" > "$TEST_CARGO_PID"
        : > "$TEST_RUNNING"
        while [ ! -f "$TEST_RELEASE" ]; do sleep 0.1; done
    fi
    if [ "${TEST_RECLAIM_CHECK:-0}" = 1 ]; then
        old=$(cat "$TEST_CARGO_PID")
        state=$(ps -p "$old" -o stat= 2>/dev/null || true)
        case "$state" in ''|Z*) : ;; *) exit 98 ;; esac
    fi
    if [ "${TEST_CORRUPT_OWNER:-0}" = 1 ]; then
        python3 - "$TEST_OWNED_RECORD" "$TEST_STEP_GROUP" <<'PY'
from pathlib import Path
import json
import sys

record = Path(sys.argv[1])
Path(sys.argv[2]).write_text(str(json.loads(record.read_text())["group"]))
record.write_text("invalid ownership record")
PY
    fi
    if [ -n "${TEST_CARGO_STATUS:-}" ]; then
        exit "$TEST_CARGO_STATUS"
    fi
    exit 0
fi
exit 97
''')
        cargo.chmod(0o755)
        git = stubs / "git"
        _ = git.write_text(r'''#!/bin/sh
case " $* " in
    *"rev-parse --show-toplevel"*) printf "%s\n" "$PACKAGE_ROOT" ;;
    *"rev-parse HEAD^{tree}"*) printf "fixed-tree\n" ;;
    *"status --porcelain"*) : ;;
    *) exit 1 ;;
esac
''')
        git.chmod(0o755)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text('lint_config_enabled() { [ "$1" != sweep ]; }\n'
                              + 'lint_config_skip_notice() { :; }\n')
        for name in ("tmp", "runtime", "state", "cache", "session", "target", "cgroup"):
            (self.root / name).mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n'
        )
        source = self.root / "src" / "lib.rs"
        source.parent.mkdir()
        _ = source.write_text("pub fn value() -> u8 { 1 }\n")
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
        self.environment = {
            **{name: value for name, value in os.environ.items()
               if name not in {"PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_TEAM_ROLE"}},
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(home),
            "RUSTUP_HOME": str(Path.home() / ".rustup"),
            "PACKAGE_ROOT": str(self.root),
            "CARGO_CALLS": str(self.root / "cargo-calls"),
            "METADATA_CALLS": str(self.root / "metadata-calls"),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "PLAN_DELEGATE_SESSION_DIR": str(self.root / "session"),
            "TEST_RUNNING": str(self.root / "running"),
            "TEST_RELEASE": str(self.root / "release"),
            "TEST_CARGO_PID": str(self.root / "cargo-pid"),
        }

    @override
    def tearDown(self) -> None:
        for group in self.started_groups:
            self.end_group(group)
            self.assertEqual(self.live_group_processes(group), [], f"group {group} still runs")
        self.temporary.cleanup()

    def live_group_processes(self, group: int) -> list[int]:
        found: list[int] = []
        result = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and int(parts[1]) == group and not parts[2].startswith("Z"):
                found.append(int(parts[0]))
        return found

    def end_group(self, group: int) -> None:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while self.live_group_processes(group) and time.monotonic() < deadline:
            time.sleep(0.05)

    def records(self) -> list[Record]:
        return [record for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()
                if (record := cast(Record, json.loads(line))).get("kind") == "call"]

    def verify(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                              env=environment, capture_output=True, text=True, check=False, timeout=30)

    def test_no_token_records_zero(self) -> None:
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(), ["metadata"])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

    def test_step_group_is_recorded_before_cargo_starts(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_REQUIRE_OWNED": "1",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_OWNED_CONFIRMED": str(self.root / "owned-confirmed"),
                       "TEST_CALLER_GROUP": str(os.getpgrp())}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "owned-confirmed").exists())

    def test_failed_release_reports_reason_and_fails_successful_step(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_CORRUPT_OWNER": "1",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_STEP_GROUP": str(self.root / "step-group")}
        result = self.verify(environment)
        group = int((self.root / "step-group").read_text())
        self.started_groups.add(group)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("board.sh: invalid step ownership record", result.stderr)
        self.assertTrue((board / "locks" / "cargo.d").exists())
        self.assertEqual(self.live_group_processes(group), [])

    def test_failed_release_preserves_step_failure(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_CORRUPT_OWNER": "1",
                       "TEST_CARGO_STATUS": "42",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_STEP_GROUP": str(self.root / "step-group")}
        result = self.verify(environment)
        group = int((self.root / "step-group").read_text())
        self.started_groups.add(group)
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertIn("board.sh: invalid step ownership record", result.stderr)
        self.assertEqual(self.live_group_processes(group), [])

    def test_waiting_for_peer_call_records_token_seconds(self) -> None:
        board = self.root / "board"
        waiting = self.root / "waiting-for-token"
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('''#!/bin/sh
if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then
    : > "$TEST_WAITING"
fi
exec "$TEST_REAL_SLEEP" "$@"
''')
        sleep.chmod(0o755)
        first_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                             "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                             "TEST_REAL_SLEEP": real_sleep}
        second_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                              "PLAN_DELEGATE_TEAM_ROLE": "second", "TEST_WAITING": str(waiting),
                              "TEST_REAL_SLEEP": real_sleep}
        first_started_at = time.monotonic()
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "running").exists() and time.monotonic() < deadline:
                if first.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue((self.root / "running").exists(), "first verify call did not reach cargo check")
            first_running_at = time.monotonic()
            first_wait_upper_bound = int(first_running_at - first_started_at) + 1
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env=second_environment, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.started_groups.add(second.pid)
            try:
                deadline = time.monotonic() + 30
                while not waiting.exists() and time.monotonic() < deadline:
                    if second.poll() is not None:
                        break
                    time.sleep(0.05)
                self.assertTrue(waiting.exists(), "second verify call did not wait for the cargo token")
                second_blocked_at = time.monotonic()
                deadline = second_blocked_at + max(30, first_wait_upper_bound + 10)
                while (int(time.monotonic() - second_blocked_at) - 1
                       <= first_wait_upper_bound and time.monotonic() < deadline):
                    time.sleep(0.05)
                # Read before the touch: the second call's real wait can only run longer than this.
                release_at = time.monotonic()
                second_wait_lower_bound = int(release_at - second_blocked_at) - 1
                self.assertGreater(second_wait_lower_bound, first_wait_upper_bound,
                                   "second call was not observed waiting long enough")
                self.assertIsNone(second.poll(), "second verify exited before the token was released")
                _ = (self.root / "release").touch()
                first_output = first.communicate(timeout=20)
                second_output = second.communicate(timeout=20)
                self.assertEqual(first.returncode, 0, first_output)
                self.assertEqual(second.returncode, 0, second_output)
            finally:
                if second.poll() is None:
                    self.end_group(second.pid)
                    _ = second.communicate()
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
                _ = first.communicate()
        records = self.records()
        self.assertEqual(len(records), 2)
        first_record = next((record for record in records
                             if f"-{first.pid}-" in cast(str, record["id"])), None)
        second_record = next((record for record in records
                              if f"-{second.pid}-" in cast(str, record["id"])), None)
        if first_record is None or second_record is None:
            self.fail("both verify calls must have buildlog records")
        first_wait = cast(int, first_record["token_wait_s"])
        second_wait = cast(int, second_record["token_wait_s"])
        self.assertLessEqual(first_wait, first_wait_upper_bound)
        self.assertGreaterEqual(second_wait, second_wait_lower_bound)
        self.assertGreater(second_wait, first_wait)

    def test_killed_verify_holder_is_reclaimed_promptly(self) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                             "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                              "PLAN_DELEGATE_TEAM_ROLE": "second"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "running").exists() and time.monotonic() < deadline:
                if first.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue((self.root / "running").exists(),
                            "first verify call did not reach cargo check")
            recorded_pid = (board / "locks" / "cargo.d" / "holder_pid").read_text()
            self.assertEqual(recorded_pid, str(first.pid))
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            self.started_groups.add(cast(int, step_record["group"]))

            self.end_group(first.pid)
            _ = first.wait(timeout=5)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertLess(cast(int, records[0]["token_wait_s"]), 3)
            self.assertIn(f"token cargo reclaimed from first: holder pid {first.pid} is gone",
                          (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=5)
            if first.stdout is not None:
                first.stdout.close()
            if first.stderr is not None:
                first.stderr.close()

    def test_reclaim_stops_old_build_before_next_build(self) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                             "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                              "PLAN_DELEGATE_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "running").exists() and time.monotonic() < deadline:
                self.assertIsNone(first.poll(), "first verify exited before cargo")
                time.sleep(0.05)
            self.assertTrue((self.root / "running").exists())
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)
            self.assertNotEqual(step_group, first.pid)
            first.kill()
            _ = first.wait(timeout=5)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.live_group_processes(first.pid), [])
            self.assertEqual(self.live_group_processes(step_group), [])
        finally:
            self.end_group(first.pid)

    def test_expired_live_holder_reclaim_keeps_its_build_running(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "running").exists() and time.monotonic() < deadline:
                self.assertIsNone(first.poll(), "first verify exited before cargo")
                time.sleep(0.05)
            self.assertTrue((self.root / "running").exists())
            lock = board / "locks" / "cargo.d"
            step_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)
            cargo_pid = int((self.root / "cargo-pid").read_text())
            self.assertIn(cargo_pid, self.live_group_processes(step_group))
            self.assertIsNone(first.poll(), "holder exited before its lock expired")

            _ = (lock / "expires").write_text("0")
            second = subprocess.run(["bash", str(BOARD), "acquire", str(board), "second",
                                     "cargo", "--wait", "0"], capture_output=True, text=True,
                                    check=False, timeout=10)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("reclaimed from first", second.stdout)
            self.assertIn("after its hold expired", (board / "board.log").read_text())
            self.assertIsNone(first.poll(), "reclaim ended the live holder")
            self.assertIn(cargo_pid, self.live_group_processes(step_group),
                          "reclaim ended the live holder's cargo")

            reclaimed_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", str(first.pid)],
                capture_output=True, text=True, check=False, timeout=10)
            self.assertEqual(reclaimed_release.returncode, 3, reclaimed_release.stderr)
            self.assertIn("first does not hold cargo (holder is second)", reclaimed_release.stderr)
            wrong_process_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", "1"],
                capture_output=True, text=True, check=False, timeout=10)
            self.assertEqual(wrong_process_release.returncode, 1, wrong_process_release.stderr)
            unowned_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "third", "cargo"],
                capture_output=True, text=True, check=False, timeout=10)
            self.assertEqual(unowned_release.returncode, 1, unowned_release.stderr)

            _ = (self.root / "release").touch()
            _, first_error = first.communicate(timeout=20)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertIn("first does not hold cargo (holder is second)", first_error)
            released = subprocess.run(["bash", str(BOARD), "release", str(board), "second",
                                       "cargo"], capture_output=True, text=True, check=False,
                                      timeout=10)
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=5)
            if first.stderr is not None:
                first.stderr.close()

    def test_same_slot_reclaim_preserves_new_step_and_old_result(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        first_release = self.root / "first-release"
        second_release = self.root / "second-release"
        first_running = self.root / "first-running"
        second_running = self.root / "second-running"
        first_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                             "PLAN_DELEGATE_TEAM_ROLE": "holder", "TEST_BLOCK": "1",
                             "TEST_RELEASE": str(first_release),
                             "TEST_RUNNING": str(first_running),
                             "TEST_CARGO_PID": str(self.root / "first-cargo-pid")}
        second_environment = {**first_environment, "TEST_RELEASE": str(second_release),
                              "TEST_RUNNING": str(second_running),
                              "TEST_CARGO_PID": str(self.root / "second-cargo-pid")}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        second: subprocess.Popen[str] | None = None
        try:
            deadline = time.monotonic() + 15
            while not first_running.exists() and time.monotonic() < deadline:
                self.assertIsNone(first.poll(), "first verify exited before cargo")
                time.sleep(0.05)
            self.assertTrue(first_running.exists(), "first cargo did not start")
            _ = (lock / "expires").write_text("0")
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env=second_environment, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.started_groups.add(second.pid)
            while not second_running.exists() and time.monotonic() < deadline:
                self.assertIsNone(second.poll(), "second verify exited before cargo")
                time.sleep(0.05)
            self.assertTrue(second_running.exists(), "second cargo did not start")
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            second_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            second_group = cast(int, second_record["group"])
            self.started_groups.add(second_group)
            second_cargo_pid = int((self.root / "second-cargo-pid").read_text())

            first_release.touch()
            _, first_error = first.communicate(timeout=20)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            self.assertIsNone(second.poll(), "old release ended the new verify")
            self.assertIn(second_cargo_pid, self.live_group_processes(second_group),
                          "old release ended the new cargo step")

            second_release.touch()
            _, second_error = second.communicate(timeout=20)
            self.assertEqual(second.returncode, 0, second_error)
        finally:
            first_release.touch()
            second_release.touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=5)
            if first.stderr is not None:
                first.stderr.close()
            if second is not None:
                if second.poll() is None:
                    self.end_group(second.pid)
                _ = second.wait(timeout=5)
                if second.stderr is not None:
                    second.stderr.close()

    def assert_interrupted_holder_stops_step_before_release(self, interruption: signal.Signals) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                             "PLAN_DELEGATE_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                              "PLAN_DELEGATE_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 preexec_fn=restore_sigint)
        self.started_groups.add(first.pid)
        try:
            deadline = time.monotonic() + 15
            while not (self.root / "running").exists() and time.monotonic() < deadline:
                self.assertIsNone(first.poll(), "first verify exited before cargo")
                time.sleep(0.05)
            self.assertTrue((self.root / "running").exists())
            lock = board / "locks" / "cargo.d"
            step_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)

            os.killpg(first.pid, interruption)
            _ = first.wait(timeout=10)
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse(lock.exists(), "interrupted verify kept its token")
            self.assertEqual(self.live_group_processes(step_group), [],
                             "token became free while its step still ran")
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        finally:
            _ = (self.root / "release").touch()
            self.end_group(first.pid)
            _ = first.wait(timeout=5)

    def test_sigterm_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGTERM)

    def test_sigint_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGINT)


if __name__ == "__main__":
    _ = unittest.main()
