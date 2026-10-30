#!/usr/bin/env python3
"""Test roster membership and one socket from the newest record per conversation."""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from contextlib import AbstractContextManager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

from scripts.lib import process_start as process_identity
from typing import cast, override
from unittest import mock

import process_table
import codex_daemon
import roster
import herdr
import sessions
import top_level

# Hang guards allow for load 136 on natedev (2026-10-29).
TEST_HANG_GUARD_SECS = 300

NO_SHOWRUNNER = roster.NoShowrunner()
NO_DIRECTOR = roster.NoDirector()


class TopLevelTests(unittest.TestCase):
    folder: Path = Path()
    rows: list[roster.RosterEntry] = []
    records: list[sessions.SessionRecord] = []
    problems: list[str] = []

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = (self.folder / "ps").write_text("", encoding="utf-8")
        binaries = self.folder / "bin"
        binaries.mkdir()
        ssh = binaries / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n"
            + "import os, sys\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(repr(sys.argv[1:]) + '\\n')\n"
            + "raise SystemExit(91)\n", encoding="utf-8")
        ssh.chmod(0o755)
        environment_context = cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "HOME": str(self.folder), "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
            "SSH_RECORD": str(self.folder / "ssh.log"),
            "NOTIFIER_SESSIONS_DIR": str(self.folder),
            "CLAUDE_CODE_SESSION_ID": "current-id", "CODEX_THREAD_ID": "",
            "BUILD_HOLD_DIR": str(self.folder / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.folder / "release"),
            "ROSTER_RUN_ROOT": str(self.folder / "runs"),
            "ROSTER_CODEX_SOCKET": str(self.folder / "no-daemon.sock"),
            "PROCESS_TABLE_PS": f"cat {self.folder / 'ps'}",
        }, clear=True))
        _ = self.enterContext(environment_context)
        self.rows, self.records, self.problems = [], [], []
        _ = self.enterContext(mock.patch.object(roster, "roster", side_effect=self.fixture_roster))
        _ = self.enterContext(mock.patch.object(sessions, "running_sessions", side_effect=self.fixture_records))
        _ = self.enterContext(mock.patch.object(sessions, "messaging_socket_exists", side_effect=self.fixture_socket_exists))
        _ = self.enterContext(mock.patch.object(process_table, "PS", ["cat", str(self.folder / "ps")]))

    def fixture_roster(self, _environment: Mapping[str, str], scope: roster.RosterScope) -> roster.Roster:
        self.assertEqual(scope, roster.ThisMachine())
        return roster.Roster(self.rows, self.problems)

    def fixture_records(self) -> list[sessions.SessionRecord]:
        return sorted(self.records, key=lambda record: record["updatedAt"], reverse=True)

    def fixture_socket_exists(self, record: sessions.SessionRecord) -> bool:
        return bool(record["messagingSocketPath"])

    def add(
        self, name: str, *, session_id: str = "", file_name: str = "",
        socket: str | None = "auto", pid: int = 12345,
        tmux: str = "", updated: int = 1,
        role: roster.Role = "freestanding",
        showrunner: roster.ShowrunnerRelationship = NO_SHOWRUNNER,
        director: roster.DirectorRelationship = NO_DIRECTOR,
    ) -> str:
        session_id = session_id or name
        address = str(self.folder / f"{file_name or name}.sock") if socket == "auto" else socket or ""
        self.records.append(sessions.SessionRecord(
            pid=pid, name=name, sessionId=session_id, messagingSocketPath=address,
            tmux=tmux, updatedAt=updated, formerNames=[],
        ))
        row = roster.RosterEntry(
            machine="fixture", kind="claude", role=role, name=name, status="working",
            reach=roster.Messageable(f"session:{session_id}"), cwd="/fixture",
            showrunner=showrunner, director=director,
            assignment=roster.WorkerAssignment("test", "/fixture/run") if role == "worker" else roster.NotAWorker(),
            thread=roster.NoCodexThread(),
            session=roster.ClaudeSession(session_id),
        )
        if not any(existing.reach == row.reach for existing in self.rows):
            self.rows.append(row)
        return address

    def run_main(self, *argv: str, record_exit: int = 0) -> tuple[int, list[str], list[str], list[tuple[str, str]]]:
        output, errors = io.StringIO(), io.StringIO()
        recorded: list[tuple[str, str]] = []

        def record(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
            self.assertEqual(Path(command[1]).name, "build_hold.py")
            self.assertEqual(command[2], "record-recipient")
            recorded.append((command[command.index("--name") + 1], command[command.index("--session-id") + 1]))
            return subprocess.CompletedProcess(command, record_exit, b"", b"")

        with (mock.patch.object(subprocess, "run", new=record),
              redirect_stdout(output), redirect_stderr(errors)):
            result = top_level.main(list(argv))
        return result, output.getvalue().splitlines(), errors.getvalue().splitlines(), recorded

    def test_top_level_reads_this_machine_and_makes_no_ssh_call(self) -> None:
        address = self.add("local", session_id="local-id")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"local\tuds:{address}"])
        self.assertEqual(recorded, [("local", "local-id")])
        self.assertFalse((self.folder / "ssh.log").exists())

    def test_two_sessions_with_one_name_have_distinct_addresses_and_records(self) -> None:
        first = self.add("shared", session_id="session-a", file_name="first")
        second = self.add("shared", session_id="session-b", file_name="second")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"shared\tuds:{first}", f"shared\tuds:{second}"])
        self.assertEqual(recorded, [("shared", "session-a"), ("shared", "session-b")])

    def test_a_conversation_two_processes_hold_is_listed_once_from_its_newest_record(self) -> None:
        _ = self.add("shared", session_id="one-id", file_name="desktop", pid=100, updated=100)
        resumed = self.add("shared", session_id="one-id", file_name="terminal", pid=200, updated=200)
        _ = self.add("shared", session_id="one-id", file_name="zz-older", pid=300, updated=150)
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"shared\tuds:{resumed}"])
        self.assertEqual(recorded, [("shared", "one-id")])

    def test_sorting_uses_name_then_address(self) -> None:
        zulu = self.add("zulu", socket="/tmp/zulu.sock")
        second = self.add("shared", session_id="b", file_name="first", socket="/tmp/b.sock")
        first = self.add("shared", session_id="a", file_name="second", socket="/tmp/a.sock")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"shared\tuds:{first}", f"shared\tuds:{second}", f"zulu\tuds:{zulu}"])
        self.assertEqual(set(recorded), {("shared", "a"), ("shared", "b"), ("zulu", "zulu")})

    def test_unaddressable_session_is_named_but_not_recorded(self) -> None:
        _ = self.add("missing", session_id="missing-id", socket=None)
        _ = self.add("empty", session_id="empty-id", socket="")
        address = self.add("reachable", session_id="reachable-id")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(lines, [f"reachable\tuds:{address}"])
        self.assertEqual(errors, [
            "not reachable: empty [empty-id] has no messaging socket",
            "not reachable: missing [missing-id] has no messaging socket",
        ])
        self.assertEqual(recorded, [("reachable", "reachable-id")])

    def test_session_types_name_addressability_at_read_boundary(self) -> None:
        address = self.add("reachable", session_id="reachable-id")
        _ = self.add("missing", session_id="missing-id", socket=None)
        forwarded = top_level.forwarded_sessions()
        self.assertEqual(len(forwarded), 2)
        reachable = next(session for session in forwarded if session.name == "reachable")
        missing = next(session for session in forwarded if session.name == "missing")
        self.assertIsInstance(reachable, top_level.AddressableSession)
        assert isinstance(reachable, top_level.AddressableSession)
        self.assertEqual((reachable.session_id, reachable.address), ("reachable-id", f"uds:{address}"))
        self.assertIsInstance(missing, top_level.UnaddressableSession)
        self.assertEqual(missing.session_id, "missing-id")

    def test_own_id_is_left_out_while_another_session_with_its_name_is_sent(self) -> None:
        _ = self.add("shared", session_id="current-id", file_name="current")
        other = self.add("shared", session_id="other-id", file_name="other")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"shared\tuds:{other}"])
        self.assertEqual(recorded, [("shared", "other-id")])

    def test_plain_shell_leaves_nobody_out(self) -> None:
        address = self.add("reachable")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"reachable\tuds:{address}"])
        self.assertEqual(recorded, [("reachable", "reachable")])

    def test_unit_directors_under_showrunners_and_all_workers_are_left_out(self) -> None:
        address = self.add("showrunner", role="showrunner", showrunner=roster.RunsProduction("production"))
        other = self.add("ordinary")
        solo = self.add("solo", role="unit director", director=roster.DirectsUnit("solo"))
        orphaned = self.add("orphaned unit", role="unit director", director=roster.DirectsUnit("orphan"),
                            showrunner=roster.ProductionWithoutRunningShowrunner("gone"))
        _ = self.add("trunk", role="unit director", director=roster.DirectsUnit("trunk"),
                     showrunner=roster.UnderShowrunner("production", "showrunner", "session:showrunner"))
        _ = self.add("worker", role="worker", director=roster.UnderDirector("trunk", "trunk", "session:trunk"))
        _ = self.add("orphan worker", role="worker")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(set(lines), {f"showrunner\tuds:{address}", f"ordinary\tuds:{other}",
                                     f"solo\tuds:{solo}", f"orphaned unit\tuds:{orphaned}"})
        self.assertEqual({session_id for _, session_id in recorded}, {"showrunner", "ordinary", "solo", "orphaned unit"})

    def test_stale_tmux_names_do_not_override_roster_unit_identity(self) -> None:
        _ = self.add("renamed unit", tmux="old-name:@3.%7", role="unit director",
                     showrunner=roster.UnderShowrunner("production", "runner", "session:runner"))
        ordinary = self.add("ordinary", tmux="unit-looking-name:@1.%1")
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"ordinary\tuds:{ordinary}"])
        self.assertEqual(recorded, [("ordinary", "ordinary")])

    def test_records_absent_from_the_live_roster_are_left_out(self) -> None:
        _ = self.add("ended", pid=2**22 + 1)
        _ = self.add("reused", pid=12345)
        self.rows = []
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, lines, errors, recorded), (0, [], [], []))

    def test_failed_recipient_record_returns_one(self) -> None:
        _ = self.add("reachable", session_id="reachable-id")
        result, lines, errors, recorded = self.run_main(record_exit=1)
        self.assertEqual((result, lines), (1, []))
        self.assertEqual(errors, ["could not record recipient reachable [reachable-id]"])
        self.assertEqual(recorded, [("reachable", "reachable-id")])

    def test_self_argument_is_no_longer_accepted(self) -> None:
        _ = self.add("reachable")
        result, lines, _errors, recorded = self.run_main("--self", "current")
        self.assertEqual((result, lines, recorded), (2, [], []))

    def test_a_name_prefix_cannot_make_a_freestanding_session_a_unit(self) -> None:
        address = self.add("unit")
        _ = self.add("unit-trunk", role="unit director",
                     showrunner=roster.UnderShowrunner("production", "runner", "session:runner"))
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"unit\tuds:{address}"])
        self.assertEqual(recorded, [("unit", "unit")])

    def test_roster_problem_prints_on_stderr_while_readable_rows_still_print(self) -> None:
        address = self.add("reachable", session_id="reachable-id")
        self.problems = ["daemon closed mid-call"]
        result, lines, errors, recorded = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(lines, [f"reachable\tuds:{address}"])
        self.assertEqual(errors, ["top_level: roster: daemon closed mid-call"])
        self.assertEqual(recorded, [("reachable", "reachable-id")])

    def test_codex_and_non_session_addresses_are_not_forwarded_to_sendmessage(self) -> None:
        _ = self.add("Codex window")
        self.rows[0] = replace(self.rows[0], kind="codex", reach=roster.Messageable("codex:thread"),
                               thread=roster.CodexThread("thread"))
        result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, lines, errors, recorded), (0, [], [], []))

    def test_codex_identity_does_not_exclude_its_inherited_claude_parent(self) -> None:
        address = self.add("parent", session_id="current-id")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "codex-child"}):
            result, lines, errors, recorded = self.run_main()
        self.assertEqual((result, errors), (0, []))
        self.assertEqual(lines, [f"parent\tuds:{address}"])
        self.assertEqual(recorded, [("parent", "current-id")])


