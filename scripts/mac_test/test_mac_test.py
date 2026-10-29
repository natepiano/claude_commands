#!/usr/bin/env python3
"""Exercise Mac test commands and their real process coordination boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
import fcntl
import socket
import os
import runpy
import select
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast, final, override
from unittest import mock
from zoneinfo import ZoneInfo

import mac_test

if TYPE_CHECKING:
    from ..production.cli_fixture import invoke_cli
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "production"))
    from cli_fixture import invoke_cli


SCRIPT = Path(__file__).with_name("mac_test.py")
LOCAL_ZONE = ZoneInfo("America/New_York")
CI_JOB = "macOS: Compile and Test"
SKIPPED_REPORT_RUNS = 200
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


class RunState(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str
    remote_host: NotRequired[str]
    holder_session: NotRequired[str]
    idle_since: NotRequired[str]


class WaitingState(TypedDict):
    kind: str
    what: NotRequired[str]


class FreeMessageState(TypedDict):
    kind: str
    what: NotRequired[str]
    ended: NotRequired[str]


class GhRun(TypedDict):
    databaseId: int
    headBranch: NotRequired[str]
    headSha: NotRequired[str]


class GhJob(TypedDict):
    name: str
    status: NotRequired[str]
    conclusion: NotRequired[str]


class GhFailure(TypedDict):
    match: str
    line: str
    status: int


class GhDelay(TypedDict):
    match: str
    seconds: float
    marker: str
    release: NotRequired[str]
    after: NotRequired[bool]


class GhFixture(TypedDict):
    variable: str
    queued: list[GhRun]
    in_progress: list[GhRun]
    created: list[GhRun]
    jobs: dict[str, list[GhJob]]
    failures: list[GhFailure]
    delays: list[GhDelay]


GH_STUB = r'''from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["MAC_TEST_STUB_DIR"])
fixture_path = root / "gh-fixture.json"
arguments = sys.argv[1:]
with (root / "gh.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\n")
fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
joined = " ".join(arguments)

def wait_on_delay(delay):
    (root / delay["marker"]).write_text("started\n", encoding="utf-8")
    release = delay.get("release")
    if release is None:
        time.sleep(float(delay["seconds"]))
    else:
        deadline = time.monotonic() + float(delay["seconds"])
        while not (root / release).exists() and time.monotonic() < deadline:
            time.sleep(0.01)

for delay in fixture["delays"]:
    if delay["match"] in joined and not delay.get("after", False):
        wait_on_delay(delay)
for failure in fixture["failures"]:
    if failure["match"] in joined:
        print(failure["line"], file=sys.stderr)
        raise SystemExit(int(failure["status"]))
if arguments[:2] == ["variable", "get"]:
    print(fixture["variable"])
elif arguments[:2] == ["variable", "set"]:
    body_index = arguments.index("--body") + 1
    fixture["variable"] = arguments[body_index]
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
elif arguments[:2] == ["run", "list"]:
    if "--status" in arguments:
        status_index = arguments.index("--status") + 1
        print(json.dumps(fixture[arguments[status_index]]))
    else:
        print(json.dumps(fixture["created"]))
elif arguments[:2] == ["run", "view"]:
    print(json.dumps({"jobs": fixture["jobs"].get(arguments[2], [])}))
else:
    print("unexpected gh call: " + joined, file=sys.stderr)
    raise SystemExit(9)
for delay in fixture["delays"]:
    if delay["match"] in joined and delay.get("after", False):
        wait_on_delay(delay)
'''


SYSTEMD_STUB = r'''from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(os.environ["MAC_TEST_STUB_DIR"])
arguments = sys.argv[1:]
with (root / "systemd-run.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\n")
status_file = root / "systemd-status"
status = int(status_file.read_text()) if status_file.exists() else 0
if status == 0 and (root / "systemd-start").exists():
    command = arguments[arguments.index("--") + 1:]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (root / "systemd-pid").write_text(str(process.pid), encoding="utf-8")
raise SystemExit(status)
'''


FIXTURE_CHILD_LIFETIME = r'''import os
import subprocess

writer_fd = int(os.environ["MAC_TEST_WRITER_FD"])
original_popen = subprocess.Popen

class FixtureChild(original_popen):
    def __init__(self, *args, **kwargs):
        kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), writer_fd)
        super().__init__(*args, **kwargs)

subprocess.Popen = FixtureChild

# Publish the contender's real flock request before it blocks behind GH.
import fcntl
from pathlib import Path
original_flock = fcntl.flock

def fixture_flock(fd, operation):
    marker = os.environ.get('MAC_TEST_LOCK_ATTEMPT')
    target = Path(os.environ['MAC_TEST_STATE_DIR']) / 'control.lock'
    if marker and operation == fcntl.LOCK_EX and target.exists():
        opened, expected = os.fstat(fd), target.stat()
        if (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino):
            Path(marker).write_text('attempted\n')
    return original_flock(fd, operation)

fcntl.flock = fixture_flock
'''


SEND_STUB = r'''from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["MAC_TEST_STUB_DIR"])
arguments = sys.argv[1:]
with (root / "send.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\n")
(root / "send-started").write_text("started\n", encoding="utf-8")
hold_file = root / "send-hold"
if hold_file.exists():
    deadline = time.monotonic() + TEST_HANG_GUARD_SECS0
    while not (root / "send-continue").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
delay_file = root / "send-delay"
if delay_file.exists():
    time.sleep(float(delay_file.read_text()))
status_file = root / "send-status"
status = int(status_file.read_text()) if status_file.exists() else 0
if status not in (0, 1):
    raise SystemExit(status)
target = arguments[arguments.index("--to") + 1]
key = arguments[arguments.index("--key") + 1]
delivered_keys_path = root / "send-keys.json"
delivered_keys = (
    json.loads(delivered_keys_path.read_text(encoding="utf-8"))
    if delivered_keys_path.exists()
    else []
)
delivery_key = [target, key]
if delivery_key in delivered_keys:
    raise SystemExit(1)
if status == 1:
    raise SystemExit(1)
delivered_keys.append(delivery_key)
delivered_keys_path.write_text(json.dumps(delivered_keys), encoding="utf-8")
with (root / "send-delivered.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\n")
'''


SESSIONS_STUB = r'''from __future__ import annotations

import json
import os
from pathlib import Path
import sys

root = Path(os.environ["MAC_TEST_STUB_DIR"])
with (root / "sessions.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\n")
if (root / "session-missing").exists():
    raise SystemExit(1)
print(root / "session.sock")
'''


class HoldEventRecord(TypedDict):
    target: str
    event: str
    hold_key: str
    holder: str
    reason: str
    at: str
    until: str


@final
class MacTestCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.state_directory = Path()
        self.stub_directory = Path()
        self.bin_directory = Path()
        self.config_path = Path()
        self.environment: dict[str, str] = {}
        self.children: list[subprocess.Popen[str]] = []
        self.writer_read_fd = -1
        self.writer_write_fd = -1
        self.gh: GhFixture = {
            "variable": "true",
            "queued": [],
            "in_progress": [],
            "created": [],
            "jobs": {},
            "failures": [],
            "delays": [],
        }

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_directory = self.root / "state"
        self.stub_directory = self.root / "stubs"
        self.bin_directory = self.root / "bin"
        self.config_path = self.root / "mac_test.conf"
        self.stub_directory.mkdir()
        self.bin_directory.mkdir()
        self.write_executable(self.bin_directory / "gh", GH_STUB)
        self.write_executable(self.bin_directory / "systemd-run", SYSTEMD_STUB)
        send_stub = self.stub_directory / "send.py"
        sessions_stub = self.stub_directory / "sessions.py"
        self.write_executable(send_stub, SEND_STUB)
        self.write_executable(sessions_stub, SESSIONS_STUB)
        self.write_config()
        self.sync_gh()
        self.environment = {
            **{
                name: value
                for name, value in os.environ.items()
                if name != "CLAUDE_CODE_SESSION_ID"
            },
            "HOME": str(self.root),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_CONFIG": str(self.config_path),
            "MAC_TEST_SEND": str(send_stub),
            "MAC_TEST_SESSIONS": str(sessions_stub),
            "MAC_TEST_STUB_DIR": str(self.stub_directory),
            "MAC_TEST_HANG_GUARD_SECS": str(TEST_HANG_GUARD_SECS),
            "MAC_TEST_WATCH_INTERVAL_S": "0.05",
            "TZ": "America/New_York",
            "USER_ZONE": "America/New_York",
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": "",
            "PLAN_DELEGATE_BOARD_DIR": "", "PLAN_DELEGATE_SESSION_DIR": "",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
        }
        self.children = []
        child_lifetime = self.root / "child-lifetime"
        child_lifetime.mkdir()
        _ = (child_lifetime / "sitecustomize.py").write_text(FIXTURE_CHILD_LIFETIME)
        self.writer_read_fd, self.writer_write_fd = os.pipe()
        self.environment["MAC_TEST_WRITER_FD"] = str(self.writer_write_fd)
        self.environment["PYTHONPATH"] = str(child_lifetime) + os.pathsep + os.environ.get("PYTHONPATH", "")
        self.addCleanup(self.wait_for_fixture_writers)

    @override
    def tearDown(self) -> None:
        for child in reversed(self.children):
            self.stop_child(child)

    def wait_for_fixture_writers(self) -> None:
        if self.writer_write_fd < 0:
            return
        os.close(self.writer_write_fd)
        self.writer_write_fd = -1
        try:
            readable, _, _ = select.select([self.writer_read_fd], [], [], TEST_HANG_GUARD_SECS)
            self.assertTrue(readable, "Fixture descendants did not finish before directory cleanup")
            self.assertEqual(os.read(self.writer_read_fd, 1), b"")
        finally:
            os.close(self.writer_read_fd)
            self.writer_read_fd = -1

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def write_config(self, **changes: str | None) -> None:
        values: dict[str, str] = {
            "ci_repo": "natepiano/hana",
            "ci_variable": "MACOS_CI",
            "ci_workflow": "ci.yml",
            "ci_job": CI_JOB,
            # Long enough for the Python gh stand-in under load; a test about
            # the limit sets its own.
            "gh_timeout_s": str(TEST_HANG_GUARD_SECS),
            "block_hours": "4",
            "block_max_hours": "48",
            "block_warn_minutes": "15",
        }
        for name, value in changes.items():
            if value is None:
                _ = values.pop(name, None)
            else:
                values[name] = value
        _ = self.config_path.write_text(
            "".join(f"{name}={value}\n" for name, value in values.items()),
            encoding="utf-8",
        )

    def sync_gh(self) -> None:
        _ = (self.stub_directory / "gh-fixture.json").write_text(
            json.dumps(self.gh), encoding="utf-8"
        )

    def command(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
        timeout: float = TEST_HANG_GUARD_SECS,
        subprocess_cli: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        selected_environment = self.environment if environment is None else environment
        if subprocess_cli:
            return subprocess.run(
                [sys.executable, str(SCRIPT), *arguments], cwd=self.root,
                env=selected_environment, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, check=False, timeout=timeout, pass_fds=(self.writer_write_fd,))
        real_run = subprocess.run
        real_record = mac_test.start_hold_recorder
        real_popen = subprocess.Popen

        def launch(command: list[str], *, stdin: int, stdout: int, stderr: int,
                   start_new_session: bool, close_fds: bool) -> subprocess.Popen[bytes]:
            return real_popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                              start_new_session=start_new_session, close_fds=close_fds,
                              pass_fds=(self.writer_write_fd,))

        def record(event: mac_test.MacBlockEvent) -> None:
            # Keep the production detached recorder and reap thread. The pipe
            # still proves every fixture writer has exited before cleanup.
            with mock.patch.object(subprocess, "Popen", side_effect=launch):
                real_record(event)

        def run(command: list[str], *, capture_output: bool = False, text: bool = False,
                check: bool, timeout: float | None = None,
                stdin: int | None = None, stdout: int | None = None,
                stderr: int | None = None) -> subprocess.CompletedProcess[str]:
            script = self.bin_directory / command[0]
            stub_arguments = command[1:]
            for name in ("sessions.py", "send.py"):
                if command[:2] == [sys.executable, str(self.stub_directory / name)]:
                    # Tests of blocked/timed-out delivery keep the actual child.
                    if name == "send.py" and any((self.stub_directory / flag).exists()
                                                for flag in ("send-hold", "send-delay")):
                        break
                    script = self.stub_directory / name
                    stub_arguments = command[2:]
            if script in (self.bin_directory / "gh", self.bin_directory / "systemd-run",
                          self.stub_directory / "sessions.py", self.stub_directory / "send.py") and script.exists():
                module = ModuleType("mac_test_fixture_tool")
                module.__file__ = str(script)

                def main() -> int:
                    _ = runpy.run_path(str(script), run_name="__main__")
                    return 0

                return invoke_cli(module, main, stub_arguments, dict(os.environ), self.root)
            return real_run(command, capture_output=capture_output, text=text, check=check,
                            timeout=timeout, stdin=stdin, stdout=stdout, stderr=stderr)

        with (mock.patch.object(subprocess, "run", side_effect=run),
              mock.patch.object(mac_test, "start_hold_recorder", side_effect=record)):
            return invoke_cli(mac_test, mac_test.main, list(arguments), selected_environment, self.root)

    def start_command(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            env=self.environment if environment is None else environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(self.writer_write_fd,),
        )
        self.children.append(process)
        return process

    def start_child(self, name: bytes = b"mac ) test") -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import ctypes, time; "
                    + f"ctypes.CDLL(None).prctl(15, {name!r}); "
                    + f"time.sleep({TEST_HANG_GUARD_SECS})"
                ),
            ],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.children.append(child)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        process_name = b""
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child.pid}/stat").read_bytes()
            process_name = stat[stat.find(b"(") + 1 : stat.rfind(b")")]
            if process_name == name:
                break
            time.sleep(0.01)
        self.assertEqual(process_name, name)
        return child

    def stop_child(self, child: subprocess.Popen[str]) -> None:
        if child.poll() is None:
            child.terminate()
            try:
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
            except subprocess.TimeoutExpired:
                child.kill()
                _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    def assert_command(
        self,
        result: subprocess.CompletedProcess[str],
        returncode: int,
        stdout: str,
        stderr: str = "",
    ) -> None:
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertEqual(result.stdout, stdout)
        self.assertEqual(result.stderr, stderr)

    def run_state(self) -> RunState:
        return cast(
            RunState,
            json.loads((self.state_directory / "run.json").read_text(encoding="utf-8")),
        )

    def block_state(self) -> dict[str, object]:
        return cast(
            dict[str, object],
            json.loads((self.state_directory / "block.json").read_text(encoding="utf-8")),
        )

    def write_block(
        self,
        *,
        holder: str = "alice",
        reason: str = "release work",
        since: datetime | None = None,
        expires: datetime | None = None,
        session: str | None = None,
        showrunner: str | None = None,
        state: str = "active",
        waiting_on: WaitingState | None = None,
        free_message: FreeMessageState | None = None,
        ci: str = "off_by_this_block",
    ) -> dict[str, object]:
        since = since or datetime.now(timezone.utc)
        expires = expires or since + timedelta(hours=4)
        record: dict[str, object] = {
            "version": 2,
            "holder": holder,
            "for": reason,
            "since": since.isoformat(timespec="seconds"),
            "expires": expires.isoformat(timespec="seconds"),
            "session": session,
            "showrunner": showrunner,
            "state": state,
            "ci": ci,
        }
        if waiting_on is not None:
            record["waiting_on"] = waiting_on
        if free_message is not None:
            record["free_message"] = free_message
        self.state_directory.mkdir(parents=True, exist_ok=True)
        _ = (self.state_directory / "block.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        return record

    def logged_arguments(self, name: str) -> list[list[str]]:
        path = self.stub_directory / f"{name}.jsonl"
        if not path.exists():
            return []
        return [
            cast(list[str], json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def clear_log(self, name: str) -> None:
        (self.stub_directory / f"{name}.jsonl").unlink(missing_ok=True)

    def wait_for_path(self, path: Path, timeout: float = TEST_HANG_GUARD_SECS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def wait_for_log_count(self, name: str, count: int, timeout: float = TEST_HANG_GUARD_SECS) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.logged_arguments(name)) >= count:
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {count} {name} calls")

    def wait_for_free_message(
        self, expected: FreeMessageState, timeout: float = TEST_HANG_GUARD_SECS
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        block = self.block_state()
        while block.get("free_message") != expected:
            if time.monotonic() >= deadline:
                self.fail(f"timed out waiting for the free message {expected}: {block}")
            time.sleep(0.01)
            block = self.block_state()
        return block

    def local_time(self, instant: str, include_day: bool = False) -> str:
        format_string = "%a %H:%M %Z" if include_day else "%H:%M %Z"
        return datetime.fromisoformat(instant).astimezone(LOCAL_ZONE).strftime(format_string)

    def expiry_line(self, block: dict[str, object]) -> str:
        expires = cast(str, block["expires"])
        return (
            f"It lifts by itself at {self.local_time(expires, include_day=True)} "
            + "unless you run block again.\n"
        )

    def assert_utc_time(self, value: str) -> None:
        parsed = datetime.fromisoformat(value)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def claim(self, child: subprocess.Popen[str], what: str = "alpha tests") -> None:
        self.assert_command(
            self.command("claim", "--pid", str(child.pid), "--what", what),
            0,
            "claimed\n",
        )

    def active_block(self, holder: str = "alice", reason: str = "release work") -> dict[str, object]:
        result = self.command("block", "--holder", holder, "--for", reason)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.block_state()

    def gh_calls(self, fragment: str) -> int:
        return sum(fragment in " ".join(call) for call in self.logged_arguments("gh"))

    def read_fixture_variable(self) -> str:
        values = cast(
            dict[str, object],
            json.loads(
                (self.stub_directory / "gh-fixture.json").read_text(encoding="utf-8")
            ),
        )
        return cast(str, values["variable"])

    def test_status_without_block_reports_all_switch_results(self) -> None:
        on = self.command("status", subprocess_cli=True)
        self.assert_command(on, 0, "free\nno block\nCI's Mac switch: on\n")

        self.gh["variable"] = "false"
        self.sync_gh()
        off = self.command("status")
        self.assert_command(
            off, 0, "free\nno block\nCI's Mac switch: off (set elsewhere)\n"
        )

        self.gh["failures"] = [
            {"match": "variable get", "line": "network unavailable", "status": 1}
        ]
        self.sync_gh()
        unknown = self.command("status")
        self.assert_command(
            unknown,
            0,
            "free\nno block\nCI's Mac switch: unknown (network unavailable)\n",
        )

    def test_missing_repository_configuration_never_calls_gh(self) -> None:
        for missing_file in (False, True):
            with self.subTest(missing_file=missing_file):
                if missing_file:
                    self.config_path.unlink(missing_ok=True)
                else:
                    self.write_config(ci_repo=None)
                self.clear_log("gh")

                blocked = self.command(
                    "block", "--holder", "alice", "--for", "rendering"
                )
                self.assertEqual(
                    blocked.returncode, 0, blocked.stdout + blocked.stderr
                )
                self.assertEqual(self.block_state()["ci"], "not_configured")
                status = self.command("status")
                self.assertIn("CI's Mac switch: not configured\n", status.stdout)
                unblocked = self.command("unblock", "--holder", "alice")
                self.assert_command(unblocked, 0, "Mac unblocked.\n")
                self.assertEqual(self.logged_arguments("gh"), [])

    def test_claim_release_and_process_name_behavior_remain_local(self) -> None:
        first = self.start_child(b"mac \xff test")
        second = self.start_child()
        worktree = self.root / "alpha"
        claimed = self.command(
            "claim",
            "--pid",
            str(first.pid),
            "--what",
            "alpha tests",
            "--worktree",
            str(worktree),
        )
        self.assert_command(claimed, 0, "claimed\n")
        run = self.run_state()
        self.assertEqual(run["pid"], first.pid)
        self.assertEqual(run["what"], "alpha tests")
        self.assertEqual(run["worktree"], str(worktree))
        self.assert_utc_time(run["since"])

        busy = self.command("claim", "--pid", str(second.pid), "--what", "beta tests")
        expected_time = datetime.fromisoformat(run["since"]).astimezone(LOCAL_ZONE).strftime("%H:%M")
        self.assert_command(busy, 11, f"busy: alpha tests since {expected_time}\n")

        self.clear_log("gh")
        self.assert_command(self.command("release", "--pid", str(first.pid)), 0, "")
        self.assertEqual(self.logged_arguments("gh"), [])
        self.assertEqual(self.logged_arguments("send"), [])
        self.assertFalse((self.state_directory / "run.json").exists())

    def kept_claim(self, host: str = "previous-mac") -> RunState:
        owner = self.start_child()
        claimed = self.command("claim", "--pid", str(owner.pid), "--what", "older tests",
                               "--worktree", str(self.root / "older"), "--remote", host)
        self.assert_command(claimed, 0, "claimed\n")
        record = self.run_state()
        self.assertEqual(record.get("remote_host"), host)
        self.stop_child(owner)
        return record

    def test_claims_record_each_mac_verb_and_workspace_scope(self) -> None:
        worktree = self.repository("hana")
        owner = self.start_child()
        for verb in ("nextest", "check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                claim = self.command("claim", "--pid", str(owner.pid), "--what", f"{verb} workspace",
                                     "--worktree", str(worktree))
                self.assert_command(claim, 0, "claimed\n")
                self.assertEqual(self.repository_claim("hana")["what"], f"{verb} workspace")
                self.assertIn(f"running: {verb} workspace", self.command("status").stdout)
                self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")

    def test_remote_claim_outlives_its_owner_and_remains_live_to_state_readers(self) -> None:
        record = self.kept_claim()
        paths = mac_test.StatePaths(self.state_directory)
        with mock.patch("subprocess.run", side_effect=AssertionError("State reads stay local")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("State reads stay local")):
            decoded = mac_test.read_run(paths.run)
            self.assertNotIsInstance(decoded, mac_test.NoRun)
            if not isinstance(decoded, mac_test.NoRun):
                self.assertTrue(mac_test.run_is_live(decoded))
                self.assertEqual(decoded["what"], record["what"])
                self.assertEqual(decoded["since"], record["since"])
            self.assertEqual([claim.record for claim in mac_test.live_claims(paths)], [decoded])
        self.assertEqual(self.run_state(), record)

    def test_claim_records_holder_session_and_starts_checkout_watcher(self) -> None:
        owner = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "holder-session"}
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "alpha tests",
                                         environment=environment), 0, "claimed\n")
        self.assertEqual(self.run_state().get("holder_session"), "holder-session")
        calls = self.logged_arguments("systemd-run")
        self.assertEqual(len(calls), 1)
        arguments = calls[0]
        self.assertEqual(arguments[:5], ["--user", "--collect", "--quiet", "--no-block", "--unit"])
        self.assertTrue(arguments[5].startswith("mac-test-claim-watch-"))
        self.assertEqual(arguments[-3:], [sys.executable, str(SCRIPT.resolve()), "watch-claim"])
        self.assertIn(f"MAC_TEST_STATE_DIR={self.state_directory}", arguments)
        self.assertIn(f"MAC_TEST_CONFIG={self.config_path}", arguments)

    def test_claim_records_worker_owning_session_and_omits_unknown_holder(self) -> None:
        owner = self.start_child()
        active = self.root / "active"
        active.mkdir()
        board = str(self.root / "run")
        _ = (active / "worker-owner").write_text(board)
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "",
                       "UNIT_RUN_DIR": board, "UNIT_SESSION_DIR": ""}
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "worker tests",
                                         environment=environment), 0, "claimed\n")
        self.assertEqual(self.run_state().get("holder_session"), "worker-owner")
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "",
                       "UNIT_RUN_DIR": "", "UNIT_SESSION_DIR": ""}
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "unowned tests",
                                         environment=environment), 0, "claimed\n")
        self.assertNotIn("holder_session", self.run_state())

    def unit_environment(self, session: str) -> dict[str, str]:
        active = self.root / "active"
        active.mkdir(exist_ok=True)
        run = str(self.root / "unit-run")
        _ = (active / session).write_text(run)
        return {**self.environment, "UNIT_RUN_DIR": run,
                "CLAUDE_CODE_SESSION_ID": "worker-session"}

    def test_active_and_pending_blocks_admit_their_session_claims(self) -> None:
        owner = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-a"}
        for state in ("active", "pending"):
            with self.subTest(state=state):
                _ = self.write_block(session="session-a", state=state,
                                     waiting_on={"kind": "ci_job"} if state == "pending" else None,
                                     free_message={"kind": "not_needed"} if state == "active" else None)
                self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "own tests",
                                                 environment=environment), 0, "claimed\n")
                self.assertEqual(self.run_state().get("holder_session"), "session-a")
                self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")

    def test_unit_worker_claim_uses_director_identity_for_block_and_record(self) -> None:
        owner = self.start_child()
        environment = self.unit_environment("session-a")
        _ = self.write_block(session="session-a", free_message={"kind": "not_needed"})
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "unit tests",
                                         environment=environment), 0, "claimed\n")
        self.assertEqual(self.run_state().get("holder_session"), "session-a")

    def test_block_refuses_other_and_unknown_sessions_without_writing_claims(self) -> None:
        owner = self.start_child()
        _ = self.write_block(session="session-a", free_message={"kind": "not_needed"})
        for session in ("session-b", ""):
            with self.subTest(session=session):
                environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": session}
                self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "other tests",
                                                 environment=environment), 10, "blocked by alice: release work\n")
                self.assertFalse((self.state_directory / "run.json").exists())

    def test_holder_queue_block_refuses_claims_from_every_session(self) -> None:
        owner = self.start_child()
        _ = self.write_block(free_message={"kind": "not_needed"})
        for session in ("alice", "session-b", ""):
            with self.subTest(session=session):
                environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": session}
                self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "tests",
                                                 environment=environment), 10, "blocked by alice: release work\n")
                self.assertFalse((self.state_directory / "run.json").exists())

    def test_free_now_reads_past_own_block_to_free_or_busy(self) -> None:
        self.write_config(offload="on")
        _ = self.write_block(session="session-a", free_message={"kind": "not_needed"})
        environment = self.unit_environment("session-a")
        self.assert_command(self.command("free-now", environment=environment), 0, "free\n")
        other = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-b"}
        self.assert_command(self.command("free-now", environment=other), 1, "blocked\n")
        owner = self.start_child()
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "own tests",
                                         environment=environment), 0, "claimed\n")
        self.assert_command(self.command("free-now", environment=environment), 1, "busy\n")
        self.assertEqual(self.logged_arguments("gh"), [])

    def test_unit_block_records_director_session_and_keeps_ci_off(self) -> None:
        environment = self.unit_environment("session-a")
        result = self.command("block", "--holder", "alice", "--for", "unit tests",
                              environment=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.block_state()["session"], "session-a")
        self.assertEqual(self.read_fixture_variable(), "false")
        owner = self.start_child()
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "unit tests",
                                         environment=environment), 0, "claimed\n")
        self.assertEqual(self.run_state().get("holder_session"), "session-a")

    def test_takeover_keeps_holder_session_and_starts_claim_watcher(self) -> None:
        previous = self.kept_claim()
        previous["holder_session"] = "original-holder"
        _ = (self.state_directory / "run.json").write_text(json.dumps(previous))
        self.clear_log("systemd-run")
        taker = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "new-holder"}
        result = self.command("claim", "--pid", str(taker.pid), "--what", "new tests", "--remote", "new-mac",
                              environment=environment)
        self.assert_command(result, 13, "leftovers: previous-mac\n")
        self.assertEqual(self.run_state().get("holder_session"), "original-holder")
        self.assertEqual([arguments[-1] for arguments in self.logged_arguments("systemd-run")], ["watch-claim"])

    def test_refused_claims_start_no_claim_watcher(self) -> None:
        self.assertEqual(self.command("claim", "--pid", "-1", "--what", "invalid owner").returncode, 1)
        self.assertEqual(self.logged_arguments("systemd-run"), [])
        owner = self.start_child()
        self.claim(owner)
        self.clear_log("systemd-run")
        self.assertEqual(self.command("claim", "--pid", str(os.getpid()), "--what", "busy tests").returncode, 11)
        self.assertEqual(self.logged_arguments("systemd-run"), [])
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        _ = self.write_block(free_message={"kind": "not_needed"})
        self.assertEqual(self.command("claim", "--pid", str(owner.pid), "--what", "blocked tests").returncode, 10)
        self.assertEqual(self.logged_arguments("systemd-run"), [])
        _ = (self.state_directory / "run.json").write_text("{}")
        self.assertEqual(self.command("claim", "--pid", str(owner.pid), "--what", "unreadable tests").returncode, 12)
        self.assertEqual(self.logged_arguments("systemd-run"), [])

    def test_run_reader_accepts_old_record_and_decodes_idle_fields(self) -> None:
        self.state_directory.mkdir()
        record: RunState = {"pid": os.getpid(), "proc_start": "owner-start", "what": "tests",
                            "worktree": str(self.root), "since": "2026-10-13T12:00:00+00:00"}
        path = self.state_directory / "run.json"
        _ = path.write_text(json.dumps(record))
        self.assertEqual(mac_test.read_run(path), record)
        record.update(holder_session="holder", idle_since="2026-10-13T12:05:00+00:00")
        _ = path.write_text(json.dumps(record))
        self.assertEqual(mac_test.read_run(path), record)

    def test_status_adds_idle_since_for_process_and_remote_claims(self) -> None:
        owner = self.start_child()
        self.claim(owner)
        self.write_config(ci_repo="")
        record = self.run_state()
        record["idle_since"] = "2026-10-13T12:05:00+00:00"
        path = self.state_directory / "run.json"
        for remote in (False, True):
            with self.subTest(remote=remote):
                if remote:
                    record["remote_host"] = "previous-mac"
                    self.stop_child(owner)
                _ = path.write_text(json.dumps(record))
                result = self.command("status")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(", idle since 08:05", result.stdout.splitlines()[0])

    def test_process_claim_lapses_after_its_owner_ends(self) -> None:
        owner = self.start_child()
        self.claim(owner)
        self.assertNotIn("remote_host", self.run_state())
        self.stop_child(owner)
        self.write_config(ci_repo="")
        self.assert_command(self.command("status"), 0, "free\nno block\nCI's Mac switch: not configured\n")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_remote_takeover_preserves_job_fields_and_refuses_a_second_live_taker(self) -> None:
        previous = self.kept_claim()
        taker, other = self.start_child(), self.start_child()
        taken = self.command("claim", "--pid", str(taker.pid), "--what", "new tests",
                             "--worktree", str(self.root / "new"), "--remote", "new-mac")
        self.assertEqual(taken.returncode, 13, taken.stdout + taken.stderr)
        self.assertEqual(taken.stdout + taken.stderr, "leftovers: previous-mac\n")
        after = self.run_state()
        self.assertEqual(after["pid"], taker.pid)
        self.assertEqual(after["proc_start"], mac_test.process_start_time(taker.pid))
        self.assertEqual({key: value for key, value in after.items() if key not in ("pid", "proc_start")},
                         {key: value for key, value in previous.items() if key not in ("pid", "proc_start")})
        refused = self.command("claim", "--pid", str(other.pid), "--what", "third tests", "--remote", "third-mac")
        self.assertEqual(refused.returncode, 11, refused.stdout + refused.stderr)
        self.assertTrue(refused.stdout.startswith("busy: older tests since "), refused.stdout)
        self.assertEqual(self.run_state(), after)

    def test_process_claim_waits_for_kept_remote_claim_and_reports_leftovers(self) -> None:
        record = self.kept_claim()
        since = datetime.fromisoformat(record["since"]).astimezone(LOCAL_ZONE).strftime("%H:%M")
        self.assert_command(self.command("claim", "--pid", str(os.getpid()), "--what", "local tests", "--wait", "0.05"),
                            11, f"busy: leftovers of older tests on previous-mac since {since}\n")
        self.assertEqual(self.run_state(), record)

    def test_status_names_kept_claim_and_matching_release_ends_it(self) -> None:
        record = self.kept_claim()
        self.write_config(ci_repo="")
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn(f"running: leftovers of older tests on previous-mac since {self.local_time(record['since'])}, "
                      + f"kept under pid {record['pid']}\n", status.stdout)
        self.assert_command(self.command("release", "--pid", str(os.getpid())), 0, "")
        self.assertEqual(self.run_state(), record)
        self.assert_command(self.command("release", "--pid", str(record["pid"])), 0, "")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_pending_block_waits_for_kept_claim_and_takeover_precedes_block_refusal(self) -> None:
        record = self.kept_claim()
        block = self.command("block", "--holder", "alice", "--for", "render work")
        self.assertEqual(block.returncode, 0, block.stdout + block.stderr)
        self.assertIn("Block pending for alice: older tests is running on the Mac.", block.stdout)
        self.assertEqual(self.block_state()["state"], "pending")
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(self.block_state()["state"], "pending")
        self.assertEqual(self.run_state(), record)
        taker = self.start_child()
        taken = self.command("claim", "--pid", str(taker.pid), "--what", "new tests", "--remote", "new-mac")
        self.assertEqual(taken.returncode, 13, taken.stdout + taken.stderr)
        self.assertEqual(taken.stdout + taken.stderr, "leftovers: previous-mac\n")
        self.assertEqual(self.block_state()["state"], "pending")
        self.assertEqual(self.run_state()["pid"], taker.pid)

    def test_run_record_without_remote_host_still_decodes_with_mapping_access(self) -> None:
        started = mac_test.process_start_time(os.getpid())
        self.assertIsNotNone(started)
        assert started is not None
        record: RunState = {"pid": os.getpid(), "proc_start": started, "what": "legacy tests",
                            "worktree": str(self.root), "since": datetime.now(timezone.utc).isoformat()}
        self.state_directory.mkdir()
        path = self.state_directory / "run.json"
        _ = path.write_text(json.dumps(record))
        before = path.read_bytes()
        decoded = mac_test.read_run(path)
        self.assertNotIsInstance(decoded, mac_test.NoRun)
        if not isinstance(decoded, mac_test.NoRun):
            self.assertTrue(mac_test.run_is_live(decoded))
            for key in ("pid", "proc_start", "what", "worktree", "since"):
                self.assertEqual(decoded[key], record[key])
        self.assertEqual(path.read_bytes(), before)

    def test_free_now_answers_from_local_state_alone(self) -> None:
        self.assert_command(self.command("free-now"), 1, "off\n")
        self.write_config(offload="on")
        self.assert_command(self.command("free-now"), 0, "free\n")

        owner = self.start_child()
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "alpha tests"), 0, "claimed\n")
        self.assert_command(self.command("free-now"), 1, "busy\n")
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        record = self.kept_claim()
        self.assert_command(self.command("free-now"), 1, "busy\n")
        self.assert_command(self.command("release", "--pid", str(record["pid"])), 0, "")
        self.assert_command(self.command("free-now"), 0, "free\n")

        backoff = self.state_directory / "unreachable_until"
        _ = backoff.write_text(f"{time.time() + 300:.6f}\n", encoding="utf-8")
        self.assert_command(self.command("free-now"), 1, "backoff\n")
        _ = backoff.write_text(f"{time.time() - 1:.6f}\n", encoding="utf-8")
        self.assert_command(self.command("free-now"), 0, "free\n")

        _ = self.write_block(free_message={"kind": "not_needed"})
        self.assert_command(self.command("free-now"), 1, "blocked\n")
        _ = (self.state_directory / "block.json").write_text("damaged\n", encoding="utf-8")
        self.assert_command(self.command("free-now"), 1, "state\n")
        (self.state_directory / "block.json").unlink()

        settings = self.root / "off.conf"
        _ = settings.write_text("offload=off\n", encoding="utf-8")
        self.assert_command(self.command("free-now", "--settings", str(settings)), 1, "off\n")
        self.assert_command(self.command("free-now"), 0, "free\n")
        self.assertEqual(self.logged_arguments("gh"), [])

    def test_claim_reports_damaged_block_state(self) -> None:
        self.state_directory.mkdir()
        block_path = self.state_directory / "block.json"
        _ = block_path.write_text("damaged\n", encoding="utf-8")

        result = self.command(
            "claim",
            "--pid",
            str(os.getpid()),
            "--what",
            "alpha tests",
            "--worktree",
            str(self.root / "alpha"),
        )

        self.assert_command(result, 12, f"state unreadable: {block_path}\n")
        self.assertFalse((self.state_directory / "run.json").exists())

    def repository(self, name: str) -> Path:
        """A folder that sweep.repo_name reads as the named repository."""
        directory = self.root / "repositories" / name
        (directory / ".git").mkdir(parents=True)
        return directory

    def repository_claim(self, name: str) -> RunState:
        return cast(RunState, json.loads((self.state_directory / "claims" / f"{name}.json").read_text(encoding="utf-8")))

    def claim_in(self, owner: int, what: str, worktree: Path) -> subprocess.CompletedProcess[str]:
        return self.command("claim", "--pid", str(owner), "--what", what, "--worktree", str(worktree))

    def busy_line(self, record: RunState) -> str:
        since = datetime.fromisoformat(record["since"]).astimezone(LOCAL_ZONE).strftime("%H:%M")
        return f"busy: {record['what']} since {since}\n"

    def claim_names(self) -> list[str]:
        claims = self.state_directory / "claims"
        return sorted(path.name for path in claims.iterdir()) if claims.exists() else []

    def test_two_repositories_claim_the_mac_at_once(self) -> None:
        hana, liner = self.repository("hana"), self.repository("cargo-liner")
        first, second = self.start_child(), self.start_child()
        self.assert_command(self.claim_in(first.pid, "hana tests", hana), 0, "claimed\n")
        self.assert_command(self.claim_in(second.pid, "liner tests", liner), 0, "claimed\n")
        self.assertEqual(self.claim_names(), ["cargo-liner.json", "hana.json"])
        self.assertFalse((self.state_directory / "run.json").exists())
        self.assertEqual((self.repository_claim("hana")["pid"], self.repository_claim("hana")["worktree"]),
                         (first.pid, str(hana)))
        self.assertEqual((self.repository_claim("cargo-liner")["pid"], self.repository_claim("cargo-liner")["worktree"]),
                         (second.pid, str(liner)))
        self.write_config(ci_repo="")
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        lines = status.stdout.splitlines()
        self.assertEqual(lines[0], f"running: hana tests ({hana}) since {self.local_time(self.repository_claim('hana')['since'])}")
        self.assertEqual(lines[1], f"running: liner tests ({liner}) since "
                         + self.local_time(self.repository_claim("cargo-liner")["since"]))
        self.assertEqual(lines[2:], ["no block", "CI's Mac switch: not configured"])

    def test_second_claim_for_one_repository_is_busy(self) -> None:
        hana = self.repository("hana")
        (hana / "crates").mkdir()
        first, second = self.start_child(), self.start_child()
        self.assert_command(self.claim_in(first.pid, "hana tests", hana), 0, "claimed\n")
        held = self.repository_claim("hana")
        self.assert_command(self.claim_in(second.pid, "more hana tests", hana / "crates"), 11, self.busy_line(held))
        self.assertEqual(self.repository_claim("hana"), held)
        self.assertEqual(self.claim_names(), ["hana.json"])

    def test_dead_repository_claim_frees_itself(self) -> None:
        hana = self.repository("hana")
        owner = self.start_child()
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 0, "claimed\n")
        self.stop_child(owner)
        taker = self.start_child()
        self.assert_command(self.claim_in(taker.pid, "new hana tests", hana), 0, "claimed\n")
        after = self.repository_claim("hana")
        self.assertEqual((after["pid"], after["what"]), (taker.pid, "new hana tests"))

    def test_block_declines_every_repository_and_whole_mac_claim(self) -> None:
        hana, liner = self.repository("hana"), self.repository("cargo-liner")
        _ = self.write_block(free_message={"kind": "not_needed"})
        owner = self.start_child()
        for worktree in (hana, liner, self.root / "elsewhere"):
            with self.subTest(worktree=worktree.name):
                self.assert_command(self.claim_in(owner.pid, "tests", worktree), 10, "blocked by alice: release work\n")
        self.assertEqual(self.claim_names(), [])
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_free_now_answers_for_the_named_repository(self) -> None:
        hana, liner = self.repository("hana"), self.repository("cargo-liner")
        (hana / "crates").mkdir()
        self.write_config(offload="on")
        owner = self.start_child()
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 0, "claimed\n")
        self.assert_command(self.command("free-now", "--repo-root", str(liner)), 0, "free\n")
        self.assert_command(self.command("free-now", "--repo-root", str(hana)), 1, "busy\n")
        self.assert_command(self.command("free-now", "--repo-root", str(hana / "crates")), 1, "busy\n")
        self.assert_command(self.command("free-now"), 1, "busy\n")
        _ = self.write_block(free_message={"kind": "not_needed"})
        self.assert_command(self.command("free-now", "--repo-root", str(liner)), 1, "blocked\n")
        self.assertEqual(self.logged_arguments("gh"), [])

    def start_script_child(self, code: str) -> subprocess.Popen[str]:
        ready = self.root / f"ready-{len(self.children)}"
        child = subprocess.Popen([sys.executable, "-c", code + f"\nopen({str(ready)!r}, 'w').close()\n"
                                  + f"import time\ntime.sleep({TEST_HANG_GUARD_SECS})"], text=True)
        self.children.append(child)
        self.wait_for_path(ready)
        return child

    def copy_claim(self, owner: int, worktree: Path) -> subprocess.CompletedProcess[str]:
        return self.command("claim", "--pid", str(owner), "--what", "nextest hana", "--worktree", str(worktree),
                            "--copy")

    def test_copy_claim_record_status_and_free_now(self) -> None:
        hana = self.repository("hana")
        self.write_config(offload="on", ci_repo="")
        owner = self.start_child()
        self.assert_command(self.copy_claim(owner.pid, hana), 0, "claimed\n")
        record = cast(dict[str, object], json.loads((self.state_directory / "claims" / "hana.json").read_text()))
        self.assertIs(record["copy"], True)
        status = self.command("status")
        self.assertEqual(status.stdout.splitlines()[0], f"running: copy of nextest hana ({hana}) since "
                         + self.local_time(cast(str, record["since"])))
        # A copy holds the scope only for another copy.
        self.assert_command(self.command("free-now", "--repo-root", str(hana)), 0, "free\n")
        self.assert_command(self.command("free-now", "--repo-root", str(hana), "--copy"), 1, "busy\n")
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 0, "claimed\n")
        self.assertNotIn("copy", self.repository_claim("hana"))
        self.assert_command(self.command("free-now", "--repo-root", str(hana)), 1, "busy\n")
        self.assert_command(self.command("free-now", "--repo-root", str(hana), "--copy"), 1, "busy\n")

    def test_new_test_stops_a_copy_and_claims(self) -> None:
        hana = self.repository("hana")
        copy = self.start_child()
        self.assert_command(self.copy_claim(copy.pid, hana), 0, "claimed\n")
        taker = self.start_child()
        # The copy's parent reaps it, as the copy watcher reaps offload.py.
        reaper = threading.Thread(target=copy.wait)
        reaper.start()
        self.assert_command(self.claim_in(taker.pid, "hana tests", hana), 0, "claimed\n")
        reaper.join(timeout=10)
        self.assertEqual(copy.returncode, -15)
        self.assertEqual(self.repository_claim("hana")["pid"], taker.pid)

    def test_copy_that_does_not_stop_leaves_the_new_test_busy(self) -> None:
        hana = self.repository("hana")
        copy = self.start_script_child("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)")
        try:
            self.assert_command(self.copy_claim(copy.pid, hana), 0, "claimed\n")
            held = self.repository_claim("hana")
            taker = self.start_child()
            started = time.monotonic()
            result = self.command("claim", "--pid", str(taker.pid), "--what", "hana tests", "--worktree", str(hana),
                                  environment={**self.environment, "MAC_TEST_COPY_YIELD_S": "0.5"})
            self.assert_command(result, 11, self.busy_line(held))
            self.assertGreaterEqual(time.monotonic() - started, 0.5)
            self.assertIsNone(copy.poll())
            self.assertEqual(self.repository_claim("hana"), held)
        finally:
            copy.kill()

    def test_a_copy_never_replaces_a_copy_and_a_mac_only_run_never_gives_way(self) -> None:
        hana = self.repository("hana")
        first, second = self.start_child(), self.start_child()
        self.assert_command(self.copy_claim(first.pid, hana), 0, "claimed\n")
        held = self.repository_claim("hana")
        self.assert_command(self.copy_claim(second.pid, hana), 11, self.busy_line(held))
        self.assertIsNone(first.poll())
        self.assert_command(self.command("release", "--pid", str(first.pid)), 0, "")
        self.assert_command(self.claim_in(first.pid, "hana tests", hana), 0, "claimed\n")
        held = self.repository_claim("hana")
        self.assert_command(self.claim_in(second.pid, "more hana tests", hana), 11, self.busy_line(held))
        self.assert_command(self.copy_claim(second.pid, hana), 11, self.busy_line(held))
        self.assertIsNone(first.poll())

    def write_machine_wide_record(self, owner: int, worktree: Path) -> RunState:
        started = mac_test.process_start_time(owner)
        assert started is not None
        record: RunState = {"pid": owner, "proc_start": started, "what": "older tests",
                            "worktree": str(worktree), "since": datetime.now(timezone.utc).isoformat()}
        self.state_directory.mkdir(exist_ok=True)
        _ = (self.state_directory / "run.json").write_text(json.dumps(record), encoding="utf-8")
        return record

    def test_machine_wide_record_holds_the_repository_its_worktree_is_in(self) -> None:
        hana, liner = self.repository("hana"), self.repository("cargo-liner")
        self.write_config(offload="on")
        owner, other = self.start_child(), self.start_child()
        record = self.write_machine_wide_record(owner.pid, hana)
        legacy = self.state_directory / "run.json"
        before = legacy.read_bytes()
        self.assert_command(self.claim_in(other.pid, "hana tests", hana), 11, self.busy_line(record))
        self.assert_command(self.command("free-now", "--repo-root", str(hana)), 1, "busy\n")
        self.assert_command(self.command("free-now", "--repo-root", str(liner)), 0, "free\n")
        self.assert_command(self.claim_in(other.pid, "liner tests", liner), 0, "claimed\n")
        self.assertEqual(legacy.read_bytes(), before)
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        self.assertFalse(legacy.exists())
        self.assertEqual(self.claim_names(), ["cargo-liner.json"])
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 0, "claimed\n")

    def test_machine_wide_record_outside_any_repository_holds_every_repository(self) -> None:
        hana, liner = self.repository("hana"), self.repository("cargo-liner")
        self.write_config(offload="on")
        owner, other = self.start_child(), self.start_child()
        record = self.write_machine_wide_record(owner.pid, self.root / "elsewhere")
        legacy = self.state_directory / "run.json"
        before = legacy.read_bytes()
        for worktree in (hana, liner):
            with self.subTest(worktree=worktree.name):
                self.assert_command(self.claim_in(other.pid, "tests", worktree), 11, self.busy_line(record))
                self.assert_command(self.command("free-now", "--repo-root", str(worktree)), 1, "busy\n")
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(self.claim_names(), [])
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        self.assertFalse(legacy.exists())

    def test_whole_mac_claim_waits_for_any_repository_claim(self) -> None:
        hana, liner = self.repository("hana"), self.repository("cargo-liner")
        owner, other = self.start_child(), self.start_child()
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 0, "claimed\n")
        held = self.repository_claim("hana")
        self.assert_command(self.command("claim", "--pid", str(other.pid), "--what", "mac tests"), 11, self.busy_line(held))
        self.assertFalse((self.state_directory / "run.json").exists())
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        self.assert_command(self.command("claim", "--pid", str(other.pid), "--what", "mac tests"), 0, "claimed\n")
        whole = self.run_state()
        self.assertEqual(whole["worktree"], str(self.root))
        self.assert_command(self.claim_in(owner.pid, "liner tests", liner), 11, self.busy_line(whole))

    def test_release_removes_the_owner_claims_past_a_damaged_claim_file(self) -> None:
        hana = self.repository("hana")
        owner = self.start_child()
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 0, "claimed\n")
        damaged = self.state_directory / "claims" / "cargo-liner.json"
        _ = damaged.write_text("damaged\n", encoding="utf-8")
        self.assert_command(self.command("release", "--pid", str(owner.pid)), 0, "")
        self.assertEqual(self.claim_names(), ["cargo-liner.json"])
        self.assert_command(self.claim_in(owner.pid, "hana tests", hana), 12, f"state unreadable: {damaged}\n")

    def hold_events(self, expected: int = 1) -> list[HoldEventRecord]:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while True:
            rows = [cast(HoldEventRecord, json.loads(line))
                    for path in (self.root / "buildlog").rglob("*.jsonl")
                    for line in path.read_text().splitlines()]
            if len(rows) >= expected or time.monotonic() >= deadline:
                return sorted(rows, key=lambda row: (row["at"], row["event"] != "began"))
            time.sleep(0.01)

    def test_locked_log_does_not_delay_block_or_keep_coordination_locks(self) -> None:
        self.write_config(ci_repo="")
        log = self.root / "buildlog" / socket.gethostname().split(".")[0] / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
        log.parent.mkdir(parents=True)
        with log.open("a+") as held_log:
            fcntl.flock(held_log, fcntl.LOCK_EX)
            try:
                result = self.command("block", "--holder", "alice", "--for", "rendering")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                block = self.block_state()
                for name in ("state.lock", "control.lock"):
                    with (self.state_directory / name).open("a+") as coordination:
                        fcntl.flock(coordination, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        fcntl.flock(coordination, fcntl.LOCK_UN)
                self.assertEqual(log.read_text(), "")
            finally:
                fcntl.flock(held_log, fcntl.LOCK_UN)
        self.assertEqual([(row["event"], row["hold_key"]) for row in self.hold_events()],
                         [("began", block["since"])])

    def test_cleanup_waits_for_detached_recorder_to_finish_writing(self) -> None:
        self.write_config(ci_repo="")
        log = self.root / "buildlog" / socket.gethostname().split(".")[0] / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
        log.parent.mkdir(parents=True)
        waiting = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []

        def wait_for_writer() -> None:
            try:
                self.wait_for_fixture_writers()
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        real_select = select.select

        def observe_wait(readers: list[int], writers: list[int], exceptions: list[int],
                         timeout: float) -> tuple[list[int], list[int], list[int]]:
            # The writer pipe is still open while its recorder is held by flock.
            self.assertEqual(real_select(readers, writers, exceptions, 0)[0], [])
            waiting.set()
            return real_select(readers, writers, exceptions, timeout)

        with log.open("a+") as held_log, mock.patch.object(select, "select", side_effect=observe_wait):
            fcntl.flock(held_log, fcntl.LOCK_EX)
            result = self.command("block", "--holder", "alice", "--for", "rendering")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            waiter = threading.Thread(target=wait_for_writer)
            waiter.start()
            try:
                self.assertTrue(waiting.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(finished.is_set())
                self.assertEqual(log.read_text(), "")
            finally:
                fcntl.flock(held_log, fcntl.LOCK_UN)
                waiter.join(timeout=TEST_HANG_GUARD_SECS)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())
        self.assertEqual(len(self.hold_events()), 1)

    def test_recorder_waits_for_both_locks_to_close_and_reaps_finished_child(self) -> None:
        _ = self.write_block(free_message={"kind": "not_needed"})
        paths = mac_test.StatePaths(self.state_directory)
        block = mac_test.read_claim_block(paths)
        assert not isinstance(block, mac_test.NoMacBlock)
        launched: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def launch(command: list[str], *, stdin: int, stdout: int, stderr: int,
                   start_new_session: bool, close_fds: bool) -> subprocess.Popen[bytes]:
            for path in (paths.lock, paths.control_lock):
                with path.open("a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
            self.assertTrue(start_new_session)
            self.assertTrue(close_fds)
            process = real_popen(command, stdin=stdin, stdout=stdout, stderr=stderr,
                                 start_new_session=start_new_session, close_fds=close_fds,
                                 pass_fds=(self.writer_write_fd,))
            launched.append(process)
            return process

        with mock.patch.dict(os.environ, self.environment), mock.patch.object(subprocess, "Popen", side_effect=launch):
            with mac_test.locked(paths.control_lock):
                with mac_test.locked(paths.lock):
                    mac_test.record_hold_event("began", block, datetime.now(timezone.utc))
                    self.assertEqual(launched, [])
                self.assertEqual(launched, [])
            for thread in threading.enumerate():
                if thread.name == "hold-event-recorder":
                    thread.join(timeout=TEST_HANG_GUARD_SECS)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(len(launched), 1)
        self.assertEqual(launched[0].returncode, 0)
        with self.assertRaises(ChildProcessError):
            _ = os.waitpid(launched[0].pid, os.WNOHANG)
        self.assertEqual(len(self.hold_events()), 1)

    def test_blocks_within_one_second_have_distinct_since_keys_and_ordered_events(self) -> None:
        self.write_config(ci_repo="")
        source = """
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, sys.argv[1])
import mac_test

