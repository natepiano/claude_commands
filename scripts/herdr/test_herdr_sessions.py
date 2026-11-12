"""CLI tests using fake herdr, process ancestry, session records and showrunner state."""

from __future__ import annotations

from scripts.production.staff_test_support import fixture_member, fixture_runtime_id
from scripts.herdr import herdr_types

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from scripts.lib.script_envelope import script_prompt

from datetime import datetime, timezone
from typing import TypedDict, cast, override
from unittest.mock import patch

from scripts.herdr import herdr
from scripts.herdr import herdr_sessions
from scripts.message import sessions, codex_daemon
from scripts.lib.atomic_file import write_text_atomic
from scripts.herdr.fake_herdr import CommandResponse
from scripts.message.sessions import SessionRecord, UnreadableSessionRecord, read_session
from scripts.production import showrunners
from scripts.accounts import accounts
from scripts.accounts.home_test_support import scratch_accounts_config
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import staff, fake_unit_host
from scripts.message import sessions as runtime_sessions
from scripts.production.staff_test_support import isolated_staff_file, showrunner_relationship, unit_membership


class SessionListReply(TypedDict):
    ok: bool
    result: herdr.SessionListResult


def pane_process_reply(session_id: str = "") -> CommandResponse:
    """Publish an agent registry record when the pane scan first observes it."""
    pid = 998041 if session_id else 998040
    response = CommandResponse(code=0, stdout=json.dumps({"result": {"process_info": {
        "shell_pid": 998040, "foreground_process_group_id": pid,
        "foreground_processes": [{"pid": pid, "name": "claude" if session_id else "zsh",
                                  "argv": ["claude" if session_id else "zsh"]}]}}}))
    if session_id:
        fake_unit_host.process(os.environ, pid, 998040, ["claude"], {})
        registry = runtime_sessions.sessions_dir() / f"{pid}.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        response["write_files"] = {str(registry): json.dumps({
            "pid": pid, "procStart": "1", "name": "solo", "sessionId": session_id,
            "updatedAt": 1, "messagingSocketPath": ""})}
    return response


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
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
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
        _ = self.enterContext(patch.object(sessions, "running_session", side_effect=self.record_is_live))
        _ = self.enterContext(patch.object(runtime_sessions, "running_session", side_effect=self.record_is_live))
        _ = self.enterContext(patch.object(showrunners, "NOTIFIER_STATE_DIR", self.state))
        _ = self.enterContext(patch.object(showrunners, "socket_for", return_value="fake-socket"))
        _ = self.enterContext(patch.object(showrunners, "_session_name", side_effect=self.registered_name))
        _ = self.enterContext(patch.object(showrunners, "user_zone_name", return_value="UTC"))
        self.respond([])

    def test_unknown_codex_requires_a_pane_foreground_group_and_keeps_failed_stat_reads(self) -> None:
        self.server(99, "agents")
        self.process(100, 99, ["zsh"], {})
        for pid in range(101, 109):
            self.process(pid, 100, ["codex"], {"HERDR_PANE_ID": "w1:p1"})
            write_text_atomic(self.proc / str(pid) / "stat",
                f"{pid} (fixture) S 100 {pid} 100 1 {pid} " + "0 " * 14)
        write_text_atomic(self.proc / "101/stat", "101 (fixture) S 100 101 100 0 -1 " + "0 " * 14)
        write_text_atomic(self.proc / "102/stat", "102 (fixture) S 100 102 102 1 102 " + "0 " * 14)
        (self.proc / "104/stat").unlink()
        write_text_atomic(self.proc / "105/stat", "105 (fixture) S 100 105 100 1 103 " + "0 " * 14)
        write_text_atomic(self.proc / "106/stat", "106 (fixture) S 100 106 100 0 -1 " + "0 " * 14)
        write_text_atomic(self.proc / "108/stat", "108 (fixture) S 100 108 100 0 -1 " + "0 " * 14)
        daemon = codex_daemon.CodexDaemon(self.root / "account", self.root / "socket")
        arguments = herdr._process_arguments  # pyright: ignore[reportPrivateUsage]

        def read_arguments(path: Path) -> list[str]:
            if path.name in ("106", "107"):
                raise PermissionError("fixture argv")
            return arguments(path)

        def native(pid: int, _kind: str) -> herdr.NativeAgentProcess:
            if pid == 108:
                return herdr.NativeAgentProcess(herdr.LaunchArgument("TEST-old-thread"), "1", {})
            raise herdr_types.HerdrFailure("fixture unknown")

        def display(pid: int) -> str:
            return f"TEST-pane-{pid}"

        with (patch.object(codex_daemon, "read_daemons", return_value=[
                  codex_daemon.DaemonRead(daemon, codex_daemon.DaemonUnreadable("fixture"))]),
              patch.object(herdr, "_process_arguments", side_effect=read_arguments),
              patch.object(herdr, "native_process", side_effect=native),
              patch.object(herdr, "_process_display_name", side_effect=display),
              patch.object(herdr.Herdr, "result", side_effect=AssertionError("no server call"))):
            observed = herdr.codex_conversations()
        self.assertCountEqual(observed.unknown, [
            "TEST-pane-103 (Codex thread unknown)", "TEST-pane-104 (Codex thread unknown)",
            "TEST-pane-107 (process argv unknown)",
            f"{daemon.home} (Codex daemon unknown: fixture)"])

    def test_darwin_unknown_codex_requires_a_foreground_group_and_keeps_failed_reads(self) -> None:
        processes = [herdr._UserProcess(pid, 1, "codex", herdr.UnreadableProcessArguments("fixture"))  # pyright: ignore[reportPrivateUsage]
                     for pid in (101, 102, 103)]

        def read(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual(argv[1:4], ["-o", "pgid=,tpgid=", "-p"])
            self.assertGreater(timeout, 0)
            if argv[-1] == "103":
                raise OSError("fixture stat")
            return subprocess.CompletedProcess(argv, 0, "101 -1" if argv[-1] == "101" else "102 102", "")

        with (patch.object(herdr, "_PLATFORM", "darwin"),
              patch.object(herdr, "_darwin_processes", return_value=processes),
              patch.object(herdr, "run_program", side_effect=read),
              patch.object(codex_daemon, "read_daemons", return_value=[]),
              patch.object(herdr, "_process_display_name", side_effect=str)):
            observed = herdr.codex_conversations()
        self.assertEqual(observed.unknown, ("102 (Codex argv unknown)", "103 (Codex argv unknown)"))

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
            **{f"--session {name} pane list": {"stdout": json.dumps({"result": {"panes": panes}}), "code": 0}
               for name in names},
            **{f"--session {name} pane process-info --pane {pane['pane_id']}": pane_process_reply()
               for name in names for pane in panes},
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
        identity = (marks or {}).get("CLAUDE_CODE_SESSION_ID", fixture_runtime_id(f"pane-session-{pid}"))
        record = SessionRecord(pid=pid, sessionId=identity, name=name, messagingSocketPath="fake-socket",
                               updatedAt=updated, tmux="", formerNames=[])
        _ = (self.records / f"{pid}.json").write_text(json.dumps(record))

    def register_showrunner(self, pid: int) -> None:
        doc = self.root / "production.md"
        _ = doc.write_text("fake production")
        record = read_session(self.records / f"{pid}.json")
        assert not isinstance(record, UnreadableSessionRecord)
        identity = record["sessionId"] if staff.snapshot().by_current_session(record["sessionId"]) is None else ""
        owner = fixture_member("showrunner", name="fixture", doc=staff.LiveDoc("natedev", doc), runtime_id=identity)
        instance = self.state / f"showrunner-{owner.id}"
        instance.mkdir()
        _ = (instance / "conf").write_text(
            f"TARGET=session:{owner.id}\nCHECK=/fake/production_check.sh {owner.id}\n")

    def call(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = herdr_sessions.main(["herdr_sessions.py", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def commands(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [cast(list[str], json.loads(line)) for line in self.log.read_text().splitlines()]

    def test_list_prints_claude_name_after_the_pane_id_changes_on_move(self) -> None:
        self.respond(["agents"], tabs=("fix-rounds",))
        self.server(100, "agents")
        self.process(11, 100, ["zsh"], {"HERDR_PANE_ID": "wM:pM"})
        member = unit_membership("sdlc", "fix-rounds")
        self.claude(12, 11, "fix-rounds", pane="wM:pM", marks={"CLAUDE_CODE_SESSION_ID": member.current_session})
        replies = cast(dict[str, CommandResponse], json.loads(self.responses.read_text()))
        replies["--session agents pane list"] = {"code": 0, "stdout": json.dumps({"result": {"panes": [
            {"pane_id": "wM:pR", "workspace_id": "wM", "label": "fix-rounds"}]}})}
        replies["--session agents pane process-info --pane wM:pR"] = {
            "code": 0, "stdout": json.dumps({"result": {"process_info": {"shell_pid": 11}}})}
        replies["--session agents workspace list"] = {"code": 0, "stdout": json.dumps({"result": {
            "workspaces": [{"workspace_id": "wM", "label": "sdlc"}]}})}
        _ = self.responses.write_text(json.dumps(replies))
        self.assertEqual(self.call("list"), (0, "fix-rounds\tunit sdlc/fix-rounds\tfix-rounds\n", ""))

    def test_list_batches_claude_place_reads_for_all_session_records(self) -> None:
        self.respond(["agents"], tabs=("one", "two"))
        self.server(100, "agents")
        self.claude(101, 100, "first", pane="w1:p1")
        self.claude(102, 100, "newest", pane="w1:p1", updated=2)
        self.claude(103, 100, "second", pane="w1:p2")
        with patch.object(herdr, "herdr_places", wraps=herdr.herdr_places) as places, patch.object(
                herdr, "herdr_place_of", side_effect=AssertionError("list must use the batched place scan")):
            self.assertEqual(self.call("list"), (0, "one\tstandalone\tnewest\ntwo\tstandalone\tsecond\n", ""))
        self.assertEqual(places.call_count, 1)

    def test_list_prints_each_tab_of_the_shared_session_with_its_role_and_newest_live_claude_name(self) -> None:
        self.respond(["agents", "other"], tabs=("unit", "showrunner", "standalone", None))
        self.server(100, "agents")
        self.server(900, "other")
        parent = showrunner_relationship("build-followups")
        unit = fixture_member("unit", name="herdr-trial", parent=parent.id)
        marks = {('' + 'SHOWRUNNER_' + 'UNIT'): "build-followups", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "herdr-trial", "CLAUDE_CODE_SESSION_ID": unit.id}
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
        self.assertEqual(stdout, "unit\tunit build-followups/herdr-trial\tnew-unit-name\n"
                         + "showrunner\tshowrunner\tdirector\nstandalone\tstandalone\tsolo\n"
                         + "w1:p4\tstandalone\t\n")
        self.assertEqual(self.commands(), [["--session", "default", "session", "list", "--json"],
                                           ["--session", "agents", "pane", "list"],
                                           ["--session", "other", "pane", "list"]])

        owner = staff.snapshot().by_current_session(fixture_runtime_id("pane-session-201"))
        assert owner is not None
        _ = staff.rename(owner.id, "renamed-showrunner")
        self.assertIn("showrunner\tshowrunner\tdirector\n", self.call("list")[1])

    def test_partial_discovery_does_not_mislabel_an_unknown_showrunner_standalone(self) -> None:
        self.respond(["agents"], tabs=("unknown",))
        self.server(100, "agents")
        self.claude(101, 100, "director", pane="w1:p1")
        partial = showrunners.ShowrunnerDiscovery([], (
            showrunners.ShowrunnerLookupFailure("director", "fixture lookup failed"),))
        with patch.object(herdr_sessions, "registered_showrunners", return_value=partial):
            code, output, errors = self.call("list")
        self.assertEqual((code, output), (3, ""))
        self.assertIn("fixture lookup failed", errors)

    def test_unit_role_takes_precedence_over_showrunner(self) -> None:
        self.respond(["agents"], tabs=("unit",))
        self.server(100, "agents")
        unit = unit_membership("build", "one")
        self.claude(101, 100, "director", pane="w1:p1", marks={('' + 'SHOWRUNNER_' + 'UNIT'): "build", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "one",
                                                              "CLAUDE_CODE_SESSION_ID": unit.id})
        self.register_showrunner(101)
        self.assertEqual(self.call("list"), (0, "unit\tunit build/one\tdirector\n", ""))

    def test_staff_marks_are_visible_without_a_claude_record(self) -> None:
        self.respond(["agents"], tabs=("unit",))
        self.server(100, "agents")
        unit = unit_membership("build", "one")
        self.process(101, 100, ["codex", "resume", unit.id], {"HERDR_PANE_ID": "w1:p1", ('' + 'SHOWRUNNER_' + 'UNIT'): "build",
                                                ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "one", "CODEX_THREAD_ID": unit.id})
        (self.proc / "101" / "fd").mkdir()
        rollout = self.root / ("rollout-" + str(unit.current_session) + ".jsonl")
        _ = rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": unit.current_session}}) + "\n")
        (self.proc / "101/fd/4").symlink_to(rollout)
        with patch.object(runtime_sessions, "process_start", return_value="fixture-codex-start"):
            self.assertEqual(self.call("list"), (0, "unit\tunit build/one\t\n", ""))

    def test_an_inherited_codex_thread_id_keeps_the_shell_membership_evidence(self) -> None:
        self.respond(["agents"], tabs=("terminal",))
        self.server(100, "agents")
        unit = unit_membership("build", "one")
        self.process(101, 100, ["zsh"], {"HERDR_PANE_ID": "w1:p1", ('' + 'SHOWRUNNER_' + 'UNIT'): "build",
                                       ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "one", "CODEX_THREAD_ID": unit.id})
        self.assertEqual(self.call("list"), (0, "terminal\tunit build/one\t\n", ""))

    def test_list_classifies_unmarked_units_and_transferred_identity_from_one_staff_read(self) -> None:
        self.respond(["agents"], tabs=("herdr", "shutdown", "style", "former-name", "terminal"))
        self.server(100, "agents")
        for name in ("herdr", "shutdown", "style"):
            _ = unit_membership("sdlc", name)
        pinned = unit_membership("sdlc", "models")
        for pid, pane, name in ((101, "w1:p1", "herdr"), (102, "w1:p2", "shutdown"), (103, "w1:p3", "style")):
            member = unit_membership("sdlc", name)
            self.claude(pid, 100, name, pane=pane, marks={"CLAUDE_CODE_SESSION_ID": member.id})
        self.claude(104, 100, "models", pane="w1:p4", marks={
            "CLAUDE_CODE_SESSION_ID": pinned.id, ('' + 'SHOWRUNNER_' + 'UNIT'): "natedev", ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "old"})
        self.process(105, 100, ["sleep", "600"], {"HERDR_PANE_ID": "w1:p5", ('' + 'SHOWRUNNER_' + 'UNIT'): "sdlc",
                                                ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "missing"})
        replies = cast(dict[str, CommandResponse], json.loads(self.responses.read_text()))
        panes = [{"pane_id": f"w1:p{index}", "label": name, "workspace_id": "w1"}
                 for index, name in enumerate(("herdr", "shutdown", "style", "former-name", "terminal"), 1)]
        panes.append({"pane_id": "w2:p1", "label": "style", "workspace_id": "w2"})
        panes.append({"pane_id": "w1:unowned", "label": "style", "workspace_id": "w1"})
        replies["--session agents pane list"] = {"stdout": json.dumps({"result": {"panes": panes}}), "code": 0}
        replies["--session agents workspace list"] = {"stdout": json.dumps({"result": {"workspaces": [
            {"workspace_id": "w1", "label": "sdlc"}, {"workspace_id": "w2", "label": "natedev"}]}}), "code": 0}
        _ = self.responses.write_text(json.dumps(replies))

        with patch.object(staff, "snapshot", wraps=staff.snapshot) as read:
            self.assertEqual(self.call("list"), (0,
                "herdr\tunit sdlc/herdr\therdr\nshutdown\tunit sdlc/shutdown\tshutdown\n"
                + "style\tunit sdlc/style\tstyle\nformer-name\tunit sdlc/models\tmodels\n"
                + "terminal\tstandalone\t\nstyle\tstandalone\t\nstyle\tstandalone\t\n", ""))
        read.assert_called_once_with()

    def test_partial_staff_marks_do_not_make_a_unit_role(self) -> None:
        self.respond(["agents"], tabs=("solo",))
        self.server(100, "agents")
        self.claude(101, 100, "solo-name", pane="w1:p1", marks={('' + 'SHOWRUNNER_' + 'UNIT'): "build"})
        self.assertEqual(self.call("list"), (0, "solo\tstandalone\tsolo-name\n", ""))

    def test_sort_tabs_ignores_saved_order_saves_unit_groups_and_orders_split_tabs(self) -> None:
        from scripts.herdr.test_herdr import LayoutHerdr
        members = {name: unit_membership("boss", name) for name in ("mike", "zulu")}
        members.update({name: fixture_member("showrunner", name=name)
                        for name in ("boss", "apple", "split", "zebra", "alpha")})
        self.respond(["agents"])
        self.server(100, "agents")
        client = LayoutHerdr("agents")
        client.spaces = [("w1", "boss"), ("w2", "split")]
        for space, titles in (("w1", ("boss", "zulu", "apple", "mike")), ("w2", ("split", "zebra", "alpha"))):
            for title in titles:
                _ = client.add_tab(space, title)
        client.tabs.insert(5, {**client.tabs[4], "pane_id": "split-sibling"})
        before = {pane["pane_id"]: pane["tab_id"] for pane in client.tabs}
        for pid, pane in enumerate(client.tabs, 101):
            self.claude(pid, 100, pane["label"], pane=pane["pane_id"],
                        marks={"CLAUDE_CODE_SESSION_ID": pane["session_id"]})
        path = herdr.layout_path("agents")
        path.parent.mkdir(parents=True)
        _ = path.write_text(json.dumps(herdr.SavedLayout(at=100.0, workspaces=[herdr.SavedSpace(
            name="boss", seen=100.0, tabs=[herdr.SavedName(name=members[pane["label"]].id, seen=100.0)
                                         for pane in client.tabs[:4]])])))
        running = herdr.live_sessions()
        with (fake_unit_host.private_process_registry(), patch.object(herdr, "Herdr", return_value=client),
              patch.object(herdr, "live_sessions", return_value=running)):
            self.assertEqual(self.call("sort-tabs"), (0, "", ""))
        self.assertEqual(client.commands, [("tab.move", before["p8"], "1"), ("tab.move", before["p14"], "1")])
        self.assertEqual({pane["pane_id"]: pane["tab_id"] for pane in client.tabs}, before)
        saved = {space["name"]: [tab["name"] for tab in space["tabs"]] for space in herdr.saved_layout("agents")["workspaces"]}
        self.assertEqual(saved, {space: [members[title].id for title in titles] for space, titles in
                                 {"boss": ["boss", "mike", "zulu", "apple"], "split": ["split", "alpha", "zebra"]}.items()})

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

    def test_remote_attach_exec_failure_names_its_host(self) -> None:
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(["ssh"], 0, "agents\n", "")), \
                patch.object(os, "execvp", side_effect=OSError("attach failed")):
            self.assertEqual(self.call("attach-remote", "mac", "writing"),
                             (1, "", "herdr_sessions: mac: attach failed: attach failed\n"))
        self.assertEqual(self.commands(), [])

    def test_remote_attach_never_executes_after_a_failed_or_empty_lookup(self) -> None:
        for status, output, diagnostic in (
                (1, "agents\n", "remote focus failed"),
                (0, " \n", "remote session is unknown")):
            with (self.subTest(status=status),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                      ["ssh"], status, output, "remote focus failed")),
                  patch.object(os, "execvp") as execute):
                self.assertEqual(self.call("attach-remote", "mac", "writing"),
                                 (1, "", f"herdr_sessions: mac: {diagnostic}\n"))
                execute.assert_not_called()
        self.assertEqual(self.commands(), [])

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

    def test_attach_finds_bare_binary_on_path(self) -> None:
        self.respond(["agents"], tabs=("target",))
        fake = self.root / "herdr"
        source = Path(__file__).with_name("fake_herdr.py").read_text()
        _ = fake.write_text("#!" + sys.executable + "\n" + source.split("\n", 1)[1])
        fake.chmod(0o755)
        with patch.dict(os.environ, {"HERDR_BIN": "herdr", "PATH": str(self.root)}):
            result = subprocess.run(
                [sys.executable, str(Path(herdr_sessions.__file__)), "attach", "target"],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(self.commands(), [
            ["--session", "default", "session", "list", "--json"],
            ["--session", "agents", "pane", "list"], ["--session", "agents", "pane", "list"],
            *self.focused("w1:t1"),
            ["session", "attach", "agents"],
        ])

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
                (herdr_sessions, "claude_sessions", ("list",)),
                (herdr_sessions, "unit_roles", ("list",)),
                (herdr_sessions, "registered_showrunners", ("list",)),
                (os, "execvp", ("attach", "target"))):
            with self.subTest(operation=operation), patch.object(owner, operation, side_effect=KeyboardInterrupt):
                self.assertEqual(self.call(*arguments), (130, "", "herdr_sessions: stopped\n"))

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
        (home / ".claude/sessions").mkdir(parents=True)
        (self.root / "sessions").mkdir()
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(home), "XDG_CONFIG_HOME": str(self.root / "config"), "PATH": os.environ.get("PATH", os.defpath),
            "HERDR_BIN": str(Path(__file__).with_name("fake_herdr.py")),
            "SYSTEMD_RUN": str(Path(__file__).with_name("fake_herdr.py")),
            "SYSTEMCTL": str(Path(__file__).with_name("fake_herdr.py")),
            "HERDR_PROC": str(self.proc), "FAKE_HERDR_LOG": str(self.log),
            "FAKE_HERDR_RESPONSES": str(self.responses_path),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
        }, clear=True))
        _ = self.enterContext(fake_unit_host.private_process_registry())
        _ = self.enterContext(patch.object(herdr.Herdr, "run_in_pane"))
        configuration = self.enterContext(scratch_accounts_config())
        _ = (configuration / "script_prompts.conf").write_text(
            (Path(__file__).resolve().parents[2] / "config/script_prompts.conf").read_text())
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {"CLAUDE_CONFIG_DIR": ""}))
        self.responses = {"default": {"stdout": json.dumps({"result": {
            "workspaces": [], "root_pane": {"pane_id": "w1:p1"}, "tab": {"tab_id": "w1:t1"}, "tabs": [], "panes": [],
        }}), "code": 0}}
        _ = (home / ".claude.json").write_text(json.dumps({"projects": {
            str(self.root): {"hasTrustDialogAccepted": True},
        }, "oauthAccount": {"emailAddress": "legacy@example.invalid"}}))
        self.save()
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
        for pane in [*before, opened]:
            self.responses[f"--session {name} pane process-info --pane {pane['pane_id']}"] = pane_process_reply()
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
        """Open one tab in its named workspace within the shared session."""
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertEqual(workspace[:6], ["--session", "agents", "workspace", "create", "--label", title])
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


    def test_launch_server_failure_prints_only_its_message(self) -> None:
        message = f"herdr server for agents did not start; see {self.root}/herdr-server.log"
        with patch.object(herdr, "start_server", side_effect=herdr_types.HerdrFailure(message)):
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)),
                             (1, f"herdr_sessions: {message}\n"))
        self.assertEqual(self.attachments, [])
        self.assertEqual(self.commands(), [])

    def test_start_after_cutover_uses_showrunners_account_for_pane(self) -> None:
        directory = self.numbered_account()
        groups = accounts.groups_file()
        groups.parent.mkdir(parents=True, exist_ok=True)
        _ = groups.write_text(json.dumps({"showrunners": "claude4"}))
        _ = (Path.home() / ".claude.json").write_text("{}")
        self.destination()
        self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)), (0, ""))
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertIn(f"CLAUDE_CODE_REMOTE_MEMORY_DIR={Path.home() / '.claude'}", workspace)
        self.assertEqual(len(self.agent_starts()), 1)

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

    def test_start_and_resume_reruns_open_no_second_tab(self) -> None:
        self.destination()
        for verb in ("start", "resume"):
            with self.subTest(verb=verb):
                title = "solo-" + verb
                member = fixture_member("showrunner", name=title)
                pane_reads = self.commands().count(["--session", "agents", "pane", "list"])
                opened: dict[str, object] = {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1",
                                             "label": title, "agent_session": {"value": member.current_session}}
                pane_lists: list[list[dict[str, object]]] = [list[dict[str, object]]() for _ in range(pane_reads + 1)] + [[opened]]
                self.responses["--session agents pane list"] = [
                    CommandResponse(code=0, stdout=json.dumps({"result": {"panes": panes}}))
                    for panes in pane_lists]
                self.responses["--session agents pane process-info --pane w1:p1"] = pane_process_reply(member.current_session)
                self.save()
                arguments = (("start", "--name", title, "--cwd", str(self.root)) if verb == "start" else
                             ("resume", title, "--session-id", member.current_session, "--cwd", str(self.root)))
                starts_before = len(self.agent_starts())
                self.assertEqual(self.call(*arguments), (0, ""))
                workspace = [command for command in self.commands() if command[2:4] == ["workspace", "create"]][-1]
                self.assertEqual(workspace[5], title)
                self.responses["--session agents pane list"] = CommandResponse(code=0, stdout=json.dumps({
                    "result": {"panes": [opened]}}))
                self.save()
                before = self.commands()
                with patch.object(runtime_sessions, "session_liveness",
                                  return_value=runtime_sessions.LiveSessionProcess(998043)):
                    self.assertEqual(self.call(*arguments),
                        (1, f"herdr_sessions: would start a second copy of running session {member.current_session}\n"))
                added = self.commands()[len(before):]
                self.assertFalse(any(command[2:4] in (["workspace", "create"], ["tab", "create"], ["agent", "start"])
                                     for command in added))
                self.assertEqual(len(self.agent_starts()), starts_before + 1)

    def test_start_inherits_named_account_for_pane(self) -> None:
        directory = self.numbered_account()
        self.destination()
        with (patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(directory), "CLAUDE_CODE_OAUTH_TOKEN": "unrelated"})):
            self.assertEqual(self.call("start", "--name", "solo", "--cwd", str(self.root)), (0, ""))
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

    def test_resume_named_account_sends_multiline_file_once_and_preserves_arguments(self) -> None:
        _ = self.enterContext(patch.object(herdr.Herdr, "native_session", return_value="recorded-id"))
        directory = self.numbered_account()
        prompt = self.root / "prompt"
        _ = prompt.write_text("first line\nsecond line\n")
        self.destination()
        transcript = directory / "projects/fixture/recorded-id.jsonl"
        self.responses["--session agents pane process-info --pane w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"process_info": {"shell_pid": 100, "foreground_process_group_id": 101,
                                        "foreground_processes": [{"pid": 101, "name": "claude", "argv": ["claude"]}]}}}))
        def native_process(pid: int, kind: str) -> herdr.NativeAgentProcess:
            self.assertEqual((pid, kind), (101, "claude"))
            return herdr.NativeAgentProcess(herdr.EnvironmentThread("recorded-id"), "fixture-start", {})
        _ = self.enterContext(patch.object(herdr, "native_process", side_effect=native_process))
        self.responses["--session agents agent get w1:p1"] = CommandResponse(code=0, stdout=json.dumps({
            "result": {"agent": {"agent_status": "idle"}}}))
        self.responses["--session agents pane read w1:p1 --source visible --format ansi"] = CommandResponse(
            code=0, stdout="────────\n❯ \n────────")
        # This fixture turn is written by the fake command, after launch, not before it.
        original_prompt = herdr.Herdr._prompt  # pyright: ignore[reportPrivateUsage]

        def submitted(client: herdr.Herdr, pane: str, text: str) -> None:
            original_prompt(client, pane, text)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            _ = transcript.write_text(json.dumps({"type": "user", "timestamp": datetime.now(timezone.utc).isoformat(),
                "message": {"role": "user", "content": text}}) + "\n")

        self.save()
        _ = self.enterContext(patch.object(herdr.Herdr, "_prompt", submitted))
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
        self.assertEqual(prompts, [["--session", "agents", "agent", "prompt", "w1:p1", script_prompt("first line\nsecond line\n")]])
        self.tab_opened("solo")
        workspace, = [command for command in self.commands() if command[2:4] == ["workspace", "create"]]
        self.assertIn(f"CLAUDE_CONFIG_DIR={directory}", workspace)
        self.assertIn(f"CLAUDE_CODE_REMOTE_MEMORY_DIR={self.root / 'home/.claude'}", workspace)
        self.assertIn("HERDR_AGENT_PANE=1", workspace)

    def test_resume_without_prompt_or_attach_leaves_user_input_untouched(self) -> None:
        self.destination()
        with patch.object(os, "execvp") as execute:
            self.assertEqual(self.call("resume", "solo", "--session-id", "recorded", "--cwd", str(self.root)), (0, ""))
        execute.assert_not_called()
        self.assertEqual(len(self.agent_starts()), 1)
        self.assertFalse(any(command[2:4] == ["agent", "prompt"] for command in self.commands()))



if __name__ == "__main__":
    _ = unittest.main()
