"""Exercise add_unit through its command line with isolated git and launcher state."""

from __future__ import annotations

from scripts.lib import names
from scripts.lib.test_names import fixture_names

import argparse
import dataclasses
import fcntl
import io
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar, TypedDict, cast, final, override
from unittest.mock import call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shutdown"))
import add_unit
import autocompact
import production_units
from cli_fixture import ImmutableGitSeed, invoke_cli
import conversation_pause
import launch_permission
import unit_lookup
import session_pane
import sessions
import inventory
import record as shutdown_store
import restart
from herdr import HerdrPlace
from production_units import Refusal, cell_value, live_unit_table, plan_cell_is_retired, retired_units
from scripts.accounts import accounts
from scripts.whoami.note_file import NumberedAccountLabel, note_name, parse_account_label
import herdr
from scripts.herdr.fake_herdr import CommandResponse
import usage_store
from test_support import ShutdownTestCase


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("add_unit.py")
REAL_WORKTREE_IS_LINKED = production_units.worktree_is_linked
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


def setUpModule() -> None:
    """The times these tests expect are Pacific; the user's zone setting must not change them."""
    unittest.enterModuleContext(patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


@final
class AddUnitTests(ShutdownTestCase):
    seed: ClassVar[ImmutableGitSeed]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="AddUnitTests-seed-")))
        cls.seed = ImmutableGitSeed.create(root, 'build-followups', 'Unit Test', 'unit@example.invalid', {})

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root = Path()
        self.checkout = Path()
        self.origin = Path()
        self.doc = Path()
        self.plan = Path()
        self.log = Path()
        self.config = Path()
        self.prompt = Path()
        self.state = Path()
        self.bin = Path()
        self.agent_config = Path()
        self.pause_state = Path()
        self.env: dict[str, str] = {}
        self.herdr_overrides: dict[str, CommandResponse | list[CommandResponse]] = {}

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(patch.object(names, "load_names", return_value=fixture_names()))
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
        _ = self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        self.plan = self.checkout / "docs/plans/given.md"
        _ = self.plan.write_text("# Given\n\n> **Production: build-followups** — unit `alpha-unit`\n", encoding="utf-8")
        _ = (self.checkout / ".gitignore").write_text("docs/plans/build-followups-log.md\n", encoding="utf-8")
        _ = self.git("add", ".")
        _ = self.git("commit", "-m", "initial")
        _ = self.git("push", "-u", "origin", "build-followups")
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

    def test_untrusted_new_worktree_refuses_before_stub_row_or_worktree(self) -> None:
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text(json.dumps({"oauthAccount": {"emailAddress": "owner@example.com"},
                                         "projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in ([], ["--check"]):
            with self.subTest(mode=mode):
                result = self.cli("alpha", "--brief", "Add a feature", *mode)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                folder = self.root / "project-alpha"
                self.assertIn(f"alpha-unit: Claude is not trusted in {folder} on claude2. Run: "
                              + f"cd {shlex.quote(str(self.checkout))} && "
                              + f"CLAUDE_CONFIG_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude2'))} "
                              + f"CLAUDE_CODE_REMOTE_MEMORY_DIR={shlex.quote(str(Path(self.env['HOME']) / '.claude'))} claude", result.stderr)
                self.assert_no_launch_change(document, head)
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
                self.assertNotIn("build-followups-alpha", self.git("branch", "--list"))

    def test_trust_uses_launch_account_instead_of_callers_account(self) -> None:
        directory = self.prepare_unit_account("claude3")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "claude-3@example.invalid"}, "projects": {}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(Path(self.env["HOME"]) / ".claude")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--brief", "Add a feature")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("on claude3. Run:", result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={shlex.quote(str(directory))}", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_trust_file_becoming_unreadable_after_account_check_refuses_before_any_write(self) -> None:
        with patch.dict(os.environ, self.env):
            selected = accounts.role_account("unit")
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text('{"oauthAccount": "SECRET-EMAIL", ')
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        output = io.StringIO()
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stdout(output), redirect_stderr(errors),
              patch.object(add_unit, "account_for_launch", return_value=selected),
              patch.object(accounts, "check", return_value=[]),
              patch.object(launch_permission, "launch_permission",
                           return_value=launch_permission.AllowedByShutdownState())):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--brief", "Add a feature"])
        self.assertEqual(code, 2, (output.getvalue(), errors.getvalue()))
        self.assertEqual(errors.getvalue(), f"add_unit: alpha-unit: cannot read {config} "
                         + "to check Claude trust (JSONDecodeError)\n")
        self.assertNotIn("SECRET", output.getvalue() + errors.getvalue())
        self.assert_no_launch_change(document, head)
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_resume_checks_session_cwd_instead_of_worktree(self) -> None:
        prior = self.root / "untrusted-session"
        prior.mkdir()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(prior), "--account", "claude2")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"Claude is not trusted in {prior}", result.stderr)
        self.assert_no_launch_change(document, head)

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

    def test_retired_marker_only_applies_at_start_of_plan_cell(self) -> None:
        cases = (
            ("retired by the user", True),
            ("(retired by the user)", True),
            ("docs/plan.md; retired by the user", False),
            ("(run done; retired by the user)", False),
            ("retiredness", False),
            ("Retired by the user", False),
        )
        for plan_cell, expected in cases:
            with self.subTest(plan_cell=plan_cell):
                self.assertEqual(plan_cell_is_retired(plan_cell), expected)

    def test_retired_readers_use_marker_on_production_rows(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| stalls-unit | (retired by the user 2026-10-07; run done, worktree removed) follow-up: `/unit:report` shows the same sections every time, the last recorded tables when no window is open (the user, 2026-10-06 22:5x); earlier follow-up: showrunner replies end with the agent usage lines, checked by a hook (the user, 2026-10-06 08:1x), then `docs/as-built/build-followups-fn-length-hook.md (run done; as-built 4ab5e4c)` (follow-up `commands/unit/eta_breakdown.md` merged as a9f35af) (earlier runs' as-builts `docs/as-built/screen-record.md`, merged as b3c069f, `docs/as-built/build-memory-admission.md`, merged as 35151e5, and `docs/as-built/buildlog-memory-stalls.md`) | `/home/natepiano/worktrees/claude-build-followups-stalls` | `build-followups-stalls` | `hook` (resumed in `/etc/nixos`; renamed from `nightly-config` 2026-10-04, then `stalls` → `footer` → `hook` by the user 2026-10-06, herdr session too; its own herdr session `stalls` since 22:13 PDT, after the nightly review's launcher closed the `nightly-config` session it ran in) | — | memory stall recording and its report section; then memory admission, which edited `scripts/buildlog/`, `scripts/lint/` (`invoke.sh`, `memory_gate.sh`, `sweep.py`), `scripts/direct/verify.sh` and `board.sh`, `scripts/build_hold/build_hold.py`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/agents/codex_mesh.py`, `scripts/message/` (`send.py`, `top_level.py`), `scripts/notify/pushover.py`, `scripts/whoami/agent_notes.py`, `scripts/production/dailies_render.py`, `settings.json`, `config/lint.conf`, `pyrightconfig.json`, and `commands/` (`build_hold.md`, `lint_config.md`, `notify_top_level.md`, `showrunner/dailies.md`, `showrunner/produce.md`); promoted by the user 2026-10-04; then the screen recording skill: `commands/screen_record.md`, `scripts/screen_record/`; and the `/unit:report` timer fixes: `scripts/production/unit_status.sh`, `scripts/direct/progress_history.py`; then the Codex `systemError` fix: `scripts/agents/codex_mesh.py`, `scripts/agents/test_codex_mesh.py`; then the kill order: `scripts/lint/invoke.sh`, `scripts/lint/test_invoke_scope.py`, `docs/as-built/build-memory-admission.md`; then `/unit:eta_breakdown`: `commands/unit/eta_breakdown.md`; then the function-length hook: `scripts/hooks/fn_length_lib.py`, `scripts/hooks/post-tool-use-fn-length.py`, `scripts/hooks/test_fn_length.py`, `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`, `settings.json`; then the footer: `scripts/production/dailies_render.py`, a showrunner-only Stop hook under `scripts/hooks/`, `settings.json`, `commands/showrunner/produce.md`; `commands/unit/report.md` (the report follow-up) |",
            "| enh-showrunner-unit | `docs/plans/build-followups-enh-showrunner-dailies.md` (follow-up: retired units drop out of the status and a simple dailies groups idle units; the user, 2026-10-07; the earlier run's as-built is `docs/as-built/showrunner-automation.md`) (Phases 7 and 8 of stalls-unit's plan, moved by the user 2026-10-06 14:2x PDT) | `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` | `build-followups-enh-showrunner` | `enh-showrunner` | — | `docs/as-built/showrunner-automation.md`; `scripts/production/add_unit.py`, `test_add_unit.py`, `merge_checkpoint.py`, `test_merge_checkpoint.py`; `commands/showrunner/add_unit.md`; after stalls-unit Phase 6 merges: `commands/showrunner/produce.md`, `commands/showrunner/promote_unit.md`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py` and their tests (hub rows); from its Phase 5 (the user, 2026-10-06 15:25 PDT, the phase-end split): `commands/unit/direct.md`, `commands/unit/checkpoint.md`, `docs/direct/run_phase_review.md`, `docs/production_format.md`, `commands/unit/add_ons.md`, `commands/plan/shrink.md`, `commands/plan/phase_review.md`, `docs/direct/phase_end.md`, `docs/direct_plan_format.md`, `commands/unit/eta_breakdown.md`, `docs/direct/final_gate_commit.md` |",
            "| model-study-unit | `docs/as-built/director-model-study.md` (run done; as-built 16e5ac6) | `/home/natepiano/worktrees/claude-build-followups-model-study` | `build-followups-model-study` | `model-study` | — | `docs/plans/build-followups-model-study.md`; `scripts/model_study/`; `docs/as-built/director-model-study-results.md` |",
        ]
        self.assertEqual(retired_units(lines, "build-followups"), {"stalls-unit"})
        self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "build-followups")],
                         ["enh-showrunner-unit", "model-study-unit"])

    def test_retired_units_include_retired_name_and_exclude_live_name(self) -> None:
        lines = [
            "## Units",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| `old-unit` | retired after completion | /tmp/old | old | old | — | — |",
            "| `live-unit` | docs/live.md | /tmp/live | live | live | — | — |",
        ]
        self.assertEqual(retired_units(lines, "show"), {"old-unit"})

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

    def test_restore_reports_not_submitted_to_its_parent_and_does_not_report_launched(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr")
        output = io.StringIO()
        with redirect_stdout(output):
            code, error, _commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), already_marked=False,
                outcome=herdr.PromptNotSubmitted("fixture prompt remains in input"))
        self.assertEqual(code, 1)
        self.assertEqual(launches, 1)
        self.assertIn("alpha-unit", error)
        self.assertIn("restart prompt not submitted", error)
        self.assertNotIn("restore launched", output.getvalue())
        line, = output.getvalue().splitlines()
        self.assertTrue(line.startswith("restart-prompt-outcome: "))
        self.assertEqual(herdr.decode_prompt_outcome(line.removeprefix("restart-prompt-outcome: ")),
                         herdr.PromptNotSubmitted("fixture prompt remains in input"))

    def test_restore_returns_zero_without_waiting_for_remote_control(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "build-followups-recorded-herdr"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), already_marked=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "restart-prompt-outcome: " + herdr.encode_prompt_outcome(SUBMITTED_PROMPT) + "\nalpha-unit restore launched: herdr session attach agents, workspace director, tab recorded-name\n")

    def test_restore_already_running_reports_missing_submit_proof_without_waiting_for_remote_control(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123)
            )
        self.assertEqual(result, 1)
        self.assertIn("restart prompt not submitted", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertNotIn("restore launched", output.getvalue())
        self.assertIn("restart-prompt-outcome: ", output.getvalue())

    def test_new_work_launch_still_waits_for_remote_control(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(
                add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning(), already_marked=False
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 1)
        self.assertEqual(output.getvalue(), "alpha-unit started: herdr session attach agents, workspace director, tab alpha\n")


    def test_restore_replaces_stopped_herdr_unit_by_name_by_herdr_name(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )
        with patch("herdr.stop_session") as stopped:
            result, error, commands, launches, writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning(), place=HerdrPlace("native-unit", "w1:p1")
            )
        self.assertEqual(result, 0, error)
        stopped.assert_called_once_with("native-unit")
        self.assertEqual(commands, [])
        self.assertEqual((launches, writes), (1, 1))

    def test_restore_already_live_in_herdr_launches_nothing_and_reports_missing_submit_proof(self) -> None:
        kind = add_unit.UnitRestoreLaunch("session-123", self.root / "restart-note", "recorded-name", "native-unit")
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, _writes = self.run_with_marked_unit(kind,
                unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123),
                place=HerdrPlace("native-unit", "w1:p1"))
        self.assertEqual(result, 1)
        self.assertIn("restart prompt not submitted", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertNotIn("restore launched", output.getvalue())
        self.assertIn("restart-prompt-outcome: ", output.getvalue())

    def test_unit_start_not_ready_prints_screen_and_sends_no_prompt_or_key(self) -> None:
        screen = "Do you trust this folder?"
        self.herdr_overrides["agent start"] = CommandResponse(
            code=1, stdout=json.dumps({"error": {"code": "agent_not_ready", "message": "dialog"}}))
        self.herdr_overrides[f"--session {SHARED} pane read w1:p1 --source visible"] = CommandResponse(code=0, stdout=screen)
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("add_unit: the session is not ready:\n" + screen, result.stderr)
        self.assertFalse(any(row[2:4] == ["server", "stop"] for row in self.herdr_calls()))
        self.assertFalse(any(row[2:4] in (["agent", "prompt"], ["pane", "send-keys"], ["pane", "send-text"])
                             for row in self.herdr_calls()))

    def test_each_production_names_its_unit_session_and_a_restore_keeps_the_recorded_name(self) -> None:
        production = add_unit.read_production(self.doc)
        request = add_unit.UnitLaunch(
            production=production, identity=add_unit.UnitIdentity("review-unit", "review"),
            branch="build-followups-review", worktree=self.root / "project-review",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")), port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(), session=add_unit.NewSession(), launch_kind=add_unit.NewWorkLaunch(),
            timeout=90)
        hana = dataclasses.replace(request, production=production._replace(slug="hana"))
        self.assertEqual(add_unit.host_session_name(request), "build-followups-review")
        self.assertEqual(add_unit.host_session_name(hana), "hana-review")
        named = dataclasses.replace(request, identity=add_unit.UnitIdentity("review-unit", "build-followups-review"))
        self.assertEqual(add_unit.host_session_name(named), "build-followups-review")
        style = dataclasses.replace(request, production=production._replace(slug="cargo-liner-style"),
                                    identity=add_unit.UnitIdentity("cargo-liner-style-names-unit", "cargo-liner-style-names"))
        self.assertEqual(add_unit.host_session_name(style), "cargo-liner-style-names")
        restore = add_unit.UnitRestoreLaunch("session-123", self.root / "note", "review", "build-followups-review")
        self.assertEqual(add_unit.host_session_name(dataclasses.replace(request, launch_kind=restore)),
                         "build-followups-review")
        # A record from before the slug led: its bare name may be another production's live session.
        before = add_unit.UnitRestoreLaunch("session-123", self.root / "note", "review", "review")
        self.assertEqual(add_unit.host_session_name(dataclasses.replace(hana, launch_kind=before)), "hana-review")

    def test_failed_unit_restore_closes_its_tab_after_agent_exit(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note"
        _ = note.write_text("Resume this unit.")
        with (patch.dict(os.environ, self.env),
              patch.object(herdr, "start_agent", side_effect=herdr.AgentExited("restored-unit", "w1:p1")),
              patch.object(herdr, "close_agent") as closed):
            request = add_unit.UnitLaunch(
                production=add_unit.read_production(self.doc),
                identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
                branch="build-followups-alpha", worktree=self.root / "project-alpha",
                plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
                port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(),
                session=add_unit.ResumedSession("session-123", prior),
                launch_kind=add_unit.UnitRestoreLaunch("session-123", note, "recorded-name", "build-followups-alpha"),
                timeout=90,
            )
            self.configure_herdr(request)
            with self.assertRaises(herdr.AgentExited):
                _ = add_unit.launch_session(request, add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
                                        "Resume this unit.", accounts.role_account("unit"))
        closed.assert_called_once_with(SHARED, "w1:p1")
        self.assertEqual(self.servers_started(), [["--user", "start", "herdr-agents.service"]])
        self.assertFalse(any(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()))

    def test_a_prompt_never_typed_closes_the_new_session_so_a_rerun_starts_it(self) -> None:
        for unit, typed, ending in (("alpha", False, "its session is closed: run the same command again"),
                                    ("beta", True, "prompt typed 1 times")):
            with self.subTest(typed=typed):
                place = herdr.HerdrPlace(SHARED, "w1:p1")
                found = add_unit.MarkedUnitFound(unit_lookup.MarkedUnit(f"{unit}-unit", unit_lookup.ClaudeNotRunning(), place))
                outcome = herdr.PromptNotSubmitted("destination never became ready for input"
                                                   + ("; prompt typed 1 times" if typed else ""), never_typed=not typed)
                with (patch.object(herdr, "start_agent", return_value=outcome),
                      patch.object(add_unit, "launched_unit", side_effect=[add_unit.NoMarkedUnit(), found]),
                      patch.object(herdr, "close_agent") as closed):
                    result = self.cli(unit, "--plan", "docs/plans/given.md")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue(result.stderr.rstrip().endswith(ending), result.stderr)
                self.assertEqual(closed.call_args_list, [] if typed else [call(SHARED, "w1:p1")])

    def test_configured_tab_title_keeps_pane_session_and_attach_keys(self) -> None:
        with patch.object(names, "runtime_names", return_value=fixture_names(director="coordinator")):
            result = self.successful("alpha", "--plan", "docs/plans/given.md")
        calls = self.herdr_calls()
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1", "build-followups · alpha · coordinator"], calls)
        self.assertIn(["--session", SHARED, "pane", "rename", "w1:p1", "alpha"], calls)
        start = next(row for row in calls if row[2:4] == ["agent", "start"])
        arguments = start[start.index("--") + 1:]
        self.assertEqual(arguments[arguments.index("-n") + 1], "alpha")
        self.assertIn("tab alpha", result.stdout)
        self.assertNotIn("tab build-followups ·", result.stdout)

    def test_unit_launch_passes_a_typed_opening_prompt_once(self) -> None:
        with patch.object(herdr, "start_agent", wraps=herdr.start_agent) as start:
            result = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 0)
        start.assert_called_once()
        opening = cast(object, start.call_args.args[4])
        self.assertIsInstance(opening, herdr.OpeningPrompt)
        assert isinstance(opening, herdr.OpeningPrompt)
        self.assertEqual(opening.text, "/unit:direct docs/plans/given.md")

    def test_server_start_timeout_exits_one_with_its_text_instead_of_a_traceback(self) -> None:
        timeout = subprocess.TimeoutExpired("fixture server start", 10)
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stderr(errors),
              patch.object(herdr, "start_server", side_effect=timeout) as start,
              patch.object(herdr, "open_tab") as opened,
              patch.object(herdr, "start_agent") as agent):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md"])
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), f"add_unit: {timeout}\n")
        start.assert_called_once_with(SHARED, 90_000)
        opened.assert_not_called()
        agent.assert_not_called()

    def test_a_running_shared_server_is_adopted_and_gets_the_units_tab(self) -> None:
        self.herdr_overrides[f"--session {SHARED} status server"] = CommandResponse(code=0, stdout="status: running\n")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.opened()[:6], ["--session", SHARED, "workspace", "create", "--label", "director"])

    def test_explicit_account_environment_omits_every_prepared_removal(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        flags = ("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                 "--cwd", str(prior), "--account", "claude2")
        self.prepare_herdr_launch(*flags)
        prepared = accounts.PreparedAccount({"ACCOUNT_TEST_VARIABLE": "selected", "CLAUDE_CODE_OAUTH_TOKEN": "remove-me"},
                                             frozenset({"CLAUDE_CODE_OAUTH_TOKEN"}))
        with patch.dict(os.environ, self.env), patch.object(accounts, "prepared_account", return_value=prepared):
            code = add_unit.main(["--production", str(self.doc), *flags])
        self.assertEqual(code, 0)
        self.assertEqual(self.pane_environment()["ACCOUNT_TEST_VARIABLE"], "selected")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.pane_environment())
        self.assertNotIn("claude", self.agent_arguments())
        self.assertFalse(any(argument.endswith("accounts.py") for argument in self.agent_arguments()))

    def test_transcript_model_reads_newest_file_and_crosses_chunk_boundaries(self) -> None:
        root = Path(self.env["HOME"]) / ".claude/projects"
        old = root / "old/session-123.jsonl"
        newest = root / "new/session-123.jsonl"
        for path in (old, newest):
            path.parent.mkdir(parents=True)
        _ = old.write_text(json.dumps({"type": "assistant", "message": {"model": "old-model"}}))
        _ = newest.write_text(json.dumps({"type": "assistant", "message": {"model": "opus", "content": "é" * 70000}})
                              + '\n' + json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + '\n{partial')
        os.utime(old, (1, 1))
        with patch.dict(os.environ, self.env):
            self.assertEqual(add_unit.transcript_model("session-123"), "opus")
            self.assertEqual(add_unit.transcript_model("absent"), "")

    def test_prepared_account_refusal_happens_before_any_herdr_call(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.prepare_unit_account("claude3")
        errors = io.StringIO()
        with (patch.dict(os.environ, self.env), redirect_stderr(errors),
              patch.object(accounts, "prepared_account", return_value=accounts.AccountRefused("account logged out")) as prepared):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
                                  "--resume", "session-123", "--cwd", str(prior), "--account", "claude3"])
        self.assertEqual(code, 2)
        self.assertIn("account logged out", errors.getvalue())
        prepared.assert_called_once_with("claude3", resume=True)
        self.assertEqual(self.herdr_calls(), [])

    def write_resume_registry_record(self, proc_start: str) -> None:
        registry = Path(self.env["NOTIFIER_SESSIONS_DIR"])
        _ = (registry / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "sessionId": "session-123", "name": "postmaster",
            "messagingSocketPath": "", "updatedAt": 1, "procStart": proc_start,
        }))

    def test_running_resume_refuses_before_any_launch_write(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.write_resume_registry_record(started)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in ([], ["--check"]):
            with self.subTest(mode=mode):
                result = self.cli("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior), *mode)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(result.stderr, f"add_unit: session session-123 already runs as pid {os.getpid()}; "
                                 + "to make it this unit, run add_unit with --attach session-123\n")
                self.assert_no_launch_change(document, head)
                self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])
                self.assertNotIn("build-followups-alpha", self.git("branch", "--list"))
        # Brief launches must refuse before their stub is written too.
        result = self.cli("alpha", "--brief", "Repair the unit", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])

    def test_running_restore_refuses_even_without_herdr_marks(self) -> None:
        self.commit_prepared_row(self.unit_row())
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        started = sessions.process_start(os.getpid())
        assert started is not None
        self.write_resume_registry_record(started)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.restore("alpha", prior)
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"session session-123 already runs as pid {os.getpid()}", result.stderr)
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_resume_with_unreadable_registry_refuses_with_the_reason(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        registry = Path(self.env["NOTIFIER_SESSIONS_DIR"])
        _ = (registry / "broken.json").write_text("{broken")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "add_unit: cannot tell whether session session-123 runs: "
                         + "unreadable session registry record: broken.json\n")
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])

    def test_resume_with_unreadable_process_identity_refuses_with_the_reason(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.write_resume_registry_record("recorded-start")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        with patch.object(sessions, "process_start", return_value=None):
            result = self.cli("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "add_unit: cannot tell whether session session-123 runs: "
                         + f"process identity unreadable for pid {os.getpid()}\n")
        self.assert_no_launch_change(document, head)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] != ["session", "list"]], [])

    def test_resume_with_stale_registry_identity_still_launches(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        self.write_resume_registry_record("a-previous-process-start")
        result = self.successful("alpha", "--permanent", "--resume", "session-123", "--cwd", str(prior))
        self.assertIn("alpha-unit started:", result.stdout)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")
        self.assertIn(str(prior), self.opened())
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)

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

    def test_attach_stops_then_launches_once_with_unit_marks_and_finishes(self) -> None:
        prior, trace = self.prepare_attach()
        result = self.successful("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(trace, ["down", "follow", "launch", "finish"])
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")
        self.assertEqual(self.pane_environment()[unit_lookup.PRODUCTION_MARK], "build-followups")
        self.assertEqual(self.pane_environment()[unit_lookup.UNIT_MARK], "alpha-unit")
        self.assertIn(str(prior), self.opened())
        self.assertIn("attached session-123 as alpha-unit: one process, pid 202", result.stdout)

    def test_attach_requires_cwd_supported_mode_and_exclusive_session_flag(self) -> None:
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        cases = ((["--permanent", "--attach", "session-123"], "--attach and --cwd"),
                 (["--brief", "repair", "--attach", "session-123", "--cwd", str(self.root)], "--attach requires"),
                 (["--permanent", "--attach", "session-123", "--resume", "other", "--cwd", str(self.root)], "not allowed"))
        for arguments, message in cases:
            with self.subTest(arguments=arguments):
                result = self.cli("alpha", *arguments)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(message, result.stderr)
                self.assert_no_launch_change(document, head)

    def test_attach_check_validates_without_stopping(self) -> None:
        prior, trace = self.prepare_attach()
        _ = self.successful("alpha", "--standby", "--attach", "session-123", "--cwd", str(prior), "--check")
        self.assertEqual(trace, [])

    def test_attach_refuses_absent_and_unknown_sessions_before_writes(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        document, head = self.doc.read_text(), self.git("rev-parse", "HEAD")
        for liveness, reason in ((sessions.SessionNotLive(), "session session-123 does not run; use --resume"),
                                 (sessions.SessionLivenessUnknown("registry permission denied"), "registry permission denied")):
            with (self.subTest(reason=reason), patch.object(sessions, "session_liveness", return_value=liveness),
                  patch.object(add_unit, "run_attach_shutdown") as shutdown):
                result = self.cli("alpha", "--standby", "--attach", "session-123", "--cwd", str(prior))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(reason, result.stderr)
                shutdown.assert_not_called()
                self.assert_no_launch_change(document, head)

    def test_attach_checks_launch_hold_before_requesting_shutdown(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.BlockedByShutdown("units account is held")):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("units account is held", result.stderr)
        self.assertEqual(trace, [])

    def test_attach_refuses_unclear_source_account_before_stopping(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(inventory, "listing", return_value=[]):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("source account is unclear", result.stderr)
        self.assertEqual(trace, [])

    def test_attach_timeout_leaves_shutdown_state_and_prints_cancel(self) -> None:
        prior, trace = self.prepare_attach(stop_progress="waiting", timeout=True)
        result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["down", "follow"])
        self.assertIn("cancel owner@example.com --here --only session-123", result.stderr)
        with patch.dict(os.environ, self.env):
            self.assertEqual(shutdown_store.session_records("owner@example.com")[0]["progress"]["kind"], "waiting")

    def test_attach_requires_stopped_record_even_when_follower_returns_success(self) -> None:
        prior, trace = self.prepare_attach(stop_progress="waiting")
        result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("record does not confirm a completed stop", result.stderr)
        self.assertIn("cancel owner@example.com --here --only session-123", result.stderr)
        self.assertEqual(trace, ["down", "follow"])

    def test_attach_refuses_process_still_registered_after_stop(self) -> None:
        prior, trace = self.prepare_attach(after_stop=(303,))
        result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("processes still run after shutdown; pids found: [303]", result.stderr)
        self.assertEqual(trace, ["down", "follow"])

    def test_attach_checks_old_pid_even_after_registry_record_disappears(self) -> None:
        prior, trace = self.prepare_attach()
        with patch.object(sessions, "process_liveness", return_value=sessions.LiveSessionProcess(os.getpid())):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn(f"pids found: [{os.getpid()}]", result.stderr)
        self.assertEqual(trace, ["down", "follow"])

    def test_attach_refuses_unfinished_shutdown_record_after_launch(self) -> None:
        prior, trace = self.prepare_attach()
        def unfinished(records: list[shutdown_store.SessionShutdown]) -> restart.TargetCheck:
            return restart.TargetCheck(records, [])

        with patch.object(restart, "clear_running_on_target", side_effect=unfinished):
            result = self.cli("alpha", "--permanent", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["down", "follow", "launch"])
        self.assertIn("shutdown record did not finish", result.stderr)
        self.assertNotIn("attached session-123 as", result.stdout)

    def test_attach_reports_duplicate_processes_after_launch_without_touching_them(self) -> None:
        prior, trace = self.prepare_attach(after_launch=(202, 303))
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--attach", "session-123", "--cwd", str(prior))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(trace, ["down", "follow", "launch", "finish"])
        self.assertIn("ATTACH UNPROVEN", result.stderr)
        self.assertIn("pids found: [202, 303]", result.stderr)
        self.assertNotIn("attached session-123 as", result.stdout)
        self.assertEqual(len([row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]), 1)
        self.assertEqual([row for row in self.herdr_calls() if row[2:4] in (["agent", "stop"], ["server", "stop"])], [])

    def test_herdr_resume_uses_group_account_and_last_real_transcript_model(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.persist_herdr_unit("alpha", prior)
        transcript = Path(self.env["HOME"]) / ".claude/projects/project/session-123.jsonl"
        transcript.parent.mkdir(parents=True)
        _ = transcript.write_text('\n'.join([json.dumps({"type": "assistant", "message": {"model": "opus"}}),
            json.dumps({"type": "user", "message": {"model": "ignored"}}),
            json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}), "{partial"]))
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior))
        self.assertIn("herdr session attach agents, workspace director, tab alpha", result.stdout)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(self.agent_arguments()[:4], ["--model", "opus", "--effort", "high"])
        self.assertIn(["--session", "alpha", "server", "stop"], self.herdr_calls())

    def test_herdr_resume_without_transcript_uses_director_model(self) -> None:
        self.write_agent_config(director="sonnet:high")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.persist_herdr_unit("alpha", prior)
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior))
        self.assertEqual(self.agent_arguments()[:2], ["--model", "sonnet"])
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))

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

    def test_new_work_is_refused_while_account_is_down(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            "add_unit: claude2 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT), requested in a terminal; /shutdown restart first\n",
        )
        self.assert_no_launch_change(document, head)

    def test_restart_restore_is_allowed_for_its_restarting_unit(self) -> None:
        self.commit_prepared_row(self.unit_row())
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(len(self.recorded_prompts("session-123")), 1)

    def test_restart_from_a_record_before_the_slug_led_skips_another_productions_live_session(self) -> None:
        self.commit_prepared_row(self.unit_row("review"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        # hana's review-unit holds the bare name this record was written under.
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(
            herdr.SessionListResult(sessions=[herdr.SessionEntry(name="review", running=True, default=False,
                                                                 session_dir="/fixture", socket_path="/fixture/socket")])))

        result = self.successful(
            "review", "--plan", "docs/plans/given.md", "--resume", "session-review", "--cwd", str(prior),
            "--restart-note", str(note), "--session-name", "review", "--host-session", "review",
        )

        self.assertIn("review-unit restore launched", result.stdout)
        self.assertEqual(len(self.servers_started()), 1)
        starts = [row for row in self.herdr_calls() if row[2:4] == ["agent", "start"]]
        self.assertEqual([row[4] for row in starts], ["build-followups-review"])

    def test_restart_restore_marks_only_the_probe_pane_despite_inherited_marker(self) -> None:
        self.env["CLAUDE_PROBE_SESSION"] = "1"
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        cases = ((False, ""), (True, ""), (False, "claude2"), (True, "claude2"))
        for probe, target in cases:
            with self.subTest(probe=probe, target=target):
                name = ("probe" if probe else "ordinary") + ("-move" if target else "-restore")
                self.commit_prepared_row(self.unit_row(name))
                result = self.successful(
                    name, "--plan", "docs/plans/given.md", "--resume", f"session-{name}",
                    "--cwd", str(prior), "--restart-note", str(note),
                    "--session-name", name, "--host-session", f"build-followups-{name}",
                    *(["--probe"] if probe else []),
                    *(["--account", target] if target else []),
                )
                self.assertIn(f"{name}-unit restore launched", result.stdout)
                # The first restore starts the shared server; each later one opens its tab there.
                self.assertEqual(len(self.servers_started()), 1)
                opened = self.opened()
                self.assertEqual(opened[opened.index("--cwd") + 1], str(prior))
                starts = [row for row in self.herdr_calls()
                          if row[:5] == ["--session", SHARED, "agent", "start", f"build-followups-{name}"]]
                self.assertEqual(len(starts), 1)
                arguments = starts[0][starts[0].index("--") + 1:]
                self.assertEqual(arguments[arguments.index("--resume") + 1], f"session-{name}")
                environment = self.pane_environment()
                self.assertEqual(environment.get("CLAUDE_PROBE_SESSION", ""), "1" if probe else "")
                self.assertNotIn("CLAUDE_TEST_SECRET", environment)

    def test_restart_restore_adopts_a_row_added_from_a_brief_whose_plan_is_now_its_as_built(self) -> None:
        checkout = self.doc.parents[2]
        (checkout / "docs/as-built").mkdir(parents=True, exist_ok=True)
        _ = (checkout / "docs/as-built/alpha.md").write_text("# Alpha\n", encoding="utf-8")
        row = self.unit_row(plan="docs/as-built/alpha.md")
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md", "docs/as-built/alpha.md")
        _ = self.git("commit", "-m", "production(build-followups): add unit alpha-unit (brief)")
        _ = self.git("push", "origin", "build-followups")
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha",
            "--plan",
            "docs/as-built/alpha.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.recorded_prompts("session-123")), 1)

    def test_restore_without_a_units_row_is_refused_and_adds_nothing(self) -> None:
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")

        result = self.cli(
            "alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name", "--host-session", "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr, "add_unit: unit alpha-unit has no Units row; a restore never adds one\n"
        )
        self.assert_no_launch_change(document, head)

    def test_restore_adopts_a_row_whose_worktree_and_branch_are_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()

        result = self.restore("trunk", recorded)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))
        self.assertEqual(self.repository_state(), before)

    def test_new_work_still_refuses_a_row_off_the_naming_pattern(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("trunk", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            f"add_unit: unit trunk-unit Worktree is {str(recorded)!r}; expected {str(self.checkout)!r}\n",
        )
        self.assert_no_launch_change(document, head)

    def test_a_resume_in_its_row_worktree_off_the_naming_pattern_launches_there(self) -> None:
        """hana's startup-polish row predates the naming rule (2026-10-19); its resume was refused."""
        recorded = self.commit_row_off_the_naming_pattern()
        result = self.cli("trunk", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(recorded), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        # The fixture marks only alpha-unit, so the launch ends at the mark wait, after the row check.
        self.assertNotIn("Worktree is", result.stderr)
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))

    def test_a_resume_outside_its_row_worktree_is_refused_with_exit_two(self) -> None:
        recorded = self.commit_row_off_the_naming_pattern()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("trunk", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(elsewhere), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stderr,
                         f"add_unit: unit trunk-unit resumes in {elsewhere.resolve()}; its Units row names {recorded}\n")
        self.assert_no_launch_change(document, head)

    def test_restore_in_a_cwd_other_than_the_row_worktree_launches_there(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        recorded = self.root / "prior-session"
        _ = self.git("worktree", "add", str(recorded), "-b", "build-followups-alpha")
        before = self.repository_state()

        result = self.restore("alpha", recorded)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.opened()[self.opened().index("--cwd") + 1], str(recorded))
        self.assertEqual(self.repository_state(), before)
        self.assertFalse(worktree.exists())

    def test_restore_in_the_row_worktree_on_another_branch_launches(self) -> None:
        self.commit_prepared_row(self.unit_row())
        worktree = self.root / "project-alpha"
        _ = self.git("worktree", "add", str(worktree), "-b", "other-branch")
        before = self.repository_state()

        result = self.restore("alpha", worktree)

        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.repository_state(), before)

    def test_restart_restore_is_refused_when_record_is_down(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("/shutdown restart first", result.stderr)
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_restart_restore_is_refused_when_record_does_not_hold_it(self) -> None:
        self.write_shutdown_record(
            self.shutdown_record("restarting", session_id="other-session")
        )
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertIn("/shutdown restart first", result.stderr)
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_unreadable_shutdown_state_refuses_a_restore_with_one_prefix(self) -> None:
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / "owner@example.com"
        account.mkdir(parents=True)
        _ = (account / "record.json").write_text("{", encoding="utf-8")
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan",
            "docs/plans/given.md",
            "--resume",
            "session-123",
            "--cwd",
            str(prior),
            "--restart-note",
            str(note),
            "--session-name",
            "recorded-name",
            "--host-session",
            "recorded-herdr",
        )

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertTrue(
            result.stderr.startswith("add_unit: shutdown state unreadable: "),
            result.stderr,
        )
        self.assertEqual(result.stderr.count("shutdown state unreadable: "), 1)
        self.assertEqual(self.servers_started(), [])
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_refused_marked_restore_does_not_stop_herdr_or_record_prompt(self) -> None:
        self.write_shutdown_record(self.shutdown_record("down"))
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123",
            self.root / "restart-note",
            "recorded-name",
            "recorded-herdr",
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeNotRunning()
        )

        self.assertEqual(result, 2)
        self.assertIn("/shutdown restart first", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 0)
        self.assertEqual(self.recorded_prompts("session-123"), ())

    def test_launch_barrier_covers_preflight_through_spawn(self) -> None:
        note = self.root / "restart-note"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc),
            identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha",
            worktree=self.root / "project-alpha",
            plan=add_unit.PlanGiven(Path("docs/plans/given.md")),
            port=add_unit.OmittedCell(),
            owns=add_unit.OmittedCell(),
            session=add_unit.ResumedSession(
                "session-123", self.root / "prior-session"
            ),
            launch_kind=add_unit.UnitRestoreLaunch(
                "session-123", note, "recorded-name", "recorded-herdr"
            ),
            timeout=1.0,
        )
        ready = add_unit.ReadyToLaunch(
            request,
            add_unit.NoUnitRow(),
            add_unit.DirectorAgent("opus", add_unit.DefaultEffort()),
        )
        old = self.shutdown_record("down")
        owner = cast(list[dict[str, object]], old["entries"])[0]
        session_shutdown: dict[str, object] = {
            "login": old["login"],
            "label": old["label"],
            "machine": old["machine"],
            "requested_at": old["requested_at"],
            "requested_by": old["requested_by"],
            "request_id": "launch-barrier-request",
            "hold": "account",
            "then": "stop",
            "force": old["force"],
            "alerts": "user",
            "workers": [],
            **owner,
        }
        creator: subprocess.Popen[str] | None = None

        def start_shutdown(_request: add_unit.UnitLaunch) -> None:
            nonlocal creator
            creator = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os, record; assert record.create_session(record.parse_session_records(os.environ['TEST_RECORD'])[0])",
                ],
                cwd=Path(__file__).parents[1] / "shutdown",
                env={
                    **self.env,
                    "TEST_RECORD": json.dumps([session_shutdown]),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        def launch(
            _request: add_unit.UnitLaunch,
            _director: add_unit.DirectorAgent,
            _prompt: str,
            _selected: accounts.ClaudeLaunchAccount,
            *, target: str = "", model: str = "", autocompact_window: str = "",
        ) -> herdr.PromptSubmitted:
            self.assertEqual(target, "claude2" if isinstance(_request.session, add_unit.ResumedSession) else "")
            self.assertEqual((model, autocompact_window), ("", ""))
            self.assertIsNotNone(creator)
            if creator is None:
                self.fail("shutdown creator did not start")
            self.assertIsNone(creator.poll(), "shutdown record appeared before spawn")
            lock_path = Path(self.env["SHUTDOWN_STATE_DIR"]) / "launch.lock"
            with lock_path.open("a+", encoding="utf-8") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(
                        contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )

            return SUBMITTED_PROMPT

        def finish_shutdown() -> None:
            self.assertIsNotNone(creator)
            if creator is None:
                self.fail("shutdown creator did not start")
            stdout, stderr = creator.communicate(timeout=300)
            self.assertEqual(creator.returncode, 0, (stdout, stderr))

        error = io.StringIO()
        with (
            patch.object(add_unit, "launch_request", return_value=request),
            patch.object(add_unit, "preflight", return_value=ready),
            patch.object(
                add_unit,
                "launched_unit",
                return_value=add_unit.NoMarkedUnit(),
            ),
            patch.object(herdr, "live_sessions", return_value=[]),
            patch.object(add_unit, "write_stub", side_effect=start_shutdown),
            patch.object(add_unit, "append_row"),
            patch.object(add_unit, "commit_unit"),
            patch.object(add_unit, "ensure_worktree"),
            patch.object(add_unit, "record_scheduled_restore_prompt"),
            patch.object(add_unit, "launch_session", side_effect=launch),
            patch.object(add_unit, "wait_for_remote_control") as wait,
            patch.object(add_unit, "record"),
            patch.dict(os.environ, self.env),
            redirect_stderr(error),
        ):
            result = add_unit.main(
                ["--production", "unused", "alpha", "--plan", "unused",
                 "--config-dir", str(Path(self.env["HOME"]) / ".claude2")]
            )
            finish_shutdown()

        self.assertEqual(result, 0, error.getvalue())
        wait.assert_not_called()
        account = Path(self.env["SHUTDOWN_STATE_DIR"]) / "owner@example.com"
        self.assertTrue((account / "sessions" / "session-123.json").is_file())

    def test_restore_recording_failure_uses_one_stderr_line_and_does_not_launch(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )

        with patch.object(
            conversation_pause,
            "record_scheduled_prompts",
            side_effect=OSError("first line\nsecond line"),
        ):
            result, error, _commands, launches, _writes = self.run_with_marked_unit(
                launch_kind, unit_lookup.ClaudeNotRunning()
            )

        self.assertEqual(result, 1)
        self.assertEqual(
            error,
            "add_unit: restart prompt not recorded: first line\n",
        )
        self.assertEqual(launches, 0)

    def test_restore_keeps_marked_herdr_with_live_claude_and_reports_missing_submit_proof(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )
        live = unit_lookup.LiveClaude("recorded-name", "session-123", "/socket", 123)

        result, error, commands, launches, writes = self.run_with_marked_unit(launch_kind, live)

        self.assertEqual(result, 1, error)
        self.assertIn("restart prompt not submitted", error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)

    def test_restore_refuses_marked_herdr_when_claude_state_is_unknown(self) -> None:
        launch_kind = add_unit.UnitRestoreLaunch(
            "session-123", self.root / "restart-note", "recorded-name", "recorded-herdr"
        )

        result, error, commands, launches, writes = self.run_with_marked_unit(
            launch_kind, unit_lookup.ClaudeUnknown("session registry unreadable")
        )

        self.assertEqual(result, 2)
        self.assertIn(
            "cannot tell whether Claude runs in session marked-herdr: session registry unreadable",
            error,
        )
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 0)

    def test_rerun_keeps_marked_herdr_and_prints_its_herdr_attach_command(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result, error, commands, launches, writes = self.run_with_marked_unit(
                add_unit.NewWorkLaunch(), unit_lookup.ClaudeNotRunning()
            )
        self.assertEqual(result, 0, error)
        self.assertEqual(commands, [])
        self.assertEqual(launches, 0)
        self.assertEqual(writes, 1)
        self.assertEqual(output.getvalue(), "alpha-unit started: herdr session attach agents, workspace director, tab alpha\n")

    def test_restart_restore_of_a_standby_unit_moved_to_another_account(self) -> None:
        directory = self.prepare_unit_account("claude3")
        self.commit_prepared_row(self.unit_row(plan="standby"))
        self.write_shutdown_record(self.shutdown_record("restarting"))
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        result = self.successful(
            "alpha", "--standby", "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note), "--session-name", "recorded-name",
            "--host-session", "recorded-herdr", "--account", "claude3",
        )

        self.assertIn("alpha-unit restore launched", result.stdout)
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume") + 1], "session-123")

    def test_resume_from_a_brief_is_refused(self) -> None:
        result = self.cli("alpha", "--brief", "Do it.", "--resume", "session-123", "--cwd", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--resume requires --plan, --standby, or --permanent", result.stdout + result.stderr)

    def test_standby_launch_records_state_without_writing_a_plan(self) -> None:
        _ = self.successful("alpha", "--standby", "--port", "8123", "--owns", "src/alpha", subprocess_cli=True)
        worktree = self.root / "project-alpha"
        self.assertIn(self.unit_row(plan="standby", port="8123", owns="src/alpha"),
                      self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (standby)")
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())
        self.assertFalse((worktree / "docs/plans/build-followups-alpha.md").exists())
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual({key: value for key, value in self.pane_environment().items() if key.startswith("SHOWRUNNER_")}, {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "alpha-unit"})
        command = self.opening_prompt()
        prompt = (f"You are alpha-unit in production build-followups (doc {self.doc}), "
                  "under the showrunner, whose `SendMessage` address `~/.claude/scripts/lib/py "
                  "~/.claude/scripts/production/showrunners.py address build-followups` prints (look it up "
                  "before each message: it changes when the showrunner restarts), on standby. "
                  f"Work only in your worktree {worktree}, "
                  "branch build-followups-alpha. Do nothing until the showrunner sends you work.")
        self.assertIn(prompt, command)
        self.assert_director_flags("opus", "xhigh")
        self.assertRegex(self.log.read_text(),
                         r"^- \d\d:\d\d PDT: added alpha-unit \(standby\), worktree ")

    def test_permanent_launch_writes_a_permanent_row_and_says_wrap_keeps_it(self) -> None:
        _ = self.successful("alpha", "--permanent")
        self.assertIn(self.unit_row(plan="permanent"), self.doc.read_text())
        self.assertEqual(self.git("log", "-1", "--format=%s"),
                         "production(build-followups): add unit alpha-unit (permanent)")
        self.assertIn("on standby. You are permanent: wrap never retires you. Work only in your worktree",
                      self.opening_prompt())

    def test_the_nightly_review_unit_runs_from_rust_under_the_review_settings(self) -> None:
        folder = Path(self.env["HOME"]) / "rust"
        folder.mkdir()
        config = Path(self.env["HOME"]) / ".claude2/.claude.json"
        _ = config.write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": {**self.trusted_projects(), str(folder): {"hasTrustDialogAccepted": True}}}))
        result = self.successful("nightly-review", "--permanent")
        self.assertIn("nightly-review-unit started: herdr session attach agents, workspace director, tab nightly-review", result.stdout)
        self.assertEqual(self.opened()[7], str(folder))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(config.parent))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--remote-control"):], [
            "--remote-control", "nightly-review", "-n", "nightly-review",
            "--add-dir", "/etc/nixos", "--add-dir", self.env["HOME"] + "/.claude",
            "--settings", str(SCRIPT.parents[1] / "nightly_review/settings.json")])
        prompt = self.opening_prompt()
        self.assertIn("You run read-only from ~/rust", prompt)
        self.assertIn("Each night the nightly timer sends you", prompt)
        self.assertEqual(sum(row[2:4] == ["agent", "prompt"] for row in self.herdr_calls()), 1)
        self.assertNotIn("claude", arguments)
        self.assertNotIn(prompt, arguments)


    def test_standby_rejects_other_modes_before_any_change(self) -> None:
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for args in (("--standby", "--plan", "docs/plans/given.md"),
                     ("--standby", "--brief", "Write a plan")):
            with self.subTest(args=args):
                result = self.cli("alpha", *args)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertEqual(self.doc.read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(self.config.exists())
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "project-alpha").exists())
                self.assertEqual(self.servers_started(), [])

    def test_plan_adds_row_commits_worktree_launches_and_records(self) -> None:
        pushed = self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups")
        result = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123",
                                 "--owns", "src/alpha")
        worktree = self.root / "project-alpha"
        row = self.unit_row(port="8123", owns="src/alpha")
        self.assertIn(row, self.doc.read_text())
        self.assertEqual(self.git("show", "--pretty=format:", "--name-only", "HEAD"),
                         "docs/plans/build-followups-production.md")
        self.assertIn("production(build-followups): add unit alpha-unit (plan)",
                      self.git("log", "-1", "--format=%s"))
        # An add pushes nothing: the unit's commit waits for the next merge push, and the unit branch
        # for its first checkpoint push, which sets the upstream.
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "refs/heads/build-followups"), pushed)
        self.assertEqual(self.git("--git-dir", str(self.origin), "for-each-ref", "--format=%(refname)", "refs/heads"),
                         "refs/heads/build-followups")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) upstream=%(upstream)",
                                  "refs/heads/build-followups-alpha"),
                         "refs/heads/build-followups-alpha upstream=")
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree),
                         "build-followups-alpha")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=worktree),
                         self.git("rev-parse", "build-followups"))
        self.assertEqual(self.servers_started(), [["--user", "start", "herdr-agents.service"]])
        self.assertEqual(self.opened()[:8], ["--session", SHARED, "workspace", "create", "--label", "director",
                                             "--cwd", str(worktree)])
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1",
                       "build-followups · alpha · unit director"], self.herdr_calls())
        self.assertIn(["--session", SHARED, "pane", "rename", "w1:p1", "alpha"], self.herdr_calls())
        self.assertEqual({key: value for key, value in self.pane_environment().items()
                          if key.startswith(("SHOWRUNNER_", "CLAUDE_", "HERDR_")) or key == "ENABLE_TOOL_SEARCH"},
                         {"SHOWRUNNER_UNIT": "build-followups",
            "SHOWRUNNER_UNIT_ID": "alpha-unit", "ENABLE_TOOL_SEARCH": "true", "HERDR_AGENT_PANE": "1",
            "CLAUDE_CONFIG_DIR": str(Path(self.env["HOME"]) / ".claude2"),
            "CLAUDE_CODE_REMOTE_MEMORY_DIR": str(Path(self.env["HOME"]) / ".claude")})
        arguments = self.agent_arguments()
        session_index = arguments.index("--session-id")
        self.assertTrue(arguments[session_index + 1])
        self.assertEqual(arguments[:session_index] + arguments[session_index + 2:],
            ["--model", "opus", "--effort", "xhigh", "--remote-control", "alpha", "-n", "alpha", "--settings", '{"disableAgentView": true}'])
        self.assertEqual(self.opening_prompt(), "/unit:direct docs/plans/given.md")
        self.assertEqual(len([row for row in self.herdr_calls() if row[2:4] == ["agent", "prompt"]]), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertRegex(self.log.read_text(),
                         r"^- \d\d:\d\d PDT: added alpha-unit \(plan\), worktree ")
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertIn("alpha-unit started: herdr session attach agents, workspace director, tab alpha", result.stdout)

    def test_a_review_named_unit_opens_unit_direct_beside_a_review_pool(self) -> None:
        _ = self.doc.write_text(self.doc.read_text().replace("## Units", "- **Review pool:** code\n\n## Units"))
        _ = self.successful("review", "--plan", "docs/plans/given.md")
        self.assertIn(shlex.quote("/unit:direct docs/plans/given.md"), self.launch_command())
        self.assertNotIn("/review:direct", self.launch_command())

    def test_named_unit_account_paths_are_literal_pane_environment_values(self) -> None:
        home = self.root / "home with 'quotes' $()"
        _ = Path(self.env["HOME"]).rename(home)
        self.env["HOME"] = str(home)
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        directory = self.prepare_unit_account("claude3")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(self.pane_environment()["CLAUDE_CODE_REMOTE_MEMORY_DIR"], str(home / ".claude"))
        self.assertNotIn("CLAUDE_TEST_SECRET", self.pane_environment())
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.pane_environment())

    def test_held_named_unit_account_refuses_new_work_before_changes(self) -> None:
        """F001: the launch guard checks the selected unit account's shutdown."""
        _ = self.prepare_unit_account("claude3")
        login = "claude-3@example.invalid"
        held = self.shutdown_record("down")
        held["login"] = login
        held["label"] = "claude3"
        self.write_shutdown_record(held, login=login)
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        result = self.cli("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(
            result.stderr,
            "add_unit: claude3 is held by a shutdown "
            + "(since 2026-10-09 14:49 PDT), requested in a terminal; /shutdown restart first\n",
        )
        self.assert_no_launch_change(document, head)

    def test_default_account_shutdown_does_not_block_a_named_unit(self) -> None:
        """F001: another account's shutdown does not block the selected unit account."""
        directory = self.prepare_unit_account("claude3")
        self.write_shutdown_record(self.shutdown_record("down"))

        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_claude2_guard_environment_selects_its_numbered_folder(self) -> None:
        """The guard sees the selected folder, never the caller or config home."""
        self.env["CLAUDE_CONFIG_DIR"] = str(self.root / "caller account")
        with (
            patch.dict(os.environ, self.env),
            patch.object(
                launch_permission, "launch_permission",
                return_value=launch_permission.AllowedByShutdownState(),
            ) as permission,
        ):
            expected = {
                key: value for key, value in os.environ.items()
                if not key.startswith("CLAUDE_")
            }
            result = add_unit.main([
                "--production", str(self.doc), "alpha",
                "--plan", "docs/plans/given.md", "--check",
            ])

        self.assertEqual(result, 0)
        expected["CLAUDE_CONFIG_DIR"] = str(Path(self.env["HOME"]) / ".claude2")
        permission.assert_called_once_with(
            expected, launch_permission.NewWorkLaunchPurpose()
        )

    def test_better_candidate_after_readiness_keeps_the_stored_group_account(self) -> None:
        """Better readings neither replace the stored group nor the checked launch account."""
        directory = self.prepare_unit_account("claude3")
        ensure_worktree = add_unit.ensure_worktree

        def change_role_after_worktree(request: add_unit.UnitLaunch) -> None:
            ensure_worktree(request)
            _ = self.prepare_unit_account("claude1")
            self.quota_reading("claude-1@example.invalid", 1, 1)
            _ = self.select_unit_account("claude3")

        with (
            patch.dict(os.environ, self.env),
            patch.object(accounts, "role_account", wraps=accounts.role_account) as selection,
            patch.object(accounts, "check", wraps=accounts.check) as readiness,
            patch.object(add_unit, "ensure_worktree", side_effect=change_role_after_worktree),
        ):
            result = add_unit.main([
                "--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
            ])

        self.assertEqual(result, 0)
        selection.assert_called_once_with("unit")
        self.assertIn("claude3", [call.args[0] for call in readiness.call_args_list])
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertTrue((directory.parent / ".claude1").exists())
        with patch.dict(os.environ, self.env):
            self.assertEqual(
                accounts.role_account("unit"),
                accounts.NamedAccount("claude3", directory),
            )

    def test_new_claude2_unit_passes_its_numbered_account_variables(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.pane_environment()["CLAUDE_CODE_REMOTE_MEMORY_DIR"], str(Path(self.env["HOME"]) / ".claude"))

    def test_new_named_unit_passes_selected_account_variables(self) -> None:
        directory = self.prepare_unit_account("claude3")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_explicit_unit_account_move_passes_prepared_account_variables(self) -> None:
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--account", "claude3")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_manual_unit_resume_keeps_its_recorded_model_despite_changed_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        self.persist_unit_account(directory, session="alpha", model="opus")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior))
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")

    def test_resume_autocompact_is_recorded_and_passed_to_claude(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(prior), "--autocompact", "200k")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--resume"):arguments.index("--resume") + 4],
                         ["--resume", "session-123", "--autocompact", "200k"])
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-123"), "200k")

    def test_restart_restore_carries_the_recorded_autocompact_window(self) -> None:
        """A /shutdown restart, account move or rename relaunches through a restore with no flag of its own."""
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        with patch.dict(os.environ, self.env):
            autocompact.record("session-compact", "200k")
        for name, flags, expected in (("compact", (), "200k"), ("reverted", ("--autocompact", "auto"), None)):
            with self.subTest(name=name):
                self.commit_prepared_row(self.unit_row(name))
                _ = self.successful(name, "--plan", "docs/plans/given.md", "--resume", "session-compact",
                                    "--cwd", str(prior), "--restart-note", str(note), "--session-name", name,
                                    "--host-session", f"build-followups-{name}", *flags)
                start = next(row for row in self.herdr_calls()
                             if row[:5] == ["--session", SHARED, "agent", "start", f"build-followups-{name}"])
                arguments = start[start.index("--") + 1:]
                if expected is None:
                    self.assertNotIn("--autocompact", arguments)
                else:
                    self.assertEqual(arguments[arguments.index("--autocompact") + 1], expected)
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-compact"), "")

    def set_unit_autocompact(self, value: str, *flags: str) -> subprocess.CompletedProcess[str]:
        """A running unit's window, set as live: its row off the naming pattern and its plan since changed."""
        prior = self.root / "prior-session"
        prior.mkdir(exist_ok=True)
        return self.successful("trunk", "--plan", "docs/plans/other.md", "--resume", "session-trunk",
                               "--cwd", str(prior), "--autocompact", value, *flags)

    def test_registered_unit_autocompact_records_without_new_work_checks(self) -> None:
        _ = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()
        result = self.set_unit_autocompact("200k")
        self.assertEqual(result.stdout.strip(),
                         "trunk-unit: auto-compact 200k applies to session session-trunk at its next restart")
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-trunk"), "200k")
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_auto_deletes_the_record(self) -> None:
        _ = self.commit_row_off_the_naming_pattern()
        with patch.dict(os.environ, self.env):
            autocompact.record("session-trunk", "200k")
        result = self.set_unit_autocompact("auto")
        self.assertIn("the saved setting applies", result.stdout)
        with patch.dict(os.environ, self.env):
            self.assertEqual(autocompact.recorded("session-trunk"), "")
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_check_writes_nothing(self) -> None:
        _ = self.commit_row_off_the_naming_pattern()
        before = self.repository_state()
        result = self.set_unit_autocompact("300k", "--check")
        self.assertEqual(result.stdout.strip(),
                         "trunk-unit: auto-compact 300k would apply to session session-trunk at its next restart")
        self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.herdr_calls(), [])

    def test_registered_unit_autocompact_reaches_its_restore_launch(self) -> None:
        worktree = self.commit_row_off_the_naming_pattern()
        _ = self.set_unit_autocompact("200k")
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        _ = self.successful("trunk", "--plan", "docs/plans/given.md", "--resume", "session-trunk",
                            "--cwd", str(worktree), "--restart-note", str(note), "--session-name", "trunk",
                            "--host-session", "build-followups-trunk")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--autocompact") + 1], "200k")

    def test_autocompact_refusals_launch_nothing_and_record_nothing(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        before = self.repository_state()
        for flags, reason in ((("--autocompact", "200k"), "--autocompact requires --resume"),
                              (("--resume", "session-123", "--cwd", str(prior), "--autocompact", "50k"),
                               "outside 100k to 1M"),
                              (("--resume", "session-123", "--cwd", str(prior), "--autocompact", "big"),
                               "token count such as 200k")):
            with self.subTest(flags=flags):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertIn(reason, result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.servers_started(), [])
        self.assertFalse((Path(self.env["HOME"]) / ".local/state/unit-launch").exists())

    def test_recorded_source_fallback_uses_add_unit_in_the_shared_host(self) -> None:
        from test_restart import entry, unit
        import record

        self.commit_prepared_row(self.unit_row())
        directory = self.prepare_unit_account("claude1")
        _ = self.prepare_unit_account("claude2")
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume on the source after the target refused.", encoding="utf-8")
        saved = entry(unit("session-123"))
        saved.update(target="claude1", source_label="claude1")
        flags = ("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior),
                 "--restart-note", str(note), "--session-name", "recorded-name", "--host-session", "shared-herdr",
                 "--account", "claude1")
        with (patch.object(record, "all_session_records", return_value=[saved]),
              patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()),
              patch.object(herdr, "shared_session", return_value="shared-herdr"),
              patch.object(herdr, "start_server") as start,
              patch.object(herdr, "open_tab", return_value="w1:p1") as tab,
              patch.object(herdr, "start_agent", return_value=SUBMITTED_PROMPT) as agent):
            refused = self.cli(*flags)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("units account is claude2", refused.stderr)
            _ = self.successful(*flags, "--source-fallback")
        start.assert_called_once_with("shared-herdr", 90_000)
        environment = cast(dict[str, str], tab.call_args.args[4])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertEqual(environment["SHOWRUNNER_UNIT_ID"], "alpha-unit")
        self.assertEqual(environment["SHOWRUNNER_UNIT"], "build-followups")
        agent.assert_called_once()
        self.assertEqual(agent.call_args.args[:2], ("shared-herdr", "w1:p1"))

    def test_source_fallback_requires_a_restore_of_its_recorded_source(self) -> None:
        import record

        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume.", encoding="utf-8")
        with (patch.object(record, "all_session_records", return_value=[]),
              patch.object(herdr, "start_agent", side_effect=AssertionError("invalid fallback launched"))):
            new = self.cli("alpha", "--plan", "docs/plans/given.md", "--source-fallback", "--account", "claude2")
            self.assertEqual(new.returncode, 2)
            self.assertIn("requires a recorded unit restore", new.stderr)
            restore = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(prior),
                "--restart-note", str(note), "--session-name", "recorded-name", "--host-session", "shared-herdr",
                "--account", "claude2", "--source-fallback")
            self.assertEqual(restore.returncode, 2)
            self.assertIn("must name the shutdown record's source account", restore.stderr)

    def test_unit_resume_uses_configured_account_instead_of_recorded_account(self) -> None:
        """F004: the configured units account decides resumes too."""
        old = self.prepare_unit_account("claude2")
        caller = self.prepare_unit_account("claude3")
        directory = self.prepare_unit_account("claude1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        self.persist_unit_account(old, session="alpha")
        prior = self.root / "prior-session"
        prior.mkdir()
        self.prepare_herdr_launch("alpha", "--plan", "docs/plans/given.md",
                                  "--resume", "session-123", "--cwd", str(prior))
        with (patch.dict(os.environ, self.env),
              patch.object(accounts, "prepared_account", wraps=accounts.prepared_account) as prepared):
            code = add_unit.main(["--production", str(self.doc), "alpha", "--plan", "docs/plans/given.md",
                "--resume", "session-123", "--cwd", str(prior)])
        self.assertEqual(code, 0)
        prepared.assert_any_call("claude1", resume=True)
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertIn("--resume session-123", self.launch_command())

    def test_unit_resume_uses_configured_account_instead_of_old_named_account(self) -> None:
        self.commit_prepared_row(self.unit_row())
        directory = self.prepare_unit_account("claude1")
        other_directory = self.prepare_unit_account("claude3")
        self.env["CLAUDE_CONFIG_DIR"] = str(other_directory)
        self.persist_unit_account(directory)
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")

        _ = self.successful(
            "alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name", "--host-session", "recorded-herdr",
        )

        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(other_directory))
        self.assertFalse(any(argument.endswith("/accounts.py") for argument in self.agent_arguments()))
        self.assertIn("--resume session-123", self.launch_command())
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_without_recorded_account_uses_configured_units(self) -> None:
        """F004: no retained account is needed once the group is configured."""
        directory = self.prepare_unit_account("claude1")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "unrecorded-session", "--cwd", str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_unit_resume_refuses_config_directory_outside_configured_account(self) -> None:
        """F004: an explicit directory cannot replace the units account."""
        directory = self.root / "external account with spaces"
        directory.mkdir()
        _ = self.prepare_unit_account("claude1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(self.checkout), "--config-dir", str(directory))
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude1", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_unit_resume_refuses_missing_directory_that_differs_from_configured_default(self) -> None:
        """F004: an override mismatch names configuration, not a missing former account."""
        directory = self.root / "gone-account"
        prior = self.root / "prior-session"
        prior.mkdir()
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--config-dir", str(directory))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("units account is claude2", result.stderr)
        self.assertIn("--account and --config-dir must name claude2", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.servers_started(), [])

    def test_resumed_unit_uses_its_recorded_model_despite_a_different_director_configuration(self) -> None:
        self.write_agent_config(director="sonnet:high")
        directory = self.prepare_unit_account("claude3")
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(prior), "--account", "claude3", "--model", "opus")
        arguments = self.agent_arguments()
        self.assertEqual(arguments[arguments.index("--model") + 1], "opus")
        self.assertNotIn(str(directory), self.launch_command())

    def test_unit_resume_refuses_account_that_differs_from_configured_units(self) -> None:
        """F004: --account must agree with the configured group."""
        _ = self.prepare_unit_account("claude2")
        _ = self.prepare_unit_account("claude1")
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md",
            "--resume", "session-123", "--cwd", str(self.checkout), "--account", "claude2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude1", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_unconfigured_units_refuse_new_standby_and_resume_without_writes(self) -> None:
        """F004: every unit launch mode requires explicit configuration."""
        with patch.dict(os.environ, self.env):
            accounts.groups_file().unlink()
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for mode in (("--plan", "docs/plans/given.md"), ("--standby",),
                     ("--plan", "docs/plans/given.md", "--resume", "session-123", "--cwd", str(self.checkout))):
            with self.subTest(mode=mode):
                result = self.cli("alpha", *mode)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("accounts.py move units <claudeN>", result.stderr)
                self.assert_no_launch_change(document, head)
                self.assertFalse((Path(self.env["HOME"]) / ".local/state/accounts/groups.json").exists())

    def test_new_unit_refuses_unfit_configured_account_without_default_fallback(self) -> None:
        """F004: a configured account cannot silently fall back to default."""
        directory = self.select_unit_account("claude3")
        directory.mkdir()
        before = self.repository_state()
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("claude3", result.stderr)
        self.assertEqual(self.repository_state(), before)
        self.assertEqual(self.events("systemd-run"), [])

    def test_new_unit_and_standby_use_configured_units_despite_showrunner_caller(self) -> None:
        """F004: caller identity does not decide either new unit launch mode."""
        caller = self.prepare_unit_account("claude3")
        directory = self.prepare_unit_account("claude1")
        self.env["CLAUDE_CONFIG_DIR"] = str(caller)
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        _ = path.write_text(json.dumps({"units": "claude1", "showrunners": "claude3"}))
        for name, mode in (("alpha", ("--plan", "docs/plans/given.md")), ("beta", ("--standby",))):
            with self.subTest(mode=mode):
                _ = self.successful(name, *mode)
                self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_unit_accepts_explicit_account_matching_configured_units(self) -> None:
        """F004: matching --account selects no alternative account."""
        directory = self.prepare_unit_account("claude1")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--account", "claude1")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_units_and_showrunners_may_share_the_launch_account(self) -> None:
        """F004: sharing one account is valid configuration."""
        directory = self.prepare_unit_account("claude1")
        path = Path(self.env["HOME"]) / ".local/state/accounts/groups.json"
        _ = path.write_text(json.dumps({"units": "claude1", "showrunners": "claude1"}))
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(directory))

    def test_ready_unit_account_passes_check_without_launch_changes(self) -> None:
        _ = self.prepare_unit_account("claude3")
        document = self.doc.read_text(encoding="utf-8")
        head = self.git("rev-parse", "HEAD")

        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--check")

        self.assert_no_launch_change(document, head)

    def test_a_doc_that_still_has_the_session_column_gets_a_dash_there(self) -> None:
        _ = self.doc.write_text(self.production_doc(session_column=True), encoding="utf-8")
        _ = self.git("commit", "-am", "an older production doc")
        _ = self.git("push", "origin", "build-followups")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--port", "8123")
        self.assertIn(f"| alpha-unit | docs/plans/given.md | {self.root / 'project-alpha'} | build-followups-alpha "
                      + "| — | 8123 | — |", self.doc.read_text())

    def test_a_rerun_finds_the_unit_by_its_mark_after_its_tab_was_renamed(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.herdr_overrides[f"--session {SHARED} pane list"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"panes": [{"pane_id": "w1:p1", "tab_id": "w1:t1", "label": "renamed"}]}}))
        result = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)
        self.assertIn("alpha-unit started: herdr session attach agents, workspace director, tab alpha", result.stdout)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_sonnet_director_row_changes_launch_model_and_effort(self) -> None:
        self.write_agent_config(director="sonnet:xhigh")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assert_director_flags("sonnet", "xhigh")

    def test_director_row_without_effort_uses_claude_default(self) -> None:
        self.write_agent_config(director="opus")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assert_director_flags("opus", None)

    def test_codex_director_refused_before_any_change_with_or_without_check(self) -> None:
        self.write_agent_config(family="codex")
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for check in (False, True):
            with self.subTest(check=check):
                arguments = ("alpha", "--plan", "docs/plans/given.md")
                result = self.cli(*arguments, *(("--check",) if check else ()))
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr.strip().splitlines(), [
                    "add_unit: unit directors launch only on claude; production.director resolves to codex (gpt-test)"])
                self.assert_no_launch_change(document, head)

    def test_missing_claude_director_set_returns_resolver_error_before_change(self) -> None:
        self.write_agent_config(claude_set=False)
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr.strip().splitlines(), [
            f"add_unit: ERROR: [production.director] missing set section [production.claude] in {self.agent_config}."])
        self.assert_no_launch_change(document, head)

    def test_prepared_row_launches_without_rewriting_or_committing_it(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn("add unit", self.git("log", "-1", "--format=%s"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.root / "project-alpha"),
                         "build-followups-alpha")
        self.assertEqual(len(self.servers_started()), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertIn("added alpha-unit (plan)", self.log.read_text())

    def test_prepared_row_refuses_mismatched_identity_cells_before_launch(self) -> None:
        cases = (("Plan", self.prepared_row(plan="docs/plans/other.md"),
                  "docs/plans/other.md", "docs/plans/given.md"),
                 ("Branch", self.prepared_row(branch="other-branch"),
                  "other-branch", "build-followups-alpha"),
                 ("Worktree", self.prepared_row(worktree=self.root / "other-worktree"),
                  str(self.root / "other-worktree"), str(self.root / "project-alpha")))
        for cell, row, actual, expected in cases:
            with self.subTest(cell=cell):
                self.commit_prepared_row(row)
                document = self.doc.read_text()
                head = self.git("rev-parse", "HEAD")
                result = self.cli("alpha", "--plan", "docs/plans/given.md")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)
                self.assertIn(cell, result.stderr)
                self.assertIn(f"is {actual!r}; expected {expected!r}", result.stderr)
                self.assert_no_launch_change(document, head)

    def test_prepared_row_refuses_different_supplied_port_before_launch(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--port", "9000")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Port is '8123'; expected '9000'", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_prepared_row_refuses_different_supplied_owns_before_launch(self) -> None:
        self.commit_prepared_row(self.prepared_row())
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--owns", "src/beta")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("Owns is 'src/alpha'; expected 'src/beta'", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_prepared_standby_row_uses_standby_mode(self) -> None:
        self.commit_prepared_row(self.prepared_row(plan="standby"))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        _ = self.successful("alpha", "--standby")
        self.assertEqual(self.doc.read_text(), document)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertIn("added alpha-unit (standby)", self.log.read_text())

    def test_other_prepared_row_reserves_backticked_branch(self) -> None:
        self.commit_prepared_row(self.prepared_row(name="beta"))
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("branch or worktree", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_check_resume_preflights_without_any_launch_side_effect(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assert_no_launch_change(document, head)

    def test_check_refuses_invalid_session_name_without_any_change(self) -> None:
        document = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("new director", "--plan", "docs/plans/given.md", "--resume", "session-123",
                          "--cwd", str(self.root / "prior-session"), "--check", "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertIn("name must contain only letters", result.stderr)
        self.assert_no_launch_change(document, head)

    def test_berth_target_is_set_only_when_berth_config_exists(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertNotIn("cargoBerthTarget", self.git("config", "--local", "--list"))

    def test_berth_target_is_set_when_config_exists(self) -> None:
        berth = self.checkout / ".claude/config/berth.toml"
        _ = berth.parent.mkdir(parents=True)
        _ = berth.write_text("", encoding="utf-8")
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("config", "branch.build-followups-alpha.cargoBerthTarget"),
                         "build-followups")

    def test_brief_commits_stub_with_verbatim_words_and_approval_prompt(self) -> None:
        words = "Investigate the slow build.\nKeep the user's exact wording."
        result = self.successful("alpha", "--brief", words)
        stub = self.checkout / "docs/plans/build-followups-alpha.md"
        self.assertTrue(stub.exists())
        content = stub.read_text()
        self.assertIn("# alpha", content)
        self.assertIn("> **Production: build-followups** — unit `alpha-unit`; production doc", content)
        self.assertIn("## Source", content)
        self.assertIn(words, content)
        self.assertRegex(content, r"\d{4}-\d\d-\d\d.*PDT")
        self.assertIn(self.unit_row(plan="docs/plans/build-followups-alpha.md"), self.doc.read_text())
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/plans/build-followups-production.md", "docs/plans/build-followups-alpha.md"})
        prompt = self.opening_prompt()
        self.assertIn("Write the full phased plan there", prompt)
        self.assertIn("wait for its approval before you run /unit:direct docs/plans/build-followups-alpha.md", prompt)
        self.assertIn("alpha-unit started: herdr session attach agents, workspace director, tab alpha", result.stdout)
        self.assert_director_flags("opus", "xhigh")

    def test_branch_takes_the_slug_and_the_stub_sits_beside_the_production_doc(self) -> None:
        _ = self.git("checkout", "-b", "init/catalyst")
        self.doc = self.checkout / "docs/hana/tool-based-ui-production.md"
        _ = self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(self.production_doc("init/catalyst"), encoding="utf-8")
        _ = self.git("add", "docs/hana/tool-based-ui-production.md")
        _ = self.git("commit", "-m", "production under another merge branch")
        _ = self.successful("lfo", "--brief", "Add the LFO tool")
        worktree = self.root / "project-lfo"
        self.assertIn(self.unit_row("lfo", branch="tool-based-ui-lfo", worktree=worktree,
                                    plan="docs/hana/tool-based-ui-lfo.md"), self.doc.read_text())
        self.assertEqual(self.git("branch", "--show-current", cwd=worktree), "tool-based-ui-lfo")
        self.assertEqual(set(self.git("show", "--pretty=format:", "--name-only", "HEAD").splitlines()),
                         {"docs/hana/tool-based-ui-production.md", "docs/hana/tool-based-ui-lfo.md"})
        self.assertFalse((self.checkout / "docs/plans/init").exists())
        self.assertEqual(self.git("branch", "--list", "init/catalyst-lfo"), "")

    def test_timeout_keeps_session_then_rerun_finishes_without_duplicate_steps(self) -> None:
        _ = (self.state / "ready").unlink()
        first = self.cli("alpha", "--plan", "docs/plans/given.md", "--timeout", "10")
        self.assertEqual(first.returncode, 1, (first.stdout, first.stderr))
        self.assertIn("/remote-control did not become active", first.stderr)
        self.assertIn("(herdr agents:", first.stderr)
        self.assertIn("Waiting for remote control", first.stdout + first.stderr)
        self.assertFalse(self.config.exists())
        self.assertFalse(self.log.exists())
        commit = self.git("rev-parse", "HEAD")
        _ = (self.state / "ready").touch()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.doc.read_text().count("| alpha-unit |"), 1)
        self.assertEqual(len(self.servers_started()), 1)
        # Adding a unit registers nothing: the showrunner's update timer already names the doc.
        self.assertFalse(self.config.exists())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_refusals_change_no_repository_or_registry_state(self) -> None:
        cases: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("no mode", ("alpha",)),
            ("two modes", ("alpha", "--plan", "docs/plans/given.md", "--brief", "words")),
            ("missing plan", ("alpha", "--plan", "docs/plans/missing.md")),
        )
        initial = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        for label, arguments in cases:
            with self.subTest(label=label):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(len((result.stdout + result.stderr).strip().splitlines()), 1)
                self.assertEqual(self.doc.read_text(), initial)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse(self.config.exists())

    def test_excluded_standing_plan_launches_and_keeps_the_units_copy(self) -> None:
        plan = Path("docs/style-production/units/alpha-unit.md")
        source = self.checkout / plan
        source.parent.mkdir(parents=True)
        header = "# Standing plan\n\n> **Production: build-followups** — unit `alpha-unit`\n"
        standing = header + "> **Standing:** style_plan.py\n"
        _ = source.write_text(standing, encoding="utf-8")
        initial = self.repository_state()
        refusal = f"plan {plan} is missing on build-followups or lacks the Production header"

        with self.subTest(plan="untracked but not excluded"):
            result = self.cli("alpha", "--plan", plan.as_posix(), "--check")
            self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
            self.assertIn(refusal, result.stderr)
            self.assertEqual(self.repository_state(), initial)

        exclude = self.checkout / ".git/info/exclude"
        _ = exclude.write_text(exclude.read_text() + "\n/docs/style-production/\n", encoding="utf-8")
        _ = source.write_text(header, encoding="utf-8")
        with self.subTest(plan="excluded without Standing header"):
            result = self.cli("alpha", "--plan", plan.as_posix(), "--check")
            self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
            self.assertIn(refusal, result.stderr)
            self.assertEqual(self.repository_state(), initial)

        _ = source.write_text(standing, encoding="utf-8")
        _ = self.successful("alpha", "--plan", plan.as_posix(), "--check")
        self.assertEqual(self.repository_state(), initial)
        _ = self.successful("alpha", "--plan", plan.as_posix())
        worktree = self.root / "project-alpha"
        copied = worktree / plan
        self.assertEqual(copied.read_text(encoding="utf-8"), standing)
        self.assertEqual(self.git("check-ignore", "--", plan.as_posix(), cwd=worktree), plan.as_posix())
        self.assertEqual(self.git("ls-tree", "HEAD", "--", plan.as_posix()), "")
        self.assertIn(f"/unit:direct {plan}", self.opening_prompt())

        progress = standing + "\n### Phase 1 — ticket in progress\n"
        _ = copied.write_text(progress, encoding="utf-8")
        _ = source.write_text(standing + "\nShowrunner template changed\n", encoding="utf-8")
        _ = self.successful("alpha", "--plan", plan.as_posix())
        self.assertEqual(copied.read_text(encoding="utf-8"), progress)

    def test_refuses_plan_without_production_header(self) -> None:
        _ = self.plan.write_text("# No production header\n", encoding="utf-8")
        _ = self.git("add", "docs/plans/given.md")
        _ = self.git("commit", "-m", "remove production header")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_missing_production_field(self) -> None:
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **Log:** `docs/plans/build-followups-log.md` — git-excluded\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_a_doc_without_a_user_zone_line_still_adds_a_unit(self) -> None:
        # The zone is the user's setting; a production doc no longer has to name one.
        original = self.doc.read_text()
        _ = self.doc.write_text(original.replace("- **User zone:** America/Los_Angeles — reports use PDT\n", ""))
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_checkout_off_merge_branch(self) -> None:
        _ = self.git("checkout", "-b", "wrong-branch")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertNotIn("| alpha-unit |", self.doc.read_text())

    def test_refuses_taken_row(self) -> None:
        original = self.doc.read_text()
        changed = original.replace("## Gates", self.unit_row(branch="different") + "\n\n## Gates")
        _ = self.doc.write_text(changed)
        taken = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(taken.returncode, 2, (taken.stdout, taken.stderr))
        self.assertEqual(self.doc.read_text(), changed)
        self.assertEqual(self.servers_started(), [])

    def test_resume_uses_session_id_cwd_and_promote_prompt(self) -> None:
        prior = self.root / "prior-session"
        prior.mkdir()
        _ = self.successful("alpha", "--plan", "docs/plans/given.md", "--resume", "session-123",
                            "--cwd", str(prior), "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.opened()[7], str(prior))
        self.assertEqual(self.pane_environment()["CLAUDE_CONFIG_DIR"], str(Path(self.env["HOME"]) / ".claude2"))
        self.assertEqual(self.agent_arguments(), [
            "--model", "opus", "--effort", "xhigh", "--resume", "session-123",
            "--remote-control", "alpha", "-n", "alpha", "--settings", '{"disableAgentView": true}',
        ])
        self.assert_director_flags("opus", "xhigh")
        command = self.opening_prompt()
        self.assertIn("You are now alpha-unit in production build-followups", command)
        promoted_plan = self.root / "project-alpha/docs/plans/given.md"
        self.assertTrue(promoted_plan.exists())
        self.assertIn(f"Run /unit:direct {promoted_plan}", command)

    def test_all_restore_flags_parse_as_a_unit_restore_and_other_launches_do_not(self) -> None:
        note = self.root / "restart note.txt"
        _ = note.write_text("resume here", encoding="utf-8")
        common = {
            "production": str(self.doc),
            "name": "alpha",
            "plan": "docs/plans/given.md",
            "brief": None,
            "standby": False,
            "permanent": False,
            "port": None,
            "owns": None,
            "timeout": 90.0,
            "check": False,
            "probe": False,
        }
        restored = add_unit.launch_request(argparse.Namespace(
            **common,
            resume="session-123",
            cwd=str(self.root / "prior-session"),
            restart_note=str(note),
            session_name="recorded name",
            host_session="recorded herdr",
        ))
        ordinary_resume = add_unit.launch_request(argparse.Namespace(
            **common,
            resume="session-123",
            cwd=str(self.root / "prior-session"),
            restart_note=None,
            session_name=None,
            host_session=None,
        ))
        new_work = add_unit.launch_request(argparse.Namespace(
            **common,
            resume=None,
            cwd=None,
            restart_note=None,
            session_name=None,
            host_session=None,
        ))

        self.assertEqual(
            restored.launch_kind,
            add_unit.UnitRestoreLaunch(
                "session-123", note, "recorded name", "recorded herdr"
            ),
        )
        self.assertIsInstance(ordinary_resume.launch_kind, add_unit.NewWorkLaunch)
        self.assertIsInstance(new_work.launch_kind, add_unit.NewWorkLaunch)

    def test_a_name_ending_in_unit_launches_as_the_name_without_it(self) -> None:
        def request(name: str) -> tuple[str, str, Path, str]:
            launched = add_unit.launch_request(argparse.Namespace(
                production=str(self.doc), name=name, plan="docs/plans/given.md", brief=None,
                standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
                resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
            ))
            return launched.unit, launched.branch, launched.worktree, launched.requested_name

        expected = request("feature-log")
        self.assertEqual(expected[0], "feature-log-unit")
        self.assertEqual(request("feature-log-unit"), expected)
        self.assertEqual(request("feature_log_unit"), request("feature_log"))
        self.assertEqual(request("unit")[0], "unit-unit")

    def assert_guild_head_launch_and_restart(self, guild: str) -> None:
        self.doc = self.checkout / "docs/plans/guilds-production.md"
        row = self.unit_row(guild, branch="guilds-" + guild, worktree=self.root / ("project-" + guild))
        row = row.replace(f"| {guild}-unit |", f"| `{guild}` |")
        _ = self.doc.write_text(self.production_doc().replace("## Gates", row + "\n\n## Gates"), encoding="utf-8")
        _ = self.git("add", str(self.doc.relative_to(self.checkout)))
        _ = self.git("commit", "-m", "Add guilds fixture production")
        _ = self.git("push", "origin", "build-followups")

        result = self.successful(guild, "--plan", "docs/plans/given.md")
        self.assertIn(f"{guild} started", result.stdout)
        rows = list(production_units.unit_table(self.doc.read_text().splitlines()))
        self.assertEqual([cell_value(row["Unit"]) for row in rows], [guild])
        self.assertEqual(self.pane_environment()["SHOWRUNNER_UNIT_ID"], guild)
        self.assertEqual(self.pane_environment()["SHOWRUNNER_UNIT"], "guilds")
        with patch.dict(os.environ, self.env):
            marked = unit_lookup.marked_unit("guilds", guild)
            self.assertIsNotNone(marked)
            if marked is not None:
                self.assertEqual(marked.unit, guild)
                self.assertEqual(marked.place.session, "agents")
            self.assertIsNone(unit_lookup.marked_unit("guilds", guild + "-unit"))

        note = self.root / "guild-restart.md"
        _ = note.write_text("Resume the guild loop.\n", encoding="utf-8")
        request = add_unit.launch_request(argparse.Namespace(
            production=str(self.doc), name=guild, plan="docs/plans/given.md", brief=None,
            standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
            resume="guild-session", cwd=str(self.root / ("project-" + guild)),
            restart_note=str(note), session_name=guild, host_session="guilds-" + guild, probe=False,
        ))
        self.assertEqual(request.unit, guild)
        self.assertIsInstance(request.launch_kind, add_unit.UnitRestoreLaunch)
        self.assertIsInstance(add_unit.row_is_present(request, self.doc.read_text().splitlines()),
                              add_unit.ExistingUnitRow)
        before = self.doc.read_bytes()
        resumed = self.cli(guild, "--plan", "docs/plans/given.md")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.doc.read_bytes(), before)
        self.assertEqual(sum(row[2:4] == ["agent", "start"] for row in self.herdr_calls()), 1)

    def test_guilds_designer_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("designers")

    def test_guilds_reviewer_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("reviewers")

    def test_guilds_showrunner_head_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("showrunners")

    def test_guilds_unit_head_launch_adoption_restart_and_lookup_keep_exact_id(self) -> None:
        self.assert_guild_head_launch_and_restart("units")

    def test_head_names_in_other_productions_and_other_names_in_guilds_keep_suffix(self) -> None:
        guilds_doc = self.doc.with_name("guilds-production.md")
        _ = guilds_doc.write_text(self.production_doc(), encoding="utf-8")
        for document, names in ((self.doc, ("designers", "reviewers", "showrunners", "units")),
                                (guilds_doc, ("reports", "designers-helper"))):
            for name in names:
                with self.subTest(production=document.name, name=name):
                    request = add_unit.launch_request(argparse.Namespace(
                        production=str(document), name=name, plan="docs/plans/given.md", brief=None,
                        standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
                        resume=None, cwd=None, restart_note=None, session_name=None, host_session=None,
                        probe=False,
                    ))
                    self.assertEqual(request.unit, name + "-unit")

    def test_exact_table_row_is_adopted_without_suffix_in_another_production(self) -> None:
        row = self.unit_row("designers", branch="build-followups-designers",
                            worktree=self.root / "project-designers").replace("| designers-unit |", "| `designers` |")
        self.commit_prepared_row(row)
        request = add_unit.launch_request(argparse.Namespace(
            production=str(self.doc), name="designers", plan="docs/plans/given.md", brief=None,
            standby=False, permanent=False, port=None, owns=None, timeout=90.0, check=False,
            resume=None, cwd=None, restart_note=None, session_name=None, host_session=None, probe=False,
        ))
        self.assertEqual(request.unit, "designers")
        self.assertIsInstance(add_unit.row_is_present(request, self.doc.read_text().splitlines()),
                              add_unit.ExistingUnitRow)

    def test_restore_flags_are_atomic_and_require_resume(self) -> None:
        note = self.root / "restart-note.txt"
        _ = note.write_text("resume here", encoding="utf-8")
        for flags in (
            ("--restart-note", str(note)),
            ("--session-name", "recorded-name"),
            ("--host-session", "recorded-herdr"),
            (
                "--restart-note", str(note),
                "--session-name", "recorded-name",
                "--host-session", "recorded-herdr",
            ),
        ):
            with self.subTest(flags=flags):
                result = self.cli("alpha", "--plan", "docs/plans/given.md", *flags)
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(self.servers_started(), [])

    def test_restore_keeps_claude_name_and_sanitizes_herdr_agent_name(self) -> None:
        self.commit_prepared_row(self.unit_row())
        prior = self.root / "prior session"
        prior.mkdir()
        note = self.root / "restart note.txt"
        restart_note = "Restarted with spaces, 'quotes', and $() unchanged."
        _ = note.write_text(restart_note, encoding="utf-8")
        recorded_session = "restored 'session' $()"
        recorded_host = "build-followups restore $()"
        existing_prompt = "Keep this previously scheduled prompt."
        with patch.dict(os.environ, self.env):
            conversation_pause.record_scheduled_prompts(
                "session-123", (existing_prompt,)
            )

        _ = self.successful(
            "alpha",
            "--plan", "docs/plans/given.md",
            "--resume", "session-123",
            "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", recorded_session,
            "--host-session", recorded_host,
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(self.opened()[1], SHARED)
        self.assertEqual(self.opened()[7], str(prior))
        self.assertIn(["--session", SHARED, "tab", "rename", "w1:t1",
                       "build-followups · alpha · unit director"], self.herdr_calls())
        self.assertIn(["--session", SHARED, "pane", "rename", "w1:p1", recorded_session], self.herdr_calls())
        claude = self.agent_arguments()
        prompt = self.opening_prompt()
        self.assertEqual(claude[claude.index("--remote-control") + 1], recorded_session)
        self.assertEqual(claude[claude.index("-n") + 1], recorded_session)
        self.assertNotIn("claude", claude)
        self.assertIn("You are now alpha-unit in production build-followups", prompt)
        self.assertTrue(prompt.endswith(restart_note), prompt)
        self.assertNotIn("Run /unit:direct", prompt)
        recorded = self.recorded_prompts("session-123")
        self.assertEqual(recorded, (existing_prompt, prompt))
        self.assertIs(conversation_pause.prompt_source(prompt, frozenset, lambda: recorded),
                      conversation_pause.PromptSource.SCHEDULED)
        start, = (row for row in self.herdr_calls() if row[2:4] == ["agent", "start"])
        self.assertEqual(start[4], "build-followups-restore----")

    def test_restore_refuses_launch_when_prompt_cannot_be_recorded(self) -> None:
        self.commit_prepared_row(self.unit_row())
        prior = self.root / "prior-session"
        prior.mkdir()
        note = self.root / "restart-note.txt"
        _ = note.write_text("Resume this unit.", encoding="utf-8")
        _ = self.pause_state.write_text("not a directory", encoding="utf-8")

        result = self.cli(
            "alpha",
            "--plan", "docs/plans/given.md",
            "--resume", "session-123",
            "--cwd", str(prior),
            "--restart-note", str(note),
            "--session-name", "recorded-name",
            "--host-session", "recorded-herdr",
         "--config-dir", str(Path(self.env["HOME"]) / ".claude2"))

        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertIn("add_unit: restart prompt not recorded:", result.stderr)
        self.assertEqual(self.servers_started(), [])

    def test_new_work_records_no_scheduled_prompt(self) -> None:
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")

        # herdr records the typed opening prompt under script-prompts; no scheduled prompt is kept.
        self.assertFalse((self.pause_state / "scheduled-prompts").exists())

    def test_brief_row_cannot_be_retried_as_plan_on_its_stub(self) -> None:
        _ = self.successful("alpha", "--brief", "Write a full plan")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        launches = len(self.servers_started())
        result = self.cli("alpha", "--plan", "docs/plans/build-followups-alpha.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(len(self.servers_started()), launches)

    def test_kebab_alias_refuses_existing_branch_and_worktree_row(self) -> None:
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("mul-add", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn("| mul-add-unit |", self.doc.read_text())

    def test_existing_local_or_origin_branch_refuses_before_any_write(self) -> None:
        for location in ("local", "origin"):
            with self.subTest(location=location):
                branch = "build-followups-alpha"
                if location == "local":
                    _ = self.git("branch", branch)
                else:
                    _ = self.git("branch", "-D", branch)
                    _ = self.git("push", "origin", f"HEAD:refs/heads/{branch}")
                original = self.doc.read_text()
                head = self.git("rev-parse", "HEAD")
                result = self.cli("alpha", "--brief", "Write a plan")
                self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                self.assertEqual(self.doc.read_text(), original)
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_occupied_worktree_refuses_before_row_commit_or_stub(self) -> None:
        occupied = self.root / "project-alpha"
        occupied.mkdir()
        _ = (occupied / "marker").write_text("keep", encoding="utf-8")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--brief", "Write a plan")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((occupied / "marker").read_text(), "keep")
        self.assertFalse((self.checkout / "docs/plans/build-followups-alpha.md").exists())

    def test_unrelated_repository_on_expected_branch_refuses_before_commit(self) -> None:
        occupied = self.root / "project-alpha"
        occupied.mkdir()
        _ = self.git("init", "-b", "build-followups-alpha", cwd=occupied)
        _ = self.doc.write_text(self.doc.read_text().replace(
            "## Gates", self.unit_row() + "\n\n## Gates"), encoding="utf-8")
        original = self.doc.read_text()
        head = self.git("rev-parse", "HEAD")
        result = self.cli("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.doc.read_text(), original)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)

    def test_another_live_session_leaves_the_launch_alone_and_the_pane_is_read_by_its_id(self) -> None:
        self.herdr_overrides["--session default session list --json"] = CommandResponse(code=0, stdout=json.dumps(
            herdr.SessionListResult(sessions=[herdr.SessionEntry(name="alpha2", running=True, default=False,
                session_dir="/fixture", socket_path="/fixture/socket")])))
        _ = self.successful("alpha", "--plan", "docs/plans/given.md")
        self.assertEqual(len(self.servers_started()), 1)
        self.assertIn(["--session", SHARED, "pane", "read", "w1:p1", "--source", "recent", "--lines", "60"], self.herdr_calls())

    def test_remote_control_polling_reads_each_host_through_its_place(self) -> None:
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc), identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(), session=add_unit.NewSession(),
            launch_kind=add_unit.NewWorkLaunch(), timeout=0,
        )
        for place in (HerdrPlace("native", "w1:p1"),):
            with self.subTest(place=place):
                marked = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(), place)
                with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                        patch.object(session_pane, "read_place", return_value="/remote-control is active") as read:
                    self.assertEqual(add_unit.wait_for_remote_control(request), marked)
                read.assert_called_once_with(place)
                with patch.object(add_unit, "launched_unit", return_value=add_unit.MarkedUnitFound(marked)), \
                        patch.object(session_pane, "read_place", side_effect=session_pane.HerdrFailure("unreadable")):
                    with self.assertRaisesRegex(RuntimeError, "did not become active\\nunreadable"):
                        _ = add_unit.wait_for_remote_control(request)

    def test_remote_control_is_read_from_the_session_record_after_the_pane_line_scrolls_off(self) -> None:
        request = add_unit.UnitLaunch(
            production=add_unit.read_production(self.doc), identity=add_unit.UnitIdentity("alpha-unit", "alpha"),
            branch="build-followups-alpha", worktree=self.root / "project-alpha", plan=add_unit.Standby(),
            port=add_unit.OmittedCell(), owns=add_unit.OmittedCell(), session=add_unit.NewSession(),
            launch_kind=add_unit.NewWorkLaunch(), timeout=0,
        )
        registry = self.root / "registry"
        registry.mkdir()
        place = HerdrPlace("native", "w1:p1")
        live = unit_lookup.LiveClaude("alpha", "session-123", "/socket", 4242)
        starting = unit_lookup.MarkedUnit("alpha-unit", unit_lookup.ClaudeNotRunning(), place)
        marked = unit_lookup.MarkedUnit("alpha-unit", live, place)
        busy_pane = "⏺ Reading the plan\n  ⎿  Read 400 lines"
        cases: tuple[tuple[str, dict[str, str], bool], ...] = (
            ("bridged", {"bridgeSessionId": "session_bridge"}, True),
            ("no bridge", {}, False),
            ("empty bridge", {"bridgeSessionId": ""}, False),
            ("another session's record", {"sessionId": "session-other", "bridgeSessionId": "session_bridge"}, False))
        for label, extra, active in cases:
            with self.subTest(label):
                _ = (registry / "4242.json").write_text(json.dumps({
                    "pid": 4242, "sessionId": "session-123", "name": "alpha", "messagingSocketPath": "/socket",
                    "updatedAt": 1, **extra}))
                # The first lookup finds the mark before the record names the live Claude.
                with patch.dict(os.environ, {"NOTIFIER_SESSIONS_DIR": str(registry)}), \
                        patch.object(add_unit, "launched_unit", side_effect=[
                            add_unit.MarkedUnitFound(starting), add_unit.MarkedUnitFound(marked)]), \
                        patch.object(session_pane, "read_place", return_value=busy_pane):
                    if active:
                        self.assertEqual(add_unit.wait_for_remote_control(request), marked)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "did not become active\\n⏺ Reading the plan"):
                            _ = add_unit.wait_for_remote_control(request)

    def test_underscore_name_and_checkout_without_trunk_suffix(self) -> None:
        renamed = self.root / "project"
        _ = self.checkout.rename(renamed)
        self.checkout = renamed
        self.doc = renamed / "docs/plans/build-followups-production.md"
        self.plan = renamed / "docs/plans/given.md"
        _ = (Path(self.env["HOME"]) / ".claude2/.claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "owner@example.com"},
            "projects": self.trusted_projects()}), encoding="utf-8")
        _ = self.doc.write_text(self.production_doc(), encoding="utf-8")
        _ = self.git("add", "docs/plans/build-followups-production.md")
        _ = self.git("commit", "-m", "move checkout")
        _ = self.git("push", "origin", "build-followups")
        _ = self.successful("mul_add", "--plan", "docs/plans/given.md")
        self.assertIn(self.unit_row("mul_add", branch="build-followups-mul-add",
                                    worktree=self.root / "project-mul-add"), self.doc.read_text())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD",
                                  cwd=self.root / "project-mul-add"), "build-followups-mul-add")


