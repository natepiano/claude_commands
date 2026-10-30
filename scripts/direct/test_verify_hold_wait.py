"""Real verification hold waits: wait."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import cast

from scripts.lib import process_start
from scripts.lib.process_start_test_support import write_stat
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_hold_wait_test_support import (
    TEST_HANG_GUARD_SECS,
    VERIFY,
    VerifyHoldWaitFixture,
    WaitObservation,
    grant_call,
    read_cycle_file as read_cycle_file,
    release_registered_call as release_registered_call,
)


class VerifyHoldWaitTests(VerifyHoldWaitFixture):
    def recovery_root(self) -> Path:
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        tree_key = hashlib.sha256(str(self.fixture.root.resolve()).encode()).hexdigest()
        return session / "verify_waits" / tree_key

    def stopped_owner_pid(self) -> int:
        with subprocess.Popen([sys.executable, "-c", "pass"]) as owner:
            _ = owner.wait(timeout=TEST_HANG_GUARD_SECS)
            return owner.pid

    def start_saved_wait(self) -> str:
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        result = invoke_recorder(
            ["start-wait", "--session-dir", str(session), "--worker-thread-id", "thread-impl",
             "--kind", "WaitingForMemoryOrSlot", "--reason", "build hold"],
            self.fixture.environment, self.fixture.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_wait_recovery_keeps_unknown_owners_and_reclaims_only_proven_stale_ones(self) -> None:
        history = self.record_phase()
        start = process_start.process_start(os.getpid())
        if not isinstance(start, process_start.ProcessStartTime):
            self.skipTest("fake proc checks need Linux ticks")
        for outcome in ("unknown", "replaced", "gone"):
            with self.subTest(outcome=outcome):
                wait_id = self.start_saved_wait()
                directory = self.recovery_root() / f"verify-hold.{os.getpid()}.{outcome}"
                directory.mkdir(parents=True)
                saved = start.ticks + 1 if outcome == "unknown" else start.ticks
                _ = (directory / "owner.json").write_text(json.dumps({
                    "pid": os.getpid(), "start_time": str(saved),
                }))
                _ = (directory / "phase-wait.stopped.hold").write_text(wait_id + "\n")
                proc_root = self.fixture.root / f"proc-{outcome}"
                proc_root.mkdir()
                if outcome == "unknown":
                    (proc_root / str(os.getpid()) / "stat").mkdir(parents=True)
                elif outcome == "replaced":
                    _ = write_stat(proc_root, os.getpid(), start_ticks=start.ticks + 1)
                observer = self.fixture.root / "wait-owner-proc-root"
                self.fixture.write_executable(observer, f'''import os, sys
if len(sys.argv) > 2 and sys.argv[1] == "-c" and 'owner.json' in sys.argv[2]:
    os.environ["VERIFY_PROC_ROOT"] = {str(proc_root)!r}
else:
    os.environ.pop("VERIFY_PROC_ROOT", None)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
                result = self.fixture.verify("test", "sample", "--no-cache", "--local", "owner recovery",
                                             "--filter", "smoke", extra={"CLAUDE_PYTHON": str(observer)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                finishes = [row for row in rows if row["event_type"] == "wait_finished" and row["wait_id"] == wait_id]
                if outcome == "unknown":
                    self.assertTrue(directory.is_dir())
                    self.assertEqual(finishes, [])
                else:
                    self.assertFalse(directory.exists())
                    self.assertEqual(len(finishes), 1)

    def test_a_scan_leaves_an_unpublished_calls_folder_alone(self) -> None:
        history = self.record_phase()
        ready = self.fixture.root / "unpublished-folder"
        resume = self.fixture.root / "resume-publication"
        os.mkfifo(resume)
        observer = self.fixture.root / "pause-owner-publication"
        self.fixture.write_executable(observer, f'''import os, sys
from pathlib import Path
if len(sys.argv) > 4 and sys.argv[1] == "-c" and sys.argv[3] == "record":
    Path({str(ready)!r}).write_text(sys.argv[4])
    print("owner publication paused", file=sys.stderr, flush=True)
    with open({str(resume)!r}) as release:
        release.read(1)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        arguments = ("test", "sample", "--no-cache", "--local", "publication", "--filter", "smoke")
        first = self.start(*arguments)
        self.wait_for_call(first, "owner publication paused")
        unpublished = Path(ready.read_text())
        self.fixture.environment["CLAUDE_PYTHON"] = sys.executable
        try:
            scanner = self.fixture.verify(*arguments)
            kept = unpublished.is_dir()
            published_early = list(self.recovery_root().glob(f"verify-hold.{first.pid}.*"))
            before = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
            self.hold()
        finally:
            with resume.open("w") as release:
                _ = release.write("resume")
        if kept:
            self.wait_for_call(first)
            grant_call(self.cycle_file)
        code, output = self.finish(first)
        self.assertEqual(scanner.returncode, 0, scanner.stdout + scanner.stderr)
        self.assertTrue(kept, "the concurrent scan removed an unpublished live call")
        self.assertEqual(published_early, [], "the call published its folder before its owner")
        self.assertFalse(any(row["event_type"] == "wait_started" for row in before))
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started, = [row for row in rows if row["event_type"] == "wait_started" and row["wait_reason"] == "build hold"]
        self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                              and row["wait_id"] == started["wait_id"]]), 1, rows)
        self.assertFalse(unpublished.exists())

    def test_rerun_recovers_legacy_waits_only_after_the_owner_exits(self) -> None:
        history = self.record_phase()
        arguments = ("test", "sample", "--no-cache", "--local", "legacy owner", "--filter", "smoke")
        for owner_state in ("missing", "empty", "unreadable"):
            for retry in (False, True):
                with self.subTest(owner_state=owner_state, retry=retry):
                    wait_id = self.start_saved_wait()
                    owner = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                             stdin=subprocess.PIPE, text=True)
                    try:
                        directory = self.recovery_root() / f"verify-hold.{owner.pid}.legacy"
                        directory.mkdir(parents=True)
                        wait_file = directory / "phase-wait.stopped.hold"
                        _ = wait_file.write_text(wait_id + "\n")
                        if owner_state == "empty":
                            _ = (directory / "owner.json").write_text("")
                        elif owner_state == "unreadable":
                            (directory / "owner.json").mkdir()
                        if retry:
                            (directory / "retry").touch()
                        live_result = self.fixture.verify(*arguments)
                        kept = wait_file.exists()
                        live_rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                    finally:
                        _ = owner.communicate(input="stop", timeout=TEST_HANG_GUARD_SECS)
                    dead_result = self.fixture.verify(*arguments)
                    rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                    self.assertEqual(live_result.returncode, 0, live_result.stdout + live_result.stderr)
                    self.assertTrue(kept, "an incomplete owner record does not prove that its pid is gone")
                    self.assertFalse(any(row["event_type"] == "wait_finished" and row["wait_id"] == wait_id
                                         for row in live_rows))
                    self.assertEqual(dead_result.returncode, 0, dead_result.stdout + dead_result.stderr)
                    self.assertFalse(directory.exists())
                    self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                                          and row["wait_id"] == wait_id]), 1, rows)

    def test_a_scan_removes_only_staging_folders_with_a_known_dead_owner(self) -> None:
        history = self.record_phase()
        root = self.recovery_root()
        root.mkdir(parents=True)
        dead_pid = self.stopped_owner_pid()
        start = process_start.process_start(os.getpid())
        self.assertIsInstance(start, process_start.ProcessStartTime)
        if not isinstance(start, process_start.ProcessStartTime):
            self.fail("this staging test needs Linux ticks")
        start_time = start.text
        dead = root / f".verify-hold-new.{dead_pid}.dead"
        reused = root / f".verify-hold-new.{os.getpid()}.reused"
        live = root / f".verify-hold-new.{os.getpid()}.live"
        ownerless = root / f".verify-hold-new.{dead_pid}.ownerless"
        for directory, pid, identity in ((dead, dead_pid, "stopped"),
                                         (reused, os.getpid(), str(start.ticks + 1)),
                                         (live, os.getpid(), start_time)):
            directory.mkdir()
            _ = (directory / "owner.json").write_text(json.dumps({"pid": pid, "start_time": identity}))
        ownerless.mkdir()
        result = self.fixture.verify("test", "sample", "--no-cache", "--local", "staging recovery", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(dead.exists())
        self.assertFalse(reused.exists())
        self.assertTrue(live.is_dir())
        self.assertTrue(ownerless.is_dir())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertFalse(any(row["event_type"] == "wait_started" for row in rows))

    def test_a_wait_file_removed_by_another_scanner_is_already_recovered(self) -> None:
        history = self.record_phase()
        wait_id = self.start_saved_wait()
        directory = self.recovery_root() / f"verify-hold.{self.stopped_owner_pid()}.stopped"
        directory.mkdir(parents=True)
        wait_file = directory / "phase-wait.stopped.hold"
        _ = wait_file.write_text(wait_id + "\n")
        injection = self.fixture.root / "remove-before-wait-read.sh"
        removed = self.fixture.root / "wait-removed-by-scanner"
        _ = injection.write_text('''set -T
trap 'if [[ "$BASH_COMMAND" == *"read -r id"* && "${file:-}" == "$TEST_VANISH_WAIT_FILE" ]]; then
    trap - DEBUG
    "$TEST_REAL_PYTHON" "$TEST_WAIT_RECORDER" finish-wait --session-dir "$UNIT_RUN_DIR" --wait-id "$TEST_VANISH_WAIT_ID"
    rm -f "$TEST_VANISH_WAIT_FILE"
    printf removed > "$TEST_VANISH_MARKER"
fi' DEBUG
''')
        result = self.fixture.verify("test", "sample", "--no-cache", "--local", "concurrent recovery", "--filter", "smoke",
                                     extra={"BASH_ENV": str(injection), "TEST_VANISH_WAIT_FILE": str(wait_file),
                                            "TEST_REAL_PYTHON": sys.executable,
                                            "TEST_WAIT_RECORDER": str(VERIFY.with_name("progress_history.py")),
                                            "TEST_VANISH_WAIT_ID": wait_id, "TEST_VANISH_MARKER": str(removed)})
        self.assertEqual(removed.read_text(), "removed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("No such file", result.stdout + result.stderr)
        self.assertNotIn("NotRecorded", result.stdout + result.stderr)
        self.assertFalse(directory.exists())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                              and row["wait_id"] == wait_id]), 1, rows)

    def test_rerun_removes_incomplete_owner_folders_without_wait_ids(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        tree_key = hashlib.sha256(str(self.fixture.root.resolve()).encode()).hexdigest()
        dead_pid = self.stopped_owner_pid()
        arguments = ("test", "sample", "--no-cache", "--local", "owner recovery", "--filter", "smoke")
        for owner_state in ("empty", "missing", "unreadable"):
            with self.subTest(owner_state=owner_state):
                abandoned = session / "verify_waits" / tree_key / f"verify-hold.{dead_pid}.stopped-{owner_state}"
                abandoned.mkdir(parents=True)
                if owner_state == "empty":
                    _ = (abandoned / "owner.json").write_text("")
                elif owner_state == "unreadable":
                    (abandoned / "owner.json").mkdir()
                before = history.read_text().splitlines()
                self.hold()
                process = self.start(*arguments)
                self.wait_for_call(process)
                grant_call(self.cycle_file)
                code, output = self.finish(process)
                self.assertEqual(code, 0, output)
                self.assertFalse(abandoned.exists())
                rows = [cast(dict[str, object], json.loads(line))
                        for line in history.read_text().splitlines()[len(before):]]
                started, = [row for row in rows if row["event_type"] == "wait_started"
                            and row["wait_reason"] == "build hold"]
                self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                                      and row["wait_id"] == started["wait_id"]]), 1, rows)
                self.assertNotIn("an earlier wait still needs to close", output)

    def test_rerun_closes_saved_waits_with_incomplete_owner_records(self) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        tree_key = hashlib.sha256(str(self.fixture.root.resolve()).encode()).hexdigest()
        dead_pid = self.stopped_owner_pid()
        arguments = ("test", "sample", "--no-cache", "--local", "owner recovery", "--filter", "smoke")
        for owner_state in ("empty", "missing", "unreadable"):
            with self.subTest(owner_state=owner_state):
                result = invoke_recorder(
                    ["start-wait", "--session-dir", str(session), "--worker-thread-id", "thread-impl",
                     "--kind", "WaitingForMemoryOrSlot", "--reason", "build hold"],
                    self.fixture.environment, self.fixture.root,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                old_wait_id = result.stdout.strip()
                abandoned = session / "verify_waits" / tree_key / f"verify-hold.{dead_pid}.stopped-{owner_state}"
                abandoned.mkdir(parents=True)
                _ = (abandoned / "phase-wait.stopped.hold").write_text(old_wait_id + "\n")
                if owner_state == "empty":
                    _ = (abandoned / "owner.json").write_text("")
                elif owner_state == "unreadable":
                    (abandoned / "owner.json").mkdir()
                before = history.read_text().splitlines()
                self.hold()
                process = self.start(*arguments)
                self.wait_for_call(process)
                grant_call(self.cycle_file)
                code, output = self.finish(process)
                self.assertEqual(code, 0, output)
                self.assertFalse(abandoned.exists())
                rows = [cast(dict[str, object], json.loads(line))
                        for line in history.read_text().splitlines()[len(before):]]
                old_finish, = [index for index, row in enumerate(rows)
                               if row["event_type"] == "wait_finished" and row["wait_id"] == old_wait_id]
                new_start, = [index for index, row in enumerate(rows)
                              if row["event_type"] == "wait_started" and row["wait_reason"] == "build hold"]
                self.assertLess(old_finish, new_start)
                self.assertNotIn("an earlier wait still needs to close", output)

    def test_rerun_closes_the_failed_resource_wait_before_starting_its_own(self) -> None:
        history = self.record_phase()
        fault = self.fixture.root / "fail-wait-close"
        _ = fault.write_text("fail")
        observer = self.fixture.root / "wait-close-observer"
        self.fixture.write_executable(observer, f'''import os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1].endswith("/progress_history.py") and sys.argv[2] == "finish-wait" and Path({str(fault)!r}).exists():
    raise SystemExit(17)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        arguments = ("test", "sample", "--no-cache", "--local", "wait recovery", "--filter", "smoke")
        self.hold()
        first = self.start(*arguments)
        self.wait_for_call(first)
        grant_call(self.cycle_file)
        code, output = self.finish(first)
        self.assertEqual(code, 0, output)
        self.assertIn("cannot close", output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))

        fault.unlink()
        self.hold()
        second = self.start(*arguments)
        self.wait_for_call(second)
        grant_call(self.cycle_file)
        code, output = self.finish(second)
        self.assertEqual(code, 0, output)
        self.assertNotIn("cannot close", output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_finishes = [index for index, row in enumerate(rows)
                        if row["event_type"] == "wait_finished" and row["wait_id"] == old_wait["wait_id"]]
        self.assertEqual(len(old_finishes), 1, rows)
        new_start, = [index for index, row in enumerate(rows)
                      if row["event_type"] == "wait_started" and row["wait_id"] != old_wait["wait_id"]]
        self.assertLess(old_finishes[0], new_start)
        new_wait = rows[new_start]
        self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                              and row["wait_id"] == new_wait["wait_id"]]), 1)

    def test_rerun_recovers_a_wait_when_its_owner_dies_before_cleanup(self) -> None:
        history = self.record_phase()
        victim = self.fixture.root / "wait-owner-to-kill"
        observer = self.fixture.root / "stop-during-wait-close"
        self.fixture.write_executable(observer, f'''import os, signal, sys
from pathlib import Path
victim = Path({str(victim)!r})
if len(sys.argv) > 2 and sys.argv[1].endswith("/progress_history.py") and sys.argv[2] == "finish-wait" and victim.exists():
    os.kill(int(victim.read_text()), signal.SIGKILL)
    raise SystemExit(17)
os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(observer)
        arguments = ("test", "sample", "--no-cache", "--local", "wait recovery", "--filter", "smoke")
        self.hold()
        first = self.start(*arguments)
        self.wait_for_call(first)
        _ = victim.write_text(str(first.pid))
        grant_call(self.cycle_file)
        code, output = self.finish(first)
        self.assertEqual(code, -9, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_wait, = [row for row in rows if row["event_type"] == "wait_started"]
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        old_file, = list((session / "verify_waits").glob("*/verify-hold.*/phase-wait.*"))
        self.assertEqual(old_file.read_text().strip(), old_wait["wait_id"])
        self.assertFalse((old_file.parent / "retry").exists())

        victim.unlink()
        self.hold()
        second = self.start(*arguments)
        self.wait_for_call(second)
        grant_call(self.cycle_file)
        code, output = self.finish(second)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        old_finishes = [index for index, row in enumerate(rows)
                        if row["event_type"] == "wait_finished" and row["wait_id"] == old_wait["wait_id"]]
        self.assertEqual(len(old_finishes), 1, rows)
        new_start, = [index for index, row in enumerate(rows)
                      if row["event_type"] == "wait_started" and row["wait_id"] != old_wait["wait_id"]]
        self.assertLess(old_finishes[0], new_start)
        self.assertFalse(old_file.parent.exists())

    def test_a_second_call_leaves_a_live_owners_wait_open(self) -> None:
        history = self.record_phase()
        arguments = ("test", "sample", "--no-cache", "--local", "live wait owner", "--filter", "smoke")
        self.hold()
        first = self.start(*arguments)
        self.wait_for_call(first)
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        live_file, = list((session / "verify_waits").glob("*/verify-hold.*/phase-wait.*"))
        # Cleanup can mark a retry before its owner exits. A concurrent call
        # must still leave that live owner's wait alone.
        (live_file.parent / "retry").touch()
        self.fixture.environment.update({"UNIT_TEAM_ROLE": "test", "CODEX_THREAD_ID": "thread-test"})
        result = invoke_recorder(
            ["start-pass", "--session-dir", str(session), "--phase-id", "6", "--pass-kind", "test",
             "--activity", "verify", "--called-task", "direct.test", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", "fixture-test"],
            self.fixture.environment, self.fixture.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        _ = (session / "mesh_roster.json").write_text(json.dumps({
            "fixture-impl": {"thread_id": "thread-impl"}, "fixture-test": {"thread_id": "thread-test"}}))
        second = self.start(*arguments)
        self.wait_for_call(second)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        hold_waits = [row["wait_id"] for row in rows if row["event_type"] == "wait_started"]
        self.assertEqual(len(hold_waits), 2)
        self.assertFalse(any(row["event_type"] == "wait_finished" for row in rows))
        grant_call(self.cycle_file)
        for process in (first, second):
            code, output = self.finish(process)
            self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        for wait_id in hold_waits:
            self.assertEqual(len([row for row in rows if row["event_type"] == "wait_finished"
                                  and row["wait_id"] == wait_id]), 1, rows)

    def test_unheld_local_call_needs_no_release(self) -> None:
        result = self.fixture.verify("test", "sample", "--local", "local admission test", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("natedev is held", result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse(self.cycle_file.exists())

    def test_call_with_an_empty_holder_directory_starts_no_hold_check(self) -> None:
        Path(self.fixture.environment["BUILD_HOLD_DIR"]).mkdir()
        starts = self.count_python_starts()
        for arguments in (("check", "sample"), ("test", "sample", "--local", "local admission test", "--filter", "smoke")):
            with self.subTest(arguments=arguments):
                starts.unlink(missing_ok=True)
                result = self.fixture.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.hold_commands(starts), [])
                self.assertEqual(self.recorded_hold_wait(), 0)

    def test_held_call_starts_the_same_hold_checks_as_before(self) -> None:
        self.hold()
        starts = self.count_python_starts()
        observation = self.observe_waits()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(output.count("; waiting (tests that can run on the Mac"), 1)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertAlmostEqual(self.recorded_hold_wait(), float(observed["total"]), places=5)
        commands = self.hold_commands(starts)
        self.assertTrue(any(command[1] == "wait-local" for command in commands))
        self.assertFalse(any(command[1] in {"mark", "check-admission", "finish-local"} for command in commands))

    def test_held_call_waits_before_compiling_then_runs_on_release(self) -> None:
        history = self.record_phase()
        self.hold()
        self.fixture.environment["BUILD_TARGETS_POLL_S"] = "3600"
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call(process)
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse((Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        self.assertEqual((started["wait_kind"], started["wait_reason"], started["owner_session"]),
                         ("WaitingForMemoryOrSlot", "build hold", "thread-impl"))
        self.assertEqual(started["wait_id"], finished["wait_id"])
        self.assertEqual(cast(dict[str, object], finished["wait_end"])["state"], "Finished")

    def test_unreadable_hold_state_starts_no_local_step(self) -> None:
        _ = Path(self.fixture.environment["BUILD_HOLD_DIR"]).write_text("not a directory")
        result = self.fixture.verify("test", "sample", "--local", "local admission test", "--filter", "smoke")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("hold state cannot be read", result.stdout + result.stderr)
        self.assertEqual(self.fixture.local_runs(), [])


if __name__ == "__main__":
    _ = unittest.main()
