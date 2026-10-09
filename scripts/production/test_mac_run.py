#!/usr/bin/env python3
"""Exercise mac_run.sh coordination with disposable state and command stand-ins."""

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


SCRIPT = Path(__file__).with_name("mac_run.sh")
MAC_TEST = SCRIPT.parent.parent / "mac_test" / "mac_test.py"
SHA = "1234567890abcdef1234567890abcdef12345678"


class CommandEvent(TypedDict):
    command: str
    args: list[str]
    claimed: bool


COMMAND_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys

state = Path(os.environ["MAC_RUN_STUB_STATE"])
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
event = {
    "command": command,
    "args": arguments,
    "claimed": (Path(os.environ["MAC_TEST_STATE_DIR"]) / "run.json").exists(),
}
with (state / "events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")

if command == "systemd-run":
    raise SystemExit(0)
if command == "scp":
    raise SystemExit(0)
if command == "git":
    if "rev-parse" in arguments and "--verify" in arguments:
        status = int(os.environ.get("MAC_RUN_GIT_RESOLVE_STATUS", "0"))
        if status == 0:
            print(os.environ["MAC_RUN_SHA"])
        raise SystemExit(status)
    if "bundle" in arguments and "create" in arguments:
        bundle = Path(arguments[arguments.index("-q") + 1])
        bundle.write_text("bundle\n", encoding="utf-8")
    raise SystemExit(0)
if command != "ssh":
    raise SystemExit(99)

if arguments[-1:] == ["true"] and "-o" in arguments:
    raise SystemExit(0 if os.environ.get("MAC_RUN_REACHABLE", "1") == "1" else 255)

remote = arguments[-1]
if "rm -f /tmp/mac_run.bundle" in remote and "pkill -f" in remote:
    (state / "cleanup-called").write_text("called\n", encoding="utf-8")
    raise SystemExit(int(os.environ.get("MAC_RUN_CLEANUP_STATUS", "0")))
if (
    os.environ.get("MAC_RUN_HOLD_BUILD") == "1"
    and "cargo build -q -p hana" in remote
):
    (state / "build-started").write_text("started\n", encoding="utf-8")

    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.pause()

if "; echo mac_exit=$?" in remote:
    status = 142 if "perl -e" in remote else 0
    print(f"mac_exit={status}")
    raise SystemExit(0)
if "git -C rust/hana_catalyst_mac rev-parse HEAD" in remote:
    print(os.environ["MAC_RUN_SHA"])
    raise SystemExit(0)
if "cat /tmp/mac_run_hana.status" in remote:
    print("142")
    raise SystemExit(0)
raise SystemExit(0)
'''


SITE_CUSTOMIZE = r'''
from __future__ import annotations

import os
import time

if os.environ.get("MAC_TEST_ADVANCE_WAIT") == "1":
    original_monotonic = time.monotonic
    offset = 0.0

    def advancing_monotonic() -> float:
        return original_monotonic() + offset

    def advancing_sleep(seconds: float) -> None:
        global offset
        offset += seconds

    time.monotonic = advancing_monotonic
    time.sleep = advancing_sleep
