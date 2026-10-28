"""CLI tests using fake herdr, process ancestry, session records and showrunner state."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir, nullcontext
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from datetime import datetime, timezone
from typing import TypedDict, cast, override
from unittest.mock import MagicMock, Mock, patch

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


def setUpModule() -> None:
    """These fixtures supply Linux proc trees and systemd stand-ins."""
    _ = unittest.enterModuleContext(patch.object(herdr, "_PLATFORM", "linux"))


class HerdrSessionsTests(unittest.TestCase):
    root: Path = Path()
    proc: Path = Path()
    records: Path = Path()
    state: Path = Path()
    responses: Path = Path()
    log: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
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
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "layout-state"),
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

    def respond(self, names: list[str], *, tabs: tuple[str | None, ...] = (), error: str = "") -> None:
        """Running sessions `names`; the shared session `agents` holds one tab per label in `tabs`, pane w1:p1 first."""
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
        panes = [{"pane_id": f"w1:p{index}", "label": label, "workspace_id": "w1", "tab_id": f"w1:t{index}"}
                 for index, label in enumerate(tabs, 1)]
        _ = self.responses.write_text(json.dumps({
            "--session default session list --json": response,
            "--session agents pane list": {"stdout": json.dumps({"result": {"panes": panes}}), "code": 0},
        }))

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

    def claude(self, pid: int, server: int, name: str, *, pane: str, updated: int = 1, live: bool = True,
               marks: dict[str, str] | None = None) -> None:
        self.process(pid, server, ["/bin/claude"], {"HERDR_PANE_ID": pane, **(marks or {})})
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

    def test_list_prints_each_tab_of_the_shared_session_with_its_role_and_newest_live_claude_name(self) -> None:
        self.respond(["agents", "other"], tabs=("unit", "showrunner", "standalone", None))
        self.server(100, "agents")
        self.server(900, "other")
        marks = {"SHOWRUNNER_UNIT": "build-followups", "SHOWRUNNER_UNIT_ID": "herdr-trial-unit"}
        self.claude(101, 100, "old-unit-name", pane="w1:p1", marks=marks)
        self.claude(102, 100, "new-unit-name", pane="w1:p1", updated=2, marks=marks)
        self.claude(103, 100, "dead-newest", pane="w1:p1", updated=3, live=False)
        self.claude(201, 100, "director", pane="w1:p2")
        self.register_showrunner(201)
        self.claude(301, 100, "solo", pane="w1:p3")
        self.claude(401, 1, "outside-herdr", pane="w1:p3", updated=9)
        # Another session's pane ids never name a tab of the shared session.
        self.claude(901, 900, "elsewhere", pane="w1:p4", updated=9, marks=marks)
        _ = (self.records / "malformed.json").write_text("{broken")
        code, stdout, stderr = self.call("list")
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, "unit\tunit build-followups/herdr-trial-unit\tnew-unit-name\n"
                         + "showrunner\tshowrunner\tdirector\nstandalone\tstandalone\tsolo\n"
                         + "w1:p4\tstandalone\t\n")
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"],
                                           ["--session", "agents", "pane", "list"]])

    def test_unit_role_takes_precedence_over_showrunner(self) -> None:
        self.respond(["agents"], tabs=("unit",))
        self.server(100, "agents")
        self.claude(101, 100, "director", pane="w1:p1", marks={"SHOWRUNNER_UNIT": "build", "SHOWRUNNER_UNIT_ID": "one"})
        self.register_showrunner(101)
        self.assertEqual(self.call("list"), (0, "unit\tunit build/one\tdirector\n", ""))

    def test_unit_marks_are_visible_without_a_claude_record(self) -> None:
        self.respond(["agents"], tabs=("unit",))
        self.server(100, "agents")
        self.process(101, 100, ["sleep", "600"], {"HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": "build",
                                                "SHOWRUNNER_UNIT_ID": "one"})
        self.assertEqual(self.call("list"), (0, "unit\tunit build/one\t\n", ""))

    def test_partial_unit_marks_do_not_make_a_unit_role(self) -> None:
        self.respond(["agents"], tabs=("solo",))
        self.server(100, "agents")
        self.claude(101, 100, "solo-name", pane="w1:p1", marks={"SHOWRUNNER_UNIT": "build"})
        self.assertEqual(self.call("list"), (0, "solo\tstandalone\tsolo-name\n", ""))

    def test_sort_tabs_ignores_saved_order_saves_unit_groups_and_keeps_split_workspaces(self) -> None:
        self.respond(["agents"])
        self.server(100, "agents")
        for pid, pane in ((101, "w1:p2"), (102, "w1:p4")):
            self.process(pid, 100, ["claude"], {"HERDR_PANE_ID": pane, "SHOWRUNNER_UNIT": "prod",
                                               "SHOWRUNNER_UNIT_ID": str(pid)})
        tabs = [{"tab_id": f"w1:t{index}", "label": title, "pane_count": 1, "workspace_id": "w1"}
                for index, title in enumerate(("boss", "zulu", "apple", "mike"), 1)]
        split_tabs = [{"tab_id": "w2:t1", "label": "split", "pane_count": 2, "workspace_id": "w2"},
                      {"tab_id": "w2:t2", "label": "zebra", "pane_count": 1, "workspace_id": "w2"},
                      {"tab_id": "w2:t3", "label": "alpha", "pane_count": 1, "workspace_id": "w2"}]
        spaces = [{"workspace_id": "w1", "label": "boss", "focused": False, "active_tab_id": "w1:t3"},
                  {"workspace_id": "w2", "label": "split", "focused": True, "active_tab_id": "w2:t1"}]

        def reply(result: dict[str, object]) -> CommandResponse:
            return {"stdout": json.dumps({"result": result}), "code": 0}

        replies = cast(dict[str, CommandResponse | list[CommandResponse]], json.loads(self.responses.read_text()))
        replies["--session agents workspace list"] = reply({"workspaces": spaces})
        replies["--session agents tab list --workspace w1"] = reply({"tabs": tabs})
        replies["--session agents tab list --workspace w2"] = reply({"tabs": split_tabs})
        replies["--session agents pane list"] = reply({"panes": [
            {"pane_id": f"w1:p{index}", "tab_id": f"w1:t{index}"} for index in range(1, 5)]})
        sorted_tabs = [tabs[0]]
        for index, tab_number in ((4, 5), (2, 6), (3, 7)):
            tab_id = f"w1:t{tab_number}"
            replies[f"--session agents pane move w1:p{index} --new-tab --workspace w1"] = reply({
                "move_result": {"created_tab": {"tab_id": tab_id}, "pane": {"pane_id": f"w1:p{index}"}}})
            sorted_tabs.append({**tabs[index - 1], "tab_id": tab_id})
        replies["--session agents tab list"] = [reply({"tabs": tabs + split_tabs}), reply({"tabs": sorted_tabs + split_tabs})]
        replies["default"] = {"stdout": "", "code": 0}
        _ = self.responses.write_text(json.dumps(replies))
        path = herdr.layout_path("agents")
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps(herdr.SavedLayout(at=100.0, workspaces=[herdr.SavedSpace(
            name="boss", seen=100.0, tabs=[herdr.SavedName(name=str(tab["label"]), seen=100.0) for tab in tabs])])))

        self.assertEqual(self.call("sort-tabs"),
                         (0, "herdr_sessions: split: tab with several panes; order kept\n", ""))
        acted = [command[2:] for command in self.commands() if command[3:4] in (["move"], ["rename"], ["focus"])]
        self.assertEqual(acted, [
            ["pane", "move", "w1:p4", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t5", "mike"],
            ["pane", "move", "w1:p2", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t6", "zulu"],
            ["pane", "move", "w1:p3", "--new-tab", "--workspace", "w1"], ["tab", "rename", "w1:t7", "apple"],
            ["tab", "focus", "w1:t7"], ["workspace", "focus", "w2"]])
        saved = {space["name"]: [tab["name"] for tab in space["tabs"]] for space in herdr.saved_layout("agents")["workspaces"]}
        self.assertEqual(saved, {"boss": ["boss", "mike", "zulu", "apple"], "split": ["split", "zebra", "alpha"]})

    def test_list_follows_the_named_shared_session(self) -> None:
        shared = self.root / "config/herdr/shared-session"
        shared.parent.mkdir(parents=True)
        _ = shared.write_text("desk\n")
        self.respond(["agents", "desk"], tabs=("not-shown",))
        replies = cast(dict[str, CommandResponse], json.loads(self.responses.read_text()))
        replies["--session desk pane list"] = {"stdout": json.dumps({"result": {"panes": [
            {"pane_id": "w2:p1", "label": "writing"}]}}), "code": 0}
        _ = self.responses.write_text(json.dumps(replies))
        self.assertEqual(self.call("list"), (0, "writing\tstandalone\t\n", ""))

    def test_empty_list(self) -> None:
        self.assertEqual(self.call("list"), (0, "", ""))
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"]])

    def test_remote_list_runs_the_hosts_cli_and_preserves_its_output(self) -> None:
        output = "writing\tstandalone\tmy conversation\nunit\tunit production/unit\tdirector\n"
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ssh"], 0, output, "")) as remote:
            self.assertEqual(self.call("list", "--on", "user@mac"), (0, output, ""))
        remote.assert_called_once_with(
            ["ssh", "user@mac", "~/.claude/scripts/lib/py", "~/.claude/scripts/herdr/herdr_sessions.py", "list"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        self.assertEqual(self.commands(), [])

    def test_remote_list_failures_name_the_host_and_return_one_line(self) -> None:
        for status, reason in ((255, "ssh: host unreachable\nConnection closed\n"),
                               (1, "herdr_sessions: cannot read sessions\n"), (3, "")):
            with (self.subTest(status=status), patch.object(subprocess, "run", return_value=
                    subprocess.CompletedProcess(["ssh"], status, "partial output", reason))):
                code, output, error = self.call("list", "--on", "mac")
            self.assertEqual((code, output), (1, ""))
            self.assertEqual(len(error.splitlines()), 1)
            self.assertIn(f"mac: remote list failed (ssh exit {status})", error)
            self.assertIn(" ".join(reason.split()) or "no error message", error)
        self.assertEqual(self.commands(), [])

    def test_remote_list_timeout_and_missing_ssh_return_one_without_a_traceback(self) -> None:
        for failure, reason in ((subprocess.TimeoutExpired(["ssh"], 15), "timed out after 15 seconds"),
                                (FileNotFoundError("ssh not installed"), "ssh not installed")):
            with self.subTest(reason=reason), patch.object(subprocess, "run", side_effect=failure):
                code, output, error = self.call("list", "--on", "mac")
            self.assertEqual((code, output), (1, ""))
            self.assertEqual(len(error.splitlines()), 1)
            self.assertIn("mac: remote list", error)
            self.assertIn(reason, error)
            self.assertNotIn("Traceback", error)
        self.assertEqual(self.commands(), [])

    def test_remote_attach_focuses_the_hosts_tab_then_attaches_its_shared_session(self) -> None:
        self.respond([], error="local sessions unreadable")
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(["ssh"], 0, "desk\n", "")) as remote, \
                patch.object(os, "execvp", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit) as replaced:
                _ = self.call("attach-remote", "user@mac", "writing")
        self.assertEqual(replaced.exception.code, 0)
        remote.assert_called_once_with(
            ["ssh", "user@mac", "~/.claude/scripts/lib/py", "~/.claude/scripts/herdr/herdr_sessions.py", "focus", "writing"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        binary = os.environ["HERDR_BIN"]
        execute.assert_called_once_with(binary, [binary, "--remote", "user@mac", "--session", "desk"])
        self.assertEqual(self.commands(), [])

    def test_remote_attach_failures_name_their_host_and_attach_nothing(self) -> None:
        for run, error in (
                (subprocess.CompletedProcess(["ssh"], 1, "", "herdr_sessions: no tab named writing\nin agents\n"),
                 "herdr_sessions: mac: herdr_sessions: no tab named writing in agents\n"),
                (subprocess.CompletedProcess(["ssh"], 255, "", ""), "herdr_sessions: mac: no error message\n"),
                (subprocess.TimeoutExpired(["ssh"], 15), "herdr_sessions: mac: attach failed: Command '['ssh']' "
                 + "timed out after 15 seconds\n")):
            with self.subTest(error=error), patch.object(os, "execvp") as execute, \
                    patch.object(subprocess, "run", side_effect=[run]):
                self.assertEqual(self.call("attach-remote", "mac", "writing"), (1, "", error))
            execute.assert_not_called()
        self.assertEqual(self.commands(), [])

    def test_remote_attach_exec_failure_names_its_host(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(["ssh"], 0, "agents\n", "")), \
                patch.object(os, "execvp", side_effect=OSError("attach failed")):
            self.assertEqual(self.call("attach-remote", "mac", "writing"),
                             (1, "", "herdr_sessions: mac: attach failed: attach failed\n"))
        self.assertEqual(self.commands(), [])

    def test_remote_commands_reject_option_targets_without_executing_them(self) -> None:
        with patch.object(subprocess, "run") as remote, patch.object(os, "execvp") as execute:
            for target in ("", "-oProxyCommand=anything", "two hosts"):
                for arguments in (("list", "--on", target), ("attach-remote", target, "writing")):
                    with self.subTest(arguments=arguments):
                        code, output, error = self.call(*arguments)
                        self.assertEqual((code, output), (2, ""))
                        self.assertIn("remote host must be an SSH target", error)
        remote.assert_not_called()
        execute.assert_not_called()

    def test_help_explains_remote_list_and_attach(self) -> None:
        code, output, error = self.call("--help")
        self.assertEqual((code, error), (0, ""))
        self.assertIn("list --on <host>: list herdr sessions on that host over SSH", output)
        self.assertIn("attach-remote <host> <name>: attach to that host's herdr session", output)
        self.assertEqual(self.commands(), [])

    def test_session_without_a_registry_has_an_empty_claude_name(self) -> None:
        self.respond(["agents"], tabs=("empty",))
        self.records.rmdir()
        self.assertEqual(self.call("list"), (0, "empty\tstandalone\t\n", ""))

    def focused(self, tab: str) -> list[list[str]]:
        """The commands that find tab `tab` of the shared session and show it."""
        return [["--session", "agents", "pane", "list"], ["--session", "agents", "workspace", "focus", "w1"],
                ["--session", "agents", "tab", "focus", tab]]

    def test_attach_focuses_the_named_tab_then_attaches_the_shared_session(self) -> None:
        self.respond(["agents"], tabs=("other", "target"))
        with patch.object(os, "execvp", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit) as replaced:
                _ = self.call("attach", "target")
        self.assertEqual(replaced.exception.code, 0)
        binary = os.environ["HERDR_BIN"]
        execute.assert_called_once_with(binary, [binary, "session", "attach", "agents"])
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"],
                                           ["--session", "agents", "pane", "list"],
                                           ["--session", "agents", "pane", "list"], *self.focused("w1:t2")])

    def test_focus_shows_the_named_tab_and_prints_the_shared_session(self) -> None:
        self.respond(["agents"], tabs=("target",))
        with patch.object(os, "execvp") as execute:
            self.assertEqual(self.call("focus", "target"), (0, "agents\n", ""))
        execute.assert_not_called()
        self.assertEqual(self.commands()[-3:], self.focused("w1:t1"))

    def test_attach_and_focus_refuse_a_missing_tab_and_list_the_open_ones(self) -> None:
        self.respond(["agents"], tabs=("two", None, "one"))
        for verb in ("attach", "focus"):
            with self.subTest(verb=verb), patch.object(os, "execvp") as execute:
                code, stdout, stderr = self.call(verb, "missing")
            self.assertEqual((code, stdout), (1, ""))
            self.assertEqual(stderr, "herdr_sessions: no tab named missing in herdr session agents; "
                             + "open tabs: one, two, w1:p2\n")
            execute.assert_not_called()
        self.assertFalse(any(command[2:3] in (["workspace"], ["tab"]) for command in self.commands()))

    def test_attach_finds_bare_binary_on_path(self) -> None:
        self.respond(["agents"], tabs=("target",))
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
            ["--session", "agents", "pane", "list"], ["--session", "agents", "pane", "list"],
            *self.focused("w1:t1"),
            ["session", "attach", "agents"],
        ])

    def test_attach_and_focus_refuse_while_the_shared_session_is_not_running(self) -> None:
        self.respond(["other"])
        for verb in ("attach", "focus"):
            with self.subTest(verb=verb):
                self.assertEqual(self.call(verb, "missing"),
                                 (1, "", "herdr_sessions: herdr session agents is not running\n"))

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
        self.respond(["agents"], tabs=("target",))
        with patch.object(os, "execvp", side_effect=OSError("attach failed")):
            self.assertEqual(self.call("attach", "target"), (3, "", "herdr_sessions: attach failed\n"))

    def test_ctrl_c_during_list_and_attach_returns_130_with_one_line(self) -> None:
        self.respond(["agents"], tabs=("target",))
        for owner, operation, arguments in (
                (herdr, "live_sessions", ("list",)),
                (herdr_sessions, "claude_names", ("list",)),
                (herdr_sessions, "unit_roles", ("list",)),
                (herdr_sessions, "registered_showrunners", ("list",)),
                (os, "execvp", ("attach", "target"))):
            with self.subTest(operation=operation), patch.object(owner, operation, side_effect=KeyboardInterrupt):
                self.assertEqual(self.call(*arguments), (130, "", "herdr_sessions: stopped\n"))

    def test_usage_errors_do_not_call_herdr(self) -> None:
        for arguments in ((), ("unknown",), ("list", "extra"), ("attach",), ("attach", ""),
                          ("sort-tabs", "extra"),
                          ("attach", "one", "extra"), ("focus",), ("focus", ""), ("focus", "one", "extra"),
                          ("list", "--on"),
                          ("list", "--on", "mac", "extra"), ("attach-remote", "mac"),
                          ("attach-remote", "mac", ""), ("attach-remote", "mac", "one", "extra")):
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
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
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
            "workspaces": [], "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": [], "panes": [],
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

    def destination(self, environment: dict[str, str] | None = None, *, tabs: tuple[str, ...] = ()) -> None:
        """The shared session `agents` starts with `environment` and holds tabs `tabs`; a new tab opens as pane w1:p1."""
        name = "agents"
        self.responses[f"--session {name} status server"] = [
            {"stdout": "status: stopped", "code": 0}, {"stdout": "status: running", "code": 0},
        ]
        before = [{"pane_id": f"w9:p{index}", "label": label, "workspace_id": "w9", "tab_id": f"w9:t{index}"}
                  for index, label in enumerate(tabs, 1)]
        opened = {"pane_id": "w1:p1", "label": "opened", "workspace_id": "w1", "tab_id": "w1:t1"}
        self.responses[f"--session {name} pane list"] = [
            {"stdout": json.dumps({"result": {"panes": panes}}), "code": 0} for panes in (before, [*before, opened])]
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

    def tab_opened(self, title: str) -> None:
        """One tab named `title` opened in the shared session's ad-hoc workspace, and nothing started another session."""
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertEqual(workspace[:6], ["--session", "agents", "workspace", "create", "--label", "ad-hoc"])
        self.assertIn(["--session", "agents", "tab", "rename", "w1:t1", title], self.commands())
        self.assertIn(["--session", "agents", "pane", "rename", "w1:p1", title], self.commands())
        self.assertEqual({command[1] for command in self.commands() if command[0] == "--session"}, {"agents"})

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

    def test_ctrl_c_during_launch_returns_130_and_releases_the_barrier(self) -> None:
        for verb, options in (("start", ["--name", "solo"]),
                              ("resume", ["solo", "--session-id", "recorded"])):
            for operation in ("start_server", "open_tab", "start_agent"):
                self.log.unlink(missing_ok=True)
                (self.proc / "net/unix").unlink(missing_ok=True)
                self.destination()
                barrier = MagicMock()
                release = Mock(return_value=False)
                barrier.__exit__ = release
                with (self.subTest(verb=verb, operation=operation),
                      patch.object(launch_permission, "launch_barrier", return_value=barrier),
                      patch.object(herdr, operation, side_effect=KeyboardInterrupt)):
                    self.assertEqual(self.call(verb, *options, "--cwd", str(self.root)),
                                     (130, "herdr_sessions: stopped\n"))
                release.assert_called_once_with(None, None, None)
                self.assertEqual(self.attachments, [])

    def test_ctrl_c_before_launch_barrier_returns_130_without_starting_commands(self) -> None:
        with patch.object(launch_permission, "launch_barrier", side_effect=KeyboardInterrupt):
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)),
                             (130, "herdr_sessions: stopped\n"))
        self.assertEqual(self.commands(), [])

    def test_launch_server_failure_prints_only_its_message(self) -> None:
        message = f"herdr server for agents did not start; see {self.root}/herdr-server.log"
        with patch.object(herdr, "start_server", side_effect=herdr.HerdrFailure(message)):
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)),
                             (1, f"herdr_sessions: {message}\n"))
        self.assertEqual(self.attachments, [])
        self.assertEqual(self.commands(), [])

    def test_start_after_cutover_uses_showrunners_account_for_pane_and_admission(self) -> None:
        directory = self.numbered_account()
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"showrunners": "claude4"}))
        _ = (Path.home() / ".claude.json").write_text("{}")
        self.destination()
        with patch.object(launch_permission, "launch_permission", return_value=launch_permission.AllowedByShutdownState()) as permission:
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)), (0, ""))
        environment = cast(dict[str, str], permission.call_args.args[0])
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(directory))
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertIn(f"CLAUDE_CODE_REMOTE_MEMORY_DIR={Path.home() / '.claude'}", workspace)
        self.assertEqual(len(self.agent_starts()), 1)

    def test_mac_start_and_resume_refuse_non_desktop_before_any_server_or_agent(self) -> None:
        for verb, arguments in (
            ("start", ["--name", "solo", "--cwd", str(self.root)]),
            ("resume", ["solo", "--session-id", "saved-id", "--cwd", str(self.root)]),
        ):
            for kind in (herdr.NonDesktopSession(), herdr.SessionKindUnreadable("query failed")):
                with (self.subTest(verb=verb, kind=kind),
                      patch.object(sys, "platform", "darwin"), patch.object(herdr, "_PLATFORM", "darwin"),
                      patch.object(herdr, "security_session", return_value=kind) as query,
                      patch.object(herdr, "start_server") as start,
                      patch.object(herdr, "start_agent") as agent,
                      redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(self.call(verb, *arguments), (1, ""))
                self.assertEqual(output.getvalue().splitlines(), [herdr.MAC_DESKTOP_TERMINAL_REQUIRED])
                query.assert_called_once_with()
                start.assert_not_called()
                agent.assert_not_called()
                self.assertEqual(self.attachments, [])

    def test_start_opens_a_promptless_default_account_tab_in_the_shared_session_and_attaches(self) -> None:
        self.destination(tabs=("other",))
        self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root), "--", "--model", "opus"), (0, ""))
        start, = self.agent_starts()
        self.assertEqual(start[:5], ["--session", "agents", "agent", "start", "solo"])
        self.assertEqual(start[start.index("--") + 1:], ["--model", "opus", "-n", "solo"])
        self.tab_opened("solo")
        self.assertEqual(self.commands()[-2:], [["--session", "agents", "workspace", "focus", "w1"],
                                                ["--session", "agents", "tab", "focus", "w1:t1"]])
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn("HERDR_AGENT_PANE=1", workspace)
        self.assertFalse(any("CLAUDE_CONFIG_DIR=" in value for value in workspace))
        self.assertFalse(any(command[2:4] in (["agent", "prompt"], ["pane", "send-keys"]) for command in self.commands()))
        binary = os.environ["HERDR_BIN"]
        self.assertEqual(self.attachments, [(binary, [binary, "session", "attach", "agents"])])

    def test_start_inherits_named_account_for_pane_and_new_work_admission(self) -> None:
        directory = self.numbered_account()
        self.destination()
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

    def test_generated_name_skips_the_labels_of_open_tabs(self) -> None:
        first = herdr.generated_session_name(self.root.name, "1")
        second = herdr.generated_session_name(self.root.name, "2")
        self.destination(tabs=(first,))
        self.assertEqual(self.call("start", "--cwd", str(self.root)), (0, ""))
        self.assertEqual([command[4] for command in self.agent_starts()], [second])
        self.tab_opened(second)

    def test_explicit_open_or_invalid_name_starts_no_agent(self) -> None:
        self.destination(tabs=("solo",))
        code, error = self.call("start", "--name", "solo", "--cwd", str(self.root))
        self.assertEqual((code, error), (1, "herdr_sessions: a tab named solo is already open in herdr session agents\n"))
        self.assertFalse(any(command[2:4] == ["workspace", "create"] for command in self.commands()))
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
        self.destination()
        transcript = directory / "projects/fixture/recorded-id.jsonl"
        self.responses["--session agents agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"agent": {"agent_status": "idle", "interactive_ready": True}}}))
        self.responses["--session agents pane read w1:p1 --source visible --format ansi"] = CommandResponse(
            code=0, stdout="────────\n❯ \n────────")
        # This fixture turn is written by the fake command, after launch, not before it.
        original_prompt = herdr.Herdr.prompt

        def submitted(client: herdr.Herdr, pane: str, text: str) -> None:
            original_prompt(client, pane, text)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            _ = transcript.write_text(json.dumps({"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": "user", "content": text}}) + "\n")

        self.save()
        _ = self.enterContext(patch.object(herdr.Herdr, "prompt", submitted))
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
        self.assertEqual(prompts, [["--session", "agents", "agent", "prompt", "w1:p1", "first line\nsecond line\n"]])
        self.tab_opened("solo")
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertIn(f"CLAUDE_CODE_REMOTE_MEMORY_DIR={self.root / 'home/.claude'}", workspace)
        self.assertIn("HERDR_AGENT_PANE=1", workspace)

    def test_resume_with_unsubmitted_prompt_reports_failure_without_attaching(self) -> None:
        directory = self.numbered_account()
        prompt = self.root / "prompt"
        _ = prompt.write_text("first prompt")
        self.destination()
        for outcome in (herdr.PromptNotSubmitted("fixture absent user turn"), herdr.PromptSubmissionNotRequired()):
            with (self.subTest(outcome=outcome), patch.object(herdr, "start_server"),
                  patch.object(herdr, "start_agent", return_value=outcome) as start):
                code, error = self.call("resume", "solo", "--session-id", "recorded-id", "--cwd", str(self.root),
                    "--config-dir", str(directory), "--prompt-file", str(prompt), "--attach")
            self.assertEqual(code, 1)
            self.assertIn("solo: opening prompt not submitted", error)
            self.assertEqual(start.call_args.kwargs["opening"], herdr.OpeningPrompt("first prompt",
                herdr.SessionTranscript(directory, "recorded-id")))
        self.assertEqual(self.attachments, [])

    def test_resume_without_prompt_or_attach_leaves_user_input_untouched(self) -> None:
        self.destination()
        with patch.object(os, "execvp") as execute:
            self.assertEqual(self.call("resume", "solo", "--session-id", "recorded", "--cwd", str(self.root)), (0, ""))
        execute.assert_not_called()
        self.assertEqual(len(self.agent_starts()), 1)
        self.assertFalse(any(command[2:4] == ["agent", "prompt"] for command in self.commands()))

    def test_untrusted_folder_refuses_with_account_trust_command_before_server_start(self) -> None:
        folder = self.root / "folder with 'quotes' and spaces"
        folder.mkdir()
        directory = self.numbered_account()
        claude_arguments = ["--model", "opus", "--settings", '{"text":"keep $(literal) and quotes"}']
        for verb in ("start", "resume"):
            if verb == "start":
                trust_file = self.root / "home/.claude.json"
                trust_command = (f"cd {shlex.quote(str(folder))} && env -u CLAUDE_CONFIG_DIR "
                                 + "-u CLAUDE_CODE_REMOTE_MEMORY_DIR -u CLAUDE_CODE_OAUTH_TOKEN claude")
                options = ["--name", "solo"]
                rerun_options = options
            else:
                trust_file = directory / ".claude.json"
                trust_command = accounts.trust_command(folder, accounts.NamedAccount("claude4", directory))
                options = ["solo", "--session-id", "recorded", "--config-dir", str(directory.relative_to(self.root)),
                           "--restore", "--prompt-file", "opening note.txt", "--attach"]
                rerun_options = ["solo"]
            original = json.dumps({"projects": {}, "oauthAccount": {"emailAddress": (
                "legacy@example.invalid" if verb == "start" else "numbered@example.invalid")}})
            _ = trust_file.write_text(original)
            with self.subTest(verb=verb), chdir(self.root):
                code, error = self.call(verb, *options, "--cwd", folder.name, "--", *claude_arguments)
            self.assertEqual(code, 1)
            self.assertEqual(error.splitlines()[:2], [f"herdr_sessions: Claude has not trusted {folder}.",
                "Paste this, accept Claude's trust prompt, then exit Claude; the session then starts:"])
            command = error.splitlines()[2]
            self.assertTrue(command.startswith(trust_command + " && "), command)
            expected = [sys.executable, str(Path(herdr_sessions.__file__).resolve()), verb,
                        *rerun_options, "--cwd", str(folder)]
            if verb == "resume":
                expected.extend(["--session-id", "recorded", "--config-dir", str(directory), "--restore",
                                 "--prompt-file", str(self.root / "opening note.txt"), "--attach"])
            expected.extend(["--", *claude_arguments])
            self.assertEqual(shlex.split(command.removeprefix(trust_command + " && ")), expected)
            self.assertEqual(trust_file.read_text(), original)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.attachments, [])

    def test_unreadable_trust_file_keeps_its_repair_message_without_a_launch_rerun(self) -> None:
        _ = (self.root / "home/.claude.json").write_text('{"oauthAccount":{"emailAddress":"legacy@example.invalid"}}')
        with patch.object(herdr, "claude_trust", return_value=herdr.UnreadableTrustFile(
                self.root, self.root / "home/.claude.json", "PermissionError")):
            code, error = self.call("start", "--name", "solo", "--cwd", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("PermissionError", error)
        self.assertIn(". Run: cd ", error)
        self.assertNotIn("Paste this", error)
        self.assertNotIn(str(Path(herdr_sessions.__file__).resolve()), error)
        self.assertEqual(self.commands(), [])

    def test_inherited_default_account_variables_refuse_before_agent_start(self) -> None:
        for variable in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            (self.proc / "net/unix").unlink(missing_ok=True)
            self.destination({variable: "private"})
            code, error = self.call("start", "--name", "solo", "--cwd", str(self.root))
            self.assertEqual((code, error), (1, f"herdr_sessions: herdr server agents carries {variable} from where "
                             + "it started, so a new tab would inherit it; start that server again from a shell without it\n"))
        self.assertEqual(self.agent_starts(), [])
        self.assertFalse(any(command[2:4] == ["workspace", "create"] for command in self.commands()))

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
        self.destination()
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