class TopLevelRunningRecordTests(unittest.TestCase):
    folder: Path = Path()
    directory: Path = Path()
    socket_path: Path = Path()
    child_pid: int = 0
    process_starts: dict[int, str] = {}

    @override
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="tl-", dir="/tmp")))
        self.directory = self.folder / "sessions"
        self.directory.mkdir()
        binaries = self.folder / "bin"
        binaries.mkdir()
        ssh = binaries / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n"
            + "import os, sys\n"
            + "with open(os.environ['SSH_RECORD'], 'a') as log: log.write(repr(sys.argv[1:]) + '\\n')\n"
            + "raise SystemExit(91)\n", encoding="utf-8")
        ssh.chmod(0o755)
        self.socket_path = self.folder / "live.sock"
        listener = self.enterContext(socket.socket(socket.AF_UNIX))
        listener.bind(str(self.socket_path))
        child = self.enterContext(subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ))
        self.child_pid = child.pid
        self.process_starts = {}
        self.addCleanup(child.communicate, timeout=TEST_HANG_GUARD_SECS)
        environment = cast(AbstractContextManager[object], mock.patch.dict(os.environ, {
            "HOME": str(self.folder), "NOTIFIER_SESSIONS_DIR": str(self.directory),
            "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}", "SSH_RECORD": str(self.folder / "ssh.log"),
            "NOTIFIER_STATE_DIR": str(self.folder / "notifier"),
            "ROSTER_RUN_ROOT": str(self.folder / "runs"),
            "ROSTER_CODEX_SOCKET": str(self.folder / "no-daemon.sock"),
        }, clear=True))
        _ = self.enterContext(environment)
        _ = self.enterContext(mock.patch.object(process_table, "snapshot", return_value={}))
        _ = self.enterContext(mock.patch.object(codex_daemon, "loaded_sessions", return_value=[]))
        # The roster lists herdr panes; a live herdr on the test machine would reach the real server.
        _ = self.enterContext(mock.patch.object(herdr, "herdr_binary", side_effect=herdr.HerdrMissing("herdr is not installed")))

    def write_record(self, pid: int, *, socket_path: str, updated: int, proc_start: str = "") -> None:
        started = process_identity.process_start(pid)
        assert not isinstance(started, process_identity.ProcessGone)
        self.process_starts[pid] = started.text
        record = sessions.SessionRecord(
            pid=pid, sessionId="shared-id", name="shared", messagingSocketPath=socket_path,
            updatedAt=updated, tmux="", formerNames=[], procStart=proc_start or started.text,
        )
        _ = (self.directory / f"{pid}.json").write_text(json.dumps(record), encoding="utf-8")

    def run_main(self) -> tuple[int, str, str, list[list[str]]]:
        output, errors = io.StringIO(), io.StringIO()
        recorded: list[list[str]] = []

        def record(
            command: list[str] | tuple[str, ...], **_options: object,
        ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
            if tuple(command[:3]) == ("ps", "-o", "lstart="):
                return subprocess.CompletedProcess(command, 0, self.process_starts[int(command[-1])] + "\n", "")
            if tuple(command[:3]) == ("ps", "-o", "stat="):
                return subprocess.CompletedProcess(command, 0, "S\n", "")
            self.assertEqual(Path(command[1]).name, "build_hold.py")
            self.assertEqual(command[2], "record-recipient")
            recorded.append(list(command))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with mock.patch.object(subprocess, "run", new=record), redirect_stdout(output), redirect_stderr(errors):
            result = top_level.main([])
        return result, output.getvalue(), errors.getvalue(), recorded

    def test_newest_running_record_without_socket_never_forwards_an_older_socket(self) -> None:
        self.write_record(os.getpid(), socket_path=str(self.socket_path), updated=10)
        self.write_record(self.child_pid, socket_path="", updated=20)
        self.assertEqual(self.run_main(), (
            0, "", "not reachable: shared [shared-id] has no messaging socket\n", [],
        ))

    def test_socketless_record_with_another_process_start_is_not_listed(self) -> None:
        self.write_record(os.getpid(), socket_path="", updated=10, proc_start="another start")
        self.assertEqual(self.run_main(), (0, "", "", []))

    def test_running_session_without_socket_is_reported_and_not_recorded(self) -> None:
        self.write_record(os.getpid(), socket_path="", updated=10)
        self.assertEqual(self.run_main(), (
            0, "", "not reachable: shared [shared-id] has no messaging socket\n", [],
        ))

    def test_sender_without_socket_is_left_out_by_its_session_identity(self) -> None:
        self.write_record(os.getpid(), socket_path=str(self.socket_path), updated=10)
        self.write_record(self.child_pid, socket_path="", updated=20)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "shared-id"}):
            found = roster.roster(os.environ, roster.ThisMachine())
            self.assertEqual(len(found.entries), 1)
            self.assertEqual(found.entries[0].reach, roster.CannotReceiveMessages("no messaging socket"))
            self.assertTrue(roster.is_sender(found.entries[0], roster.sender_identity(os.environ)))
            self.assertEqual(self.run_main(), (0, "", "", []))

    def test_newest_running_record_with_missing_socket_path_is_reported(self) -> None:
        self.write_record(os.getpid(), socket_path=str(self.socket_path), updated=10)
        self.write_record(self.child_pid, socket_path=str(self.folder / "missing.sock"), updated=20)
        self.assertEqual(self.run_main(), (
            0, "", "not reachable: shared [shared-id] has no messaging socket\n", [],
        ))


if __name__ == "__main__":
    _ = unittest.main()