'''


@final
class MacRunTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.home = Path()
        self.state_directory = Path()
        self.stub_state = Path()
        self.bin_directory = Path()
        self.repository = Path()
        self.config_path = Path()
        self.temp_directory = Path()
        self.environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".claude").symlink_to(
            SCRIPT.parent.parent.parent, target_is_directory=True
        )
        self.state_directory = self.root / "mac-test-state"
        self.stub_state = self.root / "stub-state"
        self.stub_state.mkdir()
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.config_path = self.root / "mac_test.conf"
        self.temp_directory = self.root / "tmp"
        self.temp_directory.mkdir()
        _ = self.config_path.write_text("ci_repo=\n", encoding="utf-8")
        stub = self.bin_directory / "command-stub"
        self.write_executable(stub, COMMAND_STUB)
        for name in ("ssh", "scp", "git", "systemd-run", "gh", "rsync"):
            (self.bin_directory / name).symlink_to(stub)
        startup = self.root / "python-startup"
        startup.mkdir()
        _ = (startup / "sitecustomize.py").write_text(
            SITE_CUSTOMIZE, encoding="utf-8"
        )
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(startup),
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_CONFIG": str(self.config_path),
            "MAC_RUN_STUB_STATE": str(self.stub_state),
            "MAC_RUN_SHA": SHA,
            "TMPDIR": str(self.temp_directory),
        }

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def state_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(MAC_TEST), *arguments],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def mac_run(
        self, *, extra: dict[str, str] | None = None, timeout: float = 30
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["zsh", str(SCRIPT), str(self.repository), SHA, "0"],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def start_mac_run(
        self, *, extra: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["zsh", str(SCRIPT), str(self.repository), SHA, "0"],
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def events(self) -> list[CommandEvent]:
        path = self.stub_state / "events.jsonl"
        if not path.exists():
            return []
        return [
            cast(CommandEvent, json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def clear_events(self) -> None:
        (self.stub_state / "events.jsonl").unlink(missing_ok=True)

    def assert_no_build_started(self) -> None:
        self.assertFalse(
            any(
                event["command"] == "ssh"
                and any("cargo build" in argument for argument in event["args"])
                for event in self.events()
            )
        )
        self.assertFalse(
            any(event["command"] == "scp" for event in self.events())
        )

    def assert_claim_released(self) -> None:
        self.assertFalse((self.state_directory / "run.json").exists())

    def wait_for_path(self, path: Path) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def test_blocked_mac_exits_nine_without_starting_build(self) -> None:
        blocked = self.state_command(
            "block", "--holder", "demo", "--for", "video capture"
        )
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.clear_events()

        result = self.mac_run()

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn("blocked by demo: video capture", result.stdout)
        self.assert_no_build_started()
        self.assert_claim_released()

    def test_busy_mac_exits_nine_without_starting_build(self) -> None:
        claimed = self.state_command(
            "claim",
            "--pid",
            str(os.getpid()),
            "--what",
            "unit tests",
            "--worktree",
            str(self.repository),
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        self.clear_events()

        result = self.mac_run(extra={"MAC_TEST_ADVANCE_WAIT": "1"})

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"busy: unit tests since \d\d:\d\d")
        self.assert_no_build_started()
        self.assertTrue((self.state_directory / "run.json").exists())
        released = self.state_command("release", "--pid", str(os.getpid()))
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

    def test_unreadable_state_exits_nine_without_starting_build(self) -> None:
        self.state_directory.mkdir()
        block = self.state_directory / "block.json"
        _ = block.write_text("damaged\n", encoding="utf-8")

        result = self.mac_run()

        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn(f"state unreadable: {block}", result.stdout)
        self.assert_no_build_started()
        self.assert_claim_released()

    def test_unreachable_mac_keeps_exit_three(self) -> None:
        result = self.mac_run(extra={"MAC_RUN_REACHABLE": "0"})

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("mac unreachable", result.stdout)
        self.assertEqual(
            [event["command"] for event in self.events()], ["ssh"]
        )
        self.assert_claim_released()

    def test_success_releases_claim(self) -> None:
        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_claim_released()
        remote_events = [
            event for event in self.events() if event["command"] in {"ssh", "scp"}
        ]
        self.assertGreater(len(remote_events), 1)
        self.assertFalse(remote_events[0]["claimed"])
        self.assertTrue(all(event["claimed"] for event in remote_events[1:]))

    def test_bundle_uses_fixture_temp_directory(self) -> None:
        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        copy = next(event for event in self.events() if event["command"] == "scp")
        self.assertTrue(
            Path(copy["args"][1]).is_relative_to(self.temp_directory), copy
        )

    def test_early_failure_releases_claim(self) -> None:
        result = self.mac_run(extra={"MAC_RUN_GIT_RESOLVE_STATUS": "2"})

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assert_claim_released()
        self.assert_no_build_started()

    def test_signals_clean_remote_work_before_releasing_claim(self) -> None:
        for signal_number, cleanup_status in (
            (signal.SIGTERM, 0),
            (signal.SIGHUP, 23),
        ):
            with self.subTest(
                signal_number=signal_number, cleanup_status=cleanup_status
            ):
                self.clear_events()
                (self.stub_state / "build-started").unlink(missing_ok=True)
                (self.stub_state / "cleanup-called").unlink(missing_ok=True)
                process = self.start_mac_run(
                    extra={
                        "MAC_RUN_HOLD_BUILD": "1",
                        "MAC_RUN_CLEANUP_STATUS": str(cleanup_status),
                    }
                )
                try:
                    self.wait_for_path(self.stub_state / "build-started")
                    os.killpg(process.pid, signal_number)
                    output, errors = process.communicate(timeout=15)
                except BaseException:
                    os.killpg(process.pid, signal.SIGKILL)
                    _ = process.communicate(timeout=5)
                    raise

                self.assertEqual(
                    process.returncode, 128 + signal_number, output + errors
                )
                self.assertTrue((self.stub_state / "cleanup-called").exists())
                cleanup_events = [
                    event
                    for event in self.events()
                    if event["command"] == "ssh"
                    and any(
                        "rm -f /tmp/mac_run.bundle" in argument
                        and "pkill -f" in argument
                        for argument in event["args"]
                    )
                ]
                self.assertEqual(len(cleanup_events), 1, self.events())
                cleanup = cleanup_events[0]
                self.assertTrue(cleanup["claimed"], cleanup)
                self.assertEqual(
                    cleanup["args"][:5],
                    [
                        "-o",
                        "ConnectTimeout=6",
                        "-o",
                        "BatchMode=yes",
                        "natemccoy@mac",
                    ],
                )
                remote = cleanup["args"][-1]
                self.assertIn("[c]argo build", remote)
                self.assertIn("[h]ana_catalyst_mac/target/debug/", remote)
                self.assertIn("/tmp/mac_run_hana.command", remote)
                self.assertIn("/tmp/mac_run_hana.status", remote)
                self.assert_claim_released()


if __name__ == "__main__":
    _ = unittest.main()
