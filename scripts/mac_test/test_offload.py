#!/usr/bin/env python3
"""Exercise Mac nextest offload through disposable command stand-ins."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast, final, override


SCRIPT = Path(__file__).with_name("offload.py")
STATE_SCRIPT = Path(__file__).with_name("mac_test.py")
FILTER = "package(hana) & (test(a) | test(b))"


class RemoteLine(TypedDict):
    stream: Literal["stdout", "stderr"]
    text: str


class CommandFixture(TypedDict):
    probe_status: int
    probe_delay_s: float
    probe_lines: list[str]
    rsync_status: int
    run_lines: list[RemoteLine]
    run_status_reported: bool
    run_status_line: str
    run_delay_s: float
    hold_after_first_line: bool
    cleanup_status: int
    hold_cleanup: bool


class CommandEvent(TypedDict):
    command: str
    args: list[str]
    claimed: bool
    exclude: NotRequired[str]


class WireResult(TypedDict):
    mac: str
    reason: str
    seconds: str


COMMAND_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["MAC_TEST_STUB_DIR"])
fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
event = {
    "command": command,
    "args": arguments,
    "claimed": (Path(os.environ["MAC_TEST_STATE_DIR"]) / "run.json").exists(),
}
if command == "rsync":
    exclude_argument = next(
        argument for argument in arguments if argument.startswith("--exclude-from=")
    )
    event["exclude"] = Path(exclude_argument.split("=", 1)[1]).read_text(
        encoding="utf-8"
    )
with (root / "events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")

if command == "rsync":
    raise SystemExit(int(fixture["rsync_status"]))
if command != "ssh":
    (root / (command + "-called")).write_text("called\n", encoding="utf-8")
    raise SystemExit(99)

remote = arguments[-1]
if "pkill -f" in remote:
    (root / "cleanup-called").write_text("called\n", encoding="utf-8")
    if fixture["hold_cleanup"]:
        (root / "cleanup-started").write_text("started\n", encoding="utf-8")
        while not (root / "continue-cleanup").exists():
            time.sleep(0.01)
    raise SystemExit(int(fixture["cleanup_status"]))
if "lint nextest" in remote:
    (root / "run-started").write_text("started\n", encoding="utf-8")
    time.sleep(float(fixture["run_delay_s"]))
    for index, line in enumerate(fixture["run_lines"]):
        destination = sys.stdout if line["stream"] == "stdout" else sys.stderr
        print(line["text"], file=destination, flush=True)
        if index == 0 and fixture["hold_after_first_line"]:
            (root / "first-line-sent").write_text("sent\n", encoding="utf-8")
            while not (root / "continue-run").exists():
                time.sleep(0.01)
        time.sleep(0.02)
    if fixture["run_status_reported"]:
        print(fixture["run_status_line"], flush=True)
    raise SystemExit(0)

(root / "probe-started").write_text("started\n", encoding="utf-8")
time.sleep(float(fixture["probe_delay_s"]))
for line in fixture["probe_lines"]:
    print(line, flush=True)
raise SystemExit(int(fixture["probe_status"]))
'''


CLAIM_STUB = r'''
from __future__ import annotations

import os
from pathlib import Path
import sys
import time

root = Path(os.environ["MAC_TEST_STUB_DIR"])
state = Path(os.environ["MAC_TEST_STATE_DIR"])
action = sys.argv[1]
with (root / "state-events").open("a", encoding="utf-8") as output:
    output.write(action + "\n")
if action == "claim":
    state.mkdir(parents=True, exist_ok=True)
    (state / "run.json").write_text("claimed\n", encoding="utf-8")
    (root / "claim-started").write_text("started\n", encoding="utf-8")
    while not (root / "finish-claim").exists():
        time.sleep(0.01)
    raise SystemExit(0)
if action == "release":
    (state / "run.json").unlink(missing_ok=True)
    raise SystemExit(0)
raise SystemExit(12)
'''


@final
class OffloadCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.repository = Path()
        self.unknown_directory = Path()
        self.state_directory = Path()
        self.stub_directory = Path()
        self.bin_directory = Path()
        self.config_path = Path()
        self.result_path = Path()
        self.environment: dict[str, str] = {}
        self.config: dict[str, str] = {}
        self.fixture: CommandFixture = {
            "probe_status": 0,
            "probe_delay_s": 0.0,
            "probe_lines": [],
            "rsync_status": 0,
            "run_lines": [],
            "run_status_reported": True,
            "run_status_line": "mac_exit=0",
            "run_delay_s": 0.0,
            "hold_after_first_line": False,
            "cleanup_status": 0,
            "hold_cleanup": False,
        }

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repository = self.root / "hana"
        self.unknown_directory = self.root / "not-a-repository"
        self.state_directory = self.root / "state"
        self.stub_directory = self.root / "stub-state"
        self.bin_directory = self.root / "bin"
        self.config_path = self.root / "mac_test.conf"
        self.result_path = self.root / "result"
        self.repository.mkdir()
        self.unknown_directory.mkdir()
        self.stub_directory.mkdir()
        self.bin_directory.mkdir()
        initialized = subprocess.run(
            ["git", "init", "-q", str(self.repository)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            initialized.returncode, 0, initialized.stdout + initialized.stderr
        )
        stub = self.bin_directory / "command-stub"
        self.write_executable(stub, COMMAND_STUB)
        for name in ("ssh", "rsync", "gh", "systemd-run"):
            (self.bin_directory / name).symlink_to(stub)
        self.config = {
            "offload": "on",
            "host": "mac",
            "connect_timeout_s": "6",
            "probe_timeout_s": "1",
            "unreachable_backoff_s": "300",
            "free_floor_gib": "60",
            "max_load": "6",
            "mac_budget_gib": "24",
            "mac_budget_gib.hana": "64",
            "linux_only.hana": "",
            "mac_skip.hana": "",
        }
        self.fixture = {
            "probe_status": 0,
            "probe_delay_s": 0.0,
            "probe_lines": [
                "procs=0",
                "load=1.25",
                "power=ac",
                "free_gib=120",
                "host=Mac",
                "rc=0",
            ],
            "rsync_status": 0,
            "run_lines": [],
            "run_status_reported": True,
            "run_status_line": "mac_exit=0",
            "run_delay_s": 0.0,
            "hold_after_first_line": False,
            "cleanup_status": 0,
            "hold_cleanup": False,
        }
        self.sync_config()
        self.sync_fixture()
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_CONFIG": str(self.config_path),
            "MAC_TEST_STUB_DIR": str(self.stub_directory),
        }

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text(
            f"#!{sys.executable}\n" + source, encoding="utf-8"
        )
        path.chmod(0o755)

    def sync_config(self) -> None:
        _ = self.config_path.write_text(
            "".join(f"{name}={value}\n" for name, value in self.config.items()),
            encoding="utf-8",
        )

    def sync_fixture(self) -> None:
        _ = (self.stub_directory / "fixture.json").write_text(
            json.dumps(self.fixture), encoding="utf-8"
        )

    def command(
        self,
        *nextest_arguments: str,
        repository: Path | None = None,
        filter_run: bool = False,
        timeout: float = 10,
    ) -> subprocess.CompletedProcess[str]:
        self.result_path.unlink(missing_ok=True)
        arguments = [
            sys.executable,
            str(SCRIPT),
            "run",
            "--repo-root",
            str(self.repository if repository is None else repository),
            "--package",
            "hana",
            "--call-id",
            "call-17",
        ]
        if filter_run:
            arguments.append("--filter-run")
        arguments.extend(["--result", str(self.result_path), "--"])
        arguments.extend(nextest_arguments)
        return subprocess.run(
            arguments,
            cwd=self.root,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )

    def start_command(
        self,
        *nextest_arguments: str,
        script: Path = SCRIPT,
        extra: dict[str, str] | None = None,
        ignore_interrupt: bool = False,
    ) -> subprocess.Popen[str]:
        self.result_path.unlink(missing_ok=True)
        arguments = [
            sys.executable,
            str(script),
            "run",
            "--repo-root",
            str(self.repository),
            "--package",
            "hana",
            "--call-id",
            "call-signal",
            "--result",
            str(self.result_path),
            "--",
            *nextest_arguments,
        ]
        if ignore_interrupt:
            arguments = [
                "/bin/sh",
                "-c",
                'trap "" INT; exec "$@"',
                "offload-with-ignored-interrupt",
                *arguments,
            ]
        return subprocess.Popen(
            arguments,
            cwd=self.root,
            env={**self.environment, **(extra or {})},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def claim_stub_runner(self) -> Path:
        scripts_directory = self.root / "claim-runner" / "scripts"
        runner_directory = scripts_directory / "mac_test"
        runner_directory.mkdir(parents=True)
        runner = runner_directory / "offload.py"
        _ = shutil.copyfile(SCRIPT, runner)
        self.write_executable(runner_directory / "mac_test.py", CLAIM_STUB)
        (scripts_directory / "lint").symlink_to(
            SCRIPT.parent.parent / "lint", target_is_directory=True
        )
        return runner

    def state_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(STATE_SCRIPT), *arguments],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    def events(self) -> list[CommandEvent]:
        path = self.stub_directory / "events.jsonl"
        if not path.exists():
            return []
        return [
            cast(CommandEvent, json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def events_for(self, command: str) -> list[CommandEvent]:
        return [event for event in self.events() if event["command"] == command]

    def clear_events(self) -> None:
        (self.stub_directory / "events.jsonl").unlink(missing_ok=True)

    def wire_result(self) -> WireResult:
        lines = self.result_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        values = dict(line.split("=", 1) for line in lines)
        self.assertEqual(set(values), {"mac", "reason", "seconds"})
        _ = float(values["seconds"])
        return {
            "mac": values["mac"],
            "reason": values["reason"],
            "seconds": values["seconds"],
        }

    def assert_wire(self, mac: str, reason: str) -> None:
        values = self.wire_result()
        self.assertEqual(values["mac"], mac)
        self.assertEqual(values["reason"], reason)
        self.assertGreaterEqual(float(values["seconds"]), 0.0)

    def assert_declined(
        self, result: subprocess.CompletedProcess[str], reason: str
    ) -> None:
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertEqual(result.stdout, f"mac_test: staying on natedev ({reason})\n")
        self.assert_wire("declined", reason)
        self.assertFalse((self.state_directory / "run.json").exists())

    def assert_claimed_commands(self) -> None:
        for event in self.events():
            if event["command"] in {"ssh", "rsync"}:
                self.assertTrue(event["claimed"], event)

    def wait_for_path(self, path: Path) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {path}")

    def read_until_output(
        self, process: subprocess.Popen[str], expected: str
    ) -> str:
        output = process.stdout
        if output is None:
            self.fail("runner output is unavailable")
        received = b""
        expected_bytes = expected.encode()
        deadline = time.monotonic() + 5
        while expected_bytes not in received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"timed out waiting for output {expected!r}")
            readable, _, _ = select.select([output], [], [], remaining)
            if not readable:
                self.fail(f"timed out waiting for output {expected!r}")
            chunk = os.read(output.fileno(), 4096)
            if not chunk:
                self.fail(f"runner ended before output {expected!r}")
            received += chunk
        return received.decode(errors="replace")

    def test_configuration_declines_before_claiming(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.assert_declined(self.command(), "off")
        self.assertEqual(self.events(), [])

        self.config["offload"] = "on"
        _ = self.config.pop("linux_only.hana")
        self.sync_config()
        self.assert_declined(self.command(), "repo")
        self.assertEqual(self.events(), [])

        self.config["linux_only.hana"] = "hana,platform_only"
        self.sync_config()
        self.assert_declined(self.command(), "linux_only")
        self.assertEqual(self.events(), [])

    def test_directory_outside_git_declines_as_unknown_repository(self) -> None:
        result = self.command(repository=self.unknown_directory)
        self.assert_declined(result, "repo")
        self.assertEqual(self.events(), [])

    def test_unreachable_status_sets_backoff_without_a_second_ssh(self) -> None:
        self.fixture["probe_status"] = 255
        self.sync_fixture()
        self.assert_declined(self.command(), "unreachable")
        self.assertEqual(len(self.events_for("ssh")), 1)

        self.clear_events()
        self.assert_declined(self.command(), "backoff")
        self.assertEqual(self.events(), [])

    def test_invalid_backoff_file_does_not_skip_probe(self) -> None:
        self.state_directory.mkdir()
        _ = (self.state_directory / "unreachable_until").write_text(
            "not a number\n", encoding="utf-8"
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.stub_directory / "probe-started").exists())
        self.assertEqual(len(self.events_for("ssh")), 2)

    def test_probe_timeout_declines_as_unreachable(self) -> None:
        self.config["probe_timeout_s"] = "0.1"
        self.fixture["probe_delay_s"] = 3.0
        self.sync_config()
        self.sync_fixture()
        self.assert_declined(self.command(timeout=5), "unreachable")
        self.assertTrue((self.stub_directory / "probe-started").exists())
        self.assertEqual(len(self.events_for("ssh")), 1)

    def test_probe_without_final_status_declines_as_unreachable(self) -> None:
        self.fixture["probe_lines"] = self.fixture["probe_lines"][:-1]
        self.sync_fixture()
        self.assert_declined(self.command(), "unreachable")
        self.assertEqual(len(self.events_for("ssh")), 1)

    def test_processes_and_load_each_decline_as_mac_busy(self) -> None:
        for changed_line in ("procs=1", "load=6.01"):
            with self.subTest(line=changed_line):
                self.fixture["probe_lines"] = [
                    changed_line if line.startswith(changed_line.split("=", 1)[0]) else line
                    for line in [
                        "procs=0",
                        "load=1.25",
                        "power=ac",
                        "free_gib=120",
                        "host=Mac",
                        "rc=0",
                    ]
                ]
                self.sync_fixture()
                self.assert_declined(self.command(), "mac_busy")
                self.assertEqual(len(self.events_for("ssh")), 1)
                self.assertEqual(self.events_for("rsync"), [])
                self.clear_events()

    def test_battery_and_low_disk_decline_after_probe(self) -> None:
        for original, replacement, reason in (
            ("power=ac", "power=battery", "battery"),
            ("free_gib=120", "free_gib=59", "disk"),
        ):
            with self.subTest(reason=reason):
                self.fixture["probe_lines"] = [
                    replacement if line == original else line
                    for line in [
                        "procs=0",
                        "load=1.25",
                        "power=ac",
                        "free_gib=120",
                        "host=Mac",
                        "rc=0",
                    ]
                ]
                self.sync_fixture()
                self.assert_declined(self.command(), reason)
                self.assertEqual(self.events_for("rsync"), [])
                self.clear_events()

    def test_claim_exit_reasons_never_reach_external_commands(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        self.state_directory.mkdir()
        block = {
            "version": 2,
            "holder": "alice",
            "for": "demo",
            "since": now.isoformat(),
            "expires": (now + timedelta(hours=1)).isoformat(),
            "session": None,
            "showrunner": None,
            "state": "active",
            "ci": "not_configured",
            "free_message": {"kind": "not_needed"},
        }
        _ = (self.state_directory / "block.json").write_text(
            json.dumps(block), encoding="utf-8"
        )
        self.assert_declined(self.command(), "blocked")
        self.assertEqual(self.events(), [])

        (self.state_directory / "block.json").unlink()
        claimed = self.state_command(
            "claim",
            "--pid",
            str(os.getpid()),
            "--what",
            "other tests",
            "--worktree",
            str(self.repository),
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        busy = self.command()
        self.assertEqual(busy.returncode, 75, busy.stdout)
        self.assertEqual(busy.stdout, "mac_test: staying on natedev (busy)\n")
        self.assert_wire("declined", "busy")
        self.assertTrue((self.state_directory / "run.json").exists())
        self.assertEqual(self.events(), [])
        released = self.state_command("release", "--pid", str(os.getpid()))
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

        _ = (self.state_directory / "block.json").write_text(
            "damaged\n", encoding="utf-8"
        )
        self.assert_declined(self.command(), "state")
        self.assertEqual(self.events(), [])

    def test_signal_during_claim_releases_a_possible_claim(self) -> None:
        process = self.start_command(script=self.claim_stub_runner())
        try:
            self.wait_for_path(self.stub_directory / "claim-started")
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=15)
        except BaseException:
            process.kill()
            _ = process.communicate(timeout=5)
            raise
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
        state_events = (self.stub_directory / "state-events").read_text(
            encoding="utf-8"
        )
        self.assertEqual(state_events.splitlines(), ["claim", "release"])
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_copy_failure_releases_claim(self) -> None:
        self.fixture["rsync_status"] = 23
        self.sync_fixture()
        self.assert_declined(self.command(), "copy")
        self.assertEqual(len(self.events_for("ssh")), 1)
        self.assertEqual(len(self.events_for("rsync")), 1)
        self.assert_claimed_commands()

    def test_probe_copy_and_run_arguments_preserve_nextest_words(self) -> None:
        _ = (self.repository / ".gitignore").write_text(
            "generated/\n", encoding="utf-8"
        )
        generated = self.repository / "generated"
        generated.mkdir()
        _ = (generated / "artifact").write_text("ignored\n", encoding="utf-8")
        words = ("-E", FILTER, "--no-fail-fast", "unit_case")
        result = self.command(*words, filter_run=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse((self.state_directory / "run.json").exists())
        self.assert_claimed_commands()

        events = self.events()
        self.assertEqual([event["command"] for event in events], ["ssh", "rsync", "ssh"])
        ssh_arguments = [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "mac",
        ]
        probe = events[0]["args"]
        self.assertEqual(probe[: len(ssh_arguments)], ssh_arguments)
        probe_script = probe[len(ssh_arguments)]
        for fragment in (
            ".local/state/mac-test/mirror/hana",
            "pgrep -x",
            "cargo",
            "rustc",
            "cargo-nextest",
            "sysctl -n vm.loadavg",
            "pmset -g batt",
            'df -g "$HOME"',
            "hostname -s",
        ):
            self.assertIn(fragment, probe_script)

        copy = events[1]
        exclude_argument = next(
            argument
            for argument in copy["args"]
            if argument.startswith("--exclude-from=")
        )
        self.assertEqual(
            copy["args"],
            [
                "-rlpc",
                "--delete",
                exclude_argument,
                "-e",
                "ssh -o BatchMode=yes -o ConnectTimeout=6 "
                + "-o ServerAliveInterval=15 -o ServerAliveCountMax=3",
                f"{self.repository}/",
                "mac:.local/state/mac-test/mirror/hana/",
            ],
        )
        self.assertEqual(copy.get("exclude"), "/.git\n/target/\n/generated/\n")

        run = events[2]["args"]
        self.assertEqual(run[: len(ssh_arguments)], ssh_arguments)
        remote = run[-1]
        run_command, separator, status_command = remote.rpartition("; echo mac_exit=$?")
        self.assertEqual(separator, "; echo mac_exit=$?")
        self.assertEqual(status_command, "")
        remote_words = shlex.split(run_command)
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertEqual(
            remote_words[lint_index : lint_index + 2 + len(words)],
            ["~/.claude/scripts/lint/lint", "nextest", *words],
        )
        self.assertIn("PATH=$HOME/.cargo/bin:$PATH", remote_words)
        self.assertIn("BUILDLOG_CALLER=verify-mac", remote_words)
        self.assertIn("BUILDLOG_CALL_ID=call-17", remote_words)
        self.assertIn("BUILDLOG_SYNC=1", remote_words)
        self.assertIn("LINT_SWEEP_BUDGET_GIB=64", remote_words)
        self.assertIn(shlex.quote(FILTER), remote)

    def test_non_ascii_ignored_directory_is_in_the_exclude_file(self) -> None:
        _ = (self.repository / ".gitignore").write_text(
            "café/\n", encoding="utf-8"
        )
        ignored_directory = self.repository / "café"
        ignored_directory.mkdir()
        _ = (ignored_directory / "artifact").write_text(
            "ignored\n", encoding="utf-8"
        )
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        copy = self.events_for("rsync")[0]
        self.assertIn("/café/\n", copy.get("exclude", ""))

    def test_repository_skip_is_added_to_filter(self) -> None:
        self.config["mac_skip.hana"] = "test(slow) | test(gpu)"
        self.sync_config()
        result = self.command("-E", FILTER, filter_run=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        remote = self.events_for("ssh")[-1]["args"][-1]
        run_command, separator, status_command = remote.rpartition("; echo mac_exit=$?")
        self.assertEqual(separator, "; echo mac_exit=$?")
        self.assertEqual(status_command, "")
        remote_words = shlex.split(run_command)
        self.assertIn(f"({FILTER}) & not (test(slow) | test(gpu))", remote_words)
        self.assertNotIn(FILTER, remote_words)

    def test_stream_order_and_pass_status(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "first marker"},
            {"stream": "stderr", "text": "second marker"},
            {"stream": "stdout", "text": "third marker"},
        ]
        self.sync_fixture()
        result = self.command("--workspace")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(
            result.stdout.startswith(
                "mac_test: running on the Mac (macOS, host Mac); tree copied in "
            ),
            result.stdout,
        )
        positions = [
            result.stdout.index(marker)
            for marker in ("first marker", "second marker", "third marker")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("mac_exit=", result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_first_remote_line_is_printed_before_the_next_line(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "streamed now"}
        ]
        self.fixture["hold_after_first_line"] = True
        self.sync_fixture()
        process = self.start_command()
        try:
            output_before_gate = self.read_until_output(
                process, "streamed now\n"
            )
            _ = (self.stub_directory / "continue-run").write_text(
                "continue\n", encoding="utf-8"
            )
            remaining_output, _ = process.communicate(timeout=15)
        except BaseException:
            process.send_signal(signal.SIGTERM)
            _ = process.communicate(timeout=15)
            raise
        output = output_before_gate + remaining_output
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("streamed now\n", output_before_gate)
        self.assert_wire("passed", "")

    def test_failed_run_returns_remote_status_and_notice(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stderr", "text": "nextest failed"}
        ]
        self.fixture["run_status_line"] = "mac_exit=42"
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 42, result.stdout)
        self.assertIn("nextest failed\n", result.stdout)
        self.assertIn("this ran on the Mac (macOS)", result.stdout)
        self.assertIn("rerun with --local", result.stdout)
        self.assertNotIn("mac_exit=42", result.stdout)
        self.assert_wire("failed", "")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_no_test_status_runs_on_natedev_instead(self) -> None:
        self.fixture["run_status_line"] = "mac_exit=4"
        self.sync_fixture()

        result = self.command()

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn(
            "mac_test: no test matched on the Mac; running on natedev instead\n",
            result.stdout,
        )
        self.assertNotIn("mac_exit=4", result.stdout)
        self.assert_wire("declined", "no_tests")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_nextest_failure_status_remains_a_mac_failure(self) -> None:
        self.fixture["run_status_line"] = "mac_exit=1"
        self.sync_fixture()

        result = self.command()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assert_wire("failed", "")

    def test_missing_run_status_is_lost_and_releases_claim(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "partial output"}
        ]
        self.fixture["run_status_reported"] = False
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("partial output\n", result.stdout)
        self.assertIn(
            "mac_test: the link to the Mac dropped; running on natedev instead\n",
            result.stdout,
        )
        self.assert_wire("lost", "")
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_run_signals_request_remote_cleanup_and_release_claim(self) -> None:
        self.fixture["run_delay_s"] = 30.0
        self.sync_fixture()
        for signal_number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_events()
                (self.stub_directory / "run-started").unlink(missing_ok=True)
                (self.stub_directory / "cleanup-called").unlink(missing_ok=True)
                process = self.start_command("--workspace")
                try:
                    self.wait_for_path(self.stub_directory / "run-started")
                    process.send_signal(signal_number)
                    output, _ = process.communicate(timeout=15)
                except BaseException:
                    process.kill()
                    _ = process.communicate(timeout=5)
                    raise
                self.assertEqual(process.returncode, 128 + signal_number, output)
                self.assertTrue(
                    (self.stub_directory / "cleanup-called").exists()
                )
                self.assertFalse((self.state_directory / "run.json").exists())
                ssh_events = self.events_for("ssh")
                self.assertEqual(len(ssh_events), 3)
                cleanup_arguments = ssh_events[-1]["args"]
                self.assertEqual(
                    cleanup_arguments[:-1],
                    [
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=6",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=3",
                        "mac",
                    ],
                )
                cleanup = cleanup_arguments[-1]
                self.assertIn("pkill -f", cleanup)
                self.assertIn(".local/state/mac-test/mirror/hana", cleanup)
                self.assert_claimed_commands()

    def test_second_signal_during_cleanup_keeps_first_status_and_releases(self) -> None:
        self.fixture["run_delay_s"] = 30.0
        self.fixture["hold_cleanup"] = True
        self.sync_fixture()
        process = self.start_command("--workspace")
        try:
            self.wait_for_path(self.stub_directory / "run-started")
            process.send_signal(signal.SIGTERM)
            self.wait_for_path(self.stub_directory / "cleanup-started")
            process.send_signal(signal.SIGHUP)
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            output, _ = process.communicate(timeout=15)
        except BaseException:
            process.kill()
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            _ = process.communicate(timeout=5)
            raise

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
        self.assertTrue((self.stub_directory / "cleanup-called").exists())
        self.assertFalse((self.state_directory / "run.json").exists())

    def test_ignored_interrupt_becomes_default_before_claim(self) -> None:
        startup = self.root / "interrupt-startup"
        startup.mkdir()
        marker = self.stub_directory / "interrupt-is-default"
        gate = self.stub_directory / "continue-after-interrupt-default"
        _ = (startup / "sitecustomize.py").write_text(
            "from __future__ import annotations\n"
            + "import os\n"
            + "from pathlib import Path\n"
            + "import signal\n"
            + "import time\n"
            + "original_signal = signal.signal\n"
            + "def observed_signal(signal_number: int, handler: object) -> object:\n"
            + "    previous = original_signal(signal_number, handler)\n"
            + "    if signal_number == signal.SIGINT and handler == signal.SIG_DFL:\n"
            + "        Path(os.environ['TEST_INTERRUPT_DEFAULT_MARKER']).write_text(\n"
            + "            'ready\\n', encoding='utf-8'\n"
            + "        )\n"
            + "        gate = Path(os.environ['TEST_INTERRUPT_DEFAULT_GATE'])\n"
            + "        while not gate.exists():\n"
            + "            time.sleep(0.01)\n"
            + "    return previous\n"
            + "signal.signal = observed_signal\n",
            encoding="utf-8",
        )
        process = self.start_command(
            extra={
                "PYTHONPATH": str(startup),
                "TEST_INTERRUPT_DEFAULT_MARKER": str(marker),
                "TEST_INTERRUPT_DEFAULT_GATE": str(gate),
            },
            ignore_interrupt=True,
        )
        try:
            self.wait_for_path(marker)
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=15)
        except BaseException:
            process.kill()
            _ = gate.write_text("continue\n", encoding="utf-8")
            _ = process.communicate(timeout=5)
            raise

        self.assertEqual(process.returncode, -signal.SIGINT, output)
        self.assertFalse((self.state_directory / "run.json").exists())
        self.assertEqual(self.events(), [])
        self.assertFalse(self.result_path.exists())

    def test_unwritable_result_destination_returns_internal_error(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.result_path = self.root / "missing" / "result"
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("mac_test: staying on natedev (off)\n", result.stdout)
        self.assertIn("FileNotFoundError", result.stdout)
        self.assertFalse(self.result_path.exists())


if __name__ == "__main__":
    _ = unittest.main()
