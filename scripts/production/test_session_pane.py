"""Pane operations use only fake hosts, fake process ancestry and session records."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "message"))

from scripts.lib.socket_test_support import short_socket_directory
import session_pane
from herdr import AgentBlocked, HerdrPlace, HerdrRefusal, SessionListResult
from fake_herdr import CommandResponse, herdr_listener
from sessions import SessionRecord

SCRIPT = Path(__file__).with_name("session_pane.py")


class SessionPaneTests(unittest.TestCase):
    root: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.environment = {
            **os.environ,
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
            "FAKE_HERDR_RESPONSES": str(self.root / "responses.json"),
            "HERDR_PROC": str(self.root / "proc"), "NOTIFIER_SESSIONS_DIR": str(self.records),
            "CONVERSATION_PAUSE_STATE_DIR": str(self.root / "conversation-pause"),
        }
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        self.responses()
        self.process(991000, 0, ["herdr", "--session", "native", "server"], {})
        herdr_listener(self.root / "proc", 991000, "native", self.environment)
        self.process(991001, 991000, ["zsh"], {"HERDR_PANE_ID": "w1:p1",
                     "SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "herdr-unit"})

    def responses(self, *, blocked: bool = False, failed: bool = False) -> None:
        sessions: SessionListResult = {"sessions": [{"name": "native", "running": True, "default": False,
                    "session_dir": "/fixture", "socket_path": "/fixture/socket"}]}
        refusal: HerdrRefusal = {"error": {"code": "agent_blocked", "message": "permission prompt"}}
        responses: dict[str, CommandResponse] = {
            "--session default session list --json": {"stdout": json.dumps(sessions), "code": 0},
            "--session native agent prompt w1:p1 hello": {
                "stdout": json.dumps(refusal) if blocked else "", "code": 1 if blocked else 0},
            "default": {"stdout": "native screen\n", "code": 1 if failed else 0},
        }
        _ = (self.root / "responses.json").write_text(json.dumps(responses))

    def process(self, pid: int, parent: int, argv: list[str], environment: dict[str, str]) -> None:
        path = self.root / "proc" / str(pid)
        path.mkdir(parents=True, exist_ok=True)
        _ = (path / "stat").write_text(f"{pid} (fixture) S {parent} 0 0")
        _ = (path / "cmdline").write_bytes("\0".join(argv).encode())
        _ = (path / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in environment.items()).encode())
        if not (path / "cwd").exists():
            (path / "cwd").symlink_to(self.root)

    def record(self, environment: dict[str, str], parent: int = 0) -> SessionRecord:
        self.process(os.getpid(), parent, ["claude"], environment)
        path = Path(self.enterContext(short_socket_directory())) / "session.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        record: SessionRecord = {
            "pid": os.getpid(), "sessionId": "conversation", "name": "fixture", "updatedAt": 1,
            "messagingSocketPath": str(path), "tmux": "", "formerNames": []}
        _ = (self.records / "session.json").write_text(json.dumps(record))
        return record

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, check=False)

    def log(self, host: str) -> list[list[str]]:
        path = self.root / f"{host}.log"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []


    def test_herdr_read_forwards_the_line_count_and_preserves_output_unchanged(self) -> None:
        path = self.root / "responses.json"
        responses = cast(dict[str, CommandResponse], json.loads(path.read_text()))
        for ansi in (False, True):
            for output in ("first\nsecond\nthird\n\n \t\n", "\x1b[31mshort\x1b[0m", ""):
                with self.subTest(ansi=ansi, output=output):
                    command = ["--session", "native", "pane", "read", "w1:p1", "--source", "recent",
                               "--lines", "3"]
                    if ansi:
                        command.extend(("--format", "ansi"))
                    responses[" ".join(command)] = {"stdout": output, "code": 0}
                    _ = path.write_text(json.dumps(responses))
                    options = ("--ansi",) if ansi else ()
                    result = self.run_cli("read", "unit", "show", "herdr-unit", "--lines", "3", *options)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, output)
                    self.assertEqual(self.log("herdr")[-1], command)

    def test_every_verb_targets_the_units_herdr_pane(self) -> None:
        target = ("unit", "show", "herdr-unit")
        found = self.run_cli("where", *target)
        self.assertEqual((found.returncode, found.stdout), (0, "herdr:native:w1:p1\n"), found.stderr)
        read = self.run_cli("read", *target, "--lines", "400", "--joined", "--ansi")
        self.assertEqual((read.returncode, read.stdout), (0, "native screen\n"), read.stderr)
        for verb, arguments in (("type", ("hello",)), ("keys", ("C-c", "Enter"))):
            sent = self.run_cli(verb, *target, *arguments)
            self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn(["--session", "native", "pane", "read", "w1:p1", "--source", "recent-unwrapped",
                       "--lines", "400", "--format", "ansi"], self.log("herdr"))
        self.assertIn(["--session", "native", "pane", "send-keys", "w1:p1", "C-c", "Enter"], self.log("herdr"))

    def test_session_target_resolves_the_live_process_for_each_verb(self) -> None:
        _ = self.record({"HERDR_PANE_ID": "w1:p1"}, 991000)
        for verb, args in (("where", ()), ("read", ()), ("type", ("hello",)), ("keys", ("Enter",))):
            result = self.run_cli(verb, "session", "conversation", *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            if verb == "where":
                self.assertEqual(result.stdout.strip(), "herdr:native:w1:p1")
        self.assertIn(["--session", "native", "pane", "read", "w1:p1", "--source", "recent",
                       "--lines", "60"], self.log("herdr"))

    def test_nested_tmux_session_cannot_receive_input_through_the_outer_herdr_pane(self) -> None:
        inherited = {"HERDR_PANE_ID": "w1:p1", "SHOWRUNNER_UNIT": "show",
                     "SHOWRUNNER_UNIT_ID": "herdr-unit"}
        self.process(991020, 991001, ["tmux: server"], inherited)
        nested = {**inherited, "TMUX_PANE": "%7"}
        self.process(991021, 991020, ["zsh"], nested)
        record = self.record(nested, 991021)
        with self.assertRaises(session_pane.NoSuchTarget):
            _ = session_pane.record_place(record)
        with self.assertRaises(session_pane.NoSuchTarget):
            _ = session_pane.session_place("conversation")
        for verb, argument in (("keys", "C-c"), ("type", "hello")):
            with self.subTest(verb=verb):
                result = self.run_cli(verb, "session", "conversation", argument)
                self.assertEqual((result.returncode, result.stderr.strip()),
                                 (1, "session conversation: no such target"))
        self.assertEqual(self.log("herdr"), [])

    def test_record_place_keeps_its_pid_while_session_lookup_chooses_the_newest(self) -> None:
        older = SessionRecord(pid=991010, sessionId="conversation", name="older", updatedAt=1,
            messagingSocketPath="", tmux="", formerNames=[])
        newer = SessionRecord(pid=991011, sessionId="conversation", name="newer", updatedAt=2,
            messagingSocketPath="", tmux="", formerNames=[])
        self.process(991010, 991000, ["claude"], {"HERDR_PANE_ID": "w1:p1"})
        self.process(991011, 991000, ["claude"], {"HERDR_PANE_ID": "w1:p2"})
        for saved in (older, newer):
            _ = (self.records / f"{saved['pid']}.json").write_text(json.dumps(saved))
        with mock.patch.object(session_pane, "live_session", return_value=True):
            self.assertEqual(session_pane.record_place(older), HerdrPlace("native", "w1:p1"))
            self.assertEqual(session_pane.record_place(newer), HerdrPlace("native", "w1:p2"))
            self.assertEqual(session_pane.session_place("conversation"), HerdrPlace("native", "w1:p2"))

    def test_blocked_type_exits_four_and_sends_no_followup_key(self) -> None:
        self.responses(blocked=True)
        result = self.run_cli("type", "unit", "show", "herdr-unit", "hello")
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (4, "unit show herdr-unit: blocked at a prompt; nothing typed"))
        actions = [row for row in self.log("herdr") if row[2:4] != ["session", "list"]]
        self.assertEqual(actions, [["--session", "native", "agent", "prompt", "w1:p1", "hello"]])
        with self.assertRaises(AgentBlocked):
            session_pane.type_place(HerdrPlace("native", "w1:p1"), "hello")


    def test_usage_missing_targets_and_unreadable_hosts_have_distinct_exits(self) -> None:
        for args in ((), ("keys", "unit", "show", "herdr-unit"),
                     ("read", "unit", "show", "herdr-unit", "--lines", "0"),
                     ("type", "unit", "show", "herdr-unit", "one", "two")):
            self.assertEqual(self.run_cli(*args).returncode, 2)
        self.assertEqual(self.run_cli("where", "unit", "show", "missing").returncode, 1)
        self.assertEqual(self.run_cli("where", "session", "missing").returncode, 1)
        self.responses(failed=True)
        self.assertEqual(self.run_cli("read", "unit", "show", "herdr-unit").returncode, 3)
        _ = (self.records / "broken.json").write_text("{")
        self.assertEqual(self.run_cli("where", "session", "missing").returncode, 3)


if __name__ == "__main__":
    _ = unittest.main()
