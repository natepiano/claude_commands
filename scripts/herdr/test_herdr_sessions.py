"""CLI tests using fake herdr, process ancestry, session records and showrunner state."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TypedDict, cast, override
from unittest.mock import patch

import herdr
import herdr_sessions
from fake_herdr import CommandResponse
from sessions import SessionRecord, UnreadableSessionRecord, read_session
import showrunners


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


if __name__ == "__main__":
    _ = unittest.main()
