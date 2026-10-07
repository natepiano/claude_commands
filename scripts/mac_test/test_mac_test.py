#!/usr/bin/env python3
"""Exercise the Mac test state command through its process interface."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import NotRequired, TypedDict, cast, final, override
from zoneinfo import ZoneInfo

import mac_test


SCRIPT = Path(__file__).with_name("mac_test.py")
LOCAL_ZONE = ZoneInfo("America/New_York")
CI_JOB = "macOS: Compile and Test"
CI_GATE_JOB = "macOS: Runner Availability"
SKIPPED_REPORT_RUNS = 200
# An unblock that reports this many runs starts the stand-in gh once for the
# switch, once for the list and once for each run, one after another. A start
# costs about 20 ms of processor time, so the whole report needs about 4 s on
# an idle machine; 0.3 s for each start leaves room for cores shared with builds.
SKIPPED_REPORT_TIMEOUT_S = (SKIPPED_REPORT_RUNS + 2) * 0.3


class RunState(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str


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
    deadline = time.monotonic() + 20
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
            "MAC_TEST_WATCH_INTERVAL_S": "0.05",
            "TZ": "America/New_York",
        }
        self.children = []

    @override
    def tearDown(self) -> None:
        for child in reversed(self.children):
            self.stop_child(child)

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text("#!/usr/bin/env python3\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def write_config(self, **changes: str | None) -> None:
        values: dict[str, str] = {
            "ci_repo": "natepiano/hana",
            "ci_variable": "MACOS_CI",
            "ci_workflow": "ci.yml",
            "ci_job": CI_JOB,
            "ci_gate_job": CI_GATE_JOB,
            "gh_timeout_s": "2",
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
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            env=self.environment if environment is None else environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def start_command(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            env=self.environment if environment is None else environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
                    + "time.sleep(30)"
                ),
            ],
            cwd=self.root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.children.append(child)
        deadline = time.monotonic() + 2
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
                _ = child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                _ = child.wait(timeout=3)
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

    def wait_for_path(self, path: Path, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def wait_for_log_count(self, name: str, count: int, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.logged_arguments(name)) >= count:
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {count} {name} calls")

    def wait_for_free_message(
        self, expected: FreeMessageState, timeout: float = 3
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
        on = self.command("status")
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

    def test_in_progress_ci_gate_job_keeps_block_pending(self) -> None:
        self.gh["queued"] = [{"databaseId": 40}]
        self.gh["jobs"] = {
            "40": [
                {"name": CI_GATE_JOB, "status": "in_progress", "conclusion": ""}
            ]
        }
        self.sync_gh()
        result = self.command("block", "--holder", "alice", "--for", "rendering")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CI's Mac job is running", result.stdout)
        self.assertEqual(self.block_state()["waiting_on"], {"kind": "ci_job"})

    def test_completed_ci_gate_and_mac_jobs_do_not_keep_block_pending(self) -> None:
        completed_job_sets: list[list[GhJob]] = [
            [
                {"name": CI_GATE_JOB, "status": "completed", "conclusion": "success"},
                {"name": CI_JOB, "status": "completed", "conclusion": "skipped"},
            ],
            [
                {"name": CI_GATE_JOB, "status": "completed", "conclusion": "success"}
            ],
        ]
        for jobs in completed_job_sets:
            with self.subTest(jobs=jobs):
                (self.state_directory / "block.json").unlink(missing_ok=True)
                self.gh["variable"] = "true"
                self.gh["queued"] = [{"databaseId": 42}]
                self.gh["jobs"] = {"42": jobs}
                self.sync_gh()
                result = self.command(
                    "block", "--holder", "alice", "--for", "rendering"
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                self.assertEqual(self.block_state()["state"], "active")

    def test_empty_ci_gate_job_only_counts_the_mac_job(self) -> None:
        self.write_config(ci_gate_job="")
        self.gh["queued"] = [{"databaseId": 43}]
        self.gh["jobs"] = {
            "43": [
                {"name": CI_GATE_JOB, "status": "in_progress", "conclusion": ""}
            ]
        }
        self.sync_gh()
        gate_only = self.command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        self.assertEqual(
            gate_only.returncode, 0, gate_only.stdout + gate_only.stderr
        )
        self.assertEqual(self.block_state()["state"], "active")

        (self.state_directory / "block.json").unlink()
        self.gh["variable"] = "true"
        self.gh["jobs"] = {
            "43": [{"name": CI_JOB, "status": "in_progress", "conclusion": ""}]
        }
        self.sync_gh()
        mac_job = self.command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        self.assertEqual(mac_job.returncode, 0, mac_job.stdout + mac_job.stderr)
        self.assertEqual(self.block_state()["waiting_on"], {"kind": "ci_job"})

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
            "Block pending for alice: CI's Mac job is running. Nothing new starts "
            + "there, and you get a message when it ends.\n"
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
        self.assertIn("CI's Mac job ended", deliveries[0][-1])

        unblocked = self.command("unblock", "--holder", "alice")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)
        stdout, stderr = watcher.communicate(timeout=3)
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
        deliveries = self.logged_arguments("send-delivered")
        self.assertEqual([call[1] for call in deliveries], ["alice", "showrunner-one"])
        for delivery in deliveries:
            self.assertIn("past its time limit but still in place", delivery[-1])
            self.assertIn("--repeat-minutes", delivery)
            self.assertEqual(delivery[delivery.index("--repeat-minutes") + 1], "60")
        self.assertEqual(len(self.logged_arguments("systemd-run")), 1)

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
        time.sleep(0.02)
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
        capped = self.command(
            "unblock", "--holder", "alice", timeout=SKIPPED_REPORT_TIMEOUT_S
        )
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
        self.write_config(gh_timeout_s="0.2")
        self.gh["delays"] = [
            {
                "match": "variable set",
                "seconds": 30.0,
                "marker": "set-applied",
                "after": True,
            }
        ]
        self.sync_gh()
        blocked = self.start_command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        self.wait_for_path(self.stub_directory / "set-applied")
        stdout, stderr = blocked.communicate(timeout=5)
        self.assertEqual(blocked.returncode, 2, stdout + stderr)
        self.assertIn("CI can still use the Mac:", stdout)
        self.assertEqual(self.block_state()["ci"], "off_unconfirmed")
        self.assertEqual(self.read_fixture_variable(), "false")

        self.gh["variable"] = "false"
        self.gh["delays"] = []
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
        self.gh["delays"] = [
            {
                "match": "variable get",
                "seconds": 30.0,
                "marker": "gh-timeout-started",
            }
        ]
        self.sync_gh()
        process = self.start_command(
            "block", "--holder", "alice", "--for", "rendering"
        )
        self.wait_for_path(self.stub_directory / "gh-timeout-started")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("gh timed out after 0.2 s", stdout)
        self.assertEqual(self.block_state()["ci"], "still_on")

    def test_claim_does_not_wait_for_slow_gh_in_block(self) -> None:
        child = self.start_child()
        self.gh["delays"] = [
            {
                "match": "variable get",
                "seconds": 20.0,
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
        stdout, stderr = blocker.communicate(timeout=3)
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
            {"match": "run list", "seconds": 0.5, "marker": "watch-gh-started"}
        ]
        self.sync_gh()
        watcher = self.start_command("watch")
        self.wait_for_path(self.stub_directory / "watch-gh-started")
        environment = {**self.environment, "CLAUDE_CODE_SESSION_ID": "new-session"}
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
        stdout, stderr = renewal.communicate(timeout=4)
        self.assertEqual(renewal.returncode, 0, stdout + stderr)
        block = self.block_state()
        self.assertEqual(block["for"], "renewed work")
        self.assertEqual(block["session"], "new-session")
        self.assertEqual(block["since"], original["since"])
        watcher.terminate()
        _ = watcher.wait(timeout=3)

    def test_unblock_and_expiry_restore_switch_once(self) -> None:
        now = datetime.now(timezone.utc)
        _ = self.write_block(
            since=now - timedelta(hours=2),
            expires=now - timedelta(seconds=1),
            free_message={"kind": "not_needed"},
        )
        self.gh["variable"] = "false"
        self.gh["delays"] = [
            {"match": "variable set", "seconds": 0.4, "marker": "restore-started"}
        ]
        self.sync_gh()
        expiry = self.start_command("status")
        self.wait_for_path(self.stub_directory / "restore-started")
        unblock = self.start_command("unblock", "--holder", "alice")
        expiry_output = expiry.communicate(timeout=4)
        unblock_output = unblock.communicate(timeout=4)
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
                "seconds": 20.0,
                "marker": "watch-gh-started",
                "release": "continue-watch-gh",
            }
        ]
        self.sync_gh()
        first = self.start_command("watch")
        self.wait_for_path(self.stub_directory / "watch-gh-started")
        second = self.command("watch", timeout=3)
        self.assert_command(second, 0, "")
        self.assertIsNone(first.poll())
        _ = (self.stub_directory / "continue-watch-gh").write_text(
            "continue\n", encoding="utf-8"
        )
        unblocked = self.command("unblock", "--holder", "alice")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)
        stdout, stderr = first.communicate(timeout=3)
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
        status = self.command("status", environment=environment, timeout=3)
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
            "block pending for alice (CI's Mac job is running), "
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
        since = "2026-10-07T12:00:00+00:00"
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
        stdout, stderr = status.communicate(timeout=3)
        self.assertEqual(status.returncode, 0, stdout + stderr)


if __name__ == "__main__":
    _ = unittest.main()
