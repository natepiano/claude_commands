#!/usr/bin/env python3
"""Exercise Mac nextest offload through disposable command stand-ins."""

from __future__ import annotations

from collections.abc import Callable, Sequence
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
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields
import io
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast, final, override
from unittest import mock

import offload
if TYPE_CHECKING:
    from ..production.cli_fixture import invoke_cli
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "production"))
    from cli_fixture import invoke_cli
import mac_test
import treekey


SCRIPT = Path(__file__).with_name("offload.py")
STATE_SCRIPT = Path(__file__).with_name("mac_test.py")
FILTER = "package(hana) & (test(a) | test(b))"
WORKING_TREE_SOURCE = offload.RepositoryWorkingTree()


class RemoteLine(TypedDict):
    stream: Literal["stdout", "stderr"]
    text: str


class CommandFixture(TypedDict):
    probe_status: int
    probe_error: str
    probe_delay_s: float
    probe_lines: list[str]
    rsync_status: int
    hold_copy: bool
    run_lines: list[RemoteLine]
    run_status_reported: bool
    run_status_line: str
    run_cleanup_line: str
    run_ssh_status: int
    run_delay_s: float
    hold_after_first_line: bool
    cleanup_status: int
    cleanup_delay_s: float
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


class SelectorEvent(TypedDict):
    command: str
    args: list[str]


class WireResult(TypedDict):
    mac: str
    reason: str
    seconds: str
    holder: NotRequired[str]


class RemoteClaimRecord(TypedDict):
    pid: int
    proc_start: str
    what: str
    worktree: str
    since: str
    remote_host: str


COMMAND_STUB = r'''
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import shutil
import subprocess
import shlex

root = Path(os.environ["MAC_TEST_STUB_DIR"])
fixture = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
if command == "systemd-run":
    with (root / "systemd-run.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(arguments) + "\n")
    raise SystemExit(0)
event = {
    "command": command,
    "args": arguments,
    "claimed": (Path(os.environ["MAC_TEST_STATE_DIR"]) / "claims" / "hana.json").exists(),
}
try:
    claim = json.loads((Path(os.environ["MAC_TEST_STATE_DIR"]) / "claims" / "hana.json").read_text())
    event["copy_claim"] = claim.get("copy", False)
    event["what"] = claim["what"]
except (OSError, ValueError):
    pass
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
    (root / "copy-started").write_text("started\n")
    if fixture["hold_copy"]:
        while not (root / "continue-copy").exists():
            time.sleep(0.01)
    raise SystemExit(int(fixture["rsync_status"]))
if command != "ssh":
    (root / (command + "-called")).write_text("called\n", encoding="utf-8")
    raise SystemExit(99)

remote = arguments[-1]
if "select_leftovers()" in remote and "lsof" in remote:
    (root / "cleanup-called").write_text("called\n", encoding="utf-8")
    time.sleep(float(fixture["cleanup_delay_s"]))
    if fixture["hold_cleanup"]:
        (root / "cleanup-started").write_text("started\n", encoding="utf-8")
        while not (root / "continue-cleanup").exists():
            time.sleep(0.01)
    if fixture["selector_shell"]:
        selectors = root / "selectors"
        environment = {**os.environ, "PATH": str(selectors) + os.pathsep + os.environ["PATH"]}
        for name in ("pgrep", "lsof", "sleep"):
            expected = str(selectors / name)
            if shutil.which(name, path=environment["PATH"]) != expected:
                raise SystemExit("selector stand-in missing: " + name)
        shell = ["/bin/sh"] if fixture["selector_shell"] == "sh" else ["zsh", "-f"]
        checks = "\n".join(
            '[ "$(command -v ' + name + ')" = ' + shlex.quote(str(selectors / name)) + ' ] || exit 99'
            for name in ("pgrep", "lsof", "sleep")
        )
        answer = subprocess.run([*shell, "-c", checks + "\n" + remote], env=environment, capture_output=True, text=True, timeout=300)
        print(answer.stdout, end="", flush=True)
        print(answer.stderr, end="", file=sys.stderr, flush=True)
        (root / "selector-answer").write_text(answer.stdout)
        raise SystemExit(answer.returncode)
    if fixture["cleanup_error"]:
        print(fixture["cleanup_error"], file=sys.stderr, flush=True)
    for line in fixture["cleanup_lines"]:
        print(line, flush=True)
    raise SystemExit(int(fixture["cleanup_status"]))
if any("lint " + verb in remote for verb in ("nextest", "check", "clippy", "mend-check", "doc")):
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
        if fixture["run_cleanup_line"]:
            print(fixture["run_cleanup_line"], flush=True)
    raise SystemExit(int(fixture["run_ssh_status"]))

(root / "probe-started").write_text("started\n", encoding="utf-8")
time.sleep(float(fixture["probe_delay_s"]))
if fixture["probe_error"]:
    print(fixture["probe_error"], file=sys.stderr, flush=True)
for line in fixture["probe_lines"]:
    print(line, flush=True)
raise SystemExit(int(fixture["probe_status"]))
'''


class LintVerbTests(unittest.TestCase):
    def test_check_and_mend_check_use_the_public_lint_command_without_rewrites(self) -> None:
        lint = Path(__file__).resolve().parents[1] / "lint" / "lint"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invoke = root / ".claude/scripts/lint/invoke.sh"
            invoke.parent.mkdir(parents=True)
            _ = invoke.write_text(
                'run() { printf "%s\\n" "$@"; }\n'
                + 'invoke_mend() { printf "%s\\n" mend "$@"; }\n')
            environment = {**os.environ, "HOME": str(root), "BUILDLOG_DIR": str(root / "buildlog")}
            for verb, expected in (("check", ["cargo", "check", "--workspace", "--all-features"]),
                                   ("mend-check", ["mend", "--fail-on-warn", "--workspace", "--all-features"])):
                with self.subTest(verb=verb):
                    answer = subprocess.run(["bash", str(lint), verb, "--workspace", "--all-features"],
                                            env=environment, cwd=root, capture_output=True, text=True, check=False)
                    self.assertEqual(answer.returncode, 0, answer.stderr)
                    self.assertEqual(answer.stdout.splitlines(), expected)


HANA = offload.KnownRepository("hana")
HANA_COMMAND_PATTERN = "[.]local/state/mac-test/mirror/hana([/'\"[:space:]]|$)"
HANA_CWD_PATTERN = "^n.*/[.]local/state/mac-test/mirror/hana(/|$)"


SELECTOR_STUB = r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["MAC_TEST_STUB_DIR"])
fixture = json.loads((root / "fixture.json").read_text())
command = Path(sys.argv[0]).name
with (root / "selector-events.jsonl").open("a") as output:
    output.write(json.dumps({"command": command, "args": sys.argv[1:]}) + "\n")
# Selectors print no process ids, so the tested shell text can signal no process.
if command == "pgrep":
    raise SystemExit(fixture["selector_pgrep_status"])
if command == "lsof":
    raise SystemExit(fixture["selector_lsof_status"])
if command == "sleep":
    raise SystemExit(0)
raise SystemExit(99)
'''


ACTIVITY_SELECTOR_STUB = r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["ACTIVITY_TEST_DIR"])
name = Path(sys.argv[0]).name
responses = json.loads((root / "responses.json").read_text())
with (root / "commands.jsonl").open("a") as output:
    output.write(json.dumps({"command": name, "args": sys.argv[1:]}) + "\n")
status, answer = responses[name]
print(answer, end="")
raise SystemExit(status)
'''


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


