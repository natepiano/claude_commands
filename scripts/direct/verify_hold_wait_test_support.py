"""Shared real verification, hold state and seeded tools for hold wait tests."""

from __future__ import annotations

import json
import os
import select
import shutil
import selectors
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal, TypedDict, cast, override
from unittest import mock

from scripts.build_hold import build_hold
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_mac_offload_test_support import (
    VerifyMacOffloadFixture, CallRecord, CARGO_STUB, replace_library_launcher,
)
from scripts.direct.local_release_wait import LocalReleaseEvents, local_release_waiter


VERIFY = Path(__file__).with_name("verify.sh")

TARGETS = VERIFY.parent.parent / "build_targets" / "build_targets.py"

INVOKE = VERIFY.parent.parent / "lint" / "invoke.sh"

CYCLE_ID = "c" * 32

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300

VERIFY_WAITING = "; waiting (tests that can run on the Mac"

LINT_STEP = 'set -euo pipefail; source "$1"; sweep_after_step() { :; }; run_once cargo check'

LINT_STEP_PROBE = ('set -euo pipefail; source "$1"; probe=$2; sweep_after_step() { :; }; '
                   + 'cargo() { sh -c \'printf "%s|%s" "${BUILDLOG_CALL_ID-}" "${BUILDLOG_HOLD_CALL_ID-}"\' > "$probe"; '
                   + 'command cargo "$@"; }; run_once cargo check')

class WaitObservation(TypedDict):
    total: str
    waits: list[str]
    directory: str

class StepObservation(TypedDict):
    start: str
    mem_wait: str
    arguments: list[str]

class CallWithHoldWait(CallRecord):
    hold_wait_s: float


@dataclass(frozen=True)
class PausedWaitPublication:
    """A real recorder child stopped at one selected publication boundary."""

    interpreter: Path
    resume: Path

    def continue_child(self) -> None:
        with self.resume.open("w") as output:
            _ = output.write("continue\n")


def pause_wait_publication(
    root: Path, boundary: Literal["before-event", "after-event", "before-lock", "before-file"],
) -> PausedWaitPublication:
    """Wrap the real recorder, with one FIFO stop rather than timing a race.

    Only the first wait start is stopped. Other recorder and interpreter calls
    keep their normal entry point, including recovery in a second verify shell.
    """
    control = root / "publication-stop"
    control.mkdir()
    resume = control / "resume"
    os.mkfifo(resume)
    armed = control / "armed"
    armed.touch()
    interpreter = control / "interpreter"
    repository = VERIFY.parents[2]
    _ = interpreter.write_text(f'''#!{sys.executable}
import fcntl
import os
import sys
from pathlib import Path

sys.path.insert(0, {str(repository)!r})
from scripts.direct import progress_history

armed = Path({str(armed)!r})
def pause():
    try:
        armed.unlink()
    except FileNotFoundError:
        return
    print("wait publication paused: {boundary}", file=sys.stderr, flush=True)
    with Path({str(resume)!r}).open() as input:
        input.readline()

arguments = sys.argv[1:]
turn = arguments and arguments[0] in ("--record-turn-wait", "--observe-turn-wait")
recorder = arguments and arguments[0].endswith("/progress_history.py")
if turn or recorder:
    append = progress_history._append_event
    def observed_append(state, event):
        selected = event.get("event_type") == "wait_started"
        if selected and {boundary!r} == "before-event":
            pause()
        append(state, event)
        if selected and {boundary!r} == "after-event":
            pause()
    progress_history._append_event = observed_append
    publish = progress_history._publish_wait_file
    def observed_publication(publication, wait_id):
        if {boundary!r} == "before-file" and arguments[0] != "--observe-turn-wait":
            pause()
        publish(publication, wait_id)
    progress_history._publish_wait_file = observed_publication
    if arguments[0] == "--observe-turn-wait":
        flock = fcntl.flock
        announced = False
        def observe_admission(descriptor, operation):
            global announced
            if not announced and operation == fcntl.LOCK_EX and os.readlink("/proc/self/fd/" + str(descriptor)).endswith("/.progress_history.lock"):
                announced = True
                try:
                    flock(descriptor, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("turn wait lock: blocked", file=sys.stderr, flush=True)
                else:
                    print("turn wait lock: acquired", file=sys.stderr, flush=True)
                    return
            return flock(descriptor, operation)
        fcntl.flock = observe_admission
    if {boundary!r} == "before-lock" and not turn and "start-wait" in arguments:
        flock = fcntl.flock
        def observed_lock(descriptor, operation):
            published = "--wait-file" not in arguments or Path(arguments[arguments.index("--wait-file") + 1]).exists()
            if published and operation == fcntl.LOCK_EX and os.readlink("/proc/self/fd/" + str(descriptor)).endswith("/.progress_history.lock"):
                pause()
            return flock(descriptor, operation)
        fcntl.flock = observed_lock
    if turn:
        progress_history.record_turn_wait(Path(arguments[1]), arguments[2], arguments[3])
    else:
        sys.argv = arguments
        progress_history.main()
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *arguments])
''')
    interpreter.chmod(0o755)
    return PausedWaitPublication(interpreter, resume)

