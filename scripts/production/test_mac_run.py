#!/usr/bin/env python3
"""Exercise mac_run.sh coordination with disposable state and command stand-ins."""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import TypedDict, cast, final, override
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("mac_run.sh")
MAC_TEST = SCRIPT.parent.parent / "mac_test" / "mac_test.py"
SHA = "1234567890abcdef1234567890abcdef12345678"
# build_hold.owning_session() reads these; a Mac block admits the session they name.
SESSION_SELECTORS = frozenset({
    "CLAUDE_CODE_SESSION_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR",
    "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR",
})


class CommandEvent(TypedDict):
    command: str
    args: list[str]
    claimed: bool
    input: str


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
    "input": sys.stdin.read() if command == "ssh" and "cat >" in arguments[-1] else "",
}
with (state / "events.jsonl").open("a", encoding="utf-8") as output:
    output.write(json.dumps(event) + "\n")

if command == "systemd-run":
    raise SystemExit(0)
if command == "scp":
    raise SystemExit(0)
if command == "git":
    if "merge-base" in arguments and "--is-ancestor" in arguments:
        descendant = os.environ.get("MAC_RUN_CLONE_DESCENDANT", "")
        raise SystemExit(
            0 if descendant and arguments[-2:] == [os.environ["MAC_RUN_SHA"], descendant]
            else 1
        )
    if "rev-parse" in arguments and "--verify" in arguments:
        status = int(os.environ.get("MAC_RUN_GIT_RESOLVE_STATUS", "0"))
        if status == 0:
            print(os.environ["MAC_RUN_SHA"])
        raise SystemExit(status)
    if "bundle" in arguments and "create" in arguments:
        descendant = os.environ.get("MAC_RUN_CLONE_DESCENDANT", "")
        if descendant and f"^{descendant}" in arguments:
            print("fatal: Refusing to create empty bundle.", file=sys.stderr)
            raise SystemExit(128)
        bundle = Path(arguments[arguments.index("-q") + 1])
        bundle.write_text("bundle\n", encoding="utf-8")
    raise SystemExit(0)
if command != "ssh":
    raise SystemExit(99)

if arguments[-1:] == ["true"] and "-o" in arguments:
    raise SystemExit(0 if os.environ.get("MAC_RUN_REACHABLE", "1") == "1" else 255)

remote = arguments[-1]
clone_head = state / "clone-head"
if "hanas.py stop-checkout " in remote:
    (state / "cleanup-called").write_text("called\n", encoding="utf-8")
    status = int(os.environ.get("MAC_RUN_CLEANUP_STATUS", "0"))
    if status == 0 and os.environ.get("MAC_RUN_CONFIRM_STOP", "1") == "1":
        print("hanas: checkout clear")
        (state / "checkout-clear").write_text("confirmed\n", encoding="utf-8")
    print(f"mac_exit={status}")
    raise SystemExit(0)
if "scratch.py end " in remote:
    if not (state / "checkout-clear").exists():
        print("task end before confirmed exit", file=sys.stderr)
        raise SystemExit(98)
    print("mac_exit=0")
    raise SystemExit(0)
if "scratch.py clone " in remote:
    print(os.environ["MAC_RUN_CHECKOUT"])
    print("mac_exit=0")
    raise SystemExit(0)
if "checkout -q --detach" in remote:
    clone_head.write_text(os.environ["MAC_RUN_SHA"], encoding="utf-8")