class OffloadSettingsTests(unittest.TestCase):
    root: Path = Path()
    settings: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = self.root / "mac_test.conf"
        _ = self.settings.write_text("", encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            "MAC_TEST_CONFIG": str(self.settings),
            "MAC_TEST_STATE_DIR": str(self.root / "state"),
        })))
        _ = self.enterContext(mock.patch.object(Path, "home", return_value=self.root))
        _ = self.enterContext(mock.patch("subprocess.run", side_effect=AssertionError("No external command expected")))
        _ = self.enterContext(mock.patch("subprocess.Popen", side_effect=AssertionError("No external command expected")))

    def test_explicit_settings_path_controls_every_offload_field(self) -> None:
        settings = self.root / "other-mac.conf"
        _ = settings.write_text(
            "offload=on\nhost=other-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n"
            + "unreachable_backoff_s=4\nfree_floor_gib=5\nmac_budget_gib=6\n"
            + "mac_budget_gib.hana=7\nlinux_only.hana=local-package\n",
            encoding="utf-8",
        )
        config = offload.read_config(settings)
        self.assertTrue(config.enabled)
        self.assertEqual(config.host, "other-mac")
        self.assertEqual(config.connect_timeout_s, 2.0)
        self.assertEqual(config.probe_timeout_s, 3.0)
        self.assertEqual(config.unreachable_backoff_s, 4.0)
        self.assertEqual(config.free_floor_gib, 5.0)
        self.assertEqual(config.default_target_budget_gib, "6")
        self.assertEqual(config.values["mac_budget_gib.hana"], "7")
        self.assertEqual(config.values["linux_only.hana"], "local-package")

    def test_no_argument_config_reader_keeps_existing_defaults(self) -> None:
        expected = offload.OffloadConfig(False, "mac", 6.0, 15.0, 300.0, 60.0, "24", {})
        self.assertEqual(offload.read_config(), expected)
        self.assertEqual(offload.read_config(self.settings), expected)

    def test_no_argument_config_reader_still_uses_environment_path(self) -> None:
        _ = self.settings.write_text("offload=on\nhost=environment-mac\nprobe_timeout_s=7\n", encoding="utf-8")
        expected = offload.OffloadConfig(
            True, "environment-mac", 6.0, 7.0, 300.0, 60.0, "24",
            {"offload": "on", "host": "environment-mac", "probe_timeout_s": "7"},
        )
        self.assertEqual(offload.read_config(), expected)

    def test_path_reader_keeps_invalid_numeric_defaults(self) -> None:
        _ = self.settings.write_text(
            "connect_timeout_s=bad\nprobe_timeout_s=0\nunreachable_backoff_s=-1\nfree_floor_gib=nan\n",
            encoding="utf-8",
        )
        config = offload.read_config(self.settings)
        self.assertEqual(
            (config.connect_timeout_s, config.probe_timeout_s, config.unreachable_backoff_s, config.free_floor_gib),
            (6.0, 15.0, 300.0, 60.0),
        )

    def test_reachability_probe_runs_only_a_read_only_remote_command(self) -> None:
        _ = self.settings.write_text("host=other-mac\nconnect_timeout_s=2\nprobe_timeout_s=3\n", encoding="utf-8")
        config = offload.read_config(self.settings)
        controller = offload.ProcessController()
        calls: list[tuple[list[str], offload.CommandDeadline]] = []

        def capture(
            arguments: list[str],
            deadline: offload.CommandDeadline = offload.NO_COMMAND_TIME_LIMIT,
        ) -> offload.CapturedCommandResult:
            calls.append((arguments, deadline))
            return offload.CommandOutput(0, "", "")

        with mock.patch.object(controller, "capture", side_effect=capture):
            self.assertEqual(offload.probe_reachability(controller, config), offload.ReachableHost())
        self.assertEqual(len(calls), 1)
        arguments, deadline = calls[0]
        self.assertEqual(arguments[:-1], [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "other-mac",
        ])
        self.assertEqual(arguments[-1], "true")
        self.assertNotRegex(arguments[-1], r"\b(?:mkdir|touch|tee|rm|mv|cp)\b|[>]")
        self.assertEqual(deadline, offload.CommandTimeLimit(3.0))
        self.assertFalse((self.root / "state").exists())

    def test_failed_reachability_command_reports_unreachable(self) -> None:
        config = offload.read_config(self.settings)
        failures: tuple[offload.CapturedCommandResult, ...] = (
            offload.CommandOutput(255, "", "connection refused"),
            offload.CommandOutput(1, "", "remote command failed"),
            offload.COMMAND_TIMED_OUT,
            offload.COMMAND_COULD_NOT_START,
        )
        for failure in failures:
            with self.subTest(result=type(failure).__name__):
                controller = offload.ProcessController()
                with mock.patch.object(controller, "capture", return_value=failure):
                    result = offload.probe_reachability(controller, config)
                self.assertIsInstance(result, offload.UnreachableHost)
                if isinstance(result, offload.UnreachableHost):
                    self.assertTrue(result.reason)

    def test_resource_probe_needs_no_process_count_and_ignores_legacy_counts(self) -> None:
        wire = "free_gib=120\nhost=Mac\nrc=0\n"
        expected = offload.ProbeSnapshot(120.0, "Mac")
        self.assertEqual(offload.parse_probe(wire), expected)
        self.assertEqual(offload.parse_probe("procs=12\n" + wire), expected)
        self.assertEqual(offload.parse_probe("procs=unreadable\n" + wire), expected)
        self.assertNotIn("processes", {field.name for field in fields(expected)})
        script = offload.probe_script(offload.KnownRepository("hana"))
        self.assertNotIn("pgrep", script)
        self.assertNotIn("procs", script)

    def test_leftover_check_requires_count_and_printed_success_on_the_named_host(self) -> None:
        config = offload.read_config(self.settings)
        for answer, expected in (("leftovers=0\nrc=0\n", offload.LeftoversEnded()),
                                 ("leftovers=4\nrc=0\n", offload.LeftoversRemain(4))):
            with self.subTest(answer=answer):
                response = subprocess.CompletedProcess(["ssh"], 0, answer, "")
                with mock.patch.object(subprocess, "run", return_value=response) as run:
                    self.assertEqual(offload.check_leftovers(config, "kept-host", HANA), expected)
                arguments = cast(list[str], run.call_args.args[0])
                self.assertEqual(arguments[:-1], ["ssh", *offload.ssh_options(config), "kept-host"])
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
                self.assertIn('pgrep -u "$(id -u)" -f "$mirror_command_pattern"', arguments[-1])
                self.assertIn(f"mirror_command_pattern={shlex.quote(HANA_COMMAND_PATTERN)}\n", arguments[-1])
        for answer in ("leftovers=0\n", "rc=0\n", "leftovers=-1\nrc=0\n", "leftovers=bad\nrc=0\n"):
            with self.subTest(answer=answer):
                response = subprocess.CompletedProcess(["ssh"], 0, answer, "")
                with mock.patch.object(subprocess, "run", return_value=response):
                    result = offload.check_leftovers(config, "kept-host", HANA)
                self.assertIsInstance(result, offload.LeftoverCheckFailed)
                if isinstance(result, offload.LeftoverCheckFailed):
                    self.assertIn("answer could not be read", result.reason)

    def test_leftover_check_reports_timeout_start_failure_and_ssh_error(self) -> None:
        config = offload.read_config(self.settings)
        for error, expected in ((subprocess.TimeoutExpired("ssh", 10), "timed out after 10"),
                                (FileNotFoundError("ssh missing"), "could not start")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(subprocess, "run", side_effect=error):
                    result = offload.check_leftovers(config, "kept-host", HANA)
                self.assertIsInstance(result, offload.LeftoverCheckFailed)
                if isinstance(result, offload.LeftoverCheckFailed):
                    self.assertIn(expected, result.reason)
                    self.assertIn("10", result.reason)
        response = subprocess.CompletedProcess(["ssh"], 255, "", "connection\n refused\tby host")
        with mock.patch.object(subprocess, "run", return_value=response):
            result = offload.check_leftovers(config, "kept-host", HANA)
        self.assertIsInstance(result, offload.LeftoverCheckFailed)
        if isinstance(result, offload.LeftoverCheckFailed):
            self.assertEqual(result.reason, "connection refused by host")

    def test_leftover_check_uses_configured_timeout_and_reports_it(self) -> None:
        _ = self.settings.write_text("leftover_timeout_s=0.125\n")
        config = offload.read_config(self.settings)
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 0.125)) as run:
            result = offload.check_leftovers(config, "kept-host", HANA)
        self.assertEqual(run.call_args.kwargs["timeout"], 0.125)
        self.assertEqual(result, offload.LeftoverCheckFailed("SSH leftover check timed out after 0.125 s"))
        for value in ("bad", "0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                _ = self.settings.write_text(f"leftover_timeout_s={value}\n")
                self.assertEqual(offload.read_config(self.settings).leftover_timeout_s, 10)

    def test_probe_that_cannot_start_reports_its_limit_through_run(self) -> None:
        _ = self.settings.write_text("offload=on\nlinux_only.hana=\nprobe_timeout_s=2.5\n")
        destination = self.root / "result"
        responses = [offload.COMMAND_COULD_NOT_START, offload.CommandOutput(0, "", "")]

        def complete_claim(arguments: Sequence[str], attempt: offload.LocalClaimAttempt) -> None:
            _ = arguments
            attempt.answer = offload.FreshMacClaim()

        with mock.patch.object(sys, "argv", ["offload.py", "run", "--repo-root", str(self.root),
                                             "--package", "hana", "--call-id", "probe-start", "--result", str(destination)]), \
             mock.patch.object(offload, "repository_identity", return_value=offload.KnownRepository("hana")), \
             mock.patch.object(offload.ProcessController, "complete_claim", side_effect=complete_claim), \
             mock.patch.object(offload.ProcessController, "capture", side_effect=responses) as capture, \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(offload.main(), 75)
        self.assertEqual(output.getvalue(), "mac_test: not running on the Mac (unreachable)\n")
        self.assertRegex(errors.getvalue(), r"^mac_test: could not reach the Mac: .*could not start.*2\.5.*\n$")
        self.assertEqual(capture.call_count, 2)
        self.assertEqual(destination.read_text().splitlines()[:2], ["mac=declined", "reason=unreachable"])
        self.assertTrue((self.root / "state" / "unreachable_until").exists())

    def test_checked_in_settings_send_cargo_liner_to_the_mac_except_cargo_berth(self) -> None:
        config = offload.read_config(Path(__file__).resolve().parents[2] / "config" / "mac_test.conf")
        self.assertEqual(config.values["linux_only.cargo-liner"], "cargo-berth")
        self.assertEqual(config.default_target_budget_gib, "24")
        self.assertNotIn("mac_budget_gib.cargo-liner", config.values)
        offload.write_backoff(time.time() + 60)
        expected = {"cargo-port": "backoff", "cargo-tile": "backoff", "tui_pane": "backoff", "cargo-berth": "linux_only"}
        for package, reason in expected.items():
            with self.subTest(package=package):
                request = offload.OffloadRequest(self.root, package, "cargo-liner", offload.NO_RESULT_FILE, ())
                with mock.patch.object(offload, "repository_identity",
                                       return_value=offload.KnownRepository("cargo-liner")):
                    result = offload.run_offload(offload.ProcessController(), config, request, time.monotonic())
                self.assertEqual(result.reason if isinstance(result, offload.DeclinedMacRun) else result, reason)


@final
class BusyHolderTests(unittest.TestCase):
    def test_busy_claim_line_keeps_the_job_and_kept_claim_description(self) -> None:
        for what in ("cargo nextest -p hana", "leftovers of cargo test on prior-mac", "job since last week"):
            with self.subTest(what=what):
                self.assertEqual(offload.claim_holder(f"busy: {what} since 13:05\n"), offload.KnownMacJob(what))

    def test_busy_claim_without_the_expected_line_has_unreadable_holder(self) -> None:
        for line in ("", "busy", "busy: task", "busy: task since yesterday", "busy: task since 13:05\nextra"):
            with self.subTest(line=line):
                self.assertIsInstance(offload.claim_holder(line), offload.UnreadableMacHolder)

    def test_only_busy_results_write_the_holder_line(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "result"
            for holder, expected in ((offload.KnownMacJob("render checks"), "render checks"),
                                     (offload.LostRunLeftovers(), "leftovers"),
                                     (offload.UnreadableMacHolder(), "unreadable")):
                with self.subTest(holder=expected), redirect_stdout(io.StringIO()):
                    self.assertEqual(offload.write_result(offload.BusyMacDecline(holder, 0.25), offload.ResultFile(path)), 75)
                    self.assertIn(f"holder={expected}\n", path.read_text())
            with redirect_stdout(io.StringIO()):
                _ = offload.write_result(offload.PassedOnMac(0.5), offload.ResultFile(path))
            self.assertNotIn("holder=", path.read_text())


@final
class OffloadCommandTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.repository = Path()
        self.unknown_directory = Path()
        self.state_directory = Path()
        self.claim_file = Path()
        self.stub_directory = Path()
        self.bin_directory = Path()
        self.config_path = Path()
        self.result_path = Path()
        self.environment: dict[str, str] = {}
        self.config: dict[str, str] = {}
        self.fixture: CommandFixture = {
            "probe_status": 0,
            "probe_error": "",
            "probe_delay_s": 0.0,
            "probe_lines": [],
            "rsync_status": 0,
            "hold_copy": False,
            "run_lines": [],
            "run_status_reported": True,
            "run_status_line": "mac_exit=0",
            "run_cleanup_line": "mac_cleanup=0",
            "run_ssh_status": 0,
            "run_delay_s": 0.0,
            "hold_after_first_line": False,
            "cleanup_status": 0,
            "cleanup_delay_s": 0.0,
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
        self.write_executable(stub, COMMAND_STUB)
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
            "probe_delay_s": 0.0,
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
            "run_delay_s": 0.0,
            "hold_after_first_line": False,
            "cleanup_status": 0,
            "cleanup_delay_s": 0.0,
            "hold_cleanup": False,
            "cleanup_lines": ["leftovers=0", "rc=0"],
            "cleanup_error": "",
            "selector_shell": "",
            "selector_pgrep_status": 1,
            "selector_lsof_status": 0,
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
        _ = (self.stub_directory / "fixture.json").write_text(
            json.dumps(self.fixture), encoding="utf-8"
        )

    def command(
        self,
        *nextest_arguments: str,
        repository: Path | None = None,
        filter_run: bool = False,
        timeout: float = 300,
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
            text=True, check=False, timeout=30,
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
        self.fixture["cleanup_delay_s"] = 0
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
        deadline = time.monotonic() + 300
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
        deadline = time.monotonic() + 300
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
        original_launch = subprocess.Popen
        original_communicate = cast(Callable[[subprocess.Popen[str], str | None, float | None], tuple[str, str]],
                                    original_launch.communicate)

        def communicate(process: subprocess.Popen[str], input: str | None = None,
                        timeout: float | None = None) -> tuple[str, str]:
            if timeout == 0.1:
                self.wait_for_path(self.stub_directory / "probe-started")
            return original_communicate(process, input, timeout)

        with mock.patch.object(original_launch, "communicate", communicate):
            result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertRegex(result.stderr, r"^mac_test: could not reach the Mac: .*0\.1.*\n$")
        self.assertRegex(result.stderr, r"(?i)tim(?:e|ed).*out|limit|finish")
        self.assertTrue((self.stub_directory / "probe-started").exists())
        self.assertEqual(len(self.events_for("ssh")), 1)

    def test_probe_without_final_status_declines_as_unreachable(self) -> None:
        self.fixture["probe_lines"] = self.fixture["probe_lines"][:-1]
        self.sync_fixture()
        result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertEqual(result.stderr, "mac_test: could not reach the Mac: the probe answer could not be read\n")
        self.assertEqual(len(self.events_for("ssh")), 1)

    def test_failed_probe_prints_collapsed_ssh_error_before_unreachable_decline(self) -> None:
        self.fixture["probe_status"] = 255
        self.fixture["probe_error"] = "ssh:\tconnection refused\n  host mac is unavailable  "
        self.sync_fixture()
        result = self.command(separate_stderr=True)
        self.assert_declined(result, "unreachable")
        self.assertEqual(result.stderr, "mac_test: could not reach the Mac: ssh: connection refused host mac is unavailable\n")
        self.assertEqual(self.events_for("rsync"), [])
        self.assertTrue((self.state_directory / "unreachable_until").exists())

    def test_other_build_processes_do_not_decline_mac_tests(self) -> None:
        self.fixture["probe_lines"] = [
            "procs=1",
            "free_gib=120",
            "host=Mac",
            "rc=0",
        ]
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertEqual(len(self.events_for("ssh")), 2)
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_run_settings_selects_alternate_host_and_budget_over_environment_default(self) -> None:
        alternate = self.root / "alternate settings.conf"
        values = {**self.config, "host": "alternate-mac", "mac_budget_gib.hana": "8"}
        _ = alternate.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        self.config["offload"] = "off"
        self.sync_config()
        result = self.command(settings=alternate)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        ssh = self.events_for("ssh")
        self.assertEqual(ssh[0]["args"][-2], "alternate-mac")
        self.assertEqual(ssh[1]["args"][-2], "alternate-mac")
        self.assertIn("LINT_SWEEP_BUDGET_GIB=8", ssh[1]["args"][-1])
        remote_words = shlex.split(ssh[1]["args"][-1].rpartition("; echo mac_exit=$?")[0])
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertEqual(remote_words[lint_index + 1 :], ["nextest"])
        self.assertIn("alternate-mac:.local/state/mac-test/mirror/hana/", self.events_for("rsync")[0]["args"])

    def test_low_disk_declines_after_probe(self) -> None:
        self.fixture["probe_lines"] = ["free_gib=59", "host=Mac", "rc=0"]
        self.sync_fixture()
        self.assert_declined(self.command(), "disk")
        self.assertEqual(self.events_for("rsync"), [])

    def test_battery_power_does_not_decline_mac_tests(self) -> None:
        self.assertNotIn("pmset", offload.probe_script(offload.KnownRepository("hana")))
        self.fixture["probe_lines"] = ["power=battery", "free_gib=120", "host=Mac", "rc=0"]
        self.sync_fixture()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")

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
        self.assertTrue(busy.stdout.endswith("mac_test: not running on the Mac (busy)\n"), busy.stdout)
        self.assertIn("busy", busy.stdout)
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "other tests")
        self.assertTrue(self.claim_file.exists())
        self.assertEqual(self.events(), [])
        released = self.state_command("release", "--pid", str(os.getpid()))
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)

        _ = (self.state_directory / "block.json").write_text(
            "damaged\n", encoding="utf-8"
        )
        unreadable = self.command()
        self.assert_declined(unreadable, "state")
        self.assertIn("state unreadable", unreadable.stdout)
        self.assertIn("block.json", unreadable.stdout)
        self.assertEqual(self.events(), [])

    def test_retained_expired_block_still_declines_and_prints_holder_diagnostic(self) -> None:
        self.state_directory.mkdir()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": (now - timedelta(hours=2)).isoformat(),
                 "expires": (now - timedelta(seconds=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active",
                 "ci": "off_by_this_block", "free_message": {"kind": "not_needed"}}
        path = self.state_directory / "block.json"
        _ = path.write_text(json.dumps(block))
        before = path.read_bytes()
        result = self.command()
        self.assert_declined(result, "blocked")
        self.assertIn("blocked by alice: restore CI first", result.stdout)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.events(), [])

    def test_repeated_busy_decline_is_silent_and_preserves_wire_result(self) -> None:
        claimed = self.state_command("claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(self.repository))
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        first = self.command(separate_stderr=True)
        first_wire = self.wire_result()
        self.assertEqual(first.stdout, "mac_test: not running on the Mac (busy)\n")
        self.assertTrue(first.stderr)
        repeated = self.command(last_decline="busy", separate_stderr=True)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (75, "", ""))
        self.assertEqual(repeated.returncode, first.returncode)
        self.assertEqual(self.wire_result()["mac"], first_wire["mac"])
        self.assertEqual(self.wire_result()["reason"], first_wire["reason"])
        self.assertEqual(self.wire_result().get("holder"), "other tests")
        self.assertEqual(self.events(), [])
        self.assertTrue(self.claim_file.exists())

    def test_changed_decline_prints_normally_and_repeated_blocked_decline_is_silent(self) -> None:
        self.state_directory.mkdir()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": now.isoformat(), "expires": (now + timedelta(hours=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active",
                 "ci": "not_configured", "free_message": {"kind": "not_needed"}}
        _ = (self.state_directory / "block.json").write_text(json.dumps(block))
        first = self.command(separate_stderr=True)
        changed = self.command(last_decline="busy", separate_stderr=True)
        self.assertEqual((changed.returncode, changed.stdout, changed.stderr),
                         (first.returncode, first.stdout, first.stderr))
        self.assertEqual(changed.stdout, "mac_test: not running on the Mac (blocked)\n")
        self.assertIn("blocked by alice: restore CI first", changed.stderr)
        self.assert_wire("declined", "blocked")
        repeated = self.command(last_decline="blocked", separate_stderr=True)
        self.assertEqual((repeated.returncode, repeated.stdout, repeated.stderr), (75, "", ""))
        self.assert_wire("declined", "blocked")
        self.assertEqual(self.events(), [])

    def test_mac_run_line_waits_for_claim_and_tree_copy(self) -> None:
        self.fixture["hold_copy"] = True
        self.sync_fixture()
        process = self.start_command(script=self.claim_stub_runner())
        try:
            self.wait_for_path(self.stub_directory / "claim-started")
            output = process.stdout
            assert output is not None
            self.assertEqual(select.select([output], [], [], 0)[0], [])
            self.assertEqual(self.events(), [])
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            self.wait_for_path(self.stub_directory / "copy-started")
            self.assertEqual(select.select([output], [], [], 0)[0], [])
            self.assertEqual([event["command"] for event in self.events()], ["ssh", "rsync"])
            self.assert_claimed_commands()
            _ = (self.stub_directory / "continue-copy").write_text("continue\n")
            text, _ = process.communicate(timeout=300)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=300)
        self.assertEqual(process.returncode, 0, text)
        self.assertEqual(text.count("mac_test: running on the Mac (macOS, host Mac); tree copied in "), 1)
        self.assert_wire("passed", "")

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
            output, _ = process.communicate(timeout=300)
        finally:
            _ = (self.stub_directory / "finish-claim").write_text("continue\n")
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=300)
        self.assertEqual(process.returncode, 128 + signal_number, output)
        self.assertFalse(self.result_path.exists())
        self.assertFalse((self.state_directory / "unreachable_until").exists())
        return output

    def wait_for_ignored_signals(self, process: subprocess.Popen[str]) -> None:
        expected = sum(1 << (number - 1) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP))
        deadline = time.monotonic() + 300
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

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_time_out(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("timeout")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_fail(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("ssh_error")

    def test_signal_during_fresh_claim_releases_without_ssh_when_check_would_omit_status(self) -> None:
        self.assert_fresh_claim_signal_releases_without_ssh("missing_status")

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

    def test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_check_fails(self) -> None:
        self.assert_takeover_signal_checks_reply_host("ssh_error")

    def test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_leftovers_remain(self) -> None:
        self.assert_takeover_signal_checks_reply_host("remaining")

    def test_signal_during_takeover_checks_reply_host_and_releases_claim_when_check_is_clean(self) -> None:
        self.assert_takeover_signal_checks_reply_host("clean")

    def test_signal_during_fresh_claim_from_process_group_preserves_reply_and_releases_without_ssh(self) -> None:
        self.set_leftover_answer("ssh_error")
        output = self.interrupt_held_claim(signal_group=True, signal_number=signal.SIGINT)
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(),
                         ["claim", "release"], output)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events_for("ssh"), [])
        self.assertFalse((self.stub_directory / "cleanup-called").exists())

    def test_signal_during_second_fresh_claim_releases_without_another_leftover_check(self) -> None:
        output = self.interrupt_held_claim(after_takeover=True)
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(),
                         ["claim", "release", "claim", "release"], output)
        self.assertFalse(self.claim_file.exists())
        ssh = self.events_for("ssh")
        self.assertEqual(len(ssh), 1)
        self.assertEqual(ssh[0]["args"][-2], "previous-mac")

    def test_signal_during_takeover_with_no_reply_host_keeps_claim_without_ssh(self) -> None:
        output = self.interrupt_held_claim(takeover=True, reply_host="")
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(), ["claim"])
        self.assert_claim_is_kept("previous-mac")
        self.assert_leftover_diagnostic(output, "missing_status")
        self.assertIn("the kept claim's host could not be read", output)
        self.assertEqual(self.events_for("ssh"), [])
        self.assertFalse((self.stub_directory / "cleanup-called").exists())

    def test_copy_failure_releases_claim(self) -> None:
        self.fixture["rsync_status"] = 23
        self.sync_fixture()
        self.assert_declined(self.command(), "copy")
        self.assertEqual(len(self.events_for("ssh")), 1)
        self.assertEqual(len(self.events_for("rsync")), 1)
        self.assert_claimed_commands()

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
            output, _ = process.communicate(timeout=300)
            self.assertEqual(process.returncode, 0, output)
            self.assert_wire("passed", "")
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=300)

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

    def test_unchanged_copy_sends_its_known_tree_key_beside_the_call_id(self) -> None:
        key = self.commit_sent_tree()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(treekey.tree_key(str(self.repository)), key)
        words = self.sent_command_words()
        call_index = words.index("BUILDLOG_CALL_ID=call-17")
        self.assertEqual(words[call_index + 1], f"BUILDLOG_SENT_TREE_KEY={key}")

    def test_copy_with_different_before_and_after_tree_keys_sends_no_key(self) -> None:
        before = self.commit_sent_tree()

        def change() -> None:
            _ = (self.repository / "during-copy.rs").write_text("changed during copy\n")

        self.run_with_copy_change(change)
        after = treekey.tree_key(str(self.repository))
        self.assertIsInstance(after, str)
        self.assertNotEqual(before, after)
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_copy_with_unavailable_before_key_and_known_after_key_sends_no_key(self) -> None:
        self.assertIsNone(treekey.tree_key(str(self.repository)))

        def change() -> None:
            _ = self.commit_sent_tree()

        self.run_with_copy_change(change)
        self.assertIsInstance(treekey.tree_key(str(self.repository)), str)
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def test_copy_with_known_before_key_and_unavailable_after_key_sends_no_key(self) -> None:
        _ = self.commit_sent_tree()
        head_path = self.repository / ".git" / "HEAD"
        head = head_path.read_text()

        def change() -> None:
            (self.repository / ".git" / "HEAD").unlink()

        self.run_with_copy_change(change)
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        _ = head_path.write_text(head)
        self.assert_stable_copy_sends_key()

    def test_copy_with_both_tree_keys_unavailable_sends_no_key(self) -> None:
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(treekey.tree_key(str(self.repository)))
        self.assertFalse(any(word.startswith("BUILDLOG_SENT_TREE_KEY=") for word in self.sent_command_words()))
        self.assert_stable_copy_sends_key()

    def write_portable_workspace(self) -> None:
        _ = (self.repository / "Cargo.toml").write_text('[workspace]\nmembers=["portable"]\n')
        member = self.repository / "portable"
        member.mkdir(exist_ok=True)
        _ = (member / "Cargo.toml").write_text('[package]\nname="portable"\nversion="0.1.0"\n')

    def test_each_mac_verb_keeps_arguments_and_names_its_claim(self) -> None:
        self.write_portable_workspace()
        self.config["mac_skip.hana"] = "test(skip-this)"
        self.sync_config()
        for verb in ("nextest", "check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                words = ("--workspace", "--all-features")
                result = self.command(*words, verb=verb)
                self.assertEqual(result.returncode, 0, result.stdout)
                sent = shlex.split(self.events_for("ssh")[-1]["args"][-1].rpartition("; echo mac_exit=$?")[0])
                position = sent.index("~/.claude/scripts/lint/lint")
                self.assertEqual(sent[position + 1:], [verb, *words])
                if verb == "doc":
                    self.assertIn("LINT_CONFIG_FORCE=1", sent[:position])
                # The local claim is read by rsync before remote execution.
                held = self.events_for("rsync")[-1]
                self.assertTrue(held["claimed"])
                self.assertEqual(held.get("what"), f"{verb} hana")

    def test_no_package_names_the_mac_claim_for_the_workspace(self) -> None:
        self.write_portable_workspace()
        result = self.command("--workspace", verb="check", package="")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.events_for("rsync")[-1].get("what"), "check workspace")

    def test_exit_four_is_no_tests_only_for_nextest(self) -> None:
        self.write_portable_workspace()
        self.fixture["run_status_line"] = "mac_exit=4"
        self.sync_fixture()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                result = self.command("--workspace", verb=verb)
                self.assertEqual(result.returncode, 4, result.stdout)
                self.assertIn("fix the finding in the local worktree", result.stdout)
                self.assertNotIn("only a test", result.stdout)
                self.assert_wire("failed", "")
        result = self.command("--workspace")
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("mac_test: no test matched on the Mac; running on natedev instead", result.stdout)
        self.assert_wire("declined", "no_tests")

    def test_non_test_verbs_never_apply_nextest_skip_words_or_start_a_copy(self) -> None:
        self.write_portable_workspace()
        self.config["mac_skip.hana"] = "test(skip-this)"
        self.sync_config()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                result = self.command("-E", "leave-this-argument", verb=verb)
                self.assertEqual(result.returncode, 0, result.stdout)
                remote = self.events_for("ssh")[-1]["args"][-1]
                self.assertIn("-E leave-this-argument", remote)
                self.assertNotIn("skip-this", remote)
                self.assertFalse(self.events_for("rsync")[-1].get("copy_claim"))
                for option in ("copy", "filter"):
                    with self.subTest(option=option):
                        result = self.command(verb=verb, copy=option == "copy", filter_run=option == "filter")
                        self.assertEqual(result.returncode, 2, result.stdout)

    def test_linux_only_workspace_member_declines_every_non_test_verb_before_claim(self) -> None:
        self.write_portable_workspace()
        self.config["linux_only.hana"] = "portable"
        self.sync_config()
        for verb in ("check", "clippy", "mend-check", "doc"):
            with self.subTest(verb=verb):
                self.assert_declined(self.command("--workspace", verb=verb), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_implicit_linux_member_blocks_workspace_verbs_through_target_and_shared_dependencies(self) -> None:
        for section in ("dependencies", "dev-dependencies", "build-dependencies", 'target.\'cfg(unix)\'.dependencies'):
            with self.subTest(section=section):
                self.write_portable_workspace()
                _ = (self.repository / "Cargo.toml").write_text(
                    '[workspace]\nmembers=["portable"]\n[workspace.dependencies]\nimplicit={path="implicit"}\n')
                member = self.repository / "implicit"
                member.mkdir(exist_ok=True)
                _ = (member / "Cargo.toml").write_text('[package]\nname="linux-part"\nversion="0.1.0"\n')
                with (self.repository / "portable/Cargo.toml").open("a") as output:
                    _ = output.write(f'[{section}]\nimplicit={{workspace=true}}\n')
                self.config["linux_only.hana"] = "linux-part"
                self.sync_config()
                for verb in ("check", "clippy", "mend-check", "doc"):
                    self.assert_declined(self.command("--workspace", verb=verb), "linux_only")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_unreadable_implicit_membership_refuses_workspace_before_claim(self) -> None:
        self.write_portable_workspace()
        with (self.repository / "portable/Cargo.toml").open("a") as output:
            _ = output.write('[dependencies]\nmissing={path="../missing"}\n')
        for verb in ("check", "clippy", "mend-check", "doc"):
            self.assert_declined(self.command("--workspace", verb=verb), "repo")
        self.assertEqual(self.events_for("ssh"), [])
        self.assertEqual(self.events_for("rsync"), [])

    def test_doc_copies_the_supplied_snapshot_without_working_tree_ignores(self) -> None:
        self.write_portable_workspace()
        snapshot = self.root / "staged-doc"
        snapshot.mkdir()
        _ = (snapshot / "Cargo.toml").write_text('[package]\nname="staged"\nversion="0.1.0"\n')
        _ = (self.repository / ".gitignore").write_text("staged-file.rs\n")
        _ = (snapshot / "staged-file.rs").write_text("staged source")
        result = self.command("--workspace", verb="doc", source=offload.StagedDocSnapshot(snapshot))
        self.assertEqual(result.returncode, 0, result.stdout)
        copied, = self.events_for("rsync")
        self.assertIn(str(snapshot) + "/", copied["args"])
        self.assertNotIn(str(self.repository) + "/", copied["args"])
        self.assertNotIn("/staged-file.rs", copied.get("exclude", ""))
        self.config["linux_only.hana"] = "staged"
        self.sync_config()
        self.assert_declined(self.command("--workspace", verb="doc", source=offload.StagedDocSnapshot(snapshot)), "linux_only")
        self.assertEqual(len(self.events_for("rsync")), 1)

    def test_remote_test_command_sets_development_deployment_target_before_nextest(self) -> None:
        words = ("--no-fail-fast", "unit_case")
        result = self.command(*words)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        remote = self.events_for("ssh")[-1]["args"][-1]
        remote_words = shlex.split(remote.rpartition("; echo mac_exit=$?")[0])
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
        self.assertEqual(remote_words[lint_index + 1 :], ["nextest", *words])

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
        self.assertFalse(self.claim_file.exists())
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
            'df -g "$HOME"',
            "hostname -s",
        ):
            self.assertIn(fragment, probe_script)
        self.assertNotIn("loadavg", probe_script)
        self.assertNotIn("pgrep", probe_script)
        self.assertNotIn("procs", probe_script)
        self.assertNotIn("pmset", probe_script)

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
        self.assertIn("mac_cleanup=", status_command)
        remote_words = shlex.split(run_command)
        lint_index = remote_words.index("~/.claude/scripts/lint/lint")
        self.assertEqual(
            remote_words[lint_index :],
            ["~/.claude/scripts/lint/lint", "nextest", *words],
        )
        self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0", remote_words)
        self.assertLess(remote_words.index("MACOSX_DEPLOYMENT_TARGET=14.0"), lint_index)
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
        self.assertIn("mac_cleanup=", status_command)
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
        self.assertFalse(self.claim_file.exists())

    def test_copy_claims_as_a_copy_and_names_the_copy(self) -> None:
        result = self.command("--workspace", copy=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stdout.startswith(
            "mac_test: running a copy on the Mac while natedev compiles (host Mac); tree copied in "), result.stdout)
        self.assertNotIn("mac_test: running on the Mac", result.stdout)
        self.assert_wire("passed", "")
        held = [event for event in self.events() if event["command"] in ("ssh", "rsync")]
        self.assertTrue(held)
        self.assertTrue(all(event.get("copy_claim") is True for event in held), held)
        self.assertFalse(self.claim_file.exists())
        self.clear_events()
        self.assertEqual(self.command("--workspace").returncode, 0)
        self.assertTrue(all(event.get("copy_claim") is False for event in self.events_for("rsync")))

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
            remaining_output, _ = process.communicate(timeout=300)
        except BaseException:
            process.send_signal(signal.SIGTERM)
            _ = process.communicate(timeout=300)
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
        self.assertIn(
            "only a test that must run on Linux may rerun with --local",
            result.stdout,
        )
        self.assertNotIn("unrelated to your change", result.stdout)
        self.assertNotIn("mac_exit=42", result.stdout)
        self.assert_wire("failed", "")
        self.assertFalse(self.claim_file.exists())

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
        self.assertFalse(self.claim_file.exists())

    def test_nextest_failure_status_remains_a_mac_failure(self) -> None:
        self.fixture["run_status_line"] = "mac_exit=1"
        self.sync_fixture()

        result = self.command()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assert_wire("failed", "")

    def test_cleanup_success_keeps_the_reported_test_result_and_hides_markers(self) -> None:
        for status in (0, 1, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertNotIn("mac_exit=", result.stdout)
                self.assertNotIn("mac_cleanup=", result.stdout)
                self.assertNotIn("Mac build output", result.stdout)

    def test_cleanup_failure_reports_once_without_changing_test_result(self) -> None:
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.fixture["run_cleanup_line"] = "mac_cleanup=7"
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(result.stdout.count("Mac build output not removed: 7\n"), 1)
                self.assertNotIn("mac_cleanup=", result.stdout)

    def test_kept_build_output_releases_claim_without_leftover_check_or_notice(self) -> None:
        self.fixture["run_cleanup_line"] = "mac_cleanup=kept"
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(self.leftover_events(), [])
                self.assertFalse(self.claim_file.exists())
                self.assertNotIn("Mac build output", result.stdout)
                self.assertNotIn("mac_cleanup=", result.stdout)

    def test_other_target_dir_reports_kept_output_without_changing_test_result(self) -> None:
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.fixture["run_cleanup_line"] = "mac_cleanup=skipped"
                self.sync_fixture()
                result = self.command("--target-dir", "other target")
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(result.stdout.count("Mac build output kept: the run used another target dir\n"), 1)
                self.assertNotIn("mac_cleanup=", result.stdout)

    def test_disconnect_after_test_status_reports_unconfirmed_cleanup_and_keeps_result(self) -> None:
        for status in (0, 42):
            with self.subTest(status=status):
                self.fixture["run_status_line"] = f"mac_exit={status}"
                self.fixture["run_cleanup_line"] = ""
                self.fixture["run_ssh_status"] = 255
                self.sync_fixture()
                result = self.command()
                self.assertEqual(result.returncode, status, result.stdout)
                self.assert_wire("passed" if status == 0 else "failed", "")
                self.assertEqual(result.stdout.count("Mac build output cleanup unconfirmed\n"), 1)
                self.assertNotIn("mac_exit=", result.stdout)

    def test_unconfirmed_cleanup_checks_leftovers_before_release_and_keeps_test_result(self) -> None:
        self.fixture["run_cleanup_line"] = ""
        self.fixture["run_ssh_status"] = 255
        for status in (0, 42):
            for outcome in ("clean", "remaining", "missing_status", "ssh_error"):
                with self.subTest(status=status, outcome=outcome):
                    self.reset_leftover_case()
                    self.fixture["run_status_line"] = f"mac_exit={status}"
                    self.set_leftover_answer(outcome)
                    result = self.process_command(separate_stderr=True)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assert_wire("passed" if status == 0 else "failed", "")
                    self.assertEqual(result.stdout.count("Mac build output cleanup unconfirmed\n"), 1)
                    checks = self.leftover_events()
                    self.assertEqual(len(checks), 1)
                    self.assertTrue(checks[0]["claimed"], checks[0])
                    self.assert_leftover_selectors(checks[0]["args"][-1])
                    if outcome == "clean":
                        self.assertFalse(self.claim_file.exists())
                    else:
                        self.assert_claim_is_kept()
                        self.assert_leftover_diagnostic(result.stderr, outcome)

    def test_lost_run_time_includes_remote_cleanup(self) -> None:
        self.fixture["run_lines"] = [
            {"stream": "stdout", "text": "partial output"}
        ]
        self.fixture["run_status_reported"] = False
        self.sync_fixture()
        result = self.command_with_cleanup_clock()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("partial output\n", result.stdout)
        self.assertIn(
            "mac_test: the link to the Mac dropped; running on natedev instead\n",
            result.stdout,
        )
        wire = self.wire_result()
        self.assertEqual(wire["mac"], "lost")
        self.assertEqual(wire["reason"], "")
        recorded_seconds = float(wire["seconds"])
        self.assertEqual(recorded_seconds, 8.0)
        self.assertFalse(self.claim_file.exists())
        cleanup_events = [
            event
            for event in self.events_for("ssh")
            if "select_leftovers()" in event["args"][-1]
        ]
        self.assertEqual(len(cleanup_events), 1, self.events())
        cleanup = cleanup_events[0]
        self.assertTrue(cleanup["claimed"], cleanup)
        self.assert_leftover_selectors(cleanup["args"][-1])

    def test_lost_run_keeps_claim_when_leftover_check_does_not_prove_completion(self) -> None:
        self.fixture["run_status_reported"] = False
        for outcome in ("remaining", "missing_status", "timeout", "ssh_error"):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                self.set_leftover_answer(outcome)
                result = self.command_with_cleanup_clock(separate_stderr=True)
                self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                self.assert_wire("lost", "")
                self.assertIn("mac_test: the link to the Mac dropped; running on natedev instead\n", result.stdout)
                self.assert_leftover_diagnostic(result.stderr, outcome)
                self.assert_claim_is_kept()
                self.assertEqual(len(self.leftover_events()), 1)
                self.assert_leftover_selectors(self.leftover_events()[0]["args"][-1])
                self.assertEqual(float(self.wire_result()["seconds"]), 13.0 if outcome == "timeout" else 8.0)

    def test_signal_keeps_claim_when_leftover_check_does_not_prove_completion(self) -> None:
        self.fixture["run_delay_s"] = 30
        for outcome in ("remaining", "missing_status", "timeout", "ssh_error"):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                self.set_leftover_answer(outcome)
                process = self.start_command("--workspace")
                try:
                    self.wait_for_path(self.stub_directory / "run-started")
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=300)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate(timeout=300)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                self.assert_leftover_diagnostic(output, outcome)
                self.assert_claim_is_kept()
                self.assertEqual(len(self.leftover_events()), 1)

    def test_signal_before_remote_command_starts_releases_without_leftover_check(self) -> None:
        for stage in ("probe", "copy"):
            with self.subTest(stage=stage):
                self.reset_leftover_case()
                (self.stub_directory / f"{stage}-started").unlink(missing_ok=True)
                self.fixture["probe_delay_s"] = 30 if stage == "probe" else 0
                self.fixture["hold_copy"] = stage == "copy"
                self.sync_fixture()
                process = self.start_command()
                try:
                    self.wait_for_path(self.stub_directory / f"{stage}-started")
                    process.send_signal(signal.SIGTERM)
                    output, _ = process.communicate(timeout=300)
                finally:
                    if process.poll() is None:
                        process.kill()
                        _ = process.communicate(timeout=300)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
                self.assertFalse(self.claim_file.exists())
                self.assertEqual(self.leftover_events(), [])

    def test_takeover_ends_leftovers_on_recorded_host_then_runs_on_settings_host(self) -> None:
        _ = self.write_kept_claim()
        alternate = self.root / "alternate-settings.conf"
        values = {**self.config, "host": "next-mac", "connect_timeout_s": "2"}
        _ = alternate.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
        result = self.command("--no-fail-fast", settings=alternate)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_wire("passed", "")
        self.assertFalse(self.claim_file.exists())
        events = self.events()
        self.assertEqual([event["command"] for event in events], ["ssh", "ssh", "rsync", "ssh"])
        cleanup = events[0]
        self.assertEqual(cleanup["args"][-2], "previous-mac")
        self.assertIn("ConnectTimeout=2", cleanup["args"])
        self.assert_leftover_selectors(cleanup["args"][-1])
        self.assertTrue(cleanup["claimed"])
        for event in self.events_for("ssh")[1:]:
            self.assertEqual(event["args"][-2], "next-mac")
        self.assert_claimed_commands()

    def test_takeover_with_remaining_leftovers_declines_busy_and_preserves_job(self) -> None:
        old = self.write_kept_claim()
        self.set_leftover_answer("remaining")
        result = self.process_command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (busy)\n")
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "leftovers")
        self.assert_leftover_diagnostic(result.stderr, "remaining")
        self.assert_claim_is_kept("previous-mac")
        self.assertEqual(self.remote_claim()["what"], old["what"])
        self.assertEqual(self.remote_claim()["since"], old["since"])
        self.assertEqual(self.remote_claim()["worktree"], old["worktree"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.leftover_events()[0]["args"][-2], "previous-mac")

    def test_second_takeover_declines_busy_without_releasing_the_new_kept_claim(self) -> None:
        process = self.start_command(script=self.claim_stub_runner(), extra={"MAC_TEST_TAKEOVER_TWICE": "1"})
        try:
            output, _ = process.communicate(timeout=300)
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.communicate(timeout=300)
        self.assertEqual(process.returncode, 75, output)
        self.assert_wire("declined", "busy")
        self.assertEqual(self.wire_result().get("holder"), "leftovers")
        self.assertTrue(self.claim_file.exists())
        self.assertEqual((self.stub_directory / "state-events").read_text().splitlines(), ["claim", "release", "claim"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.leftover_events()[0]["args"][-2], "previous-mac")

    def test_takeover_check_failure_declines_unreachable_keeps_claim_and_sets_backoff(self) -> None:
        _ = self.write_kept_claim()
        self.set_leftover_answer("ssh_error")
        result = self.process_command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (unreachable)\n")
        self.assert_wire("declined", "unreachable")
        self.assert_leftover_diagnostic(result.stderr, "ssh_error")
        self.assert_claim_is_kept("previous-mac")
        self.assertTrue((self.state_directory / "unreachable_until").exists())
        self.assertEqual(len(self.events()), 1)
        self.clear_events()
        repeated = self.process_command()
        self.assertEqual(repeated.returncode, 75, repeated.stdout)
        self.assert_wire("declined", "backoff")
        self.assertEqual(self.events(), [])
        self.assert_claim_is_kept("previous-mac")

    def test_takeover_under_block_ends_leftovers_before_declining_blocked(self) -> None:
        _ = self.write_kept_claim()
        now = datetime.now(timezone.utc)
        block = {"version": 2, "holder": "alice", "for": "restore CI first",
                 "since": now.isoformat(), "expires": (now + timedelta(hours=1)).isoformat(),
                 "session": None, "showrunner": None, "state": "active", "ci": "not_configured",
                 "free_message": {"kind": "not_needed"}}
        path = self.state_directory / "block.json"
        _ = path.write_text(json.dumps(block))
        before = path.read_bytes()
        result = self.command(separate_stderr=True)
        self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "mac_test: not running on the Mac (blocked)\n")
        self.assert_wire("declined", "blocked")
        self.assertIn("blocked by alice: restore CI first", result.stderr)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.events_for("ssh")), 1)
        self.assertEqual(self.events_for("rsync"), [])
        self.assertEqual(self.leftover_events()[0]["args"][-2], "previous-mac")

    def test_repeated_takeover_decline_suppresses_leftover_diagnostic(self) -> None:
        for outcome, previous_reason in (("remaining", "busy"), ("missing_status", "blocked")):
            with self.subTest(outcome=outcome):
                self.reset_leftover_case()
                _ = self.write_kept_claim()
                self.set_leftover_answer(outcome)
                result = self.process_command(last_decline=previous_reason, separate_stderr=True)
                self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                reason = "busy" if outcome == "remaining" else "unreachable"
                self.assert_wire("declined", reason)
                if reason == previous_reason:
                    self.assertEqual((result.stdout, result.stderr), ("", ""))
                else:
                    self.assert_leftover_diagnostic(result.stderr, outcome)
                self.assert_claim_is_kept("previous-mac")

    def test_leftover_selectors_complete_under_sh_and_zsh_before_releasing_claim(self) -> None:
        selectors = self.stub_directory / "selectors"
        selectors.mkdir()
        for name in ("pgrep", "lsof", "sleep"):
            self.write_executable(selectors / name, SELECTOR_STUB)
        self.fixture["run_status_reported"] = False
        self.config["leftover_timeout_s"] = "300"
        self.sync_config()
        for shell in ("sh", "zsh"):
            for pgrep_status, lsof_status in ((1, 1), (1, 0), (2, 0)):
                with self.subTest(shell=shell, pgrep_status=pgrep_status, lsof_status=lsof_status):
                    self.reset_leftover_case()
                    self.fixture["selector_shell"] = shell
                    self.fixture["selector_pgrep_status"] = pgrep_status
                    self.fixture["selector_lsof_status"] = lsof_status
                    self.sync_fixture()
                    result = self.process_command(separate_stderr=True)
                    self.assertEqual(result.returncode, 75, result.stdout + result.stderr)
                    self.assert_wire("lost", "")
                    cleanup = self.leftover_events()
                    self.assertEqual(len(cleanup), 1)
                    self.assert_leftover_selectors(cleanup[0]["args"][-1])
                    path = self.stub_directory / "selector-events.jsonl"
                    calls = [cast(SelectorEvent, json.loads(line)) for line in path.read_text().splitlines()]
                    names = [call["command"] for call in calls]
                    self.assertIn("pgrep", names)
                    if pgrep_status == 1:
                        self.assertIn("lsof", names)
                    for call in calls:
                        if call["command"] == "pgrep":
                            self.assertEqual(call["args"], ["-u", str(os.getuid()), "-f", HANA_COMMAND_PATTERN])
                        if call["command"] == "lsof":
                            self.assertEqual(call["args"], ["-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"])
                    answer = (self.stub_directory / "selector-answer").read_text()
                    if pgrep_status == 1 and lsof_status == 0:
                        self.assertIn("sleep", names)
                        self.assertEqual(answer.splitlines(), ["leftovers=0", "rc=0"])
                        self.assertFalse(self.claim_file.exists())
                    else:
                        self.assertNotIn("rc=0", answer.splitlines())
                        self.assert_claim_is_kept()
                        self.assert_leftover_diagnostic(result.stderr, "missing_status")

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
                    output, _ = process.communicate(timeout=300)
                except BaseException:
                    process.kill()
                    _ = process.communicate(timeout=300)
                    raise
                self.assertEqual(process.returncode, 128 + signal_number, output)
                self.assertTrue(
                    (self.stub_directory / "cleanup-called").exists()
                )
                self.assertFalse(self.claim_file.exists())
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
                self.assert_leftover_selectors(cleanup)
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
            output, _ = process.communicate(timeout=300)
        except BaseException:
            process.kill()
            _ = (self.stub_directory / "continue-cleanup").write_text(
                "continue\n", encoding="utf-8"
            )
            _ = process.communicate(timeout=300)
            raise

        self.assertEqual(process.returncode, 128 + signal.SIGTERM, output)
        self.assertTrue((self.stub_directory / "cleanup-called").exists())
        self.assertFalse(self.claim_file.exists())

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
            output, _ = process.communicate(timeout=300)
        except BaseException:
            process.kill()
            _ = gate.write_text("continue\n", encoding="utf-8")
            _ = process.communicate(timeout=300)
            raise

        self.assertEqual(process.returncode, -signal.SIGINT, output)
        self.assertFalse(self.claim_file.exists())
        self.assertEqual(self.events(), [])
        self.assertFalse(self.result_path.exists())

    def test_unwritable_result_destination_returns_internal_error(self) -> None:
        self.config["offload"] = "off"
        self.sync_config()
        self.result_path = self.root / "missing" / "result"
        result = self.command()
        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("mac_test: not running on the Mac (off)\n", result.stdout)
        self.assertIn("FileNotFoundError", result.stdout)
        self.assertFalse(self.result_path.exists())