class PythonExecutionEnvironment(TypedDict):
    root: str
    extraPaths: list[str]


class PythonImportConfiguration(TypedDict):
    executionEnvironments: list[PythonExecutionEnvironment]


@final
class UnitLaunchProtocolTests(ShutdownTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        home = self.enterContext(tempfile.TemporaryDirectory())
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": home})
        ))
        del home_context

    def resume_instructions(self) -> str:
        command = SCRIPT.parents[2] / "commands/showrunner/produce.md"
        return command.read_text(encoding="utf-8").split("**Resume.**", 1)[1].split(
            "</LaunchUnits>", 1
        )[0]

    def test_manual_unit_resume_uses_add_unit_without_selecting_the_current_unit_role(self) -> None:
        instructions = self.resume_instructions()
        self.assertIn("add_unit.py", instructions)
        self.assertIn("--resume <session-id>", instructions)
        self.assertNotIn("env unit", instructions)
        self.assertNotIn("claude --resume", instructions)

    def test_manual_unit_resume_uses_configured_units_account_and_names_missing_configuration_command(self) -> None:
        instructions = self.resume_instructions()
        self.assertIn("starts it on the configured units account", instructions)
        self.assertIn("units account is not configured", instructions)
        self.assertIn("accounts.py move units", instructions)

    def test_production_import_paths_resolve_the_accounts_package(self) -> None:
        """F005: production's configured import search can find scripts.accounts."""
        root = SCRIPT.parents[2]
        configuration = cast(
            PythonImportConfiguration,
            cast(object, json.loads((root / "pyrightconfig.json").read_text(encoding="utf-8"))),
        )
        environment = next(
            candidate for candidate in configuration["executionEnvironments"]
            if candidate["root"] == "scripts/production"
        )

        self.assertTrue(any(
            (root / search_path / "scripts/accounts/accounts.py").is_file()
            for search_path in [environment["root"], *environment["extraPaths"]]
        ))