if (
    os.environ.get("MAC_RUN_HOLD_BUILD") == "1"
    and "cargo build -q -p hana" in remote
):
    def stop(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    (state / "build-started").write_text("started\n", encoding="utf-8")
    signal.pause()

if "; echo mac_exit=$?" in remote:
    status = 142 if "perl -e" in remote else 0
    print(f"mac_exit={status}")
    raise SystemExit(0)
if "git -C " in remote and "rev-parse HEAD" in remote:
    print(
        clone_head.read_text(encoding="utf-8") if clone_head.exists()
        else os.environ.get("MAC_RUN_CLONE_DESCENDANT", os.environ["MAC_RUN_SHA"])
    )
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
        _ = write_config_fixture(self.config_path, "ci_repo=\n")
        stub = self.bin_directory / "command-stub"
        self.write_executable(stub, COMMAND_STUB)
        for name in ("ssh", "scp", "git", "systemd-run", "gh", "rsync"):
            (self.bin_directory / name).symlink_to(stub)
        startup = self.root / "python-startup"
        startup.mkdir()
        _ = (startup / "sitecustomize.py").write_text(
            SITE_CUSTOMIZE, encoding="utf-8"
        )
        # The runner's session must not own the fixture's Mac block.
        self.environment = {
            **{name: value for name, value in os.environ.items() if name not in SESSION_SELECTORS},
            "HOME": str(self.home),
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(startup),
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            "MAC_TEST_CONFIG": str(self.config_path), "CONF_DIR": str(Path(self.config_path).parent),
            "MAC_RUN_STUB_STATE": str(self.stub_state),
            "MAC_RUN_SHA": SHA,
            "MAC_RUN_CHECKOUT": str(self.home / "scratch" / "gate-mac-run-private" / "checkout"),
            "HANA_TEST_PURPOSE": "Mac launch coordination",
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
            timeout=TEST_HANG_GUARD_SECS,
        )

    def mac_run(
        self, *, extra: dict[str, str] | None = None, timeout: float = 300
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
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
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

    def test_both_builds_set_the_development_deployment_target(self) -> None:
        self.environment.update({
            "BUILD_HOLD_DIR": str(self.root / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
            "UNIT_ACTIVE_DIR": str(self.root / "active"),
            "BUILDLOG_DIR": str(self.root / "buildlog"),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        })
        result = self.mac_run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        builds = [event["args"][-1] for event in self.events()
                  if event["command"] == "ssh" and "cargo build" in event["args"][-1]]
        self.assertGreaterEqual(len(builds), 2)
        self.assertTrue(any("cargo build -q -p hana" in command for command in builds))
        self.assertTrue(any("--example" in command for command in builds))
        for command in builds:
            self.assertIn("MACOSX_DEPLOYMENT_TARGET=14.0 ~/.cargo/bin/cargo build", command)

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

    def test_claim_names_the_repository_so_another_repository_runs_beside_it(self) -> None:
        (self.repository / ".git").mkdir()
        other = self.root / "other"
        (other / ".git").mkdir(parents=True)
        claimed = self.state_command("claim", "--pid", str(os.getpid()), "--what", "other tests",
                                     "--worktree", str(other))
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        claims = self.state_directory / "claims"
        before = (claims / "other.json").read_bytes()
        self.clear_events()

        result = self.mac_run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreater(len([event for event in self.events() if event["command"] in {"ssh", "scp"}]), 1)
        self.assertEqual(sorted(path.name for path in claims.iterdir()), ["other.json"])
        self.assertEqual((claims / "other.json").read_bytes(), before)
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

    def test_ancestor_of_the_clone_head_checks_out_without_a_bundle(self) -> None:
        result = self.mac_run(extra={
            "MAC_RUN_CLONE_DESCENDANT": "abcdef1234567890abcdef1234567890abcdef12",
        })

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        self.assertFalse(any(
            event["command"] == "git" and "bundle" in event["args"]
            and "create" in event["args"] for event in events
        ), events)
        self.assertFalse(any(event["command"] == "scp" for event in events), events)
        remote_commands = [
            event["args"][-1] for event in events if event["command"] == "ssh"
        ]
        self.assertFalse(any("fetch" in command for command in remote_commands), events)
        self.assertEqual(sum(
            f"checkout -q --detach {SHA}" in command for command in remote_commands
        ), 1, events)
        self.assertTrue(any(
            "cargo build -q -p hana" in command for command in remote_commands
        ), events)
        self.assertTrue(any(
            "cargo build" in command and "--example" in command
            for command in remote_commands
        ), events)
        self.assertFalse(any(
            event["command"] == "git" and "update-ref" in event["args"]
            for event in events
        ), events)
        self.assertEqual(list(self.temp_directory.iterdir()), [])
        self.assertIn(f"HEAD={SHA}", result.stdout)
        self.assert_claim_released()

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
        self.assertTrue(any(
            event["command"] == "git" and "rev-parse" in event["args"]
            for event in self.events()
        ), self.events())
        self.assertNotIn("HANA_TEST_PURPOSE", result.stdout + result.stderr)
        self.assert_claim_released()
        self.assert_no_build_started()

    def test_missing_purpose_refuses_before_ssh(self) -> None:
        for purpose in (None, "", " \t "):
            with self.subTest(purpose=purpose):
                self.clear_events()
                if purpose is None:
                    _ = self.environment.pop("HANA_TEST_PURPOSE", None)
                else:
                    self.environment["HANA_TEST_PURPOSE"] = purpose
                result = self.mac_run()
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("HANA_TEST_PURPOSE", result.stderr)
                self.assertEqual(self.events(), [])
                self.assert_claim_released()

    def test_scratch_clone_launch_keeps_purpose_and_owner(self) -> None:
        purpose = "camera's $(touch PURPOSE_INJECTION); frame & light"
        for explicit_owner in (False, True):
            with self.subTest(explicit_owner=explicit_owner):
                self.clear_events()
                owner = "director's $(touch OWNER_INJECTION)" if explicit_owner else "resources director"
                result = self.mac_run(extra={
                    "HANA_TEST_PURPOSE": purpose,
                    "SHOWRUNNER_UNIT_ID": "resources director",
                    "HANA_TEST_OWNER": owner if explicit_owner else "",
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                remote = [event for event in self.events() if event["command"] == "ssh"]
                creation = next(event for event in remote if "scratch.py clone " in event["args"][-1])
                create_words = shlex.split(creation["args"][-1])
                task = create_words[create_words.index("--task") + 1]
                self.assertRegex(task, rf"^gate:mac-run-{SHA}-\d+$")
                self.assertIn("--name", create_words)
                launch = next(event["input"] for event in remote if event["input"])
                self.assertIn(f"cd {self.environment['MAC_RUN_CHECKOUT']}", launch)
                line = next(line for line in launch.splitlines() if line.startswith("HANA_TEST_PURPOSE="))
                words = shlex.split(line)
                self.assertIn(f"HANA_TEST_PURPOSE={purpose}", words)
                self.assertIn(f"HANA_TEST_OWNER={owner}", words)
                self.assertIn(f"{self.environment['MAC_RUN_CHECKOUT']}/target/debug/hana", words)
                # Evaluate the original label assignments with Zsh, replacing only the launched program.
                labels = line.partition(" BEVY_ASSET_ROOT=")[0]
                evaluated = subprocess.run(
                    ["zsh", "-f", "-c", labels + " " + shlex.join([
                        sys.executable, "-c", "import os,json; print(json.dumps([os.environ['HANA_TEST_PURPOSE'], os.environ['HANA_TEST_OWNER']]))"
                    ])], cwd=self.root, env=self.environment, capture_output=True, text=True, check=False)
                self.assertEqual(evaluated.returncode, 0, evaluated.stderr)
                self.assertEqual(json.loads(evaluated.stdout), [purpose, owner])
                self.assertFalse((self.root / "PURPOSE_INJECTION").exists())
                self.assertFalse((self.root / "OWNER_INJECTION").exists())
                ends = [event for event in remote if f"scratch.py end --task {task}" in event["args"][-1]]
                self.assertEqual(len(ends), 1)
                self.assertTrue(ends[0]["claimed"])
                self.assert_claim_released()

    def test_task_end_requires_confirmed_exit_and_failed_stops_keep_the_checkout(self) -> None:
        for stop_status, confirm in ((0, "1"), (23, "0"), (0, "0")):
            with self.subTest(stop_status=stop_status, confirm=confirm):
                self.clear_events()
                (self.stub_state / "checkout-clear").unlink(missing_ok=True)
                result = self.mac_run(extra={"MAC_RUN_CLEANUP_STATUS": str(stop_status),
                                             "MAC_RUN_CONFIRM_STOP": confirm})
                events = self.events()
                stop = [event for event in events if event["command"] == "ssh"
                        and "hanas.py stop-checkout " in event["args"][-1]]
                self.assertEqual(len(stop), 1, events)
                self.assertTrue(stop[0]["claimed"])
                words = shlex.split(stop[0]["args"][-1])
                self.assertEqual(words[words.index("stop-checkout") + 1].rstrip(";"),
                                 self.environment["MAC_RUN_CHECKOUT"])
                ended = [event for event in events if event["command"] == "ssh"
                         and "scratch.py end --task " in event["args"][-1]]
                if stop_status == 0 and confirm == "1":
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(ended), 1, events)
                    self.assertLess(events.index(stop[0]), events.index(ended[0]))
                    self.assertTrue(ended[0]["claimed"])
                else:
                    self.assertEqual(ended, [], events)
                    self.assertIn(self.environment["MAC_RUN_CHECKOUT"], result.stderr)
                    self.assertIn("kept checkout", result.stderr.lower())
                self.assert_claim_released()

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
                (self.stub_state / "checkout-clear").unlink(missing_ok=True)
                process = self.start_mac_run(
                    extra={
                        "MAC_RUN_HOLD_BUILD": "1",
                        "MAC_RUN_CLEANUP_STATUS": str(cleanup_status),
                    }
                )
                try:
                    self.wait_for_path(self.stub_state / "build-started")
                    send_process_group(process.pid, signal_number)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                except BaseException:
                    send_process_group(process.pid, signal.SIGKILL)
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
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
                        and "[c]argo build" in argument
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
                checkout = self.environment["MAC_RUN_CHECKOUT"]
                self.assertIn(f'{checkout}|{checkout}/*)', remote)
                self.assertIn("/tmp/mac_run_hana.command", remote)
                self.assertIn("/tmp/mac_run_hana.status", remote)
                events = self.events()
                stops = [event for event in events if event["command"] == "ssh"
                         and "hanas.py stop-checkout " in event["args"][-1]]
                self.assertEqual(len(stops), 1, events)
                self.assertTrue(stops[0]["claimed"])
                words = shlex.split(stops[0]["args"][-1])
                self.assertEqual(words[words.index("stop-checkout") + 1].rstrip(";"), checkout)
                self.assertLess(events.index(cleanup), events.index(stops[0]))
                ends = [event for event in events if event["command"] == "ssh"
                        and "scratch.py end --task " in event["args"][-1]]
                if cleanup_status == 0:
                    self.assertEqual(len(ends), 1, events)
                    self.assertLess(events.index(stops[0]), events.index(ends[0]))
                    self.assertTrue(ends[0]["claimed"])
                else:
                    self.assertEqual(ends, [], events)
                    self.assertIn("process exit not confirmed", errors)
                self.assert_claim_released()


if __name__ == "__main__":
    _ = unittest.main()
