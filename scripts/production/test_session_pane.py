"""Pane operations use only fake hosts, fake process ancestry and session records."""
from __future__ import annotations

from scripts.production.fake_unit_host import labeled as labeled_host

import json
import math
import os
import socket
import subprocess
import tempfile
import time
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from scripts.lib.script_envelope import script_prompt
from typing import cast, override
from unittest import mock


from scripts.lib.socket_test_support import short_socket_directory
from scripts.production import session_pane
from scripts.herdr import herdr
from scripts.message import sessions
from scripts.production import unit_lookup
from scripts.herdr.herdr import AgentBlocked, HerdrPlace, HerdrRefusal, SessionListResult
from scripts.herdr.fake_herdr import CommandResponse
from scripts.production import fake_unit_host
from scripts.message.sessions import SessionRecord
from scripts.production.staff_test_support import fixture_runtime_id, isolated_staff_file, unit_membership
from scripts.production.cli_fixture import invoke_cli
from scripts.message import sessions as runtime_sessions
from scripts.production import staff

SCRIPT = Path(__file__).with_name("session_pane.py")


class SessionPaneTests(unittest.TestCase):
    root: Path = Path()
    records: Path = Path()
    environment: dict[str, str] = {}

    @contextmanager
    def prompt_submission(self) -> Generator[mock.Mock]:
        run = herdr.Herdr.run
        submit = mock.Mock(return_value="")

        def transport(client: herdr.Herdr, *arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("agent", "prompt"):
                return cast(str, submit(*arguments))
            return run(client, *arguments, deadline=deadline)

        with mock.patch.object(herdr.Herdr, "run", new=transport):
            yield submit

    def test_source_record_failure_prevents_pane_input(self) -> None:
        from scripts.lib import script_prompts

        with (mock.patch.object(script_prompts, "record", side_effect=OSError("record failed")),
              self.prompt_submission() as submit):
            with self.assertRaisesRegex(OSError, "record failed"):
                session_pane.type_place(HerdrPlace("native", "w1:p1"), "/resume",
                                        target=unit_membership("show", "herdr").current_session)
        submit.assert_not_called()

    def test_queued_script_input_stays_non_typed_after_six_hours(self) -> None:
        from scripts.hooks import conversation_pause

        for text in ("Continue the review", "/resume"):
            with self.subTest(text=text), self.prompt_submission() as submit:
                session_pane.type_place(HerdrPlace("native", "w1:p1"), text,
                                        target=unit_membership("show", "herdr").current_session)
                sent = script_prompt(text)
                submit.assert_called_once_with("agent", "prompt", "w1:p1", sent)
                for path in (self.root / "conversation-pause" / "script-prompts").glob("*"):
                    if path.name != ".lock":
                        then = time.time() - 6 * 3600 - 60
                        os.utime(path, (then, then))
                with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p1"}):
                    source = conversation_pause.prompt_source(sent, lambda: frozenset(), lambda: ())
                self.assertIs(source, conversation_pause.PromptSource.SCHEDULED)

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
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        _ = unit_membership("show", "herdr")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        original_start = sessions.process_start
        original_running = sessions.running_session
        original_live = sessions.live_session

        def started(pid: int) -> str | None:
            return "fixture-start" if pid == 998042 else original_start(pid)

        def running(record: SessionRecord) -> bool:
            return record["pid"] == 998042 or original_running(record)

        def live(record: SessionRecord) -> bool:
            return record["pid"] == 998042 or original_live(record)

        for module in (sessions, runtime_sessions):
            _ = self.enterContext(mock.patch.object(module, "process_start", side_effect=started))
            _ = self.enterContext(mock.patch.object(module, "running_session", side_effect=running))
        _ = self.enterContext(mock.patch.object(unit_lookup, "live_session", side_effect=live))
        _ = self.enterContext(mock.patch.object(session_pane, "live_session", side_effect=live))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "composer", return_value=herdr.ComposerInput("")))
        self.responses()

    def responses(self, *, blocked: bool = False, failed: bool = False) -> None:
        sessions: SessionListResult = {"sessions": [{"name": "native", "running": True, "default": False,
                    "session_dir": "/fixture", "socket_path": "/fixture/socket"}]}
        refusal: HerdrRefusal = {"error": {"code": "agent_blocked", "message": "permission prompt"}}
        responses: dict[str, CommandResponse] = {
            "--session default session list --json": {"stdout": json.dumps(sessions), "code": 0},
            "--session native agent prompt w1:p1 " + script_prompt("hello"): {
                "stdout": json.dumps(refusal) if blocked else "", "code": 1 if blocked else 0},
            "default": {"stdout": "native screen\n", "code": 1 if failed else 0},
        }
        _ = (self.root / "responses.json").write_text(json.dumps(responses))
        fake_unit_host.write(self.environment, [
            labeled_host("native", "w1:p1", "herdr", production_name="show", screen="native screen\n"),
            fake_unit_host.HostedUnit("native", "w1:p2", {}, "native screen\n")])
        owner = unit_membership("show", "herdr")
        fake_unit_host.process(self.environment, 998042, 991001, ["claude"], {
            "HERDR_SESSION": "native", "HERDR_PANE_ID": "w1:p1",
            ('' + 'SHOWRUNNER_' + 'UNIT'): "show", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "herdr"})
        _ = (self.records / "998042.json").write_text(json.dumps({
            "pid": 998042, "sessionId": owner.current_session, "name": "fixture", "updatedAt": 1,
            "procStart": "fixture-start", "messagingSocketPath": "", "tmux": "", "formerNames": []}))
        responses = cast(dict[str, CommandResponse], json.loads((self.root / "responses.json").read_text()))
        responses["--session native pane process-info --pane w1:p1"] = {
            "code": 0, "stdout": json.dumps({"result": {"process_info": {
                "shell_pid": 991001, "foreground_process_group_id": 998042,
                "foreground_processes": [{"pid": 998042, "name": "claude", "argv": ["claude"]}]}}})}
        _ = (self.root / "responses.json").write_text(json.dumps(responses))
        if failed:
            responses = cast(dict[str, CommandResponse], json.loads((self.root / "responses.json").read_text()))
            for command, response in responses.items():
                if command.startswith("--session native pane read "):
                    response["code"] = 1
            _ = (self.root / "responses.json").write_text(json.dumps(responses))

    def process(self, pid: int, parent: int, argv: list[str], environment: dict[str, str]) -> None:
        fake_unit_host.process(self.environment, pid, parent, argv, environment, cwd=self.root)

    def record(self, environment: dict[str, str], parent: int = 0) -> SessionRecord:
        self.process(998042, parent, ["claude"], environment)
        path = Path(self.enterContext(short_socket_directory())) / "session.sock"
        held = socket.socket(socket.AF_UNIX)
        held.bind(str(path))
        self.addCleanup(held.close)
        record: SessionRecord = {
            "pid": 998042, "procStart": "fixture-start", "sessionId": fixture_runtime_id("conversation"), "name": "fixture", "updatedAt": 1,
            "messagingSocketPath": str(path), "tmux": "", "formerNames": []}
        _ = (self.records / "998042.json").write_text(json.dumps(record))
        return record

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(session_pane, lambda: session_pane.main(list(arguments)),
                          list(arguments), self.environment, self.root)

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
                    result = self.run_cli("read", "unit", "show", "herdr", "--lines", "3", *options)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, output)
                    self.assertEqual(self.log("herdr")[-1], command)

    def test_pane_text_and_status_forward_the_same_deadline_to_execution(self) -> None:
        place = HerdrPlace("native", "w1:p1")
        reply = subprocess.CompletedProcess(["fixture"], 0,
            '{"result":{"agents":[{"pane_id":"w1:p1","agent":"claude","agent_status":"blocked"}]}}', "")
        with (mock.patch.object(time, "monotonic", return_value=100.0),
              mock.patch("scripts.herdr.herdr.run_program", return_value=reply) as program):
            for deadline, expected in ((100.5, 0.5), (float("inf"), 10.0)):
                with self.subTest(deadline=deadline):
                    self.assertEqual(session_pane.read_place(place, deadline=deadline), reply.stdout)
                    self.assertEqual(cast(float, program.call_args.kwargs["timeout"]), expected)
                    self.assertEqual(session_pane.agent_status(place, deadline=deadline), "blocked")
                    self.assertEqual(cast(float, program.call_args.kwargs["timeout"]), expected)
            with self.subTest(deadline=99.0):
                program.reset_mock()
                with self.assertRaises(herdr.HerdrFailure):
                    _ = session_pane.read_place(place, deadline=99.0)
                with self.assertRaises(herdr.HerdrFailure):
                    _ = session_pane.agent_status(place, deadline=99.0)
                program.assert_not_called()

    def test_every_verb_targets_the_units_herdr_pane(self) -> None:
        target = ("unit", "show", "herdr")
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
        record = self.record({"HERDR_PANE_ID": "w1:p1"}, 991000)
        for verb, args in (("where", ()), ("read", ()), ("type", ("hello",)), ("keys", ("Enter",))):
            result = self.run_cli(verb, "session", record["sessionId"], *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            if verb == "where":
                self.assertEqual(result.stdout.strip(), "herdr:native:w1:p1")
        self.assertIn(["--session", "native", "pane", "read", "w1:p1", "--source", "recent",
                       "--lines", "60"], self.log("herdr"))

    def test_nested_tmux_session_cannot_receive_input_through_the_outer_herdr_pane(self) -> None:
        inherited = {"HERDR_PANE_ID": "w1:p1", ('' + 'SHOWRUNNER_' + 'UNIT'): "show",
                     ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "herdr"}
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
        result = self.run_cli("type", "unit", "show", "herdr", "hello")
        self.assertEqual((result.returncode, result.stderr.strip()),
                         (4, "unit show herdr: blocked at a prompt; nothing typed"))
        actions = [row for row in self.log("herdr") if row[2:4] not in (
            ["session", "list"], ["pane", "list"], ["pane", "process-info"])]
        self.assertEqual(actions, [["--session", "native", "agent", "prompt", "w1:p1", script_prompt("hello")]])
        with self.assertRaises(AgentBlocked):
            session_pane.type_place(HerdrPlace("native", "w1:p1"), "hello",
                                    target=unit_membership("show", "herdr").id)

    def test_replacement_session_receives_no_text_or_keys_for_the_selected_session(self) -> None:
        selected, replacement = "selected-session", "replacement-session"
        place = HerdrPlace("native", "w1:p1")
        client = herdr.Herdr(place.session)
        commands: list[tuple[str, ...]] = []

        def transport(*arguments: str, deadline: float = math.inf) -> str:
            del deadline
            commands.append(arguments)
            if arguments == ("pane", "list"):
                return json.dumps({"result": {"panes": [{
                    "pane_id": place.pane, "terminal_id": "selected-terminal", "workspace_id": "w1", "agent_session": {"value": replacement}}]}})
            if arguments == ("pane", "process-info", "--pane", place.pane):
                return json.dumps({"result": {"process_info": {
                    "shell_pid": 998041, "foreground_process_group_id": 998042,
                    "foreground_processes": [{"pid": 998042, "name": "claude", "argv": ["claude"]}]}}})
            return "{}"

        def native(pid: int, kind: str) -> herdr.NativeAgentProcess:
            self.assertEqual((pid, kind), (998042, "claude"))
            return herdr.NativeAgentProcess(herdr.RolloutThread(replacement), "replacement-generation", {})

        with (mock.patch.object(session_pane, "Herdr", return_value=client),
              mock.patch.object(client, "run", side_effect=transport),
              mock.patch.object(herdr, "native_process", side_effect=native)):
            for verb in ("type", "keys"):
                with self.subTest(verb=verb), self.assertRaisesRegex(
                        herdr.HerdrFailure, "does not run session selected-session"):
                    if verb == "type":
                        session_pane.type_place(place, "message for A", target=selected)
                    else:
                        session_pane.keys_place(place, "Enter", target=selected)
        self.assertEqual(commands, [
            ("pane", "list"), ("pane", "process-info", "--pane", place.pane),
            ("pane", "list"), ("pane", "process-info", "--pane", place.pane)])

    def test_a_staff_row_id_never_redirects_input_or_closure_to_its_replacement(self) -> None:
        row = unit_membership("show", "herdr")
        selected = row.id
        replacement = staff.native_id(fixture_runtime_id("replacement-conversation"))
        row = staff.update_current_session(row.current_session, replacement)
        self.assertEqual(row.id, selected)
        self.assertEqual(row.current_session, replacement)
        place = HerdrPlace("native", "w1:p1")
        client = herdr.Herdr(place.session)
        observed = str(replacement)
        commands: list[tuple[str, ...]] = []

        def transport(*arguments: str, deadline: float = math.inf) -> str:
            del deadline
            commands.append(arguments)
            if arguments == ("pane", "list"):
                return json.dumps({"result": {"panes": [{
                    "pane_id": place.pane, "terminal_id": "selected-terminal", "workspace_id": "w1", "agent_session": {"value": observed}}]}})
            if arguments == ("pane", "process-info", "--pane", place.pane):
                return json.dumps({"result": {"process_info": {
                    "shell_pid": 998041, "foreground_process_group_id": 998042,
                    "foreground_processes": [{"pid": 998042, "name": "claude", "argv": ["claude"]}]}}})
            return "{}"

        def native(pid: int, kind: str) -> herdr.NativeAgentProcess:
            self.assertEqual((pid, kind), (998042, "claude"))
            return herdr.NativeAgentProcess(herdr.RolloutThread(observed), "private-generation", {})

        def send(verb: str, target: str) -> None:
            if verb == "type":
                session_pane.type_place(place, "selected text", target=target)
            elif verb == "keys":
                session_pane.keys_place(place, "Enter", target=target)
            else:
                self.assertTrue(client.close_pane(place.pane, target=target))

        with (mock.patch.object(session_pane, "Herdr", return_value=client),
              mock.patch.object(client, "run", side_effect=transport),
              mock.patch.object(herdr, "native_process", side_effect=native)):
            for verb in ("type", "keys", "close"):
                with self.subTest(verb=verb), self.assertRaisesRegex(
                        herdr.HerdrFailure, f"does not run session {selected}") as refused:
                    send(verb, selected)
                self.assertEqual(refused.exception.code, "wrong_target")
            self.assertFalse(any(command[:2] in (
                ("agent", "prompt"), ("pane", "send-keys"), ("pane", "close")) for command in commands))
            for target in (str(replacement), str(selected)):
                observed = target
                commands.clear()
                for verb in ("type", "keys", "close"):
                    with self.subTest(target=target, verb=verb):
                        send(verb, target)
                self.assertIn(("agent", "prompt", place.pane, script_prompt("selected text")), commands)
                self.assertIn(("pane", "send-keys", place.pane, "Enter"), commands)
                self.assertIn(("pane", "close", place.pane), commands)


    def test_usage_missing_targets_and_unreadable_hosts_have_distinct_exits(self) -> None:
        for args in ((), ("keys", "unit", "show", "herdr"),
                     ("read", "unit", "show", "herdr", "--lines", "0"),
                     ("type", "unit", "show", "herdr", "one", "two")):
            self.assertEqual(self.run_cli(*args).returncode, 2)
        self.assertEqual(self.run_cli("where", "unit", "show", "missing").returncode, 1)
        self.assertEqual(self.run_cli("where", "session", "missing").returncode, 1)
        self.responses(failed=True)
        self.assertEqual(self.run_cli("read", "unit", "show", "herdr").returncode, 3)
        _ = (self.records / "broken.json").write_text("{")
        self.assertEqual(self.run_cli("where", "session", "missing").returncode, 3)


if __name__ == "__main__":
    _ = unittest.main()
