"""Real verify.sh token wait behavior."""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from typing import cast
from scripts.direct.verify_token_wait_test_support import VerifyTokenWaitFixture, VERIFY, BOARD


class VerifyTokenWaitTests(VerifyTokenWaitFixture):
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
        # Advance Bash's elapsed clock at the two reads, without the file
        # override. A real second passing is irrelevant to the subtraction.
        clock_reads = self.root / "elapsed-clock-reads"
        shell = self.root / "elapsed-clock.sh"
        _ = shell.write_text('''set -T
advance_elapsed_clock() {
    if [[ "$BASH_COMMAND" == 'TOKEN_CLOCK_NOW=$SECONDS' ]]; then
        if [[ -e "$TEST_CLOCK_READS" ]]; then SECONDS=120; else SECONDS=100; fi
        : > "$TEST_CLOCK_READS"
    fi
}
trap advance_elapsed_clock DEBUG
''')
        environment = {**self.environment, "BASH_ENV": str(shell),
                       "TEST_CLOCK_READS": str(clock_reads),
                       "UNIT_RUN_DIR": str(self.root / "clock-board"),
                       "UNIT_TEAM_ROLE": "clock"}
        for key in ("UNIT_WORKER_POLL_SECS", "UNIT_WORKER_WAIT_ATTEMPTS",
                    "UNIT_EARLY_POLL_SECS", "UNIT_TOKEN_CLOCK_FILE"):
            _ = environment.pop(key, None)
        result = self.verify(environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.records()[0]["token_wait_s"], 20)

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
        waiting = self.root / "waiting-for-token"
        resume = self.root / "resume-acquisition"
        first_clock = self.root / "first-clock"
        second_clock = self.root / "second-clock"
        _ = first_clock.write_text("1000\n")
        _ = second_clock.write_text("1000\n")
        sleep = self.root / "stubs" / "sleep"
        _ = sleep.write_text('''#!/bin/sh
if [ "$1" = 3 ] && [ -n "${TEST_WAITING:-}" ]; then
    : > "$TEST_WAITING"
    exec python3 "$TEST_WAIT_HELPER" "$TEST_WAIT_GATE"
fi
exit 0
''')
        sleep.chmod(0o755)
        first_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                             "UNIT_TEAM_ROLE": "first", "TEST_BLOCK": "1",
                             "UNIT_TOKEN_CLOCK_FILE": str(first_clock)}
        second_environment = {**self.environment, "UNIT_RUN_DIR": str(board),
                              "UNIT_TEAM_ROLE": "second", "TEST_WAITING": str(waiting),
                              "TEST_WAIT_GATE": str(resume),
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
