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

import fake_tmux
import session_pane
import herdr
from fake_tmux import FakeSession
from herdr import AgentBlocked, HerdrPlace, HerdrRefusal, SessionListResult
from fake_herdr import CommandResponse, herdr_listener
from sessions import SessionRecord
from unit_lookup import TmuxPlace

SCRIPT = Path(__file__).with_name("session_pane.py")


def tmux_adapter(root: Path) -> str:
    """Adapt capture options to the existing shared fake, recording the original argv."""
    path = root / "tmux"
    _ = path.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                        + f"sys.path.insert(0, {str(SCRIPT.parent)!r})\nimport fake_tmux\n"
                        + "args = sys.argv[1:]\n"
                        + "with open(os.environ['FAKE_PANE_LOG'], 'a') as log:\n"
                        + "    log.write(json.dumps(args) + '\\n')\n"
                        + "if args[0] == 'capture-pane':\n"
                        + "    args = ['capture-pane', '-p', '-e', '-t', args[args.index('-t') + 1]]\n"
                        + "raise SystemExit(fake_tmux.main(args))\n")
    path.chmod(0o755)
    return str(path)


class SessionPaneTests(unittest.TestCase):
    root: Path = Path()
    records: Path = Path()
    state: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.records = self.root / "sessions"
        self.records.mkdir()
        self.state = self.root / "tmux.json"
        self.environment = {
            **os.environ, "UNIT_LOOKUP_TMUX": tmux_adapter(self.root),
            "FAKE_TMUX_STATE": str(self.state), "FAKE_PANE_LOG": str(self.root / "tmux.log"),
            "HERDR_BIN": str(SCRIPT.parents[1] / "herdr/fake_herdr.py"),
            "FAKE_HERDR_LOG": str(self.root / "herdr.log"),
            "FAKE_HERDR_RESPONSES": str(self.root / "responses.json"),
            "HERDR_PROC": str(self.root / "proc"), "NOTIFIER_SESSIONS_DIR": str(self.records),
        }
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        fake_tmux.write(self.state, {"$1": FakeSession(label="tmux-unit", panes=["%41"],
                        env={"SHOWRUNNER_UNIT": "show", "SHOWRUNNER_UNIT_ID": "tmux-unit"},
                        screens={"%41": "tmux screen\n"})})
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

    def record(self, environment: dict[str, str], parent: int = 0, *,
               tmux: str = "stale-label:@1.%99") -> None:
        self.process(os.getpid(), parent, ["claude"], environment)
        path = self.root / "session.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        record: SessionRecord = {
            "pid": os.getpid(), "sessionId": "conversation", "name": "fixture", "updatedAt": 1,
            "messagingSocketPath": str(path), "tmux": tmux, "formerNames": []}
        _ = (self.records / "session.json").write_text(json.dumps(record))

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=self.environment,
                              capture_output=True, text=True, check=False)

    def log(self, host: str) -> list[list[str]]:
        path = self.root / f"{host}.log"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def test_tmux_read_returns_only_the_requested_tail_without_screen_padding(self) -> None:
        for capture, expected, ansi in (
            ("oldest\nolder\nfirst\nsecond\nthird\n\n \t\n", "first\nsecond\nthird\n", False),
            ("oldest\nolder\n\x1b[31mfirst\x1b[0m\n\nthird  \n\x1b[0m \t\x1b[2m\n\x1b[0m\n",
             "\x1b[31mfirst\x1b[0m\n\nthird  \n", True),
            ("short", "short\n", False),
            ("\n \t\n", "", False),
            ("\x1b[0m \t\x1b[2m\n\x1b[0m\n", "", True),
        ):
            with self.subTest(capture=capture, ansi=ansi):
                state = fake_tmux.read(self.state)
                state["$1"]["screens"] = {"%41": capture}
                fake_tmux.write(self.state, state)
                options = ("--ansi",) if ansi else ()
                result = self.run_cli("read", "unit", "show", "tmux-unit", "--lines", "3", *options)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
        self.assertIn(["capture-pane", "-p", "-S", "-3", "-t", "%41"], self.log("tmux"))
        self.assertIn(["capture-pane", "-p", "-e", "-S", "-3", "-t", "%41"], self.log("tmux"))

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

    def test_every_verb_on_both_unit_hosts(self) -> None:
        for unit, where, screen in (("tmux-unit", "tmux:%41", "tmux screen"),
                                    ("herdr-unit", "herdr:native:w1:p1", "native screen")):
            with self.subTest(unit=unit):
                target = ("unit", "show", unit)
                found = self.run_cli("where", *target)
                self.assertEqual((found.returncode, found.stdout), (0, where + "\n"), found.stderr)
                read = self.run_cli("read", *target, "--lines", "400", "--joined", "--ansi")
                self.assertEqual(read.returncode, 0, read.stderr)
                self.assertEqual(read.stdout.strip(), screen)
                for verb, arguments in (("type", ("hello",)), ("keys", ("C-c", "Enter"))):
                    sent = self.run_cli(verb, *target, *arguments)
                    self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertIn(["capture-pane", "-p", "-J", "-e", "-S", "-400", "-t", "%41"], self.log("tmux"))
        self.assertEqual(fake_tmux.read(self.state)["$1"].get("sent", []),
                         [["%41", "-l", "hello"], ["%41", "Enter"], ["%41", "C-c", "Enter"]])
        self.assertIn(["--session", "native", "pane", "read", "w1:p1", "--source", "recent-unwrapped",
                       "--lines", "400", "--format", "ansi"], self.log("herdr"))
        self.assertIn(["--session", "native", "pane", "send-keys", "w1:p1", "C-c", "Enter"], self.log("herdr"))

    def test_session_target_reads_pid_environment_for_each_verb(self) -> None:
        self.record({"TMUX_PANE": "%41", "HERDR_PANE_ID": "w1:p1"}, 991000)
        for native in (False, True):
            with self.subTest(native=native):
                environment = {"HERDR_PANE_ID": "w1:p1"} if native else {"TMUX_PANE": "%41"}
                self.process(os.getpid(), 991000, ["claude"], environment)
                for verb, args in (("where", ()), ("read", ()), ("type", ("hello",)), ("keys", ("Enter",))):
                    result = self.run_cli(verb, "session", "conversation", *args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if verb == "where":
                        self.assertEqual(result.stdout.strip(), "herdr:native:w1:p1" if native else "tmux:%41")
        self.assertIn(["capture-pane", "-p", "-S", "-60", "-t", "%41"], self.log("tmux"))
        self.assertIn(["--session", "native", "pane", "read", "w1:p1", "--source", "recent",
                       "--lines", "60"], self.log("herdr"))

    def test_blocked_type_exits_four_and_sends_no_followup_key(self) -> None:
        self.responses(blocked=True)
        result = self.run_cli("type", "unit", "show", "herdr-unit", "hello")
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (4, "unit show herdr-unit: blocked at a prompt; nothing typed"))
        actions = [row for row in self.log("herdr") if row[2:4] != ["session", "list"]]
        self.assertEqual(actions, [["--session", "native", "agent", "prompt", "w1:p1", "hello"]])
        with self.assertRaises(AgentBlocked):
            session_pane.type_place(HerdrPlace("native", "w1:p1"), "hello")

    def test_darwin_uses_the_live_record_pane_despite_a_stale_session_label(self) -> None:
        self.record({})
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(subprocess, "run") as read:
            self.assertEqual(session_pane.session_place("conversation"), TmuxPlace("%99"))
        read.assert_not_called()

    def test_record_place_keeps_the_given_pid_while_session_lookup_chooses_the_newest(self) -> None:
        older = SessionRecord(pid=991010, sessionId="conversation", name="older", updatedAt=1,
            messagingSocketPath="", tmux="older:@1.%41", formerNames=[])
        newer = SessionRecord(pid=991011, sessionId="conversation", name="newer", updatedAt=2,
            messagingSocketPath="", tmux="newer:@2.%99", formerNames=[])
        self.process(991010, 0, ["claude"], {"TMUX_PANE": "%41"})
        self.process(991011, 0, ["claude"], {"TMUX_PANE": "%99"})
        for saved in (older, newer):
            _ = (self.records / f"{saved['pid']}.json").write_text(json.dumps(saved))
        for platform in ("linux", "darwin"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(session_pane, "live_session", return_value=True):
                self.assertEqual(session_pane.record_place(older), TmuxPlace("%41"))
                self.assertEqual(session_pane.record_place(newer), TmuxPlace("%99"))
                self.assertEqual(session_pane.session_place("conversation"), TmuxPlace("%99"))

    def test_darwin_argument_tokens_cannot_supply_a_tmux_pane(self) -> None:
        self.record({}, tmux="")
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
                subprocess, "run", return_value=subprocess.CompletedProcess(
                    ["ps"], 0, "claude --prompt TMUX_PANE=%41 HOME=/fixture", "")) as read, \
                mock.patch.object(herdr, "herdr_place_of", return_value=herdr.NotInHerdr()):
            with self.assertRaises(session_pane.NoSuchTarget):
                _ = session_pane.session_place("conversation")
        read.assert_not_called()

    def test_usage_missing_targets_and_unreadable_hosts_have_distinct_exits(self) -> None:
        for args in ((), ("keys", "unit", "show", "tmux-unit"),
                     ("read", "unit", "show", "tmux-unit", "--lines", "0"),
                     ("type", "unit", "show", "tmux-unit", "one", "two")):
            self.assertEqual(self.run_cli(*args).returncode, 2)
        self.assertEqual(self.run_cli("where", "unit", "show", "missing").returncode, 1)
        self.assertEqual(self.run_cli("where", "session", "missing").returncode, 1)
        self.responses(failed=True)
        self.assertEqual(self.run_cli("read", "unit", "show", "herdr-unit").returncode, 3)
        with mock.patch.dict(os.environ, {"UNIT_LOOKUP_TMUX": str(self.root / "missing")}):
            with self.assertRaises(session_pane.TmuxFailure):
                _ = session_pane.read_place(TmuxPlace("%41"))
        _ = (self.records / "broken.json").write_text("{")
        self.assertEqual(self.run_cli("where", "session", "missing").returncode, 3)


if __name__ == "__main__":
    _ = unittest.main()