def read_cycle_file(path: Path) -> build_hold.HoldCycle:
    return cast(build_hold.HoldCycle, json.loads(path.read_text()))

def grant_call(path: Path) -> None:
    release = path.parent.parent
    with mock.patch.dict(os.environ, {
        "BUILD_HOLD_DIR": str(release.parent / "build-hold"),
        "BUILD_HOLD_RELEASE_DIR": str(release),
        "UNIT_ACTIVE_DIR": str(release.parent / "active"),
    }), mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")), \
         mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()):
        holders = build_hold.read_holders(build_hold.holder_directory())
        assert isinstance(holders, build_hold.ActiveHolders)
        for holder in holders.holders:
            _ = build_hold.release_cycle(build_hold.holder_directory(), holder.key)

def change_cycle(path: Path, change: Callable[[build_hold.HoldCycle], None]) -> None:
    release = path.parent.parent
    with mock.patch.dict(os.environ, {
        "BUILD_HOLD_DIR": str(release.parent / "build-hold"),
        "BUILD_HOLD_RELEASE_DIR": str(release),
        "UNIT_ACTIVE_DIR": str(release.parent / "active"),
    }), build_hold.release_lock():
        cycle = build_hold.read_cycle()
        assert cycle is not None
        change(cycle)
        build_hold.save_cycle(cycle)

@contextmanager
def release_registered_call(path: Path) -> Generator[None, None, None]:
    stop = threading.Event()
    failures: list[BaseException] = []
    root = path.parents[2]
    stopped = root / ".release-fixture-stopped"

    def release() -> None:
        try:
            with local_release_waiter(root, root) as events:
                deadline = time.monotonic() + TEST_HANG_GUARD_SECS
                while True:
                    # Watch all existing descendants before checking; watching
                    # parents discovers directories that appear during startup.
                    events.arm()
                    if isinstance(events, LocalReleaseEvents):
                        for directory in root.rglob("*"):
                            if directory.is_dir() and not directory.is_symlink():
                                _ = events.add_watch(events.descriptor, os.fsencode(directory), 0x00000FCA)
                    if stop.is_set():
                        return
                    if path.exists() and any(root.glob("**/verify-hold.*/waiter")):
                        grant_call(path)
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("registered hold call never published its waiter")
                    if isinstance(events, LocalReleaseEvents):
                        # Read every subscribed directory event, including the
                        # nested waiter and the stop marker; production's
                        # hold-only name filter is deliberately narrower.
                        readable, _, _ = select.select([events.descriptor], [], [], remaining)
                        if readable:
                            _ = os.read(events.descriptor, 65536)
                    else:
                        events.wait(remaining)
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=release)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        stopped.touch()
        worker.join(timeout=TEST_HANG_GUARD_SECS)
        stopped.unlink(missing_ok=True)
        if worker.is_alive():
            raise AssertionError("Release fixture did not stop")
        if failures:
            raise AssertionError("Release fixture failed") from failures[0]



