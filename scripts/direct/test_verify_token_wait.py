#!/usr/bin/env python3
"""Verify call records isolate cargo token time from other pre-run work."""

from __future__ import annotations

import json
import os
import shutil
import signal
import select
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import unittest
from pathlib import Path
from typing import cast, override

from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.wait_for_test_state import wait_until
from scripts.direct.test_verify_mac_offload import RUSTC_STUB, stub_build_tool_path

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
        python3 "$TEST_WAIT_HELPER" "$TEST_RELEASE"
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
        rustc = stubs / "rustc"
        _ = rustc.write_text(RUSTC_STUB)
        rustc.chmod(0o755)
        git = stubs / "git"
        _ = git.write_text(r'''#!/bin/sh
case " $* " in
    *"rev-parse --show-toplevel"*) printf "%s\n" "$PACKAGE_ROOT" ;;
    *"rev-parse HEAD^{tree}"*) printf "fixed-tree\n" ;;
    *"status --porcelain"*|*" ls-tree "*) : ;;
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
               if name not in {"UNIT_RUN_DIR", "UNIT_TEAM_ROLE", "CODEX_THREAD_ID",
                               "CODEX_SESSION_ID", "UNIT_DETACHED_RUN", "BASH_ENV", "ENV"}
               and not name.startswith(("PLAN_DELEGATE_", "BASH_FUNC_"))},
            "PATH": stub_build_tool_path(stubs),
            "HOME": str(home),
            "RUSTUP_HOME": str(self.root / "rustup"),
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
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "hold-release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_ADMISSION_FILE": "",
            "BUILDLOG_HOLD_STARTED_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.root / "registration"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "MAC_IDLE_STATE_DIR": str(self.root / "mac-idle"),
            "VERIFY_MAC_RUNNER": str(self.root / "no-mac-runner"),
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "UNIT_SESSION_DIR": str(self.root / "session"),
            "TEST_RUNNING": str(self.root / "running"),
            "TEST_RELEASE": str(self.root / "release"),
            "TEST_WAIT_HELPER": str(VERIFY.parent / "wait_for_test_state.py"),
            "TEST_CARGO_PID": str(self.root / "cargo-pid"),
        }

    @override
    def tearDown(self) -> None:
        for group in self.started_groups:
            self.end_group(group)
            self.assertEqual(self.live_group_processes(group), [], f"group {group} still runs")
        self.temporary.cleanup()

    def live_group_processes(self, group: int) -> list[int]:
        """The live processes in the group, and in the session a started call leads.

        verify.sh runs each step in a group of its own (run()'s `set -m`), in the
        call's session; a kill of the call's group alone left a step waiting for the
        token for 45 h (2026-10-20).
        """
        found: list[int] = []
        result = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
                                capture_output=True, text=True, check=True)
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[2].startswith("Z"):
                continue
            pid = int(parts[0])
            if int(parts[1]) == group or self.session_of(pid) == group:
                found.append(pid)
        return found

    @staticmethod
    def session_of(pid: int) -> int | None:
        try:
            return os.getsid(pid)
        except OSError:
            return None

    def end_group(self, group: int) -> None:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 300
        while pids := self.live_group_processes(group):
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            descriptors: list[int] = []
            poll_pids = False
            try:
                for pid in pids:
                    try:
                        descriptors.append(os.pidfd_open(pid))
                    except ProcessLookupError:
                        pass
                    except AttributeError:
                        poll_pids = True
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, f"group {group} never stopped")
                if poll_pids:
                    time.sleep(min(0.01, remaining))
                elif descriptors:
                    readable, _, _ = select.select(descriptors, [], [], remaining)
                    self.assertTrue(readable, f"group {group} never stopped")
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

    def records(self) -> list[Record]:
        return [record for path in (self.root / "buildlog").glob("*/*.jsonl")
                for line in path.read_text().splitlines()
                if (record := cast(Record, json.loads(line))).get("kind") == "call"]

    def verify(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                              env=environment, capture_output=True, text=True, check=False, timeout=300)

    def wait_for_running(self, path: Path, process: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
        wait_until(lambda: path.exists() or process.poll() is not None, path.parent, processes=(process,))
        self.assertTrue(path.exists(), "verify never reached its expected state")

    def record_phase(self, session: Path) -> None:
        self.environment.update({"UNIT_HISTORY_DIR": str(self.root / "history"),
                                 "UNIT_PHASE_ID": "6", "UNIT_SYNC_WRITES": "0",
                                 "UNIT_PASS_OWNER": "launcher",
                                 "CLAUDE_PROBE_SESSION": ""})
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.root),
             "--main-family", "codex", "--main-model", "fixture", "--main-effort", "high",
             "--main-session-id", "fixture-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "6", "--phase-title", "Resources"],
        ):
            result = invoke_recorder(arguments, self.environment, self.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        for slot in ("impl", "first", "second"):
            arguments = ["start-pass", "--session-dir", str(session), "--phase-id", "6", "--pass-kind", "impl",
                         "--activity", "verify", "--called-task", "direct.impl", "--called-family", "codex",
                         "--called-model", "fixture"]
            result = invoke_recorder(arguments, {**self.environment, "UNIT_TEAM_ROLE": slot}, self.root)
            self.assertEqual(result.returncode, 0, result.stderr)

    def phase_waits(self, session: Path) -> list[dict[str, object]]:
        return [row for row in (cast(dict[str, object], json.loads(line)) for line in
                (self.root / "history" / "runs" / f"{session.name}.jsonl").read_text().splitlines())
                if str(row["event_type"]).startswith("wait_")]

    def test_unblocked_verification_records_no_resource_wait(self) -> None:
        session = Path(self.environment["UNIT_SESSION_DIR"])
        self.record_phase(session)
        result = self.verify(self.environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.phase_waits(session), [])

    def test_group_cleanup_ends_without_pidfd_support(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                 start_new_session=True)
        try:
            # Pin the race where a killed member is still visible in the first
            # snapshot, so portable cleanup must take its polling branch.
            with patch.object(os, "pidfd_open", side_effect=AttributeError("pidfd_open unavailable"),
                              create=True), patch.object(self, "live_group_processes",
                                                         side_effect=[[child.pid], []]):
                self.end_group(child.pid)
            self.assertNotEqual(child.wait(timeout=300), 0)
            self.assertEqual(self.live_group_processes(child.pid), [])
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=300)

    def test_no_token_records_zero(self) -> None:
        # A session dir alone makes this a director's run, which takes the token.
        environment = {name: value for name, value in self.environment.items()
                       if name != "UNIT_SESSION_DIR"}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertEqual((self.root / "metadata-calls").read_text().splitlines(), ["metadata"])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))

    def test_director_run_takes_the_token_as_slot_director(self) -> None:
        clock = self.root / "director-clock"
        _ = clock.write_text("100\n")
        session = Path(self.environment["UNIT_SESSION_DIR"])
        result = self.verify({**self.environment, "UNIT_TOKEN_CLOCK_FILE": str(clock)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.records()[0]["token_wait_s"], 0)
        self.assertIn("[director] claim: token cargo acquired", (session / "board.log").read_text())
        self.assertFalse((session / "locks" / "cargo.d").exists(), "the director's token was not released")

    def test_token_wait_uses_elapsed_seconds_when_no_clock_override_is_present(self) -> None:
        # Copy the board boundary into a disposable HOME; acquire deliberately
        # crosses a whole second, so scheduling can only increase the wait.
        home = self.root / "clock-home"
        direct_dir = home / ".claude/scripts/direct"
        direct_dir.mkdir(parents=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "direct":
                (direct_dir.parent / source.name).symlink_to(source, target_is_directory=source.is_dir())
        board = direct_dir / "board.sh"
        _ = board.write_text('#!/bin/bash\nif [[ "$1" == acquire ]]; then sleep 1.1; fi\n'
                             + 'exec bash "$TEST_REAL_BOARD" "$@"\n')
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "clock-board"),
                       "UNIT_TEAM_ROLE": "clock", "TEST_REAL_BOARD": str(BOARD)}
        for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertGreaterEqual(cast(int, self.records()[0]["token_wait_s"]), 1)

    def test_step_group_is_recorded_before_cargo_starts(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_REQUIRE_OWNED": "1",
                       "TEST_OWNED_RECORD": str(board / "locks" / "cargo.d" / "owned"),
                       "TEST_OWNED_CONFIRMED": str(self.root / "owned-confirmed"),
                       "TEST_CALLER_GROUP": str(os.getpgrp())}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "owned-confirmed").exists())

    def test_a_seat_with_only_the_names_before_the_rename_takes_its_run_token(self) -> None:
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
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_CORRUPT_OWNER": "1",
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
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_CORRUPT_OWNER": "1",
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
        self.record_phase(board)
        waiting = self.root / "waiting-for-token"
        resume = self.root / "resume-acquisition"
        first_clock = self.root / "first-clock"
        second_clock = self.root / "second-clock"
        _ = first_clock.write_text("1000\n")
        _ = second_clock.write_text("1000\n")
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('''#!/bin/sh
if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then
    : > "$TEST_WAITING"
    exec python3 "$TEST_WAIT_HELPER" "$TEST_WAIT_GATE"
fi
exec "$TEST_REAL_SLEEP" "$@"
''')
        sleep.chmod(0o755)
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                             "TEST_REAL_SLEEP": real_sleep,
                             "UNIT_TOKEN_CLOCK_FILE": str(first_clock)}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_WAITING": str(waiting),
                              "TEST_REAL_SLEEP": real_sleep, "TEST_WAIT_GATE": str(resume),
                              "UNIT_TOKEN_CLOCK_FILE": str(second_clock)}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running(self.root / "running", first)
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env=second_environment, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.started_groups.add(second.pid)
            try:
                self.wait_for_running(waiting, second)
                self.assertIsNone(second.poll(), "second verify exited before the token was released")
                self.assertEqual(len(self.records()), 0)
                _ = second_clock.write_text("1020\n")
                _ = (self.root / "release").touch()
                first_output = first.communicate(timeout=300)
                self.assertEqual(first.returncode, 0, first_output)
                resume.touch()
                second_output = second.communicate(timeout=300)
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
        self.assertEqual(first_wait, 0)
        self.assertEqual(second_wait, 20)
        self.assertGreater(second_wait, first_wait)
        started, = [row for row in self.phase_waits(board) if row["event_type"] == "wait_started"]
        finished, = [row for row in self.phase_waits(board) if row["event_type"] == "wait_finished"]
        self.assertEqual((started["wait_kind"], started["wait_reason"], started["phase_id"]),
                         ("WaitingForMemoryOrSlot", "cargo token", "6"))
        self.assertEqual(started["wait_id"], finished["wait_id"])

    def test_killed_verify_holder_is_reclaimed_promptly(self) -> None:
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('''#!/bin/sh
if [ "$1" = 3 ]; then
    printf '%s\\n' "$1" >> "$TEST_RECLAIM_POLLS"
fi
exec "$TEST_REAL_SLEEP" "$@"
''')
        sleep.chmod(0o755)
        real_sleep = shutil.which("sleep")
        assert real_sleep is not None
        polls = self.root / "reclaim-polls"
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_POLLS": str(polls),
                              "TEST_REAL_SLEEP": real_sleep}
        first_environment["TEST_REAL_SLEEP"] = real_sleep
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists(),
                            "first verify call did not reach cargo check")
            recorded_pid = (board / "locks" / "cargo.d" / "holder_pid").read_text()
            self.assertEqual(recorded_pid, str(first.pid))
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            self.started_groups.add(cast(int, step_record["group"]))

            self.end_group(first.pid)
            _ = first.wait(timeout=300)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            records = self.records()
            self.assertEqual(len(records), 1)
            self.assertFalse(polls.exists(), "reclaimed holder must not require a token polling sleep")
            self.assertIn(f"token cargo reclaimed from first: holder pid {first.pid} is gone",
                          (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=300)
            if first.stdout is not None:
                first.stdout.close()
            if first.stderr is not None:
                first.stderr.close()

    def test_reclaim_stops_old_build_before_next_build(self) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists())
            step_record = cast(dict[str, object], json.loads(
                (board / "locks" / "cargo.d" / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)
            self.assertNotEqual(step_group, first.pid)
            first.kill()
            _ = first.wait(timeout=300)
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(self.live_group_processes(first.pid), [])
            self.assertEqual(self.live_group_processes(step_group), [])
        finally:
            self.end_group(first.pid)

    def test_expired_live_holder_reclaim_keeps_its_build_running(self) -> None:
        board = self.root / "board"
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
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
                                    check=False, timeout=300)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("reclaimed from first", second.stdout)
            self.assertIn("after its hold expired", (board / "board.log").read_text())
            self.assertIsNone(first.poll(), "reclaim ended the live holder")
            self.assertIn(cargo_pid, self.live_group_processes(step_group),
                          "reclaim ended the live holder's cargo")

            reclaimed_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", str(first.pid)],
                capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(reclaimed_release.returncode, 3, reclaimed_release.stderr)
            self.assertIn("first does not hold cargo (holder is second)", reclaimed_release.stderr)
            wrong_process_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "first", "cargo", "--pid", "1"],
                capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(wrong_process_release.returncode, 1, wrong_process_release.stderr)
            unowned_release = subprocess.run(
                ["bash", str(BOARD), "release", str(board), "third", "cargo"],
                capture_output=True, text=True, check=False, timeout=300)
            self.assertEqual(unowned_release.returncode, 1, unowned_release.stderr)

            _ = (self.root / "release").touch()
            _, first_error = first.communicate(timeout=300)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertIn("first does not hold cargo (holder is second)", first_error)
            released = subprocess.run(["bash", str(BOARD), "release", str(board), "second",
                                       "cargo"], capture_output=True, text=True, check=False,
                                      timeout=300)
            self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        finally:
            _ = (self.root / "release").touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=300)
            if first.stderr is not None:
                first.stderr.close()

    def test_same_slot_reclaim_preserves_new_step_and_old_result(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        first_release = self.root / "first-release"
        second_release = self.root / "second-release"
        first_running = self.root / "first-running"
        second_running = self.root / "second-running"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "holder", "TEST_BLOCK": "1",
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
            self.wait_for_running(first_running, first)
            self.assertTrue(first_running.exists(), "first cargo did not start")
            _ = (lock / "expires").write_text("0")
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env=second_environment, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True, start_new_session=True)
            self.started_groups.add(second.pid)
            self.wait_for_running(second_running, second)
            self.assertTrue(second_running.exists(), "second cargo did not start")
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            second_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            second_group = cast(int, second_record["group"])
            self.started_groups.add(second_group)
            second_cargo_pid = int((self.root / "second-cargo-pid").read_text())

            first_release.touch()
            _, first_error = first.communicate(timeout=300)
            self.assertEqual(first.returncode, 0, first_error)
            self.assertEqual((lock / "holder_pid").read_text(), str(second.pid))
            self.assertIsNone(second.poll(), "old release ended the new verify")
            self.assertIn(second_cargo_pid, self.live_group_processes(second_group),
                          "old release ended the new cargo step")

            second_release.touch()
            _, second_error = second.communicate(timeout=300)
            self.assertEqual(second.returncode, 0, second_error)
        finally:
            first_release.touch()
            second_release.touch()
            if first.poll() is None:
                self.end_group(first.pid)
            _ = first.wait(timeout=300)
            if first.stderr is not None:
                first.stderr.close()
            if second is not None:
                if second.poll() is None:
                    self.end_group(second.pid)
                _ = second.wait(timeout=300)
                if second.stderr is not None:
                    second.stderr.close()

    def start_memory_waiter(self, environment: dict[str, str], errors: Path) -> subprocess.Popen[bytes]:
        """Start a verify that takes the token and then waits on the held meminfo.

        verify.sh tees its step output to stdout, so both streams go to `errors`.
        """
        with errors.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                       cwd=self.root, env=environment, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        # The directory watch sees new files, not writes to this one, so poll it.
        deadline = time.monotonic() + 300
        while ("waiting for memory" not in errors.read_text(errors="replace")
               and process.poll() is None and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertIn("waiting for memory", errors.read_text(errors="replace"))
        self.assertIsNone(process.poll(), errors.read_text(errors="replace"))
        return process

    def wait_for_output(self, path: Path, text: str, process: subprocess.Popen[bytes]) -> None:
        # The directory watch sees new files, not writes to this one, so poll it.
        deadline = time.monotonic() + 300
        while (text not in path.read_text(errors="replace")
               and process.poll() is None and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertIn(text, path.read_text(errors="replace"))

    def test_a_memory_wait_gives_the_token_to_a_peer_and_takes_it_back_before_its_step(self) -> None:
        # Phases 15 and 16 of one unit share the session's board and its role
        # names. Phase 16 waits for memory without the token, so phase 15
        # builds meanwhile. Phase 16's step then waits for the token, and phase
        # 15's launcher ending cannot free it.
        board = self.root / "board"
        self.record_phase(board)
        lock = board / "locks" / "cargo.d"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        peer_meminfo = self.root / "peer-meminfo"
        _ = peer_meminfo.write_text("MemAvailable: 50331648 kB\n")
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1"}
        phase_16_errors = self.root / "phase-16.err"
        phase_16 = self.start_memory_waiter(environment, phase_16_errors)
        phase_15: subprocess.Popen[bytes] | None = None
        try:
            self.assertFalse(lock.exists(), "the memory wait kept the token")
            phase_15 = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                        cwd=self.root,
                                        env={**environment, "BUILDLOG_MEMINFO": str(peer_meminfo),
                                             "TEST_BLOCK": "1"},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
            self.started_groups.add(phase_15.pid)
            self.wait_for_running(self.root / "running", phase_15)
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_15.pid))
            launcher_release = subprocess.run(["bash", str(BOARD), "release", str(board), "impl",
                                               "cargo"], capture_output=True, text=True,
                                              check=False, timeout=300)
            self.assertEqual(launcher_release.returncode, 1, launcher_release.stdout)
            self.assertIn(f"held by live pid {phase_15.pid}", launcher_release.stderr)

            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            self.wait_for_output(phase_16_errors, "memory free after", phase_16)
            self.assertIsNone(phase_16.poll(), phase_16_errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            self.assertEqual((lock / "holder_pid").read_text(), str(phase_15.pid))

            _ = (self.root / "release").touch()
            self.assertEqual(phase_15.wait(timeout=300), 0)
            self.assertEqual(phase_16.wait(timeout=300), 0, phase_16_errors.read_text())
            self.assertNotIn("Traceback", phase_16_errors.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertFalse(lock.exists())
            starts = [row for row in self.phase_waits(board) if row["event_type"] == "wait_started"]
            ends = [row for row in self.phase_waits(board) if row["event_type"] == "wait_finished"]
            self.assertIn("memory admission", {row["wait_reason"] for row in starts})
            self.assertEqual({row["wait_id"] for row in starts}, {row["wait_id"] for row in ends})
        finally:
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            _ = (self.root / "release").touch()
            for process in (phase_16, phase_15):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def test_a_step_waiting_for_a_busy_token_keeps_no_memory_reservation(self) -> None:
        # A reservation kept through the token wait promises memory no build
        # uses; with many such steps the gate held every step back. The waiter
        # gives the token back for its memory wait, a peer takes it, then the
        # waiter's gate admits it while the token is busy.
        board = self.root / "board"
        admission = self.root / "buildlog" / "admission"
        waiting = self.root / "waiting-for-token"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        peer_meminfo = self.root / "peer-meminfo"
        _ = peer_meminfo.write_text("MemAvailable: 50331648 kB\n")
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text("#!/bin/sh\n"
                             + 'if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then : > "$TEST_WAITING"; fi\n'
                             + 'exec "$TEST_REAL_SLEEP" "$@"\n')
        sleep.chmod(0o755)
        environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                       "UNIT_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1",
                       "TEST_REAL_SLEEP": real_sleep}
        output = self.root / "waiter.out"
        waiter = self.start_memory_waiter({**environment, "TEST_WAITING": str(waiting)}, output)
        holder: subprocess.Popen[bytes] | None = None
        try:
            holder = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"], cwd=self.root,
                                      env={**environment, "BUILDLOG_MEMINFO": str(peer_meminfo),
                                           "TEST_BLOCK": "1"},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            self.started_groups.add(holder.pid)
            self.wait_for_running(self.root / "running", holder)
            held = sorted(admission.glob("*.reservation"))
            self.assertEqual(len(held), 1, "the running step holds one reservation")
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            self.wait_for_running(waiting, waiter)
            self.assertEqual(sorted(admission.glob("*.reservation")), held)
            _ = (self.root / "release").touch()
            self.assertEqual(holder.wait(timeout=300), 0)
            self.assertEqual(waiter.wait(timeout=300), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertEqual(list(admission.glob("*.reservation")), [])
            # The wait after the memory wait shows in the log; on 2026-10-22 its
            # log ended on "starting anyway" for hours.
            text = output.read_text(errors="replace")
            self.assertIn("waiting for the cargo token since", text)
            self.assertIn("took the cargo token after 0 min", text)
        finally:
            _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
            _ = (self.root / "release").touch()
            for process in (waiter, holder):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def test_a_step_whose_call_is_gone_stops_waiting_for_the_token(self) -> None:
        # On 2026-10-20 a test killed a call's group and left its step, in a
        # group of its own, trying for the token for 45 h. The step gives the
        # token back for its memory wait, a peer takes it, and the call is
        # killed while its step waits for the token again.
        board = self.root / "board"
        waiting = self.root / "waiting-for-token"
        meminfo = Path(self.environment["BUILDLOG_MEMINFO"])
        _ = meminfo.write_text("MemAvailable: 0 kB\n")
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the token wait test needs sleep on PATH")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text("#!/bin/sh\n"
                             + 'if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then : > "$TEST_WAITING"; fi\n'
                             + 'exec "$TEST_REAL_SLEEP" "$@"\n')
        sleep.chmod(0o755)
        environment = {**self.environment, "PLAN_DELEGATE_BOARD_DIR": str(board),
                       "PLAN_DELEGATE_TEAM_ROLE": "impl", "BUILDLOG_MEM_POLL_S": "1",
                       "TEST_REAL_SLEEP": real_sleep, "TEST_WAITING": str(waiting)}
        output = self.root / "waiter.out"
        call = self.start_memory_waiter(environment, output)
        peer = subprocess.Popen([real_sleep, "600"], start_new_session=True)
        self.started_groups.add(peer.pid)
        self.addCleanup(peer.wait, 300)
        taken = subprocess.run(["bash", str(BOARD), "acquire", str(board), "peer", "cargo", "--pid", str(peer.pid),
                                "--hold", "3600", "--wait", "0"],
                               capture_output=True, text=True, check=False, timeout=300)
        self.assertEqual(taken.returncode, 0, taken.stderr)
        _ = meminfo.write_text("MemAvailable: 50331648 kB\n")
        self.wait_for_running(waiting, call)
        os.kill(call.pid, signal.SIGKILL)
        _ = call.wait(timeout=300)
        deadline = time.monotonic() + 60
        while (left := self.live_group_processes(call.pid)) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(left, [], output.read_text(errors="replace"))
        self.assertIn("the call that started this step is gone", output.read_text(errors="replace"))

    def test_a_token_wait_keeps_the_steps_place_and_never_starts_without_memory(self) -> None:
        # On 2026-10-21 a step waited 15 min for memory, 24 min for the token,
        # then 15 min for memory again. No pass starts a step without memory now:
        # the first poll sleep frees memory, the step's token try fails, and the
        # pass after the token wait admits at once, in the first pass's place.
        # The board wrapper makes the step's one try for the token fail, and
        # logs each other acquire next to the sleep calls.
        home = self.root / "busy-home"
        delegate = home / ".claude/scripts/direct"
        delegate.mkdir(parents=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "direct":
                (delegate.parent / source.name).symlink_to(source, target_is_directory=source.is_dir())
        for source in VERIFY.parent.iterdir():
            if source.name != "board.sh":
                (delegate / source.name).symlink_to(source, target_is_directory=source.is_dir())
        _ = (delegate / "board.sh").write_text(
            '#!/bin/bash\n'
            + 'if [[ "$1" == acquire && " $* " == *" --wait 0 "* ]]; then exit 1; fi\n'
            + 'if [[ "$1" == acquire ]]; then printf "token\\n" >> "$TEST_SLEEP_LOG"; fi\n'
            + 'exec bash "$TEST_REAL_BOARD" "$@"\n')
        real_sleep = shutil.which("sleep")
        if real_sleep is None:
            self.fail("the memory wait test needs sleep on PATH")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('#!/bin/sh\nprintf "sleep %s\\n" "$*" >> "$TEST_SLEEP_LOG"\n'
                             + 'printf "MemAvailable: 67108864 kB\\n" > "$BUILDLOG_MEMINFO"\n'
                             + 'exec "$TEST_REAL_SLEEP" "$@"\n')
        sleep.chmod(0o755)
        _ = Path(self.environment["BUILDLOG_MEMINFO"]).write_text("MemAvailable: 0 kB\n")
        log = self.root / "sleep-log"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_REAL_SLEEP": real_sleep, "TEST_SLEEP_LOG": str(log),
                       "BUILDLOG_MEM_POLL_S": "1", "BUILDLOG_MEM_WAIT_LIMIT_S": "2"}
        result = self.verify(environment)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("memory wait limit reached", output)
        self.assertNotIn("starting anyway", output)
        lines = log.read_text().splitlines()
        self.assertIn("token", lines)
        # The step's acquire after its failed try is the last one in the log.
        last_token = len(lines) - 1 - lines[::-1].index("token")
        self.assertIn("sleep 1", lines[:last_token], "the first memory wait polls until memory is free")
        self.assertNotIn("sleep 1", lines[last_token:], "the pass after the token wait waited again")

    def build_stub(self, targets: list[dict[str, object]]) -> dict[str, str]:
        """A cargo whose metadata lists `targets` for sample and whose build makes each bin and example."""
        metadata = json.dumps({"packages": [{"name": "sample", "targets": targets, "features": {}}],
                               "target_directory": str(self.root / "target")})
        cargo = self.root / "stubs" / "cargo"
        _ = cargo.write_text("#!/bin/sh\n"
                             + 'if [ "$1" = metadata ]; then printf "%s\\n" "$TEST_METADATA"; exit 0; fi\n'
                             + 'printf "%s\\n" "$*" >> "$CARGO_CALLS"\n'
                             + 'profile=debug; kind=""\n'
                             + 'for word in "$@"; do case "$word" in --release) profile=release ;; esac; done\n'
                             + 'for word in "$@"; do\n'
                             + '    case "$kind" in\n'
                             + '        bin) mkdir -p "$CARGO_TARGET_DIR/$profile"; : > "$CARGO_TARGET_DIR/$profile/$word"; chmod +x "$CARGO_TARGET_DIR/$profile/$word" ;;\n'
                             + '        example) mkdir -p "$CARGO_TARGET_DIR/$profile/examples"; : > "$CARGO_TARGET_DIR/$profile/examples/$word"; chmod +x "$CARGO_TARGET_DIR/$profile/examples/$word" ;;\n'
                             + '    esac\n'
                             + '    case "$word" in --bin) kind=bin ;; --example) kind=example ;; *) kind="" ;; esac\n'
                             + 'done\n')
        return {**self.environment, "TEST_METADATA": metadata,
                "PLAN_DELEGATE_BOARD_DIR": str(self.root / "board"), "PLAN_DELEGATE_TEAM_ROLE": "impl"}

    def build(self, environment: dict[str, str], *words: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(VERIFY), "build", "sample", *words], cwd=self.root, env=environment,
                              capture_output=True, text=True, check=False, timeout=300)

    def test_build_makes_the_packages_binaries_under_the_token_and_prints_their_paths(self) -> None:
        # A smoke or live-shot helper ran raw `cargo build` before it launched
        # the app, and the user approved it by hand (2026-10-23).
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]},
                                       {"name": "sample-app", "kind": ["bin"]},
                                       {"name": "demo", "kind": ["example"]}])
        result = self.build(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"binary: {self.root / 'target' / 'debug' / 'sample-app'}\n", result.stdout)
        self.assertNotIn("demo", result.stdout)
        self.assertEqual((self.root / "cargo-calls").read_text(), "build --workspace --bin sample-app\n")
        self.assertIn("claim: token cargo acquired", (self.root / "board" / "board.log").read_text())
        self.assertFalse((self.root / "board" / "locks" / "cargo.d").exists())

    def test_check_keep_going_reaches_cargo_in_either_order_with_features(self) -> None:
        # A migration phase that fixes only some crates reads every crate's result (hana, 2026-10-26).
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]}])
        for words, expected in ((("--keep-going",), "check --workspace --lib --bins --keep-going\n"),
                                (("--features", "fast", "--keep-going"),
                                 "check --workspace --lib --bins --keep-going --features sample/fast\n"),
                                (("--keep-going", "--features", "fast"),
                                 "check --workspace --lib --bins --keep-going --features sample/fast\n"),
                                ((), "check --workspace --lib --bins\n")):
            with self.subTest(words=words):
                (self.root / "cargo-calls").unlink(missing_ok=True)
                result = subprocess.run(["bash", str(VERIFY), "check", "sample", *words], cwd=self.root,
                                        env=environment, capture_output=True, text=True, check=False, timeout=300)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((self.root / "cargo-calls").read_text(), expected)

    def test_build_of_an_example_turns_on_its_required_features(self) -> None:
        environment = self.build_stub([{"name": "sample-app", "kind": ["bin"]},
                                       {"name": "demo", "kind": ["example"], "required-features": ["extra"]}])
        result = self.build(environment, "--example", "demo", "--release", "--features", "fast")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "cargo-calls").read_text(),
                         "build --workspace --example demo --release --features sample/fast,sample/extra\n")
        self.assertIn(f"binary: {self.root / 'target' / 'release' / 'examples' / 'demo'}\n", result.stdout)

    def test_build_refuses_a_package_with_no_binary_and_a_target_it_lacks(self) -> None:
        environment = self.build_stub([{"name": "sample", "kind": ["lib"]}])
        result = self.build(environment)
        self.assertEqual(result.returncode, 2)
        self.assertIn("package sample has no binary; name an example with --example <name>", result.stderr)
        result = self.build(environment, "--bin", "other")
        self.assertEqual(result.returncode, 2)
        self.assertIn("package sample has no bin other", result.stderr)
        result = self.build(environment, "--bin", "a", "--example", "b")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "cargo-calls").exists())

    def hold_environment(self, board: Path, wait_status: int) -> dict[str, str]:
        """A held natedev for the step's hold wait only.

        verify.sh's first wait-local, before it takes the token, returns at once;
        the step's wait-local marks `hold-waiting`, then waits for `hold-release`
        and exits with `wait_status`. Every other verb succeeds.
        """
        holds = self.root / "build-hold"
        holds.mkdir(exist_ok=True)
        _ = (holds / "holder.json").write_text("{}")
        stub = self.root / "build_targets_stub.py"
        _ = stub.write_text(r"""import os, sys, time
from pathlib import Path
root = Path(os.environ["TEST_HOLD_ROOT"])
if sys.argv[1] == "wait-local":
    calls = root / "wait-local-calls"
    count = len(calls.read_text().splitlines()) if calls.exists() else 0
    with calls.open("a") as stream:
        _ = stream.write("wait-local\n")
    if count >= 1:
        (root / "hold-waiting").touch()
        while not (root / "hold-release").exists():
            time.sleep(0.01)
        raise SystemExit(int(os.environ["TEST_HOLD_STATUS"]))
""")
        return {**self.environment, "UNIT_RUN_DIR": str(board),
                "UNIT_TEAM_ROLE": "impl", "BUILD_TARGETS_SCRIPT": str(stub),
                "TEST_HOLD_ROOT": str(self.root), "TEST_HOLD_STATUS": str(wait_status)}

    def start_hold_waiter(self, environment: dict[str, str], output: Path) -> subprocess.Popen[bytes]:
        with output.open("wb") as stream:
            process = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                       cwd=self.root, env=environment, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(process.pid)
        self.wait_for_running(self.root / "hold-waiting", process)
        return process

    def test_a_hold_wait_gives_the_token_to_a_peer_and_takes_it_back_before_its_step(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        environment = self.hold_environment(board, 0)
        output = self.root / "held.out"
        held = self.start_hold_waiter(environment, output)
        peer: subprocess.Popen[bytes] | None = None
        try:
            self.assertFalse(lock.exists(), "the hold wait kept the token")
            peer_holds = self.root / "peer-holds"
            peer_holds.mkdir()
            peer = subprocess.Popen(["bash", str(VERIFY), "check", "sample", "--no-cache"],
                                    cwd=self.root,
                                    env={**environment, "BUILD_HOLD_DIR": str(peer_holds), "TEST_BLOCK": "1"},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            self.started_groups.add(peer.pid)
            self.wait_for_running(self.root / "running", peer)
            self.assertEqual((lock / "holder_pid").read_text(), str(peer.pid))

            (self.root / "hold-release").touch()
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)
            _ = (self.root / "release").touch()
            self.assertEqual(peer.wait(timeout=300), 0)
            self.assertEqual(held.wait(timeout=300), 0, output.read_text())
            self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 2)
            self.assertFalse(lock.exists())
        finally:
            for marker in ("hold-release", "release"):
                (self.root / marker).touch()
            for process in (held, peer):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def test_a_hold_wait_that_leaves_for_the_mac_does_not_take_the_token_again(self) -> None:
        board = self.root / "board"
        environment = self.hold_environment(board, 75)
        output = self.root / "held.out"
        held = self.start_hold_waiter(environment, output)
        try:
            (self.root / "hold-release").touch()
            _ = held.wait(timeout=300)
        finally:
            if held.poll() is None:
                self.end_group(held.pid)
                _ = held.wait(timeout=300)
        log = (board / "board.log").read_text()
        self.assertEqual(log.count("token cargo acquired"), 1, log)
        self.assertFalse((board / "locks" / "cargo.d").exists())
        self.assertFalse((self.root / "cargo-calls").exists(), "the step ran on natedev")

    def test_no_hold_gives_nothing_back(self) -> None:
        board = self.root / "board"
        (self.root / "build-hold").mkdir()
        result = self.verify({**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "impl"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = (board / "board.log").read_text()
        # The first take, then the memory gate's give-back and take; the hold wait adds none.
        self.assertEqual((log.count("token cargo acquired"), log.count("token cargo released")), (2, 2), log)

    def start_check(self, environment: dict[str, str]) -> subprocess.Popen[str]:
        process = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root, env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        self.started_groups.add(process.pid)
        return process

    def wait_for_waiter(self, board: Path, process: subprocess.Popen[str]) -> None:
        """Waits until the call's own file is in the board's token waiter list."""
        waiters = board / "token_waiters"
        waiters.mkdir(parents=True, exist_ok=True)

        def listed() -> bool:
            return any(path.name.endswith(f".{process.pid}") for path in waiters.iterdir())
        wait_until(lambda: listed() or process.poll() is not None, waiters, processes=(process,))
        self.assertTrue(listed(), "verify never waited for the token")

    def test_a_newer_call_supersedes_an_older_call_still_waiting_for_the_token(self) -> None:
        board = self.root / "board"
        shared = {**self.environment, "UNIT_RUN_DIR": str(board)}
        holder = self.start_check({**shared, "UNIT_TEAM_ROLE": "peer", "TEST_BLOCK": "1"})
        try:
            self.wait_for_running(self.root / "running", holder)
            older = self.start_check({**shared, "UNIT_TEAM_ROLE": "impl"})
            self.wait_for_waiter(board, older)
            newer = self.start_check({**shared, "UNIT_TEAM_ROLE": "impl"})
            older_output, older_errors = older.communicate(timeout=300)
            self.assertEqual(older.returncode, 76, older_output + older_errors)
            self.assertIn("superseded by a newer check call", older_output)
            self.assertIn("nothing needs a repair", older_output)
            self.assertIsNone(newer.poll(), "the newer call must still wait for the peer's token")
            _ = (self.root / "release").touch()
            newer_output = newer.communicate(timeout=300)
            self.assertEqual(newer.returncode, 0, newer_output)
            holder_output = holder.communicate(timeout=300)
            self.assertEqual(holder.returncode, 0, holder_output)
        finally:
            _ = (self.root / "release").touch()
        superseded = [record for record in self.records() if f"-{older.pid}-" in cast(str, record["id"])]
        self.assertEqual([(record["outcome"], record["status"]) for record in superseded], [("superseded", 76)])
        self.assertEqual(list((board / "token_waiters").iterdir()), [])

    def test_a_call_that_holds_the_token_runs_on_when_a_newer_call_starts(self) -> None:
        board = self.root / "board"
        shared = {**self.environment, "UNIT_RUN_DIR": str(board), "UNIT_TEAM_ROLE": "impl"}
        holder = self.start_check({**shared, "TEST_BLOCK": "1"})
        try:
            self.wait_for_running(self.root / "running", holder)
            newer = self.start_check(shared)
            self.wait_for_waiter(board, newer)
            self.assertIsNone(holder.poll(), "the call that holds the token must run on")
            _ = (self.root / "release").touch()
            holder_output = holder.communicate(timeout=300)
            self.assertEqual(holder.returncode, 0, holder_output)
            newer_output = newer.communicate(timeout=300)
            self.assertEqual(newer.returncode, 0, newer_output)
        finally:
            _ = (self.root / "release").touch()
        self.assertEqual(sorted(cast(str, record["outcome"]) for record in self.records()), ["ran", "ran"])

    def test_the_token_goes_to_the_call_that_waited_longest(self) -> None:
        # On 2026-10-22 about 25 calls polled one board's token, any of them
        # took it when it came free, and two calls waited 3 and 3.5 h. A call
        # now waits while an older call that still runs verify.sh waits.
        board = self.root / "board"
        waiters = board / "token_waiters"
        waiters.mkdir(parents=True)
        older = subprocess.Popen(["bash", "-c", "sleep 600; :", "verify.sh"], start_new_session=True)
        self.started_groups.add(older.pid)
        older_entry = waiters / f"peer.0123456789abcdef.1000000.{older.pid}.again"
        older_entry.touch()
        output = self.root / "queued.out"
        with output.open("wb") as stream:
            call = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                    env={**self.environment, "UNIT_RUN_DIR": str(board),
                                         "UNIT_TEAM_ROLE": "impl"},
                                    stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.started_groups.add(call.pid)
        try:
            self.wait_for_output(output, "calls ahead of this one: 1", call)
            self.assertIn("it is free and goes to an earlier call", output.read_text())
            self.assertFalse((board / "locks" / "cargo.d").exists(), "the newer call took the token")
            self.end_group(older.pid)
            _ = older.wait(timeout=300)
            self.assertEqual(call.wait(timeout=300), 0, output.read_text())
        finally:
            for process in (older, call):
                if process.poll() is None:
                    self.end_group(process.pid)
                _ = process.wait(timeout=300)
        self.assertIn("took the cargo token after 0 min", output.read_text())
        self.assertEqual(list(waiters.iterdir()), [], "an ended waiter's file stays in the queue")
        self.assertEqual((self.root / "cargo-calls").read_text().count("check"), 1)

    def test_a_long_token_wait_never_gives_up(self) -> None:
        # Each refused acquire moves the clock past the old 1800 s limit.
        home = self.root / "busy-home"
        direct_dir = home / ".claude/scripts/direct"
        direct_dir.mkdir(parents=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "direct":
                (direct_dir.parent / source.name).symlink_to(source, target_is_directory=source.is_dir())
        for source in VERIFY.parent.iterdir():
            if source.name != "board.sh":
                (direct_dir / source.name).symlink_to(source, target_is_directory=source.is_dir())
        _ = (direct_dir / "board.sh").write_text('''#!/bin/bash
if [[ "$1" == acquire ]]; then
    count=$(( $(cat "$TEST_ACQUIRES" 2>/dev/null || echo 0) + 1 ))
    printf '%s\\n' "$count" > "$TEST_ACQUIRES"
    if (( count <= 3 )); then
        printf '%s\\n' $(( $(cat "$UNIT_TOKEN_CLOCK_FILE") + 1800 )) > "$UNIT_TOKEN_CLOCK_FILE"
        echo "busy cargo held by peer" >&2
        exit 1
    fi
fi
exec bash "$TEST_REAL_BOARD" "$@"
''')
        clock = self.root / "busy-clock"
        _ = clock.write_text("100\n")
        acquires = self.root / "acquires"
        environment = {**self.environment, "HOME": str(home),
                       "UNIT_RUN_DIR": str(self.root / "busy-board"),
                       "UNIT_TEAM_ROLE": "impl", "TEST_REAL_BOARD": str(BOARD),
                       "TEST_ACQUIRES": str(acquires), "UNIT_TOKEN_CLOCK_FILE": str(clock)}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("did not get it", result.stderr)
        self.assertGreaterEqual(int(acquires.read_text()), 4)
        self.assertEqual(self.records()[0]["token_wait_s"], 5400)
        self.assertFalse((self.root / "busy-board" / "locks" / "cargo.d").exists())

    def test_sigterm_during_the_token_wait_exits_at_once_and_leaves_no_board_entry(self) -> None:
        board = self.root / "board"
        lock = board / "locks" / "cargo.d"
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env={**self.environment, "UNIT_RUN_DIR": str(board),
                                      "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self.started_groups.add(first.pid)
        second: subprocess.Popen[bytes] | None = None
        try:
            self.wait_for_running(self.root / "running", first)
            second = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                      env={**self.environment, "UNIT_RUN_DIR": str(board),
                                           "UNIT_TEAM_ROLE": "second"},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            self.started_groups.add(second.pid)
            waiting = second

            def acquiring() -> bool:
                for pid in self.live_group_processes(waiting.pid):
                    try:
                        words = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                    except OSError:
                        continue
                    if b"acquire" in words and b"second" in words:
                        return True
                return waiting.poll() is not None

            deadline = time.monotonic() + 300
            while not acquiring() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsNone(second.poll(), "second verify exited before it waited for the token")

            # The signal goes to verify alone, as a caller's kill would.
            os.kill(second.pid, signal.SIGTERM)
            self.assertEqual(second.wait(timeout=10), -signal.SIGTERM)
            deadline = time.monotonic() + 10
            while self.live_group_processes(second.pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.live_group_processes(second.pid), [],
                             "the token wait outlived its verify")
            self.assertEqual((lock / "holder_pid").read_text(), str(first.pid))

            _ = (self.root / "release").touch()
            self.assertEqual(first.wait(timeout=300), 0)
            self.assertFalse(lock.exists())
            self.assertNotIn("[second] claim", (board / "board.log").read_text())
        finally:
            _ = (self.root / "release").touch()
            for process in (second, first):
                if process is not None:
                    if process.poll() is None:
                        self.end_group(process.pid)
                    _ = process.wait(timeout=300)

    def assert_interrupted_holder_stops_step_before_release(self, interruption: signal.Signals) -> None:
        board = self.root / "board"
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1"}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_RECLAIM_CHECK": "1"}
        first = subprocess.Popen(["bash", str(VERIFY), "check", "sample"], cwd=self.root,
                                 env=first_environment, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 preexec_fn=restore_sigint)
        self.started_groups.add(first.pid)
        try:
            self.wait_for_running((self.root / "running"), first)
            self.assertTrue((self.root / "running").exists())
            lock = board / "locks" / "cargo.d"
            step_record = cast(dict[str, object], json.loads((lock / "owned").read_text()))
            step_group = cast(int, step_record["group"])
            self.started_groups.add(step_group)

            os.killpg(first.pid, interruption)
            _ = first.wait(timeout=300)
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse(lock.exists(), "interrupted verify kept its token")
            self.assertEqual(self.live_group_processes(step_group), [],
                             "token became free while its step still ran")
            second = self.verify(second_environment)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        finally:
            _ = (self.root / "release").touch()
            self.end_group(first.pid)
            _ = first.wait(timeout=300)

    def test_sigterm_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGTERM)

    def test_sigint_stops_step_before_token_release(self) -> None:
        self.assert_interrupted_holder_stops_step_before_release(signal.SIGINT)

    def detach_environment(self, status: int) -> dict[str, str]:
        """A Codex worker's environment whose mesh_runs.py stub records its argv and exits `status`."""
        stub = self.root / "mesh_runs_stub.py"
        _ = stub.write_text("import json, os, sys\n"
                            + "with open(os.environ['TEST_MESH_RUNS_CALLS'], 'a', encoding='utf-8') as calls:\n"
                            + "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                            + "raise SystemExit(int(os.environ['TEST_MESH_RUNS_STATUS']))\n")
        return {**self.environment, "CODEX_THREAD_ID": "thread-worker", "VERIFY_MESH_RUNS_SCRIPT": str(stub),
                "TEST_MESH_RUNS_CALLS": str(self.root / "mesh-runs-calls"), "TEST_MESH_RUNS_STATUS": str(status)}

    def mesh_runs_calls(self) -> list[list[str]]:
        path = self.root / "mesh-runs-calls"
        if not path.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_a_codex_worker_call_detaches_and_returns_without_building(self) -> None:
        result = self.verify(self.detach_environment(0))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mesh_runs_calls(), [[
            "start", "--label", "verify.sh check sample", "--",
            "bash", str(VERIFY.resolve()), "check", "sample",
        ]])
        self.assertFalse((self.root / "cargo-calls").exists())
        self.assertEqual(self.records(), [])

    def test_a_caller_mesh_runs_cannot_detach_runs_attached(self) -> None:
        for status, note in ((3, False), (1, True)):
            with self.subTest(status=status):
                for path in (self.root / "mesh-runs-calls", self.root / "cargo-calls"):
                    path.unlink(missing_ok=True)
                result = self.verify(self.detach_environment(status))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.mesh_runs_calls()), 1)
                self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))
                self.assertEqual("could not detach" in result.stderr, note, result.stderr)

    def test_the_detached_run_itself_does_not_detach_again(self) -> None:
        environment = {**self.detach_environment(0), "UNIT_DETACHED_RUN": "1"}
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.mesh_runs_calls(), [])
        self.assertTrue((self.root / "cargo-calls").read_text().startswith("check "))


if __name__ == "__main__":
    _ = unittest.main()