class RetiredRowTests(ShutdownTestCase):
    """A unit is retired by its Plan cell, or when its worktree and its herdr session are both gone."""

    root: Path = Path()
    linked: Path = Path()
    main: Path = Path()
    missing: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        home_context = cast(object, self.enterContext(
            patch.dict(os.environ, {"HOME": str(self.root / "home")})
        ))
        del home_context
        self.linked = self.root / "linked"
        self.linked.mkdir()
        _ = (self.linked / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        self.main = self.root / "main"
        (self.main / ".git").mkdir(parents=True)
        self.missing = self.root / "missing"

    def row(self, unit: str, worktree: Path, plan: str = "`docs/plan.md`") -> str:
        return f"| `{unit}-unit` | {plan} | `{worktree}` | `branch` | `{unit}` (resumed elsewhere) | — | — |"

    def session(self, unit: str) -> unit_lookup.MarkedUnit:
        return unit_lookup.MarkedUnit(unit, unit_lookup.ClaudeNotRunning(), HerdrPlace("fixture", "w1:p1"))

    def retired(self, worktree: Path, *, gone: bool, plan: str = "`docs/plan.md`") -> tuple[bool, int]:
        """Whether the row is retired, and how many times herdr was asked."""
        found = {} if gone else {"some-unit": self.session("some-unit")}
        cells = production_units.unit_table([*OLD_HEADER, self.row("some", worktree, plan)])[0]
        with patch.object(unit_lookup, "marked_units", return_value=found) as asked:
            return production_units.unit_is_retired(cells, production_units.SessionMarks("show")), asked.call_count

    def test_a_unit_is_gone_only_without_both_its_worktree_and_its_session(self) -> None:
        self.assertEqual(self.retired(self.missing, gone=True), (True, 1))
        self.assertEqual(self.retired(self.missing, gone=False), (False, 1))
        # A session that is only restarting keeps its row: its worktree is there, and herdr is not asked.
        self.assertEqual(self.retired(self.linked, gone=True), (False, 0))
        # A main checkout is shared, so it holds no unit once the session is gone.
        self.assertEqual(self.retired(self.main, gone=True), (True, 1))
        self.assertEqual(self.retired(self.main, gone=False), (False, 1))
        self.assertEqual(self.retired(self.linked, gone=False, plan="retired after completion"), (True, 0))

    def test_herdr_must_say_the_session_is_gone_and_is_asked_once_for_a_whole_table(self) -> None:
        lines = [*OLD_HEADER, self.row("first", self.missing), self.row("second", self.missing)]
        with patch.object(unit_lookup, "marked_units", side_effect=OSError("herdr could not list sessions")) as asked:
            self.assertEqual(retired_units(lines, "show"), set())
        self.assertEqual(asked.call_count, 1)
        with patch.object(unit_lookup, "marked_units", return_value={"second-unit": self.session("second-unit")}) as asked:
            self.assertEqual(retired_units(lines, "show"), {"first-unit"})
        self.assertEqual(asked.call_count, 1)

    def test_every_list_drops_a_unit_that_is_gone(self) -> None:
        lines = [
            *OLD_HEADER,
            self.row("marked", self.linked, "retired after completion"),
            self.row("gone", self.missing),
            self.row("parked", self.linked),
        ]
        with patch.object(unit_lookup, "marked_units", return_value={}):
            self.assertEqual(retired_units(lines, "show"), {"marked-unit", "gone-unit"})
            self.assertEqual([cell_value(cells["Unit"]) for cells in live_unit_table(lines, "show")], ["parked-unit"])


if __name__ == "__main__":
    _ = unittest.main()
