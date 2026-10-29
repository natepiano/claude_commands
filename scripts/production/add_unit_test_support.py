"""Private copied repositories and command fakes shared by add-unit subject suites."""

from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, cast, override
from types import ModuleType
from collections.abc import Callable, Iterator
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit
import production_units
from cli_fixture import ImmutableGitSeed, invoke_cli
import conversation_pause
import unit_lookup
import sessions
import inventory
import record as shutdown_store
import restart
from herdr import HerdrPlace
from production_units import Refusal, cell_value
from scripts.accounts import accounts
from scripts.whoami.note_file import NumberedAccountLabel, note_name, parse_account_label
import herdr
from scripts.herdr.fake_herdr import CommandResponse
import usage_store
from test_support import ShutdownTestCase


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("add_unit.py")
REAL_WORKTREE_IS_LINKED = production_units.worktree_is_linked
REAL_HERDR_RUN_PROGRAM = herdr.run_program
MARKED_HERDR_PLACE = HerdrPlace("marked-herdr", "w7:p1")
SHARED = herdr.SHARED_SESSION
NO_LAUNCH_FIXTURE = add_unit.NoMarkedUnit()
SUBMITTED_PROMPT = herdr.PromptSubmitted(datetime(2026, 10, 17, tzinfo=timezone.utc))
OLD_HEADER = ["## Units", "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
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


class AddUnitTestCase(ShutdownTestCase):
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
        cls.seed = ImmutableGitSeed.create(root, "build-followups", "Unit Test", "unit@example.invalid", {
            "docs/plans/build-followups-production.md": fixture.production_doc(),
            "docs/plans/given.md": "# Given\n\n> **Production: build-followups** — unit `alpha-unit`\n",
            ".gitignore": "docs/plans/build-followups-log.md\n",
        })

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
        # These CLI fixtures prove launcher plumbing; transcript proof is exercised
        # through the real submit method in PromptSubmissionTests.
        def submitted(client: herdr.Herdr, pane: str, opening: herdr.OpeningPrompt,
                      _at: datetime, _timeout_ms: int) -> herdr.PromptSubmitted:
            client.prompt(pane, opening.text)
            return herdr.PromptSubmitted(datetime.now(timezone.utc))

        _ = self.enterContext(patch.object(herdr.Herdr, "submit_opening_prompt", submitted))
        # Rows read in this process name worktrees that exist nowhere: answer for them here, so no
        # test asks the real disk, and a row with a worktree never asks herdr. RetiredRowTests covers the rule.
        _ = self.enterContext(patch.object(production_units, "worktree_is_linked", return_value=True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": str(self.root / "home")})
        ))
        del home_context
        self.checkout = self.root / "project-trunk"
        self.origin = self.root / "origin.git"
        self.seed.copy_to(self.checkout, self.origin)
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
        self.state = self.root / "stub-state"
        _ = self.state.mkdir()
        _ = (self.state / "ready").touch()
        self.bin = self.root / "bin"
        _ = self.bin.mkdir()
        for name in ("systemd-run", "claude"):
            command = self.bin / name
            _ = command.write_text(STUB, encoding="utf-8")
            _ = command.chmod(0o755)
        self.agent_config = self.root / "agents.conf"
        self.pause_state = self.root / "conversation-pause"
        codex_config = self.root / "codex.toml"
        codex_cache = self.root / "models.json"
        sync_state = self.root / "catalog-sync-success"
        self.write_agent_config()
        _ = sync_state.touch()
        self.env = {**os.environ, "HOME": str(self.root / "home"),
                    "HERDR_PLATFORM": "linux",
                    "NOTIFIER_SESSIONS_DIR": str(registry),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                    "STUB_STATE": str(self.state),
                    "SHOWRUNNERS_CONFIG": str(self.config), "CLAUDE_TEST_SECRET": "must-not-leak",
                    "CONVERSATION_PAUSE_STATE_DIR": str(self.pause_state),
                    "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                    "AGENT_NOTES_DIR": str(notes),
                    "AGENTS_CONFIG_FILE": str(self.agent_config),
                    "CODEX_CONFIG_FILE": str(codex_config),
                    "CODEX_MODELS_CACHE_FILE": str(codex_cache),
                    "CODEX_CATALOG_SYNC_STATE_FILE": str(sync_state)}
        self.herdr_overrides = {}
        fake_path = self.bin / "fake-herdr"
        source = (SCRIPT.parents[1] / "herdr/fake_herdr.py").read_text()
        # Match a command verb when its trailing generated session id is unknown.
        source = source.replace('responses.get("default", {"stdout": "", "code": 0})',
            'responses.get(" ".join(argv[2:4]), responses.get("default", {"stdout": "", "code": 0}))')
        source = source.replace("raise SystemExit(main(sys.argv[1:]))", "raise SystemExit(submitting_main(sys.argv[1:]))")
        hook = r'''def submitting_main(argv):
    result = main(argv)
    if result == 0 and argv[2:4] == ["agent", "prompt"]:
        from datetime import datetime, timezone
        calls = [json.loads(line) for line in Path(os.environ["FAKE_HERDR_LOG"]).read_text().splitlines()]
        start = next(row for row in reversed(calls) if row[:2] == argv[:2] and row[2:4] == ["agent", "start"])
        flag = "--resume" if "--resume" in start else "--session-id"
        session_id = start[start.index(flag) + 1]
        config = next((value.split("=", 1)[1] for row in reversed(calls) for value in row
                       if row[:2] == argv[:2] and value.startswith("CLAUDE_CONFIG_DIR=")),
                      str(Path.home() / ".claude"))
        path = Path(config) / "projects/fixture" / (session_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                                   "message": {"role": "user", "content": argv[-1]}}) + "\n")
    return result

'''
        source = source.replace('if __name__ == "__main__":', hook + 'if __name__ == "__main__":')
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
        file = Path(self.env["HOME"]) / ".local/state/agent-notes/usage.json"
        saved = cast(dict[str, usage_store.StoredUsage], json.loads(file.read_text())) if file.exists() else {}
        now = datetime.now(timezone.utc)
        saved[login] = {"weekly": {"left": left, "at": now.isoformat(),
                                  "resets": (now + timedelta(days=reset_days)).isoformat()}}
        file.parent.mkdir(parents=True, exist_ok=True)
        _ = file.write_text(json.dumps(saved))

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
        transcript = Path(self.env["HOME"]) / ".claude/projects/project/session-123.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        _ = transcript.write_text(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")

    def write_agent_config(self, *, family: str = "claude", director: str = "opus:xhigh",
                           claude_set: bool = True) -> None:
        claude_section = f"[production.claude]\ndirector={director}\n\n" if claude_set else ""
        codex_section = "[production.codex]\ndirector=gpt-test:high\n\n" if family == "codex" else ""
        content = (f"[assignments]\nproduction={family}\n\n"
                   f"{claude_section}{codex_section}"
                   "[codex.agents]\ngpt-test=low,medium,high\n\n"
                   "[claude.agents]\nopus=low,medium,high,xhigh\nsonnet=low,medium,high,xhigh\n")
        _ = self.agent_config.write_text(content, encoding="utf-8")

    def production_doc(self, merge_branch: str = "build-followups", *, session_column: bool = False) -> str:
        columns = ["Unit", "Plan", "Worktree", "Branch", *(["Session"] if session_column else []), "Port", "Owns"]
        return ("# Production\n\n"
                f"- **Merge branch:** `{merge_branch}` — unit checkpoints merge here\n"
                f"- **Showrunner checkout:** `{self.checkout}`\n"
                "- **Showrunner session:** director\n"
                "- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n"
                "- **User zone:** America/Los_Angeles — reports use PDT\n\n"
                "## Units\n\n"
                f"| {' | '.join(columns)} |\n"
                f"| {' | '.join('---' for _ in columns)} |\n\n"
                "## Gates\n")

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

    def configure_herdr(self, request: add_unit.UnitLaunch | add_unit.NoMarkedUnit = NO_LAUNCH_FIXTURE) -> None:
        """Fake herdr answers a launch as a tab of the shared session, whose server has a boot unit."""
        responses: dict[str, CommandResponse | list[CommandResponse]] = {
            "--session default session list --json": CommandResponse(code=0, stdout='{"sessions": []}'),
            f"--user show -p FragmentPath --value herdr-{SHARED}.service": CommandResponse(
                code=0, stdout=f"{self.env['HOME']}/.config/systemd/user/herdr-{SHARED}.service\n"),
            "default": CommandResponse(code=0, stdout=json.dumps({"result": {
                "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": []}})),
        }
        responses["agent get"] = CommandResponse(code=0, stdout=json.dumps({"result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
        # An already launched fixture advertises its session on subsequent CLI calls.
        sessions = [herdr.SessionEntry(name=path.name.removeprefix("herdr-live-"), running=True,
                    default=False, session_dir="/fixture", socket_path="/fixture/socket")
                    for path in self.state.glob("herdr-live-*")]
        responses["--session default session list --json"] = CommandResponse(
            code=0, stdout=json.dumps(herdr.SessionListResult(sessions=sessions)))
        if isinstance(request, add_unit.UnitLaunch):
            name = SHARED
            title = (request.launch_kind.session_name if isinstance(request.launch_kind, add_unit.UnitRestoreLaunch)
                     else request.identity.session)
            responses[f"--session {name} agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({"result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
            responses[f"--session {name} status server"] = [
                CommandResponse(code=0, stdout="status: stopped\n"),
                CommandResponse(code=0, stdout="status: running\n")]
            responses[f"--session {name} workspace list"] = CommandResponse(
                code=0, stdout=json.dumps({"result": {"workspaces": []}}))
            responses[f"--session {name} pane list"] = CommandResponse(code=0, stdout=json.dumps({
                "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": title}]}}))
            screen = "/remote-control is active" if (self.state / "ready").exists() else "Waiting for remote control"
            responses[f"--session {name} pane read w1:p1 --source recent --lines 60"] = CommandResponse(
                code=0, stdout=screen)
            responses[f"--session {name} pane read w1:p1 --source visible --format ansi"] = CommandResponse(
                code=0, stdout="────────\n❯ \n────────")
            process = self.root / "proc/102"
            server = self.root / "proc/101"
            if sessions and not any(session["name"] == name for session in sessions):
                server = self.root / "proc/103"
                process = self.root / "proc/104"
            if not sessions or not any(session["name"] == name for session in sessions):
                for path, parent, command in ((process, int(server.name), ["zsh"]), (server, 1, ["herdr", "--session", name, "server"])):
                    path.mkdir(parents=True, exist_ok=True)
                    _ = (path / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
                    _ = (path / "stat").write_text(f"{path.name} (fixture) S {parent} 0")
                    _ = (path / "status").write_text(f"Uid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n")
                (process / "cwd").unlink(missing_ok=True)
                (process / "cwd").symlink_to(request.session_cwd)
            self.server_listener(server, name)
            environment = {"HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": request.production.slug,
                           "SHOWRUNNER_UNIT_ID": request.identity.unit}
            running = CommandResponse(code=0, stdout=json.dumps(herdr.SessionListResult(sessions=[
                *[session for session in sessions if session["name"] != name],
                herdr.SessionEntry(name=name, running=True, default=False,
                                   session_dir="/fixture", socket_path="/fixture/socket")])))
            after = {**responses, "--session default session list --json": running}
            responses[f"--session {name} agent prompt w1:p1 {add_unit.prompt_for(request)}"] = CommandResponse(
                code=0, stdout="", write_files={str(process / "environ"): "\0".join(f"{key}={value}" for key, value in environment.items()),
                    str(self.state / f"herdr-live-{name}"): "",
                    self.env["FAKE_HERDR_RESPONSES"]: json.dumps(after)})
        responses.update(self.herdr_overrides)
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
        for flag in ("plan", "brief", "port", "owns", "resume", "attach", "cwd", "restart-note", "session-name", "host-session", "account", "config-dir", "model", "autocompact"):
            _ = parser.add_argument("--" + flag, type=int if flag == "port" else str)
        _ = parser.add_argument("--standby", action="store_true")
        _ = parser.add_argument("--permanent", action="store_true")
        _ = parser.add_argument("--check", action="store_true")
        _ = parser.add_argument("--probe", action="store_true")
        _ = parser.add_argument("--source-fallback", action="store_true")
        _ = parser.add_argument("--timeout", type=float, default=90)
        parsed = parser.parse_args(args)
        parsed.production = str(self.doc)
        try:
            requested = add_unit.launch_request(parsed)
            launch = add_unit.UnitLaunch(production=requested.production, branch=requested.branch,
                worktree=requested.worktree, plan=requested.plan, port=requested.port, owns=requested.owns,
                session=requested.session, launch_kind=requested.launch_kind, timeout=requested.timeout,
                identity=add_unit.UnitIdentity(requested.unit, requested.requested_name))
            self.configure_herdr(launch)
        except (Refusal, OSError, ValueError):
            self.configure_herdr()

    def cli(self, *args: str, timeout: float = 300, subprocess_cli: bool = False) -> subprocess.CompletedProcess[str]:
        self.prepare_herdr_launch(*args)
        arguments = ["--production", str(self.doc), *args]
        if subprocess_cli:
            return subprocess.run([sys.executable, str(SCRIPT), *arguments], cwd=self.checkout,
                                  env=self.env, capture_output=True, text=True, check=False, timeout=timeout)
        # The row-reader fixture patch never reached the old CLI child. Preserve
        # the real worktree check on this command boundary too.
        with patch.object(production_units, "worktree_is_linked", REAL_WORKTREE_IS_LINKED):
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

    def shutdown_record(
        self,
        state: str,
        *,
        session_id: str = "session-123",
        progress: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "login": "owner@example.com",
            "label": "claude2",
            "machine": "natedev",
            "state": state,
            "requested_at": "2026-10-09T21:49:10+00:00",
            "requested_by": {"kind": "terminal"},
            "scope": {"kind": "all account sessions"},
            "conductor": {"kind": "not started"},
            "force": "wait for ready",
            "entries": [
                {
                    "session": {
                        "kind": "unit",
                        "session_id": session_id,
                        "pid": 12345,
                        "proc_start": "process-start",
                        "name": "alpha",
                        "cwd": "/tmp/alpha",
                        "status": "idle",
                        "model": {"kind": "no reply yet"},
                        "checkout": {"kind": "not a checkout"},
                        "run_dirs": [],
                        "codex_servers": [],
                        "timers": [],
                        "host": {
                            "kind": "unit",
                            "production": "build-followups",
                            "unit": "alpha-unit",
                            "doc": str(self.doc),
                            "session_name": "alpha",
                            "session_host": "herdr",
                            "plan": {
                                "kind": "plan",
                                "path": str(self.plan),
                            },
                        },
                    },
                    "timers": [],
                    "settle_message": {"kind": "not sent"},
                    "where": {"kind": "not said"},
                    "progress": progress or {"kind": "waiting"},
                    "stop_issues": [],
                }
            ],
            "stop_issues": [],
        }

    def write_shutdown_record(self, value: object, *, login: str = "owner@example.com") -> None:
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / login
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    def assert_director_flags(self, model: str, effort: str | None) -> None:
        command = self.launch_command()
        flags = f"--model {model}"
        if effort is not None:
            flags += f" --effort {effort}"
        self.assertIn(flags + " ", command)
        if effort is None:
            self.assertNotIn("--effort", command)

    def unit_row(self, name: str = "alpha", *, branch: str = "build-followups-alpha",
                 worktree: Path | None = None, plan: str = "docs/plans/given.md",
                 port: str = "—", owns: str = "—") -> str:
        target = worktree or self.root / "project-alpha"
        return f"| {name}-unit | {plan} | {target} | {branch} | {port} | {owns} |"

    def prepared_row(self, *, name: str = "alpha", plan: str = "docs/plans/given.md",
                     worktree: Path | None = None, branch: str = "build-followups-alpha") -> str:
        target = worktree or self.root / "project-alpha"
        return (f"| `{name}-unit` | `{plan}` (ready to launch) | `{target}` | "
                f"`{branch}` | 8123 | `src/alpha` — assigned files |")

    def commit_prepared_row(self, row: str) -> None:
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"),
                                encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md")
        _ = self.git("commit", "-m", "prepare unit rows")
        _ = self.git("push", "origin", "build-followups")

    def assert_no_launch_change(self, document: str, head: str) -> None:
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse((self.root / "project-alpha").exists())
        self.assertEqual(self.servers_started(), [])

    def restore(self, name: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        return self.cli(name, "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(cwd),
                        "--restart-note", str(note), "--session-name", "recorded-name",
                        "--host-session", "recorded-herdr", "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

    def repository_state(self) -> tuple[str, str, str]:
        """The production doc, every branch with its commit, and every worktree with its branch."""
        return (self.doc.read_text(), self.git("for-each-ref", "refs/heads"),
                self.git("worktree", "list", "--porcelain"))

    def commit_row_off_the_naming_pattern(self) -> Path:
        """Unit `trunk` as seen live: neither its worktree nor its branch is what the naming pattern gives."""
        worktree = self.root / "tool-based-ui-trunk"
        _ = self.git("worktree", "add", str(worktree), "-b", "init/catalyst-trunk")
        self.commit_prepared_row(self.unit_row("trunk", branch="init/catalyst-trunk", worktree=worktree))
        return worktree

    def run_with_marked_unit(
        self,
        launch_kind: add_unit.NewWorkLaunch | add_unit.UnitRestoreLaunch,
        claude: unit_lookup.Claude,
        *,
        already_marked: bool = True,
        place: unit_lookup.Place = MARKED_HERDR_PLACE,
        outcome: herdr.PromptSubmitOutcome = SUBMITTED_PROMPT,
    ) -> tuple[int, str, list[list[str]], int, int]:
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc),
            identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha",
            worktree=self.root / "project-alpha",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(),
            session=(add_unit.ResumedSession("session-123", self.root / "prior-session")
                     if isinstance(launch_kind, add_unit.UnitRestoreLaunch) else add_unit.NewSession()),
            launch_kind=launch_kind,
            timeout=1.0,
        )
        marked = unit_lookup.MarkedUnit("alpha-unit", claude, place)
        launched = (marked if already_marked else unit_lookup.MarkedUnit("alpha-unit", claude, HerdrPlace(SHARED, "w1:p1")))
        ready = add_unit.ReadyToLaunch(
            request,
            add_unit.NoUnitRow(),
            add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
        )
        commands: list[list[str]] = []
        launches: list[str] = []

        if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
            _ = launch_kind.note.write_text("Resume this unit.", encoding="utf-8")

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if "show-options" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "launch-blocked" in command:
                live = (
                    Path(self.env["SHUTDOWN_STATE_DIR"])
                    / "owner@example.com"
                    / "record.json"
                ).exists()
                return subprocess.CompletedProcess(
                    command,
                    0 if live else 1,
                    (
                        "claude2 is held by a shutdown "
                        + "(since 2026-10-09 14:49 PDT)\n"
                        if live
                        else ""
                    ),
                    "",
                )
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        def launch(
            _request: add_unit.UnitLaunch,
            _director: add_unit.DirectorAgent,
            prompt: str,
            _selected: accounts.ClaudeLaunchAccount,
            *, target: str = "", model: str = "", autocompact_window: str = "",
        ) -> herdr.PromptSubmitOutcome:
            self.assertEqual(target, "claude2" if isinstance(_request.session, add_unit.ResumedSession) else "")
            self.assertEqual((model, autocompact_window), ("", ""))
            if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
                recorded = self.recorded_prompts(launch_kind.session_id)
                self.assertEqual(recorded[-1], prompt)
                self.assertIs(
                    conversation_pause.prompt_source(
                        prompt, frozenset, lambda: recorded
                    ),
                    conversation_pause.PromptSource.SCHEDULED,
                )
            launches.append(prompt)
            return outcome

        error = io.StringIO()
        with (
            patch.object(add_unit, "launch_request", return_value=request),
            patch.object(add_unit, "preflight", return_value=ready),
            patch.object(
                add_unit,
                "launched_unit",
                return_value=(add_unit.MarkedUnitFound(marked) if already_marked else add_unit.NoMarkedUnit()),
            ),
            patch.object(herdr, "live_sessions", return_value=[]),
            patch.object(add_unit, "write_stub") as write_stub,
            patch.object(add_unit, "append_row"),
            patch.object(add_unit, "commit_unit"),
            patch.object(add_unit, "ensure_worktree"),
            patch.object(add_unit, "launch_session", side_effect=launch),
            patch.object(add_unit, "wait_for_remote_control", return_value=launched) as wait,
            patch.object(add_unit, "record"),
            patch.object(subprocess, "run", side_effect=run),
            patch.dict(os.environ, self.env),
            redirect_stderr(error),
        ):
            arguments = ["--production", "unused", "alpha", "--plan", "unused"]
            if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
                arguments.extend(("--config-dir", str(Path(self.env["HOME"]) / ".claude2")))
            result = add_unit.main(arguments)
        if result == 0:
            if isinstance(launch_kind, add_unit.UnitRestoreLaunch):
                wait.assert_not_called()
            else:
                wait.assert_called_once_with(request)
        return result, error.getvalue(), commands, len(launches), write_stub.call_count

    def write_resume_registry_record(self, proc_start: str) -> None:
        registry = Path(self.env["NOTIFIER_SESSIONS_DIR"])
        _ = (registry / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": "session-123", "name": "postmaster",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": proc_start,
        }))

    def prepare_attach(self, *, after_stop: tuple[int, ...] = (), after_launch: tuple[int, ...] = (202,),
                       stop_progress: str = "stopped", timeout: bool = False) -> tuple[Path, list[str]]:
        prior = self.root / "prior-session"
        prior.mkdir()
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.write_resume_registry_record(started)
        source = inventory.ListedSession("session-123", os.getpid(), started, "postmaster", "top-level",
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

        def shutdown(arguments: list[str], within: float) -> subprocess.CompletedProcess[str]:
            nonlocal stopped
            self.assertGreater(within, 0)
            self.assert_no_launch_change(document, head)
            self.assertEqual([row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]], [])
            trace.append(arguments[0])
            if arguments[0] == "down":
                self.assertEqual(arguments[:6], ["down", "owner@example.com", "--here", "--only", "session-123", "--request-id"])
                self.write_shutdown_record(self.shutdown_record("down", progress={"kind": stop_progress,
                                                                                 "at": "2026-10-28T20:00:00+00:00"}))
                _ = shutdown_store.migrate("owner@example.com")
                def select_request(saved: shutdown_store.SessionShutdown) -> None:
                    saved["request_id"] = arguments[6]
                    saved["hold"] = "session"
                _ = shutdown_store.update_session("owner@example.com", "session-123", select_request)
                (Path(self.env["NOTIFIER_SESSIONS_DIR"]) / f"{os.getpid()}.json").unlink()
                stopped = True
            else:
                self.assertEqual(arguments[:3], ["follow", "owner@example.com", "--request"])
                if timeout:
                    raise subprocess.TimeoutExpired(arguments, within)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def processes(_identity: str) -> sessions.SessionProcesses:
            return sessions.SessionProcesses(after_launch if launched else after_stop if stopped else (os.getpid(),))

        def finish(records: list[shutdown_store.SessionShutdown]) -> restart.TargetCheck:
            trace.append("finish")
            self.assertTrue(launched)
            return restart.TargetCheck([], records)

        _ = self.enterContext(patch.object(add_unit, "run_attach_shutdown", side_effect=shutdown))
        _ = self.enterContext(patch.object(add_unit, "launch_session", side_effect=launch))
        _ = self.enterContext(patch.object(sessions, "session_processes", side_effect=processes))
        _ = self.enterContext(patch.object(sessions, "process_liveness", return_value=sessions.SessionNotLive()))
        _ = self.enterContext(patch.object(restart, "wait_for_confirmation", return_value=restart.RegistryConfirmation()))
        _ = self.enterContext(patch.object(restart, "clear_running_on_target", side_effect=finish))
        return prior, trace

    def persist_herdr_unit(self, name: str, cwd: Path) -> None:
        for pid, parent, arguments in ((201, 1, ["herdr", "--session", name, "server"]), (202, 201, ["zsh"])):
            process = self.root / "proc" / str(pid)
            process.mkdir(parents=True)
            _ = (process / "stat").write_text(f"{pid} (fixture) S {parent} 0")
            _ = (process / "cmdline").write_bytes("\0".join(arguments).encode())
            _ = (process / "environ").write_text("HERDR_PANE_ID=w2:p1\0SHOWRUNNER_UNIT=build-followups\0SHOWRUNNER_UNIT_ID=alpha-unit" if pid == 202 else "")
            (process / "cwd").symlink_to(cwd)
        self.server_listener(self.root / "proc/201", name)
        (Path(self.env["HOME"]) / ".claude/sessions").mkdir(exist_ok=True)
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(herdr.SessionListResult(sessions=[
                herdr.SessionEntry(name=name, running=True, default=False, session_dir="/fixture", socket_path="/fixture/socket")])))
        self.herdr_overrides[f"--session {name} server stop"] = CommandResponse(code=0, stdout="", remove_paths=[str(self.root / "proc/202")])

    def set_unit_autocompact(self, value: str, *flags: str) -> subprocess.CompletedProcess[str]:
        """A running unit's window, set as live: its row off the naming pattern and its plan since changed."""
        prior = self.root / "prior-session"
        prior.mkdir(exist_ok=True)
        return self.successful("trunk", "--plan", "docs/plans/other.md", "--resume", "session-trunk",
                               "--cwd", str(prior), "--autocompact", value, *flags)

    def assert_named_unit_launch_and_restart(self, unit: str) -> None:
        self.doc = self.checkout / "docs/plans/sample-production.md"
        row = self.unit_row(unit, branch="sample-" + unit, worktree=self.root / ("project-" + unit))
        row = row.replace(f"| {unit}-unit |", f"| `{unit}` |")
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"), encoding="utf-8")
        _ = self.git("add", str(self.doc.relative_to(self.checkout)))
        _ = self.git("commit", "-m", "Add sample fixture production")
        _ = self.git("push", "origin", "build-followups")

        result = self.successful(unit, "--plan", "docs/plans/given.md")
        self.assertIn(f"{unit} started", result.stdout)
        rows = list(production_units.unit_table(self.doc.read_text().splitlines()))
        self.assertEqual([cell_value(row["Unit"]) for row in rows], [unit])
        self.assertEqual(self.pane_environment()["SHOWRUNNER_UNIT_ID"], unit)
        self.assertEqual(self.pane_environment()["SHOWRUNNER_UNIT"], "sample")
        with patch.dict(os.environ, self.env):
            marked = unit_lookup.marked_unit("sample", unit)
            self.assertIsNotNone(marked)
            if marked is not None:
                self.assertEqual(marked.unit, unit)
                self.assertEqual(marked.place.session, "agents")
            self.assertIsNone(unit_lookup.marked_unit("sample", unit + "-unit"))

        note = self.root / "unit-restart.md"
        _ = note.write_text("Resume the unit work.\n", encoding="utf-8")
        request = add_unit.launch_request(argparse.Namespace(
            production=str(self.doc), name=unit, plan="docs/plans/given.md", brief=None,
            standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
            resume="unit-session", cwd=str(self.root / ("project-" + unit)),
            restart_note=str(note), session_name=unit, host_session="sample-" + unit, probe=False,
        ))
        self.assertEqual(request.unit, unit)
        self.assertIsInstance(request.launch_kind, add_unit.UnitRestoreLaunch)
        self.assertIsInstance(add_unit.row_is_present(request, self.doc.read_text().splitlines()),
                              add_unit.ExistingUnitRow)
        before = self.doc.read_bytes()
        resumed = self.cli(unit, "--plan", "docs/plans/given.md")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)


