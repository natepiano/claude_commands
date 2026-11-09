"""Private copied repositories and command fakes shared by add-unit subject suites."""

from __future__ import annotations

from scripts.shutdown import remote

from pathlib import Path


from scripts.lib.test_models_registry import write_registry

from scripts.lib import names
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.script_envelope import script_prompt
from scripts.lib.test_names import fixture_names
from scripts.lib.socket_test_support import short_socket_directory
from scripts.message import sessions as runtime_sessions
from scripts.message.process_identity import process_start as REAL_PROCESS_START

import argparse
import io
import json
import os
import shlex
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from typing import ClassVar, cast, override
from types import ModuleType
from collections.abc import Callable, Iterator
from unittest.mock import patch

from scripts.production import add_unit
from scripts.production import staff, unit_config
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli
from scripts.hooks import conversation_pause
from scripts.production import unit_lookup
from scripts.message import sessions
from scripts.shutdown import inventory
from scripts.shutdown import restart
from scripts.herdr.herdr import HerdrPlace
from scripts.production.production_doc import Refusal, read_production
from scripts.accounts import accounts
from scripts.whoami.note_file import NumberedAccountLabel, note_name, parse_account_label
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse
from scripts.production.fake_unit_host import confirm_tab_close, process as write_process, private_process_registry
from scripts.whoami.account_test_support import stored_room
from scripts.shutdown.test_support import registry_confirmation, ShutdownTestCase


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("add_unit.py")
REAL_HERDR_RUN_PROGRAM = herdr.run_program
REAL_KILL = os.kill
FAKE_CLAUDE_PID = 991003
MARKED_HERDR_PLACE = HerdrPlace("marked-herdr", "w7:p1")
SHARED = herdr.SHARED_SESSION
NO_LAUNCH_FIXTURE = add_unit.NoLabeledSession()
SUBMITTED_PROMPT = herdr.PromptSubmitted(datetime(2026, 10, 17, tzinfo=timezone.utc))
LINUX_LAUNCHER_CLI = """import runpy, sys
from unittest.mock import patch
from scripts.production.fake_unit_host import private_process_registry
from scripts.shutdown import remote
script = sys.argv.pop(1)
with private_process_registry(), patch.object(remote, 'local_machine', return_value='natedev'):
    runpy.run_path(script, run_name='__main__')
"""


def fixture_process_start(pid: int) -> str | None:
    """Read the fake agent generation from its private process files."""
    if pid != FAKE_CLAUDE_PID:
        return REAL_PROCESS_START(pid)
    try:
        fields = (Path(os.environ["HERDR_PROC"]) / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, KeyError, IndexError):
        return None


def fixture_process_probe(pid: int, number: int) -> None:
    """Read liveness for the private agent without signalling a host process."""
    if pid != FAKE_CLAUDE_PID:
        REAL_KILL(pid, number)
        return
    if number != 0:
        raise AssertionError(f"unexpected signal to private process: {number}")
    if fixture_process_start(pid) is None:
        raise ProcessLookupError(pid)


def fixture_foreground(shell: int) -> CommandResponse:
    """Report the shell until the private agent starts."""
    pane_shell = Path(os.environ["STUB_STATE"]) / "pane-shell"
    if pane_shell.exists():
        shell = int(pane_shell.read_text())
    agent_stat = Path(os.environ["HERDR_PROC"]) / str(FAKE_CLAUDE_PID) / "stat"
    running = agent_stat.exists() and int(agent_stat.read_text().rsplit(")", 1)[1].split()[1]) == shell
    pid = FAKE_CLAUDE_PID if running else shell
    command = "claude" if running else "zsh"
    return CommandResponse(code=0, stdout=json.dumps({"result": {"process_info": {
        "shell_pid": shell, "foreground_process_group_id": pid,
        "foreground_processes": [{"pid": pid, "name": command, "argv": [command]}]}}}))


def fixture_agent_started(arguments: list[str]) -> None:
    """Publish the private agent after its start command succeeds."""
    from scripts.production.staff_test_support import record_fixture_claude_start

    session = arguments[1]
    pane = arguments[arguments.index("--pane") + 1]
    shell = int((Path(os.environ["STUB_STATE"]) / "pane-shell").read_text())
    marks = {"HERDR_SESSION": session, "HERDR_PANE_ID": pane}
    directory = Path(os.environ["HERDR_PROC"]) / str(shell) / "cwd"
    write_process(os.environ, FAKE_CLAUDE_PID, shell, ["claude"], marks, cwd=directory.resolve())
    with patch.object(runtime_sessions, "process_start", side_effect=fixture_process_start):
        record_fixture_claude_start(arguments, remote_control=(Path(os.environ["STUB_STATE"]) / "ready").exists(),
                                   messaging_socket=os.environ["FAKE_CLAUDE_SOCKET"])
    registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / f"{FAKE_CLAUDE_PID}.json"
    record = cast(dict[str, str], json.loads(registry.read_text()))
    identity = record["sessionId"]
    record["messagingSocketPath"] = os.environ["FAKE_CLAUDE_SOCKET"]
    _ = registry.write_text(json.dumps(record))
    marks["CLAUDE_CODE_SESSION_ID"] = identity
    write_process(os.environ, FAKE_CLAUDE_PID, shell, ["claude"], marks, cwd=directory.resolve())
    path = Path(os.environ["FAKE_HERDR_RESPONSES"])
    responses = cast(dict[str, CommandResponse | list[CommandResponse]], json.loads(path.read_text()))
    pane_reply = responses[f"--session {session} pane list"]
    assert not isinstance(pane_reply, list)
    panes = herdr.json_object(herdr.decode_object(pane_reply["stdout"], "fixture panes")["result"], "fixture panes")
    for item in herdr.json_items(panes["panes"], "fixture panes"):
        row = herdr.json_object(item, "fixture pane")
        if row["pane_id"] == pane:
            row["agent_session"] = {"agent": "claude", "kind": "id", "source": "herdr:claude", "value": identity}
    responses[f"--session {session} pane list"] = CommandResponse(code=0, stdout=json.dumps({"result": panes}))
    responses[f"--session {session} pane process-info --pane {pane}"] = fixture_foreground(shell)
    write_text_atomic(path, json.dumps(responses))


