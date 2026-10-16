"""CLI tests using fake herdr, process ancestry, session records and showrunner state."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TypedDict, cast, override
from unittest.mock import patch

import herdr
import herdr_sessions
from fake_herdr import CommandResponse
from sessions import SessionRecord, UnreadableSessionRecord, read_session
import showrunners
from scripts.shutdown import launch_permission
from scripts.accounts import accounts


class SessionListReply(TypedDict):
    ok: bool
    result: herdr.SessionListResult


class HerdrSessionsTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    records: Path = Path()
    state: Path = Path()
    responses: Path = Path()
    log: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc = self.root / "proc"
        self.records = self.root / "sessions"
        self.state = self.root / "state"
        for directory in (self.proc, self.records, self.state):
            directory.mkdir()
        self.responses = self.root / "responses.json"
        self.log = self.root / "commands.jsonl"
        self.enterContext(patch.dict(os.environ, {
            "HERDR_BIN": str(Path(__file__).with_name("fake_herdr.py")),
            "HERDR_PROC": str(self.proc),
            "NOTIFIER_SESSIONS_DIR": str(self.records),
            "NOTIFIER_STATE_DIR": str(self.state),
            "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.responses),
        }))
        _ = self.enterContext(patch.object(herdr_sessions, "live_session", side_effect=self.record_is_live))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state))
        _ = self.enterContext(patch.object(showrunners, "SESSIONS_DIR", self.records))
        _ = self.enterContext(patch.object(showrunners, "socket_for", return_value="fake-socket"))
        _ = self.enterContext(patch.object(showrunners, "_session_name", side_effect=self.registered_name))
        _ = self.enterContext(patch.object(showrunners, "user_zone_name", return_value="UTC"))
        self.respond([])

    def record_is_live(self, record: SessionRecord) -> bool:
        return (self.proc / str(record["pid"]) / "alive").exists()

    def registered_name(self, session_id: str) -> str:
        for path in self.records.glob("*.json"):
            record = read_session(path)
            if not isinstance(record, UnreadableSessionRecord) and record["sessionId"] == session_id:
                return record["name"]
        raise ValueError(f"no fake session {session_id}")

    def respond(self, names: list[str], *, error: str = "") -> None:
        entries = [herdr.SessionEntry(name=name, running=True, default=name == "default",
                                     session_dir=str(self.root / name), socket_path="fake-socket")
                   for name in names]
        reply = SessionListReply(ok=True, result=herdr.SessionListResult(sessions=entries))
        response: CommandResponse = {"stdout": json.dumps(reply),
                                     "code": 0}
        if error:
            refusal = herdr.HerdrRefusal(error=herdr.HerdrError(code="unreadable", message=error))
            response = {"stdout": json.dumps(refusal),
                        "code": 1}
        _ = self.responses.write_text(json.dumps({"--session default session list --json": response}))

    def process(self, pid: int, parent: int, argv: list[str], environment: dict[str, str]) -> None:
        directory = self.proc / str(pid)
        directory.mkdir()
        _ = (directory / "stat").write_text(f"{pid} (fake process) S {parent} " + "0 " * 20)
        _ = (directory / "status").write_text(f"Name:\tfake\nPPid:\t{parent}\nUid:\t{os.getuid()}\n")
        _ = (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
        _ = (directory / "environ").write_bytes(
            ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode())
        (directory / "cwd").symlink_to(self.root, target_is_directory=True)

    def server(self, pid: int, name: str) -> None:
        self.process(pid, 1, ["herdr", "--session", name, "server"], {})
        config = self.root / "config"
        os.environ["XDG_CONFIG_HOME"] = str(config)
        table = self.proc / "net/unix"
        table.parent.mkdir(exist_ok=True)
        with table.open("a") as output:
            _ = output.write(f"0: 2 0 00010000 0001 01 {pid} {config}/herdr/sessions/{name}/herdr.sock\n")
        fd = self.proc / str(pid) / "fd"
        fd.mkdir()
        (fd / "3").symlink_to(f"socket:[{pid}]")

    def claude(self, pid: int, server: int, name: str, *, updated: int = 1, live: bool = True,
               marks: dict[str, str] | None = None) -> None:
        self.process(pid, server, ["/bin/claude"], {"HERDR_PANE_ID": f"w1:p{pid}", **(marks or {})})
        if live:
            (self.proc / str(pid) / "alive").touch()
        record = SessionRecord(pid=pid, sessionId=f"id-{pid}", name=name, messagingSocketPath="fake-socket",
                               updatedAt=updated, tmux="", formerNames=[])
        _ = (self.records / f"{pid}.json").write_text(json.dumps(record))

    def register_showrunner(self, pid: int) -> None:
        instance = self.state / "showrunner-production"
        instance.mkdir()
        doc = self.root / "production.md"
        _ = doc.write_text("fake production")
        _ = (instance / "conf").write_text(
            f"TARGET=session:id-{pid}\nCHECK=/fake/production_check.sh {doc}\n")

    def call(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = herdr_sessions.main(["herdr_sessions.py", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def commands(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()]

    def test_list_roles_and_newest_live_claude_name(self) -> None:
        self.respond(["unit", "showrunner", "standalone", "empty"])
        for pid, name in ((100, "unit"), (200, "showrunner"), (300, "standalone")):
            self.server(pid, name)
        marks = {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "herdr-trial-unit"}
        self.claude(101, 100, "old-unit-name", marks=marks)
        self.claude(102, 100, "new-unit-name", updated=2, marks=marks)
        self.claude(103, 100, "dead-newest", updated=3, live=False)
        self.claude(201, 200, "director")
        self.register_showrunner(201)
        self.claude(301, 300, "solo")
        self.claude(401, 1, "outside-herdr", updated=9)
        _ = (self.records / "malformed.json").write_text("{broken")
        code, stdout, stderr = self.call("list")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, "unit\tunit build-followups/herdr-trial-unit\tnew-unit-name\n"
                         + "showrunner\tshowrunner\tdirector\nstandalone\tstandalone\tsolo\n"
                         + "empty\tstandalone\t\n")
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"]])

    def test_unit_role_takes_precedence_over_showrunner(self) -> None:
        self.respond(["unit"])
        self.server(100, "unit")
        self.claude(101, 100, "director", marks={"SHOWRUNNER_UNIT": "build", "SHOWRUNNER_UNIT_ID": "one"})
        self.register_showrunner(101)
        self.assertEqual(self.call("list"), (0, "unit\tunit build/one\tdirector\n", ""))

    def test_unit_marks_are_visible_without_a_claude_record(self) -> None:
        self.respond(["unit"])
        self.server(100, "unit")
        self.process(101, 100, ["sleep", "600"], {"HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": "build",
                                                "SHOWRUNNER_UNIT_ID": "one"})
        self.assertEqual(self.call("list"), (0, "unit\tunit build/one\t\n", ""))

    def test_partial_unit_marks_do_not_make_a_unit_role(self) -> None:
        self.respond(["solo"])
        self.server(100, "solo")
        self.claude(101, 100, "solo-name", marks={"SHOWRUNNER_UNIT": "build"})
        self.assertEqual(self.call("list"), (0, "solo\tstandalone\tsolo-name\n", ""))

    def test_empty_list(self) -> None:
        self.assertEqual(self.call("list"), (0, "", ""))
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"]])

    def test_session_without_a_registry_has_an_empty_claude_name(self) -> None:
        self.respond(["empty"])
        self.records.rmdir()
        self.assertEqual(self.call("list"), (0, "empty\tstandalone\t\n", ""))

    def test_attach_executes_only_a_running_session(self) -> None:
        self.respond(["target"])
        with patch.object(os, "execvp", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit) as replaced:
                _ = self.call("attach", "target")
        self.assertEqual(replaced.exception.code, 0)
        binary = os.environ["HERDR_BIN"]
        execute.assert_called_once_with(binary, [binary, "session", "attach", "target"])
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"]])

    def test_attach_refuses_missing_session_and_lists_running_names(self) -> None:
        self.respond(["one", "two"])
        with patch.object(os, "execvp") as execute:
            code, stdout, stderr = self.call("attach", "missing")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("missing is not running; running sessions: one, two", stderr)
        execute.assert_not_called()
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"]])

    def test_attach_finds_bare_binary_on_path(self) -> None:
        self.respond(["target"])
        fake = self.root / "herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        fake.chmod(0o755)
        with patch.dict(os.environ, {"HERDR_BIN": "herdr", "PATH": str(self.root)}):
            result = subprocess.run(
                [sys.executable, str(Path(herdr_sessions.__file__)), "attach", "target"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.commands(), [
            ["--session", "default", "session", "list", "--json"],
            ["session", "attach", "target"],
        ])

    def test_attach_refusal_with_no_running_sessions(self) -> None:
        code, stdout, stderr = self.call("attach", "missing")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("running sessions: none", stderr)

    def test_herdr_failure_returns_unreadable_status(self) -> None:
        self.respond([], error="cannot read sessions")
        for arguments in (("list",), ("attach", "missing")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.call(*arguments)
                self.assertEqual((code, stdout), (3, ""))
                self.assertIn("cannot read sessions", stderr)

    def test_missing_herdr_returns_unreadable_status(self) -> None:
        with patch.dict(os.environ, {"HERDR_BIN": str(self.root / "missing")}):
            code, stdout, stderr = self.call("list")
        self.assertEqual((code, stdout), (3, ""))
        self.assertIn("herdr_sessions:", stderr)

    def test_list_timeout_returns_unreadable_status_without_traceback(self) -> None:
        with patch.object(herdr, "run_program", side_effect=subprocess.TimeoutExpired("fake-herdr", 0.01)):
            code, stdout, stderr = self.call("list")
        self.assertEqual((code, stdout), (3, ""))
        self.assertIn("herdr_sessions:", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_attach_exec_failure_returns_unreadable_status(self) -> None:
        self.respond(["target"])
        with patch.object(os, "execvp", side_effect=OSError("attach failed")):
            self.assertEqual(self.call("attach", "target"), (3, "", "herdr_sessions: attach failed\n"))

    def test_usage_errors_do_not_call_herdr(self) -> None:
        for arguments in ((), ("unknown",), ("list", "extra"), ("attach",), ("attach", ""),
                          ("attach", "one", "extra")):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.call(*arguments), (2, "", herdr_sessions.USAGE + "\n"))
        self.assertEqual(self.commands(), [])


class StandaloneLaunchTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    log: Path = Path()
    responses_path: Path = Path()
    responses: dict[str, CommandResponse | list[CommandResponse]] = {}
    attachments: list[tuple[str, list[str]]] = []

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.log = self.root / "commands"
        self.responses_path = self.root / "responses"
        home = self.root / "home"
        home.mkdir()
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(home), "XDG_CONFIG_HOME": str(self.root / "config"), "PATH": os.environ.get("PATH", os.defpath),
            "HERDR_BIN": str(Path(__file__).with_name("fake_herdr.py")),
            "SYSTEMD_RUN": str(Path(__file__).with_name("fake_herdr.py")),
            "SYSTEMCTL": str(Path(__file__).with_name("fake_herdr.py")),
            "HERDR_PROC": str(self.proc), "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.responses_path),
        }, clear=True))
        self.responses = {"default": {"stdout": json.dumps({"result": {
            "workspaces": [], "root_pane": {"pane_id": "w1:p1"},
            "panes": [{"pane_id": "w1:p1", "label": "solo"}],
        }}), "code": 0}}
        _ = (home / ".claude.json").write_text(json.dumps({"projects": {
            str(self.root): {"hasTrustDialogAccepted": True},
        }, "oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
        self.save()
        _ = self.enterContext(patch.object(launch_permission, "launch_barrier", side_effect=lambda: nullcontext()))
        _ = self.enterContext(patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()))
        self.attachments = []
        _ = self.enterContext(patch.object(os, "execvp", side_effect=self.record_attachment))

    def save(self) -> None:
        _ = self.responses_path.write_text(json.dumps(self.responses))

    def record_attachment(self, binary: str, argv: list[str]) -> None:
        self.attachments.append((binary, argv))

    def destination(self, name: str, environment: dict[str, str] | None = None, *, occupied: bool = False) -> None:
        self.responses[f"--session {name} status server"] = ([{"stdout": "status: running", "code": 0}] if occupied else [
            {"stdout": "status: stopped", "code": 0}, {"stdout": "status: running", "code": 0},
        ])
        self.save()
        pid = len(list(self.proc.glob("[0-9]*"))) + 100
        process = self.proc / str(pid)
        (process / "fd").mkdir(parents=True)
        (process / "fd/3").symlink_to(f"socket:[{pid}]")
        _ = (process / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in (environment or {}).items()).encode())
        table = self.proc / "net/unix"
        table.parent.mkdir(exist_ok=True)
        with table.open("a") as output:
            _ = output.write(f"0: 2 0 00010000 0001 01 {pid} {self.root}/config/herdr/sessions/{name}/herdr.sock\n")

    def call(self, *arguments: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = herdr_sessions.main(["herdr_sessions.py", *arguments])
        return code, stderr.getvalue()

    def commands(self) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def agent_starts(self) -> list[list[str]]:
        return [command for command in self.commands() if command[2:4] == ["agent", "start"]]

    def numbered_account(self) -> Path:
        notes = self.root / "notes"
        notes.mkdir(exist_ok=True)
        os.environ["AGENT_NOTES_DIR"] = str(notes)
        _ = (notes / "claude4.md").write_text("---\nlogin: numbered@example.invalid\n---\n")
        self.assertEqual(accounts.setup("claude4"), [])
        directory = accounts.account_dir("claude4")
        _ = (directory / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "numbered@example.invalid"},
            "projects": {str(self.root): {"hasTrustDialogAccepted": True}},
        }))
        return directory

    def test_start_after_cutover_uses_showrunners_account_for_pane_and_admission(self) -> None:
        directory = self.numbered_account()
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"showrunners": "claude4"}))
        _ = (Path.home() / ".claude.json").write_text("{}")
        self.destination("solo")
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()) as permission:
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)), (0, ""))
        environment = cast(dict[str, str], permission.call_args.args[0])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(directory))
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertIn(f"CLAUDE_CODE_REMOTE_MEMORY_DIR={Path.home() / '.claude'}", workspace)
        self.assertEqual(len(self.agent_starts()), 1)

    def test_start_opens_promptless_default_account_and_attaches(self) -> None:
        self.destination("solo")
        self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root), "--", "--model", "opus"), (0, ""))
        start, = self.agent_starts()
        self.assertEqual(start[start.index("--") + 1:], ["--model", "opus", "-n", "solo"])
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn("HERDR_AGENT_PANE=1", workspace)
        self.assertFalse(any("CLAUDE_CONFIG_DIR=" in value for value in workspace))
        self.assertFalse(any(command[2:4] in (["agent", "prompt"], ["pane", "send-keys"]) for command in self.commands()))
        binary = os.environ["HERDR_BIN"]
        self.assertEqual(self.attachments, [(binary, [binary, "session", "attach", "solo"])])

    def test_start_inherits_named_account_for_pane_and_new_work_admission(self) -> None:
        directory = self.numbered_account()
        self.destination("solo")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(directory), "CLAUDE_CODE_OAUTH_TOKEN": "unrelated"}), patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()) as permission:
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)), (0, ""))
        environment = cast(dict[str, str], permission.call_args.args[0])
        purpose = cast(launch_permission.LaunchPurpose, permission.call_args.args[1])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(directory))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
        self.assertIsInstance(purpose, launch_permission.NewWorkLaunchPurpose)
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertEqual(len(self.agent_starts()), 1)

    def test_generated_name_collision_retries_next_suffix(self) -> None:
        first = herdr.generated_session_name(self.root.name, "1")
        second = herdr.generated_session_name(self.root.name, "2")
        self.destination(first, occupied=True)
        self.destination(second)
        self.assertEqual(self.call("start", "--cwd", str(self.root)), (0, ""))
        self.assertEqual([command[1] for command in self.agent_starts()], [second])
        self.assertFalse(any(command[1:4] == [first, "workspace", "create"] for command in self.commands()))

    def test_explicit_occupied_or_invalid_name_starts_no_agent(self) -> None:
        self.destination("solo", occupied=True)
        code, error = self.call("start", "--name", "solo", "--cwd", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("already taken", error)
        before = self.commands()
        for name in ("", "a b", ".", "..", "a" * 65):
            code, error = self.call("start", "--name", name, "--cwd", str(self.root))
            self.assertEqual(code, 1)
            self.assertIn("ASCII", error)
        self.assertEqual(self.commands(), before)
        self.assertEqual(self.agent_starts(), [])

    def test_resume_named_account_sends_multiline_file_once_and_preserves_arguments(self) -> None:
        directory = self.numbered_account()
        prompt = self.root / "prompt"
        _ = prompt.write_text("first line\nsecond line\n")
        self.destination("solo")
        arguments = ("resume", "solo", "--session-id", "recorded-id", "--cwd", str(self.root),
                     "--config-dir", str(directory), "--prompt-file", str(prompt), "--attach", "--",
                     "--model", "opus", "--remote-control", "kept name", "--settings", '{"x":true}')
        original = Path.read_text
        reads: list[Path] = []

        def read(path: Path, encoding: str | None = None) -> str:
            reads.append(path)
            return original(path, encoding=encoding)

        with patch.object(Path, "read_text", read):
            self.assertEqual(self.call(*arguments), (0, ""))
        self.assertEqual(reads.count(prompt), 1)
        start, = self.agent_starts()
        self.assertEqual(start[start.index("--") + 1:], ["--model", "opus", "--remote-control", "kept name", "--settings", '{"x":true}', "--resume", "recorded-id", "-n", "solo"])
        prompts = [command for command in self.commands() if command[2:4] == ["agent", "prompt"]]
        self.assertEqual(prompts, [["--session", "solo", "agent", "prompt", "w1:p1", "first line\nsecond line\n"]])
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertIn(f"CLAUDE_CODE_REMOTE_MEMORY_DIR={self.root / 'home/.claude'}", workspace)
        self.assertIn("HERDR_AGENT_PANE=1", workspace)

    def test_resume_without_prompt_or_attach_leaves_user_input_untouched(self) -> None:
        self.destination("solo")
        with patch.object(os, "execvp") as execute:
            self.assertEqual(self.call("resume", "solo", "--session-id", "recorded", "--cwd", str(self.root)), (0, ""))
        execute.assert_not_called()
        self.assertEqual(len(self.agent_starts()), 1)
        self.assertFalse(any(command[2:4] == ["agent", "prompt"] for command in self.commands()))

    def test_untrusted_folder_refuses_with_account_trust_command_before_server_start(self) -> None:
        _ = (self.root / "home/.claude.json").write_text('{"projects": {}, "oauthAccount": {"emailAddress": "legacy@example.invalid"}}')
        for verb in (("start", "--name", "solo"), ("resume", "solo", "--session-id", "recorded")):
            code, error = self.call(*verb, "--cwd", str(self.root))
            self.assertEqual(code, 1)
            self.assertIn("Run: cd ", error)
            self.assertIn("claude", error)
        self.assertEqual(self.commands(), [])

    def test_inherited_default_account_variables_refuse_before_agent_start(self) -> None:
        for variable in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            name = "solo-" + variable.lower()
            self.destination(name, {variable: "private"})
            code, error = self.call("start", "--name", name, "--cwd", str(self.root))
            self.assertEqual(code, 1)
            self.assertIn(variable, error)
            self.assertIn(f"herdr --session {name} server stop", error)
            self.assertNotIn("private", error)
        self.assertEqual(self.agent_starts(), [])

    def test_shutdown_hold_and_unreadable_state_refuse_both_verbs_before_server_start(self) -> None:
        for permission, reason in ((launch_permission.BlockedByShutdown("account is held"), "account is held"),
                                   (launch_permission.ShutdownStateUnreadable("broken record"), "broken record")):
            for verb in (("start", "--name", "solo"), ("resume", "solo", "--session-id", "recorded")):
                with patch.object(launch_permission, "launch_permission", return_value=permission):
                    code, error = self.call(*verb, "--cwd", str(self.root))
                self.assertEqual(code, 1)
                self.assertIn(reason, error)
        self.assertEqual(self.commands(), [])

    def test_restore_admission_uses_recorded_session_and_selected_account_under_barrier(self) -> None:
        self.destination("solo")
        held = [False]

        from contextlib import contextmanager
        from collections.abc import Generator, Mapping

        @contextmanager
        def barrier() -> Generator[None, None, None]:
            held[0] = True
            try:
                yield
            finally:
                held[0] = False

        def permission(environment: Mapping[str, str], purpose: launch_permission.LaunchPurpose) -> launch_permission.LaunchPermission:
            self.assertTrue(held[0])
            self.assertNotIn("CLAUDE_CONFIG_DIR", environment)
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", environment)
            self.assertEqual(purpose, launch_permission.RecordedSessionRestoreLaunchPurpose("recorded"))
            return launch_permission.AllowedByShutdownState()

        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "inherited"}), patch.object(launch_permission, "launch_barrier", barrier), patch.object(launch_permission, "launch_permission", permission):
            self.assertEqual(self.call("resume", "solo", "--session-id", "recorded", "--cwd", str(self.root), "--restore"), (0, ""))
        self.assertFalse(held[0])
        self.assertEqual(len(self.agent_starts()), 1)

    def test_unreadable_launch_barrier_starts_no_server(self) -> None:
        with patch.object(launch_permission, "launch_barrier", side_effect=OSError("lock unavailable")):
            code, error = self.call("start", "--name", "solo", "--cwd", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("shutdown state unreadable: launch barrier: lock unavailable", error)
        self.assertEqual(self.commands(), [])


if __name__ == "__main__":
    _ = unittest.main()
