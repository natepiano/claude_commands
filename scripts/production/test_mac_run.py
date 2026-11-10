#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Exercise mac_run.sh coordination with disposable state and command stand-ins."""

from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import TypedDict, cast, final, override
from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.direct.wait_for_test_state import wait_until
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lib.process_group import send_process_group
from scripts.lib.py_floor_test_support import seed_interpreter_cache
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production.cli_fixture import ImmutableGitSeed


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("mac_run.sh")
MAC_TEST = SCRIPT.parent.parent / "mac_test" / "mac_test.py"
SHA = "1234567890abcdef1234567890abcdef12345678"
# build_hold.owning_session() reads these; a Mac block admits the session they name.
SESSION_SELECTORS = frozenset({
    "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "UNIT_RUN_DIR", "UNIT_SESSION_DIR",
    "PLAN_DELEGATE_BOARD_DIR", "PLAN_DELEGATE_SESSION_DIR",
})


class CommandEvent(TypedDict):
    command: str
    args: list[str]
    claimed: bool
    input: str


COMMAND_STUB = r'''
run_real_copies() {
    export PATH=$MAC_RUN_REAL_PATH
    export COPIES_CONFIG=$MAC_RUN_REMOTE_COPIES_CONFIG
    export MAC_TEST_STATE_DIR=$MAC_RUN_REMOTE_CLAIMS
    eval "$remote"
    exit $?
}
state=$MAC_RUN_STUB_STATE
command=${0:t}
claimed=false
[[ ! -e $MAC_TEST_STATE_DIR/run.json ]] || claimed=true
input=""
if [[ $command == ssh && ${@[-1]} == *'cat >'* ]]; then
    input=$(cat; print -rn -- .)
    input=${input%.}
fi
# NUL separates fields so labels and command input keep their original bytes.
printf '%s\0' "$command" "$claimed" "$input" "$#" "$@" >> "$state/events"

case $command in
    systemd-run|scp|curl) exit 0 ;;
    git)
        if (( ${@[(Ie)merge-base]} && ${@[(Ie)--is-ancestor]} )); then
            [[ -n $MAC_RUN_CLONE_DESCENDANT && ${@[-2]} == $MAC_RUN_SHA && ${@[-1]} == $MAC_RUN_CLONE_DESCENDANT ]]
            exit $?
        fi
        if (( ${@[(Ie)rev-parse]} )); then
            code=0
            (( ! ${@[(Ie)--verify]} )) || code=${MAC_RUN_GIT_RESOLVE_STATUS:-0}
            (( code != 0 )) || print -r -- "$MAC_RUN_SHA"
            exit $code
        fi
        if (( ${@[(Ie)bundle]} && ${@[(Ie)create]} )); then
            if [[ -n $MAC_RUN_CLONE_DESCENDANT ]] && (( ${@[(Ie)^$MAC_RUN_CLONE_DESCENDANT]} )); then
                print -u2 -- 'fatal: Refusing to create empty bundle.'
                exit 128
            fi
            bundle_index=${@[(Ie)-q]}
            print -r -- bundle > "${@[$((bundle_index + 1))]}"
        fi
        exit 0 ;;
    ssh) ;;
    *) exit 99 ;;
esac

if [[ ${@[-1]} == true ]] && (( ${@[(Ie)-o]} )); then
    [[ ${MAC_RUN_REACHABLE:-1} == 1 ]] && exit 0
    exit 255
fi

remote=${@[-1]}
clone_head=$state/clone-head
if [[ $MAC_RUN_REAL_COPIES == 1 && $remote == *'copies.py '* ]]; then
    run_real_copies
fi
if [[ $remote == *'hana_instances.py stop-checkout '* ]]; then
    print -r -- called > "$state/cleanup-called"
    code=${MAC_RUN_CLEANUP_STATUS:-0}
    if [[ $code == 0 && ${MAC_RUN_CONFIRM_STOP:-1} == 1 ]]; then
        print -r -- 'hana-instances: checkout clear'
        print -r -- confirmed > "$state/checkout-clear"
    fi
    print -r -- "mac_exit=$code"
    exit 0
fi
if [[ $remote == *'scratch.py end '* ]]; then
    if [[ ! -e $state/checkout-clear ]]; then
        print -u2 -- 'task end before confirmed exit'
        exit 98
    fi
    if [[ $MAC_RUN_REAL_COPIES == 1 ]]; then
        run_real_copies
    fi
    print -r -- mac_exit=0
    exit 0
fi
if [[ $remote == *'copies.py clone '* ]]; then
    print -rl -- "$MAC_RUN_CHECKOUT" mac_exit=0
    exit 0
fi
if [[ $remote == *'checkout -q --detach'* ]]; then
    print -rn -- "$MAC_RUN_SHA" > "$clone_head"
fi
if [[ $MAC_RUN_HOLD_BUILD == 1 && $remote == *'cargo build -q -p hana'* ]]; then
    exec "$MAC_RUN_PYTHON" -c '
import os
from pathlib import Path
import signal
signal.signal(signal.SIGTERM, lambda *arguments: os._exit(128 + arguments[0]))
Path(os.environ["MAC_RUN_STUB_STATE"], "build-started").write_text("started\n")
signal.pause()
'
fi
if [[ $remote == *'; echo mac_exit=$?'* ]]; then
    code=0
    [[ $remote != *'perl -e'* ]] || code=142
    print -r -- "mac_exit=$code"
    exit 0
fi
if [[ $remote == *'git -C '* && $remote == *'rev-parse HEAD'* ]]; then
    if [[ -e $clone_head ]]; then
        print -r -- "$(< "$clone_head")"
    else
        print -r -- "${MAC_RUN_CLONE_DESCENDANT:-$MAC_RUN_SHA}"
    fi
    exit 0
fi
[[ $remote != *'cat /tmp/mac_run_hana.status'* ]] || print -r -- 142
exit 0
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
        self.config_path = self.root / "config/mac_test.conf"
        self.temp_directory = self.root / "tmp"
        self.temp_directory.mkdir()
        _ = write_config_fixture(self.config_path, "ci_repo=\nconnect_timeout_s=6\n")
        # Keep both example argument forms without repeating all configured examples.
        _ = write_config_fixture(self.root / "config/production.conf",
                                 "mac_run_examples=hana_diegetic typography;hana_lagrange showcase fit_overlay\n")
        stub = self.bin_directory / "command-stub"
        self.write_executable(stub, COMMAND_STUB)
        for name in ("ssh", "scp", "git", "systemd-run", "gh", "rsync", "curl"):
            (self.bin_directory / name).symlink_to(stub)
        startup = self.root / "python-startup"
        startup.mkdir()
        _ = (startup / "sitecustomize.py").write_text(
            SITE_CUSTOMIZE, encoding="utf-8"
        )
        # The runner's session must not own the fixture's Mac block.
        seed_interpreter_cache(self.root / "cache")
        self.environment = {
            **{name: value for name, value in os.environ.items() if name not in SESSION_SELECTORS},
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "__ETC_ZSHENV_SOURCED": "1",
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(startup),
            "MAC_TEST_STATE_DIR": str(self.state_directory),
            **config_environment(self.config_path.parent),
            "MAC_RUN_STUB_STATE": str(self.stub_state),
            "MAC_RUN_SHA": SHA,
            "MAC_RUN_PYTHON": sys.executable,
            "MAC_RUN_CHECKOUT": str(self.home / "scratch" / "gate-mac-run-private" / "checkout"),
            "HANA_TEST_PURPOSE": "Mac launch coordination",
            "TMPDIR": str(self.temp_directory),
        }

        _ = self.environment.pop("CLAUDE_PYTHON", None)

    def write_executable(self, path: Path, source: str) -> None:
        _ = path.write_text("#!/usr/bin/env zsh\n" + source, encoding="utf-8")
        path.chmod(0o755)

    def state_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT.parents[1] / "lib/py"), str(MAC_TEST), *arguments],
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
        path = self.stub_state / "events"
        if not path.exists():
            return []
        fields = path.read_bytes().decode("utf-8").split("\0")
        events: list[CommandEvent] = []
        position = 0
        while position < len(fields) - 1:
            command, claimed, input_text, argument_count = fields[position:position + 4]
            start = position + 4
            position = start + int(argument_count)
            events.append({"command": command, "claimed": claimed == "true",
                           "input": input_text, "args": fields[start:position]})
        return events

    def clear_events(self) -> None:
        (self.stub_state / "events").unlink(missing_ok=True)

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

    def wait_for_path(self, path: Path, process: subprocess.Popen[str]) -> None:
        wait_until(lambda: path.exists() or process.poll() is not None, path.parent, processes=(process,))
        if process.poll() is not None:
            output, errors = process.communicate()
            self.fail("mac_run exited before the build marker: " + output + errors)
        self.assertTrue(path.exists())

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
        claim = cast(dict[str, str], json.loads((self.state_directory / "run.json").read_text()))
        released = self.state_command("release", "--pid", str(os.getpid()), "--claim-id", claim["claim_id"],
                                      "--episode", claim["episode"], "--attempt", claim["attempt"])
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

    def test_smoke_uses_the_shared_refused_port_and_host(self) -> None:
        _ = write_config_fixture(self.root / "config/hana_shot.conf",
                                 "refused_port=17461\nhost=192.0.2.46\n")
        script = SCRIPT.with_name("smoke_launch.sh")

        def smoke(port: str) -> subprocess.CompletedProcess[str]:
            self.clear_events()
            return subprocess.run(
                ["bash", str(script), str(self.repository), SHA, str(self.root / "smoke.log"), port],
                cwd=self.root, env=self.environment, capture_output=True, text=True,
                timeout=TEST_HANG_GUARD_SECS, check=False)

        refused = smoke("17461")
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("17461 is the user's live Hana", refused.stdout)
        self.assertEqual(self.events(), [])

        occupied = smoke("17462")
        self.assertEqual(occupied.returncode, 1, occupied.stdout + occupied.stderr)
        self.assertIn("port 17462 already answers BRP", occupied.stdout)
        self.assertEqual([event["args"][-1] for event in self.events() if event["command"] == "curl"],
                         ["http://192.0.2.46:17462/jsonrpc"])

    def test_mac_connect_timeout_uses_the_shared_fraction_and_rounds_up(self) -> None:
        _ = write_config_fixture(self.config_path, "connect_timeout_s=2.5\n")

        result = self.mac_run(extra={"MAC_RUN_REACHABLE": "0"})

        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        calls = self.events()
        self.assertEqual([event["command"] for event in calls], ["ssh"])
        self.assertIn("ConnectTimeout=3", calls[0]["args"])

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

    def test_bundle_with_a_space_has_the_same_sftp_and_ssh_filename(self) -> None:
        remote_bundle = "/tmp/Hana test.bundle"
        _ = write_config_fixture(self.root / "config/production.conf", f"mac_run_bundle={remote_bundle}\n")
        result = self.mac_run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = self.events()
        copy = next(event for event in events if event["command"] == "scp")
        destination = copy["args"][-1].split(":", 1)[1]
        command = next(event["args"][-1] for event in events
                       if event["command"] == "ssh" and " fetch -q " in event["args"][-1])
        arguments = shlex.split(command)
        fetched = arguments[arguments.index("fetch") + 2]
        self.assertEqual(destination, remote_bundle)
        self.assertEqual(fetched, remote_bundle)
        self.assertEqual(destination, fetched)

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
                owner = "director's $(touch OWNER_INJECTION)" if explicit_owner else "019b3a40-4250-7000-8000-000000000104"
                result = self.mac_run(extra={
                    "HANA_TEST_PURPOSE": purpose,
                    "CLAUDE_CODE_SESSION_ID": "019b3a40-4250-7000-8000-000000000104",
                    "HANA_TEST_OWNER": owner if explicit_owner else "",
                })
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                remote = [event for event in self.events() if event["command"] == "ssh"]
                creation = next(event for event in remote if "copies.py clone " in event["args"][-1])
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
                        and "hana_instances.py stop-checkout " in event["args"][-1]]
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

    def clone_registry_records(self, config: str) -> list[dict[str, object]]:
        with mock.patch.dict(os.environ, {"COPIES_CONFIG": config}):
            return [cast(dict[str, object], json.loads(copies.remote_record_path(mirror).read_text()))
                    for mirror in copies.remote_mirrors()]

    def assert_clone_records_after_cleanup(self, cleanup_status: int) -> None:
        source = ImmutableGitSeed.create(self.root, "main", "Clone Tests", "clone@example.invalid",
                                         {"source.txt": "content\n"}).checkout
        head = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        production = self.root / "config/production.conf"
        _ = write_config_fixture(production, production.read_text() + f"mac_run_source={source}\n")
        local = self.root / "local-copies"
        remote = self.root / "remote-copies"
        local.mkdir()
        remote.mkdir()
        local_environment = copies_environment(local)
        remote_environment = copies_environment(remote)
        self.environment.update({key: value for key, value in local_environment.items() if key != "HOME"})
        self.environment.update({
            "MAC_RUN_SHA": head, "MAC_RUN_REAL_COPIES": "1", "MAC_RUN_REAL_PATH": os.environ["PATH"],
            "MAC_RUN_REMOTE_COPIES_CONFIG": remote_environment["COPIES_CONFIG"],
            "MAC_RUN_REMOTE_CLAIMS": str(self.root / "remote-claims"),
        })
        process = self.start_mac_run(extra={"MAC_RUN_HOLD_BUILD": "1", "MAC_RUN_CLEANUP_STATUS": str(cleanup_status)})
        try:
            ready = self.stub_state / "build-started"
            wait_until(lambda: ready.exists() or process.poll() is not None, self.stub_state, processes=(process,))
            if process.poll() is not None:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.fail(output + errors)
            before_local = self.clone_registry_records(local_environment["COPIES_CONFIG"])
            before_remote = self.clone_registry_records(remote_environment["COPIES_CONFIG"])
            self.assertEqual(len(before_local), 1)
            self.assertEqual(len(before_remote), 1)
            self.assertEqual(before_local[0]["path"], before_remote[0]["path"])
            self.assertEqual(before_local[0]["host"], "natemccoy@mac")
            self.assertEqual(before_remote[0]["host"], "localhost")
            send_process_group(process.pid, signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        finally:
            if process.poll() is None:
                send_process_group(process.pid, signal.SIGKILL)
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 143, output + errors)
        for name, settings, before in (("natedev", local_environment["COPIES_CONFIG"], before_local),
                                       ("Mac", remote_environment["COPIES_CONFIG"], before_remote)):
            with self.subTest(registry=name):
                self.assertEqual(self.clone_registry_records(settings), before if cleanup_status else [])

    def test_successful_clone_cleanup_removes_both_registry_records(self) -> None:
        self.assert_clone_records_after_cleanup(0)

    def test_failed_clone_cleanup_keeps_both_registry_records(self) -> None:
        self.assert_clone_records_after_cleanup(23)

    def test_build_marker_wait_reports_a_failed_child(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.exit('fixture start failed')"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            with self.assertRaisesRegex(AssertionError, "fixture start failed"):
                self.wait_for_path(self.stub_state / "build-started", process)

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
                    self.wait_for_path(self.stub_state / "build-started", process)
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
                         and "hana_instances.py stop-checkout " in event["args"][-1]]
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
