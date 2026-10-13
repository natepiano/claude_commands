"""Local verification admission uses temporary holds and command stand-ins."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast, override
from unittest import mock

from scripts.build_hold import build_hold
from scripts.delegate import test_verify_mac_offload


VERIFY = Path(__file__).with_name("verify.sh")
TARGETS = VERIFY.parent.parent / "build_targets" / "build_targets.py"
CYCLE_ID = "c" * 32


class WaitObservation(TypedDict):
    total: str
    waits: list[str]
    directory: str


class StepObservation(TypedDict):
    start: str
    mem_wait: str
    arguments: list[str]


class CallWithHoldWait(test_verify_mac_offload.CallRecord):
    hold_wait_s: float


def read_cycle_file(path: Path) -> build_hold.HoldCycle:
    return cast(build_hold.HoldCycle, json.loads(path.read_text()))


def grant_call(path: Path) -> None:
    release = path.parent.parent
    with mock.patch.dict(os.environ, {
        "BUILD_HOLD_DIR": str(release.parent / "build-hold"),
        "BUILD_HOLD_RELEASE_DIR": str(release),
        "PLAN_DELEGATE_ACTIVE_DIR": str(release.parent / "active"),
    }), mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")), \
         mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()):
        with build_hold.release_lock():
            cycle = build_hold.read_cycle()
            assert cycle is not None
            now = datetime.now().astimezone()
            cycle["release_started_at"] = now.isoformat()
            build_hold.save_cycle(cycle)
            _ = build_hold.advance_release(cycle, now, clock=lambda: now)


def change_cycle(path: Path, change: Callable[[build_hold.HoldCycle], None]) -> None:
    release = path.parent.parent
    with mock.patch.dict(os.environ, {
        "BUILD_HOLD_DIR": str(release.parent / "build-hold"),
        "BUILD_HOLD_RELEASE_DIR": str(release),
        "PLAN_DELEGATE_ACTIVE_DIR": str(release.parent / "active"),
    }), build_hold.release_lock():
        cycle = build_hold.read_cycle()
        assert cycle is not None
        change(cycle)
        build_hold.save_cycle(cycle)


@contextmanager
def release_registered_call(path: Path) -> Generator[None, None, None]:
    stop = threading.Event()
    failures: list[BaseException] = []

    def release() -> None:
        try:
            deadline = time.monotonic() + 10
            while not stop.wait(0.01) and time.monotonic() < deadline:
                if not path.exists():
                    continue
                cycle = read_cycle_file(path)
                if any(entry.get("calls", []) for entry in cycle["entries"]):
                    grant_call(path)
                    return
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=release)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2)
        if worker.is_alive():
            raise AssertionError("Release fixture did not stop")
        if failures:
            raise AssertionError("Release fixture failed") from failures[0]


class VerifyHoldWaitTests(unittest.TestCase):
    fixture: test_verify_mac_offload.VerifyMacOffloadTests
    cycle_file: Path = Path()

    @override
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.fixture = test_verify_mac_offload.VerifyMacOffloadTests("runTest")

    @override
    def setUp(self) -> None:
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        root = self.fixture.root
        self.cycle_file = root / "release" / CYCLE_ID / "cycle.json"
        self.fixture.environment.update({
            "CLAUDE_PYTHON": sys.executable,
            "BUILD_HOLD_RELEASE_DIR": str(root / "release"),
            "PLAN_DELEGATE_ACTIVE_DIR": str(root / "active"),
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
            "PLAN_DELEGATE_TEAM_ROLE": "", "CLAUDE_CODE_SESSION_ID": "",
            "NOTIFIER_SESSIONS_DIR": str(root / "sessions"),
            "MAC_TEST_STATE_DIR": str(root / "mac-state"),
            "BUILD_TARGETS_POLL_S": "0.01", "BUILD_TARGETS_NOW": "",
            "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_STATUS": "75",
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_ADMISSION_FILE": "",
            "BUILDLOG_HOLD_STARTED_FILE": "",
            "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(root / "registration"),
        })

    def hold(self) -> None:
        holders = Path(self.fixture.environment["BUILD_HOLD_DIR"])
        holders.mkdir(exist_ok=True)
        now = datetime.now().astimezone().isoformat()
        _ = (holders / "hana").write_text(json.dumps({"holder": "hana", "since": now, "for": "render checks", "release_eta": "unknown"}))
        self.cycle_file.parent.mkdir(parents=True, exist_ok=True)
        _ = (self.cycle_file.parent.parent / "current").write_text(CYCLE_ID)
        cycle: build_hold.HoldCycle = {
            "id": CYCLE_ID, "opened_at": now, "release_started_at": "",
            "holders": {"hana": {"since": now, "released_at": ""}},
            "recipients": {}, "entries": [],
        }
        _ = self.cycle_file.write_text(json.dumps(cycle))

    def start(self, *arguments: str) -> subprocess.Popen[str]:
        process = self.fixture.start_verify(*arguments)
        self.addCleanup(self.stop_process, process)
        return process

    def stop_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        _ = process.communicate(timeout=3)

    def wait_for_call(self) -> build_hold.ReleaseEntry:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.cycle_file.exists():
                cycle = read_cycle_file(self.cycle_file)
                for entry in cycle["entries"]:
                    if entry.get("calls", []):
                        return entry
            time.sleep(0.01)
        self.fail("Verification did not register its call")

    def finish(self, process: subprocess.Popen[str]) -> tuple[int, str]:
        output, errors = process.communicate(timeout=8)
        return process.returncode, output + errors

    def test_unheld_local_call_needs_no_release(self) -> None:
        result = self.fixture.verify("test", "sample", "--local", "local admission test", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("natedev is held", result.stdout + result.stderr)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertFalse(self.cycle_file.exists())

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
        _ = self.wait_for_call()
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(output.count("natedev is held by"), 1)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertAlmostEqual(self.recorded_hold_wait(), float(observed["total"]), places=5)
        # Ordered commands recorded from the unchanged scripts at 8b3178f8.
        expected = [
            ["build_targets.py", "check-admission", "--call-id", "c1"],
            ["build_targets.py", "wait-local", "--call-id", "c1", "--pid", "<pid>"],
            ["build_targets.py", "wait-local", "--call-id", "c1", "--pid", "<pid>"],
            ["build_targets.py", "wait-local", "--call-id", "c1", "--pid", "<pid>"],
            ["build_targets.py", "check-admission", "--call-id", "c1"],
            ["build_hold.py", "mark", "--state", "MemoryGateReturned", "--outcome", "Granted",
             "--call-id", "c1", "--cycle-id", "cccccccccccccccccccccccccccccccc", "--generation", "1"],
            ["build_targets.py", "finish-local", "--call-id", "c1"],
        ]
        self.assertEqual(self.hold_commands(starts), expected)

    def test_held_call_registers_before_compiling_then_runs_on_release(self) -> None:
        self.hold()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        entry = self.wait_for_call()
        self.assertTrue(entry["session_id"].startswith("verify:"))
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for render checks; waiting", output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        self.assertTrue((Path(self.fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists())

    def test_seat_registers_and_marks_the_marker_owner(self) -> None:
        self.hold()
        active = self.fixture.root / "active"
        active.mkdir()
        run = self.fixture.root / "delegate-run"
        run.mkdir()
        _ = (active / "owner").write_text(str(run))
        self.fixture.environment.update({"PLAN_DELEGATE_BOARD_DIR": str(run), "CLAUDE_CODE_SESSION_ID": "stale-session"})
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        entry = self.wait_for_call()
        self.assertEqual(entry["session_id"], "owner")
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual(read_cycle_file(self.cycle_file)["entries"][0]["state"], "MemoryGateReturned")

    def test_mac_filter_pass_during_local_hold_needs_no_local_admission(self) -> None:
        self.hold()
        before = self.cycle_file.read_bytes()
        self.fixture.environment.update({"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        result = self.fixture.verify("test", "sample", "--filter", "smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertEqual(len(self.fixture.runner_events()), 1)
        self.assertEqual(self.cycle_file.read_bytes(), before)
        self.assertNotIn("natedev is held", result.stdout + result.stderr)

    def test_unreadable_hold_state_starts_no_local_step(self) -> None:
        _ = Path(self.fixture.environment["BUILD_HOLD_DIR"]).write_text("not a directory")
        result = self.fixture.verify("test", "sample", "--local", "local admission test", "--filter", "smoke")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertIn("hold state cannot be read", result.stdout + result.stderr)
        self.assertEqual(self.fixture.local_runs(), [])

    def test_waiter_signals_remove_only_the_cancelled_call(self) -> None:
        for signal_number, expected in ((signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signal_number):
                self.hold()
                process = subprocess.Popen(
                    [sys.executable, str(TARGETS), "wait-local", "--call-id", "cancelled"],
                    env={**self.fixture.environment, "CLAUDE_CODE_SESSION_ID": "owner"},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                )
                self.addCleanup(self.stop_process, process)
                _ = self.wait_for_call()
                def add_peer(cycle: build_hold.HoldCycle) -> None:
                    cycle["entries"][0]["calls"].append({
                        "call_id": "peer", "pid": os.getpid(),
                        "proc_start": Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19],
                        "started": False, "admitted_generation": 0,
                    })

                change_cycle(self.cycle_file, add_peer)
                process.send_signal(signal_number)
                code, output = self.finish(process)
                self.assertEqual(code, expected, output)
                remaining = [call["call_id"] for entry in read_cycle_file(self.cycle_file)["entries"] for call in entry.get("calls", [])]
                self.assertEqual(remaining, ["peer"])

    def test_standalone_and_mac_runners_do_not_register_local_waiters(self) -> None:
        self.hold()
        invoke = VERIFY.parent.parent / "lint" / "invoke.sh"
        for caller in ("lint", "verify-mac"):
            with self.subTest(caller=caller):
                before = self.cycle_file.read_bytes()
                environment = {**self.fixture.environment, "BUILDLOG_CALLER": caller, "BUILDLOG_CALL_ID": "remote-call" if caller == "verify-mac" else ""}
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail; source "$1"; sweep_after_step() { :; }; run_once cargo check', "bash", str(invoke)],
                    cwd=self.fixture.root, env=environment, capture_output=True, text=True, check=False, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.cycle_file.read_bytes(), before)

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
        source = test_verify_mac_offload.CARGO_STUB.replace(
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

    def test_hold_between_steps_is_waited_and_parent_records_its_seconds(self) -> None:
        self.hold_after_first_step()
        observation = self.observe_waits()
        process = self.start("final")
        _ = self.wait_for_call()
        self.assertEqual(self.fixture.local_runs(), [])
        time.sleep(0.05)
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertIn("held by hana for between steps", output)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertEqual(len(self.fixture.local_runs()), 1)


    def test_one_admission_covers_all_steps_in_the_same_holder_generation(self) -> None:
        self.hold()
        process = self.start("final")
        _ = self.wait_for_call()
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        commands = [event["args"][0] for event in self.fixture.cargo_events()]
        self.assertIn("check", commands)
        self.assertIn("nextest", commands)
        self.assertEqual(output.count("natedev is held by"), 1)

    def test_interrupted_verification_removes_its_registered_call(self) -> None:
        self.hold()
        observation = self.observe_waits()
        process = self.start("test", "sample", "--local", "local admission test", "--filter", "smoke")
        _ = self.wait_for_call()
        time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        code, output = self.finish(process)
        self.assertEqual(code, 143, output)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertEqual([call for entry in read_cycle_file(self.cycle_file)["entries"] for call in entry.get("calls", [])], [])
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())


    def test_nextest_selects_its_width_after_the_hold_release(self) -> None:
        self.hold()
        clock = self.fixture.root / "width-clock"
        _ = clock.write_text("0")
        invoke = VERIFY.parent.parent / "lint" / "invoke.sh"
        environment = {
            **self.fixture.environment, "BUILDLOG_CALLER": "verify", "BUILDLOG_CALL_ID": "width-call",
            "BUILDLOG_HOLD_WAIT_FILE": str(self.fixture.root / "width-wait"),
            "BUILDLOG_HOLD_ADMISSION_FILE": str(self.fixture.root / "width-admission"),
            "BUILDLOG_HOLD_STARTED_FILE": str(self.fixture.root / "width-started"),
            "BUILDLOG_HOLD_WAITER_FILE": str(self.fixture.root / "width-waiter"),
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.fixture.root / "width-registration"),
            "TEST_WIDTH_CLOCK": str(clock),
        }
        script = '''set -euo pipefail
source "$1"
unset EPOCHSECONDS NEXTEST_TEST_THREADS
LINT_PHYSICAL_CORES=16
LINT_LOGICAL_CORES=32
date() { if [[ "${1:-}" == +%s ]]; then cat "$TEST_WIDTH_CLOCK"; else command date "$@"; fi; }
sweep_after_step() { :; }
run_nextest --package sample
'''
        process = subprocess.Popen(
            ["bash", "-c", script, "bash", str(invoke)], cwd=self.fixture.root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        _ = self.wait_for_call()
        self.assertEqual(self.fixture.local_runs(), [])
        _ = clock.write_text("6000")
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        runs = self.fixture.local_runs()
        self.assertEqual(len(runs), 1)
        arguments = runs[0]["args"]
        self.assertEqual(arguments[arguments.index("--test-threads") + 1], "32")

    def test_step_start_and_memory_wait_exclude_the_hold_wait(self) -> None:
        self.hold_after_first_step()
        observation = self.observe_waits()
        self.fixture.environment.update({"BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1"})
        process = self.start("final")
        _ = self.wait_for_call()
        time.sleep(0.05)
        grant_call(self.cycle_file)
        entry = read_cycle_file(self.cycle_file)["entries"][0]
        release = datetime.fromisoformat(entry.get("released_at", "")).timestamp()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        records = [cast(StepObservation, json.loads(line)) for line in (self.fixture.root / "step-observations").read_text().splitlines()]
        nextest = [record for record in records if record["arguments"][:2] == ["cargo", "nextest"]]
        self.assertEqual(len(nextest), 1)
        self.assertGreaterEqual(float(nextest[0]["start"]), release)
        self.assertEqual(float(nextest[0]["mem_wait"]), 0)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)

    def test_interrupt_during_a_step_wait_records_seconds_and_removes_the_call(self) -> None:
        self.hold_after_first_step()
        observation = self.observe_waits()
        process = self.start("final")
        _ = self.wait_for_call()
        time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        code, output = self.finish(process)
        self.assertEqual(code, 143, output)
        self.assertEqual(self.fixture.local_runs(), [])
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertEqual([call for entry in read_cycle_file(self.cycle_file)["entries"] for call in entry.get("calls", [])], [])

    def test_holder_joining_during_memory_admission_delays_step_and_records_separate_waits(self) -> None:
        invoke = VERIFY.parent.parent / "lint" / "invoke.sh"
        memory_clock = self.fixture.root / "memory-clock"
        _ = memory_clock.write_text("100")
        compiled = self.fixture.root / "compiled"
        observation = self.fixture.root / "memory-step"
        waits = self.fixture.root / "memory-hold-seconds"
        environment = {
            **self.fixture.environment,
            "BUILDLOG_CALLER": "verify", "BUILDLOG_CALL_ID": "memory-call",
            "BUILDLOG_HOLD_WAIT_FILE": str(waits),
            "BUILDLOG_HOLD_ADMISSION_FILE": str(self.fixture.root / "memory-admission"),
            "BUILDLOG_HOLD_STARTED_FILE": str(self.fixture.root / "memory-started"),
            "BUILDLOG_HOLD_WAITER_FILE": str(self.fixture.root / "memory-waiter"),
            "TEST_MEMORY_CLOCK": str(memory_clock), "TEST_COMPILED": str(compiled),
            "TEST_MEMORY_STEP": str(observation), "TEST_CYCLE_FILE": str(self.cycle_file),
        }
        script = '''set -euo pipefail
source "$1"
memory_calls=0
buildlog_begin() { BUILDLOG_START=100; BUILDLOG_PEAK=""; BUILDLOG_MEM_WAIT_S=0; }
buildlog_now() { BUILDLOG_NOW=$(cat "$TEST_MEMORY_CLOCK"); }
buildlog_wait_for_memory() {
    memory_calls=$((memory_calls + 1))
    if [[ "$memory_calls" == 1 ]]; then
        python3 - "$TEST_CYCLE_FILE" <<'PY_HOLD'
import json, os, sys
from datetime import datetime
from pathlib import Path
path = Path(sys.argv[1])
now = datetime.now().astimezone().isoformat()
holders = Path(os.environ["BUILD_HOLD_DIR"])
holders.mkdir()
(holders / "hana").write_text(json.dumps({"holder": "hana", "since": now, "for": "memory admission", "release_eta": "unknown"}))
path.parent.mkdir(parents=True)
(path.parent.parent / "current").write_text(path.parent.name)
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps({"id": path.parent.name, "opened_at": now, "release_started_at": "", "holders": {"hana": {"since": now, "released_at": ""}}, "recipients": {}, "entries": []}))
os.replace(temporary, path)
PY_HOLD
        BUILDLOG_MEM_WAIT_S=2
    else
        BUILDLOG_MEM_WAIT_S=3
    fi
    BUILDLOG_MEM_OUTCOME=Granted
    BUILDLOG_MEM_RESERVATION=""
}
buildlog_exec() { : > "$TEST_COMPILED"; }
buildlog_end() { printf '%s|%s|%s\\n' "$BUILDLOG_START" "$BUILDLOG_MEM_WAIT_S" "$memory_calls" > "$TEST_MEMORY_STEP"; }
sweep_after_step() { :; }
run_once cargo check
'''
        process = subprocess.Popen(
            ["bash", "-c", script, "bash", str(invoke)], cwd=self.fixture.root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.addCleanup(self.stop_process, process)
        _ = self.wait_for_call()
        self.assertFalse(compiled.exists())
        _ = memory_clock.write_text("200")
        grant_call(self.cycle_file)
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertTrue(compiled.exists())
        self.assertEqual(observation.read_text().strip(), "200|5|2")
        self.assertGreater(sum(float(line) for line in waits.read_text().splitlines()), 0)

    def test_memory_admission_hold_wait_is_recorded_by_verify_after_release(self) -> None:
        observation = self.observe_waits()
        observer = Path(self.fixture.environment["CLAUDE_PYTHON"])
        source = observer.read_text().replace(
            'os.execv(os.environ["TEST_REAL_PYTHON"],',
            '''if len(sys.argv) > 2 and sys.argv[1].endswith("/lint/memory_admit.py") and sys.argv[2] == "check":
    path = Path(os.environ["TEST_CYCLE_FILE"])
    if not path.exists():
        from datetime import datetime
        now = datetime.now().astimezone().isoformat()
        holders = Path(os.environ["BUILD_HOLD_DIR"])
        holders.mkdir()
        (holders / "hana").write_text(json.dumps({"holder": "hana", "since": now, "for": "memory admission", "release_eta": "unknown"}))
        path.parent.mkdir(parents=True)
        (path.parent.parent / "current").write_text(path.parent.name)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"id": path.parent.name, "opened_at": now, "release_started_at": "", "holders": {"hana": {"since": now, "released_at": ""}}, "recipients": {}, "entries": []}))
        os.replace(temporary, path)
os.execv(os.environ["TEST_REAL_PYTHON"],''',
        )
        _ = observer.write_text(source)
        self.fixture.environment.update({
            "TEST_CYCLE_FILE": str(self.cycle_file), "BUILDLOG_OFF": "0", "BUILDLOG_SYNC": "1",
        })
        process = self.start("test", "sample", "--local", "memory admission test", "--filter", "smoke")
        _ = self.wait_for_call()
        self.assertEqual(self.fixture.local_runs(), [])
        grant_call(self.cycle_file)
        release = datetime.fromisoformat(read_cycle_file(self.cycle_file)["entries"][0].get("released_at", "")).timestamp()
        code, output = self.finish(process)
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.fixture.local_runs()), 1)
        records = [cast(StepObservation, json.loads(line)) for line in (self.fixture.root / "step-observations").read_text().splitlines()]
        nextest = [record for record in records if record["arguments"][:2] == ["cargo", "nextest"]]
        self.assertEqual(len(nextest), 1)
        self.assertGreaterEqual(float(nextest[0]["start"]), release)
        self.assertEqual(float(nextest[0]["mem_wait"]), 0)
        observed = cast(WaitObservation, json.loads(observation.read_text()))
        self.assertGreater(float(observed["total"]), 0)
        self.assertAlmostEqual(float(observed["total"]), sum(float(value) for value in observed["waits"]), places=5)


if __name__ == "__main__":
    _ = unittest.main()
