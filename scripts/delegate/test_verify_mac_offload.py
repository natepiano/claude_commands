#!/usr/bin/env python3
"""Exercise verify.sh Mac offload routing with disposable command stand-ins."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast, final, override
from unittest import mock

from scripts.build_hold import build_hold


VERIFY = Path(__file__).with_name("verify.sh")


class CargoEvent(TypedDict):
    args: list[str]
    token_held: bool


class RunnerEvent(TypedDict):
    args: list[str]
    token_held: bool
    registered: bool
    at: float


class RoutingEvent(TypedDict):
    command: str
    status: int
    at: float
    pid: int


class RoutingRecord(TypedDict):
    target: str
    hold_wait_s: float
    mem_wait_s: str


class CallRecord(TypedDict):
    kind: str
    command: str
    outcome: str
    status: int | None
    token_wait_s: int
    mac: str
    mac_reason: str
    local_reason: str
    mac_s: float | None


class CargoTargetMetadata(TypedDict):
    name: str
    kind: list[str]
    test: bool
    src_path: str


class CargoPackageMetadata(TypedDict):
    name: str
    id: str
    features: dict[str, list[str]]
    manifest_path: str
    targets: list[CargoTargetMetadata]


class CargoMetadata(TypedDict):
    workspace_members: list[str]
    packages: list[CargoPackageMetadata]


CARGO_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
event = {
    "args": arguments,
    "token_held": Path(os.environ["TEST_TOKEN_RECORD"]).exists(),
}
with Path(os.environ["TEST_CARGO_EVENTS"]).open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")

if arguments[:1] == ["metadata"]:
    print(os.environ["TEST_METADATA"])
    raise SystemExit(0)
if arguments[:2] == ["nextest", "--version"]:
    raise SystemExit(0)
if arguments[:2] == ["nextest", "run"]:
    raise SystemExit(int(os.environ.get("TEST_LOCAL_STATUS", "0")))
raise SystemExit(0)
'''


GIT_STUB = r'''
from __future__ import annotations

import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
root = Path(os.environ["PACKAGE_ROOT"])
if "rev-parse" in arguments and "HEAD^{tree}" in arguments:
    print("fixed-tree")
    raise SystemExit(0)
if "rev-parse" in arguments and "--show-toplevel" in arguments:
    if "--git-common-dir" in arguments:
        print(root / ".git")
        print(root)
        if "--abbrev-ref" in arguments:
            print("main")
    else:
        print(root)
    raise SystemExit(0)
if "rev-parse" in arguments and "--short" in arguments:
    print("abc1234")
    raise SystemExit(0)
if "status" in arguments or "diff" in arguments or "ls-files" in arguments:
    raise SystemExit(0)
raise SystemExit(1)
'''


RUSTC_STUB = r'''
import sys

if sys.argv[1:] == ["-vV"]:
    print("rustc 1.90.0\nhost: x86_64-unknown-linux-gnu")
    raise SystemExit(0)
if sys.argv[1:] == ["-V"]:
    print("rustc 1.90.0")
    raise SystemExit(0)
raise SystemExit(1)
'''


RUNNER_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import time

arguments = sys.argv[1:]
root = Path(os.environ["TEST_RUNNER_STATE"])
result = Path(arguments[arguments.index("--result") + 1])
event = {
    "args": arguments,
    "token_held": Path(os.environ["TEST_TOKEN_RECORD"]).exists(),
    "registered": Path(os.environ["BUILDLOG_HOLD_REGISTRATION_FILE"]).exists(),
    "at": time.monotonic(),
}
with (root / "runner-events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")
(root / "runner-started").write_text("started\n", encoding="utf-8")

attempt = len((root / "runner-events.jsonl").read_text().splitlines())
last_decline = arguments[arguments.index("--last-decline") + 1] if "--last-decline" in arguments else ""

def decline(reason: str, seconds: str) -> None:
    result.write_text("mac=declined\nreason=" + reason + "\nseconds=" + seconds + "\n")
    if reason not in ("busy", "blocked") or reason != last_decline:
        print("claim diagnostic: " + reason, file=sys.stderr, flush=True)
        print("mac_test: not running on the Mac (" + reason + ")", flush=True)

if os.environ.get("TEST_RUNNER_BUSY_UNTIL") and not Path(os.environ["TEST_RUNNER_BUSY_UNTIL"]).exists():
    reason_file = root / "busy-reason"
    reason = reason_file.read_text().strip() if reason_file.exists() else os.environ.get("TEST_RUNNER_BUSY_REASON", "busy")
    decline(reason, os.environ.get("TEST_DECLINED_RETRY_SECONDS", "0") if attempt > 1 else "0")
    raise SystemExit(75)
if attempt > 1 and os.environ.get("TEST_RUNNER_PAUSE_RETRY") == "1":
    (root / "retry-running").write_text("running\n")
    while not (root / "continue-retry").exists():
        time.sleep(0.01)

if os.environ.get("TEST_RUNNER_HOLD") == "1":
    def stop(signum: int, _frame: object) -> None:
        (root / "runner-signal").write_text(str(signum), encoding="utf-8")
        raise SystemExit(128 + signum)

    for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signal_number, stop)
    while True:
        signal.pause()

mode = os.environ.get("TEST_RUNNER_MAC", "declined")
if mode in ("passed", "failed", "lost"):
    print("mac_test: running on the Mac (macOS, host Mac); tree copied in 0.0 s", flush=True)
if mode == "none":
    result.unlink(missing_ok=True)
elif mode == "empty":
    result.write_text("", encoding="utf-8")
elif mode == "directory":
    result.unlink(missing_ok=True)
    result.mkdir()
else:
    result.write_text(
        "mac=" + mode + "\n"
        + "reason=" + os.environ.get("TEST_RUNNER_REASON", "") + "\n"
        + "seconds=" + os.environ.get("TEST_RUNNER_SECONDS", "3.25") + "\n",
        encoding="utf-8",
    )
if mode == "declined":
    decline(os.environ.get("TEST_RUNNER_REASON", ""), os.environ.get("TEST_RUNNER_SECONDS", "3.25"))
output = os.environ.get("TEST_RUNNER_OUTPUT", "")
if output and not (mode == "declined" and last_decline in ("busy", "blocked") and last_decline == os.environ.get("TEST_RUNNER_REASON", "")):
    print(output, flush=True)
raise SystemExit(int(os.environ.get("TEST_RUNNER_STATUS", "75")))
'''


TARGETS_WRAPPER = r'''
import json
import os
from pathlib import Path
import runpy
import sys
import time

command = sys.argv[1]
root = Path(os.environ["TEST_RUNNER_STATE"])
if command == "wait-local" and os.environ.get("TEST_LOCAL_WAIT_SECONDS") and not (root / "local-wait-added").exists():
    with Path(os.environ["BUILDLOG_HOLD_WAIT_FILE"]).open("a") as output:
        output.write(os.environ["TEST_LOCAL_WAIT_SECONDS"] + "\n")
    (root / "local-wait-added").write_text("added\n")
sys.path.insert(0, str(Path(os.environ["TEST_REAL_TARGETS"]).parent))
status = 0
try:
    runpy.run_path(os.environ["TEST_REAL_TARGETS"], run_name="__main__")
except SystemExit as result:
    status = result.code or 0
event = {"command": command, "status": status, "at": time.monotonic(),
         "pid": int(sys.argv[sys.argv.index("--pid") + 1]) if "--pid" in sys.argv else 0}
with (root / "routing-events.jsonl").open("a") as output:
    output.write(json.dumps(event) + "\n")
if command == "register-local" and status == 10:
    (root / "registered").write_text("registered\n")
raise SystemExit(status)
'''


SLEEP_STUB = r'''
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["TEST_RUNNER_STATE"])
with (root / "sleep-events").open("a") as output:
    output.write(sys.argv[1] + "\n")
if os.environ.get("TEST_STEP_ROUTING_SLEEP") == "1":
    step = len((root / "sleep-events").read_text().splitlines())
    (root / "routing-sleep").write_text("sleeping\n")
    (root / ("routing-sleep-" + str(step))).write_text("sleeping\n")
    while not (root / ("continue-sleep-" + str(step))).exists():
        time.sleep(0.01)
elif os.environ.get("TEST_PAUSE_ROUTING_SLEEP") == "1":
    (root / "routing-sleep").write_text("sleeping\n")
    while not (root / "continue-sleep").exists():
        time.sleep(0.01)
else:
    time.sleep(min(float(sys.argv[1]), 0.05))
'''


RECORD_WRAPPER = r'''
import json
import os
from pathlib import Path
import runpy
import sys

if sys.argv[1:2] == ["call"]:
    event = {"target": os.environ.get("BUILDLOG_TARGET", ""),
             "hold_wait_s": float(os.environ.get("BUILDLOG_HOLD_WAIT_S", "0")),
             "mem_wait_s": os.environ.get("BUILDLOG_MEM_WAIT_S", "0")}
    with (Path(os.environ["TEST_RUNNER_STATE"]) / "routing-records.jsonl").open("a") as output:
        output.write(json.dumps(event) + "\n")
sys.path.insert(0, str(Path(os.environ["TEST_REAL_RECORD"]).parent))
runpy.run_path(os.environ["TEST_REAL_RECORD"], run_name="__main__")
'''


@final
class VerifyMacOffloadTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.bin_directory = Path()
        self.board = Path()
        self.runner = Path()
        self.runner_state = Path()
        self.cargo_events_path = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        claude = self.home / ".claude"
        scripts = claude / "scripts"
        scripts.mkdir(parents=True)
        (claude / "config").symlink_to(VERIFY.parent.parent.parent / "config", target_is_directory=True)
        for source in VERIFY.parent.parent.iterdir():
            if source.name != "buildlog":
                (scripts / source.name).symlink_to(source, target_is_directory=source.is_dir())
        log = scripts / "buildlog"
        log.mkdir()
        self.write_executable(log / "record.py", RECORD_WRAPPER)
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        self.board = self.root / "board"
        self.runner_state = self.root / "runner-state"
        self.runner_state.mkdir()
        self.cargo_events_path = self.root / "cargo-events.jsonl"
        self.runner = self.root / "runner.py"
        self.write_executable(self.bin_directory / "cargo", CARGO_STUB)
        self.write_executable(self.bin_directory / "git", GIT_STUB)
        self.write_executable(self.bin_directory / "rustc", RUSTC_STUB)
        self.write_executable(self.runner, RUNNER_STUB)
        blocker = "#!/bin/sh\nexit 99\n"
        for name in ("ssh", "rsync", "scp", "gh", "systemd-run"):
            self.write_executable(self.bin_directory / name, blocker)
        reader = self.root / "lint-config.sh"
        _ = reader.write_text(
            "lint_config_enabled() { return 1; }\n"
            + "lint_config_skip_notice() { :; }\n",
            encoding="utf-8",
        )
        for name in (
            "tmp",
            "runtime",
            "state",
            "cache",
            "target",
            "cgroup",
            "cargo-home",
            ".git",
        ):
            (self.root / name).mkdir()
        _ = (self.root / "Cargo.toml").write_text(
            '[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n',
            encoding="utf-8",
        )
        source = self.root / "src" / "lib.rs"
        source.parent.mkdir()
        _ = source.write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
        meminfo = self.root / "meminfo"
        _ = meminfo.write_text("MemAvailable: 67108864 kB\n", encoding="utf-8")
        settings = self.root / "mac settings.conf"
        _ = settings.write_text(f"offload=on\nlinux_only.{self.root.name}=\n")
        config = self.root / "targets.conf"
        _ = config.write_text("targets=mac,natedev\nkind.mac=ssh\nplatform.mac=macos\n"
                              + f"settings.mac={settings.name}\nkind.natedev=local\nplatform.natedev=linux\n")
        metadata: CargoMetadata = {
            "workspace_members": ["sample-id"],
            "packages": [
                {
                    "name": "sample",
                    "id": "sample-id",
                    "features": {"default": [], "extra": []},
                    "manifest_path": str(self.root / "Cargo.toml"),
                    "targets": [
                        {
                            "name": "sample",
                            "kind": ["lib"],
                            "test": True,
                            "src_path": str(source),
                        }
                    ],
                }
            ],
        }
        self.environment = {
            **{
                name: value
                for name, value in os.environ.items()
                if name
                not in {
                    "PLAN_DELEGATE_BOARD_DIR",
                    "PLAN_DELEGATE_SESSION_DIR",
                    "PLAN_DELEGATE_TEAM_ROLE",
                    "BUILDLOG_MAC",
                    "BUILDLOG_MAC_REASON",
                    "BUILDLOG_LOCAL_REASON",
                    "BUILDLOG_MAC_S",
                }
            },
            "HOME": str(self.home),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "PACKAGE_ROOT": str(self.root),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "CARGO_HOME": str(self.root / "cargo-home"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "BUILDLOG_BUILDS_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_CI_CGROUP": str(self.root / "cgroup"),
            "BUILDLOG_ZRAM": str(self.root / "zram"),
            "BUILDLOG_MEMINFO": str(meminfo),
            "BUILDLOG_OFF": "1",
            "BUILDLOG_SCOPE": "0",
            "BUILD_HOLD_DIR": str(self.root / "build-hold"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "BUILD_TARGETS_SCRIPT": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_ADMISSION_FILE": "",
            "BUILDLOG_HOLD_STARTED_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_HOLD_REGISTRATION_FILE": str(self.root / "registration"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "MAC_TEST_STATE_DIR": str(self.root / "mac-state"),
            "BUILD_TARGETS_CONFIG": str(config),
            "MAC_TEST_CONFIG": str(settings),
            "CLAUDE_CODE_SESSION_ID": "", "BUILD_TARGETS_NOW": "",
            "BUILD_TARGETS_POLL_S": "0.05",
            "LINT_CONFIG_READER": str(reader),
            "LINT_CONFIG_FILE": str(reader),
            "PLAN_DELEGATE_BOARD_DIR": str(self.board),
            "PLAN_DELEGATE_TEAM_ROLE": "test",
            "VERIFY_MAC_RUNNER": str(self.runner),
            "TEST_RUNNER_STATE": str(self.runner_state),
            "TEST_CARGO_EVENTS": str(self.cargo_events_path),
            "TEST_TOKEN_RECORD": str(self.board / "locks" / "cargo.d" / "owned"),
            "TEST_METADATA": json.dumps(metadata),
            "TEST_RUNNER_SECONDS": "3.25",
            "TEST_REAL_RECORD": str(VERIFY.parent.parent / "buildlog" / "record.py"),
        }

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(VERIFY), *arguments],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def start_verify(
        self, *arguments: str, extra: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["bash", str(VERIFY), *arguments],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def cargo_events(self) -> list[CargoEvent]:
        if not self.cargo_events_path.exists():
            return []
        return [
            cast(CargoEvent, json.loads(line))
            for line in self.cargo_events_path.read_text(encoding="utf-8").splitlines()
        ]

    def runner_events(self) -> list[RunnerEvent]:
        path = self.runner_state / "runner-events.jsonl"
        if not path.exists():
            return []
        return [
            cast(RunnerEvent, json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def local_runs(self) -> list[CargoEvent]:
        return [
            event
            for event in self.cargo_events()
            if event["args"][:2] == ["nextest", "run"]
        ]

    def call_records(self) -> list[CallRecord]:
        return [
            cast(CallRecord, json.loads(line))
            for path in (self.root / "buildlog").glob("*/*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if cast(dict[str, object], json.loads(line)).get("kind") == "call"
        ]

    def last_call(self) -> CallRecord:
        records = self.call_records()
        self.assertTrue(records)
        return records[-1]

    def saved_results(self) -> list[Path]:
        cache = self.board / "verify_cache"
        return list(cache.glob("*.pass")) + list(cache.glob("*.fail"))

    def clear_observations(self) -> None:
        self.cargo_events_path.unlink(missing_ok=True)
        for name in ("runner-events.jsonl", "runner-started", "runner-signal"):
            (self.runner_state / name).unlink(missing_ok=True)

    def wait_for_path(self, path: Path) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def routing_fixture(self) -> Path:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        self.write_executable(self.bin_directory / "sleep", SLEEP_STUB)
        self.environment.update({"BUILD_TARGETS_SCRIPT": str(wrapper),
                                 "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py")})
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, "hana", "render checks", build_hold.NoReleaseEta(), datetime.now().astimezone())
            cycle = build_hold.read_cycle()
            assert cycle is not None
            return self.root / "release" / cycle["id"] / "cycle.json"

    @contextmanager
    def hold_environment(self) -> Generator[None, None, None]:
        with mock.patch.dict(os.environ, {name: self.environment[name] for name in (
            "BUILD_HOLD_DIR", "BUILD_HOLD_RELEASE_DIR", "PLAN_DELEGATE_ACTIVE_DIR")}), \
             mock.patch.dict(os.environ, {"PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
                                          "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": ""}), \
             mock.patch.object(build_hold, "socket_for", return_value=build_hold.RecipientLive("/test/socket")), \
             mock.patch.object(build_hold, "send_release", return_value=build_hold.ReleaseDelivered()):
            yield

    def release_local_turn(self) -> None:
        with self.hold_environment(), build_hold.release_lock():
            cycle = build_hold.read_cycle()
            assert cycle is not None
            now = datetime.now().astimezone()
            cycle["release_started_at"] = now.isoformat()
            build_hold.save_cycle(cycle)
            _ = build_hold.advance_release(cycle, now, clock=lambda: now)

    def routing_events(self) -> list[RoutingEvent]:
        path = self.runner_state / "routing-events.jsonl"
        return [cast(RoutingEvent, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def routing_record(self) -> RoutingRecord:
        records = (self.runner_state / "routing-records.jsonl").read_text().splitlines()
        self.assertTrue(records)
        return cast(RoutingRecord, json.loads(records[-1]))

    def stop_verify(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        _ = process.communicate(timeout=5)

    def start_routing(self, reason: str = "busy", *, pause_retry: bool = False,
                      step_sleep: bool = False) -> subprocess.Popen[str]:
        process = self.start_verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
            "TEST_RUNNER_BUSY_UNTIL": str(self.runner_state / "mac-free"),
            "TEST_RUNNER_BUSY_REASON": reason, "TEST_PAUSE_ROUTING_SLEEP": "1",
            "TEST_RUNNER_PAUSE_RETRY": "1" if pause_retry else "0",
            "TEST_STEP_ROUTING_SLEEP": "1" if step_sleep else "0",
            "TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0",
        })
        self.addCleanup(self.stop_verify, process)
        self.wait_for_path(self.runner_state / "routing-sleep")
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 1)
        self.assertIsNone(process.poll())
        return process

    def assert_no_pending_calls(self, cycle_path: Path) -> None:
        cycle = cast(build_hold.HoldCycle, json.loads(cycle_path.read_text()))
        self.assertFalse(any(entry["calls"] or entry["active_call_id"] for entry in cycle["entries"]))

    def test_local_first_target_runs_without_trying_free_mac(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        result = self.verify("test", "sample", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        self.assertIn("verify.sh: running on natedev", result.stdout)
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_alternate_target_settings_are_passed_to_runner(self) -> None:
        alternate = self.root / "alternate Mac.conf"
        _ = alternate.write_text(f"linux_only.{self.root.name}=\n")
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("mac settings.conf", alternate.name))
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache",
                             extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        arguments = self.runner_events()[0]["args"]
        self.assertEqual(arguments[arguments.index("--settings") + 1], str(alternate))
        self.assertIn("mac_test: running on the Mac (macOS, host Mac); tree copied in 0.0 s", result.stdout)
        self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
        self.assertEqual(self.routing_record()["target"], "mac")

    def test_excluded_mac_reason_is_recorded_without_calling_offload(self) -> None:
        for settings, reason in (("", "repo"), (f"linux_only.{self.root.name}=sample\n", "linux_only")):
            with self.subTest(reason=reason):
                self.clear_observations()
                _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(settings)
                result = self.verify("test", "sample", "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(self.last_call()["local_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")

    def test_declined_mac_attempt_announces_only_local_execution(self) -> None:
        for reason in ("busy", "blocked", "battery"):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_MAC": "declined", "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": "75"})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("verify.sh: running on the Mac", result.stdout + result.stderr)
                self.assertNotIn("mac_test: running on the Mac", result.stdout + result.stderr)
                self.assertIn("verify.sh: running on natedev", result.stdout)
                self.assertEqual(len(self.local_runs()), 1)

    def test_unreadable_local_first_route_still_tries_the_eligible_mac(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        for outcome, reason, status, expected in (("passed", "", "0", 0), ("declined", "busy", "75", 12)):
            with self.subTest(outcome=outcome):
                self.clear_observations()
                result = self.verify("test", "sample", "--filter", "alpha", "--no-cache", extra={
                    "TEST_RUNNER_MAC": outcome, "TEST_RUNNER_REASON": reason, "TEST_RUNNER_STATUS": status})
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(len(self.runner_events()), 1)
                self.assertEqual(self.local_runs(), [])
                self.assertIn("natedev's hold state cannot be read", result.stderr)

    def test_unreadable_local_first_route_without_eligible_mac_exits_twelve(self) -> None:
        config = Path(self.environment["BUILD_TARGETS_CONFIG"])
        _ = config.write_text(config.read_text().replace("targets=mac,natedev", "targets=natedev,mac"))
        _ = Path(self.environment["MAC_TEST_CONFIG"]).write_text(f"linux_only.{self.root.name}=sample\n")
        damaged = self.root / "damaged-holders"
        _ = damaged.write_text("a file cannot be a hold directory\n")
        self.environment["BUILD_HOLD_DIR"] = str(damaged)
        result = self.verify("test", "sample", "--filter", "alpha", "--no-cache")
        self.assertEqual(result.returncode, 12, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertIn("natedev's hold state cannot be read", result.stderr)

    def resume_routing_sleep(self, step: int) -> None:
        _ = (self.runner_state / f"continue-sleep-{step}").write_text("continue\n")
        self.wait_for_path(self.runner_state / f"routing-sleep-{step + 1}")

    def test_repeated_routing_wait_shows_each_unchanged_diagnostic_once(self) -> None:
        _ = self.routing_fixture()
        self.environment["BUILD_TARGETS_ESCALATE_S"] = "0"
        process = self.start_routing(step_sleep=True)
        for step in range(1, 4):
            self.resume_routing_sleep(step)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-4").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(errors.count("verify.sh: natedev is held by hana for render checks.\n"), 1)
        self.assertEqual(errors.count("verify.sh: natedev has been held for 2 h by hana; ask them.\n"), 1)
        self.assertEqual(errors.count("verify.sh: waiting for the Mac (busy) or natedev's release.\n"), 1)
        self.assertEqual(errors.count("claim diagnostic: busy\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertNotIn("verify.sh: running on the Mac", output + errors)
        attempts = self.runner_events()
        self.assertEqual(len(attempts), 5)
        self.assertNotIn("--last-decline", attempts[0]["args"])
        for event in attempts[1:]:
            arguments = event["args"]
            self.assertEqual(arguments[arguments.index("--last-decline") + 1], "busy")

    def test_routing_wait_shows_changed_holder_and_decline_reason_once(self) -> None:
        _ = self.routing_fixture()
        process = self.start_routing(step_sleep=True)
        with self.hold_environment():
            holders = Path(self.environment["BUILD_HOLD_DIR"])
            _ = build_hold.start_hold(holders, "julian", "restore CI", build_hold.NoReleaseEta(), datetime.now().astimezone())
            (holders / "hana").unlink()
        self.resume_routing_sleep(1)
        self.resume_routing_sleep(2)
        _ = (self.runner_state / "busy-reason").write_text("blocked\n")
        self.resume_routing_sleep(3)
        self.resume_routing_sleep(4)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-5").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        for line in ("verify.sh: natedev is held by hana for render checks.\n",
                     "verify.sh: natedev is held by julian for restore CI.\n",
                     "verify.sh: waiting for the Mac (busy) or natedev's release.\n",
                     "verify.sh: waiting for the Mac (blocked) or natedev's release.\n"):
            self.assertEqual(errors.count(line), 1, output + errors)
        self.assertEqual(output.count("mac_test: not running on the Mac (busy)\n"), 1)
        self.assertEqual(output.count("mac_test: not running on the Mac (blocked)\n"), 1)
        attempts = self.runner_events()
        self.assertEqual([event["args"][event["args"].index("--last-decline") + 1] for event in attempts[1:]],
                         ["busy", "busy", "busy", "blocked", "blocked"])

    def test_hold_wait_includes_declined_retry_seconds_and_excludes_running_retry(self) -> None:
        _ = self.routing_fixture()
        self.environment.update({"TEST_DECLINED_RETRY_SECONDS": "4.5", "TEST_RUNNER_SECONDS": "20"})
        process = self.start_routing(step_sleep=True)
        self.resume_routing_sleep(1)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep-2").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(len(self.runner_events()), 3)
        self.assertGreaterEqual(self.routing_record()["hold_wait_s"], 4.5)
        self.assertLess(self.routing_record()["hold_wait_s"], 6.5)
        self.assertEqual(self.last_call()["mac_s"], 20)

    def test_busy_mac_and_held_local_wait_then_use_first_free_mac(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("waiting", errors)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(len(self.runner_events()), 2)
        self.assertFalse(self.runner_events()[1]["registered"])
        self.assert_no_pending_calls(cycle)
        commands = [event["command"] for event in self.routing_events()]
        self.assertLess(commands.index("finish-local"), len(commands))
        registrations = [event for event in self.routing_events() if event["command"] == "register-local"]
        self.assertTrue(registrations)
        self.assertTrue(all(event["pid"] == process.pid for event in registrations))
        self.assertEqual(self.routing_record()["target"], "mac")
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)

    def test_local_release_or_admission_wins_before_another_mac_attempt(self) -> None:
        for held_file_remains in (False, True):
            with self.subTest(held_file_remains=held_file_remains):
                # This test owns a separate fixture for each release variant.
                fixture = VerifyMacOffloadTests("runTest")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                cycle = fixture.routing_fixture()
                process = fixture.start_routing()
                if held_file_remains:
                    fixture.release_local_turn()
                else:
                    (Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").unlink()
                _ = (fixture.runner_state / "continue-sleep").write_text("continue\n")
                output, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, output + errors)
                self.assertEqual(len(fixture.runner_events()), 1)
                self.assertEqual(len(fixture.local_runs()), 1)
                self.assertEqual((Path(fixture.environment["BUILD_HOLD_DIR"]) / "hana").exists(), held_file_remains)
                fixture.assert_no_pending_calls(cycle)
                self.assertEqual(fixture.routing_record()["target"], "natedev")

    def test_blocked_mac_and_held_local_wait_for_local_release(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing("blocked")
        self.release_local_turn()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("blocked", errors)
        self.assertEqual(len(self.runner_events()), 1)
        self.assertEqual(len(self.local_runs()), 1)
        self.assert_no_pending_calls(cycle)

    def test_mac_run_withdraws_local_demand_before_another_waiter_is_released(self) -> None:
        cycle_path = self.routing_fixture()
        process = self.start_routing(pause_retry=True)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        self.assert_no_pending_calls(cycle_path)
        with self.hold_environment():
            _ = build_hold.register_wait("verify:peer-call", call_id="peer-call", pid=os.getpid())
        self.release_local_turn()
        with self.hold_environment():
            self.assertIsInstance(build_hold.read_admission("verify:peer-call", "peer-call"), build_hold.AdmittedTurn)
        self.assertIsNone(process.poll())
        self.assertFalse(self.runner_events()[-1]["registered"])
        _ = (self.runner_state / "continue-retry").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertEqual(self.local_runs(), [])

    def test_interrupted_mac_retry_withdraws_registration_and_preserves_only_wait_time(self) -> None:
        cycle_path = self.routing_fixture()
        process = self.start_routing(pause_retry=True)
        time.sleep(0.1)
        _ = (self.runner_state / "mac-free").write_text("free\n")
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        self.wait_for_path(self.runner_state / "retry-running")
        remote_started = time.monotonic()
        time.sleep(0.5)
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle_path)
        record = self.routing_record()
        self.assertGreater(record["hold_wait_s"], 0)
        records = [event for event in self.routing_events() if event["command"] == "register-local"]
        self.assertLess(record["hold_wait_s"], remote_started - records[0]["at"] + 0.15)
        self.assertEqual(self.last_call()["outcome"], "interrupted")

    def test_routing_wait_and_local_wait_are_summed_without_test_runtime(self) -> None:
        _ = self.routing_fixture()
        self.environment["TEST_LOCAL_WAIT_SECONDS"] = "7.25"
        process = self.start_routing()
        time.sleep(0.15)
        self.release_local_turn()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)
        record = self.routing_record()
        self.assertGreater(record["hold_wait_s"], 7.25)
        self.assertLess(record["hold_wait_s"], 9.25)
        self.assertEqual(record["mem_wait_s"], "0")

    def test_default_routing_retry_is_paced_fifteen_seconds(self) -> None:
        _ = self.routing_fixture()
        _ = self.environment.pop("BUILD_TARGETS_POLL_S")
        process = self.start_routing()
        self.assertEqual((self.runner_state / "sleep-events").read_text().splitlines(), ["15"])
        self.release_local_turn()
        _ = (self.runner_state / "continue-sleep").write_text("continue\n")
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output + errors)

    def test_interrupted_routing_sleep_records_wait_and_cleans_pending_call(self) -> None:
        cycle = self.routing_fixture()
        process = self.start_routing()
        time.sleep(0.1)
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 143, output + errors)
        self.assert_no_pending_calls(cycle)
        self.assertGreater(self.routing_record()["hold_wait_s"], 0)
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.last_call()["outcome"], "interrupted")
        self.assertEqual(list((self.root / "tmp").glob("verify-hold.*")), [])

    def test_mac_state_and_probe_diagnostics_survive_local_fallback(self) -> None:
        for reason, diagnostic in (("state", "mac state cannot be read: invalid block"),
                                   ("blocked", "retained expired block; waiting for removal"),
                                   ("unreachable", "ssh probe failed")):
            with self.subTest(reason=reason):
                self.clear_observations()
                result = self.verify("test", "sample", "--no-cache", extra={
                    "TEST_RUNNER_REASON": reason, "TEST_RUNNER_OUTPUT": diagnostic})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                self.assertEqual(self.last_call()["mac_reason"], reason)
                self.assertEqual(self.routing_record()["target"], "natedev")

    def test_decline_runs_same_nextest_words_locally_after_runner(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--filter",
            "beta",
            "--features",
            "extra",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "busy",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": "runner output",
            },
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runner output", result.stdout)
        runners = self.runner_events()
        local = self.local_runs()
        self.assertEqual(len(runners), 1)
        self.assertEqual(len(local), 1)
        runner_arguments = runners[0]["args"]
        self.assertEqual(runner_arguments[0], "run")
        self.assertEqual(local[0]["args"][2], "--test-threads")
        self.assertGreater(int(local[0]["args"][3]), 0)
        self.assertEqual(
            runner_arguments[runner_arguments.index("--") + 1 :],
            local[0]["args"][4:],
        )
        self.assertIn("--filter-run", runner_arguments)
        self.assertNotIn("--no-cache", runner_arguments)
        self.assertFalse(runners[0]["token_held"])
        self.assertTrue(local[0]["token_held"])
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "busy")
        self.assertEqual(record["mac_s"], 3.25)
        self.assertNotIn("--no-cache", record["command"])

    def test_no_test_match_runs_locally_and_reaches_call_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "TEST_RUNNER_MAC": "declined",
                "TEST_RUNNER_REASON": "no_tests",
                "TEST_RUNNER_SECONDS": "4.5",
                "TEST_RUNNER_STATUS": "75",
                "TEST_RUNNER_OUTPUT": (
                    "mac_test: no test matched on the Mac; "
                    "running on natedev instead"
                ),
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead",
            result.stdout,
        )
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "declined")
        self.assertEqual(record["mac_reason"], "no_tests")
        self.assertEqual(record["mac_s"], 4.5)

    def test_runner_result_uses_fixture_temp_directory(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "declined", "TEST_RUNNER_STATUS": "75"},
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runner = self.runner_events()[0]
        result_index = runner["args"].index("--result") + 1
        self.assertTrue(
            Path(runner["args"][result_index]).is_relative_to(self.root / "tmp"),
            runner,
        )

    def test_lost_run_continues_locally(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "lost", "TEST_RUNNER_STATUS": "75"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.local_runs()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "lost")
        self.assertEqual(record["mac_s"], 3.25)

    def test_mac_failure_stops_before_token_and_local_run(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.local_runs(), [])
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertFalse(runners[0]["token_held"])
        self.assertEqual(self.saved_results(), [])
        record = self.last_call()
        self.assertEqual(record["status"], 42)
        self.assertEqual(record["mac"], "failed")
        self.assertEqual(record["mac_reason"], "")
        self.assertEqual(record["mac_s"], 3.25)

    def test_unusable_runner_results_continue_locally(self) -> None:
        cases = (
            ("none", "75"),
            ("none", "1"),
            ("empty", "75"),
            ("directory", "75"),
            ("other", "75"),
            ("passed", "1"),
            ("failed", "0"),
        )
        for mode, status in cases:
            with self.subTest(mode=mode, status=status):
                self.clear_observations()
                result = self.verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_MAC": mode, "TEST_RUNNER_STATUS": status},
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(self.local_runs()), 1)
                record = self.last_call()
                self.assertEqual(record["mac"], "declined")
                self.assertEqual(record["mac_reason"], "runner")
                self.assertIsNone(record["mac_s"])

    def test_full_mac_pass_is_confirmed_locally_and_records_local_pass(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "passed on the Mac (macOS); now confirming on natedev, which is the gate.",
            result.stdout,
        )
        self.assertEqual(len(self.local_runs()), 1)
        self.assertTrue(self.local_runs()[0]["token_held"])
        self.assertEqual(len(self.saved_results()), 1)
        record = self.last_call()
        self.assertEqual(record["mac"], "passed")
        self.assertEqual(record["mac_s"], 3.25)
        self.assertEqual(self.routing_record()["target"], "natedev")

    def test_filtered_mac_pass_ends_without_local_run_or_pass_record(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--filter",
            "alpha",
            "--no-cache",
            extra={"TEST_RUNNER_MAC": "passed", "TEST_RUNNER_STATUS": "0"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "PASS on the Mac (macOS). A filtered run is feedback, so nothing ran on natedev.",
            result.stdout,
        )
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), [])
        record = self.last_call()
        self.assertEqual(record["status"], 0)
        self.assertEqual(record["mac"], "passed_filter")
        self.assertEqual(record["mac_s"], 3.25)

    def test_local_flag_requires_a_non_option_reason_before_any_work(self) -> None:
        expected = (
            'verify.sh: --local needs a reason: --local "<why this test must run on '
            'Linux>". Leave it off to run on the Mac.\n'
        )
        cases = (
            ("test", "sample", "--local"),
            ("test", "sample", "--local", ""),
            ("test", "sample", "--local", " \t "),
            ("test", "sample", "--local", "--filter", "alpha"),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                call_count = len(self.call_records())
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, expected)
                self.assertEqual(self.cargo_events(), [])
                self.assertEqual(self.runner_events(), [])
                self.assertEqual(len(self.call_records()), call_count)

    def test_local_flag_skips_runner_records_reason_and_shares_pass_key(self) -> None:
        first_reason = 'needs Linux path handling with a "quoted" mount'
        first = self.verify(
            "test",
            "sample",
            "--local",
            first_reason,
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        first_record = self.last_call()
        self.assertEqual(first_record["command"], "test sample")
        self.assertEqual(first_record["mac"], "declined")
        self.assertEqual(first_record["mac_reason"], "local_flag")
        self.assertEqual(first_record["local_reason"], first_reason)
        self.assertNotIn(first_reason, first_record["command"])
        saved_results = self.saved_results()
        self.assertEqual(len(saved_results), 1)

        self.clear_observations()
        without_flag = self.verify("test", "sample")
        self.assertEqual(
            without_flag.returncode, 0, without_flag.stdout + without_flag.stderr
        )
        self.assertIn("PASS (recorded)", without_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)

        self.clear_observations()
        second_reason = "requires Linux-only process behavior"
        with_flag = self.verify("test", "sample", "--local", second_reason)
        self.assertEqual(with_flag.returncode, 0, with_flag.stdout + with_flag.stderr)
        self.assertIn("PASS (recorded)", with_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertEqual(self.saved_results(), saved_results)
        second_record = self.last_call()
        self.assertEqual(second_record["local_reason"], second_reason)
        self.assertEqual(second_record["command"], "test sample")

    def test_local_flag_parses_before_and_after_filter(self) -> None:
        reason = "uses Linux filesystem semantics"
        cases = (
            ("test", "sample", "--local", reason, "--filter", "alpha"),
            ("test", "sample", "--filter", "alpha", "--local", reason),
        )

        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments, "--no-cache")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])
                local = self.local_runs()
                self.assertEqual(len(local), 1)
                self.assertNotIn("--local", local[0]["args"])
                self.assertNotIn(reason, local[0]["args"])
                record = self.last_call()
                self.assertEqual(record["command"], "test sample --filter alpha")
                self.assertEqual(record["mac_reason"], "local_flag")
                self.assertEqual(record["local_reason"], reason)

    def test_missing_runner_skips_offload(self) -> None:
        result = self.verify(
            "test",
            "sample",
            "--no-cache",
            extra={"VERIFY_MAC_RUNNER": str(self.root / "absent-runner.py")},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)

    def test_other_verbs_never_call_runner(self) -> None:
        for arguments in (("check", "sample"), ("lint", "sample"), ("final",)):
            with self.subTest(arguments=arguments):
                self.clear_observations()
                result = self.verify(*arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.runner_events(), [])

    def test_runner_receives_each_forwarded_signal_before_local_work(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_observations()
                process = self.start_verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_HOLD": "1"},
                )
                try:
                    self.wait_for_path(self.runner_state / "runner-started")
                    process.send_signal(signal_number)
                    output, errors = process.communicate(timeout=15)
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    _ = process.communicate(timeout=5)
                    raise
                self.assertEqual(
                    process.returncode, 128 + signal_number, output + errors
                )
                self.assertEqual(
                    (self.runner_state / "runner-signal").read_text(
                        encoding="utf-8"
                    ),
                    str(signal_number),
                )
                self.assertEqual(self.local_runs(), [])
                runners = self.runner_events()
                self.assertEqual(len(runners), 1)
                self.assertFalse(runners[0]["token_held"])

    def test_signal_before_runner_start_removes_result_without_local_work(self) -> None:
        marker = self.runner_state / "before-runner"
        gate = self.root / "debug-gate.sh"
        _ = gate.write_text(
            "set -T\n"
            + "before_mac_runner() {\n"
            + "    if [[ \"$BASH_COMMAND\" == *'\"$MAC_RUNNER\"'* "
            + "&& \"$BASH_COMMAND\" == *'\"${runner_args[@]}\"'* ]]; then\n"
            + "        trap - DEBUG\n"
            + "        : > \"$TEST_BEFORE_RUNNER_MARKER\"\n"
            + "        while [[ ! -e \"$TEST_CONTINUE_RUNNER\" ]]; do\n"
            + "            sleep 0.01\n"
            + "        done\n"
            + "    fi\n"
            + "}\n"
            + "trap before_mac_runner DEBUG\n",
            encoding="utf-8",
        )
        process = self.start_verify(
            "test",
            "sample",
            "--no-cache",
            extra={
                "BASH_ENV": str(gate),
                "TEST_BEFORE_RUNNER_MARKER": str(marker),
                "TEST_CONTINUE_RUNNER": str(self.runner_state / "continue-runner"),
            },
        )
        try:
            self.wait_for_path(marker)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=15)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            _ = process.communicate(timeout=5)
            raise

        self.assertEqual(
            process.returncode, 128 + signal.SIGTERM, output + errors
        )
        self.assertFalse((self.runner_state / "runner-started").exists())
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])
        self.assertFalse(Path(self.environment["TEST_TOKEN_RECORD"]).exists())
        self.assertEqual(list((self.root / "tmp").iterdir()), [])


if __name__ == "__main__":
    _ = unittest.main()