STUB = r'''#!/usr/bin/env python3
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

state = Path(os.environ["STUB_STATE"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with (state / "events.jsonl").open("a") as events:
    events.write(json.dumps({"command": name, "args": args,
                             "claude_env": sorted(key for key in os.environ if key.startswith("CLAUDE_"))}) + "\n")
if name == "systemd-run":
    raise SystemExit(subprocess.run(args[3:], check=False).returncode)
'''


class CompletedAgentCLI(subprocess.CompletedProcess[str]):
    """A fake agent command completes inside the checkout process."""

    def __enter__(self) -> CompletedAgentCLI:
        return self

    def __exit__(self, kind: object, value: object, traceback: object) -> None:
        pass

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        del timeout
        return self.stdout, self.stderr


class RemoteControlDeadlineClock:
    """Let one pending readiness poll run, then expire its launch deadline."""

    def __init__(self) -> None:
        self.readings: Iterator[float] = iter((0.0, 0.0, 10.0))
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return next(self.readings)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def configure_test_zone() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


from scripts.production.staff_test_support import (
    StaffTestCase, fixture_runtime_id, unit_membership,
)


class ObservedUnitLaunch:
    """Validate launch identity before a test observes the spawn boundary."""

    def __init__(self, case: unittest.TestCase,
                 observe: Callable[[add_unit.UnitLaunch, str], herdr.PromptSubmitOutcome]) -> None:
        self.case: unittest.TestCase = case
        self.observe: Callable[[add_unit.UnitLaunch, str], herdr.PromptSubmitOutcome] = observe

    def __call__(self, request: add_unit.UnitLaunch, _director: add_unit.DirectorAgent,
                 prompt: str, _selected: accounts.ClaudeLaunchAccount, *, target: str = "",
                 model: str = "", autocompact_window: str = "") -> herdr.PromptSubmitOutcome:
        self.case.assertEqual(target, "claude2" if isinstance(request.session, add_unit.ResumedSession) else "")
        self.case.assertEqual((model, autocompact_window), ("", ""))
        owners = [row for row in staff.find(kind="showrunner")
                  if row.name == request.production.name]
        self.case.assertEqual(len(owners), 1)
        rows = [row for row in staff.find(kind="unit", parent=owners[0].id)
                if row.name == request.identity.session]
        self.case.assertEqual(len(rows), 1)
        row = rows[0]
        self.case.assertIsNotNone(row)
        self.case.assertEqual((row.kind, row.name), ("unit", request.identity.session))
        return self.observe(request, prompt)