class VerifyHoldTools(VerifyMacOffloadFixture):
    """The existing shell contract, seeded once by each hold test class."""

    fixture_root: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_environment: ClassVar[dict[str, str]]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        # The committed Mac fixture exposes setUp(), not a seed builder. Own
        # the copying here so this suite needs only that public lifecycle.
        builder = VerifyMacOffloadFixture()
        cls.addClassCleanup(builder.doCleanups)
        builder.setUp()
        cls.fixture_root = builder.root
        seed_directory = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="verify-hold-seed-"))
        cls.fixture_seed = Path(seed_directory) / "seed"
        # These tests select a known interpreter, including recorder observers.
        # Version discovery is the lib/py suite's boundary. Preserve every
        # requested script and its real process, without a probe before each.
        library = builder.home / ".claude/scripts/lib"
        replace_library_launcher(library, '#!/bin/sh\nexec "${CLAUDE_PYTHON:-$TEST_REAL_PYTHON}" "$@"\n')
        cls.fixture_environment = {
            **builder.environment,
            "IN_NIX_SHELL": "pure",
            "BUILDLOG_BUILD_BUDGET_GIB": "64",
        }
        _ = shutil.copytree(cls.fixture_root, cls.fixture_seed, symlinks=True)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.root: Path = self.fixture_root
        _ = shutil.copytree(self.fixture_seed, self.root, symlinks=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(self.assert_nothing_outlives)
        self.home: Path = self.root / "home"
        self.bin_directory: Path = self.root / "bin"
        self.board: Path = self.root / "board"
        self.runner: Path = self.root / "runner.py"
        self.runner_state: Path = self.root / "runner-state"
        self.cargo_events_path: Path = self.root / "cargo-events.jsonl"
        self.environment: dict[str, str] = self.fixture_environment.copy()

    @override
    def stop_verify(self, process: subprocess.Popen[str]) -> None:
        # Completed calls are drained here; the unchanged final marker scan
        # still observes and reaps detached children before restoring the seed.
        if process.poll() is None:
            super().stop_verify(process)
        else:
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)


