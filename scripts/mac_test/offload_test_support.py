"""Independent offload command fixtures shared by routing, cleanup and lifetime tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
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
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast, override
import offload
import mac_test
import treekey
from offload_test_commands import COMMAND_STUB, write_shell

if TYPE_CHECKING:
    from ..production.cli_fixture import invoke_cli
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "production"))
    from cli_fixture import invoke_cli

SCRIPT = Path(__file__).with_name("offload.py")

STATE_SCRIPT = Path(__file__).with_name("mac_test.py")

FILTER = "package(hana) & (test(a) | test(b))"

WORKING_TREE_SOURCE = offload.RepositoryWorkingTree()

# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


class RemoteLine(TypedDict):
    stream: Literal["stdout", "stderr"]
    text: str


class CommandFixture(TypedDict):
    probe_status: int
    probe_error: str
    hold_probe: bool
    probe_lines: list[str]
    rsync_status: int
    hold_copy: bool
    run_lines: list[RemoteLine]
    run_status_reported: bool
    run_status_line: str
    run_cleanup_line: str
    run_ssh_status: int
    hold_run: bool
    hold_after_first_line: bool
    cleanup_status: int
    hold_cleanup: bool
    cleanup_lines: list[str]
    cleanup_error: str
    selector_shell: str
    selector_pgrep_status: int
    selector_lsof_status: int


class CommandEvent(TypedDict):
    command: str
    args: list[str]
    claimed: bool
    exclude: NotRequired[str]
    copy_claim: NotRequired[bool]
    what: NotRequired[str]
    claim: NotRequired[RemoteClaimRecord]


class SelectorEvent(TypedDict):
    command: str
    args: list[str]


class WireResult(TypedDict):
    mac: str
    reason: str
    seconds: str
    holder: NotRequired[str]


class RemoteClaimRecord(TypedDict):
    copy: NotRequired[bool]
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str
    remote_host: str

HANA = offload.KnownRepository("hana")

HANA_COMMAND_PATTERN = "[.]local/state/mac-test/mirror/hana([/'\"[:space:]]|$)"

HANA_CWD_PATTERN = "^n.*/[.]local/state/mac-test/mirror/hana(/|$)"

CLAIM_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

def main() -> int:
    root = Path(os.environ["MAC_TEST_STUB_DIR"])
    state = Path(os.environ["MAC_TEST_STATE_DIR"])
    action = sys.argv[1]
    with (root / "state-events").open("a", encoding="utf-8") as output:
        output.write(action + "\n")
    if action == "claim":
        state.mkdir(parents=True, exist_ok=True)
        arguments = sys.argv[2:]
        owner_pid = int(arguments[arguments.index("--pid") + 1])
        proc_start = Path(f"/proc/{owner_pid}/stat").read_text().rpartition(")")[2].split()[19]
        record = {"pid": owner_pid, "proc_start": proc_start, "what": "nextest hana",
                  "worktree": arguments[arguments.index("--worktree") + 1], "since": "2026-10-12T00:00:00+00:00",
                  "remote_host": arguments[arguments.index("--remote") + 1]}
        actions = (root / "state-events").read_text().splitlines()
        if (os.environ.get("MAC_TEST_TAKEOVER_TWICE") == "1"
                or os.environ.get("MAC_TEST_TAKEOVER_FIRST") == "1" and actions == ["claim"]):
            record["what"] = "older tests"
            record["remote_host"] = "previous-mac"
            (state / "claims").mkdir(exist_ok=True)
            (state / "claims" / "hana.json").write_text(json.dumps(record))
            print("leftovers: previous-mac", flush=True)
            return 13
        takeover = os.environ.get("MAC_TEST_HELD_TAKEOVER") == "1"
        if takeover:
            record["what"] = "older tests"
            record["remote_host"] = "previous-mac"
        (state / "claims").mkdir(exist_ok=True)
        (state / "claims" / "hana.json").write_text(json.dumps(record), encoding="utf-8")
        (root / "claim-started").write_text("started\n", encoding="utf-8")
        while not (root / "finish-claim").exists():
            time.sleep(0.01)
        if takeover:
            print("leftovers: " + os.environ.get("MAC_TEST_TAKEOVER_REPLY", "reply-mac"), flush=True)
            return 13
        return 0
    if action == "release":
        (state / "claims" / "hana.json").unlink(missing_ok=True)
        return 0
    return 12

if __name__ == "__main__":
    raise SystemExit(main())
__file__ = os.environ["MAC_TEST_REAL_STATE"]
exec(compile(Path(__file__).read_text(), __file__, "exec"), globals())
'''


class OffloadCommandFixture(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.repository: Path = Path()
        self.unknown_directory: Path = Path()
        self.state_directory: Path = Path()
        self.claim_file: Path = Path()
        self.stub_directory: Path = Path()
        self.bin_directory: Path = Path()
        self.config_path: Path = Path()
        self.result_path: Path = Path()
        self.environment: dict[str, str] = {}
        self.config: dict[str, str] = {}
        self.fixture: CommandFixture = {
            "probe_status": 0,
            "probe_error": "",
            "hold_probe": False,
            "probe_lines": [],
            "rsync_status": 0,
            "hold_copy": False,
            "run_lines": [],
            "run_status_reported": True,
            "run_status_line": "mac_exit=0",
            "run_cleanup_line": "mac_cleanup=0",
            "run_ssh_status": 0,
            "hold_run": False,
            "hold_after_first_line": False,
            "cleanup_status": 0,
            "hold_cleanup": False,
            "cleanup_lines": ["leftovers=0", "rc=0"],
            "cleanup_error": "",
            "selector_shell": "",
            "selector_pgrep_status": 1,
            "selector_lsof_status": 0,
        }

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repository = self.root / "hana"
        self.unknown_directory = self.root / "not-a-repository"
        self.state_directory = self.root / "state"
        self.claim_file = self.state_directory / "claims" / "hana.json"
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
        write_shell(stub, COMMAND_STUB)
        for name in ("ssh", "rsync", "gh", "systemd-run"):
            (self.bin_directory / name).symlink_to(stub)
        self.config = {
            "offload": "on",
            "host": "mac",
            "connect_timeout_s": "6",
            "probe_timeout_s": "300",
            "leftover_timeout_s": "300",
            "unreachable_backoff_s": "300",
            "free_floor_gib": "60",
            "mac_budget_gib": "24",
            "mac_budget_gib.hana": "64",
            "linux_only.hana": "",
            "mac_skip.hana": "",
        }
        self.fixture = {
            "probe_status": 0,
            "probe_error": "",
            "hold_probe": False,
            "probe_lines": [
                "free_gib=120",
                "host=Mac",
                "rc=0",
            ],
            "rsync_status": 0,
            "hold_copy": False,
            "run_lines": [],
            "run_status_reported": True,
            "run_status_line": "mac_exit=0",
            "run_cleanup_line": "mac_cleanup=0",
            "run_ssh_status": 0,
            "hold_run": False,
            "hold_after_first_line": False,
            "cleanup_status": 0,
            "hold_cleanup": False,
            "cleanup_lines": ["leftovers=0", "rc=0"],
            "cleanup_error": "",
            "selector_shell": "",
            "selector_pgrep_status": 1,
            "selector_lsof_status": 0,
        }
        # Keep a reader open so release writes also remain safe during failure cleanup.
        for name in ("continue-copy", "continue-run", "continue-cleanup", "continue-probe", "finish-run"):
            gate = self.stub_directory / name
            os.mkfifo(gate)
            descriptor = os.open(gate, os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
        self.sync_config()
        self.sync_fixture()
        self.environment = {
            **os.environ,
            "HOME": str(self.root / "home"),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_CONFIG": str(self.config_path),
            "MAC_TEST_STUB_DIR": str(self.stub_directory),
            "MAC_TEST_REAL_STATE": str(STATE_SCRIPT),
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
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
        assignments = [f"TEST_HANG_GUARD_SECS={TEST_HANG_GUARD_SECS}"]
        for name, value in self.fixture.items():
            if name == "run_lines":
                continue
            if isinstance(value, list):
                words = cast(list[str], value)
                assignments.append(f"{name}=(" + shlex.join(words) + ")")
            else:
                scalar = str(int(value)) if isinstance(value, bool) else str(value)
                assignments.append(f"{name}=" + shlex.quote(scalar))
        assignments.append("run_lines=(" + shlex.join([line["text"] for line in self.fixture["run_lines"]]) + ")")
        assignments.append("run_streams=(" + shlex.join([line["stream"] for line in self.fixture["run_lines"]]) + ")")
        _ = (self.stub_directory / "fixture.sh").write_text("\n".join(assignments) + "\n")

    def command(
        self,
        *nextest_arguments: str,
        repository: Path | None = None,
        filter_run: bool = False,
        timeout: float = TEST_HANG_GUARD_SECS,
        subprocess_cli: bool = False,
        settings: Path | offload.DefaultMacSettings = offload.DEFAULT_MAC_SETTINGS,
        last_decline: str = "",
        separate_stderr: bool = False,
        copy: bool = False,
        verb: offload.MacVerb = "nextest",
        package: str = "hana",
        source: offload.MacSource = WORKING_TREE_SOURCE,
    ) -> subprocess.CompletedProcess[str]:
        self.result_path.unlink(missing_ok=True)
        arguments = [
            sys.executable,
            str(SCRIPT),
            "run",
            "--repo-root",
            str(self.repository if repository is None else repository),
            "--call-id",
            "call-17",
        ]
        if isinstance(source, offload.StagedDocSnapshot):
            arguments.extend(["--source-root", str(source.root)])
        if package:
            arguments.extend(["--package", package])
        arguments.extend(["--verb", verb])
        if filter_run:
            arguments.append("--filter-run")
        if copy:
            arguments.append("--copy")
        if isinstance(settings, Path):
            arguments.extend(["--settings", str(settings)])
        if last_decline:
            arguments.extend(["--last-decline", last_decline])
        arguments.extend(["--result", str(self.result_path), "--"])
        arguments.extend(nextest_arguments)
        if subprocess_cli:
            return subprocess.run(arguments, cwd=self.root, env=self.environment, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
                                  text=True, check=False, timeout=timeout)
        return invoke_cli(offload, offload.main, arguments[2:], self.environment, self.root,
                          combined_output=not separate_stderr)

    def process_command(self, *arguments: str, separate_stderr: bool = False,
                        last_decline: str = "") -> subprocess.CompletedProcess[str]:
        # These cases inspect a claim owned by an exited launcher. Its lifetime
        # is part of the assertion, so use a real child rather than this process.
        return self.command(*arguments, separate_stderr=separate_stderr,
                            last_decline=last_decline, subprocess_cli=True)

    def command_with_cleanup_clock(self, *, separate_stderr: bool = False) -> subprocess.CompletedProcess[str]:
        # Keep elapsed_seconds real; advance its underlying clock at work boundaries.
        program = r'''
import subprocess
import sys
sys.path.insert(0, sys.argv.pop(1))
import offload

clock = [100.0]
offload.time.monotonic = lambda: clock[0]
real_capture = offload.ProcessController.capture
real_stream = offload.ProcessController.stream_mac_run
real_run = subprocess.run

def capture(self, arguments, *args, **kwargs):
    answer = real_capture(self, arguments, *args, **kwargs)
    if arguments[0] == "ssh":
        clock[0] = 101.0
    elif arguments[0] == "rsync":
        clock[0] = 102.0
    return answer

def stream(self, arguments):
    answer = real_stream(self, arguments)
    clock[0] = 103.0
    return answer

def run(arguments, **kwargs):
    if arguments[0] != "ssh" or "select_leftovers()" not in arguments[-1]:
        return real_run(arguments, **kwargs)
    clock[0] = 105.0
    try:
        answer = real_run(arguments, **kwargs)
    except subprocess.TimeoutExpired:
        clock[0] = 113.0
        raise
    clock[0] = 108.0
    return answer

offload.ProcessController.capture = capture
offload.ProcessController.stream_mac_run = stream
subprocess.run = run
raise SystemExit(offload.main())
'''
        return subprocess.run(
            [sys.executable, "-c", program, str(SCRIPT.parent), "run",
             "--repo-root", str(self.repository), "--package", "hana",
             "--call-id", "call-17", "--result", str(self.result_path), "--"],
            cwd=self.root, env=self.environment, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
            text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
        )

    def start_command(
        self,
        *nextest_arguments: str,
        script: Path = SCRIPT,
        extra: dict[str, str] | None = None,
        ignore_interrupt: bool = False,
        start_new_session: bool = False,
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
            start_new_session=start_new_session,
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
        (scripts_directory / "build_hold").symlink_to(SCRIPT.parent.parent / "build_hold", target_is_directory=True)
        (scripts_directory / "buildlog").symlink_to(
            SCRIPT.parent.parent / "buildlog", target_is_directory=True
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
            timeout=TEST_HANG_GUARD_SECS,
        )

    def events(self) -> list[CommandEvent]:
        path = self.stub_directory / "events.jsonl"
        if not path.exists():
            return []
        events = [cast(CommandEvent, json.loads(line))
                  for line in path.read_text(encoding="utf-8").splitlines()]
        for event in events:
            if "claim" in event:
                event["copy_claim"] = event["claim"].get("copy", False)
                event["what"] = event["claim"]["what"]
        return events

    def events_for(self, command: str) -> list[CommandEvent]:
        return [event for event in self.events() if event["command"] == command]

    def clear_events(self) -> None:
        (self.stub_directory / "events.jsonl").unlink(missing_ok=True)

    def wire_result(self) -> WireResult:
        lines = self.result_path.read_text(encoding="utf-8").splitlines()
        values = dict(line.split("=", 1) for line in lines)
        expected = {"mac", "reason", "seconds"}
        if values["mac"] == "declined" and values["reason"] == "busy":
            expected.add("holder")
        self.assertEqual(set(values), expected)
        self.assertEqual(len(lines), len(expected))
        _ = float(values["seconds"])
        return cast(WireResult, cast(object, values))

    def assert_wire(self, mac: str, reason: str) -> None:
        values = self.wire_result()
        self.assertEqual(values["mac"], mac)
        self.assertEqual(values["reason"], reason)
        self.assertGreaterEqual(float(values["seconds"]), 0.0)

    def assert_declined(
        self, result: subprocess.CompletedProcess[str], reason: str
    ) -> None:
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertTrue(result.stdout.endswith(f"mac_test: not running on the Mac ({reason})\n"), result.stdout)
        self.assertNotIn("mac_test: running on the Mac", result.stdout)
        self.assert_wire("declined", reason)
        self.assertFalse(self.claim_file.exists())

    def assert_claimed_commands(self) -> None:
        for event in self.events():
            if event["command"] in {"ssh", "rsync"}:
                self.assertTrue(event["claimed"], event)

    def leftover_events(self) -> list[CommandEvent]:
        return [event for event in self.events_for("ssh") if "select_leftovers()" in event["args"][-1]]

    def assert_leftover_selectors(self, remote: str) -> None:
        self.assertIn(f"mirror_command_pattern={shlex.quote(HANA_COMMAND_PATTERN)}\n", remote)
        self.assertIn(f"mirror_cwd_pattern={shlex.quote(HANA_CWD_PATTERN)}\n", remote)
        self.assertIn('pgrep -u "$(id -u)" -f "$mirror_command_pattern"', remote)
        self.assertIn('lsof -a -u "$(id -u)" -d cwd -Fpn', remote)
        self.assertIn("awk -v pattern=\"$mirror_cwd_pattern\" '/^p/ { pid = substr($0, 2) } $0 ~ pattern { print pid }'", remote)
        self.assertNotIn("pkill", remote)
        self.assertNotIn("PIPESTATUS", remote)
        self.assertNotIn(".local/state/mac-test/mirror/", remote.replace("[.]local/state/mac-test/mirror/", ""))

    def remote_claim(self) -> RemoteClaimRecord:
        return cast(RemoteClaimRecord, json.loads(self.claim_file.read_text()))

    def write_kept_claim(self, host: str = "previous-mac") -> RemoteClaimRecord:
        self.claim_file.parent.mkdir(parents=True, exist_ok=True)
        record: RemoteClaimRecord = {"pid": 1073741824, "proc_start": "gone", "what": "older tests",
                                     "worktree": str(self.repository), "since": datetime.now(timezone.utc).isoformat(),
                                     "remote_host": host}
        _ = self.claim_file.write_text(json.dumps(record))
        return record

    def assert_claim_is_kept(self, host: str = "mac") -> None:
        path = self.claim_file
        self.assertTrue(path.exists())
        record = self.remote_claim()
        self.assertEqual(record["remote_host"], host)
        self.assertIsNone(mac_test.process_start_time(record["pid"]))
        decoded = mac_test.read_run(path)
        self.assertNotIsInstance(decoded, mac_test.NoRun)
        if not isinstance(decoded, mac_test.NoRun):
            self.assertTrue(mac_test.run_is_live(decoded))

    def reset_leftover_case(self) -> None:
        self.clear_events()
        self.claim_file.unlink(missing_ok=True)
        (self.state_directory / "unreachable_until").unlink(missing_ok=True)
        for name in ("run-started", "cleanup-called", "selector-events.jsonl", "selector-answer"):
            (self.stub_directory / name).unlink(missing_ok=True)

    def set_leftover_answer(self, outcome: str) -> None:
        # Only deliberate timeout cases use a short deadline; ordinary fixture
        # commands get a hang guard that does not race interpreter startup.
        self.config["leftover_timeout_s"] = "2" if outcome == "timeout" else "300"
        self.sync_config()
        self.fixture["cleanup_lines"] = ["leftovers=0", "rc=0"]
        self.fixture["cleanup_status"] = 0
        self.fixture["cleanup_error"] = ""
        self.fixture["hold_cleanup"] = False
        if outcome == "remaining":
            self.fixture["cleanup_lines"] = ["leftovers=3", "rc=0"]
        elif outcome == "missing_status":
            self.fixture["cleanup_lines"] = ["leftovers=0"]
        elif outcome == "timeout":
            # Remain blocked until the cleanup deadline, regardless of machine load.
            self.fixture["hold_cleanup"] = True
        elif outcome == "ssh_error":
            self.fixture["cleanup_lines"] = []
            self.fixture["cleanup_status"] = 255
            self.fixture["cleanup_error"] = "connection\n  refused\tby host"
        self.sync_fixture()

    def assert_leftover_diagnostic(self, output: str, outcome: str) -> None:
        if outcome == "remaining":
            self.assertIn("mac_test: 3 leftover processes of a lost run are still on the Mac; it stays busy until they end\n", output)
        else:
            self.assertRegex(output, r"mac_test: could not confirm that a lost run's leftovers ended: [^\n]+; the Mac stays busy\n")
            if outcome == "timeout":
                self.assertRegex(output, r"timed out after 2(?:\.0)? s")
            if outcome == "ssh_error":
                self.assertIn("connection refused by host", output)

    def wait_for_path(self, path: Path) -> None:
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
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
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
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

    def interrupt_held_claim(
        self, *, takeover: bool = False, after_takeover: bool = False,
        signal_group: bool = False, signal_number: int = signal.SIGTERM,
        reply_host: str = "reply-mac",
    ) -> str:
        process = self.start_command(
            script=self.claim_stub_runner(),
            extra={"MAC_TEST_HELD_TAKEOVER": "1" if takeover else "0",
                   "MAC_TEST_TAKEOVER_FIRST": "1" if after_takeover else "0",
                   "MAC_TEST_TAKEOVER_REPLY": reply_host},
            start_new_session=signal_group,
        )
        try:
            self.wait_for_path(self.stub_directory / "claim-started")
            if signal_group:
                os.killpg(process.pid, signal_number)
            else:
                process.send_signal(signal_number)
            self.wait_for_ignored_signals(process)
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            output, _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 128 + signal_number, output)
        self.assertFalse(self.result_path.exists())
        self.assertFalse((self.state_directory / "unreachable_until").exists())
        return output

    def wait_for_ignored_signals(self, process: subprocess.Popen[str]) -> None:
        expected = sum(1 << (number - 1) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            try:
                status = Path(f"/proc/{process.pid}/status").read_text()
            except FileNotFoundError:
                continue
            masks = [line.split()[1] for line in status.splitlines() if line.startswith("SigIgn:")]
            if masks and int(masks[0], 16) & expected == expected:
                return
            time.sleep(0.01)
        self.fail("runner did not handle the claim signal")

    def assert_fresh_claim_signal_releases_without_ssh(self, outcome: str) -> None:
        self.set_leftover_answer(outcome)
        output = self.interrupt_held_claim()
        state_events = (self.stub_directory / "state-events").read_text(
            encoding="utf-8"
        )
        self.assertEqual(state_events.splitlines(), ["claim", "release"], output)
        self.assertFalse(self.claim_file.exists(), output)
        self.assertEqual(self.events_for("ssh"), [])
        self.assertFalse((self.stub_directory / "cleanup-called").exists())

    def assert_takeover_signal_checks_reply_host(self, outcome: str) -> None:
        self.set_leftover_answer(outcome)
        output = self.interrupt_held_claim(takeover=True)
        ssh = self.events_for("ssh")
        self.assertEqual(len(ssh), 1, output)
        self.assertEqual(ssh[0]["args"][-2], "reply-mac", output)
        self.assertTrue((self.stub_directory / "cleanup-called").exists())
        state_events = (self.stub_directory / "state-events").read_text().splitlines()
        if outcome == "clean":
            self.assertEqual(state_events, ["claim", "release"])
            self.assertFalse(self.claim_file.exists())
            self.assertEqual(output, "")
        else:
            self.assertEqual(state_events, ["claim"])
            self.assert_claim_is_kept("previous-mac")
            self.assert_leftover_diagnostic(output, outcome)

    def commit_sent_tree(self) -> str:
        environment = {
            **self.environment,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "test", "GIT_COMMITTER_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        result = subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "sent tree"],
            cwd=self.repository, env=environment, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        key = treekey.tree_key(str(self.repository))
        self.assertIsInstance(key, str)
        assert isinstance(key, str)
        return key

    def run_with_copy_change(self, change: Callable[[], None]) -> None:
        self.fixture["hold_copy"] = True
        self.sync_fixture()
        process = self.start_command()
        try:
            self.wait_for_path(self.stub_directory / "copy-started")
            change()
            _ = (self.stub_directory / "continue-copy").write_text("continue\n")
            output, _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, 0, output)
            self.assert_wire("passed", "")
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def sent_command_words(self) -> list[str]:
        self.assertEqual(len(self.events_for("rsync")), 1)
        remote = self.events_for("ssh")[-1]["args"][-1]
        self.assertIn("lint nextest", remote)
        return shlex.split(remote.rpartition("; echo mac_exit=$?")[0])

    def assert_stable_copy_sends_key(self) -> None:
        # A later stable copy must send its key after the earlier copy omitted it.
        self.clear_events()
        self.fixture["hold_copy"] = False
        self.sync_fixture()
        key = self.commit_sent_tree()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"BUILDLOG_SENT_TREE_KEY={key}", self.sent_command_words())

    def write_portable_workspace(self) -> None:
        _ = (self.repository / "Cargo.toml").write_text('[workspace]\nmembers=["portable"]\n')
        member = self.repository / "portable"
        member.mkdir(exist_ok=True)
        _ = (member / "Cargo.toml").write_text('[package]\nname="portable"\nversion="0.1.0"\n')

