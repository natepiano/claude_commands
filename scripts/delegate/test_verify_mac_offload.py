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
from pathlib import Path
from typing import TypedDict, cast, final, override


VERIFY = Path(__file__).with_name("verify.sh")


class CargoEvent(TypedDict):
    args: list[str]
    token_held: bool


class RunnerEvent(TypedDict):
    args: list[str]
    token_held: bool


class CallRecord(TypedDict):
    kind: str
    command: str
    outcome: str
    status: int | None
    token_wait_s: int
    mac: str
    mac_reason: str
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

arguments = sys.argv[1:]
root = Path(os.environ["TEST_RUNNER_STATE"])
result = Path(arguments[arguments.index("--result") + 1])
event = {
    "args": arguments,
    "token_held": Path(os.environ["TEST_TOKEN_RECORD"]).exists(),
}
with (root / "runner-events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")
(root / "runner-started").write_text("started\n", encoding="utf-8")

if os.environ.get("TEST_RUNNER_HOLD") == "1":
    def stop(signum: int, _frame: object) -> None:
        (root / "runner-signal").write_text(str(signum), encoding="utf-8")
        raise SystemExit(128 + signum)

    for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signal_number, stop)
    while True:
        signal.pause()

mode = os.environ.get("TEST_RUNNER_MAC", "declined")
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
output = os.environ.get("TEST_RUNNER_OUTPUT", "")
if output:
    print(output, flush=True)
raise SystemExit(int(os.environ.get("TEST_RUNNER_STATUS", "75")))
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
        (self.home / ".claude").symlink_to(
            VERIFY.parent.parent.parent, target_is_directory=True
        )
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

    def test_local_flag_skips_runner_and_shares_recorded_pass_key(self) -> None:
        first = self.verify(
            "test",
            "sample",
            "--local",
            extra={"TEST_RUNNER_MAC": "failed", "TEST_RUNNER_STATUS": "42"},
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(len(self.local_runs()), 1)
        first_record = self.last_call()
        self.assertEqual(first_record["command"], "test sample")
        self.assertEqual(first_record["mac"], "declined")
        self.assertEqual(first_record["mac_reason"], "local_flag")

        self.clear_observations()
        without_flag = self.verify("test", "sample")
        self.assertEqual(
            without_flag.returncode, 0, without_flag.stdout + without_flag.stderr
        )
        self.assertIn("PASS (recorded)", without_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])

        self.clear_observations()
        with_flag = self.verify("test", "sample", "--local")
        self.assertEqual(with_flag.returncode, 0, with_flag.stdout + with_flag.stderr)
        self.assertIn("PASS (recorded)", with_flag.stdout)
        self.assertEqual(self.runner_events(), [])
        self.assertEqual(self.local_runs(), [])

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