class VerifyHoldWaitFixture(unittest.TestCase):
    fixture: VerifyHoldTools

    cycle_file: Path = Path()

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = VerifyHoldTools("runTest")
        self.wait_diagnostics: dict[int, str] = {}
        self.wait_output: dict[int, str] = {}

    @classmethod
    @override
    def setUpClass(cls) -> None:
        VerifyHoldTools.setUpClass()
        cls.addClassCleanup(VerifyHoldTools.doClassCleanups)

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.fixture_cleanups)
        root = self.fixture.root
        pressure = root / "memory-pressure"
        _ = pressure.write_text("full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.cycle_file = root / "release" / CYCLE_ID / "cycle.json"
        self.fixture.environment.update({
            "CLAUDE_PYTHON": sys.executable,
            "BUILDLOG_DIR": str(root / "buildlog"),
            "BUILDLOG_MEMORY_PRESSURE": str(pressure),
            "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
            "UNIT_ACTIVE_DIR": str(root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "UNIT_TEAM_ROLE": "", "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "",
            "NOTIFIER_SESSIONS_DIR": str(root / "sessions"),
            "MAC_TEST_STATE_DIR": str(root / "mac-state"),
            "BUILD_TARGETS_POLL_S": "0.01", "BUILD_TARGETS_NOW": "",
            "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_STATUS": "75",
            "BUILDLOG_HOLD_WAIT_FILE": "",
            "BUILDLOG_HOLD_WAITER_FILE": "",
        })

    def hold(self, key: str = "hana") -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        holders.mkdir(exist_ok=True)
        now = datetime.now().astimezone().isoformat()
        _ = (holders / key).write_text(json.dumps({"holder": "hana", "since": now, "for": "render checks", "release_eta": "unknown"}))
        self.cycle_file.parent.mkdir(parents=True, exist_ok=True)
        _ = (self.cycle_file.parent.parent / "current").write_text(CYCLE_ID)
        cycle: build_hold.HoldCycle = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": "",
            "holders": {"hana": {"since": now, "released_at": ""}},
            "recipients": {}, "entries": [],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle))

    def fixture_cleanups(self) -> None:
        # Outside a run, doCleanups keeps a failed cleanup to itself:
        # a process the fixture found outliving the test fails this one.
        self.assertTrue(self.fixture.doCleanups(), "a fixture cleanup failed; a process may have outlived the test")

    def start(self, *arguments: str) -> subprocess.Popen[str]:
        process = self.fixture.start_verify(*arguments)
        self.addCleanup(self.stop_process, process)
        return process

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        self.fixture.stop_verify(process)

    def wait_for_call(self, process: subprocess.Popen[str], announcement: str = VERIFY_WAITING) -> None:
        self.wait_for_waiting_diagnostic(process, announcement)

    def wait_for_waiting_diagnostic(self, process: subprocess.Popen[str],
                                    announcement: str = VERIFY_WAITING) -> None:
        # Registration precedes the first waiting announcement. Releasing before
        # that announcement can legitimately record zero waiting seconds.
        assert process.stderr is not None
        assert process.stdout is not None
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        captured = self.wait_diagnostics.get(process.pid, "")
        output = self.wait_output.get(process.pid, "")
        with selectors.DefaultSelector() as events:
            _ = events.register(process.stderr, selectors.EVENT_READ)
            _ = events.register(process.stdout, selectors.EVENT_READ)
            while announcement not in captured + output:
                remaining = deadline - time.monotonic()
                ready = events.select(max(0, remaining))
                if remaining <= 0 or not ready:
                    self.fail("Verification never announced its registered hold wait")
                for key, _ in ready:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        _ = events.unregister(key.fd)
                        if not events.get_map():
                            self.fail(f"Verification exited before waiting: {output + captured}")
                        continue
                    if key.fd == process.stderr.fileno():
                        captured += chunk.decode("utf-8")
                        self.wait_diagnostics[process.pid] = captured
                    else:
                        output += chunk.decode("utf-8")
                        self.wait_output[process.pid] = output

    def finish(self, process: subprocess.Popen[str]) -> tuple[int, str]:
        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        return process.returncode, self.wait_output.pop(process.pid, "") + output + self.wait_diagnostics.pop(process.pid, "") + errors

    def set_hold_routing(self, runs_on: Literal["natedev_only", "mac_allowed"]) -> None:
        targets = self.fixture.root / "hold-targets.py"
        _ = targets.write_text(f'''import os
import runpy
import sys
import tempfile

if sys.argv[1] == "eligible":
    print({runs_on!r} + (" fixture local wait" if {runs_on!r} == "natedev_only" else ""))
    print("natedev\\teligible\\t-\\t-")
    if {runs_on!r} == "mac_allowed":
        print("mac\\teligible\\t-\\t" + os.environ["MAC_TEST_CONFIG"])
else:
    sys.path.insert(0, {str(TARGETS.parent)!r})
    runpy.run_path({str(TARGETS)!r}, run_name="__main__")
''')
        self.fixture.environment["BUILD_TARGETS_SCRIPT"] = str(targets)

    def record_phase(self) -> Path:
        session = self.fixture.root / "unit-run"
        self.fixture.environment.update({"UNIT_RUN_DIR": str(session), "UNIT_PHASE_ID": "closed-server-phase",
                                         "UNIT_TEAM_ROLE": "impl", "CODEX_THREAD_ID": "thread-impl",
                                         "UNIT_PASS_OWNER": "launcher",
                                         "UNIT_HISTORY_DIR": str(self.fixture.root / "history"),
                                         "UNIT_SYNC_WRITES": "0", "CLAUDE_PROBE_SESSION": ""})
        for arguments in (
            ["start-run", "--session-dir", str(session), "--working-dir", str(self.fixture.root),
             "--main-family", "codex", "--main-model", "fixture", "--main-effort", "high",
             "--main-session-id", "fixture-director"],
            ["start-phase", "--session-dir", str(session), "--phase-id", "6", "--phase-title", "Hold"],
            ["start-pass", "--session-dir", str(session), "--phase-id", "6", "--pass-kind", "impl",
             "--activity", "verify", "--called-task", "direct.impl", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", "fixture-impl"],
        ):
            result = invoke_recorder(arguments, self.fixture.environment, self.fixture.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        _ = (session / "mesh_roster.json").write_text(json.dumps({
            "fixture-impl": {"thread_id": "thread-impl"}}))
        return self.fixture.root / "history" / "runs" / f"{session.name}.jsonl"

    def pause_publication(self, boundary: Literal["before-event", "after-event", "before-lock", "before-file"]) -> PausedWaitPublication:
        control = pause_wait_publication(self.fixture.root, boundary)
        self.fixture.environment["CLAUDE_PYTHON"] = str(control.interpreter)
        return control

    def publish_marker_during_cleanup(self, directory: Path, wait_id: str) -> Path:
        """Publish after the real recovery scan, just before folder retirement."""
        marker = directory / "phase-wait.late.hold"
        armed = self.fixture.root / "cleanup-publication-armed"
        armed.touch()
        interpreter = self.fixture.root / "late-recovery-publication"
        self.fixture.write_executable(interpreter, f'''import os, sys
from pathlib import Path
sys.path.insert(0, {str(VERIFY.parents[2])!r})
from scripts.direct import progress_history
arguments = sys.argv[1:]
if len(arguments) > 1 and arguments[0].endswith("/progress_history.py") and arguments[1] == "recover-wait-folder":
    rename = os.rename
    def publish_before_rename(source, target, *args, **kwargs):
        armed = Path({str(armed)!r})
        if Path(source) == Path({str(directory)!r}) and armed.exists():
            armed.unlink()
            progress_history._publish_wait_file(progress_history.WaitIdFile(Path({str(marker)!r})), {wait_id!r})
        return rename(source, target, *args, **kwargs)
    os.rename = publish_before_rename
    sys.argv = arguments
    progress_history.main()
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *arguments])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(interpreter)
        return marker

    @contextmanager
    def after_wait_owner_deleted(self, directory: Path, action: Callable[[], None]) -> Generator[None, None, None]:
        """Interrupt the real recursive delete after its snapshot and owner unlink."""
        identity = directory.stat()
        unlink = os.unlink
        invoked = False

        def after_unlink(path: str, *, dir_fd: int | None = None) -> None:
            nonlocal invoked
            unlink(path, dir_fd=dir_fd)
            if invoked or path != "owner.json" or dir_fd is None:
                return
            parent = os.fstat(dir_fd)
            if (parent.st_dev, parent.st_ino) == (identity.st_dev, identity.st_ino):
                invoked = True
                action()

        with mock.patch.object(os, "unlink", after_unlink):
            yield
        self.assertTrue(invoked, "Recovery never reached deletion after its folder snapshot")

    def remove_wait_after_recovery_scan(self, marker: Path, wait_id: str) -> Path:
        """Finish an id, then remove its marker after recovery lists it."""
        removed = self.fixture.root / "wait-removed-by-scanner"
        interpreter = self.fixture.root / "remove-before-wait-read"
        self.fixture.write_executable(interpreter, f'''import os, sys
from pathlib import Path
sys.path.insert(0, {str(VERIFY.parents[2])!r})
from scripts.direct import progress_history
arguments = sys.argv[1:]
if len(arguments) > 1 and arguments[0].endswith("/progress_history.py") and arguments[1] == "recover-wait-folder":
    sys.argv = [arguments[0], "finish-wait", "--session-dir", os.environ["UNIT_RUN_DIR"], "--wait-id", {wait_id!r}]
    progress_history.main()
    read_text = Path.read_text
    def remove_before_read(path, *args, **kwargs):
        if path == Path({str(marker)!r}) and path.exists():
            path.unlink()
            Path({str(removed)!r}).write_text("removed")
        return read_text(path, *args, **kwargs)
    Path.read_text = remove_before_read
    sys.argv = arguments
    progress_history.main()
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *arguments])
''')
        self.fixture.environment["CLAUDE_PYTHON"] = str(interpreter)
        return removed

    def saved_publication_file(self) -> Path:
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        files = list((session / "verify_waits").glob("*/verify-hold.*/phase-wait.*"))
        self.assertEqual(len(files), 1, f"One recoverable wait file must exist before the event append: {files}")
        return files[0]

    def wait_events(self, history: Path) -> list[dict[str, object]]:
        return [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]

    @contextmanager
    def unavailable_wait_history(self, history: Path) -> Generator[None, None, None]:
        saved = history.read_bytes()
        history.unlink()
        history.mkdir()
        try:
            yield
        finally:
            history.rmdir()
            _ = history.write_bytes(saved)

    def recover_saved_wait_after_failed_read(self, marker: Path, history: Path, wait_id: str) -> None:
        result = self.fixture.verify("test", "sample", "--no-cache", "--local", "retry readable history",
                                     "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())
        finishes = [row for row in self.wait_events(history)
                    if row.get("wait_id") == wait_id and row["event_type"] == "wait_finished"]
        self.assertEqual(len(finishes), 1)

    def waits_report(self) -> dict[str, object]:
        result = invoke_recorder(["waits", "--session-dir", self.fixture.environment["UNIT_RUN_DIR"]],
                                 self.fixture.environment, self.fixture.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return cast(dict[str, object], json.loads(result.stdout))

    def recover_publication(self) -> None:
        rerun = self.start("test", "sample", "--no-cache", "--local", "recover publication", "--filter", "smoke")
        self.wait_for_call(rerun)
        grant_call(self.cycle_file)
        code, output = self.finish(rerun)
        self.assertEqual(code, 0, output)

    def assert_stopped_publication_recovers(self, boundary: Literal["before-event", "after-event"]) -> None:
        history = self.record_phase()
        _ = self.pause_publication(boundary)
        self.hold()
        first = self.start("test", "sample", "--no-cache", "--local", "stop publication", "--filter", "smoke")
        self.wait_for_call(first, f"wait publication paused: {boundary}")
        marker = self.saved_publication_file()
        wait_id = marker.read_text().strip()
        self.assertTrue(wait_id)
        before = [row for row in self.wait_events(history) if row.get("wait_id") == wait_id]
        self.assertEqual(len(before), int(boundary == "after-event"))
        self.stop_process(first)
        self.recover_publication()
        self.assertFalse(marker.exists())
        events = [row for row in self.wait_events(history) if row.get("wait_id") == wait_id]
        report = self.waits_report()
        intervals = [row for row in cast(list[dict[str, object]], report["waits"]) if row["wait_id"] == wait_id]
        if boundary == "after-event":
            self.assertEqual([row["event_type"] for row in events], ["wait_started", "wait_finished"])
            self.assertEqual(len(intervals), 1)
            self.assertEqual(cast(dict[str, object], intervals[0]["wait_end"])["state"], "Finished")
        else:
            self.assertEqual([row["event_type"] for row in events], ["wait_abandoned"])
            self.assertEqual(intervals, [])
            abandoned = [row for row in cast(list[dict[str, object]], report["abandoned"]) if row["wait_id"] == wait_id]
            self.assertEqual(len(abandoned), 1)
            self.assertEqual(cast(dict[str, object], abandoned[0]["wait_end"])["state"], "NeverStarted")
        self.assertFalse(any(cast(dict[str, object], row["wait_end"])["state"] == "Open"
                             for row in cast(list[dict[str, object]], report["waits"])))

    def assert_wait_follows_worker_thread(self, worker_name: str) -> None:
        history = self.record_phase()
        session = Path(self.fixture.environment["UNIT_RUN_DIR"])
        _ = (session / "mesh_roster.json").write_text(json.dumps({
            "fixture-impl": {"thread_id": "thread-impl"}, worker_name: {"thread_id": "thread-test"}}))
        for arguments in (
            ["start-phase", "--session-dir", str(session), "--phase-id", "7", "--phase-title", "Test"],
            ["start-pass", "--session-dir", str(session), "--phase-id", "7", "--pass-kind", "test",
             "--activity", "verify", "--called-task", "direct.test", "--called-family", "codex",
             "--called-model", "fixture", "--worker-name", worker_name],
        ):
            result = invoke_recorder(arguments, {**self.fixture.environment, "UNIT_TEAM_ROLE": "test"}, self.fixture.root)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.fixture.environment["CODEX_THREAD_ID"] = "thread-test"
        self.hold()
        process = self.start("test", "sample", "--local", "worker attribution", "--filter", "smoke")
        self.wait_for_call(process)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        rows = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
        worker_pass, = [row for row in rows if row["event_type"] == "pass_started" and row["worker_name"] == worker_name]
        started, = [row for row in rows if row["event_type"] == "wait_started"]
        finished, = [row for row in rows if row["event_type"] == "wait_finished"]
        for wait in (started, finished):
            self.assertEqual((wait["phase_id"], wait["phase_instance_id"], wait["pass_instance_id"],
                              wait["worker_name"], wait["owner_session"]),
                             ("7", worker_pass["phase_instance_id"], worker_pass["pass_instance_id"], worker_name, "thread-test"))

    def count_python_starts(self) -> Path:
        starts = self.fixture.root / "python-starts"
        self.fixture.environment.update({"TEST_PYTHON_STARTS": str(starts), "TEST_REAL_PYTHON": sys.executable})
        self.fixture.write_executable(self.fixture.bin_directory / "python3", '''import json, os, sys
from pathlib import Path
with Path(os.environ["TEST_PYTHON_STARTS"]).open("a") as output:
    output.write(json.dumps(sys.argv[1:]) + chr(10))
os.execv(os.environ["TEST_REAL_PYTHON"], [os.environ["TEST_REAL_PYTHON"], *sys.argv[1:]])
''')
        return starts

    def hold_commands(self, starts: Path) -> list[list[str]]:
        commands: list[list[str]] = []
        for line in starts.read_text().splitlines() if starts.exists() else []:
            arguments = cast(list[str], json.loads(line))
            if (len(arguments) < 2 or Path(arguments[0]).name not in {"build_targets.py", "build_hold.py"}
                    or arguments[1] == "eligible"):
                continue
            arguments[0] = Path(arguments[0]).name
            for option, replacement in (("--call-id", "c1"), ("--pid", "<pid>"), ("--cycle-id", CYCLE_ID)):
                if option in arguments:
                    arguments[arguments.index(option) + 1] = replacement
            commands.append(arguments)
        return commands

    def recorded_hold_wait(self) -> float:
        row = cast(CallWithHoldWait, self.fixture.call_records()[-1])
        return row["hold_wait_s"]

    def start_lint_step(self, caller: str, extra: dict[str, str] | None = None,
                        probe: Path | None = None) -> subprocess.Popen[str]:
        """One compiling lint step, as `caller` runs it: no BUILDLOG_CALL_ID."""
        shell = [LINT_STEP_PROBE, "bash", str(INVOKE), str(probe)] if probe else [LINT_STEP, "bash", str(INVOKE)]
        process = subprocess.Popen(
            ["bash", "-c", *shell],
            cwd=self.fixture.root, env={**self.fixture.environment, "BUILDLOG_CALLER": caller,
                                        "BUILDLOG_CALL_ID": "", **(extra or {})},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        return process

    def observe_waits(self) -> Path:
        observer = self.fixture.root / "python-observer"
        self.fixture.write_executable(observer, '''import json, os, sys
from pathlib import Path
if len(sys.argv) > 2 and sys.argv[1].endswith("/buildlog/record.py") and sys.argv[2] == "step":
    with Path(os.environ["TEST_STEP_OBSERVATIONS"]).open("a") as output:
        output.write(json.dumps({"start": sys.argv[4], "mem_wait": os.environ.get("BUILDLOG_MEM_WAIT_S", ""), "arguments": sys.argv[9:]}) + chr(10))
if len(sys.argv) > 2 and sys.argv[1].endswith("/buildlog/record.py") and sys.argv[2] == "call":
    path = Path(os.environ["BUILDLOG_HOLD_WAIT_FILE"])
    Path(os.environ["TEST_WAIT_OBSERVATION"]).write_text(json.dumps({"total": os.environ.get("BUILDLOG_HOLD_WAIT_S", ""), "waits": path.read_text().splitlines() if path.exists() else [], "directory": str(path.parent)}))
os.execv(os.environ["TEST_REAL_PYTHON"], [os.environ["TEST_REAL_PYTHON"], *sys.argv[1:]])
''')
        observation = self.fixture.root / "wait-observation"
        self.fixture.environment.update({"CLAUDE_PYTHON": str(observer), "TEST_REAL_PYTHON": sys.executable, "TEST_WAIT_OBSERVATION": str(observation), "TEST_STEP_OBSERVATIONS": str(self.fixture.root / "step-observations")})
        return observation

    def hold_after_first_step(self) -> None:
        cargo = self.fixture.bin_directory / "cargo"
        source = CARGO_STUB.replace(
            'if arguments[:1] == ["metadata"]:',
            '''if arguments[:1] == ["check"]:
    root = Path(os.environ["PACKAGE_ROOT"])
    holders = Path(os.environ["BUILD_HOLD_DIR"])
    holders.mkdir(exist_ok=True)
    from datetime import datetime
    now = datetime.now().astimezone().isoformat()
    (holders / "hana").write_text(json.dumps({"holder": "hana", "since": now, "for": "between steps", "release_eta": "unknown"}))
    release = Path(os.environ["BUILD_HOLD_RELEASE_DIR"])
    cycle_id = "c" * 32
    (release / cycle_id).mkdir(parents=True, exist_ok=True)
    (release / "current").write_text(cycle_id)
    path = release / cycle_id / "cycle.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"id": cycle_id, "opened_at": now, "release_started_at": "", "holders": {"hana": {"since": now, "released_at": ""}}, "recipients": {}, "entries": []}))
    os.replace(temporary, path)
if arguments[:1] == ["metadata"]:''',
        )
        self.fixture.write_executable(cargo, source)
