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
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Literal, TypedDict, cast, override
from unittest import mock

from scripts.build_hold import build_hold
from scripts.direct.recorder_fixture import invoke_recorder
from scripts.direct.verify_mac_offload_test_support import VerifyMacOffloadFixture, CallRecord, CARGO_STUB
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
        source = library.resolve()
        library.unlink()
        library.mkdir()
        for entry in source.iterdir():
            if entry.name != "py":
                (library / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        builder.write_executable(library / "py", '#!/bin/sh\nexec "${CLAUDE_PYTHON:-$TEST_REAL_PYTHON}" "$@"\n')
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
if os.environ.get("TEST_PYTHON_STARTS"):
    with Path(os.environ["TEST_PYTHON_STARTS"]).open("a") as output:
        output.write(json.dumps(sys.argv[1:]) + chr(10))
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