class Clock(datetime):
    tick: int = 0

    @classmethod
    def now(cls, tz: object = None) -> datetime:
        cls.tick += 1
        return datetime(2026, 10, 13, 12, tzinfo=timezone.utc) + timedelta(microseconds=cls.tick)

mac_test.datetime = Clock
for arguments in (
    ["block", "--holder", "alice", "--for", "first block"],
    ["unblock", "--holder", "alice"],
    ["block", "--holder", "alice", "--for", "second block"],
):
    sys.argv = ["mac_test.py", *arguments]
    assert mac_test.main() == 0
"""
        result = subprocess.run([sys.executable, "-c", source, str(SCRIPT.parent)],
                                env=self.environment, capture_output=True, text=True, timeout=60, check=False,
                                pass_fds=(self.writer_write_fd,))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.hold_events(3)
        self.assertEqual([row["event"] for row in rows], ["began", "ended", "began"])
        self.assertEqual(rows[0]["hold_key"], rows[1]["hold_key"])
        self.assertNotEqual(rows[0]["hold_key"], rows[2]["hold_key"])
        self.assertEqual(rows[2]["hold_key"], self.block_state()["since"])
        self.assertLess(rows[1]["at"], rows[2]["at"])
        self.assertEqual(rows[0]["at"][:19], rows[2]["at"][:19])

    def test_repeated_block_records_one_begin_and_unblock_records_one_end_with_since_key(self) -> None:
        first = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        initial = self.block_state()
        renewed = self.command("block", "--holder", "alice", "--for", "more rendering", "--hours", "5")
        self.assertEqual(renewed.returncode, 0, renewed.stdout + renewed.stderr)
        self.assertEqual(len(self.hold_events()), 1)
        self.assertEqual(self.block_state()["since"], initial["since"])
        result = self.command("unblock", "--holder", "alice")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = self.hold_events(2)
        self.assertEqual([(row["event"], row["target"], row["hold_key"]) for row in rows],
                         [("began", "mac", initial["since"]), ("ended", "mac", initial["since"])])
        self.assertEqual(rows[0]["reason"], "rendering")
        self.assertEqual(rows[0]["until"], initial["expires"])
        self.assertEqual(rows[1]["reason"], "more rendering")
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertTrue(all(datetime.fromisoformat(row["at"].replace("Z", "+00:00")).utcoffset() == timedelta(0)
                            for row in rows))
        _ = self.command("unblock", "--holder", "alice")
        self.assertEqual(len(self.hold_events(2)), 2)

    def test_pending_block_records_begin_before_it_becomes_active(self) -> None:
        result = self.command("claim", "--pid", str(os.getpid()), "--what", "local tests", "--worktree", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        blocked = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        block = self.block_state()
        self.assertEqual(block["state"], "pending")
        self.assertEqual([(row["event"], row["hold_key"]) for row in self.hold_events()], [("began", block["since"])])
        _ = self.command("release", "--pid", str(os.getpid()))
        _ = self.command("status")
        self.assertEqual(self.block_state()["state"], "active")
        self.assertEqual(len(self.hold_events()), 1)

    def test_pending_block_with_only_own_claim_activates_without_free_message(self) -> None:
        self.write_config(ci_repo=None)
        owner = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-a"}
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "own tests",
                                         environment=environment), 0, "claimed\n")
        _ = self.write_block(session="session-a", state="pending",
                             waiting_on={"kind": "local_run", "what": "own tests"})

        result = self.command("status")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.block_state()["state"], "active")
        self.assertEqual(self.block_state()["free_message"], {"kind": "not_needed"})
        self.assertEqual(self.run_state().get("holder_session"), "session-a")
        self.assertEqual(self.logged_arguments("send"), [])
        self.assertEqual(self.logged_arguments("send-delivered"), [])

    def test_pending_block_waits_on_other_or_unrecorded_claim_beside_own_claim(self) -> None:
        own_repository = self.repository("alpha")
        other_repository = self.repository("beta")
        owner = self.start_child()
        other = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-a"}
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "own tests",
                                         "--worktree", str(own_repository), environment=environment),
                            0, "claimed\n")
        for session in ("session-b", ""):
            with self.subTest(session=session):
                (self.state_directory / "block.json").unlink(missing_ok=True)
                other_environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": session}
                self.assert_command(self.command("claim", "--pid", str(other.pid), "--what", "other tests",
                                                 "--worktree", str(other_repository), environment=other_environment),
                                    0, "claimed\n")
                _ = self.write_block(session="session-a", state="pending",
                                     waiting_on={"kind": "local_run", "what": "own tests"})

                result = self.command("status")

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.block_state()["state"], "pending")
                self.assertEqual(self.block_state()["waiting_on"], {"kind": "local_run", "what": "other tests"})
                self.assert_command(self.command("release", "--pid", str(other.pid)), 0, "")
        self.assertEqual(self.logged_arguments("send-delivered"), [])

    def test_block_placed_during_own_claim_is_active_without_free_message(self) -> None:
        owner = self.start_child()
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-a"}
        self.assert_command(self.command("claim", "--pid", str(owner.pid), "--what", "own tests",
                                         environment=environment), 0, "claimed\n")

        result = self.command("block", "--holder", "alice", "--for", "own work", environment=environment)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.block_state()["state"], "active")
        self.assertEqual(self.block_state()["free_message"], {"kind": "not_needed"})
        self.assertEqual(self.run_state().get("holder_session"), "session-a")
        self.assertEqual(self.read_fixture_variable(), "false")
        self.assertEqual(self.logged_arguments("send-delivered"), [])

    def test_block_records_state_and_turns_the_switch_off(self) -> None:
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "session-123"}
        result = self.command(
            "block",
            "--holder",
            "alice",
            "--for",
            "rendering",
            "--showrunner",
            "showrunner-one",
            environment=environment,
        )
        block = self.block_state()
        self.assert_command(
            result,
            0,
            "Mac blocked for alice: nothing is running there, it is free now.\n"
            + self.expiry_line(block),
        )
        self.assertEqual(
            set(block),
            {
                "version",
                "holder",
                "for",
                "since",
                "expires",
                "session",
                "showrunner",
                "state",
                "free_message",
                "ci",
            },
        )
        self.assertEqual(block["version"], 2)
        self.assertEqual(block["session"], "session-123")
        self.assertEqual(block["showrunner"], "showrunner-one")
        self.assertEqual(block["state"], "active")
        self.assertEqual(block["free_message"], {"kind": "not_needed"})
        self.assertEqual(block["ci"], "off_by_this_block")
        self.assertEqual(self.gh_calls("variable set"), 1)
        self.assertIn("--body", self.logged_arguments("gh")[1])
        self.assertEqual(self.read_fixture_variable(), "false")

    def test_invalid_block_hours_write_nothing(self) -> None:
        for hours in ("0", "-1", "nan", "inf", "49"):
            with self.subTest(hours=hours):
                result = self.command(
                    "block",
                    "--holder",
                    "alice",
                    "--for",
                    "rendering",
                    "--hours",
                    hours,
                )
                self.assert_command(
                    result,
                    2,
                    "a block lasts more than 0 and at most 48 hours\n",
                )
                self.assertFalse((self.state_directory / "block.json").exists())
                self.assertEqual(self.logged_arguments("gh"), [])

    def test_default_block_hours_must_not_exceed_maximum(self) -> None:
        self.write_config(block_hours="72", block_max_hours="48")
        result = self.command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        self.assert_command(
            result,
            2,
            "a block lasts more than 0 and at most 48 hours\n",
        )
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertEqual(self.logged_arguments("gh"), [])

    def test_queued_or_in_progress_run_without_mac_job_is_busy(self) -> None:
        active_runs: tuple[tuple[str, list[GhRun], list[GhRun], int], ...] = (
            ("queued", [{"databaseId": 40}], [], 40),
            ("in_progress", [], [{"databaseId": 41}], 41),
        )
        for status, queued, in_progress, run_id in active_runs:
            with self.subTest(status=status):
                (self.state_directory / "block.json").unlink(missing_ok=True)
                self.gh["variable"] = "true"
                self.gh["queued"] = queued
                self.gh["in_progress"] = in_progress
                self.gh["jobs"] = {str(run_id): []}
                self.sync_gh()

                result = self.command(
                    "block", "--holder", "alice", "--for", "rendering"
                )

                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                self.assertIn("a CI run may still use the Mac", result.stdout)
                block = self.block_state()
                self.assertEqual(block["state"], "pending")
                self.assertEqual(block["waiting_on"], {"kind": "ci_job"})

    def test_completed_skipped_mac_job_is_idle(self) -> None:
        self.gh["queued"] = [{"databaseId": 42}]
        self.gh["jobs"] = {
            "42": [
                {"name": CI_JOB, "status": "completed", "conclusion": "skipped"}
            ]
        }
        self.sync_gh()

        result = self.command("block", "--holder", "alice", "--for", "rendering")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.block_state()["state"], "active")

    def test_busy_ci_keeps_block_pending_until_watch_sends_once(self) -> None:
        self.gh["queued"] = [{"databaseId": 41}]
        self.gh["jobs"] = {
            "41": [{"name": CI_JOB, "status": "in_progress", "conclusion": ""}]
        }
        self.sync_gh()
        pending = self.command("block", "--holder", "alice", "--for", "rendering")
        block = self.block_state()
        self.assert_command(
            pending,
            0,
            "Block pending for alice: a CI run may still use the Mac. Nothing new "
            + "starts there, and you get a message when CI no longer needs the Mac.\n"
            + self.expiry_line(block),
        )
        self.assertEqual(block["state"], "pending")
        self.assertEqual(block["waiting_on"], {"kind": "ci_job"})

        self.gh["variable"] = "false"
        self.gh["queued"] = []
        self.gh["jobs"] = {}
        self.sync_gh()
        watcher = self.start_command("watch")
        # The watcher records the message as sent only after the send process
        # exits, which is later than the stand-in's delivery log line.
        active = self.wait_for_free_message({"kind": "sent"})
        self.assertEqual(active["state"], "active")
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual(len(deliveries), 1)
        self.assertIn("CI's hold on the Mac ended", deliveries[0][-1])

        unblocked = self.command("unblock", "--holder", "alice")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)
        stdout, stderr = watcher.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(watcher.returncode, 0, stdout + stderr)
        self.assertEqual(len(self.logged_arguments("send-delivered")), 1)

    def test_failed_busy_check_records_unknown_ci(self) -> None:
        self.gh["failures"] = [
            {"match": "run list", "line": "API unavailable", "status": 1}
        ]
        self.sync_gh()
        result = self.command("block", "--holder", "alice", "--for", "rendering")
        block = self.block_state()
        self.assert_command(
            result,
            0,
            "Block pending for alice: CI could not be checked (API unavailable). "
            + "Nothing new starts there, and you get a message once CI's Mac job is "
            + "known to be idle.\n"
            + self.expiry_line(block),
        )
        self.assertEqual(block["waiting_on"], {"kind": "ci_unknown"})

    def test_unblock_restores_only_a_switch_owned_by_the_block(self) -> None:
        owned = self.active_block()
        self.clear_log("gh")
        result = self.command("unblock", "--holder", "alice")
        self.assert_command(
            result,
            0,
            "Mac unblocked; CI may use the Mac again.\n"
            + "No CI run skipped the macOS job under this block.\n",
        )
        self.assertEqual(self.gh_calls("variable set"), 1)
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertIn("since", owned)

        self.gh["variable"] = "false"
        self.sync_gh()
        elsewhere = self.active_block()
        self.assertEqual(elsewhere["ci"], "off_before")
        self.clear_log("gh")
        result = self.command("unblock", "--holder", "alice")
        self.assertTrue(
            result.stdout.startswith("Mac unblocked; CI's Mac switch was already off and stays off.\n")
        )
        self.assertEqual(self.gh_calls("variable set"), 0)

    def test_unblock_keeps_block_when_switch_restore_fails(self) -> None:
        block = self.active_block()
        self.gh["variable"] = "false"
        self.gh["failures"] = [
            {"match": "variable set", "line": "permission denied", "status": 1}
        ]
        self.sync_gh()
        result = self.command("unblock", "--holder", "alice")
        self.assert_command(
            result,
            2,
            "Mac still blocked: CI's Mac switch could not be turned back on "
            + "(permission denied). Run github-warmup, then unblock again.\n",
        )
        self.assertEqual(self.block_state(), block)
        self.assertEqual([(row["event"], row["hold_key"]) for row in self.hold_events()], [("began", block["since"])])

    def test_expired_block_restores_switch_and_messages_both_recipients(self) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            holder="alice",
            reason="rendering",
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            session="holder-session",
            showrunner="showrunner-one",
            free_message={"kind": "not_needed"},
        )
        result = self.command("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertEqual([(row["event"], row["hold_key"]) for row in self.hold_events()], [("ended", block["since"])])
        self.assertEqual(self.gh_calls("variable set"), 1)
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(deliveries[0][:2], ["--to", f"uds:{self.stub_directory / 'session.sock'}"])
        self.assertEqual(deliveries[1][:2], ["--to", "showrunner-one"])
        expiry = self.local_time(cast(str, block["expires"]), include_day=True)
        for delivery in deliveries:
            text = delivery[delivery.index("--text") + 1]
            self.assertIn(f"reached its time limit at {expiry} and lifted by itself.", text)
            self.assertIn("CI may use the Mac again.", text)
            self.assertIn("No CI run skipped the macOS job under this block.", text)

    def test_expiry_restore_failure_keeps_block_and_starts_watcher(self) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            showrunner="showrunner-one",
            free_message={"kind": "not_needed"},
        )
        self.gh["failures"] = [
            {"match": "variable set", "line": "permission denied", "status": 1}
        ]
        self.sync_gh()
        result = self.command("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.block_state(), block)
        self.assertEqual(self.hold_events(0), [])
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual([call[1] for call in deliveries], ["alice", "showrunner-one"])
        for delivery in deliveries:
            self.assertIn("past its time limit but still in place", delivery[-1])
            self.assertIn("--repeat-minutes", delivery)
            self.assertEqual(delivery[delivery.index("--repeat-minutes") + 1], "60")
        self.assertEqual(len(self.logged_arguments("systemd-run")), 1)

    def test_active_expired_block_with_failed_switch_back_status_names_stuck_lift(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            free_message={"kind": "not_needed"},
        )
        self.gh["failures"] = [
            {"match": "variable set", "line": "gpg-agent is cold", "status": 1}
        ]
        self.sync_gh()

        status = self.command("status")

        self.assert_command(
            status,
            0,
            "free\n"
            + f"blocked by alice since {self.local_time(cast(str, block['since']), include_day=True)}, "
            + f"lifts {self.local_time(cast(str, block['expires']), include_day=True)}: release work\n"
            + "CI's Mac switch: off (this block)\n"
            + "The block is past its time limit but could not lift: CI's Mac switch "
            + "could not be turned back on (gpg-agent is cold). Run github-warmup, "
            + "then unblock.\n",
        )

    def test_pending_expired_block_with_failed_switch_back_status_names_stuck_lift(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            state="pending",
            waiting_on={"kind": "ci_job"},
        )
        self.gh["failures"] = [
            {"match": "variable set", "line": "gpg-agent is cold", "status": 1}
        ]
        self.sync_gh()

        status = self.command("status")

        self.assert_command(
            status,
            0,
            "free\n"
            + "block pending for alice (a CI run may still use the Mac), "
            + f"lifts {self.local_time(cast(str, block['expires']), include_day=True)}: release work\n"
            + "CI's Mac switch: off (this block)\n"
            + "The block is past its time limit but could not lift: CI's Mac switch "
            + "could not be turned back on (gpg-agent is cold). Run github-warmup, "
            + "then unblock.\n",
        )

    def test_unexpired_block_status_has_no_stuck_lift_line(self) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=1),
            expires=now + timedelta(hours=1),
            free_message={"kind": "not_needed"},
        )

        status = self.command("status")

        self.assert_command(
            status,
            0,
            "free\n"
            + f"blocked by alice since {self.local_time(cast(str, block['since']), include_day=True)}, "
            + f"lifts {self.local_time(cast(str, block['expires']), include_day=True)}: release work\n"
            + "CI's Mac switch: off (this block)\n",
        )

    def test_failed_switch_off_on_new_block_status_has_no_stuck_lift_line(
        self,
    ) -> None:
        self.gh["failures"] = [
            {"match": "variable set", "line": "request lost", "status": 1}
        ]
        self.sync_gh()
        blocked = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        block = self.block_state()

        status = self.command("status")

        self.assert_command(
            status,
            0,
            "free\n"
            + f"blocked by alice since {self.local_time(cast(str, block['since']), include_day=True)}, "
            + f"lifts {self.local_time(cast(str, block['expires']), include_day=True)}: rendering\n"
            + "CI's Mac switch: still on (request lost)\n",
        )

    def test_immediately_expired_renewal_reports_switch_restore_failure(
        self,
    ) -> None:
        _ = self.write_block(
            free_message={"kind": "not_needed"},
            ci="off_unconfirmed",
        )
        self.gh["failures"] = [
            {"match": "variable set", "line": "cold signing agent", "status": 1}
        ]
        self.sync_gh()

        renewed = self.command(
            "block",
            "--holder",
            "alice",
            "--for",
            "renewed work",
            "--hours",
            "0.0000000001",
        )

        self.assertEqual(renewed.returncode, 2, renewed.stdout + renewed.stderr)
        self.assertIn(
            "CI can still use the Mac: cold signing agent. "
            + "Run github-warmup, then block again.\n",
            renewed.stdout,
        )
        self.assertNotIn("unknown error", renewed.stdout)

    def test_failed_expired_lift_keeps_block_until_next_status_recovers(self) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            free_message={"kind": "not_needed"},
        )
        self.gh["failures"] = [
            {"match": "variable set", "line": "authentication required", "status": 1}
        ]
        self.sync_gh()
        stuck = self.command("status")
        self.assertIn(
            "The block is past its time limit but could not lift:", stuck.stdout
        )
        self.assertEqual(self.block_state(), block)

        self.gh["failures"] = []
        self.sync_gh()
        recovered = self.command("status")

        self.assert_command(
            recovered,
            0,
            "free\nno block\nCI's Mac switch: on\n",
        )
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertNotIn("could not lift", recovered.stdout)

    def test_claim_block_reader_keeps_expired_block_without_writes(self) -> None:
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            free_message={"kind": "not_needed"},
        )
        paths = mac_test.StatePaths(self.state_directory)
        before = paths.block.read_bytes()
        with mock.patch("subprocess.run", side_effect=AssertionError("State reading stays local")):
            with mock.patch("subprocess.Popen", side_effect=AssertionError("State reading stays local")):
                block = mac_test.read_claim_block(paths)
        self.assertIsInstance(block, mac_test.ActiveMacBlock)
        if isinstance(block, mac_test.ActiveMacBlock):
            self.assertLess(block.expires, now)
        self.assertEqual(paths.block.read_bytes(), before)
        self.assertEqual(tuple(self.state_directory.iterdir()), (paths.block,))
        paths.block.unlink()
        self.assertIsInstance(mac_test.read_claim_block(paths), mac_test.NoMacBlock)
        self.assertEqual(tuple(self.state_directory.iterdir()), ())

    def test_invalid_claim_block_raises_native_state_read_error(self) -> None:
        self.state_directory.mkdir()
        paths = mac_test.StatePaths(self.state_directory)
        _ = paths.block.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(mac_test.StateReadError, "invalid block record"):
            _ = mac_test.read_claim_block(paths)

    def test_invalid_claim_record_raises_native_state_read_error(self) -> None:
        self.state_directory.mkdir()
        paths = mac_test.StatePaths(self.state_directory)
        _ = paths.run.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(mac_test.StateReadError, "invalid run record"):
            _ = mac_test.read_run(paths.run)

    def test_claim_refuses_expired_block_until_ci_restore_lifts_it(self) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            free_message={"kind": "not_needed"},
        )
        self.gh["failures"] = [
            {"match": "variable set", "line": "authentication required", "status": 1}
        ]
        self.sync_gh()
        stuck = self.command("status")
        self.assertEqual(stuck.returncode, 0, stuck.stdout + stuck.stderr)
        self.assertEqual(self.block_state(), block)
        self.assert_command(
            self.command("claim", "--pid", str(os.getpid()), "--what", "nextest hana"),
            10,
            "blocked by alice: release work\n",
        )
        self.assertEqual(self.block_state(), block)
        self.assertFalse((self.state_directory / "run.json").exists())

        self.gh["failures"] = []
        self.sync_gh()
        lifted = self.command("status")
        self.assertEqual(lifted.returncode, 0, lifted.stdout + lifted.stderr)
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assert_command(
            self.command("claim", "--pid", str(os.getpid()), "--what", "nextest hana"),
            0,
            "claimed\n",
        )

    def test_unblock_lifts_owned_switch_when_repository_is_removed(self) -> None:
        self.write_config(ci_repo="")
        _ = self.write_block(free_message={"kind": "not_needed"})
        result = self.command("unblock", "--holder", "alice")
        self.assert_command(result, 0, "Mac unblocked.\n")
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertEqual(self.logged_arguments("gh"), [])

    def test_expiry_lifts_owned_switch_when_repository_is_removed(self) -> None:
        self.write_config(ci_repo="")
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            free_message={"kind": "not_needed"},
        )
        result = self.command("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertEqual(self.logged_arguments("gh"), [])
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual(len(deliveries), 1)
        text = deliveries[0][deliveries[0].index("--text") + 1]
        self.assertNotIn("CI may use the Mac again.", text)
        self.assertNotIn("skipped the macOS job", text)

    def test_expiry_warning_is_delivered_once_for_each_expiry(self) -> None:
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=1),
            expires=now + timedelta(minutes=5),
            free_message={"kind": "not_needed"},
        )
        first = self.command("status")
        second = self.command("status")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual(len(deliveries), 1)
        warning = deliveries[0]
        self.assertEqual(warning[:2], ["--to", "alice"])
        self.assertEqual(warning[warning.index("--repeat-minutes") + 1], "1440")
        self.assertIn(cast(str, block["expires"]), warning[warning.index("--key") + 1])

    def test_renewal_keeps_since_and_updates_expiry_session_and_reason(self) -> None:
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "first-session"}
        first = self.command(
            "block",
            "--holder",
            "alice",
            "--for",
            "first reason",
            "--hours",
            "1",
            environment=environment,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        original = self.block_state()
        environment["CLAUDE_CODE_SESSION_ID"] = "second-session"
        renewed = self.command(
            "block",
            "--holder",
            "alice",
            "--for",
            "second reason",
            "--hours",
            "2",
            "--showrunner",
            "showrunner-two",
            environment=environment,
        )
        self.assertEqual(renewed.returncode, 0, renewed.stdout + renewed.stderr)
        replacement = self.block_state()
        self.assertEqual(replacement["since"], original["since"])
        self.assertNotEqual(replacement["expires"], original["expires"])
        self.assertEqual(replacement["session"], "second-session")
        self.assertEqual(replacement["showrunner"], "showrunner-two")
        self.assertEqual(replacement["for"], "second reason")
        self.assertEqual(replacement["state"], original["state"])
        self.assertEqual(replacement["ci"], original["ci"])

    def test_skipped_runs_are_grouped_by_branch(self) -> None:
        _ = self.active_block()
        self.gh["variable"] = "false"
        self.gh["created"] = [
            {"databaseId": 3, "headBranch": "zeta", "headSha": "333333333abcdef"},
            {"databaseId": 2, "headBranch": "alpha", "headSha": "222222222abcdef"},
            {"databaseId": 1, "headBranch": "alpha", "headSha": "111111111abcdef"},
        ]
        self.gh["jobs"] = {
            "3": [{"name": CI_JOB, "conclusion": "skipped"}],
            "2": [{"name": CI_JOB, "conclusion": "skipped"}],
            "1": [{"name": CI_JOB, "conclusion": "skipped"}],
        }
        self.sync_gh()
        result = self.command("unblock", "--holder", "alice")
        self.assert_command(
            result,
            0,
            "Mac unblocked; CI may use the Mac again.\n"
            + "CI runs that skipped the macOS job under this block:\n"
            + "  alpha: 2 runs, newest 222222222\n"
            + "  zeta: 1 runs, newest 333333333\n"
            + "Run CI again on each branch's newest commit to make up its macOS check: "
            + "gh workflow run ci.yml --repo natepiano/hana --ref <branch>\n",
        )

    def test_skipped_run_report_handles_none_failure_and_two_hundred(self) -> None:
        _ = self.active_block()
        self.gh["variable"] = "false"
        self.gh["failures"] = [
            {"match": "--created", "line": "list failed", "status": 1}
        ]
        self.sync_gh()
        failed = self.command("unblock", "--holder", "alice")
        self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertIn(
            "Could not list the CI runs that skipped the macOS job: list failed\n",
            failed.stdout,
        )

        self.gh["failures"] = []
        self.gh["variable"] = "true"
        self.gh["created"] = []
        self.sync_gh()
        _ = self.active_block()
        self.gh["variable"] = "false"
        self.gh["created"] = [
            {
                "databaseId": index,
                "headBranch": "alpha",
                "headSha": f"{index:09d}abcdef",
            }
            for index in range(SKIPPED_REPORT_RUNS, 0, -1)
        ]
        self.gh["jobs"] = {
            str(index): [{"name": "another job", "conclusion": "success"}]
            for index in range(1, SKIPPED_REPORT_RUNS + 1)
        }
        self.sync_gh()
        self.clear_log("gh")
        capped = self.command("unblock", "--holder", "alice")
        self.assertEqual(capped.returncode, 0, capped.stdout + capped.stderr)
        self.assertEqual(self.gh_calls("run view"), SKIPPED_REPORT_RUNS)
        self.assertIn("No CI run skipped the macOS job under this block.\n", capped.stdout)
        self.assertIn(
            "Only the newest 200 runs were looked at; older ones under this block are not listed.\n",
            capped.stdout,
        )

    def test_failed_switch_on_block_is_retried_by_status(self) -> None:
        self.gh["failures"] = [
            {"match": "variable get", "line": "authentication required", "status": 1},
            {"match": "run list", "line": "authentication required", "status": 1},
        ]
        self.sync_gh()
        blocked = self.command("block", "--holder", "alice", "--for", "rendering")
        block = self.block_state()
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn(
            "CI can still use the Mac: authentication required. Run github-warmup, then block again.\n",
            blocked.stdout,
        )
        self.assertEqual(block["ci"], "still_on")

        self.gh["failures"] = []
        self.sync_gh()
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(self.block_state()["ci"], "off_by_this_block")
        self.assertEqual(self.read_fixture_variable(), "false")

    def test_timed_out_set_is_reconciled_and_restored(self) -> None:
        # The set reaches gh and is applied, then the call times out. Expire
        # it at the subprocess boundary after the stand-in returns: a real
        # short timer can kill the stand-in before it applies under load.
        timeout_stub = r'''
original_run = subprocess.run

def expire_after_set(command, *args, **kwargs):
    if command[:3] == ["gh", "variable", "set"]:
        _ = original_run(command, *args, **kwargs)
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
    return original_run(command, *args, **kwargs)

subprocess.run = expire_after_set
'''
        lifetime = self.root / "child-lifetime" / "sitecustomize.py"
        _ = lifetime.write_text(FIXTURE_CHILD_LIFETIME + timeout_stub)
        blocked = self.start_command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        stdout, stderr = blocked.communicate(timeout=TEST_HANG_GUARD_SECS)
        _ = lifetime.write_text(FIXTURE_CHILD_LIFETIME)
        self.assertEqual(blocked.returncode, 2, stdout + stderr)
        self.assertIn("CI can still use the Mac:", stdout)
        self.assertEqual(self.block_state()["ci"], "off_unconfirmed")
        self.assertEqual(self.read_fixture_variable(), "false")

        self.gh["variable"] = "false"
        self.sync_gh()
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(self.block_state()["ci"], "off_by_this_block")

        self.clear_log("gh")
        unblocked = self.command("unblock", "--holder", "alice")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)
        self.assertTrue(
            unblocked.stdout.startswith("Mac unblocked; CI may use the Mac again.\n")
        )
        self.assertEqual(self.read_fixture_variable(), "true")
        self.assertEqual(self.gh_calls("variable set"), 1)

    def test_failed_set_is_restored_even_when_the_variable_remains_on(self) -> None:
        self.gh["failures"] = [
            {"match": "variable set", "line": "request lost", "status": 1}
        ]
        self.sync_gh()
        blocked = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("CI can still use the Mac: request lost.", blocked.stdout)
        self.assertEqual(self.block_state()["ci"], "off_unconfirmed")
        self.assertEqual(self.read_fixture_variable(), "true")

        self.gh["failures"] = []
        self.sync_gh()
        self.clear_log("gh")
        unblocked = self.command("unblock", "--holder", "alice")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)
        self.assertTrue(
            unblocked.stdout.startswith("Mac unblocked; CI may use the Mac again.\n")
        )
        set_call = next(
            call for call in self.logged_arguments("gh")
            if call[:2] == ["variable", "set"]
        )
        self.assertEqual(set_call[set_call.index("--body") + 1], "true")
        self.assertEqual(self.read_fixture_variable(), "true")

    def test_expiry_of_unconfirmed_set_restores_the_switch(self) -> None:
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(minutes=1),
            free_message={"kind": "not_needed"},
            ci="off_unconfirmed",
        )
        result = self.command("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state_directory / "block.json").exists())
        self.assertEqual(self.read_fixture_variable(), "true")
        self.assertEqual(self.gh_calls("variable set"), 1)
        delivered = self.logged_arguments("send-delivered")
        self.assertEqual(len(delivered), 1)
        self.assertIn("CI may use the Mac again.", delivered[0][-1])

    def test_gh_timeout_uses_configured_limit(self) -> None:
        self.write_config(gh_timeout_s="0.2")
        # Inject expiry at the subprocess boundary: a real 0.2 s timer can
        # kill the Python gh stand-in before it even starts under load.
        timeout_stub = r'''
from pathlib import Path

original_run = subprocess.run

def expire_gh(command, *args, **kwargs):
    if command[0] == "gh":
        root = Path(os.environ["MAC_TEST_STUB_DIR"])
        with (root / "gh-timeout-limits").open("a") as limits:
            limits.write(str(kwargs.get("timeout")) + "\n")
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
    return original_run(command, *args, **kwargs)

subprocess.run = expire_gh
'''
        _ = (self.root / "child-lifetime" / "sitecustomize.py").write_text(
            FIXTURE_CHILD_LIFETIME + timeout_stub
        )
        process = self.start_command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        limits_path = self.stub_directory / "gh-timeout-limits"
        self.wait_for_path(limits_path, timeout=TEST_HANG_GUARD_SECS)
        stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        limits = [float(line) for line in limits_path.read_text().splitlines()]
        self.assertTrue(limits)
        self.assertEqual(limits, [0.2] * len(limits))
        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("gh timed out after 0.2 s", stdout)
        self.assertEqual(self.block_state()["ci"], "still_on")

    def test_claim_does_not_wait_for_slow_gh_in_block(self) -> None:
        child = self.start_child()
        self.gh["delays"] = [
            {
                "match": "variable get",
                "seconds": 300.0,
                "marker": "slow-gh-started",
                "release": "continue-gh",
            }
        ]
        self.sync_gh()
        blocker = self.start_command("block", "--holder", "alice", "--for", "rendering")
        self.wait_for_path(self.stub_directory / "slow-gh-started")
        claim = self.command("claim", "--pid", str(child.pid), "--what", "alpha tests")
        self.assert_command(claim, 10, "blocked by alice: rendering\n")
        self.assertIsNone(blocker.poll())
        _ = (self.stub_directory / "continue-gh").write_text(
            "continue\n", encoding="utf-8"
        )
        stdout, stderr = blocker.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(blocker.returncode, 0, stdout + stderr)

    def test_release_starts_watcher_without_gh_or_send(self) -> None:
        child = self.start_child()
        self.claim(child)
        pending = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(pending.returncode, 0, pending.stdout + pending.stderr)
        self.clear_log("gh")
        self.clear_log("send")
        self.clear_log("systemd-run")
        released = self.command("release", "--pid", str(child.pid))
        self.assert_command(released, 0, "")
        self.assertEqual(self.logged_arguments("gh"), [])
        self.assertEqual(self.logged_arguments("send"), [])
        calls = self.logged_arguments("systemd-run")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call[:5], ["--user", "--collect", "--quiet", "--no-block", "--unit"])
        self.assertIn("--", call)
        command = call[call.index("--") + 1 :]
        self.assertEqual(command[0], "/usr/bin/env")
        self.assertEqual(command[-1], "watch")
        self.assertEqual(self.block_state()["state"], "pending")

    def test_missing_systemd_run_warns_without_changing_block_result(self) -> None:
        self.write_config(ci_repo="")
        (self.bin_directory / "systemd-run").unlink()
        environment = {**self.environment, "PATH": str(self.bin_directory)}
        result = self.command(
            "block",
            "--holder",
            "alice",
            "--for",
            "rendering",
            environment=environment,
        )
        block = self.block_state()
        self.assert_command(
            result,
            0,
            "Mac blocked for alice: nothing is running there, it is free now.\n"
            + self.expiry_line(block),
            "warning: the Mac block watcher could not be started\n",
        )

    def test_renewal_waiting_for_watcher_does_not_get_overwritten(self) -> None:
        now = datetime.now(timezone.utc)
        original = self.write_block(
            since=now - timedelta(hours=1),
            expires=now + timedelta(hours=3),
            state="pending",
            waiting_on={"kind": "ci_unknown"},
            ci="off_by_this_block",
        )
        self.gh["variable"] = "false"
        self.gh["delays"] = [
            {"match": "run list", "seconds": TEST_HANG_GUARD_SECS, "marker": "watch-gh-started",
             "release": "continue-renewal-gh"}
        ]
        self.sync_gh()
        watcher = self.start_command("watch")
        self.wait_for_path(self.stub_directory / "watch-gh-started")
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "new-session",
                       "MAC_TEST_LOCK_ATTEMPT": str(self.stub_directory / "renewal-lock-attempt")}
        renewal = self.start_command(
            "block",
            "--holder",
            "alice",
            "--for",
            "renewed work",
            "--hours",
            "3",
            environment=environment,
        )
        self.wait_for_path(self.stub_directory / "renewal-lock-attempt")
        _ = (self.stub_directory / "continue-renewal-gh").write_text("continue\n")
        stdout, stderr = renewal.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(renewal.returncode, 0, stdout + stderr)
        block = self.block_state()
        self.assertEqual(block["for"], "renewed work")
        self.assertEqual(block["session"], "new-session")
        self.assertEqual(block["since"], original["since"])
        watcher.terminate()
        _ = watcher.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_unblock_and_expiry_restore_switch_once(self) -> None:
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(seconds=1),
            free_message={"kind": "not_needed"},
        )
        self.gh["variable"] = "false"
        self.gh["delays"] = [
            {"match": "variable set", "seconds": TEST_HANG_GUARD_SECS, "marker": "restore-started",
             "release": "continue-restore-gh"}
        ]
        self.sync_gh()
        expiry = self.start_command("status")
        self.wait_for_path(self.stub_directory / "restore-started")
        unblock = self.start_command("unblock", "--holder", "alice", environment={
            **self.environment, "MAC_TEST_LOCK_ATTEMPT": str(self.stub_directory / "unblock-lock-attempt")})
        self.wait_for_path(self.stub_directory / "unblock-lock-attempt")
        _ = (self.stub_directory / "continue-restore-gh").write_text("continue\n")
        expiry_output = expiry.communicate(timeout=TEST_HANG_GUARD_SECS)
        unblock_output = unblock.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(expiry.returncode, 0, "".join(expiry_output))
        self.assertEqual(unblock.returncode, 0, "".join(unblock_output))
        self.assertEqual(self.gh_calls("variable set"), 1)
        self.assertFalse((self.state_directory / "block.json").exists())

    def test_second_watcher_exits_while_first_holds_watch_lock(self) -> None:
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            since=now - timedelta(hours=1),
            expires=now + timedelta(hours=3),
            state="pending",
            waiting_on={"kind": "ci_unknown"},
            ci="off_before",
        )
        self.gh["variable"] = "false"
        self.gh["delays"] = [
            {
                "match": "run list",
                "seconds": 300.0,
                "marker": "watch-gh-started",
                "release": "continue-watch-gh",
            }
        ]
        self.sync_gh()
        first = self.start_command("watch")
        self.wait_for_path(self.stub_directory / "watch-gh-started")
        second = self.command("watch")
        self.assert_command(second, 0, "")
        self.assertIsNone(first.poll())
        _ = (self.stub_directory / "continue-watch-gh").write_text(
            "continue\n", encoding="utf-8"
        )
        unblocked = self.command("unblock", "--holder", "alice")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)
        stdout, stderr = first.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(first.returncode, 0, stdout + stderr)

    def test_failed_free_message_stays_owed_and_status_reports_it(self) -> None:
        child = self.start_child()
        self.claim(child, "delta tests")
        blocked = self.command("block", "--holder", "dana", "--for", "release work")
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.stop_child(child)
        _ = (self.stub_directory / "send-status").write_text("3\n", encoding="utf-8")
        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn('The "Mac is free" message has not reached dana yet.\n', status.stdout)
        block = self.block_state()
        free_message = cast(dict[str, object], block["free_message"])
        self.assertEqual(free_message["kind"], "owed")

        (self.stub_directory / "send-status").unlink()
        repeated = self.command("status")
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertNotIn('The "Mac is free" message has not reached', repeated.stdout)
        self.assertEqual(self.block_state()["free_message"], {"kind": "sent"})

    def test_failed_session_lookup_sends_to_the_holder(self) -> None:
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            holder="alice",
            since=now - timedelta(hours=1),
            expires=now + timedelta(hours=2),
            session="recorded-session",
            free_message={
                "kind": "owed",
                "what": "render tests",
                "ended": now.isoformat(timespec="seconds"),
            },
            ci="off_before",
        )
        _ = (self.stub_directory / "session-missing").write_text(
            "missing\n", encoding="utf-8"
        )
        result = self.command("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0][:2], ["--to", "alice"])

    def test_message_timeout_leaves_the_free_message_owed(self) -> None:
        child = self.start_child()
        self.claim(child, "delta tests")
        blocked = self.command("block", "--holder", "dana", "--for", "release work")
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.stop_child(child)
        _ = (self.stub_directory / "send-delay").write_text(
            "30\n", encoding="utf-8"
        )
        environment = {
            **self.environment,
            "MAC_TEST_MESSAGE_TIMEOUT_S": "0.05",
        }
        status = self.command("status", environment=environment)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn(
            'The "Mac is free" message has not reached dana yet.\n',
            status.stdout,
        )
        free_message = cast(dict[str, object], self.block_state()["free_message"])
        self.assertEqual(free_message["kind"], "owed")

    def test_status_lines_cover_active_and_pending_local_run(self) -> None:
        now = datetime.now(timezone.utc)
        active = self.write_block(
            since=now - timedelta(hours=1),
            expires=now + timedelta(hours=2),
            free_message={"kind": "not_needed"},
            ci="off_before",
        )
        result = self.command("status")
        self.assertIn(
            f"blocked by alice since {self.local_time(cast(str, active['since']), include_day=True)}, "
            + f"lifts {self.local_time(cast(str, active['expires']), include_day=True)}: release work\n",
            result.stdout,
        )
        self.assertIn("CI's Mac switch: off (set elsewhere)\n", result.stdout)

        (self.state_directory / "block.json").unlink()
        child = self.start_child()
        self.claim(child)
        pending = self.command("block", "--holder", "bob", "--for", "local work")
        self.assertEqual(pending.returncode, 0, pending.stdout + pending.stderr)
        status = self.command("status")
        self.assertIn("block pending for bob (a test is running), lifts ", status.stdout)

    def test_status_reports_pending_ci_job_and_unknown_ci(self) -> None:
        self.gh["variable"] = "false"
        self.gh["queued"] = [{"databaseId": 71}]
        self.gh["jobs"] = {
            "71": [{"name": CI_JOB, "status": "queued", "conclusion": ""}]
        }
        self.sync_gh()
        now = datetime.now(timezone.utc)
        block = self.write_block(
            since=now - timedelta(hours=1),
            expires=now + timedelta(hours=2),
            state="pending",
            waiting_on={"kind": "ci_job"},
            ci="off_before",
        )
        busy = self.command("status")
        self.assertIn(
            "block pending for alice (a CI run may still use the Mac), "
            + f"lifts {self.local_time(cast(str, block['expires']), include_day=True)}: "
            + "release work\n",
            busy.stdout,
        )

        self.gh["queued"] = []
        self.gh["failures"] = [
            {"match": "run list", "line": "API unavailable", "status": 1}
        ]
        self.sync_gh()
        unknown = self.command("status")
        self.assertIn(
            "block pending for alice (CI could not be checked), "
            + f"lifts {self.local_time(cast(str, block['expires']), include_day=True)}: "
            + "release work\n",
            unknown.stdout,
        )

    def test_empty_repository_never_calls_gh(self) -> None:
        self.write_config(ci_repo="")
        blocked = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.assertEqual(self.block_state()["ci"], "not_configured")
        status = self.command("status")
        self.assertIn("CI's Mac switch: not configured\n", status.stdout)
        unblocked = self.command("unblock", "--holder", "alice")
        self.assert_command(unblocked, 0, "Mac unblocked.\n")
        self.assertEqual(self.logged_arguments("gh"), [])

    def test_legacy_block_decodes_and_is_upgraded_during_settle(self) -> None:
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
        self.state_directory.mkdir(parents=True)
        _ = (self.state_directory / "block.json").write_text(
            json.dumps(
                {
                    "holder": "alice",
                    "session": None,
                    "for": "rendering",
                    "since": since,
                    "state": "active",
                }
            ),
            encoding="utf-8",
        )
        decoded = mac_test.read_block(self.state_directory / "block.json")
        self.assertIsInstance(decoded, mac_test.ActiveMacBlock)
        active = cast(mac_test.ActiveMacBlock, decoded)
        self.assertEqual(active.since.isoformat(), since)
        self.assertEqual(active.expires, active.since + timedelta(hours=48))
        self.assertEqual(active.ci, "still_on")

        status = self.command("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        upgraded = self.block_state()
        self.assertEqual(upgraded["version"], 2)
        self.assertEqual(upgraded["since"], since)
        self.assertEqual(upgraded["ci"], "off_by_this_block")

    def test_read_block_rejects_bad_json_missing_key_and_naive_time(self) -> None:
        self.state_directory.mkdir(parents=True)
        path = self.state_directory / "block.json"
        _ = path.write_text("{", encoding="utf-8")
        with self.assertRaises(ValueError):
            _ = mac_test.read_block(path)

        record = self.write_block(free_message={"kind": "not_needed"})
        del record["expires"]
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            _ = mac_test.read_block(path)

        record = self.write_block(
            free_message={"kind": "not_needed"}, ci="off_unconfirmed"
        )
        decoded = mac_test.read_block(path)
        self.assertIsInstance(decoded, mac_test.ActiveMacBlock)
        if isinstance(decoded, mac_test.ActiveMacBlock):
            self.assertEqual(decoded.ci, "off_unconfirmed")

        record["ci"] = "unrecognized"
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            _ = mac_test.read_block(path)

        record = self.write_block(free_message={"kind": "not_needed"})
        record["expires"] = "2026-10-07T12:00:00"
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ValueError):
            _ = mac_test.read_block(path)

    def test_slow_send_does_not_delay_claim(self) -> None:
        child = self.start_child()
        other = self.start_child()
        self.claim(child, "alpha tests")
        blocked = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        self.stop_child(child)
        _ = (self.stub_directory / "send-hold").write_text(
            "hold\n", encoding="utf-8"
        )
        status = self.start_command("status")
        self.wait_for_path(self.stub_directory / "send-started")
        claim = self.command("claim", "--pid", str(other.pid), "--what", "beta tests")
        self.assert_command(claim, 10, "blocked by alice: rendering\n")
        self.assertIsNone(status.poll())
        _ = (self.stub_directory / "send-continue").write_text(
            "continue\n", encoding="utf-8"
        )
        stdout, stderr = status.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(status.returncode, 0, stdout + stderr)


class ClaimPassEnded(Exception):
    """Stop the test after one claim watcher pass."""


class ClaimWatchTests(unittest.TestCase):
    root: Path = Path()
    paths: mac_test.StatePaths = mac_test.StatePaths(Path())
    now: datetime = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = mac_test.StatePaths(self.root / "state")
        self.paths.directory.mkdir()
        config = self.root / "mac_test.conf"
        _ = config.write_text("host=config-mac\nclaim_idle_minutes=15\nclaim_poll_seconds=60\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "MAC_TEST_STATE_DIR": str(self.paths.directory),
            "MAC_TEST_CONFIG": str(config), "USER_ZONE": "America/New_York",
            "BUILD_HOLD_DIR": str(self.root / "holders"), "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })))
        _ = self.enterContext(mock.patch.object(mac_test, "process_start_time", return_value="owner-start"))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=AssertionError("Unexpected external command")))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected external command")))

    def write_claim(self, **changes: object) -> dict[str, object]:
        record: dict[str, object] = {
            "pid": os.getpid(), "proc_start": "owner-start", "what": "alpha tests",
            "worktree": str(self.root), "since": "2026-10-13T12:00:00+00:00",
            "remote_host": "claim-mac", "holder_session": "holder-session", **changes,
        }
        _ = self.paths.run.write_text(json.dumps(record))
        return record

    def read_claim(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.paths.run.read_text()))

    def watch_once(self) -> str:
        output = io.StringIO()
        with mock.patch.object(mac_test, "utc_now", return_value=self.now), \
             mock.patch.object(time, "sleep", side_effect=ClaimPassEnded) as sleep, redirect_stdout(output):
            try:
                self.assertEqual(mac_test.command_watch_claim(self.paths, mac_test.read_config()), 0)
            except ClaimPassEnded:
                sleep.assert_called_once_with(60)
        return output.getvalue()

    def test_active_pass_clears_idle_since_with_state_lock_released(self) -> None:
        record = self.write_claim(idle_since="2026-10-13T12:00:00+00:00")

        def check(config: mac_test.MacTestConfig, host: str) -> mac_test.MacActive:
            self.assertEqual(host, "claim-mac")
            self.assertEqual(config.claim_idle_minutes, 15)
            with self.paths.lock.open("a+") as state_lock:
                fcntl.flock(state_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(state_lock.fileno(), fcntl.LOCK_UN)
            return mac_test.MacActive(3)

        with mock.patch.object(mac_test, "check_mac_activity", side_effect=check) as activity, \
             mock.patch.object(mac_test, "send_message") as send:
            _ = self.watch_once()
        activity.assert_called_once()
        send.assert_not_called()
        _ = record.pop("idle_since")
        self.assertEqual(self.read_claim(), record)

    def test_first_idle_pass_records_now_and_process_claim_uses_config_host(self) -> None:
        record = self.write_claim()
        _ = record.pop("remote_host")
        _ = self.paths.run.write_text(json.dumps(record))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as activity, \
             mock.patch.object(mac_test, "send_message") as send:
            _ = self.watch_once()
        self.assertEqual(activity.call_args.args[1], "config-mac")
        send.assert_not_called()
        self.assertEqual(self.read_claim(), {**record, "idle_since": mac_test.timestamp(self.now)})

    def test_dead_process_claim_ends_pass_without_activity_write_or_message(self) -> None:
        record = self.write_claim()
        _ = record.pop("remote_host")
        _ = self.paths.run.write_text(json.dumps(record))
        before = self.paths.run.read_bytes()
        with mock.patch.object(mac_test, "process_start_time", return_value="different-process"), \
             mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as activity, \
             mock.patch.object(mac_test, "write_json") as write, \
             mock.patch.object(mac_test, "send_message") as send:
            self.assertFalse(mac_test.claim_watch_pass(self.paths, mac_test.read_config()))
            _ = self.watch_once()
        activity.assert_not_called()
        write.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.paths.run.read_bytes(), before)

    def test_process_claim_that_dies_during_activity_check_is_left_unchanged(self) -> None:
        record = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(hours=1)))
        _ = record.pop("remote_host")
        _ = self.paths.run.write_text(json.dumps(record))
        before = self.paths.run.read_bytes()
        with mock.patch.object(mac_test, "process_start_time", side_effect=["owner-start", "different-process"]), \
             mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as activity, \
             mock.patch.object(mac_test, "write_json") as write, \
             mock.patch.object(mac_test, "send_message") as send:
            self.assertFalse(mac_test.claim_watch_pass(self.paths, mac_test.read_config()))
        activity.assert_called_once()
        write.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.paths.run.read_bytes(), before)

    def test_idle_pass_before_fifteen_minutes_keeps_claim(self) -> None:
        record = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(minutes=15) + timedelta(seconds=1)))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()), \
             mock.patch.object(mac_test, "send_message") as send:
            _ = self.watch_once()
        self.assertEqual(self.read_claim(), record)
        send.assert_not_called()

    def test_idle_release_sends_holder_one_message_and_starts_pending_block_watcher(self) -> None:
        record = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(minutes=15)))
        _ = self.paths.block.write_text(json.dumps({
            "version": 2, "holder": "alice", "for": "rendering",
            "since": "2026-10-13T12:00:00+00:00", "expires": "2026-10-13T16:00:00+00:00",
            "session": "", "showrunner": "", "state": "pending", "ci": "not_configured",
            "waiting_on": {"kind": "local_run", "what": "alpha tests"},
        }))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as activity, \
             mock.patch.object(mac_test, "send_message", return_value=True) as send, \
             mock.patch.object(mac_test, "start_watcher") as block_watch:
            output = self.watch_once()
            _ = self.watch_once()
        self.assertFalse(self.paths.run.exists())
        activity.assert_called_once()
        block_watch.assert_called_once_with(self.paths)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "session:holder-session")
        self.assertEqual(send.call_args.args[2], f"mac-claim-idle-{record['pid']}-{record['since']}")
        self.assertEqual(send.call_args.args[3], 0)
        text = (
            'Your Mac claim "alpha tests" (since 08:00) was released at 08:30: nothing ran on the Mac for 15 minutes, '
            + "so tests waiting for it can run. Claim it again before your next Mac command: "
            + "python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what 'alpha tests' "
            + f"--worktree '{self.root}' --remote 'claim-mac' (see /build:mac)."
        )
        self.assertEqual(send.call_args.args[4], text)
        self.assertIn("released", output)
        arguments = mac_test.CliArguments()
        arguments.pid = cast(int, record["pid"])
        with mock.patch.object(mac_test, "start_watcher") as release_watch:
            self.assertEqual(mac_test.command_release(arguments, self.paths), 0)
        release_watch.assert_called_once_with(self.paths)

    def test_process_claim_release_message_keeps_process_bound_claim_command(self) -> None:
        record = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(minutes=15)))
        _ = record.pop("remote_host")
        _ = self.paths.run.write_text(json.dumps(record))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()), \
             mock.patch.object(mac_test, "send_message", return_value=True) as send:
            _ = self.watch_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[4], (
            'Your Mac claim "alpha tests" (since 08:00) was released at 08:30: nothing ran on the Mac for 15 minutes, '
            + "so tests waiting for it can run. Claim it again before your next Mac command: "
            + f"python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ --what 'alpha tests' --worktree '{self.root}' "
            + "(see /build:mac)."
        ))

    def test_remote_claim_release_command_quotes_host_and_job(self) -> None:
        _ = self.write_claim(what="alpha's tests", remote_host="host's name", worktree="/work/alpha's tree",
                             idle_since=mac_test.timestamp(self.now - timedelta(minutes=15)))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()), \
             mock.patch.object(mac_test, "send_message", return_value=True) as send:
            _ = self.watch_once()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[4], (
            'Your Mac claim "alpha\'s tests" (since 08:00) was released at 08:30: nothing ran on the Mac for 15 minutes, '
            + "so tests waiting for it can run. Claim it again before your next Mac command: "
            + "python3 ~/.claude/scripts/mac_test/mac_test.py claim --pid $$ "
            + "--what 'alpha'\"'\"'s tests' --worktree '/work/alpha'\"'\"'s tree' --remote 'host'\"'\"'s name' "
            + "(see /build:mac)."
        ))

    def write_repository_claims(self, **hana_changes: object) -> dict[str, dict[str, object]]:
        self.paths.claims.mkdir()
        records: dict[str, dict[str, object]] = {}
        for name in ("hana", "cargo-liner"):
            record: dict[str, object] = {
                "pid": os.getpid(), "proc_start": "owner-start", "what": f"{name} tests",
                "worktree": f"/work/{name}", "since": "2026-10-13T12:00:00+00:00",
                "remote_host": "claim-mac", "holder_session": "holder-session",
            }
            if name == "hana":
                record.update(hana_changes)
            _ = (self.paths.claims / f"{name}.json").write_text(json.dumps(record))
            records[name] = record
        return records

    def read_repository_claim(self, name: str) -> dict[str, object]:
        return cast(dict[str, object], json.loads((self.paths.claims / f"{name}.json").read_text()))

    def test_one_activity_check_per_host_serves_every_repository_claim(self) -> None:
        records = self.write_repository_claims()
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as activity, \
             mock.patch.object(mac_test, "send_message") as send:
            _ = self.watch_once()
        activity.assert_called_once()
        self.assertEqual(activity.call_args.args[1], "claim-mac")
        send.assert_not_called()
        for name, record in records.items():
            with self.subTest(name=name):
                self.assertEqual(self.read_repository_claim(name), {**record, "idle_since": mac_test.timestamp(self.now)})

    def test_idle_release_removes_only_that_repository_claim(self) -> None:
        records = self.write_repository_claims(idle_since=mac_test.timestamp(self.now - timedelta(minutes=15)))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()) as activity, \
             mock.patch.object(mac_test, "send_message", return_value=True) as send:
            _ = self.watch_once()
        activity.assert_called_once()
        self.assertFalse((self.paths.claims / "hana.json").exists())
        self.assertEqual(self.read_repository_claim("cargo-liner"),
                         {**records["cargo-liner"], "idle_since": mac_test.timestamp(self.now)})
        send.assert_called_once()
        self.assertTrue(cast(str, send.call_args.args[4]).endswith(
            "claim --pid $$ --what 'hana tests' --worktree '/work/hana' --remote 'claim-mac' (see /build:mac)."))

    def test_idle_release_without_holder_session_prints_and_sends_nothing(self) -> None:
        record = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(minutes=20)))
        _ = record.pop("holder_session")
        _ = self.paths.run.write_text(json.dumps(record))
        with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.MacIdle()), \
             mock.patch.object(mac_test, "send_message") as send:
            output = self.watch_once()
        self.assertFalse(self.paths.run.exists())
        send.assert_not_called()
        self.assertIn("released", output)

    def test_failed_activity_check_keeps_claim_and_idle_time(self) -> None:
        for idle_recorded in (False, True):
            with self.subTest(idle_recorded=idle_recorded):
                record = self.write_claim()
                if idle_recorded:
                    record["idle_since"] = mac_test.timestamp(self.now - timedelta(hours=1))
                    _ = self.paths.run.write_text(json.dumps(record))
                with mock.patch.object(mac_test, "check_mac_activity", return_value=mac_test.ActivityCheckFailed("ssh timed out")), \
                     mock.patch.object(mac_test, "send_message") as send:
                    _ = self.watch_once()
                self.assertEqual(self.read_claim(), record)
                send.assert_not_called()

    def test_pass_does_not_modify_claim_changed_during_activity_check(self) -> None:
        for field, value in (("pid", 1234), ("proc_start", "new-start"), ("since", "2026-10-13T12:01:00+00:00")):
            with self.subTest(field=field):
                original = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(hours=1)))
                replacement = {**original, field: value}

                def check(config: mac_test.MacTestConfig, host: str) -> mac_test.MacIdle:
                    _ = config, host
                    _ = self.paths.run.write_text(json.dumps(replacement))
                    return mac_test.MacIdle()

                with mock.patch.object(mac_test, "check_mac_activity", side_effect=check), \
                     mock.patch.object(mac_test, "send_message") as send:
                    _ = self.watch_once()
                self.assertEqual(self.read_claim(), replacement)
                send.assert_not_called()

    def test_pass_does_not_restore_claim_removed_during_activity_check(self) -> None:
        _ = self.write_claim(idle_since=mac_test.timestamp(self.now - timedelta(hours=1)))

        def check(config: mac_test.MacTestConfig, host: str) -> mac_test.MacIdle:
            _ = config, host
            self.paths.run.unlink()
            return mac_test.MacIdle()

        with mock.patch.object(mac_test, "check_mac_activity", side_effect=check), \
             mock.patch.object(mac_test, "send_message") as send:
            _ = self.watch_once()
        self.assertFalse(self.paths.run.exists())
        send.assert_not_called()

    def test_watch_without_claim_or_with_another_watcher_checks_no_host(self) -> None:
        with mock.patch.object(mac_test, "check_mac_activity") as activity:
            _ = self.watch_once()
            _ = self.write_claim()
            with (self.paths.directory / "claim_watch.lock").open("a+") as claim_lock:
                fcntl.flock(claim_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                _ = self.watch_once()
                fcntl.flock(claim_lock.fileno(), fcntl.LOCK_UN)
        activity.assert_not_called()

    def test_start_claim_watcher_skips_unit_when_claim_watch_lock_is_held(self) -> None:
        with (self.paths.directory / "claim_watch.lock").open("a+") as claim_lock:
            fcntl.flock(claim_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch.object(subprocess, "run") as run:
                mac_test.start_claim_watcher(self.paths)
            run.assert_not_called()
            fcntl.flock(claim_lock.fileno(), fcntl.LOCK_UN)

    def test_activity_check_requires_readable_success_and_uses_one_bounded_ssh(self) -> None:
        config = mac_test.read_config()
        for answer, expected in (("active=0\nrc=0\n", mac_test.MacIdle()), ("active=3\nrc=0\n", mac_test.MacActive(3))):
            with self.subTest(answer=answer), mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, answer, ""),
            ) as run:
                self.assertEqual(mac_test.check_mac_activity(config, "claim-host"), expected)
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0][0], "ssh")
                self.assertEqual(run.call_args.args[0][-2], "claim-host")
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
        for answer in ("active=0\n", "rc=0\n", "active=-1\nrc=0\n", "active=x\nrc=0\n", "active=0\nrc=1\n"):
            with self.subTest(answer=answer), mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess([], 0, answer, ""),
            ):
                self.assertIsInstance(mac_test.check_mac_activity(config, "claim-host"), mac_test.ActivityCheckFailed)
        for error in (OSError("ssh cannot start"), subprocess.TimeoutExpired("ssh", 10)):
            with self.subTest(error=error), mock.patch.object(subprocess, "run", side_effect=error):
                self.assertIsInstance(mac_test.check_mac_activity(config, "claim-host"), mac_test.ActivityCheckFailed)
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "active=0\nrc=0\n", "unreachable")):
            self.assertIsInstance(mac_test.check_mac_activity(config, "claim-host"), mac_test.ActivityCheckFailed)

    def test_claim_idle_settings_have_defaults_and_accept_config_values(self) -> None:
        path = self.root / "mac_test.conf"
        _ = path.write_text("")
        config = mac_test.read_config()
        self.assertEqual((config.claim_idle_minutes, config.claim_poll_seconds), (15, 60))
        _ = path.write_text("host=other-mac\nclaim_idle_minutes=0.5\nclaim_poll_seconds=2\n")
        config = mac_test.read_config()
        self.assertEqual((config.claim_idle_minutes, config.claim_poll_seconds), (0.5, 2))


if __name__ == "__main__":
    _ = unittest.main()