class MacCleanupProtocolTests(unittest.TestCase):
    def test_status_marker_followed_by_ordinary_output_is_printed_and_not_a_result(self) -> None:
        output = io.StringIO()
        controller = offload.ProcessController()
        text = "mac_exit=0\ntest still running\n"
        with redirect_stdout(output):
            answer = controller.stream_mac_run([
                sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1]); sys.exit(255)", text,
            ])
        self.assertIsInstance(answer, offload.MissingMacStatus)
        self.assertEqual(output.getvalue(), text)

    def test_stream_returns_cleanup_outcome_beside_the_reported_test_status(self) -> None:
        cases = (
            ("mac_cleanup=0\n", 0, offload.CleanupDone()),
            ("mac_cleanup=kept\n", 0, offload.CleanupKept()),
            ("mac_cleanup=7\n", 0, offload.CleanupFailed(7)),
            ("mac_cleanup=skipped\n", 0, offload.CleanupSkipped()),
            ("", 255, offload.CleanupUnconfirmed()),
        )
        for marker, ssh_status, expected in cases:
            with self.subTest(marker=marker, ssh_status=ssh_status):
                output = io.StringIO()
                controller = offload.ProcessController()
                with redirect_stdout(output):
                    answer = controller.stream_mac_run([
                        sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1]); sys.exit(int(sys.argv[2]))",
                        "test output\nmac_exit=42\n" + marker, str(ssh_status),
                    ])
                self.assertEqual(answer, offload.ReportedMacStatus(42, expected))
                self.assertEqual(output.getvalue(), "test output\n")