class AddUnitTestCase(ShutdownTestCase):
    staff: StaffTestCase = StaffTestCase()
    production_name: str = "build-followups"
    seed: ClassVar[ImmutableGitSeed]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="AddUnitTests-seed-")))
        fixture = cls()
        # HOME is private per test; this expands to its copied checkout without
        # rewriting a tracked file (and committing/pushing it) on every setup.
        fixture.checkout = Path("~/../project-trunk")
        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "'maintenance.auto=false'"}):
            cls.seed = ImmutableGitSeed.create(root, "build-followups", "Unit Test", "unit@example.invalid", {
                "docs/plans/build-followups-production.md": fixture.production_doc(),
                "docs/plans/given.md": "# Given\n",
                ".gitignore": "docs/plans/build-followups-log.md\n",
            })
        for repository in (cls.seed.checkout, cls.seed.origin):
            _ = subprocess.run(["git", "-C", str(repository), "config", "maintenance.auto", "false"],
                               capture_output=True, check=True)

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.checkout: Path = Path()
        self.origin: Path = Path()
        self.doc: Path = Path()
        self.plan: Path = Path()
        self.log: Path = Path()
        self.config: Path = Path()
        self.prompt: Path = Path()
        self.state: Path = Path()
        self.bin: Path = Path()
        self.agent_config: Path = Path()
        self.pause_state: Path = Path()
        self.env: dict[str, str] = {}
        self.herdr_overrides: dict[str, CommandResponse | list[CommandResponse]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        self.staff = StaffTestCase()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        _ = self.enterContext(patch.object(names, "load_names", return_value=fixture_names()))
        _ = self.enterContext(patch.object(sessions, "process_start", side_effect=fixture_process_start))
        _ = self.enterContext(patch.object(runtime_sessions, "process_start", side_effect=fixture_process_start))
        _ = self.enterContext(private_process_registry())
        _ = self.enterContext(patch.object(os, "kill", side_effect=fixture_process_probe))
        # These CLI fixtures prove launcher plumbing; transcript proof is exercised
        # through the real submit method in PromptSubmissionTests.
        def submitted(client: herdr.Herdr, pane: str, opening: herdr.OpeningPrompt,
                      _at: datetime, _timeout_ms: int) -> herdr.PromptSubmitted:
            target = opening.transcript.session_id if isinstance(opening.transcript, herdr.SessionTranscript) else ""
            _ = client.prompt(pane, opening.text, target=target)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        _ = self.enterContext(patch.object(herdr.Herdr, "submit_opening_prompt", submitted))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": str(self.root / "home")})
        ))
        del home_context
        self.checkout = self.root / "project-trunk"
        self.origin = self.root / "origin.git"
        self.seed.copy_to(self.checkout, self.origin)
        self.production_name = "build-followups"
        self.doc = self.checkout / "docs/plans/build-followups-production.md"
        self.plan = self.checkout / "docs/plans/given.md"
        self.log = self.checkout / "docs/plans/build-followups-log.md"
        self.config = self.root / "home/.claude/config/showrunners.json"
        self.prompt = self.root / "home/.local/state/showrunner/build-followups/prompt.txt"
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        _ = (home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"},
                        "projects": self.trusted_projects()}),
            encoding="utf-8",
        )
        notes = self.root / "notes"
        notes.mkdir()
        _ = (notes / "claude2.md").write_text(
            "---\nlogin: owner@example.com\nsessions: 1\n---\n",
            encoding="utf-8",
        )
        registry = home / ".claude/sessions"
        registry.mkdir()
        listener = self.enterContext(socket.socket(socket.AF_UNIX))
        listener.bind(str(self.root / "claude.sock"))
        self.state = self.root / "stub-state"
        _ = self.state.mkdir()
        _ = (self.state / "ready").touch()
        self.bin = self.root / "bin"
        _ = self.bin.mkdir()
        for name in ("systemd-run", "claude"):
            command = self.bin / name
            _ = command.write_text(STUB, encoding="utf-8")
            _ = command.chmod(0o755)
        self.agent_config = self.root / "models.conf"
        self.pause_state = self.root / "conversation-pause"
        codex_config = self.root / "codex.toml"
        codex_cache = self.root / "models.json"
        sync_state = self.root / "catalog-sync-success"
        self.write_agent_config()
        _ = sync_state.touch()
        self.env = {**os.environ, "HOME": str(self.root / "home"),
                    "HERDR_PLATFORM": "linux",
                    "NOTIFIER_SESSIONS_DIR": str(registry),
                    "FAKE_CLAUDE_SOCKET": str(self.root / "claude.sock"),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "STUB_STATE": str(self.state),
                    "SHOWRUNNERS_CONFIG": str(self.config), "CLAUDE_TEST_SECRET": "must-not-leak",
                    "CONVERSATION_PAUSE_STATE_DIR": str(self.pause_state),
                    "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                    "AGENT_NOTES_DIR": str(notes),
                    "MODELS_CONFIG_FILE": str(self.agent_config),
                    "CODEX_CONFIG_FILE": str(codex_config),
                    "CODEX_MODELS_CACHE_FILE": str(codex_cache),
                    "CODEX_CATALOG_SYNC_STATE_FILE": str(sync_state)}
        self.env["STAFF_FILE"] = str(self.staff.staff_file)
        socket_path = Path(self.enterContext(short_socket_directory())) / "claude.sock"
        messaging = socket.socket(socket.AF_UNIX)
        self.addCleanup(messaging.close)
        messaging.bind(str(socket_path))
        self.env["FAKE_CLAUDE_SOCKET"] = str(socket_path)
        self.env["HERDR_PANE_RECORD_DIR"] = str(self.staff.source_root / "panes")
        _ = self.staff.record_production("build-followups", self.doc)
        self.herdr_overrides = {}
        fake_path = self.bin / "fake-herdr"
        source = (SCRIPT.parents[1] / "herdr/fake_herdr.py").read_text()
        # Match a command verb when its trailing generated session id is unknown.
        source = source.replace('responses.get("default", {"stdout": "", "code": 0})',
            'responses.get(" ".join(argv[2:4]), responses.get("default", {"stdout": "", "code": 0}))')
        source = source.replace("raise SystemExit(main(sys.argv[1:]))", "raise SystemExit(submitting_main(sys.argv[1:]))")
        hook = r'''def submitting_main(argv):
    with (Path(os.environ["STUB_STATE"]) / "process.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return submit_command(argv)


def submit_command(argv):
    if argv[2:4] == ["pane", "process-info"] and "FAKE_SHELL_PID" in os.environ:
        from scripts.production.add_unit_test_support import fixture_foreground
        from scripts.lib.atomic_file import write_text_atomic
        path = Path(os.environ["FAKE_HERDR_RESPONSES"])
        responses = json.loads(path.read_text())
        responses[" ".join(argv)] = fixture_foreground(int(os.environ["FAKE_SHELL_PID"]))
        write_text_atomic(path, json.dumps(responses))
    result = main(argv)
    if result == 0 and argv[2:4] == ["workspace", "create"]:
        from scripts.production.fake_unit_host import process
        from scripts.lib.atomic_file import write_text_atomic
        root = Path(os.environ["HERDR_PROC"])
        shell = max(int(item.name) for item in root.iterdir() if item.name.isdecimal()) + 1
        original = root / os.environ["FAKE_SHELL_PID"]
        parent = int((original / "stat").read_text().rsplit(")", 1)[1].split()[1])
        process(os.environ, shell, parent, ["zsh"],
                {"HERDR_SESSION": argv[1], "HERDR_PANE_ID": "w1:p1"}, cwd=(original / "cwd").resolve())
        write_text_atomic(Path(os.environ["STUB_STATE"]) / "pane-shell", str(shell))
    if result == 0 and argv[2:4] == ["agent", "start"]:
        from scripts.production.add_unit_test_support import fixture_agent_started
        fixture_agent_started(argv)
    if result == 0 and argv[2:4] == ["agent", "prompt"]:
        from datetime import datetime, timezone
        calls = [json.loads(line) for line in Path(os.environ["FAKE_HERDR_LOG"]).read_text().splitlines()]
        start = next(row for row in reversed(calls) if row[:2] == argv[:2] and row[2:4] == ["agent", "start"])
        registry = Path(os.environ["NOTIFIER_SESSIONS_DIR"]) / (os.environ["FAKE_CLAUDE_PID"] + ".json")
        session_id = json.loads(registry.read_text())["sessionId"]
        config = next((value.split("=", 1)[1] for row in reversed(calls) for value in row
                       if row[:2] == argv[:2] and value.startswith("CLAUDE_CONFIG_DIR=")),
                      str(Path.home() / ".claude"))
        path = Path(config) / "projects/fixture" / (session_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                                   "message": {"role": "user", "content": argv[-1]}}) + "\n")
    return result

'''
        source = source.replace('if __name__ == "__main__":',
                                hook + 'if __name__ == "__main__":\n'
                                + f'    sys.path.insert(0, {str(SCRIPT.parents[2])!r})')
        _ = fake_path.write_text(source)
        fake_path.chmod(0o755)
        fake = str(fake_path)
        fake_module = ModuleType("add_unit_command_fake")
        fake_module.__file__ = fake
        exec(compile(source, fake, "exec"), vars(fake_module))
        fake_main = cast(Callable[[list[str]], int], fake_module.submitting_main)

        def run_herdr(argv: list[str], timeout: float = 10.0, *,
                      environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if argv[0] != fake:
                return REAL_HERDR_RUN_PROGRAM(argv, timeout, environment=environment)

            def execute(command: list[str], *, capture_output: bool, text: bool, check: bool,
                        timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
                self.assertTrue(capture_output and text)
                self.assertFalse(check)
                self.assertGreater(timeout, 0)
                return invoke_cli(fake_module, lambda: fake_main(command[1:]), command[1:],
                                  env, Path.cwd())

            # Keep herdr's real environment filtering and result/error handling;
            # only replace the Python stand-in's process start with its entry point.
            with patch.object(subprocess, "run", execute):
                return REAL_HERDR_RUN_PROGRAM(argv, timeout, environment=environment)

        _ = self.enterContext(patch.object(herdr, "run_program", run_herdr))
        real_popen = cast(Callable[..., subprocess.Popen[str]], subprocess.Popen)

        def spawn(argv: list[str], **options: object) -> subprocess.Popen[str] | CompletedAgentCLI:
            if argv[0] != fake:
                return real_popen(argv, **options)
            environment = cast(dict[str, str], options["env"])
            result = invoke_cli(fake_module, lambda: fake_main(argv[1:]), argv[1:], environment, Path.cwd())
            return CompletedAgentCLI(argv, result.returncode, result.stdout, result.stderr)

        _ = self.enterContext(patch.object(subprocess, "Popen", spawn))
        self.env.update({"HERDR_BIN": fake, "SYSTEMD_RUN": fake, "SYSTEMCTL": fake,
                         "HERDR_PROC": str(self.root / "proc"),
                         "FAKE_HERDR_LOG": str(self.state / "herdr.jsonl"),
                         "FAKE_HERDR_RESPONSES": str(self.state / "herdr-responses.json")})
        _ = self.env.pop("CLAUDE_CONFIG_DIR", None)
        self.configure_herdr()
        self.quota_reading("owner@example.com", 80, 7)
        with patch.dict(os.environ, self.env):
            self.assertEqual(accounts.setup("claude2"), [])
            _ = (accounts.account_dir("claude2") / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"}, "projects": self.trusted_projects()}))
        _ = self.select_unit_account("claude2")
        self.prepare_herdr_launch("alpha", "--plan", "docs/plans/given.md")

    def quota_reading(self, login: str, left: float, reset_days: float) -> None:
        with patch.dict(os.environ, self.env):
            stored_room(login, left, datetime.now(timezone.utc) + timedelta(days=reset_days))

    def select_unit_account(self, label: str) -> Path:
        with patch.dict(os.environ, self.env):
            path = accounts.groups_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(json.dumps({"units": label, "showrunners": "claude2"}))
            return accounts.account_dir(label)

    def trusted_projects(self) -> dict[str, dict[str, bool]]:
        return {str(folder): {"hasTrustDialogAccepted": True}
                for folder in (self.checkout, self.root / "prior-session", self.root / "prior session")}

    def prepare_unit_account(self, label: str) -> Path:
        directory = self.select_unit_account(label)
        parsed = parse_account_label(label)
        assert isinstance(parsed, NumberedAccountLabel)
        login = f"{parsed.tool}-{parsed.number}@example.invalid"
        with patch.dict(os.environ, self.env):
            self.assertEqual(accounts.setup(label), [])
            _ = (directory / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": login},
                            "projects": self.trusted_projects()}),
                encoding="utf-8",
            )
            _ = (Path(self.env["AGENT_NOTES_DIR"]) / note_name(label)).write_text(
                f"---\nlogin: {login}\nsessions: 1\n---\n", encoding="utf-8"
            )
        self.quota_reading(login, 1, 2)
        return directory

    def persist_unit_account(self, directory: Path, *, session: str = "recorded-herdr", model: str = "opus") -> None:
        self.persist_herdr_unit(session, self.root)
        environment = self.root / "proc/202/environ"
        _ = environment.write_text(environment.read_text() + f"\0CLAUDE_CONFIG_DIR={directory}")
        from scripts.shutdown import pane_record
        with patch.object(pane_record, "RECORD_DIR", Path(self.env["HERDR_PANE_RECORD_DIR"])):
            pane_record.write(session, pane_record.launch("w1:p1", "62ccbd64-f22e-5647-b20e-7899aa39d766", str(self.root),
                {"CLAUDE_CONFIG_DIR": str(directory)},
                ["claude", "-n", "alpha", "--model", model]))
        transcript = Path(self.env["HOME"]) / ".claude/projects/project/62ccbd64-f22e-5647-b20e-7899aa39d766.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")

    def write_agent_config(self, *, family: str = "claude", director: str = "opus:xhigh",
                           claude_set: bool = True) -> None:
        overrides: dict[str, dict[str, str | None]] = {
            "assignments": {"production": family},
            "production.claude": {"director": director if claude_set else None},
        }
        if family == "codex":
            overrides["production.codex"] = {"director": "gpt-test:high"}
        _ = write_registry(self.agent_config, overrides)

    def production_doc(self, merge_branch: str = "build-followups") -> str:
        return ("# Production\n\n"
                f"- **Merge branch:** `{merge_branch}` — unit checkpoints merge here\n"
                f"- **Showrunner checkout:** `{self.checkout}`\n"
                "- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n"
                "- **User zone:** America/Los_Angeles — reports use PDT\n")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.checkout,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))
        return result.stdout.strip()

    def server_listener(self, server: Path, name: str) -> None:
        config = Path(self.env.get("XDG_CONFIG_HOME", str(Path(self.env["HOME"]) / ".config"))) / "herdr"
        table = self.root / "proc/net/unix"
        table.parent.mkdir(parents=True, exist_ok=True)
        line = f"0: 2 0 00010000 0001 01 {server.name} {config}/sessions/{name}/herdr.sock\n"
        previous = table.read_text() if table.exists() else ""
        if line not in previous:
            _ = table.write_text(previous + line)
        (server / "fd").mkdir(exist_ok=True)
        (server / "fd/3").unlink(missing_ok=True)
        (server / "fd/3").symlink_to(f"socket:[{server.name}]")

    def configure_herdr(self, request: add_unit.UnitLaunch | add_unit.NoLabeledSession = NO_LAUNCH_FIXTURE) -> None:
        """Fake herdr answers a launch as a tab of the shared session, whose server has a boot unit."""
        responses: dict[str, CommandResponse | list[CommandResponse]] = {
            "--session default session list --json": CommandResponse(code=0, stdout='{"sessions": []}'),
            f"--user show -p FragmentPath --value herdr-{SHARED}.service": CommandResponse(
                code=0, stdout=f"{self.env['HOME']}/.config/systemd/user/herdr-{SHARED}.service\n"),
            "default": CommandResponse(code=0, stdout=json.dumps({"result": {
                "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": []}})),
        }
        responses["agent get"] = CommandResponse(code=0, stdout=json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
        # An already launched fixture advertises its session on subsequent CLI calls.
        sessions = [herdr.SessionEntry(name=path.name.removeprefix("herdr-live-"), running=True,
                    default=False, session_dir="/fixture", socket_path="/fixture/socket")
                    for path in self.state.glob("herdr-live-*")]
        responses["--session default session list --json"] = CommandResponse(
            code=0, stdout=json.dumps(herdr.SessionListResult(sessions=sessions)))
        if isinstance(request, add_unit.UnitLaunch):
            self.env["FAKE_CLAUDE_PID"] = str(FAKE_CLAUDE_PID)
            registry = Path(self.env["NOTIFIER_SESSIONS_DIR"]) / f"{FAKE_CLAUDE_PID}.json"
            if registry.exists():
                record = cast(dict[str, object], json.loads(registry.read_text()))
                record["bridgeSessionId"] = "session_bridge" if (self.state / "ready").exists() else ""
                _ = registry.write_text(json.dumps(record))
            parent = fixture_runtime_id("showrunner::" + request.production.name)
            self.env["FAKE_CLAUDE_SESSION_ID"] = (request.session.session_id
                if isinstance(request.session, add_unit.ResumedSession)
                else fixture_runtime_id("unit:" + parent + ":" + request.identity.session))
            name = SHARED
            title = request.identity.session
            responses[f"--session {name} agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({"result": {"agent": {"agent_status": "idle"}}}))
            responses[f"--session {name} status server"] = [
                CommandResponse(code=0, stdout="status: stopped\n"),
                CommandResponse(code=0, stdout="status: running\n")]
            responses[f"--session {name} workspace list"] = CommandResponse(
                code=0, stdout=json.dumps({"result": {"workspaces": []}}))
            responses[f"--session {name} pane list"] = CommandResponse(code=0, stdout=json.dumps({
                "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": title}]}}))
            responses[f"--session {name} pane read w1:p1 --source visible --format ansi"] = CommandResponse(
                code=0, stdout="────────\n❯ \n────────")
            process = self.root / "proc/102"
            server = self.root / "proc/101"
            if sessions and not any(session["name"] == name for session in sessions):
                server = self.root / "proc/103"
                process = self.root / "proc/104"
            responses[f"--session {name} pane process-info --pane w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
                "result": {"process_info": {"shell_pid": int(process.name),
                    "foreground_process_group_id": int(process.name), "foreground_processes": [
                    {"pid": int(process.name), "name": "zsh", "argv": ["zsh"]}]}}}))
            if not sessions or not any(session["name"] == name for session in sessions):
                write_process(self.env, int(server.name), 1, ["herdr", "--session", name, "server"], {})
                write_process(self.env, int(process.name), int(server.name), ["zsh"],
                    {}, cwd=request.session_cwd)
            self.env["FAKE_SHELL_PID"] = process.name
            self.server_listener(server, name)
            running = CommandResponse(code=0, stdout=json.dumps(herdr.SessionListResult(sessions=[
                *[session for session in sessions if session["name"] != name],
                herdr.SessionEntry(name=name, running=True, default=False,
                                   session_dir="/fixture", socket_path="/fixture/socket")])))
            after = {**responses, "--session default session list --json": running,
                f"--session {name} pane list": CommandResponse(code=0, stdout=json.dumps({"result": {"panes": [
                    {"pane_id": "w1:p1", "tab_id": "w1:t1", "label": title,
                     "agent_session": {"agent": "claude", "kind": "id", "source": "herdr:claude",
                        "value": request.session.session_id if isinstance(request.session, add_unit.ResumedSession)
                        else self.env["FAKE_CLAUDE_SESSION_ID"]}}]}})),
                }
            responses[f"--session {name} agent prompt w1:p1 {script_prompt(add_unit.prompt_for(request))}"] = CommandResponse(
                code=0, stdout="", write_files={str(self.state / f"herdr-live-{name}"): "",
                    self.env["FAKE_HERDR_RESPONSES"]: json.dumps(after)})
        responses.update(self.herdr_overrides)
        if isinstance(request, add_unit.UnitLaunch):
            confirm_tab_close(responses, SHARED, "w1:t1", Path(self.env["FAKE_HERDR_RESPONSES"]))
        if isinstance(request, add_unit.UnitLaunch) and (self.root / "proc/202").exists():
            after_stop = {**responses, "--session default session list --json": CommandResponse(code=0, stdout='{"sessions": []}')}
            stopped_name = (self.root / "proc/201/cmdline").read_text().split("\0")[2]
            responses[f"--session {stopped_name} server stop"] = CommandResponse(code=0, stdout="",
                remove_paths=[str(self.root / "proc/202")],
                write_files={self.env["FAKE_HERDR_RESPONSES"]: json.dumps(after_stop)})
        _ = Path(self.env["FAKE_HERDR_RESPONSES"]).write_text(json.dumps(responses))

    def prepare_herdr_launch(self, *args: str) -> None:
        parser = argparse.ArgumentParser()
        _ = parser.add_argument("name")
        for flag in ("plan", "brief", "port", "resume", "attach", "cwd", "restart-note", "session-name", "host-session", "account", "config-dir", "model", "autocompact"):
            _ = parser.add_argument("--" + flag, type=int if flag == "port" else str)
        _ = parser.add_argument("--standby", action="store_true")
        _ = parser.add_argument("--permanent", action="store_true")
        _ = parser.add_argument("--responsibility", action="append", default=[])
        _ = parser.add_argument("--check", action="store_true")
        _ = parser.add_argument("--probe", action="store_true")
        _ = parser.add_argument("--source-fallback", action="store_true")
        _ = parser.add_argument("--timeout", type=float, default=90)
        parsed = parser.parse_args(args)
        parsed.production = self.production_name
        try:
            requested = add_unit.launch_request(parsed)
            launch = add_unit.UnitLaunch(production=requested.production, branch=requested.branch,
                worktree=requested.worktree, plan=requested.plan, port=requested.port,
                session=requested.session, timeout=requested.timeout, responsibilities=requested.responsibilities,
                identity=add_unit.UnitIdentity(requested.unit, requested.requested_name))
            self.configure_herdr(launch)
        except (Refusal, OSError, ValueError):
            self.configure_herdr()

    def cli(self, *args: str, timeout: float = 300, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        self.prepare_herdr_launch(*args)
        arguments = ["--production", self.production_name, *args]
        if subprocess_cli:
            return subprocess.run([str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", LINUX_LAUNCHER_CLI, str(SCRIPT), *arguments],
                                  env=self.env, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        return invoke_cli(add_unit, lambda: add_unit.main(arguments), arguments,
                          self.env, self.checkout, production_state=True)

    def successful(self, *args: str, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        result = self.cli(*args, subprocess_cli=subprocess_cli)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return result

    def events(self, command: str) -> list[dict[str, object]]:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        records = [cast(dict[str, object], json.loads(line)) for line in path.read_text().splitlines()]
        return [record for record in records if record["command"] == command]

    def herdr_calls(self) -> list[list[str]]:
        path = Path(self.env["FAKE_HERDR_LOG"])
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def agent_arguments(self) -> list[str]:
        row = next(row for row in self.herdr_calls() if row[2:4] == ["agent", "start"])
        return row[row.index("--") + 1:]

    def opening_prompt(self) -> str:
        return next(row[-1] for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"])

    def opened(self) -> list[str]:
        """The last workspace opened: the latest launch's."""
        return [row for row in self.herdr_calls() if row[2:4] == ["workspace", "create"]][-1]

    def pane_environment(self) -> dict[str, str]:
        row = self.opened()
        return dict(row[index + 1].split("=", 1) for index, value in enumerate(row) if value == "--env")

    def servers_started(self) -> list[list[str]]:
        """Each start of the shared session's server through its boot unit."""
        return [row for row in self.herdr_calls() if row[:2] == ["--user", "start"]]

    def launch_command(self) -> str:
        # Used by prompt-content assertions; argv and environment are checked separately.
        return shlex.join(self.agent_arguments()) + " " + shlex.quote(self.opening_prompt())

    def recorded_prompts(self, session_id: str) -> tuple[str, ...]:
        with patch.dict(os.environ, self.env):
            return conversation_pause.read_scheduled_prompts(session_id)


    def assert_director_flags(self, model: str, effort: str | None) -> None:
        command = self.launch_command()
        flags = f"--model {model}"
        if effort is not None:
            flags += f" --effort {effort}"
        self.assertIn(flags + " ", command)
        if effort is None:
            self.assertNotIn("--effort", command)

    def assert_no_launch_change(self, document: str, head: str) -> None:
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / "project-alpha").exists())
        self.assertEqual(self.servers_started(), [])


    def repository_state(self) -> tuple[str, str, str]:
        """The production doc, every branch with its commit, and every worktree with its branch."""
        return (self.doc.read_text(), self.git("for-each-ref", "refs/heads"),
                self.git("worktree", "list", "--porcelain"))

    def commit_row_off_the_naming_pattern(self, *, session_id: str = "62ccbd64-f22e-5647-b20e-7899aa39d766") -> Path:
        """Unit `trunk` as seen live: neither its staff worktree nor its branch is what the naming pattern gives."""
        worktree = self.root / "tool-based-ui-trunk"
        _ = self.git("worktree", "add", str(worktree), "-b", "init/catalyst-trunk")
        with patch.dict(self.env, {"FAKE_RESTORED_SESSION_ID": session_id}):
            _ = self.prepare_restored_worktree("trunk", worktree)
        return worktree

    def prepare_restored_worktree(self, name: str, worktree: Path, *,
                                  branch: str = "build-followups-alpha",
                                  plan: str = "docs/plans/given.md") -> staff.StaffRow:
        """Record the existing location and file a scheduled restore must keep."""
        if not worktree.exists():
            _ = self.git("worktree", "add", str(worktree), "-b", branch)
        with patch.dict(os.environ, self.env):
            row = unit_membership(self.production_name, name)
            identity = self.env.get("FAKE_RESTORED_SESSION_ID", "62ccbd64-f22e-5647-b20e-7899aa39d766")
            if row.current_session != identity:
                row = staff.update_current_session(row.current_session, staff.native_id(identity))
            row = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), worktree), expected=row.worktree)
            config_plan = (unit_config.StandbyPlan() if plan == "standby"
                           else unit_config.PlanFile(Path(plan)))
            _ = unit_config.write(worktree, unit_config.UnitConfig(config_plan, unit_config.NoAppPort(), ()))
        return row

    def run_with_labeled_session(
        self,
        claude: unit_lookup.Claude,
        *,
        already_labeled: bool = True,
        place: unit_lookup.Place = MARKED_HERDR_PLACE,
        outcome: herdr.PromptSubmitOutcome = SUBMITTED_PROMPT,
    ) -> tuple[int, str, list[list[str]], int, int]:
        request = add_unit.UnitLaunch(
            production=read_production(self.production_name),
            identity=add_unit.UnitIdentity("alpha", "alpha"),
            branch="build-followups-alpha",
            worktree=self.root / "project-alpha",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            session=add_unit.NewSession(),
            timeout=1.0,
        )
        marked = unit_lookup.LabeledSession("alpha", claude, place, identity=unit_lookup.LiveSession(unit_membership(self.production_name, "alpha").current_session))
        launched = (marked if already_labeled else unit_lookup.LabeledSession("alpha", claude, HerdrPlace(SHARED, "w1:p1"), identity=unit_lookup.LiveSession(unit_membership(self.production_name, "alpha").current_session)))
        ready = add_unit.ReadyToLaunch(
            request,
            # A rerun or restore reuses a row. Only a fresh launch has no row.
            add_unit.ExistingUnitRow("docs/plans/given.md", request.worktree, request.branch)
            if already_labeled else add_unit.NoUnitRow(),
            add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
        )
        # These launcher tests replace worktree creation and preflight. Supply
        # their real admission inputs before intercepting launcher commands.
        if not request.worktree.exists():
            _ = self.git("worktree", "add", "-b", request.branch, str(request.worktree), "build-followups")
        with patch.dict(os.environ, self.env):
            row = unit_membership(self.production_name, request.unit_id)
            _ = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), request.worktree), expected=row.worktree)
            _ = unit_config.write(request.worktree, unit_config.UnitConfig(
                unit_config.PlanFile(Path("docs/plans/given.md")), unit_config.NoAppPort(), ()))
        git_run = subprocess.run
        commands: list[list[str]] = []
        launches: list[str] = []


        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "git":
                cwd = kwargs.get("cwd")
                assert cwd is None or isinstance(cwd, (str, Path))
                return git_run(command, cwd=cwd, capture_output=True, text=True, check=False)
            if "show-options" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        def launch(_request: add_unit.UnitLaunch, prompt: str) -> herdr.PromptSubmitOutcome:
            launches.append(prompt)
            return outcome

        error = io.StringIO()
        with (
            patch.object(add_unit, "launch_request", return_value=request),
            patch.object(add_unit, "preflight", return_value=ready),
            patch.object(
                add_unit,
                "launched_unit",
                return_value=(add_unit.LabeledSessionFound(marked) if already_labeled else add_unit.NoLabeledSession()),
            ),
            patch.object(herdr, "live_sessions", return_value=[]),
            patch.object(add_unit, "write_stub") as write_stub,
            patch.object(add_unit, "commit_unit"),
            patch.object(add_unit, "ensure_worktree"),
            patch.object(add_unit, "launch_session", side_effect=ObservedUnitLaunch(self, launch)),
            patch.object(add_unit, "wait_for_remote_control", return_value=launched) as wait,
            patch.object(add_unit, "record"),
            patch.object(subprocess, "run", side_effect=run),
            patch.dict(os.environ, self.env),
            redirect_stderr(error),
        ):
            arguments = ["--production", "unused", "alpha", "--plan", "unused"]
            result = add_unit.main(arguments)
        if result == 0:
            wait.assert_called_once_with(request)
        return result, error.getvalue(), commands, len(launches), write_stub.call_count

    def write_resume_registry_record(self, proc_start: str) -> None:
        registry = Path(self.env["NOTIFIER_SESSIONS_DIR"])
        _ = (registry / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": "62ccbd64-f22e-5647-b20e-7899aa39d766", "name": "postmaster",
            "messagingSocketPath": self.env["FAKE_CLAUDE_SOCKET"], "updatedAt": 1, "procStart": proc_start,
            "bridgeSessionId": "session_bridge",
        }))

    def prepare_attach(self, *, after_stop: tuple[int, ...] = (), after_launch: tuple[int, ...] = (202,),
                       timeout: bool = False) -> tuple[Path, list[str]]:
        prior = self.root / "prior-session"
        prior.mkdir()
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.write_resume_registry_record(started)
        source = inventory.ListedSession("62ccbd64-f22e-5647-b20e-7899aa39d766", os.getpid(), started, "postmaster", "top-level",
                                         self.root / "home/.claude", "owner@example.com", "", "")
        _ = self.enterContext(patch.object(inventory, "listing", return_value=[source]))
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        trace: list[str] = []
        stopped = False
        launched = False
        launch_session = add_unit.launch_session

        def launch(request: add_unit.UnitLaunch, director: add_unit.DirectorAgent, prompt: str,
                   selected: accounts.ClaudeLaunchAccount, *, target: str = "", model: str = "",
                   autocompact_window: str = "") -> herdr.PromptSubmitOutcome:
            nonlocal launched
            trace.append("launch")
            launched = True
            return launch_session(request, director, prompt, selected, target=target, model=model,
                                  autocompact_window=autocompact_window)

        from scripts.shutdown import stop_resume
        def stop(identity: str) -> stop_resume.Stopped | stop_resume.StopFailed:
            nonlocal stopped
            self.assertEqual(identity, "62ccbd64-f22e-5647-b20e-7899aa39d766")
            self.assert_no_launch_change(document, head)
            self.assertEqual([row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]], [])
            trace.append("stop")
            if timeout:
                return stop_resume.StopFailed(identity, "fixture stop failed")
            (Path(self.env["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json").unlink()
            stopped = True
            return stop_resume.Stopped(identity, os.getpid())

        def processes(_identity: str) -> sessions.SessionProcesses:
            return sessions.SessionProcesses(after_launch if launched else after_stop if stopped else (os.getpid(),))

        _ = self.enterContext(patch.object(stop_resume, "stop", side_effect=stop))
        _ = self.enterContext(patch.object(add_unit, "launch_session", side_effect=launch))
        _ = self.enterContext(patch.object(sessions, "session_processes", side_effect=processes))
        _ = self.enterContext(patch.object(sessions, "process_liveness", return_value=sessions.SessionNotLive()))
        _ = self.enterContext(patch.object(restart, "wait_for_confirmation", return_value=registry_confirmation(frozenset({"62ccbd64-f22e-5647-b20e-7899aa39d766"}))))
        return prior, trace

    def persist_herdr_unit(self, name: str, cwd: Path) -> None:
        row = unit_membership(self.production_name, "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id("62ccbd64-f22e-5647-b20e-7899aa39d766"))
        row = staff.set_worktree(row.id, staff.PresentWorktree(remote.local_machine(), self.root / "project-alpha"), expected=row.worktree)
        stopped = add_unit.LabeledSessionFound(unit_lookup.LabeledSession(
            "alpha", unit_lookup.ClaudeNotRunning(), HerdrPlace(name, "w2:p1"),
            identity=unit_lookup.LiveSession(row.current_session)))
        launched_unit = add_unit.launched_unit

        def observed(request: add_unit.UnitLaunch | add_unit.RequestedUnitLaunch) -> add_unit.LabeledSessionFound | add_unit.NoLabeledSession:
            if not (self.root / "proc" / str(FAKE_CLAUDE_PID)).exists():
                return stopped
            return launched_unit(request)

        _ = self.enterContext(patch.object(add_unit, "launched_unit", side_effect=observed))
        for pid, parent, arguments in ((201, 1, ["herdr", "--session", name, "server"]), (202, 201, ["zsh"])):
            process = self.root / "proc" / str(pid)
            process.mkdir(parents=True)
            _ = (process / "stat").write_text(f"{pid} (fixture) S {parent} 0")
            _ = (process / "cmdline").write_bytes("\0".join(arguments).encode())
            _ = (process / "environ").write_text("HERDR_PANE_ID=w2:p1" if pid == 202 else "")
            (process / "cwd").symlink_to(cwd)
        self.server_listener(self.root / "proc/201", name)
        (Path(self.env["HOME"]) / ".claude/sessions").mkdir(exist_ok=True)
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(herdr.SessionListResult(sessions=[
                herdr.SessionEntry(name=name, running=True, default=False, session_dir="/fixture", socket_path="/fixture/socket")])))
        self.herdr_overrides[f"--session {name} pane list"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"panes": [{"pane_id": "w2:p1", "workspace_id": "w2", "label": "alpha"}]}}))
        self.herdr_overrides[f"--session {name} server stop"] = CommandResponse(code=0, stdout="", remove_paths=[str(self.root / "proc/202")])

    def set_unit_autocompact(self, value: str, *flags: str) -> subprocess.CompletedProcess[str]:
        """A running unit's window, set as live: its row off the naming pattern and its plan since changed."""
        prior = self.root / "prior-session"
        prior.mkdir(exist_ok=True)
        return self.successful("trunk", "--plan", "docs/plans/other.md", "--resume", "3001165e-ca76-59c2-9490-878224585fad",
                               "--cwd", str(prior), "--autocompact", value, *flags)