class MacTargetCleanupShellTests(unittest.TestCase):
    root: Path = Path()
    home_directory: Path = Path()
    mirror: Path = Path()
    temporary_directory: Path = Path()
    bin_directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home_directory = self.root / "home with spaces"
        self.mirror = self.home_directory / offload.remote_mirror(offload.KnownRepository("hana's project"))
        self.mirror.mkdir(parents=True)
        self.temporary_directory = self.root / "tmp"
        self.temporary_directory.mkdir()
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        disk_space = self.bin_directory / "df"
        _ = disk_space.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys

(Path(os.environ["CLEANUP_TEST_DIR"]) / "df-arguments.json").write_text(json.dumps(sys.argv[1:]))
print(os.environ["CLEANUP_TEST_DF_OUTPUT"], end="")
raise SystemExit(int(os.environ["CLEANUP_TEST_DF_STATUS"]))
''')
        disk_space.chmod(0o755)
        lint = self.home_directory / ".claude/scripts/lint/lint"
        lint.parent.mkdir(parents=True)
        _ = lint.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys

root = Path(os.environ["CLEANUP_TEST_DIR"])
(root / "lint-arguments.json").write_text(json.dumps(sys.argv[1:]))
(root / "sweep-switch").write_text(os.environ.get("LINT_SKIP_SWEEP", ""))
target = Path("target")
for index, word in enumerate(sys.argv[1:], 1):
    if word == "--target-dir":
        target = Path(sys.argv[index + 1])
    elif word.startswith("--target-dir="):
        target = Path(word.split("=", 1)[1])
target.mkdir(parents=True, exist_ok=True)
(target / "nextest-output").write_text("built\n")
print("nextest finished", flush=True)
raise SystemExit(int(os.environ["CLEANUP_TEST_STATUS"]))
''')
        lint.chmod(0o755)

    def run_remote(
        self, shell: list[str], status: int, words: tuple[str, ...] = (), *,
        disk_output: str = "Filesystem GiB Used Available Capacity Mounted\nfixture 500 401 99 80% /\n",
        disk_status: int = 0,
        free_floor_gib: float = 100,
    ) -> subprocess.CompletedProcess[str]:
        request = offload.OffloadRequest(self.root, "hana", "cleanup-call", offload.NO_RESULT_FILE, words)
        config = offload.OffloadConfig(
            enabled=True, host="unused-host", connect_timeout_s=6, probe_timeout_s=1,
            unreachable_backoff_s=300, free_floor_gib=free_floor_gib,
            default_target_budget_gib="24", values={},
        )
        text = (
            '[ "$(command -v df)" = ' + shlex.quote(str(self.bin_directory / "df")) + ' ] || exit 99\n'
            + offload.remote_test_command(config, request, offload.KnownRepository("hana's project"))
        )
        return subprocess.run(
            [*shell, "-c", text], cwd=self.home_directory,
            env={**os.environ, "HOME": str(self.home_directory), "TMPDIR": str(self.temporary_directory),
                 "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                 "CLEANUP_TEST_DIR": str(self.root), "CLEANUP_TEST_STATUS": str(status),
                 "CLEANUP_TEST_DF_OUTPUT": disk_output, "CLEANUP_TEST_DF_STATUS": str(disk_status)},
            capture_output=True, text=True, check=False, timeout=5,
        )

    def test_remote_text_below_space_floor_removes_only_repository_target_after_pass_and_failure(self) -> None:
        target = self.mirror / "target"
        sibling = self.mirror / "keep-source"
        temporary_file = self.temporary_directory / "keep-temporary"
        other_target = self.mirror.parent / "other-repository" / "target" / "keep-build"
        other_target.parent.mkdir(parents=True)
        for path in (sibling, temporary_file, other_target):
            _ = path.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                with self.subTest(shell=shell, status=status):
                    target.mkdir(exist_ok=True)
                    _ = (target / "previous-build").write_text("old\n")
                    answer = self.run_remote(shell, status)
                    self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                    self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=0"])
                    self.assertEqual(answer.stderr, "")
                    self.assertFalse(target.exists())
                    self.assertEqual((self.root / "sweep-switch").read_text(), "1")
                    self.assertEqual(
                        cast(list[str], json.loads((self.root / "df-arguments.json").read_text())),
                        ["-g", str(self.home_directory)],
                    )
                    for path in (sibling, temporary_file, other_target):
                        self.assertEqual(path.read_text(), "keep\n")

    def test_remote_text_at_or_above_space_floor_keeps_build_output(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for free_space, floor in ((100, 100), (101, 100), (50, 50)):
                    with self.subTest(shell=shell, status=status, free_space=free_space, floor=floor):
                        answer = self.run_remote(
                            shell, status, free_floor_gib=floor,
                            disk_output=f"Filesystem GiB Used Available Capacity Mounted\nfixture 500 400 {free_space} 80% /\n",
                        )
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")

    def test_remote_text_with_unreadable_space_keeps_build_output(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for disk_output, disk_status in (("", 0), ("header\nfixture 500 400 unavailable\n", 0), ("", 1)):
                    with self.subTest(shell=shell, status=status, disk_output=disk_output, disk_status=disk_status):
                        answer = self.run_remote(shell, status, disk_output=disk_output, disk_status=disk_status)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=kept"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((target / "nextest-output").read_text(), "built\n")

    def test_remote_text_with_another_target_dir_keeps_every_build_directory(self) -> None:
        target = self.mirror / "target"
        target.mkdir()
        existing = target / "previous-build"
        _ = existing.write_text("keep\n")
        other_target = self.temporary_directory / "other build"
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for status in (0, 42):
                for words in (("--target-dir", str(other_target)), (f"--target-dir={other_target}",)):
                    with self.subTest(shell=shell, status=status, words=words):
                        answer = self.run_remote(shell, status, words)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=skipped"])
                        self.assertEqual(existing.read_text(), "keep\n")
                        self.assertEqual((other_target / "nextest-output").read_text(), "built\n")
                        self.assertEqual(cast(list[str], json.loads((self.root / "lint-arguments.json").read_text())), ["nextest", *words])

    def test_remote_text_reports_delete_failure_after_the_test_status(self) -> None:
        remove = self.bin_directory / "rm"
        _ = remove.write_text(f"#!{sys.executable}\n" + r'''
import json
import os
from pathlib import Path
import sys

(Path(os.environ["CLEANUP_TEST_DIR"]) / "remove-arguments.json").write_text(json.dumps(sys.argv[1:]))
raise SystemExit(7)
''')
        remove.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"]}):
            for shell in (["/bin/sh"], ["zsh", "-f"]):
                for status in (0, 42):
                    with self.subTest(shell=shell, status=status):
                        answer = self.run_remote(shell, status)
                        self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
                        self.assertEqual(answer.stdout.splitlines(), ["nextest finished", f"mac_exit={status}", "mac_cleanup=7"])
                        self.assertTrue((self.mirror / "target" / "nextest-output").exists())
                        arguments = cast(list[str], json.loads((self.root / "remove-arguments.json").read_text()))
                        self.assertEqual([word for word in arguments if not word.startswith("-")], [str(self.mirror / "target")])


class MacActivityShellTests(unittest.TestCase):
    root: Path = Path()
    bin_directory: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bin_directory = self.root / "bin"
        self.bin_directory.mkdir()
        for name in ("pgrep", "lsof", "ps", "id"):
            path = self.bin_directory / name
            _ = path.write_text(f"#!{sys.executable}\n" + ACTIVITY_SELECTOR_STUB)
            path.chmod(0o755)

    def run_activity(self, shell: list[str], responses: dict[str, tuple[int, str]]) -> subprocess.CompletedProcess[str]:
        _ = (self.root / "responses.json").write_text(json.dumps(responses))
        (self.root / "commands.jsonl").unlink(missing_ok=True)
        environment = {**os.environ, "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                       "ACTIVITY_TEST_DIR": str(self.root)}
        checks = "\n".join(
            '[ "$(command -v ' + name + ')" = ' + shlex.quote(str(self.bin_directory / name)) + ' ] || exit 99'
            for name in ("pgrep", "lsof", "ps", "id")
        )
        text = "kill() { printf 'signal requested\\n' >&2; return 99; }\n" + checks + "\n" + mac_test.mac_activity_script()
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=300, check=False)

    def test_activity_text_has_zero_for_only_shells_in_clones_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p101\nn/Users/test/rust/hana\np102\nn/Users/test/rust/other\n"),
            "ps": (0, "101 501 /bin/zsh\n102 501 /bin/sh\n103 999 /opt/cargo\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=0\nrc=0\n", ""))

    def test_activity_text_counts_each_matching_user_pid_once_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (0, "101\n102\n"),
            "lsof": (0, "p102\nn/Users/test/.local/state/mac-test/mirror/hana\n"
                     + "p103\nn/Users/test/.local/state/mac-test/mirror\np104\nn/Users/test/rust/hana\n"),
            "ps": (0, "101 501 /opt/rust/bin/cargo\n105 501 /opt/rust/bin/rustc\n106 501 cargo-nextest\n"
                   + "107 501 /Users/test/rust/hana/target/debug/app\n108 999 /opt/cargo\n"
                   + "109 501 /bin/zsh\n110 501 /opt/cargo-helper\n112 501 /Users/test/my project/target/debug/app\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_activity(shell, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "active=7\nrc=0\n", ""))
                calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
                pgrep = [call for call in calls if call["command"] == "pgrep"]
                lsof = [call for call in calls if call["command"] == "lsof"]
                ps = [call for call in calls if call["command"] == "ps"]
                self.assertEqual(len(pgrep), 1)
                self.assertIn("-u", pgrep[0]["args"])
                self.assertIn("501", pgrep[0]["args"])
                self.assertIn("[.]local/state/mac-test/mirror/", pgrep[0]["args"])
                self.assertEqual(lsof[0]["args"], ["-a", "-u", "501", "-d", "cwd", "-Fpn"])
                self.assertEqual(len(ps), 1)

    def test_activity_selector_failure_prints_no_success_under_sh_and_zsh(self) -> None:
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            for name, status in (("pgrep", 2), ("lsof", 1), ("ps", 1)):
                with self.subTest(shell=shell, selector=name):
                    responses = {"id": (0, "501\n"), "pgrep": (1, ""), "lsof": (0, ""), "ps": (0, "")}
                    responses[name] = (status, "")
                    answer = self.run_activity(shell, responses)
                    self.assertNotEqual(answer.returncode, 0)
                    self.assertNotIn("rc=0", answer.stdout)
                    self.assertNotIn("active=0", answer.stdout)
                    self.assertNotIn("signal requested", answer.stderr)

    def test_activity_and_leftover_texts_use_the_shared_mirror_selector(self) -> None:
        marker = "select_leftovers() { return 1; }\n"
        with mock.patch.object(offload, "mirror_selector_script", return_value=marker) as selector:
            self.assertTrue(offload.leftover_check_script(HANA).startswith(marker))
            self.assertTrue(mac_test.mac_activity_script().startswith(marker))
        self.assertEqual(selector.call_args_list, [mock.call(HANA), mock.call(offload.ALL_MIRRORS)])

    def run_selector(
        self, shell: list[str], scope: offload.MirrorScope, responses: dict[str, tuple[int, str]],
    ) -> subprocess.CompletedProcess[str]:
        _ = (self.root / "responses.json").write_text(json.dumps(responses))
        (self.root / "commands.jsonl").unlink(missing_ok=True)
        environment = {**os.environ, "PATH": str(self.bin_directory) + os.pathsep + os.environ["PATH"],
                       "ACTIVITY_TEST_DIR": str(self.root)}
        text = offload.mirror_selector_script(scope) + "select_leftovers\n"
        return subprocess.run([*shell, "-c", text], env=environment, capture_output=True, text=True, timeout=300, check=False)

    def test_repository_selector_stops_at_its_own_mirror_under_sh_and_zsh(self) -> None:
        responses = {
            "id": (0, "501\n"), "pgrep": (1, ""),
            "lsof": (0, "p201\nn/Users/test/.local/state/mac-test/mirror/hana\n"
                     + "p202\nn/Users/test/.local/state/mac-test/mirror/hana/crates/hana\n"
                     + "p203\nn/Users/test/.local/state/mac-test/mirror/hana_other\n"
                     + "p204\nn/Users/test/.local/state/mac-test/mirror/cargo-liner\n"
                     + "p205\nn/Users/test/.local/state/mac-test/mirror\n"
                     + "p206\nn/Users/test/rust/hana\n"),
        }
        for shell in (["/bin/sh"], ["zsh", "-f"]):
            with self.subTest(shell=shell):
                answer = self.run_selector(shell, HANA, responses)
                self.assertEqual((answer.returncode, answer.stdout, answer.stderr), (0, "201\n202\n", ""))
                calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
                pgrep = [call for call in calls if call["command"] == "pgrep"]
                self.assertEqual(len(pgrep), 1)
                self.assertEqual(pgrep[0]["args"], ["-u", "501", "-f", HANA_COMMAND_PATTERN])

    def test_repository_command_pattern_matches_only_its_own_mirror(self) -> None:
        responses = {"id": (0, "501\n"), "pgrep": (1, ""), "lsof": (0, "")}
        answer = self.run_selector(["/bin/sh"], HANA, responses)
        self.assertEqual(answer.returncode, 0, answer.stderr)
        calls = [cast(SelectorEvent, json.loads(line)) for line in (self.root / "commands.jsonl").read_text().splitlines()]
        pattern = next(call["args"][-1] for call in calls if call["command"] == "pgrep")
        matching = [
            "sh -c cd .local/state/mac-test/mirror/hana && cargo nextest run",
            "cargo build --manifest-path /Users/test/.local/state/mac-test/mirror/hana/Cargo.toml",
            "sh -c cd '/Users/test/.local/state/mac-test/mirror/hana' && cargo test",
            "sh -c cd \"/Users/test/.local/state/mac-test/mirror/hana\"",
            "ls /Users/test/.local/state/mac-test/mirror/hana",
        ]
        other = [
            "sh -c cd .local/state/mac-test/mirror/hana_other && cargo nextest run",
            "sh -c cd .local/state/mac-test/mirror/cargo-liner && cargo nextest run",
            "sh -c cd .local/state/mac-test/mirror/hanabi && cargo test",
            "sh -c cd /Users/test/rust/hana && cargo test",
        ]
        selected = subprocess.run(["grep", "-E", "-e", pattern], input="\n".join(matching + other) + "\n",
                                  capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(selected.stdout.splitlines(), matching)


if __name__ == "__main__":
    _ = unittest.main()
